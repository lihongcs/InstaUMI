#!/usr/bin/env python3
"""标准化提取 Insta360 原始数据、三路 RGB 视角和 IMU。

输入目录允许包含一个或多个 .insv、一个 Studio 导出的 2:1 全景 .mp4、
以及一个 timestamp_log_*.jsonl。所有 RGB 输出使用全景视频的帧率和帧数
作为主时钟，保证 front_fisheye / left_view / right_view 的 frame_idx 严格对应。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np


DATAPROCESS_ROOT = Path(__file__).resolve().parent.parent
if str(DATAPROCESS_ROOT) not in sys.path:
    sys.path.insert(0, str(DATAPROCESS_ROOT))
from capture_schema import field, normalize_camera_role  # noqa: E402
from artifact_io import safe_preserve_file  # noqa: E402


VIDEO_ARTIFACT_SCHEMA = "instaumi_video_artifact_v2"
CLOCK_TIMING_ARTIFACT_SCHEMA = "instaumi_x5_clock_timing_chapter_v1"
CLOCK_TIMING_MANIFEST_SCHEMA = "instaumi_x5_clock_timing_manifest_v1"


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(str(x) for x in cmd), flush=True)
    subprocess.run(cmd, check=True)


def run_json(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(proc.stdout)


def ffmpeg_base(ffmpeg_bin: str | Path, filter_threads: int = 0) -> list[str]:
    command = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-y",
    ]
    if filter_threads > 0:
        # FFmpeg otherwise creates a filter pool from every logical CPU for
        # every concurrent process.  The wrapper calculates a host-wide
        # budget; applying it here prevents two arms/chapters oversubscribing
        # the machine without changing any filter or encoder parameters.
        command += [
            "-threads",
            str(filter_threads),
            "-filter_threads",
            str(filter_threads),
            "-filter_complex_threads",
            str(filter_threads),
        ]
    return command


def normalize_encoder_preset(video_encoder: str, preset: str) -> str:
    if video_encoder != "h264_nvenc":
        return preset
    # NVENC p1 is fastest and p7 is slowest/best. Map common x264 names.
    return {
        "ultrafast": "p1",
        "superfast": "p1",
        "veryfast": "p2",
        "faster": "p3",
        "fast": "p4",
        "medium": "p4",
        "slow": "p5",
        "slower": "p6",
        "veryslow": "p7",
    }.get(preset, preset)


def video_encoder_args(video_encoder: str, preset: str, quality: int, threads: int = 0) -> list[str]:
    if video_encoder == "h264_nvenc":
        if quality == 0:
            args = [
                "-c:v",
                "h264_nvenc",
                "-preset",
                normalize_encoder_preset(video_encoder, preset),
                "-tune",
                "lossless",
                "-rc",
                "constqp",
                "-qp",
                "0",
                "-pix_fmt",
                "yuv420p",
            ]
            if threads > 0:
                args += ["-threads", str(threads)]
            return args
        args = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            normalize_encoder_preset(video_encoder, preset),
            "-cq",
            str(quality),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
        if threads > 0:
            args += ["-threads", str(threads)]
        return args
    if video_encoder == "libx264rgb":
        args = [
            "-c:v",
            "libx264rgb",
            "-preset",
            preset,
            "-crf",
            str(quality),
            "-pix_fmt",
            "rgb24",
        ]
        if threads > 0:
            args += ["-threads", str(threads)]
        return args
    if video_encoder == "libx264":
        args = [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(quality),
            "-pix_fmt",
            "yuv420p",
        ]
        if threads > 0:
            args += ["-threads", str(threads)]
        return args
    raise ValueError(f"unsupported video encoder: {video_encoder}")


def model_image_filter(width: int, height: int) -> str:
    """Legacy model-video resize retained only for backwards compatibility.

    The source-to-224 reduction discards most spatial samples. Lanczos limits
    aliasing while the mild luma-only unsharp pass restores edge contrast
    without inventing color detail. This is *not* OpenPI's preprocessing:
    OpenPI uses PIL bilinear ``resize_with_pad``. New high-quality conversions
    therefore use ``--direct-image-mode`` and create final 224x224 PNG pixels
    directly in stage _05, without this intermediate video/filter.
    """
    return (
        f"scale={width}:{height}:flags=lanczos+accurate_rnd+full_chroma_int,"
        "unsharp=5:5:0.55:5:5:0.0"
    )


def concat_escape(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def write_concat_list(list_path: Path, parts: list[Path]) -> None:
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "".join(f"file '{concat_escape(part)}'\n" for part in parts),
        encoding="utf-8",
    )


def concat_videos(
    parts: list[Path],
    output_path: Path,
    ffmpeg_bin: str | Path,
    expected_frames: int | None = None,
    recipe: dict[str, Any] | None = None,
) -> None:
    if not parts:
        raise ValueError("cannot concatenate an empty video list")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = output_path.parent / ".segments" / output_path.stem / "concat.txt"
    tmp_path = partial_video_path(output_path)
    write_concat_list(concat_list, parts)
    tmp_path.unlink(missing_ok=True)
    if expected_frames is None:
        counts = [probe_frame_count(part) for part in parts]
        if any(count is None for count in counts):
            raise RuntimeError(f"cannot determine concat input frame counts: {parts}")
        expected_frames = sum(int(count) for count in counts if count is not None)
    source = probe_video(parts[0])
    expectation = source_video_expectation(source, expected_frames)
    artifact_recipe = recipe or {
        "operation": "concat_stream_copy",
        "inputs": [file_identity(part) for part in parts],
        "expected_frames": expected_frames,
        "stream_map": "0:v:0",
    }
    try:
        if len(parts) == 1:
            shutil.copy2(parts[0], tmp_path)
        else:
            run(
                ffmpeg_base(ffmpeg_bin)
                + [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    str(expected_frames),
                    "-an",
                    "-c:v",
                    "copy",
                    "-f",
                    "mp4",
                    str(tmp_path),
                ]
            )
        publish_video(tmp_path, output_path, expectation, artifact_recipe)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def probe_video(path: Path, stream: str = "v:0") -> dict[str, Any]:
    data = run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "stream=index,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,time_base,nb_frames,duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if not data.get("streams"):
        raise RuntimeError(f"未找到视频流: {path}")
    return data["streams"][0]


def probe_frame_count(path: Path) -> int | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        stream = probe_video(path)
    except Exception:
        return None
    nb_frames = stream.get("nb_frames")
    if nb_frames not in (None, "", "N/A"):
        try:
            return int(nb_frames)
        except ValueError:
            pass
    duration = stream.get("duration")
    rate = stream.get("r_frame_rate") or stream.get("avg_frame_rate")
    if duration and rate and rate != "0/0":
        try:
            return round(float(duration) * float(parse_fps(rate)))
        except Exception:
            return None
    return None


def frame_count_from_probe(stream: dict[str, Any], fps: Fraction | None = None) -> int:
    nb_frames = stream.get("nb_frames")
    if nb_frames not in (None, "", "N/A"):
        return int(nb_frames)
    rate = fps or parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate"))
    return round(float(stream["duration"]) * float(rate))


def exact_frame_count_from_probe(
    stream: dict[str, Any], label: str, path: Path | None = None, selector: str = "v:0"
) -> int:
    frame_count = stream.get("nb_frames")
    if frame_count in (None, "", "N/A"):
        if path is None:
            raise RuntimeError(
                f"exact nb_frames unavailable for {label}; refusing duration-based estimate"
            )
        counted = run_json(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                selector,
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(path),
            ]
        )
        streams = counted.get("streams") or []
        frame_count = streams[0].get("nb_read_frames") if streams else None
        if frame_count in (None, "", "N/A"):
            raise RuntimeError(
                f"exact frame count unavailable for {label}; refusing duration-based estimate"
            )
    try:
        value = int(frame_count)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid nb_frames for {label}: {frame_count!r}") from exc
    if value <= 0:
        raise RuntimeError(f"non-positive nb_frames for {label}: {value}")
    return value


def file_identity(path: Path, sample_size: int = 256 * 1024) -> dict[str, Any]:
    """Return a cheap content-bound identity for a potentially huge video.

    Size and mtime alone cannot distinguish a replaced input (or prove that a
    legacy output was produced by the requested lossless recipe).  Sampling the
    beginning, middle and end keeps resume checks inexpensive while binding the
    artifact sidecar to file content as well as its pathname and metadata.
    """

    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    offsets = sorted(
        {
            0,
            max(0, stat.st_size // 2 - sample_size // 2),
            max(0, stat.st_size - sample_size),
        }
    )
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            data = handle.read(min(sample_size, max(0, stat.st_size - offset)))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def artifact_sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".extract.json")


def partial_video_path(path: Path) -> Path:
    return path.with_name(path.stem + ".partial" + path.suffix)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def source_video_expectation(stream: dict[str, Any], expected_frames: int) -> dict[str, Any]:
    return {
        "codec_name": str(stream.get("codec_name") or ""),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_count": int(expected_frames),
        "frame_rate": str(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or ""),
    }


def encoded_video_expectation(
    video_encoder: str,
    width: int,
    height: int,
    expected_frames: int,
    frame_rate: str | None = None,
) -> dict[str, Any]:
    expectation = {
        "codec_name": "h264",
        "pix_fmt": "gbrp" if video_encoder == "libx264rgb" else "yuv420p",
        "width": int(width),
        "height": int(height),
        "frame_count": int(expected_frames),
    }
    if frame_rate is not None:
        expectation["frame_rate"] = str(frame_rate)
    return expectation


def validate_video(path: Path, expectation: dict[str, Any]) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return False, "missing_or_empty"
    try:
        stream = probe_video(path)
    except Exception:
        return False, "missing_or_unreadable"
    nb_frames = stream.get("nb_frames")
    if nb_frames in (None, "", "N/A"):
        return False, "exact_frame_count_unavailable"
    try:
        actual_frames = int(nb_frames)
    except (TypeError, ValueError):
        return False, "frame_count_unreadable"
    expected_frames = int(expectation["frame_count"])
    if actual_frames != expected_frames:
        suffix = ", needs_trim" if actual_frames > expected_frames else ""
        return False, f"frames={actual_frames}, expected={expected_frames}{suffix}"
    for key in ("codec_name", "pix_fmt", "width", "height"):
        expected = expectation.get(key)
        actual = stream.get(key)
        if key in {"width", "height"}:
            actual = int(actual or 0)
            expected = int(expected or 0)
        else:
            actual = str(actual or "")
            expected = str(expected or "")
        if actual != expected:
            return False, f"{key}={actual!r}, expected={expected!r}"
    if "frame_rate" in expectation:
        actual_rate = str(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "")
        try:
            rate_matches = parse_fps(actual_rate) == parse_fps(str(expectation["frame_rate"]))
        except Exception:
            rate_matches = False
        if not rate_matches:
            return False, f"frame_rate={actual_rate!r}, expected={expectation['frame_rate']!r}"
    return (
        True,
        f"codec={stream.get('codec_name')} pix_fmt={stream.get('pix_fmt')} "
        f"size={stream.get('width')}x{stream.get('height')} frames={actual_frames}",
    )


def sidecar_matches(path: Path, expectation: dict[str, Any], recipe: dict[str, Any]) -> tuple[bool, str]:
    sidecar = artifact_sidecar_path(path)
    if not sidecar.exists():
        # Media attributes cannot reveal the requested encoder quality or the
        # exact source file.  In particular, CRF 20 and lossless CRF 0 H.264 can
        # have identical codec/pixel-format/geometry/frame-count metadata.
        # Rebuild into a partial and atomically replace only after validation.
        return False, "legacy_no_provenance_sidecar"
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"sidecar_unreadable={exc}"
    if payload.get("schema") != VIDEO_ARTIFACT_SCHEMA:
        return False, "sidecar_schema_mismatch"
    if payload.get("expectation") != expectation:
        return False, "sidecar_output_parameters_changed"
    if payload.get("recipe") != recipe:
        return False, "sidecar_input_or_parameters_changed"
    try:
        if payload.get("output_identity") != file_identity(path):
            return False, "sidecar_output_identity_changed"
    except OSError as exc:
        return False, f"output_identity_unreadable={exc}"
    return True, "sidecar_current"


def existing_video_is_complete(
    path: Path,
    expected_frames: int,
    expectation: dict[str, Any] | None = None,
    recipe: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if expectation is None:
        actual_frames = probe_frame_count(path)
        if actual_frames is None:
            return False, "missing_or_unreadable"
        if actual_frames == expected_frames:
            return True, f"frames={actual_frames}"
        if actual_frames > expected_frames:
            return False, f"frames={actual_frames}, expected={expected_frames}, needs_trim"
        return False, f"frames={actual_frames}, expected={expected_frames}"
    ok, reason = validate_video(path, expectation)
    if not ok:
        return False, reason
    if recipe is not None:
        sidecar_ok, sidecar_reason = sidecar_matches(path, expectation, recipe)
        if not sidecar_ok:
            return False, sidecar_reason
        reason += f" {sidecar_reason}"
    return True, reason


def publish_video(
    partial_path: Path,
    output_path: Path,
    expectation: dict[str, Any],
    recipe: dict[str, Any],
) -> None:
    ok, reason = validate_video(partial_path, expectation)
    if not ok:
        raise RuntimeError(f"refusing to publish invalid video: {partial_path} ({reason})")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.replace(output_path)
    atomic_write_json(
        artifact_sidecar_path(output_path),
        {
            "schema": VIDEO_ARTIFACT_SCHEMA,
            "expectation": expectation,
            "recipe": recipe,
            "output_identity": file_identity(output_path),
        },
    )


def parse_fps(rate: str) -> Fraction:
    if not rate or rate == "0/0":
        raise ValueError(f"无效帧率: {rate!r}")
    return Fraction(rate)


def discover_inputs(
    input_dir: Path,
    *,
    panorama_required: bool = True,
) -> tuple[list[Path], Path | None, Path | None]:
    insv_paths = sorted(input_dir.glob("*.insv"))
    mp4_paths = sorted(input_dir.glob("*.mp4"))
    log_paths = sorted(input_dir.glob("timestamp_log_*.jsonl"))
    if not insv_paths:
        raise FileNotFoundError(f"{input_dir} 下没有 .insv")
    if panorama_required and not mp4_paths:
        raise FileNotFoundError(f"{input_dir} 下没有 Studio 导出的全景 .mp4")
    if len(mp4_paths) > 1:
        print(f"[警告] 发现多个 mp4，默认使用第一个: {mp4_paths[0]}")
    panorama = mp4_paths[0] if mp4_paths else None
    return insv_paths, panorama, log_paths[0] if log_paths else None


def episode_name_from_video(video_path: Path) -> str:
    return video_path.stem


def copy_source_file(path: Path, dst: Path, mode: str) -> str:
    return safe_preserve_file(path, dst, mode)


def select_master_frame_count(pano_frames: int, front_frames: int, allow_frame_trim: bool) -> int:
    if pano_frames <= 0 or front_frames <= 0:
        raise RuntimeError(
            f"video frame counts must be positive: panorama={pano_frames}, front_total={front_frames}"
        )
    if pano_frames == front_frames:
        return pano_frames
    if not allow_frame_trim:
        raise RuntimeError(
            "panorama/front exact frame count mismatch; refusing silent truncation: "
            f"panorama={pano_frames}, front_total={front_frames}. "
            "Re-export the panorama or explicitly pass --allow-frame-trim for legacy compatibility."
        )
    selected = min(pano_frames, front_frames)
    print(
        "[warning] explicit legacy frame trim enabled: "
        f"panorama={pano_frames}, front_total={front_frames}, master={selected}"
    )
    return selected


def copy_sources(
    insv_paths: list[Path],
    pano_mp4: Path | None,
    log_path: Path | None,
    output_dir: Path,
    copy_mode: str,
) -> dict[str, Any]:
    raw_dir = output_dir / "raw_sources"
    raw_dir.mkdir(parents=True, exist_ok=True)
    copied_insv = []
    for path in insv_paths:
        dst = raw_dir / path.name
        method = copy_source_file(path, dst, copy_mode)
        print(f"[source] {method}: {path} -> {dst}")
        copied_insv.append(str(dst))
    pano_dst = None
    if pano_mp4 is not None:
        pano_dst = raw_dir / pano_mp4.name
        method = copy_source_file(pano_mp4, pano_dst, copy_mode)
        print(f"[source] {method}: {pano_mp4} -> {pano_dst}")
    log_dst = None
    if log_path:
        log_dst_path = raw_dir / log_path.name
        method = copy_source_file(log_path, log_dst_path, copy_mode)
        print(f"[source] {method}: {log_path} -> {log_dst_path}")
        log_dst = str(log_dst_path)
    return {
        "insv": copied_insv,
        "panorama_mp4": str(pano_dst) if pano_dst is not None else None,
        "timestamp_log": log_dst,
    }


def segment_recipe_inputs(segments: list[tuple[Path, int]]) -> list[dict[str, Any]]:
    return [
        {"file": file_identity(path), "frame_limit": int(frames), "stream": "0:v:0"}
        for path, frames in segments
    ]


def front_copy_recipe(segments: list[tuple[Path, int]], expected_frames: int) -> dict[str, Any]:
    return {
        "operation": "ordered_front_fisheye_stream_copy",
        "inputs": segment_recipe_inputs(segments),
        "expected_frames": int(expected_frames),
        "stream_map": "0:v:0",
        "audio": "disabled",
        "video_codec": "copy",
    }


def resized_front_recipe(
    segments: list[tuple[Path, int]],
    expected_frames: int,
    width: int,
    height: int,
    preset: str,
    video_encoder: str,
    video_quality: int,
) -> dict[str, Any]:
    return {
        "operation": "ordered_front_fisheye_model_view",
        "inputs": segment_recipe_inputs(segments),
        "expected_frames": int(expected_frames),
        "filter": model_image_filter(width, height),
        "encoder": video_encoder,
        "preset": preset,
        "quality": int(video_quality),
        "output_size": [int(width), int(height)],
        "audio": "disabled",
    }


def side_view_recipe(
    pano_mp4: Path,
    yaw: int,
    frames: int,
    width: int,
    height: int,
    preset: str,
    video_encoder: str,
    video_quality: int,
) -> dict[str, Any]:
    return {
        "operation": "panorama_v360_side_view",
        "input": file_identity(pano_mp4),
        "stream_map": "0:v:0",
        "expected_frames": int(frames),
        "yaw": int(yaw),
        "filter": side_view_filter(yaw, width, height),
        "encoder": video_encoder,
        "preset": preset,
        "quality": int(video_quality),
        "output_size": [int(width), int(height)],
        "audio": "disabled",
    }


def extract_front_fisheye(insv_path: Path, output_path: Path, frames: int, ffmpeg_bin: str | Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = partial_video_path(output_path)
    partial_path.unlink(missing_ok=True)
    stream = probe_video(insv_path, "v:0")
    expectation = source_video_expectation(stream, frames)
    recipe = front_copy_recipe([(insv_path, frames)], frames)
    try:
        run(
            ffmpeg_base(ffmpeg_bin)
            + [
                "-i",
                str(insv_path),
                "-map",
                "0:v:0",
                "-frames:v",
                str(frames),
                "-an",
                "-c:v",
                "copy",
                str(partial_path),
            ]
        )
        publish_video(partial_path, output_path, expectation, recipe)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def extract_front_fisheye_resized(
    insv_path: Path,
    output_path: Path,
    frames: int,
    width: int,
    height: int,
    preset: str,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = partial_video_path(output_path)
    partial_path.unlink(missing_ok=True)
    source_rate = probe_video(insv_path, "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(video_encoder, width, height, frames, source_rate)
    recipe = resized_front_recipe(
        [(insv_path, frames)], frames, width, height, preset, video_encoder, video_quality
    )
    try:
        run(
            ffmpeg_base(ffmpeg_bin, ffmpeg_threads)
            + [
                "-i",
                str(insv_path),
                "-map",
                "0:v:0",
                "-frames:v",
                str(frames),
                "-vf",
                model_image_filter(width, height),
                "-an",
            ]
            + video_encoder_args(video_encoder, preset, video_quality, ffmpeg_threads)
            + [str(partial_path)]
        )
        publish_video(partial_path, output_path, expectation, recipe)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def maybe_extract_front_fisheye(
    insv_path: Path,
    output_path: Path,
    frames: int,
    skip_existing: bool,
    ffmpeg_bin: str | Path,
) -> None:
    expectation = source_video_expectation(probe_video(insv_path, "v:0"), frames)
    recipe = front_copy_recipe([(insv_path, frames)], frames)
    if skip_existing and output_path.exists():
        ok, reason = existing_video_is_complete(output_path, frames, expectation, recipe)
        if ok:
            print(f"[跳过] 已完整: {output_path} ({reason})")
            return
        print(f"[重建] 已存在但不完整: {output_path} ({reason})")
    extract_front_fisheye(insv_path, output_path, frames, ffmpeg_bin)


def maybe_extract_front_fisheye_resized(
    insv_path: Path,
    output_path: Path,
    frames: int,
    width: int,
    height: int,
    preset: str,
    skip_existing: bool,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    source_rate = probe_video(insv_path, "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(video_encoder, width, height, frames, source_rate)
    recipe = resized_front_recipe(
        [(insv_path, frames)], frames, width, height, preset, video_encoder, video_quality
    )
    if skip_existing and output_path.exists():
        ok, reason = existing_video_is_complete(output_path, frames, expectation, recipe)
        if ok:
            print(f"[跳过] 已完整: {output_path} ({reason})")
            return
        print(f"[重建] 已存在但不完整: {output_path} ({reason})")
    extract_front_fisheye_resized(
        insv_path, output_path, frames, width, height, preset, ffmpeg_bin,
        video_encoder, video_quality, ffmpeg_threads,
    )


def active_frame_segments(insv_paths: list[Path], segment_frames: list[int], total_frames: int) -> list[tuple[Path, int]]:
    remaining = total_frames
    segments: list[tuple[Path, int]] = []
    for insv_path, frames in zip(insv_paths, segment_frames):
        if remaining <= 0:
            break
        use_frames = min(frames, remaining)
        if use_frames > 0:
            segments.append((insv_path, use_frames))
        remaining -= use_frames
    return segments


def segment_output_path(output_path: Path, index: int, insv_path: Path) -> Path:
    return output_path.parent / ".segments" / output_path.stem / f"{index:03d}_{insv_path.stem}{output_path.suffix}"


def extract_front_fisheye_direct_concat(
    segments: list[tuple[Path, int]],
    output_path: Path,
    expected_frames: int,
    ffmpeg_bin: str | Path,
) -> None:
    """Remux ordered INSV chapters directly into the final MP4.

    This removes the old write/read cycle through full-size per-chapter MP4s.
    The concat demuxer only forwards packets and ``-c:v copy`` is mandatory;
    strict output validation happens before the final path is replaced.
    """
    if len(segments) < 2:
        raise ValueError("direct concat requires at least two source chapters")
    streams = [probe_video(path, "v:0") for path, _ in segments]
    compatibility_keys = ("codec_name", "pix_fmt", "width", "height", "time_base")
    first = streams[0]
    for path_and_frames, stream in zip(segments[1:], streams[1:]):
        path = path_and_frames[0]
        mismatches = [
            key for key in compatibility_keys if str(stream.get(key)) != str(first.get(key))
        ]
        if mismatches:
            raise RuntimeError(
                f"front chapter is not stream-copy compatible: {path}; changed={mismatches}"
            )

    concat_list = output_path.parent / ".segments" / output_path.stem / "source_concat.txt"
    write_concat_list(concat_list, [path for path, _ in segments])
    partial_path = partial_video_path(output_path)
    partial_path.unlink(missing_ok=True)
    expectation = source_video_expectation(first, expected_frames)
    recipe = front_copy_recipe(segments, expected_frames)
    try:
        command = (
            ffmpeg_base(ffmpeg_bin)
            + [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-map",
                "0:v:0",
                "-frames:v",
                str(expected_frames),
                "-an",
                "-c:v",
                "copy",
                "-f",
                "mp4",
                str(partial_path),
            ]
        )
        run(command)
        publish_video(partial_path, output_path, expectation, recipe)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def maybe_extract_front_fisheye_segments(
    segments: list[tuple[Path, int]],
    output_path: Path,
    expected_frames: int,
    skip_existing: bool,
    parallel_segments: bool,
    segment_workers: int,
    ffmpeg_bin: str | Path,
) -> None:
    expectation = source_video_expectation(probe_video(segments[0][0], "v:0"), expected_frames)
    recipe = front_copy_recipe(segments, expected_frames)
    if skip_existing and output_path.exists():
        ok, reason = existing_video_is_complete(
            output_path, expected_frames, expectation, recipe
        )
        if ok:
            print(f"[跳过] 已完整: {output_path} ({reason})")
            return
        print(f"[重建] 已存在但不完整: {output_path} ({reason})")
    if len(segments) == 1:
        insv_path, frames = segments[0]
        maybe_extract_front_fisheye(insv_path, output_path, frames, False, ffmpeg_bin)
        return

    try:
        extract_front_fisheye_direct_concat(segments, output_path, expected_frames, ffmpeg_bin)
        print(f"[完成] front_fisheye 直接按章节 stream-copy: {output_path}")
        return
    except Exception as exc:
        # Keep compatibility with unusual firmware/container variants.  The
        # fallback is the historical per-chapter stream-copy followed by a
        # stream-copy concat; neither path decodes or re-encodes video.
        print(f"[回退] INSV 直接 concat 不可用，改用分片 stream-copy: {exc}")

    parts = [segment_output_path(output_path, idx, insv_path) for idx, (insv_path, _) in enumerate(segments)]

    def run_one(idx: int, insv_path: Path, frames: int) -> None:
        maybe_extract_front_fisheye(insv_path, parts[idx], frames, skip_existing, ffmpeg_bin)

    if parallel_segments:
        workers = segment_workers or min(len(segments), 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(segments))) as pool:
            futures = [pool.submit(run_one, idx, insv_path, frames) for idx, (insv_path, frames) in enumerate(segments)]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    else:
        for idx, (insv_path, frames) in enumerate(segments):
            run_one(idx, insv_path, frames)

    concat_videos(parts, output_path, ffmpeg_bin, expected_frames, recipe)


def maybe_extract_front_fisheye_resized_segments(
    segments: list[tuple[Path, int]],
    output_path: Path,
    expected_frames: int,
    width: int,
    height: int,
    preset: str,
    skip_existing: bool,
    parallel_segments: bool,
    segment_workers: int,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    source_rate = probe_video(segments[0][0], "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(
        video_encoder, width, height, expected_frames, source_rate
    )
    recipe = resized_front_recipe(
        segments, expected_frames, width, height, preset, video_encoder, video_quality
    )
    if skip_existing and output_path.exists():
        ok, reason = existing_video_is_complete(
            output_path, expected_frames, expectation, recipe
        )
        if ok:
            print(f"[跳过] 已完整: {output_path} ({reason})")
            return
        print(f"[重建] 已存在但不完整: {output_path} ({reason})")
    if len(segments) == 1:
        insv_path, frames = segments[0]
        maybe_extract_front_fisheye_resized(
            insv_path,
            output_path,
            frames,
            width,
            height,
            preset,
            False,
            ffmpeg_bin,
            video_encoder,
            video_quality,
            ffmpeg_threads,
        )
        return

    parts = [segment_output_path(output_path, idx, insv_path) for idx, (insv_path, _) in enumerate(segments)]

    def run_one(idx: int, insv_path: Path, frames: int) -> None:
        maybe_extract_front_fisheye_resized(
            insv_path,
            parts[idx],
            frames,
            width,
            height,
            preset,
            skip_existing,
            ffmpeg_bin,
            video_encoder,
            video_quality,
            ffmpeg_threads,
        )

    if parallel_segments:
        logical_budget = min(os.cpu_count() or 1, 28)
        per_job_budget = max(1, 2 * max(ffmpeg_threads, 1))
        workers = segment_workers or max(1, min(len(segments), logical_budget // per_job_budget))
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(segments))) as pool:
            futures = [pool.submit(run_one, idx, insv_path, frames) for idx, (insv_path, frames) in enumerate(segments)]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    else:
        for idx, (insv_path, frames) in enumerate(segments):
            run_one(idx, insv_path, frames)

    concat_videos(parts, output_path, ffmpeg_bin, expected_frames, recipe)


def side_view_filter(yaw: int, width: int, height: int) -> str:
    return (
        f"v360=input=equirect:output=flat:yaw={yaw}:pitch=0:roll=0:"
        f"h_fov=90:v_fov=90,"
        f"{model_image_filter(width, height)}"
    )


def extract_side_view(
    pano_mp4: Path,
    output_path: Path,
    yaw: int,
    frames: int,
    width: int,
    height: int,
    preset: str,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = partial_video_path(output_path)
    partial_path.unlink(missing_ok=True)
    source_rate = probe_video(pano_mp4, "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(video_encoder, width, height, frames, source_rate)
    recipe = side_view_recipe(
        pano_mp4, yaw, frames, width, height, preset, video_encoder, video_quality
    )
    try:
        run(
            ffmpeg_base(ffmpeg_bin, ffmpeg_threads)
            + [
                "-i",
                str(pano_mp4),
                "-map",
                "0:v:0",
                "-frames:v",
                str(frames),
                "-vf",
                side_view_filter(yaw, width, height),
                "-an",
            ]
            + video_encoder_args(video_encoder, preset, video_quality, ffmpeg_threads)
            + [str(partial_path)]
        )
        publish_video(partial_path, output_path, expectation, recipe)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def extract_side_views_single_decode(
    pano_mp4: Path,
    left_output: Path,
    right_output: Path,
    frames: int,
    width: int,
    height: int,
    preset: str,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    left_output.parent.mkdir(parents=True, exist_ok=True)
    right_output.parent.mkdir(parents=True, exist_ok=True)
    graph = (
        "[0:v:0]split=2[left_in][right_in];"
        f"[left_in]{side_view_filter(-90, width, height)}[left_out];"
        f"[right_in]{side_view_filter(90, width, height)}[right_out]"
    )
    encoder_args = video_encoder_args(video_encoder, preset, video_quality, ffmpeg_threads)
    left_partial = partial_video_path(left_output)
    right_partial = partial_video_path(right_output)
    left_partial.unlink(missing_ok=True)
    right_partial.unlink(missing_ok=True)
    source_rate = probe_video(pano_mp4, "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(video_encoder, width, height, frames, source_rate)
    left_recipe = side_view_recipe(
        pano_mp4, -90, frames, width, height, preset, video_encoder, video_quality
    )
    right_recipe = side_view_recipe(
        pano_mp4, 90, frames, width, height, preset, video_encoder, video_quality
    )
    try:
        run(
            ffmpeg_base(ffmpeg_bin, ffmpeg_threads)
            + [
                "-i",
                str(pano_mp4),
                "-filter_complex",
                graph,
                "-map",
                "[left_out]",
                "-frames:v",
                str(frames),
                "-an",
            ]
            + encoder_args
            + [
                str(left_partial),
                "-map",
                "[right_out]",
                "-frames:v",
                str(frames),
                "-an",
            ]
            + encoder_args
            + [str(right_partial)]
        )
        # Validate both before replacing either existing final output.
        for partial in (left_partial, right_partial):
            ok, reason = validate_video(partial, expectation)
            if not ok:
                raise RuntimeError(f"refusing to publish invalid side view: {partial} ({reason})")
        publish_video(left_partial, left_output, expectation, left_recipe)
        publish_video(right_partial, right_output, expectation, right_recipe)
    except Exception:
        left_partial.unlink(missing_ok=True)
        right_partial.unlink(missing_ok=True)
        raise


def trim_video_to_frames(input_path: Path, output_path: Path, frames: int, ffmpeg_bin: str | Path = "ffmpeg") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    run(
        ffmpeg_base(ffmpeg_bin)
        + [
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "copy",
            str(output_path),
        ]
    )


def repair_existing_video_frame_count(path: Path, expected_frames: int, label: str) -> None:
    actual_frames = probe_frame_count(path)
    if actual_frames is None:
        raise RuntimeError(f"{label} is missing or unreadable: {path}")
    if actual_frames == expected_frames:
        print(f"[保留] 已严格对齐: {label}: {path} (frames={actual_frames})")
        return
    if actual_frames < expected_frames:
        raise RuntimeError(f"{label} is shorter than expected: {path} (frames={actual_frames}, expected={expected_frames})")

    tmp_path = path.with_name(path.stem + f".trim_{expected_frames}.tmp" + path.suffix)
    print(f"[修复] 裁剪多余帧: {label}: {actual_frames} -> {expected_frames}")
    source_stream = probe_video(path)
    source_identity = file_identity(path)
    trim_video_to_frames(path, tmp_path, expected_frames)
    expectation = source_video_expectation(source_stream, expected_frames)
    ok, reason = existing_video_is_complete(tmp_path, expected_frames, expectation)
    if not ok:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"failed to trim {label}: {tmp_path} ({reason})")
    publish_video(
        tmp_path,
        path,
        expectation,
        {
            "operation": "repair_frame_count_stream_copy",
            "input_before_repair": source_identity,
            "expected_frames": int(expected_frames),
            "stream_map": "0:v:0",
            "video_codec": "copy",
        },
    )


def maybe_extract_side_view(
    pano_mp4: Path,
    output_path: Path,
    yaw: int,
    frames: int,
    width: int,
    height: int,
    preset: str,
    skip_existing: bool,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    source_rate = probe_video(pano_mp4, "v:0").get("r_frame_rate")
    expectation = encoded_video_expectation(video_encoder, width, height, frames, source_rate)
    recipe = side_view_recipe(
        pano_mp4, yaw, frames, width, height, preset, video_encoder, video_quality
    )
    if skip_existing and output_path.exists():
        ok, reason = existing_video_is_complete(output_path, frames, expectation, recipe)
        if ok:
            print(f"[跳过] 已完整: {output_path} ({reason})")
            return
        print(f"[重建] 已存在但不完整: {output_path} ({reason})")
    extract_side_view(
        pano_mp4, output_path, yaw, frames, width, height, preset,
        ffmpeg_bin, video_encoder, video_quality, ffmpeg_threads,
    )


def maybe_extract_side_views_single_decode(
    pano_mp4: Path,
    left_output: Path,
    right_output: Path,
    frames: int,
    width: int,
    height: int,
    preset: str,
    skip_existing: bool,
    ffmpeg_bin: str | Path,
    video_encoder: str,
    video_quality: int,
    ffmpeg_threads: int = 0,
) -> None:
    source_rate = probe_video(pano_mp4, "v:0").get("r_frame_rate")

    def needs_build(path: Path, yaw: int) -> bool:
        expectation = encoded_video_expectation(
            video_encoder, width, height, frames, source_rate
        )
        recipe = side_view_recipe(
            pano_mp4, yaw, frames, width, height, preset, video_encoder, video_quality
        )
        if skip_existing and path.exists():
            ok, reason = existing_video_is_complete(path, frames, expectation, recipe)
            if ok:
                print(f"[跳过] 已完整: {path} ({reason})")
                return False
            print(f"[重建] 已存在但不完整: {path} ({reason})")
        return True

    build_left = needs_build(left_output, -90)
    build_right = needs_build(right_output, 90)
    if not build_left and not build_right:
        return
    if build_left and build_right:
        extract_side_views_single_decode(
            pano_mp4,
            left_output,
            right_output,
            frames,
            width,
            height,
            preset,
            ffmpeg_bin,
            video_encoder,
            video_quality,
            ffmpeg_threads,
        )
        return
    output = left_output if build_left else right_output
    yaw = -90 if build_left else 90
    extract_side_view(
        pano_mp4,
        output,
        yaw,
        frames,
        width,
        height,
        preset,
        ffmpeg_bin,
        video_encoder,
        video_quality,
        ffmpeg_threads,
    )


def require_existing_video(
    path: Path,
    expected_frames: int,
    label: str,
    repair_frame_counts: bool,
    expectation: dict[str, Any] | None = None,
    recipe: dict[str, Any] | None = None,
) -> None:
    if repair_frame_counts:
        repair_existing_video_frame_count(path, expected_frames, label)
        if expectation is not None:
            ok, reason = validate_video(path, expectation)
            if not ok:
                raise RuntimeError(f"{label} has unexpected media properties after repair: {path} ({reason})")
        return
    ok, reason = existing_video_is_complete(path, expected_frames, expectation, recipe)
    if not ok:
        raise RuntimeError(f"{label} is missing or incomplete for metadata-only mode: {path} ({reason})")
    print(f"[保留] 已完整: {label}: {path} ({reason})")


def load_telemetry(insv_path: Path) -> dict[str, Any]:
    from telemetry_parser import Parser

    parser = Parser(str(insv_path))
    imu = parser.normalized_imu()
    metadata = find_nested_metadata(parser.telemetry())
    return {
        "imu": imu,
        "camera": parser.camera,
        "model": parser.model,
        "metadata": metadata,
    }


def load_telemetry_segments(
    insv_paths: list[Path],
    workers: int,
) -> tuple[list[dict[str, Any]], list[Exception | None]]:
    """Parse independent X5 chapters concurrently while preserving file order."""

    if workers < 1:
        raise ValueError("telemetry workers must be >= 1")
    telemetry_segments: list[dict[str, Any]] = [{} for _ in insv_paths]
    telemetry_errors: list[Exception | None] = [None for _ in insv_paths]

    def store(index: int, path: Path) -> None:
        try:
            telemetry_segments[index] = load_telemetry(path)
        except Exception as exc:
            telemetry_errors[index] = exc

    effective_workers = min(workers, len(insv_paths))
    if effective_workers <= 1:
        for index, path in enumerate(insv_paths):
            store(index, path)
    else:
        print(
            f"[parallel] parsing {len(insv_paths)} telemetry chapter(s) "
            f"with workers={effective_workers}",
            flush=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = [
                pool.submit(store, index, path)
                for index, path in enumerate(insv_paths)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    return telemetry_segments, telemetry_errors


def find_nested_metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        metadata = value.get("Metadata")
        if isinstance(metadata, dict):
            return metadata
        for child in value.values():
            found = find_nested_metadata(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_nested_metadata(child)
            if found is not None:
                return found
    return None


def generate_sync_json(
    insv_path: Path,
    output_dir: Path,
    telemetry: dict[str, Any] | None = None,
    telemetry_error: Exception | None = None,
) -> dict:
    sync_json_path = output_dir / "raw_sources" / f"{insv_path.stem}.sync.json"
    sync_json_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if telemetry is None:
            if telemetry_error is not None:
                raise telemetry_error
            telemetry = load_telemetry(insv_path)
        imu = telemetry["imu"]
        metadata = telemetry.get("metadata") or {}
        first_frame_raw_us = metadata.get("first_frame_timestamp")
        first_frame_ms = (
            float(first_frame_raw_us) / 1000.0
            if first_frame_raw_us is not None
            else (imu[0]["timestamp_ms"] if imu else 0.0)
        )
        camera_type = telemetry["camera"]
    except Exception as exc:
        print(f"Warning: failed to extract telemetry for sync.json: {exc}")
        first_frame_ms = 0.0
        camera_type = "Insta360"

    try:
        from datetime import datetime
        probe_full = run_json([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(insv_path)
        ])
        fmt = probe_full.get("format", {})
        creation_time_str = fmt.get("tags", {}).get("creation_time")
        create_unix_ms = None
        if creation_time_str:
            dt = datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
            create_unix_ms = int(dt.timestamp() * 1000)

        video_stream = next((s for s in probe_full.get("streams", []) if s.get("codec_type") == "video"), None)
        width = int(video_stream.get("width", 0)) if video_stream else 0
        height = int(video_stream.get("height", 0)) if video_stream else 0
        duration = float(fmt.get("duration", 0)) * 1000
    except Exception as e:
        create_unix_ms = None
        width = 0
        height = 0
        duration = 0.0
        print(f"Warning: failed to extract ffprobe metadata for sync.json: {e}")

    sync_data = {
        "mediaPath": str(insv_path),
        "mediaKind": "video",
        "cameraType": camera_type,
        "serialNumber": None,
        "firmwareVersion": None,
        "createUnixTimeMs": create_unix_ms,
        "firstFrameTimestampMs": first_frame_ms,
        "firstGpsTimestampMs": None,
        "gyroTimestampAdjustMs": 0.0,
        "rollingShutterTimeMs": None,
        "fileTotalTimeMs": duration,
        "fileSize": insv_path.stat().st_size if insv_path.exists() else 0,
        "width": width,
        "height": height
    }

    atomic_write_json(sync_json_path, sync_data)

    return sync_data

def camera_dispatch_unix_ms(total_start: dict[str, Any], camera_role: str | None) -> float | None:
    adapter_start = total_start.get("extra", {}).get("adapter_start", {})
    insta = adapter_start.get("extra", {}).get("insta", {}).get("extra", {})
    results = insta.get("camera_results") or []
    selected_role = normalize_camera_role(camera_role)
    for result in results:
        role = normalize_camera_role(result.get("role"), result.get("address"))
        if selected_role is not None and role != selected_role:
            continue
        value = result.get("dispatch_unix_ns")
        if value is not None:
            return float(value) / 1e6
    return None


def write_frame_timestamps(
    path: Path,
    fps: Fraction,
    frames: int,
    log_path: Path | None,
    sync_data: dict | None,
    camera_role: str | None,
    explicit_frame0_unix_sec: float | None,
) -> dict[str, Any]:
    """Write the video clock and return an explicit alignment-quality record.

    Linux total_start/BLE dispatch is a stable host-clock anchor, but it is not
    the camera's proven first-frame timestamp. Motion calibration may replace
    it later through --video-frame0-host-unix-time-sec.
    """
    unix_start_ms = None
    source = "unavailable"
    verified = False
    total_start: dict[str, Any] | None = None

    if log_path and log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "total_start":
                total_start = row
                break

    if explicit_frame0_unix_sec is not None:
        unix_start_ms = float(explicit_frame0_unix_sec) * 1000.0
        source = "motion_calibrated_frame0"
        verified = True
    elif total_start is not None:
        unix_start_ms = camera_dispatch_unix_ms(total_start, camera_role)
        if unix_start_ms is not None:
            source = "linux_ble_start_dispatch_estimate"
        else:
            value = field(total_start, "unix_time_ms", "unixTimeMs")
            if value is not None:
                unix_start_ms = float(value)
                source = "linux_total_start_estimate"
    elif sync_data and sync_data.get("createUnixTimeMs") is not None:
        # Retained only for historical inputs without a Linux capture log. Do
        # not add firstFrameTimestampMs: telemetry timestamps may be relative
        # or camera-clock values and are not guaranteed to share the UTC epoch.
        unix_start_ms = float(sync_data["createUnixTimeMs"])
        source = "media_creation_time_estimate"

    monotonic_start = None
    if total_start is not None and unix_start_ms is not None:
        total_unix_ms = field(total_start, "unix_time_ms", "unixTimeMs")
        total_monotonic = field(total_start, "monotonic_time", "monotonicTime")
        if total_unix_ms is not None and total_monotonic is not None:
            host_clock_offset_ms = float(total_unix_ms) - float(total_monotonic) * 1000.0
            monotonic_start = (unix_start_ms - host_clock_offset_ms) / 1000.0

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    fps_float = float(fps)
    with temporary.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["frame_idx", "timestamp_sec", "fps", "unix_time_ms", "monotonic_time"],
        )
        writer.writeheader()
        for idx in range(frames):
            t = idx / fps_float
            writer.writerow(
                {
                    "frame_idx": idx,
                    "timestamp_sec": f"{t:.9f}",
                    "fps": f"{fps_float:.9f}",
                    "unix_time_ms": "" if unix_start_ms is None else f"{float(unix_start_ms) + t * 1000.0:.3f}",
                    "monotonic_time": "" if monotonic_start is None else f"{float(monotonic_start) + t:.9f}",
                }
            )
    temporary.replace(path)
    return {
        "frame0_unix_ms": unix_start_ms,
        "frame0_monotonic_time": monotonic_start,
        "source": source,
        "camera_role": normalize_camera_role(camera_role),
        "frame_alignment_verified": verified,
        "sub_10ms_verified": False,
        "note": (
            "Motion-calibrated host time was supplied. Validate residual correlation before claiming sub-10ms."
            if verified
            else "BLE/total_start anchors are command-time estimates, not measured camera first-frame timestamps."
        ),
    }

def detect_gyro_scale(imu: list[dict[str, Any]]) -> tuple[str, float]:
    norms = []
    for sample in imu:
        gx, gy, gz = [float(v) for v in sample["gyro"]]
        norms.append(math.sqrt(gx * gx + gy * gy + gz * gz))
    if not norms:
        return "unknown", 1.0
    norms.sort()
    p95 = norms[min(len(norms) - 1, int(0.95 * (len(norms) - 1)))]
    return ("deg/s", math.pi / 180.0) if p95 > 20.0 else ("rad/s", 1.0)


def extract_imu(insv_path: Path, output_dir: Path, telemetry: dict[str, Any] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if telemetry is None:
        try:
            telemetry = load_telemetry(insv_path)
        except ImportError as exc:
            raise RuntimeError("缺少 telemetry_parser，无法从 INSV 提取 IMU") from exc

    imu = telemetry["imu"]
    gyro_unit, gyro_scale = detect_gyro_scale(imu)

    raw_json = output_dir / "imu_telemetry_raw.json"
    atomic_write_json(
        raw_json,
        {
            "source": str(insv_path),
            "camera": telemetry.get("camera"),
            "model": telemetry.get("model"),
            "samples": imu,
        },
    )

    csv_path = output_dir / "imu_data.csv"
    csv_temporary = csv_path.with_name(csv_path.name + ".partial")
    with csv_temporary.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp_sec", "timestamp_ms", "gyro_x", "gyro_y", "gyro_z", "accel_x", "accel_y", "accel_z"],
        )
        writer.writeheader()
        for sample in imu:
            gx, gy, gz = sample["gyro"]
            ax, ay, az = sample["accl"]
            t_ms = float(sample["timestamp_ms"])
            writer.writerow(
                {
                    "timestamp_sec": f"{t_ms / 1000.0:.9f}",
                    "timestamp_ms": f"{t_ms:.6f}",
                    "gyro_x": f"{float(gx) * gyro_scale:.9f}",
                    "gyro_y": f"{float(gy) * gyro_scale:.9f}",
                    "gyro_z": f"{float(gz) * gyro_scale:.9f}",
                    "accel_x": f"{float(ax):.9f}",
                    "accel_y": f"{float(ay):.9f}",
                    "accel_z": f"{float(az):.9f}",
                }
            )
    csv_temporary.replace(csv_path)

    return {
        "csv": str(csv_path),
        "raw_json": str(raw_json),
        "sample_count": len(imu),
        "gyro_unit_detected": gyro_unit,
        "gyro_scale_applied": gyro_scale,
    }


IMU_COLUMNS = [
    "timestamp_sec",
    "timestamp_ms",
    "gyro_x",
    "gyro_y",
    "gyro_z",
    "accel_x",
    "accel_y",
    "accel_z",
]


def extract_imu_segments(
    insv_paths: list[Path],
    output_dir: Path,
    telemetry_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Preserve every chapter and expose one native-clock merged IMU stream.

    Chapter-local normalized samples are written unchanged under ``segments``.
    The compatibility ``imu_data.csv`` contains every sample and applies only
    the X5 native ``first_frame_timestamp`` chapter offset, exactly matching the
    time basis used by ``calibrate_no_slam_clock.py``.  A separate mapping CSV
    makes every merged row reversible to its source chapter/sample.
    """
    if len(insv_paths) != len(telemetry_segments):
        raise ValueError("INSV/telemetry chapter count mismatch")
    if not insv_paths:
        raise ValueError("no INSV chapters for IMU extraction")
    if len(insv_paths) == 1:
        info = extract_imu(insv_paths[0], output_dir, telemetry_segments[0])
        info.update(
            {
                "chapter_count": 1,
                "merge_clock": "single_chapter_local",
                "chapters": [
                    {
                        "segment_index": 0,
                        "source": file_identity(insv_paths[0]),
                        "sample_count": info["sample_count"],
                        "csv": info["csv"],
                        "raw_json": info["raw_json"],
                    }
                ],
            }
        )
        atomic_write_json(output_dir / "imu_chapters.json", info)
        return info

    output_dir.mkdir(parents=True, exist_ok=True)
    segment_root = output_dir / "segments"
    segment_root.mkdir(parents=True, exist_ok=True)
    raw_first_values: list[int] = []
    for path, telemetry in zip(insv_paths, telemetry_segments):
        metadata = telemetry.get("metadata") or {}
        raw_value = metadata.get("first_frame_timestamp")
        if raw_value is None:
            raise RuntimeError(
                f"missing X5 Metadata.first_frame_timestamp; cannot merge IMU chapters exactly: {path}"
            )
        raw_first_values.append(int(raw_value))

    reference_raw = raw_first_values[0]
    merged_csv = output_dir / "imu_data.csv"
    mapping_csv = output_dir / "imu_sample_index.csv"
    merged_tmp = merged_csv.with_name(merged_csv.name + ".partial")
    mapping_tmp = mapping_csv.with_name(mapping_csv.name + ".partial")
    chapter_records: list[dict[str, Any]] = []
    merged_sample_index = 0
    try:
        with merged_tmp.open("w", newline="", encoding="utf-8") as merged_file, mapping_tmp.open(
            "w", newline="", encoding="utf-8"
        ) as mapping_file:
            merged_writer = csv.DictWriter(merged_file, fieldnames=IMU_COLUMNS)
            merged_writer.writeheader()
            mapping_writer = csv.DictWriter(
                mapping_file,
                fieldnames=[
                    "merged_sample_idx",
                    "segment_index",
                    "segment_sample_idx",
                    "source_insv",
                    "segment_timestamp_ms",
                    "camera_native_timestamp_sec",
                    "first_frame_timestamp_raw_us",
                ],
            )
            mapping_writer.writeheader()

            for index, (path, telemetry, first_raw) in enumerate(
                zip(insv_paths, telemetry_segments, raw_first_values)
            ):
                chapter_dir = segment_root / f"{index:03d}_{path.stem}"
                chapter_info = extract_imu(path, chapter_dir, telemetry)
                native_offset_sec = (first_raw - reference_raw) * 1e-6
                sample_start = merged_sample_index
                with Path(chapter_info["csv"]).open(newline="", encoding="utf-8") as source_file:
                    for segment_sample_index, row in enumerate(csv.DictReader(source_file)):
                        local_ms = float(row["timestamp_ms"])
                        native_sec = native_offset_sec + local_ms / 1000.0
                        merged_row = dict(row)
                        merged_row["timestamp_sec"] = f"{native_sec:.9f}"
                        merged_row["timestamp_ms"] = f"{native_sec * 1000.0:.6f}"
                        merged_writer.writerow({key: merged_row[key] for key in IMU_COLUMNS})
                        mapping_writer.writerow(
                            {
                                "merged_sample_idx": merged_sample_index,
                                "segment_index": index,
                                "segment_sample_idx": segment_sample_index,
                                "source_insv": str(path.resolve()),
                                "segment_timestamp_ms": f"{local_ms:.6f}",
                                "camera_native_timestamp_sec": f"{native_sec:.9f}",
                                "first_frame_timestamp_raw_us": first_raw,
                            }
                        )
                        merged_sample_index += 1
                chapter_records.append(
                    {
                        "segment_index": index,
                        "source": file_identity(path),
                        "first_frame_timestamp_raw_us": first_raw,
                        "native_offset_from_first_sec": native_offset_sec,
                        "merged_sample_start": sample_start,
                        "merged_sample_end_exclusive": merged_sample_index,
                        **chapter_info,
                    }
                )
        merged_tmp.replace(merged_csv)
        mapping_tmp.replace(mapping_csv)
    except Exception:
        merged_tmp.unlink(missing_ok=True)
        mapping_tmp.unlink(missing_ok=True)
        raise

    raw_index = output_dir / "imu_telemetry_raw.json"
    raw_index_payload = {
        "schema": "instaumi_multichapter_imu_index_v1",
        "storage": "lossless_normalized_samples_per_segment",
        "note": "Sample arrays are stored in each chapter raw_json; this file does not duplicate them.",
        "chapters": chapter_records,
    }
    atomic_write_json(raw_index, raw_index_payload)
    result = {
        "csv": str(merged_csv),
        "raw_json": str(raw_index),
        "sample_index_csv": str(mapping_csv),
        "chapter_index_json": str(output_dir / "imu_chapters.json"),
        "sample_count": merged_sample_index,
        "chapter_count": len(chapter_records),
        "merge_clock": "x5_first_frame_timestamp_us_plus_segment_local_imu",
        "first_frame_timestamp_reference_raw_us": reference_raw,
        "chapters": chapter_records,
    }
    atomic_write_json(output_dir / "imu_chapters.json", result)
    return result


def write_clock_timing_artifacts(
    insv_paths: list[Path],
    output_dir: Path,
    telemetry_segments: list[dict[str, Any]],
    ffprobe_bin: str,
    workers: int = 1,
) -> dict[str, Any]:
    """Publish the exact inputs needed by no-SLAM clock calibration.

    ``telemetry_segments`` is the same in-memory parse already used to write
    the regular _01 IMU artifacts.  Persisting the gyro norm and exact packet
    PTS here lets the calibration stage consume a small, provenance-bound NPZ
    instead of opening each multi-gigabyte INSV a second time.
    """

    if len(insv_paths) != len(telemetry_segments):
        raise ValueError("INSV/telemetry chapter count mismatch")
    if workers < 1:
        raise ValueError("timing artifact workers must be >= 1")
    timing_root = output_dir / "timing"
    segment_root = timing_root / "segments"
    segment_root.mkdir(parents=True, exist_ok=True)

    def build_one(
        segment_index: int,
        source: Path,
        telemetry: dict[str, Any],
    ) -> dict[str, Any]:
        imu = telemetry.get("imu") or []
        metadata = telemetry.get("metadata") or {}
        first_frame_timestamp_raw_us = metadata.get("first_frame_timestamp")
        if first_frame_timestamp_raw_us is None:
            raise RuntimeError(
                "missing X5 Metadata.first_frame_timestamp for clock timing artifact: "
                f"{source}"
            )
        if not imu:
            raise RuntimeError(f"no normalized IMU samples for clock timing artifact: {source}")

        # Import locally to keep the regular extractor lightweight when clock
        # artifacts are not requested.  This is the single implementation of
        # exact packet-PTS validation used by both stages.
        from calibrate_no_slam_clock import ffprobe_stream

        video = ffprobe_stream(source, ffprobe_bin, pts_cache_dir=None)
        frame_pts_sec = np.asarray(video.pop("frame_pts_sec"), dtype=np.float64)
        imu_time_sec = np.fromiter(
            (float(sample["timestamp_ms"]) / 1000.0 for sample in imu),
            dtype=np.float64,
            count=len(imu),
        )
        gyro_unit, gyro_scale = detect_gyro_scale(imu)
        gyro_norm_rad_s = np.fromiter(
            (
                math.sqrt(
                    sum((float(component) * gyro_scale) ** 2 for component in sample["gyro"])
                )
                for sample in imu
            ),
            dtype=np.float64,
            count=len(imu),
        )
        if not (
            np.all(np.isfinite(frame_pts_sec))
            and np.all(np.isfinite(imu_time_sec))
            and np.all(np.isfinite(gyro_norm_rad_s))
        ):
            raise RuntimeError(f"non-finite timing samples while processing {source}")

        source_identity = file_identity(source)
        artifact_path = (
            segment_root / f"{segment_index:03d}_{source.stem}.clock_timing.npz"
        )
        metadata_payload = {
            "schema": CLOCK_TIMING_ARTIFACT_SCHEMA,
            "source_segment": segment_index,
            "source": source_identity,
            "first_frame_timestamp_raw_us": int(first_frame_timestamp_raw_us),
            "camera": telemetry.get("camera"),
            "model": telemetry.get("model"),
            "camera_serial": metadata.get("serial_number"),
            "firmware": metadata.get("fw_version"),
            "rolling_shutter_ms": metadata.get("rolling_shutter_time"),
            "gyro_input_unit": gyro_unit,
            "gyro_scale_applied": gyro_scale,
            "imu_sample_count": int(imu_time_sec.size),
            "video": video,
        }
        temporary = artifact_path.with_name(artifact_path.name + ".partial")
        try:
            with temporary.open("wb") as handle:
                np.savez(
                    handle,
                    frame_pts_sec=frame_pts_sec,
                    imu_time_sec=imu_time_sec,
                    gyro_norm_rad_s=gyro_norm_rad_s,
                    metadata_json=np.asarray(
                        json.dumps(metadata_payload, sort_keys=True, allow_nan=False)
                    ),
                )
            temporary.replace(artifact_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        record = {
            "source_segment": segment_index,
            "source": source_identity,
            "path": str(artifact_path),
            "file_identity": file_identity(artifact_path),
            "frame_count": int(frame_pts_sec.size),
            "imu_sample_count": int(imu_time_sec.size),
        }
        print(
            f"[timing] published chapter={segment_index} frames={frame_pts_sec.size} "
            f"imu={imu_time_sec.size} path={artifact_path}",
            flush=True,
        )
        return record

    effective_workers = min(workers, len(insv_paths))
    if effective_workers <= 1:
        artifacts = [
            build_one(index, source, telemetry_segments[index])
            for index, source in enumerate(insv_paths)
        ]
    else:
        print(
            f"[parallel] publishing {len(insv_paths)} clock timing artifact(s) "
            f"with workers={effective_workers}",
            flush=True,
        )
        ordered: list[dict[str, Any] | None] = [None for _ in insv_paths]
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = {
                pool.submit(build_one, index, source, telemetry_segments[index]): index
                for index, source in enumerate(insv_paths)
            }
            for future in concurrent.futures.as_completed(futures):
                ordered[futures[future]] = future.result()
        if any(item is None for item in ordered):
            raise RuntimeError("one or more clock timing artifact workers produced no result")
        artifacts = [item for item in ordered if item is not None]

    result = {
        "schema": CLOCK_TIMING_MANIFEST_SCHEMA,
        "source": "single_telemetry_parse_plus_exact_packet_pts_in_stage_01",
        "artifacts": artifacts,
    }
    atomic_write_json(timing_root / "manifest.json", result)
    result["manifest_path"] = str(timing_root / "manifest.json")
    return result


def recorded_identity_matches(identity: Any, path: Path) -> bool:
    if not isinstance(identity, dict) or not path.is_file():
        return False
    current = file_identity(path)
    return all(
        identity.get(key) == current.get(key)
        for key in ("path", "size_bytes", "mtime_ns", "sample_sha256")
    )


def reusable_direct_raw_manifest(
    manifest_path: Path,
    insv_paths: list[Path],
    expected_frames: int,
    materialize_front_video: bool,
) -> dict[str, Any] | None:
    """Return a complete current direct-mode manifest, otherwise ``None``.

    This makes a normal retry skip telemetry, IMU CSV/JSON and packet-PTS
    extraction together.  Validation is source- and artifact-bound; a partial
    or legacy _01 result is rebuilt instead of being trusted by filename.
    """

    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != "instaumi_raw_extract_v1"
            or manifest.get("image_pipeline") != "openpi_224_direct"
            or bool(manifest.get("materialize_front_video")) != materialize_front_video
            or int((manifest.get("master_clock") or {}).get("frame_count", -1))
            != expected_frames
        ):
            return None
        imu = manifest.get("imu") or {}
        imu_chapters = imu.get("chapters") or []
        compact_timing_only = imu.get("status") == "compact_clock_timing_only"
        timing = manifest.get("clock_timing") or {}
        if (
            timing.get("schema") != CLOCK_TIMING_MANIFEST_SCHEMA
            or len(imu_chapters) != len(insv_paths)
        ):
            return None
        timing_manifest_path = Path(str(timing.get("manifest_path", ""))).expanduser().resolve()
        timing_manifest = json.loads(timing_manifest_path.read_text(encoding="utf-8"))
        timing_records = timing_manifest.get("artifacts") or []
        if (
            timing_manifest.get("schema") != CLOCK_TIMING_MANIFEST_SCHEMA
            or len(timing_records) != len(insv_paths)
        ):
            return None
        for source, imu_record, timing_record in zip(
            insv_paths, imu_chapters, timing_records
        ):
            source = source.resolve()
            if not recorded_identity_matches(imu_record.get("source"), source):
                return None
            if not recorded_identity_matches(timing_record.get("source"), source):
                return None
            artifact_path = Path(str(timing_record.get("path", ""))).expanduser().resolve()
            if not recorded_identity_matches(
                timing_record.get("file_identity"), artifact_path
            ):
                return None
            if not compact_timing_only:
                for key in ("csv", "raw_json"):
                    if not Path(str(imu_record.get(key, ""))).is_file():
                        return None
        for key in ("timestamps_csv", "sync_json_path"):
            if not Path(str(manifest.get(key, ""))).is_file():
                return None
        for value in manifest.get("sync_json_paths") or []:
            if not Path(str(value)).is_file():
                return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="InstaUMI 第 1 阶段：原始数据、三视角和 IMU 提取")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        help="中间数据根目录；默认使用 <input-dir>/tmp",
    )
    parser.add_argument("--episode", default=None)
    parser.add_argument("--skip-imu", action="store_true")
    parser.add_argument(
        "--skip-videos",
        action="store_true",
        help="不生成/重建视频，只补 sync、frame_timestamps、IMU 和 manifest；要求三路视频已完整存在",
    )
    parser.add_argument(
        "--direct-image-mode",
        action="store_true",
        help=(
            "OpenPI image-mode 准备：保留原始 INSV、IMU 和时间信息；不要求 panorama MP4，"
            "不生成 224 MP4/side MP4。"
        ),
    )
    parser.add_argument(
        "--materialize-front-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "是否把有序 INSV 0:v:0 额外 stream-copy 为 front_fisheye.mp4；"
            "OpenPI direct + _03 raw-INSV 流程可关闭，省去大文件写入。"
        ),
    )
    parser.add_argument("--repair-frame-counts", action="store_true", help="配合 --skip-videos 使用，裁掉已有视频多余帧，使三路 RGB 帧数严格一致")
    parser.add_argument("--skip-existing-videos", action="store_true", help="目标视频已存在时跳过重编码，常用于补提 IMU")
    parser.add_argument("--parallel-side-views", action="store_true", help="并行生成左右 90 度视角；输出参数不变，只减少等待时间")
    parser.add_argument(
        "--single-decode-side-views",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="单次解码 panorama 同时生成左右视角；输出与独立 FFmpeg 进程一致",
    )
    parser.add_argument("--ffmpeg-preset", default="veryfast", help="编码器 preset；libx264 可用 ultrafast，h264_nvenc 会映射到 p1-p7")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg 可执行文件路径；GPU 编码可传 /usr/bin/ffmpeg")
    parser.add_argument(
        "--ffprobe-bin",
        default=None,
        help="逐帧 PTS 提取使用的 ffprobe；默认优先使用 ffmpeg 同目录程序。",
    )
    parser.add_argument(
        "--write-clock-timing-artifacts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "复用本阶段唯一一次 telemetry 解析并保存校准所需 IMU/元数据，同时一次性提取精确 PTS；"
            "后续时钟校准不再打开原始 INSV。"
        ),
    )
    parser.add_argument(
        "--telemetry-workers",
        type=int,
        default=1,
        help=(
            "并行解析独立 INSV 章节并生成时钟工件的 worker 数；"
            "双臂 wrapper 会按介质和整机预算设置。"
        ),
    )
    parser.add_argument(
        "--video-encoder",
        choices=["libx264rgb", "libx264", "h264_nvenc"],
        default="libx264rgb",
        help="模型视频编码器；默认 libx264rgb 可在 CRF 0 下逐像素无损",
    )
    parser.add_argument("--video-quality", type=int, default=0, help="质量参数；libx264rgb + 0 为 RGB 逐像素无损")
    parser.add_argument("--parallel-front-segments", action="store_true", help="多段 INSV 时并行提取 front_fisheye 分段后拼接")
    parser.add_argument("--front-segment-workers", type=int, default=0, help="front 分段并行 worker 数；0 表示按分段数")
    parser.add_argument(
        "--ffmpeg-threads",
        type=int,
        default=max(1, min(4, (os.cpu_count() or 1) // 4)),
        help="每个 FFmpeg 编码/滤镜任务线程上限；双臂 wrapper 会按整机预算自动计算",
    )
    parser.add_argument("--max-frames", type=int, default=None, help="调试用，只处理前 N 帧；正式运行不要传")
    parser.add_argument(
        "--allow-frame-trim",
        action="store_true",
        help="旧数据兼容：panorama 与 INSV 总帧数不一致时显式使用较小值；默认严格失败",
    )
    parser.add_argument("--model-image-width", type=int, default=224, help="输出给后续模型使用的视频宽度")
    parser.add_argument("--model-image-height", type=int, default=224, help="输出给后续模型使用的视频高度")
    parser.add_argument("--camera-role", choices=["insta_right", "insta_left"], default=None)
    parser.add_argument(
        "--video-frame0-host-unix-time-sec",
        type=float,
        default=None,
        help="Motion-calibrated video frame-zero host Unix time. Omit to use the BLE dispatch estimate.",
    )
    parser.add_argument(
        "--source-copy-mode",
        choices=["auto", "copy", "hardlink"],
        default="auto",
        help="raw_sources 保存方式；auto 同盘使用硬链接，跨盘回退 copy",
    )
    args = parser.parse_args()
    if not args.direct_image_mode and not args.materialize_front_video:
        parser.error("--no-materialize-front-video is only valid with --direct-image-mode")
    if args.ffmpeg_threads < 1:
        parser.error("--ffmpeg-threads must be >= 1")
    if args.front_segment_workers < 0:
        parser.error("--front-segment-workers must be >= 0")
    if args.telemetry_workers < 1:
        parser.error("--telemetry-workers must be >= 1")
    if args.ffprobe_bin is None:
        sibling_ffprobe = Path(args.ffmpeg_bin).expanduser().resolve().with_name("ffprobe")
        args.ffprobe_bin = (
            str(sibling_ffprobe)
            if sibling_ffprobe.is_file()
            else (shutil.which("ffprobe") or "ffprobe")
        )

    input_dir = args.input_dir.expanduser().resolve()
    insv_paths, pano_mp4, log_path = discover_inputs(
        input_dir,
        panorama_required=not args.direct_image_mode,
    )
    episode = args.episode or (
        episode_name_from_video(pano_mp4) if pano_mp4 is not None else input_dir.name
    )
    tmp_root = args.tmp_root.expanduser().resolve() if args.tmp_root is not None else input_dir / "tmp"
    output_dir = tmp_root / episode / "01_raw_extract"
    output_dir.mkdir(parents=True, exist_ok=True)

    pano_probe = probe_video(pano_mp4) if pano_mp4 is not None else None
    front_segment_frames = [
        exact_frame_count_from_probe(probe_video(path, "v:0"), str(path), path, "v:0")
        for path in insv_paths
    ]
    front_frames = sum(front_segment_frames)
    front_probe = probe_video(insv_paths[0], "v:0")
    fps = parse_fps(front_probe["r_frame_rate"])
    if pano_probe is None:
        frames = front_frames
    else:
        pano_frames = exact_frame_count_from_probe(pano_probe, str(pano_mp4), pano_mp4)
        frames = select_master_frame_count(pano_frames, front_frames, args.allow_frame_trim)
    if args.max_frames is not None:
        frames = min(frames, args.max_frames)
    front_segments = active_frame_segments(insv_paths, front_segment_frames, frames)
    if not front_segments:
        raise RuntimeError("no front_fisheye segments selected")

    manifest_path = output_dir / "manifest.json"
    if (
        args.direct_image_mode
        and args.write_clock_timing_artifacts
        and args.skip_existing_videos
    ):
        existing_manifest = reusable_direct_raw_manifest(
            manifest_path,
            insv_paths,
            frames,
            args.materialize_front_video,
        )
        if existing_manifest is not None:
            print(
                f"[skip] complete _01 timing/IMU artifacts: {manifest_path}; "
                "raw INSV telemetry and packet PTS were not reopened",
                flush=True,
            )
            print(json.dumps(existing_manifest, indent=2, ensure_ascii=False))
            return

    videos_dir = output_dir / "videos"
    front_video = videos_dir / "front_fisheye.mp4"
    front_video_224 = videos_dir / f"front_fisheye_{args.model_image_width}x{args.model_image_height}.mp4"
    left_video = videos_dir / "left_view_fov90x90.mp4"
    right_video = videos_dir / "right_view_fov90x90.mp4"

    if args.skip_videos:
        if args.materialize_front_video:
            require_existing_video(
                front_video,
                frames,
                "front_fisheye",
                args.repair_frame_counts,
                source_video_expectation(probe_video(front_segments[0][0], "v:0"), frames),
                front_copy_recipe(front_segments, frames),
            )
        front_model_expectation = encoded_video_expectation(
            args.video_encoder,
            args.model_image_width,
            args.model_image_height,
            frames,
            probe_video(front_segments[0][0], "v:0").get("r_frame_rate"),
        )
        if not args.direct_image_mode and front_video_224.exists():
            require_existing_video(
                front_video_224,
                frames,
                "front_fisheye_model_view",
                args.repair_frame_counts,
                front_model_expectation,
                resized_front_recipe(
                    front_segments,
                    frames,
                    args.model_image_width,
                    args.model_image_height,
                    args.ffmpeg_preset,
                    args.video_encoder,
                    args.video_quality,
                ),
            )
        for path, label, yaw in (() if args.direct_image_mode else (
            (left_video, "left_view_fov90x90", -90),
            (right_video, "right_view_fov90x90", 90),
        )):
            assert pano_probe is not None and pano_mp4 is not None
            side_expectation = encoded_video_expectation(
                args.video_encoder,
                args.model_image_width,
                args.model_image_height,
                frames,
                pano_probe.get("r_frame_rate"),
            )
            require_existing_video(
                path,
                frames,
                label,
                args.repair_frame_counts,
                side_expectation,
                side_view_recipe(
                    pano_mp4,
                    yaw,
                    frames,
                    args.model_image_width,
                    args.model_image_height,
                    args.ffmpeg_preset,
                    args.video_encoder,
                    args.video_quality,
                ),
            )
    else:
        if args.materialize_front_video:
            maybe_extract_front_fisheye_segments(
                front_segments,
                front_video,
                frames,
                args.skip_existing_videos,
                args.parallel_front_segments,
                args.front_segment_workers,
                args.ffmpeg_bin,
            )
        if not args.direct_image_mode:
            maybe_extract_front_fisheye_resized_segments(
                front_segments,
                front_video_224,
                frames,
                args.model_image_width,
                args.model_image_height,
                args.ffmpeg_preset,
                args.skip_existing_videos,
                args.parallel_front_segments,
                args.front_segment_workers,
                args.ffmpeg_bin,
                args.video_encoder,
                args.video_quality,
                args.ffmpeg_threads,
            )
    if args.skip_videos:
        pass
    elif args.direct_image_mode:
        pass
    elif args.single_decode_side_views:
        assert pano_mp4 is not None
        maybe_extract_side_views_single_decode(
            pano_mp4,
            left_video,
            right_video,
            frames,
            args.model_image_width,
            args.model_image_height,
            args.ffmpeg_preset,
            args.skip_existing_videos,
            args.ffmpeg_bin,
            args.video_encoder,
            args.video_quality,
            args.ffmpeg_threads,
        )
    elif args.parallel_side_views:
        assert pano_mp4 is not None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    maybe_extract_side_view,
                    pano_mp4,
                    left_video,
                    -90,
                    frames,
                    args.model_image_width,
                    args.model_image_height,
                    args.ffmpeg_preset,
                    args.skip_existing_videos,
                    args.ffmpeg_bin,
                    args.video_encoder,
                    args.video_quality,
                    args.ffmpeg_threads,
                ),
                pool.submit(
                    maybe_extract_side_view,
                    pano_mp4,
                    right_video,
                    90,
                    frames,
                    args.model_image_width,
                    args.model_image_height,
                    args.ffmpeg_preset,
                    args.skip_existing_videos,
                    args.ffmpeg_bin,
                    args.video_encoder,
                    args.video_quality,
                    args.ffmpeg_threads,
                ),
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    else:
        assert pano_mp4 is not None
        maybe_extract_side_view(
            pano_mp4,
            left_video,
            yaw=-90,
            frames=frames,
            width=args.model_image_width,
            height=args.model_image_height,
            preset=args.ffmpeg_preset,
            skip_existing=args.skip_existing_videos,
            ffmpeg_bin=args.ffmpeg_bin,
            video_encoder=args.video_encoder,
            video_quality=args.video_quality,
            ffmpeg_threads=args.ffmpeg_threads,
        )
        maybe_extract_side_view(
            pano_mp4,
            right_video,
            yaw=90,
            frames=frames,
            width=args.model_image_width,
            height=args.model_image_height,
            preset=args.ffmpeg_preset,
            skip_existing=args.skip_existing_videos,
            ffmpeg_bin=args.ffmpeg_bin,
            video_encoder=args.video_encoder,
            video_quality=args.video_quality,
            ffmpeg_threads=args.ffmpeg_threads,
        )

    telemetry_segments, telemetry_errors = load_telemetry_segments(
        insv_paths,
        args.telemetry_workers,
    )

    # Preserve a sync record for every camera chapter.  The first path remains
    # the compatibility anchor consumed by historical tools.
    sync_data_segments = [
        generate_sync_json(
            insv_path,
            output_dir,
            telemetry if telemetry else None,
            error,
        )
        for insv_path, telemetry, error in zip(insv_paths, telemetry_segments, telemetry_errors)
    ]
    sync_data = sync_data_segments[0]

    timestamps_csv = output_dir / "timestamps" / "frame_timestamps.csv"
    time_reference = write_frame_timestamps(
        timestamps_csv,
        fps,
        frames,
        log_path,
        sync_data,
        args.camera_role,
        args.video_frame0_host_unix_time_sec,
    )

    imu_info = None
    if not args.skip_imu:
        try:
            failed = [
                (path, error)
                for path, error in zip(insv_paths, telemetry_errors)
                if error is not None
            ]
            if failed:
                path, error = failed[0]
                raise RuntimeError(f"failed to parse IMU chapter {path}: {error}")
            imu_info = extract_imu_segments(insv_paths, output_dir / "imu", telemetry_segments)
        except Exception as exc:
            print(f"[warning] IMU extraction failed: {exc}", file=sys.stderr)
            imu_info = {
                "status": "failed",
                "error": str(exc),
                "note": "RGB 和 frame_timestamps 已生成；安装 telemetry_parser 后可单独重跑本阶段提取 IMU。",
            }
    else:
        imu_info = {
            "status": (
                "compact_clock_timing_only"
                if args.write_clock_timing_artifacts
                else "skipped"
            ),
            "chapter_count": len(insv_paths),
            "sample_count": sum(
                len(telemetry.get("imu") or []) for telemetry in telemetry_segments
            ),
            "chapters": [
                {
                    "segment_index": index,
                    "source": file_identity(path),
                    "sample_count": len(telemetry_segments[index].get("imu") or []),
                }
                for index, path in enumerate(insv_paths)
            ],
            "note": (
                "Redundant legacy IMU JSON/CSV was not materialized. Exact packet PTS, "
                "IMU timestamps, and gyro norm required by clock calibration are retained "
                "in provenance-bound timing NPZ artifacts; raw INSV remains unchanged."
            ),
        }

    clock_timing = None
    if args.write_clock_timing_artifacts:
        failed = [
            (path, error)
            for path, error in zip(insv_paths, telemetry_errors)
            if error is not None
        ]
        if failed:
            path, error = failed[0]
            raise RuntimeError(
                f"cannot publish required clock timing artifact for {path}: {error}"
            )
        clock_timing = write_clock_timing_artifacts(
            insv_paths,
            output_dir,
            telemetry_segments,
            str(args.ffprobe_bin),
            args.telemetry_workers,
        )

    videos: dict[str, str] = {}
    video_resolutions: dict[str, Any] = {}
    if args.materialize_front_video:
        wrist_model_video = (
            front_video
            if args.direct_image_mode
            else (front_video_224 if front_video_224.exists() else front_video)
        )
        videos.update(
            {
                "front_fisheye": str(front_video),
                "wrist_model": str(wrist_model_video),
            }
        )
        video_resolutions.update(
            {
                "front_fisheye": "source",
                "wrist_model": "source"
                if args.direct_image_mode
                else [args.model_image_width, args.model_image_height],
            }
        )
    if not args.direct_image_mode:
        videos.update(
            {
                "front_fisheye_224": str(front_video_224),
                "left_view": str(left_video),
                "right_view": str(right_video),
            }
        )
        video_resolutions.update(
            {
                "left_view": [args.model_image_width, args.model_image_height],
                "right_view": [args.model_image_width, args.model_image_height],
            }
        )
    master_source: Any = (
        [str(path) for path in insv_paths]
        if args.direct_image_mode
        else str(pano_mp4)
    )
    manifest = {
        "schema": "instaumi_raw_extract_v1",
        "episode": episode,
        "input_dir": str(input_dir),
        "copied_sources": copy_sources(insv_paths, pano_mp4, log_path, output_dir, args.source_copy_mode),
        "master_clock": {
            "source": master_source,
            "stream": "0:v:0" if args.direct_image_mode else None,
            "fps": str(fps),
            "frame_count": frames,
        },
        "time_reference": time_reference,
        "image_pipeline": "openpi_224_direct" if args.direct_image_mode else "legacy_model_video",
        "materialize_front_video": args.materialize_front_video,
        "videos": videos,
        "video_resolutions": video_resolutions,
        "ffmpeg": {
            "bin": str(args.ffmpeg_bin),
            "video_encoder": args.video_encoder,
            "preset": args.ffmpeg_preset,
            "quality": args.video_quality,
            "single_decode_side_views": args.single_decode_side_views,
            "parallel_front_segments": args.parallel_front_segments,
            "front_segment_workers": args.front_segment_workers,
            "ffmpeg_threads": args.ffmpeg_threads,
        },
        "telemetry_workers": args.telemetry_workers,
        "timestamps_csv": str(timestamps_csv),
        "sync_json_path": str(output_dir / "raw_sources" / f"{insv_paths[0].stem}.sync.json"),
        "sync_json_paths": [
            str(output_dir / "raw_sources" / f"{path.stem}.sync.json") for path in insv_paths
        ],
        "imu": imu_info,
        "clock_timing": clock_timing,
        "notes": (
            [
                "OpenPI direct image mode: _01 不生成 224/side 中间视频。",
                (
                    "front_fisheye 对 INSV 0:v:0 按章节顺序 stream-copy，无像素重编码。"
                    if args.materialize_front_video
                    else "不落盘 front_fisheye 中间视频；_03 和 _05 直接读取有序 INSV 0:v:0。"
                ),
                "最终 224 RGB 应在完成时间对齐/帧选择后直接生成并以无损 PNG 写入 Parquet。",
            ]
            if args.direct_image_mode
            else [
                "三路 RGB 均使用 panorama mp4 的 frame_idx/timestamp_sec 作为主时钟。",
                "front_fisheye 来自所有 INSV 分段的 0:v:0，并按文件顺序拼接。若未来确认前向鱼眼是另一路，需要在此处切换。",
                "历史模型视频在本阶段执行 Lanczos 缩放和 luma 锐化；该路径不是 OpenPI 的 PIL BILINEAR resize_with_pad。",
            ]
        ),
    }
    atomic_write_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
