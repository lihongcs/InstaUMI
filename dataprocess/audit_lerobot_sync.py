#!/usr/bin/env python3
"""Fail-closed timing and schema audit for an InstaUMI LeRobot dataset.

This reads the source-timing sidecars stored in Parquet.  It deliberately does
not infer synchronization from equal video lengths or nominal dataset FPS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from numbers import Integral, Real
import os
from pathlib import Path
from typing import Any
import uuid

import numpy as np
import pyarrow.parquet as pq


TIMING_COLUMNS = (
    "source.camera_unix_time_ms",
    "source.state_unix_time_ms",
    "source.htc_nearest_gap_ms",
    "source.htc_bracket_gap_ms",
    "source.frame_reused",
    "source.htc_valid",
)
CALIBRATION_MAX_RESIDUAL_P95_MS = 20.0
CALIBRATION_MAX_SHORT_TERMINAL_FALLBACK_SEC = 10.0
CAMERA_TO_STATE_MAX_ABS_MS = 20.0


def _stat_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_unchanged(path: Path, expected: tuple[int, int, int, int, int]) -> None:
    try:
        actual = _stat_signature(path)
    except OSError as exc:
        raise RuntimeError(f"input disappeared while auditing: {path}") from exc
    if actual != expected:
        raise RuntimeError(
            f"input changed while scanning/hashing: {path}; "
            f"before={expected} after={actual}"
        )


def _pair_rows(values: Any, *, name: str) -> list[list[Any]]:
    rows = values.to_pylist()
    if not isinstance(rows, list) or any(
        not isinstance(row, (list, tuple)) or len(row) != 2 for row in rows
    ):
        shape = np.asarray(rows, dtype=object).shape
        raise RuntimeError(f"{name} must contain two values per frame; got {shape}")
    return [list(row) for row in rows]


def _numeric_pairs(
    values: Any, *, name: str, nonnegative: bool = False
) -> np.ndarray:
    rows = _pair_rows(values, name=name)
    result = np.empty((len(rows), 2), dtype=np.float64)
    for row_index, row in enumerate(rows):
        for arm_index, value in enumerate(row):
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                raise RuntimeError(
                    f"{name} must contain finite numeric values; "
                    f"got {value!r} at row={row_index} arm={arm_index}"
                )
            number = float(value)
            if not np.isfinite(number):
                raise RuntimeError(
                    f"{name} must contain finite numeric values; "
                    f"got {value!r} at row={row_index} arm={arm_index}"
                )
            if nonnegative and number < 0:
                raise RuntimeError(
                    f"{name} must contain non-negative values; "
                    f"got {number!r} at row={row_index} arm={arm_index}"
                )
            result[row_index, arm_index] = number
    return result


def _boolean_pairs(values: Any, *, name: str) -> np.ndarray:
    rows = _pair_rows(values, name=name)
    result = np.empty((len(rows), 2), dtype=np.bool_)
    for row_index, row in enumerate(rows):
        for arm_index, value in enumerate(row):
            if isinstance(value, (bool, np.bool_)):
                normalized = bool(value)
            elif isinstance(value, Integral) and int(value) in (0, 1):
                # The converter stores these sidecars as uint8 0/1 so they
                # remain stable across Arrow/Parquet implementations.  Treat
                # only the two exact integer values as booleans; floats and
                # every other integer remain fail-closed.
                normalized = bool(int(value))
            else:
                raise RuntimeError(
                    f"{name} must contain non-null boolean or integer 0/1 values; "
                    f"got {value!r} at row={row_index} arm={arm_index}"
                )
            result[row_index, arm_index] = normalized
    return result


def _distribution(values: np.ndarray) -> dict[str, float]:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        return {key: float("nan") for key in ("p50", "p95", "p99", "max")}
    return {
        "p50": float(np.percentile(finite, 50)),
        "p95": float(np.percentile(finite, 95)),
        "p99": float(np.percentile(finite, 99)),
        "max": float(np.max(finite)),
    }


def _validate_contract(
    root: Path, *, allow_image_free_numeric: bool = False
) -> dict[str, Any]:
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    features = info.get("features") or {}
    expected_names = [
        *(f"right_{name}" for name in ("x", "y", "z", "rx", "ry", "rz", "gripper", "none1", "none2", "none3")),
        *(f"left_{name}" for name in ("x", "y", "z", "rx", "ry", "rz", "gripper", "none1", "none2", "none3")),
    ]
    for key in ("action", "observation.state"):
        feature = features.get(key) or {}
        if feature.get("shape") != [20] or feature.get("names") != expected_names:
            raise RuntimeError(f"{key} is not the required right-first/left-second 20D contract")
    image_keys = (
        "observation.images.right_wrist",
        "observation.images.right_left_view",
        "observation.images.right_right_view",
        "observation.images.left_wrist",
        "observation.images.left_left_view",
        "observation.images.left_right_view",
    )
    actual_image_keys = {
        key for key in features if key.startswith("observation.images.")
    }
    panorama_numeric_only = False
    if not actual_image_keys:
        plan_path = root / "conversion_plan.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_arguments = plan.get("arguments") or {}
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "image-free numeric dataset requires a valid conversion_plan.json "
                "that declares panorama_numeric_only=true"
            ) from exc
        panorama_numeric_only = plan_arguments.get("panorama_numeric_only") is True
        if not panorama_numeric_only:
            raise RuntimeError(
                "image-free numeric dataset is not declared by "
                "conversion_plan.arguments.panorama_numeric_only=true"
            )
    if actual_image_keys != set(image_keys) and not (
        panorama_numeric_only and not actual_image_keys
    ):
        raise RuntimeError(
            "dataset must contain exactly the six ordinary image streams; "
            f"got {sorted(actual_image_keys)}"
        )
    for key in image_keys if actual_image_keys else ():
        feature = features.get(key) or {}
        if feature.get("dtype") != "image" or feature.get("shape") != [224, 224, 3]:
            raise RuntimeError(f"{key} is not lossless 224x224 RGB image storage: {feature}")
    return {
        "fps": info.get("fps"),
        "total_frames_declared": info.get("total_frames"),
        "action_order": "right_left",
        "wrist_shape": [224, 224, 3],
        "image_storage": (
            "panorama_numeric_only"
            if panorama_numeric_only
            else "six_ordinary_image_streams"
        ),
        "image_free_compatibility_assertion_requested": bool(
            allow_image_free_numeric
        ),
    }


def _finite_calibration_value(
    raw: Any, *, name: str, positive: bool = False
) -> float:
    qualifier = "finite positive" if positive else "finite non-negative"
    if isinstance(raw, (bool, np.bool_)) or not isinstance(raw, Real):
        raise RuntimeError(f"{name} must be {qualifier}; got {raw!r}")
    value = float(raw)
    if not np.isfinite(value) or (positive and value <= 0) or (not positive and value < 0):
        raise RuntimeError(f"{name} must be {qualifier}; got {raw!r}")
    return value


def _finite_signed_calibration_value(raw: Any, *, name: str) -> float:
    if isinstance(raw, (bool, np.bool_)) or not isinstance(raw, Real):
        raise RuntimeError(f"{name} must be finite numeric; got {raw!r}")
    value = float(raw)
    if not np.isfinite(value):
        raise RuntimeError(f"{name} must be finite numeric; got {raw!r}")
    return value


def _calibration_source_reports(
    sync_report: dict[str, Any], *, source_label: str = "dataset"
) -> list[tuple[str, dict[str, Any] | None]]:
    """Flatten direct reports retained inside one or more merge layers."""
    if not isinstance(sync_report, dict) or not sync_report:
        return []
    sources = sync_report.get("sources")
    if not isinstance(sources, list):
        return [(source_label, sync_report)]
    reports: list[tuple[str, dict[str, Any] | None]] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            reports.append((f"{source_label}.sources[{index}]", None))
            continue
        label = str(source.get("root") or f"{source_label}.sources[{index}]")
        embedded = source.get("embedded_sync_report")
        if not isinstance(embedded, dict):
            reports.append((label, None))
            continue
        nested = _calibration_source_reports(embedded, source_label=label)
        reports.extend(nested if nested else [(label, embedded)])
    return reports


def _validate_calibration_source(
    source_label: str, sync_report: dict[str, Any]
) -> dict[str, Any]:
    calibration = sync_report.get("calibration")
    if not isinstance(calibration, dict):
        raise RuntimeError(f"calibration evidence is missing for source {source_label}")
    calibration_status = calibration.get("status")
    if calibration_status != "accepted":
        raise RuntimeError(
            f"calibration.status must be accepted for source {source_label}; "
            f"got {calibration_status!r}"
        )
    embedded = calibration.get("embedded_report")
    if not isinstance(embedded, dict) or not embedded:
        raise RuntimeError(f"calibration.embedded_report is missing for source {source_label}")
    embedded_status = embedded.get("status")
    if embedded_status != "accepted":
        raise RuntimeError(
            f"calibration.embedded_report.status must be accepted for source {source_label}; "
            f"got {embedded_status!r}"
        )

    summary: dict[str, Any] = {
        "source": source_label,
        "calibration_status": calibration_status,
        "embedded_report_status": embedded_status,
        "detailed_evidence_available": False,
        "configured_max_residual_p95_ms": None,
        "observed_max_residual_p95_ms": None,
        "inputs_unchanged_during_calibration": None,
        "arms": {},
    }
    has_configuration = "configuration" in embedded
    has_arms = "arms" in embedded
    if not has_configuration or not has_arms:
        raise RuntimeError(
            f"detailed calibration evidence must contain both configuration and arms: "
            f"source={source_label}"
        )
    configuration = embedded.get("configuration")
    arms = embedded.get("arms")
    if not isinstance(configuration, dict) or not isinstance(arms, dict):
        raise RuntimeError(
            f"detailed calibration configuration/arms must be objects: source={source_label}"
        )
    threshold = _finite_calibration_value(
        configuration.get("max_residual_p95_ms"),
        name=f"{source_label}.configuration.max_residual_p95_ms",
        positive=True,
    )
    # The embedded calibration may deliberately use a tighter acceptance
    # threshold than the dataset-wide precision ceiling.  Equality would
    # reject stronger evidence (for example, the production 5 ms calibration
    # contract under this 20 ms audit ceiling).  Fail closed only when the
    # calibration itself allowed a looser residual than the audit permits.
    if threshold > CALIBRATION_MAX_RESIDUAL_P95_MS:
        raise RuntimeError(
            "calibration max_residual_p95_ms exceeds the 20 ms precision "
            f"contract for source {source_label}: "
            f"{threshold} > {CALIBRATION_MAX_RESIDUAL_P95_MS}"
        )
    max_short_fallback_sec = _finite_calibration_value(
        configuration.get("max_short_terminal_fallback_sec"),
        name=(
            f"{source_label}.configuration."
            "max_short_terminal_fallback_sec"
        ),
        positive=True,
    )
    if max_short_fallback_sec > CALIBRATION_MAX_SHORT_TERMINAL_FALLBACK_SEC:
        raise RuntimeError(
            "calibration max_short_terminal_fallback_sec exceeds the audit "
            f"contract for source {source_label}: {max_short_fallback_sec} > "
            f"{CALIBRATION_MAX_SHORT_TERMINAL_FALLBACK_SEC}"
        )
    inputs_unchanged = embedded.get("inputs_unchanged_during_calibration")
    if inputs_unchanged is not True:
        raise RuntimeError(
            "inputs_unchanged_during_calibration must be true for source "
            f"{source_label}; got {inputs_unchanged!r}"
        )

    observed_residuals: list[float] = []
    arm_summaries: dict[str, Any] = {}
    for arm_name in ("left", "right"):
        arm = arms.get(arm_name)
        if not isinstance(arm, dict):
            raise RuntimeError(f"missing {arm_name} calibration arm for source {source_label}")
        if arm.get("status") != "accepted":
            raise RuntimeError(
                f"{arm_name} arm status must be accepted for source {source_label}; "
                f"got {arm.get('status')!r}"
            )
        quality = arm.get("quality")
        if not isinstance(quality, dict) or quality.get("calibration_accepted") is not True:
            raise RuntimeError(
                f"{arm_name} quality.calibration_accepted must be true for source "
                f"{source_label}"
            )
        clock_model = arm.get("clock_model")
        if not isinstance(clock_model, dict):
            raise RuntimeError(
                f"{arm_name} clock_model is missing for source {source_label}"
            )
        arm_residual = _finite_calibration_value(
            clock_model.get("residual_p95_ms"),
            name=f"{source_label}.{arm_name}.clock_model.residual_p95_ms",
        )
        if arm_residual > threshold:
            raise RuntimeError(
                f"{arm_name} clock_model residual_p95_ms exceeds configured limit for "
                f"source {source_label}: {arm_residual} > {threshold}"
            )
        segments = arm.get("segments")
        if not isinstance(segments, list):
            raise RuntimeError(
                f"{arm_name} segments must be a list for source {source_label}"
            )
        segment_by_source: dict[int, dict[str, Any]] = {}
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise RuntimeError(
                    f"{arm_name} segment {segment_index} must be an object for "
                    f"source {source_label}"
                )
            source_segment = segment.get("source_segment")
            if (
                isinstance(source_segment, bool)
                or not isinstance(source_segment, Integral)
                or int(source_segment) < 0
            ):
                raise RuntimeError(
                    f"{arm_name} segment {segment_index} has invalid source_segment "
                    f"for source {source_label}: {source_segment!r}"
                )
            source_segment = int(source_segment)
            if source_segment in segment_by_source:
                raise RuntimeError(
                    f"{arm_name} has duplicate source_segment={source_segment} for "
                    f"source {source_label}"
                )
            segment_by_source[source_segment] = segment
        terminal_segment_ids = (
            {min(segment_by_source), max(segment_by_source)}
            if segment_by_source
            else set()
        )
        segment_residuals: list[float] = []
        short_fallback_count = 0
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict) or not isinstance(segment.get("clock_model"), dict):
                raise RuntimeError(
                    f"{arm_name} segment {segment_index} lacks clock_model for source "
                    f"{source_label}"
                )
            segment_model = segment["clock_model"]
            source_segment = int(segment["source_segment"])
            residual_raw = segment_model.get("residual_p95_ms")
            if residual_raw is None:
                fallback = segment_model.get("fallback")
                prefix = "nearest_calibrated_segment_"
                suffix = "_no_candidate_window"
                donor_text = (
                    fallback[len(prefix) : -len(suffix)]
                    if isinstance(fallback, str)
                    and fallback.startswith(prefix)
                    and fallback.endswith(suffix)
                    else ""
                )
                accepted_windows = segment_model.get("accepted_windows")
                duration_sec = _finite_calibration_value(
                    segment.get("duration_sec"),
                    name=(
                        f"{source_label}.{arm_name}.segments[{segment_index}]"
                        ".duration_sec"
                    ),
                    positive=True,
                )
                if (
                    not donor_text.isdigit()
                    or source_segment not in terminal_segment_ids
                    or isinstance(accepted_windows, bool)
                    or not isinstance(accepted_windows, Integral)
                    or int(accepted_windows) != 0
                    or duration_sec > max_short_fallback_sec
                    or segment_model.get("residual_median_ms") is not None
                ):
                    raise RuntimeError(
                        f"{arm_name} segment {segment_index} has unaudited null "
                        f"residual fallback for source {source_label}: "
                        f"fallback={fallback!r} accepted_windows={accepted_windows!r} "
                        f"duration_sec={duration_sec}"
                    )
                donor_id = int(donor_text)
                donor = segment_by_source.get(donor_id)
                donor_model = donor.get("clock_model") if donor else None
                if donor_id == source_segment or not isinstance(donor_model, dict):
                    raise RuntimeError(
                        f"{arm_name} segment {segment_index} names an invalid donor "
                        f"segment {donor_id} for source {source_label}"
                    )
                donor_windows = donor_model.get("accepted_windows")
                donor_residual = _finite_calibration_value(
                    donor_model.get("residual_p95_ms"),
                    name=(
                        f"{source_label}.{arm_name}.segments[donor={donor_id}]"
                        ".clock_model.residual_p95_ms"
                    ),
                )
                if (
                    isinstance(donor_windows, bool)
                    or not isinstance(donor_windows, Integral)
                    or int(donor_windows) < 2
                    or donor_model.get("fallback") not in (None, "")
                    or donor_residual > threshold
                ):
                    raise RuntimeError(
                        f"{arm_name} segment {segment_index} donor {donor_id} is not "
                        f"directly calibrated within {threshold} ms for source "
                        f"{source_label}"
                    )
                for model_key in ("intercept_sec", "slope_sec_per_sec"):
                    inherited = _finite_signed_calibration_value(
                        segment_model.get(model_key),
                        name=(
                            f"{source_label}.{arm_name}.segments[{segment_index}]"
                            f".clock_model.{model_key}"
                        ),
                    )
                    donor_value = _finite_signed_calibration_value(
                        donor_model.get(model_key),
                        name=(
                            f"{source_label}.{arm_name}.segments[donor={donor_id}]"
                            f".clock_model.{model_key}"
                        ),
                    )
                    if inherited != donor_value:
                        raise RuntimeError(
                            f"{arm_name} segment {segment_index} did not exactly "
                            f"inherit donor {donor_id} {model_key} for source "
                            f"{source_label}: {inherited} != {donor_value}"
                        )
                short_fallback_count += 1
                continue
            residual = _finite_calibration_value(
                residual_raw,
                name=(
                    f"{source_label}.{arm_name}.segments[{segment_index}]"
                    ".clock_model.residual_p95_ms"
                ),
            )
            if residual > threshold:
                raise RuntimeError(
                    f"{arm_name} segment {segment_index} residual_p95_ms exceeds "
                    f"configured limit for source {source_label}: {residual} > {threshold}"
                )
            segment_residuals.append(residual)
        declared_fallback_count = clock_model.get(
            "short_terminal_segment_fallback_count"
        )
        if (
            isinstance(declared_fallback_count, bool)
            or not isinstance(declared_fallback_count, Integral)
            or int(declared_fallback_count) != short_fallback_count
        ):
            raise RuntimeError(
                f"{arm_name} short-terminal fallback count mismatch for source "
                f"{source_label}: declared={declared_fallback_count!r} "
                f"audited={short_fallback_count}"
            )
        observed_residuals.append(arm_residual)
        observed_residuals.extend(segment_residuals)
        arm_summaries[arm_name] = {
            "status": "accepted",
            "calibration_accepted": True,
            "clock_model_residual_p95_ms": arm_residual,
            "segment_count": len(segment_residuals),
            "short_terminal_fallback_count": short_fallback_count,
            "max_segment_residual_p95_ms": (
                max(segment_residuals) if segment_residuals else None
            ),
        }

    summary.update(
        {
            "detailed_evidence_available": True,
            "configured_max_residual_p95_ms": threshold,
            "observed_max_residual_p95_ms": max(observed_residuals),
            "inputs_unchanged_during_calibration": True,
            "arms": arm_summaries,
        }
    )
    return summary


def _embedded_calibration_evidence(
    sync_report: dict[str, Any], *, require_accepted: bool
) -> tuple[int, int, list[dict[str, Any]]]:
    sources = _calibration_source_reports(sync_report)
    embedded_count = sum(
        1
        for _, report in sources
        if isinstance(report, dict)
        and isinstance(report.get("calibration"), dict)
        and isinstance(report["calibration"].get("embedded_report"), dict)
        and bool(report["calibration"]["embedded_report"])
    )
    if not require_accepted:
        summaries = []
        for source_label, report in sources:
            calibration = report.get("calibration") if isinstance(report, dict) else None
            embedded = calibration.get("embedded_report") if isinstance(calibration, dict) else None
            summaries.append(
                {
                    "source": source_label,
                    "calibration_status": (
                        calibration.get("status") if isinstance(calibration, dict) else None
                    ),
                    "embedded_report_status": (
                        embedded.get("status") if isinstance(embedded, dict) else None
                    ),
                }
            )
        return embedded_count, len(sources), summaries
    if not sources or embedded_count != len(sources):
        raise RuntimeError(
            "sync_report.json does not retain calibration evidence for every source "
            f"({embedded_count}/{len(sources)})"
        )
    return (
        embedded_count,
        len(sources),
        [_validate_calibration_source(label, report) for label, report in sources if report],
    )


def _source_sync_reports(sync_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return direct/merged source reports which carry converter thresholds."""
    reports: list[dict[str, Any]] = []
    if isinstance(sync_report.get("thresholds"), dict):
        reports.append(sync_report)
    sources = sync_report.get("sources")
    if isinstance(sources, list):
        for source in sources:
            embedded = source.get("embedded_sync_report") if isinstance(source, dict) else None
            if isinstance(embedded, dict):
                reports.extend(_source_sync_reports(embedded))
    return reports


def _report_threshold(
    sync_report: dict[str, Any], key: str, fallback: float
) -> tuple[float, str]:
    values: list[float] = []
    for report in _source_sync_reports(sync_report):
        raw = (report.get("thresholds") or {}).get(key)
        if raw is None:
            continue
        value = float(raw)
        if not np.isfinite(value) or value <= 0:
            raise RuntimeError(f"invalid {key} in embedded sync report: {raw!r}")
        values.append(value)
    if not values:
        return fallback, "fallback"
    unique = sorted(set(values))
    if len(unique) != 1:
        raise RuntimeError(f"inconsistent {key} across merged sources: {unique}")
    return unique[0], "sync_report"


def audit(
    root: Path,
    hard_camera_pair_ms: float,
    hard_htc_nearest_ms: float | None,
    hard_htc_bracket_ms: float | None,
    soft_htc_nearest_ms: float | None,
    soft_htc_bracket_ms: float | None,
    require_embedded_calibration: bool,
    allow_invalid_htc: bool,
    expected_fps: int | None = None,
    allow_image_free_numeric: bool = False,
    hard_camera_to_state_ms: float = CAMERA_TO_STATE_MAX_ABS_MS,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    contract = _validate_contract(
        root, allow_image_free_numeric=allow_image_free_numeric
    )
    files = sorted((root / "data").glob("chunk-*/episode_*.parquet"))
    if not files:
        raise RuntimeError(f"No Parquet files found under {root / 'data'}")
    sync_report_path = root / "sync_report.json"
    if not sync_report_path.is_file():
        raise RuntimeError(f"sync_report.json is missing: {sync_report_path}")
    parquet_stats = {path: _stat_signature(path) for path in files}
    sync_report_stat = _stat_signature(sync_report_path)
    try:
        sync_report_bytes = sync_report_path.read_bytes()
        sync_report = json.loads(sync_report_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read valid sync_report.json: {sync_report_path}") from exc
    if not isinstance(sync_report, dict):
        raise RuntimeError(f"sync_report.json must contain an object: {sync_report_path}")
    sync_report_sha256 = hashlib.sha256(sync_report_bytes).hexdigest()

    def verify_input_snapshot() -> None:
        current_files = sorted((root / "data").glob("chunk-*/episode_*.parquet"))
        if current_files != files:
            raise RuntimeError(
                "episode Parquet file set changed while auditing: "
                f"before={files} after={current_files}"
            )
        for input_path, expected_stat in parquet_stats.items():
            _require_unchanged(input_path, expected_stat)
        _require_unchanged(sync_report_path, sync_report_stat)

    camera_delta: list[np.ndarray] = []
    state_delta: list[np.ndarray] = []
    nearest: list[np.ndarray] = []
    bracket: list[np.ndarray] = []
    reused_count = np.zeros(2, dtype=np.int64)
    invalid_count = np.zeros(2, dtype=np.int64)
    parquet_manifest: list[dict[str, Any]] = []
    total = 0
    for path in files:
        parquet = pq.ParquetFile(path)
        rows = int(parquet.metadata.num_rows)
        missing = sorted(set(TIMING_COLUMNS) - set(parquet.schema_arrow.names))
        if missing:
            raise RuntimeError(f"{path} lacks required timing sidecars: {missing}")
        scanned_rows = 0
        for batch in parquet.iter_batches(columns=list(TIMING_COLUMNS), batch_size=16384):
            columns = {name: batch.column(name) for name in TIMING_COLUMNS}
            cam = _numeric_pairs(
                columns["source.camera_unix_time_ms"], name="camera time"
            )
            state = _numeric_pairs(
                columns["source.state_unix_time_ms"], name="state time"
            )
            near = _numeric_pairs(
                columns["source.htc_nearest_gap_ms"],
                name="HTC nearest gap",
                nonnegative=True,
            )
            br = _numeric_pairs(
                columns["source.htc_bracket_gap_ms"],
                name="HTC bracket gap",
                nonnegative=True,
            )
            reuse = _boolean_pairs(
                columns["source.frame_reused"], name="frame reuse"
            )
            valid = _boolean_pairs(columns["source.htc_valid"], name="HTC valid")
            camera_delta.append(np.abs(cam[:, 1] - cam[:, 0]))
            state_delta.append(np.abs(state - cam))
            nearest.append(near)
            bracket.append(br)
            reused_count += np.count_nonzero(reuse, axis=0)
            invalid_count += np.count_nonzero(~valid, axis=0)
            total += len(cam)
            scanned_rows += len(cam)
        if scanned_rows != rows:
            raise RuntimeError(
                f"Parquet scan row count changed for {path}: "
                f"metadata={rows} scanned={scanned_rows}"
            )
        digest = _sha256_file(path)
        _require_unchanged(path, parquet_stats[path])
        parquet_manifest.append(
            {
                "path": str(path.relative_to(root)),
                "size_bytes": parquet_stats[path][2],
                "rows": rows,
                "sha256": digest,
            }
        )
    verify_input_snapshot()
    parquet_manifest_sha256 = _sha256_payload(parquet_manifest)

    camera_values = np.concatenate(camera_delta)
    state_values = np.concatenate(state_delta, axis=0)
    nearest_values = np.concatenate(nearest, axis=0)
    bracket_values = np.concatenate(bracket, axis=0)
    soft_nearest_from_cli = soft_htc_nearest_ms is not None
    soft_bracket_from_cli = soft_htc_bracket_ms is not None
    hard_nearest_from_cli = hard_htc_nearest_ms is not None
    hard_bracket_from_cli = hard_htc_bracket_ms is not None
    report_soft_nearest, soft_nearest_source = _report_threshold(
        sync_report, "max_htc_nearest_gap_ms", 10.0
    )
    report_soft_bracket, soft_bracket_source = _report_threshold(
        sync_report, "max_htc_bracket_gap_ms", 20.0
    )
    report_hard_gap, hard_gap_source = _report_threshold(
        sync_report, "hard_gap_reject_ms", 20.0
    )
    soft_htc_nearest_ms = (
        report_soft_nearest if soft_htc_nearest_ms is None else soft_htc_nearest_ms
    )
    soft_htc_bracket_ms = (
        report_soft_bracket if soft_htc_bracket_ms is None else soft_htc_bracket_ms
    )
    hard_htc_nearest_ms = (
        report_hard_gap if hard_htc_nearest_ms is None else hard_htc_nearest_ms
    )
    hard_htc_bracket_ms = (
        report_hard_gap if hard_htc_bracket_ms is None else hard_htc_bracket_ms
    )
    for name, value in (
        ("hard_camera_pair_ms", hard_camera_pair_ms),
        ("hard_camera_to_state_ms", hard_camera_to_state_ms),
        ("soft_htc_nearest_ms", soft_htc_nearest_ms),
        ("soft_htc_bracket_ms", soft_htc_bracket_ms),
        ("hard_htc_nearest_ms", hard_htc_nearest_ms),
        ("hard_htc_bracket_ms", hard_htc_bracket_ms),
    ):
        if not np.isfinite(value) or value <= 0:
            raise RuntimeError(f"{name} must be a finite positive number; got {value!r}")
    if hard_htc_nearest_ms < soft_htc_nearest_ms:
        raise RuntimeError("hard HTC nearest limit must not be below the soft quality target")
    if hard_htc_bracket_ms < soft_htc_bracket_ms:
        raise RuntimeError("hard HTC bracket limit must not be below the soft quality target")
    (
        embedded_count,
        calibration_source_count,
        calibration_evidence,
    ) = _embedded_calibration_evidence(
        sync_report, require_accepted=require_embedded_calibration
    )

    camera_over = int(np.count_nonzero(camera_values > hard_camera_pair_ms))
    state_over = np.count_nonzero(state_values > hard_camera_to_state_ms, axis=0)
    nearest_soft_over = np.count_nonzero(nearest_values > soft_htc_nearest_ms, axis=0)
    bracket_soft_over = np.count_nonzero(bracket_values > soft_htc_bracket_ms, axis=0)
    nearest_over = np.count_nonzero(nearest_values > hard_htc_nearest_ms, axis=0)
    bracket_over = np.count_nonzero(bracket_values > hard_htc_bracket_ms, axis=0)

    violations: list[str] = []
    if expected_fps is not None and int(contract["fps"]) != int(expected_fps):
        violations.append(
            f"dataset FPS mismatch: expected={int(expected_fps)} actual={contract['fps']}"
        )
    temporal_sampling = [
        {
            "source_alignment_fps": report.get("source_alignment_fps"),
            "down_sample_steps": report.get("down_sample_steps"),
            "nominal_lerobot_fps": report.get("nominal_lerobot_fps"),
        }
        for report in _source_sync_reports(sync_report)
    ]
    if expected_fps is not None:
        expected_sampling = {
            "source_alignment_fps": int(expected_fps) * 2,
            "down_sample_steps": 2,
            "nominal_lerobot_fps": int(expected_fps),
        }
        if not temporal_sampling or any(item != expected_sampling for item in temporal_sampling):
            violations.append(
                "sync report lacks the required uniform temporal decimation contract: "
                f"expected={expected_sampling} actual={temporal_sampling}"
            )
    if camera_over:
        violations.append(
            f"{camera_over} frames exceed the {hard_camera_pair_ms:g} ms left/right camera limit"
        )
    if np.any(state_over):
        violations.append(
            "camera-to-state alignment exceeds the hard limit: "
            f"limit_ms={hard_camera_to_state_ms:g} "
            f"over=(right={state_over[0]},left={state_over[1]})"
        )
    if np.any(nearest_over) or np.any(bracket_over):
        violations.append(
            "HTC sampling gaps exceed hard limits: "
            f"nearest_over=(right={nearest_over[0]},left={nearest_over[1]}) "
            f"bracket_over=(right={bracket_over[0]},left={bracket_over[1]})"
        )
    warnings: list[str] = []
    if np.any(nearest_soft_over) or np.any(bracket_soft_over):
        warnings.append(
            "HTC sampling gaps exceed soft quality targets but remain below hard rejection limits: "
            f"nearest_over=(right={nearest_soft_over[0]},left={nearest_soft_over[1]}) "
            f"bracket_over=(right={bracket_soft_over[0]},left={bracket_soft_over[1]})"
        )
    if np.any(invalid_count):
        warnings.append(
            "Rows marked source.htc_valid=false exceed converter soft quality targets: "
            f"right={invalid_count[0]} left={invalid_count[1]}"
        )

    result = {
        "schema": "instaumi-dataset-sync-audit-v2",
        "status": "accepted" if not violations else "rejected",
        "violations": violations,
        "warnings": warnings,
        "dataset": str(root),
        "frames_scanned": total,
        "contract": contract,
        "temporal_sampling": temporal_sampling,
        "camera_pair_abs_ms": _distribution(camera_values),
        "camera_pair_over_10ms": int(np.count_nonzero(camera_values > 10.0)),
        "camera_pair_over_hard_limit": camera_over,
        "hard_camera_pair_limit_ms": hard_camera_pair_ms,
        "camera_to_state_abs_ms": {
            "right": _distribution(state_values[:, 0]),
            "left": _distribution(state_values[:, 1]),
            "hard_limit_ms": hard_camera_to_state_ms,
            "over_hard_limit": {
                "right": int(state_over[0]),
                "left": int(state_over[1]),
            },
        },
        "htc_nearest_gap_ms": {
            "right": _distribution(nearest_values[:, 0]),
            "left": _distribution(nearest_values[:, 1]),
            "soft_limit_ms": soft_htc_nearest_ms,
            "over_soft_limit": {
                "right": int(nearest_soft_over[0]),
                "left": int(nearest_soft_over[1]),
            },
            "hard_limit_ms": hard_htc_nearest_ms,
            "over_hard_limit": {"right": int(nearest_over[0]), "left": int(nearest_over[1])},
        },
        "htc_bracket_gap_ms": {
            "right": _distribution(bracket_values[:, 0]),
            "left": _distribution(bracket_values[:, 1]),
            "soft_limit_ms": soft_htc_bracket_ms,
            "over_soft_limit": {
                "right": int(bracket_soft_over[0]),
                "left": int(bracket_soft_over[1]),
            },
            "hard_limit_ms": hard_htc_bracket_ms,
            "over_hard_limit": {"right": int(bracket_over[0]), "left": int(bracket_over[1])},
        },
        "source_frame_reused": {"right": int(reused_count[0]), "left": int(reused_count[1])},
        "htc_soft_invalid": {"right": int(invalid_count[0]), "left": int(invalid_count[1])},
        "threshold_sources": {
            "soft_nearest": "cli" if soft_nearest_from_cli else soft_nearest_source,
            "soft_bracket": "cli" if soft_bracket_from_cli else soft_bracket_source,
            "hard_nearest": "cli" if hard_nearest_from_cli else hard_gap_source,
            "hard_bracket": "cli" if hard_bracket_from_cli else hard_gap_source,
        },
        "legacy_allow_invalid_htc_requested": bool(allow_invalid_htc),
        "evidence_binding": {
            "sync_report_sha256": sync_report_sha256,
            "parquet_manifest_sha256": parquet_manifest_sha256,
            "parquet_file_count": len(parquet_manifest),
            "parquet_total_rows": sum(item["rows"] for item in parquet_manifest),
            "parquet_total_bytes": sum(
                item["size_bytes"] for item in parquet_manifest
            ),
        },
        "calibration_reports_embedded": {
            "count": embedded_count,
            "source_count": calibration_source_count,
            "evidence": calibration_evidence,
        },
    }
    verify_input_snapshot()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--hard-camera-pair-ms", type=float, default=20.0)
    parser.add_argument(
        "--hard-camera-to-state-ms",
        type=float,
        default=CAMERA_TO_STATE_MAX_ABS_MS,
        help=(
            "Reject when either arm's selected state timestamp differs from its "
            "camera timestamp by more than this many milliseconds."
        ),
    )
    parser.add_argument(
        "--soft-htc-nearest-ms",
        type=float,
        help="Quality target; defaults to max_htc_nearest_gap_ms from sync_report.json.",
    )
    parser.add_argument(
        "--soft-htc-bracket-ms",
        type=float,
        help="Quality target; defaults to max_htc_bracket_gap_ms from sync_report.json.",
    )
    parser.add_argument(
        "--hard-htc-nearest-ms",
        type=float,
        help="Rejection limit; defaults to hard_gap_reject_ms from sync_report.json.",
    )
    parser.add_argument(
        "--hard-htc-bracket-ms",
        type=float,
        help="Rejection limit; defaults to hard_gap_reject_ms from sync_report.json.",
    )
    parser.add_argument("--require-embedded-calibration", action="store_true")
    parser.add_argument(
        "--expected-fps",
        type=int,
        help="Reject the dataset unless its LeRobot metadata has this exact FPS.",
    )
    parser.add_argument(
        "--allow-invalid-htc",
        action="store_true",
        help="Deprecated compatibility flag; source.htc_valid is now explicitly a soft-quality marker.",
    )
    parser.add_argument(
        "--allow-image-free-numeric",
        action="store_true",
        help=(
            "Compatibility assertion for compact `_05` callers. Image-free "
            "storage is accepted only when conversion_plan.json independently "
            "declares panorama_numeric_only=true; all synchronization gates remain mandatory."
        ),
    )
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = audit(
        args.dataset,
        args.hard_camera_pair_ms,
        args.hard_htc_nearest_ms,
        args.hard_htc_bracket_ms,
        args.soft_htc_nearest_ms,
        args.soft_htc_bracket_ms,
        args.require_embedded_calibration,
        args.allow_invalid_htc,
        args.expected_fps,
        args.allow_image_free_numeric,
        args.hard_camera_to_state_ms,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    print(payload)
    if args.json_output:
        output = args.json_output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(
            f".{output.name}.partial.{os.getpid()}.{uuid.uuid4().hex}"
        )
        try:
            with partial.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, output)
            descriptor = os.open(
                output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            partial.unlink(missing_ok=True)
    if result["status"] != "accepted":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
