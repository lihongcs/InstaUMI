#!/usr/bin/env python3
"""Combine two single-hand InstaUMI episode inputs into a bimanual LeRobot v2.1 dataset."""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import csv
import fcntl
import hashlib
import importlib.metadata
import itertools
import json
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
DATAPROCESS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(DATAPROCESS_ROOT))
DEFAULT_LEROBOT_ENV = SCRIPT_DIR

from capture_schema import normalize_tracker_role  # noqa: E402
from active_frame_ranges import (  # noqa: E402
    build_active_frame_ranges as build_shared_active_frame_ranges,
    load_calibrated_frame_timeline as load_shared_calibrated_frame_timeline,
)
from direct_openpi_images import (  # noqa: E402
    DEFAULT_OPENPI_PANORAMA_HEIGHT,
    DEFAULT_OPENPI_PANORAMA_WIDTH,
    DirectOpenPIFrame,
    DirectOpenPIImageDecoder,
    DirectOpenPIImagePlan,
    DirectRawOpenPIImageDecoder,
    DirectRawOpenPIImagePlan,
    SharedMediaSDKGPUAdmission,
    full_file_identity,
    probe_video,
    source_identity,
    verify_full_file_identity,
)
from production_semantics import (  # noqa: E402
    build_nonproduction_semantics,
    build_production_semantics,
)


DEFAULT_MEDIASDK_EXPORTER = (
    DATAPROCESS_ROOT
    / "_01_raw_extrect"
    / "insta360_pano_export"
    / "build"
    / "insv_to_png_sequence"
)
DEFAULT_MEDIASDK_PANORAMA_EXPORTER = (
    DATAPROCESS_ROOT
    / "_01_raw_extrect"
    / "insta360_pano_export"
    / "build"
    / "insv_to_pano"
)
CONTINUOUS_PANORAMA_SCHEMA = "instaumi.mediasdk_continuous_h265"
CONTINUOUS_GRIPPER_QUALITY_SCHEMA = "instaumi.continuous_gripper_quality.v1"


def default_data_root() -> Path:
    env = os.environ.get("DATA_ROOT")
    if env:
        return Path(env).expanduser()
    workspace_root = SCRIPT_DIR.parents[2]
    for name in ("data", "Data"):
        candidate = workspace_root / name
        if candidate.exists():
            return candidate
    return workspace_root / "data"


def default_tmp_root() -> Path:
    if env := os.environ.get("TMP_ROOT"):
        return Path(env).expanduser()
    if env := os.environ.get("INPUT_DIR"):
        return Path(env).expanduser() / "tmp"
    return default_data_root() / "tmp"


DEFAULT_TMP_ROOT = default_tmp_root()

_PIL_FONT = None
_PIL_FONT_SMALL = None

from bimanual_convert_helpers import (  # noqa: E402
    get_cv2,
    import_lerobot,
    load_gripper_widths,
    load_tum_trajectory,
    make_state,
    natural_key,
    pose7_to_pose6,
)
from htc_to_flexiv_transform import legacy_csv_xyzw_to_firmware_xyzw  # noqa: E402


CAMERA_KEYS = (
    "observation.images.right_wrist",
    "observation.images.right_left_view",
    "observation.images.right_right_view",
    "observation.images.left_wrist",
    "observation.images.left_left_view",
    "observation.images.left_right_view",
)

STATE_NAMES = [
    "right_x",
    "right_y",
    "right_z",
    "right_rx",
    "right_ry",
    "right_rz",
    "right_gripper",
    "right_none1",
    "right_none2",
    "right_none3",
    "left_x",
    "left_y",
    "left_z",
    "left_rx",
    "left_ry",
    "left_rz",
    "left_gripper",
    "left_none1",
    "left_none2",
    "left_none3",
]


FEATURES = {
    "start_pose": {
        "dtype": "float32",
        "shape": (12,),
        "names": [
            "right_x",
            "right_y",
            "right_z",
            "right_rx",
            "right_ry",
            "right_rz",
            "left_x",
            "left_y",
            "left_z",
            "left_rx",
            "left_ry",
            "left_rz",
        ],
    },
    "end_pose": {
        "dtype": "float32",
        "shape": (12,),
        "names": [
            "right_x",
            "right_y",
            "right_z",
            "right_rx",
            "right_ry",
            "right_rz",
            "left_x",
            "left_y",
            "left_z",
            "left_rx",
            "left_ry",
            "left_rz",
        ],
    },
    "action": {"dtype": "float32", "shape": (20,), "names": STATE_NAMES},
    "observation.state": {"dtype": "float32", "shape": (20,), "names": STATE_NAMES},
    # LeRobot's required ``timestamp`` remains the regular episode clock
    # (frame_index / dataset fps).  These non-policy features retain the two
    # actual source clocks and alignment quality without changing that API.
    "source.camera_unix_time_ms": {
        "dtype": "float64",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.camera_frame_index": {
        "dtype": "int64",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.htc_nearest_gap_ms": {
        "dtype": "float32",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.htc_bracket_gap_ms": {
        "dtype": "float32",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.htc_valid": {
        "dtype": "uint8",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.segment_index": {
        "dtype": "int64",
        "shape": (1,),
        "names": ["timestamp_log_segment"],
    },
    "source.frame_reused": {
        "dtype": "uint8",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.state_unix_time_ms": {
        "dtype": "float64",
        "shape": (2,),
        "names": ["right", "left"],
    },
    "source.gripper_interpolation_alpha": {
        "dtype": "float32",
        "shape": (2,),
        "names": ["right", "left"],
    },
}

for key in CAMERA_KEYS:
    FEATURES[key] = {
        "dtype": "image",
        "shape": (224, 224, 3),
        "names": ["height", "width", "channels"],
    }


def output_features(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Return the committed LeRobot schema for the selected storage contract.

    Panorama-v2 keeps the calibrated numeric rows in Parquet and stores the
    camera signal only as the validated continuous panorama streams.  In that
    mode the six legacy image columns must never enter the dataset schema; this
    prevents LeRobot from asking the converter to decode, PNG-encode, write,
    and subsequently delete those redundant views.
    """

    if bool(getattr(args, "panorama_numeric_only", False)):
        return {
            key: feature
            for key, feature in FEATURES.items()
            if key not in CAMERA_KEYS
        }
    return dict(FEATURES)


@dataclass(frozen=True)
class HandInput:
    name: str
    episode_dir: Path
    trajectory_path: Path
    gripper_path: Path
    camera_paths: tuple[Path, Path, Path] | None
    pose_sensor: str
    timestamps_path: Path | None
    raw_manifest_path: Path | None
    tracker_path: Path | None = None
    tracker_role: str | None = None
    timestamps_source: str = "unknown"


@dataclass(frozen=True)
class FrameTimeline:
    path: Path
    frame_idx: np.ndarray
    unix_ms: np.ndarray
    valid: np.ndarray
    unix_column: str
    validity_columns: tuple[str, ...]
    calibration_id: str | None = None
    clock_domain: str = "unix_realtime"

    @property
    def valid_indices(self) -> np.ndarray:
        return np.flatnonzero(self.valid)

    def report(self) -> dict[str, Any]:
        valid_indices = self.valid_indices
        values = self.unix_ms[valid_indices]
        gaps = np.diff(values) if len(values) > 1 else np.asarray([], dtype=np.float64)
        return {
            "path": str(self.path),
            "row_count": int(len(self.frame_idx)),
            "timeline_valid_count": int(np.count_nonzero(self.valid)),
            "timeline_invalid_count": int(np.count_nonzero(~self.valid)),
            "time_column": self.unix_column,
            "clock_domain": self.clock_domain,
            "validity_columns": list(self.validity_columns),
            "calibration_id": self.calibration_id,
            "first_valid_time_ms": None if not len(values) else float(values[0]),
            "last_valid_time_ms": None if not len(values) else float(values[-1]),
            "valid_step_ms": distribution_report(gaps),
        }


@dataclass(frozen=True)
class PoseQuality:
    valid: np.ndarray
    nearest_gap_ms: np.ndarray
    bracket_gap_ms: np.ndarray
    report: dict[str, Any]
    # ``valid`` is the normal quality flag exposed in the dataset.  A sample
    # may fail that soft threshold (for example, a 40 ms HTC bracket) without
    # changing episode semantics.  ``hard_valid`` is deliberately separate:
    # any hard-invalid sample in an active timestamp_log segment rejects the
    # whole segment, so an action can never bridge a true tracking hole.
    hard_valid: np.ndarray | None = None


@dataclass(frozen=True)
class HandStates:
    poses: list[np.ndarray]
    grippers: list[float | None]
    video_frames: int
    pose_quality: PoseQuality
    temporal_pose_source: "TemporalPoseSource | None" = None
    pose_frame_count: int | None = None

    @property
    def pose_count(self) -> int:
        return len(self.poses) if self.pose_frame_count is None else self.pose_frame_count


@dataclass(frozen=True)
class TemporalPoseSource:
    timestamps_sec: np.ndarray
    positions: np.ndarray
    quaternions_xyzw: np.ndarray
    pose_sensor: str
    time_source: str


@dataclass(frozen=True)
class GridHandSamples:
    poses: list[np.ndarray]
    grippers: np.ndarray
    htc_nearest_gap_ms: np.ndarray
    htc_bracket_gap_ms: np.ndarray
    htc_valid: np.ndarray
    gripper_before_frame_idx: np.ndarray
    gripper_after_frame_idx: np.ndarray
    gripper_interpolation_alpha: np.ndarray
    gripper_zeroed_below_threshold: int = 0
    gripper_binarized_values: int = 0


@dataclass(frozen=True)
class FrameRun:
    segment_index: int
    run_index: int
    pairs: list[tuple[int, int]]
    grid_unix_time_ms: tuple[float, ...] = ()


@dataclass(frozen=True)
class FramePairResult:
    pairs: list[tuple[int, int]]
    report: dict[str, Any]
    grid_unix_time_ms: tuple[float, ...] = ()


def output_dataset_fps(args: argparse.Namespace) -> int:
    source_fps = int(getattr(args, "fps", 60))
    steps = int(getattr(args, "down_sample_steps", 2))
    if source_fps <= 0:
        raise ValueError("--fps must be positive")
    if steps <= 0:
        raise ValueError("--down-sample-steps must be positive")
    if source_fps % steps != 0:
        raise ValueError(
            f"--fps={source_fps} must be divisible by --down-sample-steps={steps}"
        )
    return source_fps // steps


def downsample_frame_run(run: FrameRun, steps: int) -> FrameRun:
    """Uniformly decimate a fully validated 60 Hz run without changing alignment."""
    if steps <= 0:
        raise ValueError("down-sample steps must be positive")
    positions = range(0, len(run.pairs), steps)
    pairs = [run.pairs[index] for index in positions]
    if len(pairs) < 2:
        raise RuntimeError(
            f"segment={run.segment_index} run={run.run_index} has fewer than two "
            f"states after down-sampling by {steps}"
        )
    # Full-rate nearest-neighbour pairing may legitimately reuse one peer
    # frame at a segment boundary.  Phase-0 decimation normally removes that
    # adjacent reuse, but that is not a mathematical guarantee for arbitrary
    # calibrated timelines (for example, three anchors can select one peer).
    # Training observations must never retain a repeated or reversed source
    # frame, so fail before panorama export rather than relying only on the
    # final Zarr audit.
    for pair_position, (before, after) in enumerate(zip(pairs, pairs[1:])):
        for arm, axis in (("right", 0), ("left", 1)):
            if int(after[axis]) <= int(before[axis]):
                raise RuntimeError(
                    f"segment={run.segment_index} run={run.run_index} "
                    f"down-sampled {arm} source frame is not strictly increasing: "
                    f"pair_positions={pair_position}->{pair_position + 1} "
                    f"frame_indices={before[axis]}->{after[axis]}; "
                    "repeated/reversed camera frames are forbidden"
                )
    grid = (
        tuple(run.grid_unix_time_ms[index] for index in range(0, len(run.grid_unix_time_ms), steps))
        if run.grid_unix_time_ms
        else ()
    )
    if grid and len(grid) != len(pairs):
        raise RuntimeError("down-sampled pair/grid lengths differ")
    return FrameRun(run.segment_index, run.run_index, pairs, grid)


@dataclass(frozen=True)
class SegmentRange:
    segment_index: int
    start_unix_time_ms: float
    end_unix_time_ms: float
    right_start_frame: int
    right_end_frame: int
    left_start_frame: int
    left_end_frame: int


@dataclass(frozen=True)
class EpisodeShardSpec:
    """One contiguous global episode range with a fixed source dependency set.

    Shards retain global episode and frame offsets so their Parquet files can
    be merged without rewriting embedded image bytes.  A camera chapter split
    is never allowed to split a logical episode; an episode touching multiple
    chapters becomes a small bridge shard instead.
    """

    shard_index: int
    episode_start: int
    episode_end: int
    right_chapter_indices: tuple[int, ...]
    left_chapter_indices: tuple[int, ...]
    frame_start: int
    frame_end: int
    export_workers_per_arm: int
    batch_frames_per_arm: int
    resize_workers_per_arm: int
    assignment_hash: str

    @property
    def episode_indices(self) -> tuple[int, ...]:
        return tuple(range(self.episode_start, self.episode_end))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "instaumi-bimanual-episode-shard-v1",
            "shard_index": self.shard_index,
            "episode_start": self.episode_start,
            "episode_end": self.episode_end,
            "right_chapter_indices": list(self.right_chapter_indices),
            "left_chapter_indices": list(self.left_chapter_indices),
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "export_workers_per_arm": self.export_workers_per_arm,
            "batch_frames_per_arm": self.batch_frames_per_arm,
            "resize_workers_per_arm": self.resize_workers_per_arm,
            "assignment_hash": self.assignment_hash,
        }


def _chapter_footprint(indices: Iterator[int], chapters: tuple[Any, ...]) -> tuple[int, ...]:
    starts = tuple(int(chapter.global_start) for chapter in chapters)
    if not starts:
        raise ValueError("raw image plan contains no chapters")
    footprint: list[int] = []
    for raw_index in indices:
        frame_index = int(raw_index)
        chapter_index = bisect.bisect_right(starts, frame_index) - 1
        if chapter_index < 0 or frame_index >= int(chapters[chapter_index].global_end):
            raise ValueError(f"source frame {frame_index} is outside the chapter plan")
        if not footprint or footprint[-1] != chapter_index:
            footprint.append(chapter_index)
    return tuple(footprint)


def build_episode_shard_specs(
    planned_runs: list[tuple[SegmentRange, FrameRun]],
    direct_plans: dict[str, DirectRawOpenPIImagePlan],
    total_export_workers: int,
    total_batch_arm_frames: int,
    total_resize_workers: int,
    max_shard_workers: int = 0,
) -> list[EpisodeShardSpec]:
    """Partition episodes by consecutive chapter dependencies and share budgets.

    ``total_*`` values are machine-wide budgets across both arms and all live
    shard workers.  The returned per-arm values therefore never multiply the
    pre-existing single-stream resource peak when chapter parallelism is on.
    """

    if set(direct_plans) != {"right", "left"}:
        raise ValueError("direct plans must contain exactly right and left")
    if min(total_export_workers, total_batch_arm_frames, total_resize_workers) <= 0:
        raise ValueError("episode shard resource budgets must be positive")
    if max_shard_workers < 0:
        raise ValueError("max_shard_workers must be non-negative")
    if not planned_runs:
        return []

    dependencies: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for _segment, run in planned_runs:
        if len(run.pairs) < 2:
            raise ValueError("a planned episode must contain at least two pairs")
        dependencies.append(
            (
                _chapter_footprint(
                    (pair[0] for pair in run.pairs),
                    direct_plans["right"].insv_chapters,
                ),
                _chapter_footprint(
                    (pair[1] for pair in run.pairs),
                    direct_plans["left"].insv_chapters,
                ),
            )
        )

    ranges: list[tuple[int, int, tuple[int, ...], tuple[int, ...]]] = []
    range_start = 0
    current = dependencies[0]
    for episode_index, dependency in enumerate(dependencies[1:], start=1):
        if dependency != current:
            ranges.append((range_start, episode_index, current[0], current[1]))
            range_start = episode_index
            current = dependency
    ranges.append((range_start, len(planned_runs), current[0], current[1]))

    live_workers = min(
        len(ranges),
        max_shard_workers if max_shard_workers else len(ranges),
        max(1, total_export_workers // 2),
        max(1, total_resize_workers // 2),
        max(1, total_batch_arm_frames // (2 * 64)),
    )
    export_per_arm = max(1, total_export_workers // (2 * live_workers))
    batch_per_arm = max(1, total_batch_arm_frames // (2 * live_workers))
    resize_per_arm = max(1, total_resize_workers // (2 * live_workers))

    frame_prefix = [0]
    for _segment, run in planned_runs:
        frame_prefix.append(frame_prefix[-1] + len(run.pairs) - 1)

    specs: list[EpisodeShardSpec] = []
    for shard_index, (episode_start, episode_end, right_indices, left_indices) in enumerate(ranges):
        assignment = {
            "episode_start": episode_start,
            "episode_end": episode_end,
            "right_chapter_indices": list(right_indices),
            "left_chapter_indices": list(left_indices),
            "frame_start": frame_prefix[episode_start],
            "frame_end": frame_prefix[episode_end],
        }
        assignment_hash = hashlib.sha256(
            json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        specs.append(
            EpisodeShardSpec(
                shard_index=shard_index,
                episode_start=episode_start,
                episode_end=episode_end,
                right_chapter_indices=right_indices,
                left_chapter_indices=left_indices,
                frame_start=frame_prefix[episode_start],
                frame_end=frame_prefix[episode_end],
                export_workers_per_arm=export_per_arm,
                batch_frames_per_arm=batch_per_arm,
                resize_workers_per_arm=resize_per_arm,
                assignment_hash=assignment_hash,
            )
        )
    return specs


def resolve_gpu_admission_settings(
    args: argparse.Namespace,
    potential_workers: int,
) -> tuple[int, int, int, int, int]:
    """Resolve maximum/startup/floor/progress/retry GPU admission knobs."""

    if potential_workers < 1:
        raise ValueError("potential_workers must be positive")
    configured_maximum = int(args.direct_image_gpu_max_workers)
    maximum = min(
        potential_workers,
        configured_maximum if configured_maximum > 0 else potential_workers,
    )
    configured_startup = int(args.direct_image_gpu_startup_workers)
    startup = min(maximum, configured_startup if configured_startup > 0 else min(8, maximum))
    configured_minimum = int(args.direct_image_gpu_min_workers)
    minimum = min(maximum, configured_minimum if configured_minimum > 0 else min(4, maximum))
    return (
        maximum,
        startup,
        minimum,
        int(args.direct_image_gpu_startup_progress),
        int(args.direct_image_gpu_oom_retries),
    )


def distribution_report(values: np.ndarray) -> dict[str, float | int | None]:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return {"count": 0, "median": None, "p95": None, "p99": None, "max": None}
    return {
        "count": int(len(finite)),
        "median": float(np.median(finite)),
        "p95": float(np.percentile(finite, 95)),
        "p99": float(np.percentile(finite, 99)),
        "max": float(np.max(finite)),
    }


def parse_csv_bool(value: object, *, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean CSV value: {value!r}")


def resolve_path_from_manifest(value: object, manifest_path: Path) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def accepted_manifest_calibrated_path(manifest: dict[str, Any], manifest_path: Path) -> Path | None:
    """Return only a manifest-declared, explicitly accepted calibrated clock."""

    timing = manifest.get("time_calibration") or manifest.get("clock_calibration") or {}
    status = str(
        timing.get("status")
        or manifest.get("time_calibration_status")
        or manifest.get("calibration_status")
        or ""
    ).strip().lower()
    if status != "accepted":
        return None
    value = (
        timing.get("calibrated_timestamps_csv")
        or timing.get("calibrated_frame_timestamps_csv")
        or manifest.get("calibrated_timestamps_csv")
        or manifest.get("calibrated_frame_timestamps_csv")
    )
    return resolve_path_from_manifest(value, manifest_path)


def select_frame_timestamps_path(
    manifest: dict[str, Any],
    manifest_path: Path,
    explicit_path: Path | None,
    require_calibrated: bool,
) -> tuple[Path, str]:
    if explicit_path is not None:
        selected = explicit_path.expanduser().resolve()
        source = "explicit_calibrated"
    else:
        selected = accepted_manifest_calibrated_path(manifest, manifest_path)
        source = "manifest_accepted_calibrated"
        if selected is None:
            if require_calibrated:
                raise RuntimeError(
                    f"{manifest_path} has no accepted calibrated frame timeline. "
                    "Pass an explicit --*-frame-timestamps path, or deliberately allow the legacy clock."
                )
            selected = resolve_path_from_manifest(manifest.get("timestamps_csv"), manifest_path)
            source = "legacy_raw_manifest"
    if selected is None or not selected.is_file():
        raise FileNotFoundError(f"Selected frame timestamp CSV does not exist: {selected}")
    return selected, source


def calibration_report_timeline_path(report: dict[str, Any], arm: str, report_path: Path) -> Path | None:
    arm_report = (report.get("arms") or {}).get(arm) or {}
    timeline = arm_report.get("frame_timeline") or {}
    value = (
        timeline.get("path")
        or arm_report.get("calibrated_frame_timestamps_csv")
        or arm_report.get("calibrated_timestamps_csv")
    )
    if value is None:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = report_path.parent / path
    return path.resolve()


def discover_session_insv(session_dir: Path, arm: str) -> list[Path]:
    paths: set[Path] = set()
    legacy_camera = "camera_1" if arm == "right" else "camera_2"
    for directory in (
        session_dir / "insta" / f"insta_{arm}",
        session_dir / "insta" / legacy_camera,
        session_dir / f"insta_{arm}",
        session_dir / arm,
    ):
        if directory.is_dir():
            paths.update(path.resolve() for path in directory.glob("*.insv"))
    canonical = session_dir / f"{arm}.insv"
    if canonical.is_file():
        paths.add(canonical.resolve())
    paths.update(path.resolve() for path in session_dir.glob(f"{arm}_*.insv") if path.is_file())
    return sorted(paths)


def sampled_content_identity(path: Path, *, sample_size: int = 256 * 1024) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    info = resolved.stat()
    offsets = sorted(
        {
            0,
            max(0, info.st_size // 2 - sample_size // 2),
            max(0, info.st_size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(handle.read(min(sample_size, max(0, info.st_size - offset))))
    return {
        "sample_size": int(sample_size),
        "sample_sha256": digest.hexdigest(),
    }


def verify_file_identity(
    identity: object,
    report_path: Path,
    label: str,
    expected_path: Path | None = None,
) -> Path:
    if not isinstance(identity, dict):
        raise RuntimeError(f"{label} file identity is missing or is not an object")
    missing = {"path", "size_bytes", "mtime_ns"} - set(identity)
    if missing:
        raise RuntimeError(f"{label} file identity is missing fields: {', '.join(sorted(missing))}")
    raw_path = Path(str(identity["path"])).expanduser()
    path = raw_path if raw_path.is_absolute() else report_path.parent / raw_path
    path = path.resolve()
    if expected_path is not None and path != expected_path.expanduser().resolve():
        raise RuntimeError(
            f"{label} file identity path mismatch: identity={path}, expected={expected_path.expanduser().resolve()}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"{label} identity target does not exist: {path}")
    try:
        expected_size = int(identity["size_bytes"])
        expected_mtime = int(identity["mtime_ns"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} file identity size_bytes/mtime_ns must be integers") from exc
    stat = path.stat()
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
        raise RuntimeError(
            f"{label} file identity mismatch for {path}: "
            f"size={stat.st_size}/{expected_size}, mtime_ns={stat.st_mtime_ns}/{expected_mtime}"
        )
    if "sample_sha256" in identity:
        sample_size = int(identity.get("sample_size", 256 * 1024))
        current = sampled_content_identity(path, sample_size=sample_size)
        if current["sample_sha256"] != identity.get("sample_sha256"):
            raise RuntimeError(f"{label} sampled content hash mismatch for {path}")
    return path


def require_same_file_identity(first: object, second: object, label: str) -> None:
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise RuntimeError(f"{label} mirrored file identity is missing")
    keys = ("path", "size_bytes", "mtime_ns")
    if any(str(first.get(key)) != str(second.get(key)) for key in keys):
        raise RuntimeError(f"{label} mirrored file identities do not match")
    if "sample_sha256" in first or "sample_sha256" in second:
        for key in ("sample_size", "sample_sha256"):
            if str(first.get(key)) != str(second.get(key)):
                raise RuntimeError(f"{label} mirrored content identities do not match")


def require_report_newer_than(report_path: Path, sources: list[tuple[str, Path]]) -> None:
    report_mtime_ns = report_path.stat().st_mtime_ns
    stale = [
        f"{label}={path} (mtime_ns={path.stat().st_mtime_ns})"
        for label, path in sources
        if report_mtime_ns <= path.stat().st_mtime_ns
    ]
    if stale:
        raise RuntimeError(
            f"Sync calibration report is stale (report mtime_ns={report_mtime_ns}); "
            "it must be newer than every calibrated input: " + "; ".join(stale)
        )


def validate_accepted_calibration_report(
    report_path: Path | None,
    right_timestamps: Path,
    left_timestamps: Path,
    *,
    session_dir: Path | None,
    right_htc_path: Path | None,
    left_htc_path: Path | None,
    timestamp_log_path: Path | None = None,
) -> dict[str, Any]:
    if report_path is None:
        raise RuntimeError(
            "Calibrated timestamps are required, but --sync-calibration-report was not provided. "
            "An explicit CSV path alone does not bypass calibration quality checks."
        )
    resolved_report = report_path.expanduser().resolve()
    if not resolved_report.is_file():
        raise FileNotFoundError(f"Sync calibration report does not exist: {resolved_report}")
    report = json.loads(resolved_report.read_text(encoding="utf-8"))
    if str(report.get("status", "")).strip().lower() != "accepted" or bool(report.get("dry_run", False)):
        raise RuntimeError(f"Sync calibration report is not accepted: {resolved_report}")
    clock_contract = report.get("clock_contract") or {}
    if (
        report.get("schema") != "instaumi-no-slam-clock-calibration-v2-monotonic-raw"
        or clock_contract.get("domain") != "host_clock_monotonic_raw"
        or clock_contract.get("alignment_policy") != "single_domain_only_fail_closed"
    ):
        raise RuntimeError(
            "Accepted sync calibration does not satisfy the strict MONOTONIC_RAW contract: "
            f"{resolved_report}"
        )
    calibration_id = report.get("calibration_id")
    if not isinstance(calibration_id, str) or not calibration_id.strip():
        raise RuntimeError(f"Accepted sync calibration report has no non-empty calibration_id: {resolved_report}")
    calibration_id = calibration_id.strip()
    if session_dir is None:
        raise RuntimeError("Accepted sync calibration validation requires the current --session-dir")
    current_session = session_dir.expanduser().resolve()
    reported_session_raw = report.get("session_dir")
    if not reported_session_raw:
        raise RuntimeError(f"Sync calibration report has no session_dir: {resolved_report}")
    reported_session = Path(str(reported_session_raw)).expanduser().resolve()
    if reported_session != current_session:
        raise RuntimeError(
            f"Sync calibration report belongs to a different session: "
            f"report={reported_session}, current={current_session}"
        )

    timestamp_log = (
        timestamp_log_path.expanduser().resolve()
        if timestamp_log_path is not None
        else (current_session / "timestamp_log.jsonl").resolve()
    )
    if not timestamp_log.is_file():
        raise FileNotFoundError(f"Current session timestamp log does not exist: {timestamp_log}")
    input_files = report.get("input_files")
    if not isinstance(input_files, dict):
        raise RuntimeError("Accepted sync calibration report has no input_files identity block")
    canonical_timestamp_identity = input_files.get("timestamp_log")
    verify_file_identity(
        canonical_timestamp_identity,
        resolved_report,
        "input_files.timestamp_log",
        timestamp_log,
    )
    canonical_arms = input_files.get("arms")
    if not isinstance(canonical_arms, dict):
        raise RuntimeError("Accepted sync calibration report has no input_files.arms identity block")

    freshness_sources: list[tuple[str, Path]] = [("timestamp_log", timestamp_log)]
    for arm, selected, actual_htc in (
        ("right", right_timestamps, right_htc_path),
        ("left", left_timestamps, left_htc_path),
    ):
        if actual_htc is None:
            raise RuntimeError(f"Accepted sync calibration validation requires the current {arm} HTC CSV")
        actual_htc = actual_htc.expanduser().resolve()
        arm_report = (report.get("arms") or {}).get(arm) or {}
        arm_status = str(arm_report.get("status", report.get("status", ""))).strip().lower()
        if arm_status != "accepted":
            raise RuntimeError(f"Sync calibration report arm={arm} is not accepted: {resolved_report}")
        if arm_report.get("calibration_id") != calibration_id:
            raise RuntimeError(
                f"Sync calibration report arm={arm} calibration_id does not match the top-level ID"
            )
        reported = calibration_report_timeline_path(report, arm, resolved_report)
        if reported is None:
            raise RuntimeError(f"Sync calibration report has no {arm} frame timeline path: {resolved_report}")
        if reported != selected.expanduser().resolve():
            raise RuntimeError(
                f"Selected {arm} frame timeline does not match the accepted report: "
                f"selected={selected}, report={reported}"
            )
        if not reported.is_file():
            raise FileNotFoundError(f"Accepted {arm} frame timeline does not exist: {reported}")

        timeline = load_frame_timeline(reported)
        if timeline.clock_domain != "host_clock_monotonic_raw":
            raise RuntimeError(
                f"Accepted {arm} timeline is not CLOCK_MONOTONIC_RAW: "
                f"column={timeline.unix_column!r}"
            )
        if timeline.calibration_id != calibration_id:
            raise RuntimeError(
                f"Accepted {arm} frame timeline calibration_id mismatch: "
                f"timeline={timeline.calibration_id!r}, report={calibration_id!r}"
            )
        timeline_report = arm_report.get("frame_timeline") or {}
        verify_file_identity(
            timeline_report.get("file_identity"),
            resolved_report,
            f"arms.{arm}.frame_timeline.file_identity",
            reported,
        )

        reported_htc = (arm_report.get("htc") or {}).get("path")
        if not reported_htc:
            raise RuntimeError(f"Sync calibration report has no arms.{arm}.htc.path")
        reported_htc_path = Path(str(reported_htc)).expanduser()
        if not reported_htc_path.is_absolute():
            reported_htc_path = resolved_report.parent / reported_htc_path
        reported_htc_path = reported_htc_path.resolve()
        if reported_htc_path != actual_htc:
            raise RuntimeError(
                f"Selected {arm} HTC CSV does not match the accepted report: "
                f"selected={actual_htc}, report={reported_htc_path}"
            )
        canonical_arm = canonical_arms.get(arm)
        if not isinstance(canonical_arm, dict):
            raise RuntimeError(f"Accepted report has no input_files.arms.{arm} identity block")
        canonical_htc_identity = canonical_arm.get("htc")
        verify_file_identity(
            canonical_htc_identity,
            resolved_report,
            f"input_files.arms.{arm}.htc",
            actual_htc,
        )
        mirrored_htc_identity = (arm_report.get("htc") or {}).get("file_identity")
        require_same_file_identity(
            canonical_htc_identity,
            mirrored_htc_identity,
            f"arms.{arm}.htc",
        )
        verify_file_identity(
            mirrored_htc_identity,
            resolved_report,
            f"arms.{arm}.htc.file_identity",
            actual_htc,
        )

        actual_insv = discover_session_insv(current_session, arm)
        if not actual_insv:
            raise RuntimeError(f"Current session has no {arm} INSV inputs")
        canonical_insv = canonical_arm.get("insv")
        if not isinstance(canonical_insv, list) or not canonical_insv:
            raise RuntimeError(f"Accepted report has no input_files.arms.{arm}.insv identities")
        canonical_insv_by_path: dict[Path, object] = {}
        for index, identity in enumerate(canonical_insv):
            path = verify_file_identity(
                identity,
                resolved_report,
                f"input_files.arms.{arm}.insv[{index}]",
            )
            if path in canonical_insv_by_path:
                raise RuntimeError(f"Duplicate {arm} INSV identity in accepted report: {path}")
            canonical_insv_by_path[path] = identity
        if set(canonical_insv_by_path) != set(actual_insv):
            raise RuntimeError(
                f"Accepted report {arm} INSV set does not match the current session: "
                f"report={sorted(map(str, canonical_insv_by_path))}, current={list(map(str, actual_insv))}"
            )
        segments = arm_report.get("segments")
        if not isinstance(segments, list) or not segments:
            raise RuntimeError(f"Accepted sync calibration report has no arms.{arm}.segments")
        mirrored_insv_paths: set[Path] = set()
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise RuntimeError(f"arms.{arm}.segments[{index}] is not an object")
            source_file = segment.get("source_file")
            if not source_file:
                raise RuntimeError(f"arms.{arm}.segments[{index}] has no source_file")
            source_path = Path(str(source_file)).expanduser()
            if not source_path.is_absolute():
                source_path = resolved_report.parent / source_path
            source_path = source_path.resolve()
            canonical_identity = canonical_insv_by_path.get(source_path)
            if canonical_identity is None:
                raise RuntimeError(
                    f"arms.{arm}.segments[{index}] source is not in canonical INSV identities: {source_path}"
                )
            mirrored_identity = segment.get("source_file_identity")
            require_same_file_identity(
                canonical_identity,
                mirrored_identity,
                f"arms.{arm}.segments[{index}].source_file",
            )
            verify_file_identity(
                mirrored_identity,
                resolved_report,
                f"arms.{arm}.segments[{index}].source_file_identity",
                source_path,
            )
            mirrored_insv_paths.add(source_path)
        if mirrored_insv_paths != set(actual_insv):
            raise RuntimeError(f"Accepted report segments do not cover every current {arm} INSV file")
        freshness_sources.extend(
            [(f"{arm}_timeline", reported), (f"{arm}_htc", actual_htc)]
        )
        freshness_sources.extend((f"{arm}_insv", path) for path in actual_insv)

    require_report_newer_than(resolved_report, freshness_sources)
    return report


def discover_episode_dir(root: Path, episode_glob: str) -> Path:
    if root.is_dir() and (root / "realtime_trajectory.txt").exists():
        return root.resolve()
    candidates = sorted([path for path in root.expanduser().resolve().glob(episode_glob) if path.is_dir()], key=natural_key)
    if not candidates:
        raise FileNotFoundError(f"No episode directory matching {episode_glob!r} under {root}")
    return candidates[0]


def find_camera_paths(ep_dir: Path) -> tuple[Path, Path, Path]:
    paths = sorted(ep_dir.glob("camera_*.mp4"), key=natural_key)
    by_name = {path.name: path for path in paths}

    def find(keyword: str) -> Path | None:
        matches = [path for name, path in by_name.items() if keyword in name]
        return sorted(matches, key=natural_key)[0] if matches else None

    wrist = find("wrist") or find("front")
    left = find("left")
    right = find("right")
    if wrist is None or left is None or right is None:
        if len(paths) < 3:
            raise ValueError(f"Expected three camera videos under {ep_dir}, got {[str(p) for p in paths]}")
        return paths[0], paths[1], paths[2]
    return wrist, left, right


def find_episode_tmp_root(ep_dir: Path) -> Path | None:
    for parent in [ep_dir, *ep_dir.parents]:
        candidate = parent / "01_raw_extract" / "timestamps" / "frame_timestamps.csv"
        if candidate.exists():
            return parent
    return None


def make_hand_input(
    name: str,
    root: Path,
    episode_glob: str,
    pose_sensor: str,
    explicit_timestamps: Path | None = None,
    require_calibrated: bool = False,
) -> HandInput:
    ep_dir = discover_episode_dir(root, episode_glob)
    tmp_root = find_episode_tmp_root(ep_dir)
    raw_manifest_path = None if tmp_root is None else tmp_root / "01_raw_extract" / "manifest.json"
    if raw_manifest_path is None or not raw_manifest_path.is_file():
        if explicit_timestamps is None:
            timestamps_path = None
            timestamps_source = "missing"
        else:
            timestamps_path = explicit_timestamps.expanduser().resolve()
            timestamps_source = "explicit_calibrated"
    else:
        raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
        timestamps_path, timestamps_source = select_frame_timestamps_path(
            raw_manifest,
            raw_manifest_path,
            explicit_timestamps,
            require_calibrated,
        )
    return HandInput(
        name=name,
        episode_dir=ep_dir,
        trajectory_path=ep_dir / "realtime_trajectory.txt",
        gripper_path=ep_dir / "realtime_marker_distance.csv",
        camera_paths=find_camera_paths(ep_dir),
        pose_sensor=pose_sensor,
        timestamps_path=timestamps_path,
        raw_manifest_path=raw_manifest_path,
        tracker_path=None,
        timestamps_source=timestamps_source,
    )


def make_direct_hand_input(
    name: str,
    episode: str,
    tmp_root: Path,
    tracker_path: Path | None,
    pose_sensor: str,
    tracker_role: str | None = None,
    explicit_timestamps: Path | None = None,
    require_calibrated: bool = False,
    direct_openpi_images: bool = False,
) -> HandInput:
    if Path(episode).name != episode or episode in {".", ".."}:
        raise ValueError(f"{name} episode must be one directory name, got: {episode!r}")
    resolved_tmp_root = tmp_root.expanduser().resolve()
    episode_root = resolved_tmp_root / episode
    raw_manifest_path = episode_root / "01_raw_extract" / "manifest.json"
    if not raw_manifest_path.exists():
        raise FileNotFoundError(f"Missing {name} raw manifest: {raw_manifest_path}")
    manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
    camera_paths: tuple[Path, Path, Path] | None
    if direct_openpi_images:
        # New direct-image manifests deliberately omit the three generated
        # 224x224 training videos.  Image sources are resolved independently
        # and strictly from copied_sources.insv below the HandInput layer.
        camera_paths = None
    else:
        videos = manifest.get("videos", {})
        wrist_video = (
            videos.get("wrist_model")
            or videos.get("front_fisheye_224")
            or videos["front_fisheye"]
        )
        camera_paths = (
            Path(wrist_video),
            Path(videos["left_view"]),
            Path(videos["right_view"]),
        )
    gripper_path = episode_root / "03_gripper_estimation" / "gripper_width.csv"
    if not gripper_path.exists():
        raise FileNotFoundError(f"Missing {name} gripper csv: {gripper_path}")
    resolved_tracker_path = None
    trajectory_path = Path("")
    if pose_sensor == "htc":
        if tracker_path is None:
            raise ValueError(f"{name} direct HTC mode requires an HTC tracker csv")
        resolved_tracker_path = tracker_path.expanduser().resolve()
        if not resolved_tracker_path.exists():
            raise FileNotFoundError(f"Missing {name} HTC tracker csv: {resolved_tracker_path}")
    elif pose_sensor == "insta":
        pose_manifest_path = episode_root / "02_pose_estimation" / "manifest.json"
        if not pose_manifest_path.exists():
            raise FileNotFoundError(f"Missing {name} pose manifest: {pose_manifest_path}")
        pose_manifest = json.loads(pose_manifest_path.read_text(encoding="utf-8"))
        pose_csv = pose_manifest.get("pose_aligned", {}).get("output_csv")
        trajectory_path = Path(pose_csv) if pose_csv else episode_root / "02_pose_estimation" / "pose_aligned.csv"
        if not trajectory_path.is_absolute():
            trajectory_path = episode_root / trajectory_path
        trajectory_path = trajectory_path.expanduser().resolve()
        if not trajectory_path.exists():
            raise FileNotFoundError(f"Missing {name} SLAM pose_aligned csv: {trajectory_path}")
    timestamps_path, timestamps_source = select_frame_timestamps_path(
        manifest,
        raw_manifest_path,
        explicit_timestamps,
        require_calibrated,
    )
    return HandInput(
        name=name,
        episode_dir=episode_root,
        trajectory_path=trajectory_path,
        gripper_path=gripper_path,
        camera_paths=camera_paths,
        pose_sensor=pose_sensor,
        timestamps_path=timestamps_path,
        raw_manifest_path=raw_manifest_path,
        tracker_path=resolved_tracker_path,
        tracker_role=tracker_role,
        timestamps_source=timestamps_source,
    )


def resolve_manifest_direct_insv_paths(manifest_path: Path) -> tuple[tuple[Path, ...], int]:
    """Resolve the canonical ordered raw chapters for direct image decoding.

    Both historical and new direct-image `_01` manifests use
    ``copied_sources.insv``.  Deliberately do not glob or fall back to generated
    videos: chapter order is part of the source-frame identity.
    """

    resolved_manifest = manifest_path.expanduser().resolve(strict=True)
    manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    copied_sources = manifest.get("copied_sources")
    if not isinstance(copied_sources, dict):
        raise RuntimeError(f"raw manifest has no copied_sources object: {resolved_manifest}")
    raw_values = copied_sources.get("insv")
    if not isinstance(raw_values, list) or not raw_values:
        raise RuntimeError(
            f"raw manifest copied_sources.insv must be a non-empty ordered list: "
            f"{resolved_manifest}"
        )
    paths: list[Path] = []
    for chapter_index, value in enumerate(raw_values):
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"raw manifest copied_sources.insv[{chapter_index}] must be a path string: "
                f"{resolved_manifest}"
            )
        path = resolve_path_from_manifest(value, resolved_manifest)
        assert path is not None
        if path.suffix.lower() != ".insv":
            raise RuntimeError(
                f"raw manifest copied_sources.insv[{chapter_index}] is not an INSV file: {path}"
            )
        if not path.is_file():
            raise FileNotFoundError(
                f"raw manifest copied_sources.insv[{chapter_index}] does not exist: {path}"
            )
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise RuntimeError(f"raw manifest contains duplicate INSV chapters: {resolved_manifest}")

    master_clock = manifest.get("master_clock")
    if not isinstance(master_clock, dict):
        raise RuntimeError(f"raw manifest has no master_clock object: {resolved_manifest}")
    try:
        master_frame_count = int(master_clock["frame_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"raw manifest master_clock.frame_count is missing or invalid: {resolved_manifest}"
        ) from exc
    if master_frame_count <= 0:
        raise RuntimeError(
            f"raw manifest master_clock.frame_count must be positive: {resolved_manifest}"
        )
    return tuple(paths), master_frame_count


def build_direct_raw_openpi_plan(
    insv_paths: tuple[Path, ...],
    args: argparse.Namespace,
) -> DirectRawOpenPIImagePlan:
    return DirectRawOpenPIImagePlan.build(
        insv_paths,
        mediasdk_exporter=args.direct_image_mediasdk_exporter,
        target_height=args.image_height,
        target_width=args.image_width,
        panorama_width=args.direct_image_panorama_width,
        panorama_height=args.direct_image_panorama_height,
        projection_size=args.direct_image_projection_size,
        left_yaw=args.direct_image_left_yaw,
        right_yaw=args.direct_image_right_yaw,
        horizontal_fov=args.direct_image_horizontal_fov,
        vertical_fov=args.direct_image_vertical_fov,
        stitch_type=args.direct_image_stitch_type,
        flowstate=args.direct_image_flowstate,
        direction_lock=args.direct_image_direction_lock,
        stitch_fusion=args.direct_image_stitch_fusion,
        cuda=args.direct_image_cuda,
        timeout_sec=args.direct_image_timeout_sec,
        model_dir=args.direct_image_model_dir,
        ffprobe_bin=args.ffprobe_bin,
    )


def build_hand_direct_raw_openpi_plan(
    hand: HandInput,
    args: argparse.Namespace,
) -> DirectRawOpenPIImagePlan:
    if hand.raw_manifest_path is None:
        raise RuntimeError(f"{hand.name} has no _01 raw manifest for direct image decoding")
    insv_paths, master_frame_count = resolve_manifest_direct_insv_paths(hand.raw_manifest_path)
    plan = build_direct_raw_openpi_plan(insv_paths, args)
    if plan.total_frames != master_frame_count:
        raise RuntimeError(
            f"{hand.name} direct raw INSV/master frame count mismatch: "
            f"plan={plan.total_frames}, master_clock={master_frame_count}, "
            f"manifest={hand.raw_manifest_path}. Direct image conversion never trims sources."
        )
    return plan


def validate_direct_raw_timeline(
    arm: str,
    plan: DirectRawOpenPIImagePlan,
    timeline: FrameTimeline,
) -> None:
    plan_frames = plan.total_frames
    if len(timeline.frame_idx) != plan_frames:
        raise RuntimeError(
            f"{arm} direct raw INSV/timestamp frame count mismatch: "
            f"plan={plan_frames}, timestamps={len(timeline.frame_idx)}. "
            "Direct image conversion never truncates the source timeline."
        )
    if not np.array_equal(
        timeline.frame_idx,
        np.arange(plan_frames, dtype=timeline.frame_idx.dtype),
    ):
        raise RuntimeError(
            f"{arm} direct raw timestamp frame_idx must exactly cover "
            f"0..{plan_frames - 1} in source order"
        )


def count_video_frames(path: Path) -> int:
    cv2 = get_cv2()
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open video: {path}")
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()


def count_hand_video_frames(hand: HandInput) -> int:
    if hand.camera_paths is None:
        raise RuntimeError(
            f"{hand.name} has no legacy three-video image source; "
            "direct OpenPI mode must supply a plan frame count"
        )
    return min(count_video_frames(path) for path in hand.camera_paths)


def maybe_cache_camera_videos(
    right: HandInput,
    left: HandInput,
    args: argparse.Namespace,
) -> tuple[HandInput, HandInput, Any | None, dict[str, Any]]:
    """Sequentially stage the six immutable video inputs on fast local storage."""

    from fast_workspace import FastWorkspace, inspect_storage

    if right.camera_paths is None or left.camera_paths is None:
        raise RuntimeError("legacy camera-video cache requires three video paths per arm")
    source_paths = list(dict.fromkeys((*right.camera_paths, *left.camera_paths)))
    source_storage = [inspect_storage(path) for path in source_paths]
    fast_root = args.fast_work_root.expanduser().absolute()
    fast_storage = inspect_storage(fast_root)
    enabled = not args.dry_run and (
        args.fast_workspace == "on"
        or (
            args.fast_workspace == "auto"
            and any(info.rotational for info in source_storage)
            and fast_storage.confident
            and not fast_storage.rotational
        )
    )
    report: dict[str, Any] = {
        "mode": args.fast_workspace,
        "enabled": enabled,
        "root": str(fast_root),
        "source_storage": [info.__dict__ for info in source_storage],
        "work_storage": fast_storage.__dict__,
        "cached_videos": [],
        "disabled_reason": "dry_run" if args.dry_run else None,
    }
    if not enabled:
        return right, left, None, report
    if fast_storage.rotational:
        raise RuntimeError(f"--fast-workspace=on requires non-rotational storage: {fast_root}")

    tmp_root = args.tmp_root.expanduser().resolve()
    final_root = args.output_root.expanduser().resolve()
    resolved_fast = fast_root.resolve(strict=False)
    if is_within(resolved_fast, tmp_root) or is_within(tmp_root, resolved_fast):
        raise ValueError(f"fast work root must not overlap TMP_ROOT: {resolved_fast}")
    if is_within(resolved_fast, final_root) or is_within(final_root, resolved_fast):
        raise ValueError(f"fast work root must not overlap final output: {resolved_fast}")

    reserve_bytes = int(args.fast_work_reserve_gb * (1024**3))
    total_input_bytes = sum(path.stat().st_size for path in source_paths)
    workspace = FastWorkspace(fast_root, reserve_bytes=reserve_bytes)
    cached_paths = workspace.cache_files(
        source_paths,
        verify_full_hash_on_hit=args.verify_fast_cache_full_hash,
    )
    cache_map = dict(zip(source_paths, cached_paths))
    for source, cached in zip(source_paths, cached_paths):
        manifest_path = cached.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_hash = manifest.get("sha256")
        if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
            raise RuntimeError(f"verified cache manifest has no SHA-256: {manifest_path}")
        report["cached_videos"].append(
            {
                "source": str(source.resolve()),
                "cached": str(cached.resolve()),
                "size_bytes": source.stat().st_size,
                "sha256": manifest_hash,
                "source_identity": manifest.get("source_identity"),
            }
        )
    print(
        f"[fast-workspace] staged={len(cached_paths)} videos bytes={total_input_bytes} "
        f"root={fast_root}",
        flush=True,
    )
    return (
        replace(right, camera_paths=tuple(cache_map[path] for path in right.camera_paths)),
        replace(left, camera_paths=tuple(cache_map[path] for path in left.camera_paths)),
        workspace,
        report,
    )


def validate_direct_plan_source_sets(
    source_plans: dict[str, DirectRawOpenPIImagePlan],
) -> None:
    """Fail closed when the two camera arms reference any common raw source."""

    if set(source_plans) != {"right", "left"}:
        raise ValueError("direct raw image plans must contain exactly right and left")
    resolved_by_arm: dict[str, tuple[Path, ...]] = {}
    for arm in ("right", "left"):
        resolved = tuple(
            chapter.path.expanduser().resolve()
            for chapter in source_plans[arm].insv_chapters
        )
        if not resolved:
            raise RuntimeError(f"{arm} direct raw image plan contains no INSV sources")
        if len(set(resolved)) != len(resolved):
            raise RuntimeError(f"{arm} direct raw image plan contains duplicate INSV sources")
        resolved_by_arm[arm] = resolved
    overlap = set(resolved_by_arm["right"]) & set(resolved_by_arm["left"])
    if overlap:
        joined = ", ".join(str(path) for path in sorted(overlap, key=str))
        raise RuntimeError(
            "right/left direct raw INSV source sets overlap; refusing cross-arm "
            f"image provenance: {joined}"
        )


def ordered_direct_plan_sources(
    source_plans: dict[str, DirectRawOpenPIImagePlan],
) -> tuple[Path, ...]:
    validate_direct_plan_source_sets(source_plans)
    return tuple(
        chapter.path
        for arm in ("right", "left")
        for chapter in source_plans[arm].insv_chapters
    )


def prepare_direct_raw_workspace(
    source_plans: dict[str, DirectRawOpenPIImagePlan],
    args: argparse.Namespace,
) -> tuple[Any | None, dict[str, Any]]:
    """Choose direct-mode storage without copying or hashing raw INSV files."""

    from fast_workspace import FastWorkspace, inspect_storage

    source_paths = ordered_direct_plan_sources(source_plans)
    source_storage = [inspect_storage(path) for path in source_paths]
    fast_root = args.fast_work_root.expanduser().absolute()
    fast_storage = inspect_storage(fast_root)
    enabled = not args.dry_run and (
        args.fast_workspace == "on"
        or (
            args.fast_workspace == "auto"
            and any(info.rotational for info in source_storage)
            and fast_storage.confident
            and not fast_storage.rotational
        )
    )
    default_shared_root = Path("/dev/shm/instaumi-direct-openpi")
    png_work_root = (
        args.direct_image_work_root.expanduser().absolute()
        if args.direct_image_work_root is not None
        else (
            default_shared_root
            if default_shared_root.parent.is_dir()
            and os.access(default_shared_root.parent, os.W_OK)
            else fast_root / "direct-openpi-png"
        )
    )
    png_fallback_work_root = fast_root / "direct-openpi-png"
    png_work_storage = inspect_storage(png_work_root)
    report: dict[str, Any] = {
        "mode": args.fast_workspace,
        "enabled": enabled,
        "image_source_mode": "official_continuous_h265_then_active_openpi",
        "root": str(fast_root),
        "png_work_root": str(png_work_root),
        "png_fallback_work_root": str(png_fallback_work_root),
        "source_storage": [info.__dict__ for info in source_storage],
        "work_storage": fast_storage.__dict__,
        "png_work_storage": png_work_storage.__dict__,
        "cached_videos": [],
        "cached_raw_insv": [],
        "cache_order": [str(path.resolve()) for path in source_paths],
        "capacity_policy": (
            "per-file raw cache preflight plus immutable continuous H.265 chapters"
        ),
        "disabled_reason": "dry_run" if args.dry_run else None,
        "staging_status": "pending" if enabled else "not_enabled",
    }
    if not enabled:
        if not args.dry_run and args.fast_workspace == "auto":
            report["disabled_reason"] = "auto_storage_policy_not_satisfied"
        return None, report
    if fast_storage.rotational:
        raise RuntimeError(f"--fast-workspace=on requires non-rotational storage: {fast_root}")
    resolved_png_work = png_work_root.resolve(strict=False)
    shared_memory_root = Path("/dev/shm").resolve(strict=False)
    png_uses_shared_memory = is_within(resolved_png_work, shared_memory_root)
    report["png_uses_shared_memory"] = png_uses_shared_memory
    if png_work_storage.rotational and not png_uses_shared_memory:
        raise RuntimeError(
            f"direct MediaSDK PNG work root must be on non-rotational storage: {png_work_root}"
        )

    tmp_root = args.tmp_root.expanduser().resolve()
    final_root = args.output_root.expanduser().resolve()
    resolved_fast = fast_root.resolve(strict=False)
    if is_within(resolved_fast, tmp_root) or is_within(tmp_root, resolved_fast):
        raise ValueError(f"fast work root must not overlap TMP_ROOT: {resolved_fast}")
    if is_within(resolved_fast, final_root) or is_within(final_root, resolved_fast):
        raise ValueError(f"fast work root must not overlap final output: {resolved_fast}")
    if is_within(resolved_png_work, tmp_root) or is_within(tmp_root, resolved_png_work):
        raise ValueError(f"direct image work root must not overlap TMP_ROOT: {resolved_png_work}")
    if is_within(resolved_png_work, final_root) or is_within(final_root, resolved_png_work):
        raise ValueError(f"direct image work root must not overlap final output: {resolved_png_work}")

    reserve_bytes = int(args.fast_work_reserve_gb * (1024**3))
    workspace = FastWorkspace(fast_root, reserve_bytes=reserve_bytes)
    return workspace, report


def _continuous_panorama_recipe(
    exporter_identity: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "api": "Insta360.MediaSDK.VideoStitcher.SetOutputPath",
        "codec": "hevc",
        "hardware_decode": True,
        "hardware_encode": True,
        "cuda": bool(args.direct_image_cuda),
        "width": int(args.direct_image_panorama_width),
        "height": int(args.direct_image_panorama_height),
        "bitrate": int(args.direct_image_panorama_bitrate),
        "stitch_type": str(args.direct_image_stitch_type),
        "flowstate": bool(args.direct_image_flowstate),
        "direction_lock": bool(args.direct_image_direction_lock),
        "stitch_fusion": bool(args.direct_image_stitch_fusion),
        "model_dir": (
            None
            if args.direct_image_model_dir is None
            else str(args.direct_image_model_dir.expanduser().resolve())
        ),
        "exporter": exporter_identity,
    }


def _validate_continuous_panorama_cache(
    *,
    output_path: Path,
    manifest_path: Path,
    cache_key: str,
    source_chapter: Any,
    recipe: dict[str, Any],
    ffprobe_bin: str,
) -> dict[str, Any] | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != CONTINUOUS_PANORAMA_SCHEMA
            or manifest.get("cache_key") != cache_key
            or manifest.get("source_identity") != source_chapter.identity
            or manifest.get("recipe") != recipe
            or Path(str(manifest.get("output_path", ""))).expanduser().resolve()
            != output_path.resolve()
            or source_identity(output_path) != manifest.get("output_identity")
        ):
            return None
        media = probe_video(output_path, ffprobe_bin)
        expected_rate = str(Fraction(str(source_chapter.frame_rate)))
        if (
            media["codec_name"] != "hevc"
            or int(media["width"]) != int(recipe["width"])
            or int(media["height"]) != int(recipe["height"])
            or int(media["frame_count"]) != int(source_chapter.frame_count)
            or str(Fraction(str(media["frame_rate"]))) != expected_rate
            or media != manifest.get("media")
        ):
            return None
        return manifest
    except Exception:
        return None


def export_continuous_panorama_chapter(
    *,
    arm: str,
    chapter_index: int,
    source_chapter: Any,
    input_path: Path,
    cache_root: Path,
    exporter_identity: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[Path, dict[str, Any]]:
    """Export and atomically publish one complete official H.265 panorama."""

    recipe = _continuous_panorama_recipe(exporter_identity, args)
    cache_payload = {
        "schema": CONTINUOUS_PANORAMA_SCHEMA,
        "source_identity": source_chapter.identity,
        "source_frame_count": int(source_chapter.frame_count),
        "source_frame_rate": str(source_chapter.frame_rate),
        "recipe": recipe,
    }
    cache_key = hashlib.sha256(
        json.dumps(
            cache_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = cache_root / cache_key[:2] / cache_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / "panorama.mp4"
    manifest_path = cache_dir / "manifest.json"
    lock_path = cache_dir / ".lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        cached = _validate_continuous_panorama_cache(
            output_path=output_path,
            manifest_path=manifest_path,
            cache_key=cache_key,
            source_chapter=source_chapter,
            recipe=recipe,
            ffprobe_bin=args.ffprobe_bin,
        )
        if cached is not None:
            print(
                f"[panorama-cache] hit arm={arm} chapter={chapter_index} "
                f"frames={source_chapter.frame_count} path={output_path}",
                flush=True,
            )
            return output_path, {
                "arm": arm,
                "chapter_index": chapter_index,
                "status": "hit",
                "cache_key": cache_key,
                "path": str(output_path),
                "manifest": str(manifest_path),
                "media": cached["media"],
            }

        output_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        partial_path = cache_dir / (
            f"panorama.partial.{os.getpid()}.{uuid.uuid4().hex}.mp4"
        )
        command = [
            str(verify_full_file_identity(exporter_identity)),
            "--input",
            str(input_path.expanduser().resolve(strict=True)),
            "--output",
            str(partial_path),
            "--width",
            str(args.direct_image_panorama_width),
            "--height",
            str(args.direct_image_panorama_height),
            "--bitrate",
            str(args.direct_image_panorama_bitrate),
            "--stitch-type",
            str(args.direct_image_stitch_type),
            "--flowstate",
            "on" if args.direct_image_flowstate else "off",
            "--direction-lock",
            "on" if args.direct_image_direction_lock else "off",
            "--stitch-fusion",
            "on" if args.direct_image_stitch_fusion else "off",
            "--cuda",
            "on" if args.direct_image_cuda else "off",
        ]
        if args.direct_image_model_dir is not None:
            command.extend(
                [
                    "--model-dir",
                    str(args.direct_image_model_dir.expanduser().resolve(strict=True)),
                ]
            )
        print(
            f"[panorama-export] start arm={arm} chapter={chapter_index} "
            f"frames={source_chapter.frame_count} input={input_path}",
            flush=True,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd="/",
                env=dict(os.environ),
                text=True,
                capture_output=True,
                timeout=(
                    None
                    if int(args.direct_image_panorama_timeout_sec) == 0
                    else int(args.direct_image_panorama_timeout_sec)
                ),
            )
            combined_log = completed.stdout + "\n" + completed.stderr
            if completed.returncode != 0:
                raise RuntimeError(
                    f"continuous MediaSDK H.265 export failed for {input_path} "
                    f"(exit={completed.returncode}): {combined_log[-6000:]}"
                )
            if "[done]" not in combined_log:
                raise RuntimeError(
                    "continuous MediaSDK H.265 exporter did not report completion: "
                    f"{combined_log[-6000:]}"
                )
            verify_full_file_identity(exporter_identity)
            media = probe_video(partial_path, args.ffprobe_bin)
            expected_rate = str(Fraction(str(source_chapter.frame_rate)))
            if (
                media["codec_name"] != "hevc"
                or int(media["width"]) != int(args.direct_image_panorama_width)
                or int(media["height"]) != int(args.direct_image_panorama_height)
                or int(media["frame_count"]) != int(source_chapter.frame_count)
                or str(Fraction(str(media["frame_rate"]))) != expected_rate
            ):
                raise RuntimeError(
                    f"continuous panorama validation failed for {input_path}: "
                    f"output={media}, expected_frames={source_chapter.frame_count}, "
                    f"expected_rate={expected_rate}"
                )
            with partial_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial_path, output_path)
            output_identity = source_identity(output_path)
            manifest = {
                **cache_payload,
                "cache_key": cache_key,
                "source_path_used": str(input_path.expanduser().resolve()),
                "output_path": str(output_path.resolve()),
                "output_identity": output_identity,
                "media": media,
                "elapsed_sec": time.monotonic() - started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(manifest_path, manifest)
            print(
                f"[panorama-export] done arm={arm} chapter={chapter_index} "
                f"frames={media['frame_count']} elapsed_sec={manifest['elapsed_sec']:.1f} "
                f"path={output_path}",
                flush=True,
            )
            return output_path, {
                "arm": arm,
                "chapter_index": chapter_index,
                "status": "exported",
                "cache_key": cache_key,
                "path": str(output_path),
                "manifest": str(manifest_path),
                "media": media,
                "elapsed_sec": manifest["elapsed_sec"],
            }
        finally:
            partial_path.unlink(missing_ok=True)


def stage_direct_raw_insv(
    source_plans: dict[str, DirectRawOpenPIImagePlan],
    args: argparse.Namespace,
    workspace: Any | None,
    report: dict[str, Any],
) -> dict[str, DirectOpenPIImagePlan]:
    """Stage raw chapters, then export each chapter exactly once as H.265.

    Unlike the old sparse-PNG path, one official MediaSDK process consumes one
    complete INSV chapter sequentially through SetOutputPath.  Every chapter
    from both arms is submitted to one global pool, so the useful process count
    is ``min(chapter_count, direct_image_panorama_export_workers)``.  Completed
    videos are immutable, validated caches and are safe to reuse after a
    conversion interruption.
    """

    source_paths = ordered_direct_plan_sources(source_plans)
    if report.get("cache_order") != [str(path.resolve()) for path in source_paths]:
        raise RuntimeError("direct raw source order changed after storage planning")
    if bool(report.get("enabled")):
        if workspace is None:
            raise RuntimeError("enabled direct raw cache has no FastWorkspace")
        cached_paths = workspace.cache_files(
            source_paths,
            verify_full_hash_on_hit=args.verify_fast_cache_full_hash,
        )
        if len(cached_paths) != len(source_paths):
            raise RuntimeError(
                f"direct raw cache returned {len(cached_paths)} files for "
                f"{len(source_paths)} sources"
            )
    else:
        if workspace is not None:
            raise RuntimeError("disabled direct raw cache unexpectedly has a workspace")
        cached_paths = source_paths
    cache_map = {
        source.expanduser().resolve(): cached
        for source, cached in zip(source_paths, cached_paths)
    }
    cached_records: list[dict[str, Any]] = []
    if bool(report.get("enabled")):
        for source, cached in zip(source_paths, cached_paths):
            manifest_path = cached.parent / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_hash = manifest.get("sha256")
            if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
                raise RuntimeError(f"verified cache manifest has no SHA-256: {manifest_path}")
            cached_records.append(
                {
                    "source": str(source.resolve()),
                    "cached": str(cached.resolve()),
                    "size_bytes": source.stat().st_size,
                    "sha256": manifest_hash,
                    "source_identity": manifest.get("source_identity"),
                }
            )

    panorama_root = (
        args.fast_work_root.expanduser().absolute()
        / "continuous-panorama-h265"
    )
    panorama_root.mkdir(parents=True, exist_ok=True)
    exporter_identity = full_file_identity(
        args.direct_image_panorama_exporter.expanduser().resolve(strict=True),
        reject_symlink=True,
    )
    jobs: list[tuple[str, int, Any, Path]] = []
    for arm in ("right", "left"):
        for chapter_index, chapter in enumerate(source_plans[arm].insv_chapters):
            jobs.append(
                (
                    arm,
                    chapter_index,
                    chapter,
                    cache_map[chapter.path.expanduser().resolve()],
                )
            )
    worker_limit = min(int(args.direct_image_panorama_export_workers), len(jobs))
    print(
        "[panorama-cache] "
        f"chapters={len(jobs)} mediasdk_workers={worker_limit}/"
        f"{args.direct_image_panorama_export_workers} codec=h265 "
        f"size={args.direct_image_panorama_width}x{args.direct_image_panorama_height} "
        f"bitrate={args.direct_image_panorama_bitrate}",
        flush=True,
    )
    panorama_by_arm: dict[str, dict[int, Path]] = {"right": {}, "left": {}}
    panorama_records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_limit,
        thread_name_prefix="mediasdk-continuous-h265",
    ) as pool:
        future_jobs = {
            pool.submit(
                export_continuous_panorama_chapter,
                arm=arm,
                chapter_index=chapter_index,
                source_chapter=chapter,
                input_path=input_path,
                cache_root=panorama_root,
                exporter_identity=exporter_identity,
                args=args,
            ): (arm, chapter_index)
            for arm, chapter_index, chapter, input_path in jobs
        }
        try:
            for future in concurrent.futures.as_completed(future_jobs):
                arm, chapter_index = future_jobs[future]
                output_path, record = future.result()
                panorama_by_arm[arm][chapter_index] = output_path
                panorama_records.append(record)
        except BaseException:
            for future in future_jobs:
                future.cancel()
            concurrent.futures.wait(future_jobs)
            raise

    runtime_plans: dict[str, DirectOpenPIImagePlan] = {}
    for arm in ("right", "left"):
        source_plan = source_plans[arm]
        cached_arm_paths = tuple(
            cache_map[chapter.path.expanduser().resolve()]
            for chapter in source_plan.insv_chapters
        )
        panorama_paths = tuple(
            panorama_by_arm[arm][index]
            for index in range(len(source_plan.insv_chapters))
        )
        runtime_plan = DirectOpenPIImagePlan.build(
            front_paths=cached_arm_paths,
            panorama_paths=panorama_paths,
            target_height=args.image_height,
            target_width=args.image_width,
            projection_size=args.direct_image_projection_size,
            left_yaw=args.direct_image_left_yaw,
            right_yaw=args.direct_image_right_yaw,
            horizontal_fov=args.direct_image_horizontal_fov,
            vertical_fov=args.direct_image_vertical_fov,
            ffprobe_bin=args.ffprobe_bin,
        )
        if runtime_plan.total_frames != source_plan.total_frames:
            raise RuntimeError(
                f"{arm} continuous panorama frame count changed: "
                f"panorama={runtime_plan.total_frames}, source={source_plan.total_frames}"
            )
        source_plan.verify_sources(verify_samples=False)
        runtime_plans[arm] = runtime_plan

    report["cached_raw_insv"] = cached_records
    report["continuous_panorama_h265"] = {
        "schema": CONTINUOUS_PANORAMA_SCHEMA,
        "root": str(panorama_root),
        "configured_workers": int(args.direct_image_panorama_export_workers),
        "active_workers": worker_limit,
        "chapter_count": len(jobs),
        "records": sorted(
            panorama_records,
            key=lambda item: (str(item["arm"]), int(item["chapter_index"])),
        ),
    }
    report["staging_status"] = "complete"
    print(
        f"[panorama-cache] ready={len(jobs)} chapters root={panorama_root}",
        flush=True,
    )
    return runtime_plans


def iter_bimanual_frames(
    # A capture may contain any positive number of INSV chapters per arm.
    # Keep this as a variable-length sequence; chapter discovery and cache staging
    # already preserve the source order dynamically.
    right_paths: tuple[Path, ...],
    left_paths: tuple[Path, ...],
    target_size: tuple[int, int],
) -> Iterator[dict[str, np.ndarray]]:
    cv2 = get_cv2()
    target_w, target_h = target_size
    all_paths = (*right_paths, *left_paths)
    caps = [cv2.VideoCapture(str(path)) for path in all_paths]
    try:
        for path, cap in zip(all_paths, caps):
            if not cap.isOpened():
                raise RuntimeError(f"could not open camera video: {path}")
        while True:
            bundle: dict[str, np.ndarray] = {}
            for key, cap in zip(CAMERA_KEYS, caps):
                ok, frame_bgr = cap.read()
                if not ok or frame_bgr is None or frame_bgr.size == 0:
                    return
                if frame_bgr.shape[:2] != (target_h, target_w):
                    raise RuntimeError(
                        f"camera input must already be {target_w}x{target_h}; "
                        f"got {frame_bgr.shape[1]}x{frame_bgr.shape[0]}. "
                        "Regenerate stage _01 outputs instead of resizing in _05."
                    )
                bundle[key] = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            yield bundle
    finally:
        for cap in caps:
            cap.release()


def read_frame_indices(
    paths: tuple[Path, Path, Path],
    indices: list[int],
    target_size: tuple[int, int],
) -> Iterator[dict[int, np.ndarray]]:
    if not indices:
        return
    cv2 = get_cv2()
    target_w, target_h = target_size
    caps = [cv2.VideoCapture(str(path)) for path in paths]
    try:
        for path, cap in zip(paths, caps):
            if not cap.isOpened():
                raise RuntimeError(f"could not open camera video: {path}")

        # Each episode/segment passes absolute frame indices from the full
        # recording. Seek to the first requested frame once, then read forward
        # inside the segment. Without this, later segments re-scan from frame 0
        # and get progressively slower.
        first_idx = int(indices[0])
        for cap in caps:
            cap.set(cv2.CAP_PROP_POS_FRAMES, first_idx)
        current_idx = first_idx - 1

        for target_idx in indices:
            if target_idx <= current_idx:
                raise RuntimeError(f"Frame indices must be strictly increasing, got {target_idx} after {current_idx}")
            frames: dict[int, np.ndarray] = {}
            while current_idx < target_idx:
                current_idx += 1
                frames.clear()
                for cap_idx, cap in enumerate(caps):
                    ok, frame_bgr = cap.read()
                    if not ok or frame_bgr is None or frame_bgr.size == 0:
                        raise RuntimeError(f"Video ended before requested frame {target_idx}")
                    if frame_bgr.shape[:2] != (target_h, target_w):
                        raise RuntimeError(
                            f"camera input must already be {target_w}x{target_h}; "
                            f"got {frame_bgr.shape[1]}x{frame_bgr.shape[0]}. "
                            "Regenerate stage _01 outputs instead of resizing in _05."
                        )
                    frames[cap_idx] = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            yield frames
    finally:
        for cap in caps:
            cap.release()


class IndexedVideoGroupReader:
    """Read matching absolute frame indices from three videos with persistent decoders.

    Frame selection remains entirely index based.  Small forward gaps are
    decoded sequentially (the reliable and fast path for inter-frame codecs),
    while large gaps or backwards requests perform an explicit absolute seek.
    No resize, interpolation, or pixel conversion other than the existing
    OpenCV BGR-to-RGB channel reorder is introduced.
    """

    def __init__(
        self,
        paths: tuple[Path, Path, Path],
        target_size: tuple[int, int],
        seek_threshold_frames: int,
    ) -> None:
        if seek_threshold_frames < 0:
            raise ValueError("seek_threshold_frames must be non-negative")
        self.paths = paths
        self.target_size = target_size
        self.seek_threshold_frames = seek_threshold_frames
        self._caps: list[Any] | None = None
        self._next_index: int | None = None
        self._last_index: int | None = None
        self._last_frames: dict[int, np.ndarray] | None = None

    def _ensure_open(self) -> None:
        if self._caps is not None:
            return
        cv2 = get_cv2()
        caps = [cv2.VideoCapture(str(path)) for path in self.paths]
        try:
            for path, cap in zip(self.paths, caps):
                if not cap.isOpened():
                    raise RuntimeError(f"could not open camera video: {path}")
        except Exception:
            for cap in caps:
                cap.release()
            raise
        self._caps = caps
        self._next_index = 0

    def _seek(self, target_index: int) -> None:
        self._ensure_open()
        assert self._caps is not None
        cv2 = get_cv2()
        for path, cap in zip(self.paths, self._caps):
            if not cap.set(cv2.CAP_PROP_POS_FRAMES, target_index):
                raise RuntimeError(f"could not seek {path} to frame {target_index}")
        self._next_index = target_index
        self._last_index = None
        self._last_frames = None

    def read(self, target_index: int) -> dict[int, np.ndarray]:
        target_index = int(target_index)
        if target_index < 0:
            raise ValueError(f"frame index must be non-negative, got {target_index}")
        self._ensure_open()
        assert self._caps is not None and self._next_index is not None

        if target_index == self._last_index:
            assert self._last_frames is not None
            return self._last_frames

        forward_gap = target_index - self._next_index
        if forward_gap < 0 or forward_gap > self.seek_threshold_frames:
            self._seek(target_index)

        cv2 = get_cv2()
        target_w, target_h = self.target_size
        frames: dict[int, np.ndarray] | None = None
        while self._next_index <= target_index:
            decoded: dict[int, np.ndarray] = {}
            for cap_index, (path, cap) in enumerate(zip(self.paths, self._caps)):
                ok, frame_bgr = cap.read()
                if not ok or frame_bgr is None or frame_bgr.size == 0:
                    raise RuntimeError(f"{path} ended before requested frame {target_index}")
                if frame_bgr.shape[:2] != (target_h, target_w):
                    raise RuntimeError(
                        f"camera input must already be {target_w}x{target_h}; "
                        f"got {frame_bgr.shape[1]}x{frame_bgr.shape[0]}. "
                        "Regenerate stage _01 outputs instead of resizing in _05."
                    )
                decoded[cap_index] = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            frames = decoded
            self._next_index += 1

        assert frames is not None
        self._last_index = target_index
        self._last_frames = frames
        return frames

    def close(self) -> None:
        if self._caps is not None:
            for cap in self._caps:
                cap.release()
        self._caps = None
        self._next_index = None
        self._last_index = None
        self._last_frames = None


class BimanualFrameReader:
    """Persistent, independently indexed readers for both camera rigs."""

    def __init__(
        self,
        right_paths: tuple[Path, ...],
        left_paths: tuple[Path, ...],
        target_size: tuple[int, int],
        seek_threshold_frames: int,
    ) -> None:
        self.right = IndexedVideoGroupReader(right_paths, target_size, seek_threshold_frames)
        self.left = IndexedVideoGroupReader(left_paths, target_size, seek_threshold_frames)

    def iter_pairs(self, pairs: list[tuple[int, int]]) -> Iterator[dict[str, np.ndarray]]:
        for right_index, left_index in pairs:
            right_frames = self.right.read(right_index)
            left_frames = self.left.read(left_index)
            yield {
                "observation.images.right_wrist": right_frames[0],
                "observation.images.right_left_view": right_frames[1],
                "observation.images.right_right_view": right_frames[2],
                "observation.images.left_wrist": left_frames[0],
                "observation.images.left_left_view": left_frames[1],
                "observation.images.left_right_view": left_frames[2],
            }

    def close(self) -> None:
        self.right.close()
        self.left.close()


def validated_openpi_image(image: np.ndarray, label: str) -> np.ndarray:
    array = np.asarray(image)
    if array.dtype != np.uint8 or array.shape != (224, 224, 3):
        raise RuntimeError(
            f"direct OpenPI image {label} must be RGB uint8 224x224x3; "
            f"got shape={array.shape} dtype={array.dtype}"
        )
    if not array.flags.c_contiguous:
        raise RuntimeError(f"direct OpenPI image {label} must be C-contiguous")
    return array


class DirectBimanualOpenPIFrameReader:
    """Persistent two-arm adapter over raw or continuous-H.265 decoders.

    The old raw decoder remains readable for legacy plans.  New runs receive
    one validated continuous MediaSDK H.265 panorama per INSV chapter and only
    decode/project the calibrated active indices here.
    """

    def __init__(
        self,
        right_plan: DirectRawOpenPIImagePlan | DirectOpenPIImagePlan,
        left_plan: DirectRawOpenPIImagePlan | DirectOpenPIImagePlan,
        args: argparse.Namespace,
        work_root: Path,
        fallback_work_root: Path | None = None,
        front_cache_manifests: dict[str, Path] | None = None,
    ) -> None:
        self.batch_size = int(
            getattr(args, "_episode_shard_batch_frames_per_arm", args.direct_image_batch_frames)
        )
        self.parallel_arms = bool(getattr(args, "direct_image_parallel_arms", True))
        cache_manifests = front_cache_manifests or {}
        resize_workers = int(
            getattr(
                args,
                "_episode_shard_resize_workers_per_arm",
                args.direct_image_resize_workers,
            )
        )

        def make_decoder(
            arm: str,
            plan: DirectRawOpenPIImagePlan | DirectOpenPIImagePlan,
        ) -> DirectRawOpenPIImageDecoder | DirectOpenPIImageDecoder:
            common = {
                "resize_workers": resize_workers,
                "v360_filter_threads": args.direct_image_v360_filter_threads,
                "repeat_cache_frames": args.direct_image_repeat_cache_frames,
                "front_cache_manifest": cache_manifests.get(arm),
                "verify_front_cache_full_hash": bool(
                    getattr(args, "verify_fast_cache_full_hash", True)
                ),
            }
            if isinstance(plan, DirectOpenPIImagePlan):
                return DirectOpenPIImageDecoder(
                    plan,
                    seek_threshold_frames=args.video_seek_threshold_frames,
                    decode_threads_per_reader=args.direct_image_decode_threads,
                    **common,
                )
            return DirectRawOpenPIImageDecoder(
                plan,
                work_root=work_root,
                fallback_work_root=fallback_work_root or work_root,
                batch_size=self.batch_size,
                export_workers=int(
                    getattr(
                        args,
                        "_episode_shard_export_workers_per_arm",
                        args.direct_image_export_workers,
                    )
                ),
                free_space_margin_bytes=int(
                    args.direct_image_temp_margin_gb * (1024**3)
                ),
                seek_threshold_frames=args.video_seek_threshold_frames,
                decode_threads=args.direct_image_decode_threads,
                gpu_admission=getattr(args, "_direct_image_gpu_admission", None),
                **common,
            )

        self.right = make_decoder("right", right_plan)
        try:
            self.left = make_decoder("left", left_plan)
        except BaseException:
            self.right.close()
            raise
        self._arm_pool = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="direct-openpi-arm",
            )
            if self.parallel_arms
            else None
        )
        self._closed = False

    @staticmethod
    def _arm_bundle(
        arm: str,
        requested_index: int,
        frame: DirectOpenPIFrame,
    ) -> dict[str, np.ndarray]:
        if frame.frame_index != requested_index:
            raise RuntimeError(
                f"direct OpenPI {arm} decoder returned frame {frame.frame_index}, "
                f"requested {requested_index}"
            )
        return {
            f"observation.images.{arm}_wrist": validated_openpi_image(
                frame.front, f"{arm}.front[{requested_index}]"
            ),
            f"observation.images.{arm}_left_view": validated_openpi_image(
                frame.left, f"{arm}.left[{requested_index}]"
            ),
            f"observation.images.{arm}_right_view": validated_openpi_image(
                frame.right, f"{arm}.right[{requested_index}]"
            ),
        }

    @staticmethod
    def _decoder_batches(
        decoder: DirectRawOpenPIImageDecoder | DirectOpenPIImageDecoder,
        indices: tuple[int, ...],
        batch_size: int,
    ) -> Iterator[tuple[DirectOpenPIFrame, ...]]:
        batch_method = getattr(decoder, "iter_frame_batches", None)
        if callable(batch_method):
            yield from batch_method(indices)
            return
        # Compatibility for test doubles and older external decoders.
        for start in range(0, len(indices), batch_size):
            yield tuple(decoder.iter_frames(indices[start : start + batch_size]))

    def _parallel_next(
        self,
        right_batches: Iterator[tuple[DirectOpenPIFrame, ...]],
        left_batches: Iterator[tuple[DirectOpenPIFrame, ...]],
    ) -> tuple[tuple[DirectOpenPIFrame, ...], tuple[DirectOpenPIFrame, ...]]:
        assert self._arm_pool is not None
        right_future = self._arm_pool.submit(next, right_batches)
        left_future = self._arm_pool.submit(next, left_batches)
        futures = (right_future, left_future)
        try:
            done, _pending = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_EXCEPTION,
            )
            failure = next(
                (
                    future.exception()
                    for future in done
                    if not future.cancelled() and future.exception() is not None
                ),
                None,
            )
            if failure is not None:
                for future in futures:
                    future.cancel()
                concurrent.futures.wait(futures)
                raise failure
            return right_future.result(), left_future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            concurrent.futures.wait(futures)
            raise

    def iter_pairs(self, pairs: list[tuple[int, int]]) -> Iterator[dict[str, np.ndarray]]:
        if self._closed:
            raise RuntimeError("direct bimanual image reader is closed")
        right_indices = tuple(pair[0] for pair in pairs)
        left_indices = tuple(pair[1] for pair in pairs)
        right_batches = self._decoder_batches(self.right, right_indices, self.batch_size)
        left_batches = self._decoder_batches(self.left, left_indices, self.batch_size)
        try:
            for batch_start in range(0, len(pairs), self.batch_size):
                batch_pairs = pairs[batch_start : batch_start + self.batch_size]
                started = time.monotonic()
                if self._arm_pool is None:
                    right_frames = next(right_batches)
                    left_frames = next(left_batches)
                else:
                    right_frames, left_frames = self._parallel_next(
                        right_batches,
                        left_batches,
                    )
                if len(right_frames) != len(batch_pairs) or len(left_frames) != len(batch_pairs):
                    raise RuntimeError(
                        "direct OpenPI arm batch frame count mismatch: "
                        f"pairs={len(batch_pairs)} right={len(right_frames)} left={len(left_frames)}"
                    )
                elapsed = time.monotonic() - started
                print(
                    "[direct-images] "
                    f"pairs={batch_start}-{batch_start + len(batch_pairs) - 1} "
                    f"count={len(batch_pairs)} parallel_arms={int(self.parallel_arms)} "
                    f"elapsed_sec={elapsed:.3f} "
                    f"pairs_per_sec={len(batch_pairs) / max(elapsed, 1e-9):.3f}",
                    flush=True,
                )
                for pair, right_frame, left_frame in zip(
                    batch_pairs,
                    right_frames,
                    left_frames,
                    strict=True,
                ):
                    right_index, left_index = pair
                    yield {
                        **self._arm_bundle("right", right_index, right_frame),
                        **self._arm_bundle("left", left_index, left_frame),
                    }
        finally:
            right_batches.close()
            left_batches.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._arm_pool is not None:
            self._arm_pool.shutdown(wait=True, cancel_futures=True)
        try:
            self.right.close()
        finally:
            self.left.close()


def resolve_front_openpi_cache_manifests(
    right: HandInput,
    left: HandInput,
    args: argparse.Namespace,
) -> dict[str, Path] | None:
    """Resolve the optional _03 decode-fanout cache for both arms atomically.

    Mixed cached/raw front paths are deliberately rejected: both arms must use
    the same deterministic recipe in one conversion run so provenance and
    performance remain symmetric.
    """

    mode = str(getattr(args, "direct_image_front_cache", "auto"))
    if mode == "off":
        return None
    paths = {
        "right": right.gripper_path.parent / "manifest.json",
        "left": left.gripper_path.parent / "manifest.json",
    }
    available: dict[str, bool] = {}
    for arm, path in paths.items():
        payload = load_json_file(path)
        available[arm] = bool(
            payload is not None
            and isinstance(payload.get("front_openpi_cache"), dict)
        )
    if all(available.values()):
        print(
            "[front-cache] mode=aruco_decode_fanout_openpi224 "
            f"right={paths['right']} left={paths['left']}",
            flush=True,
        )
        return paths
    if mode == "require":
        missing = [arm for arm, present in available.items() if not present]
        raise RuntimeError(
            "required _03 front OpenPI cache is missing for: " + ", ".join(missing)
        )
    if any(available.values()):
        raise RuntimeError(
            "front OpenPI cache exists for only one arm; rerun bimanual _03 "
            "with FRONT_OPENPI_CACHE=1"
        )
    print("[front-cache] unavailable; falling back to raw INSV front decode", flush=True)
    return None


def normalized_front_cache_report_mode(args: argparse.Namespace) -> str | None:
    """Report the cache semantic without binding an unused accelerator to resume."""

    if not bool(getattr(args, "direct_openpi_images", False)):
        return None
    if bool(getattr(args, "panorama_numeric_only", False)):
        return "off_unused_for_panorama_numeric_only"
    return str(getattr(args, "direct_image_front_cache", "auto"))


def iter_bimanual_frames_by_pairs(
    right_paths: tuple[Path, ...],
    left_paths: tuple[Path, ...],
    pairs: list[tuple[int, int]],
    target_size: tuple[int, int],
) -> Iterator[dict[str, np.ndarray]]:
    right_iter = read_frame_indices(right_paths, [pair[0] for pair in pairs], target_size)
    left_iter = read_frame_indices(left_paths, [pair[1] for pair in pairs], target_size)
    for right_frames, left_frames in zip(right_iter, left_iter):
        yield {
            "observation.images.right_wrist": right_frames[0],
            "observation.images.right_left_view": right_frames[1],
            "observation.images.right_right_view": right_frames[2],
            "observation.images.left_wrist": left_frames[0],
            "observation.images.left_left_view": left_frames[1],
            "observation.images.left_right_view": left_frames[2],
        }


@lru_cache(maxsize=None)
def load_frame_timeline(path: Path) -> FrameTimeline:
    resolved = path.expanduser().resolve()
    frame_indices: list[int] = []
    unix_values_ms: list[float] = []
    valid_values: list[bool] = []
    calibration_ids: set[str] = set()
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        index_column = next(
            (name for name in ("frame_idx", "original_concat_frame_idx") if name in fields),
            None,
        )
        if index_column is None:
            raise ValueError(
                f"{resolved} is missing frame_idx/original_concat_frame_idx; source-frame identity is required"
            )
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
            raise ValueError(
                f"{resolved} has no supported calibrated time column"
            )
        clock_domain = (
            "host_clock_monotonic_raw"
            if "monotonic_raw" in unix_column
            else "unix_realtime"
        )
        validity_columns = tuple(
            name
            for name in (
                "timeline_valid",
                "recommended_keep",
            )
            if name in fields
        )
        for row_number, row in enumerate(reader, start=2):
            try:
                frame_index = int(row[index_column])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{resolved}:{row_number} has invalid {index_column}") from exc
            is_valid = all(parse_csv_bool(row.get(name), default=True) for name in validity_columns)
            raw = row.get(unix_column)
            try:
                if raw is None or str(raw).strip() == "":
                    unix_ms = float("nan")
                elif unix_column.endswith("_ns"):
                    unix_ms = float(raw) / 1e6
                elif unix_column.endswith("_sec"):
                    unix_ms = float(raw) * 1000.0
                else:
                    unix_ms = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{resolved}:{row_number} has invalid {unix_column}") from exc
            if is_valid and not np.isfinite(unix_ms):
                raise ValueError(f"{resolved}:{row_number} is timeline-valid but has no finite time")
            frame_indices.append(frame_index)
            unix_values_ms.append(unix_ms)
            valid_values.append(is_valid)
            if "calibration_id" in fields:
                row_calibration_id = str(row.get("calibration_id") or "").strip()
                if not row_calibration_id:
                    raise ValueError(f"{resolved}:{row_number} has an empty calibration_id")
                calibration_ids.add(row_calibration_id)

    if not frame_indices:
        raise ValueError(f"empty frame timestamp csv: {path}")
    frame_idx = np.asarray(frame_indices, dtype=np.int64)
    expected = np.arange(len(frame_idx), dtype=np.int64)
    if not np.array_equal(frame_idx, expected):
        mismatch = int(np.flatnonzero(frame_idx != expected)[0]) if np.any(frame_idx != expected) else -1
        raise ValueError(
            f"{resolved} must retain every original frame in order; "
            f"row={mismatch} frame_idx={frame_idx[mismatch] if mismatch >= 0 else 'unknown'}"
        )
    unix_ms = np.asarray(unix_values_ms, dtype=np.float64)
    valid = np.asarray(valid_values, dtype=bool)
    valid_indices = np.flatnonzero(valid)
    if len(valid_indices) < 2:
        raise ValueError(f"{resolved} has fewer than two timeline-valid frames")
    if len(calibration_ids) > 1:
        raise ValueError(f"{resolved} contains multiple calibration_id values: {sorted(calibration_ids)}")
    valid_unix = unix_ms[valid_indices]
    bad_steps = np.flatnonzero(np.diff(valid_unix) <= 0.0)
    if len(bad_steps):
        left = int(valid_indices[int(bad_steps[0])])
        right = int(valid_indices[int(bad_steps[0]) + 1])
        raise ValueError(
            f"{resolved} valid timeline is not strictly increasing between original frames {left} and {right}"
        )
    return FrameTimeline(
        path=resolved,
        frame_idx=frame_idx,
        unix_ms=unix_ms,
        valid=valid,
        unix_column=unix_column,
        validity_columns=validity_columns,
        calibration_id=next(iter(calibration_ids), None),
        clock_domain=clock_domain,
    )


def load_frame_unix_seconds(path: Path) -> np.ndarray:
    return load_frame_timeline(path).unix_ms / 1000.0


def load_frame_unix_milliseconds(path: Path) -> np.ndarray:
    return load_frame_timeline(path).unix_ms


def find_frame_at_or_after(timeline: FrameTimeline, unix_ms: float, limit: int | None = None) -> int:
    limit = len(timeline.frame_idx) if limit is None else min(int(limit), len(timeline.frame_idx))
    valid_indices = timeline.valid_indices
    valid_indices = valid_indices[valid_indices < limit]
    pos = int(np.searchsorted(timeline.unix_ms[valid_indices], unix_ms, side="left"))
    return limit if pos >= len(valid_indices) else int(valid_indices[pos])


def timestamp_log_path(hand: HandInput) -> Path:
    if hand.raw_manifest_path is None or not hand.raw_manifest_path.exists():
        raise FileNotFoundError(f"{hand.name} is missing raw manifest; cannot find timestamp_log")
    manifest = json.loads(hand.raw_manifest_path.read_text(encoding="utf-8"))
    log_path = manifest.get("copied_sources", {}).get("timestamp_log")
    if not log_path:
        raise FileNotFoundError(f"{hand.name} raw manifest has no copied_sources.timestamp_log")
    path = Path(log_path)
    if not path.exists():
        raise FileNotFoundError(f"{hand.name} timestamp_log does not exist: {path}")
    return path


def load_raw_segment_times(
    log_path: Path,
    segment_time_offset_sec: float,
) -> list[tuple[int, float, float]]:
    starts: dict[int, dict] = {}
    ranges: list[tuple[int, float, float]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        event_type = row.get("type")
        segment_index = row.get("segmentIndex")
        if segment_index is None:
            continue
        segment_index = int(segment_index)
        if event_type == "segment_start":
            starts[segment_index] = row
        elif event_type == "segment_end" and segment_index in starts:
            start = starts.pop(segment_index)
            raw_start_ms = float(start["unixTimeMs"])
            raw_end_ms = float(row["unixTimeMs"])
            start_ms = raw_start_ms + segment_time_offset_sec * 1000.0
            end_ms = raw_end_ms + segment_time_offset_sec * 1000.0
            if raw_end_ms <= raw_start_ms or end_ms <= start_ms:
                continue
            ranges.append((segment_index, start_ms, end_ms))
    return sorted(ranges, key=lambda item: item[0])


def source_packet_index_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    column_present = bool(rows) and "source_packet_index_raw" in rows[0]["raw"]
    values: list[int] = []
    invalid_count = 0
    if column_present:
        for item in rows:
            raw = item["raw"].get("source_packet_index_raw")
            try:
                numeric = float(raw)
                if not np.isfinite(numeric) or not numeric.is_integer():
                    raise ValueError
                values.append(int(numeric))
            except (TypeError, ValueError):
                invalid_count += 1
    result: dict[str, Any] = {
        "column_present": column_present,
        "rows_evaluated": len(rows),
        "available_count": len(values),
        "availability_rate": (float(len(values) / len(rows)) if rows else 0.0),
        "missing_or_invalid_count": invalid_count if column_present else len(rows),
        "counter_semantics": "unverified_raw_counter_not_used_for_frame_rejection",
        "assumed_wrap_modulus_for_diagnostics_only": 65536,
        "adjacent_repeat_count": 0,
        "suspected_wrap_count": 0,
        "backward_non_wrap_count": 0,
        "forward_jump_count": 0,
        "estimated_missing_packets_diagnostic": 0,
    }
    if len(values) < 2:
        return result
    packet = np.asarray(values, dtype=np.int64)
    previous = packet[:-1]
    current = packet[1:]
    raw_step = current - previous
    suspected_wrap = (previous >= 49152) & (current <= 16384) & (raw_step < 0)
    diagnostic_step = np.where(suspected_wrap, current + 65536 - previous, raw_step)
    repeat = raw_step == 0
    backward_non_wrap = (raw_step < 0) & ~suspected_wrap
    forward_jump = diagnostic_step > 1
    result.update(
        {
            "adjacent_repeat_count": int(np.count_nonzero(repeat)),
            "suspected_wrap_count": int(np.count_nonzero(suspected_wrap)),
            "backward_non_wrap_count": int(np.count_nonzero(backward_non_wrap)),
            "forward_jump_count": int(np.count_nonzero(forward_jump)),
            "estimated_missing_packets_diagnostic": int(
                np.sum(diagnostic_step[forward_jump] - 1, dtype=np.int64)
            ),
        }
    )
    return result


def host_clock_offset_report(
    rows: list[dict[str, Any]],
    realtime_sec: np.ndarray,
    realtime_column: str,
    max_step_ms: float,
) -> dict[str, Any]:
    monotonic_candidates = (
        ["hid_read_host_monotonic_ns", "host_monotonic_time_ns"]
        if realtime_column == "hid_read_host_realtime_ns"
        else ["host_monotonic_time_ns"]
    )
    selection: list[dict[str, Any]] = []
    selected_column: str | None = None
    selected_values = np.asarray([], dtype=np.float64)
    selected_finite = np.asarray([], dtype=bool)
    available_fields = set(rows[0]["raw"]) if rows else set()
    for candidate in monotonic_candidates:
        if candidate not in available_fields:
            continue
        values_list: list[float] = []
        for item in rows:
            try:
                value = float(item["raw"].get(candidate))
            except (TypeError, ValueError):
                value = float("nan")
            values_list.append(value / 1e9)
        values = np.asarray(values_list, dtype=np.float64)
        finite = np.isfinite(values)
        finite_count = int(np.count_nonzero(finite))
        coverage = float(finite_count / len(rows)) if rows else 0.0
        accepted = finite_count >= 2 and coverage >= 0.95
        selected = accepted and selected_column is None
        selection.append(
            {
                "column": candidate,
                "finite_selected_time_rows": finite_count,
                "coverage": coverage,
                "selected": selected,
            }
        )
        if selected:
            selected_column = candidate
            selected_values = values
            selected_finite = finite

    report: dict[str, Any] = {
        "status": "unavailable",
        "realtime_column": realtime_column,
        "monotonic_column": selected_column,
        "monotonic_candidates": selection,
        "coverage": next(
            (float(item["coverage"]) for item in selection if item["selected"]),
            None,
        ),
        "minimum_coverage": 0.95,
        "max_allowed_adjacent_offset_step_ms": max_step_ms,
        "checked": selected_column is not None,
        "sample_count": 0,
        "offset_start_ms": None,
        "offset_end_ms": None,
        "offset_change_ms": None,
        "max_abs_adjacent_offset_step_ms": None,
        "jump_over_threshold_count": 0,
    }
    if selected_column is None:
        return report
    offsets_ms = (realtime_sec[selected_finite] - selected_values[selected_finite]) * 1000.0
    steps_ms = np.diff(offsets_ms)
    abs_steps_ms = np.abs(steps_ms)
    jump_count = int(np.count_nonzero(abs_steps_ms > max_step_ms))
    report.update(
        {
            "status": "verified",
            "sample_count": int(len(offsets_ms)),
            "offset_start_ms": float(offsets_ms[0]),
            "offset_end_ms": float(offsets_ms[-1]),
            "offset_change_ms": float(offsets_ms[-1] - offsets_ms[0]),
            "max_abs_adjacent_offset_step_ms": (
                float(np.max(abs_steps_ms)) if len(abs_steps_ms) else 0.0
            ),
            "jump_over_threshold_count": jump_count,
        }
    )
    return report


def load_htc_tracker_csv(
    path: Path,
    role: str | None = None,
    time_source: str = "auto",
    max_clock_offset_step_ms: float = 5.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    if max_clock_offset_step_ms <= 0.0:
        raise ValueError("max_clock_offset_step_ms must be positive")
    rows: list[dict[str, Any]] = []
    selected_role = normalize_tracker_role(role) if role is not None else None
    if role is not None and selected_role is None:
        raise ValueError(f"Unrecognized HTC role {role!r}; refusing to mix tracker streams")
    observed_roles: set[str] = set()
    total_csv_rows = 0
    role_filtered_rows = 0
    tracking_status_filtered_rows = 0
    tracking_status_missing_or_invalid_rows = 0
    tracking_status_non_full_pose_rows = 0
    tracking_status_unverified_rows = 0
    invalid_pose_rows = 0
    tracking_status_counts: dict[str, int] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        tracking_status_present = "tracking_status" in fields
        required = {"pos_x", "pos_y", "pos_z", "ori_x", "ori_y", "ori_z", "ori_w"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing HTC columns: {', '.join(sorted(missing))}")
        for row in reader:
            total_csv_rows += 1
            normalized_row_role = normalize_tracker_role(row.get("role"), row.get("mac"))
            if normalized_row_role is not None:
                observed_roles.add(normalized_row_role)
            if selected_role is not None and normalized_row_role != selected_role:
                role_filtered_rows += 1
                continue
            if tracking_status_present:
                raw_status = row.get("tracking_status")
                try:
                    numeric_status = float(raw_status)
                    if not np.isfinite(numeric_status) or not numeric_status.is_integer():
                        raise ValueError
                    status = int(numeric_status)
                except (TypeError, ValueError):
                    tracking_status_missing_or_invalid_rows += 1
                    tracking_status_filtered_rows += 1
                    continue
                status_key = str(status)
                tracking_status_counts[status_key] = tracking_status_counts.get(status_key, 0) + 1
                if status != 2:
                    tracking_status_non_full_pose_rows += 1
                    tracking_status_filtered_rows += 1
                    continue
            else:
                # Old recorder CSVs did not expose tracking_status.  They are
                # retained for compatibility, but explicitly reported as
                # unverified rather than silently treated as status=2.
                tracking_status_unverified_rows += 1
            try:
                position = [float(row["pos_x"]), float(row["pos_y"]), float(row["pos_z"])]
                quat = [float(row["ori_x"]), float(row["ori_y"]), float(row["ori_z"]), float(row["ori_w"])]
            except (TypeError, ValueError):
                invalid_pose_rows += 1
                continue
            if not np.all(np.isfinite(position)) or not np.all(np.isfinite(quat)):
                invalid_pose_rows += 1
                continue
            rows.append(
                {
                    "raw": row,
                    "position": position,
                    "quat": quat,
                    "normalized_role": normalized_row_role,
                }
            )
    if selected_role is None and len(observed_roles) > 1:
        raise ValueError(
            f"{path} contains multiple tracker roles {sorted(observed_roles)}; "
            "an explicit role is required"
        )
    if selected_role is None and len(observed_roles) == 1:
        selected_role = next(iter(observed_roles))
        before = len(rows)
        rows = [item for item in rows if item["normalized_role"] == selected_role]
        role_filtered_rows += before - len(rows)
    if len(rows) < 2:
        raise ValueError(
            f"{path} has fewer than two valid full-pose HTC rows for role={role!r}; "
            f"tracking_status_filtered={tracking_status_filtered_rows}, invalid_pose={invalid_pose_rows}"
        )
    full_pose_rows_before_time_filter = len(rows)

    aliases = {
        "hid_read_monotonic_raw": "hid_read_host_monotonic_raw_ns",
        "hid_read_realtime": "hid_read_host_realtime_ns",
        "callback": "host_unix_time_ns",
    }
    requested = aliases.get(time_source, time_source)
    candidates = (
        [
            "hid_read_host_monotonic_raw_ns",
            "hid_read_host_realtime_ns",
            "host_unix_time_ns",
            "timestamp",
        ]
        if requested == "auto"
        else [requested]
    )
    chosen: str | None = None
    timestamps = np.asarray([], dtype=np.float64)
    chosen_finite = np.asarray([], dtype=bool)
    time_selection: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate not in fields:
            continue
        candidate_values: list[float] = []
        for item in rows:
            raw = item["raw"].get(candidate)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = float("nan")
            candidate_values.append(value)
        values = np.asarray(candidate_values, dtype=np.float64)
        finite = np.isfinite(values)
        finite_count = int(np.count_nonzero(finite))
        coverage = float(finite_count / len(rows)) if rows else 0.0
        accepted = finite_count >= 2 and coverage >= 0.95
        selected = accepted and chosen is None
        time_selection.append(
            {
                "column": candidate,
                "finite_eligible_rows": finite_count,
                "eligible_coverage": coverage,
                "selected": selected,
            }
        )
        if selected:
            chosen = candidate
            timestamps = values / 1e9 if candidate.endswith("_ns") else values
            chosen_finite = finite
    if chosen is None:
        raise ValueError(
            f"{path} has no single HTC time column with >=95% full-pose coverage "
            f"for role={role!r}: {time_selection}. Clock domains are never mixed row by row."
        )

    time_source_missing_filtered_rows = int(len(rows) - np.count_nonzero(chosen_finite))
    rows = [item for item, keep in zip(rows, chosen_finite) if keep]
    ts = timestamps[chosen_finite]
    pos = np.asarray([item["position"] for item in rows], dtype=np.float64)
    quat = np.asarray([item["quat"] for item in rows], dtype=np.float64)
    metadata_path = path.expanduser().resolve().parent / "tracker_meta.json"
    quaternion_schema_source = "legacy_assumed_missing_tracker_meta"
    csv_is_firmware_xyzw = False
    if metadata_path.exists():
        try:
            tracker_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read {metadata_path}: {exc}") from exc
        quaternion_schema = tracker_metadata.get("quaternion_schema")
        if isinstance(quaternion_schema, dict):
            csv_is_firmware_xyzw = (
                quaternion_schema.get("csv_ori_equals_firmware_payload") is True
            )
            quaternion_schema_source = (
                "native_xyzw_v2" if csv_is_firmware_xyzw else "legacy_mapping_declared"
            )
        else:
            quaternion_schema_source = "legacy_tracker_meta_without_quaternion_schema"
    if not csv_is_firmware_xyzw:
        quat = np.stack(
            [legacy_csv_xyzw_to_firmware_xyzw(quaternion) for quaternion in quat]
        )
    packet_report = source_packet_index_report(rows)
    clock_offset_report = host_clock_offset_report(
        rows,
        ts,
        chosen,
        max_clock_offset_step_ms,
    )
    if clock_offset_report["jump_over_threshold_count"]:
        raise ValueError(
            f"{path} has a host realtime/monotonic clock-offset jump over "
            f"{max_clock_offset_step_ms:.3f} ms; refusing interpolation: "
            f"{json.dumps(clock_offset_report, ensure_ascii=False, allow_nan=False)}"
        )
    non_monotonic_input = int(np.count_nonzero(np.diff(ts) <= 0.0))
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    pos = pos[order]
    quat = quat[order]
    unique_ts, unique_indices = np.unique(ts, return_index=True)
    duplicate_count = int(len(ts) - len(unique_ts))
    gaps_ms = np.diff(unique_ts) * 1000.0
    report = {
        "path": str(path.expanduser().resolve()),
        "role": selected_role or "all",
        "time_source": chosen,
        "time_source_policy": "highest_priority_single_column_with_at_least_95pct_full_pose_coverage",
        "time_source_candidates": time_selection,
        "time_source_missing_filtered_rows": time_source_missing_filtered_rows,
        "total_csv_rows": total_csv_rows,
        "role_filtered_rows": role_filtered_rows,
        "tracking_status_column_present": tracking_status_present,
        "tracking_status_policy": (
            "require_status_2_full_pose"
            if tracking_status_present
            else "legacy_column_absent_accept_pose_rows_as_unverified"
        ),
        "tracking_status_counts": tracking_status_counts,
        "tracking_status_filtered_rows": tracking_status_filtered_rows,
        "tracking_status_missing_or_invalid_rows": tracking_status_missing_or_invalid_rows,
        "tracking_status_non_full_pose_rows": tracking_status_non_full_pose_rows,
        "tracking_status_unverified_rows": tracking_status_unverified_rows,
        "invalid_pose_rows": invalid_pose_rows,
        "full_pose_rows_before_time_filter": full_pose_rows_before_time_filter,
        "input_pose_rows": int(len(rows)),
        "source_packet_index_raw": packet_report,
        "realtime_monotonic_check": clock_offset_report,
        "unique_timestamp_rows": int(len(unique_ts)),
        "duplicate_timestamp_count": duplicate_count,
        "non_monotonic_input_steps": non_monotonic_input,
        "sample_gap_ms": distribution_report(gaps_ms),
        "sample_gap_over_10ms": int(np.count_nonzero(gaps_ms > 10.0)),
        "sample_gap_over_20ms": int(np.count_nonzero(gaps_ms > 20.0)),
        "sample_gap_over_25ms": int(np.count_nonzero(gaps_ms > 25.0)),
        "quaternion_schema": {
            "source": quaternion_schema_source,
            "input_csv_order": "xyzw",
            "normalized_output_order": "firmware_xyzw",
            "legacy_component_recovery_applied": not csv_is_firmware_xyzw,
        },
    }
    return unique_ts, pos[unique_indices], quat[unique_indices], report


def validate_calibrated_htc_time_source(
    calibration_report: dict[str, Any],
    arm: str,
    pose_quality: PoseQuality,
) -> None:
    arm_report = ((calibration_report.get("arms") or {}).get(arm) or {})
    htc_report = arm_report.get("htc") or {}
    expected = htc_report.get("time_column")
    actual = pose_quality.report.get("time_source")
    contract = calibration_report.get("clock_contract") or {}
    if contract.get("domain") != "host_clock_monotonic_raw":
        raise RuntimeError(
            f"Accepted calibration report is not bound to host_clock_monotonic_raw: {contract!r}"
        )
    if not expected:
        raise RuntimeError(f"Accepted calibration report has no arms.{arm}.htc.time_column")
    if actual != expected:
        raise RuntimeError(
            f"{arm} HTC time source does not match the accepted calibration report: "
            f"converter={actual!r}, calibration={expected!r}"
        )


def htc_query_quality(
    tracker_ts: np.ndarray,
    query_ts: np.ndarray,
    timeline_valid: np.ndarray,
    max_nearest_gap_ms: float,
    max_bracket_gap_ms: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    finite_query = np.isfinite(query_ts)
    safe_query = np.where(finite_query, query_ts, tracker_ts[0])
    insertion = np.searchsorted(tracker_ts, safe_query, side="left")
    left = np.clip(insertion - 1, 0, len(tracker_ts) - 1)
    right = np.clip(insertion, 0, len(tracker_ts) - 1)
    nearest_ms = np.minimum(
        np.abs(safe_query - tracker_ts[left]),
        np.abs(tracker_ts[right] - safe_query),
    ) * 1000.0
    bracket_ms = (tracker_ts[right] - tracker_ts[left]) * 1000.0
    exact_right = (insertion < len(tracker_ts)) & np.isclose(
        safe_query,
        tracker_ts[right],
        rtol=0.0,
        atol=1e-12,
    )
    # An exact tracker sample requires no interpolation across the preceding
    # gap, so its effective bracket is zero.
    bracket_ms[exact_right] = 0.0
    within_range = finite_query & (safe_query >= tracker_ts[0]) & (safe_query <= tracker_ts[-1])
    valid = (
        timeline_valid
        & within_range
        & (nearest_ms <= max_nearest_gap_ms)
        & (bracket_ms <= max_bracket_gap_ms)
    )
    nearest_ms[~finite_query] = np.inf
    bracket_ms[~finite_query] = np.inf
    return nearest_ms, bracket_ms, valid


def interpolate_quat_xyzw(ts: np.ndarray, quat: np.ndarray, query_ts: np.ndarray) -> np.ndarray:
    out = np.empty((len(query_ts), 4), dtype=np.float64)
    indices = np.searchsorted(ts, query_ts, side="left")
    for i, idx in enumerate(indices):
        if idx <= 0:
            q = quat[0]
        elif idx >= len(ts):
            q = quat[-1]
        else:
            t0 = ts[idx - 1]
            t1 = ts[idx]
            q0 = quat[idx - 1]
            q1 = quat[idx]
            alpha = 0.0 if t1 <= t0 else float((query_ts[i] - t0) / (t1 - t0))
            dot = float(np.dot(q0, q1))
            if dot < 0.0:
                q1 = -q1
                dot = -dot
            dot = float(np.clip(dot, -1.0, 1.0))
            if dot > 0.9995:
                q = (1.0 - alpha) * q0 + alpha * q1
            else:
                theta = float(np.arccos(dot))
                sin_theta = float(np.sin(theta))
                q = (
                    np.sin((1.0 - alpha) * theta) / sin_theta * q0
                    + np.sin(alpha * theta) / sin_theta * q1
                )
        norm = float(np.linalg.norm(q))
        out[i] = q if norm <= 1e-12 else q / norm
    return out


def load_htc_source_for_frames(
    hand: HandInput,
    args: argparse.Namespace,
) -> tuple[PoseQuality, TemporalPoseSource]:
    if hand.tracker_path is None:
        raise ValueError(f"{hand.name} direct HTC mode requires tracker_path")
    if hand.timestamps_path is None:
        raise ValueError(f"{hand.name} direct HTC mode requires frame_timestamps.csv")
    timeline = load_frame_timeline(hand.timestamps_path)
    if timeline.clock_domain != "host_clock_monotonic_raw":
        raise RuntimeError(
            f"{hand.name} calibrated timeline is in {timeline.clock_domain}, not "
            "host_clock_monotonic_raw"
        )
    frame_ts = timeline.unix_ms / 1000.0
    htc_ts, htc_pos, htc_quat, tracker_report = load_htc_tracker_csv(
        hand.tracker_path,
        hand.tracker_role,
        args.htc_time_source,
        args.max_host_clock_offset_step_ms,
    )
    nearest_ms, bracket_ms, valid = htc_query_quality(
        htc_ts,
        frame_ts,
        timeline.valid,
        args.max_htc_nearest_gap_ms,
        args.max_htc_bracket_gap_ms,
    )
    _, _, hard_valid = htc_query_quality(
        htc_ts,
        frame_ts,
        timeline.valid,
        args.hard_gap_reject_ms,
        args.hard_gap_reject_ms,
    )
    before = int(np.sum(timeline.valid & (frame_ts < htc_ts[0])))
    after = int(np.sum(timeline.valid & (frame_ts > htc_ts[-1])))
    query_report = {
        "frame_count": int(len(frame_ts)),
        "valid_count": int(np.count_nonzero(valid)),
        "invalid_count": int(np.count_nonzero(~valid)),
        "timeline_invalid_count": int(np.count_nonzero(~timeline.valid)),
        "clamped_before": before,
        "clamped_after": after,
        "max_nearest_gap_ms": args.max_htc_nearest_gap_ms,
        "max_bracket_gap_ms": args.max_htc_bracket_gap_ms,
        "hard_gap_reject_ms": args.hard_gap_reject_ms,
        "hard_invalid_count": int(np.count_nonzero(timeline.valid & ~hard_valid)),
        "nearest_gap_ms": distribution_report(nearest_ms[timeline.valid]),
        "bracket_gap_ms": distribution_report(bracket_ms[timeline.valid]),
    }
    report = {**tracker_report, "queries": query_report}
    print(
        f"[htc] {hand.name}: frames={len(frame_ts)} tracker_rows={len(htc_ts)} "
        f"role={hand.tracker_role or 'all'} time_source={tracker_report['time_source']} "
        f"valid={query_report['valid_count']} invalid={query_report['invalid_count']} "
        f"clamped_before={before} clamped_after={after} "
        f"nearest_p95_ms={query_report['nearest_gap_ms']['p95']} "
        f"bracket_p95_ms={query_report['bracket_gap_ms']['p95']}"
    )
    return (
        PoseQuality(
            valid=valid,
            nearest_gap_ms=nearest_ms,
            bracket_gap_ms=bracket_ms,
            report=report,
            hard_valid=hard_valid,
        ),
        TemporalPoseSource(
            timestamps_sec=htc_ts,
            positions=htc_pos,
            quaternions_xyzw=htc_quat,
            pose_sensor=hand.pose_sensor,
            time_source=str(tracker_report["time_source"]),
        ),
    )


def load_slam_poses_for_frames(hand: HandInput, args: argparse.Namespace) -> list[np.ndarray]:
    poses: list[np.ndarray] = []
    last_pose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    invalid_rows = 0
    with hand.trajectory_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"frame_idx", "x", "y", "z", "qx", "qy", "qz", "qw", "valid"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise ValueError(f"{hand.trajectory_path} is missing SLAM columns: {missing_text}")
        for row in reader:
            frame_idx = int(row["frame_idx"])
            if frame_idx < len(poses):
                raise ValueError(f"{hand.trajectory_path} frame_idx is not strictly increasing: {frame_idx}")
            while len(poses) < frame_idx:
                poses.append(pose7_to_pose6(last_pose, hand.pose_sensor))
                invalid_rows += 1
            valid = str(row.get("valid", "")).strip() in {"1", "true", "True"}
            if valid:
                last_pose = [float(row[key]) for key in ("x", "y", "z", "qx", "qy", "qz", "qw")]
            else:
                invalid_rows += 1
            poses.append(pose7_to_pose6(last_pose, hand.pose_sensor))
    if len(poses) < 2:
        raise ValueError(f"{hand.trajectory_path} has fewer than two SLAM pose rows")
    print(f"[slam] {hand.name}: frames={len(poses)} invalid_or_missing={invalid_rows} source={hand.trajectory_path}")
    return poses


def load_quality_gated_gripper_widths(
    path: Path,
    column: str,
    missing_policy: str,
    quality: dict[str, Any] | None,
    *,
    arm: str,
) -> list[float | None]:
    """Load finite widths with an episode-local hold-last contract.

    Sparse ArUco detections are a sampling property, not a reason to discard a
    demonstration.  For every audited source segment, leading holes are filled
    from that segment's first valid width and later holes hold the previous
    valid width.  Segment boundaries are hard reset points, so a width can
    never leak from the preceding demonstration.  A segment with no valid
    in-range width remains fail-closed.
    """

    strict_gripper_quality = bool((quality or {}).get("enabled"))
    # Read blanks explicitly.  The generic helper's historical ``ffill`` mode
    # is intentionally not used here because it carries state across the whole
    # CSV and substitutes zero before the first detection.  Both behaviours
    # violate the per-episode continuous-width contract.
    grippers = load_gripper_widths(
        path,
        column,
        "skip" if strict_gripper_quality else missing_policy,
    )
    if strict_gripper_quality:
        filled_frames = 0
        leading_backfill_frames = 0
        forward_fill_frames = 0
        rejected_frames = 0
        min_width_m = float((quality.get("contract") or {}).get("min_width_m", 0.0))
        max_width_m = float((quality.get("contract") or {}).get("max_width_m", 0.11))
        for segment_index, evidence in sorted(
            (quality.get("segments") or {}).items()
        ):
            try:
                start_frame = int(evidence["start_frame"])
                end_frame = int(evidence["end_frame"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{arm} segment={segment_index} continuous gripper evidence "
                    "has no valid half-open source frame range"
                ) from exc
            if not (0 <= start_frame < end_frame <= len(grippers)):
                raise RuntimeError(
                    f"{arm} segment={segment_index} continuous gripper range "
                    f"[{start_frame},{end_frame}) is outside CSV rows={len(grippers)}"
                )
            hard_reasons = continuous_gripper_segment_hard_reasons(
                evidence,
                contract=quality.get("contract") or {},
            )
            if hard_reasons:
                grippers[start_frame:end_frame] = [None] * (end_frame - start_frame)
                rejected_frames += end_frame - start_frame
                continue

            segment_values = grippers[start_frame:end_frame]
            valid_offsets = [
                offset
                for offset, value in enumerate(segment_values)
                if value is not None
                and math.isfinite(float(value))
                and min_width_m <= float(value) <= max_width_m
            ]
            if not valid_offsets:
                raise RuntimeError(
                    f"{arm} segment={segment_index} has no finite in-range gripper "
                    "width; episode-local fill cannot be initialized"
                )
            invalid_offsets = [
                offset
                for offset, value in enumerate(segment_values)
                if value is not None
                and (
                    not math.isfinite(float(value))
                    or not (min_width_m <= float(value) <= max_width_m)
                )
            ]
            if invalid_offsets:
                raise RuntimeError(
                    f"{arm} segment={segment_index} contains out-of-range gripper "
                    f"widths; first_frame_idx={start_frame + invalid_offsets[0]}"
                )

            first_valid_offset = valid_offsets[0]
            first_valid = float(segment_values[first_valid_offset])
            for offset in range(first_valid_offset):
                segment_values[offset] = first_valid
            leading_backfill_frames += first_valid_offset
            filled_frames += first_valid_offset

            last_valid = first_valid
            for offset in range(first_valid_offset, len(segment_values)):
                value = segment_values[offset]
                if value is None:
                    segment_values[offset] = last_valid
                    forward_fill_frames += 1
                    filled_frames += 1
                else:
                    last_valid = float(value)
            grippers[start_frame:end_frame] = segment_values
        print(
            f"[gripper-quality] arm={arm} loaded_with_missing_policy=skip "
            "fill_policy=episode_local_leading_backfill_then_forward_fill "
            f"filled_frames={filled_frames} leading_backfill_frames={leading_backfill_frames} "
            f"forward_fill_frames={forward_fill_frames} "
            f"hard_rejected_frames_masked={rejected_frames}",
            flush=True,
        )
    return grippers


def load_hand_states(
    hand: HandInput,
    args: argparse.Namespace,
    *,
    video_frame_count_override: int | None = None,
    gripper_quality: dict[str, Any] | None = None,
) -> HandStates:
    temporal_pose_source: TemporalPoseSource | None = None
    if args.direct_from_raw:
        if hand.pose_sensor == "htc":
            pose_quality, temporal_pose_source = load_htc_source_for_frames(hand, args)
            poses = []
        else:
            poses = load_slam_poses_for_frames(hand, args)
            timeline = load_frame_timeline(hand.timestamps_path)
            pose_quality = PoseQuality(
                valid=timeline.valid.copy(),
                nearest_gap_ms=np.full(len(timeline.frame_idx), np.nan, dtype=np.float64),
                bracket_gap_ms=np.full(len(timeline.frame_idx), np.nan, dtype=np.float64),
                report={"pose_sensor": hand.pose_sensor, "htc_alignment": False},
                hard_valid=timeline.valid.copy(),
            )
    else:
        poses = load_tum_trajectory(hand.trajectory_path, hand.pose_sensor)
        pose_quality = PoseQuality(
            valid=np.ones(len(poses), dtype=bool),
            nearest_gap_ms=np.full(len(poses), np.nan, dtype=np.float64),
            bracket_gap_ms=np.full(len(poses), np.nan, dtype=np.float64),
            report={"pose_sensor": hand.pose_sensor, "htc_alignment": False},
            hard_valid=np.ones(len(poses), dtype=bool),
        )
    grippers = load_quality_gated_gripper_widths(
        hand.gripper_path,
        args.gripper_column,
        args.missing_gripper,
        gripper_quality,
        arm=hand.name,
    )
    timeline_count = None
    if hand.timestamps_path is not None:
        timeline_count = len(load_frame_timeline(hand.timestamps_path).frame_idx)
    if video_frame_count_override is not None:
        video_frames = int(video_frame_count_override)
    elif args.dry_run and timeline_count is not None:
        # Planning/audit mode must not trigger a full decoder/ffprobe frame
        # count over multi-hour videos.  The real conversion still verifies
        # every video against the source-retaining calibrated timeline.
        video_frames = timeline_count
        print(
            f"[dry-run] {hand.name}: using calibrated timeline frame count={video_frames}; "
            "video frame count will be verified during real conversion"
        )
    else:
        video_frames = count_hand_video_frames(hand)
    if video_frames <= 0:
        raise RuntimeError(f"{hand.name} image source has no frames")
    if timeline_count is not None:
        if timeline_count != video_frames and not args.allow_partial_inputs:
            raise RuntimeError(
                f"{hand.name} calibrated timeline/video length mismatch: "
                f"timestamps={timeline_count}, video_frames={video_frames}. "
                "A calibrated CSV must retain every original source frame."
            )
    return HandStates(
        poses=poses,
        grippers=grippers,
        video_frames=video_frames,
        pose_quality=pose_quality,
        temporal_pose_source=temporal_pose_source,
        pose_frame_count=(timeline_count if temporal_pose_source is not None else len(poses)),
    )


def bimanual_state(right_pose: np.ndarray, right_gripper: float, left_pose: np.ndarray, left_gripper: float) -> np.ndarray:
    return np.concatenate(
        [
            make_state(right_pose, right_gripper),
            make_state(left_pose, left_gripper),
        ]
    ).astype(np.float32)


def gripper_transform_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    """Return the exact transform applied to both gripper vector dimensions."""

    zero_threshold = getattr(args, "gripper_zero_below_m", None)
    binary_threshold = getattr(args, "gripper_binary_threshold_m", None)
    if zero_threshold is not None and binary_threshold is not None:
        raise ValueError(
            "--gripper-zero-below-m and --gripper-binary-threshold-m are mutually exclusive"
        )
    columns = {
        key: {
            "width": len(STATE_NAMES),
            "indices": {
                "right_gripper": STATE_NAMES.index("right_gripper"),
                "left_gripper": STATE_NAMES.index("left_gripper"),
            },
        }
        for key in ("observation.state", "action")
    }
    if binary_threshold is not None:
        return {
            "kind": "binarize_calibrated_gripper_width_at_threshold",
            "threshold_m": float(binary_threshold),
            "comparison": (
                "finite_width < threshold_m -> 0; "
                "finite_width >= threshold_m -> 1"
            ),
            "low_value": 0.0,
            "high_value": 1.0,
            "columns": columns,
        }
    if zero_threshold is not None:
        return {
            "kind": "zero_calibrated_gripper_width_below_threshold",
            "threshold_m": float(zero_threshold),
            "comparison": "finite_width < threshold_m",
            "replacement_m": 0.0,
            "columns": columns,
        }
    return None


def sample_hand_states_on_grid(
    states: HandStates,
    timeline: FrameTimeline,
    source_indices: np.ndarray,
    grid_unix_time_ms: np.ndarray,
    source_start_frame: int,
    source_end_frame: int,
    gripper_zero_below_m: float | None = None,
    gripper_binary_threshold_m: float | None = None,
) -> GridHandSamples:
    """Sample state on the shared clock; source indices are image provenance only."""

    source_indices = np.asarray(source_indices, dtype=np.int64)
    grid_ms = np.asarray(grid_unix_time_ms, dtype=np.float64)
    if len(source_indices) != len(grid_ms):
        raise ValueError("source_indices and grid_unix_time_ms must have equal length")
    if len(grid_ms) < 2:
        raise ValueError("shared state grid must have at least two samples")

    pose_source = states.temporal_pose_source
    if pose_source is not None:
        grid_sec = grid_ms / 1000.0
        if grid_sec[0] < pose_source.timestamps_sec[0] or grid_sec[-1] > pose_source.timestamps_sec[-1]:
            raise RuntimeError("shared state grid extends outside the HTC timestamp range")
        positions = np.column_stack(
            [
                np.interp(grid_sec, pose_source.timestamps_sec, pose_source.positions[:, axis])
                for axis in range(3)
            ]
        )
        quaternions = interpolate_quat_xyzw(
            pose_source.timestamps_sec,
            pose_source.quaternions_xyzw,
            grid_sec,
        )
        poses = [
            pose7_to_pose6([*position, *quaternion], pose_source.pose_sensor)
            for position, quaternion in zip(positions, quaternions)
        ]
        query_settings = states.pose_quality.report.get("queries") or {}
        nearest_ms, bracket_ms, htc_valid = htc_query_quality(
            pose_source.timestamps_sec,
            grid_sec,
            np.ones(len(grid_sec), dtype=bool),
            float(query_settings.get("max_nearest_gap_ms", 12.5)),
            float(query_settings.get("max_bracket_gap_ms", 25.0)),
        )
    else:
        poses = [states.poses[int(index)] for index in source_indices]
        nearest_ms = states.pose_quality.nearest_gap_ms[source_indices]
        bracket_ms = states.pose_quality.bracket_gap_ms[source_indices]
        htc_valid = states.pose_quality.valid[source_indices]

    upper = min(source_end_frame, len(timeline.frame_idx), len(states.grippers))
    available_indices = np.arange(max(0, source_start_frame), max(0, upper), dtype=np.int64)
    if len(available_indices):
        available_indices = available_indices[timeline.valid[available_indices]]
        available_indices = np.asarray(
            [index for index in available_indices if states.grippers[int(index)] is not None],
            dtype=np.int64,
        )
    if len(available_indices) < 2:
        raise RuntimeError("fewer than two timestamped gripper samples remain in the segment")
    gripper_times = timeline.unix_ms[available_indices]
    gripper_values = np.asarray(
        [float(states.grippers[int(index)]) for index in available_indices],
        dtype=np.float64,
    )
    insertion = np.searchsorted(gripper_times, grid_ms, side="left")
    before_pos = np.clip(insertion - 1, 0, len(gripper_times) - 1)
    after_pos = np.clip(insertion, 0, len(gripper_times) - 1)
    exact_after = (insertion < len(gripper_times)) & np.isclose(
        grid_ms,
        gripper_times[after_pos],
        rtol=0.0,
        atol=1e-6,
    )
    before_pos = np.where(exact_after, after_pos, before_pos)
    denominator = gripper_times[after_pos] - gripper_times[before_pos]
    alpha = np.divide(
        grid_ms - gripper_times[before_pos],
        denominator,
        out=np.zeros(len(grid_ms), dtype=np.float64),
        where=denominator > 0.0,
    )
    alpha = np.clip(alpha, 0.0, 1.0)
    grippers = (
        (1.0 - alpha) * gripper_values[before_pos]
        + alpha * gripper_values[after_pos]
    )
    if (
        gripper_zero_below_m is not None
        and gripper_binary_threshold_m is not None
    ):
        raise ValueError(
            "gripper zero and binary thresholds are mutually exclusive"
        )
    gripper_zeroed = 0
    gripper_binarized = 0
    if gripper_zero_below_m is not None:
        below = np.isfinite(grippers) & (grippers < gripper_zero_below_m)
        gripper_zeroed = int(np.count_nonzero(below & (grippers != 0.0)))
        grippers[below] = 0.0
    elif gripper_binary_threshold_m is not None:
        if not np.isfinite(grippers).all():
            raise RuntimeError(
                "cannot binarize non-finite calibrated gripper widths"
            )
        binary = np.where(
            grippers < gripper_binary_threshold_m,
            0.0,
            1.0,
        )
        gripper_binarized = int(np.count_nonzero(binary != grippers))
        grippers = binary
    return GridHandSamples(
        poses=poses,
        grippers=grippers.astype(np.float32),
        htc_nearest_gap_ms=np.asarray(nearest_ms, dtype=np.float64),
        htc_bracket_gap_ms=np.asarray(bracket_ms, dtype=np.float64),
        htc_valid=np.asarray(htc_valid, dtype=bool),
        gripper_before_frame_idx=available_indices[before_pos],
        gripper_after_frame_idx=available_indices[after_pos],
        gripper_interpolation_alpha=alpha.astype(np.float32),
        gripper_zeroed_below_threshold=gripper_zeroed,
        gripper_binarized_values=gripper_binarized,
    )


def start_end_pose_from_state(state: np.ndarray) -> np.ndarray:
    return np.concatenate([state[:6], state[10:16]]).astype(np.float32)


def load_unix_timestamps(hand: HandInput, limit: int) -> np.ndarray | None:
    if hand.timestamps_path is None or not hand.timestamps_path.exists():
        return None
    values = load_frame_timeline(hand.timestamps_path).unix_ms
    if len(values) < limit:
        return None
    return values[:limit].copy()


def build_segment_ranges(
    right: HandInput,
    left: HandInput,
    right_video_frames: int,
    left_video_frames: int,
    args: argparse.Namespace,
) -> list[SegmentRange]:
    if right.timestamps_path is None or left.timestamps_path is None:
        raise RuntimeError("Segment slicing requires frame_timestamps.csv for both hands")

    right_timeline = load_frame_timeline(right.timestamps_path)
    left_timeline = load_frame_timeline(left.timestamps_path)
    right_limit = min(len(right_timeline.frame_idx), right_video_frames)
    left_limit = min(len(left_timeline.frame_idx), left_video_frames)
    right_valid = right_timeline.valid_indices
    right_valid = right_valid[right_valid < right_limit]
    left_valid = left_timeline.valid_indices
    left_valid = left_valid[left_valid < left_limit]
    if len(right_valid) < 2 or len(left_valid) < 2:
        raise RuntimeError("Fewer than two timeline-valid frames remain for one or both hands")

    if not args.split_by_segments:
        return [
            SegmentRange(
                segment_index=0,
                start_unix_time_ms=float(
                    max(right_timeline.unix_ms[right_valid[0]], left_timeline.unix_ms[left_valid[0]])
                ),
                end_unix_time_ms=float(
                    min(right_timeline.unix_ms[right_valid[-1]], left_timeline.unix_ms[left_valid[-1]])
                ),
                right_start_frame=int(right_valid[0]),
                right_end_frame=int(right_valid[-1]) + 1,
                left_start_frame=int(left_valid[0]),
                left_end_frame=int(left_valid[-1]) + 1,
            )
        ]

    segment_log = (
        args.timestamp_log.expanduser().resolve()
        if args.timestamp_log is not None
        else timestamp_log_path(right)
    )
    right_active = build_shared_active_frame_ranges(
        load_shared_calibrated_frame_timeline(right.timestamps_path),
        segment_log,
        segment_time_offset_sec=args.segment_time_offset_sec,
        min_segment_frames=args.min_segment_frames,
        frame_limit=right_limit,
    )
    left_active = build_shared_active_frame_ranges(
        load_shared_calibrated_frame_timeline(left.timestamps_path),
        segment_log,
        segment_time_offset_sec=args.segment_time_offset_sec,
        min_segment_frames=args.min_segment_frames,
        frame_limit=left_limit,
    )
    if not right_active or not left_active:
        raise RuntimeError("No valid segment_start/segment_end pairs found in timestamp_log")
    left_by_segment = {item.segment_index: item for item in left_active}
    segments: list[SegmentRange] = []
    for right_range in right_active:
        left_range = left_by_segment.get(right_range.segment_index)
        if left_range is None:
            continue
        if not math.isclose(
            right_range.start_unix_time_ms,
            left_range.start_unix_time_ms,
            rel_tol=0.0,
            abs_tol=1e-9,
        ) or not math.isclose(
            right_range.end_unix_time_ms,
            left_range.end_unix_time_ms,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise RuntimeError(f"arm segment time mismatch for segment={right_range.segment_index}")
        segments.append(
            SegmentRange(
                segment_index=right_range.segment_index,
                start_unix_time_ms=right_range.start_unix_time_ms,
                end_unix_time_ms=right_range.end_unix_time_ms,
                right_start_frame=right_range.start_frame,
                right_end_frame=right_range.end_frame,
                left_start_frame=left_range.start_frame,
                left_end_frame=left_range.end_frame,
            )
        )
    if not segments:
        raise RuntimeError(
            "timestamp_log has segment_start/segment_end, but none overlap both camera timelines. "
            "Check frame_timestamps.csv and SEGMENT_TIME_OFFSET_SEC."
        )
    return segments


@lru_cache(maxsize=None)
def summarize_timestamp_log(hand: HandInput) -> str | None:
    if hand.raw_manifest_path is None or not hand.raw_manifest_path.exists():
        return None
    try:
        manifest = json.loads(hand.raw_manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    log_path = manifest.get("copied_sources", {}).get("timestamp_log")
    if not log_path:
        return None
    path = Path(log_path)
    if not path.exists():
        return None
    offsets: list[float] = []
    rtts: list[float] = []
    sync_deltas: list[float] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "windows_clock_sync":
                if row.get("externalClockOffsetMs") is not None:
                    offsets.append(float(row["externalClockOffsetMs"]))
                if row.get("externalRoundTripMs") is not None:
                    rtts.append(float(row["externalRoundTripMs"]))
            if row.get("type") == "camera_command_completed" and "syncTimeMsToCamera" in str(row.get("note", "")):
                if row.get("externalClockOffsetMs") is not None:
                    sync_deltas.append(float(row["externalClockOffsetMs"]))
    except Exception:
        return None
    parts = []
    if offsets:
        parts.append(f"windows_offset_ms_median={float(np.median(offsets)):.3f}")
    if rtts:
        parts.append(f"windows_rtt_ms_median={float(np.median(rtts)):.3f} max={max(rtts):.3f}")
    if sync_deltas:
        parts.append(f"camera_sync_delta_ms_median={float(np.median(sync_deltas)):.3f} max_abs={max(abs(v) for v in sync_deltas):.3f}")
    return ", ".join(parts) if parts else None


def build_frame_pairs(
    right: HandInput,
    left: HandInput,
    segment: SegmentRange,
    args: argparse.Namespace,
) -> FramePairResult:
    if right.timestamps_path is None or left.timestamps_path is None:
        raise RuntimeError("Bimanual alignment requires frame timestamps for both hands")
    right_timeline = load_frame_timeline(right.timestamps_path)
    left_timeline = load_frame_timeline(left.timestamps_path)
    right_indices = np.arange(segment.right_start_frame, segment.right_end_frame, dtype=np.int64)
    left_indices = np.arange(segment.left_start_frame, segment.left_end_frame, dtype=np.int64)
    right_indices = right_indices[right_timeline.valid[right_indices]]
    left_indices = left_indices[left_timeline.valid[left_indices]]
    if len(right_indices) < 2 or len(left_indices) < 2:
        raise RuntimeError(
            f"segment={segment.segment_index} has fewer than two timeline-valid frames "
            f"(right={len(right_indices)}, left={len(left_indices)})"
        )

    down_sample_steps = int(getattr(args, "down_sample_steps", 2))
    max_source_pairs = (
        None
        if args.max_frames_per_episode is None
        else int(args.max_frames_per_episode) * down_sample_steps + 1
    )

    if args.time_align_mode == "row_index":
        usable = min(len(right_indices), len(left_indices))
        if max_source_pairs is not None:
            usable = min(usable, max_source_pairs)
        pairs = [(int(right_indices[idx]), int(left_indices[idx])) for idx in range(usable)]
        grid_unix_time_ms = tuple(
            float(
                0.5
                * (
                    right_timeline.unix_ms[right_idx]
                    + left_timeline.unix_ms[left_idx]
                )
            )
            for right_idx, left_idx in pairs
        )
        return FramePairResult(
            pairs=pairs,
            report={
                "mode": "row_index",
                "segment_index": segment.segment_index,
                "pairs": len(pairs),
                "right_timeline_invalid": int(
                    (segment.right_end_frame - segment.right_start_frame) - len(right_indices)
                ),
                "left_timeline_invalid": int(
                    (segment.left_end_frame - segment.left_start_frame) - len(left_indices)
                ),
            },
            grid_unix_time_ms=grid_unix_time_ms,
        )

    right_unix = right_timeline.unix_ms[right_indices]
    left_unix = left_timeline.unix_ms[left_indices]

    fps = float(getattr(args, "fps", 60.0))
    if fps <= 0.0:
        raise ValueError("--fps must be positive")
    step_ms = 1000.0 / fps
    pairing_mode = str(
        getattr(args, "camera_pairing_mode", "right_anchor_nearest")
    ).strip().lower()
    if pairing_mode not in {"right_anchor_nearest", "uniform_grid_nearest"}:
        raise ValueError(f"Unsupported camera pairing mode: {pairing_mode!r}")

    def nearest_original_indices(
        source_indices: np.ndarray,
        source_unix: np.ndarray,
        query_unix: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        insertion = np.searchsorted(source_unix, query_unix, side="left")
        before = np.clip(insertion - 1, 0, len(source_unix) - 1)
        after = np.clip(insertion, 0, len(source_unix) - 1)
        before_error = np.abs(source_unix[before] - query_unix)
        after_error = np.abs(source_unix[after] - query_unix)
        # Prefer the earlier frame on an exact tie.  This makes reruns stable
        # across NumPy/FFmpeg versions and never looks into the future merely
        # because of a floating-point tie.
        selected = np.where(before_error <= after_error, before, after)
        original = source_indices[selected]
        error_ms = source_unix[selected] - query_unix
        return original.astype(np.int64, copy=False), error_ms

    if pairing_mode == "right_anchor_nearest":
        # The training schema and action vector define insta1/right as the
        # canonical first arm.  Preserve every valid right source frame once,
        # then pair the closest calibrated left frame.  Unlike independently
        # sampling both cameras on a 60-Hz grid, this cannot create synthetic
        # right-frame duplicates merely because X5 runs at 60000/1001 Hz.
        paired_right = right_indices
        if max_source_pairs is not None:
            paired_right = paired_right[:max_source_pairs]
        right_anchor_unix = right_timeline.unix_ms[paired_right]
        paired_left, left_anchor_errors_ms = nearest_original_indices(
            left_indices, left_unix, right_anchor_unix
        )
        # Interpolate HTC state at the midpoint of the two actual exposures,
        # rather than pretending that either independently clocked camera is
        # the exact shared observation time.
        grid_unix = 0.5 * (
            right_timeline.unix_ms[paired_right]
            + left_timeline.unix_ms[paired_left]
        )
        right_grid_errors_ms = right_timeline.unix_ms[paired_right] - grid_unix
        left_grid_errors_ms = left_timeline.unix_ms[paired_left] - grid_unix
        mode_report = "right_anchor_nearest_left"
    else:
        # Compatibility mode for already planned conversions.  Both cameras
        # are selected independently on a nominal grid and therefore may each
        # repeat a source frame as 60 Hz drifts against native 60000/1001 Hz.
        duration_ms = float(segment.end_unix_time_ms - segment.start_unix_time_ms)
        grid_count = max(0, int(np.ceil(duration_ms / step_ms - 1e-12)))
        grid_unix = segment.start_unix_time_ms + np.arange(grid_count, dtype=np.float64) * step_ms
        grid_unix = grid_unix[grid_unix < segment.end_unix_time_ms]
        if max_source_pairs is not None:
            grid_unix = grid_unix[:max_source_pairs]
        paired_right, right_grid_errors_ms = nearest_original_indices(
            right_indices, right_unix, grid_unix
        )
        paired_left, left_grid_errors_ms = nearest_original_indices(
            left_indices, left_unix, grid_unix
        )
        left_anchor_errors_ms = left_timeline.unix_ms[paired_left] - right_timeline.unix_ms[paired_right]
        mode_report = "uniform_time_grid_nearest_source"

    if len(grid_unix) < 2:
        raise RuntimeError(
            f"segment={segment.segment_index} has fewer than two aligned camera samples"
        )
    pairs = [
        (int(right_idx), int(left_idx))
        for right_idx, left_idx in zip(paired_right, paired_left)
    ]
    errors_ms = left_timeline.unix_ms[paired_left] - right_timeline.unix_ms[paired_right]

    if len(pairs) < 2:
        raise RuntimeError(
            f"Global-time alignment produced fewer than two valid pairs for segment={segment.segment_index}"
        )
    abs_errors_ms = np.abs(np.asarray(errors_ms, dtype=np.float64))
    max_abs = float(np.max(abs_errors_ms))
    right_reused = np.concatenate(([False], paired_right[1:] == paired_right[:-1]))
    left_reused = np.concatenate(([False], paired_left[1:] == paired_left[:-1]))
    pair_report = {
        "mode": mode_report,
        "segment_index": segment.segment_index,
        "source_alignment_fps": fps,
        "down_sample_steps": down_sample_steps,
        "nominal_lerobot_fps": output_dataset_fps(args),
        "uniform_grid_step_ms": step_ms if pairing_mode == "uniform_grid_nearest" else None,
        "right_anchor_preserves_source_frames": pairing_mode == "right_anchor_nearest",
        "pairs": len(pairs),
        "dropped_camera_pairing": 0,
        "right_timeline_invalid": int(
            (segment.right_end_frame - segment.right_start_frame) - len(right_indices)
        ),
        "left_timeline_invalid": int(
            (segment.left_end_frame - segment.left_start_frame) - len(left_indices)
        ),
        "first_valid_frame_diff_ms": float(left_unix[0] - right_unix[0]),
        "camera_pair_delta_ms": distribution_report(abs_errors_ms),
        "right_grid_error_ms": distribution_report(np.abs(right_grid_errors_ms)),
        "left_grid_error_ms": distribution_report(np.abs(left_grid_errors_ms)),
        "left_to_right_anchor_error_ms": distribution_report(np.abs(left_anchor_errors_ms)),
        "camera_pair_over_soft_threshold": int(
            np.count_nonzero(abs_errors_ms > args.max_camera_time_diff_ms)
        ),
        "right_frame_reused": int(np.count_nonzero(right_reused)),
        "left_frame_reused": int(np.count_nonzero(left_reused)),
        "first_pair": list(pairs[0]),
        "last_pair": list(pairs[-1]),
    }
    print(
        f"[sync] {pairing_mode} "
        f"segment={segment.segment_index} "
        f"fps={fps:g} pairs={len(pairs)} "
        f"first_valid_diff_ms={left_unix[0] - right_unix[0]:.3f} "
        f"dt_median_ms={float(np.median(errors_ms)):.3f} "
        f"dt_p95_ms={float(np.percentile(abs_errors_ms, 95)):.3f} "
        f"dt_max_abs_ms={max_abs:.3f} "
        f"reused_right={int(np.count_nonzero(right_reused))} "
        f"reused_left={int(np.count_nonzero(left_reused))} "
        f"first_pair={pairs[0]} last_pair={pairs[-1]}"
    )
    right_log_summary = summarize_timestamp_log(right)
    left_log_summary = summarize_timestamp_log(left)
    if right_log_summary:
        print(f"[sync] right_log {right_log_summary}")
    if left_log_summary:
        print(f"[sync] left_log {left_log_summary}")
    return FramePairResult(
        pairs=pairs,
        report=pair_report,
        grid_unix_time_ms=tuple(float(value) for value in grid_unix),
    )


def split_frame_pair_runs(
    segment: SegmentRange,
    pairs: list[tuple[int, int]],
    right_states: HandStates,
    left_states: HandStates,
    right_timeline: FrameTimeline,
    left_timeline: FrameTimeline,
    hard_gap_reject_ms: float,
    grid_unix_time_ms: tuple[float, ...] = (),
    hard_camera_time_diff_ms: float | None = None,
) -> tuple[list[FrameRun], dict[str, Any]]:
    """Accept one complete logical segment or reject it in full.

    A timestamp_log segment is a demonstration boundary, not a container for
    an arbitrary number of continuity runs.  Normal 59.94/60 Hz drift and
    short, calibration-accepted HTC brackets are represented with nearest
    source-frame reuse.  Conversely, a true hard gap rejects the complete
    segment so no LeRobot action can jump across missing time.
    """

    if hard_gap_reject_ms <= 0.0:
        raise ValueError("hard_gap_reject_ms must be positive")
    if hard_camera_time_diff_ms is not None and hard_camera_time_diff_ms <= 0.0:
        raise ValueError("hard_camera_time_diff_ms must be positive")
    hard_rejections: list[dict[str, Any]] = []

    def hard_mask(states: HandStates) -> np.ndarray:
        mask = states.pose_quality.hard_valid
        if mask is None:
            # Backwards-compatible conservative fallback for callers that do
            # not provide a separate hard-quality channel.
            mask = states.pose_quality.valid
        return np.asarray(mask, dtype=bool)

    right_hard = hard_mask(right_states)
    left_hard = hard_mask(left_states)

    def segment_source_quality(
        arm: str,
        start: int,
        end: int,
        timeline: FrameTimeline,
        states: HandStates,
        hard_valid: np.ndarray,
    ) -> dict[str, Any]:
        upper = min(
            end,
            len(timeline.frame_idx),
            states.pose_count,
            len(states.grippers),
            len(hard_valid),
        )
        indices = np.arange(max(0, start), max(0, upper), dtype=np.int64)
        indices = indices[timeline.valid[indices]] if len(indices) else indices
        times = timeline.unix_ms[indices]
        gaps = np.diff(times) if len(times) > 1 else np.asarray([], dtype=np.float64)
        max_gap = float(np.max(gaps)) if len(gaps) else 0.0
        hard_invalid_indices = indices[~hard_valid[indices]] if len(indices) else indices
        missing_gripper_indices = np.asarray(
            [idx for idx in indices if states.grippers[int(idx)] is None],
            dtype=np.int64,
        )
        if len(indices) < 2:
            hard_rejections.append(
                {"arm": arm, "reason": "fewer_than_two_usable_source_frames", "count": int(len(indices))}
            )
        if max_gap > hard_gap_reject_ms:
            position = int(np.argmax(gaps))
            hard_rejections.append(
                {
                    "arm": arm,
                    "reason": "camera_source_gap",
                    "gap_ms": max_gap,
                    "before_frame_idx": int(indices[position]),
                    "after_frame_idx": int(indices[position + 1]),
                }
            )
        if len(hard_invalid_indices):
            bracket_values = states.pose_quality.bracket_gap_ms[hard_invalid_indices]
            nearest_values = states.pose_quality.nearest_gap_ms[hard_invalid_indices]
            hard_rejections.append(
                {
                    "arm": arm,
                    "reason": "htc_hard_gap_or_out_of_range",
                    "count": int(len(hard_invalid_indices)),
                    "first_frame_idx": int(hard_invalid_indices[0]),
                    "max_htc_bracket_gap_ms": distribution_report(bracket_values)["max"],
                    "max_htc_nearest_gap_ms": distribution_report(nearest_values)["max"],
                }
            )
        if len(missing_gripper_indices):
            hard_rejections.append(
                {
                    "arm": arm,
                    "reason": "missing_gripper",
                    "count": int(len(missing_gripper_indices)),
                    "first_frame_idx": int(missing_gripper_indices[0]),
                }
            )
        soft_invalid = int(
            np.count_nonzero(~states.pose_quality.valid[indices]) if len(indices) else 0
        )
        return {
            "source_frames": int(len(indices)),
            "source_step_ms": distribution_report(gaps),
            "soft_invalid_pose_frames": soft_invalid,
            "hard_invalid_pose_frames": int(len(hard_invalid_indices)),
            "missing_gripper_frames": int(len(missing_gripper_indices)),
        }

    right_report = segment_source_quality(
        "right",
        segment.right_start_frame,
        segment.right_end_frame,
        right_timeline,
        right_states,
        right_hard,
    )
    left_report = segment_source_quality(
        "left",
        segment.left_start_frame,
        segment.left_end_frame,
        left_timeline,
        left_states,
        left_hard,
    )

    if grid_unix_time_ms:
        grid_sec = np.asarray(grid_unix_time_ms, dtype=np.float64) / 1000.0
        for arm, states in (("right", right_states), ("left", left_states)):
            pose_source = states.temporal_pose_source
            already_rejected = any(
                item.get("arm") == arm
                and item.get("reason") == "htc_hard_gap_or_out_of_range"
                for item in hard_rejections
            )
            if pose_source is None or already_rejected:
                continue
            nearest_ms, bracket_ms, grid_hard_valid = htc_query_quality(
                pose_source.timestamps_sec,
                grid_sec,
                np.ones(len(grid_sec), dtype=bool),
                hard_gap_reject_ms,
                hard_gap_reject_ms,
            )
            if not np.all(grid_hard_valid):
                invalid = ~grid_hard_valid
                hard_rejections.append(
                    {
                        "arm": arm,
                        "reason": "htc_grid_hard_gap_or_out_of_range",
                        "count": int(np.count_nonzero(invalid)),
                        "max_htc_bracket_gap_ms": distribution_report(bracket_ms[invalid])["max"],
                        "max_htc_nearest_gap_ms": distribution_report(nearest_ms[invalid])["max"],
                    }
                )

    out_of_range_pairs = 0
    non_monotonic_pairs = 0
    previous: tuple[int, int] | None = None
    for right_idx, left_idx in pairs:
        in_range = (
            0 <= right_idx < right_states.pose_count
            and 0 <= left_idx < left_states.pose_count
            and right_idx < len(right_hard)
            and left_idx < len(left_hard)
            and right_idx < len(right_states.grippers)
            and left_idx < len(left_states.grippers)
        )
        if not in_range:
            out_of_range_pairs += 1
            continue
        if previous is not None:
            if right_idx < previous[0] or left_idx < previous[1]:
                non_monotonic_pairs += 1
        previous = (right_idx, left_idx)

    if out_of_range_pairs:
        hard_rejections.append(
            {"reason": "pair_index_out_of_range", "count": out_of_range_pairs}
        )
    if non_monotonic_pairs:
        hard_rejections.append(
            {"reason": "source_frame_time_reversed", "count": non_monotonic_pairs}
        )
    if hard_camera_time_diff_ms is not None and not out_of_range_pairs:
        pair_delta_ms = np.asarray(
            [
                abs(left_timeline.unix_ms[left_idx] - right_timeline.unix_ms[right_idx])
                for right_idx, left_idx in pairs
            ],
            dtype=np.float64,
        )
        over = pair_delta_ms > hard_camera_time_diff_ms
        if np.any(over):
            hard_rejections.append(
                {
                    "reason": "camera_pair_time_difference",
                    "count": int(np.count_nonzero(over)),
                    "limit_ms": float(hard_camera_time_diff_ms),
                    "max_ms": float(np.max(pair_delta_ms)),
                    "first_pair_position": int(np.flatnonzero(over)[0]),
                }
            )
    if grid_unix_time_ms:
        if len(grid_unix_time_ms) != len(pairs):
            hard_rejections.append(
                {
                    "reason": "grid_pair_length_mismatch",
                    "grid_count": len(grid_unix_time_ms),
                    "pair_count": len(pairs),
                }
            )
        elif not out_of_range_pairs:
            right_grid_error = np.asarray(
                [abs(right_timeline.unix_ms[pair[0]] - grid_ms) for pair, grid_ms in zip(pairs, grid_unix_time_ms)],
                dtype=np.float64,
            )
            left_grid_error = np.asarray(
                [abs(left_timeline.unix_ms[pair[1]] - grid_ms) for pair, grid_ms in zip(pairs, grid_unix_time_ms)],
                dtype=np.float64,
            )
            if np.any(right_grid_error > hard_gap_reject_ms) or np.any(
                left_grid_error > hard_gap_reject_ms
            ):
                hard_rejections.append(
                    {
                        "reason": "grid_has_no_nearby_camera_frame",
                        "right_max_error_ms": float(np.max(right_grid_error)),
                        "left_max_error_ms": float(np.max(left_grid_error)),
                    }
                )

    if len(pairs) < 2:
        hard_rejections.append(
            {"reason": "fewer_than_two_aligned_pairs", "count": len(pairs)}
        )

    # Keep the rejection summary machine-countable: one normalized record per
    # arm/reason/detail, even if multiple validation paths observe the same
    # discontinuity.
    unique_rejections: list[dict[str, Any]] = []
    seen_rejections: set[str] = set()
    for rejection in hard_rejections:
        key = json.dumps(rejection, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if key not in seen_rejections:
            unique_rejections.append(rejection)
            seen_rejections.add(key)
    hard_rejections = unique_rejections

    accepted = not hard_rejections
    runs = (
        [
            FrameRun(
                segment.segment_index,
                0,
                pairs,
                tuple(grid_unix_time_ms),
            )
        ]
        if accepted
        else []
    )
    report = {
        "status": "accepted" if accepted else "rejected_hard_gap",
        "episode_semantics": "one_timestamp_log_segment_to_at_most_one_lerobot_episode",
        "input_pairs": len(pairs),
        "valid_runs": len(runs),
        "valid_pairs": len(pairs) if accepted else 0,
        "frames_to_write": max(0, len(pairs) - 1) if accepted else 0,
        "invalid_pose_pairs": int(
            sum(
                not (
                    right_states.pose_quality.valid[right_idx]
                    and left_states.pose_quality.valid[left_idx]
                )
                for right_idx, left_idx in pairs
                if right_idx < len(right_states.pose_quality.valid)
                and left_idx < len(left_states.pose_quality.valid)
            )
        ),
        "missing_gripper_pairs": int(
            sum(
                right_idx >= len(right_states.grippers)
                or left_idx >= len(left_states.grippers)
                or right_states.grippers[right_idx] is None
                or left_states.grippers[left_idx] is None
                for right_idx, left_idx in pairs
            )
        ),
        "source_discontinuities": int(
            sum(item.get("reason") == "camera_source_gap" for item in hard_rejections)
        ),
        "discarded_short_run_pairs": 0,
        "hard_gap_reject_ms": hard_gap_reject_ms,
        "hard_rejections": hard_rejections,
        "right": right_report,
        "left": left_report,
        "runs": [
            {
                "run_index": 0,
                "pair_count": len(pairs),
                "first_pair": list(pairs[0]),
                "last_pair": list(pairs[-1]),
            }
        ]
        if accepted
        else [],
    }
    return runs, report


def put_label(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int]) -> None:
    cv2 = get_cv2()
    global _PIL_FONT, _PIL_FONT_SMALL
    try:
        from PIL import Image, ImageDraw, ImageFont

        if _PIL_FONT is None:
            font_candidates = [
                "/System/Library/Fonts/SFNS.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
            ]
            font_path = next((path for path in font_candidates if Path(path).exists()), None)
            _PIL_FONT = ImageFont.truetype(font_path, 15) if font_path else ImageFont.load_default()
            _PIL_FONT_SMALL = ImageFont.truetype(font_path, 13) if font_path else ImageFont.load_default()

        x, y = org
        font = _PIL_FONT_SMALL if len(text) > 42 else _PIL_FONT
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)
        outline = (255, 255, 255) if sum(color) < 420 else (0, 0, 0)
        rgb_color = (int(color[2]), int(color[1]), int(color[0]))
        draw.text((x + 1, y + 1), text, font=font, fill=outline)
        draw.text((x, y), text, font=font, fill=rgb_color)
        img[:, :, :] = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        x, y = org
        font = cv2.FONT_HERSHEY_DUPLEX
        outline = (0, 0, 0) if sum(color) > 450 else (255, 255, 255)
        cv2.putText(img, text, (x + 1, y + 1), font, 0.48, outline, 2, cv2.LINE_AA)
        cv2.putText(img, text, (x, y), font, 0.48, color, 1, cv2.LINE_AA)


def annotate_tile(img: np.ndarray, title: str, color: tuple[int, int, int]) -> np.ndarray:
    cv2 = get_cv2()
    out = img.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 28), (0, 0, 0), -1)
    put_label(out, title, (8, 20), color)
    return out


def normalize_points(points: np.ndarray, box: tuple[int, int, int, int], pad: int = 12) -> np.ndarray:
    x0, y0, w, h = box
    if len(points) == 0:
        return np.zeros((0, 2), dtype=np.int32)
    pts = points.astype(np.float64)
    mins = np.nanmin(pts, axis=0)
    maxs = np.nanmax(pts, axis=0)
    span = np.maximum(maxs - mins, 1e-6)
    usable_w = max(1, w - pad * 2)
    usable_h = max(1, h - pad * 2)
    xy = np.empty((len(pts), 2), dtype=np.int32)
    xy[:, 0] = np.round(x0 + pad + (pts[:, 0] - mins[0]) / span[0] * usable_w).astype(np.int32)
    xy[:, 1] = np.round(y0 + h - pad - (pts[:, 1] - mins[1]) / span[1] * usable_h).astype(np.int32)
    return xy


def draw_polyline(
    canvas: np.ndarray,
    values: np.ndarray,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    current_idx: int,
) -> None:
    cv2 = get_cv2()
    x0, y0, w, h = box
    cv2.rectangle(canvas, (x0, y0), (x0 + w, y0 + h), (190, 190, 190), 1)
    if len(values) == 0:
        return
    vals = values.astype(np.float64)
    vmin = float(np.nanmin(vals))
    vmax = float(np.nanmax(vals))
    span = max(vmax - vmin, 1e-6)
    xs = np.linspace(x0 + 8, x0 + w - 8, len(vals))
    ys = y0 + h - 10 - (vals - vmin) / span * max(1, h - 20)
    pts = np.column_stack([xs, ys]).round().astype(np.int32)
    if len(pts) > 1:
        cv2.polylines(canvas, [pts], False, color, 2, cv2.LINE_AA)
    if 0 <= current_idx < len(pts):
        cv2.circle(canvas, tuple(pts[current_idx]), 4, color, -1, cv2.LINE_AA)


def project_world_xyz(points: np.ndarray) -> np.ndarray:
    """Project world XYZ to a readable 2D isometric view.

    World convention:
      right-handed frame with +Y = down:
      +X = right, +Y = down, +Z = forward.

    Screen convention:
      x grows right, y grows down.
    """
    pts = points.astype(np.float64)
    pts = rotate_world_y_180(points)
    return np.column_stack(
        [
            -pts[:, 0] - 0.45 * pts[:, 2],
            pts[:, 1] - 0.35 * pts[:, 2],
        ]
    )


def rotate_world_y_180(points: np.ndarray) -> np.ndarray:
    """Visualization-only mirror: R_y(pi), so X/Z flip and Y is unchanged."""
    pts = np.asarray(points, dtype=np.float64).copy()
    pts[:, 0] *= -1.0
    pts[:, 2] *= -1.0
    return pts


def make_world_projector(
    points_xyz: np.ndarray,
    box: tuple[int, int, int, int],
    pad: int = 32,
) -> tuple:
    projected = project_world_xyz(points_xyz)
    mins = np.nanmin(projected, axis=0)
    maxs = np.nanmax(projected, axis=0)
    span = np.maximum(maxs - mins, 1e-6)
    x0, y0, w, h = box
    usable_w = max(1, w - pad * 2)
    usable_h = max(1, h - pad * 2)

    def project(points: np.ndarray) -> np.ndarray:
        pts = project_world_xyz(np.asarray(points, dtype=np.float64))
        xy = np.empty((len(pts), 2), dtype=np.int32)
        xy[:, 0] = np.round(x0 + pad + (pts[:, 0] - mins[0]) / span[0] * usable_w).astype(np.int32)
        xy[:, 1] = np.round(y0 + h - pad - (pts[:, 1] - mins[1]) / span[1] * usable_h).astype(np.int32)
        return xy

    return project


def rotation_vector_to_matrix(rvec: np.ndarray) -> np.ndarray:
    cv2 = get_cv2()
    mat, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3, 1))
    return mat


def draw_3d_grid(
    canvas: np.ndarray,
    project,
    xyz_min: np.ndarray,
    xyz_max: np.ndarray,
) -> None:
    cv2 = get_cv2()
    x_vals = np.linspace(xyz_min[0], xyz_max[0], 5)
    y_vals = np.linspace(xyz_min[1], xyz_max[1], 5)
    z0 = xyz_min[2]
    grid_color = (210, 210, 210)
    for x in x_vals:
        pts = project(np.array([[x, xyz_min[1], z0], [x, xyz_max[1], z0]], dtype=np.float64))
        cv2.line(canvas, tuple(pts[0]), tuple(pts[1]), grid_color, 1, cv2.LINE_AA)
    for y in y_vals:
        pts = project(np.array([[xyz_min[0], y, z0], [xyz_max[0], y, z0]], dtype=np.float64))
        cv2.line(canvas, tuple(pts[0]), tuple(pts[1]), grid_color, 1, cv2.LINE_AA)

    # Vertical guide lines at the floor corners make the Z dimension visible.
    for x, y in [
        (xyz_min[0], xyz_min[1]),
        (xyz_min[0], xyz_max[1]),
        (xyz_max[0], xyz_min[1]),
        (xyz_max[0], xyz_max[1]),
    ]:
        pts = project(np.array([[x, y, xyz_min[2]], [x, y, xyz_max[2]]], dtype=np.float64))
        cv2.line(canvas, tuple(pts[0]), tuple(pts[1]), (225, 225, 225), 1, cv2.LINE_AA)


def draw_world_axes(canvas: np.ndarray, box: tuple[int, int, int, int]) -> None:
    cv2 = get_cv2()
    x0, y0, w, h = box
    origin = np.array([x0 + w - 74, y0 + h - 42], dtype=np.float64)
    axis_scale = 38.0
    base = project_world_xyz(np.array([[0.0, 0.0, 0.0]], dtype=np.float64))[0]
    axes = []
    for label, vec in (
        ("+X", np.array([[1.0, 0.0, 0.0]], dtype=np.float64)),
        ("+Y", np.array([[0.0, 1.0, 0.0]], dtype=np.float64)),
        ("+Z", np.array([[0.0, 0.0, 1.0]], dtype=np.float64)),
    ):
        direction = project_world_xyz(vec)[0] - base
        norm = float(np.linalg.norm(direction))
        if norm <= 1e-9:
            direction = np.array([1.0, 0.0], dtype=np.float64)
        else:
            direction = direction / norm
        axes.append((label, direction, (35, 35, 35)))
    for label, direction, color in axes:
        end = origin + direction * axis_scale
        cv2.arrowedLine(
            canvas,
            tuple(origin.round().astype(int)),
            tuple(end.round().astype(int)),
            color,
            1,
            cv2.LINE_AA,
            tipLength=0.22,
        )
        put_label(canvas, label, tuple((end + direction * 8).round().astype(int)), color)


def draw_pose_axes(
    canvas: np.ndarray,
    project,
    position: np.ndarray,
    rvec: np.ndarray,
    axis_len: float,
) -> None:
    cv2 = get_cv2()
    rot = rotation_vector_to_matrix(rvec)
    origin = np.asarray(position, dtype=np.float64)
    colors = [
        ("x", (80, 80, 255)),
        ("y", (80, 255, 80)),
        ("z", (255, 120, 80)),
    ]
    origin_px = project(origin.reshape(1, 3))[0]
    for axis_idx, (label, color) in enumerate(colors):
        end = origin + rot[:, axis_idx] * axis_len
        end_px = project(end.reshape(1, 3))[0]
        cv2.arrowedLine(canvas, tuple(origin_px), tuple(end_px), color, 2, cv2.LINE_AA, tipLength=0.28)
        put_label(canvas, label, tuple(end_px), color)

def draw_trajectory_panel(states: list[np.ndarray], current_idx: int, size: tuple[int, int], episode_index: int) -> np.ndarray:
    cv2 = get_cv2()
    width, height = size
    panel = np.full((height, width, 3), 255, dtype=np.uint8)
    put_label(panel, f"episode {episode_index:06d}  frame {current_idx + 1}/{len(states)}", (12, 24), (35, 35, 35))

    arr = np.asarray(states, dtype=np.float32)
    right_xyz = arr[:, 0:3]
    right_rvec = arr[:, 3:6]
    left_xyz = arr[:, 10:13]
    left_rvec = arr[:, 13:16]
    right_gripper = arr[:, 6]
    left_gripper = arr[:, 16]
    right_color = (60, 185, 255)
    left_color = (80, 235, 125)

    gripper_h = 58
    traj_box = (14, 42, width - 28, max(150, height - 42 - 2 * gripper_h - 58))
    cv2.rectangle(
        panel,
        (traj_box[0], traj_box[1]),
        (traj_box[0] + traj_box[2], traj_box[1] + traj_box[3]),
        (190, 190, 190),
        1,
    )
    put_label(panel, "3D right-handed grid: Y-axis 180 deg view, +Y down", (traj_box[0] + 8, traj_box[1] + 20), (35, 35, 35))

    all_xyz = np.vstack([right_xyz, left_xyz]).astype(np.float64)
    xyz_min = np.nanmin(all_xyz, axis=0)
    xyz_max = np.nanmax(all_xyz, axis=0)
    xyz_span = np.maximum(xyz_max - xyz_min, 1e-4)
    margin = np.maximum(xyz_span * 0.12, 0.03)
    xyz_min = xyz_min - margin
    xyz_max = xyz_max + margin
    corners = np.array(
        [
            [x, y, z]
            for x in (xyz_min[0], xyz_max[0])
            for y in (xyz_min[1], xyz_max[1])
            for z in (xyz_min[2], xyz_max[2])
        ],
        dtype=np.float64,
    )
    project = make_world_projector(corners, traj_box, pad=34)
    draw_3d_grid(panel, project, xyz_min, xyz_max)
    right_px = project(right_xyz)
    left_px = project(left_xyz)
    visible_end = min(current_idx + 1, len(states))
    if visible_end > 1:
        cv2.polylines(panel, [right_px[:visible_end]], False, right_color, 2, cv2.LINE_AA)
        cv2.polylines(panel, [left_px[:visible_end]], False, left_color, 2, cv2.LINE_AA)

    cv2.circle(panel, tuple(right_px[0]), 4, right_color, -1, cv2.LINE_AA)
    put_label(panel, "R start", tuple(right_px[0] + np.array([6, -4])), right_color)
    cv2.circle(panel, tuple(left_px[0]), 4, left_color, -1, cv2.LINE_AA)
    put_label(panel, "L start", tuple(left_px[0] + np.array([6, 14])), left_color)

    if current_idx >= len(states) - 1:
        cv2.rectangle(panel, tuple(right_px[-1] - 4), tuple(right_px[-1] + 4), right_color, -1)
        put_label(panel, "R end", tuple(right_px[-1] + np.array([6, -4])), right_color)
        cv2.rectangle(panel, tuple(left_px[-1] - 4), tuple(left_px[-1] + 4), left_color, -1)
        put_label(panel, "L end", tuple(left_px[-1] + np.array([6, 14])), left_color)

    cv2.circle(panel, tuple(right_px[current_idx]), 5, right_color, -1, cv2.LINE_AA)
    cv2.circle(panel, tuple(left_px[current_idx]), 5, left_color, -1, cv2.LINE_AA)
    axis_len = float(max(np.nanmax(xyz_span) * 0.12, 0.035))
    draw_pose_axes(panel, project, right_xyz[current_idx], right_rvec[current_idx], axis_len)
    draw_pose_axes(panel, project, left_xyz[current_idx], left_rvec[current_idx], axis_len)
    put_label(panel, "RIGHT arm", (traj_box[0] + 10, traj_box[1] + traj_box[3] - 28), right_color)
    put_label(panel, "LEFT arm", (traj_box[0] + 112, traj_box[1] + traj_box[3] - 28), left_color)
    draw_world_axes(panel, traj_box)

    right_g_box = (14, traj_box[1] + traj_box[3] + 26, width - 28, gripper_h)
    left_g_box = (14, right_g_box[1] + right_g_box[3] + 26, width - 28, gripper_h)
    put_label(panel, f"RIGHT gripper width: {right_gripper[current_idx]:.3f} m", (right_g_box[0], right_g_box[1] - 5), right_color)
    draw_polyline(panel, right_gripper, right_g_box, right_color, current_idx)
    put_label(panel, f"LEFT gripper width: {left_gripper[current_idx]:.3f} m", (left_g_box[0], left_g_box[1] - 5), left_color)
    draw_polyline(panel, left_gripper, left_g_box, left_color, current_idx)
    return panel


def maybe_write_combined_video(
    episode_index: int,
    right: HandInput,
    left: HandInput,
    states: list[np.ndarray],
    pairs: list[tuple[int, int]],
    args: argparse.Namespace,
) -> None:
    if args.preview_every_n <= 0 or episode_index % args.preview_every_n != 0 or not states:
        return
    if right.camera_paths is None or left.camera_paths is None:
        raise RuntimeError("preview generation requires legacy six-video inputs")
    cv2 = get_cv2()
    out_dir = args.output_root.expanduser().resolve() / "combined_videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"episode_{episode_index:06d}.mp4"
    tile = (args.image_width, args.image_height)
    video_grid_size = (tile[0] * 3, tile[1] * 2)
    panel_size = (max(tile[0] * 2, 360), video_grid_size[1])
    output_size = (video_grid_size[0] + panel_size[0], video_grid_size[1])
    stride = max(1, int(args.preview_stride))
    writer = cv2.VideoWriter(
        str(out_path),
        cv2.VideoWriter_fourcc(*args.preview_fourcc),
        max(1.0, float(output_dataset_fps(args)) / float(stride)),
        output_size,
        True,
    )
    if not writer.isOpened():
        raise RuntimeError(f"Could not open preview video writer: {out_path}")

    frame_iter = iter_bimanual_frames_by_pairs(right.camera_paths, left.camera_paths, pairs, tile)
    try:
        for pair_pos, bundle in enumerate(frame_iter):
            if pair_pos >= len(states):
                break
            if pair_pos % stride != 0:
                continue
            row1 = np.hstack(
                [
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.right_left_view"], cv2.COLOR_RGB2BGR),
                        "RIGHT  left-view",
                        (60, 185, 255),
                    ),
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.right_wrist"], cv2.COLOR_RGB2BGR),
                        "RIGHT  wrist/front",
                        (60, 185, 255),
                    ),
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.right_right_view"], cv2.COLOR_RGB2BGR),
                        "RIGHT  right-view",
                        (60, 185, 255),
                    ),
                ]
            )
            row2 = np.hstack(
                [
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.left_left_view"], cv2.COLOR_RGB2BGR),
                        "LEFT  left-view",
                        (80, 235, 125),
                    ),
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.left_wrist"], cv2.COLOR_RGB2BGR),
                        "LEFT  wrist/front",
                        (80, 235, 125),
                    ),
                    annotate_tile(
                        cv2.cvtColor(bundle["observation.images.left_right_view"], cv2.COLOR_RGB2BGR),
                        "LEFT  right-view",
                        (80, 235, 125),
                    ),
                ]
            )
            video_grid = np.vstack([row1, row2])
            panel = draw_trajectory_panel(states, pair_pos, panel_size, episode_index)
            writer.write(np.hstack([video_grid, panel]))
    finally:
        writer.release()
    print(f"[preview] wrote {out_path}")


def remove_output_root(path: Path, overwrite: bool) -> None:
    if not path.exists():
        return
    if not overwrite:
        raise FileExistsError(f"Output root already exists: {path}. Use --overwrite to replace it.")
    shutil.rmtree(path)


def discard_output_backup(path: Path) -> None:
    """Delete only a backup created by this process after safe publication."""

    backup = path.expanduser().absolute()
    if backup.is_symlink() or not backup.is_dir():
        raise RuntimeError(f"refusing to discard unsafe overwrite backup: {backup}")
    shutil.rmtree(backup)
    fsync_directory(backup.parent)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unique_output_sibling(output_root: Path, label: str) -> Path:
    output = output_root.expanduser().absolute()
    parent = output.parent.resolve(strict=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return parent / f".{output.name}.{label}-{timestamp}-{uuid.uuid4().hex[:8]}"


def exchange_output_with_backup(candidate: Path, final: Path) -> Path:
    """Install a complete sibling candidate without ever removing ``final``."""

    from fast_workspace import atomic_exchange_directories

    candidate = candidate.expanduser().absolute()
    final = final.expanduser().absolute()
    backup = unique_output_sibling(final, "backup")
    atomic_exchange_directories(candidate, final)
    # After the exchange, final is the complete new generation and candidate
    # is the complete old one.  Persist that atomic name swap before giving the
    # old generation its durable backup name.
    fsync_directory(final.parent)
    candidate.rename(backup)
    fsync_directory(final.parent)
    return backup


def publish_fast_workspace_output(
    fast_workspace: Any,
    completed_output: Path,
    requested_output: Path,
    *,
    overwrite: bool,
    reserve_bytes: int,
) -> tuple[Any, Path | None]:
    """Publish from fast storage, preserving an existing final during copying."""

    requested_output = requested_output.expanduser().absolute()
    if requested_output.exists() or requested_output.is_symlink():
        if not overwrite:
            raise FileExistsError(f"publish target already exists: {requested_output}")
        candidate = unique_output_sibling(requested_output, "candidate")
        result = fast_workspace.publish(
            completed_output,
            candidate,
            reserve_bytes=reserve_bytes,
            logical_final_directory=requested_output,
        )
        backup = exchange_output_with_backup(candidate, requested_output)
        result = replace(
            result,
            destination=requested_output,
            manifest=requested_output / result.manifest.name,
        )
        return result, backup
    result = fast_workspace.publish(
        completed_output,
        requested_output,
        reserve_bytes=reserve_bytes,
    )
    return result, None


def remove_embedded_episode_pngs(dataset, episode_index: int) -> None:
    """Remove temporary PNGs after their bytes have been embedded in Parquet."""
    for key in dataset.meta.image_keys:
        image_dir = dataset._get_image_file_path(
            episode_index=episode_index,
            image_key=key,
            frame_index=0,
        ).parent
        if image_dir.is_dir():
            shutil.rmtree(image_dir)
    images_root = dataset.root / "images"
    if images_root.is_dir() and not any(images_root.iterdir()):
        images_root.rmdir()


def preflight_episode_output_space(
    destination: Path,
    *,
    remaining_frames: int,
    image_width: int,
    image_height: int,
    reserve_gb: float,
    image_feature_count: int = len(CAMERA_KEYS),
) -> dict[str, int]:
    """Reserve a conservative worst-case bound for embedded lossless PNGs."""

    from fast_workspace import require_free_space

    if remaining_frames <= 0:
        return {"remaining_frames": 0, "required_bytes": 0}
    raw_image_bytes = int(image_width) * int(image_height) * 3
    # Deflate overhead plus PNG chunks are small, but use a generous fixed
    # margin per image and another 2 KiB per row for Arrow/numeric metadata.
    per_image_bound = raw_image_bytes + max(4096, raw_image_bytes // 100)
    required_bytes = int(remaining_frames) * (
        int(image_feature_count) * per_image_bound + 2048
    ) + 1024**3
    require_free_space(
        destination,
        required_bytes,
        reserve_bytes=int(float(reserve_gb) * 1024**3),
    )
    return {
        "remaining_frames": int(remaining_frames),
        "required_bytes": required_bytes,
        "image_feature_count": int(image_feature_count),
    }


class ParquetImageValues:
    """Lazy, row-group-cached image column used by exact episode statistics."""

    def __init__(self, parquet_path: Path, column: str):
        import pyarrow.parquet as pq

        self._parquet = pq.ParquetFile(parquet_path)
        self._column = column
        self._starts: list[int] = []
        total = 0
        for index in range(self._parquet.metadata.num_row_groups):
            self._starts.append(total)
            total += int(self._parquet.metadata.row_group(index).num_rows)
        self._length = total
        self._cached_group = -1
        self._cached_values = None

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int):
        if index < 0:
            index += self._length
        if index < 0 or index >= self._length:
            raise IndexError(index)
        group = bisect.bisect_right(self._starts, index) - 1
        if group != self._cached_group:
            self._cached_values = self._parquet.read_row_group(
                group, columns=[self._column]
            ).column(0).combine_chunks()
            self._cached_group = group
        assert self._cached_values is not None
        return self._cached_values[index - self._starts[group]].as_py()


def compute_episode_stats_from_parquet(
    parquet_path: Path,
    features: dict[str, dict[str, Any]],
    *,
    fps: int,
) -> dict[str, Any]:
    """Reproduce LeRobot episode stats without retaining every image byte."""

    import pyarrow as pa
    import pyarrow.parquet as pq
    from lerobot.datasets.compute_stats import compute_episode_stats

    numeric_keys = [
        key
        for key, feature in features.items()
        if feature["dtype"] not in {"image", "video", "string"}
    ]
    numeric_table = pq.read_table(parquet_path, columns=numeric_keys)
    episode_data: dict[str, Any] = {}
    for key, feature in features.items():
        if feature["dtype"] in {"image", "video"}:
            episode_data[key] = ParquetImageValues(parquet_path, key)
            continue
        if feature["dtype"] == "string":
            episode_data[key] = []
            continue
        array = numeric_table.column(key).combine_chunks()
        if key == "timestamp":
            # Historical LeRobot computes stats from Python float timestamps
            # before Arrow casts the stored column to float32.
            episode_data[key] = np.arange(len(array), dtype=np.float64) / float(fps)
            continue
        if pa.types.is_fixed_size_list(array.type):
            flat = array.values.to_numpy(zero_copy_only=False)
            episode_data[key] = np.asarray(flat).reshape(len(array), array.type.list_size)
        else:
            episode_data[key] = np.asarray(array.to_numpy(zero_copy_only=False))
    return compute_episode_stats(episode_data, features)


class BoundedEpisodeParquetWriter:
    """Append bounded Arrow batches to one unchanged logical LeRobot episode."""

    def __init__(self, dataset: Any, episode_index: int, batch_frames: int):
        import pyarrow.parquet as pq

        if batch_frames <= 0:
            raise ValueError("episode batch size must be positive")
        self.dataset = dataset
        self.episode_index = int(episode_index)
        self.batch_frames = int(batch_frames)
        self.path = dataset.root / dataset.meta.get_data_file_path(ep_index=episode_index)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.partial = self.path.with_suffix(self.path.suffix + ".streaming.tmp")
        self.partial.unlink(missing_ok=True)
        self._pq = pq
        self._writer = None
        self.rows = 0
        self.tasks: list[str] = []
        self._finished = False

    def should_flush(self) -> bool:
        return int(self.dataset.episode_buffer.get("size", 0)) >= self.batch_frames

    def flush(self) -> None:
        batch_size = int(self.dataset.episode_buffer.get("size", 0))
        if batch_size <= 0:
            return
        table, batch_tasks = self.dataset.pop_episode_batch_table(
            episode_frame_offset=self.rows
        )
        expected_schema = self.dataset.hf_features.arrow_schema
        if not table.schema.equals(expected_schema, check_metadata=False):
            raise RuntimeError("streaming Arrow batch schema does not match LeRobot features")
        if self._writer is None:
            self._writer = self._pq.ParquetWriter(
                self.partial,
                table.schema,
                compression="snappy",
            )
        self._writer.write_table(table, row_group_size=len(table))
        self.rows += len(table)
        for task in batch_tasks:
            if task not in self.tasks:
                self.tasks.append(task)

    def finish(self, expected_rows: int) -> dict[str, Any]:
        buffered_rows = int(self.dataset.episode_buffer.get("size", 0))
        if self._writer is None and self.rows == 0 and buffered_rows == int(expected_rows):
            # The common short-episode path is already bounded by batch_frames;
            # retain LeRobot's in-memory stats computation and avoid a needless
            # Parquet reread.  Longer episodes use the row-group path below.
            self.dataset.save_episode(commit_info=False)
            self.rows = int(expected_rows)
            self.tasks = list(self.dataset.meta.episodes[self.episode_index]["tasks"])
            self._finished = True
            return self.dataset.meta.episodes_stats[self.episode_index]
        self.flush()
        if self._writer is None:
            raise RuntimeError("cannot finish an empty streaming episode")
        self._writer.close()
        self._writer = None
        if self.rows != int(expected_rows):
            raise RuntimeError(f"streaming episode row mismatch: {self.rows} != {expected_rows}")
        descriptor = os.open(self.partial, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.partial.replace(self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        stats = compute_episode_stats_from_parquet(
            self.path,
            self.dataset.features,
            fps=self.dataset.fps,
        )
        self.dataset.meta.save_episode(
            self.episode_index,
            self.rows,
            self.tasks,
            stats,
            commit_info=False,
        )
        self.dataset.episode_buffer = self.dataset.create_episode_buffer()
        self.dataset._episode_frame_offset = 0
        self._finished = True
        return stats

    def abort(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if not self._finished:
            self.partial.unlink(missing_ok=True)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def configure_fast_work_root(args: argparse.Namespace) -> None:
    """Keep generated caches with the raw session or exported dataset.

    Large INSV/panorama caches must never silently fall back to the user's Home
    filesystem.  Public launchers pass an explicit per-session path; this
    resolver also makes direct Python invocations safe and fail-closed.
    """

    output_root = args.output_root.expanduser().resolve()
    export_parent = output_root.parent
    raw_roots: list[Path] = []

    session_dir = getattr(args, "session_dir", None)
    if session_dir is not None:
        raw_roots.append(session_dir.expanduser().resolve())
    if env_input := os.environ.get("INPUT_DIR"):
        raw_roots.append(Path(env_input).expanduser().resolve())

    tmp_root = args.tmp_root.expanduser().resolve()
    # In direct conversion TMP_ROOT is SESSION/tmp.  Keep this fallback for a
    # direct Python invocation that did not provide --session-dir/INPUT_DIR.
    if getattr(args, "direct_from_raw", False):
        raw_roots.append(tmp_root.parent)

    requested = getattr(args, "fast_work_root", None)
    if requested is None:
        cache_parent = raw_roots[0] if raw_roots else export_parent
        requested = cache_parent / "instaumi-dataprocess-cache"
    cache_root = Path(requested).expanduser().resolve()

    home_root = Path.home().resolve()
    allow_home_fast_work_root = bool(
        getattr(args, "allow_home_fast_work_root", False)
    )
    if (
        not allow_home_fast_work_root
        and (cache_root == home_root or is_within(cache_root, home_root))
    ):
        raise ValueError(
            f"--fast-work-root must not be stored under HOME: {cache_root}; "
            "place it under the raw session or beside the exported dataset"
        )
    trusted_internal_shard = is_trusted_episode_shard_output(args, output_root)
    if not trusted_internal_shard and (
        cache_root == output_root
        or is_within(cache_root, output_root)
        or is_within(output_root, cache_root)
    ):
        raise ValueError(
            f"--fast-work-root must not overlap the exported dataset: {cache_root}"
        )

    allowed_parents = [*raw_roots, export_parent]
    if not any(
        cache_root != parent and is_within(cache_root, parent)
        for parent in allowed_parents
    ):
        raise ValueError(
            "--fast-work-root must live under the raw session or export parent: "
            f"cache={cache_root}, allowed={[str(path) for path in allowed_parents]}"
        )
    args.fast_work_root = cache_root


def is_trusted_episode_shard_output(
    args: argparse.Namespace,
    output_root: Path,
) -> bool:
    """Accept only the exact internal shard root generated by the parent.

    These attributes are attached to an in-memory Namespace immediately before
    ``multiprocessing.spawn``; they are deliberately not command-line options.
    A normal conversion therefore cannot use this exception to place an
    arbitrary dataset below INPUT_DIR.
    """

    spec = getattr(args, "_episode_shard_spec", None)
    allowed_group = getattr(args, "_episode_shard_allowed_group", None)
    if spec is None or allowed_group is None:
        return False
    expected_name = (
        f"shard-{int(spec.shard_index):03d}-"
        f"ep-{int(spec.episode_start):06d}-{int(spec.episode_end):06d}-"
        f"{str(spec.assignment_hash)[:12]}"
    )
    return (
        output_root.name == expected_name
        and output_root.parent == Path(allowed_group).expanduser().resolve()
    )


def validate_output_root(args: argparse.Namespace) -> None:
    output_root = args.output_root.expanduser().resolve()
    tmp_root = args.tmp_root.expanduser().resolve()
    if output_root == Path("/") or output_root == tmp_root or is_within(output_root, tmp_root) or is_within(tmp_root, output_root):
        raise ValueError(f"unsafe output root overlapping TMP_ROOT: {output_root}")

    for input_root in (args.right_input_root, args.left_input_root):
        if input_root is None:
            continue
        source_root = input_root.expanduser().resolve()
        if output_root == source_root or is_within(output_root, source_root) or is_within(source_root, output_root):
            raise ValueError(f"unsafe output root overlapping converter input: {output_root}")

    if env := os.environ.get("INPUT_DIR"):
        session_dir = Path(env).expanduser().resolve()
        if output_root == session_dir or is_within(session_dir, output_root):
            raise ValueError(f"output root must not replace or contain the capture session: {session_dir}")
        if is_within(output_root, session_dir):
            allowed_name = output_root.name == "lerobot" or "_lerobot" in output_root.name
            allowed_final_output = output_root.parent == session_dir and allowed_name
            allowed_internal_shard = is_trusted_episode_shard_output(args, output_root)
            if not allowed_final_output and not allowed_internal_shard:
                raise ValueError(
                    "a dataset inside INPUT_DIR must be the top-level lerobot or *_lerobot* directory: "
                    f"{output_root}"
                )


SOURCE_MAPPING_FIELDS = [
    "episode_index",
    "episode_frame_index",
    "segment_index",
    "run_index",
    "grid_unix_time_ms",
    "right_frame_idx",
    "left_frame_idx",
    "right_frame_reused",
    "left_frame_reused",
    "right_gripper_before_frame_idx",
    "right_gripper_after_frame_idx",
    "right_gripper_interpolation_alpha",
    "left_gripper_before_frame_idx",
    "left_gripper_after_frame_idx",
    "left_gripper_interpolation_alpha",
    "action_right_frame_idx",
    "action_left_frame_idx",
    "right_unix_time_ms",
    "left_unix_time_ms",
    "camera_delta_ms",
    "right_htc_nearest_gap_ms",
    "left_htc_nearest_gap_ms",
    "right_htc_bracket_gap_ms",
    "left_htc_bracket_gap_ms",
    "right_htc_valid",
    "left_htc_valid",
]


def source_feature_values(
    right_idx: int,
    left_idx: int,
    segment_index: int,
    previous_pair: tuple[int, int] | None,
    state_pos: int,
    grid_unix_time_ms: float,
    right_timeline: FrameTimeline,
    left_timeline: FrameTimeline,
    right_grid: GridHandSamples,
    left_grid: GridHandSamples,
) -> dict[str, np.ndarray]:
    return {
        "source.camera_unix_time_ms": np.asarray(
            [right_timeline.unix_ms[right_idx], left_timeline.unix_ms[left_idx]],
            dtype=np.float64,
        ),
        "source.camera_frame_index": np.asarray([right_idx, left_idx], dtype=np.int64),
        "source.htc_nearest_gap_ms": np.asarray(
            [
                right_grid.htc_nearest_gap_ms[state_pos],
                left_grid.htc_nearest_gap_ms[state_pos],
            ],
            dtype=np.float32,
        ),
        "source.htc_bracket_gap_ms": np.asarray(
            [
                right_grid.htc_bracket_gap_ms[state_pos],
                left_grid.htc_bracket_gap_ms[state_pos],
            ],
            dtype=np.float32,
        ),
        "source.htc_valid": np.asarray(
            [right_grid.htc_valid[state_pos], left_grid.htc_valid[state_pos]],
            dtype=np.uint8,
        ),
        "source.segment_index": np.asarray([segment_index], dtype=np.int64),
        "source.frame_reused": np.asarray(
            [
                previous_pair is not None and right_idx == previous_pair[0],
                previous_pair is not None and left_idx == previous_pair[1],
            ],
            dtype=np.uint8,
        ),
        "source.state_unix_time_ms": np.asarray(
            [grid_unix_time_ms, grid_unix_time_ms],
            dtype=np.float64,
        ),
        "source.gripper_interpolation_alpha": np.asarray(
            [
                right_grid.gripper_interpolation_alpha[state_pos],
                left_grid.gripper_interpolation_alpha[state_pos],
            ],
            dtype=np.float32,
        ),
    }


def source_mapping_row(
    episode_index: int,
    episode_frame_index: int,
    run: FrameRun,
    pair: tuple[int, int],
    action_pair: tuple[int, int],
    grid_unix_time_ms: float,
    previous_pair: tuple[int, int] | None,
    state_pos: int,
    right_timeline: FrameTimeline,
    left_timeline: FrameTimeline,
    right_grid: GridHandSamples,
    left_grid: GridHandSamples,
) -> dict[str, Any]:
    right_idx, left_idx = pair
    action_right_idx, action_left_idx = action_pair
    right_ms = float(right_timeline.unix_ms[right_idx])
    left_ms = float(left_timeline.unix_ms[left_idx])
    return {
        "episode_index": episode_index,
        "episode_frame_index": episode_frame_index,
        "segment_index": run.segment_index,
        "run_index": run.run_index,
        "grid_unix_time_ms": f"{grid_unix_time_ms:.6f}",
        "right_frame_idx": right_idx,
        "left_frame_idx": left_idx,
        "right_frame_reused": int(
            previous_pair is not None and right_idx == previous_pair[0]
        ),
        "left_frame_reused": int(
            previous_pair is not None and left_idx == previous_pair[1]
        ),
        "right_gripper_before_frame_idx": int(right_grid.gripper_before_frame_idx[state_pos]),
        "right_gripper_after_frame_idx": int(right_grid.gripper_after_frame_idx[state_pos]),
        "right_gripper_interpolation_alpha": (
            f"{float(right_grid.gripper_interpolation_alpha[state_pos]):.9f}"
        ),
        "left_gripper_before_frame_idx": int(left_grid.gripper_before_frame_idx[state_pos]),
        "left_gripper_after_frame_idx": int(left_grid.gripper_after_frame_idx[state_pos]),
        "left_gripper_interpolation_alpha": (
            f"{float(left_grid.gripper_interpolation_alpha[state_pos]):.9f}"
        ),
        "action_right_frame_idx": action_right_idx,
        "action_left_frame_idx": action_left_idx,
        "right_unix_time_ms": f"{right_ms:.6f}",
        "left_unix_time_ms": f"{left_ms:.6f}",
        "camera_delta_ms": f"{left_ms - right_ms:.6f}",
        "right_htc_nearest_gap_ms": f"{float(right_grid.htc_nearest_gap_ms[state_pos]):.6f}",
        "left_htc_nearest_gap_ms": f"{float(left_grid.htc_nearest_gap_ms[state_pos]):.6f}",
        "right_htc_bracket_gap_ms": f"{float(right_grid.htc_bracket_gap_ms[state_pos]):.6f}",
        "left_htc_bracket_gap_ms": f"{float(left_grid.htc_bracket_gap_ms[state_pos]):.6f}",
        "right_htc_valid": int(bool(right_grid.htc_valid[state_pos])),
        "left_htc_valid": int(bool(left_grid.htc_valid[state_pos])),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def validate_written_episode_tables(dataset: Any, expected_episodes: int, expected_frames: int) -> dict[str, int]:
    """Validate every published parquet footer and Arrow schema once."""

    import pyarrow.parquet as pq

    if int(dataset.meta.total_episodes) != expected_episodes:
        raise RuntimeError(
            f"metadata episode count mismatch: {dataset.meta.total_episodes} != {expected_episodes}"
        )
    if int(dataset.meta.total_frames) != expected_frames:
        raise RuntimeError(f"metadata frame count mismatch: {dataset.meta.total_frames} != {expected_frames}")

    parquet_rows = 0
    expected_schema = dataset.hf_features.arrow_schema
    for episode_index in range(expected_episodes):
        path = dataset.root / dataset.meta.get_data_file_path(ep_index=episode_index)
        if not path.is_file():
            raise RuntimeError(f"missing episode parquet: {path}")
        parquet_file = pq.ParquetFile(path)
        if not parquet_file.schema_arrow.equals(expected_schema, check_metadata=False):
            raise RuntimeError(
                f"episode parquet schema mismatch: {path}\n"
                f"actual={parquet_file.schema_arrow}\nexpected={expected_schema}"
            )
        parquet_rows += int(parquet_file.metadata.num_rows)
    if parquet_rows != expected_frames:
        raise RuntimeError(f"parquet row count mismatch: {parquet_rows} != {expected_frames}")
    return {"episode_parquets": expected_episodes, "parquet_rows": parquet_rows}


def validate_written_episode_table_range(
    dataset: Any,
    conversion_plan: dict[str, Any],
    episode_start: int,
    episode_end: int,
) -> dict[str, int]:
    """Validate a global-index shard without assuming episode zero is present."""

    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    expected_schema = dataset.hf_features.arrow_schema
    rows = 0
    expected_global_index = sum(
        int(item["frames"]) for item in conversion_plan["episodes"][:episode_start]
    )
    for episode_index in range(episode_start, episode_end):
        path = dataset.root / dataset.meta.get_data_file_path(ep_index=episode_index)
        if not path.is_file():
            raise RuntimeError(f"missing shard episode parquet: {path}")
        parquet = pq.ParquetFile(path)
        planned_rows = int(conversion_plan["episodes"][episode_index]["frames"])
        if int(parquet.metadata.num_rows) != planned_rows:
            raise RuntimeError(f"shard episode row mismatch: {path}")
        if not parquet.schema_arrow.equals(expected_schema, check_metadata=False):
            raise RuntimeError(f"shard episode schema mismatch: {path}")
        identity = pq.read_table(path, columns=["index", "episode_index", "frame_index"])
        expected_indices = pa.array(
            range(expected_global_index, expected_global_index + planned_rows), type=pa.int64()
        )
        expected_frames = pa.array(range(planned_rows), type=pa.int64())
        if not pc.all(pc.equal(identity["index"], expected_indices)).as_py():
            raise RuntimeError(f"shard global index column mismatch: {path}")
        expected_episode = pa.scalar(episode_index, type=pa.int64())
        if not pc.all(pc.equal(identity["episode_index"], expected_episode)).as_py():
            raise RuntimeError(f"shard episode_index column mismatch: {path}")
        if not pc.all(pc.equal(identity["frame_index"], expected_frames)).as_py():
            raise RuntimeError(f"shard frame_index column mismatch: {path}")
        expected_global_index += planned_rows
        rows += planned_rows
    return {"episode_parquets": episode_end - episode_start, "parquet_rows": rows}


def conversion_file_identity(
    path: Path | None,
    *,
    known_sha256: str | None = None,
) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    before = resolved.stat()
    digest = known_sha256 or sha256_file(resolved)
    after = resolved.stat()
    before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_signature != after_signature:
        raise RuntimeError(f"conversion input changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": digest,
    }


def gripper_source_video_identity(
    path: Path,
    sample_size: int = 256 * 1024,
) -> dict[str, Any]:
    """Reproduce the sampled canonical-video identity written by stage 03."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"_03 canonical front_fisheye is missing or not a file: {resolved}")
    before = resolved.stat()
    offsets = sorted(
        {
            0,
            max(0, before.st_size // 2 - sample_size // 2),
            max(0, before.st_size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for offset in offsets:
            source.seek(offset)
            data = source.read(min(sample_size, before.st_size - offset))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    after = resolved.stat()
    before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_signature != after_signature:
        raise RuntimeError(f"_03 canonical front_fisheye changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def conversion_implementation_identity(args: argparse.Namespace) -> dict[str, Any]:
    lerobot_env = args.lerobot_env.expanduser().resolve()
    lerobot_package = lerobot_env if lerobot_env.name == "lerobot" else lerobot_env / "lerobot"
    implementation_paths = {
        "converter": Path(__file__),
        "bimanual_helpers": SCRIPT_DIR / "bimanual_convert_helpers.py",
        "htc_to_flexiv_transform": SCRIPT_DIR / "htc_to_flexiv_transform.py",
        "capture_schema": DATAPROCESS_ROOT / "capture_schema.py",
        "direct_openpi_images": DATAPROCESS_ROOT / "direct_openpi_images.py",
        "production_semantics": DATAPROCESS_ROOT / "production_semantics.py",
        "lerobot_dataset": lerobot_package / "datasets" / "lerobot_dataset.py",
        "lerobot_image_writer": lerobot_package / "datasets" / "image_writer.py",
        "lerobot_compute_stats": lerobot_package / "datasets" / "compute_stats.py",
        "lerobot_dataset_utils": lerobot_package / "datasets" / "utils.py",
    }
    cv2 = get_cv2()
    return {
        "files": {
            name: conversion_file_identity(path)
            for name, path in implementation_paths.items()
        },
        "runtime": {
            "python": sys.version,
            "numpy": np.__version__,
            "opencv": cv2.__version__,
            "opencv_build_sha256": hashlib.sha256(
                cv2.getBuildInformation().encode("utf-8")
            ).hexdigest(),
            "datasets": package_version("datasets"),
            "pyarrow": package_version("pyarrow"),
            "pillow": package_version("Pillow"),
            "pyav": package_version("av"),
        },
    }


def gripper_provenance(
    gripper_path: Path,
    raw_manifest_path: Path | None,
    *,
    expected_frame_timestamps_path: Path | None = None,
    expected_timestamp_log_path: Path | None = None,
    expected_segment_time_offset_sec: float | None = None,
    expected_min_segment_frames: int | None = None,
) -> dict[str, Any]:
    gripper_identity = conversion_file_identity(gripper_path)
    manifest_path = gripper_path.parent / "manifest.json"
    manifest_identity = conversion_file_identity(manifest_path)
    payload = load_json_file(manifest_path)
    if payload is None:
        return {
            "status": "legacy_unverified",
            "reason": "missing_or_unreadable_03_manifest",
            "csv": gripper_identity,
            "manifest": manifest_identity,
        }
    expected_csv_hash = payload.get("output_csv_sha256")
    if expected_csv_hash is None:
        return {
            "status": "legacy_unverified",
            "reason": "03_manifest_has_no_output_hash_or_run_fingerprint",
            "csv": gripper_identity,
            "manifest": manifest_identity,
        }
    if expected_csv_hash != gripper_identity["sha256"]:
        raise RuntimeError(f"_03 manifest CSV hash mismatch: {gripper_path}")
    if not isinstance(payload.get("run_fingerprint"), str):
        raise RuntimeError(f"_03 manifest has no run fingerprint: {manifest_path}")
    recorded_raw = payload.get("raw_manifest_identity")
    current_raw = conversion_file_identity(raw_manifest_path)
    if not isinstance(recorded_raw, dict) or current_raw is None:
        raise RuntimeError(f"_03 manifest has no raw-manifest provenance: {manifest_path}")
    for key in ("path", "size", "sha256"):
        current_key = "size_bytes" if key == "size" else key
        if recorded_raw.get(key) != current_raw.get(current_key):
            raise RuntimeError(
                f"_03 output does not correspond to current _01 raw manifest ({key}): {manifest_path}"
            )

    source_layout = payload.get("source_layout")
    if not isinstance(source_layout, dict):
        raise RuntimeError(f"_03 manifest has no source-layout provenance: {manifest_path}")
    raw_payload = load_json_file(raw_manifest_path)
    if not isinstance(raw_payload, dict):
        raise RuntimeError(f"current _01 raw manifest is unreadable: {raw_manifest_path}")
    source_kind = source_layout.get("kind")
    if source_kind == "ordered_raw_insv_stream0":
        copied_sources = raw_payload.get("copied_sources")
        raw_insv = copied_sources.get("insv") if isinstance(copied_sources, dict) else None
        if not isinstance(raw_insv, list) or not raw_insv:
            raise RuntimeError(f"current _01 raw manifest has no ordered INSV sources: {raw_manifest_path}")
        current_paths = [resolve_path_from_manifest(str(value), raw_manifest_path) for value in raw_insv]
        if any(path is None for path in current_paths):
            raise RuntimeError(f"current _01 raw manifest contains an invalid INSV source: {raw_manifest_path}")
        resolved_paths = [path for path in current_paths if path is not None]
        if len(set(resolved_paths)) != len(resolved_paths):
            raise RuntimeError(f"current _01 raw manifest contains duplicate INSV sources: {raw_manifest_path}")

        decode_sources = source_layout.get("decode_sources")
        if not isinstance(decode_sources, list) or len(decode_sources) != len(resolved_paths):
            raise RuntimeError(
                "_03 ordered raw INSV source count does not match the current _01 raw manifest: "
                f"{manifest_path}"
            )
        current_identities: list[dict[str, Any]] = []
        expected_global_start = 0
        for index, (current_path, recorded_source) in enumerate(zip(resolved_paths, decode_sources)):
            if not isinstance(recorded_source, dict):
                raise RuntimeError(f"_03 raw INSV source entry is invalid at index {index}: {manifest_path}")
            recorded_path = recorded_source.get("original_path")
            if not isinstance(recorded_path, str) or resolve_path_from_manifest(recorded_path, manifest_path) != current_path:
                raise RuntimeError(
                    "_03 ordered raw INSV path does not match the current _01 raw manifest "
                    f"at index {index}: {manifest_path}"
                )
            current_identity = gripper_source_video_identity(current_path)
            if recorded_source.get("identity") != current_identity:
                raise RuntimeError(
                    f"_03 ordered raw INSV identity changed at index {index}: {manifest_path}"
                )
            if int(recorded_source.get("global_start", -1)) != expected_global_start:
                raise RuntimeError(
                    f"_03 ordered raw INSV global frame offset is invalid at index {index}: {manifest_path}"
                )
            probe = recorded_source.get("probe")
            if not isinstance(probe, dict) or int(probe.get("frame_count", -1)) <= 0:
                raise RuntimeError(f"_03 ordered raw INSV probe is invalid at index {index}: {manifest_path}")
            expected_global_start += int(probe["frame_count"])
            current_identities.append(current_identity)

        master_clock = raw_payload.get("master_clock")
        expected_frames = master_clock.get("frame_count") if isinstance(master_clock, dict) else None
        if expected_frames is not None and expected_global_start != int(expected_frames):
            raise RuntimeError(
                "_03 ordered raw INSV total frame count does not match the current _01 master clock: "
                f"{expected_global_start} != {expected_frames}"
            )
        expected_canonical_identity = {
            "kind": "ordered_raw_insv_stream0",
            "chapters": current_identities,
        }
        if source_layout.get("canonical_identity") != expected_canonical_identity:
            raise RuntimeError(f"_03 ordered raw INSV canonical identity is invalid: {manifest_path}")
    else:
        raw_videos = raw_payload.get("videos")
        raw_front_path = raw_videos.get("front_fisheye") if isinstance(raw_videos, dict) else None
        if not isinstance(raw_front_path, str) or not raw_front_path:
            raise RuntimeError(f"current _01 raw manifest has no front_fisheye path: {raw_manifest_path}")
        current_front_path = resolve_path_from_manifest(raw_front_path, raw_manifest_path)
        assert current_front_path is not None
        recorded_front_path = source_layout.get("canonical_path")
        recorded_front_identity = source_layout.get("canonical_identity")
        if not isinstance(recorded_front_path, str) or not recorded_front_path:
            raise RuntimeError(f"_03 manifest has no canonical front_fisheye path: {manifest_path}")
        if not isinstance(recorded_front_identity, dict):
            raise RuntimeError(f"_03 manifest has no canonical front_fisheye identity: {manifest_path}")
        if resolve_path_from_manifest(recorded_front_path, manifest_path) != current_front_path:
            raise RuntimeError(
                "_03 canonical front_fisheye path does not match the current _01 raw manifest: "
                f"{manifest_path}"
            )
        current_front_identity = gripper_source_video_identity(current_front_path)
        if recorded_front_identity != current_front_identity:
            raise RuntimeError(
                "_03 canonical front_fisheye identity does not match the current full-resolution video: "
                f"{manifest_path}"
            )
    active_selection = payload.get("active_frame_selection")
    if active_selection is not None:
        if not isinstance(active_selection, dict) or active_selection.get("schema") != "instaumi_active_calibrated_camera_ranges_v1":
            raise RuntimeError(f"_03 active-frame selection is invalid: {manifest_path}")

        def verify_small_input(label: str, recorded: object, expected_path: Path | None) -> None:
            if expected_path is None or not isinstance(recorded, dict):
                raise RuntimeError(f"_03 active-frame selection has no verifiable {label}: {manifest_path}")
            current = conversion_file_identity(expected_path)
            if current is None:
                raise RuntimeError(f"current {label} is missing: {expected_path}")
            if (
                recorded.get("path") != current.get("path")
                or int(recorded.get("size", -1)) != int(current.get("size_bytes", -2))
                or recorded.get("sha256") != current.get("sha256")
            ):
                raise RuntimeError(f"_03 {label} provenance does not match the current input: {manifest_path}")

        verify_small_input(
            "calibrated frame timestamps",
            active_selection.get("calibrated_timestamps_identity"),
            expected_frame_timestamps_path,
        )
        verify_small_input(
            "timestamp log",
            active_selection.get("timestamp_log_identity"),
            expected_timestamp_log_path,
        )
        if expected_segment_time_offset_sec is None or not math.isclose(
            float(active_selection.get("segment_time_offset_sec", float("nan"))),
            float(expected_segment_time_offset_sec),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(f"_03/_05 segment_time_offset_sec mismatch: {manifest_path}")
        if expected_min_segment_frames is None or int(active_selection.get("min_segment_frames", -1)) != int(
            expected_min_segment_frames
        ):
            raise RuntimeError(f"_03/_05 min_segment_frames mismatch: {manifest_path}")
    return {
        "status": "verified",
        "csv": gripper_identity,
        "manifest": manifest_identity,
        "run_fingerprint": payload["run_fingerprint"],
        "source_layout": source_layout,
        "raw_manifest_identity": recorded_raw,
        "active_frame_selection": active_selection,
    }


def load_continuous_gripper_segment_quality(gripper_path: Path) -> dict[str, Any]:
    """Load content-bound `_03` evidence without treating bad segments as usable.

    Legacy manifests without an enabled audit retain their historical behavior.
    Once the strict audit is enabled, every segment must carry internally
    consistent accepted/rejected evidence.  The caller can then discard only
    rejected demonstrations while keeping the expensive detection result.
    """

    manifest_path = gripper_path.parent / "manifest.json"
    manifest = load_json_file(manifest_path)
    if not isinstance(manifest, dict):
        return {"enabled": False, "segments": {}, "source": "legacy_no_manifest"}
    fill_report = manifest.get("fill_report") or {}
    quality = fill_report.get("continuous_gripper_quality") or {}
    contract = quality.get("contract") or {}
    if contract.get("enabled") is not True:
        return {"enabled": False, "segments": {}, "source": "legacy_audit_disabled"}
    if quality.get("schema") != CONTINUOUS_GRIPPER_QUALITY_SCHEMA:
        raise RuntimeError(f"unsupported continuous gripper quality schema: {manifest_path}")

    report_identity = fill_report.get("continuous_gripper_quality_report") or {}
    report_value = report_identity.get("path")
    if not isinstance(report_value, str) or not report_value:
        raise RuntimeError(f"_03 continuous gripper report identity is absent: {manifest_path}")
    report_path = Path(report_value).expanduser()
    if not report_path.is_absolute():
        report_path = manifest_path.parent / report_path
    report_path = report_path.resolve(strict=True)
    recorded_report_path = Path(report_value).expanduser()
    if not recorded_report_path.is_absolute():
        recorded_report_path = manifest_path.parent / recorded_report_path
    recorded_report_path = recorded_report_path.resolve()
    current_report_identity = conversion_file_identity(report_path)
    if current_report_identity is None or (
        report_path != recorded_report_path
        or int(report_identity.get("size", -1)) != int(current_report_identity["size_bytes"])
        or report_identity.get("sha256") != current_report_identity["sha256"]
        or load_json_file(report_path) != quality
    ):
        raise RuntimeError(f"_03 continuous gripper report changed: {report_path}")

    raw_segments = quality.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise RuntimeError(f"_03 continuous gripper report has no segment evidence: {report_path}")
    overall_status = quality.get("status")
    normalized: dict[int, dict[str, Any]] = {}
    accepted: list[int] = []
    rejected: list[int] = []
    flattened: list[str] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise RuntimeError(f"_03 continuous gripper segment is not an object: {report_path}")
        segment_index = int(raw.get("segment_index", -1))
        if segment_index < 0 or segment_index in normalized:
            raise RuntimeError(
                f"_03 continuous gripper segment index is invalid/duplicate: {report_path}"
            )
        status = raw.get("status")
        violations = raw.get("violations")
        # Backwards compatibility is safe only for the historical all-accepted
        # report.  An old aggregate rejection cannot identify which episode is
        # safe and therefore remains fail closed.
        if status is None and violations is None and overall_status == "accepted":
            status = "accepted"
            violations = []
        if not isinstance(violations, list) or not all(
            isinstance(value, str) and value for value in violations
        ):
            raise RuntimeError(
                f"segment={segment_index} has invalid continuous gripper violations"
            )
        if status == "accepted" and not violations:
            accepted.append(segment_index)
        elif status == "rejected" and violations:
            rejected.append(segment_index)
            flattened.extend(violations)
        else:
            raise RuntimeError(
                f"segment={segment_index} has inconsistent continuous gripper quality"
            )
        normalized[segment_index] = {**raw, "status": status, "violations": violations}

    expected_status = (
        "accepted" if not rejected else "partially_accepted" if accepted else "rejected"
    )
    if overall_status != expected_status:
        raise RuntimeError(f"_03 continuous gripper aggregate status is inconsistent: {report_path}")
    if list(quality.get("violations") or []) != flattened:
        raise RuntimeError(f"_03 continuous gripper aggregate violations are inconsistent: {report_path}")
    if rejected:
        summary = quality.get("summary") or {}
        if (
            int(summary.get("segment_count", -1)) != len(normalized)
            or int(summary.get("accepted_segment_count", -1)) != len(accepted)
            or int(summary.get("rejected_segment_count", -1)) != len(rejected)
            or list(summary.get("accepted_segment_indices") or []) != accepted
            or list(summary.get("rejected_segment_indices") or []) != rejected
        ):
            raise RuntimeError(f"_03 continuous gripper summary is inconsistent: {report_path}")
    return {
        "enabled": True,
        "schema": CONTINUOUS_GRIPPER_QUALITY_SCHEMA,
        "status": overall_status,
        "contract": contract,
        "segments": normalized,
        "accepted_segment_indices": accepted,
        "rejected_segment_indices": rejected,
        "report": current_report_identity,
    }


def continuous_gripper_segment_rejections(
    segment_index: int,
    quality_by_arm: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only hard, data-invalidating gripper rejection reasons.

    Detection sparsity, long hold intervals and leading/trailing extrapolation
    are warnings: the continuous width is reconstructed within the episode by
    first-valid backfill followed by hold-last.  Missing evidence, zero usable
    detections, invalid physical widths and unknown integrity violations remain
    fail-closed.
    """

    rejections: list[dict[str, Any]] = []
    for arm in ("right", "left"):
        quality = quality_by_arm[arm]
        if not quality.get("enabled"):
            continue
        evidence = (quality.get("segments") or {}).get(segment_index)
        if evidence is None:
            rejections.append(
                {
                    "arm": arm,
                    "reason": "continuous_gripper_quality_evidence_missing",
                    "segment_index": segment_index,
                }
            )
            continue
        hard_reasons = continuous_gripper_segment_hard_reasons(
            evidence,
            contract=quality.get("contract") or {},
        )
        if hard_reasons:
            rejections.append(
                {
                    "arm": arm,
                    "reason": "continuous_gripper_quality_hard_rejected",
                    "segment_index": segment_index,
                    "violations": hard_reasons,
                    "warnings": continuous_gripper_segment_warnings(evidence),
                    "metrics": {
                        key: evidence.get(key)
                        for key in (
                            "real_detection_frames",
                            "detection_rate",
                            "leading_constant_extrapolation_ms",
                            "trailing_constant_extrapolation_ms",
                            "max_internal_detection_gap_ms",
                            "real_width_min_m",
                            "real_width_max_m",
                            "filled_width_min_m",
                            "filled_width_max_m",
                        )
                    },
                }
            )
    return rejections


_NONFATAL_GRIPPER_VIOLATION_PATTERNS = (
    re.compile(r"(?:internal_gap|max_internal_detection_gap)_ms="),
    re.compile(r"(?:leading|trailing)(?:_constant)?_extrapolation_ms="),
    re.compile(r"edge_extrapolation_ms="),
)


def continuous_gripper_segment_warnings(evidence: dict[str, Any]) -> list[str]:
    """Normalize new warning fields and historical sparsity violations."""

    warnings = list(evidence.get("warnings") or [])
    for violation in evidence.get("violations") or []:
        text = str(violation)
        if any(pattern.search(text) for pattern in _NONFATAL_GRIPPER_VIOLATION_PATTERNS):
            warnings.append(text)
            continue
        match = re.search(r"real_detections=(\d+)\s*<", text)
        if match and int(match.group(1)) >= 1:
            warnings.append(text)
    return warnings


def continuous_gripper_segment_hard_reasons(
    evidence: dict[str, Any],
    *,
    contract: dict[str, Any],
) -> list[str]:
    """Classify only evidence that cannot yield a trustworthy finite width.

    The classifier intentionally understands the previous v1 report where
    gap/extrapolation warnings were stored in ``violations`` and the segment
    status was ``rejected``.  This lets a checkpoint resume without rerunning
    expensive image detection while still failing closed on unknown failures.
    """

    reasons: list[str] = []
    detections = evidence.get("real_detection_frames")
    if isinstance(detections, bool) or not isinstance(detections, int):
        reasons.append("real_detection_frames is absent or invalid")
    elif detections < 1:
        reasons.append("no valid gripper detection exists inside the episode")
    finite_detections = evidence.get("finite_real_detection_frames")
    if finite_detections is not None and (
        isinstance(finite_detections, bool)
        or not isinstance(finite_detections, int)
        or finite_detections < 1
    ):
        reasons.append("no finite gripper detection exists inside the episode")

    min_width_m = float(contract.get("min_width_m", 0.0))
    max_width_m = float(contract.get("max_width_m", 0.11))
    for prefix in ("real", "filled"):
        lower = evidence.get(f"{prefix}_width_min_m")
        upper = evidence.get(f"{prefix}_width_max_m")
        # Production `_03` evidence always publishes these.  Missing metrics
        # are rejected here instead of silently weakening the release audit.
        if lower is None or upper is None:
            reasons.append(f"{prefix} width range evidence is absent")
            continue
        try:
            lower_value = float(lower)
            upper_value = float(upper)
        except (TypeError, ValueError):
            reasons.append(f"{prefix} width range evidence is non-numeric")
            continue
        if (
            not math.isfinite(lower_value)
            or not math.isfinite(upper_value)
            or lower_value > upper_value
            or lower_value < min_width_m
            or upper_value > max_width_m
        ):
            reasons.append(
                f"{prefix} width range [{lower_value},{upper_value}] is outside "
                f"[{min_width_m},{max_width_m}] m"
            )

    warnings = set(continuous_gripper_segment_warnings(evidence))
    for violation in evidence.get("violations") or []:
        text = str(violation)
        if text not in warnings:
            reasons.append(text)
    return reasons


def require_planned_runs_before_media(
    planned_runs: list[tuple[SegmentRange, FrameRun]],
    *,
    source_segment_count: int,
) -> None:
    """Stop an all-rejected conversion before any costly media preparation.

    Segment alignment and quality gates are deliberately numeric-only.  Once
    they prove that no source segment can produce an episode, staging raw
    videos, exporting continuous panoramas, constructing frame readers, or
    creating a partial dataset cannot change that result and only wastes I/O
    and compute.
    """

    if planned_runs:
        return
    raise RuntimeError(
        "No valid episode runs remain after timestamp alignment and quality "
        f"gating; all {source_segment_count} source segment(s) were rejected. "
        "Aborting before panorama export, media cache/staging, or frame-reader "
        "initialization."
    )


def build_conversion_plan(
    args: argparse.Namespace,
    right: HandInput,
    left: HandInput,
    planned_runs: list[tuple[SegmentRange, FrameRun]],
    calibration_report: dict[str, Any] | None,
    fast_workspace_report: dict[str, Any],
    direct_image_plans: dict[str, DirectRawOpenPIImagePlan] | None = None,
) -> dict[str, Any]:
    video_identities: list[dict[str, Any] | None] = []
    direct_mode = bool(getattr(args, "direct_openpi_images", False))
    panorama_numeric_only = bool(getattr(args, "panorama_numeric_only", False))
    if direct_mode:
        if direct_image_plans is None or set(direct_image_plans) != {"right", "left"}:
            raise RuntimeError("direct OpenPI conversion plan requires right and left raw image plans")
        validate_direct_plan_source_sets(direct_image_plans)
        for plan in direct_image_plans.values():
            plan.verify_sources(verify_samples=False)
        image_sources: dict[str, Any] = {
            "mode": (
                "official_continuous_h265_panorama_only"
                if panorama_numeric_only
                else "official_continuous_h265_then_active_openpi"
            ),
            "right": direct_image_plans["right"].to_dict(),
            "left": direct_image_plans["left"].to_dict(),
        }
    else:
        if right.camera_paths is None or left.camera_paths is None:
            raise RuntimeError("legacy conversion plan requires six camera videos")
        image_sources = {"mode": "legacy_six_videos"}
        original_videos = [
            item["source"] for item in fast_workspace_report.get("cached_videos", [])
        ]
        if not original_videos:
            original_videos = [str(path) for path in (*right.camera_paths, *left.camera_paths)]
        cached_records = {
            str(Path(item["source"]).expanduser().resolve()): item
            for item in fast_workspace_report.get("cached_videos", [])
        }
        for path_value in original_videos:
            resolved_path = Path(path_value).expanduser().resolve()
            cached_record = cached_records.get(str(resolved_path))
            if cached_record is None:
                if args.dry_run:
                    stat = resolved_path.stat()
                    video_identities.append(
                        {
                            "path": str(resolved_path),
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "identity_mode": "sampled_dry_run",
                            **sampled_content_identity(resolved_path),
                        }
                    )
                else:
                    video_identities.append(conversion_file_identity(resolved_path))
                continue
            from fast_workspace import SourceChangedError, identify_file

            frozen_identity = cached_record.get("source_identity")
            if not isinstance(frozen_identity, dict):
                raise RuntimeError(f"cache report has no frozen source identity: {resolved_path}")
            current_identity = identify_file(
                resolved_path,
                sample_bytes=int(frozen_identity.get("sample_bytes", 1024 * 1024)),
            ).to_dict()
            if current_identity != frozen_identity:
                raise SourceChangedError(
                    f"camera source changed after cache staging and before plan commit: {resolved_path}"
                )
            video_identities.append(
                {
                    "path": current_identity["path"],
                    "size_bytes": current_identity["size_bytes"],
                    "mtime_ns": current_identity["mtime_ns"],
                    "sample_sha256": current_identity["sample_sha256"],
                    "sha256": cached_record["sha256"],
                }
            )
    files = {
        "videos": video_identities,
        "image_sources": image_sources,
        "right_timestamps": conversion_file_identity(right.timestamps_path),
        "left_timestamps": conversion_file_identity(left.timestamps_path),
        "right_pose": conversion_file_identity(right.trajectory_path),
        "left_pose": conversion_file_identity(left.trajectory_path),
        "right_gripper": conversion_file_identity(right.gripper_path),
        "left_gripper": conversion_file_identity(left.gripper_path),
        "right_tracker": conversion_file_identity(right.tracker_path),
        "left_tracker": conversion_file_identity(left.tracker_path),
        "right_raw_manifest": conversion_file_identity(right.raw_manifest_path),
        "left_raw_manifest": conversion_file_identity(left.raw_manifest_path),
        "sync_calibration_report": conversion_file_identity(args.sync_calibration_report),
        "timestamp_log": conversion_file_identity(args.timestamp_log),
    }
    gripper_inputs = {
        "right": gripper_provenance(
            right.gripper_path,
            right.raw_manifest_path,
            expected_frame_timestamps_path=right.timestamps_path,
            expected_timestamp_log_path=args.timestamp_log,
            expected_segment_time_offset_sec=args.segment_time_offset_sec,
            expected_min_segment_frames=args.min_segment_frames,
        ),
        "left": gripper_provenance(
            left.gripper_path,
            left.raw_manifest_path,
            expected_frame_timestamps_path=left.timestamps_path,
            expected_timestamp_log_path=args.timestamp_log,
            expected_segment_time_offset_sec=args.segment_time_offset_sec,
            expected_min_segment_frames=args.min_segment_frames,
        ),
    }
    semantic_args = {
        name: getattr(args, name)
        for name in (
            "repo_id",
            "robot_type",
            "task",
            "fps",
            "down_sample_steps",
            "image_width",
            "image_height",
            "right_pose_sensor",
            "left_pose_sensor",
            "right_htc_role",
            "left_htc_role",
            "htc_time_source",
            "direct_from_raw",
            "require_calibrated_timestamps",
            "time_align_mode",
            "camera_pairing_mode",
            "max_camera_time_diff_ms",
            "hard_camera_time_diff_ms",
            "max_htc_nearest_gap_ms",
            "max_htc_bracket_gap_ms",
            "max_host_clock_offset_step_ms",
            "hard_gap_reject_ms",
            "split_by_segments",
            "min_segment_frames",
            "segment_time_offset_sec",
            "gripper_column",
            "missing_gripper",
            "max_frames_per_episode",
            "allow_partial_inputs",
            "png_compress_level",
            "preview_every_n",
            "preview_stride",
            "preview_fourcc",
            "streaming_dataset_write",
            "episode_write_batch_frames",
            "embed_images_in_memory",
            "persistent_video_readers",
            "video_seek_threshold_frames",
            "disable_lerobot_combined_video",
            "episode_shards",
            "episode_shard_workers",
        )
    }
    semantic_args["gripper_zero_below_m"] = getattr(
        args, "gripper_zero_below_m", None
    )
    semantic_args["gripper_binary_threshold_m"] = getattr(
        args, "gripper_binary_threshold_m", None
    )
    semantic_args["resolved_right_tracker_role"] = right.tracker_role
    semantic_args["resolved_left_tracker_role"] = left.tracker_role
    semantic_args["right_timestamps_source"] = right.timestamps_source
    semantic_args["left_timestamps_source"] = left.timestamps_source
    semantic_args["direct_openpi_images"] = direct_mode
    semantic_args["panorama_numeric_only"] = panorama_numeric_only
    if direct_mode:
        # MediaSDK may schedule work differently with a different request
        # grouping/cache policy.  Conservatively bind all decoder knobs that
        # can change the exact sequence of raw export calls across a resume.
        for name in (
            "direct_image_batch_frames",
            "direct_image_export_workers",
            "direct_image_parallel_arms",
            "direct_image_decode_threads",
            "direct_image_resize_workers",
            "direct_image_v360_filter_threads",
            "direct_image_repeat_cache_frames",
            "direct_image_gpu_max_workers",
            "direct_image_gpu_startup_workers",
            "direct_image_gpu_min_workers",
            "direct_image_gpu_startup_progress",
            "direct_image_gpu_oom_retries",
            "direct_image_panorama_export_workers",
            "direct_image_panorama_bitrate",
            "direct_image_panorama_timeout_sec",
        ):
            semantic_args[name] = getattr(args, name)
        semantic_args["direct_image_panorama_exporter"] = full_file_identity(
            args.direct_image_panorama_exporter.expanduser().resolve(strict=True),
            reject_symlink=True,
        )
    episodes = []
    for episode_index, (segment, run) in enumerate(planned_runs):
        pairs_array = np.asarray(run.pairs, dtype="<i8")
        grid_array = np.asarray(run.grid_unix_time_ms, dtype="<f8")
        episodes.append(
            {
                "episode_index": episode_index,
                "segment_index": segment.segment_index,
                "run_index": run.run_index,
                "pair_count": len(run.pairs),
                "frames": len(run.pairs) - 1,
                "first_pair": list(run.pairs[0]),
                "last_pair": list(run.pairs[-1]),
                "pairs_sha256": hashlib.sha256(pairs_array.tobytes()).hexdigest(),
                "grid_unix_time_ms_sha256": hashlib.sha256(grid_array.tobytes()).hexdigest(),
            }
        )
    implementation = conversion_implementation_identity(args)
    if direct_mode:
        production_semantics = build_production_semantics(
            image_sources=image_sources,
            arguments=semantic_args,
            implementation=implementation,
        )
    else:
        production_semantics = build_nonproduction_semantics(
            arguments=semantic_args,
            implementation=implementation,
        )
    plan = {
        "schema": "instaumi-bimanual-conversion-plan-v6",
        "calibration_id": None if calibration_report is None else calibration_report.get("calibration_id"),
        "features": output_features(args),
        "arguments": semantic_args,
        "inputs": files,
        "gripper_provenance": gripper_inputs,
        "gripper_transform": gripper_transform_from_args(args),
        "implementation": implementation,
        "production_semantics": production_semantics,
        "episodes": episodes,
    }
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    plan["plan_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


def json_compatible(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    return value


def sha256_file(path: Path, buffer_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while chunk := handle.read(buffer_size):
            digest.update(chunk)
    return digest.hexdigest()


EPISODE_CHECKPOINT_SCHEMA = "instaumi-bimanual-episode-checkpoint-v1"


def write_episode_checkpoint(
    dataset: Any,
    conversion_plan: dict[str, Any],
    episode_index: int,
) -> None:
    import pyarrow.parquet as pq

    parquet_path = dataset.root / dataset.meta.get_data_file_path(ep_index=episode_index)
    row_count = int(pq.ParquetFile(parquet_path).metadata.num_rows)
    planned = conversion_plan["episodes"][episode_index]
    if row_count != int(planned["frames"]):
        raise RuntimeError(
            f"checkpoint row mismatch for episode {episode_index}: {row_count} != {planned['frames']}"
        )
    checkpoint = {
        "schema": EPISODE_CHECKPOINT_SCHEMA,
        "plan_hash": conversion_plan["plan_hash"],
        "episode_index": episode_index,
        "episode_plan": planned,
        "episode_metadata": dataset.meta.episodes[episode_index],
        "episode_stats": json_compatible(dataset.meta.episodes_stats[episode_index]),
        "parquet": {
            "relative_path": parquet_path.relative_to(dataset.root).as_posix(),
            "size_bytes": parquet_path.stat().st_size,
            "rows": row_count,
            "sha256": sha256_file(parquet_path),
        },
    }
    atomic_write_json(
        dataset.root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json",
        checkpoint,
    )


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def conversion_plan_matches(root: Path, expected: dict[str, Any]) -> bool:
    actual = load_json_file(root / "conversion_plan.json")
    return actual is not None and actual.get("plan_hash") == expected.get("plan_hash")


CONVERSION_COMPLETE_SCHEMA = "instaumi-bimanual-conversion-complete-v1"


def write_conversion_complete(
    root: Path,
    conversion_plan: dict[str, Any],
    *,
    episodes: int,
    frames: int,
) -> None:
    mapping_path = root / "source_frame_mapping.csv"
    report_path = root / "sync_report.json"
    sync_report = load_json_file(report_path)
    if sync_report is None or sync_report.get(
        "gripper_transform"
    ) != conversion_plan.get("gripper_transform"):
        raise RuntimeError(
            "sync report and conversion plan gripper transforms differ: "
            f"{report_path}"
        )
    atomic_write_json(
        root / "conversion_complete.json",
        {
            "schema": CONVERSION_COMPLETE_SCHEMA,
            "plan_hash": conversion_plan["plan_hash"],
            "episodes": int(episodes),
            "frames": int(frames),
            "gripper_transform": conversion_plan.get("gripper_transform"),
            "source_frame_mapping_sha256": sha256_file(mapping_path),
            "sync_report_sha256": sha256_file(report_path),
        },
    )


def validate_conversion_complete(
    root: Path,
    conversion_plan: dict[str, Any],
    *,
    episodes: int,
    frames: int,
) -> None:
    marker_path = root / "conversion_complete.json"
    marker = load_json_file(marker_path)
    if marker is None or marker.get("schema") != CONVERSION_COMPLETE_SCHEMA:
        raise RuntimeError(f"completed output marker is missing or invalid: {marker_path}")
    expected = {
        "plan_hash": conversion_plan["plan_hash"],
        "episodes": int(episodes),
        "frames": int(frames),
        "gripper_transform": conversion_plan.get("gripper_transform"),
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise RuntimeError(f"completed output marker mismatch for {key}: {marker_path}")
    for relative_path, key in (
        ("source_frame_mapping.csv", "source_frame_mapping_sha256"),
        ("sync_report.json", "sync_report_sha256"),
    ):
        path = root / relative_path
        if not path.is_file() or marker.get(key) != sha256_file(path):
            raise RuntimeError(f"completed output file hash mismatch: {path}")
    sync_report = load_json_file(root / "sync_report.json")
    if sync_report is None or sync_report.get(
        "gripper_transform"
    ) != conversion_plan.get("gripper_transform"):
        raise RuntimeError(
            "completed sync report and conversion plan gripper transforms differ: "
            f"{root}"
        )


def atomic_write_jsonlines(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_resume_root(
    root: Path,
    conversion_plan: dict[str, Any],
    *,
    verify_full_hash: bool,
) -> tuple[int, int]:
    """Rebuild JSONL indexes and validate the info.json-committed checkpoint prefix."""

    import pyarrow.parquet as pq
    from lerobot.datasets.utils import get_hf_features_from_features

    if not conversion_plan_matches(root, conversion_plan):
        raise RuntimeError(f"existing output has no matching conversion plan: {root}")
    info_path = root / "meta" / "info.json"
    info = load_json_file(info_path)
    if info is None:
        raise RuntimeError(f"resume metadata is missing or unreadable: {info_path}")
    committed = int(info.get("total_episodes", -1))
    committed_frames = int(info.get("total_frames", -1))
    if committed < 0 or committed > len(conversion_plan["episodes"]):
        raise RuntimeError(f"invalid committed episode count for resume: {committed}")
    info_features = {
        key: {**feature, "shape": tuple(feature["shape"])}
        for key, feature in info["features"].items()
    }
    expected_schema = get_hf_features_from_features(info_features).arrow_schema

    expected_frames = 0
    ordered_episodes = []
    ordered_stats = []
    for episode_index in range(committed):
        checkpoint_path = root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json"
        checkpoint = load_json_file(checkpoint_path)
        if checkpoint is None:
            raise RuntimeError(
                f"committed episode checkpoint is missing or unreadable: {checkpoint_path}"
            )
        if checkpoint.get("schema") != EPISODE_CHECKPOINT_SCHEMA:
            raise RuntimeError(f"committed episode checkpoint schema mismatch: {checkpoint_path}")
        if checkpoint.get("episode_index") != episode_index:
            raise RuntimeError(f"checkpoint episode index mismatch: {checkpoint_path}")
        if checkpoint.get("plan_hash") != conversion_plan["plan_hash"]:
            raise RuntimeError(f"checkpoint plan mismatch: {checkpoint_path}")
        if checkpoint.get("episode_plan") != conversion_plan["episodes"][episode_index]:
            raise RuntimeError(f"checkpoint episode plan mismatch: {checkpoint_path}")
        episode_record = checkpoint.get("episode_metadata")
        episode_stats = checkpoint.get("episode_stats")
        if not isinstance(episode_record, dict) or not isinstance(episode_stats, dict):
            raise RuntimeError(f"committed episode {episode_index} has incomplete resume metadata")
        if episode_record.get("episode_index") != episode_index:
            raise RuntimeError(f"checkpoint episode metadata index mismatch: {checkpoint_path}")
        planned_rows = int(conversion_plan["episodes"][episode_index]["frames"])
        if int(episode_record.get("length", -1)) != planned_rows:
            raise RuntimeError(f"episode {episode_index} metadata length does not match conversion plan")
        data_template = str(info["data_path"])
        chunk_size = int(info.get("chunks_size", 1000))
        parquet_path = root / data_template.format(
            episode_chunk=episode_index // chunk_size,
            episode_index=episode_index,
        )
        if not parquet_path.is_file():
            raise RuntimeError(f"committed episode parquet is missing: {parquet_path}")
        parquet_file = pq.ParquetFile(parquet_path)
        if int(parquet_file.metadata.num_rows) != planned_rows:
            raise RuntimeError(f"committed episode parquet row mismatch: {parquet_path}")
        if not parquet_file.schema_arrow.equals(expected_schema, check_metadata=False):
            raise RuntimeError(f"committed episode parquet schema mismatch: {parquet_path}")
        parquet_record = checkpoint.get("parquet")
        if not isinstance(parquet_record, dict):
            raise RuntimeError(f"checkpoint parquet identity is missing: {checkpoint_path}")
        expected_relative_path = parquet_path.relative_to(root).as_posix()
        if parquet_record.get("relative_path") != expected_relative_path:
            raise RuntimeError(f"checkpoint parquet path mismatch: {checkpoint_path}")
        if int(parquet_record.get("rows", -1)) != planned_rows:
            raise RuntimeError(f"checkpoint parquet row identity mismatch: {checkpoint_path}")
        if int(parquet_record.get("size_bytes", -1)) != parquet_path.stat().st_size:
            raise RuntimeError(f"checkpoint parquet size mismatch: {parquet_path}")
        if verify_full_hash and parquet_record.get("sha256") != sha256_file(parquet_path):
            raise RuntimeError(f"checkpoint parquet hash mismatch: {parquet_path}")
        expected_frames += planned_rows
        ordered_episodes.append(episode_record)
        ordered_stats.append(
            {
                "episode_index": episode_index,
                "stats": episode_stats,
            }
        )

    if committed_frames != expected_frames:
        raise RuntimeError(f"committed frame count mismatch: {committed_frames} != {expected_frames}")
    committed_task_names: list[str] = []
    for record in ordered_episodes:
        tasks = record.get("tasks")
        if not isinstance(tasks, list):
            raise RuntimeError(f"committed episode has invalid tasks metadata: {record}")
        for task in tasks:
            if not isinstance(task, str):
                raise RuntimeError(f"committed episode has non-string task: {record}")
            if task not in committed_task_names:
                committed_task_names.append(task)
    if int(info.get("total_tasks", -1)) != len(committed_task_names):
        raise RuntimeError(
            f"committed task count mismatch: {info.get('total_tasks')} != {len(committed_task_names)}"
        )
    # Checkpoints are the authoritative copies of committed JSONL metadata.
    # Rebuild every log from the info.json-committed checkpoint prefix so a
    # torn append, duplicate index, or modified committed record can never be
    # adopted during resume.  Checkpoints beyond the durable info.json count
    # remain uncommitted and are deliberately ignored.
    atomic_write_jsonlines(root / "meta" / "episodes.jsonl", ordered_episodes)
    atomic_write_jsonlines(root / "meta" / "episodes_stats.jsonl", ordered_stats)
    atomic_write_jsonlines(
        root / "meta" / "tasks.jsonl",
        [
            {"task_index": task_index, "task": task}
            for task_index, task in enumerate(committed_task_names)
        ],
    )
    return committed, committed_frames


def prepare_resume_shard_root(
    root: Path,
    conversion_plan: dict[str, Any],
    shard: EpisodeShardSpec,
    *,
    task: str,
    verify_full_hash: bool,
) -> tuple[int, int]:
    """Validate and reconstruct one private shard's committed global prefix."""

    import pyarrow.parquet as pq
    from lerobot.datasets.utils import get_hf_features_from_features

    if not conversion_plan_matches(root, conversion_plan):
        raise RuntimeError(f"existing shard has no matching conversion plan: {root}")
    shard_plan = load_json_file(root / "episode_shard_plan.json")
    expected_shard_plan = {
        **shard.to_dict(),
        "conversion_plan_hash": conversion_plan["plan_hash"],
    }
    if shard_plan != expected_shard_plan:
        raise RuntimeError(f"existing episode shard plan does not match: {root}")
    info_path = root / "meta" / "info.json"
    info = load_json_file(info_path)
    if info is None:
        raise RuntimeError(f"episode shard metadata is missing: {info_path}")
    committed = int(info.get("total_episodes", -1))
    committed_frames = int(info.get("total_frames", -1))
    if not shard.episode_start <= committed <= shard.episode_end:
        raise RuntimeError(
            f"invalid shard committed episode prefix: {committed} not in "
            f"[{shard.episode_start}, {shard.episode_end}]"
        )
    info_features = {
        key: {**feature, "shape": tuple(feature["shape"])}
        for key, feature in info["features"].items()
    }
    expected_schema = get_hf_features_from_features(info_features).arrow_schema
    expected_frames = shard.frame_start
    ordered_episodes: list[dict[str, Any]] = []
    ordered_stats: list[dict[str, Any]] = []
    for episode_index in range(shard.episode_start, committed):
        checkpoint_path = root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json"
        checkpoint = load_json_file(checkpoint_path)
        if checkpoint is None or checkpoint.get("schema") != EPISODE_CHECKPOINT_SCHEMA:
            raise RuntimeError(f"shard checkpoint is missing or invalid: {checkpoint_path}")
        if checkpoint.get("plan_hash") != conversion_plan["plan_hash"]:
            raise RuntimeError(f"shard checkpoint plan mismatch: {checkpoint_path}")
        if checkpoint.get("episode_index") != episode_index:
            raise RuntimeError(f"shard checkpoint episode mismatch: {checkpoint_path}")
        if checkpoint.get("episode_plan") != conversion_plan["episodes"][episode_index]:
            raise RuntimeError(f"shard checkpoint episode plan changed: {checkpoint_path}")
        episode_record = checkpoint.get("episode_metadata")
        episode_stats = checkpoint.get("episode_stats")
        parquet_record = checkpoint.get("parquet")
        if not all(isinstance(item, dict) for item in (episode_record, episode_stats, parquet_record)):
            raise RuntimeError(f"shard checkpoint payload is incomplete: {checkpoint_path}")
        planned_rows = int(conversion_plan["episodes"][episode_index]["frames"])
        relative_path = str(parquet_record["relative_path"])
        parquet_path = root / relative_path
        if not parquet_path.is_file() or parquet_path.stat().st_size != int(
            parquet_record.get("size_bytes", -1)
        ):
            raise RuntimeError(f"shard parquet identity mismatch: {parquet_path}")
        parquet_file = pq.ParquetFile(parquet_path)
        if int(parquet_file.metadata.num_rows) != planned_rows:
            raise RuntimeError(f"shard parquet row mismatch: {parquet_path}")
        if not parquet_file.schema_arrow.equals(expected_schema, check_metadata=False):
            raise RuntimeError(f"shard parquet schema mismatch: {parquet_path}")
        if verify_full_hash and parquet_record.get("sha256") != sha256_file(parquet_path):
            raise RuntimeError(f"shard parquet SHA-256 mismatch: {parquet_path}")
        expected_frames += planned_rows
        ordered_episodes.append(episode_record)
        ordered_stats.append({"episode_index": episode_index, "stats": episode_stats})
    if committed_frames != expected_frames:
        raise RuntimeError(
            f"shard committed frame count mismatch: {committed_frames} != {expected_frames}"
        )
    if int(info.get("total_tasks", -1)) != 1:
        raise RuntimeError("episode shard must contain exactly one pre-registered task")
    atomic_write_jsonlines(root / "meta" / "episodes.jsonl", ordered_episodes)
    atomic_write_jsonlines(root / "meta" / "episodes_stats.jsonl", ordered_stats)
    atomic_write_jsonlines(root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": task}])
    return committed, committed_frames


def _episode_shard_worker_entry(args: argparse.Namespace) -> None:
    """Spawn target.  A shard root is generated state and never user data."""

    root = args.output_root.expanduser().resolve()
    if root.exists() and not (root / "conversion_plan.json").is_file():
        remove_output_root(root, True)
    run(args)


def run_episode_shard_workers(
    args: argparse.Namespace,
    specs: list[EpisodeShardSpec],
    shard_roots: dict[int, Path],
    source_plans: dict[str, DirectRawOpenPIImagePlan],
    runtime_plans: dict[
        str, DirectRawOpenPIImagePlan | DirectOpenPIImagePlan
    ],
    *,
    max_live_workers: int,
) -> None:
    """Run isolated shard converters with a bounded process-level budget."""

    if max_live_workers <= 0:
        raise ValueError("max_live_workers must be positive")
    context = multiprocessing.get_context("spawn")
    gpu_admission = None
    if bool(args.direct_image_cuda) and any(
        isinstance(plan, DirectRawOpenPIImagePlan)
        for plan in runtime_plans.values()
    ):
        potential_gpu_workers = 2 * max_live_workers * int(specs[0].export_workers_per_arm)
        maximum, startup, minimum, startup_progress, retry_count = (
            resolve_gpu_admission_settings(args, potential_gpu_workers)
        )
        gpu_admission = SharedMediaSDKGPUAdmission.create(
            context,
            maximum_limit=maximum,
            minimum_limit=minimum,
            startup_limit=startup,
            startup_progress=startup_progress,
            retry_count=retry_count,
        )
        print(
            "[gpu-admission] "
            f"potential={potential_gpu_workers} max={maximum} startup={startup} "
            f"floor={minimum} startup_progress={startup_progress}% "
            f"oom_retries={retry_count}",
            flush=True,
        )
    pending = list(specs)
    active: dict[int, tuple[multiprocessing.Process, EpisodeShardSpec]] = {}
    while pending or active:
        while pending and len(active) < max_live_workers:
            spec = pending.pop(0)
            child_args = argparse.Namespace(**vars(args))
            child_args.output_root = shard_roots[spec.shard_index]
            child_args.fast_workspace = "off"
            child_args.overwrite = False
            child_args.resume = True
            child_args._episode_shard_spec = spec
            child_args._episode_shard_allowed_group = shard_roots[spec.shard_index].parent
            # Freeze the exact parent source plan.  A spawned Conda child may
            # receive an extra loader path even though the camera sources and
            # MediaSDK binaries are unchanged; rebuilding provenance there
            # would create a non-semantic conversion-plan hash mismatch.
            child_args._episode_shard_source_plans = source_plans
            child_args._episode_shard_runtime_plans = runtime_plans
            child_args._episode_shard_export_workers_per_arm = spec.export_workers_per_arm
            child_args._episode_shard_batch_frames_per_arm = spec.batch_frames_per_arm
            child_args._episode_shard_resize_workers_per_arm = spec.resize_workers_per_arm
            child_args._direct_image_gpu_admission = gpu_admission
            if int(child_args.image_writer_threads) > 0:
                child_args.image_writer_threads = max(
                    1, int(child_args.image_writer_threads) // max_live_workers
                )
            if int(child_args.stats_image_threads) > 0:
                child_args.stats_image_threads = max(
                    1, int(child_args.stats_image_threads) // max_live_workers
                )
            process = context.Process(
                target=_episode_shard_worker_entry,
                args=(child_args,),
                name=f"instaumi-episode-shard-{spec.shard_index:03d}",
            )
            process.start()
            active[process.pid] = (process, spec)
            print(
                f"[shard-start] shard={spec.shard_index} pid={process.pid} "
                f"episodes={spec.episode_start}:{spec.episode_end} "
                f"chapters=R{spec.right_chapter_indices}/L{spec.left_chapter_indices} "
                f"export_per_arm={spec.export_workers_per_arm} "
                f"batch_per_arm={spec.batch_frames_per_arm} "
                f"resize_per_arm={spec.resize_workers_per_arm}",
                flush=True,
            )
        finished_pid = None
        for pid, (process, spec) in active.items():
            process.join(timeout=0.1)
            if process.exitcode is None:
                continue
            finished_pid = pid
            if process.exitcode != 0:
                for other, _other_spec in active.values():
                    if other.is_alive():
                        other.terminate()
                for other, _other_spec in active.values():
                    other.join()
                raise RuntimeError(
                    f"episode shard {spec.shard_index} failed with exit code {process.exitcode}"
                )
            print(
                f"[shard-finished] shard={spec.shard_index} pid={pid} "
                f"episodes={spec.episode_start}:{spec.episode_end}",
                flush=True,
            )
            break
        if finished_pid is not None:
            del active[finished_pid]
        elif active:
            time.sleep(0.1)
    if gpu_admission is not None:
        active_jobs, active_startups, final_limit = gpu_admission.snapshot()
        print(
            "[gpu-admission] "
            f"finished=1 final_limit={final_limit} active={active_jobs} "
            f"startup={active_startups}",
            flush=True,
        )


def _install_shard_file(source: Path, destination: Path, expected_sha256: str) -> None:
    """Publish one immutable shard artifact without rewriting its contents."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != expected_sha256:
                raise RuntimeError(f"copied shard artifact hash mismatch: {source}")
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def merge_episode_shards(
    output_root: Path,
    conversion_plan: dict[str, Any],
    specs: list[EpisodeShardSpec],
    shard_roots: dict[int, Path],
    sync_report: dict[str, Any],
    *,
    task: str,
    verify_full_hash: bool,
) -> None:
    """Merge already-final global-index Parquets and rebuild ordered v2.1 metadata."""

    import pyarrow.parquet as pq
    from lerobot.datasets.utils import get_hf_features_from_features

    info_path = output_root / "meta" / "info.json"
    info = load_json_file(info_path)
    if info is None:
        raise RuntimeError(f"canonical merge metadata is missing: {info_path}")
    expected_schema = get_hf_features_from_features(
        {
            key: {**feature, "shape": tuple(feature["shape"])}
            for key, feature in info["features"].items()
        }
    ).arrow_schema
    episode_to_shard: dict[int, EpisodeShardSpec] = {}
    shard_reports: list[dict[str, Any]] = []
    for spec in specs:
        for episode_index in spec.episode_indices:
            if episode_index in episode_to_shard:
                raise RuntimeError(f"episode {episode_index} appears in multiple shards")
            episode_to_shard[episode_index] = spec
        shard_root = shard_roots[spec.shard_index]
        marker = load_json_file(shard_root / "episode_shard_complete.json")
        expected_marker = {
            "conversion_plan_hash": conversion_plan["plan_hash"],
            "assignment_hash": spec.assignment_hash,
            "episode_start": spec.episode_start,
            "episode_end": spec.episode_end,
            "frame_start": spec.frame_start,
            "frame_end": spec.frame_end,
        }
        if marker is None or marker.get("schema") != "instaumi-bimanual-episode-shard-complete-v1":
            raise RuntimeError(f"episode shard completion marker is invalid: {shard_root}")
        for key, value in expected_marker.items():
            if marker.get(key) != value:
                raise RuntimeError(f"episode shard completion mismatch for {key}: {shard_root}")
        mapping_path = shard_root / "source_frame_mapping.csv"
        if marker.get("source_frame_mapping_sha256") != sha256_file(mapping_path):
            raise RuntimeError(f"episode shard mapping hash mismatch: {mapping_path}")
        report = load_json_file(shard_root / "sync_report.json")
        if report is None or report.get("conversion_plan_hash") != conversion_plan["plan_hash"]:
            raise RuntimeError(f"episode shard sync report is invalid: {shard_root}")
        shard_reports.append(report)
    if sorted(episode_to_shard) != list(range(len(conversion_plan["episodes"]))):
        raise RuntimeError("episode shards do not cover the complete conversion plan exactly once")

    data_template = str(info["data_path"])
    chunk_size = int(info["chunks_size"])
    for episode_index in range(len(conversion_plan["episodes"])):
        spec = episode_to_shard[episode_index]
        shard_root = shard_roots[spec.shard_index]
        checkpoint_path = shard_root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json"
        checkpoint = load_json_file(checkpoint_path)
        if checkpoint is None or checkpoint.get("schema") != EPISODE_CHECKPOINT_SCHEMA:
            raise RuntimeError(f"episode shard checkpoint is invalid: {checkpoint_path}")
        if checkpoint.get("plan_hash") != conversion_plan["plan_hash"]:
            raise RuntimeError(f"episode shard checkpoint plan mismatch: {checkpoint_path}")
        if checkpoint.get("episode_plan") != conversion_plan["episodes"][episode_index]:
            raise RuntimeError(f"episode shard checkpoint assignment changed: {checkpoint_path}")
        parquet_record = checkpoint.get("parquet")
        if not isinstance(parquet_record, dict):
            raise RuntimeError(f"episode shard parquet record is missing: {checkpoint_path}")
        relative_path = data_template.format(
            episode_chunk=episode_index // chunk_size,
            episode_index=episode_index,
        )
        if parquet_record.get("relative_path") != relative_path:
            raise RuntimeError(f"episode shard parquet path mismatch: {checkpoint_path}")
        source_parquet = shard_root / relative_path
        parquet = pq.ParquetFile(source_parquet)
        planned_rows = int(conversion_plan["episodes"][episode_index]["frames"])
        if int(parquet.metadata.num_rows) != planned_rows:
            raise RuntimeError(f"episode shard parquet row mismatch: {source_parquet}")
        if not parquet.schema_arrow.equals(expected_schema, check_metadata=False):
            raise RuntimeError(f"episode shard parquet schema mismatch: {source_parquet}")
        if source_parquet.stat().st_size != int(parquet_record.get("size_bytes", -1)):
            raise RuntimeError(f"episode shard parquet size mismatch: {source_parquet}")
        expected_hash = str(parquet_record.get("sha256"))
        if verify_full_hash and sha256_file(source_parquet) != expected_hash:
            raise RuntimeError(f"episode shard parquet hash mismatch: {source_parquet}")
        destination = output_root / relative_path
        _install_shard_file(source_parquet, destination, expected_hash)
        atomic_write_json(
            output_root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json",
            checkpoint,
        )

    ordered_episodes: list[dict[str, Any]] = []
    ordered_stats: list[dict[str, Any]] = []
    total_frames = 0
    for episode_index, planned in enumerate(conversion_plan["episodes"]):
        checkpoint = load_json_file(
            output_root / ".instaumi_checkpoints" / f"episode_{episode_index:06d}.json"
        )
        if checkpoint is None:
            raise RuntimeError(f"canonical checkpoint missing after shard merge: {episode_index}")
        episode_record = checkpoint.get("episode_metadata")
        episode_stats = checkpoint.get("episode_stats")
        if not isinstance(episode_record, dict) or not isinstance(episode_stats, dict):
            raise RuntimeError(f"canonical checkpoint metadata invalid: {episode_index}")
        if int(episode_record.get("episode_index", -1)) != episode_index:
            raise RuntimeError(f"canonical episode metadata out of order: {episode_index}")
        if int(episode_record.get("length", -1)) != int(planned["frames"]):
            raise RuntimeError(f"canonical episode length mismatch: {episode_index}")
        ordered_episodes.append(episode_record)
        ordered_stats.append({"episode_index": episode_index, "stats": episode_stats})
        total_frames += int(planned["frames"])
    atomic_write_jsonlines(output_root / "meta" / "episodes.jsonl", ordered_episodes)
    atomic_write_jsonlines(output_root / "meta" / "episodes_stats.jsonl", ordered_stats)
    atomic_write_jsonlines(output_root / "meta" / "tasks.jsonl", [{"task_index": 0, "task": task}])
    total_episodes = len(ordered_episodes)
    info.update(
        {
            "total_episodes": total_episodes,
            "total_frames": total_frames,
            "total_tasks": 1,
            "total_videos": 0,
            "total_chunks": (total_episodes + chunk_size - 1) // chunk_size,
            "splits": {"train": f"0:{total_episodes}"},
        }
    )
    atomic_write_json(info_path, info)

    mapping_path = output_root / "source_frame_mapping.csv"
    mapping_tmp = mapping_path.parent / f".{mapping_path.name}.merge-{os.getpid()}-{uuid.uuid4().hex}"
    mapping_rows = 0
    expected_header: str | None = None
    try:
        with mapping_tmp.open("x", encoding="utf-8", newline="") as destination:
            for spec in specs:
                source_mapping = shard_roots[spec.shard_index] / "source_frame_mapping.csv"
                with source_mapping.open("r", encoding="utf-8", newline="") as source:
                    header = source.readline()
                    if expected_header is None:
                        expected_header = header
                        destination.write(header)
                    elif header != expected_header:
                        raise RuntimeError(f"episode shard mapping header mismatch: {source_mapping}")
                    for line in source:
                        destination.write(line)
                        mapping_rows += 1
            destination.flush()
            os.fsync(destination.fileno())
        if mapping_rows != total_frames:
            raise RuntimeError(f"merged source mapping rows mismatch: {mapping_rows} != {total_frames}")
        mapping_tmp.replace(mapping_path)
        mapping_directory_fd = os.open(mapping_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(mapping_directory_fd)
        finally:
            os.close(mapping_directory_fd)
    finally:
        mapping_tmp.unlink(missing_ok=True)

    merged_episode_reports = [
        item
        for report in shard_reports
        for item in report.get("episodes", [])
        if isinstance(item, dict)
    ]
    merged_episode_reports.sort(key=lambda item: int(item["episode_index"]))
    if [int(item["episode_index"]) for item in merged_episode_reports] != list(range(total_episodes)):
        raise RuntimeError("episode shard sync reports are incomplete or out of order")
    sync_report["episodes"] = merged_episode_reports
    sync_report["processing"]["episode_shards"] = {
        "mode": "isolated_chapter_dependency_ranges_then_ordered_merge",
        "shard_count": len(specs),
        "shards": [spec.to_dict() for spec in specs],
    }
    sync_report["summary"] = {
        "episodes": total_episodes,
        "frames": total_frames,
        "episode_parquets": total_episodes,
        "parquet_rows": total_frames,
    }
    atomic_write_json(output_root / "sync_report.json", sync_report)
    prepare_resume_root(
        output_root,
        conversion_plan,
        verify_full_hash=verify_full_hash,
    )


def run(args: argparse.Namespace) -> None:
    if not hasattr(args, "gripper_zero_below_m"):
        args.gripper_zero_below_m = None
    if not hasattr(args, "gripper_binary_threshold_m"):
        args.gripper_binary_threshold_m = None
    if not hasattr(args, "panorama_numeric_only"):
        args.panorama_numeric_only = False
    dataset_fps = output_dataset_fps(args)
    print(
        "[downsample] "
        f"source_fps={args.fps} steps={args.down_sample_steps} output_fps={dataset_fps} "
        "phase=0 policy=align_full_rate_then_uniformly_decimate_all_modalities",
        flush=True,
    )
    validate_output_root(args)
    configure_fast_work_root(args)
    requested_output_root = args.output_root.expanduser().resolve()
    if not args.dry_run and requested_output_root.exists() and not args.overwrite and not args.resume:
        raise FileExistsError(
            f"Output root already exists: {requested_output_root}. Use a new path or --overwrite explicitly."
        )
    if (
        not args.dry_run
        and requested_output_root.exists()
        and args.resume
        and not args.overwrite
        and not (requested_output_root / "conversion_plan.json").is_file()
    ):
        raise RuntimeError(
            f"existing output predates safe resume or is still being produced: {requested_output_root}; "
            "do not touch it, and use a new OUTPUT_ROOT for the optimized conversion"
        )
    for name in (
        "max_camera_time_diff_ms",
        "hard_camera_time_diff_ms",
        "max_htc_nearest_gap_ms",
        "max_htc_bracket_gap_ms",
        "hard_gap_reject_ms",
        "max_host_clock_offset_step_ms",
    ):
        if float(getattr(args, name)) <= 0.0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    if args.gripper_zero_below_m is not None:
        if not np.isfinite(args.gripper_zero_below_m) or args.gripper_zero_below_m <= 0.0:
            raise ValueError("--gripper-zero-below-m must be a finite positive value")
    if args.gripper_binary_threshold_m is not None:
        if (
            not np.isfinite(args.gripper_binary_threshold_m)
            or args.gripper_binary_threshold_m <= 0.0
        ):
            raise ValueError(
                "--gripper-binary-threshold-m must be a finite positive value"
            )
    gripper_transform = gripper_transform_from_args(args)
    if args.max_camera_time_diff_ms > args.hard_camera_time_diff_ms:
        raise ValueError(
            "--max-camera-time-diff-ms (soft reporting threshold) cannot exceed "
            "--hard-camera-time-diff-ms"
        )
    if args.video_seek_threshold_frames < 0:
        raise ValueError("--video-seek-threshold-frames must be non-negative")
    if not 0 <= args.png_compress_level <= 9:
        raise ValueError("--png-compress-level must be in [0, 9]")
    if args.fast_work_reserve_gb < 0:
        raise ValueError("--fast-work-reserve-gb must be non-negative")
    if args.output_reserve_gb < 0:
        raise ValueError("--output-reserve-gb must be non-negative")
    if args.stats_image_threads < 0:
        raise ValueError("--stats-image-threads must be non-negative")
    if args.episode_write_batch_frames <= 0:
        raise ValueError("--episode-write-batch-frames must be positive")
    if args.episode_shard_workers < 0:
        raise ValueError("--episode-shard-workers must be non-negative")
    if args.direct_openpi_images:
        if not args.direct_from_raw:
            raise ValueError("--direct-openpi-images requires --direct-from-raw")
        if (args.image_width, args.image_height) != (224, 224):
            raise ValueError("--direct-openpi-images requires --image-width=224 --image-height=224")
        if args.preview_every_n != 0:
            raise ValueError("--direct-openpi-images requires --preview-every-n=0")
        if not 1 <= args.direct_image_batch_frames <= 1024:
            raise ValueError("--direct-image-batch-frames must be in [1, 1024]")
        if not 1 <= args.direct_image_export_workers <= 8:
            raise ValueError("--direct-image-export-workers must be in [1, 8]")
        if not 1 <= args.direct_image_panorama_export_workers <= 12:
            raise ValueError(
                "--direct-image-panorama-export-workers must be in [1, 12]"
            )
        if args.direct_image_panorama_bitrate <= 0:
            raise ValueError("--direct-image-panorama-bitrate must be positive")
        if args.direct_image_panorama_timeout_sec < 0:
            raise ValueError("--direct-image-panorama-timeout-sec must be non-negative")
        if args.direct_image_gpu_max_workers < 0:
            raise ValueError("--direct-image-gpu-max-workers must be non-negative")
        if args.direct_image_gpu_startup_workers < 0:
            raise ValueError("--direct-image-gpu-startup-workers must be non-negative")
        if args.direct_image_gpu_min_workers < 0:
            raise ValueError("--direct-image-gpu-min-workers must be non-negative")
        if not 1 <= args.direct_image_gpu_startup_progress <= 100:
            raise ValueError("--direct-image-gpu-startup-progress must be in [1, 100]")
        if args.direct_image_gpu_oom_retries < 0:
            raise ValueError("--direct-image-gpu-oom-retries must be non-negative")
        if args.direct_image_temp_margin_gb < 0:
            raise ValueError("--direct-image-temp-margin-gb must be non-negative")
    if args.panorama_numeric_only and not args.direct_openpi_images:
        raise ValueError("--panorama-numeric-only requires --direct-openpi-images")
    os.environ["INSTAUMI_PNG_COMPRESS_LEVEL"] = str(args.png_compress_level)
    os.environ["INSTAUMI_STATS_IMAGE_THREADS"] = str(args.stats_image_threads)
    if args.direct_from_raw:
        required_direct = ["right_episode", "left_episode"]
        if args.right_pose_sensor == "htc":
            required_direct.append("right_htc_csv")
        if args.left_pose_sensor == "htc":
            required_direct.append("left_htc_csv")
        missing_direct = [name for name in required_direct if getattr(args, name) is None]
        if missing_direct:
            raise ValueError(f"direct_from_raw requires: {', '.join('--' + name.replace('_', '-') for name in missing_direct)}")
        right = make_direct_hand_input(
            "right",
            args.right_episode,
            args.tmp_root,
            args.right_htc_csv,
            args.right_pose_sensor,
            args.right_htc_role,
            args.right_frame_timestamps,
            args.require_calibrated_timestamps,
            args.direct_openpi_images,
        )
        left = make_direct_hand_input(
            "left",
            args.left_episode,
            args.tmp_root,
            args.left_htc_csv,
            args.left_pose_sensor,
            args.left_htc_role,
            args.left_frame_timestamps,
            args.require_calibrated_timestamps,
            args.direct_openpi_images,
        )
    else:
        if args.right_input_root is None or args.left_input_root is None:
            raise ValueError("Legacy mode requires --right-input-root and --left-input-root")
        right = make_hand_input(
            "right",
            args.right_input_root,
            args.episode_glob,
            args.right_pose_sensor,
            args.right_frame_timestamps,
            args.require_calibrated_timestamps,
        )
        left = make_hand_input(
            "left",
            args.left_input_root,
            args.episode_glob,
            args.left_pose_sensor,
            args.left_frame_timestamps,
            args.require_calibrated_timestamps,
        )
    if right.timestamps_path is None or left.timestamps_path is None:
        raise RuntimeError("Both hands require a frame timestamp timeline")

    calibration_report: dict[str, Any] | None = None
    if args.require_calibrated_timestamps:
        calibration_report = validate_accepted_calibration_report(
            args.sync_calibration_report,
            right.timestamps_path,
            left.timestamps_path,
            session_dir=args.session_dir,
            right_htc_path=right.tracker_path,
            left_htc_path=left.tracker_path,
            timestamp_log_path=args.timestamp_log,
        )
    elif args.sync_calibration_report is not None and args.sync_calibration_report.expanduser().is_file():
        calibration_report = json.loads(args.sync_calibration_report.expanduser().read_text(encoding="utf-8"))

    # Fail closed on calibration identity/status and direct-image provenance
    # before performing the large one-time source cache copy.
    source_direct_image_plans: dict[str, DirectRawOpenPIImagePlan] | None = None
    runtime_direct_image_plans: (
        dict[str, DirectRawOpenPIImagePlan | DirectOpenPIImagePlan] | None
    ) = None
    if args.direct_openpi_images:
        inherited_source_plans = getattr(
            args, "_episode_shard_source_plans", None
        )
        if inherited_source_plans is not None:
            source_direct_image_plans = dict(inherited_source_plans)
            if set(source_direct_image_plans) != {"right", "left"}:
                raise RuntimeError("inherited shard source plans are incomplete")
        else:
            source_direct_image_plans = {
                "right": build_hand_direct_raw_openpi_plan(right, args),
                "left": build_hand_direct_raw_openpi_plan(left, args),
            }
        validate_direct_plan_source_sets(source_direct_image_plans)

    print(
        f"[timestamps] right source={right.timestamps_source} path={right.timestamps_path} "
        f"require_calibrated={args.require_calibrated_timestamps}"
    )
    print(
        f"[timestamps] left source={left.timestamps_source} path={left.timestamps_path} "
        f"require_calibrated={args.require_calibrated_timestamps}"
    )
    right_timeline = load_frame_timeline(right.timestamps_path)
    left_timeline = load_frame_timeline(left.timestamps_path)
    if source_direct_image_plans is not None:
        for arm, timeline in (("right", right_timeline), ("left", left_timeline)):
            validate_direct_raw_timeline(arm, source_direct_image_plans[arm], timeline)

    # Read and authenticate segment-level `_03` evidence before loading the
    # gripper CSV.  In particular, a rejected zero-detection segment may be
    # entirely blank and must be discarded as a whole rather than causing a
    # global parse failure or being filled with a synthetic width.
    gripper_quality_by_arm = {
        "right": load_continuous_gripper_segment_quality(right.gripper_path),
        "left": load_continuous_gripper_segment_quality(left.gripper_path),
    }
    for arm, quality in gripper_quality_by_arm.items():
        if quality.get("enabled"):
            print(
                f"[gripper-quality] arm={arm} status={quality['status']} "
                f"accepted={len(quality['accepted_segment_indices'])} "
                f"rejected={len(quality['rejected_segment_indices'])} "
                "policy=reject_complete_source_segment",
                flush=True,
            )

    right_states = load_hand_states(
        right,
        args,
        video_frame_count_override=(
            None
            if source_direct_image_plans is None
            else source_direct_image_plans["right"].total_frames
        ),
        gripper_quality=gripper_quality_by_arm["right"],
    )
    left_states = load_hand_states(
        left,
        args,
        video_frame_count_override=(
            None
            if source_direct_image_plans is None
            else source_direct_image_plans["left"].total_frames
        ),
        gripper_quality=gripper_quality_by_arm["left"],
    )
    if args.require_calibrated_timestamps:
        assert calibration_report is not None
        validate_calibrated_htc_time_source(calibration_report, "right", right_states.pose_quality)
        validate_calibrated_htc_time_source(calibration_report, "left", left_states.pose_quality)
    right_video_frames = right_states.video_frames
    left_video_frames = left_states.video_frames

    if args.direct_from_raw:
        def usable_direct_frames(hand: HandInput, states: HandStates) -> int:
            usable = min(states.pose_count, len(states.grippers), states.video_frames)
            if states.pose_count == states.video_frames and len(states.grippers) == states.video_frames:
                return states.video_frames
            if not args.allow_partial_inputs:
                raise RuntimeError(
                    f"{hand.name} direct inputs are not frame-aligned: "
                    f"poses={states.pose_count}, grippers={len(states.grippers)}, "
                    f"video_frames={states.video_frames}. "
                    "Run _03_gripper_estimation for the full episode before _05, or pass --allow-partial-inputs."
                )
            if usable < 2:
                raise RuntimeError(
                    f"{hand.name} partial direct inputs have fewer than 2 usable frames: "
                    f"poses={states.pose_count}, grippers={len(states.grippers)}, "
                    f"video_frames={states.video_frames}."
                )
            print(
                f"[partial] {hand.name}: using {usable} frames "
                f"from poses={states.pose_count}, grippers={len(states.grippers)}, "
                f"video_frames={states.video_frames}"
            )
            return usable

        right_video_frames = usable_direct_frames(right, right_states)
        left_video_frames = usable_direct_frames(left, left_states)

    segments = build_segment_ranges(right, left, right_video_frames, left_video_frames, args)
    print(f"[segments] count={len(segments)} split_by_segments={args.split_by_segments}")

    planned_runs: list[tuple[SegmentRange, FrameRun]] = []
    segment_reports: list[dict[str, Any]] = []
    for segment in segments:
        pair_result = build_frame_pairs(right, left, segment, args)
        gripper_quality_rejections = continuous_gripper_segment_rejections(
            segment.segment_index,
            gripper_quality_by_arm,
        )
        if gripper_quality_rejections:
            runs = []
            # Do not inspect, interpolate, or otherwise consume per-frame
            # widths from a rejected demonstration.  This is deliberately
            # decided before split_frame_pair_runs() so an all-empty bad range
            # cannot abort planning and cannot leak a fabricated value.
            run_report = {
                "status": "rejected_gripper_quality",
                "episode_semantics": "one_timestamp_log_segment_to_at_most_one_lerobot_episode",
                "input_pairs": len(pair_result.pairs),
                "valid_runs": 0,
                "valid_pairs": 0,
                "frames_to_write": 0,
                "invalid_pose_pairs": 0,
                "missing_gripper_pairs": None,
                "source_discontinuities": 0,
                "discarded_short_run_pairs": 0,
                "hard_gap_reject_ms": args.hard_gap_reject_ms,
                "hard_rejections": gripper_quality_rejections,
                "gripper_values_examined": 0,
                "runs": [],
            }
        else:
            runs, run_report = split_frame_pair_runs(
                segment,
                pair_result.pairs,
                right_states,
                left_states,
                right_timeline,
                left_timeline,
                args.hard_gap_reject_ms,
                pair_result.grid_unix_time_ms,
                args.hard_camera_time_diff_ms,
            )
        if runs:
            if len(runs) != 1:
                raise RuntimeError("one source segment unexpectedly produced multiple runs")
            source_pair_count = len(runs[0].pairs)
            runs = [downsample_frame_run(runs[0], args.down_sample_steps)]
            output_pair_count = len(runs[0].pairs)
            run_report["downsampling"] = {
                "method": "uniform_row_decimation_after_full_rate_time_alignment",
                "source_fps": int(args.fps),
                "down_sample_steps": int(args.down_sample_steps),
                "output_fps": output_dataset_fps(args),
                "phase": 0,
                "source_pair_count": source_pair_count,
                "output_pair_count": output_pair_count,
            }
            run_report["valid_pairs"] = output_pair_count
            run_report["frames_to_write"] = output_pair_count - 1
            run_report["runs"] = [
                {
                    "run_index": runs[0].run_index,
                    "pair_count": output_pair_count,
                    "first_pair": list(runs[0].pairs[0]),
                    "last_pair": list(runs[0].pairs[-1]),
                }
            ]
        segment_reports.append(
            {"segment_index": segment.segment_index, "pairing": pair_result.report, "runs": run_report}
        )
        if not runs:
            print(
                f"[reject] segment={segment.segment_index} will not produce an episode: "
                f"{json.dumps(run_report['hard_rejections'], ensure_ascii=False, allow_nan=False)}"
            )
        planned_runs.extend((segment, run) for run in runs)

    if len(planned_runs) > len(segments):
        raise RuntimeError(
            "internal episode-semantics violation: one timestamp_log segment produced multiple episodes"
        )
    rejected_segment_reports = [
        {
            "segment_index": item["segment_index"],
            "reasons": item["runs"].get("hard_rejections", []),
        }
        for item in segment_reports
        if item["runs"].get("status") != "accepted"
    ]
    print(
        f"[segments] accepted={len(planned_runs)} rejected={len(rejected_segment_reports)} "
        "policy=one_timestamp_log_segment_to_at_most_one_lerobot_episode"
    )

    # Planning above reads only authenticated numeric/timestamp evidence.  An
    # empty plan is terminal: fail before staging/caching any media, exporting
    # panoramas, creating a dataset, or constructing a frame reader.
    require_planned_runs_before_media(
        planned_runs,
        source_segment_count=len(segments),
    )

    if source_direct_image_plans is not None:
        fast_workspace, fast_workspace_report = prepare_direct_raw_workspace(
            source_direct_image_plans,
            args,
        )
    else:
        right, left, fast_workspace, fast_workspace_report = maybe_cache_camera_videos(
            right, left, args
        )

    conversion_plan = build_conversion_plan(
        args,
        right,
        left,
        planned_runs,
        calibration_report,
        fast_workspace_report,
        source_direct_image_plans,
    )
    worker_shard: EpisodeShardSpec | None = getattr(args, "_episode_shard_spec", None)
    active_planned_runs = (
        planned_runs[worker_shard.episode_start : worker_shard.episode_end]
        if worker_shard is not None
        else planned_runs
    )
    active_episode_start = 0 if worker_shard is None else worker_shard.episode_start
    active_frame_start = 0 if worker_shard is None else worker_shard.frame_start

    calibration_summary = None
    if calibration_report is not None:
        calibration_summary = {
            "path": None
            if args.sync_calibration_report is None
            else str(args.sync_calibration_report.expanduser().resolve()),
            "schema": calibration_report.get("schema"),
            "status": calibration_report.get("status"),
            "created_utc": calibration_report.get("created_utc"),
            "calibration_id": calibration_report.get("calibration_id"),
            # The temporary calibration directory is removed after a
            # successful conversion.  Embed the accepted fit, its window
            # residuals and input identities so the published dataset remains
            # independently auditable instead of retaining a dead path.
            "embedded_report": json_compatible(calibration_report),
        }
    sync_report: dict[str, Any] = {
        "schema": "instaumi-bimanual-sync-report-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_id": None if calibration_report is None else calibration_report.get("calibration_id"),
        "conversion_plan_hash": conversion_plan["plan_hash"],
        "calibration": calibration_summary,
        "require_calibrated_timestamps": bool(args.require_calibrated_timestamps),
        "thresholds": {
            "max_camera_time_diff_ms": args.max_camera_time_diff_ms,
            "hard_camera_time_diff_ms": args.hard_camera_time_diff_ms,
            "max_htc_nearest_gap_ms": args.max_htc_nearest_gap_ms,
            "max_htc_bracket_gap_ms": args.max_htc_bracket_gap_ms,
            "hard_gap_reject_ms": args.hard_gap_reject_ms,
            "max_host_clock_offset_step_ms": args.max_host_clock_offset_step_ms,
            "gripper_zero_below_m": args.gripper_zero_below_m,
            "gripper_binary_threshold_m": args.gripper_binary_threshold_m,
        },
        "gripper_transform": gripper_transform,
        "source_alignment_fps": args.fps,
        "down_sample_steps": args.down_sample_steps,
        "nominal_lerobot_fps": output_dataset_fps(args),
        "reserved_timestamp_semantics": "episode frame_index / nominal_lerobot_fps",
        "episode_semantics": {
            "policy": "one_timestamp_log_segment_to_at_most_one_lerobot_episode",
            "input_segments": len(segments),
            "accepted_episodes": len(planned_runs),
            "rejected_segments": len(segments) - len(planned_runs),
            "alignment": args.camera_pairing_mode,
            "source_frame_reuse": {
                "right": "forbidden_by_right_anchor"
                if args.camera_pairing_mode == "right_anchor_nearest"
                else "allowed_for_legacy_uniform_grid",
                "left": "allowed_only_when_nearest_pairing_requires_it",
            },
        },
        "processing": {
            "png_format": None if args.panorama_numeric_only else "lossless",
            "png_compress_level": args.png_compress_level,
            "image_source_mode": (
                "direct_raw_panorama_numeric_only"
                if args.panorama_numeric_only
                else (
                    "direct_raw_openpi" if args.direct_openpi_images else "legacy_six_videos"
                )
            ),
            "direct_openpi_images": bool(args.direct_openpi_images),
            "panorama_numeric_only": bool(args.panorama_numeric_only),
            "legacy_six_view_columns_generated": not bool(args.panorama_numeric_only),
            "direct_image_batch_frames": (
                args.direct_image_batch_frames if args.direct_openpi_images else None
            ),
            "direct_image_export_workers": (
                args.direct_image_export_workers if args.direct_openpi_images else None
            ),
            "direct_image_gpu_admission": (
                {
                    "max_workers": args.direct_image_gpu_max_workers,
                    "startup_workers": args.direct_image_gpu_startup_workers,
                    "minimum_workers": args.direct_image_gpu_min_workers,
                    "startup_progress": args.direct_image_gpu_startup_progress,
                    "oom_retries": args.direct_image_gpu_oom_retries,
                }
                if args.direct_openpi_images and args.direct_image_cuda
                else None
            ),
            "direct_image_parallel_arms": (
                bool(args.direct_image_parallel_arms) if args.direct_openpi_images else None
            ),
            # This cache is an implementation-only accelerator for the
            # materialized six-view path.  Panorama-numeric conversion never
            # reads it, so normalize the report instead of recording the
            # caller's stale `require`/`auto` value.  This keeps an `_03`
            # checkpoint started by an older shell resumable after the
            # panorama launcher permanently switches the unused cache off.
            "direct_image_front_cache_mode": normalized_front_cache_report_mode(args),
            "direct_image_pipeline": (
                {
                    "bounded_lookahead_batches": 1,
                    "batch_scope": "all_pending_episodes_in_source_order",
                    "front_decode_overlaps_mediasdk": True,
                    "next_export_overlaps_v360_resize": True,
                    "side_frame_workers_per_arm": args.direct_image_resize_workers,
                    "v360_filter_threads_per_worker": args.direct_image_v360_filter_threads,
                }
                if args.direct_openpi_images
                else None
            ),
            "direct_image_recipe_plan_hashes": (
                None
                if source_direct_image_plans is None
                else {
                    arm: plan.plan_hash for arm, plan in source_direct_image_plans.items()
                }
            ),
            "embed_images_in_memory": bool(args.embed_images_in_memory),
            "streaming_dataset_write": bool(args.streaming_dataset_write),
            "persistent_video_readers": bool(args.persistent_video_readers),
            "video_seek_threshold_frames": args.video_seek_threshold_frames,
            "stats_image_threads": args.stats_image_threads,
            "fast_workspace": fast_workspace_report,
            "state_alignment": {
                "clock": (
                    "midpoint_of_actual_right_anchor_and_nearest_left_exposure_times"
                    if args.camera_pairing_mode == "right_anchor_nearest"
                    else "shared_uniform_grid"
                ),
                "htc_pose": "position_linear_and_quaternion_slerp_from_raw_htc_timestamps",
                "gripper": "linear_interpolation_on_calibrated_camera_timeline",
                "images": "nearest_calibrated_camera_source_frame",
            },
        },
        "right": {
            "timestamps_source": right.timestamps_source,
            "timeline": right_timeline.report(),
            "pose_alignment": right_states.pose_quality.report,
        },
        "left": {
            "timestamps_source": left.timestamps_source,
            "timeline": left_timeline.report(),
            "pose_alignment": left_states.pose_quality.report,
        },
        "segments": segment_reports,
        "rejected_segments": rejected_segment_reports,
        "episodes": [],
        "source_mapping_csv": "source_frame_mapping.csv",
    }

    front_cache_manifests = (
        resolve_front_openpi_cache_manifests(right, left, args)
        if args.direct_openpi_images and not args.panorama_numeric_only and not args.dry_run
        else None
    )
    sync_report["processing"]["direct_image_front_cache_used"] = bool(
        front_cache_manifests
    )

    resume_episode_count = 0
    resume_frame_count = 0
    publish_fast_output = False
    publish_local_output = False
    resume_existing_final = False
    if args.dry_run:
        dataset = None
        mapping_handle = None
        mapping_writer = None
        mapping_tmp = None
    else:
        LeRobotDataset, _VideoEncodingManager, _encode_video_frames, _safe_stop_image_writer, version = import_lerobot(
            args.lerobot_env.expanduser()
        )
        print(f"[info] using LeRobot codebase_version={version} from {args.lerobot_env.expanduser()}")
        resume_existing_final = (
            requested_output_root.is_dir()
            and not args.overwrite
            and args.resume
            and conversion_plan_matches(requested_output_root, conversion_plan)
        )
        if resume_existing_final:
            output_root = requested_output_root
            print(f"[resume] using existing final output: {output_root}", flush=True)
        elif fast_workspace is not None:
            if requested_output_root.exists() and not args.overwrite:
                raise RuntimeError(
                    f"existing output does not match this conversion plan: {requested_output_root}; "
                    "use a new OUTPUT_ROOT or OVERWRITE=1"
                )
            # Keep an existing final dataset untouched while the replacement
            # is built and fully validated.  Cross-filesystem publication first
            # creates a verified sibling candidate on the final filesystem,
            # then atomically exchanges the two directory names.
            output_parent = fast_workspace.root / "output-work"
            output_parent.mkdir(parents=True, exist_ok=True)
            output_key = hashlib.sha256(str(requested_output_root).encode("utf-8")).hexdigest()[:12]
            output_root = output_parent / (
                f"{requested_output_root.name}-{output_key}-{conversion_plan['plan_hash'][:12]}.working"
            )
            publish_fast_output = True
            fast_workspace_report["working_output"] = str(output_root)
            fast_workspace_report["final_output"] = str(requested_output_root)
            # Preview and mapping helpers use args.output_root, so direct all
            # intermediate writes to the private NVMe work directory too.
            args.output_root = output_root
        elif requested_output_root.exists() and args.overwrite:
            # A forced rebuild must never resume or delete the suspect final
            # dataset.  Build in a deterministic same-filesystem sibling so a
            # power loss can resume that private work; swap only after complete
            # validation, preserving the prior final as a backup.
            output_root = requested_output_root.parent / (
                f".{requested_output_root.name}.working-{conversion_plan['plan_hash'][:12]}"
            )
            publish_local_output = True
            args.output_root = output_root
        else:
            output_root = requested_output_root

        resume_output = output_root.is_dir() and args.resume and conversion_plan_matches(output_root, conversion_plan)
        if output_root.exists() and not resume_output:
            if output_root == requested_output_root:
                remove_output_root(output_root, args.overwrite)
            elif args.overwrite:
                remove_output_root(output_root, True)
            else:
                raise RuntimeError(
                    f"fast work output exists but is not resumable: {output_root}; "
                    "set OVERWRITE=1 to discard only this generated work directory"
                )

        if resume_output:
            if worker_shard is None:
                resume_episode_count, resume_frame_count = prepare_resume_root(
                    output_root,
                    conversion_plan,
                    verify_full_hash=args.verify_resume_full_hash,
                )
            else:
                resume_episode_count, resume_frame_count = prepare_resume_shard_root(
                    output_root,
                    conversion_plan,
                    worker_shard,
                    task=args.task,
                    verify_full_hash=args.verify_resume_full_hash,
                )
                # A process killed before its first episode leaves only seeded
                # metadata.  Recreate that private generated root because the
                # stock metadata loader cannot aggregate an empty v2.1 stats log.
                if resume_episode_count == worker_shard.episode_start:
                    remove_output_root(output_root, True)
                    resume_output = False
            planned_frame_count = sum(
                int(item["frames"])
                for item in conversion_plan["episodes"][active_episode_start : (
                    len(conversion_plan["episodes"])
                    if worker_shard is None
                    else worker_shard.episode_end
                )]
            )
        if resume_output:
            output_space_report = preflight_episode_output_space(
                output_root,
                remaining_frames=(active_frame_start + planned_frame_count) - resume_frame_count,
                image_width=args.image_width,
                image_height=args.image_height,
                reserve_gb=(args.fast_work_reserve_gb if publish_fast_output else args.output_reserve_gb),
                image_feature_count=0 if args.panorama_numeric_only else len(CAMERA_KEYS),
            )
            dataset = LeRobotDataset.resume_for_writing(
                repo_id=args.repo_id,
                root=output_root,
                batch_encoding_size=args.batch_encoding_size,
                image_writer_processes=args.image_writer_processes,
                image_writer_threads=args.image_writer_threads,
                embed_images_in_memory=args.embed_images_in_memory,
            )
            print(
                f"[resume] committed_episodes={resume_episode_count} "
                f"committed_frames={resume_frame_count} root={output_root}",
                flush=True,
            )
        else:
            planned_frame_count = sum(
                int(item["frames"])
                for item in conversion_plan["episodes"][active_episode_start : (
                    len(conversion_plan["episodes"])
                    if worker_shard is None
                    else worker_shard.episode_end
                )]
            )
            output_space_report = preflight_episode_output_space(
                output_root,
                remaining_frames=planned_frame_count,
                image_width=args.image_width,
                image_height=args.image_height,
                reserve_gb=(args.fast_work_reserve_gb if publish_fast_output else args.output_reserve_gb),
                image_feature_count=0 if args.panorama_numeric_only else len(CAMERA_KEYS),
            )
            dataset = LeRobotDataset.create(
                repo_id=args.repo_id,
                fps=output_dataset_fps(args),
                root=output_root,
                robot_type=args.robot_type,
                use_videos=False,
                features=output_features(args),
                batch_encoding_size=args.batch_encoding_size,
                image_writer_processes=args.image_writer_processes,
                image_writer_threads=args.image_writer_threads,
                embed_images_in_memory=args.embed_images_in_memory,
            )
            if worker_shard is not None:
                chunk_size = int(dataset.meta.info["chunks_size"])
                dataset.meta.info["total_episodes"] = worker_shard.episode_start
                dataset.meta.info["total_frames"] = worker_shard.frame_start
                dataset.meta.info["total_chunks"] = (
                    worker_shard.episode_start + chunk_size - 1
                ) // chunk_size
                dataset.meta.info["splits"] = {
                    "train": f"0:{worker_shard.episode_start}"
                }
                if dataset.meta.get_task_index(args.task) is None:
                    dataset.meta.add_task(args.task)
                dataset.episode_buffer = dataset.create_episode_buffer(
                    episode_index=worker_shard.episode_start
                )
                dataset.meta.commit_info()
                atomic_write_json(
                    output_root / "episode_shard_plan.json",
                    {
                        **worker_shard.to_dict(),
                        "conversion_plan_hash": conversion_plan["plan_hash"],
                    },
                )
            atomic_write_json(output_root / "conversion_plan.json", conversion_plan)
        sync_report["processing"]["output_space_preflight"] = output_space_report
        if args.streaming_dataset_write:
            # Each episode parquet is self contained.  Retaining the concatenated
            # embedded PNG bytes serves no purpose during offline creation and
            # makes memory usage grow with the complete recording.
            dataset.hf_dataset = None
        if args.disable_lerobot_combined_video and hasattr(dataset, "_save_episode_as_video"):
            dataset._save_episode_as_video = lambda *unused_args, **unused_kwargs: None

        if resume_existing_final and resume_episode_count == len(conversion_plan["episodes"]) and worker_shard is None:
            expected_frames = sum(int(item["frames"]) for item in conversion_plan["episodes"])
            # prepare_resume_root already checked every committed Parquet footer,
            # row count, and Arrow schema (plus optional full hashes).  Avoid a
            # second complete footer scan when this published output is done.
            validate_conversion_complete(
                output_root,
                conversion_plan,
                episodes=resume_episode_count,
                frames=expected_frames,
            )
            if getattr(dataset, "image_writer", None) is not None:
                dataset.stop_image_writer()
            print(
                f"[skip] validated complete output without rewriting published files: {output_root}",
                flush=True,
            )
            return

        if source_direct_image_plans is not None:
            prestaged_plans = getattr(args, "_episode_shard_runtime_plans", None)
            if worker_shard is not None and prestaged_plans is not None:
                runtime_direct_image_plans = dict(prestaged_plans)
                if set(runtime_direct_image_plans) != {"right", "left"}:
                    raise RuntimeError("pre-staged shard runtime plans are incomplete")
                for arm in ("right", "left"):
                    if runtime_direct_image_plans[arm].total_frames != (
                        source_direct_image_plans[arm].total_frames
                    ):
                        raise RuntimeError(f"pre-staged {arm} shard image plan changed")
                fast_workspace_report["staging_status"] = "pre_staged_by_parent"
            else:
                runtime_direct_image_plans = stage_direct_raw_insv(
                    source_direct_image_plans,
                    args,
                    fast_workspace,
                    fast_workspace_report,
                )

        mapping_tmp = output_root / "source_frame_mapping.csv.tmp"
        mapping_handle = mapping_tmp.open("w", newline="", encoding="utf-8")
        mapping_writer = csv.DictWriter(mapping_handle, fieldnames=SOURCE_MAPPING_FIELDS)
        mapping_writer.writeheader()

    if (
        not args.dry_run
        and worker_shard is None
        and args.direct_openpi_images
        and not args.panorama_numeric_only
        and args.episode_shards != "off"
    ):
        if source_direct_image_plans is None or runtime_direct_image_plans is None:
            raise RuntimeError("episode shard planning requires direct raw source/runtime plans")
        shard_specs = build_episode_shard_specs(
            planned_runs,
            source_direct_image_plans,
            total_export_workers=2 * int(args.direct_image_export_workers),
            total_batch_arm_frames=2 * int(args.direct_image_batch_frames),
            total_resize_workers=2 * int(args.direct_image_resize_workers),
            max_shard_workers=int(args.episode_shard_workers),
        )
        if len(shard_specs) > 1:
            assert dataset is not None and mapping_handle is not None and mapping_tmp is not None
            mapping_handle.flush()
            os.fsync(mapping_handle.fileno())
            mapping_handle.close()
            mapping_handle = None
            mapping_writer = None
            mapping_tmp.unlink(missing_ok=True)
            if getattr(dataset, "image_writer", None) is not None:
                dataset.stop_image_writer()
            live_workers = min(
                len(shard_specs),
                int(args.episode_shard_workers) if args.episode_shard_workers else len(shard_specs),
                max(1, (2 * int(args.direct_image_export_workers)) // 2),
                max(1, (2 * int(args.direct_image_resize_workers)) // 2),
                max(1, (2 * int(args.direct_image_batch_frames)) // (2 * 64)),
            )
            shard_base = (
                fast_workspace.root / "episode-shards"
                if fast_workspace is not None
                else output_root.parent / f".{output_root.name}.episode-shards"
            )
            shard_group = shard_base / (
                f"{hashlib.sha256(str(requested_output_root).encode('utf-8')).hexdigest()[:12]}-"
                f"{conversion_plan['plan_hash'][:16]}"
            )
            shard_roots = {
                spec.shard_index: shard_group
                / (
                    f"shard-{spec.shard_index:03d}-"
                    f"ep-{spec.episode_start:06d}-{spec.episode_end:06d}-"
                    f"{spec.assignment_hash[:12]}"
                )
                for spec in shard_specs
            }
            sync_report["processing"]["episode_shards"] = {
                "mode": args.episode_shards,
                "live_workers": live_workers,
                "global_export_workers": 2 * int(args.direct_image_export_workers),
                "global_batch_arm_frames": 2 * int(args.direct_image_batch_frames),
                "global_resize_workers": 2 * int(args.direct_image_resize_workers),
                "shards": [spec.to_dict() for spec in shard_specs],
            }
            print(
                f"[shards] count={len(shard_specs)} live_workers={live_workers} "
                f"global_export_workers={2 * int(args.direct_image_export_workers)} "
                f"global_batch_arm_frames={2 * int(args.direct_image_batch_frames)} "
                f"global_resize_workers={2 * int(args.direct_image_resize_workers)}",
                flush=True,
            )
            run_episode_shard_workers(
                args,
                shard_specs,
                shard_roots,
                source_direct_image_plans,
                runtime_direct_image_plans,
                max_live_workers=live_workers,
            )
            merge_episode_shards(
                output_root,
                conversion_plan,
                shard_specs,
                shard_roots,
                sync_report,
                task=args.task,
                verify_full_hash=args.verify_resume_full_hash,
            )
            merged_dataset = LeRobotDataset.resume_for_writing(
                repo_id=args.repo_id,
                root=output_root,
                batch_encoding_size=args.batch_encoding_size,
                image_writer_processes=0,
                image_writer_threads=0,
                embed_images_in_memory=False,
            )
            total_frames = sum(int(item["frames"]) for item in conversion_plan["episodes"])
            validate_written_episode_tables(
                merged_dataset,
                len(conversion_plan["episodes"]),
                total_frames,
            )
            validate_written_episode_table_range(
                merged_dataset,
                conversion_plan,
                0,
                len(conversion_plan["episodes"]),
            )
            write_conversion_complete(
                output_root,
                conversion_plan,
                episodes=len(conversion_plan["episodes"]),
                frames=total_frames,
            )
            completed_output = output_root
            if publish_fast_output:
                publish_result, previous_backup = publish_fast_workspace_output(
                    fast_workspace,
                    completed_output,
                    requested_output_root,
                    overwrite=args.overwrite,
                    reserve_bytes=int(args.output_reserve_gb * (1024**3)),
                )
                if previous_backup is not None:
                    if args.discard_overwrite_backup:
                        discard_output_backup(previous_backup)
                    else:
                        print(f"[safe-overwrite] preserved previous output at {previous_backup}")
                print(
                    f"[fast-workspace] published files={publish_result.file_count} "
                    f"bytes={publish_result.total_bytes} target={requested_output_root}",
                    flush=True,
                )
                if not args.keep_fast_work_output:
                    shutil.rmtree(completed_output)
                completed_output = requested_output_root
            elif publish_local_output:
                previous_backup = exchange_output_with_backup(completed_output, requested_output_root)
                if args.discard_overwrite_backup:
                    discard_output_backup(previous_backup)
                else:
                    print(f"[safe-overwrite] preserved previous output at {previous_backup}")
                completed_output = requested_output_root
            print(
                f"[done] output={completed_output} episodes={len(conversion_plan['episodes'])} "
                f"frames={total_frames} shards={len(shard_specs)}",
                flush=True,
            )
            return

    sync_report["processing"]["resume"] = {
        "enabled": bool(args.resume),
        "committed_episodes_reused": resume_episode_count,
        "committed_frames_reused": resume_frame_count,
        "verify_full_hash": bool(args.verify_resume_full_hash),
    }

    if (
        not args.dry_run
        and args.direct_openpi_images
        and not args.panorama_numeric_only
        and args.direct_image_cuda
        and getattr(args, "_direct_image_gpu_admission", None) is None
    ):
        maximum, startup, minimum, startup_progress, retry_count = (
            resolve_gpu_admission_settings(args, 2 * int(args.direct_image_export_workers))
        )
        args._direct_image_gpu_admission = SharedMediaSDKGPUAdmission.create(
            multiprocessing.get_context("spawn"),
            maximum_limit=maximum,
            minimum_limit=minimum,
            startup_limit=startup,
            startup_progress=startup_progress,
            retry_count=retry_count,
        )
        print(
            "[gpu-admission] "
            f"potential={2 * int(args.direct_image_export_workers)} max={maximum} "
            f"startup={startup} floor={minimum} "
            f"startup_progress={startup_progress}% oom_retries={retry_count}",
            flush=True,
        )

    frame_reader = None
    if not args.dry_run and args.direct_openpi_images:
        if not args.panorama_numeric_only:
            if runtime_direct_image_plans is None:
                raise RuntimeError("direct OpenPI runtime plans were not prepared")
            frame_reader = DirectBimanualOpenPIFrameReader(
                runtime_direct_image_plans["right"],
                runtime_direct_image_plans["left"],
                args,
                Path(fast_workspace_report["png_work_root"]),
                Path(fast_workspace_report["png_fallback_work_root"]),
                front_cache_manifests=front_cache_manifests,
            )
    elif not args.dry_run and args.persistent_video_readers:
        if right.camera_paths is None or left.camera_paths is None:
            raise RuntimeError("legacy persistent readers require six camera videos")
        frame_reader = BimanualFrameReader(
            right.camera_paths,
            left.camera_paths,
            (args.image_width, args.image_height),
            args.video_seek_threshold_frames,
        )

    # Keep one ordered image stream across all pending episodes.  Episode
    # boundaries are a dataset concern, not a decoder boundary: restarting the
    # bounded MediaSDK pipeline for every short segment wastes setup/seek work
    # and prevents look-ahead from overlapping the next episode.  The writer
    # still consumes exactly the planned number of frames per episode, so
    # pixels, timestamps, actions, checkpoints and episode semantics are
    # unchanged.
    global_frame_iter: Iterator[dict[str, np.ndarray]] | None = None
    global_frame_count = 0
    global_frames_consumed = 0
    if frame_reader is not None:
        pending_observation_pairs = [
            pair
            for episode_index, (_segment, planned_run) in enumerate(
                active_planned_runs, start=active_episode_start
            )
            if episode_index >= resume_episode_count
            for pair in planned_run.pairs[:-1]
        ]
        global_frame_count = len(pending_observation_pairs)
        global_frame_iter = frame_reader.iter_pairs(pending_observation_pairs)
        print(
            "[direct-images] global_ordered_stream=1 "
            f"pending_episodes={(active_episode_start + len(active_planned_runs)) - resume_episode_count} "
            f"pending_frames={global_frame_count} "
            f"batch_frames={getattr(frame_reader, 'batch_size', 'reader_default')}",
            flush=True,
        )

    total_written = active_frame_start
    episodes_written = active_episode_start
    try:
        for segment, run in active_planned_runs:
            grid_unix_time_ms = np.asarray(
                run.grid_unix_time_ms
                if run.grid_unix_time_ms
                else [
                    0.5
                    * (
                        right_timeline.unix_ms[right_idx]
                        + left_timeline.unix_ms[left_idx]
                    )
                    for right_idx, left_idx in run.pairs
                ],
                dtype=np.float64,
            )
            right_grid = sample_hand_states_on_grid(
                right_states,
                right_timeline,
                np.asarray([pair[0] for pair in run.pairs], dtype=np.int64),
                grid_unix_time_ms,
                segment.right_start_frame,
                segment.right_end_frame,
                args.gripper_zero_below_m,
                args.gripper_binary_threshold_m,
            )
            left_grid = sample_hand_states_on_grid(
                left_states,
                left_timeline,
                np.asarray([pair[1] for pair in run.pairs], dtype=np.int64),
                grid_unix_time_ms,
                segment.left_start_frame,
                segment.left_end_frame,
                args.gripper_zero_below_m,
                args.gripper_binary_threshold_m,
            )
            states = [
                bimanual_state(
                    right_grid.poses[state_pos],
                    float(right_grid.grippers[state_pos]),
                    left_grid.poses[state_pos],
                    float(left_grid.grippers[state_pos]),
                )
                for state_pos in range(len(run.pairs))
            ]
            frames_to_write = len(states) - 1
            episode_index = episodes_written
            print(
                "[episode] "
                f"segment={segment.segment_index} run={run.run_index} "
                f"pairs={len(run.pairs)} frames_to_write={frames_to_write} "
                f"first_pair={run.pairs[0]} last_pair={run.pairs[-1]}"
            )
            episode_report = {
                "episode_index": episode_index,
                "segment_index": segment.segment_index,
                "run_index": run.run_index,
                "pair_count": len(run.pairs),
                "frames_written": frames_to_write,
                "first_grid_unix_time_ms": float(grid_unix_time_ms[0]),
                "last_grid_unix_time_ms": float(grid_unix_time_ms[-1]),
                "right_grid_htc_soft_invalid": int(np.count_nonzero(~right_grid.htc_valid)),
                "left_grid_htc_soft_invalid": int(np.count_nonzero(~left_grid.htc_valid)),
                "right_gripper_interpolated": int(
                    np.count_nonzero(right_grid.gripper_interpolation_alpha > 0.0)
                ),
                "left_gripper_interpolated": int(
                    np.count_nonzero(left_grid.gripper_interpolation_alpha > 0.0)
                ),
                "right_gripper_zeroed_below_threshold": (
                    right_grid.gripper_zeroed_below_threshold
                ),
                "left_gripper_zeroed_below_threshold": (
                    left_grid.gripper_zeroed_below_threshold
                ),
                "right_gripper_binarized_values": (
                    right_grid.gripper_binarized_values
                ),
                "left_gripper_binarized_values": (
                    left_grid.gripper_binarized_values
                ),
                "first_pair": list(run.pairs[0]),
                "last_pair": list(run.pairs[-1]),
            }
            sync_report["episodes"].append(episode_report)
            if mapping_writer is not None:
                for state_pos in range(frames_to_write):
                    pair = run.pairs[state_pos]
                    grid_ms = float(grid_unix_time_ms[state_pos])
                    mapping_writer.writerow(
                        source_mapping_row(
                            episode_index,
                            state_pos,
                            run,
                            pair,
                            run.pairs[state_pos + 1],
                            grid_ms,
                            run.pairs[state_pos - 1] if state_pos > 0 else None,
                            state_pos,
                            right_timeline,
                            left_timeline,
                            right_grid,
                            left_grid,
                        )
                    )
            if args.dry_run:
                total_written += frames_to_write
                episodes_written += 1
                continue

            assert dataset is not None and mapping_writer is not None
            if episode_index < resume_episode_count:
                print(
                    f"[resume] episode={episode_index} rows={frames_to_write} already committed",
                    flush=True,
                )
                total_written += frames_to_write
                episodes_written += 1
                continue
            if int(dataset.meta.total_episodes) != episode_index:
                raise RuntimeError(
                    f"resume append index mismatch: metadata={dataset.meta.total_episodes} plan={episode_index}"
                )
            start_pose = start_end_pose_from_state(states[0])
            end_pose = start_end_pose_from_state(states[-1])
            observation_pairs = run.pairs[:-1]
            if args.panorama_numeric_only:
                frame_iter = itertools.repeat({}, frames_to_write)
            elif frame_reader is None:
                if right.camera_paths is None or left.camera_paths is None:
                    raise RuntimeError("legacy frame iteration requires six camera videos")
                frame_iter = iter_bimanual_frames_by_pairs(
                    right.camera_paths,
                    left.camera_paths,
                    observation_pairs,
                    (args.image_width, args.image_height),
                )
            else:
                if global_frame_iter is None:
                    raise RuntimeError("global frame iterator was not initialized")
                frame_iter = itertools.islice(global_frame_iter, frames_to_write)
            episode_stream = (
                BoundedEpisodeParquetWriter(
                    dataset,
                    episode_index,
                    args.episode_write_batch_frames,
                )
                if args.streaming_dataset_write
                else None
            )
            written = 0
            frame_pipeline_started = time.monotonic()
            try:
                for written, image_bundle in enumerate(frame_iter, start=1):
                    state_pos = written - 1
                    right_idx, left_idx = run.pairs[state_pos]
                    dataset.add_frame(
                        {
                            "start_pose": start_pose,
                            "end_pose": end_pose,
                            "observation.state": states[state_pos],
                            "action": states[state_pos + 1],
                            **source_feature_values(
                                right_idx,
                                left_idx,
                                segment.segment_index,
                                run.pairs[state_pos - 1] if state_pos > 0 else None,
                                state_pos,
                                float(grid_unix_time_ms[state_pos]),
                                right_timeline,
                                left_timeline,
                                right_grid,
                                left_grid,
                            ),
                            **image_bundle,
                        },
                        task=args.task,
                        timestamp=state_pos / output_dataset_fps(args),
                    )
                    if (
                        episode_stream is not None
                        and written < frames_to_write
                        and episode_stream.should_flush()
                    ):
                        episode_stream.flush()
                    if args.progress_every > 0 and written % args.progress_every == 0:
                        print(
                            f"[write] segment={segment.segment_index} run={run.run_index}: "
                            f"added {written}/{frames_to_write} frames",
                            flush=True,
                        )
            except BaseException:
                if episode_stream is not None:
                    episode_stream.abort()
                raise
            if written != frames_to_write:
                raise RuntimeError(
                    f"Video iterator returned {written}/{frames_to_write} frames for "
                    f"segment={segment.segment_index} run={run.run_index}"
                )
            if global_frame_iter is not None:
                global_frames_consumed += written
            frame_pipeline_seconds = time.monotonic() - frame_pipeline_started

            finalize_started = time.monotonic()
            maybe_write_combined_video(
                episode_index,
                right,
                left,
                states[:-1],
                observation_pairs,
                args,
            )
            # Keep info.json as the final durable commit marker.  A power loss
            # before the checkpoint is fsynced therefore leaves this episode
            # uncommitted and safe to rebuild on resume.
            if episode_stream is None:
                dataset.save_episode(commit_info=False)
            else:
                try:
                    episode_stream.finish(frames_to_write)
                except BaseException:
                    episode_stream.abort()
                    raise
            remove_embedded_episode_pngs(dataset, episode_index)
            write_episode_checkpoint(dataset, conversion_plan, episode_index)
            dataset.meta.commit_info()
            finalize_seconds = time.monotonic() - finalize_started
            print(
                "[episode-performance] "
                f"segment={segment.segment_index} run={run.run_index} "
                f"frames={written} frame_pipeline_sec={frame_pipeline_seconds:.3f} "
                f"finalize_sec={finalize_seconds:.3f} "
                f"effective_fps={written / max(frame_pipeline_seconds + finalize_seconds, 1e-9):.3f}",
                flush=True,
            )
            total_written += written
            episodes_written += 1
    finally:
        if global_frame_iter is not None:
            close_global_iter = getattr(global_frame_iter, "close", None)
            if callable(close_global_iter):
                close_global_iter()
        if frame_reader is not None:
            frame_reader.close()
        if dataset is not None and getattr(dataset, "image_writer", None) is not None:
            dataset.stop_image_writer()
        if mapping_handle is not None:
            mapping_handle.flush()
            os.fsync(mapping_handle.fileno())
            mapping_handle.close()

    if global_frame_iter is not None and global_frames_consumed != global_frame_count:
        raise RuntimeError(
            "global image stream frame count mismatch: "
            f"consumed={global_frames_consumed} expected={global_frame_count}"
        )

    if args.dry_run:
        print(f"[dry-run] episodes={episodes_written} frames={total_written}")
        return
    if episodes_written == 0:
        raise RuntimeError("No LeRobot episodes were written; all segments were skipped or invalid")
    assert dataset is not None
    assert mapping_tmp is not None
    mapping_path = args.output_root.expanduser().resolve() / "source_frame_mapping.csv"
    mapping_tmp.replace(mapping_path)
    mapping_directory_fd = os.open(mapping_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(mapping_directory_fd)
    finally:
        os.close(mapping_directory_fd)
    if worker_shard is not None:
        if episodes_written != worker_shard.episode_end:
            raise RuntimeError(
                f"episode shard ended at {episodes_written}, expected {worker_shard.episode_end}"
            )
        if total_written != worker_shard.frame_end:
            raise RuntimeError(
                f"episode shard frame count ended at {total_written}, expected {worker_shard.frame_end}"
            )
        shard_validation = validate_written_episode_table_range(
            dataset,
            conversion_plan,
            worker_shard.episode_start,
            worker_shard.episode_end,
        )
        sync_report["summary"] = {
            "episode_start": worker_shard.episode_start,
            "episode_end": worker_shard.episode_end,
            "frames": worker_shard.frame_end - worker_shard.frame_start,
            **shard_validation,
        }
        atomic_write_json(args.output_root.expanduser().resolve() / "sync_report.json", sync_report)
        atomic_write_json(
            args.output_root.expanduser().resolve() / "episode_shard_complete.json",
            {
                "schema": "instaumi-bimanual-episode-shard-complete-v1",
                "conversion_plan_hash": conversion_plan["plan_hash"],
                "assignment_hash": worker_shard.assignment_hash,
                "episode_start": worker_shard.episode_start,
                "episode_end": worker_shard.episode_end,
                "frame_start": worker_shard.frame_start,
                "frame_end": worker_shard.frame_end,
                "source_frame_mapping_sha256": sha256_file(mapping_path),
            },
        )
        print(
            f"[shard-done] shard={worker_shard.shard_index} "
            f"episodes={worker_shard.episode_start}:{worker_shard.episode_end} "
            f"frames={worker_shard.frame_start}:{worker_shard.frame_end} "
            f"root={args.output_root}",
            flush=True,
        )
        return
    table_validation = validate_written_episode_tables(dataset, episodes_written, total_written)
    sync_report["summary"] = {
        "episodes": episodes_written,
        "frames": total_written,
        **table_validation,
    }
    atomic_write_json(args.output_root.expanduser().resolve() / "sync_report.json", sync_report)
    completed_output = args.output_root.expanduser().resolve()
    write_conversion_complete(
        completed_output,
        conversion_plan,
        episodes=episodes_written,
        frames=total_written,
    )
    if publish_fast_output:
        publish_result, previous_backup = publish_fast_workspace_output(
            fast_workspace,
            completed_output,
            requested_output_root,
            overwrite=args.overwrite,
            reserve_bytes=int(args.output_reserve_gb * (1024**3)),
        )
        if previous_backup is not None:
            if args.discard_overwrite_backup:
                discard_output_backup(previous_backup)
                print(f"[safe-overwrite] discarded previous output: {previous_backup}", flush=True)
            else:
                print(f"[safe-overwrite] preserved previous output at {previous_backup}", flush=True)
        print(
            f"[fast-workspace] published files={publish_result.file_count} "
            f"bytes={publish_result.total_bytes} target={requested_output_root}",
            flush=True,
        )
        if not args.keep_fast_work_output:
            shutil.rmtree(completed_output)
        completed_output = requested_output_root
        args.output_root = requested_output_root
    elif publish_local_output:
        previous_backup = exchange_output_with_backup(completed_output, requested_output_root)
        if args.discard_overwrite_backup:
            discard_output_backup(previous_backup)
            print(f"[safe-overwrite] discarded previous output: {previous_backup}", flush=True)
        else:
            print(f"[safe-overwrite] preserved previous output at {previous_backup}", flush=True)
        completed_output = requested_output_root
        args.output_root = requested_output_root
    print(f"[done] output={completed_output} episodes={episodes_written} frames={total_written}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct-from-raw", action="store_true", help="Read _01 videos/timestamps, _02 SLAM or HTC poses, and _03 gripper CSV directly; skip _04 conversion.")
    parser.add_argument(
        "--direct-openpi-images",
        action="store_true",
        help=(
            "Read ordered copied_sources.insv chapters directly and create the final "
            "six RGB uint8 224x224 OpenPI images without generated training videos."
        ),
    )
    parser.add_argument(
        "--panorama-numeric-only",
        action="store_true",
        help=(
            "Panorama-v2 storage: export and validate the continuous H.265 panorama "
            "cache, but commit only numeric/time-alignment columns to LeRobot. The "
            "six legacy RGB224 columns are never decoded or written."
        ),
    )
    parser.add_argument(
        "--direct-image-mediasdk-exporter",
        type=Path,
        default=DEFAULT_MEDIASDK_EXPORTER,
    )
    parser.add_argument(
        "--direct-image-panorama-exporter",
        type=Path,
        default=DEFAULT_MEDIASDK_PANORAMA_EXPORTER,
        help=(
            "Official MediaSDK SetOutputPath H.265 exporter used once per INSV "
            "chapter before active-frame projection."
        ),
    )
    parser.add_argument(
        "--direct-image-panorama-export-workers",
        type=int,
        default=12,
        help=(
            "Global concurrent complete-chapter MediaSDK exporters across both "
            "arms (maximum 12; actual workers cannot exceed chapter count)."
        ),
    )
    parser.add_argument(
        "--direct-image-panorama-bitrate",
        type=int,
        default=67_000_000,
        help="Official H.265 panorama cache target bitrate in bits/s.",
    )
    parser.add_argument(
        "--direct-image-panorama-timeout-sec",
        type=int,
        default=7200,
        help="Per-chapter continuous export timeout; 0 disables the timeout.",
    )
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--direct-image-work-root", type=Path, default=None)
    parser.add_argument("--direct-image-batch-frames", type=int, default=512)
    parser.add_argument("--direct-image-export-workers", type=int, default=4)
    parser.add_argument(
        "--direct-image-gpu-max-workers",
        type=int,
        default=0,
        help="Global live MediaSDK CUDA jobs; 0 uses all configured exporter lanes.",
    )
    parser.add_argument(
        "--direct-image-gpu-startup-workers",
        type=int,
        default=0,
        help="Concurrent MediaSDK CUDA initialization phases; 0 selects up to 8.",
    )
    parser.add_argument(
        "--direct-image-gpu-min-workers",
        type=int,
        default=0,
        help="Lowest automatic OOM fallback ceiling; 0 selects up to 4.",
    )
    parser.add_argument("--direct-image-gpu-startup-progress", type=int, default=10)
    parser.add_argument("--direct-image-gpu-oom-retries", type=int, default=6)
    parser.add_argument(
        "--direct-image-parallel-arms",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Process the independent left/right raw-INSV image batches concurrently, "
            "then join frames in the original calibrated pair order."
        ),
    )
    parser.add_argument("--direct-image-temp-margin-gb", type=float, default=0.5)
    parser.add_argument(
        "--direct-image-panorama-width",
        type=int,
        default=DEFAULT_OPENPI_PANORAMA_WIDTH,
        help=(
            "Transient official MediaSDK panorama width used only to derive final "
            "224 side views (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--direct-image-panorama-height",
        type=int,
        default=DEFAULT_OPENPI_PANORAMA_HEIGHT,
        help=(
            "Transient official MediaSDK panorama height used only to derive final "
            "224 side views (default: %(default)s)."
        ),
    )
    parser.add_argument("--direct-image-projection-size", type=int, default=None)
    parser.add_argument("--direct-image-left-yaw", type=float, default=-90.0)
    parser.add_argument("--direct-image-right-yaw", type=float, default=90.0)
    parser.add_argument("--direct-image-horizontal-fov", type=float, default=90.0)
    parser.add_argument("--direct-image-vertical-fov", type=float, default=90.0)
    parser.add_argument(
        "--direct-image-stitch-type",
        choices=["template", "dynamic", "optflow", "aiflow"],
        default="optflow",
    )
    parser.add_argument(
        "--direct-image-flowstate",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--direct-image-direction-lock",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--direct-image-stitch-fusion",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--direct-image-cuda",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--direct-image-timeout-sec", type=int, default=600)
    parser.add_argument("--direct-image-model-dir", type=Path, default=None)
    parser.add_argument("--direct-image-decode-threads", type=int, default=1)
    parser.add_argument("--direct-image-resize-workers", type=int, default=4)
    parser.add_argument("--direct-image-v360-filter-threads", type=int, default=2)
    parser.add_argument("--direct-image-repeat-cache-frames", type=int, default=8)
    parser.add_argument(
        "--direct-image-front-cache",
        choices=["auto", "require", "off"],
        default=os.environ.get("DIRECT_IMAGE_FRONT_CACHE", "auto"),
        help=(
            "Reuse sparse RGB224 fronts generated during the same native _03 ArUco "
            "decode. auto falls back for legacy data; require is fail-closed."
        ),
    )
    parser.add_argument(
        "--episode-shards",
        choices=["auto", "on", "off"],
        default=os.environ.get("EPISODE_SHARDS", "auto"),
        help=(
            "Run independent contiguous chapter-dependency episode ranges in isolated "
            "LeRobot roots, then merge their already-final Parquet files in global order."
        ),
    )
    parser.add_argument(
        "--episode-shard-workers",
        type=int,
        default=int(os.environ.get("EPISODE_SHARD_WORKERS", "0")),
        help="Maximum live chapter shards; 0 selects the largest resource-budget-safe value.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help="Original capture session root; required when enforcing an accepted calibration report.",
    )
    parser.add_argument(
        "--timestamp-log",
        type=Path,
        default=None,
        help="Timestamp log used by calibration; defaults to SESSION_DIR/timestamp_log.jsonl.",
    )
    parser.add_argument("--tmp-root", type=Path, default=DEFAULT_TMP_ROOT)
    parser.add_argument("--right-episode", default=None)
    parser.add_argument("--left-episode", default=None)
    parser.add_argument("--right-htc-csv", type=Path, default=None)
    parser.add_argument("--left-htc-csv", type=Path, default=None)
    parser.add_argument("--right-htc-role", default="htc_right")
    parser.add_argument("--left-htc-role", default="htc_left")
    parser.add_argument(
        "--right-frame-timestamps",
        type=Path,
        default=None,
        help="Explicit right source-frame timeline; normally resolved from an accepted sync calibration report.",
    )
    parser.add_argument(
        "--left-frame-timestamps",
        type=Path,
        default=None,
        help="Explicit left source-frame timeline; normally resolved from an accepted sync calibration report.",
    )
    parser.add_argument("--sync-calibration-report", type=Path, default=None)
    parser.add_argument(
        "--require-calibrated-timestamps",
        action="store_true",
        help="Require both selected timelines to match a status=accepted calibration report.",
    )
    parser.add_argument(
        "--htc-time-source",
        choices=[
            "auto",
            "hid_read_monotonic_raw",
            "hid_read_host_monotonic_raw_ns",
            "hid_read_realtime",
            "callback",
            "hid_read_host_realtime_ns",
            "host_unix_time_ns",
            "timestamp",
        ],
        default="auto",
        help="Use one HTC host-clock column with >=95%% full-pose coverage. auto prefers CLOCK_MONOTONIC_RAW; accepted strict calibration forbids realtime fallback.",
    )
    parser.add_argument("--right-input-root", type=Path, default=None)
    parser.add_argument("--left-input-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--fast-workspace",
        choices=["auto", "on", "off"],
        default=os.environ.get("FAST_WORKSPACE", "auto"),
        help="Stage rotational-disk video inputs and build output on local fast storage (auto/on/off).",
    )
    parser.add_argument(
        "--fast-work-root",
        type=Path,
        default=(
            Path(os.environ["FAST_WORK_ROOT"])
            if os.environ.get("FAST_WORK_ROOT")
            else None
        ),
        help=(
            "Cache root under the raw session or export parent. Defaults to "
            "SESSION_DIR/instaumi-dataprocess-cache; paths under HOME are rejected."
        ),
    )
    parser.add_argument(
        "--allow-home-fast-work-root",
        action="store_true",
        help=(
            "Explicitly allow --fast-work-root below HOME. The cache must still "
            "live under the raw session or export parent and must not overlap "
            "the exported dataset."
        ),
    )
    parser.add_argument("--fast-work-reserve-gb", type=float, default=100.0)
    parser.add_argument(
        "--output-reserve-gb",
        type=float,
        default=20.0,
        help="Minimum free space retained when writing directly on the output filesystem.",
    )
    parser.add_argument(
        "--verify-fast-cache-full-hash",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Audit current source, cached payload, and manifest with full SHA-256 before "
            "conversion. Normal cache creation already hashes the streamed source and "
            "fully reads back the NVMe copy; cache hits always verify identity and samples."
        ),
    )
    parser.add_argument("--keep-fast-work-output", action="store_true")
    parser.add_argument("--repo-id", default="local/instaumi_bimanual")
    parser.add_argument("--robot-type", default="instaumi_bimanual")
    parser.add_argument("--task", default="bimanual_instaumi_task")
    parser.add_argument("--lerobot-env", type=Path, default=DEFAULT_LEROBOT_ENV)
    parser.add_argument("--episode-glob", default="ep*")
    parser.add_argument("--right-pose-sensor", choices=["insta", "htc"], default="htc")
    parser.add_argument("--left-pose-sensor", choices=["insta", "htc"], default="htc")
    parser.add_argument("--time-align-mode", choices=["global_time", "row_index"], default="global_time")
    parser.add_argument(
        "--camera-pairing-mode",
        choices=["right_anchor_nearest", "uniform_grid_nearest"],
        default="right_anchor_nearest",
        help=(
            "For global_time alignment, preserve every valid right/insta1 source frame and "
            "pair its nearest calibrated left frame. uniform_grid_nearest is retained only "
            "for reproducing older conversion plans."
        ),
    )
    parser.add_argument("--max-camera-time-diff-ms", type=float, default=10.0)
    parser.add_argument(
        "--hard-camera-time-diff-ms",
        type=float,
        default=20.0,
        help="Reject a complete timestamp_log segment if any selected left/right frame pair exceeds this calibrated-time difference.",
    )
    parser.add_argument("--max-htc-nearest-gap-ms", type=float, default=10.0)
    parser.add_argument("--max-htc-bracket-gap-ms", type=float, default=20.0)
    parser.add_argument(
        "--max-host-clock-offset-step-ms",
        type=float,
        default=5.0,
        help="Reject HTC interpolation when host realtime-minus-monotonic changes by more than this between adjacent samples.",
    )
    parser.add_argument(
        "--hard-gap-reject-ms",
        "--max-source-frame-gap-ms",
        dest="hard_gap_reject_ms",
        type=float,
        default=50.0,
        help=(
            "Reject the complete timestamp_log segment if a camera step, HTC bracket, "
            "or grid-to-camera distance exceeds this many milliseconds. The legacy "
            "--max-source-frame-gap-ms spelling remains accepted. The production contract "
            "keeps 10/20 ms HTC soft targets and uses 50 ms only as the hard rejection "
            "boundary. Segments are never split."
        ),
    )
    parser.add_argument("--split-by-segments", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-segment-frames", type=int, default=2)
    parser.add_argument(
        "--segment-time-offset-sec",
        type=float,
        default=0.0,
        help="Apply this offset to every segment_start/segment_end before mapping to left/right video frames. Positive cuts later.",
    )
    parser.add_argument("--gripper-column", default="auto")
    parser.add_argument("--missing-gripper", choices=["skip", "zero", "ffill"], default="skip")
    gripper_transform_group = parser.add_mutually_exclusive_group()
    gripper_transform_group.add_argument(
        "--gripper-zero-below-m",
        type=float,
        default=None,
        help=(
            "After calibrated gripper interpolation, set every finite width strictly "
            "below this many metres to zero. Disabled by default."
        ),
    )
    gripper_transform_group.add_argument(
        "--gripper-binary-threshold-m",
        type=float,
        default=None,
        help=(
            "After calibrated gripper interpolation on the final shared grid, "
            "map every finite width strictly below this many metres to 0 and "
            "every width greater than or equal to it to 1. Disabled by default."
        ),
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=60,
        help=(
            "Full-rate camera alignment frequency before row decimation. It is also the "
            "compatibility-grid frequency only for --camera-pairing-mode=uniform_grid_nearest."
        ),
    )
    parser.add_argument(
        "--down-sample-steps",
        type=int,
        default=2,
        help=(
            "After full-rate camera/HTC alignment, keep rows 0, N, 2N, ... for every "
            "modality. The default 60/2 publishes a 30 Hz LeRobot dataset."
        ),
    )
    parser.add_argument("--image-width", type=int, default=224)
    parser.add_argument("--image-height", type=int, default=224)
    parser.add_argument("--max-frames-per-episode", type=int, default=None)
    parser.add_argument("--allow-partial-inputs", action="store_true", help="Allow direct-from-raw conversion with partial _03 gripper CSVs by truncating to the shortest available stream.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--discard-overwrite-backup",
        action="store_true",
        help=(
            "After a complete replacement has been atomically published, delete the old "
            "dataset generation instead of retaining its hidden backup directory."
        ),
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume the atomically committed episode prefix when conversion_plan.json still matches.",
    )
    parser.add_argument(
        "--verify-resume-full-hash",
        action="store_true",
        help="Re-read and SHA-256 every committed episode parquet before resuming (footer/size checks are always run).",
    )
    parser.add_argument(
        "--preview-every-n",
        type=int,
        default=0,
        help="Write a diagnostic preview every N episodes; 0 disables the extra six-video decode pass.",
    )
    parser.add_argument("--preview-stride", type=int, default=4)
    parser.add_argument("--preview-fourcc", default="mp4v")
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--batch-encoding-size", type=int, default=1)
    parser.add_argument("--image-writer-processes", type=int, default=0)
    parser.add_argument("--image-writer-threads", type=int, default=4)
    parser.add_argument("--stats-image-threads", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument(
        "--png-compress-level",
        type=int,
        default=int(os.environ.get("INSTAUMI_PNG_COMPRESS_LEVEL", "1")),
        help="Lossless PNG compression level (0-9). Level 1 preserves identical decoded RGB pixels and is faster.",
    )
    parser.add_argument(
        "--streaming-dataset-write",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Release embedded image bytes after each atomic episode parquet instead of retaining the full dataset in RAM.",
    )
    parser.add_argument(
        "--episode-write-batch-frames",
        type=int,
        default=256,
        help="Bound Arrow/PNG memory by appending this many frames per Parquet row group while preserving logical episode boundaries.",
    )
    parser.add_argument(
        "--embed-images-in-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Encode PNG bytes with a bounded thread pool and write them directly to Parquet, avoiding temporary image files.",
    )
    parser.add_argument(
        "--persistent-video-readers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep all six source decoders open across episodes while preserving absolute source-frame indexing.",
    )
    parser.add_argument(
        "--video-seek-threshold-frames",
        type=int,
        default=300,
        help="Decode forward across gaps up to this many frames; seek only for larger/backwards gaps.",
    )
    parser.add_argument("--disable-lerobot-combined-video", action=argparse.BooleanOptionalAction, default=True)
    return parser


@contextmanager
def conversion_output_lock(output_root: Path) -> Iterator[None]:
    expanded = output_root.expanduser().absolute()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    if expanded.is_symlink():
        raise ValueError(f"OUTPUT_ROOT itself must not be a symlink: {expanded}")
    # Resolve all existing parent aliases before deriving the lock name.  This
    # prevents two commands using real/ and symlinked-parent/ spellings from
    # acquiring different locks for the same final directory.
    resolved = expanded.parent.resolve(strict=True) / expanded.name
    lock_path = resolved.parent / f".{resolved.name}.conversion.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise RuntimeError(f"another conversion holds {lock_path} (owner {owner})") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> None:
    args = build_arg_parser().parse_args()
    with conversion_output_lock(args.output_root):
        run(args)


if __name__ == "__main__":
    main()
