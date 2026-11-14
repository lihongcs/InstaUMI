#!/usr/bin/env python3
"""Direct, bounded OpenPI-compatible image decoding from raw camera videos.

This module deliberately does not depend on the LeRobot converter.  It reads
front frames from ordered raw INSV/video chapters, projects both side views in
one FFmpeg filter graph, and applies the same PIL bilinear ``resize_with_pad``
algorithm used by ``openpi_client``.

The pre-exported-panorama path adds no intermediate image/video.  The raw-INSV
path asks MediaSDK for bounded batches of lossless full-resolution PNGs, then
deletes those PNGs in ``finally`` after producing only the requested 224 views.
Neither path uses Lanczos, unsharp, or x264.  The chunk checkpoint class is a
commit-marker interface for callers: artifact files must already have been
atomically published before the checkpoint is committed.
"""

from __future__ import annotations

import bisect
import concurrent.futures
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np
from PIL import Image

try:
    from dataprocess._01_raw_extrect.insta360_pano_export.scripts.png_sequence_artifact import (
        ArtifactError,
        inspect_png,
    )
except ModuleNotFoundError as exc:
    if exc.name != "dataprocess":
        raise
    # ``convert_bimanual_to_lerobot.py`` is also supported as a directly
    # executed script; in that mode DATAPROCESS_ROOT itself is on sys.path.
    from _01_raw_extrect.insta360_pano_export.scripts.png_sequence_artifact import (
        ArtifactError,
        inspect_png,
    )


PLAN_SCHEMA = "instaumi.direct_openpi_images.plan.v1"
RAW_PLAN_SCHEMA = "instaumi.direct_raw_openpi_images.plan.v2"
RAW_RUNTIME_SCHEMA = "instaumi.direct_raw_openpi_images.runtime.v1"
RAW_MODEL_SCHEMA = "instaumi.direct_raw_openpi_images.models.v1"
RAW_OWNER_SCHEMA = "instaumi.direct_raw_openpi_images.owner.v1"
FRONT_CACHE_SCHEMA = "instaumi.front_openpi_cache.v1"

# Third-party imports (notably native video packages) may prepend their own
# directories to LD_LIBRARY_PATH at runtime.  Freeze only loader/device
# selection variables when this module enters the process; unrelated runtime
# variables still pass through to exporter jobs.
_PROCESS_START_ENVIRONMENT = dict(os.environ)
CHECKPOINT_SCHEMA = "instaumi.direct_openpi_images.chunk.v1"
OPENPI_RESIZE_RECIPE = "openpi_client.resize_with_pad.pil_bilinear.v1"
SIDE_RECIPE = "ffmpeg.v360.flat.linear.split_pair_then_openpi_resize.v1"
DEFAULT_SAMPLE_BYTES = 256 * 1024
# The training dataset publishes only 224x224 views.  Keep the official
# MediaSDK optical-flow stitcher, but ask it for the smallest 2:1 panorama
# that still leaves more than 2x linear sampling headroom for a 90-degree
# 224px side view.  The generic high-resolution exporter keeps its own 3840
# default; these values apply only to the transient direct-to-OpenPI path.
DEFAULT_OPENPI_PANORAMA_WIDTH = 2048
DEFAULT_OPENPI_PANORAMA_HEIGHT = 1024
_SAFE_CHUNK_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_RAW_BATCH_NAME = re.compile(
    r"^raw-(?P<plan>[0-9a-f]{64})-b(?P<boot>[0-9a-f]{12})-"
    r"p(?P<pid>[0-9]+)-s(?P<start>[0-9]+)-(?P<nonce>[0-9a-f]+)$"
)
_WORK_ROOT_LOCK_NAME = ".instaumi-direct-openpi.lock"
_OWNER_MANIFEST_NAME = ".instaumi-owner.json"


class DirectImageError(RuntimeError):
    """Base error for direct image processing."""


class SourceValidationError(DirectImageError):
    """Raised when a source no longer matches its recorded identity."""


class FrameDecodeError(DirectImageError):
    """Raised when an absolute frame cannot be decoded exactly."""


@dataclass
class SharedMediaSDKGPUAdmission:
    """Process-shared admission control for memory-heavy MediaSDK jobs.

    MediaSDK allocates CUVID and optical-flow buffers lazily during the first
    few output frames.  Starting every exporter at exactly the same time can
    therefore exhaust VRAM even when the later steady-state snapshot looks
    modest.  Keep the total number of live jobs bounded, independently bound
    the startup phase, and lower the live-job ceiling after a confirmed CUDA
    OOM.  The synchronization primitives are created from the same
    multiprocessing context that spawns episode-shard workers.
    """

    condition: Any
    active_jobs: Any
    active_startups: Any
    current_limit: Any
    last_reduction_ns: Any
    maximum_limit: int
    minimum_limit: int
    startup_limit: int
    startup_progress: int
    retry_count: int

    @classmethod
    def create(
        cls,
        context: Any,
        *,
        maximum_limit: int,
        minimum_limit: int,
        startup_limit: int,
        startup_progress: int = 10,
        retry_count: int = 6,
    ) -> "SharedMediaSDKGPUAdmission":
        if maximum_limit < 1:
            raise ValueError("maximum_limit must be positive")
        if not 1 <= minimum_limit <= maximum_limit:
            raise ValueError("minimum_limit must be within the GPU worker limit")
        if not 1 <= startup_limit <= maximum_limit:
            raise ValueError("startup_limit must be within the GPU worker limit")
        if not 1 <= startup_progress <= 100:
            raise ValueError("startup_progress must be in [1, 100]")
        if retry_count < 0:
            raise ValueError("retry_count must be non-negative")
        return cls(
            condition=context.Condition(context.RLock()),
            active_jobs=context.Value("i", 0, lock=False),
            active_startups=context.Value("i", 0, lock=False),
            current_limit=context.Value("i", maximum_limit, lock=False),
            last_reduction_ns=context.Value("q", 0, lock=False),
            maximum_limit=int(maximum_limit),
            minimum_limit=int(minimum_limit),
            startup_limit=int(startup_limit),
            startup_progress=int(startup_progress),
            retry_count=int(retry_count),
        )

    def acquire_job(self) -> tuple[int, int]:
        with self.condition:
            while int(self.active_jobs.value) >= int(self.current_limit.value):
                self.condition.wait(timeout=0.5)
            self.active_jobs.value += 1
            return int(self.active_jobs.value), int(self.current_limit.value)

    def release_job(self) -> None:
        with self.condition:
            self.active_jobs.value -= 1
            if int(self.active_jobs.value) < 0:
                raise RuntimeError("MediaSDK GPU active-job accounting underflow")
            self.condition.notify_all()

    def acquire_startup(self) -> int:
        with self.condition:
            while int(self.active_startups.value) >= self.startup_limit:
                self.condition.wait(timeout=0.25)
            self.active_startups.value += 1
            return int(self.active_startups.value)

    def release_startup(self) -> None:
        with self.condition:
            self.active_startups.value -= 1
            if int(self.active_startups.value) < 0:
                raise RuntimeError("MediaSDK GPU startup accounting underflow")
            self.condition.notify_all()

    def report_cuda_oom(self) -> tuple[int, bool]:
        """Lower the limit once for one contemporaneous OOM wave."""

        now_ns = time.monotonic_ns()
        with self.condition:
            current = int(self.current_limit.value)
            # Several exporters can observe the same exhaustion wave.  Count
            # it once, then let the failed jobs retry under the new ceiling.
            separated_wave = now_ns - int(self.last_reduction_ns.value) >= 5_000_000_000
            reduced = current > self.minimum_limit and separated_wave
            if reduced:
                current -= 1
                self.current_limit.value = current
                self.last_reduction_ns.value = now_ns
            self.condition.notify_all()
            return current, reduced

    def snapshot(self) -> tuple[int, int, int]:
        with self.condition:
            return (
                int(self.active_jobs.value),
                int(self.active_startups.value),
                int(self.current_limit.value),
            )


class CheckpointValidationError(DirectImageError):
    """Raised when a chunk checkpoint or one of its artifacts is stale."""


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_file(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _stable_stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def full_file_identity(path: Path, *, reject_symlink: bool = False) -> dict[str, Any]:
    """Return a stable full-SHA identity for one ordinary file."""

    expanded = path.expanduser()
    try:
        if reject_symlink and stat.S_ISLNK(expanded.lstat().st_mode):
            raise SourceValidationError(f"symbolic links are forbidden: {expanded}")
        resolved = expanded.resolve(strict=True)
        before = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise SourceValidationError(f"could not identify regular file: {expanded}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise SourceValidationError(f"not an ordinary file: {resolved}")
    digest = _sha256_file(resolved)
    after = resolved.stat()
    if _stable_stat_signature(before) != _stable_stat_signature(after):
        raise SourceValidationError(f"file changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "ctime_ns": int(after.st_ctime_ns),
        "sha256": digest,
    }


def verify_full_file_identity(identity: Mapping[str, Any]) -> Path:
    try:
        expected = {
            "path": str(Path(str(identity["path"])).expanduser().resolve(strict=True)),
            "size_bytes": int(identity["size_bytes"]),
            "mtime_ns": int(identity["mtime_ns"]),
            "ctime_ns": int(identity["ctime_ns"]),
            "sha256": str(identity["sha256"]),
        }
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SourceValidationError(f"invalid full file identity: {identity}") from exc
    actual = full_file_identity(Path(expected["path"]))
    if actual != expected:
        raise SourceValidationError(f"full file identity changed: {expected['path']}")
    return Path(expected["path"])


def _normalized_loader_environment(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Create the fixed loader environment used by both ldd and MediaSDK."""

    source = dict(os.environ if environ is None else environ)
    if environ is None:
        for frozen_name in (
            "PATH",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "LD_AUDIT",
            "CUDA_VISIBLE_DEVICES",
            "CUDA_DEVICE_ORDER",
        ):
            if frozen_name in _PROCESS_START_ENVIRONMENT:
                source[frozen_name] = _PROCESS_START_ENVIRONMENT[frozen_name]
            else:
                source.pop(frozen_name, None)
    for unsafe_name in ("LD_PRELOAD", "LD_AUDIT"):
        if source.get(unsafe_name, "").strip():
            raise SourceValidationError(f"unsafe loader injection is forbidden: {unsafe_name}")
    normalized_paths: list[str] = []
    for item in source.get("LD_LIBRARY_PATH", "").split(":"):
        if not item or not os.path.isabs(item):
            continue
        normalized = os.path.normpath(item)
        if normalized not in normalized_paths:
            normalized_paths.append(normalized)
    for name in tuple(source):
        if name.startswith("LD_"):
            source.pop(name, None)
    if normalized_paths:
        source["LD_LIBRARY_PATH"] = ":".join(normalized_paths)
    source["LC_ALL"] = "C"
    source["LANG"] = "C"
    record = {
        "policy": "absolute_nonempty_ld_library_path_and_no_preload_or_audit_v1",
        "ld_library_path": normalized_paths,
        "cuda_visible_devices": source.get("CUDA_VISIBLE_DEVICES"),
        "cuda_device_order": source.get("CUDA_DEVICE_ORDER"),
    }
    return source, record


def _replay_loader_environment(
    expected: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Rebuild the captured loader environment for a spawned worker.

    Python/native packages may prepend an interpreter-private directory to
    ``LD_LIBRARY_PATH`` after this module has captured a raw-image plan.  A
    multiprocessing ``spawn`` child then sees that mutated value as its
    process-start environment even though neither the requested MediaSDK
    runtime nor its resolved dependency closure changed.  Replaying the
    already validated plan environment makes the exporter deterministic in
    every child instead of treating that harmless inheritance detail as a
    source mutation.

    The current process environment is normalized first so unsafe loader
    injection is still rejected.  The caller subsequently verifies the full
    exporter/MediaSDK identity and dependency closure under this replayed
    environment.
    """

    runtime, _current = _normalized_loader_environment(environ)
    policy = expected.get("policy")
    if policy != "absolute_nonempty_ld_library_path_and_no_preload_or_audit_v1":
        raise SourceValidationError("invalid captured loader environment policy")
    raw_paths = expected.get("ld_library_path")
    if not isinstance(raw_paths, list) or not all(
        isinstance(item, str) for item in raw_paths
    ):
        raise SourceValidationError("invalid captured LD_LIBRARY_PATH")
    normalized_paths: list[str] = []
    for item in raw_paths:
        if not item or not os.path.isabs(item):
            raise SourceValidationError("captured LD_LIBRARY_PATH must be absolute")
        normalized = os.path.normpath(item)
        if normalized != item or normalized in normalized_paths:
            raise SourceValidationError("captured LD_LIBRARY_PATH is not canonical")
        normalized_paths.append(normalized)

    runtime.pop("LD_LIBRARY_PATH", None)
    if normalized_paths:
        runtime["LD_LIBRARY_PATH"] = ":".join(normalized_paths)

    device_values: dict[str, str | None] = {}
    for record_name, environment_name in (
        ("cuda_visible_devices", "CUDA_VISIBLE_DEVICES"),
        ("cuda_device_order", "CUDA_DEVICE_ORDER"),
    ):
        value = expected.get(record_name)
        if value is not None and not isinstance(value, str):
            raise SourceValidationError(f"invalid captured {environment_name}")
        device_values[record_name] = value
        if value is None:
            runtime.pop(environment_name, None)
        else:
            runtime[environment_name] = value

    replayed = {
        "policy": policy,
        "ld_library_path": normalized_paths,
        **device_values,
    }
    if replayed != dict(expected):
        raise SourceValidationError("captured loader environment has unknown fields")
    return runtime, replayed


def _parse_ldd_output(output: str) -> dict[str, Path]:
    dependencies: dict[str, Path] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso.so"):
            continue
        if "=> not found" in line:
            raise SourceValidationError(f"dynamic dependency not found: {line}")
        if "=>" not in line:
            # The ELF interpreter is printed as an absolute path without an arrow.
            if line.startswith("/") and " (0x" in line:
                continue
            if "statically linked" in line or "not a dynamic executable" in line:
                raise SourceValidationError(f"ldd did not inspect a dynamic executable: {line}")
            continue
        soname, target_text = (part.strip() for part in line.split("=>", 1))
        target = target_text.split(" (0x", 1)[0].strip()
        if not soname or not target or not os.path.isabs(target):
            raise SourceValidationError(f"invalid ldd dependency line: {line}")
        if soname in dependencies:
            raise SourceValidationError(f"duplicate ldd dependency: {soname}")
        try:
            dependencies[soname] = Path(target).resolve(strict=True)
        except OSError as exc:
            raise SourceValidationError(f"ldd dependency disappeared: {target}") from exc
    return dependencies


def _resolve_dynamic_dependencies(
    binary: Path, runtime_env: Mapping[str, str]
) -> tuple[Path, dict[str, Path]]:
    ldd = Path(shutil.which("ldd") or "/usr/bin/ldd").resolve(strict=True)
    if not ldd.is_file() or not os.access(ldd, os.X_OK):
        raise SourceValidationError(f"ldd is unavailable: {ldd}")
    completed = subprocess.run(
        [str(ldd), str(binary)],
        env=dict(runtime_env),
        cwd="/",
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise SourceValidationError(
            f"ldd failed for {binary} (exit={completed.returncode}): "
            f"{(completed.stdout + completed.stderr)[-2000:]}"
        )
    return ldd, _parse_ldd_output(completed.stdout + "\n" + completed.stderr)


def _model_tree_snapshot(root: Path) -> dict[str, Any]:
    expanded = root.expanduser()
    try:
        if stat.S_ISLNK(expanded.lstat().st_mode):
            raise SourceValidationError(f"model_dir cannot be a symbolic link: {expanded}")
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise SourceValidationError(f"MediaSDK model directory is unavailable: {expanded}") from exc
    if not resolved.is_dir():
        raise SourceValidationError(f"MediaSDK model path is not a directory: {resolved}")
    files: list[dict[str, Any]] = []
    directories: list[str] = []

    def visit(directory: Path) -> None:
        before = directory.stat()
        if not stat.S_ISDIR(before.st_mode):
            raise SourceValidationError(f"model tree entry is not a directory: {directory}")
        entries = sorted(os.scandir(directory), key=lambda item: item.name)
        for entry in entries:
            entry_path = Path(entry.path)
            relative = entry_path.relative_to(resolved).as_posix()
            if entry.is_symlink():
                raise SourceValidationError(f"model tree symbolic link is forbidden: {entry_path}")
            if entry.is_dir(follow_symlinks=False):
                directories.append(relative)
                visit(entry_path)
            elif entry.is_file(follow_symlinks=False):
                identity = full_file_identity(entry_path, reject_symlink=True)
                files.append(
                    {
                        "relative_path": relative,
                        "size_bytes": identity["size_bytes"],
                        "mtime_ns": identity["mtime_ns"],
                        "ctime_ns": identity["ctime_ns"],
                        "sha256": identity["sha256"],
                    }
                )
            else:
                raise SourceValidationError(f"special model tree entry is forbidden: {entry_path}")
        after = directory.stat()
        if _stable_stat_signature(before) != _stable_stat_signature(after):
            raise SourceValidationError(f"model directory changed while hashing: {directory}")

    visit(resolved)
    body = {"directories": directories, "files": files}
    return {
        "schema": RAW_MODEL_SCHEMA,
        "mode": "explicit_recursive_full_sha256",
        "path": str(resolved),
        **body,
        "tree_sha256": hashlib.sha256(_canonical_json(body)).hexdigest(),
    }


def _model_provenance(model_dir: Path | None) -> dict[str, Any]:
    if model_dir is None:
        body = {"directories": [], "files": []}
        return {
            "schema": RAW_MODEL_SCHEMA,
            "mode": "unset_sdk_default",
            "path": None,
            **body,
            "tree_sha256": hashlib.sha256(_canonical_json(body)).hexdigest(),
        }
    return _model_tree_snapshot(model_dir)


def _capture_raw_runtime_provenance(
    exporter: Path,
    *,
    cuda_requested: bool,
    loader_environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if loader_environment is None:
        runtime_env, loader_record = _normalized_loader_environment()
    else:
        runtime_env, loader_record = _replay_loader_environment(loader_environment)
    ldd, dependencies = _resolve_dynamic_dependencies(exporter, runtime_env)
    mediasdk_matches = [
        path for soname, path in dependencies.items() if soname == "libMediaSDK.so"
    ]
    if len(mediasdk_matches) != 1:
        raise SourceValidationError(
            "exporter must resolve exactly one libMediaSDK.so; "
            f"found={len(mediasdk_matches)}"
        )
    try:
        import av

        # Constructing the filter descriptor proves this PyAV/libavfilter build
        # actually exposes the v360 implementation used by RawV360PairProjector.
        av.filter.Filter("v360")
    except Exception as exc:
        raise SourceValidationError("PyAV with the native v360 filter is required") from exc
    library_versions = {
        str(name): [int(value) for value in version]
        for name, version in sorted(av.library_versions.items())
    }
    cuda_record: dict[str, Any]
    if cuda_requested:
        nvidia_smi_text = shutil.which("nvidia-smi")
        if not nvidia_smi_text:
            raise SourceValidationError("CUDA was requested but nvidia-smi is unavailable")
        nvidia_smi = Path(nvidia_smi_text).resolve(strict=True)
        completed = subprocess.run(
            [
                str(nvidia_smi),
                "--query-gpu=driver_version,name,uuid,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            env=runtime_env,
            cwd="/",
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise SourceValidationError(
                f"nvidia-smi identity query failed: {(completed.stdout + completed.stderr)[-1000:]}"
            )
        devices: list[dict[str, str]] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 4 or any(not field for field in fields):
                raise SourceValidationError(f"unexpected nvidia-smi identity row: {line!r}")
            devices.append(
                {
                    "driver_version": fields[0],
                    "name": fields[1],
                    "uuid": fields[2],
                    "compute_capability": fields[3],
                }
            )
        if not devices:
            raise SourceValidationError("CUDA was requested but nvidia-smi returned no GPU")
        cuda_record = {
            "requested": True,
            "nvidia_smi_path": str(nvidia_smi),
            "devices": sorted(devices, key=lambda item: item["uuid"]),
        }
    else:
        cuda_record = {"requested": False, "state": "disabled"}
    dependency_closure = [
        {"soname": soname, "resolved_path": str(path)}
        for soname, path in sorted(dependencies.items())
    ]
    return {
        "schema": RAW_RUNTIME_SCHEMA,
        "loader_environment": loader_record,
        "ldd_path": str(ldd),
        "dependency_closure": dependency_closure,
        "libmediasdk": {
            "soname": "libMediaSDK.so",
            "identity": full_file_identity(mediasdk_matches[0]),
        },
        "pyav": {
            "version": str(av.__version__),
            "library_versions": library_versions,
            "required_filter": "v360",
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "cuda": cuda_record,
    }


def _provenance_differences(
    expected: Any,
    actual: Any,
    *,
    path: str = "$",
    limit: int = 8,
) -> list[str]:
    """Return bounded, deterministic field-level provenance differences."""

    differences: list[str] = []

    def add(message: str) -> None:
        if len(differences) < limit:
            differences.append(message)

    def walk(left: Any, right: Any, current_path: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            add(
                f"{current_path}: type {type(left).__name__} != "
                f"{type(right).__name__}"
            )
            return
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{current_path}.{key}"
                if key not in left:
                    add(f"{child}: unexpectedly present")
                elif key not in right:
                    add(f"{child}: missing")
                else:
                    walk(left[key], right[key], child)
            return
        if isinstance(left, list):
            if len(left) != len(right):
                add(f"{current_path}: length {len(left)} != {len(right)}")
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                walk(left_item, right_item, f"{current_path}[{index}]")
            return
        if left != right:
            left_text = repr(left)
            right_text = repr(right)
            add(f"{current_path}: {left_text[:160]} != {right_text[:160]}")

    walk(expected, actual, path)
    return differences


def _verify_runtime_provenance(
    expected: Mapping[str, Any],
    exporter: Path,
    *,
    cuda_requested: bool,
    attempts: int = 3,
) -> None:
    """Reject stable runtime changes while tolerating a single transient probe."""

    if attempts < 1:
        raise ValueError("runtime provenance verification attempts must be positive")
    last_differences: list[str] = []
    for attempt in range(attempts):
        current = _capture_raw_runtime_provenance(
            exporter,
            cuda_requested=cuda_requested,
            loader_environment=expected.get("loader_environment") or {},
        )
        if current == expected:
            return
        last_differences = _provenance_differences(expected, current)
        if attempt + 1 < attempts:
            time.sleep(0.2)
    detail = "; ".join(last_differences) or "unknown field difference"
    raise SourceValidationError(
        "MediaSDK/PyAV/CUDA runtime provenance changed after "
        f"{attempts} probes: {detail}"
    )


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SourceValidationError("Linux boot_id is required for temporary ownership") from exc
    normalized = value.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise SourceValidationError(f"invalid Linux boot_id: {value!r}")
    return normalized


def _process_start_time_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return None
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields_after_comm = text[closing + 1 :].split()
    # /proc/PID/stat field 22 is starttime; field 3 is the first token here.
    if len(fields_after_comm) <= 19:
        return None
    try:
        return int(fields_after_comm[19])
    except ValueError:
        return None


@contextmanager
def _work_root_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _WORK_ROOT_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SourceValidationError(f"work-root lock is not an ordinary file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _batch_has_live_output_process(batch_root: Path) -> bool | None:
    """Return True/False, or None when /proc cannot prove the batch is idle."""

    target = str(batch_root)
    unknown = False
    try:
        process_entries = tuple(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in process_entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            unknown = True
            continue
        except OSError:
            continue
        arguments = [os.fsdecode(value) for value in raw.split(b"\0") if value]
        if any(argument == target or argument.startswith(target + os.sep) for argument in arguments):
            return True
    return None if unknown else False


def _sample_digest(path: Path, size: int, sample_bytes: int) -> str:
    offsets = sorted(
        {
            0,
            max(0, size // 2 - sample_bytes // 2),
            max(0, size - sample_bytes),
        }
    )
    digest = hashlib.sha256()
    digest.update(b"instaumi-direct-openpi-source-v1\0")
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(handle.read(min(sample_bytes, max(0, size - offset))))
    return digest.hexdigest()


def source_identity(path: Path, sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> dict[str, Any]:
    """Identify a large source by path/stat and beginning/middle/end samples."""

    resolved = path.expanduser().resolve(strict=True)
    before = resolved.stat()
    digest = _sample_digest(resolved, before.st_size, sample_bytes)
    after = resolved.stat()
    signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if signature_before != signature_after:
        raise SourceValidationError(f"source changed while being identified: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "sample_bytes": int(sample_bytes),
        "sample_sha256": digest,
    }


def verify_source_identity(identity: Mapping[str, Any], verify_samples: bool = False) -> Path:
    try:
        path = Path(str(identity["path"])).expanduser().resolve(strict=True)
        expected_size = int(identity["size_bytes"])
        expected_mtime = int(identity["mtime_ns"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SourceValidationError(f"invalid source identity: {identity}") from exc
    info = path.stat()
    if info.st_size != expected_size or info.st_mtime_ns != expected_mtime:
        raise SourceValidationError(
            f"source stat changed: {path} size={info.st_size}/{expected_size} "
            f"mtime_ns={info.st_mtime_ns}/{expected_mtime}"
        )
    if verify_samples:
        sample_bytes = int(identity.get("sample_bytes", DEFAULT_SAMPLE_BYTES))
        current = _sample_digest(path, info.st_size, sample_bytes)
        if current != identity.get("sample_sha256"):
            raise SourceValidationError(f"source sampled content changed: {path}")
    return path


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def probe_video(path: Path, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    """Probe exact primary-stream dimensions, rate, and frame count."""

    resolved = path.expanduser().resolve(strict=True)
    data = _run_json(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames",
            "-of",
            "json",
            str(resolved),
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise SourceValidationError(f"source has no primary video stream: {resolved}")
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
                str(resolved),
            ]
        )
        counted_streams = counted.get("streams") or []
        frame_count = counted_streams[0].get("nb_read_frames") if counted_streams else None
    if frame_count in (None, "", "N/A"):
        raise SourceValidationError(f"exact frame count unavailable: {resolved}")
    rate = stream.get("r_frame_rate") or stream.get("avg_frame_rate")
    if not rate or rate == "0/0":
        raise SourceValidationError(f"frame rate unavailable: {resolved}")
    result = {
        "codec_name": str(stream.get("codec_name") or ""),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_rate": str(Fraction(str(rate))),
        "frame_count": int(frame_count),
    }
    if result["width"] <= 0 or result["height"] <= 0 or result["frame_count"] <= 0:
        raise SourceValidationError(f"invalid video metadata for {resolved}: {result}")
    return result


@dataclass(frozen=True)
class VideoChapter:
    path: Path
    global_start: int
    frame_count: int
    width: int
    height: int
    frame_rate: str
    codec_name: str
    pix_fmt: str
    identity: dict[str, Any]

    @property
    def global_end(self) -> int:
        return self.global_start + self.frame_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "global_start": self.global_start,
            "global_end_exclusive": self.global_end,
            "frame_count": self.frame_count,
            "width": self.width,
            "height": self.height,
            "frame_rate": self.frame_rate,
            "codec_name": self.codec_name,
            "pix_fmt": self.pix_fmt,
            "identity": self.identity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VideoChapter":
        return cls(
            path=Path(str(payload["path"])).expanduser().resolve(),
            global_start=int(payload["global_start"]),
            frame_count=int(payload["frame_count"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            frame_rate=str(payload["frame_rate"]),
            codec_name=str(payload.get("codec_name", "")),
            pix_fmt=str(payload.get("pix_fmt", "")),
            identity=dict(payload["identity"]),
        )


def _build_chapters(paths: Sequence[Path], ffprobe_bin: str) -> tuple[VideoChapter, ...]:
    if not paths:
        raise ValueError("at least one video chapter is required")
    chapters: list[VideoChapter] = []
    start = 0
    for raw_path in paths:
        path = raw_path.expanduser().resolve(strict=True)
        identity = source_identity(path)
        media = probe_video(path, ffprobe_bin)
        if source_identity(path) != identity:
            raise SourceValidationError(f"source changed while its video metadata was probed: {path}")
        chapter = VideoChapter(
            path=path,
            global_start=start,
            frame_count=int(media["frame_count"]),
            width=int(media["width"]),
            height=int(media["height"]),
            frame_rate=str(media["frame_rate"]),
            codec_name=str(media["codec_name"]),
            pix_fmt=str(media["pix_fmt"]),
            identity=identity,
        )
        chapters.append(chapter)
        start = chapter.global_end
    return tuple(chapters)


@dataclass(frozen=True)
class DirectOpenPIImagePlan:
    front_chapters: tuple[VideoChapter, ...]
    panorama_chapters: tuple[VideoChapter, ...]
    target_height: int = 224
    target_width: int = 224
    projection_size: int | None = None
    left_yaw: float = -90.0
    right_yaw: float = 90.0
    horizontal_fov: float = 90.0
    vertical_fov: float = 90.0

    @classmethod
    def build(
        cls,
        front_paths: Sequence[Path],
        panorama_paths: Sequence[Path],
        *,
        target_height: int = 224,
        target_width: int = 224,
        projection_size: int | None = None,
        left_yaw: float = -90.0,
        right_yaw: float = 90.0,
        horizontal_fov: float = 90.0,
        vertical_fov: float = 90.0,
        ffprobe_bin: str = "ffprobe",
    ) -> "DirectOpenPIImagePlan":
        if target_height <= 0 or target_width <= 0:
            raise ValueError("target dimensions must be positive")
        if projection_size is not None and projection_size <= 0:
            raise ValueError("projection_size must be positive when provided")
        front = _build_chapters(front_paths, ffprobe_bin)
        panorama = _build_chapters(panorama_paths, ffprobe_bin)
        front_total = front[-1].global_end
        panorama_total = panorama[-1].global_end
        if front_total != panorama_total:
            raise SourceValidationError(
                "front/panorama exact frame totals differ: "
                f"front={front_total}, panorama={panorama_total}"
            )
        return cls(
            front_chapters=front,
            panorama_chapters=panorama,
            target_height=int(target_height),
            target_width=int(target_width),
            projection_size=int(projection_size) if projection_size is not None else None,
            left_yaw=float(left_yaw),
            right_yaw=float(right_yaw),
            horizontal_fov=float(horizontal_fov),
            vertical_fov=float(vertical_fov),
        )

    @property
    def total_frames(self) -> int:
        return self.front_chapters[-1].global_end

    def payload(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "front_chapters": [chapter.to_dict() for chapter in self.front_chapters],
            "panorama_chapters": [chapter.to_dict() for chapter in self.panorama_chapters],
            "recipe": {
                "front": "decode_primary_stream_then_openpi_resize",
                "side": SIDE_RECIPE,
                "resize": OPENPI_RESIZE_RECIPE,
                "target_height": self.target_height,
                "target_width": self.target_width,
                "projection_size": self.projection_size,
                "projection_size_auto": "min(panorama_width/4,panorama_height/2)",
                "left_yaw": self.left_yaw,
                "right_yaw": self.right_yaw,
                "horizontal_fov": self.horizontal_fov,
                "vertical_fov": self.vertical_fov,
                "v360_interpolation": "linear",
                "forbidden_steps": ["lanczos", "unsharp", "libx264", "libx264rgb"],
            },
            "total_frames": self.total_frames,
        }

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = self.payload()
        result["plan_hash"] = self.plan_hash
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DirectOpenPIImagePlan":
        if payload.get("schema") != PLAN_SCHEMA:
            raise SourceValidationError("direct image plan schema mismatch")
        recipe = payload.get("recipe") or {}
        plan = cls(
            front_chapters=tuple(
                VideoChapter.from_dict(item) for item in payload.get("front_chapters") or []
            ),
            panorama_chapters=tuple(
                VideoChapter.from_dict(item) for item in payload.get("panorama_chapters") or []
            ),
            target_height=int(recipe["target_height"]),
            target_width=int(recipe["target_width"]),
            projection_size=(
                int(recipe["projection_size"])
                if recipe.get("projection_size") is not None
                else None
            ),
            left_yaw=float(recipe["left_yaw"]),
            right_yaw=float(recipe["right_yaw"]),
            horizontal_fov=float(recipe["horizontal_fov"]),
            vertical_fov=float(recipe["vertical_fov"]),
        )
        if not plan.front_chapters or not plan.panorama_chapters:
            raise SourceValidationError("direct image plan has no source chapters")
        if payload.get("plan_hash") != plan.plan_hash:
            raise SourceValidationError("direct image plan hash mismatch")
        return plan

    @classmethod
    def load(cls, path: Path) -> "DirectOpenPIImagePlan":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def verify_sources(self, verify_samples: bool = False) -> None:
        expected_start = 0
        for group_name, chapters in (
            ("front", self.front_chapters),
            ("panorama", self.panorama_chapters),
        ):
            for chapter in chapters:
                if chapter.global_start != expected_start:
                    raise SourceValidationError(
                        f"{group_name} chapter continuity mismatch at {chapter.path}"
                    )
                path = verify_source_identity(chapter.identity, verify_samples=verify_samples)
                if path != chapter.path:
                    raise SourceValidationError(
                        f"{group_name} chapter path mismatch: {path} != {chapter.path}"
                    )
                expected_start = chapter.global_end
            expected_start = 0
        if self.front_chapters[-1].global_end != self.panorama_chapters[-1].global_end:
            raise SourceValidationError("front/panorama plan totals differ")

    def write(self, path: Path) -> None:
        _atomic_write_json(path, self.to_dict())


def openpi_resize_with_pad(
    image: np.ndarray, height: int = 224, width: int = 224
) -> np.ndarray:
    """Pixel-equivalent single-image form of OpenPI's PIL resize_with_pad."""

    array = np.asarray(image)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(f"expected RGB uint8 HxWx3 image, got {array.shape} {array.dtype}")
    if array.shape[:2] == (height, width):
        return np.ascontiguousarray(array).copy()
    pil_image = Image.fromarray(array)
    current_width, current_height = pil_image.size
    ratio = max(current_width / width, current_height / height)
    resized_height = int(current_height / ratio)
    resized_width = int(current_width / ratio)
    resized = pil_image.resize((resized_width, resized_height), resample=Image.BILINEAR)
    padded = Image.new(resized.mode, (width, height), 0)
    pad_height = max(0, int((height - resized_height) / 2))
    pad_width = max(0, int((width - resized_width) / 2))
    padded.paste(resized, (pad_width, pad_height))
    return np.ascontiguousarray(np.asarray(padded, dtype=np.uint8))


def _get_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for direct video decoding") from exc
    return cv2


class ChapteredVideoReader:
    """Persistent absolute-index reader over ordered video chapters."""

    def __init__(
        self,
        chapters: Sequence[VideoChapter],
        seek_threshold_frames: int = 120,
        decode_threads: int = 1,
    ) -> None:
        if not chapters:
            raise ValueError("chapters cannot be empty")
        if seek_threshold_frames < 0:
            raise ValueError("seek_threshold_frames must be non-negative")
        if decode_threads < 1 or decode_threads > 4:
            raise ValueError("decode_threads must be in [1, 4]")
        self.chapters = tuple(chapters)
        self.starts = tuple(chapter.global_start for chapter in self.chapters)
        self.total_frames = self.chapters[-1].global_end
        self.seek_threshold_frames = int(seek_threshold_frames)
        self.decode_threads = int(decode_threads)
        self._chapter_index: int | None = None
        self._capture: Any | None = None
        self._next_local_index = 0
        self._last_global_index: int | None = None
        self._last_frame: np.ndarray | None = None

    def _locate(self, global_index: int) -> tuple[int, VideoChapter, int]:
        if global_index < 0 or global_index >= self.total_frames:
            raise IndexError(
                f"absolute frame index {global_index} outside [0, {self.total_frames})"
            )
        chapter_index = bisect.bisect_right(self.starts, global_index) - 1
        chapter = self.chapters[chapter_index]
        return chapter_index, chapter, global_index - chapter.global_start

    def _open(self, chapter_index: int) -> None:
        self._release_capture()
        cv2 = _get_cv2()
        chapter = self.chapters[chapter_index]
        if not hasattr(cv2, "CAP_PROP_N_THREADS"):
            raise RuntimeError("OpenCV lacks CAP_PROP_N_THREADS; decoder thread count cannot be bounded")
        capture = cv2.VideoCapture(
            str(chapter.path),
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_N_THREADS, self.decode_threads],
        )
        if not capture.isOpened():
            capture.release()
            raise FrameDecodeError(f"could not open video chapter: {chapter.path}")
        self._capture = capture
        self._chapter_index = chapter_index
        self._next_local_index = 0

    def _release_capture(self) -> None:
        if self._capture is not None:
            self._capture.release()
        self._capture = None
        self._chapter_index = None

    def read(self, global_index: int) -> np.ndarray:
        global_index = int(global_index)
        if global_index == self._last_global_index:
            assert self._last_frame is not None
            return self._last_frame.copy()
        chapter_index, chapter, local_index = self._locate(global_index)
        if self._chapter_index != chapter_index:
            self._open(chapter_index)
        assert self._capture is not None
        gap = local_index - self._next_local_index
        did_seek = False
        if gap < 0 or gap > self.seek_threshold_frames:
            cv2 = _get_cv2()
            if not bool(self._capture.set(cv2.CAP_PROP_POS_FRAMES, local_index)):
                raise FrameDecodeError(
                    f"decoder rejected seek to local frame {local_index}: {chapter.path}"
                )
            self._next_local_index = local_index
            did_seek = True
        decoded: np.ndarray | None = None
        while self._next_local_index <= local_index:
            ok, frame_bgr = self._capture.read()
            if not ok or frame_bgr is None or frame_bgr.size == 0:
                raise FrameDecodeError(
                    f"{chapter.path} ended before local frame {local_index} "
                    f"(absolute {global_index})"
                )
            decoded = frame_bgr
            self._next_local_index += 1
            if did_seek:
                cv2 = _get_cv2()
                observed_position = float(self._capture.get(cv2.CAP_PROP_POS_FRAMES))
                if not np.isfinite(observed_position) or abs(
                    observed_position - self._next_local_index
                ) > 0.5:
                    raise FrameDecodeError(
                        f"decoder seek position mismatch for {chapter.path}: "
                        f"got next={observed_position}, expected={self._next_local_index}"
                    )
        assert decoded is not None
        if decoded.shape[:2] != (chapter.height, chapter.width):
            raise FrameDecodeError(
                f"decoded geometry changed for {chapter.path}: "
                f"{decoded.shape[1]}x{decoded.shape[0]} != {chapter.width}x{chapter.height}"
            )
        cv2 = _get_cv2()
        rgb = np.ascontiguousarray(cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB), dtype=np.uint8)
        self._last_global_index = global_index
        self._last_frame = rgb
        return rgb.copy()

    def close(self) -> None:
        self._release_capture()
        self._last_global_index = None
        self._last_frame = None


class FrontOpenPICacheReader:
    """Read sparse final 224 RGB fronts produced during native ArUco decode.

    The cache is a runtime acceleration artifact, not a semantic image source:
    every chunk is bound to the raw INSV source set and the exact OpenPI resize
    recipe in the _03 manifest.  Arrays are mmap'ed lazily and full hashes are
    checked at most once per chunk, so validation does not multiply across
    frames.
    """

    def __init__(self, manifest_path: Path, *, verify_full_hash: bool = True) -> None:
        self.manifest_path = manifest_path.expanduser().resolve()
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceValidationError(
                f"front OpenPI cache manifest is unreadable: {self.manifest_path}"
            ) from exc
        cache = manifest.get("front_openpi_cache")
        if not isinstance(cache, dict) or cache.get("schema") != FRONT_CACHE_SCHEMA:
            raise SourceValidationError(
                f"front OpenPI cache is missing from _03 manifest: {self.manifest_path}"
            )
        expected_recipe = {
            "operation": OPENPI_RESIZE_RECIPE,
            "source_color": "bgr24",
            "output_color": "rgb24",
            "width": 224,
            "height": 224,
            "dtype": "uint8",
        }
        if cache.get("recipe") != expected_recipe:
            raise SourceValidationError(
                f"front OpenPI cache recipe changed: {self.manifest_path}"
            )
        chunks = cache.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise SourceValidationError(
                f"front OpenPI cache has no chunks: {self.manifest_path}"
            )
        self.verify_full_hash = bool(verify_full_hash)
        detection_fingerprint = str(cache.get("detection_fingerprint", ""))
        source_set_fingerprint = str(cache.get("source_set_fingerprint", ""))
        if len(detection_fingerprint) != 64 or len(source_set_fingerprint) != 64:
            raise SourceValidationError(
                f"front OpenPI cache provenance is incomplete: {self.manifest_path}"
            )
        if (
            manifest.get("detection_fingerprint") != detection_fingerprint
            or manifest.get("source_set_fingerprint") != source_set_fingerprint
        ):
            raise SourceValidationError(
                f"front OpenPI cache provenance disagrees with _03: {self.manifest_path}"
            )
        self._locations: dict[int, tuple[int, int]] = {}
        self._chunks: list[dict[str, Any]] = []
        self._arrays: dict[int, np.ndarray] = {}
        for chunk_index, record in enumerate(chunks):
            if not isinstance(record, dict):
                raise SourceValidationError("front OpenPI cache chunk record is invalid")
            chunk_manifest_path = Path(str(record.get("chunk_manifest", ""))).expanduser().resolve()
            try:
                chunk_manifest = json.loads(chunk_manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceValidationError(
                    f"front OpenPI chunk manifest is unreadable: {chunk_manifest_path}"
                ) from exc
            artifact = chunk_manifest.get("front_openpi_cache")
            if not isinstance(artifact, dict):
                raise SourceValidationError(
                    f"front OpenPI chunk artifact is missing: {chunk_manifest_path}"
                )
            if (
                artifact.get("schema") != FRONT_CACHE_SCHEMA
                or artifact.get("recipe") != expected_recipe
            ):
                raise SourceValidationError(
                    f"front OpenPI chunk recipe changed: {chunk_manifest_path}"
                )
            if chunk_manifest.get("detection_fingerprint") != detection_fingerprint:
                raise SourceValidationError(
                    f"front OpenPI cache detection plan changed: {chunk_manifest_path}"
                )
            indices = tuple(int(value) for value in chunk_manifest.get("requested_global_indices", ()))
            if not indices or indices != tuple(sorted(set(indices))):
                raise SourceValidationError(
                    f"front OpenPI chunk indices are invalid: {chunk_manifest_path}"
                )
            path = Path(str(artifact.get("path", ""))).expanduser().resolve()
            expected_shape = [len(indices), 224, 224, 3]
            if artifact.get("shape") != expected_shape or artifact.get("dtype") != "uint8":
                raise SourceValidationError(
                    f"front OpenPI chunk array contract changed: {chunk_manifest_path}"
                )
            if not path.is_file() or path.stat().st_size != int(artifact.get("size_bytes", -1)):
                raise SourceValidationError(f"front OpenPI chunk is missing or truncated: {path}")
            if (
                Path(str(record.get("array_path", ""))).expanduser().resolve() != path
                or int(record.get("size_bytes", -1)) != int(artifact.get("size_bytes", -2))
                or str(record.get("sha256", "")) != str(artifact.get("sha256", ""))
                or int(record.get("frame_count", -1)) != len(indices)
            ):
                raise SourceValidationError(
                    f"front OpenPI parent/chunk manifest mismatch: {chunk_manifest_path}"
                )
            stored = {
                "path": path,
                "sha256": str(artifact.get("sha256", "")),
                "shape": tuple(expected_shape),
            }
            self._chunks.append(stored)
            for row_index, frame_index in enumerate(indices):
                if frame_index in self._locations:
                    raise SourceValidationError(
                        f"front OpenPI cache contains duplicate frame {frame_index}"
                    )
                self._locations[frame_index] = (chunk_index, row_index)

    def _array(self, chunk_index: int) -> np.ndarray:
        existing = self._arrays.get(chunk_index)
        if existing is not None:
            return existing
        record = self._chunks[chunk_index]
        path = record["path"]
        if self.verify_full_hash and _sha256_file(path) != record["sha256"]:
            raise SourceValidationError(f"front OpenPI chunk SHA-256 mismatch: {path}")
        try:
            array = np.load(path, mmap_mode="r", allow_pickle=False)
        except Exception as exc:
            raise SourceValidationError(f"front OpenPI chunk cannot be mmap'ed: {path}") from exc
        if array.dtype != np.uint8 or tuple(array.shape) != record["shape"]:
            raise SourceValidationError(
                f"front OpenPI chunk shape/dtype mismatch: {path} {array.shape} {array.dtype}"
            )
        self._arrays[chunk_index] = array
        return array

    def read(self, global_index: int) -> np.ndarray:
        location = self._locations.get(int(global_index))
        if location is None:
            raise FrameDecodeError(
                f"front OpenPI cache does not contain active frame {global_index}: "
                f"{self.manifest_path}"
            )
        chunk_index, row_index = location
        result = np.asarray(self._array(chunk_index)[row_index])
        if result.dtype != np.uint8 or result.shape != (224, 224, 3):
            raise FrameDecodeError(f"front OpenPI cache frame changed: {global_index}")
        return np.ascontiguousarray(result).copy()

    def close(self) -> None:
        self._arrays.clear()


class RawV360PairProjector:
    """Project both views through one persistent, bounded libavfilter graph.

    PyAV exposes the same FFmpeg ``v360``/``split``/``hstack`` filters without
    an intermediate codec or a subprocess pipe.  Pushing one decoded panorama
    produces one stacked pair immediately, so repeated operation has no
    process-start overhead and no first-frame pipe buffering.
    """

    def __init__(
        self,
        *,
        projection_size: int | None = None,
        left_yaw: float = -90.0,
        right_yaw: float = 90.0,
        horizontal_fov: float = 90.0,
        vertical_fov: float = 90.0,
        filter_threads: int = 4,
    ) -> None:
        if projection_size is not None and projection_size <= 0:
            raise ValueError("projection_size must be positive")
        if filter_threads < 1 or filter_threads > 8:
            raise ValueError("filter_threads must be in [1, 8]")
        self.configured_projection_size = projection_size
        self.left_yaw = float(left_yaw)
        self.right_yaw = float(right_yaw)
        self.horizontal_fov = float(horizontal_fov)
        self.vertical_fov = float(vertical_fov)
        self.filter_threads = int(filter_threads)
        self._graph_object: Any | None = None
        self._source_filter: Any | None = None
        self._sink_filter: Any | None = None
        self._input_shape: tuple[int, int] | None = None
        self._active_projection_size: int | None = None
        self._next_pts = 0
        self.process_start_count = 0
        self.frames_processed = 0

    @staticmethod
    def automatic_projection_size(width: int, height: int) -> int:
        return max(1, min(int(round(width / 4)), int(round(height / 2))))

    def _graph(self, projection_size: int) -> str:
        common = (
            "v360=input=equirect:output=flat:interp=line:"
            f"pitch=0:roll=0:h_fov={self.horizontal_fov}:v_fov={self.vertical_fov}:"
            f"w={projection_size}:h={projection_size}"
        )
        return (
            "[0:v]split=2[left_in][right_in];"
            f"[left_in]{common}:yaw={self.left_yaw}[left];"
            f"[right_in]{common}:yaw={self.right_yaw}[right];"
            "[left][right]hstack=inputs=2[out]"
        )

    def _v360_args(self, projection_size: int, yaw: float) -> str:
        return (
            "input=equirect:output=flat:interp=line:"
            f"pitch=0:roll=0:h_fov={self.horizontal_fov}:v_fov={self.vertical_fov}:"
            f"w={projection_size}:h={projection_size}:yaw={yaw}"
        )

    def _start(self, width: int, height: int) -> None:
        self.close()
        try:
            import av
        except ImportError as exc:
            raise RuntimeError("PyAV is required for the persistent v360 filter graph") from exc
        from fractions import Fraction as _Fraction

        projection_size = self.configured_projection_size or self.automatic_projection_size(
            width, height
        )
        template = av.VideoFrame(width, height, "rgb24")
        template.pts = 0
        template.time_base = _Fraction(1, 1)
        graph = av.filter.Graph()
        graph.threads = self.filter_threads
        source = graph.add_buffer(template=template)
        split = graph.add("split", "2")
        left = graph.add("v360", self._v360_args(projection_size, self.left_yaw))
        right = graph.add("v360", self._v360_args(projection_size, self.right_yaw))
        stack = graph.add("hstack", "inputs=2")
        sink = graph.add("buffersink")
        source.link_to(split)
        split.link_to(left, 0, 0)
        split.link_to(right, 1, 0)
        left.link_to(stack, 0, 0)
        right.link_to(stack, 0, 1)
        stack.link_to(sink)
        graph.configure()
        self._graph_object = graph
        self._source_filter = source
        self._sink_filter = sink
        self._input_shape = (height, width)
        self._active_projection_size = projection_size
        self._next_pts = 0
        self.process_start_count += 1

    def project(self, panorama_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        image = np.asarray(panorama_rgb)
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"expected RGB uint8 panorama, got {image.shape} {image.dtype}")
        height, width = image.shape[:2]
        if self._graph_object is None or self._input_shape != (height, width):
            self._start(width, height)
        try:
            import av

            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(image), format="rgb24")
            frame.pts = self._next_pts
            frame.time_base = Fraction(1, 1)
            self._next_pts += 1
            assert self._source_filter is not None and self._sink_filter is not None
            self._source_filter.push(frame)
            output_frame = self._sink_filter.pull()
            stacked = output_frame.to_ndarray(format="rgb24")
        except Exception as exc:
            raise FrameDecodeError(f"persistent v360 graph failed: {exc}") from exc
        projection_size = int(self._active_projection_size or 0)
        if stacked.shape != (projection_size, projection_size * 2, 3):
            raise FrameDecodeError(
                f"v360 output shape changed: {stacked.shape} != "
                f"{(projection_size, projection_size * 2, 3)}"
            )
        self.frames_processed += 1
        return (
            np.ascontiguousarray(stacked[:, :projection_size]).copy(),
            np.ascontiguousarray(stacked[:, projection_size:]).copy(),
        )

    def close(self) -> None:
        self._graph_object = None
        self._source_filter = None
        self._sink_filter = None
        self._input_shape = None
        self._active_projection_size = None
        self._next_pts = 0


@dataclass(frozen=True)
class DirectOpenPIFrame:
    frame_index: int
    front: np.ndarray
    left: np.ndarray
    right: np.ndarray

    def copy(self) -> "DirectOpenPIFrame":
        return DirectOpenPIFrame(
            frame_index=self.frame_index,
            front=self.front.copy(),
            left=self.left.copy(),
            right=self.right.copy(),
        )

    def as_dict(self) -> dict[str, np.ndarray]:
        return {
            "front": self.front,
            "left": self.left,
            "right": self.right,
        }


class DirectOpenPIImageDecoder:
    """Bounded iterator for direct 224x224 front/left/right RGB arrays.

    The panorama may be a continuously encoded MediaSDK H.265 chapter cache.
    When `_03` already produced the exact active front frames, reuse that
    sparse cache and never decode the raw INSV a second time for the front
    view.
    """

    def __init__(
        self,
        plan: DirectOpenPIImagePlan,
        *,
        seek_threshold_frames: int = 120,
        decode_threads_per_reader: int = 1,
        resize_workers: int = 3,
        v360_filter_threads: int = 4,
        repeat_cache_frames: int = 8,
        verify_source_samples: bool = True,
        front_cache_manifest: Path | None = None,
        verify_front_cache_full_hash: bool = True,
    ) -> None:
        if resize_workers < 1 or resize_workers > 8:
            raise ValueError("resize_workers must be in [1, 8]")
        if repeat_cache_frames < 0 or repeat_cache_frames > 256:
            raise ValueError("repeat_cache_frames must be in [0, 256]")
        plan.verify_sources(verify_samples=verify_source_samples)
        self.plan = plan
        self.front_cache = (
            None
            if front_cache_manifest is None
            else FrontOpenPICacheReader(
                front_cache_manifest,
                verify_full_hash=verify_front_cache_full_hash,
            )
        )
        self.front_reader = (
            ChapteredVideoReader(
                plan.front_chapters,
                seek_threshold_frames=seek_threshold_frames,
                decode_threads=decode_threads_per_reader,
            )
            if self.front_cache is None
            else None
        )
        self.panorama_reader = ChapteredVideoReader(
            plan.panorama_chapters,
            seek_threshold_frames=seek_threshold_frames,
            decode_threads=decode_threads_per_reader,
        )
        self.projector = RawV360PairProjector(
            projection_size=plan.projection_size,
            left_yaw=plan.left_yaw,
            right_yaw=plan.right_yaw,
            horizontal_fov=plan.horizontal_fov,
            vertical_fov=plan.vertical_fov,
            filter_threads=v360_filter_threads,
        )
        self._decode_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="direct-openpi-decode"
        )
        self._resize_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=resize_workers, thread_name_prefix="direct-openpi-resize"
        )
        self._repeat_cache_frames = int(repeat_cache_frames)
        self._cache: OrderedDict[int, DirectOpenPIFrame] = OrderedDict()
        self._closed = False

    def _resize(self, image: np.ndarray) -> np.ndarray:
        return openpi_resize_with_pad(
            image, height=self.plan.target_height, width=self.plan.target_width
        )

    def read(self, frame_index: int) -> DirectOpenPIFrame:
        if self._closed:
            raise RuntimeError("direct image decoder is closed")
        frame_index = int(frame_index)
        cached = self._cache.get(frame_index)
        if cached is not None:
            self._cache.move_to_end(frame_index)
            return cached.copy()
        front_source = self.front_cache or self.front_reader
        assert front_source is not None
        front_future = self._decode_pool.submit(front_source.read, frame_index)
        panorama_future = self._decode_pool.submit(self.panorama_reader.read, frame_index)
        front_rgb = front_future.result()
        panorama_rgb = panorama_future.result()
        left_native, right_native = self.projector.project(panorama_rgb)
        futures = [
            self._resize_pool.submit(self._resize, image)
            for image in (front_rgb, left_native, right_native)
        ]
        front, left, right = (future.result() for future in futures)
        result = DirectOpenPIFrame(
            frame_index=frame_index,
            front=front,
            left=left,
            right=right,
        )
        if self._repeat_cache_frames:
            self._cache[frame_index] = result.copy()
            self._cache.move_to_end(frame_index)
            while len(self._cache) > self._repeat_cache_frames:
                self._cache.popitem(last=False)
        return result

    def iter_frames(self, absolute_frame_indices: Iterable[int]) -> Iterator[DirectOpenPIFrame]:
        """Yield in caller order without materializing or prefetching the iterable."""

        for frame_index in absolute_frame_indices:
            yield self.read(int(frame_index))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.front_reader is not None:
            self.front_reader.close()
        if self.front_cache is not None:
            self.front_cache.close()
        self.panorama_reader.close()
        self.projector.close()
        self._decode_pool.shutdown(wait=True, cancel_futures=True)
        self._resize_pool.shutdown(wait=True, cancel_futures=True)
        self._cache.clear()

    def __enter__(self) -> "DirectOpenPIImageDecoder":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def iter_index_chunks(
    absolute_frame_indices: Iterable[int], chunk_size: int
) -> Iterator[tuple[str, tuple[int, ...]]]:
    """Boundedly group indices while preserving order and duplicates."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunk: list[int] = []
    chunk_index = 0
    for value in absolute_frame_indices:
        chunk.append(int(value))
        if len(chunk) == chunk_size:
            yield f"chunk_{chunk_index:06d}", tuple(chunk)
            chunk_index += 1
            chunk.clear()
    if chunk:
        yield f"chunk_{chunk_index:06d}", tuple(chunk)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + f".partial.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with partial.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
        _fsync_directory(path.parent)
    finally:
        partial.unlink(missing_ok=True)


def _artifact_identity(path: Path) -> dict[str, Any]:
    before = source_identity(path)
    digest = _sha256_file(path)
    after = source_identity(path)
    if before != after:
        raise CheckpointValidationError(f"artifact changed while being hashed: {path}")
    after["sha256"] = digest
    return after


def _request_digest(indices: Sequence[int]) -> str:
    return hashlib.sha256(_canonical_json([int(value) for value in indices])).hexdigest()


class AtomicChunkCheckpointStore:
    """Atomic commit markers bound to a direct-image plan and ordered indices."""

    def __init__(
        self,
        root: Path,
        plan: DirectOpenPIImagePlan | DirectRawOpenPIImagePlan,
        *,
        verify_source_samples: bool = True,
    ) -> None:
        plan.verify_sources(verify_samples=verify_source_samples)
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.plan = plan

    def _path(self, chunk_key: str) -> Path:
        if not _SAFE_CHUNK_KEY.fullmatch(chunk_key):
            raise ValueError(f"unsafe chunk key: {chunk_key!r}")
        return self.root / f"{chunk_key}.json"

    def commit(
        self,
        chunk_key: str,
        absolute_frame_indices: Sequence[int],
        artifacts: Mapping[str, Path],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        if not absolute_frame_indices:
            raise ValueError("cannot checkpoint an empty chunk")
        if not artifacts:
            raise ValueError("at least one committed artifact is required")
        # Fast stat verification on every commit.  The sampled identities were
        # already verified when this store was constructed.
        self.plan.verify_sources(verify_samples=False)
        artifact_records: dict[str, dict[str, Any]] = {}
        for name, raw_path in sorted(artifacts.items()):
            if not name or "/" in name or "\\" in name:
                raise ValueError(f"invalid artifact name: {name!r}")
            path = raw_path.expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"chunk artifact is not a regular file: {path}")
            artifact_records[name] = _artifact_identity(path)
        indices = tuple(int(value) for value in absolute_frame_indices)
        payload: dict[str, Any] = {
            "schema": CHECKPOINT_SCHEMA,
            "chunk_key": chunk_key,
            "plan_hash": self.plan.plan_hash,
            "frame_indices": list(indices),
            "frame_indices_sha256": _request_digest(indices),
            "frame_count": len(indices),
            "artifacts": artifact_records,
            "metadata": dict(metadata or {}),
        }
        path = self._path(chunk_key)
        _atomic_write_json(path, payload)
        return path

    def load(
        self,
        chunk_key: str,
        absolute_frame_indices: Sequence[int],
        *,
        expected_artifacts: Mapping[str, Path] | None = None,
        verify_full_artifact_hash: bool = False,
    ) -> dict[str, Any]:
        self.plan.verify_sources(verify_samples=False)
        path = self._path(chunk_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointValidationError(f"checkpoint missing or unreadable: {path}") from exc
        indices = tuple(int(value) for value in absolute_frame_indices)
        if payload.get("schema") != CHECKPOINT_SCHEMA:
            raise CheckpointValidationError(f"checkpoint schema mismatch: {path}")
        if payload.get("chunk_key") != chunk_key:
            raise CheckpointValidationError(f"checkpoint key mismatch: {path}")
        if payload.get("plan_hash") != self.plan.plan_hash:
            raise CheckpointValidationError(f"checkpoint source/recipe plan mismatch: {path}")
        if payload.get("frame_indices") != list(indices):
            raise CheckpointValidationError(f"checkpoint frame order changed: {path}")
        if payload.get("frame_indices_sha256") != _request_digest(indices):
            raise CheckpointValidationError(f"checkpoint frame digest mismatch: {path}")
        records = payload.get("artifacts")
        if not isinstance(records, dict) or not records:
            raise CheckpointValidationError(f"checkpoint artifact identities missing: {path}")
        expected = dict(expected_artifacts or {})
        if expected and set(expected) != set(records):
            raise CheckpointValidationError(f"checkpoint artifact set changed: {path}")
        for name, identity in records.items():
            if not isinstance(identity, dict):
                raise CheckpointValidationError(f"invalid artifact identity {name}: {path}")
            artifact_path = verify_source_identity(identity, verify_samples=True)
            if name in expected and artifact_path != expected[name].expanduser().resolve():
                raise CheckpointValidationError(
                    f"checkpoint artifact path changed for {name}: {artifact_path}"
                )
            if not identity.get("sha256"):
                raise CheckpointValidationError(f"artifact SHA-256 missing for {name}: {path}")
            if verify_full_artifact_hash and _sha256_file(artifact_path) != identity["sha256"]:
                raise CheckpointValidationError(f"artifact SHA-256 changed for {name}: {path}")
        return payload

    def is_complete(
        self,
        chunk_key: str,
        absolute_frame_indices: Sequence[int],
        *,
        expected_artifacts: Mapping[str, Path] | None = None,
        verify_full_artifact_hash: bool = False,
    ) -> bool:
        try:
            self.load(
                chunk_key,
                absolute_frame_indices,
                expected_artifacts=expected_artifacts,
                verify_full_artifact_hash=verify_full_artifact_hash,
            )
        except (CheckpointValidationError, SourceValidationError, OSError, ValueError):
            return False
        return True


@dataclass(frozen=True)
class DirectRawOpenPIImagePlan:
    """Source/recipe plan for direct MediaSDK PNG batches from raw INSV."""

    insv_chapters: tuple[VideoChapter, ...]
    mediasdk_exporter: dict[str, Any]
    model_provenance: dict[str, Any]
    runtime_provenance: dict[str, Any]
    target_height: int = 224
    target_width: int = 224
    panorama_width: int = DEFAULT_OPENPI_PANORAMA_WIDTH
    panorama_height: int = DEFAULT_OPENPI_PANORAMA_HEIGHT
    projection_size: int | None = None
    left_yaw: float = -90.0
    right_yaw: float = 90.0
    horizontal_fov: float = 90.0
    vertical_fov: float = 90.0
    stitch_type: str = "optflow"
    flowstate: bool = False
    direction_lock: bool = False
    stitch_fusion: bool = False
    cuda: bool = True
    timeout_sec: int = 600
    model_dir: str | None = None

    @classmethod
    def build(
        cls,
        insv_paths: Sequence[Path],
        *,
        mediasdk_exporter: Path = Path(__file__).resolve().parent
        / "_01_raw_extrect/insta360_pano_export/build/insv_to_png_sequence",
        target_height: int = 224,
        target_width: int = 224,
        panorama_width: int = DEFAULT_OPENPI_PANORAMA_WIDTH,
        panorama_height: int = DEFAULT_OPENPI_PANORAMA_HEIGHT,
        projection_size: int | None = None,
        left_yaw: float = -90.0,
        right_yaw: float = 90.0,
        horizontal_fov: float = 90.0,
        vertical_fov: float = 90.0,
        stitch_type: str = "optflow",
        flowstate: bool = False,
        direction_lock: bool = False,
        stitch_fusion: bool = False,
        cuda: bool = True,
        timeout_sec: int = 600,
        model_dir: Path | None = None,
        ffprobe_bin: str = "ffprobe",
    ) -> "DirectRawOpenPIImagePlan":
        if target_height <= 0 or target_width <= 0:
            raise ValueError("target dimensions must be positive")
        if panorama_width <= 0 or panorama_height <= 0 or panorama_width != 2 * panorama_height:
            raise ValueError("MediaSDK panorama dimensions must be positive and 2:1")
        if projection_size is not None and projection_size <= 0:
            raise ValueError("projection_size must be positive")
        if stitch_type not in {"template", "dynamic", "optflow", "aiflow"}:
            raise ValueError(f"unsupported MediaSDK stitch type: {stitch_type}")
        if direction_lock and not flowstate:
            raise ValueError("direction_lock requires flowstate")
        if timeout_sec < 0:
            raise ValueError("timeout_sec must be non-negative")
        exporter = mediasdk_exporter.expanduser().resolve(strict=True)
        if not exporter.is_file() or not os.access(exporter, os.X_OK):
            raise SourceValidationError(f"MediaSDK PNG exporter is not executable: {exporter}")
        exporter_identity = full_file_identity(exporter)
        model_identity = _model_provenance(model_dir)
        resolved_model = (
            str(model_identity["path"])
            if model_identity["mode"] == "explicit_recursive_full_sha256"
            else None
        )
        runtime_identity = _capture_raw_runtime_provenance(
            exporter, cuda_requested=bool(cuda)
        )
        chapters = _build_chapters(insv_paths, ffprobe_bin)
        return cls(
            insv_chapters=chapters,
            mediasdk_exporter=exporter_identity,
            model_provenance=model_identity,
            runtime_provenance=runtime_identity,
            target_height=int(target_height),
            target_width=int(target_width),
            panorama_width=int(panorama_width),
            panorama_height=int(panorama_height),
            projection_size=int(projection_size) if projection_size is not None else None,
            left_yaw=float(left_yaw),
            right_yaw=float(right_yaw),
            horizontal_fov=float(horizontal_fov),
            vertical_fov=float(vertical_fov),
            stitch_type=stitch_type,
            flowstate=bool(flowstate),
            direction_lock=bool(direction_lock),
            stitch_fusion=bool(stitch_fusion),
            cuda=bool(cuda),
            timeout_sec=int(timeout_sec),
            model_dir=resolved_model,
        )

    @property
    def total_frames(self) -> int:
        return self.insv_chapters[-1].global_end

    def payload(self) -> dict[str, Any]:
        return {
            "schema": RAW_PLAN_SCHEMA,
            "insv_chapters": [chapter.to_dict() for chapter in self.insv_chapters],
            "mediasdk_exporter": self.mediasdk_exporter,
            "model_provenance": self.model_provenance,
            "runtime_provenance": self.runtime_provenance,
            "recipe": {
                "front": "decode_insv_primary_stream_then_openpi_resize",
                "side": "bounded_mediasdk_png_then_" + SIDE_RECIPE,
                "resize": OPENPI_RESIZE_RECIPE,
                "target_height": self.target_height,
                "target_width": self.target_width,
                "panorama_width": self.panorama_width,
                "panorama_height": self.panorama_height,
                "projection_size": self.projection_size,
                "left_yaw": self.left_yaw,
                "right_yaw": self.right_yaw,
                "horizontal_fov": self.horizontal_fov,
                "vertical_fov": self.vertical_fov,
                "stitch_type": self.stitch_type,
                "flowstate": self.flowstate,
                "direction_lock": self.direction_lock,
                "stitch_fusion": self.stitch_fusion,
                "cuda": self.cuda,
                "timeout_sec": self.timeout_sec,
                "model_dir": self.model_dir,
                "model_dir_state": self.model_provenance.get("mode"),
                "full_resolution_png_retention": "bounded_batch_finally_delete",
            },
            "total_frames": self.total_frames,
        }

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload["plan_hash"] = self.plan_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DirectRawOpenPIImagePlan":
        if payload.get("schema") != RAW_PLAN_SCHEMA:
            raise SourceValidationError("direct raw image plan schema mismatch")
        recipe = payload.get("recipe") or {}
        plan = cls(
            insv_chapters=tuple(
                VideoChapter.from_dict(item) for item in payload.get("insv_chapters") or []
            ),
            mediasdk_exporter=dict(payload["mediasdk_exporter"]),
            model_provenance=dict(payload["model_provenance"]),
            runtime_provenance=dict(payload["runtime_provenance"]),
            target_height=int(recipe["target_height"]),
            target_width=int(recipe["target_width"]),
            panorama_width=int(recipe["panorama_width"]),
            panorama_height=int(recipe["panorama_height"]),
            projection_size=(
                int(recipe["projection_size"])
                if recipe.get("projection_size") is not None
                else None
            ),
            left_yaw=float(recipe["left_yaw"]),
            right_yaw=float(recipe["right_yaw"]),
            horizontal_fov=float(recipe["horizontal_fov"]),
            vertical_fov=float(recipe["vertical_fov"]),
            stitch_type=str(recipe["stitch_type"]),
            flowstate=bool(recipe["flowstate"]),
            direction_lock=bool(recipe["direction_lock"]),
            stitch_fusion=bool(recipe["stitch_fusion"]),
            cuda=bool(recipe["cuda"]),
            timeout_sec=int(recipe["timeout_sec"]),
            model_dir=str(recipe["model_dir"]) if recipe.get("model_dir") else None,
        )
        if not plan.insv_chapters:
            raise SourceValidationError("direct raw image plan has no INSV chapters")
        if payload.get("plan_hash") != plan.plan_hash:
            raise SourceValidationError("direct raw image plan hash mismatch")
        plan.verify_sources(verify_samples=True)
        return plan

    @classmethod
    def load(cls, path: Path) -> "DirectRawOpenPIImagePlan":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write(self, path: Path) -> None:
        _atomic_write_json(path, self.to_dict())

    def verify_sources(self, verify_samples: bool = False) -> None:
        expected_start = 0
        for chapter in self.insv_chapters:
            if chapter.global_start != expected_start:
                raise SourceValidationError(f"INSV chapter continuity mismatch: {chapter.path}")
            actual = verify_source_identity(chapter.identity, verify_samples=verify_samples)
            if actual != chapter.path:
                raise SourceValidationError(f"INSV chapter path mismatch: {actual} != {chapter.path}")
            expected_start = chapter.global_end
        exporter = verify_full_file_identity(self.mediasdk_exporter)
        expected_model = self.model_provenance
        if expected_model.get("mode") == "explicit_recursive_full_sha256":
            if self.model_dir != expected_model.get("path"):
                raise SourceValidationError("model_dir does not match explicit model provenance")
            current_model = _model_tree_snapshot(Path(str(expected_model.get("path"))))
        elif expected_model.get("mode") == "unset_sdk_default":
            if self.model_dir is not None:
                raise SourceValidationError("unset SDK-default model state has a model_dir")
            current_model = _model_provenance(None)
        else:
            raise SourceValidationError("invalid MediaSDK model provenance mode")
        if current_model != expected_model:
            raise SourceValidationError("MediaSDK model tree provenance changed")
        _verify_runtime_provenance(
            self.runtime_provenance,
            exporter,
            cuda_requested=self.cuda,
        )


def _sdk_output_index(path: Path) -> int | None:
    return int(path.stem) if path.stem.isdigit() else None


@dataclass(frozen=True)
class _MediaSDKExportJob:
    chapter_index: int
    part_index: int
    chapter: VideoChapter
    pairs: tuple[tuple[int, int], ...]
    output_dir: Path

    @property
    def local_indices(self) -> tuple[int, ...]:
        return tuple(local_index for _global_index, local_index in self.pairs)


@dataclass(frozen=True)
class _CompletedMediaSDKBatch:
    """A validated PNG batch whose directory is owned by one provider."""

    batch_root: Path
    paths: dict[int, Path]


class MediaSDKPNGSideProvider:
    """Bounded, transient MediaSDK panorama PNG provider for raw INSV chapters."""

    def __init__(
        self,
        plan: DirectRawOpenPIImagePlan,
        *,
        work_root: Path | None = None,
        fallback_work_root: Path | None = None,
        export_workers: int = 4,
        max_batch_frames: int = 512,
        free_space_margin_bytes: int = 512 * 1024 * 1024,
        stale_grace_seconds: int = 120,
        gpu_admission: SharedMediaSDKGPUAdmission | None = None,
    ) -> None:
        if export_workers < 1 or export_workers > 8:
            raise ValueError("export_workers must be in [1, 8]")
        if max_batch_frames < 1 or max_batch_frames > 1024:
            raise ValueError("max_batch_frames must be in [1, 1024]")
        if free_space_margin_bytes < 0:
            raise ValueError("free_space_margin_bytes must be non-negative")
        if stale_grace_seconds < 0:
            raise ValueError("stale_grace_seconds must be non-negative")
        plan.verify_sources(verify_samples=True)
        self.plan = plan
        if work_root is None:
            shared = Path("/dev/shm")
            if shared.is_dir() and os.access(shared, os.W_OK):
                work_root = shared / "instaumi-direct-openpi"
            elif fallback_work_root is not None:
                work_root = fallback_work_root
            else:
                raise FrameDecodeError(
                    "/dev/shm is unavailable; provide fallback_work_root on fast NVMe storage"
                )
        assert work_root is not None
        self.work_root = work_root.expanduser().resolve()
        self.fallback_work_root = (
            fallback_work_root.expanduser().resolve()
            if fallback_work_root is not None
            else None
        )
        roots = [self.work_root]
        if self.fallback_work_root is not None and self.fallback_work_root != self.work_root:
            roots.append(self.fallback_work_root)
        self._work_roots = tuple(roots)
        self.export_workers = int(export_workers)
        self.max_batch_frames = int(max_batch_frames)
        self.free_space_margin_bytes = int(free_space_margin_bytes)
        self.stale_grace_seconds = int(stale_grace_seconds)
        self.gpu_admission = gpu_admission if plan.cuda else None
        self._boot_id = _current_boot_id()
        self._owner_pid = os.getpid()
        owner_start = _process_start_time_ticks(self._owner_pid)
        if owner_start is None:
            raise SourceValidationError("could not read current process starttime")
        self._owner_start_time_ticks = owner_start
        self._starts = tuple(chapter.global_start for chapter in plan.insv_chapters)
        self._active_directories: set[Path] = set()
        self._active_directories_lock = threading.Lock()
        self._export_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.export_workers,
            thread_name_prefix="mediasdk-png-export",
        )
        self._closed = False
        for root in self._work_roots:
            try:
                with _work_root_lock(root):
                    self._cleanup_stale_batches_locked(root)
            except OSError:
                # The fallback selector will report inaccessible roots together
                # with their free-space failure when a batch is requested.
                continue

    def _required_work_bytes(self, frame_count: int) -> int:
        # Four bytes/pixel covers RGB/RGBA PNG scanlines before compression;
        # five percent covers PNG/filter/container overhead for incompressible data.
        pixel_bytes = (
            int(frame_count)
            * self.plan.panorama_width
            * self.plan.panorama_height
            * 4
        )
        return pixel_bytes + max(64 * 1024 * 1024, pixel_bytes // 20) + self.free_space_margin_bytes

    def _cleanup_stale_batches_locked(self, root: Path) -> None:
        current_boot_short = self._boot_id[:12]
        now_boottime = time.clock_gettime(time.CLOCK_BOOTTIME)
        now_wall = time.time()
        for candidate in sorted(root.iterdir()):
            match = _RAW_BATCH_NAME.fullmatch(candidate.name)
            if match is None:
                continue
            candidate_plan = match.group("plan")
            candidate_stat = candidate.lstat()
            if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode):
                raise SourceValidationError(
                    f"unsafe raw batch entry in work root: {candidate}"
                )
            manifest_path = candidate / _OWNER_MANIFEST_NAME
            manifest: dict[str, Any] | None = None
            if manifest_path.exists():
                manifest_stat = manifest_path.lstat()
                if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(
                    manifest_stat.st_mode
                ):
                    raise SourceValidationError(f"unsafe raw batch owner manifest: {manifest_path}")
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        manifest = loaded
                except (OSError, json.JSONDecodeError):
                    manifest = None

            name_boot = match.group("boot")
            if manifest is not None:
                manifest_boot = str(manifest.get("boot_id", ""))
                if (
                    manifest.get("schema") != RAW_OWNER_SCHEMA
                    or manifest.get("plan_hash") != candidate_plan
                    or manifest_boot[:12] != name_boot
                ):
                    # Ambiguous ownership is preserved rather than guessed.
                    continue
                owner_boot = manifest_boot
                owner_pid = int(manifest.get("owner_pid", -1))
                owner_start = int(manifest.get("owner_start_time_ticks", -1))
            else:
                owner_boot = name_boot
                owner_pid = int(match.group("pid"))
                owner_start = int(match.group("start"))

            if owner_boot != self._boot_id and owner_boot != current_boot_short:
                shutil.rmtree(candidate)
                continue
            if _process_start_time_ticks(owner_pid) == owner_start:
                continue

            if manifest is not None:
                created_boottime = float(manifest.get("created_boottime_seconds", now_boottime))
                age_seconds = now_boottime - created_boottime
                timeout_seconds = max(0, int(manifest.get("timeout_sec", self.plan.timeout_sec)))
                grace_seconds = max(
                    self.stale_grace_seconds,
                    max(0, int(manifest.get("cleanup_grace_seconds", 0))),
                )
            else:
                age_seconds = now_wall - candidate_stat.st_mtime
                timeout_seconds = max(0, self.plan.timeout_sec)
                grace_seconds = self.stale_grace_seconds
            if age_seconds < 0 or age_seconds < timeout_seconds + grace_seconds:
                continue
            child_state = _batch_has_live_output_process(candidate)
            if child_state is not False:
                continue
            shutil.rmtree(candidate)

    def _create_owned_batch_root(self, root: Path) -> Path:
        with _work_root_lock(root):
            self._cleanup_stale_batches_locked(root)
            name = (
                f"raw-{self.plan.plan_hash}-b{self._boot_id[:12]}-"
                f"p{self._owner_pid}-s{self._owner_start_time_ticks}-{uuid.uuid4().hex}"
            )
            batch_root = root / name
            batch_root.mkdir(mode=0o700)
            try:
                _atomic_write_json(
                    batch_root / _OWNER_MANIFEST_NAME,
                    {
                        "schema": RAW_OWNER_SCHEMA,
                        "plan_hash": self.plan.plan_hash,
                        "boot_id": self._boot_id,
                        "owner_pid": self._owner_pid,
                        "owner_start_time_ticks": self._owner_start_time_ticks,
                        "created_wall_time_ns": time.time_ns(),
                        "created_boottime_seconds": time.clock_gettime(time.CLOCK_BOOTTIME),
                        "timeout_sec": self.plan.timeout_sec,
                        "cleanup_grace_seconds": self.stale_grace_seconds,
                    },
                )
            except BaseException:
                shutil.rmtree(batch_root, ignore_errors=True)
                raise
            return batch_root

    def _remove_owned_batch_root(self, batch_root: Path) -> None:
        with _work_root_lock(batch_root.parent):
            try:
                shutil.rmtree(batch_root)
            except FileNotFoundError:
                pass

    def _select_work_root(self, frame_count: int) -> Path:
        required = self._required_work_bytes(frame_count)
        failures: list[str] = []
        for root in self._work_roots:
            try:
                with _work_root_lock(root):
                    self._cleanup_stale_batches_locked(root)
                    free = shutil.disk_usage(root).free
            except OSError as exc:
                failures.append(f"{root}: unavailable ({exc})")
                continue
            if free >= required:
                return root
            failures.append(f"{root}: free={free} required={required}")
        raise FrameDecodeError(
            "insufficient temporary space for bounded MediaSDK PNG batch; "
            "provide fallback_work_root on fast NVMe storage: " + "; ".join(failures)
        )

    def _locate(self, global_index: int) -> tuple[int, VideoChapter, int]:
        if global_index < 0 or global_index >= self.plan.total_frames:
            raise IndexError(
                f"absolute frame index {global_index} outside [0, {self.plan.total_frames})"
            )
        chapter_index = bisect.bisect_right(self._starts, global_index) - 1
        chapter = self.plan.insv_chapters[chapter_index]
        return chapter_index, chapter, global_index - chapter.global_start

    def _build_jobs(
        self,
        grouped: Mapping[int, Sequence[tuple[int, int]]],
        batch_root: Path,
    ) -> tuple[_MediaSDKExportJob, ...]:
        chapter_indices = sorted(grouped)
        allocations = {chapter_index: 1 for chapter_index in chapter_indices}
        # In the common one-chapter batch this creates exactly export_workers
        # chronological slices.  At a chapter boundary worker slots are shared
        # proportionally; if a sparse request spans more chapters than workers,
        # one job/chapter is queued through the same bounded pool.
        while sum(allocations.values()) < self.export_workers:
            candidates = [
                chapter_index
                for chapter_index in chapter_indices
                if allocations[chapter_index] < len(grouped[chapter_index])
            ]
            if not candidates:
                break
            chapter_index = max(
                candidates,
                key=lambda value: (
                    len(grouped[value]) / allocations[value],
                    len(grouped[value]),
                    -value,
                ),
            )
            allocations[chapter_index] += 1

        jobs: list[_MediaSDKExportJob] = []
        for chapter_index in chapter_indices:
            ordered_pairs = sorted(grouped[chapter_index], key=lambda pair: pair[1])
            part_count = allocations[chapter_index]
            base_size, extra = divmod(len(ordered_pairs), part_count)
            cursor = 0
            for part_index in range(part_count):
                part_size = base_size + (1 if part_index < extra else 0)
                pairs = tuple(ordered_pairs[cursor : cursor + part_size])
                cursor += part_size
                output_dir = batch_root / (
                    f"chapter_{chapter_index:04d}_part_{part_index:04d}"
                )
                output_dir.mkdir()
                jobs.append(
                    _MediaSDKExportJob(
                        chapter_index=chapter_index,
                        part_index=part_index,
                        chapter=self.plan.insv_chapters[chapter_index],
                        pairs=pairs,
                        output_dir=output_dir,
                    )
                )
        return tuple(jobs)

    def _command(
        self,
        chapter: VideoChapter,
        output_dir: Path,
        local_indices: Sequence[int],
    ) -> list[str]:
        command = [
            str(verify_full_file_identity(self.plan.mediasdk_exporter)),
            "--input",
            str(chapter.path),
            "--output-dir",
            str(output_dir),
            "--frames",
            ",".join(str(value) for value in local_indices),
            "--width",
            str(self.plan.panorama_width),
            "--height",
            str(self.plan.panorama_height),
            "--flowstate",
            "on" if self.plan.flowstate else "off",
            "--direction-lock",
            "on" if self.plan.direction_lock else "off",
            "--stitch-type",
            self.plan.stitch_type,
            "--stitch-fusion",
            "on" if self.plan.stitch_fusion else "off",
            "--cuda",
            "on" if self.plan.cuda else "off",
            "--timeout-sec",
            str(self.plan.timeout_sec),
        ]
        if self.plan.model_dir is not None:
            command += ["--model-dir", self.plan.model_dir]
        return command

    def _run_job(self, job: _MediaSDKExportJob) -> dict[int, Path]:
        local_indices = job.local_indices
        verify_source_identity(job.chapter.identity, verify_samples=False)
        runtime_env, _loader_record = _replay_loader_environment(
            self.plan.runtime_provenance.get("loader_environment") or {}
        )
        attempts = 1 + (self.gpu_admission.retry_count if self.gpu_admission else 0)
        completed: subprocess.CompletedProcess[str] | None = None
        combined_log = ""
        for attempt in range(1, attempts + 1):
            try:
                completed = self._run_job_process(
                    job,
                    self._command(job.chapter, job.output_dir, local_indices),
                    runtime_env,
                )
            except subprocess.TimeoutExpired as exc:
                raise FrameDecodeError(
                    f"MediaSDK PNG export process timed out for {job.chapter.path}"
                ) from exc
            except OSError as exc:
                raise FrameDecodeError(
                    f"MediaSDK PNG exporter could not start for {job.chapter.path}: {exc}"
                ) from exc
            combined_log = completed.stdout + "\n" + completed.stderr
            if completed.returncode == 0 or not self._is_cuda_oom(combined_log):
                break
            assert self.gpu_admission is not None
            limit, reduced = self.gpu_admission.report_cuda_oom()
            if attempt >= attempts:
                break
            self._reset_failed_job_output(job.output_dir)
            print(
                "[gpu-adaptive] "
                f"cuda_oom=1 retry={attempt}/{attempts - 1} "
                f"new_limit={limit} reduced={int(reduced)} "
                f"chapter={job.chapter_index} part={job.part_index}",
                flush=True,
            )
            time.sleep(0.5)
        assert completed is not None
        if completed.returncode != 0:
            raise FrameDecodeError(
                f"MediaSDK PNG export failed for {job.chapter.path} "
                f"(exit={completed.returncode}): {combined_log[-4000:]}"
            )
        if "progress=100%" not in combined_log or "[done]" not in combined_log:
            raise FrameDecodeError(
                "MediaSDK PNG exporter did not report a completed 100% batch: "
                f"{combined_log[-4000:]}"
            )
        verify_source_identity(job.chapter.identity, verify_samples=False)
        verify_full_file_identity(self.plan.mediasdk_exporter)
        mapped_local = self._validate_outputs(job.output_dir, local_indices)
        return {
            global_index: mapped_local[local_index]
            for global_index, local_index in job.pairs
        }

    @staticmethod
    def _is_cuda_oom(log: str) -> bool:
        normalized = log.lower()
        return any(
            marker in normalized
            for marker in (
                "cuda_error_out_of_memory",
                "cuda error 2 :out of memory",
                "cuda error 2: out of memory",
                "unknown out of memory",
            )
        )

    @staticmethod
    def _reset_failed_job_output(output_dir: Path) -> None:
        parent = output_dir.parent
        if output_dir.parent == output_dir or not parent.is_dir():
            raise FrameDecodeError(f"unsafe MediaSDK retry directory: {output_dir}")
        shutil.rmtree(output_dir)
        output_dir.mkdir()

    def _run_job_process(
        self,
        job: _MediaSDKExportJob,
        command: list[str],
        runtime_env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        gate = self.gpu_admission
        if gate is None:
            return subprocess.run(
                command,
                env=runtime_env,
                cwd="/",
                text=True,
                capture_output=True,
                timeout=(self.plan.timeout_sec + 60) if self.plan.timeout_sec else None,
            )

        active, limit = gate.acquire_job()
        startup_acquired = False
        process: subprocess.Popen[bytes] | None = None
        log_path = job.output_dir.parent / (
            f".{job.output_dir.name}.attempt-{uuid.uuid4().hex}.log"
        )
        try:
            startup_active = gate.acquire_startup()
            startup_acquired = True
            print(
                "[gpu-admission] "
                f"active={active}/{limit} startup={startup_active}/{gate.startup_limit} "
                f"chapter={job.chapter_index} part={job.part_index}",
                flush=True,
            )
            deadline = (
                time.monotonic() + self.plan.timeout_sec + 60
                if self.plan.timeout_sec
                else None
            )
            progress_seen = -1
            scan_offset = 0
            combined = bytearray()
            with log_path.open("w+b") as log_handle:
                process = subprocess.Popen(
                    command,
                    env=dict(runtime_env),
                    cwd="/",
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                while True:
                    returncode = process.poll()
                    log_handle.flush()
                    log_handle.seek(scan_offset)
                    block = log_handle.read()
                    if block:
                        scan_offset += len(block)
                        combined.extend(block)
                        for match in re.finditer(rb"\[stitch-png\] progress=(\d+)%", block):
                            progress_seen = max(progress_seen, int(match.group(1)))
                    if startup_acquired and progress_seen >= gate.startup_progress:
                        gate.release_startup()
                        startup_acquired = False
                    if returncode is not None:
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise subprocess.TimeoutExpired(command, self.plan.timeout_sec + 60)
                    time.sleep(0.1)
                log_handle.seek(scan_offset)
                combined.extend(log_handle.read())
            return subprocess.CompletedProcess(
                command,
                int(process.returncode),
                combined.decode("utf-8", errors="replace"),
                "",
            )
        finally:
            if startup_acquired:
                gate.release_startup()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            gate.release_job()
            log_path.unlink(missing_ok=True)

    def _validate_outputs(
        self, output_dir: Path, local_indices: Sequence[int]
    ) -> dict[int, Path]:
        entries = sorted(output_dir.iterdir())
        unexpected_directories = [path.name for path in entries if not path.is_file()]
        if unexpected_directories:
            raise FrameDecodeError(
                f"MediaSDK batch contains unexpected directories: {unexpected_directories}"
            )
        all_files = entries
        png_files = [path for path in all_files if path.suffix.lower() == ".png"]
        if len(all_files) != len(png_files):
            extras = [str(path.relative_to(output_dir)) for path in all_files if path not in png_files]
            raise FrameDecodeError(f"MediaSDK batch contains unexpected non-PNG files: {extras}")
        if len(png_files) != len(local_indices):
            raise FrameDecodeError(
                f"MediaSDK PNG count mismatch: got {len(png_files)}, expected {len(local_indices)}"
            )
        mapped: dict[int, Path] = {}
        for path in png_files:
            index = _sdk_output_index(path)
            if index is None or index in mapped:
                raise FrameDecodeError(
                    "MediaSDK PNG filename indices do not match requested "
                    f"chapter-local frames: invalid or duplicate name {path.name!r}"
                )
            mapped[index] = path
        expected = {int(value) for value in local_indices}
        if set(mapped) != expected:
            raise FrameDecodeError(
                "MediaSDK PNG filename indices do not match requested chapter-local frames: "
                f"got={sorted(mapped)} expected={sorted(expected)}"
            )
        for local_index, path in mapped.items():
            try:
                # Validate the complete PNG container and every chunk CRC here.
                # The consuming PIL load immediately afterwards is the single
                # authoritative IDAT decode, so avoid inflating every 4K frame
                # twice while retaining fail-closed structural validation.
                details = inspect_png(path, verify_decoded_stream=False)
            except ArtifactError as exc:
                raise FrameDecodeError(
                    f"MediaSDK PNG validation failed for local frame {local_index}: {exc}"
                ) from exc
            if (
                int(details["width"]) != self.plan.panorama_width
                or int(details["height"]) != self.plan.panorama_height
            ):
                raise FrameDecodeError(
                    f"MediaSDK PNG geometry mismatch for local frame {local_index}: "
                    f"{details['width']}x{details['height']} != "
                    f"{self.plan.panorama_width}x{self.plan.panorama_height}"
                )
        return mapped

    def export_batch(self, absolute_frame_indices: Sequence[int]) -> _CompletedMediaSDKBatch:
        """Export and validate a batch while retaining ownership until release."""

        if self._closed:
            raise RuntimeError("MediaSDK PNG provider is closed")
        ordered_unique = tuple(dict.fromkeys(int(value) for value in absolute_frame_indices))
        if not ordered_unique:
            raise ValueError("MediaSDK export batch must not be empty")
        if len(ordered_unique) > self.max_batch_frames:
            raise ValueError(
                f"MediaSDK batch has {len(ordered_unique)} unique frames; "
                f"maximum is {self.max_batch_frames}"
            )
        grouped: dict[int, list[tuple[int, int]]] = {}
        for global_index in ordered_unique:
            chapter_index, _chapter, local_index = self._locate(global_index)
            grouped.setdefault(chapter_index, []).append((global_index, local_index))
        selected_work_root = self._select_work_root(len(ordered_unique))
        batch_root = self._create_owned_batch_root(selected_work_root)
        with self._active_directories_lock:
            self._active_directories.add(batch_root)
        mapped_global: dict[int, Path] = {}
        try:
            jobs = self._build_jobs(grouped, batch_root)
            futures: list[concurrent.futures.Future[dict[int, Path]]] = []
            try:
                for job in jobs:
                    futures.append(self._export_pool.submit(self._run_job, job))
                for future in concurrent.futures.as_completed(futures):
                    job_mapping = future.result()
                    overlap = set(mapped_global).intersection(job_mapping)
                    if overlap:
                        raise FrameDecodeError(
                            "MediaSDK parallel jobs produced duplicate global indices: "
                            f"{sorted(overlap)}"
                        )
                    mapped_global.update(job_mapping)
            except BaseException:
                for future in futures:
                    future.cancel()
                concurrent.futures.wait(futures)
                raise
            if set(mapped_global) != set(ordered_unique):
                raise FrameDecodeError("MediaSDK batch global/source mapping is incomplete")
            return _CompletedMediaSDKBatch(batch_root=batch_root, paths=mapped_global)
        except BaseException:
            try:
                self._remove_owned_batch_root(batch_root)
            except OSError:
                # Keep failed cleanup registered so close() can retry and the
                # next process can reclaim it through the owner manifest.
                raise
            else:
                with self._active_directories_lock:
                    self._active_directories.discard(batch_root)
            raise

    def release_batch(self, batch: _CompletedMediaSDKBatch) -> None:
        """Delete one successfully exported batch exactly once."""

        with self._active_directories_lock:
            owned = batch.batch_root in self._active_directories
        if not owned:
            raise RuntimeError(f"MediaSDK PNG batch is not active: {batch.batch_root}")
        self._remove_owned_batch_root(batch.batch_root)
        with self._active_directories_lock:
            self._active_directories.discard(batch.batch_root)

    @contextmanager
    def exported_batch(self, absolute_frame_indices: Sequence[int]) -> Iterator[dict[int, Path]]:
        """Yield validated temporary PNG paths and always delete the whole batch."""

        ordered_unique = tuple(dict.fromkeys(int(value) for value in absolute_frame_indices))
        if not ordered_unique:
            yield {}
            return
        batch = self.export_batch(ordered_unique)
        try:
            yield batch.paths
        finally:
            self.release_batch(batch)

    @staticmethod
    def read_rgb(path: Path) -> np.ndarray:
        with Image.open(path) as image:
            image.load()
            return np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._export_pool.shutdown(wait=True, cancel_futures=True)
        with self._active_directories_lock:
            active_directories = tuple(self._active_directories)
        for path in active_directories:
            try:
                self._remove_owned_batch_root(path)
            except OSError:
                continue
            else:
                with self._active_directories_lock:
                    self._active_directories.discard(path)

    def __enter__(self) -> "MediaSDKPNGSideProvider":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass(frozen=True)
class _PreparedRawOpenPIBatch:
    ordered: tuple[int, ...]
    missing: tuple[int, ...]
    fronts: dict[int, np.ndarray]
    side_batch: _CompletedMediaSDKBatch | None
    export_seconds: float
    front_seconds: float


@dataclass(frozen=True)
class _ProjectorPoolMetrics:
    process_start_count: int
    frames_processed: int


class DirectRawOpenPIImageDecoder:
    """Pipelined direct front decode and transient MediaSDK side batches.

    At most two bounded PNG batches exist at once.  While one batch is read,
    projected and resized, the next batch is exported and its front frames are
    decoded.  Independent frames use independent persistent v360 graphs, so
    no filter state or pixels are shared across worker lanes.
    """

    def __init__(
        self,
        plan: DirectRawOpenPIImagePlan,
        *,
        work_root: Path | None = None,
        fallback_work_root: Path | None = None,
        batch_size: int = 512,
        export_workers: int = 4,
        free_space_margin_bytes: int = 512 * 1024 * 1024,
        seek_threshold_frames: int = 120,
        decode_threads: int = 1,
        resize_workers: int = 4,
        v360_filter_threads: int = 2,
        repeat_cache_frames: int = 8,
        verify_source_samples: bool = True,
        front_cache_manifest: Path | None = None,
        verify_front_cache_full_hash: bool = True,
        gpu_admission: SharedMediaSDKGPUAdmission | None = None,
    ) -> None:
        if batch_size < 1 or batch_size > 1024:
            raise ValueError("batch_size must be in [1, 1024]")
        if resize_workers < 1 or resize_workers > 8:
            raise ValueError("resize_workers must be in [1, 8]")
        if repeat_cache_frames < 0 or repeat_cache_frames > 256:
            raise ValueError("repeat_cache_frames must be in [0, 256]")
        plan.verify_sources(verify_samples=verify_source_samples)
        self.plan = plan
        self.batch_size = int(batch_size)
        self.front_cache = (
            None
            if front_cache_manifest is None
            else FrontOpenPICacheReader(
                front_cache_manifest,
                verify_full_hash=verify_front_cache_full_hash,
            )
        )
        self.front_reader = (
            ChapteredVideoReader(
                plan.insv_chapters,
                seek_threshold_frames=seek_threshold_frames,
                decode_threads=decode_threads,
            )
            if self.front_cache is None
            else None
        )
        self.side_provider = MediaSDKPNGSideProvider(
            plan,
            work_root=work_root,
            fallback_work_root=fallback_work_root,
            export_workers=export_workers,
            max_batch_frames=self.batch_size,
            free_space_margin_bytes=free_space_margin_bytes,
            gpu_admission=gpu_admission,
        )
        self._v360_filter_threads = int(v360_filter_threads)
        self._projector_local = threading.local()
        self._projectors: list[RawV360PairProjector] = []
        self._projectors_lock = threading.Lock()
        self._side_transform_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=resize_workers,
            thread_name_prefix="direct-raw-openpi-v360",
        )
        # A preparation task waits for MediaSDK while a dedicated front task
        # decodes and shrinks the same indices.  One look-ahead preparation is
        # enough to overlap the next export with current v360 work.
        self._prepare_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="direct-raw-openpi-prefetch",
        )
        self._front_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="direct-raw-openpi-front",
        )
        self._repeat_cache_frames = int(repeat_cache_frames)
        self._cache: OrderedDict[int, DirectOpenPIFrame] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._closed = False

    @property
    def projector(self) -> _ProjectorPoolMetrics:
        """Compatibility metrics aggregated over all persistent v360 lanes."""

        with self._projectors_lock:
            return _ProjectorPoolMetrics(
                process_start_count=sum(item.process_start_count for item in self._projectors),
                frames_processed=sum(item.frames_processed for item in self._projectors),
            )

    def _resize(self, image: np.ndarray) -> np.ndarray:
        return openpi_resize_with_pad(
            image, height=self.plan.target_height, width=self.plan.target_width
        )

    def _thread_projector(self) -> RawV360PairProjector:
        projector = getattr(self._projector_local, "projector", None)
        if projector is None:
            projector = RawV360PairProjector(
                projection_size=self.plan.projection_size,
                left_yaw=self.plan.left_yaw,
                right_yaw=self.plan.right_yaw,
                horizontal_fov=self.plan.horizontal_fov,
                vertical_fov=self.plan.vertical_fov,
                filter_threads=self._v360_filter_threads,
            )
            self._projector_local.projector = projector
            with self._projectors_lock:
                self._projectors.append(projector)
        return projector

    def _cached(self, frame_index: int) -> DirectOpenPIFrame | None:
        with self._cache_lock:
            result = self._cache.get(frame_index)
            if result is None:
                return None
            self._cache.move_to_end(frame_index)
            return result.copy()

    def _remember(self, result: DirectOpenPIFrame) -> None:
        if not self._repeat_cache_frames:
            return
        with self._cache_lock:
            self._cache[result.frame_index] = result.copy()
            self._cache.move_to_end(result.frame_index)
            while len(self._cache) > self._repeat_cache_frames:
                self._cache.popitem(last=False)

    def _decode_fronts(
        self, indices: tuple[int, ...]
    ) -> tuple[dict[int, np.ndarray], float]:
        started = time.monotonic()
        fronts: dict[int, np.ndarray] = {}
        for frame_index in indices:
            if self.front_cache is not None:
                fronts[frame_index] = self.front_cache.read(frame_index)
            else:
                assert self.front_reader is not None
                fronts[frame_index] = self._resize(self.front_reader.read(frame_index))
        return fronts, time.monotonic() - started

    def _prepare_batch(
        self,
        indices: tuple[int, ...],
        pending_cache_indices: tuple[int, ...] = (),
    ) -> _PreparedRawOpenPIBatch:
        if self._closed:
            raise RuntimeError("direct raw image decoder is closed")
        ordered = tuple(int(value) for value in indices)
        # Only the tail of the current batch is guaranteed to remain in the
        # bounded repeat cache when this look-ahead batch is consumed.  Exclude
        # exactly that pending tail, preventing duplicate MediaSDK work at a
        # batch boundary without relying on a racy snapshot of older cache data.
        pending = set(pending_cache_indices)
        missing = tuple(dict.fromkeys(value for value in ordered if value not in pending))
        if not missing:
            return _PreparedRawOpenPIBatch(ordered, missing, {}, None, 0.0, 0.0)

        front_future = self._front_pool.submit(self._decode_fronts, missing)
        side_batch: _CompletedMediaSDKBatch | None = None
        export_started = time.monotonic()
        try:
            side_batch = self.side_provider.export_batch(missing)
            export_seconds = time.monotonic() - export_started
            fronts, front_seconds = front_future.result()
            return _PreparedRawOpenPIBatch(
                ordered,
                missing,
                fronts,
                side_batch,
                export_seconds,
                front_seconds,
            )
        except BaseException:
            front_future.cancel()
            concurrent.futures.wait((front_future,))
            if side_batch is not None:
                self.side_provider.release_batch(side_batch)
            raise

    def _transform_side(
        self, item: tuple[int, Path]
    ) -> tuple[int, np.ndarray, np.ndarray]:
        frame_index, path = item
        panorama = self.side_provider.read_rgb(path)
        left_native, right_native = self._thread_projector().project(panorama)
        return frame_index, self._resize(left_native), self._resize(right_native)

    def _consume_prepared_batch(
        self, prepared: _PreparedRawOpenPIBatch
    ) -> tuple[DirectOpenPIFrame, ...]:
        built: dict[int, DirectOpenPIFrame] = {}
        # A look-ahead batch may deliberately omit a frame that is guaranteed
        # to be in the repeat-cache tail of the preceding batch.  Pin copies of
        # those omitted frames *before* remembering this batch's newly decoded
        # frames.  Otherwise a full batch of new frames evicts the preceding
        # tail before the ordered output loop gets a chance to retrieve it.
        # This is especially visible at 256-frame boundaries with the normal
        # eight-frame cache and a reused camera frame.
        missing_set = set(prepared.missing)
        for frame_index in dict.fromkeys(prepared.ordered):
            if frame_index in missing_set:
                continue
            cached = self._cached(frame_index)
            if cached is not None:
                built[frame_index] = cached
        transform_started = time.monotonic()
        try:
            if prepared.missing:
                assert prepared.side_batch is not None
                items = tuple(
                    (frame_index, prepared.side_batch.paths[frame_index])
                    for frame_index in prepared.missing
                )
                side_views = {
                    frame_index: (left, right)
                    for frame_index, left, right in self._side_transform_pool.map(
                        self._transform_side,
                        items,
                    )
                }
                for frame_index in prepared.missing:
                    left, right = side_views[frame_index]
                    result = DirectOpenPIFrame(
                        frame_index,
                        prepared.fronts[frame_index],
                        left,
                        right,
                    )
                    built[frame_index] = result
                    self._remember(result)
        finally:
            if prepared.side_batch is not None:
                self.side_provider.release_batch(prepared.side_batch)

        output: list[DirectOpenPIFrame] = []
        for frame_index in prepared.ordered:
            result = built.get(frame_index)
            if result is None:
                result = self._cached(frame_index)
            if result is None:
                raise FrameDecodeError(f"direct raw batch lost frame {frame_index}")
            output.append(result.copy())
        transform_seconds = time.monotonic() - transform_started
        print(
            "[direct-stage] "
            f"plan={self.plan.plan_hash[:12]} count={len(prepared.ordered)} "
            f"unique={len(prepared.missing)} export_sec={prepared.export_seconds:.3f} "
            f"front_sec={prepared.front_seconds:.3f} "
            f"v360_resize_sec={transform_seconds:.3f}",
            flush=True,
        )
        return tuple(output)

    @staticmethod
    def _release_future_batch(
        provider: MediaSDKPNGSideProvider,
        future: concurrent.futures.Future[_PreparedRawOpenPIBatch] | None,
    ) -> None:
        if future is None:
            return
        if future.cancel():
            return
        try:
            prepared = future.result()
        except BaseException:
            return
        if prepared.side_batch is not None:
            provider.release_batch(prepared.side_batch)

    def iter_frame_batches(
        self, absolute_frame_indices: Iterable[int]
    ) -> Iterator[tuple[DirectOpenPIFrame, ...]]:
        if self._closed:
            raise RuntimeError("direct raw image decoder is closed")
        chunks = iter(iter_index_chunks(absolute_frame_indices, self.batch_size))
        first = next(chunks, None)
        if first is None:
            return
        current_future: concurrent.futures.Future[_PreparedRawOpenPIBatch] | None = (
            self._prepare_pool.submit(self._prepare_batch, first[1])
        )
        try:
            while current_future is not None:
                prepared = current_future.result()
                following = next(chunks, None)
                guaranteed_pending = (
                    prepared.missing[-self._repeat_cache_frames :]
                    if self._repeat_cache_frames
                    else ()
                )
                current_future = (
                    None
                    if following is None
                    else self._prepare_pool.submit(
                        self._prepare_batch,
                        following[1],
                        guaranteed_pending,
                    )
                )
                yield self._consume_prepared_batch(prepared)
        finally:
            self._release_future_batch(self.side_provider, current_future)

    def _process_batch(self, indices: Sequence[int]) -> list[DirectOpenPIFrame]:
        batches = list(self.iter_frame_batches(tuple(int(value) for value in indices)))
        return [frame for batch in batches for frame in batch]

    def read(self, absolute_frame_index: int) -> DirectOpenPIFrame:
        return self._process_batch((int(absolute_frame_index),))[0]

    def iter_frames(self, absolute_frame_indices: Iterable[int]) -> Iterator[DirectOpenPIFrame]:
        for batch in self.iter_frame_batches(absolute_frame_indices):
            yield from batch

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._prepare_pool.shutdown(wait=True, cancel_futures=True)
        self._front_pool.shutdown(wait=True, cancel_futures=True)
        self._side_transform_pool.shutdown(wait=True, cancel_futures=True)
        if self.front_reader is not None:
            self.front_reader.close()
        if self.front_cache is not None:
            self.front_cache.close()
        self.side_provider.close()
        with self._projectors_lock:
            projectors = tuple(self._projectors)
            self._projectors.clear()
        for projector in projectors:
            projector.close()
        with self._cache_lock:
            self._cache.clear()

    def __enter__(self) -> "DirectRawOpenPIImageDecoder":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = [
    "AtomicChunkCheckpointStore",
    "CheckpointValidationError",
    "ChapteredVideoReader",
    "DirectImageError",
    "DirectOpenPIFrame",
    "DirectOpenPIImageDecoder",
    "DirectOpenPIImagePlan",
    "DirectRawOpenPIImageDecoder",
    "DirectRawOpenPIImagePlan",
    "FrameDecodeError",
    "RawV360PairProjector",
    "MediaSDKPNGSideProvider",
    "SourceValidationError",
    "VideoChapter",
    "iter_index_chunks",
    "openpi_resize_with_pad",
    "probe_video",
    "source_identity",
    "verify_source_identity",
]
