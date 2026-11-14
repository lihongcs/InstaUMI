#!/usr/bin/env python3
"""Crash-safe panorama publication and raw-source preservation helpers.

The panorama exporter is a separate C++ process, so this module also exposes a
small command line interface for the shell wrappers.  A completed artifact is
accepted only when its media properties, ordered source identities and export
recipe match the adjacent sidecar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


PANO_PLAN_SCHEMA = "instaumi_pano_plan_v1"
PANO_ARTIFACT_SCHEMA = "instaumi_pano_artifact_v1"
SOURCE_ARTIFACT_SCHEMA = "instaumi_source_artifact_v1"
DEFAULT_SAMPLE_SIZE = 256 * 1024


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".partial.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
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
            data = handle.read(min(sample_size, max(0, size - offset)))
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return digest.hexdigest()


def sampled_file_identity(path: Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict[str, Any]:
    """Return a stable size/mtime plus beginning/middle/end content identity."""

    resolved = path.expanduser().resolve()
    before = resolved.stat()
    digest = _sample_digest(resolved, before.st_size, sample_size)
    after = resolved.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise RuntimeError(f"file changed while computing identity: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sample_size_bytes": int(sample_size),
        "sample_sha256": digest,
    }


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def exact_file_identity(path: Path, known_sha256: str | None = None) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    before = resolved.stat()
    digest = known_sha256 or sha256_file(resolved)
    after = resolved.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise RuntimeError(f"file changed while computing full identity: {resolved}")
    identity = sampled_file_identity(resolved)
    if identity["size_bytes"] != after.st_size or identity["mtime_ns"] != after.st_mtime_ns:
        raise RuntimeError(f"file changed while completing identity: {resolved}")
    identity["sha256"] = digest
    return identity


def pano_sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".pano.json")


def source_sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".source.json")


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def probe_video(path: Path, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"missing or empty video: {path}")
    entries = "stream=codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration"
    data = _run_json(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            entries,
            "-of",
            "json",
            str(path),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"no primary video stream: {path}")
    stream = streams[0]
    frame_count = stream.get("nb_frames")
    if frame_count in (None, "", "N/A"):
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
        frame_count = counted_streams[0].get("nb_read_frames") if counted_streams else None
    if frame_count in (None, "", "N/A"):
        raise RuntimeError(f"exact frame count unavailable: {path}")
    rate = stream.get("r_frame_rate") or stream.get("avg_frame_rate")
    if not rate or rate == "0/0":
        raise RuntimeError(f"frame rate unavailable: {path}")
    return {
        "codec_name": str(stream.get("codec_name") or ""),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_rate": str(Fraction(str(rate))),
        "frame_count": int(frame_count),
    }


def _stable_input_record(
    path: Path, ffprobe_bin: str
) -> tuple[dict[str, Any], dict[str, Any], int]:
    before = sampled_file_identity(path)
    media = probe_video(path, ffprobe_bin)
    count_data = _run_json(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(path),
        ]
    )
    after = sampled_file_identity(path)
    if before != after:
        raise RuntimeError(f"input changed while creating panorama plan: {path}")
    return after, media, len(count_data.get("streams") or [])


def build_pano_plan(
    inputs: Iterable[Path],
    *,
    width: int,
    height: int,
    fps: str,
    bitrate: str,
    flowstate: str,
    stitch_type: str,
    stitch_fusion: str,
    direction_lock: str = "off",
    input_mode: str = "auto",
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    ordered_inputs = [Path(item).expanduser().resolve() for item in inputs]
    if not ordered_inputs:
        raise ValueError("panorama plan requires at least one input")
    input_records: list[dict[str, Any]] = []
    input_frame_counts: list[int] = []
    stream_counts: list[int] = []
    for path in ordered_inputs:
        identity, media, stream_count = _stable_input_record(path, ffprobe_bin)
        input_records.append({"file": identity, "primary_video": media})
        input_frame_counts.append(int(media["frame_count"]))
        stream_counts.append(stream_count)
    if input_mode not in {"auto", "segments", "group"}:
        raise ValueError(f"invalid panorama input mode: {input_mode}")
    requested_input_mode = input_mode
    if input_mode != "auto":
        effective_input_mode = input_mode
    elif len(ordered_inputs) == 1:
        effective_input_mode = "single"
    elif all(count >= 2 for count in stream_counts):
        effective_input_mode = "segments"
    else:
        effective_input_mode = "group"
    if effective_input_mode == "group":
        if len(set(input_frame_counts)) != 1:
            raise RuntimeError(
                "dual-file panorama group inputs must have identical primary frame counts: "
                + ", ".join(
                    f"{path}={count}" for path, count in zip(ordered_inputs, input_frame_counts)
                )
            )
        # Repeated group inputs describe synchronized companion/lens material,
        # not temporal chapters.  MediaSDK consumes them together.
        expected_frames = input_frame_counts[0]
    else:
        # A single file contributes itself; temporal chapter mode concatenates
        # every chapter in order.
        expected_frames = sum(input_frame_counts)
    normalized_fps = str(Fraction(str(fps)))
    return {
        "schema": PANO_PLAN_SCHEMA,
        "recipe": {
            "operation": "insta360_panorama_export",
            "ordered_inputs": input_records,
            "input_mode": effective_input_mode,
            "export_parameters": {
                "width": int(width),
                "height": int(height),
                "fps": normalized_fps,
                "bitrate": str(bitrate),
                "flowstate": str(flowstate),
                "stitch_type": str(stitch_type),
                "stitch_fusion": str(stitch_fusion),
                "direction_lock": str(direction_lock),
                "input_mode_request": requested_input_mode,
            },
        },
        "expectation": {
            "width": int(width),
            "height": int(height),
            "frame_rate": normalized_fps,
            "frame_count": expected_frames,
        },
    }


def validate_pano_media(
    path: Path, expectation: dict[str, Any], ffprobe_bin: str = "ffprobe"
) -> dict[str, Any]:
    media = probe_video(path, ffprobe_bin)
    for key in ("width", "height", "frame_count"):
        if int(media[key]) != int(expectation[key]):
            raise RuntimeError(
                f"panorama {key} mismatch for {path}: got {media[key]}, expected {expectation[key]}"
            )
    if Fraction(str(media["frame_rate"])) != Fraction(str(expectation["frame_rate"])):
        raise RuntimeError(
            f"panorama frame_rate mismatch for {path}: "
            f"got {media['frame_rate']}, expected {expectation['frame_rate']}"
        )
    return media


def _fast_identity_matches(recorded: dict[str, Any], path: Path) -> bool:
    current = sampled_file_identity(path, int(recorded.get("sample_size_bytes", DEFAULT_SAMPLE_SIZE)))
    return all(
        recorded.get(key) == current.get(key)
        for key in ("path", "size_bytes", "mtime_ns", "sample_size_bytes", "sample_sha256")
    )


def _write_pano_sidecar(
    output: Path,
    plan: dict[str, Any],
    media: dict[str, Any],
    *,
    publish_mode: str,
    known_sha256: str | None = None,
) -> None:
    identity = exact_file_identity(output, known_sha256=known_sha256)
    atomic_write_json(
        pano_sidecar_path(output),
        {
            "schema": PANO_ARTIFACT_SCHEMA,
            "recipe": plan["recipe"],
            "expectation": plan["expectation"],
            "media": media,
            "output_identity": identity,
            "publish_mode": publish_mode,
        },
    )


def check_pano(
    video: Path,
    plan: dict[str, Any],
    *,
    ffprobe_bin: str = "ffprobe",
    adopt_legacy: bool = False,
    verify_full_output_hash: bool = False,
) -> tuple[bool, str]:
    try:
        media = validate_pano_media(video, plan["expectation"], ffprobe_bin)
    except Exception as exc:
        return False, str(exc)
    sidecar = pano_sidecar_path(video)
    if not sidecar.is_file():
        if not adopt_legacy:
            return False, "missing panorama provenance sidecar"
        try:
            fsync_file(video)
            _write_pano_sidecar(video, plan, media, publish_mode="legacy_adopted_after_exact_media_validation")
        except Exception as exc:
            return False, f"failed to adopt legacy panorama: {exc}"
        return True, "legacy panorama validated and provenance sidecar created"
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"unreadable panorama sidecar: {exc}"
    if payload.get("schema") != PANO_ARTIFACT_SCHEMA:
        return False, "panorama sidecar schema mismatch"
    if payload.get("recipe") != plan.get("recipe"):
        return False, "panorama ordered inputs or export parameters changed"
    if payload.get("expectation") != plan.get("expectation"):
        return False, "panorama expectation changed"
    if payload.get("media") != media:
        return False, "panorama media identity changed"
    output_identity = payload.get("output_identity")
    if not isinstance(output_identity, dict) or not output_identity.get("sha256"):
        return False, "panorama exact output identity missing"
    try:
        if not _fast_identity_matches(output_identity, video):
            return False, "panorama output identity changed"
        if verify_full_output_hash and sha256_file(video) != output_identity["sha256"]:
            return False, "panorama output sha256 changed"
    except Exception as exc:
        return False, f"cannot validate panorama output identity: {exc}"
    return True, f"frames={media['frame_count']} sha256={output_identity['sha256']}"


def publish_pano(
    partial: Path,
    output: Path,
    plan: dict[str, Any],
    *,
    ffprobe_bin: str = "ffprobe",
) -> None:
    media = validate_pano_media(partial, plan["expectation"], ffprobe_bin)
    fsync_file(partial)
    digest = sha256_file(partial)
    # Detect a source replacement during a long MediaSDK render.  The final
    # pathname is not touched if the current recipe no longer matches.
    current = build_pano_plan_from_recipe(plan["recipe"], ffprobe_bin=ffprobe_bin)
    if current != plan:
        raise RuntimeError("panorama inputs changed while export was running")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, output)
    _fsync_directory(output.parent)
    _write_pano_sidecar(output, plan, media, publish_mode="validated_atomic_replace", known_sha256=digest)


def build_pano_plan_from_recipe(
    recipe: dict[str, Any], *, ffprobe_bin: str = "ffprobe"
) -> dict[str, Any]:
    parameters = recipe["export_parameters"]
    inputs = [Path(item["file"]["path"]) for item in recipe["ordered_inputs"]]
    return build_pano_plan(
        inputs,
        width=int(parameters["width"]),
        height=int(parameters["height"]),
        fps=str(parameters["fps"]),
        bitrate=str(parameters["bitrate"]),
        flowstate=str(parameters["flowstate"]),
        stitch_type=str(parameters["stitch_type"]),
        stitch_fusion=str(parameters["stitch_fusion"]),
        direction_lock=str(parameters.get("direction_lock", "off")),
        input_mode=str(parameters.get("input_mode_request", "auto")),
        ffprobe_bin=ffprobe_bin,
    )


def _source_records_match(source: Path, destination: Path) -> bool:
    source_identity = sampled_file_identity(source)
    destination_identity = sampled_file_identity(destination)
    return (
        source_identity["size_bytes"] == destination_identity["size_bytes"]
        and source_identity["sample_size_bytes"] == destination_identity["sample_size_bytes"]
        and source_identity["sample_sha256"] == destination_identity["sample_sha256"]
    )


def _write_source_sidecar(
    source: Path,
    destination: Path,
    *,
    method: str,
    content_sha256: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema": SOURCE_ARTIFACT_SCHEMA,
        "source": sampled_file_identity(source),
        "destination": sampled_file_identity(destination),
        "method": method,
    }
    if content_sha256 is not None:
        payload["content_sha256"] = content_sha256
    atomic_write_json(source_sidecar_path(destination), payload)


def _existing_source_is_current(source: Path, destination: Path) -> bool:
    if not destination.is_file():
        return False
    sidecar = source_sidecar_path(destination)
    if not sidecar.is_file():
        return False
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if payload.get("schema") != SOURCE_ARTIFACT_SCHEMA:
            return False
        if payload.get("source") != sampled_file_identity(source):
            return False
        if payload.get("destination") != sampled_file_identity(destination):
            return False
        return _source_records_match(source, destination)
    except Exception:
        return False


def _copy_to_partial(source: Path, partial: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, partial.open("xb") as writer:
        while True:
            block = reader.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, partial, follow_symlinks=True)
    return digest.hexdigest()


def safe_preserve_file(source: Path, destination: Path, mode: str = "auto") -> str:
    """Preserve a source without ever exposing a partial destination."""

    source = source.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if mode not in {"auto", "copy", "hardlink"}:
        raise ValueError(f"invalid source copy mode: {mode}")
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and os.path.samefile(source, destination):
        if not _existing_source_is_current(source, destination):
            _write_source_sidecar(source, destination, method="hardlink")
        return "existing-hardlink"
    if _existing_source_is_current(source, destination):
        return "existing"
    if destination.is_file() and _source_records_match(source, destination):
        # A legacy cross-filesystem copy has no commit marker.  Size and three
        # samples exclude truncation cheaply; one full comparison before
        # adoption also excludes unsampled corruption without taxing later
        # resumes (which use the sidecar identity).
        source_digest = sha256_file(source)
        if sha256_file(destination) == source_digest:
            _write_source_sidecar(
                source,
                destination,
                method="legacy-adopted-full-sha256-verified",
                content_sha256=source_digest,
            )
            return "existing-validated"

    partial = destination.with_name(destination.name + f".partial.{os.getpid()}")
    partial.unlink(missing_ok=True)
    method = "copy"
    digest: str | None = None
    source_before = sampled_file_identity(source)
    try:
        if mode in {"auto", "hardlink"}:
            try:
                os.link(source, partial)
                method = "hardlink"
            except OSError:
                if mode == "hardlink":
                    raise
        if not partial.exists():
            digest = _copy_to_partial(source, partial)
            method = "copy"
        if sampled_file_identity(source) != source_before:
            raise RuntimeError(f"source changed while it was being preserved: {source}")
        if not _source_records_match(source, partial):
            raise RuntimeError(f"source copy verification failed: {source} -> {partial}")
        fsync_file(partial)
        os.replace(partial, destination)
        _fsync_directory(destination.parent)
        _write_source_sidecar(
            source,
            destination,
            method=method,
            content_sha256=digest,
        )
        return method
    finally:
        partial.unlink(missing_ok=True)


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", required=True)
    parser.add_argument("--bitrate", required=True)
    parser.add_argument("--flowstate", required=True)
    parser.add_argument("--stitch-type", required=True)
    parser.add_argument("--stitch-fusion", required=True)
    parser.add_argument("--direction-lock", required=True)
    parser.add_argument("--input-mode", choices=["auto", "segments", "group"], default="auto")
    parser.add_argument("--ffprobe-bin", default=os.environ.get("FFPROBE_BIN", "ffprobe"))


def _plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return build_pano_plan(
        args.input,
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate=args.bitrate,
        flowstate=args.flowstate,
        stitch_type=args.stitch_type,
        stitch_fusion=args.stitch_fusion,
        direction_lock=args.direction_lock,
        input_mode=args.input_mode,
        ffprobe_bin=args.ffprobe_bin,
    )


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("pano-plan")
    plan_parser.add_argument("--plan", type=Path, required=True)
    _add_plan_arguments(plan_parser)

    check_parser = subparsers.add_parser("pano-check")
    check_parser.add_argument("--video", type=Path, required=True)
    check_parser.add_argument("--adopt-legacy", action="store_true")
    check_parser.add_argument("--verify-full-output-hash", action="store_true")
    _add_plan_arguments(check_parser)

    publish_parser = subparsers.add_parser("pano-publish")
    publish_parser.add_argument("--partial", type=Path, required=True)
    publish_parser.add_argument("--output", type=Path, required=True)
    publish_parser.add_argument("--plan", type=Path, required=True)
    publish_parser.add_argument("--ffprobe-bin", default=os.environ.get("FFPROBE_BIN", "ffprobe"))

    copy_parser = subparsers.add_parser("safe-copy")
    copy_parser.add_argument("--source", type=Path, required=True)
    copy_parser.add_argument("--destination", type=Path, required=True)
    copy_parser.add_argument("--mode", choices=["auto", "copy", "hardlink"], default="auto")

    args = parser.parse_args(argv)
    if args.command == "pano-plan":
        atomic_write_json(args.plan, _plan_from_args(args))
        return 0
    if args.command == "pano-check":
        ok, reason = check_pano(
            args.video,
            _plan_from_args(args),
            ffprobe_bin=args.ffprobe_bin,
            adopt_legacy=args.adopt_legacy,
            verify_full_output_hash=args.verify_full_output_hash,
        )
        print(reason)
        return 0 if ok else 1
    if args.command == "pano-publish":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if plan.get("schema") != PANO_PLAN_SCHEMA:
            raise RuntimeError(f"invalid panorama plan: {args.plan}")
        publish_pano(args.partial, args.output, plan, ffprobe_bin=args.ffprobe_bin)
        return 0
    if args.command == "safe-copy":
        print(safe_preserve_file(args.source, args.destination, args.mode))
        return 0
    raise AssertionError(args.command)


def main() -> None:
    try:
        raise SystemExit(cli())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[artifact-error] {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
