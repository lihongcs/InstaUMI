#!/usr/bin/env python3
"""Crash-safe planning and publication for selected MediaSDK PNG frames.

The MediaSDK process writes into throw-away batch directories.  This helper
validates every PNG before atomically committing a complete batch into the
persistent ``*.partial`` directory.  Only a fully validated directory whose
ordered chapter identities still match the plan is renamed to the final path.

This module deliberately uses only the Python standard library so its frame
specification, plan, resume and publication tests do not require MediaSDK.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from bisect import bisect_right
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


PLAN_SCHEMA = "instaumi_mediasdk_png_sequence_plan_v1"
BATCH_SCHEMA = "instaumi_mediasdk_png_sequence_batch_v1"
ARTIFACT_SCHEMA = "instaumi_mediasdk_png_sequence_artifact_v1"
PLAN_NAME = ".instaumi_png_sequence_plan.json"
MANIFEST_NAME = ".instaumi_png_sequence_manifest.json"
PARTIAL_META_DIR = ".instaumi_partial"
CHECKPOINT_DIR = "checkpoints"
SDK_RUN_DIR = "sdk_runs"
DEFAULT_SAMPLE_SIZE = 256 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FRAME_PATTERN = re.compile(r"^frame_(\d{12})\.png$")
FRAME_SPEC_PATTERN = re.compile(r"^(\d+)(?:-(\d+)(?::(\d+))?)?$")


class ArtifactError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".partial.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sample_digest(path: Path, size: int, sample_size: int) -> str:
    offsets = sorted(
        {
            0,
            max(0, size // 2 - sample_size // 2),
            max(0, size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            block = handle.read(min(sample_size, max(0, size - offset)))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(block)
    return digest.hexdigest()


def sampled_file_identity(path: Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ArtifactError(f"missing source chapter: {resolved}")
    before = resolved.stat()
    digest = _sample_digest(resolved, before.st_size, sample_size)
    after = resolved.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ArtifactError(f"source changed while computing identity: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sample_size_bytes": int(sample_size),
        "sample_sha256": digest,
    }


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _run_json(command: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def probe_primary_frame_count(path: Path, ffprobe_bin: str = "ffprobe") -> int:
    data = _run_json(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_frames",
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise ArtifactError(f"no primary video stream in chapter: {path}")
    value = streams[0].get("nb_frames")
    if value in (None, "", "N/A"):
        counted = _run_json(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(path),
            ]
        )
        counted_streams = counted.get("streams") or []
        value = counted_streams[0].get("nb_read_frames") if counted_streams else None
    if value in (None, "", "N/A") or int(value) <= 0:
        raise ArtifactError(f"exact primary frame count unavailable for chapter: {path}")
    return int(value)


def _stable_chapter_record(
    path: Path,
    chapter_index: int,
    global_start_frame: int,
    *,
    ffprobe_bin: str,
    frame_count_probe: Callable[[Path, str], int],
) -> dict[str, Any]:
    before = sampled_file_identity(path)
    frame_count = int(frame_count_probe(path, ffprobe_bin))
    after = sampled_file_identity(path)
    if before != after:
        raise ArtifactError(f"source chapter changed while probing: {path}")
    return {
        "chapter_index": chapter_index,
        "global_start_frame": global_start_frame,
        "global_end_frame_exclusive": global_start_frame + frame_count,
        "frame_count": frame_count,
        "file": after,
    }


def _split_spec_text(text: str) -> list[str]:
    return [token for token in re.split(r"[,;\s]+", text.strip()) if token]


def parse_frame_specs(
    *,
    frames: Iterable[int | str] = (),
    specs: Iterable[str] = (),
    files: Iterable[Path] = (),
) -> list[int]:
    tokens: list[str] = [str(value) for value in frames]
    for spec in specs:
        tokens.extend(_split_spec_text(spec))
    for path in files:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            for line in handle:
                tokens.extend(_split_spec_text(line.split("#", 1)[0]))

    result: set[int] = set()
    for token in tokens:
        match = FRAME_SPEC_PATTERN.fullmatch(token)
        if match is None:
            raise ArtifactError(
                f"invalid frame specification {token!r}; use N or inclusive START-END[:STEP]"
            )
        first = int(match.group(1))
        last_text = match.group(2)
        step_text = match.group(3)
        if last_text is None:
            result.add(first)
            continue
        last = int(last_text)
        step = int(step_text or "1")
        if last < first:
            raise ArtifactError(f"descending frame range is not allowed: {token}")
        if step <= 0:
            raise ArtifactError(f"frame range step must be positive: {token}")
        result.update(range(first, last + 1, step))
    if not result:
        raise ArtifactError("at least one frame index or range is required")
    return sorted(result)


def _plan_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_plan(
    inputs: Sequence[Path],
    requested_frames: Sequence[int],
    *,
    width: int,
    height: int,
    stitch_type: str,
    flowstate: str,
    direction_lock: str,
    stitch_fusion: str,
    cuda: str,
    model_dir: str,
    ffprobe_bin: str = "ffprobe",
    frame_count_probe: Callable[[Path, str], int] = probe_primary_frame_count,
) -> dict[str, Any]:
    if not inputs:
        raise ArtifactError("at least one ordered source chapter is required")
    if width <= 0 or height <= 0 or width != height * 2:
        raise ArtifactError(f"PNG panorama size must be positive and 2:1, got {width}x{height}")
    if stitch_type not in {"template", "dynamic", "dynamicstitch", "optflow", "aiflow"}:
        raise ArtifactError(f"invalid stitch type: {stitch_type}")
    if direction_lock == "on" and flowstate != "on":
        raise ArtifactError("direction lock requires flowstate")
    for name, value in {
        "flowstate": flowstate,
        "direction_lock": direction_lock,
        "stitch_fusion": stitch_fusion,
        "cuda": cuda,
    }.items():
        if value not in {"on", "off"}:
            raise ArtifactError(f"{name} must be on or off, got {value!r}")

    chapters: list[dict[str, Any]] = []
    global_start = 0
    for chapter_index, raw_path in enumerate(inputs):
        record = _stable_chapter_record(
            Path(raw_path).expanduser().resolve(),
            chapter_index,
            global_start,
            ffprobe_bin=ffprobe_bin,
            frame_count_probe=frame_count_probe,
        )
        chapters.append(record)
        global_start = int(record["global_end_frame_exclusive"])

    normalized_frames = sorted(set(int(value) for value in requested_frames))
    if not normalized_frames:
        raise ArtifactError("the PNG sequence plan has no requested frames")
    if normalized_frames[0] < 0 or normalized_frames[-1] >= global_start:
        raise ArtifactError(
            f"requested frame range {normalized_frames[0]}..{normalized_frames[-1]} is outside "
            f"the ordered chapter timeline 0..{global_start - 1}"
        )

    payload: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "recipe": {
            "operation": "insta360_selected_panorama_png_export",
            "ordered_chapters": chapters,
            "export_parameters": {
                "format": "png",
                "lossless": True,
                "width": int(width),
                "height": int(height),
                "stitch_type": stitch_type,
                "flowstate": flowstate,
                "direction_lock": direction_lock,
                "stitch_fusion": stitch_fusion,
                "cuda": cuda,
                "model_dir": str(Path(model_dir).expanduser().resolve()) if model_dir else "",
            },
        },
        "requested_frame_indices": normalized_frames,
        "expectation": {
            "format": "png",
            "width": int(width),
            "height": int(height),
            "frame_count": len(normalized_frames),
            "source_timeline_frame_count": global_start,
        },
    }
    payload["plan_hash"] = _plan_digest(payload)
    return payload


def rebuild_plan(plan: dict[str, Any], *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    parameters = plan["recipe"]["export_parameters"]
    inputs = [Path(item["file"]["path"]) for item in plan["recipe"]["ordered_chapters"]]
    return build_plan(
        inputs,
        plan["requested_frame_indices"],
        width=int(parameters["width"]),
        height=int(parameters["height"]),
        stitch_type=str(parameters["stitch_type"]),
        flowstate=str(parameters["flowstate"]),
        direction_lock=str(parameters["direction_lock"]),
        stitch_fusion=str(parameters["stitch_fusion"]),
        cuda=str(parameters["cuda"]),
        model_dir=str(parameters.get("model_dir", "")),
        ffprobe_bin=ffprobe_bin,
    )


def load_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ArtifactError(f"cannot read PNG sequence plan {path}: {error}") from error
    if payload.get("schema") != PLAN_SCHEMA:
        raise ArtifactError(f"PNG sequence plan schema mismatch: {path}")
    stored_hash = payload.get("plan_hash")
    unhashed = dict(payload)
    unhashed.pop("plan_hash", None)
    if stored_hash != _plan_digest(unhashed):
        raise ArtifactError(f"PNG sequence plan hash mismatch: {path}")
    return payload


def canonical_frame_name(global_frame_index: int) -> str:
    return f"frame_{global_frame_index:012d}.png"


def frame_location(plan: dict[str, Any], global_frame_index: int) -> tuple[int, int]:
    chapters = plan["recipe"]["ordered_chapters"]
    starts = [int(item["global_start_frame"]) for item in chapters]
    chapter_index = bisect_right(starts, int(global_frame_index)) - 1
    if chapter_index < 0:
        raise ArtifactError(f"global frame precedes the first chapter: {global_frame_index}")
    chapter = chapters[chapter_index]
    if global_frame_index >= int(chapter["global_end_frame_exclusive"]):
        raise ArtifactError(f"global frame falls outside chapter timeline: {global_frame_index}")
    return chapter_index, int(global_frame_index) - int(chapter["global_start_frame"])


def _png_scanline_layout(width: int, height: int, bits_per_pixel: int, interlace: int) -> list[int]:
    if interlace == 0:
        return [((width * bits_per_pixel + 7) // 8)] * height
    if interlace != 1:
        raise ArtifactError(f"invalid PNG interlace method: {interlace}")
    rows: list[int] = []
    for x0, y0, dx, dy in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = 0 if width <= x0 else (width - x0 + dx - 1) // dx
        pass_height = 0 if height <= y0 else (height - y0 + dy - 1) // dy
        if pass_width:
            rows.extend([((pass_width * bits_per_pixel + 7) // 8)] * pass_height)
    return rows


def inspect_png(path: Path, *, verify_decoded_stream: bool = True) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ArtifactError(f"missing or empty PNG: {path}")
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ArtifactError(f"invalid PNG signature: {path}")
    offset = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int, int] | None = None
    compressed: list[bytes] = []
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ArtifactError(f"truncated PNG chunk header: {path}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ArtifactError(f"truncated PNG chunk {chunk_type!r}: {path}")
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        actual_crc = binascii.crc32(chunk_type)
        actual_crc = binascii.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ArtifactError(f"PNG CRC mismatch in {chunk_type!r}: {path}")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise ArtifactError(f"invalid PNG IHDR: {path}")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if not width or not height or compression != 0 or filter_method != 0:
                raise ArtifactError(f"invalid PNG IHDR values: {path}")
            ihdr = width, height, bit_depth, color_type, interlace
        elif chunk_type == b"IDAT":
            compressed.append(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0:
                raise ArtifactError(f"invalid PNG IEND: {path}")
            saw_iend = True
            offset = end
            break
        offset = end
    if ihdr is None or not compressed or not saw_iend or offset != len(data):
        raise ArtifactError(f"incomplete PNG structure: {path}")

    width, height, bit_depth, color_type, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ArtifactError(f"unsupported PNG color type {color_type}: {path}")
    if verify_decoded_stream:
        try:
            raw = zlib.decompress(b"".join(compressed))
        except zlib.error as error:
            raise ArtifactError(f"PNG IDAT cannot be decompressed: {path}: {error}") from error
        row_sizes = _png_scanline_layout(width, height, channels * bit_depth, interlace)
        expected_raw_size = sum(size + 1 for size in row_sizes)
        if len(raw) != expected_raw_size:
            raise ArtifactError(
                f"PNG decoded byte count mismatch for {path}: got {len(raw)}, expected {expected_raw_size}"
            )
        position = 0
        for row_size in row_sizes:
            if raw[position] > 4:
                raise ArtifactError(f"invalid PNG row filter {raw[position]}: {path}")
            position += row_size + 1
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "interlace": interlace,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _frame_identity(path: Path, png: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    return {
        "file_name": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": png["sha256"],
        "width": png["width"],
        "height": png["height"],
        "bit_depth": png["bit_depth"],
        "color_type": png["color_type"],
        "interlace": png["interlace"],
    }


def _partial_meta(partial_dir: Path) -> Path:
    return partial_dir / PARTIAL_META_DIR


def _checkpoint_dir(partial_dir: Path) -> Path:
    return _partial_meta(partial_dir) / CHECKPOINT_DIR


def sdk_run_root(partial_dir: Path) -> Path:
    return _partial_meta(partial_dir) / SDK_RUN_DIR


def _checkpoint_paths(partial_dir: Path) -> list[Path]:
    root = _checkpoint_dir(partial_dir)
    return sorted(root.glob("batch_*.json")) if root.is_dir() else []


def _remove_checkpoint_batch(partial_dir: Path, checkpoint: Path, payload: dict[str, Any]) -> None:
    for item in payload.get("frames", []):
        name = item.get("output", {}).get("file_name")
        if isinstance(name, str) and FRAME_PATTERN.fullmatch(name):
            (partial_dir / name).unlink(missing_ok=True)
    checkpoint.unlink(missing_ok=True)


def _load_committed_frames(
    partial_dir: Path,
    plan: dict[str, Any],
    *,
    repair_partial: bool,
) -> dict[int, dict[str, Any]]:
    committed: dict[int, dict[str, Any]] = {}
    expected_width = int(plan["expectation"]["width"])
    expected_height = int(plan["expectation"]["height"])
    requested = set(int(value) for value in plan["requested_frame_indices"])
    for checkpoint in _checkpoint_paths(partial_dir):
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            if payload.get("schema") != BATCH_SCHEMA or payload.get("plan_hash") != plan["plan_hash"]:
                raise ArtifactError("checkpoint schema or plan hash mismatch")
            batch_items: list[tuple[int, dict[str, Any]]] = []
            for item in payload.get("frames", []):
                global_index = int(item["global_frame_index"])
                chapter_index, local_index = frame_location(plan, global_index)
                if global_index not in requested:
                    raise ArtifactError(f"checkpoint contains unrequested frame {global_index}")
                if int(item["chapter_index"]) != chapter_index or int(item["chapter_local_frame_index"]) != local_index:
                    raise ArtifactError(f"checkpoint source mapping mismatch for frame {global_index}")
                if global_index in committed:
                    raise ArtifactError(f"duplicate committed frame {global_index}")
                output = item["output"]
                expected_name = canonical_frame_name(global_index)
                if output.get("file_name") != expected_name:
                    raise ArtifactError(f"checkpoint filename mismatch for frame {global_index}")
                path = partial_dir / expected_name
                png = inspect_png(path)
                stat = path.stat()
                for key, actual in {
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": png["sha256"],
                    "width": png["width"],
                    "height": png["height"],
                    "bit_depth": png["bit_depth"],
                    "color_type": png["color_type"],
                    "interlace": png["interlace"],
                }.items():
                    if output.get(key) != actual:
                        raise ArtifactError(f"checkpoint {key} mismatch for frame {global_index}")
                if png["width"] != expected_width or png["height"] != expected_height:
                    raise ArtifactError(f"PNG dimensions mismatch for frame {global_index}")
                batch_items.append((global_index, item))
            if not batch_items:
                raise ArtifactError("empty checkpoint")
            committed.update(batch_items)
        except Exception as error:
            if not repair_partial:
                raise ArtifactError(f"invalid PNG batch checkpoint {checkpoint}: {error}") from error
            try:
                payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            _remove_checkpoint_batch(partial_dir, checkpoint, payload)
            print(f"[resume] discarded invalid partial batch {checkpoint.name}: {error}", file=sys.stderr)

    # A power loss between frame rename and checkpoint publication can leave
    # canonical but unproven PNGs.  They are never adopted silently.
    committed_names = {item["output"]["file_name"] for item in committed.values()}
    for path in partial_dir.glob("frame_*.png"):
        if path.name not in committed_names:
            if not repair_partial:
                raise ArtifactError(f"unproven PNG exists outside a committed batch: {path}")
            path.unlink(missing_ok=True)
            print(f"[resume] removed unproven partial PNG {path.name}", file=sys.stderr)
    return committed


def prepare_partial(
    output_dir: Path,
    partial_dir: Path,
    plan: dict[str, Any],
    *,
    ffprobe_bin: str = "ffprobe",
) -> str:
    output_dir = output_dir.expanduser().resolve()
    partial_dir = partial_dir.expanduser().resolve()
    if output_dir.exists():
        ok, reason = check_final(output_dir, plan, ffprobe_bin=ffprobe_bin, verify_full_hash=False)
        if ok:
            print(f"[skip] complete selected PNG sequence: {output_dir}: {reason}", file=sys.stderr)
            return "COMPLETE"
        raise ArtifactError(f"final output exists but is not the requested complete artifact: {reason}")
    partial_dir.mkdir(parents=True, exist_ok=True)
    plan_path = partial_dir / PLAN_NAME
    if plan_path.exists():
        existing = load_plan(plan_path)
        if existing != plan:
            raise ArtifactError(
                f"partial directory belongs to a different source/frame/recipe plan: {partial_dir}"
            )
    else:
        unexpected = [item for item in partial_dir.iterdir() if item.name != PARTIAL_META_DIR]
        if unexpected:
            raise ArtifactError(f"partial directory has no plan but is not empty: {partial_dir}")
        atomic_write_json(plan_path, plan)
    _checkpoint_dir(partial_dir).mkdir(parents=True, exist_ok=True)
    # SDK run directories have no commit authority.  Completed batches have
    # already been moved and checkpointed, so stale runs are safe to discard.
    shutil.rmtree(sdk_run_root(partial_dir), ignore_errors=True)
    sdk_run_root(partial_dir).mkdir(parents=True, exist_ok=True)
    _load_committed_frames(partial_dir, plan, repair_partial=True)
    return str(plan_path)


def missing_batches(
    partial_dir: Path,
    plan: dict[str, Any],
    *,
    chunk_size: int,
) -> list[dict[str, Any]]:
    if chunk_size <= 0:
        raise ArtifactError("chunk size must be positive")
    committed = _load_committed_frames(partial_dir, plan, repair_partial=True)
    grouped: dict[int, list[tuple[int, int]]] = {}
    for global_index in plan["requested_frame_indices"]:
        global_index = int(global_index)
        if global_index in committed:
            continue
        chapter_index, local_index = frame_location(plan, global_index)
        grouped.setdefault(chapter_index, []).append((global_index, local_index))
    batches: list[dict[str, Any]] = []
    for chapter_index in sorted(grouped):
        values = grouped[chapter_index]
        for offset in range(0, len(values), chunk_size):
            chunk = values[offset : offset + chunk_size]
            batches.append(
                {
                    "chapter_index": chapter_index,
                    "global_frame_indices": [item[0] for item in chunk],
                    "chapter_local_frame_indices": [item[1] for item in chunk],
                }
            )
    return batches


def _sdk_png_index(path: Path) -> int | None:
    if path.stem.isdigit():
        return int(path.stem)
    matches = re.findall(r"\d+", path.stem)
    return int(matches[-1]) if matches else None


def _map_sdk_pngs(batch_dir: Path, local_indices: Sequence[int]) -> dict[int, Path]:
    files = sorted(
        [path for path in batch_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".png"],
        key=lambda path: str(path.relative_to(batch_dir)),
    )
    if len(files) != len(local_indices):
        raise ArtifactError(
            f"MediaSDK PNG count mismatch in {batch_dir}: got {len(files)}, expected {len(local_indices)}"
        )
    if len(files) == 1 and len(local_indices) == 1:
        return {int(local_indices[0]): files[0]}
    indexed: dict[int, Path] = {}
    for path in files:
        value = _sdk_png_index(path)
        if value is None or value in indexed:
            raise ArtifactError(
                "MediaSDK output filenames do not uniquely expose source frame indices; "
                f"cannot prove frame provenance for batch {batch_dir}"
            )
        indexed[value] = path
    expected = {int(value) for value in local_indices}
    if set(indexed) != expected:
        raise ArtifactError(
            "MediaSDK output filename indices do not match requested chapter-local frames: "
            f"got={sorted(indexed)} expected={sorted(expected)}"
        )
    return indexed


def ingest_batch(
    partial_dir: Path,
    plan: dict[str, Any],
    *,
    chapter_index: int,
    global_indices: Sequence[int],
    local_indices: Sequence[int],
    batch_dir: Path,
) -> Path:
    if len(global_indices) != len(local_indices) or not global_indices:
        raise ArtifactError("batch global/local frame lists must be non-empty and equal length")
    expected_pairs: list[tuple[int, int]] = []
    for global_index, local_index in zip(global_indices, local_indices):
        mapped_chapter, mapped_local = frame_location(plan, int(global_index))
        if mapped_chapter != int(chapter_index) or mapped_local != int(local_index):
            raise ArtifactError(
                f"batch source mapping mismatch: global={global_index} requested="
                f"chapter {chapter_index} local {local_index}, plan=chapter {mapped_chapter} local {mapped_local}"
            )
        expected_pairs.append((int(global_index), int(local_index)))
    if len(set(global_indices)) != len(global_indices) or len(set(local_indices)) != len(local_indices):
        raise ArtifactError("batch contains duplicate global or chapter-local frame indices")

    sdk_outputs = _map_sdk_pngs(batch_dir, local_indices)
    validated: list[tuple[int, int, Path, dict[str, Any]]] = []
    for global_index, local_index in expected_pairs:
        source = sdk_outputs[local_index]
        png = inspect_png(source)
        if png["width"] != int(plan["expectation"]["width"]) or png["height"] != int(
            plan["expectation"]["height"]
        ):
            raise ArtifactError(
                f"MediaSDK PNG dimensions mismatch for local frame {local_index}: "
                f"got {png['width']}x{png['height']}, expected "
                f"{plan['expectation']['width']}x{plan['expectation']['height']}"
            )
        validated.append((global_index, local_index, source, png))

    checkpoint_id = hashlib.sha256(
        (plan["plan_hash"] + ":" + ",".join(str(value) for value in global_indices)).encode("ascii")
    ).hexdigest()[:24]
    checkpoint = _checkpoint_dir(partial_dir) / f"batch_{checkpoint_id}.json"
    if checkpoint.exists():
        raise ArtifactError(f"batch checkpoint already exists unexpectedly: {checkpoint}")

    frame_records: list[dict[str, Any]] = []
    for global_index, local_index, source, png in validated:
        destination = partial_dir / canonical_frame_name(global_index)
        if destination.exists():
            raise ArtifactError(f"canonical partial frame already exists without this checkpoint: {destination}")
        _fsync_file(source)
        os.replace(source, destination)
        _fsync_directory(partial_dir)
        frame_records.append(
            {
                "global_frame_index": global_index,
                "chapter_index": int(chapter_index),
                "chapter_local_frame_index": local_index,
                "output": _frame_identity(destination, png),
            }
        )
    atomic_write_json(
        checkpoint,
        {
            "schema": BATCH_SCHEMA,
            "plan_hash": plan["plan_hash"],
            "chapter_index": int(chapter_index),
            "frames": frame_records,
        },
    )
    shutil.rmtree(batch_dir, ignore_errors=True)
    print(
        f"[commit] chapter={chapter_index} frames={len(frame_records)} "
        f"first_global={global_indices[0]} last_global={global_indices[-1]}",
        file=sys.stderr,
    )
    return checkpoint


def _artifact_frame_records(
    directory: Path,
    plan: dict[str, Any],
    records: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    requested = [int(value) for value in plan["requested_frame_indices"]]
    if set(records) != set(requested):
        missing = sorted(set(requested) - set(records))
        extra = sorted(set(records) - set(requested))
        raise ArtifactError(f"PNG sequence is incomplete: missing={missing[:20]} extra={extra[:20]}")
    expected_names = {canonical_frame_name(value) for value in requested}
    actual_names = {path.name for path in directory.glob("frame_*.png")}
    if actual_names != expected_names:
        raise ArtifactError(
            f"canonical PNG file set mismatch: missing={sorted(expected_names - actual_names)[:20]} "
            f"extra={sorted(actual_names - expected_names)[:20]}"
        )
    return [records[value] for value in requested]


def publish_partial(
    partial_dir: Path,
    output_dir: Path,
    plan: dict[str, Any],
    *,
    ffprobe_bin: str = "ffprobe",
) -> None:
    partial_dir = partial_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise ArtifactError(f"refusing to replace existing final directory: {output_dir}")
    committed = _load_committed_frames(partial_dir, plan, repair_partial=False)
    frames = _artifact_frame_records(partial_dir, plan, committed)
    current_plan = rebuild_plan(plan, ffprobe_bin=ffprobe_bin)
    if current_plan != plan:
        raise ArtifactError("ordered source chapters or export recipe changed while PNG export was running")
    atomic_write_json(
        partial_dir / MANIFEST_NAME,
        {
            "schema": ARTIFACT_SCHEMA,
            "plan_hash": plan["plan_hash"],
            "plan": plan,
            "frame_count": len(frames),
            "frames": frames,
            "publish_mode": "validated_atomic_directory_rename",
        },
    )
    shutil.rmtree(_partial_meta(partial_dir), ignore_errors=True)
    _fsync_directory(partial_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(partial_dir, output_dir)
    _fsync_directory(output_dir.parent)


def check_final(
    output_dir: Path,
    plan: dict[str, Any],
    *,
    ffprobe_bin: str = "ffprobe",
    verify_full_hash: bool,
) -> tuple[bool, str]:
    try:
        output_dir = output_dir.expanduser().resolve()
        manifest_path = output_dir / MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("schema") != ARTIFACT_SCHEMA:
            raise ArtifactError("artifact manifest schema mismatch")
        if payload.get("plan") != plan or payload.get("plan_hash") != plan["plan_hash"]:
            raise ArtifactError("artifact source/frame/recipe plan mismatch")
        current = rebuild_plan(plan, ffprobe_bin=ffprobe_bin)
        if current != plan:
            raise ArtifactError("ordered source chapter identity changed")
        frame_records = payload.get("frames")
        if not isinstance(frame_records, list) or len(frame_records) != len(plan["requested_frame_indices"]):
            raise ArtifactError("artifact frame manifest count mismatch")
        expected_names = {
            canonical_frame_name(int(value)) for value in plan["requested_frame_indices"]
        }
        actual_names = {path.name for path in output_dir.glob("frame_*.png")}
        if actual_names != expected_names:
            raise ArtifactError("artifact canonical PNG set mismatch")
        for global_index, item in zip(plan["requested_frame_indices"], frame_records):
            global_index = int(global_index)
            chapter_index, local_index = frame_location(plan, global_index)
            if (
                int(item.get("global_frame_index", -1)) != global_index
                or int(item.get("chapter_index", -1)) != chapter_index
                or int(item.get("chapter_local_frame_index", -1)) != local_index
            ):
                raise ArtifactError(f"artifact source mapping mismatch for frame {global_index}")
            output = item.get("output") or {}
            path = output_dir / canonical_frame_name(global_index)
            stat = path.stat()
            if output.get("file_name") != path.name:
                raise ArtifactError(f"artifact filename mismatch for frame {global_index}")
            if int(output.get("size_bytes", -1)) != stat.st_size or int(
                output.get("mtime_ns", -1)
            ) != stat.st_mtime_ns:
                raise ArtifactError(f"artifact PNG fast identity changed for frame {global_index}")
            png = inspect_png(path)
            if png["width"] != int(plan["expectation"]["width"]) or png["height"] != int(
                plan["expectation"]["height"]
            ):
                raise ArtifactError(f"artifact PNG dimensions changed for frame {global_index}")
            if verify_full_hash and output.get("sha256") != png["sha256"]:
                raise ArtifactError(f"artifact PNG sha256 changed for frame {global_index}")
        return True, f"frames={len(frame_records)} plan_hash={plan['plan_hash']}"
    except Exception as error:
        return False, str(error)


def _csv_ints(text: str) -> list[int]:
    try:
        values = [int(item) for item in text.split(",") if item != ""]
    except ValueError as error:
        raise ArtifactError(f"invalid comma-separated integer list: {text}") from error
    if not values:
        raise ArtifactError("empty comma-separated integer list")
    return values


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--partial-dir", type=Path)
    parser.add_argument("--frame", action="append", default=[])
    parser.add_argument("--frames", action="append", default=[])
    parser.add_argument("--frames-file", action="append", type=Path, default=[])
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--stitch-type", default="optflow")
    parser.add_argument("--flowstate", choices=["on", "off"], default="off")
    parser.add_argument("--direction-lock", choices=["on", "off"], default="off")
    parser.add_argument("--stitch-fusion", choices=["on", "off"], default="off")
    parser.add_argument("--cuda", choices=["on", "off"], default="on")
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--ffprobe-bin", default="ffprobe")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Build/verify a source-bound partial plan")
    _add_plan_arguments(prepare)

    missing = subparsers.add_parser("missing", help="Print resumable missing batches as TSV")
    missing.add_argument("--plan", type=Path, required=True)
    missing.add_argument("--partial-dir", type=Path, required=True)
    missing.add_argument("--chunk-size", type=int, default=256)

    ingest = subparsers.add_parser("ingest", help="Validate and atomically commit one SDK batch")
    ingest.add_argument("--plan", type=Path, required=True)
    ingest.add_argument("--partial-dir", type=Path, required=True)
    ingest.add_argument("--batch-dir", type=Path, required=True)
    ingest.add_argument("--chapter-index", type=int, required=True)
    ingest.add_argument("--global-frames", required=True)
    ingest.add_argument("--local-frames", required=True)

    publish = subparsers.add_parser("publish", help="Validate all frames and atomically publish")
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--partial-dir", type=Path, required=True)
    publish.add_argument("--output-dir", type=Path, required=True)
    publish.add_argument("--ffprobe-bin", default="ffprobe")

    check = subparsers.add_parser("check", help="Validate an already published directory")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--output-dir", type=Path, required=True)
    check.add_argument("--ffprobe-bin", default="ffprobe")
    check.add_argument("--verify-full-hash", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            requested = parse_frame_specs(frames=args.frame, specs=args.frames, files=args.frames_file)
            plan = build_plan(
                args.input,
                requested,
                width=args.width,
                height=args.height,
                stitch_type=args.stitch_type,
                flowstate=args.flowstate,
                direction_lock=args.direction_lock,
                stitch_fusion=args.stitch_fusion,
                cuda=args.cuda,
                model_dir=args.model_dir,
                ffprobe_bin=args.ffprobe_bin,
            )
            output_dir = args.output_dir.expanduser().resolve()
            partial_dir = (
                args.partial_dir.expanduser().resolve()
                if args.partial_dir is not None
                else output_dir.with_name(output_dir.name + ".partial")
            )
            print(
                prepare_partial(
                    output_dir,
                    partial_dir,
                    plan,
                    ffprobe_bin=args.ffprobe_bin,
                )
            )
            return 0
        if args.command == "missing":
            plan = load_plan(args.plan)
            for batch in missing_batches(args.partial_dir, plan, chunk_size=args.chunk_size):
                print(
                    "\t".join(
                        [
                            str(batch["chapter_index"]),
                            ",".join(str(value) for value in batch["global_frame_indices"]),
                            ",".join(str(value) for value in batch["chapter_local_frame_indices"]),
                        ]
                    )
                )
            return 0
        if args.command == "ingest":
            plan = load_plan(args.plan)
            ingest_batch(
                args.partial_dir,
                plan,
                chapter_index=args.chapter_index,
                global_indices=_csv_ints(args.global_frames),
                local_indices=_csv_ints(args.local_frames),
                batch_dir=args.batch_dir,
            )
            return 0
        if args.command == "publish":
            plan = load_plan(args.plan)
            publish_partial(
                args.partial_dir,
                args.output_dir,
                plan,
                ffprobe_bin=args.ffprobe_bin,
            )
            print(f"[done] validated PNG sequence published atomically: {args.output_dir}")
            return 0
        if args.command == "check":
            plan = load_plan(args.plan)
            ok, reason = check_final(
                args.output_dir,
                plan,
                ffprobe_bin=args.ffprobe_bin,
                verify_full_hash=args.verify_full_hash,
            )
            print(reason)
            return 0 if ok else 1
        raise AssertionError(args.command)
    except (ArtifactError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
