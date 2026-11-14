"""Datacap-owned HTC Tracker to Flexiv end-effector calibration.

The raw pyvut pose is kept intact by the recorder. This dependency-free module
is owned by the datacap live plot and mirrors the single production _05 transform.
Quaternions accepted and returned here use XYZW.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


Matrix3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]

# Keep these values identical to
# dataprocess/_05_convert_bimanual_lerobot/htc_to_flexiv_transform.py.
# This module is dependency-free because it is also packaged in the Ubuntu app.
HTC_WORLD_OFFSET_M: Vector3 = (0.01414, 0.05415, -0.05619)
HTC_OFFSET_MM: Vector3 = tuple(value * 1000.0 for value in HTC_WORLD_OFFSET_M)  # type: ignore[assignment]
TRANSFORM_NAME = "htc_to_flexiv_ry90_fixed_offset"


def rotation_matrix(axis: str, degrees: float) -> Matrix3:
    angle = math.radians(degrees)
    c = math.cos(angle)
    s = math.sin(angle)
    if axis == "x":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    if axis == "z":
        return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
    raise ValueError(f"unknown rotation axis: {axis}")


def matmul3(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> Matrix3:
    return tuple(
        tuple(sum(float(left[row][k]) * float(right[k][column]) for k in range(3)) for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


HTC_RIGHT_ROTATION: Matrix3 = rotation_matrix("y", 90.0)


def _values(source: Iterable[float], expected: int, label: str) -> tuple[float, ...]:
    values = tuple(float(value) for value in source)
    if len(values) != expected or not all(math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain {expected} finite values")
    return values


def quat_xyzw_to_matrix(quat_xyzw: Iterable[float]) -> Matrix3:
    qx, qy, qz, qw = _values(quat_xyzw, 4, "quaternion")
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 1e-12:
        raise ValueError("HTC pose contains an invalid zero quaternion")
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    return (
        (1.0 - 2.0 * (qy * qy + qz * qz), 2.0 * (qx * qy - qz * qw), 2.0 * (qx * qz + qy * qw)),
        (2.0 * (qx * qy + qz * qw), 1.0 - 2.0 * (qx * qx + qz * qz), 2.0 * (qy * qz - qx * qw)),
        (2.0 * (qx * qz - qy * qw), 2.0 * (qy * qz + qx * qw), 1.0 - 2.0 * (qx * qx + qy * qy)),
    )


def matrix_to_quat_xyzw(matrix: Sequence[Sequence[float]]) -> Quaternion:
    m = matrix
    trace = float(m[0][0] + m[1][1] + m[2][2])
    if trace > 0.0:
        s = math.sqrt(max(trace + 1.0, 1e-12)) * 2.0
        qw = 0.25 * s
        qx = (m[2][1] - m[1][2]) / s
        qy = (m[0][2] - m[2][0]) / s
        qz = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(max(1.0 + m[0][0] - m[1][1] - m[2][2], 1e-12)) * 2.0
        qw = (m[2][1] - m[1][2]) / s
        qx = 0.25 * s
        qy = (m[0][1] + m[1][0]) / s
        qz = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(max(1.0 + m[1][1] - m[0][0] - m[2][2], 1e-12)) * 2.0
        qw = (m[0][2] - m[2][0]) / s
        qx = (m[0][1] + m[1][0]) / s
        qy = 0.25 * s
        qz = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(max(1.0 + m[2][2] - m[0][0] - m[1][1], 1e-12)) * 2.0
        qw = (m[1][0] - m[0][1]) / s
        qx = (m[0][2] + m[2][0]) / s
        qy = (m[1][2] + m[2][1]) / s
        qz = 0.25 * s
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    quat = (qx / norm, qy / norm, qz / norm, qw / norm)
    return tuple(-value for value in quat) if quat[3] < 0.0 else quat  # type: ignore[return-value]


def quat_xyzw_to_rotvec(quat_xyzw: Iterable[float]) -> Vector3:
    qx, qy, qz, qw = _values(quat_xyzw, 4, "quaternion")
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    if qw < 0.0:
        qx, qy, qz, qw = -qx, -qy, -qz, -qw
    vector_norm = math.sqrt(qx * qx + qy * qy + qz * qz)
    if vector_norm <= 1e-12:
        return (0.0, 0.0, 0.0)
    scale = 2.0 * math.atan2(vector_norm, qw) / vector_norm
    return (qx * scale, qy * scale, qz * scale)


def compute_pose_matrix(
    position: Iterable[float],
    quat_xyzw: Iterable[float],
) -> tuple[Vector3, Matrix3]:
    """Apply the fixed per-frame HTC-to-Flexiv transform."""

    px, py, pz = _values(position, 3, "position")
    output_position: Vector3 = (
        px + HTC_WORLD_OFFSET_M[0],
        py + HTC_WORLD_OFFSET_M[1],
        pz + HTC_WORLD_OFFSET_M[2],
    )
    output_rotation = matmul3(quat_xyzw_to_matrix(quat_xyzw), HTC_RIGHT_ROTATION)
    return output_position, output_rotation


def transform_htc_pose_to_eef(
    position: Iterable[float], quat_xyzw: Iterable[float]
) -> tuple[Vector3, Quaternion, tuple[float, ...]]:
    """Return LeRobot-calibrated EEF XYZ, XYZW quaternion, and XYZ+rotvec."""

    output_position, output_rotation = compute_pose_matrix(position, quat_xyzw)
    output_quat = matrix_to_quat_xyzw(output_rotation)
    pose6 = output_position + quat_xyzw_to_rotvec(output_quat)
    return output_position, output_quat, pose6
