#!/usr/bin/env python3
"""Shared timestamp-log to calibrated-camera frame range mapping.

The calibrated timeline deliberately retains every original source frame.  This
module selects the subset belonging to recorded demonstration segments without
renumbering frames, so `_03` and `_05` can use exactly the same half-open range
semantics.
"""

from __future__ import annotations

import bisect
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path


def parse_csv_bool(value: object, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean CSV value: {value!r}")


@dataclass(frozen=True)
class CalibratedFrameTimeline:
    path: Path
    frame_idx: tuple[int, ...]
    unix_ms: tuple[float, ...]
    valid: tuple[bool, ...]
    unix_column: str
    validity_columns: tuple[str, ...]
    calibration_id: str | None
    clock_domain: str = "unix_realtime"

    @property
    def valid_indices(self) -> tuple[int, ...]:
        return tuple(index for index, keep in enumerate(self.valid) if keep)


@dataclass(frozen=True)
class TimestampSegment:
    segment_index: int
    start_unix_time_ms: float
    end_unix_time_ms: float


@dataclass(frozen=True)
class ActiveFrameRange:
    segment_index: int
    start_unix_time_ms: float
    end_unix_time_ms: float
    start_frame: int
    end_frame: int
    frame_indices: tuple[int, ...]

    def as_dict(self) -> dict:
        return {
            "segment_index": self.segment_index,
            "start_unix_time_ms": self.start_unix_time_ms,
            "end_unix_time_ms": self.end_unix_time_ms,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timeline_valid_frames": len(self.frame_indices),
        }


def load_calibrated_frame_timeline(path: Path) -> CalibratedFrameTimeline:
    resolved = path.expanduser().resolve()
    frame_indices: list[int] = []
    unix_values_ms: list[float] = []
    valid_values: list[bool] = []
    calibration_ids: set[str] = set()
    with resolved.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        index_column = next(
            (name for name in ("frame_idx", "original_concat_frame_idx") if name in fields),
            None,
        )
        if index_column is None:
            raise ValueError(f"{resolved} is missing frame_idx/original_concat_frame_idx")
        unix_column = next(
            (
                name
                for name in (
                    "calibrated_host_monotonic_raw_ns",
                    "calibrated_host_monotonic_raw_sec",
                    "unix_time_ms",
                    "calibrated_host_unix_ns",
                    "calibrated_host_unix_sec",
                    "host_unix_time_ns",
                )
                if name in fields
            ),
            None,
        )
        if unix_column is None:
            raise ValueError(f"{resolved} has no supported calibrated time column")
        clock_domain = (
            "host_clock_monotonic_raw"
            if "monotonic_raw" in unix_column
            else "unix_realtime"
        )
        validity_columns = tuple(
            name for name in ("timeline_valid", "recommended_keep") if name in fields
        )
        for row_number, row in enumerate(reader, start=2):
            try:
                frame_index = int(row[index_column])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{resolved}:{row_number} has invalid {index_column}") from exc
            raw_time = row.get(unix_column)
            try:
                if raw_time is None or str(raw_time).strip() == "":
                    unix_ms = float("nan")
                elif unix_column.endswith("_ns"):
                    unix_ms = float(raw_time) / 1e6
                elif unix_column.endswith("_sec"):
                    unix_ms = float(raw_time) * 1000.0
                else:
                    unix_ms = float(raw_time)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{resolved}:{row_number} has invalid {unix_column}") from exc
            is_valid = all(parse_csv_bool(row.get(name), default=True) for name in validity_columns)
            if is_valid and not math.isfinite(unix_ms):
                raise ValueError(f"{resolved}:{row_number} is timeline-valid without a finite time")
            frame_indices.append(frame_index)
            unix_values_ms.append(unix_ms)
            valid_values.append(is_valid)
            if "calibration_id" in fields:
                calibration_id = str(row.get("calibration_id") or "").strip()
                if not calibration_id:
                    raise ValueError(f"{resolved}:{row_number} has an empty calibration_id")
                calibration_ids.add(calibration_id)

    if not frame_indices:
        raise ValueError(f"empty calibrated frame timeline: {resolved}")
    expected = list(range(len(frame_indices)))
    if frame_indices != expected:
        mismatch = next(
            index for index, (actual, wanted) in enumerate(zip(frame_indices, expected)) if actual != wanted
        )
        raise ValueError(
            f"{resolved} must retain every source frame: row={mismatch}, "
            f"frame_idx={frame_indices[mismatch]}"
        )
    if len(calibration_ids) > 1:
        raise ValueError(f"{resolved} contains multiple calibration_id values: {sorted(calibration_ids)}")
    valid_indices = [index for index, keep in enumerate(valid_values) if keep]
    if len(valid_indices) < 2:
        raise ValueError(f"{resolved} has fewer than two timeline-valid frames")
    valid_times = [unix_values_ms[index] for index in valid_indices]
    for position, (before, after) in enumerate(zip(valid_times, valid_times[1:])):
        if after <= before:
            raise ValueError(
                f"{resolved} valid timeline is not strictly increasing between original frames "
                f"{valid_indices[position]} and {valid_indices[position + 1]}"
            )
    return CalibratedFrameTimeline(
        path=resolved,
        frame_idx=tuple(frame_indices),
        unix_ms=tuple(unix_values_ms),
        valid=tuple(valid_values),
        unix_column=unix_column,
        validity_columns=validity_columns,
        calibration_id=next(iter(calibration_ids), None),
        clock_domain=clock_domain,
    )


def load_timestamp_segments(
    log_path: Path,
    segment_time_offset_sec: float = 0.0,
    *,
    clock_domain: str = "unix_realtime",
) -> list[TimestampSegment]:
    resolved = log_path.expanduser().resolve()
    starts: dict[int, dict] = {}
    ranges: list[TimestampSegment] = []
    for line_number, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{resolved}:{line_number} is not valid JSON") from exc
        segment_index = row.get("segmentIndex")
        if segment_index is None:
            continue
        segment_index = int(segment_index)
        event_type = row.get("type")
        if event_type == "segment_start":
            if segment_index in starts:
                raise ValueError(f"duplicate open segment_start for segment={segment_index}")
            starts[segment_index] = row
        elif event_type == "segment_end":
            start = starts.pop(segment_index, None)
            if start is None:
                continue
            if clock_domain == "host_clock_monotonic_raw":
                def strict_raw_ms(event: dict, label: str) -> float:
                    raw_ns = event.get("monotonic_raw_time_ns")
                    mono_ns = event.get("monotonic_time_ns") or event.get("monotonicTimeNs")
                    snapshot = event.get("host_clock_snapshot")
                    if raw_ns is None or mono_ns is None or not isinstance(snapshot, dict):
                        raise ValueError(
                            f"{resolved} {label} segment={segment_index} lacks the strict "
                            "monotonic_raw/monotonic/snapshot clock tuple"
                        )
                    if snapshot.get("monotonic_raw_ns") is None or snapshot.get("monotonic_ns") is None:
                        raise ValueError(
                            f"{resolved} {label} segment={segment_index} has an incomplete host clock snapshot"
                        )
                    event_offset_ns = int(mono_ns) - int(raw_ns)
                    snapshot_offset_ns = int(snapshot["monotonic_ns"]) - int(snapshot["monotonic_raw_ns"])
                    if abs(event_offset_ns - snapshot_offset_ns) > 100_000:
                        raise ValueError(
                            f"{resolved} {label} segment={segment_index} has inconsistent "
                            "MONOTONIC-to-RAW evidence (>0.1ms)"
                        )
                    return int(raw_ns) / 1e6

                raw_start_ms = strict_raw_ms(start, "segment_start")
                raw_end_ms = strict_raw_ms(row, "segment_end")
            elif clock_domain == "unix_realtime":
                raw_start_ms = float(start["unixTimeMs"])
                raw_end_ms = float(row["unixTimeMs"])
            else:
                raise ValueError(f"unsupported timestamp clock domain: {clock_domain}")
            offset_ms = float(segment_time_offset_sec) * 1000.0
            start_ms = raw_start_ms + offset_ms
            end_ms = raw_end_ms + offset_ms
            if raw_end_ms <= raw_start_ms or end_ms <= start_ms:
                continue
            ranges.append(TimestampSegment(segment_index, start_ms, end_ms))
    ranges.sort(key=lambda item: item.segment_index)
    return ranges


def find_frame_at_or_after(
    timeline: CalibratedFrameTimeline,
    unix_ms: float,
    limit: int | None = None,
) -> int:
    frame_limit = len(timeline.frame_idx) if limit is None else min(int(limit), len(timeline.frame_idx))
    valid_indices = [index for index in timeline.valid_indices if index < frame_limit]
    valid_times = [timeline.unix_ms[index] for index in valid_indices]
    position = bisect.bisect_left(valid_times, float(unix_ms))
    return frame_limit if position >= len(valid_indices) else valid_indices[position]


def build_active_frame_ranges(
    timeline: CalibratedFrameTimeline,
    timestamp_log: Path,
    *,
    segment_time_offset_sec: float = 0.0,
    min_segment_frames: int = 2,
    frame_limit: int | None = None,
) -> list[ActiveFrameRange]:
    if min_segment_frames <= 0:
        raise ValueError("min_segment_frames must be positive")
    limit = len(timeline.frame_idx) if frame_limit is None else min(int(frame_limit), len(timeline.frame_idx))
    valid_indices = [index for index, keep in enumerate(timeline.valid[:limit]) if keep]
    valid_times = [timeline.unix_ms[index] for index in valid_indices]

    def frame_at_or_after(unix_ms: float) -> int:
        position = bisect.bisect_left(valid_times, float(unix_ms))
        return limit if position >= len(valid_indices) else valid_indices[position]

    ranges: list[ActiveFrameRange] = []
    claimed_frames: set[int] = set()
    for segment in load_timestamp_segments(
        timestamp_log,
        segment_time_offset_sec,
        clock_domain=timeline.clock_domain,
    ):
        start_frame = frame_at_or_after(segment.start_unix_time_ms)
        end_frame = min(frame_at_or_after(segment.end_unix_time_ms), limit)
        frame_indices = tuple(
            index for index in range(start_frame, end_frame) if timeline.valid[index]
        )
        if len(frame_indices) < min_segment_frames:
            continue
        overlap = claimed_frames.intersection(frame_indices)
        if overlap:
            first = min(overlap)
            raise ValueError(
                f"timestamp segments overlap on calibrated source frame {first}; "
                "one frame cannot belong to multiple demonstrations"
            )
        claimed_frames.update(frame_indices)
        ranges.append(
            ActiveFrameRange(
                segment_index=segment.segment_index,
                start_unix_time_ms=segment.start_unix_time_ms,
                end_unix_time_ms=segment.end_unix_time_ms,
                start_frame=start_frame,
                end_frame=end_frame,
                frame_indices=frame_indices,
            )
        )
    return ranges
