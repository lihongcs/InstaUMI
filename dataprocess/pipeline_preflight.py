#!/usr/bin/env python3
"""Fail fast on syntax errors in long-running data-processing entry points.

The processing jobs can run for hours.  This preflight deliberately performs
only read-only syntax checks, so a broken downstream script is reported before
the first frame is decoded or an output directory is rebuilt.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tokenize
from pathlib import Path


ROOT = Path(__file__).resolve().parent

ORDINARY_FILES = (
    ROOT / "pipeline_preflight.py",
    ROOT / "adaptive_cpu_quota.py",
    ROOT / "active_frame_ranges.py",
    ROOT / "audit_lerobot_sync.py",
    ROOT / "capture_schema.py",
    ROOT / "direct_openpi_images.py",
    ROOT / "uvc_capture_source.py",
    ROOT / "fast_workspace.py",
    ROOT / "production_semantics.py",
    ROOT / "run_bimanual_htc.sh",
    ROOT / "_01_raw_extrect/run_01_raw_extract_biarm.sh",
    ROOT / "_01_raw_extrect/run_01_uvc_extract_biarm.sh",
    ROOT / "_01_raw_extrect/artifact_io.py",
    ROOT / "_01_raw_extrect/extract_raw_views_imu.py",
    ROOT / "_01_raw_extrect/extract_uvc_capture.py",
    ROOT / "_01_raw_extrect/calibrate_no_slam_clock.py",
    ROOT / "_03_gripper_estimation/run_03_gripper_estimation_biarm.sh",
    ROOT / "_03_gripper_estimation/validate_completed_biarm_stage.py",
    ROOT / "_03_gripper_estimation/estimate_gripper_width.py",
    ROOT / "_05_convert_bimanual_lerobot/run_05_convert_bimanual_lerobot_htc.sh",
    ROOT / "_05_convert_bimanual_lerobot/convert_bimanual_to_lerobot.py",
    ROOT / "_05_convert_bimanual_lerobot/bimanual_convert_helpers.py",
    ROOT / "_05_convert_bimanual_lerobot/htc_to_flexiv_transform.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/constants.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/configs/types.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/backward_compatibility.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/compute_stats.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/image_writer.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/lerobot_dataset.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/pose_util.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/utils.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/datasets/video_utils.py",
    ROOT / "_05_convert_bimanual_lerobot/lerobot/utils/utils.py",
)
UVC_FILES = (
    ROOT / "pipeline_preflight.py",
    ROOT / "capture_schema.py",
    ROOT / "uvc_capture_source.py",
    ROOT / "_01_raw_extrect/run_01_raw_extract_biarm.sh",
    ROOT / "_01_raw_extrect/run_01_uvc_extract_biarm.sh",
    ROOT / "_01_raw_extrect/extract_uvc_capture.py",
)


def iter_sources(profile: str) -> tuple[list[Path], list[Path]]:
    if profile == "uvc":
        candidates = list(UVC_FILES)
    else:
        candidates = list(ORDINARY_FILES)

    # This is an explicit production dependency manifest.  Debug, legacy,
    # SAM2 and installer helpers must not block an unrelated production run.
    missing = sorted(path for path in candidates if not path.is_file())
    if missing:
        raise FileNotFoundError(f"required source file is missing: {missing[0]}")

    unique = sorted(set(path.resolve() for path in candidates))
    return (
        [path for path in unique if path.suffix == ".sh"],
        [path for path in unique if path.suffix == ".py"],
    )


def check_shell(path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    detail = (result.stderr or result.stdout).strip()
    syntax_warning = re.search(
        r"(?i)(here-document.*delimited by end-of-file|unexpected EOF|syntax error)",
        result.stderr,
    )
    # GNU bash can return zero for an unterminated heredoc while printing only
    # a warning.  Treat that warning as a hard failure for production jobs.
    if result.returncode != 0 or syntax_warning:
        raise SyntaxError(f"bash syntax check failed: {path}\n{detail}")
    check_embedded_python(path)


HEREDOC_START = re.compile(
    r"<<(?P<strip>-)?\s*(?P<quote>['\"])(?P<delimiter>PY(?:THON|CODE)?)(?P=quote)"
)
MULTILINE_PYTHON_C = re.compile(r"(?:PYTHON|python)[^\n]*\s-c\s+'\s*$")


def _compile_embedded(source: str, path: Path, start_line: int, kind: str) -> None:
    try:
        compile(source, f"{path}:{start_line} ({kind})", "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise SyntaxError(
            f"embedded Python syntax check failed: {path}:{start_line} ({kind})\n{exc}"
        ) from exc


def check_embedded_python(path: Path) -> None:
    """Compile quoted Python heredocs and multiline ``python -c`` bodies."""

    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        heredoc = HEREDOC_START.search(line)
        if heredoc:
            delimiter = heredoc.group("delimiter")
            strip_tabs = heredoc.group("strip") is not None
            body: list[str] = []
            cursor = index + 1
            while cursor < len(lines):
                candidate = lines[cursor].lstrip("\t") if strip_tabs else lines[cursor]
                if candidate == delimiter:
                    break
                body.append(candidate)
                cursor += 1
            if cursor >= len(lines):
                raise SyntaxError(
                    f"unterminated embedded Python heredoc: {path}:{index + 1}"
                )
            _compile_embedded(
                "\n".join(body) + "\n", path, index + 2, f"heredoc {delimiter}"
            )
            index = cursor + 1
            continue

        if MULTILINE_PYTHON_C.search(line):
            body = []
            cursor = index + 1
            while cursor < len(lines) and not re.match(r"^\s*'", lines[cursor]):
                body.append(lines[cursor])
                cursor += 1
            if cursor >= len(lines):
                raise SyntaxError(
                    f"unterminated multiline Python -c body: {path}:{index + 1}"
                )
            _compile_embedded(
                "\n".join(body) + "\n", path, index + 2, "multiline python -c"
            )
            index = cursor + 1
            continue
        index += 1


def check_python(path: Path) -> None:
    # compile() checks syntax without importing modules or writing .pyc files.
    with tokenize.open(path) as handle:
        source = handle.read()
    compile(source, str(path), "exec", dont_inherit=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("ordinary", "uvc"),
        required=True,
    )
    args = parser.parse_args()

    shell_files, python_files = iter_sources(args.profile)
    for path in shell_files:
        check_shell(path)
    for path in python_files:
        check_python(path)
    print(
        f"[preflight] profile={args.profile} status=passed "
        f"shell_files={len(shell_files)} python_files={len(python_files)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[preflight:error] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
