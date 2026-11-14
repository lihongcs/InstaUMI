#!/usr/bin/env python3
"""Ingest a completed DataCap USB/UVC capture without masquerading as INSV.

The UVC worker stores the native V4L2 MJPEG packets verbatim in
``video.mjpeg`` and records the authoritative packet boundaries/timestamps in
``frames.jsonl``.  This stage validates that completed contract first and
remuxes the selected camera-native profile to a seekable MP4 without
transcoding or reprojection.  Only an explicitly recognized legacy panorama
v1 capture retains its historical derived-view compatibility artifacts.

No camera device is enumerated or opened by this program.  Its only media
input is a completed capture directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DATAPROCESS_ROOT = Path(__file__).resolve().parents[1]
if str(DATAPROCESS_ROOT) not in sys.path:
    sys.path.insert(0, str(DATAPROCESS_ROOT))

from uvc_capture_source import (  # noqa: E402
    NORMALIZED_SCHEMA,
    UvcCaptureSource,
    UvcRoleSource,
    validate_uvc_capture,
)
from capture_schema import normalize_tracker_role  # noqa: E402


RAW_EXTRACT_SCHEMA = "instaumi_raw_extract_v1"
UVC_STAGE_SCHEMA = "instaumi_uvc_stage01_v1"
SYNC_SCHEMA = "instaumi-uvc-host-clock-alignment-v1-monotonic-raw"
BIARM_SCHEMA = "instaumi_biarm_raw_extract_v1"
ROLE_TO_ARM = {"insta_right": "right", "insta_left": "left"}
FPS = 30
PANORAMA_WIDTH = 2880
PANORAMA_HEIGHT = 1440
FRONT_WIDTH = 1440
FRONT_HEIGHT = 1440
MODEL_WIDTH = 224
MODEL_HEIGHT = 224
SIDE_PROJECTION_SIZE = 720
UVC_COMPLETE_SCHEMA = "instaumi_uvc_stage01_complete_v1"
DEFAULT_MAX_RAW_CLOCK_ERROR_NS = 1_000_000
MIN_CAMERA_HTC_OVERLAP_FRACTION = 0.95
MAX_TIMESTAMP_LOG_ROW_BYTES = 1024 * 1024
TIMESTAMP_START_ORDER_TOLERANCE_NS = 5_000_000_000
TIMESTAMP_STOP_COMPLETION_GRACE_NS = 60_000_000_000


class UvcStage01Error(RuntimeError):
    """The completed UVC source cannot be admitted to stage 01."""


@dataclass(frozen=True)
class MediaProbe:
    codec: str
    width: int
    height: int
    fps: float
    frame_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frame_count": self.frame_count,
        }


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path, *, known_sha256: str | None = None) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    metadata = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "sha256": known_sha256 or sha256_file(resolved),
    }


def preserve_file(source: Path, destination: Path) -> str:
    """Preserve a source with a hard link when possible, otherwise copy it."""

    source = source.expanduser().resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite staged source: {destination}")
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy2"


def require_regular_nonsymlink(path: Path, label: str) -> Path:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise UvcStage01Error(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        kind = "symlink" if stat.S_ISLNK(metadata.st_mode) else "non-regular file"
        raise UvcStage01Error(f"{label} must be a regular non-symlink file ({kind}): {path}")
    return path.resolve(strict=True)


def discover_timestamp_log(session_dir: Path) -> Path:
    canonical = session_dir / "timestamp_log.jsonl"
    if os.path.lexists(canonical):
        return require_regular_nonsymlink(canonical, "timestamp log")
    alternatives = sorted(session_dir.glob("timestamp_log_*.jsonl"))
    if len(alternatives) != 1:
        raise UvcStage01Error(
            "UVC stage 01 requires timestamp_log.jsonl or exactly one "
            f"timestamp_log_*.jsonl; found {len(alternatives)}"
        )
    return require_regular_nonsymlink(alternatives[0], "timestamp log")


def discover_tracker(session_dir: Path, arm: str) -> Path:
    dedicated = session_dir / f"htc_{arm}.csv"
    if os.path.lexists(dedicated):
        return require_regular_nonsymlink(dedicated, f"{arm} HTC CSV")
    shared = session_dir / "tracker_data.csv"
    if os.path.lexists(shared):
        return require_regular_nonsymlink(shared, "shared HTC CSV")
    raise UvcStage01Error(
        f"UVC role {arm!r} has no htc_{arm}.csv and no tracker_data.csv"
    )


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise UvcStage01Error(f"{label} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UvcStage01Error(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise UvcStage01Error(f"{label} must be a positive integer")
    return parsed


def _event_raw_time(event: Mapping[str, Any], label: str) -> tuple[int, str]:
    for key in ("monotonic_raw_time_ns", "input_handler_monotonic_raw_time_ns"):
        value = event.get(key)
        if value is not None:
            return _positive_integer(value, f"{label}.{key}"), key
    raise UvcStage01Error(
        f"{label} has neither monotonic_raw_time_ns nor "
        "input_handler_monotonic_raw_time_ns"
    )


def validate_timestamp_log_session(
    path: Path,
    *,
    session_dir: Path,
    role_sources: Mapping[str, UvcRoleSource],
) -> dict[str, Any]:
    """Bind the timestamp log to this completed UVC RAW-clock interval.

    A file hash proves immutability, not that the file belongs to this capture.
    Current DataCap UVC sessions therefore need exactly one successful
    ``total_start``/``total_end`` pair in one session id.  Their RAW clock
    interval must contain camera start and the final camera SOE must follow the
    stop request by no more than the bounded worker-flush grace period.
    """

    events: list[tuple[int, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if len(line.encode("utf-8")) > MAX_TIMESTAMP_LOG_ROW_BYTES:
                raise UvcStage01Error(
                    f"timestamp log row exceeds {MAX_TIMESTAMP_LOG_ROW_BYTES} bytes: "
                    f"{path}:{line_number}"
                )
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise UvcStage01Error(
                    f"invalid timestamp log JSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise UvcStage01Error(
                    f"timestamp log row is not an object: {path}:{line_number}"
                )
            events.append((line_number, value))
    if not events:
        raise UvcStage01Error(f"timestamp log is empty: {path}")

    starts = [(line, row) for line, row in events if row.get("type") == "total_start"]
    ends = [(line, row) for line, row in events if row.get("type") == "total_end"]
    if len(starts) != 1 or len(ends) != 1:
        raise UvcStage01Error(
            "UVC timestamp log requires exactly one total_start and one successful "
            f"total_end; found start={len(starts)} end={len(ends)} in {path}"
        )
    start_line, start = starts[0]
    end_line, end = ends[0]
    if end_line <= start_line:
        raise UvcStage01Error("timestamp log total_end precedes total_start")

    start_extra = start.get("extra")
    end_extra = end.get("extra")
    if not isinstance(start_extra, dict) or not isinstance(end_extra, dict):
        raise UvcStage01Error("timestamp log total start/end lacks adapter evidence")
    for name in ("adapter_prepare", "adapter_start"):
        result = start_extra.get(name)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise UvcStage01Error(f"timestamp log total_start has no successful {name}")
    stop = end_extra.get("adapter_stop")
    if not isinstance(stop, dict) or stop.get("ok") is not True:
        raise UvcStage01Error("timestamp log total_end has no successful adapter_stop")
    stop_extra = stop.get("extra")
    if not isinstance(stop_extra, dict):
        raise UvcStage01Error("timestamp log adapter_stop lacks UVC completion evidence")
    if stop_extra.get("completion_status") != "complete":
        raise UvcStage01Error(
            "timestamp log adapter_stop does not declare completion_status=complete"
        )
    completion_value = stop_extra.get("completion_marker")
    if not isinstance(completion_value, str) or Path(completion_value).name != (
        "uvc_capture_complete.json"
    ):
        raise UvcStage01Error(
            "timestamp log adapter_stop does not reference uvc_capture_complete.json"
        )

    start_session = str(start.get("session_id") or "").strip()
    end_session = str(end.get("session_id") or "").strip()
    if not start_session or start_session != end_session:
        raise UvcStage01Error(
            "timestamp log total_start/total_end do not share one non-empty session_id"
        )
    session_manifest = session_dir / "manifest.json"
    session_manifest_id: dict[str, Any] | None = None
    if os.path.lexists(session_manifest):
        session_manifest = require_regular_nonsymlink(
            session_manifest, "DataCap session manifest"
        )
        session_payload = read_json_object(session_manifest, "DataCap session manifest")
        if str(session_payload.get("session_id") or "").strip() != start_session:
            raise UvcStage01Error(
                "timestamp log session_id disagrees with the DataCap session manifest"
            )
        session_manifest_id = file_identity(session_manifest)

    start_raw, start_field = _event_raw_time(start, "total_start")
    end_raw, end_field = _event_raw_time(end, "total_end")
    if end_raw <= start_raw:
        raise UvcStage01Error(
            "timestamp log total_end RAW time is not after total_start RAW time"
        )
    camera_start = min(
        role.records[0].preferred_timestamp_ns for role in role_sources.values()
    )
    camera_end = max(
        role.records[-1].preferred_timestamp_ns for role in role_sources.values()
    )
    if camera_start < start_raw - TIMESTAMP_START_ORDER_TOLERANCE_NS:
        raise UvcStage01Error(
            "UVC camera RAW interval starts before this timestamp-log session: "
            f"camera_start={camera_start} total_start={start_raw}"
        )
    if camera_start > end_raw:
        raise UvcStage01Error(
            "UVC camera RAW interval starts after timestamp-log total_end; "
            "the log may belong to another session or boot"
        )
    if camera_end > end_raw + TIMESTAMP_STOP_COMPLETION_GRACE_NS:
        raise UvcStage01Error(
            "UVC camera RAW interval ends too far after timestamp-log total_end: "
            f"camera_end={camera_end} total_end={end_raw}; the log may belong "
            "to another session or boot"
        )

    return {
        "status": "accepted",
        "session_id": start_session,
        "timestamp_log": str(path),
        "event_count": len(events),
        "total_start_line": start_line,
        "total_end_line": end_line,
        "total_start_monotonic_raw_ns": start_raw,
        "total_end_monotonic_raw_ns": end_raw,
        "total_start_raw_field": start_field,
        "total_end_raw_field": end_field,
        "camera_start_monotonic_raw_ns": camera_start,
        "camera_end_monotonic_raw_ns": camera_end,
        "camera_start_minus_total_start_ns": camera_start - start_raw,
        "camera_end_minus_total_end_ns": camera_end - end_raw,
        "maximum_camera_start_order_tolerance_ns": (
            TIMESTAMP_START_ORDER_TOLERANCE_NS
        ),
        "maximum_camera_stop_completion_grace_ns": (
            TIMESTAMP_STOP_COMPLETION_GRACE_NS
        ),
        "uvc_completion_marker": "uvc_capture_complete.json",
        "session_manifest_identity": session_manifest_id,
    }


def validate_tracker_raw_clock(path: Path, arm: str) -> dict[str, Any]:
    """Require the HTC stream to use the same host MONOTONIC_RAW domain."""

    values: list[int] = []
    eligible_rows = 0
    role_name = f"htc_{arm}"
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        required = "hid_read_host_monotonic_raw_ns"
        if required not in fields:
            raise UvcStage01Error(
                f"{path} lacks {required}; UVC camera/HTC alignment will not fall "
                "back to Unix or CLOCK_MONOTONIC"
            )
        for row_number, row in enumerate(reader, start=2):
            raw_role = str(row.get("role") or "").strip()
            row_role = normalize_tracker_role(raw_role)
            if raw_role and row_role != role_name:
                continue
            status = str(row.get("tracking_status") or "").strip()
            if status and status not in {"2", "2.0"}:
                continue
            eligible_rows += 1
            raw = str(row.get(required) or "").strip()
            if not raw:
                continue
            try:
                value = int(raw)
            except ValueError as exc:
                raise UvcStage01Error(
                    f"{path}:{row_number} has invalid {required}={raw!r}"
                ) from exc
            if value <= 0:
                raise UvcStage01Error(
                    f"{path}:{row_number} has non-positive {required}"
                )
            values.append(value)
    if len(values) < 2:
        raise UvcStage01Error(
            f"{path} has fewer than two eligible {role_name} MONOTONIC_RAW samples"
        )
    if any(after <= before for before, after in zip(values, values[1:])):
        raise UvcStage01Error(
            f"{path} {role_name} MONOTONIC_RAW timestamps are not strictly increasing"
        )
    coverage = len(values) / max(1, eligible_rows)
    if coverage < 0.95:
        raise UvcStage01Error(
            f"{path} {role_name} MONOTONIC_RAW coverage is {coverage:.3%}, below 95%"
        )
    return {
        "time_column": "hid_read_host_monotonic_raw_ns",
        "clock_domain": "host_clock_monotonic_raw",
        "eligible_rows": eligible_rows,
        "finite_rows": len(values),
        "coverage": coverage,
        "first_monotonic_raw_ns": values[0],
        "last_monotonic_raw_ns": values[-1],
    }


def require_raw_camera_clock(role_source: UvcRoleSource) -> None:
    missing = [
        record.frame_index
        for record in role_source.records
        if not record.has_monotonic_raw_mapping
    ]
    if missing:
        raise UvcStage01Error(
            f"{role_source.role} was captured by a legacy UVC worker without the "
            "per-frame CLOCK_MONOTONIC_RAW mapping. It can be inspected, but it "
            "cannot enter the strict HTC processing route."
        )


def validate_raw_mapping_error(
    role_source: UvcRoleSource, maximum_error_ns: int
) -> dict[str, Any]:
    if maximum_error_ns < 0:
        raise ValueError("maximum MONOTONIC_RAW mapping error must be non-negative")
    errors = [
        int(record.monotonic_to_raw_error_bound_ns or 0)
        for record in role_source.records
    ]
    observed = max(errors)
    if observed > maximum_error_ns:
        raise UvcStage01Error(
            f"{role_source.role} MONOTONIC_RAW mapping error bound {observed}ns "
            f"exceeds the admitted {maximum_error_ns}ns"
        )
    return {
        "maximum_observed_error_bound_ns": observed,
        "maximum_admitted_error_bound_ns": maximum_error_ns,
        "status": "accepted",
    }


def validate_camera_htc_overlap(
    role_source: UvcRoleSource,
    tracker_clock: Mapping[str, Any],
    *,
    minimum_fraction: float = MIN_CAMERA_HTC_OVERLAP_FRACTION,
) -> dict[str, Any]:
    camera_start = role_source.records[0].preferred_timestamp_ns
    camera_end = role_source.records[-1].preferred_timestamp_ns
    htc_start = int(tracker_clock["first_monotonic_raw_ns"])
    htc_end = int(tracker_clock["last_monotonic_raw_ns"])
    camera_duration = camera_end - camera_start
    if camera_duration <= 0:
        raise UvcStage01Error(f"{role_source.role} camera raw-clock span is empty")
    overlap_start = max(camera_start, htc_start)
    overlap_end = min(camera_end, htc_end)
    overlap_ns = max(0, overlap_end - overlap_start)
    overlap_fraction = overlap_ns / camera_duration
    if overlap_fraction < minimum_fraction:
        raise UvcStage01Error(
            f"{role_source.role} camera/HTC MONOTONIC_RAW ranges do not reasonably "
            f"overlap: camera=[{camera_start},{camera_end}] htc=[{htc_start},{htc_end}] "
            f"coverage={overlap_fraction:.3%}, required={minimum_fraction:.3%}; "
            "the HTC file may belong to another session or boot"
        )
    return {
        "camera_start_monotonic_raw_ns": camera_start,
        "camera_end_monotonic_raw_ns": camera_end,
        "htc_start_monotonic_raw_ns": htc_start,
        "htc_end_monotonic_raw_ns": htc_end,
        "overlap_start_monotonic_raw_ns": overlap_start,
        "overlap_end_monotonic_raw_ns": overlap_end,
        "overlap_duration_ns": overlap_ns,
        "camera_span_coverage": overlap_fraction,
        "minimum_camera_span_coverage": minimum_fraction,
        "status": "accepted",
    }


def calibration_id_for(source: UvcCaptureSource, settings: Mapping[str, Any]) -> str:
    evidence = {
        "schema": SYNC_SCHEMA,
        "normalized_source": source.normalized_manifest,
        "settings": settings,
    }
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "uvc-" + hashlib.sha256(encoded).hexdigest()[:24]


def build_input_identities(
    *,
    source: UvcCaptureSource,
    timestamp_log: Path,
    trackers: Mapping[str, Path],
) -> dict[str, Any]:
    """Content-bind all timing and native packet inputs used by stage 01."""

    roles: dict[str, Any] = {}
    for role, role_source in source.roles.items():
        roles[role] = {
            "packet_stream": file_identity(
                role_source.video_path, known_sha256=role_source.video_sha256
            ),
            "packet_index": file_identity(
                role_source.frames_path, known_sha256=role_source.frames_sha256
            ),
            "worker_manifest": file_identity(
                role_source.manifest_path,
                known_sha256=role_source.worker_manifest_sha256,
            ),
        }
    timestamp_log = require_regular_nonsymlink(timestamp_log, "timestamp log")
    regular_trackers = {
        arm: require_regular_nonsymlink(path, f"{arm} HTC CSV")
        for arm, path in trackers.items()
    }
    return {
        "timestamp_log": file_identity(timestamp_log),
        "htc": {
            arm: file_identity(path) for arm, path in regular_trackers.items()
        },
        "uvc_capture_manifest": file_identity(
            source.session_dir / "uvc_capture_manifest.json"
        ),
        "uvc_capture_complete": file_identity(
            source.session_dir / "uvc_capture_complete.json"
        ),
        "roles": roles,
    }


def write_frame_timeline(
    path: Path,
    role_source: UvcRoleSource,
    calibration_id: str,
) -> None:
    require_raw_camera_clock(role_source)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fieldnames = [
        "frame_idx",
        "original_concat_frame_idx",
        "timestamp_sec",
        "fps",
        "calibrated_host_monotonic_raw_ns",
        "calibrated_host_monotonic_raw_sec",
        "source_soe_monotonic_ns",
        "source_sequence",
        "source_interval_ms",
        "monotonic_to_raw_offset_ns",
        "monotonic_to_raw_error_bound_ns",
        "timeline_valid",
        "recommended_keep",
        "calibration_id",
        "alignment_method",
        "unix_time_ms",
    ]
    first_raw = role_source.records[0].preferred_timestamp_ns
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in role_source.records:
            raw_ns = record.soe_monotonic_raw_ns
            if raw_ns is None:
                raise AssertionError("raw clock was validated before timeline writing")
            writer.writerow(
                {
                    "frame_idx": record.frame_index,
                    "original_concat_frame_idx": record.frame_index,
                    "timestamp_sec": f"{(raw_ns - first_raw) / 1e9:.9f}",
                    "fps": f"{FPS:.9f}",
                    "calibrated_host_monotonic_raw_ns": raw_ns,
                    "calibrated_host_monotonic_raw_sec": f"{raw_ns / 1e9:.9f}",
                    "source_soe_monotonic_ns": record.soe_monotonic_ns,
                    "source_sequence": record.sequence,
                    "source_interval_ms": f"{record.source_interval_ms:.6f}",
                    "monotonic_to_raw_offset_ns": record.monotonic_to_raw_offset_ns,
                    "monotonic_to_raw_error_bound_ns": (
                        record.monotonic_to_raw_error_bound_ns
                    ),
                    "timeline_valid": 1,
                    "recommended_keep": 1,
                    "calibration_id": calibration_id,
                    "alignment_method": "direct_v4l2_soe_to_host_monotonic_raw",
                    # CLOCK_MONOTONIC_RAW is deliberately not mislabeled as Unix.
                    "unix_time_ms": "",
                }
            )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def parse_fraction(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    if separator:
        return float(numerator) / float(denominator)
    return float(value)


def probe_video(path: Path, ffprobe_bin: str) -> MediaProbe:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if len(streams) != 1:
        raise UvcStage01Error(f"expected one video stream in {path}, got {len(streams)}")
    stream = streams[0]
    raw_count = stream.get("nb_read_frames") or stream.get("nb_frames")
    if raw_count in (None, "N/A"):
        raise UvcStage01Error(f"ffprobe could not count frames in {path}")
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    return MediaProbe(
        codec=str(stream.get("codec_name") or ""),
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=parse_fraction(str(rate)),
        frame_count=int(raw_count),
    )


def run_checked(command: Sequence[str], label: str) -> None:
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-8000:]
        raise UvcStage01Error(
            f"{label} failed with exit code {result.returncode}:\n{detail}"
        )


def verify_media(
    path: Path,
    ffprobe_bin: str,
    *,
    frames: int,
    width: int,
    height: int,
    codec: str | None = None,
) -> MediaProbe:
    probe = probe_video(path, ffprobe_bin)
    if probe.frame_count != frames:
        raise UvcStage01Error(
            f"{path} has {probe.frame_count} frames, expected {frames}; refusing "
            "a silent decode drop or duplicate"
        )
    if (probe.width, probe.height) != (width, height):
        raise UvcStage01Error(
            f"{path} geometry is {probe.width}x{probe.height}, expected {width}x{height}"
        )
    if not math.isclose(probe.fps, FPS, rel_tol=0.0, abs_tol=0.001):
        raise UvcStage01Error(f"{path} is {probe.fps:.6f} fps, expected {FPS} fps")
    if codec is not None and probe.codec != codec:
        raise UvcStage01Error(f"{path} codec is {probe.codec!r}, expected {codec!r}")
    return probe


def _partial_mp4(path: Path) -> Path:
    return path.with_name(f".{path.stem}.{os.getpid()}.partial.mp4")


def remux_panorama(
    source: UvcRoleSource,
    output: Path,
    *,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> MediaProbe:
    """Backward-compatible wrapper for the legacy panorama profile."""

    return remux_native_profile(
        source,
        output,
        width=PANORAMA_WIDTH,
        height=PANORAMA_HEIGHT,
        profile_name="panorama",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
    )


def remux_native_profile(
    source: UvcRoleSource,
    output: Path,
    *,
    width: int,
    height: int,
    profile_name: str,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> MediaProbe:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_mp4(output)
    partial.unlink(missing_ok=True)
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-fflags",
        "+genpts",
        "-f",
        "mjpeg",
        "-framerate",
        str(FPS),
        "-i",
        str(source.video_path),
        "-map",
        "0:v:0",
        "-frames:v",
        str(len(source.records)),
        "-an",
        "-c:v",
        "copy",
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(partial),
    ]
    try:
        run_checked(
            command,
            f"{source.role} no-pixel-transcode {profile_name} profile remux",
        )
        probe = verify_media(
            partial,
            ffprobe_bin,
            frames=len(source.records),
            width=width,
            height=height,
            codec="mjpeg",
        )
        os.replace(partial, output)
        return probe
    finally:
        partial.unlink(missing_ok=True)


def normalized_angle(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("projection angles must be finite")
    value = math.fmod(value, 360.0)
    if value > 180.0:
        value -= 360.0
    if value < -180.0:
        value += 360.0
    return value


def _number(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def render_views(
    panorama: Path,
    output_dir: Path,
    *,
    frames: int,
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    threads: int,
) -> tuple[dict[str, Path], dict[str, MediaProbe]]:
    yaw = normalized_angle(yaw_deg)
    left_yaw = normalized_angle(yaw - 90.0)
    right_yaw = normalized_angle(yaw + 90.0)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "front_fisheye": output_dir / "front_fisheye.mp4",
        "front_fisheye_224": output_dir / "front_fisheye_224x224.mp4",
        "left_view": output_dir / "left_view_fov90x90.mp4",
        "right_view": output_dir / "right_view_fov90x90.mp4",
    }
    partials = {name: _partial_mp4(path) for name, path in outputs.items()}
    for path in partials.values():
        path.unlink(missing_ok=True)
    graph = (
        "[0:v]split=3[front_src][left_src][right_src];"
        "[front_src]v360=input=equirect:output=fisheye:interp=line:"
        f"yaw={_number(yaw)}:pitch={_number(pitch_deg)}:roll={_number(roll_deg)}:"
        f"h_fov=180:v_fov=180:w={FRONT_WIDTH}:h={FRONT_HEIGHT},"
        "format=rgb24,split=2[front_full][front_model_src];"
        f"[front_model_src]scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear,"
        "format=rgb24[front_model];"
        "[left_src]v360=input=equirect:output=flat:interp=line:"
        f"yaw={_number(left_yaw)}:pitch={_number(pitch_deg)}:roll={_number(roll_deg)}:"
        f"h_fov=90:v_fov=90:w={SIDE_PROJECTION_SIZE}:h={SIDE_PROJECTION_SIZE},"
        f"scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear,format=rgb24[left];"
        "[right_src]v360=input=equirect:output=flat:interp=line:"
        f"yaw={_number(right_yaw)}:pitch={_number(pitch_deg)}:roll={_number(roll_deg)}:"
        f"h_fov=90:v_fov=90:w={SIDE_PROJECTION_SIZE}:h={SIDE_PROJECTION_SIZE},"
        f"scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear,format=rgb24[right]"
    )
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        str(panorama),
        "-filter_complex",
        graph,
    ]
    for label, name in (
        ("[front_full]", "front_fisheye"),
        ("[front_model]", "front_fisheye_224"),
        ("[left]", "left_view"),
        ("[right]", "right_view"),
    ):
        command += [
            "-map",
            label,
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "libx264rgb",
            "-preset",
            "veryfast",
            "-crf",
            "0",
            "-pix_fmt",
            "rgb24",
            "-g",
            str(FPS),
            "-threads",
            str(max(1, threads)),
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(partials[name]),
        ]
    try:
        run_checked(command, "UVC panorama view rendering")
        probes = {
            "front_fisheye": verify_media(
                partials["front_fisheye"],
                ffprobe_bin,
                frames=frames,
                width=FRONT_WIDTH,
                height=FRONT_HEIGHT,
            ),
            "front_fisheye_224": verify_media(
                partials["front_fisheye_224"],
                ffprobe_bin,
                frames=frames,
                width=MODEL_WIDTH,
                height=MODEL_HEIGHT,
            ),
            "left_view": verify_media(
                partials["left_view"],
                ffprobe_bin,
                frames=frames,
                width=MODEL_WIDTH,
                height=MODEL_HEIGHT,
            ),
            "right_view": verify_media(
                partials["right_view"],
                ffprobe_bin,
                frames=frames,
                width=MODEL_WIDTH,
                height=MODEL_HEIGHT,
            ),
        }
        for name, destination in outputs.items():
            os.replace(partials[name], destination)
        return outputs, probes
    finally:
        for path in partials.values():
            path.unlink(missing_ok=True)


def role_projection(
    capture_contract: Mapping[str, Any], arm: str, args: argparse.Namespace
) -> tuple[float, float, float]:
    flat = capture_contract.get("flat_projection")
    flat = flat if isinstance(flat, dict) else {}
    yaw = float(flat.get("yaw_deg", 0.0))
    pitch = float(flat.get("pitch_deg", 0.0))
    roll = float(flat.get("roll_deg", 0.0))
    override = getattr(args, f"{arm}_yaw_deg")
    if override is not None:
        yaw = float(override)
    override = getattr(args, f"{arm}_pitch_deg")
    if override is not None:
        pitch = float(override)
    override = getattr(args, f"{arm}_roll_deg")
    if override is not None:
        roll = float(override)
    return normalized_angle(yaw), normalized_angle(pitch), normalized_angle(roll)


def native_source_profile(source: UvcCaptureSource) -> dict[str, Any]:
    profile = source.normalized_manifest.get("source_profile")
    if not isinstance(profile, Mapping):
        raise UvcStage01Error("normalized UVC source is missing source_profile")
    required = (
        "view_mode",
        "width",
        "height",
        "pixel_format",
        "fps",
        "semantic",
        "projection",
        "layout",
        "kind",
        "compression",
        "source",
        "legacy_panorama_v1",
    )
    missing = [key for key in required if key not in profile]
    if missing:
        raise UvcStage01Error(
            f"normalized UVC source_profile is missing {missing[0]}"
        )
    return dict(profile)


def ensure_no_projection_overrides(args: argparse.Namespace) -> None:
    for arm in ("right", "left"):
        for axis in ("yaw", "pitch", "roll"):
            if getattr(args, f"{arm}_{axis}_deg", None) is not None:
                raise UvcStage01Error(
                    "camera-native UVC profiles do not accept host projection "
                    f"overrides ({arm}_{axis}_deg was set)"
                )


def build_role_stage(
    *,
    source: UvcCaptureSource,
    role_source: UvcRoleSource,
    arm: str,
    stage_raw_dir: Path,
    final_raw_dir: Path,
    stage_timeline: Path,
    final_timeline: Path,
    timestamp_log: Path,
    tracker: Path,
    tracker_clock: Mapping[str, Any],
    raw_mapping_quality: Mapping[str, Any],
    camera_htc_overlap: Mapping[str, Any],
    timestamp_session: Mapping[str, Any],
    calibration_id: str,
    capture_contract: Mapping[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    output = stage_raw_dir
    raw_sources = output / "raw_sources"
    videos_dir = output / "videos"
    raw_sources.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)

    staged_sources = {
        "packet_stream": raw_sources / "uvc_video.mjpeg",
        "packet_index": raw_sources / "uvc_frames.jsonl",
        "worker_manifest": raw_sources / "uvc_worker_manifest.json",
        "capture_contract": raw_sources / "uvc_capture_manifest.json",
        "completion_marker": raw_sources / "uvc_capture_complete.json",
        "timestamp_log": raw_sources / timestamp_log.name,
        "htc": raw_sources / f"htc_{arm}.csv",
    }
    source_paths = {
        "packet_stream": role_source.video_path,
        "packet_index": role_source.frames_path,
        "worker_manifest": role_source.manifest_path,
        "capture_contract": source.session_dir / "uvc_capture_manifest.json",
        "completion_marker": source.session_dir / "uvc_capture_complete.json",
        "timestamp_log": timestamp_log,
        "htc": tracker,
    }
    preserve_methods = {
        key: preserve_file(source_paths[key], destination)
        for key, destination in staged_sources.items()
    }

    profile = native_source_profile(source)
    view_mode = str(profile["view_mode"])
    legacy_panorama_v1 = bool(profile["legacy_panorama_v1"])
    native_master_key = (
        "panorama_master" if view_mode == "panorama" else "native_flat_master"
    )
    native_master_stage = videos_dir / f"{native_master_key}.mp4"
    native_master_probe = remux_native_profile(
        role_source,
        native_master_stage,
        width=int(profile["width"]),
        height=int(profile["height"]),
        profile_name=view_mode,
        ffmpeg_bin=args.ffmpeg_bin,
        ffprobe_bin=args.ffprobe_bin,
    )
    view_paths_stage: dict[str, Path] = {}
    view_probes: dict[str, MediaProbe] = {}
    projection_manifest: dict[str, Any] = {
        "source_projection": profile["projection"],
        "source_layout": profile["layout"],
        "host_reprojection_performed": False,
    }
    if legacy_panorama_v1:
        yaw, pitch, roll = role_projection(capture_contract, arm, args)
        view_paths_stage, view_probes = render_views(
            native_master_stage,
            videos_dir,
            frames=len(role_source.records),
            yaw_deg=yaw,
            pitch_deg=pitch,
            roll_deg=roll,
            ffmpeg_bin=args.ffmpeg_bin,
            ffprobe_bin=args.ffprobe_bin,
            threads=args.ffmpeg_threads,
        )
        projection_manifest |= {
            "host_reprojection_performed": True,
            "legacy_compatibility_only": True,
            "front_yaw_deg": yaw,
            "pitch_deg": pitch,
            "roll_deg": roll,
            "left_yaw_deg": normalized_angle(yaw - 90.0),
            "right_yaw_deg": normalized_angle(yaw + 90.0),
            "requires_operator_orientation_check": True,
        }

    local_timeline = output / "timestamps" / "frame_timestamps.csv"
    write_frame_timeline(local_timeline, role_source, calibration_id)
    stage_timeline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_timeline, stage_timeline)

    final_videos = final_raw_dir / "videos"
    final_raw_sources = final_raw_dir / "raw_sources"
    final_paths = {
        native_master_key: final_videos / native_master_stage.name,
        **{name: final_videos / path.name for name, path in view_paths_stage.items()},
    }
    final_sources = {
        key: final_raw_sources / destination.name
        for key, destination in staged_sources.items()
    }
    role_quality = source.normalized_manifest["roles_detail"][role_source.role][
        "quality"
    ]
    copied_sources: dict[str, Any] = {
        "uvc": {key: str(path) for key, path in final_sources.items()},
        "native_profile_mp4": str(final_paths[native_master_key]),
        "timestamp_log": str(final_sources["timestamp_log"]),
    }
    if view_mode == "panorama":
        copied_sources["panorama_mp4"] = str(final_paths[native_master_key])
    else:
        copied_sources["native_flat_mp4"] = str(final_paths[native_master_key])

    videos_manifest = {key: str(value) for key, value in final_paths.items()}
    video_resolutions = {
        native_master_key: [int(profile["width"]), int(profile["height"])]
    }
    if legacy_panorama_v1:
        videos_manifest["wrist_model"] = str(final_paths["front_fisheye_224"])
        video_resolutions |= {
            "front_fisheye": [FRONT_WIDTH, FRONT_HEIGHT],
            "front_fisheye_224": [MODEL_WIDTH, MODEL_HEIGHT],
            "wrist_model": [MODEL_WIDTH, MODEL_HEIGHT],
            "left_view": [MODEL_WIDTH, MODEL_HEIGHT],
            "right_view": [MODEL_WIDTH, MODEL_HEIGHT],
        }

    image_pipeline = (
        "uvc_equirectangular_views_v1_legacy_compatibility"
        if legacy_panorama_v1
        else "uvc_camera_native_profile_v1_no_reprojection"
    )
    limitations = [
        "The authoritative native source is lossy camera MJPEG.",
        "No gyro is available; INSV gyro-correlation calibration is inapplicable.",
        "Existing INSV gripper calibration must not be reused for this UVC profile.",
    ]
    if legacy_panorama_v1:
        limitations.insert(
            1,
            (
                "Legacy compatibility views use v360 linear + FFmpeg bilinear; "
                "the preserved panorama master and packet stream are not modified."
            ),
        )
    native_media = {
        **native_master_probe.as_dict(),
        "name": native_master_key,
        "view_mode": view_mode,
        "semantic": profile["semantic"],
        "projection": profile["projection"],
        "layout": profile["layout"],
        "operation": "mjpeg_stream_copy_into_mp4",
        "pixel_transcode": False,
        "host_reprojection": False,
        "container_packet_bytes_guaranteed_identical": False,
        "authoritative_packet_boundaries": str(final_sources["packet_index"]),
    }

    manifest = {
        # Keep the generic stage-01 envelope consumed by legacy video readers;
        # source_family below explicitly prevents INSV-only paths.
        "schema": RAW_EXTRACT_SCHEMA,
        "source_stage_schema": UVC_STAGE_SCHEMA,
        "source_family": "usb_uvc",
        "capture_transport": "usb_uvc",
        "episode": arm,
        "camera_role": role_source.role,
        "input_dir": str(source.session_dir),
        "source_profile": profile,
        "source_profile_dispatch": (
            "legacy_panorama_views"
            if legacy_panorama_v1
            else f"native_{view_mode}_passthrough"
        ),
        # Deliberately no copied_sources.insv key.
        "copied_sources": copied_sources,
        "master_clock": {
            "source": str(final_sources["packet_index"]),
            "fps": str(FPS),
            "frame_count": len(role_source.records),
            "clock_domain": "host_clock_monotonic_raw",
            "timestamp_field": "soe_monotonic_raw_ns",
            "container_pts": "generated_constant_30fps_for_decode_only",
            "sensor_to_driver_latency": "unknown",
        },
        "time_reference": {
            "status": "accepted",
            "method": "direct_v4l2_soe_to_host_monotonic_raw",
            "clock_domain": "host_clock_monotonic_raw",
            "frame0_monotonic_raw_ns": role_source.records[0].preferred_timestamp_ns,
            "calibration_id": calibration_id,
            "raw_mapping_quality": dict(raw_mapping_quality),
        },
        "time_calibration": {
            "status": "accepted",
            "method": "direct_shared_host_monotonic_raw_no_cross_correlation",
            "calibrated_frame_timestamps_csv": str(final_timeline),
            "report": str(final_timeline.parents[1] / "calibration.json"),
            "calibration_id": calibration_id,
            "camera_htc_overlap": dict(camera_htc_overlap),
            "timestamp_log_session": dict(timestamp_session),
        },
        "timestamps_csv": str(final_raw_dir / "timestamps/frame_timestamps.csv"),
        "image_pipeline": image_pipeline,
        "videos": videos_manifest,
        "video_resolutions": video_resolutions,
        "media": {
            "native_profile_master": native_media,
            native_master_key: native_media,
            "views": {
                name: {
                    **probe.as_dict(),
                    "operation": "ffmpeg_v360_linear_bilinear_then_lossless_libx264rgb",
                    "additional_lossy_encoding": False,
                    "openpi_pil_resize_pixel_exact": False,
                }
                for name, probe in view_probes.items()
            },
            "projection": projection_manifest,
        },
        "imu": {
            "status": "unavailable_by_source_contract",
            "sample_count": 0,
            "gyro_available": False,
            "reason": "USB webcam mode exposes camera-native MJPEG pixels but no INSV telemetry",
        },
        "source_quality": role_quality,
        "latency_semantics": {
            "v4l2_soe_to_record_measured": True,
            "optical_exposure_to_v4l2_soe": "unknown",
            "sensor_and_in_camera_profile_latency": "unknown",
            "preview_participates_in_recording": False,
        },
        "processing_defaults": {
            "source_fps": FPS,
            "down_sample_steps": 1,
            "output_fps": FPS,
            "synthetic_60fps_duplication_allowed": False,
        },
        "tracker_clock": dict(tracker_clock),
        "clock_admission": {
            "raw_mapping_error": dict(raw_mapping_quality),
            "camera_htc_overlap": dict(camera_htc_overlap),
            "timestamp_log_session": dict(timestamp_session),
        },
        "preserve_methods": preserve_methods,
        "limitations": limitations,
    }
    atomic_write_json(output / "manifest.json", manifest)
    return {
        "arm": arm,
        "role": role_source.role,
        "raw_extract_dir": str(final_raw_dir),
        "raw_manifest": str(final_raw_dir / "manifest.json"),
        "calibrated_frame_timestamps": str(final_timeline),
        "frame_count": len(role_source.records),
        "fps": FPS,
        "source_profile": profile,
        "projection": manifest["media"]["projection"],
        "tracker": str(tracker),
        "tracker_clock": dict(tracker_clock),
        "raw_mapping_quality": dict(raw_mapping_quality),
        "camera_htc_overlap": dict(camera_htc_overlap),
    }


def build_sync_report(
    *,
    source: UvcCaptureSource,
    role_results: Mapping[str, Mapping[str, Any]],
    role_sources: Mapping[str, UvcRoleSource],
    timestamp_log: Path,
    timestamp_session: Mapping[str, Any],
    calibration_id: str,
    final_report: Path,
) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    input_arms: dict[str, Any] = {}
    for arm, result in role_results.items():
        role_source = role_sources[arm]
        timeline = Path(str(result["calibrated_frame_timestamps"]))
        tracker = Path(str(result["tracker"]))
        timeline_identity = file_identity(timeline)
        tracker_identity = file_identity(tracker)
        packet_identity = file_identity(
            role_source.video_path, known_sha256=role_source.video_sha256
        )
        index_identity = file_identity(
            role_source.frames_path, known_sha256=role_source.frames_sha256
        )
        worker_identity = file_identity(
            role_source.manifest_path,
            known_sha256=role_source.worker_manifest_sha256,
        )
        arm_quality = source.normalized_manifest["roles_detail"][
            role_source.role
        ]["quality"]
        arms[arm] = {
            "status": "accepted",
            "calibration_id": calibration_id,
            "source_family": "usb_uvc",
            "camera_role": role_source.role,
            "method": "direct_shared_host_monotonic_raw_no_gyro",
            "frame_timeline": {
                "path": str(timeline),
                "file_identity": timeline_identity,
                "frame_count": len(role_source.records),
                "clock_domain": "host_clock_monotonic_raw",
            },
            "htc": {
                "path": str(tracker),
                "file_identity": tracker_identity,
                **result["tracker_clock"],
            },
            "uvc_source": {
                "source_profile": native_source_profile(source),
                "packet_stream": packet_identity,
                "packet_index": index_identity,
                "worker_manifest": worker_identity,
            },
            "quality": {
                "calibration_accepted": True,
                "sequence_gaps": 0,
                "effective_fps": arm_quality["effective_fps_from_soe"],
                "maximum_monotonic_to_raw_error_bound_ns": result[
                    "raw_mapping_quality"
                ]["maximum_observed_error_bound_ns"],
                "maximum_admitted_monotonic_to_raw_error_bound_ns": result[
                    "raw_mapping_quality"
                ]["maximum_admitted_error_bound_ns"],
                "camera_htc_overlap": result["camera_htc_overlap"],
                "sensor_to_driver_latency": "unknown",
            },
        }
        input_arms[arm] = {
            "htc": tracker_identity,
            "uvc_packet_stream": packet_identity,
            "uvc_packet_index": index_identity,
            "uvc_worker_manifest": worker_identity,
        }
    return {
        "schema": SYNC_SCHEMA,
        "status": "accepted",
        "dry_run": False,
        "calibration_id": calibration_id,
        "session_dir": str(source.session_dir),
        "report_path": str(final_report),
        "source_family": "usb_uvc",
        "source_profile": native_source_profile(source),
        "clock_contract": {
            "domain": "host_clock_monotonic_raw",
            "alignment_policy": "direct_single_domain_only_fail_closed",
            "camera_timestamp": "V4L2 SOE mapped per DQBUF to CLOCK_MONOTONIC_RAW",
            "htc_timestamp": "hid_read_host_monotonic_raw_ns",
            "cross_correlation": "not_needed_and_not_available_without_gyro",
            "sensor_to_driver_latency": "unknown",
            "timestamp_log_session": dict(timestamp_session),
        },
        "input_files": {
            "timestamp_log": file_identity(timestamp_log),
            "arms": input_arms,
        },
        "arms": arms,
        "cross_role_pairing_quality": source.normalized_manifest[
            "cross_role_pairing_quality"
        ],
        "quality_interpretation": {
            "camera_host_clock_alignment": "accepted",
            "optical_latency_calibrated": False,
            "hardware_camera_sync": False,
            "nearest_pair_skew_is_diagnostic_not_hardware_sync": True,
        },
        "created_unix_time_ns": time.time_ns(),
    }


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UvcStage01Error(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UvcStage01Error(f"{label} is not a JSON object: {path}")
    return value


def recognized_incomplete_target(path: Path) -> bool:
    if path.name == "01_raw_extract" and path.parent.name in {"right", "left"}:
        manifest = path / "manifest.json"
        if not manifest.is_file():
            return False
        payload = read_json_object(manifest, "stage-01 raw manifest")
        return (
            payload.get("source_stage_schema") == UVC_STAGE_SCHEMA
            and payload.get("source_family") == "usb_uvc"
        )
    if path.name == "sync_calibration":
        report = path / "calibration.json"
        return report.is_file() and read_json_object(
            report, "UVC clock report"
        ).get("schema") == SYNC_SCHEMA
    if path.name == "01_raw_extract_biarm":
        manifest = path / "manifest.json"
        if not manifest.is_file():
            return False
        payload = read_json_object(manifest, "bimanual UVC raw manifest")
        return (
            payload.get("source_stage_schema") == UVC_STAGE_SCHEMA
            and payload.get("source_family") == "usb_uvc"
        )
    return False


def quarantine_incomplete_outputs(tmp_root: Path, occupied: Sequence[Path]) -> Path:
    unknown = [path for path in occupied if not recognized_incomplete_target(path)]
    if unknown:
        raise FileExistsError(
            "refusing to move an existing non-UVC or unidentifiable stage output: "
            + ", ".join(map(str, unknown))
        )
    quarantine = (
        tmp_root
        / "uvc_stage01_incomplete"
        / f"{time.time_ns()}-{os.getpid()}"
    )
    for source in occupied:
        relative = source.relative_to(tmp_root)
        destination = quarantine / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)
    print(
        "[uvc-stage01:recover] preserved incomplete prior publication at "
        f"{quarantine}",
        file=sys.stderr,
        flush=True,
    )
    return quarantine


def validate_recorded_identity(
    expected_identity: object,
    label: str,
    *,
    verify_sha256: bool,
) -> None:
    if not isinstance(expected_identity, dict):
        raise UvcStage01Error(f"UVC completion marker lacks {label} identity")
    recorded_path = expected_identity.get("path")
    if not isinstance(recorded_path, str) or not recorded_path:
        raise UvcStage01Error(f"UVC completion marker has invalid {label} path")
    expected_sha = expected_identity.get("sha256")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise UvcStage01Error(f"UVC completion marker has invalid {label} SHA-256")
    actual_path = require_regular_nonsymlink(Path(recorded_path), label)
    metadata = actual_path.stat()
    if (
        str(actual_path) != recorded_path
        or int(metadata.st_size) != expected_identity.get("size_bytes")
        or int(metadata.st_mtime_ns) != expected_identity.get("mtime_ns")
    ):
        raise UvcStage01Error(
            f"completed {label} changed after publication: {recorded_path}"
        )
    if verify_sha256 and sha256_file(actual_path) != expected_sha:
        raise UvcStage01Error(
            f"completed {label} content hash changed after publication: {recorded_path}"
        )


def collect_critical_artifact_identities(
    *, tmp_root: Path, arms: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Hash the timelines and every preserved native/timing artifact."""

    result: dict[str, dict[str, Any]] = {}
    for arm in arms:
        raw_dir = tmp_root / arm / "01_raw_extract"
        raw_manifest = read_json_object(raw_dir / "manifest.json", f"{arm} raw manifest")
        copied_sources = raw_manifest.get("copied_sources")
        uvc_sources = (
            copied_sources.get("uvc") if isinstance(copied_sources, dict) else None
        )
        if not isinstance(uvc_sources, dict):
            raise UvcStage01Error(f"{arm} raw manifest lacks preserved UVC sources")
        arm_identities = {
            f"preserved_{name}": file_identity(Path(str(path)))
            for name, path in uvc_sources.items()
        }
        arm_identities["calibrated_timeline"] = file_identity(
            tmp_root
            / "sync_calibration"
            / arm
            / "calibrated_frame_timestamps.csv"
        )
        arm_identities["raw_extract_timeline"] = file_identity(
            raw_dir / "timestamps" / "frame_timestamps.csv"
        )
        result[arm] = arm_identities
    return result


def load_reusable_completion(
    marker_path: Path,
    *,
    calibration_id: str,
    expected_targets: Sequence[Path],
    current_input_identities: Mapping[str, Any],
    arms: Sequence[str],
) -> dict[str, Any] | None:
    if not os.path.lexists(marker_path):
        return None
    marker_path = require_regular_nonsymlink(
        marker_path, "UVC stage-01 completion marker"
    )
    payload = read_json_object(marker_path, "UVC stage-01 completion marker")
    if payload.get("schema") != UVC_COMPLETE_SCHEMA or payload.get("complete") is not True:
        raise UvcStage01Error(f"invalid UVC stage-01 completion marker: {marker_path}")
    if payload.get("calibration_id") != calibration_id:
        raise UvcStage01Error(
            "completed UVC stage-01 output belongs to different source/settings; "
            "select another TMP_ROOT instead of overwriting it"
        )
    if payload.get("input_identities") != current_input_identities:
        raise UvcStage01Error(
            "completed UVC stage-01 inputs changed after publication; "
            "the previous complete output cannot be reused"
        )
    missing = [path for path in expected_targets if not path.is_dir()]
    if missing:
        raise UvcStage01Error(
            f"UVC completion marker exists but an output directory is missing: {missing[0]}"
        )
    manifest_path = Path(str(payload.get("biarm_manifest") or ""))
    if not manifest_path.is_file():
        raise UvcStage01Error(
            f"UVC completion marker points at a missing bimanual manifest: {manifest_path}"
        )
    strict_identities: list[tuple[str, object]] = [
        ("bimanual manifest", payload.get("biarm_manifest_identity")),
        ("clock report", payload.get("clock_report_identity")),
        ("normalized UVC source", payload.get("normalized_source_identity")),
    ]
    raw_identities = payload.get("raw_manifest_identities")
    video_identities = payload.get("video_identities")
    critical_identities = payload.get("critical_artifact_identities")
    expected_arms = set(arms)
    if (
        not isinstance(raw_identities, dict)
        or not isinstance(video_identities, dict)
        or not isinstance(critical_identities, dict)
        or set(raw_identities) != expected_arms
        or set(video_identities) != expected_arms
        or set(critical_identities) != expected_arms
    ):
        raise UvcStage01Error("UVC completion marker lacks output identity tables")
    strict_identities.extend(
        (f"{arm} raw manifest", identity)
        for arm, identity in raw_identities.items()
    )
    for arm, artifacts in critical_identities.items():
        if not isinstance(artifacts, dict) or not artifacts:
            raise UvcStage01Error(
                f"UVC completion marker has invalid {arm} critical artifact identities"
            )
        strict_identities.extend(
            (f"{arm} critical artifact {name}", identity)
            for name, identity in artifacts.items()
        )
    fast_identities: list[tuple[str, object]] = []
    for arm, videos in video_identities.items():
        if not isinstance(videos, dict):
            raise UvcStage01Error(
                f"UVC completion marker has invalid {arm} video identity table"
            )
        fast_identities.extend(
            (f"{arm} video {name}", identity)
            for name, identity in videos.items()
        )
    for label, expected_identity in strict_identities:
        validate_recorded_identity(
            expected_identity, label, verify_sha256=True
        )
    # Derived videos are large and reproducible.  Their content SHA is
    # committed at publication while routine resume uses path/size/mtime.
    # Native packets and all timing artifacts above are always rehashed.
    for label, expected_identity in fast_identities:
        validate_recorded_identity(
            expected_identity, label, verify_sha256=False
        )
    print(f"[uvc-stage01:skip] current complete output: {marker_path}", flush=True)
    return read_json_object(manifest_path, "completed bimanual UVC manifest")


def write_completion_marker(
    marker_path: Path,
    *,
    source: UvcCaptureSource,
    calibration_id: str,
    tmp_root: Path,
    arms: Sequence[str],
    input_identities: Mapping[str, Any],
) -> None:
    biarm_manifest = tmp_root / "01_raw_extract_biarm" / "manifest.json"
    report = tmp_root / "sync_calibration" / "calibration.json"
    normalized_source = (
        tmp_root / "01_raw_extract_biarm" / "uvc_source_manifest.json"
    )
    raw_manifests = {
        arm: file_identity(tmp_root / arm / "01_raw_extract" / "manifest.json")
        for arm in arms
    }
    video_identities: dict[str, dict[str, Any]] = {}
    for arm in arms:
        raw_manifest_path = tmp_root / arm / "01_raw_extract" / "manifest.json"
        raw_manifest = read_json_object(raw_manifest_path, f"{arm} raw manifest")
        videos = raw_manifest.get("videos")
        if not isinstance(videos, dict):
            raise UvcStage01Error(f"{arm} raw manifest lacks videos")
        # wrist_model aliases front_fisheye_224; hash each distinct path once.
        identities_by_path: dict[Path, dict[str, Any]] = {}
        named: dict[str, Any] = {}
        for name, raw_path in videos.items():
            video_path = Path(str(raw_path)).expanduser().resolve()
            identity = identities_by_path.get(video_path)
            if identity is None:
                identity = file_identity(video_path)
                identities_by_path[video_path] = identity
            named[name] = identity
        video_identities[arm] = named
    atomic_write_json(
        marker_path,
        {
            "schema": UVC_COMPLETE_SCHEMA,
            "status": "complete",
            "complete": True,
            "source_family": "usb_uvc",
            "source_profile": native_source_profile(source),
            "session_dir": str(source.session_dir),
            "calibration_id": calibration_id,
            "arms": list(arms),
            "input_identities": input_identities,
            "biarm_manifest": str(biarm_manifest),
            "biarm_manifest_identity": file_identity(biarm_manifest),
            "clock_report_identity": file_identity(report),
            "normalized_source_identity": file_identity(normalized_source),
            "raw_manifest_identities": raw_manifests,
            "critical_artifact_identities": collect_critical_artifact_identities(
                tmp_root=tmp_root, arms=arms
            ),
            "video_identities": video_identities,
            "completed_unix_time_ns": time.time_ns(),
        },
    )


def publish_stage(stage_root: Path, tmp_root: Path, arms: Sequence[str]) -> None:
    for arm in arms:
        destination_parent = tmp_root / arm
        destination_parent.mkdir(parents=True, exist_ok=True)
        os.rename(stage_root / arm / "01_raw_extract", destination_parent / "01_raw_extract")
    os.rename(stage_root / "sync_calibration", tmp_root / "sync_calibration")
    os.rename(stage_root / "01_raw_extract_biarm", tmp_root / "01_raw_extract_biarm")


def extract(args: argparse.Namespace) -> dict[str, Any]:
    session_dir = args.session_dir.expanduser().resolve(strict=True)
    tmp_root = (
        args.tmp_root.expanduser().resolve()
        if args.tmp_root is not None
        else session_dir / "tmp"
    )
    source = validate_uvc_capture(session_dir)
    source_profile = native_source_profile(source)
    legacy_panorama_v1 = bool(source_profile["legacy_panorama_v1"])
    if not legacy_panorama_v1:
        ensure_no_projection_overrides(args)
    for role_source in source.roles.values():
        require_raw_camera_clock(role_source)
    maximum_raw_clock_error_ns = int(
        getattr(args, "max_raw_clock_error_ns", DEFAULT_MAX_RAW_CLOCK_ERROR_NS)
    )
    if maximum_raw_clock_error_ns < 0:
        raise UvcStage01Error("--max-raw-clock-error-ns must be non-negative")
    timestamp_log = discover_timestamp_log(session_dir)
    capture_contract = json.loads(
        (session_dir / "uvc_capture_manifest.json").read_text(encoding="utf-8")
    )
    arms = tuple(ROLE_TO_ARM[role] for role in source.roles)
    if not arms:
        raise UvcStage01Error("completed UVC capture declares no supported roles")
    trackers = {arm: discover_tracker(session_dir, arm) for arm in arms}
    tracker_clocks = {
        arm: validate_tracker_raw_clock(trackers[arm], arm) for arm in arms
    }
    role_sources = {
        ROLE_TO_ARM[role]: role_source for role, role_source in source.roles.items()
    }
    raw_mapping_quality = {
        arm: validate_raw_mapping_error(role_sources[arm], maximum_raw_clock_error_ns)
        for arm in arms
    }
    camera_htc_overlap = {
        arm: validate_camera_htc_overlap(
            role_sources[arm], tracker_clocks[arm]
        )
        for arm in arms
    }
    timestamp_session = validate_timestamp_log_session(
        timestamp_log,
        session_dir=session_dir,
        role_sources=role_sources,
    )
    input_identities = build_input_identities(
        source=source,
        timestamp_log=timestamp_log,
        trackers=trackers,
    )
    settings: dict[str, Any] = {
        "fps": FPS,
        "source_profile": source_profile,
        "native_profile_geometry": [
            int(source_profile["width"]),
            int(source_profile["height"]),
        ],
        "host_reprojection_performed": legacy_panorama_v1,
        "clock_admission": {
            "maximum_raw_clock_error_ns": maximum_raw_clock_error_ns,
            "minimum_camera_htc_overlap_fraction": (
                MIN_CAMERA_HTC_OVERLAP_FRACTION
            ),
            "raw_mapping_quality_by_arm": raw_mapping_quality,
            "camera_htc_overlap_by_arm": camera_htc_overlap,
            "timestamp_log_session": timestamp_session,
        },
        "input_identities": input_identities,
    }
    if legacy_panorama_v1:
        settings |= {
            "front_fisheye": [FRONT_WIDTH, FRONT_HEIGHT],
            "model_views": [MODEL_WIDTH, MODEL_HEIGHT],
            "projection_by_arm": {
                arm: role_projection(capture_contract, arm, args) for arm in arms
            },
        }
    calibration_id = calibration_id_for(source, settings)

    final_targets = [
        *(tmp_root / arm / "01_raw_extract" for arm in arms),
        tmp_root / "sync_calibration",
        tmp_root / "01_raw_extract_biarm",
    ]
    completion_marker = tmp_root / "uvc_stage01_complete.json"
    reusable = load_reusable_completion(
        completion_marker,
        calibration_id=calibration_id,
        expected_targets=final_targets,
        current_input_identities=input_identities,
        arms=arms,
    )
    if reusable is not None:
        return reusable
    occupied = [path for path in final_targets if path.exists() or path.is_symlink()]
    if occupied:
        quarantine_incomplete_outputs(tmp_root, occupied)
    tmp_root.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".uvc-stage01-", dir=tmp_root))
    try:
        role_results: dict[str, dict[str, Any]] = {}
        for role, role_source in source.roles.items():
            arm = ROLE_TO_ARM[role]
            final_raw_dir = tmp_root / arm / "01_raw_extract"
            final_timeline = (
                tmp_root
                / "sync_calibration"
                / arm
                / "calibrated_frame_timestamps.csv"
            )
            result = build_role_stage(
                source=source,
                role_source=role_source,
                arm=arm,
                stage_raw_dir=stage_root / arm / "01_raw_extract",
                final_raw_dir=final_raw_dir,
                stage_timeline=(
                    stage_root
                    / "sync_calibration"
                    / arm
                    / "calibrated_frame_timestamps.csv"
                ),
                final_timeline=final_timeline,
                timestamp_log=timestamp_log,
                tracker=trackers[arm],
                tracker_clock=tracker_clocks[arm],
                raw_mapping_quality=raw_mapping_quality[arm],
                camera_htc_overlap=camera_htc_overlap[arm],
                timestamp_session=timestamp_session,
                calibration_id=calibration_id,
                capture_contract=capture_contract,
                args=args,
            )
            role_results[arm] = result

        final_sync_report = tmp_root / "sync_calibration" / "calibration.json"
        # Build identities through their final paths.  The staged timelines are
        # byte-identical, so temporarily publish only the path substitution for
        # identity creation, then write the report while everything remains in
        # the isolated stage tree.
        for arm, result in role_results.items():
            stage_timeline = (
                stage_root
                / "sync_calibration"
                / arm
                / "calibrated_frame_timestamps.csv"
            )
            result["calibrated_frame_timestamps"] = str(stage_timeline)
        sync_report = build_sync_report(
            source=source,
            role_results=role_results,
            role_sources=role_sources,
            timestamp_log=timestamp_log,
            timestamp_session=timestamp_session,
            calibration_id=calibration_id,
            final_report=final_sync_report,
        )
        # Replace only staged timeline identity paths with their final paths;
        # rename preserves size, mtime and content hash.
        for arm in arms:
            final_timeline = (
                tmp_root
                / "sync_calibration"
                / arm
                / "calibrated_frame_timestamps.csv"
            )
            sync_report["arms"][arm]["frame_timeline"]["path"] = str(final_timeline)
            sync_report["arms"][arm]["frame_timeline"]["file_identity"]["path"] = str(
                final_timeline
            )
            role_results[arm]["calibrated_frame_timestamps"] = str(final_timeline)
        atomic_write_json(
            stage_root / "sync_calibration" / "calibration.json", sync_report
        )

        biarm_manifest = {
            "schema": BIARM_SCHEMA,
            "source_stage_schema": UVC_STAGE_SCHEMA,
            "source_family": "usb_uvc",
            "capture_transport": "usb_uvc",
            "source_profile": source_profile,
            "source_profile_dispatch": (
                "legacy_panorama_views"
                if legacy_panorama_v1
                else f"native_{source_profile['view_mode']}_passthrough"
            ),
            "session": session_dir.name,
            "input_dir": str(session_dir),
            "tmp_root": str(tmp_root),
            "image_pipeline": (
                "uvc_equirectangular_views_v1_legacy_compatibility"
                if legacy_panorama_v1
                else "uvc_camera_native_profile_v1_no_reprojection"
            ),
            "clock_alignment": {
                "schema": SYNC_SCHEMA,
                "status": "accepted",
                "report": str(final_sync_report),
                "calibration_id": calibration_id,
            },
            "roles": list(source.roles),
            "arms": role_results,
            "normalized_source_schema": NORMALIZED_SCHEMA,
            "normalized_source_manifest": str(
                tmp_root
                / "01_raw_extract_biarm"
                / "uvc_source_manifest.json"
            ),
            "cross_role_pairing_quality": source.normalized_manifest[
                "cross_role_pairing_quality"
            ],
            "processing_defaults": {
                "source_fps": FPS,
                "down_sample_steps": 1,
                "output_fps": FPS,
                "synthetic_60fps_duplication_allowed": False,
            },
            "downstream_status": {
                "stage_01": "complete",
                "legacy_video_readers": (
                    "artifacts_available"
                    if legacy_panorama_v1
                    else "not_available_native_profile_not_reprojected"
                ),
                "insv_direct_image_path": "unsupported_by_source_contract",
                "existing_insv_gripper_calibration": "must_not_be_reused",
                "full_lerobot_release": "not_certified_by_this_stage",
            },
        }
        atomic_write_json(
            stage_root
            / "01_raw_extract_biarm"
            / "uvc_source_manifest.json",
            source.normalized_manifest,
        )
        atomic_write_json(
            stage_root / "01_raw_extract_biarm" / "manifest.json",
            biarm_manifest,
        )
        current_source = validate_uvc_capture(session_dir)
        current_input_identities = build_input_identities(
            source=current_source,
            timestamp_log=timestamp_log,
            trackers=trackers,
        )
        if current_input_identities != input_identities:
            raise UvcStage01Error(
                "UVC/HTC/timestamp inputs changed during stage-01 extraction; "
                "discarding the staged result"
            )
        publish_stage(stage_root, tmp_root, arms)
        write_completion_marker(
            completion_marker,
            source=source,
            calibration_id=calibration_id,
            tmp_root=tmp_root,
            arms=arms,
            input_identities=input_identities,
        )
        return biarm_manifest
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--tmp-root", type=Path)
    parser.add_argument("--ffmpeg-bin", default="/usr/bin/ffmpeg")
    parser.add_argument("--ffprobe-bin", default="/usr/bin/ffprobe")
    parser.add_argument("--ffmpeg-threads", type=int, default=2)
    parser.add_argument(
        "--max-raw-clock-error-ns",
        type=int,
        default=DEFAULT_MAX_RAW_CLOCK_ERROR_NS,
        help=(
            "Maximum admitted per-frame CLOCK_MONOTONIC to CLOCK_MONOTONIC_RAW "
            "mapping error bound (default: 1000000ns)."
        ),
    )
    for arm in ("right", "left"):
        parser.add_argument(f"--{arm}-yaw-deg", type=float)
        parser.add_argument(f"--{arm}-pitch-deg", type=float)
        parser.add_argument(f"--{arm}-roll-deg", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.ffmpeg_threads < 1:
        raise ValueError("--ffmpeg-threads must be positive")
    result = extract(args)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[uvc-stage01:error] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
