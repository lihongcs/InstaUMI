from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


EPISODE_FILE_RE = re.compile(r"^episode_(\d+)\.parquet$")
VECTOR_COLUMNS = ("observation.state", "action")
ZARR_SHARD_SCHEMAS = {
    "openpi-biarm-handcap-zarr-v1",
    "openpi-biarm-handcap-zarr-v2",
}
ZARR_COLLECTION_SCHEMA = "openpi-biarm-handcap-zarr-collection-v1"
ZARR_QUALITY_SCHEMA = "openpi-biarm-handcap-zarr-quality-v1"
CONTINUOUS_GRIPPER_QUALITY_SCHEMA = "instaumi.continuous_gripper_quality.v1"
GRIPPER_ESTIMATION_SCHEMA = "instaumi_gripper_estimation_v1"
BIMANUAL_GRIPPER_SCHEMA = "instaumi_biarm_gripper_estimation_v1"
THRESHOLD_PREVIEW_SCHEMA = "instaumi_gripper_threshold_preview_v1"
GRIPPER_VALUE_COLUMNS = (
    "center_distance_m_filled",
    "distance_m_filled",
    "center_distance_m",
    "distance_m",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing dataset metadata: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read dataset metadata: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"dataset metadata must be a JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _read_manifest_if_schema(path: Path, schemas: set[str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") not in schemas:
        return None
    return value


def _known_gripper_manifest_candidates(directory: Path) -> list[Path]:
    """Return bounded, deterministic candidates without recursively scanning a capture."""

    relative_paths = (
        "manifest.json",
        "gripper_threshold_preview/manifest.json",
        "gripper_threshold_sampling/manifest.json",
        "03_gripper_estimation_biarm/manifest.json",
        "tmp/03_gripper_estimation_biarm/manifest.json",
        "active_view/intermediate/base_pipeline/tmp/03_gripper_estimation_biarm/manifest.json",
    )
    return [directory / relative for relative in relative_paths]


def resolve_gripper_artifact_manifest(value: str | Path) -> Path:
    """Resolve a completed pre-LeRobot gripper-estimation artifact read-only."""

    requested = Path(value).expanduser().resolve()
    schemas = {BIMANUAL_GRIPPER_SCHEMA, THRESHOLD_PREVIEW_SCHEMA}
    if requested.is_file():
        if requested.name == "gripper_width.csv":
            # `<tmp>/<arm>/03_gripper_estimation/gripper_width.csv`.
            candidate = requested.parents[2] / "03_gripper_estimation_biarm" / "manifest.json"
            if _read_manifest_if_schema(candidate, schemas) is not None:
                return candidate.resolve()
            raise ValueError(f"gripper CSV has no paired bimanual manifest: {requested}")
        if _read_manifest_if_schema(requested, schemas) is not None:
            return requested
        raise ValueError(f"unsupported gripper preview artifact: {requested}")
    if not requested.is_dir():
        raise ValueError(f"preview source does not exist: {requested}")
    matches = [
        path.resolve()
        for path in _known_gripper_manifest_candidates(requested)
        if _read_manifest_if_schema(path, schemas) is not None
    ]
    matches = list(dict.fromkeys(matches))
    if not matches:
        raise ValueError(f"no completed pre-LeRobot gripper artifact found under: {requested}")
    if len(matches) > 1:
        choices = "\n".join(f"  - {path}" for path in matches)
        raise ValueError(
            "multiple gripper preview artifacts were found; enter the exact manifest path:\n"
            f"{choices}"
        )
    return matches[0]


def _is_lerobot_root(path: Path) -> bool:
    return (
        (path / "meta" / "info.json").is_file()
        and (path / "data").is_dir()
    )


def _dataset_root_for_episode_file(path: Path) -> Path:
    for parent in path.parents:
        if _is_lerobot_root(parent):
            try:
                path.relative_to(parent / "data")
            except ValueError:
                continue
            return parent
    raise ValueError(f"episode Parquet is not inside a LeRobot dataset: {path}")


def resolve_dataset_root(value: str | Path) -> tuple[Path, int | None]:
    requested = Path(value).expanduser().resolve()
    if requested.is_file():
        match = EPISODE_FILE_RE.fullmatch(requested.name)
        if match is None:
            raise ValueError(
                "a file input must be an episode_XXXXXX.parquet file"
            )
        return _dataset_root_for_episode_file(requested), int(match.group(1))
    if not requested.is_dir():
        raise ValueError(f"dataset or capture directory does not exist: {requested}")
    if _is_lerobot_root(requested):
        return requested, None

    candidates: set[Path] = set()
    for name in ("lerobot", "active_view_lerobot"):
        candidate = requested / name
        if _is_lerobot_root(candidate):
            candidates.add(candidate.resolve())
    for candidate in requested.glob("*_lerobot*"):
        if candidate.is_dir() and _is_lerobot_root(candidate):
            candidates.add(candidate.resolve())
    if not candidates:
        raise ValueError(
            f"no LeRobot dataset found directly under capture directory: {requested}"
        )
    if len(candidates) > 1:
        choices = "\n".join(f"  - {path}" for path in sorted(candidates))
        raise ValueError(
            "multiple LeRobot datasets were found; enter the exact dataset path:\n"
            f"{choices}"
        )
    return next(iter(candidates)), None


def discover_episode_files(dataset_root: Path) -> dict[int, Path]:
    episodes: dict[int, Path] = {}
    for path in sorted((dataset_root / "data").glob("chunk-*/episode_*.parquet")):
        match = EPISODE_FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        index = int(match.group(1))
        if index in episodes:
            raise ValueError(
                f"duplicate episode {index} in dataset: {episodes[index]} and {path}"
            )
        episodes[index] = path.resolve()
    if not episodes:
        raise ValueError(f"dataset contains no episode Parquet files: {dataset_root}")
    return episodes


def _is_zarr_shard_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "zarr.json").is_file()
        and (path / "manifest.json").is_file()
        and (path / "meta" / "zarr_quality_certificate.json").is_file()
    )


def _is_zarr_collection_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "collection.json").is_file()
        and (path / "manifest.json").is_file()
    )


def resolve_zarr_source(value: str | Path) -> tuple[str, Path]:
    """Resolve one audited Zarr shard or zero-copy collection, without writing it."""

    requested = Path(value).expanduser().resolve()
    if requested.is_file():
        if requested.name == "collection.json" and _is_zarr_collection_root(requested.parent):
            return "collection", requested.parent.resolve()
        if requested.name == "zarr.json" and _is_zarr_shard_root(requested.parent):
            return "shard", requested.parent.resolve()
        raise ValueError(
            "a Zarr file input must be zarr.json or collection.json inside a complete release"
        )
    if not requested.is_dir():
        raise ValueError(f"Zarr dataset path does not exist: {requested}")
    if _is_zarr_collection_root(requested):
        return "collection", requested
    if _is_zarr_shard_root(requested):
        return "shard", requested

    # Bounded one-level discovery is useful when the user enters a release
    # parent.  Never recursively scan raw captures or silently choose between
    # a collection and one of its shards.
    candidates: list[tuple[str, Path]] = []
    for child in sorted(requested.iterdir()):
        if not child.is_dir():
            continue
        if _is_zarr_collection_root(child):
            candidates.append(("collection", child.resolve()))
        elif _is_zarr_shard_root(child):
            candidates.append(("shard", child.resolve()))
    if not candidates:
        raise ValueError(f"no complete Zarr shard or collection found under: {requested}")
    if len(candidates) > 1:
        choices = "\n".join(f"  - {kind}: {path}" for kind, path in candidates)
        raise ValueError(
            "multiple Zarr releases were found; enter the exact .zarr or .collection path:\n"
            f"{choices}"
        )
    return candidates[0]


def _load_zarr_modules() -> tuple[Any, Any]:
    try:
        import numpy as np
        import zarr
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "the selected data Python has no Zarr reader; configure data_preview.python "
            "to a Python environment "
            "containing zarr>=3 and numpy"
        ) from exc
    return np, zarr


def _validate_identity(root: Path, identity: Any, label: str) -> None:
    if not isinstance(identity, dict):
        raise ValueError(f"{label} has no immutable file identity")
    relative = identity.get("path")
    digest = identity.get("sha256")
    try:
        size = int(identity.get("size_bytes", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} has an invalid file size identity") from exc
    if not isinstance(relative, str) or not relative or not isinstance(digest, str):
        raise ValueError(f"{label} has an incomplete immutable file identity")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} identity escapes its Zarr shard: {relative}") from exc
    if not path.is_file() or path.stat().st_size != size or _sha256_file(path) != digest:
        raise ValueError(f"{label} immutable identity changed: {path}")


def _zarr_gripper_transform(root: Path) -> dict[str, Any] | None:
    evidence: list[tuple[str, Any]] = []
    for name in ("conversion_plan.json", "sync_report.json", "manifest.json"):
        path = root / name
        if not path.is_file():
            continue
        payload = _read_json(path)
        if "gripper_transform" in payload:
            evidence.append((name, payload.get("gripper_transform")))
    if not evidence:
        return None
    reference = _canonical_bytes(evidence[0][1])
    inconsistent = [name for name, value in evidence if _canonical_bytes(value) != reference]
    if inconsistent:
        raise ValueError(
            f"Zarr gripper transform metadata is inconsistent: {root}: {inconsistent}"
        )
    transform = evidence[0][1]
    if transform is None:
        return None
    if not isinstance(transform, dict) or not isinstance(transform.get("kind"), str):
        raise ValueError(f"invalid Zarr gripper transform: {root}: {transform!r}")
    if transform.get("kind") == "binarize_calibrated_gripper_width_at_threshold":
        threshold = _finite_or_none(transform.get("threshold_m"))
        if threshold is None or threshold <= 0.0:
            raise ValueError(f"invalid binary gripper threshold in Zarr metadata: {root}")
    return transform


def _task_name_zarr(dataset_root: Path) -> str:
    path = dataset_root / "meta" / "tasks.jsonl"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"invalid task metadata: {path}")
        return str(value.get("task", ""))
    return ""


def _validate_continuous_gripper_quality(
    quality: dict[str, Any],
    transform: dict[str, Any] | None,
    root: Path,
) -> dict[str, Any]:
    """Prove that an untransformed Zarr source really contains audited widths.

    Older explicitly-binary Zarr releases remain viewable for compatibility,
    but absence of the continuous-width evidence is surfaced and can never be
    interpreted as a continuous source.
    """

    evidence = quality.get("continuous_gripper_quality")
    source_mode = _source_mode(transform)
    if evidence is None:
        if source_mode == "binary":
            return {
                "status": "binary_compatibility_only",
                "accepted": False,
                "warning": (
                    "This explicitly-binary legacy Zarr has no accepted continuous-gripper "
                    "quality certificate; it may be inspected as binary data only."
                ),
                "contract": None,
            }
        raise ValueError(
            f"continuous Zarr lacks accepted continuous_gripper_quality evidence: {root}"
        )
    if not isinstance(evidence, dict):
        raise ValueError(f"continuous_gripper_quality is malformed: {root}")
    if (
        evidence.get("schema") != CONTINUOUS_GRIPPER_QUALITY_SCHEMA
        or evidence.get("status") != "accepted"
    ):
        raise ValueError(f"continuous_gripper_quality is not accepted: {root}")
    cameras = evidence.get("cameras")
    if not isinstance(cameras, dict) or set(cameras) != {"left", "right"}:
        raise ValueError(
            f"continuous_gripper_quality must contain exactly left and right evidence: {root}"
        )
    covered = evidence.get("covered_training_segment_indices")
    if (
        not isinstance(covered, list)
        or not covered
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in covered
        )
        or covered != sorted(set(covered))
        or evidence.get("covered_training_segment_count") != len(covered)
    ):
        raise ValueError(f"continuous gripper segment coverage is invalid: {root}")
    common_contract: dict[str, Any] | None = None
    for camera in ("right", "left"):
        item = cameras[camera]
        if not isinstance(item, dict):
            raise ValueError(f"{camera} continuous gripper evidence is malformed: {root}")
        contract = item.get("contract")
        if not isinstance(contract, dict) or contract.get("enabled") is not True:
            raise ValueError(f"{camera} continuous gripper contract is not enabled: {root}")
        if common_contract is None:
            common_contract = contract
        elif _canonical_bytes(contract) != _canonical_bytes(common_contract):
            raise ValueError(f"left/right continuous gripper contracts differ: {root}")
        evidence_indices = item.get("evidence_segment_indices")
        segments = item.get("segments")
        if (
            not isinstance(evidence_indices, list)
            or evidence_indices != sorted(set(evidence_indices))
            or not isinstance(segments, list)
            or not segments
            or not isinstance(item.get("summary"), dict)
        ):
            raise ValueError(f"{camera} continuous gripper evidence is incomplete: {root}")
        try:
            segment_indices = [segment["segment_index"] for segment in segments]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"{camera} continuous gripper segment evidence is malformed: {root}"
            ) from exc
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in segment_indices
            )
            or evidence_indices != sorted(set(segment_indices))
            or not set(covered).issubset(evidence_indices)
        ):
            raise ValueError(
                f"{camera} continuous gripper evidence does not cover training segments: {root}"
            )
    return {
        "status": "accepted",
        "accepted": True,
        "warning": None,
        "contract": common_contract,
    }


def _validate_zarr_vector_contract(
    np: Any,
    zroot: Any,
    info: dict[str, Any],
    state: Any,
    action: Any,
    root: Path,
) -> dict[str, list[tuple[str, str, int]]]:
    """Require the exact bimanual 20-D feature layout used by OpenPI."""

    arrays = {"observation.state": state, "action": action}
    expected_indices = [("right", "right_gripper", 6), ("left", "left_gripper", 16)]
    names_by_feature: dict[str, list[str]] = {}
    channels: dict[str, list[tuple[str, str, int]]] = {}
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError(f"Zarr feature metadata is absent: {root}")
    for feature_name in VECTOR_COLUMNS:
        feature = features.get(feature_name)
        if not isinstance(feature, dict):
            raise ValueError(f"Zarr feature metadata is absent for {feature_name}: {root}")
        names = feature.get("names")
        if (
            feature.get("dtype") != "float32"
            or feature.get("shape") != [20]
            or not isinstance(names, list)
            or len(names) != 20
            or not all(isinstance(name, str) and name for name in names)
        ):
            raise ValueError(f"{feature_name} must be named float32 shape [20]: {root}")
        if np.dtype(arrays[feature_name].dtype) != np.dtype("float32"):
            raise ValueError(f"{feature_name} Zarr array must have float32 dtype: {root}")
        observed = _feature_gripper_indices(info, feature_name)
        if observed != expected_indices:
            raise ValueError(
                f"{feature_name} gripper roles/indices must be right=6,left=16: "
                f"observed={observed}: {root}"
            )
        names_by_feature[feature_name] = names
        channels[feature_name] = observed
    if names_by_feature["observation.state"] != names_by_feature["action"]:
        raise ValueError(f"Zarr state/action feature names differ: {root}")
    if list(zroot.attrs.get("state_names") or []) != names_by_feature["observation.state"]:
        raise ValueError(f"Zarr state_names attribute differs from info.json: {root}")
    if list(zroot.attrs.get("action_names") or []) != names_by_feature["action"]:
        raise ValueError(f"Zarr action_names attribute differs from info.json: {root}")
    return channels


def _embedded_source_session(root: Path) -> str | None:
    sync_path = root / "sync_report.json"
    if not sync_path.is_file():
        return None
    embedded = ((_read_json(sync_path).get("calibration") or {}).get("embedded_report") or {})
    if not embedded:
        return None
    if embedded.get("status") != "accepted" or not embedded.get("session_dir"):
        raise ValueError(f"Zarr embedded source calibration is not accepted: {sync_path}")
    return str(Path(str(embedded["session_dir"])).expanduser().resolve())


def _validate_zarr_shard(root: Path) -> dict[str, Any]:
    np, zarr = _load_zarr_modules()
    manifest_path = root / "manifest.json"
    quality_path = root / "meta" / "zarr_quality_certificate.json"
    info_path = root / "meta" / "info.json"
    for path in (manifest_path, quality_path, info_path, root / "meta" / "tasks.jsonl"):
        if not path.is_file():
            raise ValueError(f"incomplete Zarr shard; required file is missing: {path}")
    manifest = _read_json(manifest_path)
    quality = _read_json(quality_path)
    info = _read_json(info_path)
    zroot = zarr.open_group(str(root), mode="r")
    schema = str(zroot.attrs.get("schema") or "")
    if schema not in ZARR_SHARD_SCHEMAS:
        raise ValueError(f"unsupported Zarr shard schema {schema!r}: {root}")
    if (
        manifest.get("schema") != schema
        or manifest.get("status") != "complete"
        or manifest.get("numeric_exact_roundtrip_equal") is not True
    ):
        raise ValueError(f"Zarr publication manifest is incomplete: {manifest_path}")
    if (
        quality.get("schema") != ZARR_QUALITY_SCHEMA
        or quality.get("status") != "accepted"
        or quality.get("numeric_exact_roundtrip_equal") is not True
        or zroot.attrs.get("quality_status") != "accepted"
    ):
        raise ValueError(f"Zarr quality certificate is not accepted: {quality_path}")
    _validate_identity(root, manifest.get("quality_certificate"), "quality certificate")

    fps = int(zroot.attrs.get("fps", -1))
    frames = int(zroot.attrs.get("total_frames", -1))
    episodes = int(zroot.attrs.get("total_episodes", -1))
    if fps <= 0 or frames <= 0 or episodes <= 0:
        raise ValueError(f"invalid Zarr coverage/FPS metadata: {root}")
    if (
        int(info.get("fps", -1)) != fps
        or int(info.get("total_frames", -1)) != frames
        or int(info.get("total_episodes", -1)) != episodes
        or int(manifest.get("frames", -1)) != frames
        or int(manifest.get("episodes", -1)) != episodes
        or int(quality.get("frames", -1)) != frames
        or int(quality.get("episodes", -1)) != episodes
    ):
        raise ValueError(f"Zarr metadata coverage is inconsistent: {root}")
    try:
        data = zroot["data"]
        meta = zroot["meta"]
        state = data["observation_state"]
        action = data["action"]
        episode_ends = np.asarray(meta["episode_ends"][:], dtype=np.int64)
    except KeyError as exc:
        raise ValueError(f"Zarr shard is missing a required numeric array: {root}: {exc}") from exc
    if state.shape != (frames, 20) or action.shape != (frames, 20):
        raise ValueError(f"Zarr state/action shape is inconsistent: {root}")
    if (
        episode_ends.shape != (episodes,)
        or int(episode_ends[-1]) != frames
        or np.any(np.diff(episode_ends) <= 0)
    ):
        raise ValueError(f"Zarr episode boundaries are invalid: {root}")
    feature_channels = _validate_zarr_vector_contract(
        np, zroot, info, state, action, root
    )
    gripper_transform = _zarr_gripper_transform(root)
    continuous_quality = _validate_continuous_gripper_quality(
        quality, gripper_transform, root
    )
    return {
        "root": root,
        "zroot": zroot,
        "schema": schema,
        "manifest": manifest,
        "quality": quality,
        "info": info,
        "fps": fps,
        "frames": frames,
        "episodes": episodes,
        "episode_ends": episode_ends,
        "feature_channels": feature_channels,
        "gripper_transform": gripper_transform,
        "continuous_gripper_quality": continuous_quality,
        "embedded_source_session": _embedded_source_session(root),
        "task": _task_name_zarr(root),
    }


def _source_mode(transform: Any) -> str:
    if (
        isinstance(transform, dict)
        and transform.get("kind") == "binarize_calibrated_gripper_width_at_threshold"
    ):
        return "binary"
    # For audited Zarr data the recorded transform is authoritative.  A
    # continuous episode can legitimately contain only zero-width samples and
    # must not be mislabeled as a binary export by value sniffing.
    return "continuous" if transform is None else "transformed"


def _extract_zarr_shard_episode(
    descriptor: dict[str, Any],
    local_episode_index: int,
    *,
    max_frames: int,
    dataset_root: Path,
    dataset_name: str,
    source_kind: str,
    public_episode_index: int,
    episode_indices: list[int],
) -> dict[str, Any]:
    np, _zarr = _load_zarr_modules()
    if local_episode_index < 0 or local_episode_index >= descriptor["episodes"]:
        raise ValueError(
            f"episode {public_episode_index} is absent; available range is "
            f"{episode_indices[0]}..{episode_indices[-1]} ({len(episode_indices)} episodes)"
        )
    ends = descriptor["episode_ends"]
    start = 0 if local_episode_index == 0 else int(ends[local_episode_index - 1])
    end = int(ends[local_episode_index])
    frame_count = end - start
    if frame_count <= 0 or frame_count > max_frames:
        raise ValueError(
            f"Zarr episode {public_episode_index} has {frame_count} frames, exceeding "
            f"the read-only limit of {max_frames}"
        )
    data = descriptor["zroot"]["data"]
    arrays = {
        "observation.state": np.asarray(data["observation_state"][start:end]),
        "action": np.asarray(data["action"][start:end]),
    }
    for feature_name, array in arrays.items():
        if array.shape != (frame_count, 20) or not np.isfinite(array).all():
            raise ValueError(
                f"selected Zarr episode has invalid/non-finite {feature_name} values: "
                f"{descriptor['root']}::episode[{local_episode_index}]"
            )
    channel_order: list[str] = []
    channel_names: dict[str, dict[str, str]] = {}
    extracted: dict[str, dict[str, list[float | None]]] = {}
    for feature in VECTOR_COLUMNS:
        extracted[feature] = {}
        for channel_id, dimension_name, index in descriptor["feature_channels"][feature]:
            if channel_id not in channel_order:
                channel_order.append(channel_id)
            channel_names.setdefault(channel_id, {})[feature] = dimension_name
            extracted[feature][channel_id] = [
                _finite_or_none(value) for value in arrays[feature][:, index].tolist()
            ]
    colors = {"right": "#16a26a", "left": "#087af5", "gripper": "#7a5af8"}
    channels: list[dict[str, Any]] = []
    for position, channel_id in enumerate(channel_order):
        series = {
            "observation_state": extracted["observation.state"].get(channel_id),
            "action": extracted["action"].get(channel_id),
        }
        channels.append(
            {
                "id": channel_id,
                "label": {
                    "right": "Right gripper",
                    "left": "Left gripper",
                    "gripper": "Gripper",
                }.get(channel_id, channel_id.replace("_", " ").title()),
                "color": colors.get(
                    channel_id, f"hsl({(position * 83 + 26) % 360} 68% 45%)"
                ),
                "dimensions": channel_names[channel_id],
                "series": series,
            }
        )
    transform = descriptor["gripper_transform"]
    fps = float(descriptor["fps"])
    return {
        "schema": "instadatacap.gripper_binary_preview.v1",
        "read_only": True,
        "source_kind": source_kind,
        "dataset_root": str(dataset_root),
        "dataset_name": dataset_name,
        "task": descriptor["task"],
        "episode_index": public_episode_index,
        "episode_indices": episode_indices,
        "episode_count": len(episode_indices),
        "episode_file": f"{descriptor['root']}::episode[{local_episode_index}]",
        "source_shard_root": str(descriptor["root"]),
        "source_shard_episode_index": local_episode_index,
        "frame_count": frame_count,
        "fps": fps,
        "duration_sec": frame_count / fps,
        "source_mode": _source_mode(transform),
        "continuous_gripper_quality_status": descriptor[
            "continuous_gripper_quality"
        ]["status"],
        "source_quality_warning": descriptor["continuous_gripper_quality"]["warning"],
        "conversion_complete_schema": descriptor["schema"],
        "source_gripper_transform": transform,
        "series_options": [
            {"id": "observation_state", "label": "observation.state"},
            {"id": "action", "label": "action"},
        ],
        "channels": channels,
        "comparison_rule": "width < threshold -> 0; width >= threshold -> 1",
    }


def _current_identity(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _validate_collection_identity(shard_root: Path, identities: Any) -> str:
    if not isinstance(identities, dict) or not identities:
        raise ValueError(f"Zarr collection shard has no immutable identities: {shard_root}")
    required_paths = {
        "manifest": "manifest.json",
        "quality": "meta/zarr_quality_certificate.json",
        "sync": "sync_report.json",
    }
    if not set(required_paths).issubset(identities):
        raise ValueError(
            f"Zarr collection shard identities lack manifest/quality/sync: {shard_root}"
        )
    for label, identity in identities.items():
        _validate_identity(shard_root, identity, f"collection shard {label}")
    for label, relative in required_paths.items():
        expected = _current_identity(shard_root / relative, shard_root)
        if _canonical_bytes(identities[label]) != _canonical_bytes(expected):
            raise ValueError(
                f"Zarr collection {label} identity is not bound to {relative}: {shard_root}"
            )
    return hashlib.sha256(_canonical_bytes(identities)).hexdigest()


def _validate_collection_ranges(
    collection: dict[str, Any],
    total_episodes: int,
    total_frames: int,
    collection_path: Path,
) -> list[dict[str, Any]]:
    records = collection.get("shards")
    if not isinstance(records, list) or not records:
        raise ValueError(f"Zarr collection contains no shard records: {collection_path}")
    frame_cursor = 0
    episode_cursor = 0
    shard_ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Zarr collection shard {index} is malformed: {collection_path}")
        try:
            frames = int(record["frames"])
            episodes = int(record["episodes"])
            frame_start = int(record["global_frame_start"])
            frame_end = int(record["global_frame_end_exclusive"])
            episode_start = int(record["global_episode_start"])
            episode_end = int(record["global_episode_end_exclusive"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Zarr collection shard {index} has invalid coverage metadata"
            ) from exc
        if (
            frames <= 0
            or episodes <= 0
            or frame_start != frame_cursor
            or episode_start != episode_cursor
            or frame_end != frame_start + frames
            or episode_end != episode_start + episodes
        ):
            raise ValueError(
                f"Zarr collection global ranges are not contiguous/consistent at shard {index}"
            )
        shard_id = record.get("shard_id")
        if (
            not isinstance(shard_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", shard_id) is None
            or shard_id in shard_ids
        ):
            raise ValueError(f"Zarr collection shard_id is invalid/duplicate at shard {index}")
        identities = record.get("identities")
        if not isinstance(identities, dict) or not identities:
            raise ValueError(f"Zarr collection identities are absent at shard {index}")
        if hashlib.sha256(_canonical_bytes(identities)).hexdigest() != shard_id:
            raise ValueError(f"Zarr collection shard_id does not match identities at shard {index}")
        if not isinstance(record.get("path"), str) or not record["path"]:
            raise ValueError(f"Zarr collection shard path is absent at shard {index}")
        shard_ids.add(shard_id)
        frame_cursor = frame_end
        episode_cursor = episode_end
    if frame_cursor != total_frames or episode_cursor != total_episodes:
        raise ValueError(
            "Zarr collection shard ranges do not equal the declared frame/episode totals"
        )
    return records


def _extract_zarr_preview(
    kind: str,
    root: Path,
    episode_index: int | None,
    *,
    max_frames: int,
) -> dict[str, Any]:
    if kind == "shard":
        descriptor = _validate_zarr_shard(root)
        indices = list(range(descriptor["episodes"]))
        selected = indices[0] if episode_index is None else episode_index
        return _extract_zarr_shard_episode(
            descriptor,
            selected,
            max_frames=max_frames,
            dataset_root=root,
            dataset_name=root.name,
            source_kind="zarr",
            public_episode_index=selected,
            episode_indices=indices,
        )

    collection_path = root / "collection.json"
    manifest_path = root / "manifest.json"
    collection = _read_json(collection_path)
    manifest = _read_json(manifest_path)
    if collection.get("schema") != ZARR_COLLECTION_SCHEMA or collection.get("status") != "complete":
        raise ValueError(f"unsupported or incomplete Zarr collection: {collection_path}")
    expected_hash = collection.get("collection_hash")
    unhashed = dict(collection)
    unhashed.pop("collection_hash", None)
    if expected_hash != hashlib.sha256(_canonical_bytes(unhashed)).hexdigest():
        raise ValueError(f"Zarr collection hash is invalid: {collection_path}")
    try:
        total_episodes = int(collection.get("episodes", -1))
        total_frames = int(collection.get("frames", -1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Zarr collection coverage: {collection_path}") from exc
    if (
        manifest.get("schema") != ZARR_COLLECTION_SCHEMA
        or manifest.get("status") != "complete"
        or manifest.get("collection_hash") != expected_hash
        or int(manifest.get("episodes", -1)) != total_episodes
        or int(manifest.get("frames", -1)) != total_frames
        or total_episodes <= 0
        or total_frames <= 0
    ):
        raise ValueError(f"Zarr collection manifest is inconsistent: {manifest_path}")
    records = _validate_collection_ranges(
        collection, total_episodes, total_frames, collection_path
    )
    if "shards" in manifest and int(manifest.get("shards", -1)) != len(records):
        raise ValueError(f"Zarr collection manifest shard count is inconsistent: {manifest_path}")
    indices = list(range(total_episodes))
    selected = indices[0] if episode_index is None else episode_index
    if selected < 0 or selected >= total_episodes:
        raise ValueError(
            f"episode {selected} is absent; available range is 0..{total_episodes - 1} "
            f"({total_episodes} episodes)"
        )
    selected_record: dict[str, Any] | None = None
    for record in records:
        first = int(record["global_episode_start"])
        last = int(record["global_episode_end_exclusive"])
        if first <= selected < last:
            if selected_record is not None:
                raise ValueError(f"overlapping Zarr collection episode ranges: {selected}")
            selected_record = record
    if selected_record is None:
        raise ValueError(f"Zarr collection does not map global episode {selected}")
    relative = Path(str(selected_record.get("path", "")))
    shard_root = (
        relative.resolve(strict=True)
        if relative.is_absolute()
        else (root / relative).resolve(strict=True)
    )
    computed_shard_id = _validate_collection_identity(
        shard_root, selected_record.get("identities")
    )
    if selected_record.get("shard_id") != computed_shard_id:
        raise ValueError(f"Zarr collection selected shard_id is not immutable: {shard_root}")
    descriptor = _validate_zarr_shard(shard_root)
    first = int(selected_record["global_episode_start"])
    last = int(selected_record["global_episode_end_exclusive"])
    if (
        descriptor["episodes"] != int(selected_record["episodes"])
        or descriptor["frames"] != int(selected_record["frames"])
        or descriptor["episodes"] != last - first
        or int(selected_record["global_frame_end_exclusive"])
        - int(selected_record["global_frame_start"])
        != descriptor["frames"]
    ):
        raise ValueError(f"Zarr collection/shard frame or episode coverage mismatch: {shard_root}")
    recorded_session = selected_record.get("source_capture_session_dir")
    embedded_session = descriptor["embedded_source_session"]
    if (
        not isinstance(recorded_session, str)
        or not recorded_session
        or embedded_session is None
        or str(Path(recorded_session).expanduser().resolve()) != embedded_session
    ):
        raise ValueError(f"Zarr collection/shard source capture binding mismatch: {shard_root}")
    capture_binding = selected_record.get("source_capture_binding")
    if capture_binding is not None:
        if (
            not isinstance(capture_binding, dict)
            or str(Path(str(capture_binding.get("session_dir", ""))).expanduser().resolve())
            != embedded_session
            or int(capture_binding.get("verified_raw_input_files", 0)) <= 0
        ):
            raise ValueError(f"Zarr collection raw-source binding is malformed: {shard_root}")
    compatibility = collection.get("compatibility") or {}
    compatibility_transform = compatibility.get("gripper_transform")
    if _canonical_bytes(compatibility_transform) != _canonical_bytes(
        descriptor["gripper_transform"]
    ):
        raise ValueError(f"Zarr collection/shard gripper semantics mismatch: {shard_root}")
    if (
        int(compatibility.get("fps", -1)) != descriptor["fps"]
        or list(compatibility.get("state_names") or [])
        != descriptor["info"]["features"]["observation.state"]["names"]
        or list(compatibility.get("action_names") or [])
        != descriptor["info"]["features"]["action"]["names"]
        or _canonical_bytes(compatibility.get("continuous_gripper_quality_contract"))
        != _canonical_bytes(descriptor["continuous_gripper_quality"]["contract"])
    ):
        raise ValueError(f"Zarr collection/shard numeric or quality contract mismatch: {shard_root}")
    return _extract_zarr_shard_episode(
        descriptor,
        selected - first,
        max_frames=max_frames,
        dataset_root=root,
        dataset_name=root.name,
        source_kind="zarr_collection",
        public_episode_index=selected,
        episode_indices=indices,
    )


def _gripper_channel_id(name: str) -> str | None:
    normalized = name.strip().lower()
    if normalized == "right_gripper":
        return "right"
    if normalized == "left_gripper":
        return "left"
    if normalized == "gripper":
        return "gripper"
    if normalized.endswith("_gripper"):
        return normalized.removesuffix("_gripper") or "gripper"
    return None


def _feature_gripper_indices(
    info: dict[str, Any], feature_name: str
) -> list[tuple[str, str, int]]:
    feature = (info.get("features") or {}).get(feature_name) or {}
    names = feature.get("names")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f"{feature_name} has no named vector dimensions")
    result: list[tuple[str, str, int]] = []
    for index, name in enumerate(names):
        channel_id = _gripper_channel_id(name)
        if channel_id is not None:
            result.append((channel_id, name, index))
    if not result:
        raise ValueError(f"{feature_name} has no gripper dimension: {names}")
    if len({item[0] for item in result}) != len(result):
        raise ValueError(f"{feature_name} contains duplicate gripper roles")
    return result


def _finite_or_none(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _extract_feature_series(
    table: Any,
    feature_name: str,
    channels: list[tuple[str, str, int]],
) -> dict[str, list[float | None]]:
    array = table[feature_name].combine_chunks()
    rows = array.to_pylist()
    output: dict[str, list[float | None]] = {}
    for channel_id, _name, index in channels:
        values: list[float | None] = []
        for row_number, row in enumerate(rows):
            if not isinstance(row, (list, tuple)) or index >= len(row):
                raise ValueError(
                    f"invalid {feature_name} vector at row {row_number}"
                )
            values.append(_finite_or_none(row[index]))
        output[channel_id] = values
    return output


def _task_name(dataset_root: Path) -> str:
    path = dataset_root / "meta" / "tasks.jsonl"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return ""
        return str(record.get("task", ""))
    return ""


def _validate_completed_dataset(dataset_root: Path, info: dict[str, Any]) -> dict[str, Any]:
    marker_path = dataset_root / "conversion_complete.json"
    marker = _read_json(marker_path)
    schema = str(marker.get("schema") or "")
    if not schema.startswith("instaumi-") or not schema.endswith("complete-v1"):
        raise ValueError(f"unsupported or incomplete conversion marker: {marker_path}")
    expected_episodes = int(info.get("total_episodes") or 0)
    expected_frames = int(info.get("total_frames") or 0)
    if expected_episodes <= 0 or expected_frames <= 0:
        raise ValueError(f"LeRobot metadata is incomplete: {dataset_root / 'meta' / 'info.json'}")
    if int(marker.get("episodes") or -1) != expected_episodes:
        raise ValueError(f"conversion marker episode count does not match metadata: {marker_path}")
    if int(marker.get("frames") or -1) != expected_frames:
        raise ValueError(f"conversion marker frame count does not match metadata: {marker_path}")
    plan_path = dataset_root / "conversion_plan.json"
    if plan_path.is_file():
        plan = _read_json(plan_path)
        if marker.get("plan_hash") != plan.get("plan_hash"):
            raise ValueError(f"conversion marker does not match conversion plan: {marker_path}")
    return marker


def _path_from_manifest(manifest_path: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing from gripper preview manifest: {manifest_path}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _load_arm_estimation_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path)
    if manifest.get("schema") != GRIPPER_ESTIMATION_SCHEMA:
        raise ValueError(f"unsupported gripper-estimation manifest: {path}")
    processed_frames = int(manifest.get("processed_frames") or 0)
    if processed_frames <= 0:
        raise ValueError(f"gripper-estimation manifest is incomplete: {path}")
    validation = manifest.get("validation") or {}
    if int(validation.get("output_frame_count") or -1) != processed_frames:
        raise ValueError(f"gripper-estimation frame validation is incomplete: {path}")
    if validation.get("timestamp_indices_continuous") is not True:
        raise ValueError(f"gripper-estimation frame indices are not validated: {path}")
    return manifest


def _arm_csv_descriptor(
    arm: str,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    csv_override: Any = None,
    value_column: Any = None,
) -> dict[str, Any]:
    csv_path = _path_from_manifest(
        manifest_path,
        csv_override if csv_override is not None else manifest.get("output_csv"),
        f"{arm}.output_csv",
    )
    if not csv_path.is_file():
        raise ValueError(f"missing {arm} gripper-estimation CSV: {csv_path}")
    expected_hash = manifest.get("output_csv_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError(f"{arm} gripper-estimation manifest has no CSV SHA-256: {manifest_path}")
    actual_hash = _sha256_file(csv_path)
    if actual_hash != expected_hash:
        raise ValueError(
            f"{arm} gripper-estimation CSV does not match its manifest: {csv_path}"
        )
    requested_column = None if value_column is None else str(value_column)
    return {
        "arm": arm,
        "csv": csv_path,
        "manifest": manifest_path,
        "manifest_payload": manifest,
        "processed_frames": int(manifest["processed_frames"]),
        "value_column": requested_column,
    }


def _range_map_from_arm_manifest(
    arm: str, manifest_path: Path, manifest: dict[str, Any]
) -> dict[int, dict[str, int]]:
    selection = manifest.get("active_frame_selection") or {}
    ranges = selection.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError(
            f"{arm} gripper-estimation manifest has no capture-segment ranges: {manifest_path}"
        )
    result: dict[int, dict[str, int]] = {}
    for item in ranges:
        if not isinstance(item, dict):
            raise ValueError(f"invalid {arm} segment range in {manifest_path}")
        try:
            index = int(item["segment_index"])
            start = int(item["start_frame"])
            end = int(item["end_frame"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid {arm} segment range in {manifest_path}: {item}") from exc
        if index < 0 or start < 0 or end <= start:
            raise ValueError(f"invalid {arm} segment bounds in {manifest_path}: {item}")
        if end > int(manifest["processed_frames"]):
            raise ValueError(f"{arm} segment exceeds estimated CSV length: {item}")
        if index in result:
            raise ValueError(f"duplicate {arm} segment {index} in {manifest_path}")
        result[index] = {"start_frame": start, "end_frame": end}
    return result


def _fps_from_arm_manifests(arms: dict[str, dict[str, Any]]) -> float:
    values: list[float] = []
    for descriptor in arms.values():
        probe = (
            descriptor["manifest_payload"].get("source_layout", {})
            .get("canonical_probe", {})
        )
        value = _finite_or_none(probe.get("fps_float"))
        if value is not None and value > 0:
            values.append(value)
    if not values:
        return 0.0
    if max(values) - min(values) > 1e-3:
        raise ValueError(f"left/right gripper artifacts have inconsistent frame rates: {values}")
    return sum(values) / len(values)


def _descriptor_from_bimanual_manifest(
    manifest_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    arms: dict[str, dict[str, Any]] = {}
    range_maps: dict[str, dict[int, dict[str, int]]] = {}
    for arm in ("right", "left"):
        entry = payload.get(arm)
        if not isinstance(entry, dict):
            raise ValueError(f"bimanual gripper manifest has no {arm} entry: {manifest_path}")
        if entry.get("episode") != arm:
            raise ValueError(f"bimanual gripper manifest has invalid {arm} role: {manifest_path}")
        if entry.get("gripper_dir"):
            gripper_dir = _path_from_manifest(
                manifest_path, entry["gripper_dir"], f"{arm}.gripper_dir"
            )
            arm_manifest_path = gripper_dir / "manifest.json"
        else:
            csv_path = _path_from_manifest(
                manifest_path, entry.get("gripper_csv"), f"{arm}.gripper_csv"
            )
            arm_manifest_path = csv_path.parent / "manifest.json"
        arm_manifest_path = arm_manifest_path.resolve()
        arm_manifest = _load_arm_estimation_manifest(arm_manifest_path)
        arms[arm] = _arm_csv_descriptor(
            arm,
            arm_manifest_path,
            arm_manifest,
            csv_override=entry.get("gripper_csv"),
        )
        range_maps[arm] = _range_map_from_arm_manifest(
            arm, arm_manifest_path, arm_manifest
        )
    episode_indices = sorted(set(range_maps["right"]) & set(range_maps["left"]))
    if not episode_indices:
        raise ValueError(f"left/right gripper artifacts share no capture segments: {manifest_path}")
    if set(range_maps["right"]) != set(range_maps["left"]):
        raise ValueError(f"left/right gripper segment sets differ: {manifest_path}")
    episodes = {
        index: {
            "right": range_maps["right"][index],
            "left": range_maps["left"][index],
        }
        for index in episode_indices
    }
    return {
        "manifest": manifest_path,
        "root": manifest_path.parent.parent,
        "name": manifest_path.parent.parent.name,
        "task": "",
        "fps": _fps_from_arm_manifests(arms),
        "arms": arms,
        "episodes": episodes,
        "episode_indices": episode_indices,
    }


def _descriptor_from_threshold_preview_manifest(
    manifest_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """Load the compact manifest emitted by an early threshold-sampling pass.

    Contract::

      Full `_03` range form:
        arms.<role>.manifest  completed `_03` arm manifest
        arms.<role>.csv       optional CSV override (normally omitted)
        episodes[].<role>     {start_frame, end_frame}, end-exclusive

      Standalone sampled-trace form (no complete `_03` or LeRobot required):
        fps: 30
        episodes[].<role>: {
          trace_csv, sha256, frame_count, value_column,
          valid_column?  # explicit false/0 makes that sample null
        }

    Keeping ranges in the manifest lets the pipeline estimate only selected
    representative capture segments; DataCap never needs images or LeRobot.
    """

    episode_entries = payload.get("episodes")
    if not isinstance(episode_entries, list):
        raise ValueError(f"invalid lightweight gripper preview manifest: {manifest_path}")
    arms: dict[str, dict[str, Any]] = {}
    arm_entries = payload.get("arms")
    if arm_entries is not None:
        if not isinstance(arm_entries, dict):
            raise ValueError(f"invalid arms object in lightweight preview manifest: {manifest_path}")
        for arm in ("right", "left"):
            entry = arm_entries.get(arm)
            if not isinstance(entry, dict):
                raise ValueError(f"lightweight preview manifest has no {arm} arm: {manifest_path}")
            arm_manifest_path = _path_from_manifest(
                manifest_path, entry.get("manifest"), f"arms.{arm}.manifest"
            )
            arm_manifest = _load_arm_estimation_manifest(arm_manifest_path)
            arms[arm] = _arm_csv_descriptor(
                arm,
                arm_manifest_path,
                arm_manifest,
                csv_override=entry.get("csv"),
                value_column=entry.get("value_column"),
            )

    def trace_descriptor(arm: str, index: int, value: dict[str, Any]) -> dict[str, Any]:
        trace_path = _path_from_manifest(
            manifest_path,
            value.get("trace_csv"),
            f"episodes[{index}].{arm}.trace_csv",
        )
        if not trace_path.is_file():
            raise ValueError(f"missing {arm} sampled gripper trace: {trace_path}")
        expected_hash = value.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"episode {index} {arm} trace has no SHA-256: {manifest_path}")
        if _sha256_file(trace_path) != expected_hash:
            raise ValueError(f"episode {index} {arm} trace does not match manifest: {trace_path}")
        try:
            frame_count = int(value["frame_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"episode {index} {arm} trace has no frame_count") from exc
        if frame_count <= 0:
            raise ValueError(f"episode {index} {arm} trace has invalid frame_count={frame_count}")
        valid_column = value.get("valid_column")
        if valid_column is not None and not isinstance(valid_column, str):
            raise ValueError(f"episode {index} {arm} valid_column must be a string")
        value_column = value.get("value_column", "width_m")
        if not isinstance(value_column, str) or not value_column:
            raise ValueError(f"episode {index} {arm} value_column must be a string")
        return {
            "trace_csv": trace_path,
            "sha256": expected_hash,
            "frame_count": frame_count,
            "value_column": value_column,
            "valid_column": valid_column,
        }
    episodes: dict[int, dict[str, dict[str, int]]] = {}
    for item in episode_entries:
        if not isinstance(item, dict):
            raise ValueError(f"invalid episode in lightweight preview manifest: {manifest_path}")
        try:
            index = int(item["episode_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid episode index in {manifest_path}: {item}") from exc
        if index < 0 or index in episodes:
            raise ValueError(f"invalid or duplicate episode {index} in {manifest_path}")
        ranges: dict[str, dict[str, int]] = {}
        for arm in ("right", "left"):
            value = item.get(arm)
            if not isinstance(value, dict):
                raise ValueError(f"episode {index} has no {arm} range: {manifest_path}")
            if "trace_csv" in value:
                ranges[arm] = {"trace": trace_descriptor(arm, index, value)}
                continue
            if arm not in arms:
                raise ValueError(
                    f"episode {index} {arm} must provide trace_csv when no arms.{arm} `_03` source exists"
                )
            try:
                start = int(value["start_frame"])
                end = int(value["end_frame"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"episode {index} has invalid {arm} range: {value}") from exc
            if start < 0 or end <= start or end > arms[arm]["processed_frames"]:
                raise ValueError(f"episode {index} has invalid {arm} bounds: {value}")
            ranges[arm] = {"start_frame": start, "end_frame": end}
        episodes[index] = ranges
    if not episodes:
        raise ValueError(f"lightweight preview manifest contains no episodes: {manifest_path}")
    fps = _finite_or_none(payload.get("fps"))
    if fps is None or fps <= 0:
        if not arms:
            raise ValueError(f"standalone sampled-trace manifest requires a positive fps: {manifest_path}")
        fps = _fps_from_arm_manifests(arms)
    return {
        "manifest": manifest_path,
        "root": manifest_path.parent,
        "name": str(payload.get("name") or manifest_path.parent.name),
        "task": str(payload.get("task") or ""),
        "fps": fps,
        "arms": arms,
        "episodes": episodes,
        "episode_indices": sorted(episodes),
    }


def _load_gripper_artifact_descriptor(manifest_path: Path) -> dict[str, Any]:
    payload = _read_json(manifest_path)
    schema = payload.get("schema")
    if schema == BIMANUAL_GRIPPER_SCHEMA:
        return _descriptor_from_bimanual_manifest(manifest_path, payload)
    if schema == THRESHOLD_PREVIEW_SCHEMA:
        return _descriptor_from_threshold_preview_manifest(manifest_path, payload)
    raise ValueError(f"unsupported gripper preview manifest schema {schema!r}: {manifest_path}")


def _read_gripper_csv_range(
    descriptor: dict[str, Any],
    start_frame: int,
    end_frame: int,
    *,
    max_frames: int,
) -> tuple[list[float | None], str]:
    if end_frame - start_frame > max_frames:
        raise ValueError(
            f"gripper preview range has {end_frame - start_frame} frames, exceeding "
            f"the read-only limit of {max_frames}"
        )
    path: Path = descriptor["csv"]
    identity_before = (path.stat().st_size, path.stat().st_mtime_ns)
    values: list[float | None] = []
    resolved_column: str | None = None
    with path.open("r", encoding="ascii", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        requested_column = descriptor.get("value_column")
        if requested_column:
            if requested_column not in fieldnames:
                raise ValueError(f"{path} has no requested gripper column {requested_column!r}")
            resolved_column = requested_column
        else:
            resolved_column = next(
                (column for column in GRIPPER_VALUE_COLUMNS if column in fieldnames),
                None,
            )
        if resolved_column is None:
            raise ValueError(f"{path} has no calibrated gripper-width column")
        if "frame_idx" not in fieldnames:
            raise ValueError(f"{path} has no frame_idx column")
        for row_number, row in enumerate(reader):
            if row_number >= end_frame:
                break
            try:
                frame_idx = int(row.get("frame_idx", ""))
            except ValueError as exc:
                raise ValueError(f"invalid frame_idx at CSV row {row_number}: {path}") from exc
            if frame_idx != row_number:
                raise ValueError(
                    f"non-contiguous gripper CSV at row {row_number}: "
                    f"frame_idx={frame_idx}, path={path}"
                )
            if row_number >= start_frame:
                values.append(_finite_or_none(row.get(resolved_column)))
    if len(values) != end_frame - start_frame:
        raise ValueError(
            f"gripper CSV ended before requested range {start_frame}:{end_frame}: {path}"
        )
    stat_after = path.stat()
    if identity_before != (stat_after.st_size, stat_after.st_mtime_ns):
        raise RuntimeError(f"gripper CSV changed while previewing it: {path}")
    return values, resolved_column


def _valid_flag(value: Any) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ValueError(f"invalid trace validity flag: {value!r}")


def _read_sampled_gripper_trace(
    trace: dict[str, Any], *, max_frames: int
) -> tuple[list[float | None], str]:
    frame_count = int(trace["frame_count"])
    if frame_count > max_frames:
        raise ValueError(
            f"sampled gripper trace has {frame_count} frames, exceeding "
            f"the read-only limit of {max_frames}"
        )
    path: Path = trace["trace_csv"]
    identity_before = (path.stat().st_size, path.stat().st_mtime_ns)
    value_column = trace["value_column"]
    valid_column = trace.get("valid_column")
    values: list[float | None] = []
    with path.open("r", encoding="ascii", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        if value_column not in fieldnames:
            raise ValueError(f"sampled trace {path} has no value column {value_column!r}")
        if valid_column is not None and valid_column not in fieldnames:
            raise ValueError(f"sampled trace {path} has no validity column {valid_column!r}")
        frame_column = "frame_idx" if "frame_idx" in fieldnames else None
        for row_number, row in enumerate(reader):
            if row_number >= frame_count:
                raise ValueError(
                    f"sampled trace has more than declared {frame_count} frames: {path}"
                )
            if frame_column is not None:
                try:
                    actual_frame = int(row.get(frame_column, ""))
                except ValueError as exc:
                    raise ValueError(f"invalid frame_idx at trace row {row_number}: {path}") from exc
                if actual_frame != row_number:
                    raise ValueError(
                        f"sampled trace frame_idx is not contiguous at row {row_number}: {path}"
                    )
            valid = True if valid_column is None else _valid_flag(row.get(valid_column))
            values.append(_finite_or_none(row.get(value_column)) if valid else None)
    if len(values) != frame_count:
        raise ValueError(
            f"sampled trace row count {len(values)} does not match frame_count "
            f"{frame_count}: {path}"
        )
    stat_after = path.stat()
    if identity_before != (stat_after.st_size, stat_after.st_mtime_ns):
        raise RuntimeError(f"sampled gripper trace changed while previewing it: {path}")
    return values, value_column


def extract_gripper_artifact_preview(
    manifest_path: Path,
    episode_index: int | None,
    *,
    max_frames: int,
) -> dict[str, Any]:
    descriptor = _load_gripper_artifact_descriptor(manifest_path)
    indices = descriptor["episode_indices"]
    if episode_index is None:
        episode_index = indices[0]
    if episode_index not in descriptor["episodes"]:
        raise ValueError(
            f"capture segment {episode_index} is absent; available range is "
            f"{indices[0]}..{indices[-1]} ({len(indices)} segments)"
        )
    episode = descriptor["episodes"][episode_index]
    colors = {"right": "#16a26a", "left": "#087af5"}
    labels = {"right": "Right gripper", "left": "Left gripper"}
    channels: list[dict[str, Any]] = []
    frame_counts: list[int] = []
    columns: dict[str, str] = {}
    for arm in ("right", "left"):
        bounds = episode[arm]
        if "trace" in bounds:
            values, column = _read_sampled_gripper_trace(
                bounds["trace"], max_frames=max_frames
            )
            source_frame_range = None
        else:
            values, column = _read_gripper_csv_range(
                descriptor["arms"][arm],
                bounds["start_frame"],
                bounds["end_frame"],
                max_frames=max_frames,
            )
            source_frame_range = [bounds["start_frame"], bounds["end_frame"]]
        frame_counts.append(len(values))
        columns[arm] = column
        channels.append(
            {
                "id": arm,
                "label": labels[arm],
                "color": colors[arm],
                "dimensions": {"estimated_width": column},
                "series": {"estimated_width": values},
                "source_frame_range": source_frame_range,
            }
        )
    frame_count = max(frame_counts)
    fps = float(descriptor["fps"] or 0)
    return {
        "schema": "instadatacap.gripper_binary_preview.v1",
        "read_only": True,
        "source_kind": "pre_lerobot_gripper_estimation",
        "dataset_root": str(descriptor["root"]),
        "dataset_name": descriptor["name"],
        "task": descriptor["task"],
        "episode_index": episode_index,
        "episode_indices": indices,
        "episode_count": len(indices),
        "episode_file": None,
        "frame_count": frame_count,
        "fps": fps,
        "duration_sec": frame_count / fps if fps > 0 else None,
        "source_mode": "continuous",
        "conversion_complete_schema": None,
        "source_gripper_transform": None,
        "artifact_manifest": str(manifest_path),
        "gripper_columns": columns,
        "series_options": [
            {"id": "estimated_width", "label": "Calibrated _03 estimate"}
        ],
        "channels": channels,
        "comparison_rule": "width < threshold -> 0; width >= threshold -> 1",
    }


def _extract_lerobot_episode_preview(
    input_path: str | Path,
    episode_index: int | None = None,
    *,
    max_frames: int = 200_000,
) -> dict[str, Any]:
    try:
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "the selected data Python has no pyarrow; configure "
            "data_preview.python to the InstaUMI processing environment"
        ) from exc

    dataset_root, episode_from_file = resolve_dataset_root(input_path)
    episode_files = discover_episode_files(dataset_root)
    if episode_from_file is not None:
        if episode_index is not None and episode_index != episode_from_file:
            raise ValueError(
                f"episode file is {episode_from_file}, but requested {episode_index}"
            )
        episode_index = episode_from_file
    if episode_index is None:
        episode_index = min(episode_files)
    if episode_index not in episode_files:
        available = sorted(episode_files)
        raise ValueError(
            f"episode {episode_index} is absent; available range is "
            f"{available[0]}..{available[-1]} ({len(available)} episodes)"
        )

    info = _read_json(dataset_root / "meta" / "info.json")
    completion = _validate_completed_dataset(dataset_root, info)
    feature_channels = {
        feature: _feature_gripper_indices(info, feature)
        for feature in VECTOR_COLUMNS
    }
    channel_order: list[str] = []
    channel_names: dict[str, dict[str, str]] = {}
    for feature in VECTOR_COLUMNS:
        for channel_id, dimension_name, _index in feature_channels[feature]:
            if channel_id not in channel_order:
                channel_order.append(channel_id)
            channel_names.setdefault(channel_id, {})[feature] = dimension_name

    parquet_path = episode_files[episode_index]
    table = pq.read_table(parquet_path, columns=list(VECTOR_COLUMNS))
    if table.num_rows <= 0:
        raise ValueError(f"episode {episode_index} is empty: {parquet_path}")
    if table.num_rows > max_frames:
        raise ValueError(
            f"episode {episode_index} has {table.num_rows} rows, exceeding the "
            f"read-only preview limit of {max_frames}"
        )
    extracted = {
        feature: _extract_feature_series(table, feature, feature_channels[feature])
        for feature in VECTOR_COLUMNS
    }

    colors = {
        "right": "#16a26a",
        "left": "#087af5",
        "gripper": "#7a5af8",
    }
    channels = []
    for position, channel_id in enumerate(channel_order):
        label = {
            "right": "Right gripper",
            "left": "Left gripper",
            "gripper": "Gripper",
        }.get(channel_id, channel_id.replace("_", " ").title())
        channels.append(
            {
                "id": channel_id,
                "label": label,
                "color": colors.get(channel_id, f"hsl({(position * 83 + 26) % 360} 68% 45%)"),
                "dimensions": channel_names[channel_id],
                "series": {
                    "observation_state": extracted["observation.state"].get(channel_id),
                    "action": extracted["action"].get(channel_id),
                },
            }
        )

    transform = completion.get("gripper_transform")
    source_mode = (
        "binary"
        if isinstance(transform, dict)
        and transform.get("kind") == "binarize_calibrated_gripper_width_at_threshold"
        else "continuous"
    )
    all_values = [
        value
        for channel in channels
        for feature_values in channel["series"].values()
        for value in feature_values or []
        if value is not None
    ]
    values_already_binary = bool(all_values) and all(
        value in (0.0, 1.0) for value in all_values
    )
    if values_already_binary:
        source_mode = "binary"

    fps = float(info.get("fps", 0) or 0)
    return {
        "schema": "instadatacap.gripper_binary_preview.v1",
        "read_only": True,
        "source_kind": "lerobot",
        "dataset_root": str(dataset_root),
        "dataset_name": dataset_root.name,
        "task": _task_name(dataset_root),
        "episode_index": episode_index,
        "episode_indices": sorted(episode_files),
        "episode_file": str(parquet_path.relative_to(dataset_root)),
        "episode_count": len(episode_files),
        "frame_count": table.num_rows,
        "fps": fps,
        "duration_sec": table.num_rows / fps if fps > 0 else None,
        "source_mode": source_mode,
        "conversion_complete_schema": completion.get("schema"),
        "source_gripper_transform": transform,
        "series_options": [
            {"id": "observation_state", "label": "observation.state"},
            {"id": "action", "label": "action"},
        ],
        "channels": channels,
        "comparison_rule": "width < threshold -> 0; width >= threshold -> 1",
    }


def extract_episode_preview(
    input_path: str | Path,
    episode_index: int | None = None,
    *,
    max_frames: int = 200_000,
) -> dict[str, Any]:
    """Read one Zarr/collection, legacy LeRobot, or completed `_03` episode.

    Source resolution happens before importing PyArrow or Zarr, so lightweight
    `_03` preview remains available in the DataCap environment without either
    dataset package.  Every supported path is opened read-only.
    """

    requested = Path(input_path).expanduser().resolve()
    preferred_artifact: Path | None = None
    if requested.is_file() and (
        requested.name == "gripper_width.csv"
        or _read_manifest_if_schema(
            requested, {BIMANUAL_GRIPPER_SCHEMA, THRESHOLD_PREVIEW_SCHEMA}
        )
        is not None
    ):
        preferred_artifact = resolve_gripper_artifact_manifest(requested)
    elif requested.is_dir():
        compact_manifests = [
            requested / name / "manifest.json"
            for name in ("gripper_threshold_sampling", "gripper_threshold_preview")
        ]
        compact_matches = [
            path.resolve()
            for path in compact_manifests
            if _read_manifest_if_schema(path, {THRESHOLD_PREVIEW_SCHEMA}) is not None
        ]
        if len(compact_matches) > 1:
            choices = "\n".join(f"  - {path}" for path in compact_matches)
            raise ValueError(
                "multiple lightweight gripper threshold artifacts were found; "
                f"enter the exact manifest path:\n{choices}"
            )
        if compact_matches:
            # A deliberately sampled threshold artifact is cheaper and more
            # relevant than any older full LeRobot export under the capture.
            preferred_artifact = compact_matches[0]
    if preferred_artifact is not None:
        return extract_gripper_artifact_preview(
            preferred_artifact,
            episode_index,
            max_frames=max_frames,
        )

    try:
        zarr_kind, zarr_root = resolve_zarr_source(input_path)
    except ValueError as zarr_error:
        zarr_kind = None
        zarr_root = None
    else:
        return _extract_zarr_preview(
            zarr_kind,
            zarr_root,
            episode_index,
            max_frames=max_frames,
        )

    try:
        resolve_dataset_root(input_path)
    except ValueError as lerobot_error:
        try:
            manifest_path = resolve_gripper_artifact_manifest(input_path)
        except ValueError as artifact_error:
            raise ValueError(
                f"input is neither an audited Zarr shard/collection, a completed "
                f"legacy LeRobot dataset, nor a completed gripper-estimation artifact:\n"
                f"Zarr: {zarr_error}\nLeRobot: {lerobot_error}\n"
                f"Gripper artifact: {artifact_error}"
            ) from artifact_error
        return extract_gripper_artifact_preview(
            manifest_path,
            episode_index,
            max_frames=max_frames,
        )
    return _extract_lerobot_episode_preview(
        input_path,
        episode_index,
        max_frames=max_frames,
    )


class GripperBinaryPreviewReader:
    """Run Zarr/legacy-Parquet extraction in data Python, never in DataCap."""

    def __init__(
        self,
        python_executable: str | Path,
        *,
        timeout_sec: float = 30.0,
        max_frames: int = 200_000,
    ) -> None:
        # Preserve virtual-environment launcher symlinks.  Resolving
        # `.venv/bin/python` to the base interpreter silently drops that
        # environment's zarr/pyarrow packages.
        self.python_executable = Path(
            os.path.abspath(os.path.expanduser(os.fspath(python_executable)))
        )
        self.timeout_sec = float(timeout_sec)
        self.max_frames = int(max_frames)
        self._lock = threading.Lock()

    def load(self, input_path: str, episode_index: int | None) -> dict[str, Any]:
        if not self.python_executable.is_file() or not os.access(
            self.python_executable, os.X_OK
        ):
            raise ValueError(
                f"data preview Python is unavailable: {self.python_executable}"
            )
        command = [
            str(self.python_executable),
            str(Path(__file__).resolve()),
            "--extract-json",
            str(input_path),
            "--max-frames",
            str(self.max_frames),
        ]
        if episode_index is not None:
            command.extend(("--episode", str(episode_index)))
        with self._lock:
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"episode preview exceeded {self.timeout_sec:g}s"
                ) from exc
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise ValueError(message or "episode preview extraction failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("episode preview returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("read_only") is not True:
            raise RuntimeError("episode preview did not return the read-only contract")
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Zarr, legacy LeRobot, or pre-dataset gripper preview"
    )
    parser.add_argument("--extract-json", metavar="DATASET_OR_ARTIFACT")
    parser.add_argument("--episode", type=int)
    parser.add_argument("--max-frames", type=int, default=200_000)
    args = parser.parse_args(argv)
    if not args.extract_json:
        parser.error("--extract-json is required")
    try:
        payload = extract_episode_preview(
            args.extract_json,
            args.episode,
            max_frames=args.max_frames,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
