#!/usr/bin/env python3
"""Calibrate Insta360 video time to HTC host time without visual SLAM.

The calibration signal is the rotation-speed norm measured by the X5 IMU and
the matching HTC tracker.  Norms are invariant to the unknown rigid rotation
between the camera and tracker, so no camera/HTC extrinsic is required.

This tool is intentionally non-destructive.  It writes a parallel calibrated
timeline and a keep/drop recommendation for chapter overlaps.  It never
rewrites extracted videos, their existing frame_timestamps.csv, or raw input.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d
from scipy.signal import correlate


SCHEMA = "instaumi-no-slam-clock-calibration-v2-monotonic-raw"
CLOCK_DOMAIN = "host_clock_monotonic_raw"
HTC_RAW_TIME_COLUMN = "hid_read_host_monotonic_raw_ns"
CLOCK_TIMING_ARTIFACT_SCHEMA = "instaumi_x5_clock_timing_chapter_v1"
CLOCK_TIMING_MANIFEST_SCHEMA = "instaumi_x5_clock_timing_manifest_v1"
NATIVE_TIMESTAMP_SCALE_SEC = 1e-6  # X5 first_frame_timestamp is in microseconds.


@dataclass
class Segment:
    arm: str
    path: Path
    source_segment: int
    first_frame_timestamp_raw: int
    native_start_sec: float
    frame_count: int
    fps: float
    duration_sec: float
    pts_start_sec: float
    time_base: str
    avg_frame_rate: str
    r_frame_rate: str
    rolling_shutter_ms: float | None
    camera_serial: str | None
    firmware: str | None
    imu_time_sec: np.ndarray = field(repr=False)
    gyro_norm_rad_s: np.ndarray = field(repr=False)
    gyro_input_unit: str
    timing_artifact: dict[str, Any] = field(default_factory=dict)
    frame_pts_sec: np.ndarray | None = field(default=None, repr=False)
    pts_diagnostics: dict[str, Any] = field(default_factory=dict)
    concat_start_frame: int = 0
    concat_start_sec: float = 0.0
    concat_discontinuity_sec: float = 0.0
    overlap_with_previous_last_frame_sec: float = 0.0

    @property
    def native_end_frame_sec(self) -> float:
        if self.frame_count <= 0:
            return self.native_start_sec
        pts = self.source_frame_pts()
        return self.native_start_sec + float(pts[-1] - pts[0])

    def source_frame_pts(self) -> np.ndarray:
        if self.frame_pts_sec is not None:
            return self.frame_pts_sec
        return self.pts_start_sec + np.arange(self.frame_count, dtype=np.float64) / self.fps


@dataclass
class WindowEstimate:
    arm: str
    source_segment: int
    start_camera_sec: float
    end_camera_sec: float
    center_camera_sec: float
    offset_sec: float
    correlation: float
    peak_margin: float
    peak_width_ms: float
    motion_std_rad_s: float
    accepted: bool
    reject_reason: str | None


@dataclass
class SegmentModel:
    source_segment: int
    intercept_sec: float
    slope_sec_per_sec: float
    confidence: float
    accepted_windows: int
    median_correlation: float
    residual_median_ms: float
    residual_p95_ms: float
    fallback: str | None = None

    def offset(self, camera_time_sec: np.ndarray | float) -> np.ndarray | float:
        return self.intercept_sec + self.slope_sec_per_sec * camera_time_sec


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def file_identity(path: Path, sample_size: int = 256 * 1024) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
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


def nested_metadata(value: Any) -> dict[str, Any] | None:
    """Find the telemetry-parser Metadata mapping without assuming group name."""

    if isinstance(value, dict):
        metadata = value.get("Metadata")
        if isinstance(metadata, dict):
            return metadata
        for child in value.values():
            found = nested_metadata(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_metadata(child)
            if found is not None:
                return found
    return None


def parse_fraction(value: str) -> float:
    if not value or value == "0/0":
        raise ValueError(f"invalid frame rate: {value!r}")
    return float(Fraction(value))


def ffprobe_stream(
    path: Path, ffprobe_bin: str, pts_cache_dir: Path | None = None
) -> dict[str, Any]:
    source_identity = file_identity(path)
    cache_path: Path | None = None
    if pts_cache_dir is not None:
        cache_key = hashlib.sha256(
            json.dumps(
                {"schema": "instaumi-pts-cache-v2", **source_identity},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cache_path = pts_cache_dir / f"{cache_key}.npz"
        if cache_path.is_file():
            try:
                with np.load(cache_path, allow_pickle=False) as cached:
                    metadata = json.loads(str(cached["metadata_json"].item()))
                    frame_pts = np.asarray(cached["frame_pts_sec"], dtype=np.float64)
                if metadata.pop("source_identity", None) == source_identity:
                    metadata["frame_pts_sec"] = frame_pts
                    metadata["pts_diagnostics"]["cache_hit"] = True
                    return metadata
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                cache_path.unlink(missing_ok=True)
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate,time_base,start_time,duration,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise RuntimeError(f"no video stream found: {path}")
    stream = streams[0]
    fps_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    fps = parse_fraction(str(fps_text))
    frame_count_text = stream.get("nb_frames")
    if frame_count_text in (None, "", "N/A"):
        raise RuntimeError(
            f"ffprobe did not report nb_frames for {path}; exact concat-frame mapping is required"
        )
    packet_command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "json",
        str(path),
    ]
    packet_result = subprocess.run(packet_command, check=True, capture_output=True, text=True)
    packet_items = json.loads(packet_result.stdout).get("packets") or []
    packet_pts = np.asarray(
        [finite_float(item.get("pts_time"), float("nan")) for item in packet_items],
        dtype=np.float64,
    )
    declared_count = int(frame_count_text)
    if packet_pts.size != declared_count:
        raise RuntimeError(
            f"ffprobe video-packet PTS count mismatch for {path}: "
            f"timestamps={packet_pts.size}, nb_frames={declared_count}"
        )
    if not np.all(np.isfinite(packet_pts)):
        raise RuntimeError(f"ffprobe returned non-finite video-packet PTS: {path}")
    packet_order_nonmonotonic = int(np.count_nonzero(np.diff(packet_pts) <= 0))
    # Packet order may be decode order when B-frames are present. Stable sort
    # by PTS recovers the presentation order used by decoded video frames.
    frame_pts = np.sort(packet_pts, kind="stable")
    deltas = np.diff(frame_pts)
    if deltas.size and np.any(deltas <= 0):
        raise RuntimeError(f"video frame PTS is not strictly increasing: {path}")
    nominal_step = 1.0 / fps
    pts_diagnostics = {
        "method": "ffprobe_video_packet_pts_time_sorted_to_presentation_order",
        "timestamp_count": int(frame_pts.size),
        "packet_order_nonmonotonic_steps": packet_order_nonmonotonic,
        "strictly_increasing": True,
        "nominal_step_ms": nominal_step * 1000.0,
        "median_step_ms": float(np.median(deltas) * 1000.0) if deltas.size else None,
        "p95_step_ms": float(np.percentile(deltas, 95) * 1000.0) if deltas.size else None,
        "max_step_ms": float(np.max(deltas) * 1000.0) if deltas.size else None,
        "gap_over_1_5_frames": int(np.count_nonzero(deltas > nominal_step * 1.5)),
        "short_step_under_0_5_frames": int(np.count_nonzero(deltas < nominal_step * 0.5)),
    }
    stream_result = {
        "fps": fps,
        "frame_count": declared_count,
        "duration_sec": float(stream.get("duration") or declared_count / fps),
        "pts_start_sec": float(stream.get("start_time") or 0.0),
        "frame_pts_sec": frame_pts,
        "pts_diagnostics": pts_diagnostics,
        "time_base": str(stream.get("time_base") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
    }
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {key: value for key, value in stream_result.items() if key != "frame_pts_sec"}
        metadata["source_identity"] = source_identity
        metadata["pts_diagnostics"]["cache_hit"] = False
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                frame_pts_sec=frame_pts,
                metadata_json=np.asarray(json.dumps(metadata, allow_nan=False)),
            )
        temporary.replace(cache_path)
    return stream_result


def detect_gyro_scale(raw_norm: np.ndarray) -> tuple[str, float]:
    finite = raw_norm[np.isfinite(raw_norm)]
    if not finite.size:
        raise RuntimeError("camera IMU contains no finite gyro samples")
    p95 = float(np.percentile(finite, 95))
    return ("deg/s", math.pi / 180.0) if p95 > 20.0 else ("rad/s", 1.0)


def load_segment(
    arm: str,
    path: Path,
    source_segment: int,
    first_raw_reference: int | None,
    ffprobe_bin: str,
    pts_cache_dir: Path | None = None,
) -> tuple[Segment, int]:
    try:
        from telemetry_parser import Parser
    except ImportError as exc:
        raise RuntimeError(
            "telemetry_parser is required; run with the instaumi conda Python"
        ) from exc

    parser = Parser(str(path))
    telemetry = parser.telemetry()
    metadata = nested_metadata(telemetry)
    if metadata is None:
        raise RuntimeError(f"telemetry_parser did not expose Metadata: {path}")
    raw_value = metadata.get("first_frame_timestamp")
    if raw_value is None:
        raise RuntimeError(f"missing X5 native first_frame_timestamp: {path}")
    first_raw = int(raw_value)
    if first_raw_reference is None:
        first_raw_reference = first_raw
    native_start_sec = (first_raw - first_raw_reference) * NATIVE_TIMESTAMP_SCALE_SEC

    normalized = parser.normalized_imu()
    if not normalized:
        raise RuntimeError(f"no normalized IMU samples: {path}")
    imu_ms = np.fromiter(
        (float(sample["timestamp_ms"]) for sample in normalized),
        dtype=np.float64,
        count=len(normalized),
    )
    raw_norm = np.fromiter(
        (
            math.sqrt(sum(float(component) ** 2 for component in sample["gyro"]))
            for sample in normalized
        ),
        dtype=np.float64,
        count=len(normalized),
    )
    gyro_unit, gyro_scale = detect_gyro_scale(raw_norm)
    rolling_shutter_ms = finite_float(metadata.get("rolling_shutter_time"))
    camera_serial = metadata.get("serial_number")
    firmware = metadata.get("fw_version")
    del normalized, telemetry, parser, metadata
    video = ffprobe_stream(path, ffprobe_bin, pts_cache_dir)
    segment = Segment(
        arm=arm,
        path=path,
        source_segment=source_segment,
        first_frame_timestamp_raw=first_raw,
        native_start_sec=native_start_sec,
        frame_count=video["frame_count"],
        fps=video["fps"],
        duration_sec=video["duration_sec"],
        pts_start_sec=video["pts_start_sec"],
        time_base=video["time_base"],
        avg_frame_rate=video["avg_frame_rate"],
        r_frame_rate=video["r_frame_rate"],
        rolling_shutter_ms=rolling_shutter_ms,
        camera_serial=camera_serial,
        firmware=firmware,
        imu_time_sec=native_start_sec + imu_ms / 1000.0,
        gyro_norm_rad_s=raw_norm * gyro_scale,
        gyro_input_unit=gyro_unit,
        frame_pts_sec=video["frame_pts_sec"],
        pts_diagnostics=video["pts_diagnostics"],
    )
    return segment, first_raw_reference


def identity_matches_source(identity: Any, path: Path) -> bool:
    if not isinstance(identity, dict):
        return False
    current = file_identity(path)
    return (
        Path(str(identity.get("path", ""))).expanduser().resolve() == path.resolve()
        and int(identity.get("size_bytes", -1)) == current["size_bytes"]
        and int(identity.get("mtime_ns", -1)) == current["mtime_ns"]
        and str(identity.get("sample_sha256", "")) == current["sample_sha256"]
    )


def load_extracted_timing_artifacts(
    raw_manifest_path: Path,
    expected_sources: Sequence[Path],
) -> list[Path]:
    """Resolve and validate _01 timing artifacts without opening INSV media."""

    manifest_path = raw_manifest_path.expanduser().resolve()
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clock_timing = raw_manifest.get("clock_timing") or {}
    if clock_timing.get("schema") != CLOCK_TIMING_MANIFEST_SCHEMA:
        raise RuntimeError(
            f"_01 manifest has no current clock timing artifacts: {manifest_path}"
        )
    timing_manifest_path = Path(str(clock_timing.get("manifest_path", ""))).expanduser().resolve()
    try:
        timing_manifest_path.relative_to(manifest_path.parent)
    except ValueError as exc:
        raise RuntimeError(
            f"clock timing manifest escapes _01 output directory: {timing_manifest_path}"
        ) from exc
    timing_manifest = json.loads(timing_manifest_path.read_text(encoding="utf-8"))
    if timing_manifest.get("schema") != CLOCK_TIMING_MANIFEST_SCHEMA:
        raise RuntimeError(f"invalid clock timing manifest schema: {timing_manifest_path}")
    records = timing_manifest.get("artifacts") or []
    if len(records) != len(expected_sources):
        raise RuntimeError(
            "clock timing artifact/source chapter count mismatch: "
            f"artifacts={len(records)} sources={len(expected_sources)}"
        )
    artifact_paths: list[Path] = []
    for source_segment, (record, source) in enumerate(zip(records, expected_sources)):
        source = source.expanduser().resolve()
        if int(record.get("source_segment", -1)) != source_segment:
            raise RuntimeError(
                f"clock timing artifact order mismatch at chapter {source_segment}"
            )
        if not identity_matches_source(record.get("source"), source):
            raise RuntimeError(
                f"clock timing source provenance changed at chapter {source_segment}: {source}"
            )
        artifact_path = Path(str(record.get("path", ""))).expanduser().resolve()
        try:
            artifact_path.relative_to(manifest_path.parent)
        except ValueError as exc:
            raise RuntimeError(
                f"clock timing artifact escapes _01 output directory: {artifact_path}"
            ) from exc
        if not artifact_path.is_file():
            raise RuntimeError(f"missing clock timing artifact: {artifact_path}")
        artifact_identity = record.get("file_identity") or {}
        current_artifact_identity = file_identity(artifact_path)
        if any(
            artifact_identity.get(key) != current_artifact_identity.get(key)
            for key in ("size_bytes", "mtime_ns", "sample_sha256")
        ):
            raise RuntimeError(f"clock timing artifact provenance changed: {artifact_path}")
        artifact_paths.append(artifact_path)
    return artifact_paths


def load_segment_from_timing_artifact(
    arm: str,
    path: Path,
    source_segment: int,
    first_raw_reference: int | None,
    artifact_path: Path,
) -> tuple[Segment, int]:
    """Load a chapter from the exact _01 artifact, never from raw INSV."""

    try:
        with np.load(artifact_path, allow_pickle=False) as artifact:
            metadata = json.loads(str(artifact["metadata_json"].item()))
            frame_pts_sec = np.asarray(artifact["frame_pts_sec"], dtype=np.float64)
            local_imu_time_sec = np.asarray(artifact["imu_time_sec"], dtype=np.float64)
            gyro_norm_rad_s = np.asarray(artifact["gyro_norm_rad_s"], dtype=np.float64)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid _01 clock timing artifact: {artifact_path}: {exc}") from exc
    if metadata.get("schema") != CLOCK_TIMING_ARTIFACT_SCHEMA:
        raise RuntimeError(f"unsupported _01 clock timing artifact: {artifact_path}")
    if int(metadata.get("source_segment", -1)) != source_segment:
        raise RuntimeError(f"clock timing chapter index mismatch: {artifact_path}")
    if not identity_matches_source(metadata.get("source"), path):
        raise RuntimeError(f"clock timing INSV provenance changed: {path}")
    if (
        frame_pts_sec.ndim != 1
        or local_imu_time_sec.ndim != 1
        or gyro_norm_rad_s.ndim != 1
        or local_imu_time_sec.size != gyro_norm_rad_s.size
        or frame_pts_sec.size == 0
        or local_imu_time_sec.size == 0
        or not np.all(np.isfinite(frame_pts_sec))
        or not np.all(np.isfinite(local_imu_time_sec))
        or not np.all(np.isfinite(gyro_norm_rad_s))
        or np.any(np.diff(frame_pts_sec) <= 0)
        or np.any(np.diff(local_imu_time_sec) <= 0)
    ):
        raise RuntimeError(f"invalid timing arrays in {artifact_path}")
    video = metadata.get("video") or {}
    frame_count = int(video.get("frame_count", -1))
    if frame_count != frame_pts_sec.size:
        raise RuntimeError(
            f"clock timing frame count mismatch for {path}: "
            f"metadata={frame_count} pts={frame_pts_sec.size}"
        )
    first_raw = int(metadata["first_frame_timestamp_raw_us"])
    if first_raw_reference is None:
        first_raw_reference = first_raw
    native_start_sec = (first_raw - first_raw_reference) * NATIVE_TIMESTAMP_SCALE_SEC
    segment = Segment(
        arm=arm,
        path=path,
        source_segment=source_segment,
        first_frame_timestamp_raw=first_raw,
        native_start_sec=native_start_sec,
        frame_count=frame_count,
        fps=float(video["fps"]),
        duration_sec=float(video["duration_sec"]),
        pts_start_sec=float(video["pts_start_sec"]),
        time_base=str(video.get("time_base") or ""),
        avg_frame_rate=str(video.get("avg_frame_rate") or ""),
        r_frame_rate=str(video.get("r_frame_rate") or ""),
        rolling_shutter_ms=finite_float(metadata.get("rolling_shutter_ms")),
        camera_serial=metadata.get("camera_serial"),
        firmware=metadata.get("firmware"),
        imu_time_sec=native_start_sec + local_imu_time_sec,
        gyro_norm_rad_s=gyro_norm_rad_s,
        gyro_input_unit=str(metadata.get("gyro_input_unit") or "unknown"),
        timing_artifact={
            "mode": "stage_01_clock_timing_artifact",
            "path": str(artifact_path.resolve()),
            "file_identity": file_identity(artifact_path),
        },
        frame_pts_sec=frame_pts_sec,
        pts_diagnostics=dict(video.get("pts_diagnostics") or {}),
    )
    return segment, first_raw_reference


def discover_insv(session_dir: Path, arm: str) -> list[Path]:
    candidates: list[Path] = []
    for directory in (
        session_dir / "insta" / f"insta_{arm}",
        session_dir / f"insta_{arm}",
        session_dir / arm,
    ):
        if directory.is_dir():
            candidates.extend(directory.glob("*.insv"))
    return sorted({path.resolve() for path in candidates})


def finalize_segment_layout(segments: list[Segment]) -> None:
    segments.sort(key=lambda item: (item.first_frame_timestamp_raw, item.path.name))
    concat_frame = 0
    concat_time = 0.0
    previous_last_native: float | None = None
    for index, segment in enumerate(segments):
        segment.source_segment = index
        segment.concat_start_frame = concat_frame
        segment.concat_start_sec = concat_time
        segment.concat_discontinuity_sec = segment.native_start_sec - concat_time
        if previous_last_native is not None:
            segment.overlap_with_previous_last_frame_sec = max(
                0.0, previous_last_native - segment.native_start_sec
            )
        concat_frame += segment.frame_count
        concat_time += segment.frame_count / segment.fps
        previous_last_native = max(
            previous_last_native if previous_last_native is not None else -np.inf,
            segment.native_end_frame_sec,
        )


def load_htc(path: Path, arm: str | None = None) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    columns = list(pd.read_csv(path, nrows=0).columns)
    if HTC_RAW_TIME_COLUMN not in columns:
        raise RuntimeError(
            f"{path} has no {HTC_RAW_TIME_COLUMN}; strict calibration requires "
            f"{CLOCK_DOMAIN} and never falls back to realtime/Unix timestamps"
        )
    angular = ["angular_velocity_x", "angular_velocity_y", "angular_velocity_z"]
    missing = [name for name in angular if name not in columns]
    if missing:
        raise RuntimeError(f"{path} is missing HTC angular velocity columns: {missing}")
    audit_clock_columns = [
        name
        for name in ("hid_read_host_monotonic_ns", "hid_read_host_realtime_ns")
        if name in columns
    ]
    usecols = [HTC_RAW_TIME_COLUMN, *audit_clock_columns, *angular]
    usecols.extend(
        name for name in ("tracking_status", "role", "tracker_index") if name in columns
    )
    data = pd.read_csv(path, usecols=usecols)
    raw_row_count = len(data)
    omega = data[angular].apply(pd.to_numeric, errors="coerce").to_numpy(np.float64)
    norm = np.linalg.norm(omega, axis=1)
    eligible = np.isfinite(norm)

    role_policy = "single_stream_without_role_column"
    role_filtered_count = 0
    if "role" in data.columns:
        roles = data["role"].fillna("").astype(str).str.strip().str.lower()
        explicit = roles != ""
        if bool(explicit.any()):
            unique_roles = sorted(set(roles[explicit]))
            if arm is None:
                if len(unique_roles) != 1:
                    raise RuntimeError(
                        f"{path} contains multiple explicit tracker roles {unique_roles}; arm is required"
                    )
                role_match = roles == unique_roles[0]
                selected_role = unique_roles[0]
            else:
                accepted_roles = {
                    arm.lower(),
                    f"htc_{arm.lower()}",
                    f"tracker_{arm.lower()}",
                    f"{arm.lower()}_arm",
                    f"{arm.lower()}_elbow",
                    "tracker_1" if arm.lower() == "right" else "tracker_2",
                }
                role_match = roles.isin(accepted_roles)
                selected_role = f"htc_{arm.lower()}"
                if not bool(role_match.any()):
                    raise RuntimeError(
                        f"{path} has explicit roles {unique_roles}, but no rows for {selected_role}"
                    )
            role_filtered_count = int(np.count_nonzero(eligible & ~role_match.to_numpy()))
            eligible &= role_match.to_numpy()
            role_policy = f"explicit_role_equals_{selected_role}"
    if role_policy == "single_stream_without_role_column" and "tracker_index" in data.columns:
        tracker_indices = pd.to_numeric(data["tracker_index"], errors="coerce").to_numpy()
        unique_indices = np.unique(tracker_indices[eligible & np.isfinite(tracker_indices)])
        if unique_indices.size > 1:
            raise RuntimeError(
                f"{path} contains multiple tracker_index values without an explicit role; "
                "split the CSV or add a trustworthy role column"
            )

    tracking_status_filtered_count = 0
    if "tracking_status" in data.columns:
        tracking = pd.to_numeric(data["tracking_status"], errors="coerce").to_numpy(np.float64)
        full_pose = tracking == 2
        tracking_status_filtered_count = int(np.count_nonzero(eligible & ~full_pose))
        eligible &= full_pose
        tracking_status_policy = "full_pose_only_tracking_status_eq_2"
    else:
        tracking_status_policy = "unavailable_legacy_assumed_valid"

    eligible_count = int(np.count_nonzero(eligible))
    selected_values = pd.to_numeric(
        data[HTC_RAW_TIME_COLUMN], errors="coerce"
    ).to_numpy(np.float64)
    finite_count = int(np.count_nonzero(eligible & np.isfinite(selected_values)))
    coverage = finite_count / eligible_count if eligible_count else 0.0
    if finite_count < 100 or coverage < 0.95:
        raise RuntimeError(
            f"{path} {HTC_RAW_TIME_COLUMN} has insufficient eligible coverage: "
            f"finite={finite_count}, eligible={eligible_count}, coverage={coverage:.6f}"
        )
    time_column = HTC_RAW_TIME_COLUMN
    time_selection = [
        {
            "column": HTC_RAW_TIME_COLUMN,
            "finite_eligible_rows": finite_count,
            "eligible_coverage": coverage,
            "selected": True,
        }
    ]
    host_sec = selected_values / 1e9
    valid = eligible & np.isfinite(host_sec)
    monotonic_column = (
        "hid_read_host_monotonic_ns"
        if "hid_read_host_monotonic_ns" in data.columns
        else None
    )
    clock_pair_stats: dict[str, Any] = {
        "status": "unavailable",
        "monotonic_column": monotonic_column,
        "reference_column": HTC_RAW_TIME_COLUMN,
        "minimum_coverage": 0.95,
    }
    if monotonic_column is not None:
        monotonic_ns = pd.to_numeric(data[monotonic_column], errors="coerce").to_numpy(np.float64)
        pair_valid = valid & np.isfinite(monotonic_ns)
        coverage = float(np.count_nonzero(pair_valid) / max(1, np.count_nonzero(valid)))
        clock_pair_stats["coverage"] = coverage
        if coverage >= 0.95:
            raw_ns = selected_values[pair_valid]
            offsets_ms = (monotonic_ns[pair_valid] - raw_ns) / 1e6
            offset_steps_ms = np.diff(offsets_ms)
            max_step_ms = float(np.max(np.abs(offset_steps_ms))) if offset_steps_ms.size else 0.0
            clock_pair_stats.update(
                {
                    "status": "verified",
                    "offset_start_ms": float(offsets_ms[0]),
                    "offset_end_ms": float(offsets_ms[-1]),
                    "offset_change_ms": float(offsets_ms[-1] - offsets_ms[0]),
                    "max_abs_adjacent_offset_step_ms": max_step_ms,
                }
            )
    host_sec = host_sec[valid]
    norm = norm[valid]
    order = np.argsort(host_sec, kind="stable")
    host_sec = host_sec[order]
    norm = norm[order]
    unique = np.r_[True, np.diff(host_sec) > 0]
    duplicate_count = int(np.count_nonzero(~unique))
    host_sec = host_sec[unique]
    norm = norm[unique]
    if host_sec.size < 100:
        raise RuntimeError(f"too few valid HTC samples: {path}")
    gaps_ms = np.diff(host_sec) * 1000.0
    stats = {
        "path": str(path.resolve()),
        "file_identity": file_identity(path),
        "time_column": time_column,
        "clock_domain": CLOCK_DOMAIN,
        "time_column_policy": (
            "strict single CLOCK_MONOTONIC_RAW column with >=95% eligible coverage; "
            "realtime/Unix fallback is forbidden"
        ),
        "time_column_candidates": time_selection,
        "monotonic_minus_raw_audit": clock_pair_stats,
        "raw_row_count": raw_row_count,
        "role_policy": role_policy,
        "role_filtered_count": role_filtered_count,
        "tracking_status_policy": tracking_status_policy,
        "tracking_status_filtered_count": tracking_status_filtered_count,
        "sample_count": int(host_sec.size),
        "duplicate_timestamp_count": duplicate_count,
        "start_monotonic_raw_sec": float(host_sec[0]),
        "end_monotonic_raw_sec": float(host_sec[-1]),
        "duration_sec": float(host_sec[-1] - host_sec[0]),
        "gap_ms": {
            "median": float(np.median(gaps_ms)),
            "p95": float(np.percentile(gaps_ms, 95)),
            "p99": float(np.percentile(gaps_ms, 99)),
            "max": float(np.max(gaps_ms)),
            "over_10ms": int(np.count_nonzero(gaps_ms > 10.0)),
            "over_16_683ms": int(np.count_nonzero(gaps_ms > 1001.0 / 60.0)),
            "over_25ms": int(np.count_nonzero(gaps_ms > 25.0)),
        },
    }
    return host_sec, norm, stats


def _walk_camera_results(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        results = value.get("camera_results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    yield item
        for child in value.values():
            yield from _walk_camera_results(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_camera_results(child)


def load_clock_anchor(
    log_path: Path, arm: str, capture_role: str | None = None
) -> dict[str, Any]:
    total_start: dict[str, Any] | None = None
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "total_start":
                total_start = row
                break
    if total_start is None:
        raise RuntimeError(f"no total_start record in {log_path}")
    snapshot = total_start.get("host_clock_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"total_start has no host_clock_snapshot: {log_path}; strict "
            f"{CLOCK_DOMAIN} calibration cannot infer across clock domains"
        )
    required_snapshot = ("monotonic_ns", "monotonic_raw_ns", "realtime_unix_ns")
    missing_snapshot = [name for name in required_snapshot if snapshot.get(name) is None]
    if missing_snapshot:
        raise RuntimeError(
            f"total_start host_clock_snapshot is missing {missing_snapshot}: {log_path}"
        )
    snapshot_mono_ns = int(snapshot["monotonic_ns"])
    snapshot_raw_ns = int(snapshot["monotonic_raw_ns"])
    snapshot_unix_ns = int(snapshot["realtime_unix_ns"])
    bracket_ns = int(snapshot.get("realtime_bracket_ns") or 0)
    if bracket_ns < 0 or bracket_ns > 1_000_000:
        raise RuntimeError(
            f"total_start host_clock_snapshot realtime bracket is not trustworthy: "
            f"{bracket_ns}ns in {log_path}"
        )

    def build_anchor(
        *, source: str, monotonic_ns: int, observed_unix_ns: int | None
    ) -> dict[str, Any]:
        # CLOCK_MONOTONIC and CLOCK_MONOTONIC_RAW are sampled together in the
        # timestamp-log snapshot.  The BLE dispatch supplies MONOTONIC, so it
        # is translated once at the nearby snapshot.  Subsequent fitting and
        # all downstream selection stay entirely in RAW; the realtime value is
        # provenance/display metadata only.
        raw_ns = snapshot_raw_ns + (monotonic_ns - snapshot_mono_ns)
        projected_unix_ns = snapshot_unix_ns + (raw_ns - snapshot_raw_ns)
        return {
            "source": source,
            "clock_domain": CLOCK_DOMAIN,
            "clock_sec": raw_ns / 1e9,
            "clock_ns": raw_ns,
            "monotonic_raw_sec": raw_ns / 1e9,
            "monotonic_raw_ns": raw_ns,
            "dispatch_monotonic_ns": monotonic_ns,
            "observed_dispatch_unix_ns": observed_unix_ns,
            "unix_projection_ns": projected_unix_ns,
            "unix_projection_policy": (
                "single host_clock_snapshot projection for metadata only; forbidden for alignment"
            ),
            "snapshot": {
                "monotonic_ns": snapshot_mono_ns,
                "monotonic_raw_ns": snapshot_raw_ns,
                "realtime_unix_ns": snapshot_unix_ns,
                "realtime_bracket_ns": bracket_ns,
            },
            "role": expected_role,
        }

    expected_role = capture_role or f"insta_{arm}"
    for item in _walk_camera_results(total_start):
        if item.get("role") != expected_role:
            continue
        mono_ns = item.get("dispatch_monotonic_ns")
        if mono_ns is None:
            raise RuntimeError(
                f"BLE start result for {expected_role} has no dispatch_monotonic_ns: {log_path}"
            )
        unix_ns = item.get("dispatch_unix_ns")
        return build_anchor(
            source="timestamp_log.ble_start_dispatch_monotonic_to_raw_snapshot",
            monotonic_ns=int(mono_ns),
            observed_unix_ns=None if unix_ns is None else int(unix_ns),
        )
    mono_ns = total_start.get("monotonic_time_ns") or total_start.get("monotonicTimeNs")
    unix_ns = total_start.get("unix_time_ns") or total_start.get("unixTimeNs")
    if mono_ns is None:
        raise RuntimeError(f"total_start has no monotonic timestamp: {log_path}")
    return build_anchor(
        source="timestamp_log.total_start_monotonic_to_raw_snapshot",
        monotonic_ns=int(mono_ns),
        observed_unix_ns=None if unix_ns is None else int(unix_ns),
    )


def load_camera_role_binding(session: Path, arms: Sequence[str]) -> dict[str, dict[str, str]]:
    """Resolve canonical physical camera roles to immutable acquisition-log roles."""

    binding = {
        arm: {
            "canonical_role": f"insta_{arm}",
            "capture_role": f"insta_{arm}",
        }
        for arm in arms
    }
    manifest_path = session / "insta" / "transfer_manifest.json"
    if not manifest_path.is_file():
        return binding
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("cameras", []):
        canonical_role = str(item.get("role", "")).strip()
        if canonical_role not in {f"insta_{arm}" for arm in arms}:
            continue
        arm = canonical_role.removeprefix("insta_")
        capture_role = str(item.get("capture_role") or canonical_role).strip()
        if capture_role not in ("insta_left", "insta_right"):
            raise RuntimeError(
                f"invalid capture_role={capture_role!r} in {manifest_path}"
            )
        binding[arm] = {
            "canonical_role": canonical_role,
            "capture_role": capture_role,
            "camera_name": str(item.get("name", "")),
            "camera_address": str(item.get("address", "")),
        }
    return binding


def sliding_normalized_correlation(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Pearson correlation of x against every len(x) slice of y."""

    n = x.size
    if y.size < n or n < 3:
        raise ValueError("invalid arrays for sliding correlation")
    xc = x - np.mean(x)
    x_energy = float(np.dot(xc, xc))
    if x_energy <= np.finfo(float).eps:
        return np.full(y.size - n + 1, np.nan)
    dots = correlate(y, xc, mode="valid", method="fft")
    cumulative = np.r_[0.0, np.cumsum(y, dtype=np.float64)]
    cumulative_sq = np.r_[0.0, np.cumsum(y * y, dtype=np.float64)]
    sums = cumulative[n:] - cumulative[:-n]
    sums_sq = cumulative_sq[n:] - cumulative_sq[:-n]
    y_energy = np.maximum(sums_sq - sums * sums / n, 0.0)
    denominator = np.sqrt(x_energy * y_energy)
    return np.divide(
        dots,
        denominator,
        out=np.full_like(dots, np.nan, dtype=np.float64),
        where=denominator > np.finfo(float).eps,
    )


def estimate_window_offset(
    arm: str,
    segment: Segment,
    start_sec: float,
    end_sec: float,
    anchor_clock_sec: float,
    htc_time_sec: np.ndarray,
    htc_norm: np.ndarray,
    search_range_sec: float,
    sample_hz: float,
    smooth_sec: float,
    min_correlation: float,
    min_peak_margin: float,
    min_motion_std: float,
) -> WindowEstimate:
    dt = 1.0 / sample_hz
    x_time = np.arange(start_sec, end_sec, dt, dtype=np.float64)
    search_steps = int(round(search_range_sec * sample_hz))
    actual_search = search_steps * dt
    expected_y_count = x_time.size + 2 * search_steps
    y_relative_time = (
        start_sec - actual_search + np.arange(expected_y_count, dtype=np.float64) * dt
    )
    x = np.interp(x_time, segment.imu_time_sec, segment.gyro_norm_rad_s)
    y = np.interp(anchor_clock_sec + y_relative_time, htc_time_sec, htc_norm)
    sigma = max(0.0, smooth_sec * sample_hz)
    if sigma >= 0.5:
        x = gaussian_filter1d(x, sigma=sigma, mode="nearest")
        y = gaussian_filter1d(y, sigma=sigma, mode="nearest")
    motion_std = float(np.std(x))
    scores = sliding_normalized_correlation(x, y)
    if not np.any(np.isfinite(scores)):
        peak_index = 0
        peak_score = float("nan")
        refined_index = 0.0
    else:
        peak_index = int(np.nanargmax(scores))
        peak_score = float(scores[peak_index])
        refined_index = float(peak_index)
        if 0 < peak_index < scores.size - 1:
            left, middle, right = scores[peak_index - 1 : peak_index + 2]
            curvature = left - 2.0 * middle + right
            if np.isfinite(curvature) and abs(curvature) > 1e-12:
                refined_index += float(np.clip(0.5 * (left - right) / curvature, -1.0, 1.0))
    offset_sec = -actual_search + refined_index * dt
    exclusion = max(1, int(round(0.050 * sample_hz)))
    competing = scores.copy()
    competing[max(0, peak_index - exclusion) : peak_index + exclusion + 1] = np.nan
    second_score = float(np.nanmax(competing)) if np.any(np.isfinite(competing)) else -1.0
    peak_margin = peak_score - second_score
    near_peak = np.flatnonzero(scores >= peak_score - 0.01) if np.isfinite(peak_score) else np.array([])
    peak_width_ms = (
        float((near_peak[-1] - near_peak[0] + 1) * dt * 1000.0) if near_peak.size else float("inf")
    )
    reasons: list[str] = []
    if not np.isfinite(peak_score) or peak_score < min_correlation:
        reasons.append("low_correlation")
    if peak_margin < min_peak_margin:
        reasons.append("ambiguous_peak")
    if motion_std < min_motion_std:
        reasons.append("insufficient_rotation_motion")
    return WindowEstimate(
        arm=arm,
        source_segment=segment.source_segment,
        start_camera_sec=start_sec,
        end_camera_sec=end_sec,
        center_camera_sec=(start_sec + end_sec) / 2.0,
        offset_sec=float(offset_sec),
        correlation=peak_score,
        peak_margin=float(peak_margin),
        peak_width_ms=peak_width_ms,
        motion_std_rad_s=motion_std,
        accepted=not reasons,
        reject_reason=",".join(reasons) if reasons else None,
    )


def window_ranges(
    segment: Segment,
    htc_start_relative: float,
    htc_end_relative: float,
    search_range_sec: float,
    window_sec: float,
    stride_sec: float,
) -> list[tuple[float, float]]:
    video_start = segment.native_start_sec
    video_end = segment.native_end_frame_sec
    start = max(
        video_start,
        float(segment.imu_time_sec[0]),
        htc_start_relative + search_range_sec,
    )
    end = min(
        video_end,
        float(segment.imu_time_sec[-1]),
        htc_end_relative - search_range_sec,
    )
    available = end - start
    minimum_window = max(6.0, 2.0 * search_range_sec + 2.0)
    if available < minimum_window:
        return []
    effective_window = window_sec
    effective_stride = stride_sec
    # A 120-second fixed window produced zero/one estimates for ordinary short
    # demonstrations. Keep the long-session defaults, but form at least two
    # endpoint windows when there is enough signal for safe correlation.
    if available < window_sec + stride_sec:
        effective_window = max(minimum_window, min(window_sec, available * 0.60))
        effective_stride = max(1e-3, min(stride_sec, available - effective_window))
    starts = list(np.arange(start, end - effective_window + 1e-9, effective_stride))
    last_start = end - effective_window
    if not starts or last_start - starts[-1] > effective_stride * 0.25:
        starts.append(last_start)
    return [(float(item), float(item + effective_window)) for item in starts]


def pairing_window_summary(windows: Sequence[WindowEstimate]) -> dict[str, Any]:
    correlations = np.asarray(
        [item.correlation for item in windows if np.isfinite(item.correlation)],
        dtype=np.float64,
    )
    accepted = [item for item in windows if item.accepted]
    return {
        "window_count": len(windows),
        "accepted_window_count": len(accepted),
        "median_correlation": (
            float(np.median(correlations)) if correlations.size else None
        ),
        "p10_correlation": (
            float(np.percentile(correlations, 10)) if correlations.size else None
        ),
        "p90_correlation": (
            float(np.percentile(correlations, 90)) if correlations.size else None
        ),
        "max_correlation": float(np.max(correlations)) if correlations.size else None,
        "median_peak_margin": (
            float(np.median([item.peak_margin for item in windows])) if windows else None
        ),
        "reject_reasons": {
            reason: sum(
                reason in (item.reject_reason or "").split(",") for item in windows
            )
            for reason in (
                "low_correlation",
                "ambiguous_peak",
                "insufficient_rotation_motion",
            )
        },
    }


def select_tracker_assignment(
    pairings: dict[str, dict[str, dict[str, Any]]],
    *,
    min_correlation: float,
    min_score_margin: float,
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Select one camera-to-tracker bijection, or fail closed if ambiguous."""

    candidates = {
        "identity": {"left": "left", "right": "right"},
        "swapped": {"left": "right", "right": "left"},
    }
    candidate_reports: dict[str, dict[str, Any]] = {}
    for name, mapping in candidates.items():
        selected = [pairings[camera][tracker] for camera, tracker in mapping.items()]
        medians = [item.get("median_correlation") for item in selected]
        finite_medians = [float(value) for value in medians if value is not None]
        score = min(finite_medians) if len(finite_medians) == 2 else float("-inf")
        minimum_accepted = min(
            int(item.get("accepted_window_count", 0)) for item in selected
        )
        viable = minimum_accepted >= 2 and score >= min_correlation
        candidate_reports[name] = {
            "camera_to_tracker": mapping,
            "score": None if not math.isfinite(score) else score,
            "minimum_accepted_window_count": minimum_accepted,
            "viable": viable,
        }

    viable = [name for name, item in candidate_reports.items() if item["viable"]]
    selected_name: str | None = None
    reason: str
    if len(viable) == 1:
        selected_name = viable[0]
        reason = "only_one_bijection_passed_strict_pair_quality"
    elif len(viable) == 2:
        ranked = sorted(
            viable,
            key=lambda name: float(candidate_reports[name]["score"]),
            reverse=True,
        )
        score_margin = float(candidate_reports[ranked[0]]["score"]) - float(
            candidate_reports[ranked[1]]["score"]
        )
        if score_margin >= min_score_margin:
            selected_name = ranked[0]
            reason = "best_bijection_cleared_correlation_margin"
        else:
            reason = "both_bijections_passed_but_assignment_is_ambiguous"
    else:
        reason = "neither_bijection_passed_strict_pair_quality"

    selected_mapping = None if selected_name is None else candidates[selected_name]
    return selected_mapping, {
        "method": "audited_2x2_gyro_norm_bijection",
        "status": "accepted" if selected_mapping is not None else "ambiguous_or_insufficient",
        "status_reason": reason,
        "selected_bijection": selected_name,
        "camera_to_tracker": selected_mapping,
        "minimum_correlation": min_correlation,
        "minimum_bijection_score_margin": min_score_margin,
        "candidates": candidate_reports,
        "pairings": pairings,
    }


def robust_models(
    segments: Sequence[Segment], windows: Sequence[WindowEstimate]
) -> tuple[dict[int, SegmentModel], dict[str, Any]]:
    accepted = [item for item in windows if item.accepted]
    if len(accepted) < 2:
        raise RuntimeError("fewer than two unambiguous motion windows; clock calibration is unsafe")
    x = np.asarray([item.center_camera_sec for item in accepted], dtype=np.float64)
    y = np.asarray([item.offset_sec for item in accepted], dtype=np.float64)
    group = np.asarray([item.source_segment for item in accepted], dtype=np.int64)

    def segment_slope(segment_id: int, mask: np.ndarray) -> float:
        """Robust within-chapter Theil-Sen slope.

        X5 chapters have independent mux/clock epochs.  Requiring one shared
        slope across a multi-hour capture can turn a valid non-linear host/X5
        clock relationship into a false chapter rejection.  No cross-chapter
        pair is used here, so chapter timestamp discontinuities cannot bias
        either the slope or intercept.
        """
        slopes: list[float] = []
        indices = np.flatnonzero(mask & (group == segment_id))
        for right_pos in range(1, indices.size):
            right = indices[right_pos]
            left_indices = indices[:right_pos]
            delta_x = x[right] - x[left_indices]
            usable = np.abs(delta_x) > 1e-9
            slopes.extend(((y[right] - y[left_indices[usable]]) / delta_x[usable]).tolist())
        if not slopes:
            return 0.0
        return float(np.median(np.asarray(slopes)))

    inlier = np.ones(x.size, dtype=bool)
    for segment_id in np.unique(group):
        selected = group == segment_id
        initial_slope = segment_slope(int(segment_id), inlier)
        segment_intercept = float(np.median(y[selected] - initial_slope * x[selected]))
        residual = y[selected] - (segment_intercept + initial_slope * x[selected])
        center = float(np.median(residual))
        mad = float(np.median(np.abs(residual - center)))
        threshold = max(0.020, 4.0 * 1.4826 * mad)
        inlier[selected] = np.abs(residual - center) <= threshold
    segment_slopes = {
        int(segment_id): segment_slope(int(segment_id), inlier)
        for segment_id in np.unique(group)
    }
    representative_slope = float(np.median(list(segment_slopes.values())))
    per_group_intercepts = [
        float(
            np.median(
                y[inlier & (group == segment_id)]
                - segment_slopes[int(segment_id)] * x[inlier & (group == segment_id)]
            )
        )
        for segment_id in np.unique(group)
        if np.any(inlier & (group == segment_id))
    ]
    global_intercept = float(np.median(per_group_intercepts))

    models: dict[int, SegmentModel] = {}
    for segment in segments:
        selected = [item for item in accepted if item.source_segment == segment.source_segment]
        fallback = None
        if selected:
            sx = np.asarray([item.center_camera_sec for item in selected])
            sy = np.asarray([item.offset_sec for item in selected])
            slope = segment_slopes[segment.source_segment]
            segment_intercept = float(np.median(sy - slope * sx))
            segment_residual_ms = (sy - (segment_intercept + slope * sx)) * 1000.0
            correlations = np.asarray([item.correlation for item in selected])
        else:
            slope = representative_slope
            segment_intercept = global_intercept
            segment_residual_ms = np.asarray([], dtype=np.float64)
            correlations = np.asarray([], dtype=np.float64)
            has_candidate_window = any(
                item.source_segment == segment.source_segment for item in windows
            )
            fallback = (
                "global_intercept_no_accepted_segment_window"
                if has_candidate_window
                else "pending_nearest_calibrated_segment_no_candidate_window"
            )
        residual_p95 = (
            float(np.percentile(np.abs(segment_residual_ms), 95))
            if segment_residual_ms.size
            else float("inf")
        )
        median_corr = float(np.median(correlations)) if correlations.size else 0.0
        count_factor = min(1.0, len(selected) / 4.0)
        residual_factor = max(0.0, 1.0 - min(residual_p95, 50.0) / 50.0)
        confidence = float(np.clip(median_corr * count_factor * residual_factor, 0.0, 1.0))
        models[segment.source_segment] = SegmentModel(
            source_segment=segment.source_segment,
            intercept_sec=segment_intercept,
            slope_sec_per_sec=slope,
            confidence=confidence,
            accepted_windows=len(selected),
            median_correlation=median_corr,
            residual_median_ms=(
                float(np.median(segment_residual_ms)) if segment_residual_ms.size else float("nan")
            ),
            residual_p95_ms=residual_p95,
            fallback=fallback,
        )

    # A terminal camera chapter can be only a few seconds long (for example,
    # recording stopped immediately after the firmware opened a new chapter).
    # It cannot contain even one safe correlation window.  Reuse the nearest
    # directly calibrated chapter model instead of inventing a global average
    # intercept across independent chapter fits.  The quality gate below only
    # permits this for short terminal chapters with zero candidate windows.
    calibrated_ids = [
        segment_id
        for segment_id, model in models.items()
        if model.accepted_windows >= 2
    ]
    for segment in segments:
        model = models[segment.source_segment]
        if model.fallback != "pending_nearest_calibrated_segment_no_candidate_window":
            continue
        if not calibrated_ids:
            model.fallback = "global_intercept_no_candidate_window_and_no_calibrated_donor"
            continue
        center = (segment.native_start_sec + segment.native_end_frame_sec) / 2.0
        donor_id = min(
            calibrated_ids,
            key=lambda segment_id: abs(
                center
                - (
                    segments[segment_id].native_start_sec
                    + segments[segment_id].native_end_frame_sec
                )
                / 2.0
            ),
        )
        donor = models[donor_id]
        model.intercept_sec = donor.intercept_sec
        model.slope_sec_per_sec = donor.slope_sec_per_sec
        model.confidence = donor.confidence * 0.8
        model.fallback = f"nearest_calibrated_segment_{donor_id}_no_candidate_window"
    global_residual_ms = (
        y
        - np.asarray(
            [models[item.source_segment].offset(item.center_camera_sec) for item in accepted]
        )
    ) * 1000.0
    segment_drift_ppm = {
        str(segment_id): slope * 1e6 for segment_id, slope in sorted(segment_slopes.items())
    }
    report = {
        "clock_domain": CLOCK_DOMAIN,
        "formula": (
            "host_monotonic_raw_sec = anchor_monotonic_raw_sec + "
            "camera_native_sec + offset(camera_native_sec)"
        ),
        "offset_formula": "offset_sec = segment_intercept_sec + slope_sec_per_sec * camera_native_sec",
        "slope_policy": "independent_robust_within_chapter",
        # Retained as a backwards-compatible aggregate diagnostic.  Timeline
        # generation uses each SegmentModel's own slope below.
        "common_slope_sec_per_sec": representative_slope,
        "common_drift_ppm": representative_slope * 1e6,
        "segment_drift_ppm": segment_drift_ppm,
        "max_abs_segment_drift_ppm": max(abs(value) for value in segment_drift_ppm.values()),
        "accepted_window_count": len(accepted),
        "rejected_window_count": len(windows) - len(accepted),
        "outlier_window_count": int(np.count_nonzero(~inlier)),
        "residual_median_ms": float(np.median(global_residual_ms)),
        "residual_p95_ms": float(np.percentile(np.abs(global_residual_ms), 95)),
        "segment_intercepts_allow_native_chapter_discontinuities": True,
        "short_terminal_segment_fallback_count": sum(
            "_no_candidate_window" in (model.fallback or "")
            and model.fallback.startswith("nearest_calibrated_segment_")
            for model in models.values()
        ),
    }
    return models, report


def calibration_quality_accepted(
    model_report: dict[str, Any],
    models: Iterable[SegmentModel],
    *,
    min_correlation: float,
    max_residual_p95_ms: float,
    max_abs_drift_ppm: float,
    segments: Sequence[Segment] | None = None,
    max_short_terminal_fallback_sec: float = 10.0,
) -> bool:
    model_list = list(models)
    segment_by_id = {
        item.source_segment: item for item in (segments or [])
    }

    def model_accepted(model: SegmentModel) -> bool:
        directly_calibrated = bool(
            model.accepted_windows >= 2
            and model.median_correlation >= min_correlation
            and math.isfinite(model.slope_sec_per_sec)
            and abs(model.slope_sec_per_sec * 1e6) <= max_abs_drift_ppm
            and math.isfinite(model.residual_p95_ms)
            and model.residual_p95_ms <= max_residual_p95_ms
        )
        if directly_calibrated:
            return True
        segment = segment_by_id.get(model.source_segment)
        if segment is None or not model.fallback:
            return False
        terminal_ids = {min(segment_by_id), max(segment_by_id)}
        return bool(
            model.source_segment in terminal_ids
            and segment.duration_sec <= max_short_terminal_fallback_sec
            and model.accepted_windows == 0
            and model.fallback.startswith("nearest_calibrated_segment_")
            and model.fallback.endswith("_no_candidate_window")
            and math.isfinite(model.slope_sec_per_sec)
            and abs(model.slope_sec_per_sec * 1e6) <= max_abs_drift_ppm
        )

    return bool(
        model_report["accepted_window_count"] >= 2
        and math.isfinite(float(model_report["residual_p95_ms"]))
        and float(model_report["residual_p95_ms"]) <= max_residual_p95_ms
        and math.isfinite(float(model_report["common_drift_ppm"]))
        and abs(float(model_report["common_drift_ppm"])) <= max_abs_drift_ppm
        and model_list
        and all(model_accepted(model) for model in model_list)
    )


def htc_gap_metrics(
    query_sec: np.ndarray, htc_sec: np.ndarray, max_gap_ms: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    right = np.searchsorted(htc_sec, query_sec, side="left")
    has_left = right > 0
    has_right = right < htc_sec.size
    left_index = np.clip(right - 1, 0, htc_sec.size - 1)
    right_index = np.clip(right, 0, htc_sec.size - 1)
    left_delta = np.abs(query_sec - htc_sec[left_index])
    right_delta = np.abs(htc_sec[right_index] - query_sec)
    nearest_ms = np.minimum(left_delta, right_delta) * 1000.0
    bracket_ms = (htc_sec[right_index] - htc_sec[left_index]) * 1000.0
    bracket_ms[~(has_left & has_right)] = np.inf
    valid = has_left & has_right & (bracket_ms <= max_gap_ms)
    return nearest_ms, bracket_ms, valid


def frame_rows_for_segment(
    segment: Segment,
    model: SegmentModel,
    anchor: dict[str, Any],
    htc_sec: np.ndarray,
    max_htc_gap_ms: float,
    last_kept_native_sec: float | None,
    last_kept_host_sec: float | None,
    next_dedup_idx: int,
) -> tuple[list[dict[str, Any]], float | None, float | None, int, dict[str, Any]]:
    local = np.arange(segment.frame_count, dtype=np.int64)
    source_pts = segment.source_frame_pts()
    camera_native = segment.native_start_sec + (source_pts - source_pts[0])
    offsets = np.asarray(model.offset(camera_native), dtype=np.float64)
    host_raw = float(anchor["clock_sec"]) + camera_native + offsets
    unix_projection_anchor = int(anchor["unix_projection_ns"]) / 1e9
    host_unix = unix_projection_anchor + (host_raw - float(anchor["clock_sec"]))
    nearest_ms, bracket_ms, htc_valid = htc_gap_metrics(host_raw, htc_sec, max_htc_gap_ms)

    keep = np.ones(segment.frame_count, dtype=bool)
    if last_kept_native_sec is not None:
        keep &= camera_native > last_kept_native_sec + 1e-9
    if last_kept_host_sec is not None:
        keep &= host_raw > last_kept_host_sec + 1e-9
    dedup = np.full(segment.frame_count, -1, dtype=np.int64)
    kept_count = int(np.count_nonzero(keep))
    dedup[keep] = np.arange(next_dedup_idx, next_dedup_idx + kept_count)
    if kept_count:
        last_kept_index = np.flatnonzero(keep)[-1]
        last_kept_native_sec = float(camera_native[last_kept_index])
        last_kept_host_sec = float(host_raw[last_kept_index])
    next_dedup_idx += kept_count

    rows: list[dict[str, Any]] = []
    for index in range(segment.frame_count):
        overlap = not bool(keep[index])
        if overlap:
            boundary_status = "overlap_drop_candidate"
        elif index == 0 and segment.concat_discontinuity_sec < -1e-9:
            boundary_status = "segment_start_overlap"
        elif index == 0 and segment.concat_discontinuity_sec > 1e-9:
            boundary_status = "segment_start_gap"
        else:
            boundary_status = "ok"
        rows.append(
            {
                "original_concat_frame_idx": segment.concat_start_frame + index,
                "frame_idx": segment.concat_start_frame + index,
                "source_segment": segment.source_segment,
                "source_file": segment.path.name,
                "source_local_frame_idx": index,
                "source_pts_sec": source_pts[index],
                "source_pts_method": (
                    "ffprobe_video_packet_pts_time_sorted_to_presentation_order"
                    if segment.frame_pts_sec is not None
                    else "derived_cfr_from_ffprobe_stream"
                ),
                "camera_native_time_sec": camera_native[index],
                "timestamp_sec": camera_native[index],
                "fps": segment.fps,
                "concat_nominal_time_sec": segment.concat_start_sec + index / segment.fps,
                "concat_vs_native_ms": (
                    segment.concat_start_sec + index / segment.fps - camera_native[index]
                )
                * 1000.0,
                "calibrated_host_unix_sec": host_unix[index],
                "calibrated_host_unix_ns": int(round(host_unix[index] * 1e9)),
                "unix_time_ms": host_unix[index] * 1000.0,
                "calibrated_host_monotonic_raw_sec": host_raw[index],
                "calibrated_host_monotonic_raw_ns": int(round(host_raw[index] * 1e9)),
                "calibrated_host_monotonic_sec": float("nan"),
                "applied_offset_ms": offsets[index] * 1000.0,
                "calibration_confidence": model.confidence,
                "htc_nearest_sample_delta_ms": nearest_ms[index],
                "htc_bracket_gap_ms": bracket_ms[index],
                "htc_interpolation_valid": int(htc_valid[index]),
                "recommended_keep": int(keep[index]),
                "timeline_valid": int(keep[index]),
                "usable_for_timeline": int(keep[index] and htc_valid[index]),
                "dedup_frame_idx": "" if dedup[index] < 0 else int(dedup[index]),
                "boundary_status": boundary_status,
            }
        )
    dropped = np.flatnonzero(~keep)
    mapping = {
        "source_segment": segment.source_segment,
        "original_concat_frame_start": segment.concat_start_frame,
        "frame_count": segment.frame_count,
        "recommended_drop_local_frame_start": int(dropped[0]) if dropped.size else None,
        "recommended_drop_local_frame_end_inclusive": int(dropped[-1]) if dropped.size else None,
        "recommended_drop_count": int(dropped.size),
        "reason": "native_chapter_time_overlap_keep_previous_segment" if dropped.size else None,
    }
    return rows, last_kept_native_sec, last_kept_host_sec, next_dedup_idx, mapping


FRAME_COLUMNS = [
    "calibration_id",
    "original_concat_frame_idx",
    "frame_idx",
    "source_segment",
    "source_file",
    "source_local_frame_idx",
    "source_pts_sec",
    "source_pts_method",
    "camera_native_time_sec",
    "timestamp_sec",
    "fps",
    "concat_nominal_time_sec",
    "concat_vs_native_ms",
    "calibrated_host_unix_sec",
    "calibrated_host_unix_ns",
    "unix_time_ms",
    "calibrated_host_monotonic_raw_sec",
    "calibrated_host_monotonic_raw_ns",
    "calibrated_host_monotonic_sec",
    "applied_offset_ms",
    "calibration_confidence",
    "htc_nearest_sample_delta_ms",
    "htc_bracket_gap_ms",
    "htc_interpolation_valid",
    "recommended_keep",
    "timeline_valid",
    "usable_for_timeline",
    "dedup_frame_idx",
    "boundary_status",
]


def format_frame_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "source_pts_sec",
        "camera_native_time_sec",
        "timestamp_sec",
        "concat_nominal_time_sec",
        "calibrated_host_unix_sec",
        "calibrated_host_monotonic_raw_sec",
        "calibrated_host_monotonic_sec",
    ):
        value = result[key]
        result[key] = "" if not np.isfinite(value) else f"{float(value):.9f}"
    for key in (
        "concat_vs_native_ms",
        "unix_time_ms",
        "applied_offset_ms",
        "htc_nearest_sample_delta_ms",
        "htc_bracket_gap_ms",
    ):
        value = result[key]
        result[key] = "" if not np.isfinite(value) else f"{float(value):.6f}"
    result["calibration_confidence"] = f"{float(result['calibration_confidence']):.6f}"
    result["fps"] = f"{float(result['fps']):.9f}"
    return result


def atomic_csv(path: Path, rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    temporary.replace(path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def json_number(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def segment_to_report(segment: Segment, model: SegmentModel, mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_segment": segment.source_segment,
        "source_file": str(segment.path.resolve()),
        "source_file_identity": file_identity(segment.path),
        "first_frame_timestamp_raw_us": segment.first_frame_timestamp_raw,
        "native_start_sec": segment.native_start_sec,
        "frame_count": segment.frame_count,
        "fps": segment.fps,
        "duration_sec": segment.duration_sec,
        "source_pts_start_sec": segment.pts_start_sec,
        "source_pts_method": (
            "ffprobe_video_packet_pts_time_sorted_to_presentation_order"
            if segment.frame_pts_sec is not None
            else "derived_cfr_from_ffprobe_stream"
        ),
        "source_pts_diagnostics": segment.pts_diagnostics,
        "timing_artifact": segment.timing_artifact or {"mode": "legacy_direct_insv"},
        "ffprobe_time_base": segment.time_base,
        "ffprobe_avg_frame_rate": segment.avg_frame_rate,
        "ffprobe_r_frame_rate": segment.r_frame_rate,
        "concat_start_frame": segment.concat_start_frame,
        "concat_start_sec": segment.concat_start_sec,
        "concat_start_minus_native_start_ms": -segment.concat_discontinuity_sec * 1000.0,
        "native_overlap_with_previous_last_frame_ms": segment.overlap_with_previous_last_frame_sec
        * 1000.0,
        "camera_serial": segment.camera_serial,
        "firmware": segment.firmware,
        "rolling_shutter_ms": segment.rolling_shutter_ms,
        "gyro_input_unit": segment.gyro_input_unit,
        "imu_sample_count": int(segment.imu_time_sec.size),
        "imu_start_camera_sec": float(segment.imu_time_sec[0]),
        "imu_end_camera_sec": float(segment.imu_time_sec[-1]),
        "clock_model": {
            "intercept_sec": model.intercept_sec,
            "slope_sec_per_sec": model.slope_sec_per_sec,
            "confidence": model.confidence,
            "accepted_windows": model.accepted_windows,
            "median_correlation": model.median_correlation,
            "residual_median_ms": json_number(model.residual_median_ms),
            "residual_p95_ms": json_number(model.residual_p95_ms),
            "fallback": model.fallback,
        },
        "dedup_recommendation": mapping,
    }


def window_to_report(item: WindowEstimate) -> dict[str, Any]:
    return {
        "source_segment": item.source_segment,
        "start_camera_sec": item.start_camera_sec,
        "end_camera_sec": item.end_camera_sec,
        "center_camera_sec": item.center_camera_sec,
        "offset_ms": json_number(item.offset_sec * 1000.0),
        "correlation": json_number(item.correlation),
        "peak_margin": json_number(item.peak_margin),
        "peak_width_ms": json_number(item.peak_width_ms),
        "motion_std_rad_s": json_number(item.motion_std_rad_s),
        "accepted": item.accepted,
        "reject_reason": item.reject_reason,
    }


def load_camera_segments(
    arm: str,
    paths: Sequence[Path],
    output_root: Path,
    args: argparse.Namespace,
    timing_artifact_paths: Sequence[Path] | None = None,
    *,
    announce: bool = True,
) -> list[Segment]:
    if not paths:
        raise RuntimeError(f"no {arm} INSV files found")
    if announce:
        print(f"[info] {arm}: loading {len(paths)} X5 chapter(s)", flush=True)
    if timing_artifact_paths is not None and len(timing_artifact_paths) != len(paths):
        raise RuntimeError(
            f"{arm} timing artifact/source count mismatch: "
            f"artifacts={len(timing_artifact_paths)} sources={len(paths)}"
        )
    segments: list[Segment] = []
    reference: int | None = None
    for index, path in enumerate(paths):
        if timing_artifact_paths is None:
            segment, reference = load_segment(
                arm,
                path,
                index,
                reference,
                args.ffprobe_bin,
                None if args.dry_run else output_root / ".pts_cache",
            )
        else:
            segment, reference = load_segment_from_timing_artifact(
                arm,
                path,
                index,
                reference,
                timing_artifact_paths[index],
            )
        segments.append(segment)
        if announce:
            print(
                f"[info] {arm}: {path.name}: native_start={segment.native_start_sec:.6f}s "
                f"frames={segment.frame_count} imu={segment.imu_time_sec.size} "
                f"source={'_01_artifact' if timing_artifact_paths is not None else 'raw_insv'}",
                flush=True,
            )
    finalize_segment_layout(segments)
    return segments


def estimate_pair_windows(
    camera_arm: str,
    segments: Sequence[Segment],
    anchor: dict[str, Any],
    htc_sec: np.ndarray,
    htc_norm: np.ndarray,
    args: argparse.Namespace,
) -> list[WindowEstimate]:
    windows: list[WindowEstimate] = []
    htc_start_relative = float(htc_sec[0] - anchor["clock_sec"])
    htc_end_relative = float(htc_sec[-1] - anchor["clock_sec"])
    for segment in segments:
        ranges = window_ranges(
            segment,
            htc_start_relative,
            htc_end_relative,
            args.search_range_sec,
            args.window_sec,
            args.stride_sec,
        )
        for start, end in ranges:
            windows.append(
                estimate_window_offset(
                    camera_arm,
                    segment,
                    start,
                    end,
                    float(anchor["clock_sec"]),
                    htc_sec,
                    htc_norm,
                    args.search_range_sec,
                    args.resample_hz,
                    args.smooth_sec,
                    args.min_correlation,
                    args.min_peak_margin,
                    args.min_motion_std,
                )
            )
    return windows


def audit_tracker_assignment(
    paths_by_camera: dict[str, Sequence[Path]],
    htc_paths_by_tracker: dict[str, Path],
    timestamp_log: Path,
    output_root: Path,
    args: argparse.Namespace,
    timing_artifacts_by_camera: dict[str, Sequence[Path] | None],
    camera_role_binding: dict[str, dict[str, str]],
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Audit all four camera/tracker pairings before fitting either timeline."""

    print("[audit] resolving camera-to-HTC assignment from all 2x2 gyro pairings", flush=True)
    segments = {
        camera: load_camera_segments(
            camera,
            paths_by_camera[camera],
            output_root,
            args,
            timing_artifacts_by_camera[camera],
            announce=False,
        )
        for camera in ("left", "right")
    }
    anchors = {
        camera: load_clock_anchor(
            timestamp_log,
            camera,
            camera_role_binding[camera]["capture_role"],
        )
        for camera in ("left", "right")
    }
    tracker_signals = {
        tracker: load_htc(htc_paths_by_tracker[tracker], tracker)[:2]
        for tracker in ("left", "right")
    }
    pairings: dict[str, dict[str, dict[str, Any]]] = {}
    for camera in ("left", "right"):
        pairings[camera] = {}
        for tracker in ("left", "right"):
            htc_sec, htc_norm = tracker_signals[tracker]
            windows = estimate_pair_windows(
                camera,
                segments[camera],
                anchors[camera],
                htc_sec,
                htc_norm,
                args,
            )
            summary = pairing_window_summary(windows)
            summary.update(
                {
                    "camera_arm": camera,
                    "tracker_arm": tracker,
                    "tracker_path": str(htc_paths_by_tracker[tracker]),
                }
            )
            pairings[camera][tracker] = summary
            print(
                f"[audit] camera={camera} tracker={tracker} "
                f"accepted={summary['accepted_window_count']}/{summary['window_count']} "
                f"median_corr={summary['median_correlation']}",
                flush=True,
            )
    mapping, audit = select_tracker_assignment(
        pairings,
        min_correlation=args.min_correlation,
        min_score_margin=args.min_assignment_score_margin,
    )
    print(
        f"[audit] tracker assignment status={audit['status']} "
        f"selected={audit['selected_bijection']} mapping={mapping}",
        flush=True,
    )
    return mapping, audit


def calibrate_arm(
    arm: str,
    tracker_arm: str,
    capture_camera_role: str,
    paths: Sequence[Path],
    htc_path: Path,
    timestamp_log: Path,
    output_root: Path,
    args: argparse.Namespace,
    calibration_id: str,
    timing_artifact_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    segments = load_camera_segments(
        arm,
        paths,
        output_root,
        args,
        timing_artifact_paths,
    )
    htc_sec, htc_norm, htc_stats = load_htc(htc_path, tracker_arm)
    anchor = load_clock_anchor(timestamp_log, arm, capture_camera_role)
    print(
        f"[info] camera={arm} tracker={tracker_arm}: "
        f"anchor_raw={anchor['clock_sec']:.9f} ({anchor['source']}), "
        f"HTC samples={htc_sec.size}",
        flush=True,
    )

    windows = estimate_pair_windows(arm, segments, anchor, htc_sec, htc_norm, args)
    models, model_report = robust_models(segments, windows)
    print(
        f"[result] {arm}: accepted={model_report['accepted_window_count']}/"
        f"{len(windows)}, drift={model_report['common_drift_ppm']:.3f}ppm, "
        f"residual_p95={model_report['residual_p95_ms']:.3f}ms",
        flush=True,
    )

    all_rows: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    last_kept: float | None = None
    last_kept_host: float | None = None
    next_dedup = 0
    for segment in segments:
        rows, last_kept, last_kept_host, next_dedup, mapping = frame_rows_for_segment(
            segment,
            models[segment.source_segment],
            anchor,
            htc_sec,
            args.max_htc_gap_ms,
            last_kept,
            last_kept_host,
            next_dedup,
        )
        all_rows.extend(rows)
        mappings.append(mapping)
    invalid_htc = sum(1 for row in all_rows if not row["htc_interpolation_valid"])
    dropped_overlap = sum(1 for row in all_rows if not row["recommended_keep"])
    for row in all_rows:
        row["calibration_id"] = calibration_id

    arm_dir = output_root / arm
    frame_path = arm_dir / "calibrated_frame_timestamps.csv"
    mapping_path = arm_dir / "frame_dedup_mapping.csv"
    if not args.dry_run:
        atomic_csv(frame_path, (format_frame_row(row) for row in all_rows), FRAME_COLUMNS)
        mapping_columns = [
            "calibration_id",
            "original_concat_frame_idx",
            "source_segment",
            "source_local_frame_idx",
            "recommended_keep",
            "dedup_frame_idx",
            "boundary_status",
        ]
        atomic_csv(
            mapping_path,
            ({key: row[key] for key in mapping_columns} for row in all_rows),
            mapping_columns,
        )
    report = {
        "arm": arm,
        "camera_arm": arm,
        "tracker_arm": tracker_arm,
        "tracker_role": f"htc_{tracker_arm}",
        "calibration_id": calibration_id,
        "anchor": anchor,
        "htc": htc_stats,
        "clock_model": model_report,
        "segments": [
            segment_to_report(segment, models[segment.source_segment], mappings[index])
            for index, segment in enumerate(segments)
        ],
        "windows": [window_to_report(item) for item in windows],
        "frame_timeline": {
            "path": str(frame_path),
            "dedup_mapping_path": str(mapping_path),
            "file_identity": file_identity(frame_path) if not args.dry_run else None,
            "dedup_mapping_file_identity": file_identity(mapping_path) if not args.dry_run else None,
            "original_frame_count": len(all_rows),
            "recommended_keep_count": len(all_rows) - dropped_overlap,
            "recommended_overlap_drop_count": dropped_overlap,
            "htc_interpolation_invalid_count": invalid_htc,
            "htc_max_bracket_gap_ms": args.max_htc_gap_ms,
            "clock_domain": CLOCK_DOMAIN,
            "primary_time_columns": [
                "calibrated_host_monotonic_raw_sec",
                "calibrated_host_monotonic_raw_ns",
            ],
            "unix_columns_policy": "metadata-only fixed-snapshot projection; never use for alignment",
            "monotonicity_policy": (
                "Full CSV preserves original concat order and may be non-monotonic at a native chapter overlap. "
                "Use recommended_keep=1 and dedup_frame_idx for a monotonic downstream timeline."
            ),
        },
        "quality": {
            "calibration_accepted": calibration_quality_accepted(
                model_report,
                models.values(),
                min_correlation=args.min_correlation,
                max_residual_p95_ms=args.max_residual_p95_ms,
                max_abs_drift_ppm=args.max_abs_drift_ppm,
                segments=segments,
                max_short_terminal_fallback_sec=args.max_short_terminal_fallback_sec,
            ),
            "sub_10ms_guaranteed": False,
            "reason": (
                "Correlation calibrates effective X5-IMU exposure timing to HTC HID-read "
                "CLOCK_MONOTONIC_RAW. Residual and per-frame interpolation validity are reported; "
                "sensor-internal exposure/packet latency is not independently observable."
            ),
        },
    }
    report["status"] = (
        "accepted" if report["quality"]["calibration_accepted"] else "rejected"
    )
    # Release the multi-million-sample arrays before calibrating the other arm.
    del all_rows, htc_sec, htc_norm
    return report


def resolve_ffprobe(value: str | None) -> str:
    if value:
        return value
    sibling = Path(sys.executable).with_name("ffprobe")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("ffprobe")
    if not found:
        raise RuntimeError("ffprobe not found; use --ffprobe-bin")
    return found


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline X5 IMU to HTC host-clock calibration without SLAM"
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timestamp-log", type=Path)
    parser.add_argument("--left-insv", action="append", type=Path, default=[])
    parser.add_argument("--right-insv", action="append", type=Path, default=[])
    parser.add_argument(
        "--left-raw-manifest",
        type=Path,
        help="_01 left manifest containing provenance-bound clock timing artifacts.",
    )
    parser.add_argument(
        "--right-raw-manifest",
        type=Path,
        help="_01 right manifest containing provenance-bound clock timing artifacts.",
    )
    parser.add_argument("--htc-left", type=Path)
    parser.add_argument("--htc-right", type=Path)
    parser.add_argument("--arms", choices=("left", "right", "both"), default="both")
    parser.add_argument(
        "--arm-workers",
        type=int,
        default=1,
        help="左右臂独立校准进程数；both 模式在 SSD 主机上建议 2。",
    )
    parser.add_argument("--ffprobe-bin")
    parser.add_argument("--window-sec", type=float, default=120.0)
    parser.add_argument("--stride-sec", type=float, default=90.0)
    parser.add_argument("--search-range-sec", type=float, default=2.0)
    parser.add_argument("--resample-hz", type=float, default=500.0)
    parser.add_argument("--smooth-sec", type=float, default=0.020)
    parser.add_argument("--min-correlation", type=float, default=0.50)
    parser.add_argument("--min-peak-margin", type=float, default=0.005)
    parser.add_argument("--min-motion-std", type=float, default=0.03)
    parser.add_argument(
        "--min-assignment-score-margin",
        type=float,
        default=0.15,
        help=(
            "Minimum worst-pair median-correlation advantage required when both "
            "camera/HTC bijections otherwise pass. Ambiguous assignments fail closed."
        ),
    )
    parser.add_argument(
        "--max-short-terminal-fallback-sec",
        type=float,
        default=10.0,
        help=(
            "Maximum duration of a first/last chapter that may inherit the nearest "
            "calibrated chapter model when it contains no possible correlation window."
        ),
    )
    parser.add_argument("--max-htc-gap-ms", type=float, default=25.0)
    parser.add_argument("--max-residual-p95-ms", type=float, default=10.0)
    parser.add_argument("--max-abs-drift-ppm", type=float, default=200.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print the calibration summary, but write no files",
    )
    args = parser.parse_args(argv)
    for name in (
        "window_sec",
        "stride_sec",
        "search_range_sec",
        "resample_hz",
        "max_htc_gap_ms",
        "max_residual_p95_ms",
        "max_abs_drift_ppm",
        "max_short_terminal_fallback_sec",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.window_sec <= 2 * args.search_range_sec:
        parser.error("--window-sec must be greater than twice --search-range-sec")
    if args.arm_workers < 1:
        parser.error("--arm-workers must be >= 1")
    if args.min_assignment_score_margin < 0:
        parser.error("--min-assignment-score-margin must be non-negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    session = args.session_dir.expanduser().resolve()
    if not session.is_dir():
        raise SystemExit(f"session directory does not exist: {session}")
    output = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else session / "tmp" / "sync_calibration"
    )
    timestamp_log = (
        args.timestamp_log.expanduser().resolve()
        if args.timestamp_log
        else session / "timestamp_log.jsonl"
    )
    if not timestamp_log.is_file():
        raise SystemExit(f"timestamp log does not exist: {timestamp_log}")
    args.ffprobe_bin = resolve_ffprobe(args.ffprobe_bin)
    arms = ("left", "right") if args.arms == "both" else (args.arms,)
    configured_paths = {
        "left": [item.expanduser().resolve() for item in args.left_insv],
        "right": [item.expanduser().resolve() for item in args.right_insv],
    }
    configured_htc = {
        "left": args.htc_left.expanduser().resolve() if args.htc_left else session / "htc_left.csv",
        "right": args.htc_right.expanduser().resolve() if args.htc_right else session / "htc_right.csv",
    }
    camera_role_binding = load_camera_role_binding(session, arms)
    transfer_manifest = session / "insta" / "transfer_manifest.json"
    role_correction = session / "insta" / "camera_role_correction.json"
    resolved_arm_paths = {
        arm: (configured_paths[arm] or discover_insv(session, arm)) for arm in arms
    }
    configured_raw_manifests = {
        "left": (
            args.left_raw_manifest.expanduser().resolve()
            if args.left_raw_manifest is not None
            else None
        ),
        "right": (
            args.right_raw_manifest.expanduser().resolve()
            if args.right_raw_manifest is not None
            else None
        ),
    }
    timing_artifact_paths: dict[str, list[Path] | None] = {}
    for arm in arms:
        raw_manifest = configured_raw_manifests[arm]
        timing_artifact_paths[arm] = (
            load_extracted_timing_artifacts(raw_manifest, resolved_arm_paths[arm])
            if raw_manifest is not None
            else None
        )
        if raw_manifest is not None:
            print(
                f"[info] {arm}: using {len(timing_artifact_paths[arm] or [])} "
                f"stage-_01 clock timing artifact(s); raw INSV telemetry/PTS will not be read",
                flush=True,
            )
    calibration_id = uuid.uuid4().hex
    if not args.dry_run:
        # Invalidate a previous accepted fixed-path report before the pairing
        # audit starts. An interrupted audit must never leave stale accepted
        # timelines looking current.
        atomic_json(
            output / "calibration.json",
            {
                "schema": SCHEMA,
                "calibration_id": calibration_id,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "session_dir": str(session),
                "output_dir": str(output),
                "status": "building",
                "status_reason": "tracker_assignment_audit_or_calibration_in_progress",
                "arms": {},
            },
        )

    tracker_assignment = {arm: arm for arm in arms}
    assignment_error: str | None = None
    if arms == ("left", "right"):
        missing_candidates = [
            str(path) for path in configured_htc.values() if not path.is_file()
        ]
        if missing_candidates:
            assignment_error = (
                "2x2 tracker assignment audit requires both HTC candidates; missing: "
                + ", ".join(missing_candidates)
            )
            assignment_audit = {
                "method": "audited_2x2_gyro_norm_bijection",
                "status": "insufficient_data",
                "status_reason": assignment_error,
                "camera_to_tracker": None,
            }
        else:
            try:
                selected_assignment, assignment_audit = audit_tracker_assignment(
                    resolved_arm_paths,
                    configured_htc,
                    timestamp_log,
                    output,
                    args,
                    timing_artifact_paths,
                    camera_role_binding,
                )
                if selected_assignment is None:
                    assignment_error = str(assignment_audit["status_reason"])
                else:
                    tracker_assignment = selected_assignment
            except (RuntimeError, ValueError, OSError) as exc:
                assignment_error = f"tracker assignment audit failed: {exc}"
                assignment_audit = {
                    "method": "audited_2x2_gyro_norm_bijection",
                    "status": "insufficient_data",
                    "status_reason": assignment_error,
                    "camera_to_tracker": None,
                }
    else:
        assignment_audit = {
            "method": "single_arm_explicit_identity",
            "status": "accepted",
            "status_reason": "single_arm_mode_has_no_bijection_to_audit",
            "camera_to_tracker": tracker_assignment,
        }

    selected_htc = {
        camera: configured_htc[tracker_assignment[camera]] for camera in arms
    }
    input_files = {
        "timestamp_log": file_identity(timestamp_log),
        "camera_transfer_manifest": (
            file_identity(transfer_manifest) if transfer_manifest.is_file() else None
        ),
        "camera_role_correction": (
            file_identity(role_correction) if role_correction.is_file() else None
        ),
        "htc_candidates": {
            tracker: (
                file_identity(path)
                if path.is_file()
                else {"path": str(path), "missing": True}
            )
            for tracker, path in configured_htc.items()
        },
        "arms": {
            arm: {
                "htc": (
                    file_identity(selected_htc[arm])
                    if selected_htc[arm].is_file()
                    else {"path": str(selected_htc[arm]), "missing": True}
                ),
                "insv": [file_identity(path) for path in resolved_arm_paths[arm] if path.is_file()],
                "raw_manifest": (
                    file_identity(configured_raw_manifests[arm])
                    if configured_raw_manifests[arm] is not None
                    else None
                ),
                "timing_artifacts": (
                    [file_identity(path) for path in timing_artifact_paths[arm] or []]
                    if timing_artifact_paths[arm] is not None
                    else None
                ),
            }
            for arm in arms
        },
    }
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "calibration_id": calibration_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(session),
        "output_dir": str(output),
        "dry_run": args.dry_run,
        "non_destructive": True,
        "method": "windowed gyro-norm cross-correlation with robust per-chapter drift/intercept fit",
        "clock_contract": {
            "schema": "instaumi-strict-clock-domain-v1",
            "domain": CLOCK_DOMAIN,
            "linux_clock": "CLOCK_MONOTONIC_RAW",
            "htc_time_column": HTC_RAW_TIME_COLUMN,
            "frame_time_columns": [
                "calibrated_host_monotonic_raw_sec",
                "calibrated_host_monotonic_raw_ns",
            ],
            "timestamp_log_event_column": "monotonic_raw_time_ns",
            "alignment_policy": "single_domain_only_fail_closed",
            "realtime_unix_policy": "metadata_only_never_alignment",
        },
        "tracker_assignment": assignment_audit,
        "camera_role_binding": camera_role_binding,
        "native_time_axis": (
            "Each chapter uses telemetry Metadata.first_frame_timestamp (microseconds) relative to chapter 0; "
            "normalized_imu timestamp_ms and video PTS remain local to that chapter."
        ),
        "configuration": {
            "window_sec": args.window_sec,
            "stride_sec": args.stride_sec,
            "search_range_sec": args.search_range_sec,
            "resample_hz": args.resample_hz,
            "smooth_sec": args.smooth_sec,
            "min_correlation": args.min_correlation,
            "min_peak_margin": args.min_peak_margin,
            "min_motion_std": args.min_motion_std,
            "min_assignment_score_margin": args.min_assignment_score_margin,
            "max_short_terminal_fallback_sec": args.max_short_terminal_fallback_sec,
            "max_htc_gap_ms": args.max_htc_gap_ms,
            "max_residual_p95_ms": args.max_residual_p95_ms,
            "max_abs_drift_ppm": args.max_abs_drift_ppm,
            "ffprobe_bin": args.ffprobe_bin,
            "arm_workers": min(args.arm_workers, len(arms)),
        },
        "input_files": input_files,
        "arms": {},
        "warnings": [
            "Do not overwrite legacy frame_timestamps.csv; apply this calibrated timeline downstream.",
            "All frame selection/interpolation must use calibrated_host_monotonic_raw_ns and timestamp-log monotonic_raw_time_ns. Unix/realtime columns are metadata-only projections.",
            "A native chapter overlap is preserved and reported. Drop only rows explicitly marked recommended_keep=0, together with the same frames in every derived view.",
            "source_pts_sec comes from ffprobe video-packet PTS sorted into presentation order and is checked for count and strict monotonicity; it still cannot prove sensor exposure uniqueness when firmware duplicates an encoded frame.",
        ],
    }
    if not args.dry_run:
        # Invalidate any previously accepted fixed-path report before touching
        # its timeline files. A power loss can then leave only status=building,
        # never an old accepted report pointing at partially replaced CSVs.
        atomic_json(
            output / "calibration.json",
            {
                "schema": SCHEMA,
                "calibration_id": calibration_id,
                "created_utc": report["created_utc"],
                "session_dir": str(session),
                "output_dir": str(output),
                "status": "building",
                "status_reason": "calibration_in_progress_or_interrupted",
                "input_files": input_files,
                "arms": {},
            },
        )
    insufficient_arms: list[str] = []
    runnable_arms: list[str] = []
    for arm in arms:
        if assignment_error is not None:
            report["arms"][arm] = {
                "arm": arm,
                "camera_arm": arm,
                "status": "insufficient_data",
                "error": assignment_error,
            }
            insufficient_arms.append(arm)
            continue
        htc_path = selected_htc[arm]
        if not htc_path.is_file():
            report["arms"][arm] = {
                "arm": arm,
                "status": "insufficient_data",
                "error": f"HTC CSV does not exist: {htc_path}",
            }
            insufficient_arms.append(arm)
            continue
        runnable_arms.append(arm)

    def calibration_arguments(arm: str) -> tuple[Any, ...]:
        return (
            arm,
            tracker_assignment[arm],
            camera_role_binding[arm]["capture_role"],
            resolved_arm_paths[arm],
            selected_htc[arm],
            timestamp_log,
            output,
            args,
            calibration_id,
            timing_artifact_paths[arm],
        )

    effective_arm_workers = min(args.arm_workers, len(runnable_arms))
    if effective_arm_workers > 1:
        print(
            f"[parallel] calibrating arms={','.join(runnable_arms)} "
            f"with processes={effective_arm_workers}",
            flush=True,
        )
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=effective_arm_workers
        ) as pool:
            futures = {
                arm: pool.submit(calibrate_arm, *calibration_arguments(arm))
                for arm in runnable_arms
            }
            for arm in runnable_arms:
                try:
                    report["arms"][arm] = futures[arm].result()
                except RuntimeError as exc:
                    print(f"[error] {arm}: {exc}", file=sys.stderr, flush=True)
                    report["arms"][arm] = {
                        "arm": arm,
                        "status": "insufficient_data",
                        "error": str(exc),
                    }
                    insufficient_arms.append(arm)
    else:
        for arm in runnable_arms:
            try:
                report["arms"][arm] = calibrate_arm(
                    *calibration_arguments(arm)
                )
            except RuntimeError as exc:
                print(f"[error] {arm}: {exc}", file=sys.stderr, flush=True)
                report["arms"][arm] = {
                    "arm": arm,
                    "status": "insufficient_data",
                    "error": str(exc),
                }
                insufficient_arms.append(arm)

    arm_quality_ok = not insufficient_arms and bool(report["arms"]) and all(
        arm_report.get("quality", {}).get("calibration_accepted", False)
        for arm_report in report["arms"].values()
    )
    inputs_unchanged = True
    if not args.dry_run:
        try:
            current_inputs = {
                "timestamp_log": file_identity(timestamp_log),
                "camera_transfer_manifest": (
                    file_identity(transfer_manifest) if transfer_manifest.is_file() else None
                ),
                "camera_role_correction": (
                    file_identity(role_correction) if role_correction.is_file() else None
                ),
                "htc_candidates": {
                    tracker: file_identity(path)
                    for tracker, path in configured_htc.items()
                },
                "arms": {
                    arm: {
                        "htc": file_identity(selected_htc[arm]),
                        "insv": [file_identity(path) for path in resolved_arm_paths[arm]],
                        "raw_manifest": (
                            file_identity(configured_raw_manifests[arm])
                            if configured_raw_manifests[arm] is not None
                            else None
                        ),
                        "timing_artifacts": (
                            [file_identity(path) for path in timing_artifact_paths[arm] or []]
                            if timing_artifact_paths[arm] is not None
                            else None
                        ),
                    }
                    for arm in arms
                },
            }
            inputs_unchanged = current_inputs == input_files
        except FileNotFoundError:
            inputs_unchanged = False
    report["inputs_unchanged_during_calibration"] = inputs_unchanged
    if insufficient_arms:
        report["status"] = "insufficient_data"
        report["status_reason"] = (
            "tracker_assignment_ambiguous_or_insufficient"
            if assignment_error is not None
            else "missing_input_or_too_few_unambiguous_motion_windows"
        )
        report["insufficient_arms"] = insufficient_arms
    elif args.dry_run:
        report["status"] = "rejected"
        report["status_reason"] = "dry_run_does_not_publish_an_accepted_artifact"
    elif not arm_quality_ok:
        report["status"] = "rejected"
        report["status_reason"] = "one_or_more_arm_or_chapter_quality_checks_failed"
    elif not inputs_unchanged:
        report["status"] = "rejected"
        report["status_reason"] = "input_files_changed_during_calibration"
    else:
        report["status"] = "accepted"
        report["status_reason"] = "window_correlation_and_residual_quality_checks_passed"

    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.dry_run:
        print(payload)
    else:
        atomic_json(output / "calibration.json", report)
        print(f"[done] calibration report: {output / 'calibration.json'}")
    return 0 if args.dry_run or report["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
