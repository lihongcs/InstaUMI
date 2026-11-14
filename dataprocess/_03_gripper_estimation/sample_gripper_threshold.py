#!/usr/bin/env python3
"""Sample representative complete INSV episodes with the production _03 path.

This independent preview never creates/edits LeRobot data and writes only below
``<capture>/gripper_threshold_sampling``. It selects complete representative
episodes cheaply from the capture event log, then reuses the production PTS
decoder, ArUco detector, calibration, and segment-local interpolation. It
publishes every second filled row at 30 Hz without applying binarization.
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import contextlib
import csv
import fcntl
import importlib.util
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence


HERE = Path(__file__).resolve().parent
ESTIMATOR_PATH = HERE / "estimate_gripper_width.py"
SCHEMA = "instaumi.gripper_threshold_sampling.v1"
ON_DEMAND_POINTER_SCHEMA = "instaumi.gripper_threshold_on_demand_pointer.v1"
sys.dont_write_bytecode = True


def _load_estimator():
    spec = importlib.util.spec_from_file_location("instaumi_exact_gripper_estimator", ESTIMATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load production gripper estimator: {ESTIMATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EST = _load_estimator()


def require_capture_child(path: Path, capture: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(capture)
    except ValueError as exc:
        raise ValueError(f"{label} must be stored under the raw capture: {resolved}") from exc
    if not relative.parts:
        raise ValueError(f"{label} cannot be the raw capture root")
    if relative.parts[0] in {"tmp", "lerobot", "active_view", "active_view_lerobot"}:
        raise ValueError(f"{label} must not overlap a standard or active pipeline workspace: {resolved}")
    return resolved


def camera_dir(capture: Path, side: str) -> Path:
    role = capture / "insta" / f"insta_{side}"
    legacy = capture / "insta" / ("camera_1" if side == "right" else "camera_2")
    for candidate in (role, legacy):
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(f"missing {side} Insta directory under {capture / 'insta'}")


def chapter_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_00_(\d+)\.insv$", path.name, flags=re.IGNORECASE)
    return (int(match.group(1)) if match else 2**31 - 1, path.name)


def discover_sources(capture: Path, side: str, ffprobe_bin: str) -> tuple[list[dict], float, int]:
    paths = sorted(camera_dir(capture, side).glob("*.insv"), key=chapter_sort_key)
    if not paths:
        raise FileNotFoundError(f"no INSV chapters found for {side}")
    sources: list[dict] = []
    global_start = 0
    reference: tuple[int, int, str] | None = None
    for source_index, path in enumerate(paths):
        path = path.resolve()
        probe = EST.probe_video(path, ffprobe_bin)
        signature = (int(probe["width"]), int(probe["height"]), str(probe["fps"]))
        if reference is None:
            reference = signature
        elif signature != reference:
            raise RuntimeError(
                f"{side} INSV format changes across chapters: {signature} != {reference}: {path}"
            )
        sources.append(
            {
                "source_index": source_index,
                "path": path,
                "probe": probe,
                "identity": EST.sampled_file_identity(path),
                "global_start": global_start,
            }
        )
        global_start += int(probe["frame_count"])
    assert reference is not None
    return sources, float(sources[0]["probe"]["fps_float"]), global_start


def unix_ns(record: dict) -> int | None:
    for key in ("unix_time_ns", "unixTimeNs"):
        value = record.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def walk_dicts(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def parse_capture_events(timestamp_log: Path, side: str) -> tuple[int, list[tuple[int, int, int]]]:
    records: list[dict] = []
    with timestamp_log.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {timestamp_log}:{line_number}") from exc
            if isinstance(item, dict):
                records.append(item)

    anchor_ns: int | None = None
    role = f"insta_{side}"
    for record in records:
        if str(record.get("type", record.get("title", ""))) != "total_start":
            continue
        for node in walk_dicts(record.get("extra")):
            results = node.get("camera_results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict) or result.get("role") != role:
                    continue
                value = result.get("dispatch_unix_ns")
                if value is not None:
                    anchor_ns = int(value)
                    break
            if anchor_ns is not None:
                break
        if anchor_ns is None:
            anchor_ns = unix_ns(record)
        if anchor_ns is not None:
            break
    if anchor_ns is None:
        raise RuntimeError(f"timestamp log has no camera start anchor for {side}: {timestamp_log}")

    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    for record in records:
        kind = str(record.get("type", record.get("title", "")))
        if kind not in {"segment_start", "segment_end"}:
            continue
        value = record.get("segment_index", record.get("segmentIndex"))
        timestamp = unix_ns(record)
        if value is None or timestamp is None:
            continue
        index = int(value)
        target = starts if kind == "segment_start" else ends
        if index in target:
            raise RuntimeError(f"duplicate {kind} for segment {index}: {timestamp_log}")
        target[index] = timestamp
    ranges = []
    for index in sorted(set(starts) & set(ends)):
        if ends[index] <= starts[index]:
            raise RuntimeError(f"non-positive segment {index} in {timestamp_log}")
        ranges.append((index, starts[index], ends[index]))
    if not ranges:
        raise RuntimeError(f"timestamp log has no complete segment ranges: {timestamp_log}")
    return anchor_ns, ranges


def evenly_select(values: Sequence[int], count: int) -> list[int]:
    if count <= 0 or count >= len(values):
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    positions = [round(i * (len(values) - 1) / (count - 1)) for i in range(count)]
    return [values[index] for index in positions]


def sample_interval(start: int, end_exclusive: int, count: int) -> list[int]:
    if count <= 0 or end_exclusive <= start:
        return []
    length = end_exclusive - start
    count = min(count, length)
    return sorted({start + min(length - 1, ((2 * i + 1) * length) // (2 * count)) for i in range(count)})


def parse_episode_indices(text: str | None) -> list[int] | None:
    if text is None or not text.strip():
        return None
    values = [int(item.strip()) for item in text.split(",") if item.strip()]
    if len(values) != len(set(values)):
        raise ValueError("--episode-indices contains duplicates")
    return values


def choose_sample_frames(
    *,
    ranges: Sequence[tuple[int, int, int]],
    anchor_ns: int,
    fps: float,
    total_frames: int,
    episode_count: int,
    episode_indices: Sequence[int] | None,
    require_fully_bounded: bool = False,
) -> tuple[list[tuple[int, int]], list[int]]:
    by_index = {index: (start_ns, end_ns) for index, start_ns, end_ns in ranges}
    if episode_indices is None:
        selected_indices = evenly_select(sorted(by_index), episode_count)
    else:
        missing = sorted(set(episode_indices) - set(by_index))
        if missing:
            raise ValueError(f"requested episode indices are missing complete start/end events: {missing}")
        selected_indices = list(episode_indices)
    selected: list[tuple[int, int]] = []
    for episode_index in selected_indices:
        start_ns, end_ns = by_index[episode_index]
        mapped_start = int(round((start_ns - anchor_ns) * fps / 1_000_000_000.0))
        mapped_end = int(round((end_ns - anchor_ns) * fps / 1_000_000_000.0))
        if require_fully_bounded and (mapped_start < 0 or mapped_end > total_frames):
            raise ValueError(
                f"episode {episode_index} is not fully contained in the {total_frames}-frame "
                f"{fps:.9f} Hz camera timeline: "
                f"mapped={mapped_start}:{mapped_end}"
            )
        start = max(0, mapped_start)
        end = min(total_frames, mapped_end)
        # Decode the complete episode. Sparse isolated frames can miss a fast
        # close/open transition and would not exercise production segment-local
        # interpolation, making a threshold decision deceptively optimistic.
        for frame_index in range(start, end):
            selected.append((episode_index, frame_index))
    selected.sort(key=lambda item: item[1])
    if not selected:
        raise RuntimeError("the selected capture episodes do not overlap the INSV timeline")
    frame_indices = [item[1] for item in selected]
    if len(frame_indices) != len(set(frame_indices)):
        raise RuntimeError("episode-to-camera sampling produced duplicate frame indices")
    return selected, selected_indices


def source_for_global_frame(sources: Sequence[dict], frame_index: int) -> tuple[dict, int]:
    starts = [int(source["global_start"]) for source in sources]
    position = bisect.bisect_right(starts, frame_index) - 1
    if position < 0:
        raise ValueError(f"frame before first source: {frame_index}")
    source = sources[position]
    local = frame_index - int(source["global_start"])
    if local < 0 or local >= int(source["probe"]["frame_count"]):
        raise ValueError(f"frame outside source plan: {frame_index}")
    return source, local


def atomic_write_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        digest = EST.sha256_file(temporary)
        os.replace(temporary, path)
        EST.fsync_directory(path.parent)
        return digest
    finally:
        temporary.unlink(missing_ok=True)


def quantile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def summarize(rows: Sequence[dict]) -> dict:
    widths = [float(row["width_m"]) for row in rows if row["width_m"] not in (None, "")]
    raw_valid = sum(int(row["valid"]) == 1 for row in rows)
    return {
        "sample_count": len(rows),
        "raw_aruco_valid_count": raw_valid,
        "raw_aruco_valid_ratio": 0.0 if not rows else raw_valid / len(rows),
        "filled_width_count": len(widths),
        "filled_width_ratio": 0.0 if not rows else len(widths) / len(rows),
        "width_m": {
            "min": None if not widths else min(widths),
            "p05": quantile(widths, 0.05),
            "p25": quantile(widths, 0.25),
            "p50": quantile(widths, 0.50),
            "p75": quantile(widths, 0.75),
            "p95": quantile(widths, 0.95),
            "max": None if not widths else max(widths),
        },
    }


def find_timestamp_log(capture: Path) -> Path:
    primary = capture / "timestamp_log.jsonl"
    if primary.is_file():
        return primary.resolve()
    alternatives = sorted(capture.glob("timestamp_log_*.jsonl"))
    if alternatives:
        return alternatives[0].resolve()
    raise FileNotFoundError(f"missing timestamp_log under {capture}")


def strict_manifest_child(value: object, root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes its immutable run root: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or unsafe {label}: {path}")
    return path


def validate_trace_csv(path: Path, expected_sha256: object, expected_count: object) -> int:
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError(f"trace has no SHA-256: {path}")
    if EST.sha256_file(path) != expected_sha256:
        raise ValueError(f"trace SHA-256 mismatch: {path}")
    count = int(expected_count)
    if count <= 0:
        raise ValueError(f"trace frame_count must be positive: {path}")
    with path.open("r", newline="", encoding="ascii", errors="strict") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["frame_idx", "width_m", "valid"]:
            raise ValueError(f"unexpected canonical trace columns: {path}: {reader.fieldnames}")
        rows = list(reader)
    if len(rows) != count:
        raise ValueError(f"trace row count mismatch: {path}: {len(rows)} != {count}")
    for expected, row in enumerate(rows):
        if int(row["frame_idx"]) != expected:
            raise ValueError(f"trace frame_idx is not contiguous at {expected}: {path}")
        if row["valid"] not in {"0", "1"}:
            raise ValueError(f"trace valid flag is not binary at {expected}: {path}")
        if row["valid"] == "1":
            width = float(row["width_m"])
            if not math.isfinite(width):
                raise ValueError(f"trace width is not finite at {expected}: {path}")
        elif row["width_m"]:
            raise ValueError(f"invalid trace row unexpectedly carries a width at {expected}: {path}")
    return count


def load_reusable_side_result(
    side_root: Path,
    *,
    fingerprint: str,
    side: str,
    episode_indices: Sequence[int],
) -> dict | None:
    manifest_path = side_root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SCHEMA
            or payload.get("status") != "complete"
            or payload.get("fingerprint") != fingerprint
            or payload.get("side") != side
        ):
            return None
        if payload.get("selection", {}).get("episode_indices") != list(episode_indices):
            return None
        samples = strict_manifest_child(payload.get("samples_csv"), side_root, "samples_csv")
        if EST.sha256_file(samples) != payload.get("samples_csv_sha256"):
            return None
        traces = payload.get("episode_traces")
        if not isinstance(traces, dict) or set(traces) != {str(value) for value in episode_indices}:
            return None
        for episode_index in episode_indices:
            descriptor = traces[str(episode_index)]
            if not isinstance(descriptor, dict):
                return None
            trace = strict_manifest_child(
                descriptor.get("trace_csv"), side_root, f"episode {episode_index} trace"
            )
            validate_trace_csv(trace, descriptor.get("sha256"), descriptor.get("frame_count"))
            if descriptor.get("value_column") != "width_m" or descriptor.get("valid_column") != "valid":
                return None
        result = dict(payload)
        result["reused_complete_artifact"] = True
        return result
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def run_side(args: argparse.Namespace, capture: Path, output_root: Path, side: str) -> dict:
    ffprobe_bin = EST.resolve_ffprobe_bin(args.ffprobe_bin)
    sources, fps, total_frames = discover_sources(capture, side, ffprobe_bin)
    timestamp_log = find_timestamp_log(capture)
    timestamp_log_identity = EST.content_file_identity(timestamp_log)
    anchor_ns, ranges = parse_capture_events(timestamp_log, side)
    selected, selected_episodes = choose_sample_frames(
        ranges=ranges,
        anchor_ns=anchor_ns,
        fps=fps,
        total_frames=total_frames,
        episode_count=args.episode_count,
        episode_indices=args.parsed_episode_indices,
        require_fully_bounded=args.on_demand_episode is not None,
    )
    selected_ranges = []
    for episode_index in selected_episodes:
        frames = [frame for episode, frame in selected if episode == episode_index]
        selected_ranges.append(
            {
                "episode_index": episode_index,
                "start_frame": frames[0],
                "end_frame": frames[-1] + 1,
                "frame_count": len(frames),
            }
        )
    calibration_path = args.gripper_calibration_json.expanduser().resolve()
    calibration_identity = EST.content_file_identity(calibration_path)
    calibration = EST.load_gripper_calibration(calibration_path)
    estimator_identity = EST.content_file_identity(ESTIMATOR_PATH)
    sampler_path = Path(__file__).resolve()
    sampler_identity = EST.content_file_identity(sampler_path)
    detector = EST.detector_parameter_snapshot(args.dictionary)
    decoder = EST.pyav_decoder_runtime_snapshot()
    selection = {
        "side": side,
        "camera_anchor_unix_ns": anchor_ns,
        "mapping": "ble_start_dispatch_plus_native_cfr_approximation_for_threshold_sampling_only",
        "native_fps": fps,
        "total_frames": total_frames,
        "episode_indices": selected_episodes,
        "complete_episode_frames": True,
        "ranges": selected_ranges,
    }
    marker_a = args.left_marker_a if side == "left" else args.right_marker_a
    marker_b = args.left_marker_b if side == "left" else args.right_marker_b
    fingerprint = EST.object_fingerprint(
        {
            "schema": SCHEMA,
            "side": side,
            "sources": [source["identity"] for source in sources],
            "timestamp_log": timestamp_log_identity,
            "selection": selection,
            "dictionary": args.dictionary,
            "marker_a": marker_a,
            "marker_b": marker_b,
            "marker_width_m": args.marker_width_m,
            "calibration": calibration_identity,
            "estimator": estimator_identity,
            "sampler": sampler_identity,
            "detector": detector,
            "decoder": decoder,
        }
    )
    side_root = output_root / "runs" / fingerprint / side
    if not args.plan_only:
        reusable = load_reusable_side_result(
            side_root,
            fingerprint=fingerprint,
            side=side,
            episode_indices=selected_episodes,
        )
        if reusable is not None:
            print(
                f"[reuse] validated immutable {side} threshold trace: {side_root / 'manifest.json'}",
                flush=True,
            )
            return reusable
    # One decode task per contiguous episode/source interval. This preserves
    # every raw 60 Hz frame while avoiding one random seek per frame.
    grouped: list[tuple[int, dict, list[int]]] = []
    for episode_index, global_index in selected:
        source, local_index = source_for_global_frame(sources, global_index)
        if (
            grouped
            and grouped[-1][0] == episode_index
            and int(grouped[-1][1]["source_index"]) == int(source["source_index"])
            and grouped[-1][2][-1] + 1 == local_index
        ):
            grouped[-1][2].append(local_index)
        else:
            grouped.append((episode_index, source, [local_index]))

    tasks: list[dict] = []
    for task_index, (episode_index, source, local_indices) in enumerate(grouped):
        local_start = local_indices[0]
        local_end = local_indices[-1] + 1
        global_start = int(source["global_start"]) + local_start
        global_end = int(source["global_start"]) + local_end
        stem = f"episode_{episode_index:05d}_{global_start:09d}_{global_end:09d}"
        task = {
            "chunk_index": task_index,
            "source_index": int(source["source_index"]),
            "source_identity": source["identity"],
            "decode_path": str(source["path"]),
            "width": int(source["probe"]["width"]),
            "height": int(source["probe"]["height"]),
            "local_start": local_start,
            "local_end": local_end,
            "global_start": global_start,
            "global_end": global_end,
            "source_global_start": int(source["global_start"]),
            "segment_index": episode_index,
            "requested_local_indices": local_indices,
            "requested_global_indices": [int(source["global_start"]) + value for value in local_indices],
            "keyframe_aligned": False,
            "dictionary": args.dictionary,
            "marker_a": marker_a,
            "marker_b": marker_b,
            "decode_threads": args.decode_threads,
            "detection_fingerprint": fingerprint,
            "raw_path": str(side_root / "chunks" / f"{stem}.jsonl"),
            "chunk_manifest_path": str(side_root / "chunks" / f"{stem}.manifest.json"),
            "front_cache_path": None,
        }
        task["task_fingerprint"] = EST.object_fingerprint(
            {
                "detection_fingerprint": fingerprint,
                "source_identity": source["identity"],
                "local_start": local_start,
                "local_end": local_end,
                "global_start": global_start,
                "global_end": global_end,
                "episode_index": episode_index,
            }
        )
        tasks.append(task)
    if args.plan_only:
        return {
            "side": side,
            "fingerprint": fingerprint,
            "selection": selection,
            "sample_count": len(selected),
            "decode_intervals": len(tasks),
            "sources": [str(source["path"]) for source in sources],
        }

    side_root.mkdir(parents=True, exist_ok=True)
    worker_count = min(args.workers, len(tasks))
    resumed, executed = EST.run_detection_chunks(tasks, worker_count, True, args.chunk_retries)
    measurement_args = argparse.Namespace(
        marker_width_m=args.marker_width_m,
        px_to_meter_scale=None,
        calibration_distance_px=None,
        calibration_distance_m=0.10,
        gripper_zero_below_m=None,
    )
    raw_by_frame: dict[int, dict] = {}
    episode_by_frame = {frame: episode for episode, frame in selected}
    source_by_index = {int(source["source_index"]): source for source in sources}
    for task in tasks:
        with Path(task["raw_path"]).open("r", encoding="utf-8") as handle:
            raw_rows = [json.loads(line) for line in handle if line.strip()]
        if len(raw_rows) != len(task["requested_global_indices"]):
            raise RuntimeError(f"episode chunk row count mismatch: {task['raw_path']}")
        for raw in raw_rows:
            frame_index = int(raw["frame_idx"])
            if frame_index in raw_by_frame:
                raise RuntimeError(f"duplicate raw sample frame: {frame_index}")
            raw_by_frame[frame_index] = raw
    expected_frames = [frame for _, frame in selected]
    if sorted(raw_by_frame) != expected_frames:
        raise RuntimeError("raw sample rows do not match the complete-episode selection")

    measurements = {
        frame_index: EST.raw_measurement(raw, calibration, measurement_args)
        for frame_index, raw in raw_by_frame.items()
    }
    nominal_unix_ms = [anchor_ns / 1_000_000.0 + frame / fps * 1000.0 for frame in range(total_frames)]
    filled: dict[int, tuple[float, float, float]] = {}
    segment_reports: list[dict] = []
    for episode_index in selected_episodes:
        episode_frames = [frame for episode, frame in selected if episode == episode_index]
        measured_count = sum(bool(measurements[frame]["valid"]) for frame in episode_frames)
        if measured_count:
            filled.update(
                EST.interpolate_segment_measurements(episode_frames, measurements, nominal_unix_ms)
            )
        segment_reports.append(
            {
                "episode_index": episode_index,
                "source_frame_count_60hz": len(episode_frames),
                "aruco_valid_frames": measured_count,
                "aruco_valid_ratio": measured_count / len(episode_frames),
                "filled_frames": sum(frame in filled for frame in episode_frames),
            }
        )

    # Match the production every-second-row temporal reduction. The preview
    # trace is 30 Hz; raw ArUco detection and interpolation still happen on the
    # complete native 60 Hz segment first.
    selected_30hz: list[int] = []
    for episode_index in selected_episodes:
        frames = [frame for episode, frame in selected if episode == episode_index]
        selected_30hz.extend(frames[::2])

    rows: list[dict] = []
    for frame_index in selected_30hz:
        raw = raw_by_frame[frame_index]
        measurement = measurements[frame_index]
        source, local_index = source_for_global_frame(sources, frame_index)
        filled_value = filled.get(frame_index)
        rows.append(
            {
                "side": side,
                "episode_index": episode_by_frame[frame_index],
                "global_frame_idx": frame_index,
                "source_chapter": int(source["source_index"]),
                "source_local_frame_idx": local_index,
                "valid": int(bool(measurement["valid"])),
                "raw_width_m": "" if measurement["distance_m"] is None else f"{measurement['distance_m']:.9f}",
                "width_m": "" if filled_value is None else f"{filled_value[1]:.9f}",
                "width_cm": "" if filled_value is None else f"{filled_value[2]:.6f}",
                "distance_px": "" if measurement["distance"] is None else f"{measurement['distance']:.6f}",
                "marker_side_px": "" if measurement["marker_side_px"] is None else f"{measurement['marker_side_px']:.6f}",
            }
        )
    rows.sort(key=lambda row: int(row["global_frame_idx"]))
    # Refuse to publish a trace if any source, code, event log, or calibration
    # changed while the complete episodes were being decoded.
    for source in sources:
        if EST.sampled_file_identity(source["path"]) != source["identity"]:
            raise RuntimeError(f"source INSV changed during threshold sampling: {source['path']}")
    if EST.content_file_identity(timestamp_log) != timestamp_log_identity:
        raise RuntimeError(f"timestamp log changed during threshold sampling: {timestamp_log}")
    if EST.content_file_identity(calibration_path) != calibration_identity:
        raise RuntimeError(f"gripper calibration changed during threshold sampling: {calibration_path}")
    if EST.content_file_identity(ESTIMATOR_PATH) != estimator_identity:
        raise RuntimeError(f"production _03 estimator changed during threshold sampling: {ESTIMATOR_PATH}")
    if EST.content_file_identity(sampler_path) != sampler_identity:
        raise RuntimeError(f"threshold sampler changed during threshold sampling: {sampler_path}")
    fieldnames = [
        "side", "episode_index", "global_frame_idx", "source_chapter",
        "source_local_frame_idx", "valid", "raw_width_m", "width_m", "width_cm",
        "distance_px", "marker_side_px",
    ]
    csv_path = side_root / "samples.csv"
    csv_sha256 = atomic_write_csv(csv_path, fieldnames, rows)
    episode_traces: dict[str, dict] = {}
    for episode_index in selected_episodes:
        episode_rows = [row for row in rows if int(row["episode_index"]) == episode_index]
        if not episode_rows:
            raise RuntimeError(f"30 Hz trace is empty for {side} episode {episode_index}")
        trace_rows = [
            {
                "frame_idx": index,
                "width_m": row["width_m"],
                # Canonical preview validity means the production-style
                # segment-local filled width exists, not raw ArUco visibility.
                "valid": int(row["width_m"] not in (None, "")),
            }
            for index, row in enumerate(episode_rows)
        ]
        trace_path = side_root / "traces" / f"episode_{episode_index:06d}_30hz.csv"
        trace_sha256 = atomic_write_csv(
            trace_path, ["frame_idx", "width_m", "valid"], trace_rows
        )
        episode_traces[str(episode_index)] = {
            "trace_csv": str(trace_path),
            "sha256": trace_sha256,
            "frame_count": len(trace_rows),
            "value_column": "width_m",
            "valid_column": "valid",
        }
    summary = summarize(rows)
    result = {
        "schema": SCHEMA,
        "status": "complete",
        "capture": str(capture),
        "side": side,
        "fingerprint": fingerprint,
        "selection": selection,
        "sources": [
            {
                "source_index": source["source_index"],
                "path": str(source["path"]),
                "identity": source["identity"],
                "probe": source["probe"],
                "global_start": source["global_start"],
            }
            for source in sources
        ],
        "quality_contract": {
            "sampler_implementation": sampler_identity,
            "detector_implementation": estimator_identity,
            "decoder": {
                "backend": "production_pyav_pts_cfr_v1",
                "stream": "0:v:0",
                "pixel_format": "bgr24",
                "colorspace": "ITU-601",
                "interpolation": "bilinear",
                "runtime": decoder,
            },
            "detector": detector,
            "calibration": calibration_identity,
            "raw_detection_interpolation_or_fill": False,
            "segment_local_interpolation": "production_interpolate_segment_measurements",
            "temporal_downsample": "complete_60hz_episode_then_every_second_row_to_30hz",
            "deadzone_or_binarization": False,
            "reusable_by_full_stage03": False,
            "reuse_reason": (
                "preview uses a distinct evidence/fingerprint and approximate event-to-frame selection; "
                "full _03 requires its calibrated timeline, complete active-range contract, and front-image cache"
            ),
        },
        "marker_ids": [marker_a, marker_b],
        "samples_csv": str(csv_path),
        "samples_csv_sha256": csv_sha256,
        "execution": {
            "workers": worker_count,
            "decode_threads_per_worker": args.decode_threads,
            "chunk_retries": args.chunk_retries,
            "resumed_chunks": resumed,
            "executed_chunks": executed,
        },
        "summary": summary,
        "segment_reports": segment_reports,
        "episode_traces": episode_traces,
    }
    EST.atomic_write_json(side_root / "manifest.json", result)
    return result


def combine_results(capture: Path, output_root: Path, results: Sequence[dict]) -> dict:
    if any(result.get("status") != "complete" for result in results):
        raise RuntimeError("cannot publish an incomplete sampling run")
    fingerprints = {result["side"]: result["fingerprint"] for result in results}
    combined_fingerprint = EST.object_fingerprint(fingerprints)
    combined_root = output_root / "runs" / combined_fingerprint
    combined_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for result in results:
        with Path(result["samples_csv"]).open("r", newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    rows.sort(key=lambda row: (int(row["global_frame_idx"]), row["side"]))
    fieldnames = list(rows[0]) if rows else []
    csv_path = combined_root / "samples.csv"
    csv_sha256 = atomic_write_csv(csv_path, fieldnames, rows)
    summary = {
        "combined": summarize(rows),
        **{result["side"]: result["summary"] for result in results},
    }
    manifest = {
        "schema": SCHEMA,
        "status": "complete",
        "capture": str(capture),
        "fingerprint": combined_fingerprint,
        "sides": fingerprints,
        "samples_csv": str(csv_path),
        "samples_csv_sha256": csv_sha256,
        "summary": summary,
        "threshold_semantics": "preview only: width < threshold -> 0, width >= threshold -> 1",
        "warning": (
            "Episode mapping uses BLE start dispatch and native CFR only to choose representative "
            "frames. It is not training synchronization evidence. Width pixels, ArUco detection, "
            "and calibration use the exact production stage-_03 implementation."
        ),
        "side_manifests": {
            result["side"]: str(output_root / "runs" / result["fingerprint"] / result["side"] / "manifest.json")
            for result in results
        },
    }
    EST.atomic_write_json(combined_root / "manifest.json", manifest)
    by_side = {result["side"]: result for result in results}
    episode_sets = {
        side: set(result["episode_traces"])
        for side, result in by_side.items()
    }
    if set(episode_sets) != {"left", "right"} or episode_sets["left"] != episode_sets["right"]:
        raise RuntimeError(f"left/right preview episodes differ: {episode_sets}")
    canonical_episodes = []
    for episode_index_text in sorted(episode_sets["left"], key=int):
        entry: dict[str, object] = {"episode_index": int(episode_index_text)}
        for side in ("right", "left"):
            descriptor = dict(by_side[side]["episode_traces"][episode_index_text])
            descriptor["trace_csv"] = os.path.relpath(
                descriptor["trace_csv"], output_root
            )
            entry[side] = descriptor
        canonical_episodes.append(entry)
    canonical_manifest = {
        "schema": "instaumi_gripper_threshold_preview_v1",
        "name": f"{capture.name} representative complete-episode gripper traces",
        "task": "",
        "fps": 30,
        "selection": {
            "episode_count": len(canonical_episodes),
            "same_episode_indices_for_both_arms": True,
            "complete_native_60hz_segments": True,
            "down_sample_steps": 2,
        },
        "quality": {
            "raw_detector": "exact production stage-_03 implementation",
            "segment_local_interpolation": "production interpolate_segment_measurements",
            "binarization_applied": False,
            "reusable_by_full_stage03": False,
        },
        "audit_manifest": os.path.relpath(combined_root / "manifest.json", output_root),
        "episodes": canonical_episodes,
    }
    # This is the stable DataCap-facing publication barrier. It is committed
    # only after every trace and both audit manifests have been fsynced.
    EST.atomic_write_json(output_root / "manifest.json", canonical_manifest)
    EST.atomic_write_json(
        output_root / "latest.json",
        {
            "schema": SCHEMA,
            "status": "complete",
            "capture": str(capture),
            "fingerprint": combined_fingerprint,
            "manifest": str(combined_root / "manifest.json"),
            "samples_csv": str(csv_path),
            "samples_csv_sha256": csv_sha256,
        },
    )
    return manifest


def atomic_copy_verified(source: Path, destination: Path, expected_sha256: str) -> str:
    if EST.sha256_file(source) != expected_sha256:
        raise ValueError(f"source trace changed before immutable publication: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            while block := reader.read(1024 * 1024):
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        copied_sha256 = EST.sha256_file(temporary)
        if copied_sha256 != expected_sha256:
            raise RuntimeError(f"trace copy SHA-256 mismatch: {source} -> {destination}")
        os.replace(temporary, destination)
        EST.fsync_directory(destination.parent)
        return copied_sha256
    finally:
        temporary.unlink(missing_ok=True)


def validate_on_demand_manifest(
    manifest_path: Path,
    *,
    capture: Path,
    episode_index: int,
    fingerprint: str,
) -> dict | None:
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    run_root = manifest_path.parent.resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "instaumi_gripper_threshold_preview_v1"
            or payload.get("status") != "complete"
            or payload.get("mode") != "on_demand_single_episode"
            or payload.get("fingerprint") != fingerprint
            or payload.get("fps") != 30
            or payload.get("capture") != str(capture)
        ):
            return None
        side_fingerprints = payload.get("source_side_fingerprints")
        if not isinstance(side_fingerprints, dict) or set(side_fingerprints) != {"left", "right"}:
            return None
        expected_fingerprint = EST.object_fingerprint(
            {
                "schema": "instaumi_gripper_threshold_on_demand.v1",
                "capture": str(capture),
                "episode_index": episode_index,
                "side_fingerprints": {
                    side: side_fingerprints[side] for side in ("right", "left")
                },
                "fps": 30,
            }
        )
        if expected_fingerprint != fingerprint:
            return None
        episodes = payload.get("episodes")
        if not isinstance(episodes, list) or len(episodes) != 1:
            return None
        episode = episodes[0]
        if not isinstance(episode, dict) or episode.get("episode_index") != episode_index:
            return None
        for side in ("right", "left"):
            descriptor = episode.get(side)
            if not isinstance(descriptor, dict):
                return None
            trace = strict_manifest_child(
                descriptor.get("trace_csv"), run_root, f"on-demand {side} trace"
            )
            validate_trace_csv(trace, descriptor.get("sha256"), descriptor.get("frame_count"))
            if descriptor.get("value_column") != "width_m" or descriptor.get("valid_column") != "valid":
                return None
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def publish_on_demand_results(
    capture: Path,
    base_output_root: Path,
    episode_index: int,
    results: Sequence[dict],
) -> dict:
    if any(result.get("status") != "complete" for result in results):
        raise RuntimeError("cannot publish incomplete on-demand threshold traces")
    by_side = {result.get("side"): result for result in results}
    if set(by_side) != {"left", "right"}:
        raise RuntimeError("on-demand sampling requires exactly one result for each arm")
    episode_key = str(episode_index)
    for side, result in by_side.items():
        if set(result.get("episode_traces", {})) != {episode_key}:
            raise RuntimeError(f"{side} on-demand result is not isolated to episode {episode_index}")

    side_fingerprints = {side: by_side[side]["fingerprint"] for side in ("right", "left")}
    fingerprint = EST.object_fingerprint(
        {
            "schema": "instaumi_gripper_threshold_on_demand.v1",
            "capture": str(capture),
            "episode_index": episode_index,
            "side_fingerprints": side_fingerprints,
            "fps": 30,
        }
    )
    on_demand_root = base_output_root / "on_demand"
    run_root = on_demand_root / "runs" / fingerprint
    manifest_path = run_root / "manifest.json"
    reusable = validate_on_demand_manifest(
        manifest_path,
        capture=capture,
        episode_index=episode_index,
        fingerprint=fingerprint,
    )
    if reusable is not None:
        manifest_sha256 = EST.sha256_file(manifest_path)
        pointer_path = ensure_on_demand_pointer(
            capture=capture,
            on_demand_root=on_demand_root,
            episode_index=episode_index,
            fingerprint=fingerprint,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )
        return {
            "status": "complete",
            "reused": True,
            "episode_index": episode_index,
            "fingerprint": fingerprint,
            "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "pointer": str(pointer_path),
        }

    descriptors: dict[str, dict] = {}
    for side in ("right", "left"):
        source_descriptor = by_side[side]["episode_traces"][episode_key]
        source_trace = Path(source_descriptor["trace_csv"]).resolve()
        destination = run_root / "traces" / f"episode_{episode_index:06d}_{side}_30hz.csv"
        copied_hash = atomic_copy_verified(
            source_trace, destination, str(source_descriptor["sha256"])
        )
        frame_count = validate_trace_csv(
            destination, copied_hash, source_descriptor["frame_count"]
        )
        descriptors[side] = {
            "trace_csv": os.path.relpath(destination, run_root),
            "sha256": copied_hash,
            "frame_count": frame_count,
            "value_column": "width_m",
            "valid_column": "valid",
        }
    manifest = {
        "schema": "instaumi_gripper_threshold_preview_v1",
        "status": "complete",
        "mode": "on_demand_single_episode",
        "name": f"{capture.name} on-demand gripper episode {episode_index}",
        "task": "",
        "fps": 30,
        "capture": str(capture),
        "episode_index": episode_index,
        "fingerprint": fingerprint,
        "source_side_fingerprints": side_fingerprints,
        "quality": {
            "complete_native_60hz_episode": True,
            "raw_detector": "exact production stage-_03 implementation",
            "segment_local_interpolation": "production interpolate_segment_measurements",
            "down_sample_steps": 2,
            "binarization_applied": False,
            "original_capture_mutated": False,
        },
        "execution": {
            "parallel_arms": True,
            "workers_per_arm": 1,
            "decode_threads_per_arm": 1,
        },
        "episodes": [
            {
                "episode_index": episode_index,
                "right": descriptors["right"],
                "left": descriptors["left"],
            }
        ],
    }
    EST.atomic_write_json(manifest_path, manifest)
    verified = validate_on_demand_manifest(
        manifest_path,
        capture=capture,
        episode_index=episode_index,
        fingerprint=fingerprint,
    )
    if verified is None:
        raise RuntimeError(f"on-demand manifest failed post-publication validation: {manifest_path}")
    manifest_sha256 = EST.sha256_file(manifest_path)
    pointer_path = ensure_on_demand_pointer(
        capture=capture,
        on_demand_root=on_demand_root,
        episode_index=episode_index,
        fingerprint=fingerprint,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    return {
        "status": "complete",
        "reused": False,
        "episode_index": episode_index,
        "fingerprint": fingerprint,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "pointer": str(pointer_path),
    }


def ensure_on_demand_pointer(
    *,
    capture: Path,
    on_demand_root: Path,
    episode_index: int,
    fingerprint: str,
    manifest_path: Path,
    manifest_sha256: str,
) -> Path:
    """Publish the stable mutable episode pointer without rewriting valid runs."""

    pointer_path = on_demand_root / "episodes" / f"episode_{episode_index:06d}.json"
    payload = {
        "schema": ON_DEMAND_POINTER_SCHEMA,
        "status": "complete",
        "capture": str(capture),
        "episode_index": episode_index,
        "fingerprint": fingerprint,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }
    if pointer_path.is_file() and not pointer_path.is_symlink():
        try:
            if json.loads(pointer_path.read_text(encoding="utf-8")) == payload:
                if validate_on_demand_pointer(
                    pointer_path,
                    capture=capture,
                    episode_index=episode_index,
                    fingerprint=fingerprint,
                    manifest_path=manifest_path,
                    manifest_sha256=manifest_sha256,
                ):
                    return pointer_path
        except (OSError, json.JSONDecodeError):
            pass
    EST.atomic_write_json(pointer_path, payload)
    if not validate_on_demand_pointer(
        pointer_path,
        capture=capture,
        episode_index=episode_index,
        fingerprint=fingerprint,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    ):
        raise RuntimeError(f"on-demand pointer failed post-publication validation: {pointer_path}")
    return pointer_path


def validate_on_demand_pointer(
    pointer_path: Path,
    *,
    capture: Path,
    episode_index: int,
    fingerprint: str,
    manifest_path: Path,
    manifest_sha256: str,
) -> bool:
    """Strictly validate the mutable pointer and its immutable target."""

    if not pointer_path.is_file() or pointer_path.is_symlink():
        return False
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        expected = {
            "schema": ON_DEMAND_POINTER_SCHEMA,
            "status": "complete",
            "capture": str(capture),
            "episode_index": episode_index,
            "fingerprint": fingerprint,
            "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha256,
        }
        if payload != expected:
            return False
        target = Path(payload["manifest"])
        if not target.is_absolute() or target.resolve() != manifest_path.resolve():
            return False
        immutable_runs_root = (pointer_path.parent.parent / "runs").resolve()
        try:
            target.resolve().relative_to(immutable_runs_root)
        except ValueError:
            return False
        if target.is_symlink() or EST.sha256_file(target) != manifest_sha256:
            return False
        return validate_on_demand_manifest(
            target,
            capture=capture,
            episode_index=episode_index,
            fingerprint=fingerprint,
        ) is not None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def run_on_demand_sides(
    args: argparse.Namespace,
    capture: Path,
    on_demand_root: Path,
) -> list[dict]:
    """Decode both arms concurrently while keeping each arm single-threaded.

    A several-second episode normally maps to one contiguous decode task per
    arm.  Running the arms in two threads lets the independent INSV streams
    decode concurrently.  ``run_detection_chunks`` stays on its direct,
    single-worker path, and every PyAV decoder is explicitly limited to one
    decode thread, so this creates exactly two video decoders rather than a
    nested process/thread fan-out.
    """

    if args.decode_threads != 1 or args.workers != 1:
        raise ValueError("on-demand sampling requires workers=1 and decode_threads=1 per arm")
    if args.plan_only:
        return [run_side(args, capture, on_demand_root, side) for side in ("left", "right")]

    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="gripper-on-demand-arm"
    ) as pool:
        futures = {
            pool.submit(run_side, args, capture, on_demand_root, side): side
            for side in ("left", "right")
        }
        try:
            for future in concurrent.futures.as_completed(futures):
                side = futures[future]
                results[side] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [results[side] for side in ("left", "right")]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lightweight exact-_03 gripper-width sampling before full LeRobot conversion"
    )
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--episode-count", type=int, default=8)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--episode-indices", default=None, help="comma-separated explicit segment indices"
    )
    selection.add_argument(
        "--on-demand-episode",
        type=int,
        default=None,
        help="decode one complete episode into an isolated reusable DataCap artifact",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--decode-threads", type=int, default=1)
    parser.add_argument("--chunk-retries", type=int, default=2)
    parser.add_argument("--dictionary", default="DICT_4X4_50")
    parser.add_argument("--left-marker-a", type=int, default=0)
    parser.add_argument("--left-marker-b", type=int, default=3)
    parser.add_argument("--right-marker-a", type=int, default=1)
    parser.add_argument("--right-marker-b", type=int, default=2)
    parser.add_argument("--marker-width-m", type=float, default=0.018)
    parser.add_argument(
        "--gripper-calibration-json", type=Path, default=EST.DEFAULT_GRIPPER_CALIBRATION_JSON
    )
    parser.add_argument("--ffprobe-bin", default=None)
    parser.add_argument("--plan-only", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.episode_count < 0:
        parser.error("--episode-count must be >= 0 (0 means all episodes)")
    if args.workers <= 0 or args.decode_threads <= 0:
        parser.error("--workers and --decode-threads must be > 0")
    if args.chunk_retries < 0:
        parser.error("--chunk-retries must be >= 0")
    if not math.isfinite(args.marker_width_m) or args.marker_width_m <= 0:
        parser.error("--marker-width-m must be a finite positive value")
    if args.on_demand_episode is not None and args.on_demand_episode < 0:
        parser.error("--on-demand-episode must be >= 0")
    if args.on_demand_episode is not None and (args.workers != 1 or args.decode_threads != 1):
        parser.error("on-demand sampling requires --workers=1 and --decode-threads=1")
    args.parsed_episode_indices = (
        [args.on_demand_episode]
        if args.on_demand_episode is not None
        else parse_episode_indices(args.episode_indices)
    )
    capture = args.capture.expanduser().resolve()
    if not capture.is_dir():
        parser.error(f"capture directory does not exist: {capture}")
    output_root = require_capture_child(
        args.output_root or capture / "gripper_threshold_sampling", capture, "sampling output"
    )
    # Select segment IDs once so both arms always decode the same demonstrations.
    if args.parsed_episode_indices is None:
        _, complete_ranges = parse_capture_events(find_timestamp_log(capture), "left")
        args.parsed_episode_indices = evenly_select(
            [episode for episode, _, _ in complete_ranges], args.episode_count
        )
    if args.on_demand_episode is not None:
        on_demand_root = output_root / "on_demand"
        if args.plan_only:
            # On-demand stdout is an API: progress belongs on stderr and the
            # final stdout record is exactly one JSON object.
            with contextlib.redirect_stdout(sys.stderr):
                results = run_on_demand_sides(args, capture, on_demand_root)
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "mode": "on_demand_single_episode",
                        "plan_only": True,
                        "capture": str(capture),
                        "episode_index": args.on_demand_episode,
                        "sides": results,
                    },
                    separators=(",", ":"),
                )
            )
            return
        lock_root = on_demand_root / "locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"episode_{args.on_demand_episode:06d}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            # A second request for the same episode waits for the first one and
            # then takes the validated fingerprint fast path.  It never races
            # an immutable run publication.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with contextlib.redirect_stdout(sys.stderr):
                results = run_on_demand_sides(args, capture, on_demand_root)
                publication = publish_on_demand_results(
                    capture, output_root, args.on_demand_episode, results
                )
        print(json.dumps(publication, separators=(",", ":"), ensure_ascii=False))
        return
    if args.plan_only:
        results = [run_side(args, capture, output_root, side) for side in ("left", "right")]
        print(json.dumps({"schema": SCHEMA, "plan_only": True, "capture": str(capture), "sides": results}, indent=2))
        return
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".sampling.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another threshold sampler is running for {capture}") from exc
        results = [run_side(args, capture, output_root, side) for side in ("left", "right")]
        print(json.dumps(combine_results(capture, output_root, results), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
