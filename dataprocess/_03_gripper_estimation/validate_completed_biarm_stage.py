#!/usr/bin/env python3
"""Fast fail-closed validation for a completed bimanual _03 stage.

The normal estimator must probe every packet before it can construct a chunk
plan.  That is necessary when work may need to be resumed, but needlessly reads
tens of gigabytes when the final manifests, CSVs, and all image-cache chunks
are already complete.  This validator checks the frozen input identities and
every published artifact without decoding or probing either INSV stream.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "instaumi_gripper_estimation_v1"
FRONT_CACHE_SCHEMA = "instaumi.front_openpi_cache.v1"


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def content_identity(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "sha256": sha256_file(path)}


def sampled_identity(path: Path, sample_size: int = 256 * 1024) -> dict[str, Any]:
    path = path.expanduser().resolve()
    stat = path.stat()
    offsets = sorted(
        {
            0,
            max(0, stat.st_size // 2 - sample_size // 2),
            max(0, stat.st_size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            data = stream.read(min(sample_size, stat.st_size - offset))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return value


def same_calibration_points(embedded: object, current: object) -> bool:
    if not isinstance(embedded, list) or not isinstance(current, list):
        return False
    if len(embedded) != len(current):
        return False
    for old, new in zip(embedded, current, strict=True):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if old.get("source") != new.get("source"):
            return False
        for key in ("px", "m"):
            try:
                if not math.isclose(
                    float(old[key]), float(new[key]), rel_tol=0.0, abs_tol=1e-12
                ):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
    return True


def require_under(path: Path, root: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside {root}: {path}") from exc
    return path


def canonical_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} is not a non-empty path")
    return Path(value).expanduser().resolve()


def validate_biarm_calibration_routing(
    manifest: dict[str, Any], calibrations: dict[str, Path | None]
) -> None:
    """Cross-check new provenance fields while accepting legacy manifests.

    Older bimanual manifests predate arm-specific calibration paths.  They are
    still validated from each side's embedded calibration points below.  Once
    any new routing field is present, however, every present copy must agree
    with the calibration selected for this validation run.
    """

    routed = manifest.get("calibration_json_by_side")
    if routed is not None:
        if not isinstance(routed, dict):
            raise RuntimeError("bimanual calibration_json_by_side is not an object")
        for side in ("left", "right"):
            expected = calibrations[side]
            assert expected is not None
            actual = canonical_path(
                routed.get(side), label=f"bimanual calibration_json_by_side.{side}"
            )
            if actual != expected:
                raise RuntimeError(
                    f"bimanual {side} calibration routing changed: "
                    f"manifest={actual}, current={expected}"
                )

    if routed is not None and "calibration_json" in manifest:
        legacy_value = canonical_path(
            manifest.get("calibration_json"), label="bimanual calibration_json"
        )
        left = calibrations["left"]
        right = calibrations["right"]
        assert left is not None and right is not None
        if left == right and legacy_value != left:
            raise RuntimeError(
                "bimanual shared calibration routing changed: "
                f"manifest={legacy_value}, current={left}"
            )

    for side in ("left", "right"):
        side_record = manifest.get(side)
        if not isinstance(side_record, dict) or "calibration_json" not in side_record:
            continue
        expected = calibrations[side]
        assert expected is not None
        actual = canonical_path(
            side_record.get("calibration_json"),
            label=f"bimanual {side}.calibration_json",
        )
        if actual != expected:
            raise RuntimeError(
                f"bimanual nested {side} calibration routing changed: "
                f"manifest={actual}, current={expected}"
            )


def validate_csv(path: Path, expected_frames: int, expected_sha256: str) -> None:
    if sha256_file(path) != expected_sha256:
        raise RuntimeError(f"gripper CSV hash mismatch: {path}")
    count = 0
    with path.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or "frame_idx" not in reader.fieldnames:
            raise RuntimeError(f"gripper CSV lacks frame_idx: {path}")
        for count, row in enumerate(reader, start=1):
            if int(row["frame_idx"]) != count - 1:
                raise RuntimeError(f"non-contiguous frame_idx at row {count}: {path}")
    if count != expected_frames:
        raise RuntimeError(f"gripper CSV rows {count} != {expected_frames}: {path}")


def validate_segment_quality_report(quality: dict[str, Any], expected_contract: dict[str, Any]) -> None:
    """Accept a completed audit while keeping every rejected segment explicit."""

    if (
        quality.get("schema") != "instaumi.continuous_gripper_quality.v1"
        or quality.get("contract") != expected_contract
    ):
        raise RuntimeError("continuous gripper quality schema/contract mismatch")
    segments = quality.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("continuous gripper quality has no segment evidence")
    seen: set[int] = set()
    accepted: list[int] = []
    rejected: list[int] = []
    flattened: list[str] = []
    legacy_all_accepted = quality.get("status") == "accepted" and not list(
        quality.get("violations") or []
    )
    for item in segments:
        if not isinstance(item, dict):
            raise RuntimeError("continuous gripper quality segment is not an object")
        segment_index = int(item.get("segment_index", -1))
        if segment_index < 0 or segment_index in seen:
            raise RuntimeError("continuous gripper quality segment index is invalid/duplicate")
        seen.add(segment_index)
        status = item.get("status")
        violations = item.get("violations")
        if status is None and violations is None and legacy_all_accepted:
            status = "accepted"
            violations = []
        if not isinstance(violations, list) or not all(
            isinstance(value, str) and value for value in violations
        ):
            raise RuntimeError(f"segment={segment_index} has invalid quality violations")
        if status == "accepted" and not violations:
            accepted.append(segment_index)
        elif status == "rejected" and violations:
            rejected.append(segment_index)
            flattened.extend(violations)
        else:
            raise RuntimeError(f"segment={segment_index} has inconsistent quality status")

    expected_status = (
        "accepted"
        if not rejected
        else "partially_accepted"
        if accepted
        else "rejected"
    )
    summary = quality.get("summary") or {}
    if (
        quality.get("status") != expected_status
        or list(quality.get("violations") or []) != flattened
        or int(summary.get("segment_count", -1)) != len(segments)
    ):
        raise RuntimeError("continuous gripper quality aggregate is inconsistent")
    if not legacy_all_accepted and (
        int(summary.get("accepted_segment_count", -1)) != len(accepted)
        or int(summary.get("rejected_segment_count", -1)) != len(rejected)
        or list(summary.get("accepted_segment_indices") or []) != accepted
        or list(summary.get("rejected_segment_indices") or []) != rejected
    ):
        raise RuntimeError("continuous gripper quality segment summary is inconsistent")


def validate_side(
    *,
    side: str,
    tmp_root: Path,
    cache_root: Path,
    calibration_json: Path,
    expect_front_openpi_cache: bool,
    identity_cache: dict[Path, dict[str, Any]],
) -> tuple[int, int]:
    manifest_path = tmp_root / side / "03_gripper_estimation" / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise RuntimeError(f"unsupported {side} manifest schema: {manifest.get('schema')}")

    if os.environ.get("CONTINUOUS_GRIPPER_QUALITY_AUDIT", "0") == "1":
        quality = ((manifest.get("fill_report") or {}).get("continuous_gripper_quality") or {})
        expected_contract = {
            "enabled": True,
            "min_real_detections_per_segment": int(
                os.environ.get("GRIPPER_QUALITY_MIN_DETECTIONS_PER_SEGMENT", "1")
            ),
            "max_internal_gap_ms": float(
                os.environ.get("GRIPPER_QUALITY_MAX_INTERNAL_GAP_MS", "500")
            ),
            "max_edge_extrapolation_ms": float(
                os.environ.get("GRIPPER_QUALITY_MAX_EDGE_EXTRAPOLATION_MS", "250")
            ),
            "min_width_m": float(os.environ.get("GRIPPER_QUALITY_MIN_WIDTH_M", "0")),
            "max_width_m": float(os.environ.get("GRIPPER_QUALITY_MAX_WIDTH_M", "0.11")),
            "fill_policy": "segment_local_first_valid_backfill_then_forward_fill",
            "gap_and_edge_thresholds_are_diagnostic_only": True,
        }
        try:
            validate_segment_quality_report(quality, expected_contract)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"{side} completed _03 lacks a valid segment-level quality audit: {exc}"
            ) from exc
        report_identity = (manifest.get("fill_report") or {}).get(
            "continuous_gripper_quality_report"
        ) or {}
        report_path = Path(str(report_identity.get("path", ""))).expanduser().resolve()
        if (
            not report_path.is_file()
            or content_identity(report_path) != report_identity
            or load_json(report_path) != quality
        ):
            raise RuntimeError(f"{side} continuous gripper quality report changed")

    calibration = manifest.get("calibration") or {}
    if calibration.get("mode") != "calibration_json":
        raise RuntimeError(f"{side} did not use calibration_json")
    if calibration.get("gripper_zero_below_m") is not None:
        raise RuntimeError(f"{side} _03 output is not continuous gripper width")
    embedded_calibration = (calibration.get("json") or {}).get("points")
    current_calibration = json.loads(calibration_json.read_text(encoding="utf-8"))
    if not same_calibration_points(embedded_calibration, current_calibration):
        raise RuntimeError(f"{side} gripper calibration changed")

    raw_identity = manifest.get("raw_manifest_identity")
    raw_path = Path(str((raw_identity or {}).get("path", "")))
    if content_identity(raw_path) != raw_identity:
        raise RuntimeError(f"{side} raw manifest changed: {raw_path}")

    active = manifest.get("active_frame_selection") or {}
    for key in ("calibrated_timestamps_identity", "timestamp_log_identity"):
        expected = active.get(key)
        path = Path(str((expected or {}).get("path", ""))).expanduser().resolve()
        if path not in identity_cache:
            identity_cache[path] = content_identity(path)
        if identity_cache[path] != expected:
            raise RuntimeError(f"{side} {key} changed: {path}")

    decode_sources = ((manifest.get("source_layout") or {}).get("decode_sources") or [])
    if not decode_sources:
        raise RuntimeError(f"{side} manifest has no decode sources")
    for source in decode_sources:
        expected = source.get("identity")
        source_path = Path(str((expected or {}).get("path", "")))
        if sampled_identity(source_path) != expected:
            raise RuntimeError(f"{side} source INSV changed: {source_path}")

    expected_frames = int(manifest.get("processed_frames", -1))
    if expected_frames <= 0:
        raise RuntimeError(f"invalid {side} processed_frames={expected_frames}")
    csv_path = require_under(Path(str(manifest.get("output_csv", ""))), tmp_root, "gripper CSV")
    validate_csv(csv_path, expected_frames, str(manifest.get("output_csv_sha256", "")))

    raw_cache = manifest.get("front_openpi_cache")
    if not expect_front_openpi_cache:
        if raw_cache not in (None, {}):
            raise RuntimeError(
                f"{side} unexpectedly contains a front OpenPI cache while the "
                "current pipeline contract disables it"
            )
        return expected_frames, 0

    cache = raw_cache or {}
    if cache.get("schema") != FRONT_CACHE_SCHEMA:
        raise RuntimeError(f"{side} front OpenPI cache is missing")
    records = cache.get("chunks")
    if not isinstance(records, list) or not records:
        raise RuntimeError(f"{side} front OpenPI cache has no chunks")
    frame_sum = 0
    byte_sum = 0
    seen_indices: list[int] = []
    for record in records:
        index = int(record["chunk_index"])
        frame_count = int(record["frame_count"])
        size_bytes = int(record["size_bytes"])
        array_path = require_under(Path(record["array_path"]), cache_root, "front cache array")
        chunk_manifest = require_under(
            Path(record["chunk_manifest"]), cache_root, "front cache chunk manifest"
        )
        if not chunk_manifest.is_file():
            raise RuntimeError(f"missing front cache manifest: {chunk_manifest}")
        if not array_path.is_file() or array_path.stat().st_size != size_bytes:
            raise RuntimeError(f"missing/truncated front cache array: {array_path}")
        if frame_count <= 0 or size_bytes <= 0:
            raise RuntimeError(f"invalid front cache record: {array_path}")
        seen_indices.append(index)
        frame_sum += frame_count
        byte_sum += size_bytes
    if seen_indices != list(range(len(records))):
        raise RuntimeError(f"{side} front cache chunk indices are not contiguous")
    if frame_sum != int(cache.get("frame_count", -1)):
        raise RuntimeError(f"{side} front cache frame total mismatch")
    if byte_sum != int(cache.get("size_bytes", -1)):
        raise RuntimeError(f"{side} front cache byte total mismatch")
    return expected_frames, len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp-root", type=Path, required=True)
    parser.add_argument("--fast-cache-root", type=Path, required=True)
    parser.add_argument(
        "--gripper-calibration-json",
        type=Path,
        default=None,
        help="legacy shared calibration fallback for both arms",
    )
    parser.add_argument("--left-gripper-calibration-json", type=Path, default=None)
    parser.add_argument("--right-gripper-calibration-json", type=Path, default=None)
    parser.add_argument(
        "--expect-front-openpi-cache",
        choices=("0", "1"),
        default="1",
        help="Validate the current _03 cache contract, including intentional disablement.",
    )
    args = parser.parse_args()

    tmp_root = args.tmp_root.expanduser().resolve()
    cache_root = args.fast_cache_root.expanduser().resolve()
    shared_calibration = (
        None
        if args.gripper_calibration_json is None
        else args.gripper_calibration_json.expanduser().resolve()
    )
    calibrations = {
        "left": (
            args.left_gripper_calibration_json.expanduser().resolve()
            if args.left_gripper_calibration_json is not None
            else shared_calibration
        ),
        "right": (
            args.right_gripper_calibration_json.expanduser().resolve()
            if args.right_gripper_calibration_json is not None
            else shared_calibration
        ),
    }
    missing = [side for side, path in calibrations.items() if path is None]
    if missing:
        parser.error(
            "missing calibration for "
            + ",".join(missing)
            + "; provide the side-specific option or --gripper-calibration-json fallback"
        )
    for side, path in calibrations.items():
        assert path is not None
        if not path.is_file():
            parser.error(f"{side} gripper calibration JSON does not exist: {path}")
    biarm_manifest = tmp_root / "03_gripper_estimation_biarm" / "manifest.json"
    if not biarm_manifest.is_file():
        raise RuntimeError(f"missing bimanual _03 manifest: {biarm_manifest}")
    validate_biarm_calibration_routing(load_json(biarm_manifest), calibrations)
    identity_cache: dict[Path, dict[str, Any]] = {}
    results = {
        side: validate_side(
            side=side,
            tmp_root=tmp_root,
            cache_root=cache_root,
            calibration_json=calibrations[side],
            expect_front_openpi_cache=args.expect_front_openpi_cache == "1",
            identity_cache=identity_cache,
        )
        for side in ("left", "right")
    }
    print(
        "[stage-cache] validated completed continuous _03 without INSV packet scan: "
        + " ".join(
            f"{side}_frames={frames} {side}_chunks={chunks}"
            for side, (frames, chunks) in results.items()
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
