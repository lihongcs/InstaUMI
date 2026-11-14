#!/usr/bin/env python3
"""第 3 阶段：从前向 180 度鱼眼视频估计夹爪宽度。

默认检测 ArUco 4x4_50 字典中的 marker 1 和 marker 2，二者中心距离作为
夹爪宽度像素值，再通过 config/gripper_calibration.json 的标定曲线转换为
cm 和 m。输出逐帧 CSV，缺失帧也写入，便于和 RGB/pose 严格按 frame_idx 对齐。
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import csv
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, Sequence

DATAPROCESS_ROOT = Path(__file__).resolve().parents[1]
if str(DATAPROCESS_ROOT) not in sys.path:
    sys.path.insert(0, str(DATAPROCESS_ROOT))

from fast_workspace import cache_input_file, inspect_storage
from active_frame_ranges import (
    ActiveFrameRange,
    build_active_frame_ranges,
    load_calibrated_frame_timeline,
)


def default_data_root() -> Path:
    env = os.environ.get("DATA_ROOT")
    if env:
        return Path(env).expanduser()
    workspace_root = Path(__file__).resolve().parents[3]
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


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TMP_ROOT = default_tmp_root()
DEFAULT_GRIPPER_CALIBRATION_JSON = REPO_ROOT / "config" / "gripper_calibration.json"

CHUNK_SCHEMA = "instaumi_gripper_detection_chunk_v1"
FRONT_CACHE_SCHEMA = "instaumi.front_openpi_cache.v1"
RAW_DETECTION_ROW_SCHEMA_V3 = "instaumi.gripper_detection_row.v3"
RAW_DETECTION_ROW_SCHEMA_V4 = "instaumi.gripper_detection_row.v4"
FRONT_CACHE_RECIPE = {
    "operation": "openpi_client.resize_with_pad.pil_bilinear.v1",
    "source_color": "bgr24",
    "output_color": "rgb24",
    "width": 224,
    "height": 224,
    "dtype": "uint8",
}
# Keep the public schema identifier backward compatible; the new fields are
# additive, so existing `_04`/`_05` readers continue to see the v1 contract.
MANIFEST_SCHEMA = "instaumi_gripper_estimation_v1"
RAW_VIDEO_ARTIFACT_SCHEMA = "instaumi_video_artifact_v2"
CONTINUOUS_GRIPPER_QUALITY_SCHEMA = "instaumi.continuous_gripper_quality.v1"
SINGLE_MARKER_RECOVERY_SCHEMA = "instaumi.single_marker_gripper_recovery.v1"
ADAPTIVE_DUAL_PROMOTION_SCHEMA = "instaumi.adaptive_dual_promotion.v2"
ADAPTIVE_DUAL_GEOMETRY_SCHEMA = "instaumi.adaptive_dual_geometry.v1"

# Detection chunks created before the segment-level quality gate was added are
# expensive (they require decoding the native INSV stream), but their ArUco
# detection semantics did not change.  Keep the detection-only fingerprint
# frozen at that exact implementation identity so changes confined to merge,
# interpolation, or quality reporting can reuse those content-validated chunks.
# Any future change to decode/detect semantics MUST update this identity.
DETECTION_IMPLEMENTATION_IDENTITY_V1 = {
    "path": str(Path(__file__).resolve()),
    "size": 126724,
    "sha256": "3f494fa75d682df0bb72b27faff6a8345db5b18f8439044ecdd651737acc828b",
}
DETECTION_IMPLEMENTATION_IDENTITY_V2 = {
    "revision": "opencv_aruco_raw_expected_id_adaptive_threshold_retry_v2",
    "base_revision": DETECTION_IMPLEMENTATION_IDENTITY_V1,
}
DETECTION_IMPLEMENTATION_IDENTITY_V3 = {
    "revision": "opencv_aruco_raw_expected_id_adaptive_threshold_retry_v3",
    "supersedes": DETECTION_IMPLEMENTATION_IDENTITY_V2,
    "raw_row_schema": RAW_DETECTION_ROW_SCHEMA_V3,
    "semantics": {
        "primary_valid": "both configured IDs detected by the primary detector",
        "adaptive_trigger": "exactly one configured ID detected by the primary detector",
        "adaptive_uniqueness": (
            "one primary peer plus exactly one fallback peer and one fallback missing ID"
        ),
        "adaptive_peer_consistency": "fallback peer center must match the primary peer",
        "raw_valid": "primary_valid_only; adaptive detections remain candidates",
    },
}
DETECTION_IMPLEMENTATION_IDENTITY_V4 = {
    "revision": "opencv_aruco_raw_expected_id_roi_cascade_retry_v4",
    "supersedes": DETECTION_IMPLEMENTATION_IDENTITY_V3,
    "raw_row_schema": RAW_DETECTION_ROW_SCHEMA_V4,
    "semantics": {
        "priority": "primary_dual_then_multithreshold_dual_then_single_marker",
        "adaptive_trigger": "primary_detector_did_not_return_a_unique_expected_pair",
        "adaptive_cascade": (
            "exactly_one_primary_expected_id_uses_ordered_native_roi_multiscale_"
            "preprocessing; otherwise ordered_full_frame_thresholds; stop_at_first_"
            "strict_expected_pair"
        ),
        "partial_pair": (
            "a unique primary peer may be combined with one unique recovered expected ID; "
            "a fallback peer, when present, must agree with the primary peer"
        ),
        "zero_primary_pair": (
            "one fallback pass must return each configured ID exactly once; "
            "the same independent geometry and continuity gates still apply"
        ),
        "raw_valid": "primary_valid_only; adaptive detections remain candidates",
        "promotion": (
            "independent_primary_dual_geometry_and_temporal_continuity; "
            "single-marker model acceptance is irrelevant"
        ),
        "retry_provenance": (
            "raw row records the unique detector strategy and threshold; ROI geometry "
            "and strict gates are bound by the detection fingerprint"
        ),
    },
}


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def object_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def sampled_file_identity(path: Path, sample_size: int = 256 * 1024) -> dict:
    """Return a cheap but strong identity without reading a multi-GB video twice.

    The cache copy itself is fully SHA-256 hashed.  For source invalidation we use
    resolved path, size, nanosecond mtime and samples at the beginning/middle/end.
    This makes resume checks fast while still detecting silent replacement far
    more reliably than a path/mtime-only cache key.
    """

    path = path.expanduser().resolve()
    stat = path.stat()
    offsets = sorted({0, max(0, stat.st_size // 2 - sample_size // 2), max(0, stat.st_size - sample_size)})
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for offset in offsets:
            f.seek(offset)
            data = f.read(min(sample_size, stat.st_size - offset))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def content_file_identity(path: Path) -> dict:
    """Content identity for small metadata inputs that may be atomically regenerated."""

    path = path.expanduser().resolve()
    return {"path": str(path), "size": path.stat().st_size, "sha256": sha256_file(path)}


def fsync_directory(path: Path) -> None:
    """Durably order payload renames before an atomic manifest commit."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def parse_fraction(value: str) -> float:
    numerator, denominator = value.split("/", 1)
    denominator_value = float(denominator)
    if denominator_value == 0.0:
        raise ValueError(f"invalid zero-denominator fraction: {value}")
    return float(numerator) / denominator_value


def resolve_ffprobe_bin(requested: str | None) -> str:
    if requested:
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(requested)
        if found:
            return found
        raise FileNotFoundError(f"ffprobe not found: {requested}")
    if env := os.environ.get("FFPROBE_BIN"):
        return resolve_ffprobe_bin(env)
    found = shutil.which("ffprobe")
    if found:
        return found
    sibling = Path(sys.executable).with_name("ffprobe")
    if sibling.is_file():
        return str(sibling)
    raise FileNotFoundError("ffprobe not found; set --ffprobe-bin or FFPROBE_BIN")


def probe_video(path: Path, ffprobe_bin: str) -> dict:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,start_time,time_base",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1:
        raise RuntimeError(f"expected exactly one video stream in {path}, got {len(streams)}")
    stream = streams[0]
    nb_frames = stream.get("nb_frames")
    if not nb_frames or nb_frames == "N/A":
        count_cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        nb_frames = subprocess.run(count_cmd, check=True, capture_output=True, text=True).stdout.strip()
    if not nb_frames or nb_frames == "N/A":
        raise RuntimeError(f"ffprobe could not determine an exact frame count: {path}")
    fps_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    return {
        "frame_count": int(nb_frames),
        "fps": fps_text,
        "fps_float": parse_fraction(fps_text),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec_name": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "duration": None if stream.get("duration") in (None, "N/A") else float(stream["duration"]),
        "start_time": 0.0 if stream.get("start_time") in (None, "N/A") else float(stream["start_time"]),
        "time_base": stream.get("time_base"),
    }


def probe_keyframes(path: Path, ffprobe_bin: str, probe: dict) -> list[int]:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,flags",
        "-of",
        "csv=p=0",
        str(path),
    ]
    output = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    start_time = float(probe["start_time"])
    fps = float(probe["fps_float"])
    indices = []
    for line in output.splitlines():
        fields = line.strip().split(",")
        value = fields[0] if fields else ""
        flags = fields[1] if len(fields) > 1 else ""
        if "K" not in flags:
            continue
        if not value or value == "N/A":
            continue
        index = int(round((float(value) - start_time) * fps))
        if 0 <= index < int(probe["frame_count"]):
            indices.append(index)
    indices = sorted(set(indices))
    if not indices or indices[0] != 0:
        raise RuntimeError(f"video does not expose frame 0 as a keyframe: {path}")
    return indices


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_timestamps(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def validate_timestamps(timestamps: list[dict[str, str]], path: Path) -> None:
    if not timestamps:
        raise ValueError(f"timestamp CSV is empty: {path}")
    for expected, row in enumerate(timestamps):
        try:
            actual = int(row["frame_idx"])
            float(row["timestamp_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid timestamp row {expected} in {path}: {row}") from exc
        if actual != expected:
            raise ValueError(
                f"timestamp frame_idx must be continuous from zero: row={expected}, frame_idx={actual}, path={path}"
            )


def detector_parameter_snapshot(
    dictionary_name: str,
    *,
    adaptive_thresh_constant: float | None = None,
) -> dict:
    import cv2
    from cv2 import aruco

    if not hasattr(aruco, dictionary_name):
        raise ValueError(f"unknown ArUco dictionary: {dictionary_name}")
    params = aruco.DetectorParameters()
    if adaptive_thresh_constant is not None:
        params.adaptiveThreshConstant = float(adaptive_thresh_constant)
    values = {}
    for name in dir(params):
        if name.startswith("_"):
            continue
        try:
            value = getattr(params, name)
        except Exception:
            continue
        if isinstance(value, (bool, int, float, str)):
            values[name] = value
    return {
        "opencv_version": cv2.__version__,
        "opencv_build_sha256": hashlib.sha256(cv2.getBuildInformation().encode("utf-8")).hexdigest(),
        "dictionary": dictionary_name,
        "detector_parameters": values,
    }


def pyav_decoder_runtime_snapshot() -> dict:
    """Bind resumable decode artifacts to the exact FFmpeg/PyAV runtime."""

    try:
        import av
    except ImportError as exc:
        raise RuntimeError("PyAV is required for exact chunk decoding") from exc
    return {
        "pyav_version": av.__version__,
        "ffmpeg_libraries": {
            name: list(version)
            for name, version in sorted(av.library_versions.items())
        },
    }


def make_detector(
    dictionary_name: str,
    *,
    adaptive_thresh_constant: float | None = None,
) -> aruco.ArucoDetector:
    from cv2 import aruco

    dictionary_id = getattr(aruco, dictionary_name)
    dictionary = aruco.getPredefinedDictionary(dictionary_id)
    params = aruco.DetectorParameters()
    if adaptive_thresh_constant is not None:
        params.adaptiveThreshConstant = float(adaptive_thresh_constant)
    return aruco.ArucoDetector(dictionary, params)


def compute_centers(corners, ids) -> dict[int, tuple[float, float]]:
    centers: dict[int, tuple[float, float]] = {}
    if ids is None:
        return centers
    for i in range(len(ids)):
        marker_id = int(ids[i][0])
        c = corners[i][0]
        centers[marker_id] = (float(c[:, 0].mean()), float(c[:, 1].mean()))
    return centers


def compute_marker_side_lengths(corners, ids) -> dict[int, float]:
    import numpy as np

    side_lengths: dict[int, float] = {}
    if ids is None:
        return side_lengths
    for i in range(len(ids)):
        marker_id = int(ids[i][0])
        c = corners[i][0].astype(np.float64)
        sides = [
            np.linalg.norm(c[1] - c[0]),
            np.linalg.norm(c[2] - c[1]),
            np.linalg.norm(c[3] - c[2]),
            np.linalg.norm(c[0] - c[3]),
        ]
        side_lengths[marker_id] = float(np.mean(sides))
    return side_lengths


def adaptive_retry_threshold_constants(base_constant: float) -> list[float]:
    """Return one deterministic retry cascade, ordered by observed recall.

    OpenCV's primary detector already uses its default threshold constant (7).
    On the native X5 front lens, constants around 23 recover the weak finger
    marker most often, while 15/19/11 cover different local-contrast cases.
    This is one cascade, not multiple competing detector versions: processing
    stops at the first candidate that passes the strict expected-ID checks.
    """

    base = float(base_constant)
    values = (base + 8.0, base, base + 4.0, base - 4.0)
    result: list[float] = []
    for value in values:
        if math.isfinite(value) and value > 0.0 and value not in result:
            result.append(value)
    return result


def adaptive_roi_retry_specs(base_constant: float) -> list[dict]:
    """Return the one production ROI cascade for an exactly-one-marker frame.

    The pass order was measured on native 1920x1920 X5 stream-0 frames.  It is
    deliberately deterministic and early-stopping: these are complementary
    preprocessing passes inside one detector contract, not competing output
    versions.  Threshold values remain relative to the public base value so a
    non-default caller still receives a completely fingerprinted recipe.
    """

    base = float(base_constant)
    raw_specs = [
        ("roi_native_raw", 1, "none", "raw", base + 8.0),
        ("roi_s2_nearest_raw", 2, "nearest", "raw", base - 4.0),
        ("roi_s4_nearest_raw", 4, "nearest", "raw", base - 4.0),
        ("roi_s4_nearest_raw_low_c", 4, "nearest", "raw", base - 12.0),
        ("roi_s3_linear_gray_gamma07", 3, "linear", "gray_gamma_0.7", base),
        ("roi_s3_nearest_raw_low_c", 3, "nearest", "raw", base - 12.0),
        ("roi_s3_linear_gray_gamma07_high_c", 3, "linear", "gray_gamma_0.7", base + 4.0),
        ("roi_s4_nearest_raw_default_c", 4, "nearest", "raw", base - 8.0),
    ]
    result = []
    for name, scale, interpolation, preprocessing, threshold in raw_specs:
        if not math.isfinite(threshold) or threshold <= 0.0:
            continue
        result.append(
            {
                "name": name,
                "scale": int(scale),
                "interpolation": interpolation,
                "preprocessing": preprocessing,
                "adaptive_thresh_constant": float(threshold),
            }
        )
    return result


def reflected_missing_marker_roi(
    *,
    frame_width: int,
    frame_height: int,
    peer_center: Sequence[float],
    half_width_px: int = 140,
    half_height_px: int = 110,
) -> dict:
    """Predict and clip the missing-finger ROI by reflection about image center."""

    width = int(frame_width)
    height = int(frame_height)
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if int(half_width_px) <= 0 or int(half_height_px) <= 0:
        raise ValueError("ROI half extents must be positive")
    peer = _finite_marker_center(peer_center)
    if peer is None:
        raise ValueError("peer center must be finite")
    center_x = width / 2.0
    predicted = (2.0 * center_x - peer[0], peer[1])
    roi_width = min(width, 2 * int(half_width_px))
    roi_height = min(height, 2 * int(half_height_px))
    x0 = int(round(predicted[0] - roi_width / 2.0))
    y0 = int(round(predicted[1] - roi_height / 2.0))
    x0 = min(max(0, x0), width - roi_width)
    y0 = min(max(0, y0), height - roi_height)
    return {
        "predicted_center": predicted,
        "roi_xyxy_px": [x0, y0, x0 + roi_width, y0 + roi_height],
        "image_center_x": center_x,
    }


def _prepare_adaptive_roi_image(roi, spec: dict):
    import cv2
    import numpy as np

    scale = int(spec["scale"])
    interpolation_name = str(spec["interpolation"])
    if scale == 1:
        prepared = roi
    else:
        interpolation = {
            "nearest": cv2.INTER_NEAREST,
            "linear": cv2.INTER_LINEAR,
        }.get(interpolation_name)
        if interpolation is None:
            raise ValueError(f"unsupported ROI interpolation: {interpolation_name}")
        prepared = cv2.resize(
            roi,
            None,
            fx=float(scale),
            fy=float(scale),
            interpolation=interpolation,
        )
    preprocessing = str(spec["preprocessing"])
    if preprocessing == "raw":
        return prepared
    if preprocessing == "gray_gamma_0.7":
        gray = cv2.cvtColor(prepared, cv2.COLOR_BGR2GRAY)
        lut = np.rint(
            np.power(np.arange(256, dtype=np.float32) / 255.0, 0.7) * 255.0
        ).astype(np.uint8)
        return cv2.LUT(gray, lut)
    raise ValueError(f"unsupported ROI preprocessing: {preprocessing}")


def _map_roi_detection_to_frame(
    *,
    corners,
    ids,
    roi_xyxy_px: Sequence[int],
    scale: int,
) -> tuple[dict[int, tuple[float, float]], dict[int, float]]:
    centers = compute_centers(corners, ids)
    sides = compute_marker_side_lengths(corners, ids)
    x0, y0, _, _ = map(float, roi_xyxy_px)
    scale_f = float(scale)
    mapped_centers = {
        marker_id: (x0 + center[0] / scale_f, y0 + center[1] / scale_f)
        for marker_id, center in centers.items()
    }
    mapped_sides = {
        marker_id: float(side) / scale_f for marker_id, side in sides.items()
    }
    return mapped_centers, mapped_sides


def detect_missing_marker_roi_cascade(
    *,
    frame,
    marker_a: int,
    marker_b: int,
    primary_centers: dict[int, tuple[float, float]],
    primary_sides: dict[int, float],
    primary_id_values: Sequence[int],
    detector_passes: Sequence[tuple[dict, object]],
    distance_support_px: Sequence[float],
    peer_consistency_px: float = 8.0,
    center_error_limit_px: float = 35.0,
    vertical_error_limit_px: float = 25.0,
    midpoint_error_limit_px: float = 17.5,
    minimum_side_ratio: float = 0.65,
) -> dict | None:
    """Exhaust the measured ROI cascade for an exactly-one-primary frame."""

    counts = {
        int(marker_a): list(primary_id_values).count(int(marker_a)),
        int(marker_b): list(primary_id_values).count(int(marker_b)),
    }
    unique = [marker_id for marker_id, count in counts.items() if count == 1]
    if len(unique) != 1:
        return None
    peer_id = unique[0]
    missing_id = int(marker_b) if peer_id == int(marker_a) else int(marker_a)
    if counts[missing_id] != 0:
        return None
    peer = _finite_marker_center(primary_centers.get(peer_id))
    if peer is None:
        return None
    geometry = reflected_missing_marker_roi(
        frame_width=int(frame.shape[1]),
        frame_height=int(frame.shape[0]),
        peer_center=peer,
    )
    x0, y0, x1, y1 = geometry["roi_xyxy_px"]
    roi = frame[y0:y1, x0:x1]
    predicted_x, predicted_y = geometry["predicted_center"]
    center_x = float(geometry["image_center_x"])
    minimum_distance, maximum_distance = map(float, distance_support_px)
    for spec, retry_detector in detector_passes:
        prepared = _prepare_adaptive_roi_image(roi, spec)
        corners, ids, _ = retry_detector.detectMarkers(prepared)
        id_values = [] if ids is None else [int(value) for value in ids.reshape(-1)]
        if id_values.count(missing_id) != 1 or id_values.count(peer_id) > 1:
            continue
        mapped_centers, mapped_sides = _map_roi_detection_to_frame(
            corners=corners,
            ids=ids,
            roi_xyxy_px=geometry["roi_xyxy_px"],
            scale=int(spec["scale"]),
        )
        recovered = _finite_marker_center(mapped_centers.get(missing_id))
        if recovered is None:
            continue
        center_error = math.hypot(recovered[0] - predicted_x, recovered[1] - predicted_y)
        vertical_error = abs(recovered[1] - peer[1])
        midpoint_error = abs((recovered[0] + peer[0]) / 2.0 - center_x)
        if (
            center_error > float(center_error_limit_px)
            or vertical_error > float(vertical_error_limit_px)
            or midpoint_error > float(midpoint_error_limit_px)
        ):
            continue
        distance = math.hypot(recovered[0] - peer[0], recovered[1] - peer[1])
        if not minimum_distance <= distance <= maximum_distance:
            continue
        peer_side = primary_sides.get(peer_id)
        recovered_side = mapped_sides.get(missing_id)
        try:
            peer_side_f = float(peer_side)
            recovered_side_f = float(recovered_side)
        except (TypeError, ValueError):
            continue
        if not (
            math.isfinite(peer_side_f)
            and math.isfinite(recovered_side_f)
            and peer_side_f > 0.0
            and recovered_side_f > 0.0
        ):
            continue
        side_ratio = min(peer_side_f, recovered_side_f) / max(
            peer_side_f, recovered_side_f
        )
        if side_ratio < float(minimum_side_ratio):
            continue
        peer_delta = None
        fallback_peer = _finite_marker_center(mapped_centers.get(peer_id))
        if id_values.count(peer_id) == 1:
            if fallback_peer is None:
                continue
            peer_delta = math.hypot(
                fallback_peer[0] - peer[0], fallback_peer[1] - peer[1]
            )
            if peer_delta > float(peer_consistency_px):
                continue
        return {
            "threshold_constant": float(spec["adaptive_thresh_constant"]),
            "detector_strategy": str(spec["name"]),
            "centers": {missing_id: recovered, **({peer_id: fallback_peer} if fallback_peer else {})},
            "marker_sides": {
                missing_id: recovered_side_f,
                **({peer_id: mapped_sides[peer_id]} if peer_id in mapped_sides else {}),
            },
            "id_values": id_values,
            "roi_xyxy_px": list(geometry["roi_xyxy_px"]),
            "center_error_px": center_error,
            "vertical_error_px": vertical_error,
            "midpoint_error_px": midpoint_error,
            "side_ratio": side_ratio,
            "peer_delta_px": peer_delta,
        }
    return None


def select_adaptive_dual_candidate(
    *,
    marker_a: int,
    marker_b: int,
    primary_centers: dict[int, tuple[float, float]],
    primary_sides: dict[int, float],
    primary_id_values: Sequence[int],
    fallback_detections: Sequence[dict],
    peer_consistency_px: float,
    distance_support_px: Sequence[float],
) -> dict | None:
    """Select the first strict two-marker result from an ordered retry cascade.

    A unique marker found by the primary detector remains the peer anchor.  A
    retry may then contribute only the missing expected ID; it need not also
    rediscover the already trusted peer.  If it does rediscover the peer, the
    centers must agree.  When primary found neither (or an ambiguous duplicate),
    one retry must independently return each expected ID exactly once.
    """

    if int(marker_a) == int(marker_b):
        raise ValueError("adaptive dual selection requires distinct marker IDs")
    minimum_distance, maximum_distance = map(float, distance_support_px)
    primary_counts = {
        marker_a: list(primary_id_values).count(marker_a),
        marker_b: list(primary_id_values).count(marker_b),
    }
    primary_unique = [
        marker_id
        for marker_id in (marker_a, marker_b)
        if primary_counts[marker_id] == 1
    ]
    partial_peer = None
    if len(primary_unique) == 1:
        peer_id = primary_unique[0]
        missing_id = marker_b if peer_id == marker_a else marker_a
        if primary_counts[missing_id] == 0 and peer_id in primary_centers:
            partial_peer = (peer_id, missing_id)

    for fallback in fallback_detections:
        centers = fallback.get("centers") or {}
        sides = fallback.get("marker_sides") or {}
        id_values = [int(value) for value in fallback.get("id_values") or []]
        counts = {
            marker_a: id_values.count(marker_a),
            marker_b: id_values.count(marker_b),
        }
        peer_delta_px: float | None = None
        if partial_peer is not None:
            peer_id, missing_id = partial_peer
            # The missing expected marker must be unique.  A fallback peer is
            # optional, but duplicate expected IDs are always ambiguous.
            if counts[missing_id] != 1 or counts[peer_id] > 1:
                continue
            recovered = _finite_marker_center(centers.get(missing_id))
            peer = _finite_marker_center(primary_centers.get(peer_id))
            if recovered is None or peer is None:
                continue
            fallback_peer = _finite_marker_center(centers.get(peer_id))
            if counts[peer_id] == 1:
                if fallback_peer is None:
                    continue
                peer_delta_px = math.hypot(
                    fallback_peer[0] - peer[0], fallback_peer[1] - peer[1]
                )
                if peer_delta_px > float(peer_consistency_px):
                    continue
            selected_centers = {peer_id: peer, missing_id: recovered}
            selected_sides = {
                peer_id: primary_sides.get(peer_id),
                missing_id: sides.get(missing_id),
            }
            recovered_role = "a" if missing_id == marker_a else "b"
            strategy = f"recover_missing_{recovered_role}"
        else:
            # With no primary peer, both configured IDs must be recovered by
            # the same retry pass. Never combine detections across thresholds.
            if counts[marker_a] != 1 or counts[marker_b] != 1:
                continue
            center_a = _finite_marker_center(centers.get(marker_a))
            center_b = _finite_marker_center(centers.get(marker_b))
            if center_a is None or center_b is None:
                continue
            selected_centers = {marker_a: center_a, marker_b: center_b}
            selected_sides = {
                marker_a: sides.get(marker_a),
                marker_b: sides.get(marker_b),
            }
            recovered_role = "both"
            strategy = "recover_both"

        center_a = selected_centers[marker_a]
        center_b = selected_centers[marker_b]
        distance_px = math.hypot(
            center_b[0] - center_a[0], center_b[1] - center_a[1]
        )
        if not minimum_distance <= distance_px <= maximum_distance:
            continue
        finite_sides = []
        for value in selected_sides.values():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0.0:
                finite_sides.append(value)
        # Both fingers carry equal-size printed markers.  Keep this deliberately
        # broad for fisheye perspective, but reject an obviously unrelated quad.
        if len(finite_sides) == 2 and min(finite_sides) / max(finite_sides) < 0.55:
            continue
        threshold_constant = float(fallback["threshold_constant"])
        return {
            "centers": selected_centers,
            "marker_sides": selected_sides,
            "distance_px": distance_px,
            "adaptive_recovered_role": recovered_role,
            "adaptive_retry_strategy": strategy,
            "adaptive_retry_detector_strategy": str(
                fallback.get("detector_strategy")
                or f"full_frame_raw_c{threshold_constant:g}"
            ),
            "adaptive_retry_threshold_constant": threshold_constant,
            "adaptive_retry_peer_delta_px": peer_delta_px,
            "adaptive_retry_roi_xyxy_px": fallback.get("roi_xyxy_px"),
            "adaptive_retry_center_error_px": fallback.get("center_error_px"),
            "adaptive_retry_vertical_error_px": fallback.get("vertical_error_px"),
            "adaptive_retry_midpoint_error_px": fallback.get("midpoint_error_px"),
            "adaptive_retry_side_ratio": fallback.get("side_ratio"),
        }
    return None


def validate_raw_detection_row(row: object, row_schema: str | None) -> bool:
    """Validate the committed JSONL contract without accepting semantic drift.

    Legacy v1 chunks had no explicit row schema and are kept resume-compatible.
    Adaptive-retry chunks use strict versioned contracts so a valid SHA-256
    cannot make an obsolete or internally contradictory row reusable.
    """

    if not isinstance(row, dict):
        return False
    frame_idx = row.get("frame_idx")
    if not isinstance(frame_idx, int) or isinstance(frame_idx, bool):
        return False
    if row_schema is None:
        # Historical chunks are intentionally additive/loosely shaped. Their
        # detection fingerprint and raw SHA-256 remain the compatibility gate.
        return True
    if row_schema not in {RAW_DETECTION_ROW_SCHEMA_V3, RAW_DETECTION_ROW_SCHEMA_V4}:
        return False

    required = {
        "frame_idx",
        "a",
        "b",
        "marker_side_px",
        "distance_px",
        "valid",
        "primary_valid",
        "adaptive_retry_valid",
        "adaptive_recovered_role",
    }
    if not required.issubset(row):
        return False
    if row_schema == RAW_DETECTION_ROW_SCHEMA_V4:
        retry_fields = {
            "adaptive_retry_strategy",
            "adaptive_retry_detector_strategy",
            "adaptive_retry_threshold_constant",
            "adaptive_retry_peer_delta_px",
            "adaptive_retry_roi_xyxy_px",
            "adaptive_retry_center_error_px",
            "adaptive_retry_vertical_error_px",
            "adaptive_retry_midpoint_error_px",
            "adaptive_retry_side_ratio",
        }
        if not retry_fields.issubset(row):
            return False
    valid = row["valid"]
    primary_valid = row["primary_valid"]
    adaptive_valid = row["adaptive_retry_valid"]
    if not all(isinstance(value, bool) for value in (valid, primary_valid, adaptive_valid)):
        return False
    if valid != primary_valid or (primary_valid and adaptive_valid):
        return False

    def finite_center(value: object) -> bool:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return False
        try:
            return all(math.isfinite(float(item)) for item in value)
        except (TypeError, ValueError):
            return False

    a_present = row["a"] is not None
    b_present = row["b"] is not None
    if (a_present and not finite_center(row["a"])) or (
        b_present and not finite_center(row["b"])
    ):
        return False
    distance = row["distance_px"]
    if distance is not None:
        try:
            distance = float(distance)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(distance) or distance <= 0.0:
            return False
    marker_side = row["marker_side_px"]
    if marker_side is not None:
        try:
            marker_side = float(marker_side)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(marker_side) or marker_side <= 0.0:
            return False

    role = row["adaptive_recovered_role"]
    if primary_valid:
        valid_primary = (
            a_present
            and b_present
            and distance is not None
            and role is None
        )
        if row_schema == RAW_DETECTION_ROW_SCHEMA_V4:
            valid_primary = valid_primary and all(
                row[field] is None
                for field in (
                    "adaptive_retry_strategy",
                    "adaptive_retry_detector_strategy",
                    "adaptive_retry_threshold_constant",
                    "adaptive_retry_peer_delta_px",
                    "adaptive_retry_roi_xyxy_px",
                    "adaptive_retry_center_error_px",
                    "adaptive_retry_vertical_error_px",
                    "adaptive_retry_midpoint_error_px",
                    "adaptive_retry_side_ratio",
                )
            )
        return valid_primary
    if adaptive_valid:
        valid_adaptive = (
            a_present
            and b_present
            and distance is not None
            and role in ({"a", "b"} if row_schema == RAW_DETECTION_ROW_SCHEMA_V3 else {"a", "b", "both"})
        )
        if row_schema == RAW_DETECTION_ROW_SCHEMA_V4:
            strategy = row["adaptive_retry_strategy"]
            detector_strategy = row["adaptive_retry_detector_strategy"]
            expected_strategy = {
                "a": "recover_missing_a",
                "b": "recover_missing_b",
                "both": "recover_both",
            }.get(role)
            try:
                threshold = float(row["adaptive_retry_threshold_constant"])
            except (TypeError, ValueError):
                return False
            peer_delta = row["adaptive_retry_peer_delta_px"]
            if peer_delta is not None:
                try:
                    peer_delta = float(peer_delta)
                except (TypeError, ValueError):
                    return False
            roi_strategy_names = {
                spec["name"] for spec in adaptive_roi_retry_specs(15.0)
            }
            roi_xyxy = row["adaptive_retry_roi_xyxy_px"]
            roi_evidence_fields = (
                "adaptive_retry_center_error_px",
                "adaptive_retry_vertical_error_px",
                "adaptive_retry_midpoint_error_px",
                "adaptive_retry_side_ratio",
            )
            if detector_strategy in roi_strategy_names:
                if not (
                    isinstance(roi_xyxy, (list, tuple))
                    and len(roi_xyxy) == 4
                    and all(
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 0
                        for value in roi_xyxy
                    )
                    and roi_xyxy[0] < roi_xyxy[2]
                    and roi_xyxy[1] < roi_xyxy[3]
                ):
                    return False
                try:
                    center_error, vertical_error, midpoint_error, side_ratio = (
                        float(row[field]) for field in roi_evidence_fields
                    )
                except (TypeError, ValueError):
                    return False
                valid_roi_evidence = (
                    math.isfinite(center_error)
                    and 0.0 <= center_error <= 35.0
                    and math.isfinite(vertical_error)
                    and 0.0 <= vertical_error <= 25.0
                    and math.isfinite(midpoint_error)
                    and 0.0 <= midpoint_error <= 17.5
                    and math.isfinite(side_ratio)
                    and 0.65 <= side_ratio <= 1.0
                )
            else:
                valid_roi_evidence = (
                    isinstance(detector_strategy, str)
                    and detector_strategy.startswith("full_frame_raw_c")
                    and roi_xyxy is None
                    and all(row[field] is None for field in roi_evidence_fields)
                )
            valid_adaptive = valid_adaptive and (
                strategy == expected_strategy
                and isinstance(detector_strategy, str)
                and bool(detector_strategy.strip())
                and valid_roi_evidence
                and math.isfinite(threshold)
                and threshold > 0.0
                and (
                    peer_delta is None
                    or (math.isfinite(peer_delta) and peer_delta >= 0.0)
                )
            )
        return valid_adaptive
    valid_missing = role is None and distance is None and marker_side is None
    if row_schema == RAW_DETECTION_ROW_SCHEMA_V4:
        valid_missing = valid_missing and all(
            row[field] is None
            for field in (
                "adaptive_retry_strategy",
                "adaptive_retry_detector_strategy",
                "adaptive_retry_threshold_constant",
                "adaptive_retry_peer_delta_px",
                "adaptive_retry_roi_xyxy_px",
                "adaptive_retry_center_error_px",
                "adaptive_retry_vertical_error_px",
                "adaptive_retry_midpoint_error_px",
                "adaptive_retry_side_ratio",
            )
        )
    return valid_missing


def fixed_px_to_meter_scale(args: argparse.Namespace) -> float | None:
    if args.px_to_meter_scale is not None:
        return float(args.px_to_meter_scale)
    if args.calibration_distance_px is not None:
        if args.calibration_distance_px <= 0.0:
            raise ValueError("--calibration-distance-px must be > 0")
        return float(args.calibration_distance_m / args.calibration_distance_px)
    return None


def load_gripper_calibration(path: Path | None) -> dict | None:
    import numpy as np

    if path is None:
        return None
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"gripper calibration JSON not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    points = []
    for row in data:
        if "px" not in row:
            continue
        if "m" in row:
            meters = float(row["m"])
        elif "cm" in row:
            meters = float(row["cm"]) / 100.0
        else:
            continue
        points.append((float(row["px"]), meters, meters * 100.0, row.get("source", "")))
    if len(points) < 2:
        raise ValueError(f"gripper calibration needs at least 2 valid points: {path}")
    points.sort(key=lambda item: item[0])
    px = np.asarray([p[0] for p in points], dtype=np.float64)
    meters = np.asarray([p[1] for p in points], dtype=np.float64)
    cm = np.asarray([p[2] for p in points], dtype=np.float64)
    if np.any(np.diff(px) <= 0.0):
        raise ValueError(f"gripper calibration px values must be strictly increasing: {path}")
    return {
        "path": str(path),
        "points": points,
        "px": px,
        "m": meters,
        "cm": cm,
        "min_px": float(px[0]),
        "max_px": float(px[-1]),
        "min_m": float(meters[0]),
        "max_m": float(meters[-1]),
    }


def apply_calibrated_width_deadzone(
    width_m: float | None,
    width_cm: float | None,
    args: argparse.Namespace,
) -> tuple[float | None, float | None, bool]:
    threshold = getattr(args, "gripper_zero_below_m", None)
    if (
        threshold is not None
        and width_m is not None
        and math.isfinite(width_m)
        and width_m < threshold
    ):
        return 0.0, 0.0, width_m != 0.0
    return width_m, width_cm, False


def distance_px_to_width(
    distance_px: float | None,
    calibration: dict | None,
    marker_side_px: float | None,
    args: argparse.Namespace,
    *,
    apply_deadzone: bool = True,
) -> tuple[float | None, float | None, str]:
    import numpy as np

    if distance_px is None:
        return None, None, "missing"
    if calibration is not None:
        px = calibration["px"]
        meters = calibration["m"]
        clipped_px = float(np.clip(distance_px, px[0], px[-1]))
        width_m = float(np.interp(clipped_px, px, meters))
        width_cm = width_m * 100.0
        zeroed = False
        if apply_deadzone:
            width_m, width_cm, zeroed = apply_calibrated_width_deadzone(
                width_m, width_cm, args
            )
        mode = "calibration_json_clamped" if clipped_px != float(distance_px) else "calibration_json"
        return width_m, width_cm, f"{mode}_deadzone" if zeroed else mode
    fixed_scale = fixed_px_to_meter_scale(args)
    if fixed_scale is not None:
        width_m = float(distance_px * fixed_scale)
        width_cm = width_m * 100.0
        zeroed = False
        if apply_deadzone:
            width_m, width_cm, zeroed = apply_calibrated_width_deadzone(
                width_m, width_cm, args
            )
        return width_m, width_cm, "fixed_px_to_meter_scale_deadzone" if zeroed else "fixed_px_to_meter_scale"
    if marker_side_px is None or marker_side_px <= 1e-9:
        return None, None, "unavailable"
    width_m = float(distance_px / marker_side_px * args.marker_width_m)
    width_cm = width_m * 100.0
    zeroed = False
    if apply_deadzone:
        width_m, width_cm, zeroed = apply_calibrated_width_deadzone(
            width_m, width_cm, args
        )
    return width_m, width_cm, "aruco_marker_width_per_frame_deadzone" if zeroed else "aruco_marker_width_per_frame"


def fmt(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def gripper_fieldnames() -> list[str]:
    return [
        "frame_idx",
        "timestamp_sec",
        "marker_a",
        "marker_b",
        "a_x",
        "a_y",
        "b_x",
        "b_y",
        "marker_side_px",
        "px_to_meter_scale",
        "center_distance_cm",
        "center_distance_cm_filled",
        "center_distance_px",
        "center_distance_px_filled",
        "center_distance_m",
        "center_distance_m_filled",
        "distance_px",
        "distance_px_filled",
        "distance_m",
        "distance_m_filled",
        "dual_marker_valid",
        "primary_dual_marker_valid",
        "adaptive_dual_marker_valid",
        "adaptive_retry_strategy",
        "adaptive_retry_detector_strategy",
        "adaptive_retry_threshold_constant",
        "adaptive_retry_peer_delta_px",
        "adaptive_retry_roi_xyxy_px",
        "adaptive_retry_center_error_px",
        "adaptive_retry_vertical_error_px",
        "adaptive_retry_midpoint_error_px",
        "adaptive_retry_side_ratio",
        "single_marker_valid",
        "marker_mask",
        "fallback_marker_id",
        "fallback_run_id",
        "continuity_pass",
        "calibration_clamped",
        "filled_from_frame_idx",
        "filled_from_measurement_source",
        "filled_calibration_clamped",
        "direct_deadzone_zeroed",
        "filled_deadzone_zeroed",
        "measurement_source",
        "filled_source",
        "valid",
    ]


def raw_extract_artifact_path(video_path: Path) -> Path:
    return video_path.with_suffix(video_path.suffix + ".extract.json")


def raw_extract_identity(path: Path) -> dict:
    identity = sampled_file_identity(path)
    return {
        "path": identity["path"],
        "size_bytes": identity["size"],
        "mtime_ns": identity["mtime_ns"],
        "sample_sha256": identity["sample_sha256"],
    }


def load_verified_raw_extract_artifact(video_path: Path) -> tuple[dict | None, str]:
    """Load a v2 `_01` artifact only when it still describes this exact file."""

    sidecar_path = raw_extract_artifact_path(video_path)
    if not sidecar_path.is_file():
        return None, "missing_provenance_sidecar"
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"unreadable_provenance_sidecar={exc}"
    if payload.get("schema") != RAW_VIDEO_ARTIFACT_SCHEMA:
        return None, f"unsupported_provenance_schema={payload.get('schema')!r}"
    try:
        current_identity = raw_extract_identity(video_path)
    except OSError as exc:
        return None, f"unreadable_video_identity={exc}"
    if payload.get("output_identity") != current_identity:
        return None, "provenance_output_identity_changed"
    if not isinstance(payload.get("recipe"), dict):
        return None, "provenance_recipe_missing"
    return payload, "verified"


def native_segments_match_canonical(
    canonical_path: Path,
    candidates: list[Path],
) -> tuple[bool, str]:
    """Prove that `_01` chapter artifacts are the canonical video's inputs.

    A direct-concat `_01` run deliberately leaves historical chapter files in
    place.  Frame count/geometry alone therefore cannot authorize their use:
    stale chapters can look media-compatible while containing different
    pixels.  Require the canonical v2 recipe and every chapter v2 recipe to
    bind to the same ordered source identities.
    """

    canonical_artifact, reason = load_verified_raw_extract_artifact(canonical_path)
    if canonical_artifact is None:
        return False, f"canonical_{reason}"
    canonical_recipe = canonical_artifact["recipe"]
    if canonical_recipe.get("operation") != "ordered_front_fisheye_stream_copy":
        return False, "canonical_recipe_is_not_front_stream_copy"
    canonical_inputs = canonical_recipe.get("inputs")
    if not isinstance(canonical_inputs, list) or not canonical_inputs:
        return False, "canonical_recipe_inputs_missing"
    if len(candidates) != len(canonical_inputs):
        return False, f"segment_count={len(candidates)}, expected={len(canonical_inputs)}"

    for index, (candidate, canonical_input) in enumerate(zip(candidates, canonical_inputs)):
        artifact, segment_reason = load_verified_raw_extract_artifact(candidate)
        if artifact is None:
            return False, f"segment[{index}]_{segment_reason}"
        recipe = artifact["recipe"]
        if recipe.get("operation") != "ordered_front_fisheye_stream_copy":
            return False, f"segment[{index}]_recipe_is_not_front_stream_copy"
        segment_inputs = recipe.get("inputs")
        if segment_inputs != [canonical_input]:
            return False, f"segment[{index}]_source_provenance_mismatch"
        expectation = artifact.get("expectation")
        if not isinstance(expectation, dict):
            return False, f"segment[{index}]_expectation_missing"
        if int(expectation.get("frame_count") or -1) != int(canonical_input.get("frame_limit") or -2):
            return False, f"segment[{index}]_frame_limit_mismatch"
    return True, "ordered_segment_provenance_verified"


def discover_video_sources(video_path: Path, timestamp_count: int, ffprobe_bin: str) -> tuple[list[dict], dict]:
    """Prefer the losslessly concatenated `_01` chapter files when complete."""

    video_path = video_path.expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"front fisheye video not found: {video_path}")
    canonical_probe = probe_video(video_path, ffprobe_bin)
    canonical_identity = sampled_file_identity(video_path)
    if canonical_probe["frame_count"] != timestamp_count:
        raise ValueError(
            "strict frame-count check failed for front_fisheye: "
            f"video={canonical_probe['frame_count']}, timestamps={timestamp_count}, path={video_path}"
        )

    segment_dir = video_path.parent / ".segments" / video_path.stem
    candidates = sorted(segment_dir.glob(f"*{video_path.suffix}")) if segment_dir.is_dir() else []
    source_paths = [video_path]
    source_kind = "canonical_video"
    segment_probes: list[dict] = []
    if candidates:
        try:
            provenance_ok, provenance_reason = native_segments_match_canonical(video_path, candidates)
            if not provenance_ok:
                print(
                    "[warn] ignoring unproven _01 native segments without probing them: "
                    f"provenance={provenance_reason}",
                    flush=True,
                )
            else:
                segment_probes = [probe_video(path, ffprobe_bin) for path in candidates]
                segment_total = sum(item["frame_count"] for item in segment_probes)
                compatible = all(
                    item["width"] == canonical_probe["width"]
                    and item["height"] == canonical_probe["height"]
                    and math.isclose(item["fps_float"], canonical_probe["fps_float"], rel_tol=0.0, abs_tol=1e-9)
                    for item in segment_probes
                )
                if segment_total == timestamp_count and compatible:
                    source_paths = candidates
                    source_kind = "native_01_segments"
                else:
                    print(
                        "[warn] ignoring incomplete/incompatible proven _01 native segments: "
                        f"count={segment_total}/{timestamp_count}, compatible={compatible}",
                        flush=True,
                    )
        except Exception as exc:
            print(f"[warn] could not validate _01 native segments; using canonical video: {exc}", flush=True)

    probes = segment_probes if source_kind == "native_01_segments" else [canonical_probe]
    sources = []
    global_start = 0
    for index, (path, probe) in enumerate(zip(source_paths, probes)):
        identity = canonical_identity if path.resolve() == video_path else sampled_file_identity(path)
        sources.append(
            {
                "source_index": index,
                "original_path": str(path.resolve()),
                "decode_path": str(path.resolve()),
                "identity": identity,
                "probe": probe,
                "global_start": global_start,
            }
        )
        global_start += int(probe["frame_count"])
    if global_start != timestamp_count:
        raise AssertionError(f"internal source plan frame count mismatch: {global_start} != {timestamp_count}")
    return sources, {
        "kind": source_kind,
        "canonical_path": str(video_path),
        "canonical_identity": canonical_identity,
        "canonical_probe": canonical_probe,
        "decode_sources": [
            {
                "source_index": source["source_index"],
                "original_path": source["original_path"],
                "identity": source["identity"],
                "probe": source["probe"],
                "global_start": source["global_start"],
            }
            for source in sources
        ],
    }


def discover_raw_insv_sources(
    raw_manifest: dict,
    timestamp_count: int,
    ffprobe_bin: str,
) -> tuple[list[dict], dict]:
    """Use ordered INSV stream 0 chapters directly as the 1920px fisheye.

    `_01` historically stream-copied this exact HEVC stream into a canonical
    MP4 before ArUco detection.  Reading the ordered raw chapters removes that
    extra multi-GB write while preserving decoded pixels and global frame
    indices across camera chapter boundaries.
    """

    copied = raw_manifest.get("copied_sources") or {}
    raw_paths = copied.get("insv")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise RuntimeError("raw manifest has no ordered copied_sources.insv chapters")
    paths = [Path(value).expanduser().resolve() for value in raw_paths]
    if len(set(paths)) != len(paths):
        raise RuntimeError("raw manifest contains duplicate INSV chapters")

    sources: list[dict] = []
    global_start = 0
    reference_geometry: tuple[int, int] | None = None
    reference_fps: str | None = None
    for index, path in enumerate(paths):
        if not path.is_file():
            raise FileNotFoundError(f"raw INSV chapter not found: {path}")
        probe = probe_video(path, ffprobe_bin)
        geometry = (int(probe["width"]), int(probe["height"]))
        fps = str(probe["fps"])
        if reference_geometry is None:
            reference_geometry = geometry
            reference_fps = fps
        elif geometry != reference_geometry or fps != reference_fps:
            raise RuntimeError(
                "raw INSV stream-0 format changed across chapters: "
                f"chapter={path} geometry={geometry} fps={fps}; "
                f"expected={reference_geometry} fps={reference_fps}"
            )
        identity = sampled_file_identity(path)
        sources.append(
            {
                "source_index": index,
                "original_path": str(path),
                "decode_path": str(path),
                "identity": identity,
                "probe": probe,
                "global_start": global_start,
            }
        )
        global_start += int(probe["frame_count"])
    if global_start != timestamp_count:
        raise RuntimeError(
            "raw INSV stream-0 frame count does not match calibrated timestamps: "
            f"insv={global_start}, timestamps={timestamp_count}"
        )
    assert reference_geometry is not None and reference_fps is not None
    combined_probe = dict(sources[0]["probe"])
    combined_probe["frame_count"] = global_start
    combined_probe["width"], combined_probe["height"] = reference_geometry
    combined_probe["fps"] = reference_fps
    return sources, {
        "kind": "ordered_raw_insv_stream0",
        "canonical_path": None,
        "canonical_identity": {
            "kind": "ordered_raw_insv_stream0",
            "chapters": [source["identity"] for source in sources],
        },
        "canonical_probe": combined_probe,
        "decode_sources": [
            {
                "source_index": source["source_index"],
                "original_path": source["original_path"],
                "identity": source["identity"],
                "probe": source["probe"],
                "global_start": source["global_start"],
            }
            for source in sources
        ],
    }


def prepare_fast_cache(
    sources: list[dict],
    fast_cache_root: Path | None,
    source_set_fingerprint: str,
    input_cache_policy: str = "auto",
) -> tuple[list[dict], Path | None, str]:
    if fast_cache_root is None:
        return sources, None, "disabled"
    if input_cache_policy not in {"auto", "copy", "direct"}:
        raise ValueError(f"unsupported input cache policy: {input_cache_policy}")
    cache_root = fast_cache_root.expanduser().resolve()
    work_dir = cache_root / "stage-work" / "03_gripper_estimation" / source_set_fingerprint
    work_dir.mkdir(parents=True, exist_ok=True)
    from fast_workspace import shares_physical_storage

    same_storage = all(
        shares_physical_storage(Path(source["original_path"]), cache_root)
        for source in sources
    )
    should_copy = input_cache_policy == "copy" or (
        input_cache_policy == "auto" and not same_storage
    )
    if not should_copy:
        reason = "explicit-direct" if input_cache_policy == "direct" else "same-physical-storage"
        print(
            "[cache] direct immutable INSV decode; skipping duplicate input copy "
            f"reason={reason} work_dir={work_dir}",
            flush=True,
        )
        return sources, work_dir, f"direct:{reason}"
    prepared = []
    for source in sources:
        original = Path(source["original_path"])
        print(f"[cache] verify/copy {original}", flush=True)
        started = time.monotonic()
        cached_path = cache_input_file(original, cache_root)
        if sampled_file_identity(original) != source["identity"]:
            raise RuntimeError(f"source video changed while it was being cached: {original}")
        print(
            f"[cache] ready {cached_path} ({original.stat().st_size / (1024**3):.2f} GiB, "
            f"{time.monotonic() - started:.1f}s)",
            flush=True,
        )
        cached_source = dict(source)
        cached_source["decode_path"] = str(cached_path)
        prepared.append(cached_source)
    return prepared, work_dir, "copied"


def build_chunk_plan(
    sources: list[dict],
    target_frames: int,
    ffprobe_bin: str,
    split_source: bool,
    chunk_size_frames: int,
    active_ranges: Sequence[ActiveFrameRange] | None = None,
) -> list[dict]:
    if target_frames <= 0:
        return []
    chunks = []
    if active_ranges is not None:
        for source in sources:
            source_global_start = int(source["global_start"])
            source_frame_count = int(source["probe"]["frame_count"])
            source_global_end = min(source_global_start + source_frame_count, target_frames)
            requested_by_segment: list[tuple[int, list[int]]] = []
            for active_range in active_ranges:
                requested = [
                    frame_idx - source_global_start
                    for frame_idx in active_range.frame_indices
                    if source_global_start <= frame_idx < source_global_end
                ]
                if requested:
                    requested_by_segment.append((active_range.segment_index, requested))
            if not requested_by_segment:
                continue
            keyframes = probe_keyframes(Path(source["decode_path"]), ffprobe_bin, source["probe"])
            for segment_index, requested in requested_by_segment:
                cursor = 0
                while cursor < len(requested):
                    if split_source:
                        requested_end = requested[cursor] + chunk_size_frames
                        next_cursor = bisect.bisect_left(requested, requested_end, lo=cursor + 1)
                        next_cursor = max(cursor + 1, next_cursor)
                    else:
                        next_cursor = len(requested)
                    selected = requested[cursor:next_cursor]
                    requested_start = selected[0]
                    keyframe_position = bisect.bisect_right(keyframes, requested_start) - 1
                    if keyframe_position < 0:
                        raise RuntimeError(
                            f"no keyframe precedes requested frame {requested_start}: {source['decode_path']}"
                        )
                    decode_start = keyframes[keyframe_position]
                    decode_end = selected[-1] + 1
                    chunks.append(
                        {
                            "source_index": int(source["source_index"]),
                            "original_path": source["original_path"],
                            "decode_path": source["decode_path"],
                            "source_identity": source["identity"],
                            "width": int(source["probe"]["width"]),
                            "height": int(source["probe"]["height"]),
                            "local_start": decode_start,
                            "local_end": decode_end,
                            "global_start": source_global_start + decode_start,
                            "global_end": source_global_start + decode_end,
                            "source_global_start": source_global_start,
                            "segment_index": segment_index,
                            "requested_local_indices": selected,
                            "requested_global_indices": [source_global_start + index for index in selected],
                            "keyframe_aligned": True,
                        }
                    )
                    cursor = next_cursor
        chunks.sort(key=lambda item: item["requested_global_indices"][0])
        for chunk_index, task in enumerate(chunks):
            task["chunk_index"] = chunk_index
        return chunks

    expected_global = 0
    for source in sources:
        source_global_start = int(source["global_start"])
        if source_global_start >= target_frames:
            break
        available = min(int(source["probe"]["frame_count"]), target_frames - source_global_start)
        boundaries = [0]
        if split_source and chunk_size_frames > 0 and available > chunk_size_frames:
            keyframes = probe_keyframes(Path(source["decode_path"]), ffprobe_bin, source["probe"])
            desired = chunk_size_frames
            while desired < available:
                position = bisect.bisect_left(keyframes, desired)
                if position >= len(keyframes):
                    break
                boundary = keyframes[position]
                if boundary <= boundaries[-1] or boundary >= available:
                    break
                boundaries.append(boundary)
                desired = boundary + chunk_size_frames
        boundaries.append(available)
        for local_start, local_end in zip(boundaries, boundaries[1:]):
            global_start = source_global_start + local_start
            global_end = source_global_start + local_end
            if global_start != expected_global:
                raise AssertionError(f"non-contiguous chunk plan at frame {expected_global}: {global_start}")
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "source_index": int(source["source_index"]),
                    "original_path": source["original_path"],
                    "decode_path": source["decode_path"],
                    "source_identity": source["identity"],
                    "width": int(source["probe"]["width"]),
                    "height": int(source["probe"]["height"]),
                    "local_start": local_start,
                    "local_end": local_end,
                    "global_start": global_start,
                    "global_end": global_end,
                    "source_global_start": source_global_start,
                    "segment_index": None,
                    "requested_local_indices": list(range(local_start, local_end)),
                    "requested_global_indices": list(range(global_start, global_end)),
                    "keyframe_aligned": True,
                }
            )
            expected_global = global_end
    if expected_global != target_frames:
        raise AssertionError(f"chunk plan covers {expected_global} frames, expected {target_frames}")
    return chunks


def _frame_index_from_pts(
    frame_pts: int,
    *,
    stream_start_pts: int,
    time_base: Fraction,
    frame_rate: Fraction,
) -> int:
    """Map a decoded presentation timestamp to the canonical frame index."""

    position = Fraction(frame_pts - stream_start_pts) * time_base * frame_rate
    index = round(position)
    # The X5 streams are CFR. Refuse a timestamp that cannot be assigned to a
    # frame unambiguously instead of silently shifting all subsequent rows.
    if abs(position - index) > Fraction(1, 4):
        raise RuntimeError(
            "decoded frame PTS is off the canonical CFR grid: "
            f"pts={frame_pts} start_pts={stream_start_pts} "
            f"position={float(position):.6f}"
        )
    return int(index)


def _iter_pyav_chunk_frames(
    path: str,
    decode_threads: int,
    local_start: int,
    local_end: int,
) -> Iterator[tuple[int, object]]:
    """Yield an exact frame range using presentation timestamps, not CV seek state.

    Under heavy concurrent random I/O, OpenCV ``CAP_PROP_POS_FRAMES`` was
    observed to report an inconsistent position and one decoder reported a
    false EOF. PyAV seeks backward to a keyframe as FFmpeg intends; decoded PTS
    values then identify and validate every frame before publication.

    PyAV's default color-space inference is deliberately not used: X5 frames
    are reformatted through explicit ITU-601 bilinear swscale, which is
    byte-identical to the established OpenCV VideoCapture BGR output. Thus this
    function changes frame addressing only, never the image contract.
    """

    try:
        import av
        from av.video.reformatter import Colorspace, Interpolation, VideoReformatter
    except ImportError as exc:
        raise RuntimeError("PyAV is required for exact chunk decoding") from exc

    if local_start < 0 or local_end <= local_start:
        raise ValueError(f"invalid decode interval: {local_start}:{local_end}")
    container = av.open(path, mode="r")
    try:
        video_streams = container.streams.video
        if not video_streams:
            raise RuntimeError(f"video stream 0:v:0 is missing: {path}")
        # X5 INSV containers expose both lens streams. The entire pipeline's
        # front-fisheye contract is explicitly 0:v:0, matching ffprobe's
        # -select_streams v:0 and the previous OpenCV default stream choice.
        stream = video_streams[0]
        stream.thread_count = max(1, int(decode_threads))
        if stream.time_base is None or stream.average_rate is None:
            raise RuntimeError(f"video stream lacks exact time_base/frame_rate metadata: {path}")
        time_base = Fraction(stream.time_base)
        frame_rate = Fraction(stream.average_rate)
        stream_start_pts = int(stream.start_time or 0)
        reformatter = VideoReformatter()
        if local_start:
            target_pts = stream_start_pts + round(
                Fraction(local_start, 1) / (time_base * frame_rate)
            )
            # Backward keyframe seek is expected to decode frames before the
            # target. Their PTS-derived indices are discarded explicitly.
            container.seek(target_pts, stream=stream, backward=True, any_frame=False)

        expected_index = local_start
        for decoded in container.decode(stream):
            if decoded.pts is None:
                raise RuntimeError(f"decoded frame has no PTS: path={path}")
            frame_index = _frame_index_from_pts(
                int(decoded.pts),
                stream_start_pts=stream_start_pts,
                time_base=time_base,
                frame_rate=frame_rate,
            )
            if frame_index < local_start:
                continue
            if frame_index >= local_end:
                break
            if frame_index != expected_index:
                raise RuntimeError(
                    "decoded frame sequence is not contiguous: "
                    f"expected={expected_index}, got={frame_index}, path={path}"
                )
            if bool(getattr(decoded, "is_corrupt", False)):
                raise RuntimeError(f"decoder returned a corrupt frame: frame={frame_index}, path={path}")
            bgr_frame = reformatter.reformat(
                decoded,
                format="bgr24",
                src_colorspace=Colorspace.ITU601,
                dst_colorspace=Colorspace.ITU601,
                interpolation=Interpolation.BILINEAR,
            ).to_ndarray()
            yield frame_index, bgr_frame
            expected_index += 1
            if expected_index == local_end:
                return
        raise RuntimeError(
            "early EOF during PTS-indexed decode: "
            f"next_frame={expected_index}, expected_end={local_end}, path={path}"
        )
    finally:
        container.close()


def _worker_setup() -> None:
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    import cv2

    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)


def detect_chunk(task: dict) -> dict:
    """Decode one keyframe-aligned range and atomically persist raw detections."""

    _worker_setup()
    import cv2
    import numpy as np

    marker_a = int(task["marker_a"])
    marker_b = int(task["marker_b"])
    if marker_a == marker_b:
        raise ValueError("marker_a and marker_b must be distinct configured IDs")
    output_path = Path(task["raw_path"])
    manifest_path = Path(task["chunk_manifest_path"])
    front_cache_path = (
        None
        if task.get("front_cache_path") is None
        else Path(task["front_cache_path"])
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp.{os.getpid()}")
    front_cache_temp = (
        None
        if front_cache_path is None
        else front_cache_path.with_name(
            f".{front_cache_path.stem}.tmp.{os.getpid()}{front_cache_path.suffix}"
        )
    )
    detector = make_detector(task["dictionary"])
    adaptive_retry_constants = task.get("adaptive_retry_constants")
    if adaptive_retry_constants is None and task.get("adaptive_retry_constant") is not None:
        # Resume compatibility for an explicitly constructed v3 task. New
        # production tasks always carry the complete v4 cascade.
        adaptive_retry_constants = [float(task["adaptive_retry_constant"])]
    adaptive_retry_detectors = [
        (
            float(constant),
            make_detector(
                task["dictionary"], adaptive_thresh_constant=float(constant)
            ),
        )
        for constant in (adaptive_retry_constants or [])
    ]
    adaptive_roi_specs = task.get("adaptive_roi_retry_specs") or []
    adaptive_roi_detector_passes = [
        (
            dict(spec),
            make_detector(
                task["dictionary"],
                adaptive_thresh_constant=float(spec["adaptive_thresh_constant"]),
            ),
        )
        for spec in adaptive_roi_specs
    ]
    local_start = int(task["local_start"])
    local_end = int(task["local_end"])
    requested_local_indices = [int(value) for value in task["requested_local_indices"]]
    if (
        requested_local_indices != sorted(set(requested_local_indices))
        or any(index < local_start or index >= local_end for index in requested_local_indices)
    ):
        raise RuntimeError(
            "requested chunk frame indices must be unique, ordered, and inside the decode interval"
        )
    requested_set = set(requested_local_indices)
    requested_row = {value: row for row, value in enumerate(requested_local_indices)}
    front_cache_array = None
    if front_cache_path is not None:
        from direct_openpi_images import openpi_resize_with_pad

        front_cache_path.parent.mkdir(parents=True, exist_ok=True)
        assert front_cache_temp is not None
        front_cache_temp.unlink(missing_ok=True)
        front_cache_array = np.lib.format.open_memmap(
            front_cache_temp,
            mode="w+",
            dtype=np.uint8,
            shape=(len(requested_local_indices), 224, 224, 3),
        )
    written_local_indices: list[int] = []
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            for local_index, frame in _iter_pyav_chunk_frames(
                task["decode_path"],
                int(task["decode_threads"]),
                local_start,
                local_end,
            ):
                actual_height, actual_width = frame.shape[:2]
                if (actual_width, actual_height) != (int(task["width"]), int(task["height"])):
                    raise RuntimeError(
                        f"decoded resolution changed for {task['decode_path']}: "
                        f"{actual_width}x{actual_height} != {task['width']}x{task['height']}"
                    )
                if local_index not in requested_set:
                    continue
                written_local_indices.append(local_index)
                if front_cache_array is not None:
                    front_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    front_cache_array[requested_row[local_index]] = openpi_resize_with_pad(
                        front_rgb,
                        height=224,
                        width=224,
                    )
                corners, ids, _ = detector.detectMarkers(frame)
                centers = compute_centers(corners, ids)
                marker_sides = compute_marker_side_lengths(corners, ids)
                primary_id_values = (
                    [] if ids is None else [int(value) for value in ids.reshape(-1)]
                )
                primary_valid = (
                    primary_id_values.count(marker_a) == 1
                    and primary_id_values.count(marker_b) == 1
                )
                adaptive_retry_valid = False
                adaptive_recovered_role = None
                adaptive_retry_strategy = None
                adaptive_retry_detector_strategy = None
                adaptive_retry_threshold_constant = None
                adaptive_retry_peer_delta_px = None
                adaptive_retry_roi_xyxy_px = None
                adaptive_retry_center_error_px = None
                adaptive_retry_vertical_error_px = None
                adaptive_retry_midpoint_error_px = None
                adaptive_retry_side_ratio = None
                if adaptive_retry_detectors and not primary_valid:
                    selected = None
                    primary_counts = {
                        marker_a: primary_id_values.count(marker_a),
                        marker_b: primary_id_values.count(marker_b),
                    }
                    exactly_one_primary = (
                        sorted(primary_counts.values()) == [0, 1]
                    )
                    if exactly_one_primary and adaptive_roi_detector_passes:
                        roi_fallback = detect_missing_marker_roi_cascade(
                            frame=frame,
                            marker_a=marker_a,
                            marker_b=marker_b,
                            primary_centers=centers,
                            primary_sides=marker_sides,
                            primary_id_values=primary_id_values,
                            detector_passes=adaptive_roi_detector_passes,
                            distance_support_px=task[
                                "adaptive_retry_distance_support_px"
                            ],
                        )
                        if roi_fallback is not None:
                            selected = select_adaptive_dual_candidate(
                                marker_a=marker_a,
                                marker_b=marker_b,
                                primary_centers=centers,
                                primary_sides=marker_sides,
                                primary_id_values=primary_id_values,
                                fallback_detections=[roi_fallback],
                                peer_consistency_px=8.0,
                                distance_support_px=task[
                                    "adaptive_retry_distance_support_px"
                                ],
                            )
                    for threshold_constant, retry_detector in adaptive_retry_detectors:
                        if selected is not None:
                            break
                        fallback_corners, fallback_ids, _ = retry_detector.detectMarkers(frame)
                        fallback_id_values = (
                            []
                            if fallback_ids is None
                            else [int(value) for value in fallback_ids.reshape(-1)]
                        )
                        selected = select_adaptive_dual_candidate(
                            marker_a=marker_a,
                            marker_b=marker_b,
                            primary_centers=centers,
                            primary_sides=marker_sides,
                            primary_id_values=primary_id_values,
                            fallback_detections=[
                                {
                                    "threshold_constant": threshold_constant,
                                    "detector_strategy": (
                                        f"full_frame_raw_c{threshold_constant:g}"
                                    ),
                                    "centers": compute_centers(
                                        fallback_corners, fallback_ids
                                    ),
                                    "marker_sides": compute_marker_side_lengths(
                                        fallback_corners, fallback_ids
                                    ),
                                    "id_values": fallback_id_values,
                                }
                            ],
                            peer_consistency_px=float(
                                task["adaptive_retry_peer_consistency_px"]
                            ),
                            distance_support_px=task[
                                "adaptive_retry_distance_support_px"
                            ],
                        )
                        if selected is not None:
                            break
                    if selected is not None:
                        centers.update(selected["centers"])
                        for marker_id, side_length in selected[
                            "marker_sides"
                        ].items():
                            try:
                                side_length = float(side_length)
                            except (TypeError, ValueError):
                                continue
                            if math.isfinite(side_length) and side_length > 0.0:
                                marker_sides[marker_id] = side_length
                        adaptive_retry_valid = True
                        adaptive_recovered_role = selected[
                            "adaptive_recovered_role"
                        ]
                        adaptive_retry_strategy = selected[
                            "adaptive_retry_strategy"
                        ]
                        adaptive_retry_detector_strategy = selected[
                            "adaptive_retry_detector_strategy"
                        ]
                        adaptive_retry_threshold_constant = selected[
                            "adaptive_retry_threshold_constant"
                        ]
                        adaptive_retry_peer_delta_px = selected[
                            "adaptive_retry_peer_delta_px"
                        ]
                        adaptive_retry_roi_xyxy_px = selected[
                            "adaptive_retry_roi_xyxy_px"
                        ]
                        adaptive_retry_center_error_px = selected[
                            "adaptive_retry_center_error_px"
                        ]
                        adaptive_retry_vertical_error_px = selected[
                            "adaptive_retry_vertical_error_px"
                        ]
                        adaptive_retry_midpoint_error_px = selected[
                            "adaptive_retry_midpoint_error_px"
                        ]
                        adaptive_retry_side_ratio = selected[
                            "adaptive_retry_side_ratio"
                        ]
                # Keep the legacy raw `valid` bit strictly primary-only.
                # Adaptive output is a candidate until the session-level
                # geometry and temporal gates in the ordered merge accept it.
                valid = primary_valid
                marker_side_px = None
                distance_px = None
                if primary_valid or adaptive_retry_valid:
                    a = np.array(centers[marker_a], dtype=np.float64)
                    b = np.array(centers[marker_b], dtype=np.float64)
                    distance_px = float(np.linalg.norm(a - b))
                    side_candidates = [
                        marker_sides[mid]
                        for mid in (marker_a, marker_b)
                        if mid in marker_sides and marker_sides[mid] > 0.0
                    ]
                    marker_side_px = float(np.mean(side_candidates)) if side_candidates else None
                raw = {
                    "frame_idx": int(task["source_global_start"]) + local_index,
                    "a": centers.get(marker_a),
                    "b": centers.get(marker_b),
                    "marker_side_px": marker_side_px,
                    "distance_px": distance_px,
                    "valid": bool(valid),
                    "primary_valid": bool(primary_valid),
                    "adaptive_retry_valid": bool(adaptive_retry_valid),
                    "adaptive_recovered_role": adaptive_recovered_role,
                    "adaptive_retry_strategy": adaptive_retry_strategy,
                    "adaptive_retry_detector_strategy": (
                        adaptive_retry_detector_strategy
                    ),
                    "adaptive_retry_threshold_constant": (
                        adaptive_retry_threshold_constant
                    ),
                    "adaptive_retry_peer_delta_px": adaptive_retry_peer_delta_px,
                    "adaptive_retry_roi_xyxy_px": adaptive_retry_roi_xyxy_px,
                    "adaptive_retry_center_error_px": (
                        adaptive_retry_center_error_px
                    ),
                    "adaptive_retry_vertical_error_px": (
                        adaptive_retry_vertical_error_px
                    ),
                    "adaptive_retry_midpoint_error_px": (
                        adaptive_retry_midpoint_error_px
                    ),
                    "adaptive_retry_side_ratio": adaptive_retry_side_ratio,
                }
                if not validate_raw_detection_row(
                    raw, task.get("raw_row_schema")
                ):
                    raise RuntimeError(
                        "detector produced a row that violates the committed raw schema: "
                        f"schema={task.get('raw_row_schema')} frame_idx={raw['frame_idx']}"
                    )
                f.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
                f.write("\n")
            if written_local_indices != requested_local_indices:
                raise RuntimeError(
                    "decoded chunk did not produce every requested frame exactly once: "
                    f"expected_count={len(requested_local_indices)}, "
                    f"actual_count={len(written_local_indices)}"
                )
            f.flush()
            os.fsync(f.fileno())
        if front_cache_array is not None:
            front_cache_array.flush()
            del front_cache_array
            front_cache_array = None
            assert front_cache_temp is not None and front_cache_path is not None
            descriptor = os.open(front_cache_temp, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(front_cache_temp, front_cache_path)
        os.replace(temp_path, output_path)
        fsync_directory(output_path.parent)
        if front_cache_path is not None and front_cache_path.parent != output_path.parent:
            fsync_directory(front_cache_path.parent)
    finally:
        if front_cache_array is not None:
            del front_cache_array
        temp_path.unlink(missing_ok=True)
        if front_cache_temp is not None:
            front_cache_temp.unlink(missing_ok=True)

    front_cache_record = None
    if front_cache_path is not None:
        front_cache_record = {
            "schema": FRONT_CACHE_SCHEMA,
            "path": str(front_cache_path.resolve()),
            "shape": [len(requested_local_indices), 224, 224, 3],
            "dtype": "uint8",
            "size_bytes": front_cache_path.stat().st_size,
            "sha256": sha256_file(front_cache_path),
            "recipe": FRONT_CACHE_RECIPE,
        }

    manifest = {
        "schema": CHUNK_SCHEMA,
        "raw_row_schema": task.get("raw_row_schema"),
        "task_fingerprint": task["task_fingerprint"],
        "detection_fingerprint": task["detection_fingerprint"],
        "chunk_index": int(task["chunk_index"]),
        "source_identity": task["source_identity"],
        "local_start": local_start,
        "local_end": local_end,
        "global_start": int(task["global_start"]),
        "global_end": int(task["global_end"]),
        "segment_index": task.get("segment_index"),
        "requested_global_indices": task["requested_global_indices"],
        "keyframe_aligned": bool(task["keyframe_aligned"]),
        "row_count": len(requested_local_indices),
        "decoded_frame_count": local_end - local_start,
        "raw_path": str(output_path),
        "raw_sha256": sha256_file(output_path),
        "front_openpi_cache": front_cache_record,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def detect_chunk_with_retries(task: dict, retries: int) -> dict:
    """Retry a whole atomic chunk; never publish partial or substituted frames."""

    attempts = int(retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            return detect_chunk(task)
        except Exception as exc:
            if attempt >= attempts:
                raise
            print(
                f"[retry] chunk={task['chunk_index']} attempt={attempt}/{attempts} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(min(2.0, 0.25 * attempt))
    raise AssertionError("unreachable chunk retry state")


def adaptive_detection_runtime_identity() -> dict:
    """Hash only functions that can change V4 decoded detection semantics.

    This supplements the human-readable revision.  Merge, fill, reporting and
    quality-policy edits intentionally do not invalidate expensive native-video
    chunks, while any future detector/ROI/decode edit does so automatically.
    """

    import inspect

    functions = (
        _frame_index_from_pts,
        _iter_pyav_chunk_frames,
        make_detector,
        compute_centers,
        compute_marker_side_lengths,
        adaptive_retry_threshold_constants,
        adaptive_roi_retry_specs,
        reflected_missing_marker_roi,
        _prepare_adaptive_roi_image,
        _map_roi_detection_to_frame,
        detect_missing_marker_roi_cascade,
        select_adaptive_dual_candidate,
        validate_raw_detection_row,
        detect_chunk,
    )
    records = [
        {"name": function.__name__, "source": inspect.getsource(function)}
        for function in functions
    ]
    return {
        "schema": "instaumi.gripper_detection_runtime_identity.v1",
        "functions": [record["name"] for record in records],
        "sha256": object_fingerprint(records),
    }


def validate_completed_chunk(
    task: dict,
    verify_sha256: bool = True,
    *,
    allow_ignored_front_cache: bool = False,
) -> dict | None:
    import numpy as np

    raw_path = Path(task["raw_path"])
    manifest_path = Path(task["chunk_manifest_path"])
    if not raw_path.is_file() or not manifest_path.is_file():
        return None
    try:
        manifest = load_manifest(manifest_path)
        if manifest.get("schema") != CHUNK_SCHEMA or manifest.get("task_fingerprint") != task["task_fingerprint"]:
            return None
        expected_row_schema = task.get("raw_row_schema")
        if manifest.get("raw_row_schema") != expected_row_schema:
            return None
        requested_indices = [int(value) for value in task["requested_global_indices"]]
        if (
            manifest.get("detection_fingerprint") != task["detection_fingerprint"]
            or int(manifest.get("chunk_index", -1)) != int(task["chunk_index"])
            or manifest.get("source_identity") != task["source_identity"]
            or int(manifest.get("local_start", -1)) != int(task["local_start"])
            or int(manifest.get("local_end", -1)) != int(task["local_end"])
            or int(manifest.get("global_start", -1)) != int(task["global_start"])
            or int(manifest.get("global_end", -1)) != int(task["global_end"])
            or manifest.get("segment_index") != task.get("segment_index")
            or list(manifest.get("requested_global_indices") or []) != requested_indices
            or bool(manifest.get("keyframe_aligned")) != bool(task["keyframe_aligned"])
        ):
            return None
        if int(manifest.get("row_count", -1)) != len(requested_indices):
            return None
        if verify_sha256 and sha256_file(raw_path) != manifest.get("raw_sha256"):
            return None
        front_cache_path = (
            None
            if task.get("front_cache_path") is None
            else Path(task["front_cache_path"])
        )
        front_cache = manifest.get("front_openpi_cache")
        if front_cache_path is not None:
            if not isinstance(front_cache, dict):
                return None
            expected_shape = [len(requested_indices), 224, 224, 3]
            if (
                front_cache.get("schema") != FRONT_CACHE_SCHEMA
                or front_cache.get("recipe") != FRONT_CACHE_RECIPE
                or Path(str(front_cache.get("path", ""))).expanduser().resolve()
                != front_cache_path.expanduser().resolve()
                or front_cache.get("shape") != expected_shape
                or front_cache.get("dtype") != "uint8"
                or not front_cache_path.is_file()
                or front_cache_path.stat().st_size != int(front_cache.get("size_bytes", -1))
            ):
                return None
            if verify_sha256 and sha256_file(front_cache_path) != front_cache.get("sha256"):
                return None
            try:
                array = np.load(front_cache_path, mmap_mode="r", allow_pickle=False)
                if array.dtype != np.uint8 or list(array.shape) != expected_shape:
                    return None
            except Exception:
                return None
        elif front_cache is not None and not allow_ignored_front_cache:
            return None
        position = 0
        with raw_path.open("r", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if position >= len(requested_indices) or int(row["frame_idx"]) != requested_indices[position]:
                    return None
                if not validate_raw_detection_row(row, expected_row_schema):
                    return None
                position += 1
        if position != len(requested_indices):
            return None
        return manifest
    except Exception:
        return None


def chunk_task_fingerprint(
    task: dict,
    detection_fingerprint: str,
    *,
    front_openpi_cache: bool,
) -> str:
    return object_fingerprint(
        {
            "detection_fingerprint": detection_fingerprint,
            "source_identity": task["source_identity"],
            "local_start": task["local_start"],
            "local_end": task["local_end"],
            "global_start": task["global_start"],
            "global_end": task["global_end"],
            "segment_index": task["segment_index"],
            "requested_global_indices": task["requested_global_indices"],
            "keyframe_aligned": task["keyframe_aligned"],
            "front_openpi_cache": front_openpi_cache,
            "front_openpi_cache_recipe": (
                FRONT_CACHE_RECIPE if front_openpi_cache else None
            ),
        }
    )


def adopt_legacy_front_cache_detection_chunks(
    chunks: list[dict],
    legacy_work_dir: Path,
    legacy_detection_fingerprint: str,
) -> int:
    """Reuse validated raw detections from the former decode fan-out mode.

    The old run additionally wrote a large OpenPI `.npy` cache.  That
    auxiliary artifact has no influence on ArUco JSONL rows, so the new
    panorama-only path validates the old task and raw SHA-256 but deliberately
    does not read the `.npy`. Missing/invalid chunks remain on the new no-cache
    plan and will be decoded normally.
    """

    adopted = 0
    for task in chunks:
        stem = (
            f"chunk_{task['chunk_index']:05d}_"
            f"{task['global_start']:09d}_{task['global_end']:09d}"
        )
        legacy_task = dict(task)
        legacy_task.update(
            {
                "detection_fingerprint": legacy_detection_fingerprint,
                "task_fingerprint": chunk_task_fingerprint(
                    task,
                    legacy_detection_fingerprint,
                    front_openpi_cache=True,
                ),
                "raw_path": str(legacy_work_dir / f"{stem}.jsonl"),
                "chunk_manifest_path": str(legacy_work_dir / f"{stem}.manifest.json"),
                "front_cache_path": None,
            }
        )
        if validate_completed_chunk(
            legacy_task,
            allow_ignored_front_cache=True,
        ) is None:
            continue
        task.update(legacy_task)
        task["ignore_published_front_cache"] = True
        adopted += 1
    return adopted


def iter_chunk_rows(chunks: list[dict]) -> Iterator[dict]:
    previous = -1
    expected_count = 0
    actual_count = 0
    for task in chunks:
        requested_indices = [int(value) for value in task["requested_global_indices"]]
        expected_count += len(requested_indices)
        position = 0
        with Path(task["raw_path"]).open("r", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                actual = int(row["frame_idx"])
                if position >= len(requested_indices) or actual != requested_indices[position]:
                    expected = None if position >= len(requested_indices) else requested_indices[position]
                    raise RuntimeError(f"raw chunk merge index mismatch: expected={expected}, got={actual}")
                if actual <= previous:
                    raise RuntimeError(f"raw chunk merge is not strictly increasing: {previous} -> {actual}")
                position += 1
                actual_count += 1
                previous = actual
                yield row
        if position != len(requested_indices):
            raise RuntimeError(
                f"raw chunk merge row count mismatch in chunk={task['chunk_index']}: "
                f"{position} != {len(requested_indices)}"
            )
    if actual_count != expected_count:
        raise RuntimeError(f"raw chunk merge row count mismatch: {actual_count} != {expected_count}")


def annotate(frame, corners, ids, centers: dict[int, tuple[float, float]], marker_a: int, marker_b: int):
    import cv2
    from cv2 import aruco

    out = aruco.drawDetectedMarkers(frame.copy(), corners, ids) if ids is not None else frame.copy()
    for marker_id, center in centers.items():
        p = (int(center[0]), int(center[1]))
        cv2.circle(out, p, 5, (0, 0, 255), -1)
        cv2.putText(out, f"id={marker_id}", (p[0] + 8, p[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    if marker_a in centers and marker_b in centers:
        p0 = (int(centers[marker_a][0]), int(centers[marker_a][1]))
        p1 = (int(centers[marker_b][0]), int(centers[marker_b][1]))
        cv2.line(out, p0, p1, (0, 255, 255), 3)
    return out


def write_constant_zero(args: argparse.Namespace) -> dict:
    validate_distinct_marker_ids(args)
    episode_root = args.tmp_root.expanduser().resolve() / args.episode
    raw_manifest = (args.raw_manifest or episode_root / "01_raw_extract" / "manifest.json").expanduser().resolve()
    raw_manifest_identity = content_file_identity(raw_manifest)
    raw = load_manifest(raw_manifest)
    timestamps_path = Path(raw["timestamps_csv"]).expanduser().resolve()
    timestamps_identity = content_file_identity(timestamps_path)
    timestamps = read_timestamps(timestamps_path)
    validate_timestamps(timestamps, timestamps_path)
    if args.max_frames:
        timestamps = timestamps[: args.max_frames]

    output_dir = episode_root / "03_gripper_estimation"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "gripper_width.csv"
    csv_temp_path = csv_path.with_name(f".{csv_path.name}.tmp.{os.getpid()}")

    with csv_temp_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = gripper_fieldnames()
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in timestamps:
            writer.writerow(
                {
                    "frame_idx": row["frame_idx"],
                    "timestamp_sec": row["timestamp_sec"],
                    "marker_a": args.marker_a,
                    "marker_b": args.marker_b,
                    "a_x": "",
                    "a_y": "",
                    "b_x": "",
                    "b_y": "",
                    "marker_side_px": "",
                    "px_to_meter_scale": "",
                    "center_distance_cm": "",
                    "center_distance_cm_filled": "0.000000",
                    "center_distance_px": "",
                    "center_distance_px_filled": "0.000000",
                    "center_distance_m": "",
                    "center_distance_m_filled": "0.000000000",
                    "distance_px": "",
                    "distance_px_filled": "0.000000",
                    "distance_m": "",
                    "distance_m_filled": "0.000000000",
                    "valid": 0,
                }
            )
        f.flush()
        os.fsync(f.fileno())
    os.replace(csv_temp_path, csv_path)

    if content_file_identity(raw_manifest) != raw_manifest_identity:
        raise RuntimeError(f"raw manifest changed during constant-zero generation: {raw_manifest}")
    if content_file_identity(timestamps_path) != timestamps_identity:
        raise RuntimeError(f"timestamp CSV changed during constant-zero generation: {timestamps_path}")
    run_fingerprint = object_fingerprint(
        {
            "algorithm": "constant_zero_v2",
            "implementation_file": content_file_identity(Path(__file__)),
            "raw_manifest_identity": raw_manifest_identity,
            "timestamps_identity": timestamps_identity,
            "target_frames": len(timestamps),
            "marker_a": args.marker_a,
            "marker_b": args.marker_b,
        }
    )

    return {
        "video": None,
        "output_csv": str(csv_path),
        "annotated_video": None,
        "processed_frames": len(timestamps),
        "valid_frames": 0,
        "missing_policy": "constant_zero",
        "output_csv_sha256": sha256_file(csv_path),
        "run_fingerprint": run_fingerprint,
        "raw_manifest_identity": raw_manifest_identity,
        "marker_width_m": args.marker_width_m,
        "calibration": {
            "mode": "constant_zero",
            "px_to_meter_scale": fixed_px_to_meter_scale(args),
            "json": None,
        },
        "note": "Tracker/ArUco 暂未使用；按用户要求逐帧夹爪宽度先以 0 占位，保持 frame_idx 对齐。",
    }


def estimate_with_annotations(args: argparse.Namespace) -> dict:
    validate_estimation_mode_contract(args)
    import cv2
    import numpy as np

    episode_root = args.tmp_root.expanduser().resolve() / args.episode
    raw_manifest = args.raw_manifest or episode_root / "01_raw_extract" / "manifest.json"
    raw = load_manifest(raw_manifest)
    video_path = args.video or Path(raw["videos"]["front_fisheye"])
    timestamps = read_timestamps(Path(raw["timestamps_csv"]))

    output_dir = episode_root / "03_gripper_estimation"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "gripper_width.csv"
    annotated_path = output_dir / "gripper_annotated.mp4"
    csv_temp_path = csv_path.with_name(f".{csv_path.name}.tmp.{os.getpid()}")
    annotated_temp_path = annotated_path.with_name(f".{annotated_path.stem}.tmp.{os.getpid()}{annotated_path.suffix}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"无法打开视频: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    writer = None
    if args.write_annotated:
        writer = cv2.VideoWriter(str(annotated_temp_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError(f"无法写入标注视频: {annotated_temp_path}")

    detector = make_detector(args.dictionary)
    gripper_calibration = None
    if args.gripper_calibration_json is not None:
        gripper_calibration = load_gripper_calibration(args.gripper_calibration_json)
    frame_idx = 0
    valid_count = 0
    last_distance = None
    last_distance_m = None
    last_distance_cm = None

    with csv_temp_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = gripper_fieldnames()
        writer_csv = csv.DictWriter(f, fieldnames=fieldnames)
        writer_csv.writeheader()

        while frame_idx < len(timestamps):
            ok, frame = cap.read()
            if not ok:
                break
            corners, ids, _ = detector.detectMarkers(frame)
            centers = compute_centers(corners, ids)
            marker_sides = compute_marker_side_lengths(corners, ids)
            valid = args.marker_a in centers and args.marker_b in centers
            distance = None
            distance_m = None
            distance_cm = None
            marker_side_px = None
            px_to_meter = fixed_px_to_meter_scale(args)
            calibration_mode = "missing"
            if valid:
                a = np.array(centers[args.marker_a], dtype=np.float64)
                b = np.array(centers[args.marker_b], dtype=np.float64)
                distance = float(np.linalg.norm(a - b))
                side_candidates = [
                    marker_sides[mid]
                    for mid in (args.marker_a, args.marker_b)
                    if mid in marker_sides and marker_sides[mid] > 0.0
                ]
                marker_side_px = float(np.mean(side_candidates)) if side_candidates else None
                if gripper_calibration is None and px_to_meter is None and marker_side_px is not None:
                    px_to_meter = float(args.marker_width_m / marker_side_px)
                distance_m, distance_cm, calibration_mode = distance_px_to_width(distance, gripper_calibration, marker_side_px, args)
                last_distance = distance
                last_distance_m = distance_m
                last_distance_cm = distance_cm
                valid_count += 1
            filled = distance
            filled_m = distance_m
            filled_cm = distance_cm
            if filled is None:
                if args.missing_policy == "ffill":
                    filled = last_distance
                    filled_m = last_distance_m
                    filled_cm = last_distance_cm
                elif args.missing_policy == "zero":
                    filled = 0.0
                    filled_m = 0.0
                    filled_cm = 0.0

            row = timestamps[frame_idx]
            writer_csv.writerow(
                {
                    "frame_idx": row["frame_idx"],
                    "timestamp_sec": row["timestamp_sec"],
                    "marker_a": args.marker_a,
                    "marker_b": args.marker_b,
                    "a_x": "" if args.marker_a not in centers else f"{centers[args.marker_a][0]:.3f}",
                    "a_y": "" if args.marker_a not in centers else f"{centers[args.marker_a][1]:.3f}",
                    "b_x": "" if args.marker_b not in centers else f"{centers[args.marker_b][0]:.3f}",
                    "b_y": "" if args.marker_b not in centers else f"{centers[args.marker_b][1]:.3f}",
                    "marker_side_px": fmt(marker_side_px, 6),
                    "px_to_meter_scale": fmt(px_to_meter, 12),
                    "center_distance_cm": fmt(distance_cm, 6),
                    "center_distance_cm_filled": fmt(filled_cm, 6),
                    "center_distance_px": fmt(distance, 6),
                    "center_distance_px_filled": fmt(filled, 6),
                    "center_distance_m": fmt(distance_m, 9),
                    "center_distance_m_filled": fmt(filled_m, 9),
                    "distance_px": fmt(distance, 6),
                    "distance_px_filled": fmt(filled, 6),
                    "distance_m": fmt(distance_m, 9),
                    "distance_m_filled": fmt(filled_m, 9),
                    "valid": int(valid),
                }
            )
            if writer is not None:
                writer.write(annotate(frame, corners, ids, centers, args.marker_a, args.marker_b))
            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
            if frame_idx % args.progress_every == 0:
                print(f"processed {frame_idx}/{len(timestamps)} frames ({calibration_mode})", flush=True)

        f.flush()
        os.fsync(f.fileno())

    cap.release()
    if writer is not None:
        writer.release()
    expected_frames = min(len(timestamps), args.max_frames) if args.max_frames else len(timestamps)
    if frame_idx != expected_frames:
        csv_temp_path.unlink(missing_ok=True)
        annotated_temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"strict decoded frame count mismatch: decoded={frame_idx}, expected={expected_frames}")
    os.replace(csv_temp_path, csv_path)
    if args.write_annotated:
        os.replace(annotated_temp_path, annotated_path)

    return {
        "video": str(video_path),
        "output_csv": str(csv_path),
        "annotated_video": str(annotated_path) if args.write_annotated else None,
        "processed_frames": frame_idx,
        "valid_frames": valid_count,
        "missing_policy": args.missing_policy,
        "marker_width_m": args.marker_width_m,
        "calibration": {
            "mode": "calibration_json"
            if gripper_calibration is not None
            else ("fixed_px_scale" if fixed_px_to_meter_scale(args) is not None else "aruco_marker_width_per_frame"),
            "json": None
            if gripper_calibration is None
            else {
                "path": gripper_calibration["path"],
                "points": [
                    {"px": float(px), "m": float(m), "cm": float(cm), "source": source}
                    for px, m, cm, source in gripper_calibration["points"]
                ],
                "min_px": gripper_calibration["min_px"],
                "max_px": gripper_calibration["max_px"],
                "min_m": gripper_calibration["min_m"],
                "max_m": gripper_calibration["max_m"],
            },
            "px_to_meter_scale": fixed_px_to_meter_scale(args),
            "marker_width_m": args.marker_width_m,
            "calibration_distance_px": args.calibration_distance_px,
            "calibration_distance_m": args.calibration_distance_m,
        },
    }


def calibration_summary(gripper_calibration: dict | None, args: argparse.Namespace) -> dict:
    return {
        "mode": "calibration_json"
        if gripper_calibration is not None
        else ("fixed_px_scale" if fixed_px_to_meter_scale(args) is not None else "aruco_marker_width_per_frame"),
        "json": None
        if gripper_calibration is None
        else {
            "path": gripper_calibration["path"],
            "points": [
                {"px": float(px), "m": float(m), "cm": float(cm), "source": source}
                for px, m, cm, source in gripper_calibration["points"]
            ],
            "min_px": gripper_calibration["min_px"],
            "max_px": gripper_calibration["max_px"],
            "min_m": gripper_calibration["min_m"],
            "max_m": gripper_calibration["max_m"],
        },
        "px_to_meter_scale": fixed_px_to_meter_scale(args),
        "marker_width_m": args.marker_width_m,
        "calibration_distance_px": args.calibration_distance_px,
        "calibration_distance_m": args.calibration_distance_m,
        "gripper_zero_below_m": getattr(args, "gripper_zero_below_m", None),
    }


def validate_final_csv(csv_path: Path, expected_frames: int, expected_sha256: str | None = None) -> bool:
    if not csv_path.is_file():
        return False
    if expected_sha256 is not None and sha256_file(csv_path) != expected_sha256:
        return False
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != gripper_fieldnames():
                return False
            count = 0
            for count, row in enumerate(reader, start=1):
                if int(row["frame_idx"]) != count - 1:
                    return False
            return count == expected_frames
    except Exception:
        return False


def build_front_openpi_cache_summary(chunks: Sequence[dict]) -> dict:
    records = []
    total_frames = 0
    total_bytes = 0
    for task in chunks:
        manifest_path = Path(task["chunk_manifest_path"]).expanduser().resolve()
        manifest = load_manifest(manifest_path)
        artifact = manifest.get("front_openpi_cache")
        if not isinstance(artifact, dict):
            raise RuntimeError(f"front OpenPI cache missing from chunk: {manifest_path}")
        frame_count = int(manifest.get("row_count", -1))
        if frame_count <= 0:
            raise RuntimeError(f"front OpenPI cache chunk is empty: {manifest_path}")
        records.append(
            {
                "chunk_index": int(task["chunk_index"]),
                "chunk_manifest": str(manifest_path),
                "frame_count": frame_count,
                "first_frame_idx": int(task["requested_global_indices"][0]),
                "last_frame_idx": int(task["requested_global_indices"][-1]),
                "array_path": artifact["path"],
                "size_bytes": int(artifact["size_bytes"]),
                "sha256": artifact["sha256"],
            }
        )
        total_frames += frame_count
        total_bytes += int(artifact["size_bytes"])
    return {
        "schema": FRONT_CACHE_SCHEMA,
        "recipe": FRONT_CACHE_RECIPE,
        "storage": "sparse_chunked_numpy_npy_mmap",
        "scope": "timestamp_log_active_frames_only",
        "frame_count": total_frames,
        "size_bytes": total_bytes,
        "chunks": records,
    }


def validate_front_openpi_cache_summary(summary: object) -> bool:
    if not isinstance(summary, dict):
        return False
    if summary.get("schema") != FRONT_CACHE_SCHEMA or summary.get("recipe") != FRONT_CACHE_RECIPE:
        return False
    chunks = summary.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return False
    total_frames = 0
    total_bytes = 0
    try:
        for record in chunks:
            manifest_path = Path(record["chunk_manifest"]).expanduser().resolve()
            array_path = Path(record["array_path"]).expanduser().resolve()
            if not manifest_path.is_file() or not array_path.is_file():
                return False
            size_bytes = int(record["size_bytes"])
            frame_count = int(record["frame_count"])
            if frame_count <= 0 or array_path.stat().st_size != size_bytes:
                return False
            total_frames += frame_count
            total_bytes += size_bytes
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return (
        total_frames == int(summary.get("frame_count", -1))
        and total_bytes == int(summary.get("size_bytes", -1))
    )


def write_merged_csv(
    csv_path: Path,
    timestamps: list[dict[str, str]],
    chunks: list[dict],
    gripper_calibration: dict | None,
    args: argparse.Namespace,
    active_ranges: Sequence[ActiveFrameRange] | None = None,
    calibrated_unix_ms: Sequence[float] | None = None,
    pre_publish_check: Callable[[], None] | None = None,
) -> tuple[int, str, dict]:
    if active_ranges is not None:
        if calibrated_unix_ms is None or len(calibrated_unix_ms) != len(timestamps):
            raise ValueError("sparse merge requires one calibrated Unix timestamp per source frame")
        return write_sparse_merged_csv(
            csv_path,
            timestamps,
            chunks,
            gripper_calibration,
            args,
            active_ranges,
            calibrated_unix_ms,
            pre_publish_check,
        )
    temp_path = csv_path.with_name(f".{csv_path.name}.tmp.{os.getpid()}")
    valid_count = 0
    last_distance = None
    last_distance_m = None
    last_distance_cm = None
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=gripper_fieldnames())
            writer.writeheader()
            for expected, raw_row in enumerate(iter_chunk_rows(chunks)):
                timestamp_row = timestamps[expected]
                valid = bool(raw_row["valid"])
                distance = raw_row["distance_px"] if valid else None
                marker_side_px = raw_row["marker_side_px"] if valid else None
                distance_m = None
                distance_cm = None
                px_to_meter = fixed_px_to_meter_scale(args)
                if valid:
                    if gripper_calibration is None and px_to_meter is None and marker_side_px is not None:
                        px_to_meter = float(args.marker_width_m / marker_side_px)
                    distance_m, distance_cm, _ = distance_px_to_width(
                        distance, gripper_calibration, marker_side_px, args
                    )
                    last_distance = distance
                    last_distance_m = distance_m
                    last_distance_cm = distance_cm
                    valid_count += 1
                filled = distance
                filled_m = distance_m
                filled_cm = distance_cm
                if filled is None:
                    if args.missing_policy == "ffill":
                        filled = last_distance
                        filled_m = last_distance_m
                        filled_cm = last_distance_cm
                    elif args.missing_policy == "zero":
                        filled = 0.0
                        filled_m = 0.0
                        filled_cm = 0.0

                a = raw_row["a"]
                b = raw_row["b"]
                writer.writerow(
                    {
                        "frame_idx": timestamp_row["frame_idx"],
                        "timestamp_sec": timestamp_row["timestamp_sec"],
                        "marker_a": args.marker_a,
                        "marker_b": args.marker_b,
                        "a_x": "" if a is None else f"{a[0]:.3f}",
                        "a_y": "" if a is None else f"{a[1]:.3f}",
                        "b_x": "" if b is None else f"{b[0]:.3f}",
                        "b_y": "" if b is None else f"{b[1]:.3f}",
                        "marker_side_px": fmt(marker_side_px, 6),
                        "px_to_meter_scale": fmt(px_to_meter, 12),
                        "center_distance_cm": fmt(distance_cm, 6),
                        "center_distance_cm_filled": fmt(filled_cm, 6),
                        "center_distance_px": fmt(distance, 6),
                        "center_distance_px_filled": fmt(filled, 6),
                        "center_distance_m": fmt(distance_m, 9),
                        "center_distance_m_filled": fmt(filled_m, 9),
                        "distance_px": fmt(distance, 6),
                        "distance_px_filled": fmt(filled, 6),
                        "distance_m": fmt(distance_m, 9),
                        "distance_m_filled": fmt(filled_m, 9),
                        "valid": int(valid),
                    }
                )
            f.flush()
            os.fsync(f.fileno())
        output_sha256 = sha256_file(temp_path)
        if pre_publish_check is not None:
            pre_publish_check()
        os.replace(temp_path, csv_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return valid_count, output_sha256, {
        "policy": args.missing_policy,
        "scope": "legacy_full_timeline",
        "segments_without_detection": [],
    }


def aruco_adaptive_retry_enabled(args: argparse.Namespace) -> bool:
    """Return the explicit adaptive-retry setting for this caller.

    The production CLI and shell wrapper set this option explicitly.  Older
    programmatic callers predate the retry detector, so absence must retain the
    historical primary-detector-only behavior instead of silently changing
    their output.
    """

    return bool(getattr(args, "aruco_adaptive_retry", False))


def adaptive_dual_promotion_contract(args: argparse.Namespace) -> dict:
    """Describe the fail-closed bridge from retry detections to measurements.

    Adaptive two-marker evidence is deliberately independent of whether the
    more approximate single-marker projection model passes its held-out test.
    """

    return {
        "schema": ADAPTIVE_DUAL_PROMOTION_SCHEMA,
        "enabled": aruco_adaptive_retry_enabled(args),
        "requires_accepted_single_marker_model": False,
        "model_training_evidence": "primary_dual_marker_only",
        "geometry_gate": "independent_robust_primary_dual_geometry_model",
        "continuity_anchor": "primary_dual_marker_only",
        "primary_anchor_policy": "check_available_anchors_only",
        "whole_segment_run_policy": (
            "allowed_with_independent_model_and_internal_continuity"
        ),
        "long_run_diagnostic_ms": float(
            getattr(args, "aruco_adaptive_max_run_duration_ms", 250.0)
        ),
        "failure_policy": "reject_candidate_then_use_segment_local_fill",
    }


def adaptive_dual_promotion_authorized(recovery_report: dict | None) -> bool:
    """Authorize retry promotion only under the exact published contract."""

    report = recovery_report or {}
    contract = report.get("adaptive_dual_promotion_contract") or {}
    geometry = report.get("adaptive_dual_geometry") or {}
    model = geometry.get("model")
    config = geometry.get("config")
    try:
        long_run_diagnostic_ms = float(contract.get("long_run_diagnostic_ms"))
    except (TypeError, ValueError):
        return False
    return bool(
        report.get("schema") == SINGLE_MARKER_RECOVERY_SCHEMA
        and geometry.get("schema") == ADAPTIVE_DUAL_GEOMETRY_SCHEMA
        and geometry.get("status") == "accepted"
        and isinstance(model, dict)
        and {
            "axis_deg",
            "midpoint_px",
            "marker_roi_xyxy_px",
            "inferred_distance_support_px",
            "midpoint_support_px",
        }.issubset(model)
        and isinstance(config, dict)
        and contract.get("schema") == ADAPTIVE_DUAL_PROMOTION_SCHEMA
        and contract.get("enabled") is True
        and contract.get("requires_accepted_single_marker_model") is False
        and contract.get("model_training_evidence") == "primary_dual_marker_only"
        and contract.get("geometry_gate")
        == "independent_robust_primary_dual_geometry_model"
        and contract.get("continuity_anchor") == "primary_dual_marker_only"
        and contract.get("primary_anchor_policy") == "check_available_anchors_only"
        and contract.get("whole_segment_run_policy")
        == "allowed_with_independent_model_and_internal_continuity"
        and contract.get("failure_policy")
        == "reject_candidate_then_use_segment_local_fill"
        and math.isfinite(long_run_diagnostic_ms)
        and long_run_diagnostic_ms > 0.0
    )


def validate_distinct_marker_ids(args: argparse.Namespace) -> None:
    if int(args.marker_a) == int(args.marker_b):
        raise ValueError("--marker-a and --marker-b must identify two distinct ArUco markers")


def validate_estimation_mode_contract(args: argparse.Namespace) -> None:
    """Reject modes whose detector semantics cannot be made byte-consistent."""

    validate_distinct_marker_ids(args)
    if bool(args.write_annotated) and aruco_adaptive_retry_enabled(args):
        raise ValueError(
            "--write-annotated uses the legacy sequential primary detector and cannot "
            "be combined with --aruco-adaptive-retry; pass "
            "--no-aruco-adaptive-retry for debug-only annotated output"
        )


def single_marker_recovery_config(args: argparse.Namespace) -> dict:
    """Return merge-only recovery limits without changing raw detection semantics."""

    return {
        "enabled": bool(getattr(args, "single_marker_recovery", True)),
        "minimum_dual_marker_frames": int(
            getattr(args, "single_marker_min_dual_frames", 1_000)
        ),
        "minimum_dual_marker_segments": int(
            getattr(args, "single_marker_min_dual_segments", 10)
        ),
        "cross_validation_folds": int(
            getattr(args, "single_marker_cv_folds", 0)
        ),
        "maximum_cv_p95_error_m": float(
            getattr(args, "single_marker_max_cv_p95_m", 0.004)
        ),
        "maximum_cv_p99_error_m": float(
            getattr(args, "single_marker_max_cv_p99_m", 0.005)
        ),
        "maximum_cv_error_m": float(
            getattr(args, "single_marker_max_cv_error_m", 0.006)
        ),
        "maximum_cv_absolute_bias_m": float(
            getattr(args, "single_marker_max_cv_bias_m", 0.0015)
        ),
        "minimum_cv_coverage": float(
            getattr(args, "single_marker_min_cv_coverage", 0.99)
        ),
        "minimum_cv_predictions_per_role": int(
            getattr(args, "single_marker_min_cv_predictions_per_role", 1_000)
        ),
        "minimum_cv_episodes_per_role": int(
            getattr(args, "single_marker_min_cv_episodes_per_role", 10)
        ),
        "maximum_episode_p95_error_m": float(
            getattr(args, "single_marker_max_episode_p95_m", 0.006)
        ),
        "maximum_midpoint_episode_p95_px": float(
            getattr(args, "single_marker_max_midpoint_p95_px", 8.0)
        ),
        "maximum_axis_episode_p95_deg": float(
            getattr(args, "single_marker_max_axis_p95_deg", 1.0)
        ),
        "clean_distance_range_px": [
            float(getattr(args, "single_marker_min_distance_px", 100.0)),
            float(getattr(args, "single_marker_max_distance_px", 600.0)),
        ],
        "maximum_axis_deviation_deg": float(
            getattr(args, "single_marker_max_axis_deviation_deg", 5.0)
        ),
        "marker_roi_margin_px": float(
            getattr(args, "single_marker_roi_margin_px", 5.0)
        ),
        "midpoint_translation_min_gate_px": float(
            getattr(args, "single_marker_midpoint_min_gate_px", 25.0)
        ),
        "continuity_base_tolerance_m": float(
            getattr(args, "single_marker_continuity_base_m", 0.005)
        ),
        "continuity_max_slew_m_per_sec": float(
            getattr(args, "single_marker_continuity_slew_mps", 0.1)
        ),
        "continuity_anchor_horizon_ms": float(
            getattr(args, "single_marker_anchor_horizon_ms", 250.0)
        ),
        "continuity_max_timeline_step_ms": float(
            getattr(args, "single_marker_max_timeline_step_ms", 50.0)
        ),
        "geometry": (
            "one wrist-rigid arm-specific center and jaw axis fitted from primary "
            "dual-marker episodes; single marker is projected onto the jaw axis "
            "and doubled; the production center is fixed for the whole capture"
        ),
        "literal_image_optical_center_requires_held_out_validation": True,
        "requires_calibration_json": True,
    }


def _finite_marker_center(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        center = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return center if all(math.isfinite(item) for item in center) else None


def _single_marker_error_statistics(errors_m: Sequence[float]) -> dict:
    import numpy as np

    values = np.asarray(list(errors_m), dtype=np.float64)
    if values.size == 0:
        return {
            "count": 0,
            "mae_m": None,
            "bias_m": None,
            "p95_abs_error_m": None,
            "p99_abs_error_m": None,
            "max_abs_error_m": None,
        }
    return {
        "count": int(values.size),
        "mae_m": float(np.abs(values).mean()),
        "bias_m": float(values.mean()),
        "p95_abs_error_m": float(np.percentile(np.abs(values), 95.0)),
        "p99_abs_error_m": float(np.percentile(np.abs(values), 99.0)),
        "max_abs_error_m": float(np.abs(values).max()),
    }


def evaluate_single_marker_role_contract(
    role: str,
    metrics: dict,
    per_episode: Sequence[dict],
    config: dict,
) -> dict:
    """Evaluate held-out physical-width accuracy for one marker role only.

    Validation coverage describes how much of the observed marker ROI lies
    inside the production support envelope.  It is intentionally diagnostic:
    an actual fallback frame still has to pass that ROI and calibrated-distance
    envelope in :func:`raw_measurement`.  Accuracy, bias, evidence count, and
    per-episode held-out error remain hard gates.
    """

    if role not in {"a", "b"}:
        raise ValueError(f"unknown single-marker role: {role!r}")
    rejection_reasons: list[str] = []
    warnings: list[str] = []
    predictions = int(metrics.get("accepted_predictions") or metrics.get("count") or 0)
    validated_episodes = sum(int(item.get("count") or 0) > 0 for item in per_episode)
    if predictions < int(config["minimum_cv_predictions_per_role"]):
        rejection_reasons.append(
            f"held_out_predictions={predictions} < "
            f"{config['minimum_cv_predictions_per_role']}"
        )
    if validated_episodes < int(config["minimum_cv_episodes_per_role"]):
        rejection_reasons.append(
            f"held_out_episodes={validated_episodes} < "
            f"{config['minimum_cv_episodes_per_role']}"
        )
    for metric, limit_key in (
        ("p95_abs_error_m", "maximum_cv_p95_error_m"),
        ("p99_abs_error_m", "maximum_cv_p99_error_m"),
        ("max_abs_error_m", "maximum_cv_error_m"),
    ):
        value = metrics.get(metric)
        if value is None or not math.isfinite(float(value)) or float(value) > float(config[limit_key]):
            rejection_reasons.append(
                f"{metric}={value} exceeds {limit_key}={config[limit_key]}"
            )
    bias = metrics.get("bias_m")
    if (
        bias is None
        or not math.isfinite(float(bias))
        or abs(float(bias)) > float(config["maximum_cv_absolute_bias_m"])
    ):
        rejection_reasons.append(
            f"absolute_bias_m={bias} exceeds "
            f"{config['maximum_cv_absolute_bias_m']}"
        )
    worst_episode_p95 = max(
        (
            float(item["p95_abs_error_m"])
            for item in per_episode
            if item.get("p95_abs_error_m") is not None
            and math.isfinite(float(item["p95_abs_error_m"]))
        ),
        default=math.inf,
    )
    if worst_episode_p95 > float(config["maximum_episode_p95_error_m"]):
        rejection_reasons.append(
            f"worst_episode_p95_error_m={worst_episode_p95} > "
            f"{config['maximum_episode_p95_error_m']}"
        )
    coverage = metrics.get("coverage")
    if (
        coverage is None
        or not math.isfinite(float(coverage))
        or float(coverage) < float(config["minimum_cv_coverage"])
    ):
        warnings.append(
            f"held_out_roi_coverage={coverage} < diagnostic "
            f"{config['minimum_cv_coverage']}; out-of-domain frames remain fail-closed"
        )
    return {
        "role": role,
        "status": "accepted" if not rejection_reasons else "rejected",
        "rejection_reasons": rejection_reasons,
        "warnings": warnings,
        "held_out_predictions": predictions,
        "held_out_episodes": validated_episodes,
        "worst_episode_p95_error_m": (
            None if not math.isfinite(worst_episode_p95) else worst_episode_p95
        ),
        "coverage_is_diagnostic_not_accuracy_gate": True,
        "runtime_domain_gates": [
            "marker_role_specific_roi",
            "held_out_inferred_distance_support",
            "calibration_distance_support",
            "segment_local_temporal_width_continuity",
        ],
    }


def accepted_single_marker_roles(report: dict | None) -> tuple[str, ...]:
    """Return authorized roles, retaining compatibility with accepted v1 fixtures."""

    if not isinstance(report, dict):
        return ()
    status = report.get("status")
    configured = report.get("accepted_roles")
    if configured is None:
        # Old in-memory/tests and old accepted v1 reports were all-or-nothing.
        return ("a", "b") if status == "accepted" else ()
    if status not in {"accepted", "partially_accepted"}:
        return ()
    if not isinstance(configured, list):
        return ()
    selected = tuple(role for role in ("a", "b") if role in configured)
    if status == "accepted" and selected != ("a", "b"):
        return ()
    if status == "partially_accepted" and len(selected) != 1:
        return ()
    return selected


def _wrap_angle_radians(value: float) -> float:
    return (float(value) + math.pi) % (2.0 * math.pi) - math.pi


def _circular_median_radians(values: Sequence[float]) -> float:
    import numpy as np

    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        raise ValueError("circular median requires at least one angle")
    reference = math.atan2(float(np.median(np.sin(array))), float(np.median(np.cos(array))))
    unwrapped = np.asarray(
        [reference + _wrap_angle_radians(value - reference) for value in array],
        dtype=np.float64,
    )
    return _wrap_angle_radians(float(np.median(unwrapped)))


def adaptive_dual_geometry_config(args: argparse.Namespace) -> dict:
    """Independent gates for promoting an adaptive two-marker detection."""

    return {
        "minimum_primary_frames": int(
            getattr(args, "aruco_adaptive_min_primary_frames", 20)
        ),
        "minimum_primary_segments": int(
            getattr(args, "aruco_adaptive_min_primary_segments", 1)
        ),
        "maximum_axis_deviation_deg": float(
            getattr(args, "aruco_adaptive_max_axis_deviation_deg", 8.0)
        ),
        "midpoint_translation_min_gate_px": float(
            getattr(args, "aruco_adaptive_midpoint_min_gate_px", 30.0)
        ),
        "marker_roi_margin_px": float(
            getattr(args, "aruco_adaptive_marker_roi_margin_px", 10.0)
        ),
        "continuity_base_tolerance_m": float(
            getattr(args, "single_marker_continuity_base_m", 0.005)
        ),
        "continuity_max_slew_m_per_sec": float(
            getattr(args, "single_marker_continuity_slew_mps", 0.1)
        ),
        "continuity_anchor_horizon_ms": float(
            getattr(args, "single_marker_anchor_horizon_ms", 250.0)
        ),
        "continuity_max_timeline_step_ms": float(
            getattr(args, "single_marker_max_timeline_step_ms", 50.0)
        ),
    }


def build_adaptive_dual_geometry(
    raw_rows: dict[int, dict],
    active_ranges: Sequence[ActiveFrameRange],
    gripper_calibration: dict | None,
    args: argparse.Namespace,
) -> dict:
    """Fit a robust geometry envelope using primary two-marker frames only.

    This model intentionally has no single-marker prediction or held-out error
    requirement.  Its sole purpose is to reject an adaptive threshold result
    that is geometrically incompatible with trustworthy primary two-marker
    observations from the same camera/session.
    """

    import numpy as np

    config = adaptive_dual_geometry_config(args)
    report: dict = {
        "schema": ADAPTIVE_DUAL_GEOMETRY_SCHEMA,
        "status": "rejected",
        "config": config,
        "rejection_reasons": [],
        "training_evidence": {},
    }
    if gripper_calibration is None:
        report["rejection_reasons"].append(
            "calibration_json_is_required_for_adaptive_dual_promotion"
        )
        return report

    frame_to_segment = {
        int(frame_idx): int(active_range.segment_index)
        for active_range in active_ranges
        for frame_idx in active_range.frame_indices
    }
    configured_low = float(
        getattr(args, "aruco_adaptive_min_distance_px", 100.0)
    )
    configured_high = float(
        getattr(args, "aruco_adaptive_max_distance_px", 600.0)
    )
    support_low = max(configured_low, float(gripper_calibration["min_px"]))
    support_high = min(configured_high, float(gripper_calibration["max_px"]))
    if not support_low < support_high:
        report["rejection_reasons"].append(
            "adaptive distance support does not overlap calibration support"
        )
        return report

    samples: list[dict] = []
    for frame_idx, row in raw_rows.items():
        if not bool(row.get("primary_valid", row.get("valid", False))):
            continue
        center_a = _finite_marker_center(row.get("a"))
        center_b = _finite_marker_center(row.get("b"))
        segment_index = frame_to_segment.get(int(frame_idx))
        if center_a is None or center_b is None or segment_index is None:
            continue
        distance_px = math.hypot(
            center_b[0] - center_a[0], center_b[1] - center_a[1]
        )
        if not support_low <= distance_px <= support_high:
            continue
        marker_side_px = row.get("marker_side_px")
        try:
            marker_side_px = float(marker_side_px)
        except (TypeError, ValueError):
            marker_side_px = math.nan
        samples.append(
            {
                "frame_idx": int(frame_idx),
                "segment_index": segment_index,
                "a": center_a,
                "b": center_b,
                "distance_px": distance_px,
                "axis_rad": math.atan2(
                    center_b[1] - center_a[1], center_b[0] - center_a[0]
                ),
                "midpoint": (
                    (center_a[0] + center_b[0]) * 0.5,
                    (center_a[1] + center_b[1]) * 0.5,
                ),
                "marker_side_px": marker_side_px,
            }
        )

    segment_count = len({item["segment_index"] for item in samples})
    report["training_evidence"] = {
        "primary_dual_frames_in_calibration_support": len(samples),
        "primary_dual_segments": segment_count,
        "model_source": "primary_dual_marker_only",
    }
    if len(samples) < config["minimum_primary_frames"]:
        report["rejection_reasons"].append(
            f"primary_dual_frames={len(samples)} < {config['minimum_primary_frames']}"
        )
    if segment_count < config["minimum_primary_segments"]:
        report["rejection_reasons"].append(
            f"primary_dual_segments={segment_count} < {config['minimum_primary_segments']}"
        )
    if report["rejection_reasons"]:
        return report

    axis_rad = _circular_median_radians([item["axis_rad"] for item in samples])
    midpoints = np.asarray([item["midpoint"] for item in samples], dtype=np.float64)
    midpoint = np.median(midpoints, axis=0)
    radii = np.linalg.norm(midpoints - midpoint, axis=1)
    radius_median = float(np.median(radii))
    radius_mad = float(np.median(np.abs(radii - radius_median)))
    midpoint_support_px = max(
        config["midpoint_translation_min_gate_px"],
        radius_median + 8.0 * 1.4826 * radius_mad,
    )
    maximum_axis_deviation_rad = math.radians(
        config["maximum_axis_deviation_deg"]
    )
    clean = [
        item
        for item, radius in zip(samples, radii, strict=True)
        if float(radius) <= midpoint_support_px
        and abs(_wrap_angle_radians(item["axis_rad"] - axis_rad))
        <= maximum_axis_deviation_rad
    ]
    if len(clean) < config["minimum_primary_frames"]:
        report["rejection_reasons"].append(
            "insufficient primary two-marker frames after robust geometry filtering: "
            f"clean={len(clean)}"
        )
        return report

    marker_roi: dict[str, list[float]] = {}
    margin = config["marker_roi_margin_px"]
    for role in ("a", "b"):
        centers = np.asarray([item[role] for item in clean], dtype=np.float64)
        x0, x1 = np.percentile(centers[:, 0], [0.1, 99.9])
        y0, y1 = np.percentile(centers[:, 1], [0.1, 99.9])
        marker_roi[role] = [
            float(x0 - margin),
            float(y0 - margin),
            float(x1 + margin),
            float(y1 + margin),
        ]
    finite_sides = np.asarray(
        [
            item["marker_side_px"]
            for item in clean
            if math.isfinite(item["marker_side_px"])
            and item["marker_side_px"] > 0.0
        ],
        dtype=np.float64,
    )
    marker_side_support = None
    if finite_sides.size:
        side_low, side_high = np.percentile(finite_sides, [0.1, 99.9])
        # Adaptive binarization can shift detected corners slightly. A broad
        # multiplicative envelope catches unrelated quads without punishing it.
        marker_side_support = [
            max(1e-6, float(side_low) * 0.65),
            float(side_high) * 1.35,
        ]
    report["training_evidence"].update(
        {
            "clean_primary_dual_frames": len(clean),
            "gross_geometry_rejected_frames": len(samples) - len(clean),
        }
    )
    report["model"] = {
        "axis_deg": math.degrees(axis_rad),
        "midpoint_px": [float(midpoint[0]), float(midpoint[1])],
        "marker_roi_xyxy_px": marker_roi,
        "inferred_distance_support_px": [support_low, support_high],
        "midpoint_support_px": midpoint_support_px,
        "marker_side_support_px": marker_side_support,
    }
    report["status"] = "accepted"
    return report


def build_single_marker_recovery(
    raw_rows: dict[int, dict],
    active_ranges: Sequence[ActiveFrameRange],
    gripper_calibration: dict | None,
    args: argparse.Namespace,
) -> dict:
    """Fit a wrist-rigid axis model and validate it with held-out episodes."""

    import numpy as np

    config = single_marker_recovery_config(args)
    report: dict = {
        "schema": SINGLE_MARKER_RECOVERY_SCHEMA,
        "status": "disabled",
        "config": config,
        "adaptive_dual_promotion_contract": adaptive_dual_promotion_contract(args),
        "adaptive_dual_geometry": build_adaptive_dual_geometry(
            raw_rows, active_ranges, gripper_calibration, args
        ),
        "rejection_reasons": [],
        "candidate_counts": {
            "dual_marker": 0,
            "adaptive_dual_candidate": 0,
            "single_marker_a": 0,
            "single_marker_b": 0,
            "neither_marker": 0,
        },
    }
    if not config["enabled"]:
        report["rejection_reasons"].append("disabled_by_configuration")
        return report
    if gripper_calibration is None:
        report["status"] = "rejected"
        report["rejection_reasons"].append(
            "calibration_json_is_required_for_single_marker_physical_width"
        )
        return report
    segment_indices = [int(item.segment_index) for item in active_ranges]
    if len(segment_indices) != len(set(segment_indices)):
        report["status"] = "rejected"
        report["rejection_reasons"].append(
            "timestamp_log_segment_indices_must_be_unique_for_episode_held_out_validation"
        )
        return report

    frame_to_segment = {
        int(frame_idx): int(active_range.segment_index)
        for active_range in active_ranges
        for frame_idx in active_range.frame_indices
    }
    dual_by_segment: dict[int, list[dict]] = {}
    for frame_idx, row in raw_rows.items():
        a = _finite_marker_center(row.get("a"))
        b = _finite_marker_center(row.get("b"))
        adaptive_recovered_role = row.get("adaptive_recovered_role")
        primary_a = None if adaptive_recovered_role in {"a", "both"} else a
        primary_b = None if adaptive_recovered_role in {"b", "both"} else b
        if row.get("adaptive_retry_valid"):
            report["candidate_counts"]["adaptive_dual_candidate"] += 1
        if primary_a is not None and primary_b is not None:
            try:
                distance_px = float(row.get("distance_px"))
            except (TypeError, ValueError):
                distance_px = math.nan
            segment_index = frame_to_segment.get(int(frame_idx))
            if math.isfinite(distance_px) and segment_index is not None:
                dual_by_segment.setdefault(segment_index, []).append(
                    {
                        "row": row,
                        "a": primary_a,
                        "b": primary_b,
                        "distance_px": distance_px,
                        "axis_rad": math.atan2(
                            primary_b[1] - primary_a[1],
                            primary_b[0] - primary_a[0],
                        ),
                        "midpoint": (
                            (primary_a[0] + primary_b[0]) * 0.5,
                            (primary_a[1] + primary_b[1]) * 0.5,
                        ),
                    }
                )
                report["candidate_counts"]["dual_marker"] += 1
        elif primary_a is not None:
            report["candidate_counts"]["single_marker_a"] += 1
        elif primary_b is not None:
            report["candidate_counts"]["single_marker_b"] += 1
        else:
            report["candidate_counts"]["neither_marker"] += 1

    dual_segments = sorted(dual_by_segment)
    dual_frame_count = sum(len(rows) for rows in dual_by_segment.values())
    report["training_evidence"] = {
        "dual_marker_frames": dual_frame_count,
        "dual_marker_segments": len(dual_segments),
        "segment_indices": dual_segments,
    }
    if dual_frame_count < config["minimum_dual_marker_frames"]:
        report["rejection_reasons"].append(
            f"dual_marker_frames={dual_frame_count} < {config['minimum_dual_marker_frames']}"
        )
    if len(dual_segments) < config["minimum_dual_marker_segments"]:
        report["rejection_reasons"].append(
            f"dual_marker_segments={len(dual_segments)} < {config['minimum_dual_marker_segments']}"
        )
    if report["rejection_reasons"]:
        report["status"] = "rejected"
        return report

    configured_minimum_px, configured_maximum_px = config["clean_distance_range_px"]
    minimum_distance_px = max(
        float(configured_minimum_px), float(gripper_calibration["min_px"])
    )
    maximum_distance_px = min(
        float(configured_maximum_px), float(gripper_calibration["max_px"])
    )
    if minimum_distance_px >= maximum_distance_px:
        report["status"] = "rejected"
        report["rejection_reasons"].append(
            "configured distance support does not overlap the calibration support: "
            f"configured={[configured_minimum_px, configured_maximum_px]} "
            f"calibration={[gripper_calibration['min_px'], gripper_calibration['max_px']]}"
        )
        return report
    maximum_axis_deviation_rad = math.radians(config["maximum_axis_deviation_deg"])
    episode_summaries: dict[int, dict] = {}
    for segment_index, rows in dual_by_segment.items():
        distance_clean = [
            item
            for item in rows
            if minimum_distance_px <= item["distance_px"] <= maximum_distance_px
        ]
        if not distance_clean:
            continue
        episode_axis = _circular_median_radians(
            [item["axis_rad"] for item in distance_clean]
        )
        angle_clean = [
            item
            for item in distance_clean
            if abs(_wrap_angle_radians(item["axis_rad"] - episode_axis))
            <= maximum_axis_deviation_rad
        ]
        if not angle_clean:
            continue
        midpoint_samples = np.asarray(
            [item["midpoint"] for item in angle_clean], dtype=np.float64
        )
        provisional_midpoint = np.median(midpoint_samples, axis=0)
        midpoint_radius = np.linalg.norm(midpoint_samples - provisional_midpoint, axis=1)
        radius_median = float(np.median(midpoint_radius))
        radius_mad = float(np.median(np.abs(midpoint_radius - radius_median)))
        translation_limit_px = max(
            float(config["midpoint_translation_min_gate_px"]),
            radius_median + 6.0 * 1.4826 * radius_mad,
        )
        clean = [
            item
            for item, radius in zip(angle_clean, midpoint_radius)
            if float(radius) <= translation_limit_px
        ]
        if not clean:
            continue
        midpoint = np.median(
            np.asarray([item["midpoint"] for item in clean], dtype=np.float64),
            axis=0,
        )
        episode_summaries[segment_index] = {
            "axis_rad": episode_axis,
            "midpoint": (float(midpoint[0]), float(midpoint[1])),
            "clean_frames": len(clean),
            "translation_gate_px": translation_limit_px,
            "clean_rows": clean,
        }

    usable_segments = sorted(episode_summaries)
    if len(usable_segments) < config["minimum_dual_marker_segments"]:
        report["status"] = "rejected"
        report["rejection_reasons"].append(
            "insufficient episodes after gross dual-marker geometry filtering"
        )
        return report

    def fit_episode_equal_model(segment_indices: Sequence[int]) -> tuple[float, tuple[float, float]]:
        axis = _circular_median_radians(
            [episode_summaries[index]["axis_rad"] for index in segment_indices]
        )
        midpoint = np.median(
            np.asarray(
                [episode_summaries[index]["midpoint"] for index in segment_indices],
                dtype=np.float64,
            ),
            axis=0,
        )
        return axis, (float(midpoint[0]), float(midpoint[1]))

    def inferred_distance_px(
        role: str,
        center: tuple[float, float],
        axis_rad: float,
        midpoint: tuple[float, float],
    ) -> float:
        ux = math.cos(axis_rad)
        uy = math.sin(axis_rad)
        center_axis = center[0] * ux + center[1] * uy
        midpoint_axis = midpoint[0] * ux + midpoint[1] * uy
        return 2.0 * (
            midpoint_axis - center_axis if role == "a" else center_axis - midpoint_axis
        )

    requested_folds = int(config["cross_validation_folds"])
    folds = len(usable_segments) if requested_folds == 0 else min(requested_folds, len(usable_segments))
    if folds < 2:
        report["status"] = "rejected"
        report["rejection_reasons"].append("at_least_two_episode_folds_are_required")
        return report
    segment_to_fold = {
        segment_index: position % folds
        for position, segment_index in enumerate(usable_segments)
    }

    def episode_equal_marker_roi(
        segment_indices: Sequence[int], role: str, margin_px: float
    ) -> tuple[float, float, float, float]:
        envelopes = []
        for segment_index in segment_indices:
            rows = episode_summaries[segment_index]["clean_rows"]
            x_values = np.asarray([item[role][0] for item in rows])
            y_values = np.asarray([item[role][1] for item in rows])
            x0, x1 = np.percentile(x_values, [0.1, 99.9])
            y0, y1 = np.percentile(y_values, [0.1, 99.9])
            envelopes.append((x0, y0, x1, y1))
        envelope_array = np.asarray(envelopes, dtype=np.float64)
        return (
            float(np.percentile(envelope_array[:, 0], 1.0) - margin_px),
            float(np.percentile(envelope_array[:, 1], 1.0) - margin_px),
            float(np.percentile(envelope_array[:, 2], 99.0) + margin_px),
            float(np.percentile(envelope_array[:, 3], 99.0) + margin_px),
        )

    errors: dict[str, list[float]] = {"a": [], "b": []}
    episode_role_errors: dict[int, dict[str, list[float]]] = {
        index: {"a": [], "b": []} for index in usable_segments
    }
    validation_possible_per_role = sum(
        len(episode_summaries[index]["clean_rows"])
        for index in usable_segments
    )
    validation_possible = 2 * validation_possible_per_role
    validation_accepted_by_role = {"a": 0, "b": 0}
    fold_models: list[dict] = []
    for fold in range(folds):
        training_segments = [
            index for index in usable_segments if segment_to_fold[index] != fold
        ]
        validation_segments = [
            index for index in usable_segments if segment_to_fold[index] == fold
        ]
        if not training_segments or not validation_segments:
            report["status"] = "rejected"
            report["rejection_reasons"].append(f"empty_cross_validation_fold={fold}")
            return report
        axis_rad, midpoint = fit_episode_equal_model(training_segments)
        roi_margin = float(config["marker_roi_margin_px"])
        fold_roi: dict[str, tuple[float, float, float, float]] = {}
        for role in ("a", "b"):
            fold_roi[role] = episode_equal_marker_roi(
                training_segments, role, roi_margin
            )
        fold_models.append(
            {
                "fold": fold,
                "held_segment_count": len(validation_segments),
                "axis_deg": math.degrees(axis_rad),
                "midpoint_px": list(midpoint),
            }
        )
        for segment_index in validation_segments:
            for item in episode_summaries[segment_index]["clean_rows"]:
                reference_m, _reference_cm, _mode = distance_px_to_width(
                    item["distance_px"],
                    gripper_calibration,
                    item["row"].get("marker_side_px"),
                    args,
                    apply_deadzone=False,
                )
                if reference_m is None or not math.isfinite(reference_m):
                    continue
                for role in ("a", "b"):
                    roi_x0, roi_y0, roi_x1, roi_y1 = fold_roi[role]
                    if not (
                        roi_x0 <= item[role][0] <= roi_x1
                        and roi_y0 <= item[role][1] <= roi_y1
                    ):
                        continue
                    predicted_px = inferred_distance_px(
                        role, item[role], axis_rad, midpoint
                    )
                    if not minimum_distance_px <= predicted_px <= maximum_distance_px:
                        continue
                    predicted_m, _predicted_cm, _mode = distance_px_to_width(
                        predicted_px,
                        gripper_calibration,
                        None,
                        args,
                        apply_deadzone=False,
                    )
                    if predicted_m is None or not math.isfinite(predicted_m):
                        continue
                    signed_error = float(predicted_m) - float(reference_m)
                    errors[role].append(signed_error)
                    episode_role_errors[segment_index][role].append(signed_error)
                    validation_accepted_by_role[role] += 1

    per_episode_by_role: dict[str, list[dict]] = {"a": [], "b": []}
    per_episode = []
    for segment_index in usable_segments:
        combined_errors: list[float] = []
        for role in ("a", "b"):
            role_errors = episode_role_errors[segment_index][role]
            combined_errors.extend(role_errors)
            per_episode_by_role[role].append(
                {
                    "segment_index": segment_index,
                    **_single_marker_error_statistics(role_errors),
                }
            )
        per_episode.append(
            {
                "segment_index": segment_index,
                **_single_marker_error_statistics(combined_errors),
            }
        )
    validation = {
        role: _single_marker_error_statistics(role_errors)
        for role, role_errors in errors.items()
    }
    for role in ("a", "b"):
        validation[role]["coverage"] = (
            float(validation_accepted_by_role[role] / validation_possible_per_role)
            if validation_possible_per_role
            else 0.0
        )
        validation[role]["accepted_predictions"] = validation_accepted_by_role[role]
        validation[role]["possible_predictions"] = validation_possible_per_role
    validation["combined"] = _single_marker_error_statistics(errors["a"] + errors["b"])
    validation["mode"] = "leave_one_episode_out" if folds == len(usable_segments) else "episode_group_kfold"
    validation["fold_count"] = folds
    validation["coverage"] = (
        float(sum(validation_accepted_by_role.values()) / validation_possible)
        if validation_possible
        else 0.0
    )
    validation["accepted_predictions"] = sum(validation_accepted_by_role.values())
    validation["possible_predictions"] = validation_possible
    validation["fold_models"] = fold_models
    validation["per_episode"] = per_episode
    validation["per_episode_by_role"] = per_episode_by_role
    report["cross_validation"] = validation

    axis_rad, midpoint = fit_episode_equal_model(usable_segments)
    episode_midpoints = np.asarray(
        [episode_summaries[index]["midpoint"] for index in usable_segments],
        dtype=np.float64,
    )
    midpoint_deviation = np.linalg.norm(
        episode_midpoints - np.asarray(midpoint), axis=1
    )
    midpoint_deviation_median = float(np.median(midpoint_deviation))
    midpoint_deviation_mad = float(
        np.median(np.abs(midpoint_deviation - midpoint_deviation_median))
    )
    global_midpoint_support_px = max(
        float(config["midpoint_translation_min_gate_px"]),
        midpoint_deviation_median + 6.0 * 1.4826 * midpoint_deviation_mad,
    )
    globally_supported_segments = [
        segment_index
        for segment_index, deviation in zip(
            usable_segments, midpoint_deviation, strict=True
        )
        if float(deviation) <= global_midpoint_support_px
    ]
    global_clean_by_segment = {
        segment_index: [
            item
            for item in episode_summaries[segment_index]["clean_rows"]
            if abs(_wrap_angle_radians(item["axis_rad"] - axis_rad))
            <= maximum_axis_deviation_rad
        ]
        for segment_index in globally_supported_segments
    }
    global_clean_rows = [
        item
        for segment_index in globally_supported_segments
        for item in global_clean_by_segment[segment_index]
    ]
    gross_rejected = dual_frame_count - len(global_clean_rows)
    gross_rejection_rate = float(gross_rejected / dual_frame_count)
    roi_margin = float(config["marker_roi_margin_px"])
    marker_roi: dict[str, list[float]] = {}
    role_model_rejection_reasons: dict[str, list[str]] = {"a": [], "b": []}
    for role in ("a", "b"):
        # Each episode contributes one robust envelope, so a long shifted
        # episode cannot dominate the production ROI merely by having more
        # frames. The outer 1% of episode envelopes is excluded symmetrically.
        episode_envelopes = []
        for segment_index in globally_supported_segments:
            rows = global_clean_by_segment[segment_index]
            if not rows:
                continue
            x_values = np.asarray([item[role][0] for item in rows])
            y_values = np.asarray([item[role][1] for item in rows])
            x0_episode, x1_episode = np.percentile(x_values, [0.1, 99.9])
            y0_episode, y1_episode = np.percentile(y_values, [0.1, 99.9])
            episode_envelopes.append(
                (x0_episode, y0_episode, x1_episode, y1_episode)
            )
        if not episode_envelopes:
            role_model_rejection_reasons[role].append(
                f"marker_{role}_has_no_episode_equal_roi_evidence"
            )
            continue
        envelope_array = np.asarray(episode_envelopes, dtype=np.float64)
        x0 = float(np.percentile(envelope_array[:, 0], 1.0))
        y0 = float(np.percentile(envelope_array[:, 1], 1.0))
        x1 = float(np.percentile(envelope_array[:, 2], 99.0))
        y1 = float(np.percentile(envelope_array[:, 3], 99.0))
        marker_roi[role] = [
            float(x0 - roi_margin),
            float(y0 - roi_margin),
            float(x1 + roi_margin),
            float(y1 + roi_margin),
        ]

    axis_deviation_deg = np.asarray(
        [
            abs(math.degrees(_wrap_angle_radians(episode_summaries[index]["axis_rad"] - axis_rad)))
            for index in usable_segments
        ],
        dtype=np.float64,
    )
    report["training_evidence"].update(
        {
            "clean_dual_marker_frames": len(global_clean_rows),
            "gross_rejected_dual_marker_frames": gross_rejected,
            "gross_rejection_rate": gross_rejection_rate,
            "usable_episode_count": len(usable_segments),
        }
    )
    report["model"] = {
        "axis_deg": math.degrees(axis_rad),
        "axis_unit": [math.cos(axis_rad), math.sin(axis_rad)],
        "midpoint_px": list(midpoint),
        "marker_roi_xyxy_px": marker_roi,
        "inferred_distance_support_px": [minimum_distance_px, maximum_distance_px],
        "midpoint_episode_p95_px": float(np.percentile(midpoint_deviation, 95.0)),
        "midpoint_global_support_px": global_midpoint_support_px,
        "midpoint_supported_episode_count": len(globally_supported_segments),
        "axis_episode_p95_deg": float(np.percentile(axis_deviation_deg, 95.0)),
        "calibration_distance_range_px": [
            float(gripper_calibration["min_px"]),
            float(gripper_calibration["max_px"]),
        ],
        "calibration_width_range_m": [
            float(gripper_calibration["min_m"]),
            float(gripper_calibration["max_m"]),
        ],
    }

    warnings: list[str] = []
    if gross_rejection_rate > 0.01:
        warnings.append(
            f"gross_dual_geometry_rejection_rate={gross_rejection_rate:.6f} > "
            "diagnostic 0.01; robustly rejected frames were not model evidence"
        )
    if validation["coverage"] < config["minimum_cv_coverage"]:
        warnings.append(
            f"combined_held_out_roi_coverage={validation['coverage']:.6f} < "
            f"diagnostic {config['minimum_cv_coverage']}"
        )
    if report["model"]["midpoint_episode_p95_px"] > config["maximum_midpoint_episode_p95_px"]:
        warnings.append(
            "episode_midpoint_p95_px="
            f"{report['model']['midpoint_episode_p95_px']:.6f} > diagnostic "
            f"{config['maximum_midpoint_episode_p95_px']}; held-out millimetre "
            "errors remain authoritative"
        )
    if report["model"]["axis_episode_p95_deg"] > config["maximum_axis_episode_p95_deg"]:
        warnings.append(
            "episode_axis_p95_deg="
            f"{report['model']['axis_episode_p95_deg']:.6f} > diagnostic "
            f"{config['maximum_axis_episode_p95_deg']}; held-out millimetre "
            "errors remain authoritative"
        )

    role_contracts: dict[str, dict] = {}
    for role in ("a", "b"):
        contract = evaluate_single_marker_role_contract(
            role,
            validation[role],
            per_episode_by_role[role],
            config,
        )
        if role_model_rejection_reasons[role]:
            contract["rejection_reasons"].extend(
                role_model_rejection_reasons[role]
            )
            contract["status"] = "rejected"
        role_contracts[role] = contract
        warnings.extend(
            f"marker_{role}: {warning}" for warning in contract["warnings"]
        )
    accepted_roles = [
        role for role in ("a", "b") if role_contracts[role]["status"] == "accepted"
    ]
    rejected_roles = [role for role in ("a", "b") if role not in accepted_roles]
    report["accepted_roles"] = accepted_roles
    report["rejected_roles"] = rejected_roles
    report["role_contracts"] = role_contracts
    report["warnings"] = warnings
    report["diagnostic_thresholds_are_not_authorization_gates"] = [
        "minimum_cv_coverage",
        "gross_dual_geometry_rejection_rate",
        "maximum_midpoint_episode_p95_px",
        "maximum_axis_episode_p95_deg",
    ]
    if len(accepted_roles) == 2:
        report["status"] = "accepted"
    elif len(accepted_roles) == 1:
        report["status"] = "partially_accepted"
    else:
        report["status"] = "rejected"
        report["rejection_reasons"].append(
            "no_marker_role_passed_the_held_out_physical_width_contract"
        )
    return report


def raw_measurement(
    raw_row: dict,
    gripper_calibration: dict | None,
    args: argparse.Namespace,
    single_marker_recovery: dict | None = None,
) -> dict:
    marker_a_center = _finite_marker_center(raw_row.get("a"))
    marker_b_center = _finite_marker_center(raw_row.get("b"))
    primary_dual_marker_valid = bool(raw_row.get("primary_valid", raw_row["valid"]))
    adaptive_dual_marker_valid = False
    adaptive_recovered_role = raw_row.get("adaptive_recovered_role")
    marker_mask = int(marker_a_center is not None) | (2 * int(marker_b_center is not None))
    dual_marker_valid = primary_dual_marker_valid
    single_marker_valid = False
    single_marker_candidate = False
    single_marker_role: str | None = None
    continuity_pass: bool | None = None
    valid = dual_marker_valid
    distance = raw_row["distance_px"] if dual_marker_valid else None
    marker_side_px = raw_row["marker_side_px"] if dual_marker_valid else None
    measurement_source = "dual_marker_primary" if dual_marker_valid else "missing"
    if (
        not dual_marker_valid
        and bool(raw_row.get("adaptive_retry_valid"))
        and adaptive_dual_promotion_authorized(single_marker_recovery)
        and gripper_calibration is not None
        and marker_a_center is not None
        and marker_b_center is not None
    ):
        adaptive_geometry = single_marker_recovery["adaptive_dual_geometry"]
        model = adaptive_geometry["model"]
        config = adaptive_geometry["config"]
        candidate_distance = float(raw_row["distance_px"])
        candidate_axis = math.atan2(
            marker_b_center[1] - marker_a_center[1],
            marker_b_center[0] - marker_a_center[0],
        )
        midpoint = (
            (marker_a_center[0] + marker_b_center[0]) * 0.5,
            (marker_a_center[1] + marker_b_center[1]) * 0.5,
        )
        model_midpoint = model["midpoint_px"]
        midpoint_delta = math.hypot(
            midpoint[0] - float(model_midpoint[0]),
            midpoint[1] - float(model_midpoint[1]),
        )
        support_low, support_high = model["inferred_distance_support_px"]
        calibration_low = float(gripper_calibration["min_px"])
        calibration_high = float(gripper_calibration["max_px"])
        axis_delta = abs(
            _wrap_angle_radians(candidate_axis - math.radians(float(model["axis_deg"])))
        )
        inside_rois = True
        for role, center in (("a", marker_a_center), ("b", marker_b_center)):
            x0, y0, x1, y1 = model["marker_roi_xyxy_px"][role]
            inside_rois = inside_rois and (
                float(x0) <= center[0] <= float(x1)
                and float(y0) <= center[1] <= float(y1)
            )
        marker_side_supported = True
        marker_side_support = model.get("marker_side_support_px")
        if marker_side_support is not None and raw_row.get("marker_side_px") is not None:
            try:
                candidate_marker_side = float(raw_row["marker_side_px"])
            except (TypeError, ValueError):
                marker_side_supported = False
            else:
                marker_side_supported = (
                    math.isfinite(candidate_marker_side)
                    and float(marker_side_support[0])
                    <= candidate_marker_side
                    <= float(marker_side_support[1])
                )
        if (
            inside_rois
            and marker_side_supported
            and float(support_low) <= candidate_distance <= float(support_high)
            and calibration_low <= candidate_distance <= calibration_high
            and axis_delta <= math.radians(float(config["maximum_axis_deviation_deg"]))
            and midpoint_delta <= float(model["midpoint_support_px"])
        ):
            dual_marker_valid = True
            adaptive_dual_marker_valid = True
            valid = True
            distance = candidate_distance
            marker_side_px = raw_row["marker_side_px"]
            measurement_source = "dual_marker_adaptive_threshold_retry"
        elif not calibration_low <= candidate_distance <= calibration_high:
            measurement_source = "adaptive_threshold_retry_calibration_support_rejected"
        else:
            measurement_source = "adaptive_threshold_retry_geometry_rejected"
    if not adaptive_dual_marker_valid and adaptive_recovered_role == "a":
        marker_a_center = None
        marker_mask &= ~1
    elif not adaptive_dual_marker_valid and adaptive_recovered_role == "b":
        marker_b_center = None
        marker_mask &= ~2
    elif not adaptive_dual_marker_valid and adaptive_recovered_role == "both":
        marker_a_center = None
        marker_b_center = None
        marker_mask = 0
    authorized_single_roles = accepted_single_marker_roles(single_marker_recovery)
    if (
        not dual_marker_valid
        and authorized_single_roles
        and gripper_calibration is not None
    ):
        a = marker_a_center
        b = marker_b_center
        if (a is None) != (b is None):
            role = "a" if a is not None else "b"
            single_marker_role = role
            center = a if a is not None else b
            assert center is not None
            if role not in authorized_single_roles:
                measurement_source = f"single_marker_{role}_role_contract_rejected"
                center = None
            if center is None:
                pass
            else:
                model = single_marker_recovery["model"]
                midpoint = model["midpoint_px"]
                axis_unit = model["axis_unit"]
                center_axis = (
                    center[0] * float(axis_unit[0]) + center[1] * float(axis_unit[1])
                )
                midpoint_axis = (
                    float(midpoint[0]) * float(axis_unit[0])
                    + float(midpoint[1]) * float(axis_unit[1])
                )
                inferred_distance = 2.0 * (
                    midpoint_axis - center_axis if role == "a" else center_axis - midpoint_axis
                )
                support_low, support_high = model["inferred_distance_support_px"]
                calibration_low = float(gripper_calibration["min_px"])
                calibration_high = float(gripper_calibration["max_px"])
                roi_x0, roi_y0, roi_x1, roi_y1 = model["marker_roi_xyxy_px"][role]
                inside_roi = (
                    float(roi_x0) <= center[0] <= float(roi_x1)
                    and float(roi_y0) <= center[1] <= float(roi_y1)
                )
                if (
                    inside_roi
                    and float(support_low) <= inferred_distance <= float(support_high)
                    and calibration_low <= inferred_distance <= calibration_high
                ):
                    distance = inferred_distance
                    valid = True
                    single_marker_valid = True
                    single_marker_candidate = True
                    measurement_source = f"single_marker_{role}"
                else:
                    if not inside_roi:
                        rejection = "roi"
                    elif not calibration_low <= inferred_distance <= calibration_high:
                        rejection = "calibration_support"
                    else:
                        rejection = "distance_support"
                    measurement_source = (
                        f"single_marker_{role}_outside_validated_"
                        + rejection
                    )
    distance_m = None
    distance_cm = None
    calibration_mode = "missing"
    px_to_meter = fixed_px_to_meter_scale(args)
    if valid:
        if gripper_calibration is None and px_to_meter is None and marker_side_px is not None:
            px_to_meter = float(args.marker_width_m / marker_side_px)
        # Keep the continuous calibrated measurement for segment-local filling.
        # The fold-only dead zone is applied to the final per-frame value after
        # filling, avoiding a nonlinear dead-zone boundary distortion.
        distance_m, distance_cm, calibration_mode = distance_px_to_width(
            distance,
            gripper_calibration,
            marker_side_px,
            args,
            apply_deadzone=False,
        )
    return {
        "valid": valid,
        "dual_marker_valid": dual_marker_valid,
        "primary_dual_marker_valid": primary_dual_marker_valid,
        "adaptive_dual_marker_valid": adaptive_dual_marker_valid,
        "adaptive_recovered_role": adaptive_recovered_role,
        "adaptive_retry_strategy": raw_row.get("adaptive_retry_strategy"),
        "adaptive_retry_detector_strategy": raw_row.get(
            "adaptive_retry_detector_strategy"
        ),
        "adaptive_retry_threshold_constant": raw_row.get(
            "adaptive_retry_threshold_constant"
        ),
        "adaptive_retry_peer_delta_px": raw_row.get(
            "adaptive_retry_peer_delta_px"
        ),
        "adaptive_retry_roi_xyxy_px": raw_row.get(
            "adaptive_retry_roi_xyxy_px"
        ),
        "adaptive_retry_center_error_px": raw_row.get(
            "adaptive_retry_center_error_px"
        ),
        "adaptive_retry_vertical_error_px": raw_row.get(
            "adaptive_retry_vertical_error_px"
        ),
        "adaptive_retry_midpoint_error_px": raw_row.get(
            "adaptive_retry_midpoint_error_px"
        ),
        "adaptive_retry_side_ratio": raw_row.get("adaptive_retry_side_ratio"),
        "single_marker_valid": single_marker_valid,
        "single_marker_candidate": single_marker_candidate,
        "single_marker_role": single_marker_role,
        "continuity_pass": continuity_pass,
        "fallback_run_id": None,
        "marker_mask": marker_mask,
        "calibration_mode": calibration_mode,
        "calibration_clamped": "clamped" in calibration_mode,
        "measurement_source": measurement_source,
        "distance": distance,
        "marker_side_px": marker_side_px,
        "distance_m": distance_m,
        "distance_cm": distance_cm,
        "px_to_meter": px_to_meter,
    }


def apply_adaptive_dual_continuity_gate(
    measurements: dict[int, dict],
    active_ranges: Sequence[ActiveFrameRange],
    calibrated_unix_ms: Sequence[float],
    recovery_report: dict,
) -> dict:
    """Validate retry dual-marker runs without creating a trust loop.

    Every candidate has already passed frame-local ID uniqueness, peer-center,
    independently trained geometry, and calibration-domain gates.  Primary
    detections are never replaced as anchors: when one is nearby it adds a
    boundary consistency check, while its absence is diagnostic rather than a
    reason to discard otherwise independent dual-marker evidence.
    """

    adaptive_geometry = recovery_report.get("adaptive_dual_geometry") or {}
    config = adaptive_geometry.get("config") or {}
    promotion_contract = recovery_report.get("adaptive_dual_promotion_contract") or {}
    promotion_authorized = adaptive_dual_promotion_authorized(recovery_report)
    application = {
        "promotion_authorized": promotion_authorized,
        "promotion_contract_schema": promotion_contract.get("schema"),
        "candidate_frames": 0,
        "accepted_frames": 0,
        "rejected_frames": 0,
        "candidate_runs": 0,
        "accepted_runs": 0,
        "rejected_runs": 0,
        "rejected_run_reasons": {},
        "maximum_accepted_run_duration_ms": 0.0,
        "long_run_diagnostic_threshold_ms": promotion_contract.get(
            "long_run_diagnostic_ms"
        ),
        "long_run_count": 0,
        "runs": [],
    }
    if not promotion_authorized:
        # This should be unreachable through raw_measurement(), which never
        # promotes an adaptive row without the same authorization.  Keep the
        # gate independently fail-closed for programmatic callers and future
        # refactors that may supply prebuilt measurement dictionaries.
        for active_range in active_ranges:
            for frame_idx in active_range.frame_indices:
                item = measurements[frame_idx]
                if not item.get("adaptive_dual_marker_valid"):
                    continue
                application["candidate_frames"] += 1
                application["rejected_frames"] += 1
                item["continuity_pass"] = False
                item["adaptive_dual_marker_valid"] = False
                item["dual_marker_valid"] = False
                item["valid"] = False
                item["candidate_distance"] = item.get("distance")
                item["candidate_distance_m"] = item.get("distance_m")
                item["distance"] = None
                item["distance_m"] = None
                item["distance_cm"] = None
                item["measurement_source"] = (
                    "adaptive_threshold_retry_promotion_contract_rejected"
                )
        if application["candidate_frames"]:
            application["candidate_runs"] = 1
            application["rejected_runs"] = 1
            application["rejected_run_reasons"] = {
                "adaptive_promotion_contract_not_authorized": 1
            }
        recovery_report["adaptive_application"] = application
        return application

    base_tolerance_m = float(config["continuity_base_tolerance_m"])
    slew_m_per_sec = float(config["continuity_max_slew_m_per_sec"])
    anchor_horizon_ms = float(config["continuity_anchor_horizon_ms"])
    maximum_step_ms = float(config["continuity_max_timeline_step_ms"])
    long_run_diagnostic_ms = float(promotion_contract["long_run_diagnostic_ms"])

    for active_range in active_ranges:
        indices = list(active_range.frame_indices)
        position = 0
        while position < len(indices):
            frame_idx = indices[position]
            item = measurements[frame_idx]
            if not item.get("adaptive_dual_marker_valid"):
                position += 1
                continue
            run_start = position
            run_end = position
            while run_end + 1 < len(indices):
                current_idx = indices[run_end]
                next_idx = indices[run_end + 1]
                next_item = measurements[next_idx]
                timestamp_step_ms = (
                    float(calibrated_unix_ms[next_idx])
                    - float(calibrated_unix_ms[current_idx])
                )
                if (
                    next_idx != current_idx + 1
                    or not next_item.get("adaptive_dual_marker_valid")
                ):
                    break
                run_end += 1
            run_indices = indices[run_start : run_end + 1]
            recovered_roles = sorted(
                {
                    str(measurements[index].get("adaptive_recovered_role"))
                    for index in run_indices
                }
            )
            run_id = int(application["candidate_runs"])
            application["candidate_runs"] += 1
            application["candidate_frames"] += len(run_indices)

            def primary_anchor(search_positions, reference_idx: int, before: bool):
                for anchor_position in search_positions:
                    candidate_idx = indices[anchor_position]
                    candidate = measurements[candidate_idx]
                    if candidate.get("primary_dual_marker_valid"):
                        delta_ms = (
                            float(calibrated_unix_ms[reference_idx])
                            - float(calibrated_unix_ms[candidate_idx])
                            if before
                            else float(calibrated_unix_ms[candidate_idx])
                            - float(calibrated_unix_ms[reference_idx])
                        )
                        if 0.0 <= delta_ms <= anchor_horizon_ms:
                            return candidate_idx, delta_ms
                        return None
                return None

            previous_anchor = primary_anchor(
                range(run_start - 1, -1, -1), run_indices[0], True
            )
            next_anchor = primary_anchor(
                range(run_end + 1, len(indices)), run_indices[-1], False
            )
            touches_segment_start = run_start == 0
            touches_segment_end = run_end == len(indices) - 1
            anchors_by_side = {
                "previous": previous_anchor,
                "next": next_anchor,
            }
            available_anchor_sides = [
                side for side, anchor in anchors_by_side.items() if anchor is not None
            ]
            boundary_checks: list[dict] = []
            for side in available_anchor_sides:
                anchor = anchors_by_side[side]
                assert anchor is not None
                anchor_idx, delta_ms = anchor
                endpoint_idx = run_indices[0] if side == "previous" else run_indices[-1]
                residual_m = abs(
                    float(measurements[endpoint_idx]["distance_m"])
                    - float(measurements[anchor_idx]["distance_m"])
                )
                allowed_m = base_tolerance_m + slew_m_per_sec * delta_ms / 1000.0
                boundary_checks.append(
                    {
                        "side": side,
                        "anchor_frame_idx": anchor_idx,
                        "delta_ms": delta_ms,
                        "residual_m": residual_m,
                        "allowed_m": allowed_m,
                        "passed": math.isfinite(residual_m) and residual_m <= allowed_m,
                    }
                )
            duration_ms = max(
                0.0,
                float(calibrated_unix_ms[run_indices[-1]])
                - float(calibrated_unix_ms[run_indices[0]]),
            )
            is_long_run = duration_ms > long_run_diagnostic_ms
            if is_long_run:
                application["long_run_count"] += 1
            internal_checks: list[dict] = []
            for previous_idx, current_idx in zip(run_indices, run_indices[1:]):
                delta_ms = (
                    float(calibrated_unix_ms[current_idx])
                    - float(calibrated_unix_ms[previous_idx])
                )
                timestamp_step_valid = 0.0 < delta_ms <= maximum_step_ms
                residual_m = abs(
                    float(measurements[current_idx]["distance_m"])
                    - float(measurements[previous_idx]["distance_m"])
                )
                allowed_m = base_tolerance_m + slew_m_per_sec * max(
                    0.0, delta_ms
                ) / 1000.0
                internal_checks.append(
                    {
                        "previous_frame_idx": previous_idx,
                        "current_frame_idx": current_idx,
                        "delta_ms": delta_ms,
                        "maximum_timeline_step_ms": maximum_step_ms,
                        "timestamp_step_valid": timestamp_step_valid,
                        "residual_m": residual_m,
                        "allowed_m": allowed_m,
                        "passed": (
                            timestamp_step_valid
                            and math.isfinite(residual_m)
                            and residual_m <= allowed_m
                        ),
                    }
                )
            if not all(check["passed"] for check in boundary_checks):
                passed = False
                reason = "primary_boundary_width_continuity_failed"
            elif not all(check["passed"] for check in internal_checks):
                passed = False
                reason = "internal_temporal_or_width_continuity_failed"
            else:
                passed = True
                reason = "accepted"
            application["runs"].append(
                {
                    "segment_index": int(active_range.segment_index),
                    "run_id": run_id,
                    "recovered_roles": recovered_roles,
                    "first_frame_idx": run_indices[0],
                    "last_frame_idx": run_indices[-1],
                    "frame_count": len(run_indices),
                    "duration_ms": duration_ms,
                    "long_run_diagnostic_ms": long_run_diagnostic_ms,
                    "long_run_diagnostic": is_long_run,
                    "touches_segment_start": touches_segment_start,
                    "touches_segment_end": touches_segment_end,
                    "available_primary_anchor_sides": available_anchor_sides,
                    "status": "accepted" if passed else "rejected",
                    "reason": reason,
                    "boundary_checks": boundary_checks,
                    "internal_checks": internal_checks,
                }
            )
            if passed:
                application["accepted_runs"] += 1
                application["accepted_frames"] += len(run_indices)
                application["maximum_accepted_run_duration_ms"] = max(
                    application["maximum_accepted_run_duration_ms"], duration_ms
                )
                for accepted_idx in run_indices:
                    accepted_item = measurements[accepted_idx]
                    accepted_item["continuity_pass"] = True
                    accepted_item["fallback_run_id"] = run_id
                    accepted_item["measurement_source"] = (
                        "dual_marker_adaptive_threshold_retry_continuity_validated"
                    )
            else:
                application["rejected_runs"] += 1
                application["rejected_frames"] += len(run_indices)
                reasons = application["rejected_run_reasons"]
                reasons[reason] = int(reasons.get(reason, 0)) + 1
                for rejected_idx in run_indices:
                    rejected_item = measurements[rejected_idx]
                    rejected_item["continuity_pass"] = False
                    rejected_item["fallback_run_id"] = run_id
                    rejected_item["candidate_distance"] = rejected_item["distance"]
                    rejected_item["candidate_distance_m"] = rejected_item["distance_m"]
                    rejected_item["adaptive_dual_marker_valid"] = False
                    rejected_item["dual_marker_valid"] = False
                    rejected_item["valid"] = False
                    rejected_item["distance"] = None
                    rejected_item["distance_m"] = None
                    rejected_item["distance_cm"] = None
                    rejected_item["measurement_source"] = (
                        "adaptive_threshold_retry_continuity_rejected"
                    )
            position = run_end + 1

    recovery_report["adaptive_application"] = application
    return application


def apply_single_marker_continuity_gate(
    measurements: dict[int, dict],
    active_ranges: Sequence[ActiveFrameRange],
    calibrated_unix_ms: Sequence[float],
    recovery_report: dict,
) -> dict:
    """Reject unsupported single-marker runs; rejected frames fall through to fill."""

    config = recovery_report.get("config") or {}
    application = {
        "candidate_frames": 0,
        "accepted_frames": 0,
        "rejected_frames": 0,
        "candidate_runs": 0,
        "accepted_runs": 0,
        "rejected_runs": 0,
        "rejected_run_reasons": {},
        "maximum_accepted_run_duration_ms": 0.0,
        "accepted_width_range_m": [None, None],
        "runs": [],
    }
    authorized_roles = accepted_single_marker_roles(recovery_report)
    if not authorized_roles:
        recovery_report["application"] = application
        return application

    base_tolerance_m = float(config["continuity_base_tolerance_m"])
    slew_m_per_sec = float(config["continuity_max_slew_m_per_sec"])
    anchor_horizon_ms = float(config["continuity_anchor_horizon_ms"])
    maximum_step_ms = float(config["continuity_max_timeline_step_ms"])
    accepted_widths: list[float] = []

    for active_range in active_ranges:
        indices = list(active_range.frame_indices)
        position = 0
        while position < len(indices):
            frame_idx = indices[position]
            item = measurements[frame_idx]
            if not item.get("single_marker_candidate"):
                position += 1
                continue
            role = item.get("single_marker_role")
            run_start = position
            run_end = position
            while run_end + 1 < len(indices):
                current_idx = indices[run_end]
                next_idx = indices[run_end + 1]
                next_item = measurements[next_idx]
                timestamp_step_ms = (
                    float(calibrated_unix_ms[next_idx])
                    - float(calibrated_unix_ms[current_idx])
                )
                if (
                    next_idx != current_idx + 1
                    or not next_item.get("single_marker_candidate")
                    or next_item.get("single_marker_role") != role
                    or not (0.0 < timestamp_step_ms <= maximum_step_ms)
                ):
                    break
                run_end += 1
            run_indices = indices[run_start : run_end + 1]
            application["candidate_runs"] += 1
            run_id = int(application["candidate_runs"] - 1)
            application["candidate_frames"] += len(run_indices)

            previous_anchor = None
            for anchor_position in range(run_start - 1, -1, -1):
                candidate_idx = indices[anchor_position]
                if measurements[candidate_idx].get("primary_dual_marker_valid"):
                    delta_ms = (
                        float(calibrated_unix_ms[run_indices[0]])
                        - float(calibrated_unix_ms[candidate_idx])
                    )
                    if 0.0 <= delta_ms <= anchor_horizon_ms:
                        previous_anchor = (candidate_idx, delta_ms)
                    break
            next_anchor = None
            for anchor_position in range(run_end + 1, len(indices)):
                candidate_idx = indices[anchor_position]
                if measurements[candidate_idx].get("primary_dual_marker_valid"):
                    delta_ms = (
                        float(calibrated_unix_ms[candidate_idx])
                        - float(calibrated_unix_ms[run_indices[-1]])
                    )
                    if 0.0 <= delta_ms <= anchor_horizon_ms:
                        next_anchor = (candidate_idx, delta_ms)
                    break

            internal_checks: list[dict] = []
            for previous_idx, current_idx in zip(
                run_indices, run_indices[1:]
            ):
                delta_ms = (
                    float(calibrated_unix_ms[current_idx])
                    - float(calibrated_unix_ms[previous_idx])
                )
                previous_width_m = float(measurements[previous_idx]["distance_m"])
                current_width_m = float(measurements[current_idx]["distance_m"])
                residual_m = abs(current_width_m - previous_width_m)
                allowed_m = base_tolerance_m + slew_m_per_sec * delta_ms / 1000.0
                internal_checks.append(
                    {
                        "previous_frame_idx": previous_idx,
                        "current_frame_idx": current_idx,
                        "delta_ms": delta_ms,
                        "residual_m": residual_m,
                        "allowed_m": allowed_m,
                        "passed": (
                            math.isfinite(previous_width_m)
                            and math.isfinite(current_width_m)
                            and residual_m <= allowed_m
                        ),
                    }
                )

            boundary_checks: list[dict] = []
            if previous_anchor is not None:
                anchor_idx, delta_ms = previous_anchor
                residual_m = abs(
                    float(measurements[run_indices[0]]["distance_m"])
                    - float(measurements[anchor_idx]["distance_m"])
                )
                allowed_m = base_tolerance_m + slew_m_per_sec * delta_ms / 1000.0
                boundary_checks.append(
                    {
                        "side": "previous",
                        "anchor_frame_idx": anchor_idx,
                        "delta_ms": delta_ms,
                        "residual_m": residual_m,
                        "allowed_m": allowed_m,
                        "passed": residual_m <= allowed_m,
                    }
                )
            if next_anchor is not None:
                anchor_idx, delta_ms = next_anchor
                residual_m = abs(
                    float(measurements[run_indices[-1]]["distance_m"])
                    - float(measurements[anchor_idx]["distance_m"])
                )
                allowed_m = base_tolerance_m + slew_m_per_sec * delta_ms / 1000.0
                boundary_checks.append(
                    {
                        "side": "next",
                        "anchor_frame_idx": anchor_idx,
                        "delta_ms": delta_ms,
                        "residual_m": residual_m,
                        "allowed_m": allowed_m,
                        "passed": residual_m <= allowed_m,
                    }
                )

            if role not in authorized_roles:
                passed = False
                reason = "marker_role_held_out_contract_rejected"
            elif not boundary_checks:
                passed = False
                reason = "no_dual_marker_anchor_within_horizon"
            elif not all(check["passed"] for check in boundary_checks):
                passed = False
                reason = "boundary_width_continuity_failed"
            elif not all(check["passed"] for check in internal_checks):
                passed = False
                reason = "internal_width_continuity_failed"
            else:
                passed = True
                reason = "accepted"
            duration_ms = max(
                0.0,
                float(calibrated_unix_ms[run_indices[-1]])
                - float(calibrated_unix_ms[run_indices[0]]),
            )
            run_report = {
                "segment_index": int(active_range.segment_index),
                "run_id": run_id,
                "role": role,
                "first_frame_idx": run_indices[0],
                "last_frame_idx": run_indices[-1],
                "frame_count": len(run_indices),
                "duration_ms": duration_ms,
                "status": "accepted" if passed else "rejected",
                "reason": reason,
                "boundary_checks": boundary_checks,
                "internal_checks": internal_checks,
            }
            application["runs"].append(run_report)
            if passed:
                application["accepted_runs"] += 1
                application["accepted_frames"] += len(run_indices)
                application["maximum_accepted_run_duration_ms"] = max(
                    application["maximum_accepted_run_duration_ms"], duration_ms
                )
                for accepted_idx in run_indices:
                    accepted_item = measurements[accepted_idx]
                    accepted_item["continuity_pass"] = True
                    accepted_item["fallback_run_id"] = run_id
                    accepted_item["measurement_source"] = (
                        f"single_marker_{role}_continuity_validated"
                    )
                    accepted_widths.append(float(accepted_item["distance_m"]))
            else:
                application["rejected_runs"] += 1
                application["rejected_frames"] += len(run_indices)
                reasons = application["rejected_run_reasons"]
                reasons[reason] = int(reasons.get(reason, 0)) + 1
                for rejected_idx in run_indices:
                    rejected_item = measurements[rejected_idx]
                    rejected_item["continuity_pass"] = False
                    rejected_item["fallback_run_id"] = run_id
                    rejected_item["candidate_distance"] = rejected_item["distance"]
                    rejected_item["candidate_distance_m"] = rejected_item["distance_m"]
                    rejected_item["single_marker_valid"] = False
                    rejected_item["valid"] = False
                    rejected_item["distance"] = None
                    rejected_item["distance_m"] = None
                    rejected_item["distance_cm"] = None
                    rejected_item["measurement_source"] = (
                        f"single_marker_{role}_continuity_rejected"
                    )
            position = run_end + 1

    if accepted_widths:
        application["accepted_width_range_m"] = [
            min(accepted_widths),
            max(accepted_widths),
        ]
    recovery_report["application"] = application
    return application


def single_marker_recovery_summary(report: dict, report_path: Path | None = None) -> dict:
    cross_validation = report.get("cross_validation") or {}
    application = report.get("application") or {}
    adaptive_application = report.get("adaptive_application") or {}
    result = {
        "schema": report.get("schema"),
        "status": report.get("status"),
        "accepted_roles": list(report.get("accepted_roles") or []),
        "rejected_roles": list(report.get("rejected_roles") or []),
        "role_contracts": report.get("role_contracts") or {},
        "rejection_reasons": list(report.get("rejection_reasons") or []),
        "warnings": list(report.get("warnings") or []),
        "diagnostic_thresholds_are_not_authorization_gates": list(
            report.get("diagnostic_thresholds_are_not_authorization_gates") or []
        ),
        "candidate_counts": report.get("candidate_counts") or {},
        "training_evidence": report.get("training_evidence") or {},
        "adaptive_dual_promotion_contract": report.get(
            "adaptive_dual_promotion_contract"
        ),
        "adaptive_dual_geometry": report.get("adaptive_dual_geometry"),
        "model": report.get("model"),
        "cross_validation": {
            key: cross_validation.get(key)
            for key in (
                "mode",
                "fold_count",
                "coverage",
                "accepted_predictions",
                "possible_predictions",
                "a",
                "b",
                "combined",
            )
            if key in cross_validation
        },
        "application": {
            key: application.get(key)
            for key in (
                "candidate_frames",
                "accepted_frames",
                "rejected_frames",
                "candidate_runs",
                "accepted_runs",
                "rejected_runs",
                "rejected_run_reasons",
                "maximum_accepted_run_duration_ms",
                "accepted_width_range_m",
            )
            if key in application
        },
        "adaptive_application": {
            key: adaptive_application.get(key)
            for key in (
                "candidate_frames",
                "accepted_frames",
                "rejected_frames",
                "candidate_runs",
                "accepted_runs",
                "rejected_runs",
                "rejected_run_reasons",
                "promotion_authorized",
                "promotion_contract_schema",
                "maximum_accepted_run_duration_ms",
                "long_run_diagnostic_threshold_ms",
                "long_run_count",
            )
            if key in adaptive_application
        },
        "full_report_sha256": object_fingerprint(report),
    }
    if report_path is not None:
        result["full_report"] = content_file_identity(report_path)
    return result


def interpolate_segment_measurements(
    frame_indices: Sequence[int],
    measurements: dict[int, dict],
    calibrated_unix_ms: Sequence[float],
) -> dict[int, tuple[float, float, float]]:
    measured_indices = [index for index in frame_indices if measurements[index]["valid"]]
    if not measured_indices:
        return {}
    measured_times = [float(calibrated_unix_ms[index]) for index in measured_indices]
    filled: dict[int, tuple[float, float, float]] = {}
    for frame_idx in frame_indices:
        position = bisect.bisect_left(measured_times, float(calibrated_unix_ms[frame_idx]))
        if position <= 0:
            before = after = 0
        elif position >= len(measured_indices):
            before = after = len(measured_indices) - 1
        else:
            before = position - 1
            after = position
        before_idx = measured_indices[before]
        after_idx = measured_indices[after]
        before_values = measurements[before_idx]
        after_values = measurements[after_idx]
        denominator = measured_times[after] - measured_times[before]
        alpha = (
            0.0
            if denominator <= 0.0
            else (float(calibrated_unix_ms[frame_idx]) - measured_times[before]) / denominator
        )
        alpha = min(1.0, max(0.0, alpha))
        filled[frame_idx] = tuple(
            (1.0 - alpha) * float(before_values[key]) + alpha * float(after_values[key])
            for key in ("distance", "distance_m", "distance_cm")
        )
    return filled


def forward_fill_segment_measurements(
    frame_indices: Sequence[int],
    measurements: dict[int, dict],
    calibrated_unix_ms: Sequence[float],
) -> tuple[dict[int, tuple[float, float, float]], dict]:
    """Fill one timestamp-log segment without borrowing from its neighbours.

    A leading missing run has no previous value inside the segment, so it is
    backfilled from that segment's first finite real detection.  From that
    point onward every missing frame holds the most recent finite real
    detection.  An all-missing segment is deliberately left empty: borrowing
    from another demonstration would fabricate gripper state.
    """

    ordered_indices = list(frame_indices)

    def finite_values(frame_idx: int) -> tuple[float, float, float] | None:
        item = measurements[frame_idx]
        if not item["valid"]:
            return None
        try:
            values = tuple(
                float(item[key]) for key in ("distance", "distance_m", "distance_cm")
            )
        except (TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    finite_by_frame = {
        frame_idx: values
        for frame_idx in ordered_indices
        if (values := finite_values(frame_idx)) is not None
    }
    statistics = {
        "output_width_frames": 0,
        "imputed_width_frames": 0,
        "leading_backfill_frames": 0,
        "forward_fill_frames": 0,
        "max_forward_hold_frames": 0,
        "max_forward_hold_ms": 0.0,
    }
    if not finite_by_frame:
        return {}, statistics

    first_position = next(
        position
        for position, frame_idx in enumerate(ordered_indices)
        if frame_idx in finite_by_frame
    )
    first_values = finite_by_frame[ordered_indices[first_position]]
    filled: dict[int, tuple[float, float, float]] = {}
    last_values: tuple[float, float, float] | None = None
    last_detection_time_ms: float | None = None
    hold_frames = 0
    for position, frame_idx in enumerate(ordered_indices):
        values = finite_by_frame.get(frame_idx)
        if values is not None:
            last_values = values
            last_detection_time_ms = float(calibrated_unix_ms[frame_idx])
            hold_frames = 0
            filled[frame_idx] = values
            continue
        if position < first_position:
            filled[frame_idx] = first_values
            statistics["leading_backfill_frames"] += 1
            continue
        if last_values is None or last_detection_time_ms is None:
            raise AssertionError("segment-local forward-fill lost its first detection")
        filled[frame_idx] = last_values
        statistics["forward_fill_frames"] += 1
        hold_frames += 1
        statistics["max_forward_hold_frames"] = max(
            statistics["max_forward_hold_frames"], hold_frames
        )
        statistics["max_forward_hold_ms"] = max(
            statistics["max_forward_hold_ms"],
            max(0.0, float(calibrated_unix_ms[frame_idx]) - last_detection_time_ms),
        )

    statistics["output_width_frames"] = len(filled)
    statistics["imputed_width_frames"] = (
        statistics["leading_backfill_frames"] + statistics["forward_fill_frames"]
    )
    return filled, statistics


def continuous_gripper_quality_config(args: argparse.Namespace) -> dict:
    """Return the fingerprinted continuous-width evidence contract.

    Gap/edge targets are retained as observability thresholds, not rejection
    gates.  A segment is rejected only when it cannot produce a finite,
    physically valid width without borrowing from another segment.
    """

    return {
        "enabled": bool(getattr(args, "continuous_gripper_quality_audit", False)),
        "min_real_detections_per_segment": int(
            getattr(args, "quality_min_detections_per_segment", 1)
        ),
        "max_internal_gap_ms": float(
            getattr(args, "quality_max_internal_gap_ms", 500.0)
        ),
        "max_edge_extrapolation_ms": float(
            getattr(args, "quality_max_edge_extrapolation_ms", 250.0)
        ),
        "min_width_m": float(getattr(args, "quality_min_width_m", 0.0)),
        "max_width_m": float(getattr(args, "quality_max_width_m", 0.11)),
        "fill_policy": "segment_local_first_valid_backfill_then_forward_fill",
        "gap_and_edge_thresholds_are_diagnostic_only": True,
    }


def audit_continuous_gripper_quality(
    active_ranges: Sequence[ActiveFrameRange],
    measurements: dict[int, dict],
    filled_by_frame: dict[int, tuple[float, float, float]],
    calibrated_unix_ms: Sequence[float],
    args: argparse.Namespace,
) -> dict:
    """Audit segment-local holds, rejecting only absent or invalid width evidence."""

    contract = continuous_gripper_quality_config(args)
    report: dict = {
        "schema": CONTINUOUS_GRIPPER_QUALITY_SCHEMA,
        "status": "disabled" if not contract["enabled"] else "accepted",
        "contract": contract,
        "segments": [],
        "violations": [],
        "warnings": [],
    }
    if not contract["enabled"]:
        return report

    global_violations: list[str] = []
    if args.missing_policy != "ffill":
        global_violations.append(
            f"missing_policy={args.missing_policy!r} is not 'ffill'"
        )

    all_real_widths: list[float] = []
    all_filled_widths: list[float] = []
    for active_range in active_ranges:
        frame_indices = list(active_range.frame_indices)
        measured = [index for index in frame_indices if measurements[index]["valid"]]
        finite_measured = [
            index
            for index in measured
            if measurements[index]["distance_m"] is not None
            and math.isfinite(float(measurements[index]["distance_m"]))
        ]
        measured_times = [float(calibrated_unix_ms[index]) for index in finite_measured]
        leading_metric_ms = (
            float(measured_times[0] - calibrated_unix_ms[frame_indices[0]])
            if finite_measured
            else math.inf
        )
        trailing_metric_ms = (
            float(calibrated_unix_ms[frame_indices[-1]] - measured_times[-1])
            if finite_measured
            else math.inf
        )
        internal_gaps = [
            float(after - before)
            for before, after in zip(measured_times, measured_times[1:])
        ]
        max_internal_metric_ms = max(
            internal_gaps, default=0.0 if finite_measured else math.inf
        )
        real_widths = [measurements[index]["distance_m"] for index in measured]
        filled_widths = [
            None if index not in filled_by_frame else filled_by_frame[index][1]
            for index in frame_indices
        ]
        finite_real = [
            float(value)
            for value in real_widths
            if value is not None and math.isfinite(float(value))
        ]
        finite_filled = [
            float(value)
            for value in filled_widths
            if value is not None and math.isfinite(float(value))
        ]
        all_real_widths.extend(finite_real)
        all_filled_widths.extend(finite_filled)
        dual_marker_frames = sum(
            bool(measurements[index].get("dual_marker_valid", measurements[index]["valid"]))
            for index in frame_indices
        )
        primary_dual_marker_frames = sum(
            bool(measurements[index].get("primary_dual_marker_valid", measurements[index]["valid"]))
            for index in frame_indices
        )
        adaptive_dual_marker_frames = sum(
            bool(measurements[index].get("adaptive_dual_marker_valid", False))
            for index in frame_indices
        )
        single_marker_frames = sum(
            bool(measurements[index].get("single_marker_valid", False))
            for index in frame_indices
        )
        measurement_source_counts: dict[str, int] = {}
        for index in frame_indices:
            source = str(measurements[index].get("measurement_source", "legacy"))
            measurement_source_counts[source] = measurement_source_counts.get(source, 0) + 1
        segment_violations = list(global_violations)
        segment_warnings: list[str] = []
        _, fill_statistics = forward_fill_segment_measurements(
            frame_indices, measurements, calibrated_unix_ms
        )
        segment = {
            **active_range.as_dict(),
            "real_detection_frames": len(measured),
            "finite_real_detection_frames": len(finite_measured),
            "dual_marker_frames": int(dual_marker_frames),
            "primary_dual_marker_frames": int(primary_dual_marker_frames),
            "adaptive_dual_marker_frames": int(adaptive_dual_marker_frames),
            "single_marker_recovered_frames": int(single_marker_frames),
            "measurement_source_counts": measurement_source_counts,
            "detection_rate": float(len(measured) / len(frame_indices)),
            "leading_constant_extrapolation_ms": (
                leading_metric_ms if finite_measured else None
            ),
            "trailing_constant_extrapolation_ms": (
                trailing_metric_ms if finite_measured else None
            ),
            "max_internal_detection_gap_ms": (
                max_internal_metric_ms if finite_measured else None
            ),
            "real_width_min_m": min(finite_real, default=None),
            "real_width_max_m": max(finite_real, default=None),
            "filled_width_min_m": min(finite_filled, default=None),
            "filled_width_max_m": max(finite_filled, default=None),
            "fill_statistics": fill_statistics,
        }
        prefix = f"segment={active_range.segment_index}"
        if not finite_measured:
            segment_violations.append(
                f"{prefix} has no finite real gripper-width detection; "
                "cross-segment fill is forbidden"
            )
        elif len(finite_measured) < contract["min_real_detections_per_segment"]:
            segment_warnings.append(
                f"{prefix} real_detections={len(measured)} < "
                f"diagnostic_target={contract['min_real_detections_per_segment']}"
            )
        if finite_measured and max_internal_metric_ms > contract["max_internal_gap_ms"]:
            segment_warnings.append(
                f"{prefix} internal_gap_ms={max_internal_metric_ms:.3f} > "
                f"diagnostic_target={contract['max_internal_gap_ms']:.3f}; "
                "segment-local previous value was held"
            )
        if finite_measured and leading_metric_ms > contract["max_edge_extrapolation_ms"]:
            segment_warnings.append(
                f"{prefix} leading_extrapolation_ms={leading_metric_ms:.3f} > "
                f"diagnostic_target={contract['max_edge_extrapolation_ms']:.3f}; "
                "segment first value was backfilled"
            )
        if finite_measured and trailing_metric_ms > contract["max_edge_extrapolation_ms"]:
            segment_warnings.append(
                f"{prefix} trailing_extrapolation_ms={trailing_metric_ms:.3f} > "
                f"diagnostic_target={contract['max_edge_extrapolation_ms']:.3f}; "
                "segment-local previous value was held"
            )
        if len(finite_real) != len(real_widths) or len(finite_filled) != len(filled_widths):
            segment_violations.append(f"{prefix} has missing/non-finite width values")
        for label, values in (("real", finite_real), ("filled", finite_filled)):
            if values and (
                min(values) < contract["min_width_m"]
                or max(values) > contract["max_width_m"]
            ):
                segment_violations.append(
                    f"{prefix} {label}_width_range_m=[{min(values):.6f},{max(values):.6f}] "
                    f"outside [{contract['min_width_m']:.6f},{contract['max_width_m']:.6f}]"
                )
        segment["status"] = "rejected" if segment_violations else "accepted"
        segment["violations"] = segment_violations
        segment["warnings"] = segment_warnings
        report["segments"].append(segment)
        report["violations"].extend(segment_violations)
        report["warnings"].extend(segment_warnings)

    segments = report["segments"]
    accepted_segment_indices = [
        int(item["segment_index"])
        for item in segments
        if item["status"] == "accepted"
    ]
    rejected_segment_indices = [
        int(item["segment_index"])
        for item in segments
        if item["status"] == "rejected"
    ]
    report["summary"] = {
        "segment_count": len(segments),
        "accepted_segment_count": len(accepted_segment_indices),
        "rejected_segment_count": len(rejected_segment_indices),
        "accepted_segment_indices": accepted_segment_indices,
        "rejected_segment_indices": rejected_segment_indices,
        "real_detection_frames": sum(item["real_detection_frames"] for item in segments),
        "finite_real_detection_frames": sum(
            item["finite_real_detection_frames"] for item in segments
        ),
        "dual_marker_frames": sum(item["dual_marker_frames"] for item in segments),
        "primary_dual_marker_frames": sum(
            item["primary_dual_marker_frames"] for item in segments
        ),
        "adaptive_dual_marker_frames": sum(
            item["adaptive_dual_marker_frames"] for item in segments
        ),
        "single_marker_recovered_frames": sum(
            item["single_marker_recovered_frames"] for item in segments
        ),
        "imputed_width_frames": sum(
            item["fill_statistics"]["imputed_width_frames"] for item in segments
        ),
        "leading_backfill_frames": sum(
            item["fill_statistics"]["leading_backfill_frames"] for item in segments
        ),
        "forward_fill_frames": sum(
            item["fill_statistics"]["forward_fill_frames"] for item in segments
        ),
        "maximum_forward_hold_frames": max(
            (item["fill_statistics"]["max_forward_hold_frames"] for item in segments),
            default=0,
        ),
        "maximum_forward_hold_ms": max(
            (item["fill_statistics"]["max_forward_hold_ms"] for item in segments),
            default=0.0,
        ),
        "warning_count": len(report["warnings"]),
        "minimum_segment_detection_rate": min(
            (item["detection_rate"] for item in segments), default=0.0
        ),
        "maximum_internal_detection_gap_ms": max(
            (
                item["max_internal_detection_gap_ms"]
                for item in segments
                if item["max_internal_detection_gap_ms"] is not None
            ),
            default=None,
        ),
        "maximum_edge_extrapolation_ms": max(
            (
                max(
                    item["leading_constant_extrapolation_ms"],
                    item["trailing_constant_extrapolation_ms"],
                )
                for item in segments
                if item["leading_constant_extrapolation_ms"] is not None
            ),
            default=None,
        ),
        "real_width_range_m": [
            min(all_real_widths, default=None),
            max(all_real_widths, default=None),
        ],
        "filled_width_range_m": [
            min(all_filled_widths, default=None),
            max(all_filled_widths, default=None),
        ],
    }
    if rejected_segment_indices:
        report["status"] = (
            "partially_accepted" if accepted_segment_indices else "rejected"
        )
    return report


def write_sparse_merged_csv(
    csv_path: Path,
    timestamps: list[dict[str, str]],
    chunks: list[dict],
    gripper_calibration: dict | None,
    args: argparse.Namespace,
    active_ranges: Sequence[ActiveFrameRange],
    calibrated_unix_ms: Sequence[float],
    pre_publish_check: Callable[[], None] | None,
) -> tuple[int, str, dict]:
    requested_indices = [index for item in active_ranges for index in item.frame_indices]
    raw_rows = {int(row["frame_idx"]): row for row in iter_chunk_rows(chunks)}
    if sorted(raw_rows) != sorted(requested_indices):
        missing = sorted(set(requested_indices) - set(raw_rows))
        extra = sorted(set(raw_rows) - set(requested_indices))
        raise RuntimeError(
            "sparse raw detections do not match the active-frame plan: "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    single_marker_recovery = build_single_marker_recovery(
        raw_rows,
        active_ranges,
        gripper_calibration,
        args,
    )
    promotion_contract = single_marker_recovery["adaptive_dual_promotion_contract"]
    promotion_contract["single_marker_model_status"] = single_marker_recovery.get(
        "status"
    )
    promotion_contract["promotion_authorized"] = (
        adaptive_dual_promotion_authorized(single_marker_recovery)
    )
    recovery_report_path = csv_path.parent / "single_marker_recovery.json"
    measurements = {
        frame_idx: raw_measurement(
            raw_row,
            gripper_calibration,
            args,
            single_marker_recovery,
        )
        for frame_idx, raw_row in raw_rows.items()
    }
    adaptive_continuity_application = apply_adaptive_dual_continuity_gate(
        measurements,
        active_ranges,
        calibrated_unix_ms,
        single_marker_recovery,
    )
    continuity_application = apply_single_marker_continuity_gate(
        measurements,
        active_ranges,
        calibrated_unix_ms,
        single_marker_recovery,
    )
    atomic_write_json(recovery_report_path, single_marker_recovery)
    recovery_summary = single_marker_recovery_summary(
        single_marker_recovery,
        recovery_report_path,
    )
    candidate_counts = single_marker_recovery.get("candidate_counts") or {}
    print(
        "[adaptive-dual:contract] "
        f"enabled={int(bool(promotion_contract.get('enabled')))} "
        f"raw_candidates={candidate_counts.get('adaptive_dual_candidate', 0)} "
        f"single_marker_model={single_marker_recovery.get('status')} "
        f"authorized={int(bool(promotion_contract.get('promotion_authorized')))} "
        "anchors=primary_only_if_available adaptive_is_never_anchor "
        f"long_run_diagnostic_ms={float(promotion_contract['long_run_diagnostic_ms']):.1f} "
        "long_run_is_not_rejection=1 "
        "failure=segment_local_fill",
        flush=True,
    )
    print(
        "[single-marker] "
        f"status={single_marker_recovery.get('status')} "
        f"accepted_roles={','.join(accepted_single_marker_roles(single_marker_recovery)) or 'none'} "
        f"dual={candidate_counts.get('dual_marker', 0)} "
        f"single_a={candidate_counts.get('single_marker_a', 0)} "
        f"single_b={candidate_counts.get('single_marker_b', 0)} "
        f"adaptive_continuity_accepted="
        f"{adaptive_continuity_application.get('accepted_frames', 0)}/"
        f"{adaptive_continuity_application.get('candidate_frames', 0)} "
        f"continuity_accepted={continuity_application.get('accepted_frames', 0)}/"
        f"{continuity_application.get('candidate_frames', 0)} "
        f"report={recovery_report_path}",
        flush=True,
    )
    filled_by_frame: dict[int, tuple[float, float, float]] = {}
    filled_source_by_frame: dict[int, str] = {}
    filled_donor_by_frame: dict[int, int] = {}
    segments_without_detection: list[int] = []
    segment_reports: list[dict] = []
    for active_range in active_ranges:
        segment_fill_statistics: dict | None = None
        measured_count = sum(measurements[index]["valid"] for index in active_range.frame_indices)
        dual_marker_count = sum(
            measurements[index]["dual_marker_valid"]
            for index in active_range.frame_indices
        )
        single_marker_count = sum(
            measurements[index]["single_marker_valid"]
            for index in active_range.frame_indices
        )
        if measured_count == 0:
            segments_without_detection.append(active_range.segment_index)
        for index in active_range.frame_indices:
            item = measurements[index]
            if item["valid"]:
                filled_by_frame[index] = (
                    float(item["distance"]),
                    float(item["distance_m"]),
                    float(item["distance_cm"]),
                )
                filled_source_by_frame[index] = str(item["measurement_source"])
                filled_donor_by_frame[index] = index
        if args.missing_policy == "segment_interp":
            filled_by_frame.update(
                interpolate_segment_measurements(
                    active_range.frame_indices,
                    measurements,
                    calibrated_unix_ms,
                )
            )
        elif args.missing_policy == "zero":
            for index in active_range.frame_indices:
                item = measurements[index]
                filled_by_frame[index] = (
                    0.0 if item["distance"] is None else float(item["distance"]),
                    0.0 if item["distance_m"] is None else float(item["distance_m"]),
                    0.0 if item["distance_cm"] is None else float(item["distance_cm"]),
                )
        elif args.missing_policy == "ffill":
            segment_filled, segment_fill_statistics = forward_fill_segment_measurements(
                active_range.frame_indices,
                measurements,
                calibrated_unix_ms,
            )
            filled_by_frame.update(segment_filled)
            direct_indices = [
                index
                for index in active_range.frame_indices
                if measurements[index]["valid"]
            ]
            if direct_indices:
                first_direct_idx = direct_indices[0]
                last_direct_idx = first_direct_idx
                for index in active_range.frame_indices:
                    if measurements[index]["valid"]:
                        last_direct_idx = index
                    filled_donor_by_frame[index] = (
                        first_direct_idx
                        if index < first_direct_idx
                        else last_direct_idx
                    )
        direct_positions = [
            position
            for position, index in enumerate(active_range.frame_indices)
            if measurements[index]["valid"]
        ]
        first_direct_position = min(direct_positions, default=None)
        for position, index in enumerate(active_range.frame_indices):
            if index not in filled_by_frame or index in filled_source_by_frame:
                continue
            if args.missing_policy == "ffill":
                filled_source_by_frame[index] = (
                    "segment_first_valid_backfill"
                    if first_direct_position is not None and position < first_direct_position
                    else "segment_previous_value_hold"
                )
            elif args.missing_policy == "segment_interp":
                filled_source_by_frame[index] = "segment_interpolation_or_endpoint_hold"
            elif args.missing_policy == "zero":
                filled_source_by_frame[index] = "explicit_zero_fill"
        segment_reports.append(
            {
                **active_range.as_dict(),
                "aruco_valid_frames": int(dual_marker_count),
                "aruco_valid_rate": float(dual_marker_count / len(active_range.frame_indices)),
                "single_marker_recovered_frames": int(single_marker_count),
                "usable_direct_width_frames": int(measured_count),
                "usable_direct_width_rate": float(
                    measured_count / len(active_range.frame_indices)
                ),
                "filled_frames": sum(index in filled_by_frame for index in active_range.frame_indices),
                "fill_statistics": segment_fill_statistics,
            }
        )

    continuous_quality = audit_continuous_gripper_quality(
        active_ranges,
        measurements,
        filled_by_frame,
        calibrated_unix_ms,
        args,
    )
    continuous_quality["single_marker_recovery"] = recovery_summary
    quality_report_path = csv_path.parent / "continuous_gripper_quality.json"
    atomic_write_json(quality_report_path, continuous_quality)
    if continuous_quality["status"] in {"partially_accepted", "rejected"}:
        summary = continuous_quality.get("summary") or {}
        print(
            "[gripper-quality] preserving segment-level evidence: "
            f"accepted={summary.get('accepted_segment_count', 0)} "
            f"rejected={summary.get('rejected_segment_count', 0)}; "
            "downstream conversion must discard every rejected source segment; "
            f"report={quality_report_path}",
            flush=True,
        )
    elif continuous_quality.get("warnings"):
        summary = continuous_quality.get("summary") or {}
        print(
            "[gripper-quality] all segments accepted with segment-local holds: "
            f"segments={summary.get('accepted_segment_count', 0)} "
            f"real_frames={summary.get('finite_real_detection_frames', 0)} "
            f"imputed_frames={summary.get('imputed_width_frames', 0)} "
            f"max_hold_ms={summary.get('maximum_forward_hold_ms', 0.0):.3f} "
            f"warnings={summary.get('warning_count', 0)}; "
            f"report={quality_report_path}",
            flush=True,
        )

    temp_path = csv_path.with_name(f".{csv_path.name}.tmp.{os.getpid()}")
    valid_count = sum(item["valid"] for item in measurements.values())
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=gripper_fieldnames())
            writer.writeheader()
            for frame_idx, timestamp_row in enumerate(timestamps):
                raw_row = raw_rows.get(frame_idx)
                item = measurements.get(frame_idx)
                valid = bool(item and item["valid"])
                a = None if raw_row is None else raw_row["a"]
                b = None if raw_row is None else raw_row["b"]
                filled = filled_by_frame.get(frame_idx)
                distance_m = None if item is None else item["distance_m"]
                distance_cm = None if item is None else item["distance_cm"]
                distance_m, distance_cm, direct_deadzone_zeroed = apply_calibrated_width_deadzone(
                    distance_m, distance_cm, args
                )
                filled_deadzone_zeroed = False
                if filled is not None:
                    filled_m, filled_cm, filled_deadzone_zeroed = apply_calibrated_width_deadzone(
                        filled[1], filled[2], args
                    )
                    filled = (filled[0], float(filled_m), float(filled_cm))
                filled_donor_idx = filled_donor_by_frame.get(frame_idx)
                filled_donor = (
                    None
                    if filled_donor_idx is None
                    else measurements.get(filled_donor_idx)
                )
                writer.writerow(
                    {
                        "frame_idx": timestamp_row["frame_idx"],
                        "timestamp_sec": timestamp_row["timestamp_sec"],
                        "marker_a": args.marker_a,
                        "marker_b": args.marker_b,
                        "a_x": "" if a is None else f"{a[0]:.3f}",
                        "a_y": "" if a is None else f"{a[1]:.3f}",
                        "b_x": "" if b is None else f"{b[0]:.3f}",
                        "b_y": "" if b is None else f"{b[1]:.3f}",
                        "marker_side_px": fmt(None if item is None else item["marker_side_px"], 6),
                        "px_to_meter_scale": fmt(None if item is None else item["px_to_meter"], 12),
                        "center_distance_cm": fmt(distance_cm, 6),
                        "center_distance_cm_filled": fmt(None if filled is None else filled[2], 6),
                        "center_distance_px": fmt(None if item is None else item["distance"], 6),
                        "center_distance_px_filled": fmt(None if filled is None else filled[0], 6),
                        "center_distance_m": fmt(distance_m, 9),
                        "center_distance_m_filled": fmt(None if filled is None else filled[1], 9),
                        "distance_px": fmt(None if item is None else item["distance"], 6),
                        "distance_px_filled": fmt(None if filled is None else filled[0], 6),
                        "distance_m": fmt(distance_m, 9),
                        "distance_m_filled": fmt(None if filled is None else filled[1], 9),
                        "dual_marker_valid": int(
                            bool(item and item["dual_marker_valid"])
                        ),
                        "primary_dual_marker_valid": int(
                            bool(item and item["primary_dual_marker_valid"])
                        ),
                        "adaptive_dual_marker_valid": int(
                            bool(item and item["adaptive_dual_marker_valid"])
                        ),
                        "adaptive_retry_strategy": (
                            ""
                            if item is None
                            or item["adaptive_retry_strategy"] is None
                            else item["adaptive_retry_strategy"]
                        ),
                        "adaptive_retry_detector_strategy": (
                            ""
                            if item is None
                            or item["adaptive_retry_detector_strategy"] is None
                            else item["adaptive_retry_detector_strategy"]
                        ),
                        "adaptive_retry_threshold_constant": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_threshold_constant"],
                            3,
                        ),
                        "adaptive_retry_peer_delta_px": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_peer_delta_px"],
                            6,
                        ),
                        "adaptive_retry_roi_xyxy_px": (
                            ""
                            if item is None
                            or item["adaptive_retry_roi_xyxy_px"] is None
                            else ",".join(
                                str(int(value))
                                for value in item["adaptive_retry_roi_xyxy_px"]
                            )
                        ),
                        "adaptive_retry_center_error_px": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_center_error_px"],
                            6,
                        ),
                        "adaptive_retry_vertical_error_px": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_vertical_error_px"],
                            6,
                        ),
                        "adaptive_retry_midpoint_error_px": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_midpoint_error_px"],
                            6,
                        ),
                        "adaptive_retry_side_ratio": fmt(
                            None
                            if item is None
                            else item["adaptive_retry_side_ratio"],
                            6,
                        ),
                        "single_marker_valid": int(
                            bool(item and item["single_marker_valid"])
                        ),
                        "marker_mask": "" if item is None else item["marker_mask"],
                        "fallback_marker_id": (
                            ""
                            if item is None or item["single_marker_role"] is None
                            else (args.marker_a if item["single_marker_role"] == "a" else args.marker_b)
                        ),
                        "fallback_run_id": (
                            "" if item is None or item["fallback_run_id"] is None else item["fallback_run_id"]
                        ),
                        "continuity_pass": (
                            ""
                            if item is None or item["continuity_pass"] is None
                            else int(bool(item["continuity_pass"]))
                        ),
                        "calibration_clamped": int(
                            bool(item and item["calibration_clamped"])
                        ),
                        "filled_from_frame_idx": (
                            "" if filled_donor_idx is None else filled_donor_idx
                        ),
                        "filled_from_measurement_source": (
                            ""
                            if filled_donor is None
                            else filled_donor["measurement_source"]
                        ),
                        "filled_calibration_clamped": int(
                            bool(
                                filled_donor
                                and filled_donor["calibration_clamped"]
                            )
                        ),
                        "direct_deadzone_zeroed": int(direct_deadzone_zeroed),
                        "filled_deadzone_zeroed": int(filled_deadzone_zeroed),
                        "measurement_source": (
                            "" if item is None else item["measurement_source"]
                        ),
                        "filled_source": filled_source_by_frame.get(frame_idx, ""),
                        "valid": int(valid),
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        output_sha256 = sha256_file(temp_path)
        if pre_publish_check is not None:
            pre_publish_check()
        os.replace(temp_path, csv_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return valid_count, output_sha256, {
        "policy": args.missing_policy,
        "scope": "timestamp_segment_local",
        "inactive_frames_left_empty": len(timestamps) - len(requested_indices),
        "segments_without_detection": segments_without_detection,
        "segments": segment_reports,
        "single_marker_recovery": recovery_summary,
        "single_marker_recovery_report": content_file_identity(recovery_report_path),
        "continuous_gripper_quality": continuous_quality,
        "continuous_gripper_quality_report": content_file_identity(quality_report_path),
    }


def run_detection_chunks(
    chunks: list[dict],
    workers: int,
    resume: bool,
    chunk_retries: int,
) -> tuple[int, int]:
    completed = 0
    pending = []
    for task in chunks:
        manifest = (
            validate_completed_chunk(
                task,
                allow_ignored_front_cache=bool(
                    task.get("ignore_published_front_cache", False)
                ),
            )
            if resume
            else None
        )
        if manifest is not None:
            completed += 1
            print(
                f"[resume] chunk {task['chunk_index'] + 1}/{len(chunks)} "
                f"frames={task['global_start']}:{task['global_end']}",
                flush=True,
            )
        else:
            pending.append(task)

    if not pending:
        return completed, 0
    if workers <= 1:
        for index, task in enumerate(pending, start=1):
            detect_chunk_with_retries(task, chunk_retries)
            print(
                f"[progress] completed chunk {index}/{len(pending)} "
                f"frames={task['global_start']}:{task['global_end']}",
                flush=True,
            )
    else:
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(workers, len(pending)), mp_context=context, initializer=_worker_setup
        ) as pool:
            futures = {
                pool.submit(detect_chunk_with_retries, task, chunk_retries): task
                for task in pending
            }
            done = 0
            try:
                for future in concurrent.futures.as_completed(futures):
                    task = futures[future]
                    future.result()
                    done += 1
                    print(
                        f"[progress] completed chunk {done}/{len(pending)} "
                        f"frames={task['global_start']}:{task['global_end']}",
                        flush=True,
                    )
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    for task in chunks:
        if validate_completed_chunk(
            task,
            allow_ignored_front_cache=bool(
                task.get("ignore_published_front_cache", False)
            ),
        ) is None:
            raise RuntimeError(f"chunk failed post-write validation: {task['chunk_index']}")
    return completed, len(pending)


def estimate(args: argparse.Namespace) -> dict:
    validate_estimation_mode_contract(args)
    if not hasattr(args, "front_openpi_cache"):
        args.front_openpi_cache = False
    if not hasattr(args, "chunk_retries"):
        args.chunk_retries = 2
    if not hasattr(args, "rotational_total_workers"):
        args.rotational_total_workers = 4
    episode_root = args.tmp_root.expanduser().resolve() / args.episode
    raw_manifest_path = (args.raw_manifest or episode_root / "01_raw_extract" / "manifest.json").expanduser().resolve()
    raw_manifest_identity = content_file_identity(raw_manifest_path)
    raw = load_manifest(raw_manifest_path)
    if content_file_identity(raw_manifest_path) != raw_manifest_identity:
        raise RuntimeError(f"raw manifest changed while being read: {raw_manifest_path}")
    timestamps_path = Path(raw["timestamps_csv"]).expanduser().resolve()
    timestamps_identity = content_file_identity(timestamps_path)
    timestamps = read_timestamps(timestamps_path)
    if content_file_identity(timestamps_path) != timestamps_identity:
        raise RuntimeError(f"timestamp CSV changed while being read: {timestamps_path}")
    validate_timestamps(timestamps, timestamps_path)
    target_frames = min(len(timestamps), args.max_frames) if args.max_frames else len(timestamps)
    if target_frames <= 0:
        raise ValueError("--max-frames must be > 0")
    frame_timestamps_arg = getattr(args, "frame_timestamps", None)
    timestamp_log_arg = getattr(args, "timestamp_log", None)
    if (frame_timestamps_arg is None) != (timestamp_log_arg is None):
        raise ValueError("--frame-timestamps and --timestamp-log must be provided together")
    active_ranges: list[ActiveFrameRange] | None = None
    calibrated_timeline = None
    active_selection: dict | None = None
    frame_timestamps_path: Path | None = None
    timestamp_log_path: Path | None = None
    calibrated_timestamps_identity: dict | None = None
    timestamp_log_identity: dict | None = None
    if frame_timestamps_arg is not None:
        frame_timestamps_path = Path(frame_timestamps_arg).expanduser().resolve()
        timestamp_log_path = Path(timestamp_log_arg).expanduser().resolve()
        calibrated_timestamps_identity = content_file_identity(frame_timestamps_path)
        timestamp_log_identity = content_file_identity(timestamp_log_path)
        calibrated_timeline = load_calibrated_frame_timeline(frame_timestamps_path)
        if len(calibrated_timeline.frame_idx) != len(timestamps):
            raise ValueError(
                "calibrated timeline must retain every raw source frame: "
                f"calibrated={len(calibrated_timeline.frame_idx)}, raw={len(timestamps)}"
            )
        active_ranges = build_active_frame_ranges(
            calibrated_timeline,
            timestamp_log_path,
            segment_time_offset_sec=float(getattr(args, "segment_time_offset_sec", 0.0)),
            min_segment_frames=int(getattr(args, "min_segment_frames", 2)),
            frame_limit=target_frames,
        )
        if not active_ranges:
            raise RuntimeError("timestamp_log has no usable ranges on the calibrated camera timeline")
        active_selection = {
            "schema": "instaumi_active_calibrated_camera_ranges_v1",
            "calibrated_timestamps_identity": calibrated_timestamps_identity,
            "timestamp_log_identity": timestamp_log_identity,
            "calibration_id": calibrated_timeline.calibration_id,
            "segment_time_offset_sec": float(getattr(args, "segment_time_offset_sec", 0.0)),
            "min_segment_frames": int(getattr(args, "min_segment_frames", 2)),
            "source_frame_count": len(timestamps),
            "selected_frame_count": sum(len(item.frame_indices) for item in active_ranges),
            "segment_count": len(active_ranges),
            "ranges": [item.as_dict() for item in active_ranges],
        }
        if args.write_annotated:
            raise ValueError("--write-annotated is not supported with sparse active-frame selection")

    output_dir = episode_root / "03_gripper_estimation"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "gripper_width.csv"
    manifest_path = output_dir / "manifest.json"
    ffprobe_bin = resolve_ffprobe_bin(args.ffprobe_bin)
    if args.raw_insv_input:
        if args.video is not None:
            raise ValueError("--video cannot be combined with --raw-insv-input")
        if args.write_annotated:
            raise ValueError(
                "--write-annotated is not supported with ordered raw INSV chapters; "
                "use --no-raw-insv-input for that debug-only output"
            )
        video_path: Path | None = None
        sources, source_layout = discover_raw_insv_sources(
            raw,
            len(timestamps),
            ffprobe_bin,
        )
    else:
        video_path = (
            args.video or Path(raw["videos"]["front_fisheye"])
        ).expanduser().resolve()
        sources, source_layout = discover_video_sources(
            video_path,
            len(timestamps),
            ffprobe_bin,
        )
    source_set_fingerprint = object_fingerprint(
        {
            "canonical_identity": source_layout["canonical_identity"],
            "source_identities": [item["identity"] for item in sources],
            "source_kind": source_layout["kind"],
        }
    )
    effective_cache_root = args.fast_cache_root
    if args.write_annotated and not args.prepare_cache_only and args.fast_cache_root is not None:
        print("[warn] fast cache is disabled for sequential annotated-video output", flush=True)
        effective_cache_root = None
    sources, cache_dir, input_cache_mode = prepare_fast_cache(
        sources,
        effective_cache_root,
        source_set_fingerprint,
        # Programmatic callers created before this option existed retain the
        # historical copy semantics; the production CLI explicitly defaults
        # to the new safe auto policy.
        getattr(args, "input_cache_policy", "copy"),
    )
    if args.prepare_cache_only:
        return {
            "_prepare_cache_only": True,
            "video": None if video_path is None else str(video_path),
            "source_layout": source_layout,
            "source_set_fingerprint": source_set_fingerprint,
            "fast_cache_dir": None if cache_dir is None else str(cache_dir),
            "input_cache_mode": input_cache_mode,
            "active_frame_selection": active_selection,
        }

    split_source = bool(cache_dir is not None) if args.split_source is None else bool(args.split_source)
    chunk_plan = build_chunk_plan(
        sources,
        target_frames,
        ffprobe_bin,
        split_source=split_source,
        chunk_size_frames=args.chunk_size_frames,
        active_ranges=active_ranges,
    )
    detector_snapshot = detector_parameter_snapshot(args.dictionary)
    adaptive_retry_enabled = aruco_adaptive_retry_enabled(args)
    adaptive_retry_constant = float(
        getattr(args, "aruco_adaptive_thresh_constant", 15.0)
    )
    adaptive_retry_constants = adaptive_retry_threshold_constants(
        adaptive_retry_constant
    )
    adaptive_roi_specs = adaptive_roi_retry_specs(adaptive_retry_constant)
    adaptive_retry_distance_support_px = [
        float(getattr(args, "aruco_adaptive_min_distance_px", 100.0)),
        float(getattr(args, "aruco_adaptive_max_distance_px", 600.0)),
    ]
    adaptive_retry_peer_consistency_px = float(
        getattr(args, "aruco_adaptive_peer_consistency_px", 2.0)
    )
    decoder_runtime = pyav_decoder_runtime_snapshot()
    detection_parameters = {
        "algorithm": (
            "opencv_aruco_raw_expected_id_roi_cascade_retry_v4"
            if adaptive_retry_enabled
            else "opencv_aruco_raw_detection_v1"
        ),
        # This identity is deliberately detection-only: merge/quality changes
        # must not force another native-media decode. Source/detector/chunk
        # parameters below remain independently fingerprinted.
        "implementation_file": (
            DETECTION_IMPLEMENTATION_IDENTITY_V4
            if adaptive_retry_enabled
            else DETECTION_IMPLEMENTATION_IDENTITY_V1
        ),
        "detector": detector_snapshot,
        "marker_a": args.marker_a,
        "marker_b": args.marker_b,
        "decode_threads": args.decode_threads,
        "target_frames": target_frames,
        "source_set_fingerprint": source_set_fingerprint,
        "active_frame_selection": active_selection,
        "front_openpi_cache": bool(args.front_openpi_cache),
        "front_openpi_cache_recipe": FRONT_CACHE_RECIPE if args.front_openpi_cache else None,
        "decoder": {
            "backend": "pyav_pts_cfr_v1",
            "stream": "0:v:0",
            "pixel_format": "bgr24",
            "colorspace": "ITU-601",
            "interpolation": "bilinear",
            "compatibility": "byte-identical-to-established-opencv-videocapture",
            "runtime": decoder_runtime,
        },
        "chunks": [
            {
                key: task[key]
                for key in (
                    "source_index",
                    "source_identity",
                    "local_start",
                    "local_end",
                    "global_start",
                    "global_end",
                    "segment_index",
                    "requested_global_indices",
                    "keyframe_aligned",
                )
            }
            for task in chunk_plan
        ],
    }
    if adaptive_retry_enabled:
        detection_parameters["raw_row_schema"] = RAW_DETECTION_ROW_SCHEMA_V4
        detection_parameters["implementation_runtime"] = (
            adaptive_detection_runtime_identity()
        )
        # Exhaust the deterministic threshold cascade before falling back to
        # single-marker inference or fill. With no primary peer, both expected
        # IDs must come from the same retry pass.
        detection_parameters["adaptive_retry"] = {
            "trigger": "primary_did_not_return_unique_expected_pair",
            "exactly_one_primary_roi_cascade": adaptive_roi_specs,
            "roi_geometry": {
                "prediction": "reflect_primary_peer_about_frame_center_x",
                "half_width_px": 140,
                "half_height_px": 110,
            },
            "roi_strict_gates": {
                "unique_missing_expected_id": True,
                "maximum_center_error_px": 35.0,
                "maximum_vertical_error_px": 25.0,
                "maximum_midpoint_x_error_px": 17.5,
                "minimum_marker_side_ratio": 0.65,
                "maximum_optional_peer_disagreement_px": 8.0,
            },
            "roi_failure_fallback": "ordered_full_frame_threshold_cascade",
            "detectors": [
                detector_parameter_snapshot(
                    args.dictionary,
                    adaptive_thresh_constant=constant,
                )
                for constant in adaptive_retry_constants
            ],
            "adaptive_thresh_constants": adaptive_retry_constants,
            "cascade_policy": "ordered_stop_at_first_strict_expected_pair",
            "distance_support_px": adaptive_retry_distance_support_px,
            "peer_consistency_px": adaptive_retry_peer_consistency_px,
            "uniqueness": (
                "unique_missing_id_with_optional_consistent_primary_peer_or_"
                "both_expected_ids_unique_in_one_retry_pass"
            ),
            "acceptance": (
                "strict_expected_pair_then_independent_primary_dual_geometry_validation"
            ),
        }
    detection_fingerprint = object_fingerprint(detection_parameters)
    legacy_front_detection_parameters = {
        **detection_parameters,
        "front_openpi_cache": True,
        "front_openpi_cache_recipe": FRONT_CACHE_RECIPE,
    }
    legacy_front_detection_fingerprint = object_fingerprint(
        legacy_front_detection_parameters
    )
    work_parent = cache_dir / "work" if cache_dir is not None else output_dir / ".chunks"
    work_dir = (
        work_parent / detection_fingerprint
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    for task in chunk_plan:
        task.update(
            {
                "dictionary": args.dictionary,
                "marker_a": args.marker_a,
                "marker_b": args.marker_b,
                "decode_threads": args.decode_threads,
                "detection_fingerprint": detection_fingerprint,
                "raw_row_schema": (
                    RAW_DETECTION_ROW_SCHEMA_V4 if adaptive_retry_enabled else None
                ),
                "adaptive_retry_constants": (
                    adaptive_retry_constants if adaptive_retry_enabled else None
                ),
                "adaptive_roi_retry_specs": (
                    adaptive_roi_specs if adaptive_retry_enabled else None
                ),
                "adaptive_retry_distance_support_px": (
                    adaptive_retry_distance_support_px
                    if adaptive_retry_enabled
                    else None
                ),
                "adaptive_retry_peer_consistency_px": (
                    adaptive_retry_peer_consistency_px
                    if adaptive_retry_enabled
                    else None
                ),
            }
        )
        task["task_fingerprint"] = chunk_task_fingerprint(
            task,
            detection_fingerprint,
            front_openpi_cache=bool(args.front_openpi_cache),
        )
        stem = f"chunk_{task['chunk_index']:05d}_{task['global_start']:09d}_{task['global_end']:09d}"
        task["raw_path"] = str(work_dir / f"{stem}.jsonl")
        task["chunk_manifest_path"] = str(work_dir / f"{stem}.manifest.json")
        task["front_cache_path"] = (
            str(work_dir / f"{stem}.front_openpi_224.npy")
            if args.front_openpi_cache
            else None
        )

    adopted_legacy_front_cache_chunks = 0
    if args.resume and not args.front_openpi_cache:
        adopted_legacy_front_cache_chunks = adopt_legacy_front_cache_detection_chunks(
            chunk_plan,
            work_parent / legacy_front_detection_fingerprint,
            legacy_front_detection_fingerprint,
        )
        if adopted_legacy_front_cache_chunks:
            print(
                "[resume] reused validated ArUco JSONL from legacy front-cache fan-out "
                f"chunks={adopted_legacy_front_cache_chunks}/{len(chunk_plan)}; "
                "large .npy files were not read",
                flush=True,
            )

    if args.front_openpi_cache:
        predicted_bytes = sum(
            len(task["requested_global_indices"]) * 224 * 224 * 3 + 4096
            for task in chunk_plan
        )
        reserve_bytes = int(
            float(getattr(args, "front_cache_reserve_gb", 0.0)) * (1024**3)
        )
        free_bytes = shutil.disk_usage(work_dir).free
        if free_bytes < predicted_bytes + reserve_bytes:
            raise RuntimeError(
                "insufficient fast-workspace space for sparse OpenPI front cache: "
                f"free={free_bytes} predicted={predicted_bytes} reserve={reserve_bytes} "
                f"root={work_dir}"
            )
        print(
            "[front-cache] preflight "
            f"frames={sum(len(task['requested_global_indices']) for task in chunk_plan)} "
            f"predicted_bytes={predicted_bytes} free_bytes={free_bytes} "
            f"reserve_bytes={reserve_bytes}",
            flush=True,
        )

    calibration_identity = (
        None if args.gripper_calibration_json is None else content_file_identity(args.gripper_calibration_json)
    )
    gripper_calibration = (
        load_gripper_calibration(args.gripper_calibration_json) if args.gripper_calibration_json is not None else None
    )
    if args.gripper_calibration_json is not None and content_file_identity(args.gripper_calibration_json) != calibration_identity:
        raise RuntimeError(f"gripper calibration changed while being read: {args.gripper_calibration_json}")
    run_parameters = {
        "algorithm": (
            "ordered_sparse_merge_per_marker_held_out_recovery_segment_local_fill_v3"
            if active_ranges is not None
            else "ordered_raw_merge_global_fill_v1"
        ),
        "detection_fingerprint": detection_fingerprint,
        "timestamps_identity": timestamps_identity,
        "raw_manifest_identity": raw_manifest_identity,
        "calibration_identity": calibration_identity,
        "marker_width_m": args.marker_width_m,
        "px_to_meter_scale": args.px_to_meter_scale,
        "calibration_distance_px": args.calibration_distance_px,
        "calibration_distance_m": args.calibration_distance_m,
        "gripper_zero_below_m": getattr(args, "gripper_zero_below_m", None),
        "missing_policy": args.missing_policy,
        "single_marker_recovery": single_marker_recovery_config(args),
        "adaptive_dual_promotion": adaptive_dual_promotion_contract(args),
        "continuous_gripper_quality": continuous_gripper_quality_config(args),
        "write_annotated": args.write_annotated,
        "target_frames": target_frames,
        "active_frame_selection": active_selection,
        "front_openpi_cache": bool(args.front_openpi_cache),
        "front_openpi_cache_recipe": FRONT_CACHE_RECIPE if args.front_openpi_cache else None,
    }
    run_fingerprint = object_fingerprint(run_parameters)

    if args.resume and manifest_path.is_file():
        try:
            existing = load_manifest(manifest_path)
            if (
                existing.get("schema") == MANIFEST_SCHEMA
                and existing.get("run_fingerprint") == run_fingerprint
                and int(existing.get("processed_frames", -1)) == target_frames
                and validate_final_csv(csv_path, target_frames, existing.get("output_csv_sha256"))
                and (
                    not args.front_openpi_cache
                    or validate_front_openpi_cache_summary(existing.get("front_openpi_cache"))
                )
                and (
                    not args.write_annotated
                    or (
                        existing.get("annotated_video") is not None
                        and Path(existing["annotated_video"]).is_file()
                        and Path(existing["annotated_video"]).stat().st_size > 0
                    )
                )
            ):
                print(f"[skip] validated complete output: {csv_path}", flush=True)
                existing["_preserve_manifest"] = True
                return existing
        except Exception:
            pass

    if args.write_annotated:
        print("[info] --write-annotated uses the canonical sequential decoder to preserve annotation order", flush=True)
        result = estimate_with_annotations(args)
        result.update(
            {
                "run_fingerprint": run_fingerprint,
                "detection_fingerprint": detection_fingerprint,
                "output_csv_sha256": sha256_file(csv_path),
                "source_layout": source_layout,
                "raw_manifest_identity": raw_manifest_identity,
                "execution": {"mode": "sequential_annotated", "workers": 1, "chunk_count": 1},
            }
        )
        return result

    cpu_count = os.cpu_count() or 1
    workers = args.workers
    source_storage = inspect_storage(sources[0]["decode_path"])
    if workers == 0:
        workers = max(1, cpu_count // max(1, args.concurrent_estimators))
        if source_storage.rotational:
            # The value is a total across concurrently running arm estimators.
            # On a rotational disk, many independent HEVC random readers are
            # slower and can trigger FFmpeg interrupt timeouts. Keep aggregate
            # I/O concurrency bounded while CPU quota remains independent.
            per_estimator_io_limit = max(
                1,
                args.rotational_total_workers // max(1, args.concurrent_estimators),
            )
            workers = min(workers, per_estimator_io_limit)
    workers = min(workers, len(chunk_plan))
    print(
        f"[info] source_layout={source_layout['kind']} resolution="
        f"{source_layout['canonical_probe']['width']}x{source_layout['canonical_probe']['height']} "
        f"chunks={len(chunk_plan)} workers={workers} decode_threads={args.decode_threads} "
        f"split_source={int(split_source)} decoder=pyav_pts "
        f"storage_rotational={int(source_storage.rotational)} "
        f"chunk_retries={args.chunk_retries}",
        flush=True,
    )
    started = time.monotonic()
    resumed_chunks, executed_chunks = run_detection_chunks(
        chunk_plan,
        workers,
        args.resume,
        args.chunk_retries,
    )

    def validate_inputs_unchanged() -> None:
        for source in sources:
            if sampled_file_identity(Path(source["original_path"])) != source["identity"]:
                raise RuntimeError(f"source video changed during detection: {source['original_path']}")
        if content_file_identity(timestamps_path) != timestamps_identity:
            raise RuntimeError(f"timestamp CSV changed during detection: {timestamps_path}")
        if content_file_identity(raw_manifest_path) != raw_manifest_identity:
            raise RuntimeError(f"raw manifest changed during detection: {raw_manifest_path}")
        if (
            frame_timestamps_path is not None
            and content_file_identity(frame_timestamps_path) != calibrated_timestamps_identity
        ):
            raise RuntimeError(f"calibrated frame timeline changed during detection: {frame_timestamps_path}")
        if timestamp_log_path is not None and content_file_identity(timestamp_log_path) != timestamp_log_identity:
            raise RuntimeError(f"timestamp log changed during detection: {timestamp_log_path}")
        if (
            args.gripper_calibration_json is not None
            and content_file_identity(args.gripper_calibration_json) != calibration_identity
        ):
            raise RuntimeError(f"gripper calibration changed during detection: {args.gripper_calibration_json}")

    validate_inputs_unchanged()
    valid_count, output_sha256, fill_report = write_merged_csv(
        csv_path,
        timestamps[:target_frames],
        chunk_plan,
        gripper_calibration,
        args,
        active_ranges=active_ranges,
        calibrated_unix_ms=(
            None
            if calibrated_timeline is None
            else calibrated_timeline.unix_ms[:target_frames]
        ),
        pre_publish_check=validate_inputs_unchanged,
    )
    if not validate_final_csv(csv_path, target_frames, output_sha256):
        raise RuntimeError(f"final gripper CSV failed strict validation: {csv_path}")
    elapsed = time.monotonic() - started
    front_openpi_cache = (
        build_front_openpi_cache_summary(chunk_plan)
        if args.front_openpi_cache
        else None
    )
    if front_openpi_cache is not None:
        front_openpi_cache["detection_fingerprint"] = detection_fingerprint
        front_openpi_cache["source_set_fingerprint"] = source_set_fingerprint
    return {
        "video": None if video_path is None else str(video_path),
        "output_csv": str(csv_path),
        "output_csv_sha256": output_sha256,
        "annotated_video": None,
        "processed_frames": target_frames,
        "valid_frames": valid_count,
        "missing_policy": args.missing_policy,
        "marker_width_m": args.marker_width_m,
        "calibration": calibration_summary(gripper_calibration, args),
        "run_fingerprint": run_fingerprint,
        "detection_fingerprint": detection_fingerprint,
        "source_set_fingerprint": source_set_fingerprint,
        "source_layout": source_layout,
        "active_frame_selection": active_selection,
        "fill_report": fill_report,
        "front_openpi_cache": front_openpi_cache,
        "raw_manifest_identity": raw_manifest_identity,
        "execution": {
            "mode": "deterministic_keyframe_chunks",
            "workers": workers,
            "decode_threads_per_worker": args.decode_threads,
            "decoder": {
                "backend": "pyav_pts_cfr_v1",
                "runtime": decoder_runtime,
            },
            "source_storage": {
                "rotational": source_storage.rotational,
                "confident": source_storage.confident,
                "reason": source_storage.reason,
            },
            "chunk_retries": args.chunk_retries,
            "split_source": split_source,
            "chunk_size_frames": args.chunk_size_frames,
            "chunk_count": len(chunk_plan),
            "resumed_chunks": resumed_chunks,
            "executed_chunks": executed_chunks,
            "reused_legacy_front_cache_chunks": adopted_legacy_front_cache_chunks,
            "work_dir": str(work_dir),
            "fast_cache_dir": None if cache_dir is None else str(cache_dir),
            "input_cache_mode": input_cache_mode,
            "elapsed_sec": elapsed,
            "requested_detection_frames": sum(
                len(task["requested_global_indices"]) for task in chunk_plan
            ),
            "decoded_frames_including_keyframe_warmup": sum(
                int(task["local_end"]) - int(task["local_start"]) for task in chunk_plan
            ),
        },
        "validation": {
            "timestamp_indices_continuous": True,
            "source_frame_count": len(timestamps),
            "output_frame_count": target_frames,
            "ordered_merge": True,
            "global_missing_fill": active_ranges is None,
            "segment_local_missing_fill": active_ranges is not None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="InstaUMI 第 3 阶段：夹爪宽度估计")
    parser.add_argument("--episode", required=True)
    parser.add_argument("--tmp-root", type=Path, default=DEFAULT_TMP_ROOT)
    parser.add_argument("--raw-manifest", type=Path, default=None)
    parser.add_argument(
        "--frame-timestamps",
        type=Path,
        default=None,
        help="Full calibrated source-frame timeline used to select timestamp_log ranges.",
    )
    parser.add_argument(
        "--timestamp-log",
        type=Path,
        default=None,
        help="Capture timestamp_log containing segment_start/segment_end events.",
    )
    parser.add_argument("--segment-time-offset-sec", type=float, default=0.0)
    parser.add_argument("--min-segment-frames", type=int, default=2)
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument(
        "--raw-insv-input",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Decode ordered copied_sources.insv stream 0 chapters directly instead of "
            "requiring the _01 front_fisheye stream-copy MP4."
        ),
    )
    parser.add_argument("--dictionary", default="DICT_4X4_50")
    parser.add_argument("--marker-a", type=int, default=1)
    parser.add_argument("--marker-b", type=int, default=2)
    parser.add_argument("--marker-width-m", type=float, default=0.018, help="单个 ArUco marker 的实际边长，默认 18mm")
    parser.add_argument(
        "--gripper-calibration-json",
        type=Path,
        default=DEFAULT_GRIPPER_CALIBRATION_JSON,
        help="像素距离到夹爪开口的标定 JSON，默认使用 calibration/gripper_calibration 生成的 config/gripper_calibration.json",
    )
    parser.add_argument("--px-to-meter-scale", type=float, default=None, help="固定像素到米比例；设置后覆盖 marker 边长估计")
    parser.add_argument("--calibration-distance-px", type=float, default=None, help="一段已知宽度的像素距离，用于计算固定 px->m")
    parser.add_argument("--calibration-distance-m", type=float, default=0.10, help="配合 --calibration-distance-px 使用的真实距离，默认 10cm")
    parser.add_argument(
        "--gripper-zero-below-m",
        type=float,
        default=None,
        help=(
            "Fold/task-specific calibrated-width dead zone: write finite widths "
            "strictly below this many metres as zero. Disabled by default."
        ),
    )
    parser.add_argument(
        "--missing-policy",
        choices=["empty", "ffill", "zero", "segment_interp"],
        default="ffill",
    )
    parser.add_argument(
        "--aruco-adaptive-retry",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "When the native-resolution primary detector sees exactly one expected "
            "marker, exhaust the deterministic native-ROI multiscale cascade and then "
            "the full-frame adaptive-threshold cascade for the other. "
            "When it sees neither, a single retry pass must find both configured IDs. "
            "All recovered pairs still pass an independent primary-dual geometry model."
        ),
    )
    parser.add_argument(
        "--aruco-adaptive-thresh-constant",
        type=float,
        default=15.0,
        help=(
            "Base for the fingerprinted ROI/full-frame retry recipe; the default full-"
            "frame cascade is [23,15,19,11], while the ROI passes use the documented "
            "scale/interpolation/gamma combinations derived from the same base."
        ),
    )
    parser.add_argument("--aruco-adaptive-min-distance-px", type=float, default=100.0)
    parser.add_argument("--aruco-adaptive-max-distance-px", type=float, default=600.0)
    parser.add_argument(
        "--aruco-adaptive-peer-consistency-px",
        type=float,
        default=2.0,
        help=(
            "Maximum center disagreement for the already-visible peer marker between "
            "the primary and full-frame adaptive detectors. The measured ROI cascade "
            "uses its fingerprinted 8 px bound because resampling shifts corners."
        ),
    )
    parser.add_argument(
        "--aruco-adaptive-long-run-diagnostic-ms",
        "--aruco-adaptive-max-run-duration-ms",
        dest="aruco_adaptive_max_run_duration_ms",
        type=float,
        default=250.0,
        help=(
            "Diagnostic threshold for reporting long promoted adaptive-dual runs. "
            "It never rejects a run; independently validated frame geometry, "
            "calibration support, internal continuity, and any available primary "
            "boundary anchors remain the safety gates. The old --max-run name is "
            "accepted as a compatibility alias."
        ),
    )
    parser.add_argument(
        "--single-marker-recovery",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Recover a physical width from exactly one expected marker only after "
            "arm/session-specific episode-held-out validation against dual-marker frames."
        ),
    )
    parser.add_argument("--single-marker-min-dual-frames", type=int, default=1000)
    parser.add_argument("--single-marker-min-dual-segments", type=int, default=10)
    parser.add_argument(
        "--single-marker-cv-folds",
        type=int,
        default=0,
        help="0 means strict leave-one-episode-out validation.",
    )
    parser.add_argument("--single-marker-max-cv-p95-m", type=float, default=0.004)
    parser.add_argument("--single-marker-max-cv-p99-m", type=float, default=0.005)
    parser.add_argument("--single-marker-max-cv-error-m", type=float, default=0.006)
    parser.add_argument("--single-marker-max-cv-bias-m", type=float, default=0.0015)
    parser.add_argument(
        "--single-marker-min-cv-coverage",
        type=float,
        default=0.99,
        help=(
            "Diagnostic held-out ROI coverage threshold. Accuracy is authorized "
            "per marker by physical-width error gates; an actual frame still has "
            "to lie inside the validated ROI and calibration support."
        ),
    )
    parser.add_argument(
        "--single-marker-min-cv-predictions-per-role", type=int, default=1000
    )
    parser.add_argument(
        "--single-marker-min-cv-episodes-per-role", type=int, default=10
    )
    parser.add_argument("--single-marker-max-episode-p95-m", type=float, default=0.006)
    parser.add_argument(
        "--single-marker-max-midpoint-p95-px",
        type=float,
        default=8.0,
        help="Diagnostic only; held-out per-role width error is the authorization gate.",
    )
    parser.add_argument(
        "--single-marker-max-axis-p95-deg",
        type=float,
        default=1.0,
        help="Diagnostic only; held-out per-role width error is the authorization gate.",
    )
    parser.add_argument("--single-marker-min-distance-px", type=float, default=100.0)
    parser.add_argument("--single-marker-max-distance-px", type=float, default=600.0)
    parser.add_argument("--single-marker-max-axis-deviation-deg", type=float, default=5.0)
    parser.add_argument("--single-marker-roi-margin-px", type=float, default=5.0)
    parser.add_argument("--single-marker-midpoint-min-gate-px", type=float, default=25.0)
    parser.add_argument("--single-marker-continuity-base-m", type=float, default=0.005)
    parser.add_argument("--single-marker-continuity-slew-mps", type=float, default=0.1)
    parser.add_argument("--single-marker-anchor-horizon-ms", type=float, default=250.0)
    parser.add_argument("--single-marker-max-timeline-step-ms", type=float, default=50.0)
    parser.add_argument(
        "--continuous-gripper-quality-audit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Publish per-segment fill telemetry and accepted/rejected evidence. Detection "
            "gaps are warnings and are filled inside the segment; only absent finite "
            "evidence or invalid widths reject a segment."
        ),
    )
    parser.add_argument("--quality-min-detections-per-segment", type=int, default=1)
    parser.add_argument("--quality-max-internal-gap-ms", type=float, default=500.0)
    parser.add_argument("--quality-max-edge-extrapolation-ms", type=float, default=250.0)
    parser.add_argument("--quality-min-width-m", type=float, default=0.0)
    parser.add_argument("--quality-max-width-m", type=float, default=0.11)
    parser.add_argument("--constant-zero", action="store_true", help="不检测 ArUco，直接输出逐帧 0 夹爪宽度占位")
    parser.add_argument("--write-annotated", action="store_true")
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--progress-every", type=int, default=200)
    parser.add_argument("--workers", type=int, default=0, help="检测 worker 数；0 按 CPU 和并发臂数自动分配")
    parser.add_argument(
        "--concurrent-estimators",
        type=int,
        default=1,
        help="同时运行的手臂估计器数量，用于 workers=0 时避免 CPU 过订阅",
    )
    parser.add_argument("--decode-threads", type=int, default=1, help="每个 worker 的 FFmpeg 解码线程数")
    parser.add_argument(
        "--chunk-retries",
        type=int,
        default=2,
        help="瞬时解码失败时整块原子重试次数；不会跳帧或发布部分结果",
    )
    parser.add_argument(
        "--rotational-total-workers",
        type=int,
        default=4,
        help="workers=0 且源位于机械盘时，所有并发手臂合计的最大解码 worker 数",
    )
    parser.add_argument("--chunk-size-frames", type=int, default=7200, help="关键帧对齐的目标分块长度，默认约 2 分钟")
    parser.add_argument(
        "--split-source",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="是否在原生章节内部按关键帧继续分块；默认仅在启用快速缓存时开启",
    )
    parser.add_argument("--fast-cache-root", type=Path, default=None, help="可选快速工作根目录；正式输入和输出不会被覆盖")
    parser.add_argument(
        "--input-cache-policy",
        choices=("auto", "copy", "direct"),
        default="auto",
        help=(
            "Raw INSV input staging policy. auto avoids a byte-identical copy when "
            "source and cache share one physical device; chunking, decoder and output "
            "validation remain unchanged."
        ),
    )
    parser.add_argument(
        "--front-openpi-cache",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "During the same native 1920x1920 decode used by ArUco, persist sparse "
            "lossless uint8 RGB 224x224 OpenPI fronts for direct reuse by _05."
        ),
    )
    parser.add_argument(
        "--front-cache-reserve-gb",
        type=float,
        default=0.0,
        help="Free-space reserve retained after allocating the sparse RGB224 cache.",
    )
    parser.add_argument("--ffprobe-bin", default=None)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="校验并复用完整 chunk/最终输出")
    parser.add_argument("--prepare-cache-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if Path(args.episode).name != args.episode or args.episode in {".", ".."}:
        raise ValueError(f"--episode must be one directory name, got: {args.episode!r}")
    if args.workers < 0:
        raise ValueError("--workers must be >= 0")
    if args.concurrent_estimators <= 0:
        raise ValueError("--concurrent-estimators must be > 0")
    if args.decode_threads <= 0:
        raise ValueError("--decode-threads must be > 0")
    if args.chunk_retries < 0:
        raise ValueError("--chunk-retries must be >= 0")
    if args.rotational_total_workers <= 0:
        raise ValueError("--rotational-total-workers must be > 0")
    if args.chunk_size_frames <= 0:
        raise ValueError("--chunk-size-frames must be > 0")
    if args.min_segment_frames <= 0:
        raise ValueError("--min-segment-frames must be > 0")
    if args.front_cache_reserve_gb < 0:
        raise ValueError("--front-cache-reserve-gb must be >= 0")
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be > 0")
    if args.gripper_zero_below_m is not None:
        if not math.isfinite(args.gripper_zero_below_m) or args.gripper_zero_below_m <= 0.0:
            raise ValueError("--gripper-zero-below-m must be a finite positive value")
    if (
        not math.isfinite(args.aruco_adaptive_thresh_constant)
        or args.aruco_adaptive_thresh_constant <= 0.0
    ):
        raise ValueError("--aruco-adaptive-thresh-constant must be finite and > 0")
    if not (
        math.isfinite(args.aruco_adaptive_min_distance_px)
        and math.isfinite(args.aruco_adaptive_max_distance_px)
        and 0.0
        < args.aruco_adaptive_min_distance_px
        < args.aruco_adaptive_max_distance_px
    ):
        raise ValueError("adaptive ArUco retry distance support is invalid")
    if (
        not math.isfinite(args.aruco_adaptive_peer_consistency_px)
        or args.aruco_adaptive_peer_consistency_px <= 0.0
    ):
        raise ValueError("--aruco-adaptive-peer-consistency-px must be finite and > 0")
    if (
        not math.isfinite(args.aruco_adaptive_max_run_duration_ms)
        or args.aruco_adaptive_max_run_duration_ms <= 0.0
    ):
        raise ValueError(
            "--aruco-adaptive-long-run-diagnostic-ms must be finite and > 0"
        )
    quality = continuous_gripper_quality_config(args)
    recovery = single_marker_recovery_config(args)
    for key in (
        "minimum_dual_marker_frames",
        "minimum_dual_marker_segments",
        "cross_validation_folds",
        "minimum_cv_predictions_per_role",
        "minimum_cv_episodes_per_role",
    ):
        if key == "cross_validation_folds":
            if recovery[key] == 1 or recovery[key] < 0:
                raise ValueError("single marker cross_validation_folds must be 0 or >= 2")
            continue
        if recovery[key] < 1:
            raise ValueError(f"single marker recovery {key} is too small")
    for key in (
        "maximum_cv_p95_error_m",
        "maximum_cv_p99_error_m",
        "maximum_cv_error_m",
        "maximum_cv_absolute_bias_m",
        "maximum_episode_p95_error_m",
        "maximum_midpoint_episode_p95_px",
        "maximum_axis_episode_p95_deg",
        "maximum_axis_deviation_deg",
        "marker_roi_margin_px",
        "midpoint_translation_min_gate_px",
        "continuity_base_tolerance_m",
        "continuity_max_slew_m_per_sec",
        "continuity_anchor_horizon_ms",
        "continuity_max_timeline_step_ms",
    ):
        if not math.isfinite(recovery[key]) or recovery[key] <= 0.0:
            raise ValueError(f"single marker recovery {key} must be finite and > 0")
    if not 0.0 < recovery["minimum_cv_coverage"] <= 1.0:
        raise ValueError("single marker recovery minimum_cv_coverage must be in (0, 1]")
    if not (
        0.0 < recovery["clean_distance_range_px"][0]
        < recovery["clean_distance_range_px"][1]
    ):
        raise ValueError("single marker recovery distance support is invalid")
    if quality["min_real_detections_per_segment"] < 1:
        raise ValueError("--quality-min-detections-per-segment must be >= 1")
    for key in ("max_internal_gap_ms", "max_edge_extrapolation_ms"):
        if not math.isfinite(quality[key]) or quality[key] < 0.0:
            raise ValueError(f"continuous gripper quality {key} must be finite and >= 0")
    if (
        not math.isfinite(quality["min_width_m"])
        or not math.isfinite(quality["max_width_m"])
        or quality["min_width_m"] < 0.0
        or quality["min_width_m"] >= quality["max_width_m"]
    ):
        raise ValueError("continuous gripper quality width range is invalid")
    if quality["enabled"] and (args.frame_timestamps is None or args.timestamp_log is None):
        raise ValueError("continuous gripper quality audit requires calibrated active segments")
    if quality["enabled"] and args.write_annotated:
        raise ValueError("continuous gripper quality audit requires deterministic chunk mode")
    if args.front_openpi_cache and args.constant_zero:
        raise ValueError("--front-openpi-cache cannot be combined with --constant-zero")
    if args.front_openpi_cache and args.write_annotated:
        raise ValueError("--front-openpi-cache requires deterministic chunk mode")
    summary = write_constant_zero(args) if args.constant_zero else estimate(args)
    if summary.pop("_prepare_cache_only", False):
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    if summary.pop("_preserve_manifest", False):
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    output_dir = args.tmp_root.expanduser().resolve() / args.episode / "03_gripper_estimation"
    raw_manifest_path = (
        args.raw_manifest
        or args.tmp_root.expanduser().resolve() / args.episode / "01_raw_extract" / "manifest.json"
    ).expanduser().resolve()
    frozen_raw_manifest_identity = summary.get("raw_manifest_identity")
    if frozen_raw_manifest_identity != content_file_identity(raw_manifest_path):
        raise RuntimeError(f"raw manifest changed before _03 manifest commit: {raw_manifest_path}")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "implementation_schema": "instaumi_gripper_deterministic_chunks_v1",
        "episode": args.episode,
        "raw_manifest_identity": frozen_raw_manifest_identity,
        **summary,
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
