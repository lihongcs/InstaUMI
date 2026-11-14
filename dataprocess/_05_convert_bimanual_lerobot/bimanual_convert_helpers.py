"""Private helpers for the bimanual LeRobot converter.

This module intentionally does not import the single-arm converter. The
bimanual pipeline applies only fixed, stateless per-frame pose transforms.
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

from htc_to_flexiv_transform import compute_pose_transform as compute_htc_pose_transform


_CV2 = None


def natural_key(path: Path | str) -> list[object]:
    text = Path(path).name if not isinstance(path, str) else path
    parts: list[object] = []
    buffer = ""
    is_digit = False
    for char in text:
        char_is_digit = char.isdigit()
        if buffer and char_is_digit != is_digit:
            parts.append(int(buffer) if is_digit else buffer)
            buffer = ""
        buffer += char
        is_digit = char_is_digit
    if buffer:
        parts.append(int(buffer) if is_digit else buffer)
    return parts


def get_cv2():
    global _CV2
    if _CV2 is None:
        try:
            import cv2 as cv2_module
        except ImportError as exc:
            raise RuntimeError("OpenCV (cv2) is required to read MP4 files") from exc
        _CV2 = cv2_module
    return _CV2


def import_lerobot(lerobot_env: Path):
    if lerobot_env.exists():
        package_parent = lerobot_env.parent if lerobot_env.name == "lerobot" else lerobot_env
        sys.path.insert(0, str(package_parent))
    try:
        import datasets
        from lerobot.datasets.image_writer import safe_stop_image_writer
        from lerobot.datasets.lerobot_dataset import CODEBASE_VERSION, LeRobotDataset
        from lerobot.datasets.video_utils import VideoEncodingManager, encode_video_frames
    except ImportError as exc:
        raise RuntimeError(
            "LeRobot conversion dependencies are missing; run _05 in the configured LeRobot environment"
        ) from exc
    if CODEBASE_VERSION != "v2.1":
        raise RuntimeError(f"LeRobot codebase_version must be v2.1, got {CODEBASE_VERSION}")
    if datasets.__version__ != "3.6.0":
        raise RuntimeError(
            "Official pi0.5 LeRobot v2.1 compatibility requires datasets==3.6.0; "
            f"got datasets=={datasets.__version__}. Install the pinned version before converting."
        )
    return LeRobotDataset, VideoEncodingManager, encode_video_frames, safe_stop_image_writer, CODEBASE_VERSION


def quat_xyzw_to_rotvec(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    quaternion = np.array([qx, qy, qz, qw], dtype=np.float64)
    norm = float(np.linalg.norm(quaternion))
    if norm <= 0.0 or not np.isfinite(norm):
        return np.zeros(3, dtype=np.float32)
    qx, qy, qz, qw = (quaternion / norm).tolist()
    if qw < 0.0:
        qx, qy, qz, qw = -qx, -qy, -qz, -qw
    sin_half = math.sqrt(qx * qx + qy * qy + qz * qz)
    if sin_half < 1e-12:
        return np.zeros(3, dtype=np.float32)
    angle = 2.0 * math.atan2(sin_half, qw)
    return (np.array([qx, qy, qz], dtype=np.float64) / sin_half * angle).astype(np.float32)


def quat_xyzw_to_matrix(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    quaternion = np.array([qx, qy, qz, qw], dtype=np.float64)
    norm = float(np.linalg.norm(quaternion))
    if norm <= 0.0 or not np.isfinite(norm):
        return np.eye(3, dtype=np.float64)
    x, y, z, w = (quaternion / norm).tolist()
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def matrix_to_quat_xyzw(matrix: np.ndarray) -> np.ndarray:
    m = np.asarray(matrix, dtype=np.float64)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw, qx, qy, qz = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(max(1.0 + m[0, 0] - m[1, 1] - m[2, 2], 1e-12)) * 2.0
        qw, qx, qy, qz = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(max(1.0 + m[1, 1] - m[0, 0] - m[2, 2], 1e-12)) * 2.0
        qw, qx, qy, qz = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(max(1.0 + m[2, 2] - m[0, 0] - m[1, 1], 1e-12)) * 2.0
        qw, qx, qy, qz = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    quaternion = np.array([qx, qy, qz, qw], dtype=np.float64)
    norm = float(np.linalg.norm(quaternion))
    if norm <= 0.0 or not np.isfinite(norm):
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    quaternion /= norm
    return -quaternion if quaternion[3] < 0.0 else quaternion


def rotation_matrix(axis: str, degrees: float) -> np.ndarray:
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    if axis == "x":
        return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], dtype=np.float64)
    if axis == "y":
        return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], dtype=np.float64)
    if axis == "z":
        return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    raise ValueError(f"unknown rotation axis: {axis}")


def mat_to_pose6(matrix: np.ndarray) -> np.ndarray:
    quaternion = matrix_to_quat_xyzw(matrix[:3, :3])
    rotvec = quat_xyzw_to_rotvec(*quaternion)
    return np.concatenate((matrix[:3, 3], rotvec)).astype(np.float32)


def convert_sensor_pose_to_pose6(
    position: Iterable[float],
    quat_xyzw: Iterable[float],
    pose_sensor: str,
) -> np.ndarray:
    if pose_sensor not in {"htc", "insta"}:
        raise ValueError(f"Unsupported pose_sensor: {pose_sensor}")
    qx, qy, qz, qw = [float(value) for value in quat_xyzw]
    if pose_sensor == "htc":
        return compute_htc_pose_transform([*position, qx, qy, qz, qw])

    # Preserve the established Insta/SLAM coordinate conversion. HTC uses the
    # dedicated fixed Tracker-to-EEF transform above.
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = quat_xyzw_to_matrix(qx, qy, qz, qw)
    matrix[:3, 3] = np.asarray([float(value) for value in position], dtype=np.float64)
    matrix[:3, :3] = (
        matrix[:3, :3]
        @ rotation_matrix("y", 90.0)
        @ rotation_matrix("z", 90.0)
        @ rotation_matrix("z", -90.0)
        @ rotation_matrix("y", -15.0)
    )
    matrix[:3, 3] += np.array([-0.01822, 0.05416, 0.0541], dtype=np.float64)
    return mat_to_pose6(matrix)


def pose7_to_pose6(row: Iterable[float], pose_sensor: str) -> np.ndarray:
    x, y, z, qx, qy, qz, qw = [float(value) for value in row]
    return convert_sensor_pose_to_pose6(
        (x, y, z),
        (qx, qy, qz, qw),
        pose_sensor,
    )


def load_tum_trajectory(path: Path, pose_sensor: str) -> list[np.ndarray]:
    poses: list[np.ndarray] = []
    with path.open("r", encoding="ascii", errors="ignore") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 8:
                raise ValueError(f"{path}:{line_number} expected at least 8 TUM fields, got {len(parts)}")
            poses.append(pose7_to_pose6([float(value) for value in parts[1:8]], pose_sensor))
    return poses


def _parse_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if np.isfinite(parsed) else None


def _resolve_gripper_column(path: Path, fieldnames: list[str], requested: str) -> str:
    if requested != "auto":
        if requested not in fieldnames:
            raise ValueError(f"{path} does not contain gripper column {requested!r}")
        return requested
    for candidate in ("center_distance_m_filled", "distance_m_filled", "center_distance_m", "distance_m", "distance_px_filled", "distance_px"):
        if candidate in fieldnames:
            return candidate
    raise ValueError(f"{path} has no supported gripper column")


def load_gripper_widths(path: Path, column: str, missing_policy: str) -> list[float | None]:
    widths: list[float | None] = []
    last_valid: float | None = None
    with path.open("r", encoding="ascii", errors="ignore", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            return widths
        resolved_column = _resolve_gripper_column(path, reader.fieldnames, column)
        for row in reader:
            value = _parse_float(row.get(resolved_column))
            if value is None:
                if missing_policy == "skip":
                    widths.append(None)
                    continue
                if missing_policy == "zero":
                    value = 0.0
                elif missing_policy == "ffill":
                    value = 0.0 if last_valid is None else last_valid
                else:
                    raise ValueError(f"Unsupported missing gripper policy: {missing_policy}")
            last_valid = value
            widths.append(float(value))
    return widths


def make_state(pose6: np.ndarray, gripper_width: float) -> np.ndarray:
    return np.concatenate((pose6.astype(np.float32), np.array([gripper_width, 0.0, 0.0, 0.0], dtype=np.float32))).astype(np.float32)
