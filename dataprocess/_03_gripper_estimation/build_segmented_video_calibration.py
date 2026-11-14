#!/usr/bin/env python3
"""Build a provenance-bound gripper calibration from held-width video segments.

This tool is intentionally separate from the production ``_03`` estimator.  It
consumes that estimator's CSV and the accepted calibrated camera timeline, but
never changes either input or the repository's existing calibration JSON.

Only native primary two-marker measurements are calibration evidence.  Adaptive
two-marker measurements are summarized for diagnosis and never enter a knot.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import math
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence


DATAPROCESS_ROOT = Path(__file__).resolve().parents[1]
if str(DATAPROCESS_ROOT) not in sys.path:
    sys.path.insert(0, str(DATAPROCESS_ROOT))

from active_frame_ranges import (  # noqa: E402
    ActiveFrameRange,
    build_active_frame_ranges,
    load_calibrated_frame_timeline,
)


SCHEMA = "instaumi.segmented_video_gripper_calibration.v1"
LABEL_SCHEMA = "instaumi.segmented_video_gripper_labels.v1"
REPORT_SCHEMA = "instaumi.segmented_video_gripper_calibration_report.v1"
DEFAULT_WIDTHS_MM = tuple(float(value) for value in range(0, 86, 5))
CALIBRATION_FILENAME = "gripper_calibration.json"
LABELS_FILENAME = "segment_labels.json"
STATS_FILENAME = "segment_stats.csv"
REPORT_FILENAME = "calibration_report.json"
OUTPUT_FILENAMES = (
    CALIBRATION_FILENAME,
    LABELS_FILENAME,
    STATS_FILENAME,
    REPORT_FILENAME,
)


class CalibrationError(RuntimeError):
    """Input, provenance, or quality contract failure."""


class CalibrationQualityError(CalibrationError):
    """The evidence was readable but did not pass publication gates."""


class WidthCountMismatchError(CalibrationError):
    """Physical labels cannot be bound one-to-one to complete segments."""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class SegmentEventPair:
    segment_index: int
    start_id: str
    end_id: str
    start_uuid: str
    end_uuid: str
    start_unix_time_ms: float
    end_unix_time_ms: float
    start_unix_time_ns: int
    end_unix_time_ns: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class FrameMeasurement:
    distance_px: float | None
    primary: bool
    adaptive: bool
    measurement_source: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CalibrationError(f"required file is missing: {resolved}")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
    }


def sampled_file_identity(path: Path, sample_size: int = 256 * 1024) -> dict:
    """Reproduce the identity stored by calibrate_no_slam_clock.py."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CalibrationError(f"required file is missing: {resolved}")
    stat = resolved.stat()
    offsets = sorted(
        {
            0,
            max(0, stat.st_size // 2 - sample_size // 2),
            max(0, stat.st_size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(handle.read(min(sample_size, max(0, stat.st_size - offset))))
    return {
        "path": str(resolved),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sample_size": int(sample_size),
        "sample_sha256": digest.hexdigest(),
    }


def load_accepted_clock_contract(
    *,
    report_path: Path,
    capture_root: Path,
    arm: str,
    timeline_path: Path,
    timeline_calibration_id: str,
) -> dict:
    """Validate and bind the exact accepted clock report used by the fit."""

    resolved = report_path.expanduser().resolve()
    identity = file_identity(resolved)
    try:
        report = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(
            f"clock calibration report is not valid JSON: {resolved}"
        ) from exc
    if not isinstance(report, dict):
        raise CalibrationError(f"clock calibration report is not an object: {resolved}")
    arm_report = ((report.get("arms") or {}).get(arm) or {})
    calibration_id = str(report.get("calibration_id") or "")
    if (
        report.get("status") != "accepted"
        or report.get("inputs_unchanged_during_calibration") is not True
        or arm_report.get("status") != "accepted"
        or not calibration_id
        or arm_report.get("calibration_id") != calibration_id
    ):
        raise CalibrationError(
            f"clock calibration report is not an accepted provenance-bound {arm} report: "
            f"{resolved}"
        )
    clock_contract = report.get("clock_contract") or {}
    if (
        report.get("schema") != "instaumi-no-slam-clock-calibration-v2-monotonic-raw"
        or clock_contract.get("domain") != "host_clock_monotonic_raw"
        or clock_contract.get("alignment_policy") != "single_domain_only_fail_closed"
    ):
        raise CalibrationError(
            "clock calibration report does not satisfy the strict MONOTONIC_RAW contract: "
            f"{resolved}"
        )
    if Path(str(report.get("session_dir") or "")).expanduser().resolve() != capture_root:
        raise CalibrationError(
            "clock calibration report belongs to another capture: "
            f"report={report.get('session_dir')!r}, expected={capture_root}"
        )
    if timeline_calibration_id != calibration_id:
        raise CalibrationError(
            "calibrated timeline calibration_id does not match the accepted clock report: "
            f"timeline={timeline_calibration_id!r}, report={calibration_id!r}"
        )
    timeline_record = arm_report.get("frame_timeline") or {}
    recorded_path = Path(str(timeline_record.get("path") or "")).expanduser().resolve()
    if recorded_path != timeline_path:
        raise CalibrationError(
            "accepted clock report points to another calibrated timeline: "
            f"report={recorded_path}, expected={timeline_path}"
        )
    recorded_identity = timeline_record.get("file_identity") or {}
    try:
        sample_size = int(recorded_identity.get("sample_size", 256 * 1024))
    except (TypeError, ValueError) as exc:
        raise CalibrationError(
            "clock report timeline identity has an invalid sample_size"
        ) from exc
    current_identity = sampled_file_identity(timeline_path, sample_size)
    if any(
        recorded_identity.get(key) != current_identity[key]
        for key in (
            "path",
            "size_bytes",
            "mtime_ns",
            "sample_size",
            "sample_sha256",
        )
    ):
        raise CalibrationError(
            "calibrated timeline no longer matches the accepted clock report identity"
        )
    return {
        "report": identity,
        "schema": report.get("schema"),
        "status": report.get("status"),
        "arm_status": arm_report.get("status"),
        "calibration_id": calibration_id,
        "inputs_unchanged_during_calibration": True,
        "timeline_sampled_identity": current_identity,
    }


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def parse_bool(value: object, *, field: str, frame_idx: int) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n", ""}:
        return False
    raise CalibrationError(
        f"frame {frame_idx} has invalid boolean {field}={value!r}"
    )


def finite_float(value: object, *, description: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{description} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise CalibrationError(f"{description} is not finite: {value!r}")
    return result


def canonical_uuid(value: object, *, description: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise CalibrationError(f"{description} has no event UUID")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise CalibrationError(f"{description} has invalid event UUID: {raw!r}") from exc
    if parsed.int == 0:
        raise CalibrationError(f"{description} uses the nil UUID")
    return raw, str(parsed)


def event_time(row: dict, *, description: str) -> tuple[float, int]:
    milliseconds = finite_float(row.get("unixTimeMs"), description=f"{description}.unixTimeMs")
    try:
        nanoseconds = int(row.get("unixTimeNs"))
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{description}.unixTimeNs is invalid") from exc
    if nanoseconds <= 0:
        raise CalibrationError(f"{description}.unixTimeNs must be positive")
    # unixTimeMs is intentionally integer-millisecond compatible in datacap.
    # It must nevertheless refer to the same instant as the exact ns field.
    if abs(nanoseconds / 1e6 - milliseconds) >= 1.0:
        raise CalibrationError(
            f"{description} unixTimeMs/unixTimeNs disagree by >=1 ms"
        )
    return milliseconds, nanoseconds


def load_complete_segment_pairs(timestamp_log: Path) -> list[SegmentEventPair]:
    resolved = timestamp_log.expanduser().resolve()
    if not resolved.is_file():
        raise CalibrationError(f"timestamp log is missing: {resolved}")
    starts: dict[int, tuple[dict, int]] = {}
    ends: dict[int, tuple[dict, int]] = {}
    seen_event_uuids: set[str] = set()
    for line_number, line in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationError(
                f"{resolved}:{line_number} is not valid JSON"
            ) from exc
        event_type = row.get("type")
        if event_type not in {"segment_start", "segment_end"}:
            continue
        camel_index = row.get("segmentIndex")
        snake_index = row.get("segment_index")
        if camel_index is None:
            raise CalibrationError(
                f"{resolved}:{line_number} {event_type} has no segmentIndex"
            )
        try:
            segment_index = int(camel_index)
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                f"{resolved}:{line_number} has invalid segmentIndex={camel_index!r}"
            ) from exc
        if segment_index <= 0:
            raise CalibrationError(f"segment index must be positive: {segment_index}")
        if snake_index is not None and int(snake_index) != segment_index:
            raise CalibrationError(
                f"{resolved}:{line_number} segmentIndex and segment_index disagree"
            )
        raw_id, normalized_id = canonical_uuid(
            row.get("id"),
            description=f"{resolved}:{line_number} {event_type}",
        )
        del raw_id
        if normalized_id in seen_event_uuids:
            raise CalibrationError(
                f"duplicate segment event UUID at {resolved}:{line_number}: {normalized_id}"
            )
        seen_event_uuids.add(normalized_id)
        destination = starts if event_type == "segment_start" else ends
        if segment_index in destination:
            raise CalibrationError(
                f"duplicate {event_type} for segment {segment_index}"
            )
        destination[segment_index] = (row, line_number)

    if not starts and not ends:
        raise CalibrationError(f"timestamp log has no segment events: {resolved}")
    incomplete = sorted(set(starts).symmetric_difference(ends))
    if incomplete:
        raise CalibrationError(
            "every segment must have exactly one start and one end; "
            f"incomplete segment indices={incomplete}"
        )
    indices = sorted(starts)
    expected = list(range(1, len(indices) + 1))
    if indices != expected:
        raise CalibrationError(
            f"segment indices must be contiguous from 1: got={indices}, expected={expected}"
        )

    pairs: list[SegmentEventPair] = []
    for segment_index in indices:
        start, start_line = starts[segment_index]
        end, end_line = ends[segment_index]
        start_id, start_uuid = canonical_uuid(
            start.get("id"), description=f"segment {segment_index} start"
        )
        end_id, end_uuid = canonical_uuid(
            end.get("id"), description=f"segment {segment_index} end"
        )
        start_ms, start_ns = event_time(
            start, description=f"segment {segment_index} start"
        )
        end_ms, end_ns = event_time(end, description=f"segment {segment_index} end")
        if end_ns <= start_ns or end_ms <= start_ms:
            raise CalibrationError(
                f"segment {segment_index} does not have a positive duration"
            )
        pairs.append(
            SegmentEventPair(
                segment_index=segment_index,
                start_id=start_id,
                end_id=end_id,
                start_uuid=start_uuid,
                end_uuid=end_uuid,
                start_unix_time_ms=start_ms,
                end_unix_time_ms=end_ms,
                start_unix_time_ns=start_ns,
                end_unix_time_ns=end_ns,
                start_line=start_line,
                end_line=end_line,
            )
        )
    return pairs


def load_capture_manifest(capture_root: Path, arm: str, segment_count: int) -> dict:
    path = capture_root / "manifest.json"
    identity = file_identity(path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"capture manifest is not valid JSON: {path}") from exc
    profile = manifest.get("capture_profile") or {}
    camera_role = f"insta_{arm}"
    tracker_role = f"htc_{arm}"
    if camera_role not in (profile.get("camera_roles") or []):
        raise CalibrationError(
            f"capture manifest does not bind arm={arm} to camera role {camera_role}"
        )
    if tracker_role not in (profile.get("tracker_roles") or []):
        raise CalibrationError(
            f"capture manifest does not bind arm={arm} to tracker role {tracker_role}"
        )
    completed = (manifest.get("status") or {}).get("completed_segments")
    if completed is not None and int(completed) != segment_count:
        raise CalibrationError(
            "capture manifest completed segment count differs from timestamp log: "
            f"manifest={completed}, log={segment_count}"
        )
    return {
        "identity": identity,
        "session_id": manifest.get("session_id"),
        "task_name": manifest.get("task_name"),
        "arm_mode": profile.get("arm_mode"),
        "camera_role": camera_role,
        "tracker_role": tracker_role,
    }


def parse_widths(values: Sequence[float] | None) -> tuple[list[float], str]:
    source = "default_0_to_85_mm_inclusive_step_5_mm"
    widths = list(DEFAULT_WIDTHS_MM if values is None else values)
    if values is not None:
        source = "explicit_cli"
    if len(widths) < 2:
        raise CalibrationError("at least two physical width labels are required")
    widths = [finite_float(value, description="width_mm") for value in widths]
    if any(value < 0.0 or value > 110.0 for value in widths):
        raise CalibrationError("all widths must be inside the physical range [0, 110] mm")
    if any(after <= before for before, after in zip(widths, widths[1:])):
        raise CalibrationError(
            "width labels must be strictly increasing in segment order"
        )
    return widths, source


def quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise CalibrationError("cannot compute a quantile of an empty sequence")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def summarize_values(values: Sequence[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "median_px": None,
            "mad_px": None,
            "robust_sigma_px": None,
            "p95_abs_deviation_px": None,
            "minimum_px": None,
            "maximum_px": None,
        }
    center = float(median(values))
    deviations = [abs(float(value) - center) for value in values]
    mad = float(median(deviations))
    return {
        "count": len(values),
        "median_px": center,
        "mad_px": mad,
        "robust_sigma_px": 1.4826 * mad,
        "p95_abs_deviation_px": quantile(deviations, 0.95),
        "minimum_px": min(values),
        "maximum_px": max(values),
    }


def least_squares_slope_px_per_sec(
    unix_time_ms: Sequence[float], values_px: Sequence[float]
) -> float:
    if len(unix_time_ms) != len(values_px) or not values_px:
        raise CalibrationError("slope inputs must be non-empty and equally sized")
    if len(values_px) == 1:
        return 0.0
    origin_ms = float(unix_time_ms[0])
    times_sec = [(float(value) - origin_ms) / 1000.0 for value in unix_time_ms]
    mean_time = sum(times_sec) / len(times_sec)
    mean_value = sum(float(value) for value in values_px) / len(values_px)
    denominator = sum((value - mean_time) ** 2 for value in times_sec)
    if denominator <= 0.0:
        return 0.0
    return sum(
        (time_sec - mean_time) * (float(value_px) - mean_value)
        for time_sec, value_px in zip(times_sec, values_px, strict=True)
    ) / denominator


def frame_ranges_evidence(frame_indices: Sequence[int], timeline) -> list[dict]:
    if not frame_indices:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = int(frame_indices[0])
    for frame_idx in frame_indices[1:]:
        frame_idx = int(frame_idx)
        if frame_idx != previous + 1:
            ranges.append((start, previous))
            start = frame_idx
        previous = frame_idx
    ranges.append((start, previous))
    return [
        {
            "first_frame_idx": start,
            "last_frame_idx": end,
            "frame_count": end - start + 1,
            "first_unix_time_ms": float(timeline.unix_ms[start]),
            "last_unix_time_ms": float(timeline.unix_ms[end]),
        }
        for start, end in ranges
    ]


def select_longest_stable_plateau(
    *,
    segment_index: int,
    trimmed_frames: Sequence[int],
    measurements: dict[int, FrameMeasurement],
    timeline,
    min_duration_ms: float,
    min_primary_frames: int,
    min_primary_detection_rate: float,
    max_window_p95_deviation_px: float,
    max_abs_slope_px_per_sec: float,
    uniqueness_margin_ms: float,
    selection_policy: str = "unique_longest",
    event_start_unix_time_ms: float | None = None,
    max_event_start_delay_ms: float = 100.0,
) -> tuple[tuple[int, ...], list[float], dict]:
    """Select one stable held-width platform without using a whole-segment median.

    A fixed-minimum sliding window is stable only when it has enough native
    primary detections, a high enough detection rate, bounded robust spread,
    and bounded linear drift.  Overlapping stable windows form candidate
    platforms.  The longest platform wins only when its duration exceeds the
    runner-up by the configured margin.  This intentionally rejects ambiguous
    captures instead of resolving them with an arbitrary first/last tie-break.

    ``event_start_anchored`` is a separate, explicit acquisition contract: the
    operator establishes the labeled physical width *before* emitting the
    segment-start event.  In that mode only the earliest stable component whose
    first frame is close to the event boundary is eligible.  A later platform
    is never used as a fallback, even when it is longer.
    """

    if selection_policy not in ("unique_longest", "event_start_anchored"):
        raise CalibrationError(
            f"unsupported plateau selection policy: {selection_policy!r}"
        )
    if selection_policy == "event_start_anchored":
        if event_start_unix_time_ms is None:
            raise CalibrationError(
                "event_start_anchored plateau selection requires the segment-start "
                "timestamp"
            )
        event_start_unix_time_ms = finite_float(
            event_start_unix_time_ms,
            description="event_start_unix_time_ms",
        )
        max_event_start_delay_ms = finite_float(
            max_event_start_delay_ms,
            description="max_event_start_delay_ms",
        )
        if max_event_start_delay_ms < 0.0:
            raise CalibrationError("max_event_start_delay_ms must be non-negative")

    frames = tuple(int(value) for value in trimmed_frames)
    times = [float(timeline.unix_ms[frame_idx]) for frame_idx in frames]
    if len(frames) < min_primary_frames:
        raise CalibrationQualityError(
            f"segment {segment_index} has too few edge-trimmed frames for a stable "
            f"platform: {len(frames)} < {min_primary_frames}"
        )

    primary_prefix = [0]
    for frame_idx in frames:
        primary_prefix.append(
            primary_prefix[-1] + int(measurements[frame_idx].primary)
        )

    stable_windows: list[dict] = []
    for start_pos in range(len(frames)):
        duration_end_pos = bisect.bisect_left(
            times,
            times[start_pos] + min_duration_ms,
            lo=start_pos + 1,
        )
        end_pos = max(start_pos + min_primary_frames, duration_end_pos + 1)
        while (
            end_pos <= len(frames)
            and primary_prefix[end_pos] - primary_prefix[start_pos]
            < min_primary_frames
        ):
            end_pos += 1
        if end_pos > len(frames):
            continue
        window_frames = frames[start_pos:end_pos]
        primary_frames = [
            frame_idx
            for frame_idx in window_frames
            if measurements[frame_idx].primary
        ]
        detection_rate = len(primary_frames) / len(window_frames)
        if detection_rate < min_primary_detection_rate:
            continue
        primary_values = [
            float(measurements[frame_idx].distance_px)
            for frame_idx in primary_frames
            if measurements[frame_idx].distance_px is not None
        ]
        primary_times = [float(timeline.unix_ms[index]) for index in primary_frames]
        summary = summarize_values(primary_values)
        slope = least_squares_slope_px_per_sec(primary_times, primary_values)
        if (
            summary["p95_abs_deviation_px"] is None
            or summary["p95_abs_deviation_px"] > max_window_p95_deviation_px
            or abs(slope) > max_abs_slope_px_per_sec
        ):
            continue
        stable_windows.append(
            {
                "start_pos": start_pos,
                "end_pos": end_pos,
                "p95_abs_deviation_px": summary["p95_abs_deviation_px"],
                "slope_px_per_sec": slope,
            }
        )

    components: list[dict] = []
    for window in stable_windows:
        if components and window["start_pos"] <= components[-1]["end_pos"]:
            components[-1]["end_pos"] = max(
                components[-1]["end_pos"], window["end_pos"]
            )
            components[-1]["stable_window_count"] += 1
        else:
            components.append(
                {
                    "start_pos": window["start_pos"],
                    "end_pos": window["end_pos"],
                    "stable_window_count": 1,
                }
            )

    candidates: list[dict] = []
    for ordinal, component in enumerate(components, start=1):
        component_frames = frames[component["start_pos"] : component["end_pos"]]
        primary_frames = [
            frame_idx
            for frame_idx in component_frames
            if measurements[frame_idx].primary
        ]
        primary_values = [
            float(measurements[frame_idx].distance_px)
            for frame_idx in primary_frames
            if measurements[frame_idx].distance_px is not None
        ]
        primary_times = [float(timeline.unix_ms[index]) for index in primary_frames]
        summary = summarize_values(primary_values)
        duration_ms = (
            float(timeline.unix_ms[component_frames[-1]])
            - float(timeline.unix_ms[component_frames[0]])
        )
        detection_rate = len(primary_frames) / len(component_frames)
        slope = least_squares_slope_px_per_sec(primary_times, primary_values)
        rejection_reasons: list[str] = []
        if duration_ms < min_duration_ms:
            rejection_reasons.append("duration_below_minimum")
        if len(primary_frames) < min_primary_frames:
            rejection_reasons.append("primary_frames_below_minimum")
        if detection_rate < min_primary_detection_rate:
            rejection_reasons.append("primary_detection_rate_below_minimum")
        if (
            summary["p95_abs_deviation_px"] is None
            or summary["p95_abs_deviation_px"] > max_window_p95_deviation_px
        ):
            rejection_reasons.append("whole_candidate_p95_deviation_above_limit")
        if abs(slope) > max_abs_slope_px_per_sec:
            rejection_reasons.append("whole_candidate_slope_above_limit")
        candidates.append(
            {
                "candidate_id": ordinal,
                "accepted_candidate": not rejection_reasons,
                "rejection_reasons": rejection_reasons,
                "first_frame_idx": component_frames[0],
                "last_frame_idx": component_frames[-1],
                "frame_count": len(component_frames),
                "primary_count": len(primary_frames),
                "primary_detection_rate": detection_rate,
                "duration_ms": duration_ms,
                "stable_window_count": component["stable_window_count"],
                "primary_median_px": summary["median_px"],
                "primary_mad_px": summary["mad_px"],
                "primary_p95_abs_deviation_px": summary[
                    "p95_abs_deviation_px"
                ],
                "primary_minimum_px": summary["minimum_px"],
                "primary_maximum_px": summary["maximum_px"],
                "least_squares_slope_px_per_sec": slope,
                "_frames": component_frames,
                "_primary_values": primary_values,
            }
        )

    accepted = [item for item in candidates if item["accepted_candidate"]]
    if event_start_unix_time_ms is not None:
        for item in candidates:
            item["event_start_delay_ms"] = (
                float(timeline.unix_ms[item["first_frame_idx"]])
                - event_start_unix_time_ms
            )
    if selection_policy == "unique_longest":
        accepted.sort(
            key=lambda item: (
                -item["duration_ms"],
                -item["primary_count"],
                item["first_frame_idx"],
            )
        )
    else:
        accepted = [
            item
            for item in accepted
            if 0.0 <= item["event_start_delay_ms"] <= max_event_start_delay_ms
        ]
        accepted.sort(
            key=lambda item: (
                item["event_start_delay_ms"],
                item["first_frame_idx"],
                -item["duration_ms"],
            )
        )
    public_candidates = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in candidates
    ]
    if not accepted:
        if selection_policy == "event_start_anchored" and candidates:
            raise CalibrationQualityError(
                f"segment {segment_index} has no stable primary two-marker plateau "
                f"anchored within {max_event_start_delay_ms:.1f} ms of segment_start; "
                f"later platforms are intentionally forbidden; "
                f"candidates={public_candidates}"
            )
        raise CalibrationQualityError(
            f"segment {segment_index} has no stable primary two-marker plateau "
            f"lasting >= {min_duration_ms:.1f} ms with >= {min_primary_frames} frames; "
            f"stable_windows={len(stable_windows)}, candidates={public_candidates}"
        )
    winner = accepted[0]
    if selection_policy == "unique_longest" and len(accepted) > 1:
        duration_margin_ms = winner["duration_ms"] - accepted[1]["duration_ms"]
        if duration_margin_ms < uniqueness_margin_ms:
            raise CalibrationQualityError(
                f"segment {segment_index} stable platform is ambiguous: longest="
                f"{winner['duration_ms']:.3f}ms runner_up={accepted[1]['duration_ms']:.3f}ms "
                f"margin={duration_margin_ms:.3f}ms < {uniqueness_margin_ms:.3f}ms; "
                f"candidates={public_candidates}"
            )
    else:
        duration_margin_ms = None

    winner_frames = tuple(int(value) for value in winner["_frames"])
    winner_set = set(winner_frames)
    excluded_frames = tuple(value for value in frames if value not in winner_set)
    selection_report = {
        "algorithm": (
            "deterministic_longest_overlapping_stable_window_component_v1"
            if selection_policy == "unique_longest"
            else "event_start_anchored_earliest_stable_component_v1"
        ),
        "selection_policy": selection_policy,
        "minimum_duration_ms": min_duration_ms,
        "minimum_primary_frames": min_primary_frames,
        "minimum_primary_detection_rate": min_primary_detection_rate,
        "maximum_window_p95_abs_deviation_px": max_window_p95_deviation_px,
        "maximum_abs_least_squares_slope_px_per_sec": max_abs_slope_px_per_sec,
        "minimum_winner_duration_margin_ms": (
            uniqueness_margin_ms if selection_policy == "unique_longest" else None
        ),
        "event_start_unix_time_ms": event_start_unix_time_ms,
        "maximum_event_start_delay_ms": (
            max_event_start_delay_ms
            if selection_policy == "event_start_anchored"
            else None
        ),
        "stable_window_count": len(stable_windows),
        "candidate_count": len(candidates),
        "candidates": public_candidates,
        "selected_candidate_id": winner["candidate_id"],
        "selected_event_start_delay_ms": winner.get("event_start_delay_ms"),
        "winner_duration_margin_ms": duration_margin_ms,
        "selected_frame_ranges": frame_ranges_evidence(winner_frames, timeline),
        "excluded_motion_or_nonselected_frame_count": len(excluded_frames),
        "excluded_motion_or_nonselected_frame_ranges": frame_ranges_evidence(
            excluded_frames, timeline
        ),
    }
    return winner_frames, list(winner["_primary_values"]), selection_report


def trim_active_range(
    active_range: ActiveFrameRange,
    timeline,
    edge_trim_ms: float,
) -> tuple[int, ...]:
    lower = float(active_range.start_unix_time_ms) + edge_trim_ms
    upper = float(active_range.end_unix_time_ms) - edge_trim_ms
    if upper <= lower:
        return ()
    return tuple(
        frame_idx
        for frame_idx in active_range.frame_indices
        if lower <= float(timeline.unix_ms[frame_idx]) < upper
    )


def load_selected_measurements(
    gripper_csv: Path,
    *,
    timeline_frame_count: int,
    selected_frames: set[int],
) -> dict[int, FrameMeasurement]:
    resolved = gripper_csv.expanduser().resolve()
    required = {
        "frame_idx",
        "distance_px",
        "dual_marker_valid",
        "primary_dual_marker_valid",
        "adaptive_dual_marker_valid",
        "measurement_source",
        "valid",
    }
    selected: dict[int, FrameMeasurement] = {}
    with resolved.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(required - fields)
        if missing:
            raise CalibrationError(
                f"gripper CSV is missing required production columns: {missing}"
            )
        row_count = 0
        for row_number, row in enumerate(reader, start=2):
            try:
                frame_idx = int(row["frame_idx"])
            except (TypeError, ValueError) as exc:
                raise CalibrationError(
                    f"{resolved}:{row_number} has invalid frame_idx"
                ) from exc
            if frame_idx != row_count:
                raise CalibrationError(
                    f"gripper CSV must retain every source frame in order: "
                    f"row={row_number}, expected={row_count}, got={frame_idx}"
                )
            row_count += 1
            if frame_idx not in selected_frames:
                continue
            primary = parse_bool(
                row.get("primary_dual_marker_valid"),
                field="primary_dual_marker_valid",
                frame_idx=frame_idx,
            )
            adaptive = parse_bool(
                row.get("adaptive_dual_marker_valid"),
                field="adaptive_dual_marker_valid",
                frame_idx=frame_idx,
            )
            dual = parse_bool(
                row.get("dual_marker_valid"),
                field="dual_marker_valid",
                frame_idx=frame_idx,
            )
            valid = parse_bool(row.get("valid"), field="valid", frame_idx=frame_idx)
            source = str(row.get("measurement_source") or "").strip()
            raw_distance = str(row.get("distance_px") or "").strip()
            distance_px = None
            if raw_distance:
                distance_px = finite_float(
                    raw_distance, description=f"frame {frame_idx} distance_px"
                )
                if distance_px <= 0.0:
                    raise CalibrationError(
                        f"frame {frame_idx} distance_px must be positive"
                    )
            if primary:
                if source != "dual_marker_primary" or not dual or not valid:
                    raise CalibrationError(
                        f"frame {frame_idx} primary flags/source are inconsistent: "
                        f"source={source!r}, dual={dual}, valid={valid}"
                    )
                if distance_px is None:
                    raise CalibrationError(
                        f"frame {frame_idx} primary measurement has no distance_px"
                    )
            elif source == "dual_marker_primary":
                raise CalibrationError(
                    f"frame {frame_idx} claims primary source without primary validity"
                )
            if adaptive:
                if not source.startswith("dual_marker_adaptive_threshold_retry"):
                    raise CalibrationError(
                        f"frame {frame_idx} adaptive flags/source are inconsistent: {source!r}"
                    )
                if not dual or not valid or distance_px is None:
                    raise CalibrationError(
                        f"frame {frame_idx} accepted adaptive evidence is incomplete"
                    )
            selected[frame_idx] = FrameMeasurement(
                distance_px=distance_px,
                primary=primary,
                adaptive=adaptive,
                measurement_source=source,
            )
    if row_count != timeline_frame_count:
        raise CalibrationError(
            "gripper CSV frame count differs from calibrated timeline: "
            f"gripper={row_count}, timeline={timeline_frame_count}"
        )
    missing_selected = sorted(selected_frames - set(selected))
    if missing_selected:
        raise CalibrationError(
            f"gripper CSV is missing selected frames: {missing_selected[:10]}"
        )
    return selected


def interpolate_width_mm(
    distance_px: float,
    knot_px: Sequence[float],
    knot_width_mm: Sequence[float],
) -> float:
    if distance_px <= knot_px[0]:
        return float(knot_width_mm[0])
    if distance_px >= knot_px[-1]:
        return float(knot_width_mm[-1])
    upper = bisect.bisect_right(knot_px, distance_px)
    lower = upper - 1
    span = knot_px[upper] - knot_px[lower]
    ratio = (distance_px - knot_px[lower]) / span
    return float(
        knot_width_mm[lower]
        + ratio * (knot_width_mm[upper] - knot_width_mm[lower])
    )


def stats_csv_bytes(segment_stats: Sequence[dict]) -> bytes:
    fields = [
        "segment_index",
        "width_mm",
        "start_event_id",
        "end_event_id",
        "start_unix_time_ns",
        "end_unix_time_ns",
        "mapped_frame_count",
        "trimmed_frame_count",
        "trimmed_first_frame_idx",
        "trimmed_last_frame_idx",
        "trimmed_primary_count_all",
        "trimmed_primary_detection_rate_all",
        "trimmed_primary_median_px_all",
        "trimmed_primary_mad_px_all",
        "plateau_first_frame_idx",
        "plateau_last_frame_idx",
        "plateau_frame_count",
        "plateau_duration_ms",
        "plateau_excluded_frame_count",
        "plateau_candidate_count",
        "plateau_selection_policy",
        "plateau_event_start_delay_ms",
        "plateau_winner_duration_margin_ms",
        "primary_count",
        "primary_detection_rate",
        "primary_median_px",
        "primary_mad_px",
        "primary_robust_sigma_px",
        "primary_p95_abs_deviation_px",
        "adaptive_count_diagnostic_only",
        "adaptive_detection_rate_diagnostic_only",
        "adaptive_median_px_diagnostic_only",
        "adaptive_minus_primary_median_px_diagnostic_only",
        "mapping_error_p95_mm",
        "mapping_error_max_mm",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in segment_stats:
        writer.writerow({name: row.get(name, "") for name in fields})
    return stream.getvalue().encode("utf-8")


def compare_existing_output(output_dir: Path, artifacts: dict[str, bytes]) -> None:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise CalibrationError(
            f"refusing to overwrite existing non-directory output: {output_dir}"
        )
    existing_names = sorted(path.name for path in output_dir.iterdir())
    expected_names = sorted(artifacts)
    if existing_names != expected_names:
        raise CalibrationError(
            "existing output directory is not an identical completed calibration: "
            f"existing={existing_names}, expected={expected_names}"
        )
    for name, expected in artifacts.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
            raise CalibrationError(
                f"refusing to overwrite non-identical existing calibration artifact: {path}"
            )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_rejected_report_only_output(output: Path) -> Path | None:
    """Atomically retain a superseded rejection before publishing new evidence.

    Accepted calibrations remain immutable.  Only a one-file, schema-valid
    rejected output is eligible for promotion to history.  This lets a
    corrected recapture reuse the same named calibration version without
    deleting the audit explaining why its earlier attempt was rejected.
    """

    if not output.is_dir() or output.is_symlink():
        return None
    entries = list(output.iterdir())
    if len(entries) != 1 or entries[0].name != REPORT_FILENAME:
        return None
    report_path = entries[0]
    if not report_path.is_file() or report_path.is_symlink():
        return None
    payload = report_path.read_bytes()
    try:
        report = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(report, dict)
        or report.get("schema") != REPORT_SCHEMA
        or report.get("status") != "rejected"
        or report.get("calibration_written") is not False
    ):
        return None

    digest = hashlib.sha256(payload).hexdigest()
    history_root = output.parent / "rejected_history"
    history_root.mkdir(parents=True, exist_ok=True)
    archive = history_root / f"{output.name}_{digest}"
    if archive.exists() or archive.is_symlink():
        # Never merge into or overwrite an existing audit directory.  A UUID
        # suffix preserves both copies if a prior manual archive used the same
        # content-addressed name.
        archive = history_root / f"{output.name}_{digest}_{uuid.uuid4().hex}"
    output.rename(archive)
    fsync_directory(history_root)
    fsync_directory(output.parent)
    return archive


def publish_artifacts(output_dir: Path, artifacts: dict[str, bytes]) -> str:
    output = output_dir.expanduser().resolve()
    archived_rejection: Path | None = None
    if output.exists() or output.is_symlink():
        try:
            compare_existing_output(output, artifacts)
        except CalibrationError:
            archived_rejection = archive_rejected_report_only_output(output)
            if archived_rejection is None:
                raise
        else:
            return "resumed_identical"
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o755)
    try:
        for name, payload in artifacts.items():
            destination = staging / name
            with destination.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        fsync_directory(staging)
        try:
            staging.rename(output)
        except FileExistsError:
            compare_existing_output(output, artifacts)
            return "resumed_identical_after_race"
        fsync_directory(output.parent)
        if archived_rejection is not None:
            return f"created_after_archiving_rejected:{archived_rejection}"
        return "created"
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def build_artifacts(args: argparse.Namespace) -> tuple[dict[str, bytes], dict]:
    capture_root = args.capture_root.expanduser().resolve()
    if not capture_root.is_dir():
        raise CalibrationError(f"capture root is missing: {capture_root}")
    timestamp_log = (
        args.timestamp_log.expanduser().resolve()
        if args.timestamp_log is not None
        else capture_root / "timestamp_log.jsonl"
    )
    gripper_csv = args.gripper_csv.expanduser().resolve()
    calibrated_timeline_path = args.calibrated_timeline.expanduser().resolve()
    timestamp_identity = file_identity(timestamp_log)
    gripper_identity = file_identity(gripper_csv)
    timeline_identity = file_identity(calibrated_timeline_path)

    try:
        timeline = load_calibrated_frame_timeline(calibrated_timeline_path)
    except (OSError, ValueError) as exc:
        raise CalibrationError(
            f"calibrated timeline is invalid: {calibrated_timeline_path}: {exc}"
        ) from exc
    if not timeline.calibration_id:
        raise CalibrationError(
            "calibrated timeline has no calibration_id; an accepted provenance-bound "
            "_01 clock calibration is required"
        )
    clock_contract = load_accepted_clock_contract(
        report_path=args.clock_calibration_report,
        capture_root=capture_root,
        arm=args.arm,
        timeline_path=calibrated_timeline_path,
        timeline_calibration_id=timeline.calibration_id,
    )

    event_pairs = load_complete_segment_pairs(timestamp_log)
    widths_mm, width_source = parse_widths(args.widths_mm)
    if len(widths_mm) != len(event_pairs):
        message = (
            "physical width count must exactly match the complete timestamp segments: "
            f"widths={len(widths_mm)}, complete_segments={len(event_pairs)}"
        )
        raise WidthCountMismatchError(
            message,
            {
                "schema": REPORT_SCHEMA,
                "status": "rejected",
                "reason_code": "physical_width_count_mismatch",
                "reason": message,
                "calibration_written": False,
                "inputs": {
                    "capture_root": str(capture_root),
                    "arm": args.arm,
                    "timestamp_log": timestamp_identity,
                    "gripper_csv": gripper_identity,
                    "calibrated_timeline": timeline_identity,
                    "clock_calibration": clock_contract,
                    "implementation": file_identity(Path(__file__)),
                },
                "width_sequence": {
                    "source": width_source,
                    "values_mm": widths_mm,
                    "count": len(widths_mm),
                },
                "event_binding": {
                    "complete_segment_count": len(event_pairs),
                    "timestamp_log_sha256": timestamp_identity["sha256"],
                    "segments": [
                        {
                            "segment_index": pair.segment_index,
                            "start_event_id": pair.start_id,
                            "start_event_uuid": pair.start_uuid,
                            "end_event_id": pair.end_id,
                            "end_event_uuid": pair.end_uuid,
                        }
                        for pair in event_pairs
                    ],
                },
                "output_contract": {
                    "rejected_report_only": True,
                    "calibration_filename_absent": CALIBRATION_FILENAME,
                    "existing_input_modified": False,
                    "existing_repository_calibration_modified": False,
                },
            },
        )
    capture_contract = load_capture_manifest(
        capture_root, args.arm, len(event_pairs)
    )

    active_ranges = build_active_frame_ranges(
        timeline,
        timestamp_log,
        segment_time_offset_sec=0.0,
        min_segment_frames=2,
    )
    event_by_index = {item.segment_index: item for item in event_pairs}
    range_by_index = {item.segment_index: item for item in active_ranges}
    if sorted(range_by_index) != sorted(event_by_index):
        raise CalibrationError(
            "every complete timestamp segment must map to the calibrated camera timeline: "
            f"events={sorted(event_by_index)}, mapped={sorted(range_by_index)}"
        )
    for segment_index, active_range in range_by_index.items():
        evidence = event_by_index[segment_index]
        if (
            active_range.start_unix_time_ms != evidence.start_unix_time_ms
            or active_range.end_unix_time_ms != evidence.end_unix_time_ms
        ):
            raise CalibrationError(
                f"segment {segment_index} active-frame mapping changed its event boundaries"
            )

    edge_trim_ms = finite_float(args.edge_trim_ms, description="edge_trim_ms")
    if edge_trim_ms < 0.0:
        raise CalibrationError("edge_trim_ms must be non-negative")
    plateau_selection_policy = getattr(
        args, "plateau_selection_policy", "unique_longest"
    )
    max_event_start_delay_ms = finite_float(
        getattr(args, "max_event_start_delay_ms", 100.0),
        description="max_event_start_delay_ms",
    )
    trimmed_by_segment = {
        segment_index: trim_active_range(active_range, timeline, edge_trim_ms)
        for segment_index, active_range in range_by_index.items()
    }
    for segment_index, frames in trimmed_by_segment.items():
        if len(frames) < args.min_trimmed_frames:
            raise CalibrationQualityError(
                f"segment {segment_index} has {len(frames)} frames after edge trim; "
                f"minimum={args.min_trimmed_frames}"
            )
    selected_frames = {
        frame_idx for frames in trimmed_by_segment.values() for frame_idx in frames
    }
    measurements = load_selected_measurements(
        gripper_csv,
        timeline_frame_count=len(timeline.frame_idx),
        selected_frames=selected_frames,
    )

    segment_stats: list[dict] = []
    calibration_px: list[float] = []
    primary_values_by_segment: dict[int, list[float]] = {}
    violations: list[str] = []
    for width_mm, event_pair in zip(widths_mm, event_pairs, strict=True):
        active_range = range_by_index[event_pair.segment_index]
        trimmed = trimmed_by_segment[event_pair.segment_index]
        trimmed_primary_values_all = [
            float(measurements[index].distance_px)
            for index in trimmed
            if measurements[index].primary
            and measurements[index].distance_px is not None
        ]
        trimmed_primary_all = summarize_values(trimmed_primary_values_all)
        plateau_frames, primary_values, plateau_selection = (
            select_longest_stable_plateau(
                segment_index=event_pair.segment_index,
                trimmed_frames=trimmed,
                measurements=measurements,
                timeline=timeline,
                min_duration_ms=args.min_stable_plateau_duration_ms,
                min_primary_frames=args.min_stable_plateau_frames,
                min_primary_detection_rate=args.min_primary_detection_rate,
                max_window_p95_deviation_px=(
                    args.max_stable_window_p95_deviation_px
                ),
                max_abs_slope_px_per_sec=(
                    args.max_stable_plateau_slope_px_per_sec
                ),
                uniqueness_margin_ms=args.min_plateau_uniqueness_margin_ms,
                selection_policy=plateau_selection_policy,
                event_start_unix_time_ms=(
                    event_pair.start_unix_time_ms
                    if plateau_selection_policy == "event_start_anchored"
                    else None
                ),
                max_event_start_delay_ms=max_event_start_delay_ms,
            )
        )
        adaptive_values = [
            float(measurements[index].distance_px)
            for index in trimmed
            if measurements[index].adaptive
            and measurements[index].distance_px is not None
        ]
        primary = summarize_values(primary_values)
        adaptive = summarize_values(adaptive_values)
        primary_values_by_segment[event_pair.segment_index] = primary_values
        detection_rate = len(primary_values) / len(plateau_frames)
        adaptive_rate = len(adaptive_values) / len(trimmed)
        if len(primary_values) < args.min_primary_frames:
            violations.append(
                f"segment {event_pair.segment_index} primary_frames={len(primary_values)} "
                f"< {args.min_primary_frames}"
            )
        if detection_rate < args.min_primary_detection_rate:
            violations.append(
                f"segment {event_pair.segment_index} primary_detection_rate="
                f"{detection_rate:.6f} < {args.min_primary_detection_rate:.6f}"
            )
        if primary["mad_px"] is not None and primary["mad_px"] > args.max_primary_mad_px:
            violations.append(
                f"segment {event_pair.segment_index} primary_mad_px="
                f"{primary['mad_px']:.6f} > {args.max_primary_mad_px:.6f}"
            )
        if primary["median_px"] is None:
            # Retain deterministic stats for the failure message; no output is
            # published when any segment lacks primary evidence.
            calibration_px.append(math.nan)
        else:
            calibration_px.append(float(primary["median_px"]))
        segment_stats.append(
            {
                "segment_index": event_pair.segment_index,
                "width_mm": width_mm,
                "start_event_id": event_pair.start_id,
                "end_event_id": event_pair.end_id,
                "start_event_uuid": event_pair.start_uuid,
                "end_event_uuid": event_pair.end_uuid,
                "start_unix_time_ms": event_pair.start_unix_time_ms,
                "end_unix_time_ms": event_pair.end_unix_time_ms,
                "start_unix_time_ns": event_pair.start_unix_time_ns,
                "end_unix_time_ns": event_pair.end_unix_time_ns,
                "event_duration_ms": (
                    event_pair.end_unix_time_ns - event_pair.start_unix_time_ns
                )
                / 1e6,
                "mapped_frame_count": len(active_range.frame_indices),
                "mapped_first_frame_idx": active_range.frame_indices[0],
                "mapped_last_frame_idx": active_range.frame_indices[-1],
                "edge_trim_ms": edge_trim_ms,
                "trimmed_frame_count": len(trimmed),
                "trimmed_first_frame_idx": trimmed[0],
                "trimmed_last_frame_idx": trimmed[-1],
                "trimmed_primary_count_all": len(trimmed_primary_values_all),
                "trimmed_primary_detection_rate_all": (
                    len(trimmed_primary_values_all) / len(trimmed)
                ),
                "trimmed_primary_median_px_all": trimmed_primary_all["median_px"],
                "trimmed_primary_mad_px_all": trimmed_primary_all["mad_px"],
                "trimmed_primary_p95_abs_deviation_px_all": (
                    trimmed_primary_all["p95_abs_deviation_px"]
                ),
                "plateau_first_frame_idx": plateau_frames[0],
                "plateau_last_frame_idx": plateau_frames[-1],
                "plateau_frame_count": len(plateau_frames),
                "plateau_duration_ms": (
                    float(timeline.unix_ms[plateau_frames[-1]])
                    - float(timeline.unix_ms[plateau_frames[0]])
                ),
                "plateau_excluded_frame_count": len(trimmed) - len(plateau_frames),
                "plateau_candidate_count": plateau_selection["candidate_count"],
                "plateau_selection_policy": plateau_selection["selection_policy"],
                "plateau_event_start_delay_ms": plateau_selection[
                    "selected_event_start_delay_ms"
                ],
                "plateau_winner_duration_margin_ms": plateau_selection[
                    "winner_duration_margin_ms"
                ],
                "plateau_selection": plateau_selection,
                "primary_count": len(primary_values),
                "primary_detection_rate": detection_rate,
                "primary_median_px": primary["median_px"],
                "primary_mad_px": primary["mad_px"],
                "primary_robust_sigma_px": primary["robust_sigma_px"],
                "primary_p95_abs_deviation_px": primary[
                    "p95_abs_deviation_px"
                ],
                "primary_minimum_px": primary["minimum_px"],
                "primary_maximum_px": primary["maximum_px"],
                "adaptive_count_diagnostic_only": len(adaptive_values),
                "adaptive_detection_rate_diagnostic_only": adaptive_rate,
                "adaptive_median_px_diagnostic_only": adaptive["median_px"],
                "adaptive_mad_px_diagnostic_only": adaptive["mad_px"],
                "adaptive_minus_primary_median_px_diagnostic_only": (
                    None
                    if adaptive["median_px"] is None or primary["median_px"] is None
                    else adaptive["median_px"] - primary["median_px"]
                ),
            }
        )

    if all(math.isfinite(value) for value in calibration_px):
        for position, (before, after) in enumerate(
            zip(calibration_px, calibration_px[1:]), start=1
        ):
            gap = after - before
            if gap <= 0.0:
                violations.append(
                    "primary pixel medians are not strictly increasing: "
                    f"segment {position}={before:.6f}, segment {position + 1}={after:.6f}"
                )
            elif gap < args.min_adjacent_pixel_gap:
                violations.append(
                    f"adjacent primary pixel gap {position}->{position + 1}="
                    f"{gap:.6f} < {args.min_adjacent_pixel_gap:.6f}"
                )

    all_errors_mm: list[float] = []
    if all(math.isfinite(value) for value in calibration_px) and all(
        after > before for before, after in zip(calibration_px, calibration_px[1:])
    ):
        for stats, width_mm in zip(segment_stats, widths_mm, strict=True):
            errors = [
                abs(
                    interpolate_width_mm(value, calibration_px, widths_mm)
                    - width_mm
                )
                for value in primary_values_by_segment[stats["segment_index"]]
            ]
            all_errors_mm.extend(errors)
            stats["mapping_error_median_mm"] = (
                None if not errors else float(median(errors))
            )
            stats["mapping_error_p95_mm"] = (
                None if not errors else quantile(errors, 0.95)
            )
            stats["mapping_error_max_mm"] = None if not errors else max(errors)
            if (
                stats["mapping_error_p95_mm"] is not None
                and stats["mapping_error_p95_mm"]
                > args.max_frame_error_p95_mm
            ):
                violations.append(
                    f"segment {stats['segment_index']} mapping_error_p95_mm="
                    f"{stats['mapping_error_p95_mm']:.6f} > "
                    f"{args.max_frame_error_p95_mm:.6f}"
                )
        if all_errors_mm and max(all_errors_mm) > args.max_frame_error_mm:
            violations.append(
                f"maximum frame mapping error={max(all_errors_mm):.6f} mm > "
                f"{args.max_frame_error_mm:.6f} mm"
            )
    else:
        for stats in segment_stats:
            stats["mapping_error_median_mm"] = None
            stats["mapping_error_p95_mm"] = None
            stats["mapping_error_max_mm"] = None

    if violations:
        raise CalibrationQualityError(
            "calibration quality gate rejected the evidence: " + "; ".join(violations)
        )

    calibration = [
        {
            "cm": width_mm / 10.0,
            "m": width_mm / 1000.0,
            "px": distance_px,
            "source": (
                f"{args.arm}:segment={pair.segment_index}:"
                f"start={pair.start_id}:end={pair.end_id}:primary_dual_marker_median"
            ),
        }
        for width_mm, distance_px, pair in zip(
            widths_mm, calibration_px, event_pairs, strict=True
        )
    ]

    labels = {
        "schema": LABEL_SCHEMA,
        "arm": args.arm,
        "capture_root": str(capture_root),
        "width_sequence_source": width_source,
        "timestamp_log": timestamp_identity,
        "clock_calibration": clock_contract,
        "calibration_id": timeline.calibration_id,
        "segments": [
            {
                "ordinal": ordinal,
                "segment_index": pair.segment_index,
                "width_mm": width_mm,
                "width_m": width_mm / 1000.0,
                "start": {
                    "event_id": pair.start_id,
                    "event_uuid": pair.start_uuid,
                    "line_number": pair.start_line,
                    "unix_time_ms": pair.start_unix_time_ms,
                    "unix_time_ns": pair.start_unix_time_ns,
                },
                "end": {
                    "event_id": pair.end_id,
                    "event_uuid": pair.end_uuid,
                    "line_number": pair.end_line,
                    "unix_time_ms": pair.end_unix_time_ms,
                    "unix_time_ns": pair.end_unix_time_ns,
                },
            }
            for ordinal, (width_mm, pair) in enumerate(
                zip(widths_mm, event_pairs, strict=True), start=1
            )
        ],
    }
    mapping_summary = {
        "frame_count": len(all_errors_mm),
        "median_absolute_error_mm": float(median(all_errors_mm)),
        "p95_absolute_error_mm": quantile(all_errors_mm, 0.95),
        "p99_absolute_error_mm": quantile(all_errors_mm, 0.99),
        "maximum_absolute_error_mm": max(all_errors_mm),
    }
    report = {
        "schema": REPORT_SCHEMA,
        "status": "accepted",
        "method": {
            "knot_estimator": (
                "median_primary_dual_marker_distance_px_from_"
                + (
                    "unique_longest_stable_plateau"
                    if plateau_selection_policy == "unique_longest"
                    else "event_start_anchored_earliest_stable_plateau"
                )
            ),
            "calibration_evidence": "primary_dual_marker_only",
            "adaptive_two_marker_policy": "diagnostic_only_never_used_for_fit",
            "single_marker_policy": "excluded",
            "filled_measurement_policy": "excluded",
            "segment_mapping": "active_frame_ranges_half_open_calibrated_timeline",
            "edge_trim_ms": edge_trim_ms,
            "stable_plateau_selection": (
                "deterministic_longest_overlapping_stable_window_component_v1"
                if plateau_selection_policy == "unique_longest"
                else "event_start_anchored_earliest_stable_component_v1"
            ),
            "plateau_selection_policy": plateau_selection_policy,
            "event_start_semantics": (
                "physical_width_established_before_segment_start"
                if plateau_selection_policy == "event_start_anchored"
                else None
            ),
            "interpolation": "piecewise_linear_clamped_like_numpy_interp",
        },
        "inputs": {
            "capture_root": str(capture_root),
            "arm": args.arm,
            "capture_manifest": capture_contract,
            "timestamp_log": timestamp_identity,
            "gripper_csv": gripper_identity,
            "calibrated_timeline": timeline_identity,
            "clock_calibration": clock_contract,
            "calibration_id": timeline.calibration_id,
            "source_frame_count": len(timeline.frame_idx),
            "implementation": file_identity(Path(__file__)),
            "active_frame_ranges_implementation": file_identity(
                DATAPROCESS_ROOT / "active_frame_ranges.py"
            ),
        },
        "width_sequence": {
            "source": width_source,
            "values_mm": widths_mm,
        },
        "event_binding": {
            "complete_segment_count": len(event_pairs),
            "all_segments_have_unique_start_end_uuids": True,
            "timestamp_log_sha256": timestamp_identity["sha256"],
        },
        "quality_gates": {
            "minimum_trimmed_frames_per_segment": args.min_trimmed_frames,
            "minimum_primary_frames_per_segment": args.min_primary_frames,
            "minimum_primary_detection_rate": args.min_primary_detection_rate,
            "maximum_primary_mad_px": args.max_primary_mad_px,
            "minimum_stable_plateau_duration_ms": (
                args.min_stable_plateau_duration_ms
            ),
            "minimum_stable_plateau_primary_frames": args.min_stable_plateau_frames,
            "maximum_stable_window_p95_abs_deviation_px": (
                args.max_stable_window_p95_deviation_px
            ),
            "maximum_stable_plateau_abs_slope_px_per_sec": (
                args.max_stable_plateau_slope_px_per_sec
            ),
            "minimum_plateau_winner_duration_margin_ms": (
                args.min_plateau_uniqueness_margin_ms
                if plateau_selection_policy == "unique_longest"
                else None
            ),
            "maximum_event_start_delay_ms": (
                max_event_start_delay_ms
                if plateau_selection_policy == "event_start_anchored"
                else None
            ),
            "minimum_adjacent_pixel_gap": args.min_adjacent_pixel_gap,
            "maximum_per_segment_frame_error_p95_mm": args.max_frame_error_p95_mm,
            "maximum_any_frame_error_mm": args.max_frame_error_mm,
            "passed": True,
        },
        "summary": {
            "segment_count": len(segment_stats),
            "primary_frame_count": sum(item["primary_count"] for item in segment_stats),
            "edge_trimmed_primary_frame_count_before_plateau_selection": sum(
                item["trimmed_primary_count_all"] for item in segment_stats
            ),
            "motion_or_nonselected_frame_count": sum(
                item["plateau_excluded_frame_count"] for item in segment_stats
            ),
            "adaptive_frame_count_diagnostic_only": sum(
                item["adaptive_count_diagnostic_only"] for item in segment_stats
            ),
            "minimum_primary_detection_rate": min(
                item["primary_detection_rate"] for item in segment_stats
            ),
            "maximum_primary_mad_px": max(
                item["primary_mad_px"] for item in segment_stats
            ),
            "minimum_adjacent_pixel_gap": min(
                after - before
                for before, after in zip(calibration_px, calibration_px[1:])
            ),
            "frame_mapping_error": mapping_summary,
        },
        "calibration_points": calibration,
        "segment_stats": segment_stats,
        "output_contract": {
            "calibration_filename": CALIBRATION_FILENAME,
            "legacy_loader_compatible_list_json": True,
            "existing_repository_calibration_modified": False,
            "existing_input_modified": False,
        },
    }
    artifacts = {
        CALIBRATION_FILENAME: json_bytes(calibration),
        LABELS_FILENAME: json_bytes(labels),
        STATS_FILENAME: stats_csv_bytes(segment_stats),
        REPORT_FILENAME: json_bytes(report),
    }
    return artifacts, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an arm-specific pixel-distance to physical gripper-width "
            "calibration from timestamped held-width segments."
        )
    )
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--arm", choices=("left", "right"), required=True)
    parser.add_argument("--gripper-csv", type=Path, required=True)
    parser.add_argument("--calibrated-timeline", type=Path, required=True)
    parser.add_argument(
        "--clock-calibration-report",
        type=Path,
        required=True,
        help=(
            "Accepted _01 calibration.json which cryptographically binds the "
            "calibrated timeline to this capture and arm."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--timestamp-log",
        type=Path,
        default=None,
        help="Defaults to <capture-root>/timestamp_log.jsonl.",
    )
    parser.add_argument(
        "--widths-mm",
        type=float,
        nargs="+",
        default=None,
        help="Explicit widths in segment order. Default: 0 5 ... 85 (18 labels).",
    )
    parser.add_argument("--edge-trim-ms", type=float, default=250.0)
    parser.add_argument("--min-trimmed-frames", type=int, default=20)
    parser.add_argument("--min-primary-frames", type=int, default=20)
    parser.add_argument("--min-primary-detection-rate", type=float, default=0.80)
    parser.add_argument("--max-primary-mad-px", type=float, default=2.0)
    parser.add_argument("--min-stable-plateau-duration-ms", type=float, default=750.0)
    parser.add_argument("--min-stable-plateau-frames", type=int, default=30)
    parser.add_argument(
        "--max-stable-window-p95-deviation-px", type=float, default=1.5
    )
    parser.add_argument(
        "--max-stable-plateau-slope-px-per-sec", type=float, default=1.5
    )
    parser.add_argument(
        "--min-plateau-uniqueness-margin-ms", type=float, default=250.0
    )
    parser.add_argument(
        "--plateau-selection-policy",
        choices=("unique_longest", "event_start_anchored"),
        default="unique_longest",
        help=(
            "Use the generic unique-longest platform rule, or require the earliest "
            "stable platform to begin near segment_start when acquisition labels "
            "are emitted only after establishing the physical width."
        ),
    )
    parser.add_argument(
        "--max-event-start-delay-ms",
        type=float,
        default=100.0,
        help=(
            "Maximum stable-candidate start delay for event_start_anchored mode; "
            "later candidates are rejected rather than used as fallback."
        ),
    )
    parser.add_argument("--min-adjacent-pixel-gap", type=float, default=2.0)
    parser.add_argument("--max-frame-error-p95-mm", type=float, default=1.0)
    parser.add_argument("--max-frame-error-mm", type=float, default=3.0)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.min_trimmed_frames < 2:
        raise CalibrationError("--min-trimmed-frames must be >= 2")
    if args.min_primary_frames < 2:
        raise CalibrationError("--min-primary-frames must be >= 2")
    if args.min_stable_plateau_frames < 2:
        raise CalibrationError("--min-stable-plateau-frames must be >= 2")
    if not 0.0 < args.min_primary_detection_rate <= 1.0:
        raise CalibrationError("--min-primary-detection-rate must be in (0, 1]")
    for name in (
        "max_primary_mad_px",
        "min_stable_plateau_duration_ms",
        "max_stable_window_p95_deviation_px",
        "max_stable_plateau_slope_px_per_sec",
        "min_plateau_uniqueness_margin_ms",
        "max_event_start_delay_ms",
        "min_adjacent_pixel_gap",
        "max_frame_error_p95_mm",
        "max_frame_error_mm",
    ):
        value = finite_float(getattr(args, name), description=name)
        if value <= 0.0:
            raise CalibrationError(f"--{name.replace('_', '-')} must be > 0")
    if args.plateau_selection_policy == "event_start_anchored":
        if args.edge_trim_ms != 0.0:
            raise CalibrationError(
                "event_start_anchored selection requires --edge-trim-ms 0; "
                "the start-delay gate is the only permitted leading guard"
            )
    output = args.output_dir.expanduser().resolve()
    input_paths = {
        args.capture_root.expanduser().resolve(),
        args.gripper_csv.expanduser().resolve(),
        args.calibrated_timeline.expanduser().resolve(),
        args.clock_calibration_report.expanduser().resolve(),
    }
    if args.timestamp_log is not None:
        input_paths.add(args.timestamp_log.expanduser().resolve())
    if output in input_paths:
        raise CalibrationError("output directory must be distinct from every input")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        artifacts, report = build_artifacts(args)
        disposition = publish_artifacts(args.output_dir, artifacts)
    except WidthCountMismatchError as exc:
        try:
            disposition = publish_artifacts(
                args.output_dir,
                {REPORT_FILENAME: json_bytes(exc.report)},
            )
        except CalibrationError as publish_exc:
            parser.exit(
                1,
                "[segmented-calibration:error] "
                f"{exc}; rejected report publication failed: {publish_exc}\n",
            )
        parser.exit(
            1,
            "[segmented-calibration:rejected] "
            f"{exc}; report_status={disposition} "
            f"report={args.output_dir.expanduser().resolve() / REPORT_FILENAME}; "
            f"{CALIBRATION_FILENAME}=absent\n",
        )
    except CalibrationError as exc:
        parser.exit(1, f"[segmented-calibration:error] {exc}\n")
    print(
        "[segmented-calibration:complete] "
        f"status={disposition} arm={args.arm} "
        f"segments={report['summary']['segment_count']} "
        f"primary_frames={report['summary']['primary_frame_count']} "
        f"output={args.output_dir.expanduser().resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
