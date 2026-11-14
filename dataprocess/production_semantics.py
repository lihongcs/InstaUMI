#!/usr/bin/env python3
"""Fail-closed production semantics for ordinary bimanual LeRobot exports.

This module deliberately records *what* the numeric and image columns mean in
addition to recording the input files.  The contract proves that every source
conversion used the prescribed transform/projection/resize/storage chain.  It
does not claim that MediaSDK H.265 transport or any source-to-model image
operation is mathematically lossless.  Only the final RGB-to-embedded-PNG step
is lossless.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import stat
import sys
from typing import Any, Mapping


DATAPROCESS_ROOT = Path(__file__).resolve().parent
CONVERTER_ROOT = DATAPROCESS_ROOT / "_05_convert_bimanual_lerobot"
SEMANTICS_SCHEMA = "instaumi-bimanual-production-semantics-v1"
DIRECT_IMAGE_MODE = "official_continuous_h265_then_active_openpi"
PANORAMA_NUMERIC_MODE = "official_continuous_h265_panorama_only"
EXPECTED_IMAGE_KEYS = (
    "observation.images.right_wrist",
    "observation.images.right_left_view",
    "observation.images.right_right_view",
    "observation.images.left_wrist",
    "observation.images.left_left_view",
    "observation.images.left_right_view",
)
EXPECTED_STREAMS = (
    {"feature": EXPECTED_IMAGE_KEYS[0], "arm": "right", "view": "front"},
    {"feature": EXPECTED_IMAGE_KEYS[1], "arm": "right", "view": "left_yaw"},
    {"feature": EXPECTED_IMAGE_KEYS[2], "arm": "right", "view": "right_yaw"},
    {"feature": EXPECTED_IMAGE_KEYS[3], "arm": "left", "view": "front"},
    {"feature": EXPECTED_IMAGE_KEYS[4], "arm": "left", "view": "left_yaw"},
    {"feature": EXPECTED_IMAGE_KEYS[5], "arm": "left", "view": "right_yaw"},
)
EXPECTED_KNOWN_IMPLEMENTATION_PATHS = {
    "converter": CONVERTER_ROOT / "convert_bimanual_to_lerobot.py",
    "bimanual_helpers": CONVERTER_ROOT / "bimanual_convert_helpers.py",
    "htc_to_flexiv_transform": CONVERTER_ROOT / "htc_to_flexiv_transform.py",
    "capture_schema": DATAPROCESS_ROOT / "capture_schema.py",
    "direct_openpi_images": DATAPROCESS_ROOT / "direct_openpi_images.py",
    "production_semantics": Path(__file__).resolve(),
}
REQUIRED_IMPLEMENTATION_NAMES = (
    *EXPECTED_KNOWN_IMPLEMENTATION_PATHS,
    "lerobot_dataset",
    "lerobot_image_writer",
)


class ProductionSemanticsError(ValueError):
    """A conversion does not satisfy the production semantics contract."""


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def stable_file_identity(path: Path) -> dict[str, Any]:
    """Return a full identity while rejecting links, special files and races."""

    expanded = path.expanduser()
    try:
        lstat = expanded.lstat()
        if stat.S_ISLNK(lstat.st_mode):
            raise ProductionSemanticsError(f"implementation is a symlink: {expanded}")
        resolved = expanded.resolve(strict=True)
        before = resolved.stat()
    except OSError as exc:
        raise ProductionSemanticsError(f"implementation is missing: {expanded}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ProductionSemanticsError(f"implementation is not a regular file: {resolved}")
    digest = sha256_file(resolved)
    after = resolved.stat()
    signature = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if signature(before) != signature(after):
        raise ProductionSemanticsError(
            f"implementation changed while hashing: {resolved}"
        )
    return {
        "path": str(resolved),
        "size_bytes": int(after.st_size),
        "sha256": digest,
    }


def _normalized_recorded_identity(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductionSemanticsError(f"missing implementation identity: {name}")
    path = value.get("path")
    digest = value.get("sha256")
    size = value.get("size_bytes")
    if not isinstance(path, str) or not path:
        raise ProductionSemanticsError(f"invalid implementation path: {name}")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProductionSemanticsError(f"invalid implementation SHA-256: {name}")
    try:
        size_value = int(size)
    except (TypeError, ValueError) as exc:
        raise ProductionSemanticsError(
            f"invalid implementation size: {name}"
        ) from exc
    if size_value < 0:
        raise ProductionSemanticsError(f"invalid implementation size: {name}")
    return {
        "path": str(Path(path).expanduser().resolve()),
        "size_bytes": size_value,
        "sha256": digest,
    }


def _selected_implementations(implementation: Mapping[str, Any]) -> dict[str, Any]:
    files = implementation.get("files")
    if not isinstance(files, Mapping):
        raise ProductionSemanticsError("conversion plan has no implementation files")
    selected = {
        name: _normalized_recorded_identity(files.get(name), name=name)
        for name in REQUIRED_IMPLEMENTATION_NAMES
    }
    runtime = implementation.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ProductionSemanticsError("conversion plan has no runtime identity")
    selected_runtime = {
        key: runtime.get(key)
        for key in ("python", "numpy", "opencv", "pillow", "pyav")
    }
    for name, value in selected_runtime.items():
        if not isinstance(value, str) or not value.strip():
            raise ProductionSemanticsError(
                f"conversion runtime identity is missing: {name}"
            )
    return {
        "files": selected,
        "runtime": selected_runtime,
    }


def _require_finite_number(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductionSemanticsError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ProductionSemanticsError(f"invalid {label}: {value!r}")
    return result


def _optional_positive_threshold(value: Any, *, label: str) -> float | None:
    """Normalize one optional threshold while rejecting ambiguous values."""

    if value in (None, ""):
        return None
    result = _require_finite_number(value, label=label)
    if result <= 0.0:
        raise ProductionSemanticsError(f"{label} must be positive")
    return result


def _action_contract(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the exact absolute-pose and gripper representation."""

    zero_threshold = _optional_positive_threshold(
        arguments.get("gripper_zero_below_m"),
        label="gripper zero threshold",
    )
    binary_threshold = _optional_positive_threshold(
        arguments.get("gripper_binary_threshold_m"),
        label="gripper binary threshold",
    )
    if zero_threshold is not None and binary_threshold is not None:
        raise ProductionSemanticsError(
            "gripper zero and binary thresholds are mutually exclusive"
        )

    action: dict[str, Any] = {
        "action_order": "right_left",
        "action_is_delta": False,
        "gripper_is_delta": False,
        "arm_width": 10,
        "pose_encoding": "absolute_xyz_rotation_vector",
        "gripper_encoding": "absolute_calibrated_width_m",
    }
    if binary_threshold is not None:
        action["gripper_encoding"] = (
            "binary_0_or_1_from_interpolated_absolute_calibrated_width_m"
        )
        action["gripper_transform"] = {
            "kind": "binarize_calibrated_gripper_width_at_threshold",
            "threshold_m": binary_threshold,
            "comparison": (
                "finite_width < threshold_m -> 0; "
                "finite_width >= threshold_m -> 1"
            ),
            "low_value": 0.0,
            "high_value": 1.0,
            "application": "after_shared_grid_interpolation_before_state_action_encoding",
        }
    elif zero_threshold is not None:
        action["gripper_encoding"] = (
            "absolute_calibrated_width_m_with_zero_below_threshold"
        )
        action["gripper_transform"] = {
            "kind": "zero_calibrated_gripper_width_below_threshold",
            "threshold_m": zero_threshold,
            "comparison": "finite_width < threshold_m",
            "replacement_m": 0.0,
            "application": "after_shared_grid_interpolation_before_state_action_encoding",
        }
    return action


def _validate_action_contract(action: Any) -> None:
    """Validate every supported production gripper representation fail-closed."""

    if not isinstance(action, Mapping):
        raise ProductionSemanticsError("action contract is missing")
    common = {
        "action_order": "right_left",
        "action_is_delta": False,
        "gripper_is_delta": False,
        "arm_width": 10,
        "pose_encoding": "absolute_xyz_rotation_vector",
    }
    for key, expected in common.items():
        if action.get(key) != expected:
            raise ProductionSemanticsError(
                f"absolute action/gripper contract changed: {key}"
            )

    encoding = action.get("gripper_encoding")
    transform = action.get("gripper_transform")
    if encoding == "absolute_calibrated_width_m":
        if set(action) != {*common, "gripper_encoding"}:
            raise ProductionSemanticsError(
                "continuous gripper contract has unexpected fields"
            )
        return
    if not isinstance(transform, Mapping):
        raise ProductionSemanticsError("transformed gripper contract is incomplete")
    threshold = _optional_positive_threshold(
        transform.get("threshold_m"), label="recorded gripper threshold"
    )
    if threshold is None:
        raise ProductionSemanticsError("recorded gripper threshold is missing")
    application = (
        "after_shared_grid_interpolation_before_state_action_encoding"
    )
    if encoding == "binary_0_or_1_from_interpolated_absolute_calibrated_width_m":
        expected_transform = {
            "kind": "binarize_calibrated_gripper_width_at_threshold",
            "threshold_m": threshold,
            "comparison": (
                "finite_width < threshold_m -> 0; "
                "finite_width >= threshold_m -> 1"
            ),
            "low_value": 0.0,
            "high_value": 1.0,
            "application": application,
        }
    elif encoding == "absolute_calibrated_width_m_with_zero_below_threshold":
        expected_transform = {
            "kind": "zero_calibrated_gripper_width_below_threshold",
            "threshold_m": threshold,
            "comparison": "finite_width < threshold_m",
            "replacement_m": 0.0,
            "application": application,
        }
    else:
        raise ProductionSemanticsError(
            f"unsupported gripper encoding: {encoding!r}"
        )
    if dict(transform) != expected_transform:
        raise ProductionSemanticsError("gripper transform contract changed")
    if set(action) != {*common, "gripper_encoding", "gripper_transform"}:
        raise ProductionSemanticsError(
            "transformed gripper contract has unexpected fields"
        )


def _verify_transform_definition() -> None:
    """Bind the stated parameters to the implementation currently imported."""

    path = EXPECTED_KNOWN_IMPLEMENTATION_PATHS["htc_to_flexiv_transform"]
    spec = importlib.util.spec_from_file_location(
        "_instaumi_production_htc_transform", path
    )
    if spec is None or spec.loader is None:
        raise ProductionSemanticsError("cannot load HTC transform implementation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "TRANSFORM_NAME", None) != "htc_to_flexiv_ry90_fixed_offset":
        raise ProductionSemanticsError("HTC transform name changed")
    offset = getattr(module, "WORLD_OFFSET_M", None)
    matrix = getattr(module, "TRACKER_TO_EEF_ROTATION", None)
    if offset is None or matrix is None or not callable(
        getattr(module, "compute_pose_transform", None)
    ):
        raise ProductionSemanticsError("HTC transform definition is incomplete")
    expected_offset = [0.01414, 0.05415, -0.05619]
    expected_matrix = [
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
        [-1.0, 0.0, 0.0],
    ]
    actual_offset = getattr(offset, "tolist", lambda: offset)()
    actual_matrix = getattr(matrix, "tolist", lambda: matrix)()
    try:
        offset_ok = all(
            math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-12)
            for actual, expected in zip(actual_offset, expected_offset, strict=True)
        )
        matrix_ok = all(
            math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-12)
            for actual_row, expected_row in zip(
                actual_matrix, expected_matrix, strict=True
            )
            for actual, expected in zip(actual_row, expected_row, strict=True)
        )
    except (TypeError, ValueError) as exc:
        raise ProductionSemanticsError("HTC transform parameters are invalid") from exc
    if not offset_ok or not matrix_ok:
        raise ProductionSemanticsError("HTC transform parameters changed")


def _shared_direct_recipe(
    image_sources: Mapping[str, Any],
    *,
    expected_mode: str = DIRECT_IMAGE_MODE,
) -> dict[str, Any]:
    if image_sources.get("mode") != expected_mode:
        raise ProductionSemanticsError(
            f"production image mode must be {expected_mode}"
        )
    recipes: dict[str, Mapping[str, Any]] = {}
    for arm in ("right", "left"):
        plan = image_sources.get(arm)
        if not isinstance(plan, Mapping):
            raise ProductionSemanticsError(f"missing direct image plan for {arm}")
        recipe = plan.get("recipe")
        if not isinstance(recipe, Mapping):
            raise ProductionSemanticsError(f"missing direct image recipe for {arm}")
        recipes[arm] = recipe
    if dict(recipes["right"]) != dict(recipes["left"]):
        raise ProductionSemanticsError("left/right direct image recipes differ")
    recipe = recipes["right"]
    expected_literals = {
        "front": "decode_insv_primary_stream_then_openpi_resize",
        "side": (
            "bounded_mediasdk_png_then_"
            "ffmpeg.v360.flat.linear.split_pair_then_openpi_resize.v1"
        ),
        "resize": "openpi_client.resize_with_pad.pil_bilinear.v1",
        "target_height": 224,
        "target_width": 224,
        "panorama_width": 2048,
        "panorama_height": 1024,
        "left_yaw": -90.0,
        "right_yaw": 90.0,
        "horizontal_fov": 90.0,
        "vertical_fov": 90.0,
        "stitch_type": "optflow",
        "flowstate": False,
        "direction_lock": False,
        "stitch_fusion": False,
        "full_resolution_png_retention": "bounded_batch_finally_delete",
    }
    for key, expected in expected_literals.items():
        actual = recipe.get(key)
        if isinstance(expected, float):
            if _require_finite_number(actual, label=f"image recipe {key}") != expected:
                raise ProductionSemanticsError(
                    f"image recipe {key} changed: {actual!r} != {expected!r}"
                )
        elif actual != expected:
            raise ProductionSemanticsError(
                f"image recipe {key} changed: {actual!r} != {expected!r}"
            )
    if recipe.get("projection_size") not in (None, 512):
        # ``None`` resolves to min(2048/4, 1024/2) == 512 in the decoder.
        raise ProductionSemanticsError("image projection_size must resolve to 512")
    if recipe.get("cuda") is not True:
        raise ProductionSemanticsError("MediaSDK production image path must use CUDA")
    return dict(recipe)


def _image_configuration(
    image_sources: Mapping[str, Any], arguments: Mapping[str, Any]
) -> dict[str, Any]:
    panorama_numeric_only = bool(arguments.get("panorama_numeric_only", False))
    expected_mode = (
        PANORAMA_NUMERIC_MODE if panorama_numeric_only else DIRECT_IMAGE_MODE
    )
    recipe = _shared_direct_recipe(image_sources, expected_mode=expected_mode)
    bitrate = arguments.get("direct_image_panorama_bitrate")
    try:
        bitrate_value = int(bitrate)
    except (TypeError, ValueError) as exc:
        raise ProductionSemanticsError("invalid continuous panorama bitrate") from exc
    if bitrate_value <= 0:
        raise ProductionSemanticsError("continuous panorama bitrate must be positive")
    panorama_exporter = arguments.get("direct_image_panorama_exporter")
    panorama_exporter_identity = _normalized_recorded_identity(
        panorama_exporter, name="direct_image_panorama_exporter"
    )
    configuration = {
        "mode": expected_mode,
        "streams": [] if panorama_numeric_only else list(EXPECTED_STREAMS),
        "front": {
            "source": "ordered INSV primary video stream",
            "operation": recipe["front"],
        },
        "panorama_transport": {
            "api": "Insta360.MediaSDK.VideoStitcher.SetOutputPath",
            "codec": "hevc",
            "hardware_decode": True,
            "hardware_encode": True,
            "width": int(recipe["panorama_width"]),
            "height": int(recipe["panorama_height"]),
            "bitrate": bitrate_value,
            "stitch_type": recipe["stitch_type"],
            "flowstate": bool(recipe["flowstate"]),
            "direction_lock": bool(recipe["direction_lock"]),
            "stitch_fusion": bool(recipe["stitch_fusion"]),
            "exporter": panorama_exporter_identity,
        },
        "side_projection": {
            # The raw source plan still carries the legacy/transient-PNG
            # recipe literal because it owns INSV discovery and MediaSDK
            # runtime provenance.  The production converter no longer uses
            # that transient path: it first publishes a fully validated
            # continuous HEVC panorama chapter and then decodes only the
            # aligned rows through the same libavfilter v360 recipe.
            "operation": (
                "validated_mediasdk_continuous_hevc_then_"
                "ffmpeg.v360.flat.linear.split_pair_then_openpi_resize.v1"
            ),
            "projection": "ffmpeg_v360_flat",
            "interpolation": "linear",
            "projection_size": 512,
            "left_yaw_deg": float(recipe["left_yaw"]),
            "right_yaw_deg": float(recipe["right_yaw"]),
            "horizontal_fov_deg": float(recipe["horizontal_fov"]),
            "vertical_fov_deg": float(recipe["vertical_fov"]),
        },
        "resize": {
            "operation": recipe["resize"],
            "library": "Pillow",
            "interpolation": "PIL.Image.BILINEAR",
            "resize_policy": "exact OpenPI resize_with_pad",
            "output": {"height": 224, "width": 224, "channels": 3},
            "pixel_format": "RGB uint8",
        },
        "storage": {
            "format": "PNG",
            "location": "embedded bytes in Parquet image structs",
            "source_to_model_pixels": "prescribed_chain_not_mathematically_lossless",
            "final_rgb_to_embedded_png": "lossless",
        },
    }
    if panorama_numeric_only:
        configuration.pop("front")
        configuration.pop("side_projection")
        configuration.pop("resize")
        configuration["storage"] = {
            "format": "packet-exact episode panorama videos",
            "legacy_six_view_columns_in_lerobot": False,
            "view_materialization": "training-local deterministic cache",
        }
    return configuration


def build_production_semantics(
    *,
    image_sources: Mapping[str, Any],
    arguments: Mapping[str, Any],
    implementation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the canonical direct-image production contract."""

    _verify_transform_definition()
    if arguments.get("right_pose_sensor") != "htc" or arguments.get(
        "left_pose_sensor"
    ) != "htc":
        raise ProductionSemanticsError("production pose sensors must both be HTC")
    action = _action_contract(arguments)
    htc_configuration = {
        "input_after_capture_schema_normalization": "xyz_quaternion_xyzw",
        "output": "xyz_rotation_vector",
        "rotation_composition": "R_htc @ Ry(+90deg)",
        "tracker_to_eef_rotation_matrix": [
            [0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0],
            [-1.0, 0.0, 0.0],
        ],
        "world_offset_m": [0.01414, 0.05415, -0.05619],
        "transform_name": "htc_to_flexiv_ry90_fixed_offset",
        "application": "same_stateless_function_each_frame_both_arms",
        "episode_first_frame_dependency": False,
        "episode_relative_pose_correction": False,
        "pose_sensors": {"right": "htc", "left": "htc"},
    }
    image_configuration = _image_configuration(image_sources, arguments)
    selected_implementation = _selected_implementations(implementation)
    payload = {
        "schema": SEMANTICS_SCHEMA,
        "release_eligible": True,
        "action_contract": action,
        "htc_to_flexiv": {
            "configuration": htc_configuration,
            "configuration_sha256": sha256_payload(htc_configuration),
            "implementation": selected_implementation["files"][
                "htc_to_flexiv_transform"
            ],
        },
        "image_chain": {
            "configuration": image_configuration,
            "configuration_sha256": sha256_payload(image_configuration),
        },
        "implementation": selected_implementation,
    }
    payload["contract_sha256"] = sha256_payload(payload)
    return payload


def build_nonproduction_semantics(
    *, arguments: Mapping[str, Any], implementation: Mapping[str, Any]
) -> dict[str, Any]:
    """Record an explicit non-release marker for the legacy six-video path."""

    payload = {
        "schema": SEMANTICS_SCHEMA,
        "release_eligible": False,
        "reason": "legacy_six_videos_is_not_the_prescribed_production_image_chain",
        "pose_sensors": {
            "right": arguments.get("right_pose_sensor"),
            "left": arguments.get("left_pose_sensor"),
        },
        # Legacy/test plans remain representable, but the production merger
        # rejects this marker before it can enter a release dataset.
        "implementation_evidence": dict(implementation),
    }
    payload["contract_sha256"] = sha256_payload(payload)
    return payload


def _verify_current_implementations(semantics: Mapping[str, Any]) -> None:
    implementation = semantics.get("implementation")
    files = implementation.get("files") if isinstance(implementation, Mapping) else None
    if not isinstance(files, Mapping):
        raise ProductionSemanticsError("production semantics has no implementation files")
    for name in REQUIRED_IMPLEMENTATION_NAMES:
        recorded = _normalized_recorded_identity(files.get(name), name=name)
        if name in EXPECTED_KNOWN_IMPLEMENTATION_PATHS:
            expected_path = EXPECTED_KNOWN_IMPLEMENTATION_PATHS[name].resolve(strict=True)
            if Path(recorded["path"]) != expected_path:
                raise ProductionSemanticsError(
                    f"implementation path changed for {name}: {recorded['path']}"
                )
            current_path = expected_path
        else:
            current_path = Path(recorded["path"])
        current = stable_file_identity(current_path)
        if current != recorded:
            raise ProductionSemanticsError(
                f"current implementation does not match conversion plan: {name}"
            )


def _verify_current_runtime(semantics: Mapping[str, Any]) -> None:
    implementation = semantics.get("implementation")
    recorded = (
        implementation.get("runtime")
        if isinstance(implementation, Mapping)
        else None
    )
    if not isinstance(recorded, Mapping):
        raise ProductionSemanticsError("production runtime identity is missing")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ProductionSemanticsError(
            "current production Python runtime is incomplete"
        ) from exc
    current = {
        "python": sys.version,
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "pillow": importlib.metadata.version("Pillow"),
        "pyav": importlib.metadata.version("av"),
    }
    if dict(recorded) != current:
        raise ProductionSemanticsError(
            "current image runtime versions do not match conversion plan"
        )


def validate_production_semantics_payload(
    semantics: Mapping[str, Any], *, verify_current_files: bool = True
) -> dict[str, Any]:
    """Validate a canonical semantics payload carried by a merge plan."""

    _verify_transform_definition()
    if semantics.get("schema") != SEMANTICS_SCHEMA or semantics.get(
        "release_eligible"
    ) is not True:
        raise ProductionSemanticsError("not a release-eligible production contract")
    unhashed = dict(semantics)
    stored_hash = unhashed.pop("contract_sha256", None)
    if not isinstance(stored_hash, str) or stored_hash != sha256_payload(unhashed):
        raise ProductionSemanticsError("production semantics contract hash is invalid")
    _validate_action_contract(semantics.get("action_contract"))
    expected_htc = {
        "input_after_capture_schema_normalization": "xyz_quaternion_xyzw",
        "output": "xyz_rotation_vector",
        "rotation_composition": "R_htc @ Ry(+90deg)",
        "tracker_to_eef_rotation_matrix": [
            [0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0],
            [-1.0, 0.0, 0.0],
        ],
        "world_offset_m": [0.01414, 0.05415, -0.05619],
        "transform_name": "htc_to_flexiv_ry90_fixed_offset",
        "application": "same_stateless_function_each_frame_both_arms",
        "episode_first_frame_dependency": False,
        "episode_relative_pose_correction": False,
        "pose_sensors": {"right": "htc", "left": "htc"},
    }
    htc = semantics.get("htc_to_flexiv")
    if not isinstance(htc, Mapping) or htc.get("configuration") != expected_htc:
        raise ProductionSemanticsError("HTC-to-Flexiv configuration changed")
    if htc.get("configuration_sha256") != sha256_payload(expected_htc):
        raise ProductionSemanticsError("HTC-to-Flexiv configuration hash is invalid")
    implementation = semantics.get("implementation")
    selected = _selected_implementations(
        implementation if isinstance(implementation, Mapping) else {}
    )
    if htc.get("implementation") != selected["files"]["htc_to_flexiv_transform"]:
        raise ProductionSemanticsError("HTC transform implementation binding changed")
    image = semantics.get("image_chain")
    configuration = image.get("configuration") if isinstance(image, Mapping) else None
    if not isinstance(configuration, Mapping):
        raise ProductionSemanticsError("image-chain configuration is missing")
    if image.get("configuration_sha256") != sha256_payload(configuration):
        raise ProductionSemanticsError("image-chain configuration hash is invalid")
    if configuration.get("mode") != DIRECT_IMAGE_MODE:
        raise ProductionSemanticsError("image-chain mode changed")
    if configuration.get("streams") != list(EXPECTED_STREAMS):
        raise ProductionSemanticsError("six-stream orientation mapping changed")
    expected_front = {
        "source": "ordered INSV primary video stream",
        "operation": "decode_insv_primary_stream_then_openpi_resize",
    }
    if configuration.get("front") != expected_front:
        raise ProductionSemanticsError("front-view image chain changed")
    panorama = configuration.get("panorama_transport")
    if not isinstance(panorama, Mapping):
        raise ProductionSemanticsError("panorama transport is missing")
    fixed_panorama = {
        "api": "Insta360.MediaSDK.VideoStitcher.SetOutputPath",
        "codec": "hevc",
        "hardware_decode": True,
        "hardware_encode": True,
        "width": 2048,
        "height": 1024,
        "stitch_type": "optflow",
        "flowstate": False,
        "direction_lock": False,
        "stitch_fusion": False,
    }
    for key, expected in fixed_panorama.items():
        if panorama.get(key) != expected:
            raise ProductionSemanticsError(f"panorama transport changed: {key}")
    try:
        bitrate = int(panorama.get("bitrate"))
    except (TypeError, ValueError) as exc:
        raise ProductionSemanticsError("panorama bitrate is invalid") from exc
    if bitrate <= 0:
        raise ProductionSemanticsError("panorama bitrate is invalid")
    expected_side = {
        "operation": (
            "validated_mediasdk_continuous_hevc_then_"
            "ffmpeg.v360.flat.linear.split_pair_then_openpi_resize.v1"
        ),
        "projection": "ffmpeg_v360_flat",
        "interpolation": "linear",
        "projection_size": 512,
        "left_yaw_deg": -90.0,
        "right_yaw_deg": 90.0,
        "horizontal_fov_deg": 90.0,
        "vertical_fov_deg": 90.0,
    }
    if configuration.get("side_projection") != expected_side:
        raise ProductionSemanticsError("side-view projection/orientation changed")
    expected_resize = {
        "operation": "openpi_client.resize_with_pad.pil_bilinear.v1",
        "library": "Pillow",
        "interpolation": "PIL.Image.BILINEAR",
        "resize_policy": "exact OpenPI resize_with_pad",
        "output": {"height": 224, "width": 224, "channels": 3},
        "pixel_format": "RGB uint8",
    }
    if configuration.get("resize") != expected_resize:
        raise ProductionSemanticsError("OpenPI PIL resize chain changed")
    expected_storage = {
        "format": "PNG",
        "location": "embedded bytes in Parquet image structs",
        "source_to_model_pixels": "prescribed_chain_not_mathematically_lossless",
        "final_rgb_to_embedded_png": "lossless",
    }
    if configuration.get("storage") != expected_storage:
        raise ProductionSemanticsError("image fidelity/storage statement changed")
    if verify_current_files:
        _verify_current_implementations(semantics)
        _verify_current_runtime(semantics)
        exporter = _normalized_recorded_identity(
            panorama.get("exporter"), name="direct_image_panorama_exporter"
        )
        if stable_file_identity(Path(exporter["path"])) != exporter:
            raise ProductionSemanticsError(
                "current panorama exporter does not match conversion plan"
            )
    return dict(semantics)


def validate_conversion_plan_production_semantics(
    plan: Mapping[str, Any], *, verify_current_files: bool = True
) -> dict[str, Any]:
    """Reconstruct and verify one source conversion's semantics fail-closed."""

    arguments = plan.get("arguments")
    inputs = plan.get("inputs")
    implementation = plan.get("implementation")
    semantics = plan.get("production_semantics")
    if not all(isinstance(value, Mapping) for value in (arguments, inputs, implementation, semantics)):
        raise ProductionSemanticsError("conversion plan lacks production semantics evidence")
    expected = build_production_semantics(
        image_sources=inputs.get("image_sources") or {},
        arguments=arguments,
        implementation=implementation,
    )
    if dict(semantics) != expected:
        raise ProductionSemanticsError(
            "recorded production semantics does not match conversion plan inputs"
        )
    return validate_production_semantics_payload(
        semantics, verify_current_files=verify_current_files
    )
