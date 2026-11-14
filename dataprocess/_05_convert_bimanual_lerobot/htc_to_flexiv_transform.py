#!/usr/bin/env python3
"""Stateless per-frame HTC Tracker to Flexiv end-effector transform."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from scipy.spatial.transform import Rotation


WORLD_OFFSET_M = np.array([0.01414, 0.05415, -0.05619], dtype=np.float64)
TRACKER_TO_EEF_ROTATION = Rotation.from_euler(
    "y", 90.0, degrees=True
).as_matrix()
TRANSFORM_NAME = "htc_to_flexiv_ry90_fixed_offset"


def legacy_csv_xyzw_to_firmware_xyzw(quaternion_xyzw: Iterable[float]) -> np.ndarray:
    """Recover firmware XYZW from CSVs recorded before quaternion schema v2."""

    stored = np.asarray(tuple(quaternion_xyzw), dtype=np.float64)
    if stored.shape != (4,) or not np.all(np.isfinite(stored)):
        raise ValueError("legacy quaternion must contain four finite XYZW values")
    return stored[::-1].copy()


def compute_pose_transform(
    raw_pose: Iterable[float],
    world_offset_m: Iterable[float] = WORLD_OFFSET_M,
) -> np.ndarray:
    """Convert ``[x,y,z,qx,qy,qz,qw]`` to Flexiv XYZ + rotation vector."""

    pose = np.asarray(tuple(raw_pose), dtype=np.float64)
    if pose.shape != (7,) or not np.all(np.isfinite(pose)):
        raise ValueError("raw_pose must be [x,y,z,qx,qy,qz,qw] with finite values")
    offset = np.asarray(tuple(world_offset_m), dtype=np.float64)
    if offset.shape != (3,) or not np.all(np.isfinite(offset)):
        raise ValueError("world_offset_m must contain three finite values")
    quaternion = pose[3:7]
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("raw_pose contains a zero quaternion")
    rotation = Rotation.from_quat(quaternion / norm).as_matrix() @ TRACKER_TO_EEF_ROTATION
    position = pose[:3] + offset
    return np.concatenate((position, Rotation.from_matrix(rotation).as_rotvec())).astype(
        np.float32
    )
