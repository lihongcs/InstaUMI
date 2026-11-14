from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Protocol, TextIO


ON_DEMAND_POINTER_SCHEMA = "instaumi.gripper_threshold_on_demand_pointer.v1"


class PreviewReader(Protocol):
    def load(self, input_path: str, episode_index: int | None) -> dict[str, Any]: ...


class GripperEstimationBusyError(RuntimeError):
    pass


class GripperEstimationNotReadyError(RuntimeError):
    pass


def _capture_timestamp_log(capture: Path) -> Path | None:
    primary = capture / "timestamp_log.jsonl"
    if primary.is_file() and not primary.is_symlink():
        return primary
    alternatives = sorted(
        path
        for path in capture.glob("timestamp_log_*.jsonl")
        if path.is_file() and not path.is_symlink()
    )
    return alternatives[0] if alternatives else None


def _capture_has_bimanual_insv(capture: Path) -> bool:
    insta_root = capture / "insta"
    camera_choices = (
        (insta_root / "insta_right", insta_root / "camera_1"),
        (insta_root / "insta_left", insta_root / "camera_2"),
    )
    return all(
        any(
            directory.is_dir()
            and not directory.is_symlink()
            and any(
                path.is_file() and not path.is_symlink()
                for path in directory.glob("*.insv")
            )
            for directory in choices
        )
        for choices in camera_choices
    )


def _complete_episode_indices(timestamp_log: Path) -> list[int]:
    """Parse the same complete segment contract used by the _03 sampler."""

    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    with timestamp_log.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON at {timestamp_log}:{line_number}"
                ) from exc
            if not isinstance(record, dict):
                continue
            kind = str(record.get("type", record.get("title", "")))
            if kind not in {"segment_start", "segment_end"}:
                continue
            raw_index = record.get("segment_index", record.get("segmentIndex"))
            raw_timestamp = record.get("unix_time_ns", record.get("unixTimeNs"))
            if raw_index is None or raw_timestamp is None:
                continue
            try:
                episode_index = int(raw_index)
                timestamp_ns = int(raw_timestamp)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid segment event at {timestamp_log}:{line_number}"
                ) from exc
            target = starts if kind == "segment_start" else ends
            if episode_index in target:
                raise ValueError(
                    f"duplicate {kind} for episode {episode_index}: {timestamp_log}"
                )
            target[episode_index] = timestamp_ns

    complete = []
    for episode_index in sorted(set(starts) & set(ends)):
        if ends[episode_index] <= starts[episode_index]:
            raise ValueError(
                f"non-positive episode {episode_index} in {timestamp_log}"
            )
        complete.append(episode_index)
    if not complete:
        raise ValueError(f"no complete episodes found in {timestamp_log}")
    return complete


def discover_gripper_capture_sources(input_root: str | Path) -> dict[str, Any]:
    """Discover raw captures and complete episodes without writing anything.

    ``input_root`` may be one raw capture or a directory whose immediate
    children are raw captures.  Scanning is deliberately shallow so a typo
    cannot trigger an unbounded walk across mounted datasets.
    """

    requested = Path(input_root).expanduser()
    if requested.is_symlink():
        raise ValueError(f"raw capture input cannot be a symlink: {requested}")
    root = requested.resolve()
    if not root.is_dir():
        raise ValueError(f"raw capture directory does not exist: {root}")

    candidates: list[Path]
    own_log = _capture_timestamp_log(root)
    if own_log is not None and _capture_has_bimanual_insv(root):
        candidates = [root]
        mode = "capture"
    else:
        candidates = [
            child.resolve()
            for child in sorted(root.iterdir(), key=lambda path: path.name)
            if child.is_dir()
            and not child.is_symlink()
            and _capture_timestamp_log(child) is not None
            and _capture_has_bimanual_insv(child)
        ]
        mode = "collection"
    if not candidates:
        raise ValueError(
            "no raw bimanual capture found; select a capture or a parent whose "
            "immediate children contain timestamp_log.jsonl and both Insta INSV directories"
        )

    captures = []
    for capture in candidates:
        timestamp_log = _capture_timestamp_log(capture)
        assert timestamp_log is not None
        episodes = _complete_episode_indices(timestamp_log)
        captures.append(
            {
                "name": capture.name,
                "path": str(capture),
                "timestamp_log": str(timestamp_log),
                "episode_indices": episodes,
                "episode_count": len(episodes),
                "first_episode": episodes[0],
                "last_episode": episodes[-1],
            }
        )
    return {
        "schema": "instadatacap.gripper_capture_discovery.v1",
        "read_only": True,
        "requested_root": str(root),
        "mode": mode,
        "captures": captures,
        "capture_count": len(captures),
        "episode_count": sum(item["episode_count"] for item in captures),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_on_demand_pointer(
    pointer_path: Path,
    *,
    capture_root: Path,
    output_root: Path,
    episode_index: int,
) -> tuple[Path, str] | None:
    """Resolve the sampler's stable pointer without trusting path existence."""

    if not pointer_path.is_file() or pointer_path.is_symlink():
        return None
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != ON_DEMAND_POINTER_SCHEMA
            or payload.get("status") != "complete"
            or Path(str(payload.get("capture", ""))).expanduser().resolve() != capture_root
            or payload.get("episode_index") != episode_index
        ):
            return None
        expected_hash = str(payload.get("manifest_sha256", ""))
        if len(expected_hash) != 64 or any(
            char not in "0123456789abcdef" for char in expected_hash
        ):
            return None
        manifest_path = Path(str(payload.get("manifest", ""))).expanduser().resolve()
        manifest_path.relative_to(output_root)
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or _sha256_file(manifest_path) != expected_hash
        ):
            return None
        return manifest_path, expected_hash
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _acquire_pipeline_locks(capture_root: Path) -> list[TextIO]:
    """Exclude the ordinary full conversion for the complete async job."""

    handles: list[TextIO] = []
    try:
        for name in (".fresh_rebuild.lock", ".terminal_processing.lock"):
            lock_path = capture_root / name
            if lock_path.is_symlink():
                raise ValueError(f"refusing symlink pipeline lock: {lock_path}")
            # O_NOFOLLOW closes the lstat/open race: a lock path replaced by a
            # symlink between the check above and open() must still be refused.
            flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise ValueError(f"cannot safely open pipeline lock: {lock_path}: {exc}") from exc
            try:
                handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            except Exception:
                os.close(descriptor)
                raise
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise GripperEstimationBusyError(
                    "the capture is already being processed by a full conversion; "
                    "wait for it to finish before estimating one gripper episode"
                ) from exc
            handles.append(handle)
        return handles
    except Exception:
        _release_pipeline_locks(handles)
        raise


def _release_pipeline_locks(handles: list[TextIO]) -> None:
    for handle in reversed(handles):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


class GripperEpisodeEstimationJob:
    """Run one exact-_03 sampling subprocess without touching capture state.

    The manager has its own mutex. Capture exclusion is enforced by
    ``CaptureRuntime`` while holding its existing ``capture_postprocess_lock``;
    no worker thread acquires or releases that RLock.
    """

    def __init__(
        self,
        *,
        python_executable: str | Path,
        sampler_script: str | Path,
        preview_reader: PreviewReader,
        output_directory_name: str = "gripper_threshold_sampling",
        max_log_lines: int = 200,
    ) -> None:
        # Keep a venv's `bin/python` path intact.  realpath would execute the
        # base interpreter and lose the processing environment's packages.
        self.python_executable = Path(
            os.path.abspath(os.path.expanduser(os.fspath(python_executable)))
        )
        self.sampler_script = Path(sampler_script).expanduser().resolve()
        self.preview_reader = preview_reader
        if Path(output_directory_name).name != output_directory_name or output_directory_name in {
            "",
            ".",
            "..",
        }:
            raise ValueError(f"invalid gripper job output directory: {output_directory_name!r}")
        self.output_directory_name = output_directory_name
        self.max_log_lines = max(10, int(max_log_lines))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._process_group_id: int | None = None
        self._closing = False
        self._logs: deque[dict[str, Any]] = deque(maxlen=self.max_log_lines)
        self._job: dict[str, Any] = self._idle_status()
        self._preview: dict[str, Any] | None = None

    @staticmethod
    def _idle_status() -> dict[str, Any]:
        return {
            "job_id": None,
            "status": "idle",
            "state": "idle",
            "capture_root": None,
            "episode": None,
            "elapsed_sec": 0.0,
            "cache_hit": False,
            "cache_state": "unknown",
            "message": "No gripper estimation job has been started",
            "error": None,
            "manifest_path": None,
            "output_root": None,
            "pid": None,
            "process_group_id": None,
            "returncode": None,
            "result_ready": False,
            "started_unix_ns": None,
            "finished_unix_ns": None,
        }

    @property
    def running(self) -> bool:
        with self._lock:
            return self._job["status"] == "running"

    def _public_locked(
        self, *, include_preview: bool, include_logs: bool
    ) -> dict[str, Any]:
        result = dict(self._job)
        started_ns = result.get("started_unix_ns")
        finished_ns = result.get("finished_unix_ns")
        if started_ns is not None:
            end_ns = finished_ns if finished_ns is not None else time.time_ns()
            result["elapsed_sec"] = max(0.0, (end_ns - started_ns) / 1_000_000_000.0)
        if include_logs:
            result["logs"] = list(self._logs)
        if include_preview and self._preview is not None:
            result["preview"] = self._preview
        return result

    def status(
        self,
        job_id: str | None = None,
        *,
        include_preview: bool = False,
        include_logs: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            current_id = self._job.get("job_id")
            if job_id is not None and job_id != current_id:
                raise ValueError(f"unknown gripper estimation job_id: {job_id}")
            return self._public_locked(
                include_preview=include_preview,
                include_logs=include_logs,
            )

    def _append_log(self, message: str) -> None:
        normalized = message.rstrip("\r\n")
        if not normalized:
            return
        with self._lock:
            self._logs.append(
                {
                    "unix_time_ns": time.time_ns(),
                    "message": normalized[:8192],
                }
            )

    def start(self, capture_root: str | Path, episode: int) -> dict[str, Any]:
        requested_capture = Path(capture_root).expanduser()
        if requested_capture.is_symlink():
            raise ValueError(f"raw capture root cannot be a symlink: {requested_capture}")
        capture = requested_capture.resolve()
        if not capture.is_dir():
            raise ValueError(f"capture directory does not exist: {capture}")
        timestamp_logs = [capture / "timestamp_log.jsonl", *capture.glob("timestamp_log_*.jsonl")]
        insta_root = capture / "insta"
        camera_choices = (
            (insta_root / "insta_right", insta_root / "camera_1"),
            (insta_root / "insta_left", insta_root / "camera_2"),
        )
        valid_camera_sides = [
            any(
                directory.is_dir()
                and not directory.is_symlink()
                and any(path.is_file() and not path.is_symlink() for path in directory.glob("*.insv"))
                for directory in choices
            )
            for choices in camera_choices
        ]
        if (
            not any(path.is_file() for path in timestamp_logs)
            or not all(valid_camera_sides)
        ):
            raise ValueError(
                "gripper estimation requires a raw capture with timestamp_log.jsonl "
                "(or timestamp_log_*.jsonl) and non-symlink INSV files for both arms "
                "under insta/insta_{left,right} (legacy camera_{2,1} is accepted)"
            )
        if isinstance(episode, bool):
            raise ValueError("episode must be a non-negative integer")
        try:
            episode_index = int(episode)
        except (TypeError, ValueError) as exc:
            raise ValueError("episode must be a non-negative integer") from exc
        if episode_index < 0 or episode_index != episode:
            raise ValueError("episode must be a non-negative integer")
        if not self.python_executable.is_file() or not os.access(
            self.python_executable, os.X_OK
        ):
            raise ValueError(f"gripper estimation Python is unavailable: {self.python_executable}")
        if not self.sampler_script.is_file():
            raise ValueError(f"gripper threshold sampler is unavailable: {self.sampler_script}")

        output_root = (capture / self.output_directory_name).resolve()
        try:
            output_root.relative_to(capture)
        except ValueError as exc:
            raise ValueError(f"gripper job output escapes capture root: {output_root}") from exc
        pointer_path = (
            output_root
            / "on_demand"
            / "episodes"
            / f"episode_{episode_index:06d}.json"
        )
        previous_pointer = _validated_on_demand_pointer(
            pointer_path,
            capture_root=capture,
            output_root=output_root,
            episode_index=episode_index,
        )

        command = [
            str(self.python_executable),
            str(self.sampler_script),
            str(capture),
            "--output-root",
            str(output_root),
            "--on-demand-episode",
            str(episode_index),
        ]
        with self._lock:
            if self._job["status"] == "running":
                raise GripperEstimationBusyError(
                    f"gripper estimation job {self._job['job_id']} is already running"
                )
            pipeline_locks = _acquire_pipeline_locks(capture)
            self._closing = False
            self._preview = None
            self._logs.clear()
            self._process_group_id = None
            job_id = uuid.uuid4().hex
            self._job = {
                **self._idle_status(),
                "job_id": job_id,
                "status": "running",
                "state": "running",
                "capture_root": str(capture),
                "episode": episode_index,
                "cache_state": "candidate" if previous_pointer else "miss",
                "message": "Estimating continuous gripper width in the background",
                "manifest_path": (
                    str(previous_pointer[0]) if previous_pointer is not None else None
                ),
                "pointer_path": str(pointer_path),
                "output_root": str(output_root),
                "started_unix_ns": time.time_ns(),
                "command": command,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    job_id,
                    command,
                    capture,
                    episode_index,
                    output_root,
                    pointer_path,
                    previous_pointer,
                    pipeline_locks,
                ),
                name=f"GripperEstimate-{episode_index}",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                _release_pipeline_locks(pipeline_locks)
                self._job = self._idle_status()
                self._thread = None
                raise
            return self._public_locked(include_preview=False, include_logs=True)

    def _finish_failure(
        self,
        job_id: str,
        message: str,
        *,
        returncode: int | None,
        cancelled: bool = False,
    ) -> None:
        with self._lock:
            if self._job.get("job_id") != job_id:
                return
            state = "cancelled" if cancelled else "failed"
            self._job.update(
                {
                    "status": state,
                    "state": state,
                    "message": message,
                    "error": message,
                    "returncode": returncode,
                    "finished_unix_ns": time.time_ns(),
                    "result_ready": False,
                    "pid": None,
                    "process_group_id": None,
                }
            )
            self._process = None
            self._process_group_id = None

    def _run(
        self,
        job_id: str,
        command: list[str],
        capture_root: Path,
        episode_index: int,
        output_root: Path,
        pointer_path: Path,
        previous_pointer: tuple[Path, str] | None,
        pipeline_locks: list[TextIO],
    ) -> None:
        try:
            self._run_job(
                job_id,
                command,
                capture_root,
                episode_index,
                output_root,
                pointer_path,
                previous_pointer,
            )
        except Exception as exc:
            self._terminate_process_group(signal.SIGKILL, wait_timeout=2.0)
            self._finish_failure(
                job_id,
                f"gripper estimation worker failed: {type(exc).__name__}: {exc}",
                returncode=None,
            )
        finally:
            _release_pipeline_locks(pipeline_locks)

    def _run_job(
        self,
        job_id: str,
        command: list[str],
        capture_root: Path,
        episode_index: int,
        output_root: Path,
        pointer_path: Path,
        previous_pointer: tuple[Path, str] | None,
    ) -> None:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            self._finish_failure(
                job_id,
                f"failed to start gripper estimator: {type(exc).__name__}: {exc}",
                returncode=None,
            )
            return
        with self._lock:
            if self._job.get("job_id") != job_id:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2.0)
                return
            self._process = process
            self._process_group_id = process.pid
            self._job["pid"] = process.pid
            self._job["process_group_id"] = process.pid
        assert process.stdout is not None
        for line in process.stdout:
            self._append_log(line)
        process.stdout.close()
        returncode = process.wait()
        # A successful/failed leader must not leave a silent decoder child in
        # its session after stdout reached EOF.
        self._terminate_process_group(signal.SIGKILL, wait_timeout=0.0)
        with self._lock:
            cancelled = self._closing and self._job.get("job_id") == job_id
        if cancelled:
            self._finish_failure(
                job_id,
                "gripper estimation was cancelled during DataCap shutdown",
                returncode=returncode,
                cancelled=True,
            )
            return
        if returncode != 0:
            with self._lock:
                detail = self._logs[-1]["message"] if self._logs else "no subprocess output"
            self._finish_failure(
                job_id,
                f"gripper estimator exited with code {returncode}: {detail}",
                returncode=returncode,
            )
            return
        try:
            current_pointer = _validated_on_demand_pointer(
                pointer_path,
                capture_root=capture_root,
                output_root=output_root,
                episode_index=episode_index,
            )
            if current_pointer is None:
                raise RuntimeError(
                    f"sampler did not publish a valid on-demand pointer: {pointer_path}"
                )
            manifest_path, current_manifest_hash = current_pointer
            # The reader validates the canonical trace paths, frame counts and
            # every per-arm SHA before the job can become successful.
            preview = self.preview_reader.load(
                str(manifest_path), episode_index
            )
            if preview.get("read_only") is not True:
                raise RuntimeError("gripper preview reader returned no read-only contract")
        except Exception as exc:
            self._finish_failure(
                job_id,
                f"gripper output validation failed: {type(exc).__name__}: {exc}",
                returncode=returncode,
            )
            return
        cache_hit = (
            previous_pointer is not None
            and previous_pointer[0] == manifest_path
            and previous_pointer[1] == current_manifest_hash
        )
        with self._lock:
            if self._job.get("job_id") != job_id:
                return
            self._preview = preview
            self._job.update(
                {
                    "status": "succeeded",
                    "state": "succeeded",
                    "message": (
                        "Loaded validated cached gripper estimate"
                        if cache_hit
                        else "Continuous gripper-width estimate is ready"
                    ),
                    "error": None,
                    "cache_hit": cache_hit,
                    "cache_state": (
                        "hit"
                        if cache_hit
                        else "refreshed"
                        if previous_pointer is not None
                        else "miss"
                    ),
                    "manifest_path": str(manifest_path),
                    "returncode": returncode,
                    "finished_unix_ns": time.time_ns(),
                    "result_ready": True,
                    "pid": None,
                    "process_group_id": None,
                }
            )
            self._process = None
            self._process_group_id = None

    def result(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id != self._job.get("job_id"):
                raise ValueError(f"unknown gripper estimation job_id: {job_id}")
            if self._job["status"] == "running":
                raise GripperEstimationNotReadyError("gripper estimation is still running")
            if self._job["status"] != "succeeded" or self._preview is None:
                raise GripperEstimationNotReadyError(self._job["message"])
            return self._public_locked(include_preview=True, include_logs=True)

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _terminate_process_group(
        self, sig: signal.Signals, *, wait_timeout: float | None = None
    ) -> None:
        with self._lock:
            process_group_id = self._process_group_id
            process = self._process
        if process_group_id is None:
            return
        try:
            # The group may still contain a decoder child even after its Python
            # leader exited. Never gate killpg() on Popen.poll().
            os.killpg(process_group_id, sig)
        except ProcessLookupError:
            pass
        if process is not None and wait_timeout is not None:
            try:
                process.wait(timeout=max(0.0, wait_timeout))
            except subprocess.TimeoutExpired:
                pass

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._closing = True
            thread = self._thread
        self._terminate_process_group(signal.SIGTERM)
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        if thread is not None and thread.is_alive():
            self._terminate_process_group(signal.SIGKILL, wait_timeout=2.0)
            thread.join(timeout=2.0)
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                "gripper estimation worker did not stop after its complete process group "
                "was killed; pipeline locks remain held"
            )
