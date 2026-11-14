from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any

from .insta_adapter import AdapterResult, InstaAdapter


class MvsCameraAdapter(InstaAdapter):
    """Two-camera Hikrobot MVS capture backend.

    Each configured serial is owned by one worker process.  Workers stay alive
    while idle to provide preview frames.  Recording is opened and sealed at
    each DataCap episode boundary, never as one session-long video.
    """

    protocol_prefix = "DATACAP_MVS_JSON "
    protocol_name = "instadatacap-mvs-worker-v1"
    supported_roles = frozenset({"insta_right", "insta_left"})
    canonical_role_order = ("insta_right", "insta_left")
    response_actions = frozenset({"ready", "start", "stop", "status", "quit", "fatal"})
    preview_stale_after_ms = 1500.0

    def __init__(
        self,
        *,
        serials: dict[str, str],
        worker_path: Path,
        python_executable: str,
        exumi_root: Path,
        preview_dir: Path,
        fps: float = 30.0,
        exposure_us: int = 20_000,
        width: int = 224,
        height: int = 224,
        raw_width: int = 1536,
        raw_height: int = 1536,
        ready_timeout_sec: float = 15.0,
        command_timeout_sec: float = 8.0,
        stop_timeout_sec: float = 30.0,
        require_usb3: bool = True,
    ) -> None:
        if not isinstance(serials, dict) or not serials:
            raise ValueError("MVS serials must be a non-empty role-to-serial mapping")
        normalized: dict[str, str] = {}
        for raw_role, raw_serial in serials.items():
            role = self._normalize_role(raw_role)
            serial = str(raw_serial).strip()
            if not serial:
                raise ValueError(f"MVS serial for {role} is empty")
            if role in normalized:
                raise ValueError(f"duplicate MVS role: {role}")
            normalized[role] = serial
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("MVS roles must use different camera serials")
        if fps <= 0 or exposure_us <= 0 or min(width, height, raw_width, raw_height) <= 0:
            raise ValueError("MVS fps, exposure, and image dimensions must be positive")
        if (int(raw_width), int(raw_height), int(width), int(height)) != (
            1536,
            1536,
            224,
            224,
        ):
            raise ValueError(
                "DataCap MVS has one fixed image contract: native 1536x1536 BGR8 "
                "is resized exactly once to lossless 224x224 RGB8"
            )
        if min(ready_timeout_sec, command_timeout_sec, stop_timeout_sec) <= 0:
            raise ValueError("MVS worker timeouts must be positive")

        # A role is a physical identity, not an SDK enumeration index. Keep
        # both startup order and all generated manifests canonical even when
        # JSON key order changes.
        self.serials = {
            role: normalized[role]
            for role in self.canonical_role_order
            if role in normalized
        }
        self.worker_path = worker_path.expanduser().resolve()
        self.python_executable = str(Path(python_executable).expanduser().resolve())
        self.exumi_root = exumi_root.expanduser().resolve()
        self.preview_dir = preview_dir.expanduser().resolve()
        self.fps = float(fps)
        self.exposure_us = int(exposure_us)
        self.width = int(width)
        self.height = int(height)
        self.raw_width = int(raw_width)
        self.raw_height = int(raw_height)
        self.ready_timeout_sec = float(ready_timeout_sec)
        self.command_timeout_sec = float(command_timeout_sec)
        self.stop_timeout_sec = float(stop_timeout_sec)
        self.require_usb3 = bool(require_usb3)
        self.capture_roles = list(self.serials)
        self.expected_cameras = len(self.capture_roles)

        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.response_queues: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.worker_states: dict[str, dict[str, Any]] = {}
        self.output_tails: dict[str, list[str]] = {}
        self.worker_generations: dict[str, int] = {}
        self.worker_started_unix_ns: dict[str, int] = {}
        self.recording = False
        self.active_session_dir: Path | None = None
        self.active_episode_dir: Path | None = None
        self.active_episode_index: int | None = None
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.closing_event = threading.Event()

    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        role = str(value).strip().lower()
        if role not in cls.supported_roles:
            raise ValueError(
                f"unsupported MVS role {value!r}; expected one of "
                + ", ".join(sorted(cls.supported_roles))
            )
        return role

    def set_capture_roles(self, roles: list[str]) -> None:
        selected = [self._normalize_role(role) for role in roles]
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("MVS capture roles must be non-empty and unique")
        missing = [role for role in selected if role not in self.serials]
        if missing:
            raise ValueError(f"MVS roles have no configured serial: {', '.join(missing)}")
        with self.lock:
            if self.recording:
                raise RuntimeError("cannot change MVS roles while recording")
            if selected != self.capture_roles:
                self._disconnect_locked()
                self.capture_roles = selected
                self.expected_cameras = len(selected)

    def _validate_runtime(self) -> None:
        python = Path(self.python_executable)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise RuntimeError(f"MVS Python is not executable: {python}")
        if not self.worker_path.is_file():
            raise RuntimeError(f"MVS worker does not exist: {self.worker_path}")
        if not self.exumi_root.is_dir():
            raise RuntimeError(f"ExUMI root does not exist: {self.exumi_root}")

    def scan_cameras(self) -> AdapterResult:
        try:
            self._validate_runtime()
            completed = subprocess.run(
                [
                    self.python_executable,
                    str(self.worker_path),
                    "--list-devices",
                    "--exumi-root",
                    str(self.exumi_root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.ready_timeout_sec,
                check=False,
            )
        except Exception as exc:
            return AdapterResult(False, f"MVS enumeration failed: {type(exc).__name__}: {exc}")
        response: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            if line.startswith(self.protocol_prefix):
                try:
                    payload = json.loads(line[len(self.protocol_prefix) :])
                except json.JSONDecodeError:
                    continue
                if payload.get("action") == "list":
                    response = payload
        if completed.returncode != 0 or response is None or not response.get("ok"):
            tail = "\n".join(completed.stdout.splitlines()[-8:])
            return AdapterResult(False, f"MVS SDK enumeration failed (exit={completed.returncode}): {tail}")
        discovered = {str(value) for value in response.get("serials", [])}
        cameras = [
            {
                "role": role,
                "serial": self.serials[role],
                "available": self.serials[role] in discovered,
                "connected": self._worker_alive_locked(role),
            }
            for role in self.capture_roles
        ]
        missing = [item["serial"] for item in cameras if not item["available"]]
        if missing:
            return AdapterResult(
                False,
                "MVS cameras missing from SDK enumeration: " + ", ".join(missing),
                {"transport": "mvs_sdk", "cameras": cameras, "discovered_serials": sorted(discovered)},
            )
        usb_links = self._hikrobot_usb_links()
        if self.require_usb3:
            if len(usb_links) < len(cameras):
                return AdapterResult(
                    False,
                    f"MVS SDK found both serials, but only {len(usb_links)} Hikrobot USB link(s) "
                    f"were visible in sysfs; need {len(cameras)}",
                    {
                        "transport": "mvs_sdk",
                        "cameras": cameras,
                        "discovered_serials": sorted(discovered),
                        "usb_links": usb_links,
                    },
                )
            slow = [link for link in usb_links if float(link.get("speed_mbps", 0.0)) < 5000.0]
            if slow:
                return AdapterResult(
                    False,
                    "Every MV-CB060 must negotiate USB 3.x (5000 Mbps); slow links: "
                    + ", ".join(
                        f"{link['path']}={link.get('speed_mbps', 'unknown')}Mbps"
                        for link in slow
                    ),
                    {"transport": "mvs_sdk", "cameras": cameras, "usb_links": usb_links},
                )
        return AdapterResult(
            True,
            f"MVS SDK found all {len(cameras)} configured camera(s)",
            {
                "transport": "mvs_sdk",
                "cameras": cameras,
                "discovered_serials": sorted(discovered),
                "usb_links": usb_links,
            },
        )

    def connect_cameras(self) -> AdapterResult:
        with self.lock:
            if self.closing_event.is_set():
                return AdapterResult(False, "MVS adapter is closing")
            try:
                self._validate_runtime()
                self.preview_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return AdapterResult(False, str(exc))
            scan = self.scan_cameras()
            if not scan.ok:
                return scan
            responses: dict[str, Any] = {}
            errors: list[str] = []
            # The reference teleop path starts its observation camera and waits
            # for the first valid SDK frame before opening the marker camera.
            # MVS SDK initialization is process-global enough that opening two
            # USB cameras concurrently can wedge both OpenDevice calls.
            for role in self.capture_roles:
                if self._worker_ready_locked(role):
                    responses[role] = dict(self.worker_states[role])
                    continue
                self._terminate_worker_locked(role, graceful=False)
                try:
                    self._spawn_worker_locked(role)
                except Exception as exc:
                    errors.append(f"{role}: failed to start worker: {exc}")
                    break
                response = self._wait_response_locked(role, "ready", self.ready_timeout_sec)
                if response is None or not response.get("ok"):
                    errors.append(
                        f"{role}: "
                        + (str(response.get("message")) if response else "ready timeout")
                    )
                    break
                elif response.get("role") != role or response.get("serial") != self.serials[role]:
                    errors.append(
                        f"{role}: worker identity mismatch; expected serial={self.serials[role]!r}, "
                        f"got role={response.get('role')!r} serial={response.get('serial')!r}"
                    )
                    break
                else:
                    responses[role] = response
            if errors:
                self._disconnect_locked(graceful=False)
                return AdapterResult(False, "MVS connection failed: " + "; ".join(errors))
            return AdapterResult(
                True,
                f"{len(responses)} MVS camera preview worker(s) ready",
                {"workers": responses},
            )

    def disconnect_cameras(self) -> AdapterResult:
        with self.lock:
            return self._disconnect_locked()

    def prepare(self, session_dir: Path) -> AdapterResult:
        del session_dir
        with self.lock:
            if all(self._worker_ready_locked(role) for role in self.capture_roles):
                return AdapterResult(True, "MVS workers already ready")
        return self.connect_cameras()

    def start_recording(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            if self.recording:
                return AdapterResult(True, "MVS capture session was already started")
            if not all(self._worker_ready_locked(role) for role in self.capture_roles):
                return AdapterResult(False, "MVS workers are not all ready")
            root = session_dir.expanduser().resolve()
            episodes = root / "episodes"
            episodes.mkdir(parents=True, exist_ok=False)
            manifest = {
                "schema": "instadatacap.mvs.capture.v2",
                "transport": "mvs_sdk",
                "layout": "one_lossless_directory_per_episode",
                "serials": {role: self.serials[role] for role in self.capture_roles},
                "fps": self.fps,
                "exposure_us": self.exposure_us,
                "width": self.width,
                "height": self.height,
                "raw_width": self.raw_width,
                "raw_height": self.raw_height,
                "image_processing": (
                    f"MVS BGR8 {self.raw_width}x{self.raw_height} -> isotropic "
                    f"INTER_AREA RGB{self.width}x{self.height}; no crop, panorama, "
                    "warp, active-view projection, or later image resize"
                ),
                "recording_encoding": (
                    "Matroska libx264rgb CRF0 RGB24 lossless; decoded RGB frame hash "
                    "must equal the camera manifest"
                ),
                "timestamp": (
                    "per-frame MVS SDK host frame-availability minus half exposure, converted "
                    "to CLOCK_MONOTONIC; explicit GetImageBuffer-return fallback is labeled "
                    "in each camera_timestamps.csv row"
                ),
                "started_unix_ns": time.time_ns(),
            }
            path = root / "mvs_capture.json"
            path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.recording = True
            self.active_session_dir = root
            return AdapterResult(
                True,
                "MVS capture session armed; camera files open only during each episode",
                {"episodes_dir": str(episodes)},
            )

    def start_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        with self.lock:
            root = session_dir.expanduser().resolve()
            if not self.recording or self.active_session_dir != root:
                return AdapterResult(False, "MVS capture session is not armed for this directory")
            if self.active_episode_dir is not None:
                return AdapterResult(False, f"MVS episode {self.active_episode_index} is already recording")
            if episode_index <= 0:
                return AdapterResult(False, f"invalid MVS episode index: {episode_index}")
            episode_root = root / "episodes" / f"episode_{episode_index:06d}"
            try:
                episode_root.mkdir(parents=False, exist_ok=False)
                episode_manifest = {
                    "schema": "instadatacap.mvs.episode.v1",
                    "episode_index": int(episode_index),
                    "roles": list(self.capture_roles),
                    "serials": {role: self.serials[role] for role in self.capture_roles},
                    "width": self.width,
                    "height": self.height,
                    "fps": self.fps,
                    "started_unix_ns": time.time_ns(),
                }
                (episode_root / "mvs_episode.json").write_text(
                    json.dumps(episode_manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                return AdapterResult(False, f"cannot create MVS episode directory: {exc}")
            command = "START\t" + json.dumps(str(episode_root), ensure_ascii=False)
            results = self._parallel_request_locked(command, "start", self.command_timeout_sec)
            succeeded = [role for role, result in results.items() if result and result.get("ok")]
            failed = [role for role in self.capture_roles if role not in succeeded]
            if failed:
                if succeeded:
                    self._parallel_request_locked("STOP", "stop", self.stop_timeout_sec, succeeded)
                failed_root = episode_root.with_name(
                    f".{episode_root.name}.failed-{time.time_ns()}"
                )
                try:
                    episode_root.replace(failed_root)
                except OSError:
                    failed_root = episode_root
                return AdapterResult(
                    False,
                    "MVS episode recording start failed for " + ", ".join(failed),
                    {"workers": results, "failed_attempt_dir": str(failed_root)},
                )
            self.active_episode_dir = episode_root
            self.active_episode_index = int(episode_index)
            return AdapterResult(
                True,
                f"MVS episode {episode_index} recording started",
                {"workers": results, "episode_dir": str(episode_root)},
            )

    def stop_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        with self.lock:
            root = session_dir.expanduser().resolve()
            if self.active_session_dir != root:
                return AdapterResult(False, "MVS episode stop targets the wrong capture session")
            if self.active_episode_dir is None or self.active_episode_index != int(episode_index):
                return AdapterResult(False, f"MVS episode {episode_index} is not active")
            episode_root = self.active_episode_dir
            results = self._parallel_request_locked("STOP", "stop", self.stop_timeout_sec)
            failed = [
                role for role in self.capture_roles
                if results.get(role) is None or not results[role].get("ok")
            ]
            if failed:
                return AdapterResult(
                    False,
                    "MVS episode recording stop failed for " + ", ".join(failed),
                    {"workers": results, "episode_dir": str(episode_root)},
                )
            try:
                summaries = {
                    role: self._validate_capture_output(episode_root, role, results[role])
                    for role in self.capture_roles
                }
            except Exception as exc:
                return AdapterResult(
                    False,
                    "MVS episode stopped, but validation failed: "
                    f"{type(exc).__name__}: {exc}",
                    {"workers": results, "episode_dir": str(episode_root)},
                )
            marker = episode_root / "mvs_episode_complete.json"
            payload = {
                "schema": "instadatacap.mvs.episode_complete.v1",
                "complete": True,
                "episode_index": int(episode_index),
                "stopped_unix_ns": time.time_ns(),
                "cameras": summaries,
            }
            temporary = marker.with_name(".mvs_episode_complete.json.tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(marker)
            self.active_episode_dir = None
            self.active_episode_index = None
            return AdapterResult(
                True,
                f"MVS episode {episode_index} sealed and validated",
                {"workers": results, "completion_marker": str(marker)},
            )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            root = session_dir.expanduser().resolve()
            if not self.recording or self.active_session_dir != root:
                return AdapterResult(False, "MVS capture session is not active")
            if self.active_episode_dir is not None:
                return AdapterResult(
                    False,
                    f"MVS episode {self.active_episode_index} must be stopped before total end",
                )
            episodes = []
            for marker in sorted((root / "episodes").glob("episode_*/mvs_episode_complete.json")):
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if payload.get("complete") is not True:
                    return AdapterResult(False, f"incomplete MVS episode marker: {marker}")
                episodes.append(
                    {
                        "episode_index": int(payload["episode_index"]),
                        "directory": str(marker.parent.relative_to(root)),
                        "completion_marker": str(marker.relative_to(root)),
                    }
                )
            marker = root / "mvs_capture_complete.json"
            payload = {
                "schema": "instadatacap.mvs.capture_complete.v2",
                "complete": True,
                "stopped_unix_ns": time.time_ns(),
                "episodes": episodes,
            }
            temporary = marker.with_name(".mvs_capture_complete.json.tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(marker)
            self.recording = False
            self.active_session_dir = None
            return AdapterResult(
                True,
                f"MVS capture session ended with {len(episodes)} sealed episode(s); preview remains active",
                {"completion_marker": str(marker), "completion_status": "complete"},
            )

    def latest_preview_path(self, role: str) -> Path | None:
        try:
            normalized = self._normalize_role(role)
        except ValueError:
            return None
        with self.lock:
            if normalized not in self.capture_roles or not self._worker_alive_locked(normalized):
                return None
            path = self._preview_path(normalized)
            try:
                metadata = path.lstat()
            except OSError:
                return None
            if path.is_symlink() or not path.is_file() or metadata.st_size <= 4:
                return None
            return path

    def latest_preview_bytes(self, role: str) -> tuple[bytes, dict[str, Any]] | None:
        path = self.latest_preview_path(role)
        if path is None:
            return None
        try:
            payload = path.read_bytes()
            metadata = path.stat()
        except OSError:
            return None
        return payload, {"mtime_ns": metadata.st_mtime_ns, "size": metadata.st_size}

    def status(self) -> dict[str, Any]:
        with self.lock:
            cameras: list[dict[str, Any]] = []
            now_ns = time.time_ns()
            ready_count = 0
            connected_count = 0
            for role in self.capture_roles:
                alive = self._worker_alive_locked(role)
                ready = self._worker_ready_locked(role)
                connected_count += int(alive)
                ready_count += int(ready)
                state = dict(self.worker_states.get(role, {}))
                preview = self._preview_path(role)
                try:
                    stat_result = preview.stat()
                    available = preview.is_file() and not preview.is_symlink() and stat_result.st_size > 4
                    age_ms = max(0.0, (now_ns - stat_result.st_mtime_ns) / 1_000_000.0)
                    version = f"{stat_result.st_mtime_ns}-{stat_result.st_size}"
                except OSError:
                    available = False
                    age_ms = None
                    version = None
                cameras.append(
                    {
                        **state,
                        "role": role,
                        "serial": self.serials[role],
                        "role_locked": True,
                        "connected": alive,
                        "ready": ready,
                        "recording": bool(state.get("recording", False)),
                        "preview_running": alive,
                        "preview_available": available,
                        "preview_age_ms": age_ms,
                        "preview_version": version,
                        "stale": bool(alive and (not available or age_ms is None or age_ms > self.preview_stale_after_ms)),
                        "width": self.width,
                        "height": self.height,
                        "fps": self.fps,
                        "exposure_us": self.exposure_us,
                    }
                )
            stale = any(bool(camera["stale"]) for camera in cameras)
            return {
                "type": "mvs",
                "transport": "mvs_sdk",
                "protocol": self.protocol_name,
                "expected_cameras": self.expected_cameras,
                "connected_cameras": connected_count,
                "ready_cameras": ready_count,
                "capture_roles": list(self.capture_roles),
                "role_serials": {
                    role: self.serials[role] for role in self.capture_roles
                },
                "connected": connected_count == self.expected_cameras,
                "ready": ready_count == self.expected_cameras and not stale,
                "preview_running": connected_count > 0,
                "preview_stale_after_ms": self.preview_stale_after_ms,
                "stale": stale,
                "recording": self.active_episode_dir is not None,
                "session_armed": self.recording,
                "episode_recording": self.active_episode_dir is not None,
                "active_episode_index": self.active_episode_index,
                "source": f"Hikrobot MVS SDK {self.width}x{self.height} RGB @ {self.fps:g}Hz",
                "source_contract": {
                    "kind": "hikrobot_mvs_rgb",
                    "width": self.width,
                    "height": self.height,
                    "fps": self.fps,
                    "pixel_format": "RGB8",
                    "compression": "matroska_libx264rgb_crf0_rgb24_lossless",
                    "projection": "camera_native",
                    "layout": "one_independent_image_per_camera",
                    "frame_clock": (
                        "mvs_sdk_host_ms_minus_half_exposure_monotonic_with_labeled_host_fallback"
                    ),
                    "exposure_us": self.exposure_us,
                },
                "require_usb3": self.require_usb3,
                "cameras": cameras,
                "recent_logs": [
                    {"role": role, "line": line}
                    for role in self.capture_roles
                    for line in self.output_tails.get(role, [])[-8:]
                ][-20:],
            }

    def close(self) -> None:
        self.closing_event.set()
        with self.lock:
            if self.active_episode_dir is not None:
                self._parallel_request_locked(
                    "STOP", "stop", self.stop_timeout_sec
                )
            self.recording = False
            self.active_session_dir = None
            self.active_episode_dir = None
            self.active_episode_index = None
            self._disconnect_locked()

    @staticmethod
    def _hikrobot_usb_links() -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for path in sorted(Path("/sys/bus/usb/devices").glob("*")):
            try:
                vendor = (path / "idVendor").read_text(encoding="ascii").strip().lower()
                product = (path / "idProduct").read_text(encoding="ascii").strip().lower()
            except OSError:
                continue
            if vendor != "2bdf" or product != "0001":
                continue
            try:
                speed = float((path / "speed").read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                speed = 0.0
            try:
                usb_serial = (path / "serial").read_text(encoding="utf-8").strip()
            except OSError:
                usb_serial = ""
            links.append(
                {
                    "path": str(path),
                    "speed_mbps": speed,
                    "usb_descriptor_serial": usb_serial or None,
                }
            )
        return links

    def _validate_capture_output(
        self,
        root: Path,
        role: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        summary = response.get("summary")
        if not isinstance(summary, dict):
            raise RuntimeError(f"{role} worker returned no recording summary")
        if summary.get("role") != role or summary.get("serial") != self.serials[role]:
            raise RuntimeError(f"{role} worker summary identity does not match configuration")
        frame_count = summary.get("frame_count")
        if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
            raise RuntimeError(f"{role} worker reported invalid frame_count={frame_count!r}")
        directory = root / role
        manifest_path = directory / "manifest.json"
        video_path = directory / str(summary.get("video", ""))
        timestamps_path = directory / "camera_timestamps.csv"
        for path in (manifest_path, video_path, timestamps_path):
            if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
                raise RuntimeError(f"{role} output is missing or empty: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest != summary:
            raise RuntimeError(f"{role} on-disk manifest differs from worker summary")
        if (
            manifest.get("video") != "video.mkv"
            or manifest.get("video_container") != "matroska"
            or manifest.get("video_codec") != "libx264rgb"
            or manifest.get("video_pixel_format") != "rgb24"
            or manifest.get("video_lossless") is not True
            or len(str(manifest.get("rgb_sha256_c_order", ""))) != 64
        ):
            raise RuntimeError(f"{role} output does not satisfy the lossless RGB contract")
        with timestamps_path.open("r", encoding="utf-8", newline="") as file:
            rows = sum(1 for _line in file) - 1
        if rows != frame_count:
            raise RuntimeError(
                f"{role} timestamp rows={rows} do not match frame_count={frame_count}"
            )
        return summary

    def _preview_path(self, role: str) -> Path:
        return self.preview_dir / f"{role}.jpg"

    def _spawn_worker_locked(self, role: str) -> None:
        generation = self.worker_generations.get(role, 0) + 1
        started_ns = time.time_ns()
        command = [
            self.python_executable,
            str(self.worker_path),
            "--role", role,
            "--serial", self.serials[role],
            "--preview-path", str(self._preview_path(role)),
            "--exumi-root", str(self.exumi_root),
            "--fps", str(self.fps),
            "--exposure-us", str(self.exposure_us),
            "--width", str(self.width),
            "--height", str(self.height),
            "--raw-width", str(self.raw_width),
            "--raw-height", str(self.raw_height),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # MVSCameraProcess creates another Python process that owns the SDK
            # handle. A dedicated process group lets timeout cleanup terminate
            # both levels without leaving an exclusive camera handle behind.
            start_new_session=True,
        )
        self.processes[role] = process
        self.response_queues[role] = queue.Queue()
        self.output_tails[role] = []
        self.worker_generations[role] = generation
        self.worker_started_unix_ns[role] = started_ns
        relay = threading.Thread(
            target=self._relay_output,
            args=(role, generation, process),
            name=f"mvs-{role}-output",
            daemon=True,
        )
        relay.start()

    def _relay_output(self, role: str, generation: int, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            with self.state_lock:
                if self.worker_generations.get(role) != generation:
                    continue
                tail = self.output_tails.setdefault(role, [])
                tail.append(line)
                del tail[:-80]
            if not line.startswith(self.protocol_prefix):
                continue
            try:
                payload = json.loads(line[len(self.protocol_prefix) :])
            except json.JSONDecodeError:
                continue
            if payload.get("protocol") != self.protocol_name:
                continue
            with self.state_lock:
                if self.worker_generations.get(role) != generation:
                    continue
                self.worker_states[role] = {**self.worker_states.get(role, {}), **payload}
                response_queue = self.response_queues.get(role)
            if response_queue is not None and payload.get("action") in self.response_actions:
                response_queue.put(payload)

    def _send_locked(self, role: str, command: str) -> None:
        process = self.processes.get(role)
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError(f"{role} MVS worker is not running")
        process.stdin.write(command + "\n")
        process.stdin.flush()

    def _wait_response_locked(self, role: str, action: str, timeout: float) -> dict[str, Any] | None:
        responses = self.response_queues.get(role)
        if responses is None:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process = self.processes.get(role)
            if process is None or process.poll() is not None:
                return None
            try:
                payload = responses.get(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if payload.get("action") == action:
                return payload
            if payload.get("action") == "fatal":
                return payload
        return None

    def _request_one_locked(self, role: str, command: str, action: str, timeout: float) -> dict[str, Any] | None:
        try:
            self._send_locked(role, command)
        except Exception:
            return None
        return self._wait_response_locked(role, action, timeout)

    def _parallel_request_locked(
        self,
        command: str,
        action: str,
        timeout: float,
        roles: list[str] | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        selected = list(roles or self.capture_roles)
        results: dict[str, dict[str, Any] | None] = {}
        threads: list[threading.Thread] = []

        def request(role: str) -> None:
            results[role] = self._request_one_locked(role, command, action, timeout)

        for role in selected:
            thread = threading.Thread(target=request, args=(role,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout + 0.5)
        return {role: results.get(role) for role in selected}

    def _worker_alive_locked(self, role: str) -> bool:
        process = self.processes.get(role)
        return process is not None and process.poll() is None

    def _worker_ready_locked(self, role: str) -> bool:
        if not self._worker_alive_locked(role):
            return False
        with self.state_lock:
            state = self.worker_states.get(role, {})
            return bool(
                state.get("ready")
                and state.get("role") == role
                and state.get("serial") == self.serials.get(role)
            )

    def _terminate_worker_locked(self, role: str, graceful: bool = True) -> None:
        process = self.processes.get(role)
        if process is None:
            return
        if process.poll() is None and graceful:
            self._request_one_locked(role, "QUIT", "quit", self.command_timeout_sec)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self.processes.pop(role, None)
        self.response_queues.pop(role, None)
        with self.state_lock:
            self.worker_states.pop(role, None)
        try:
            self._preview_path(role).unlink()
        except FileNotFoundError:
            pass

    def _disconnect_locked(self, graceful: bool = True) -> AdapterResult:
        if self.recording:
            return AdapterResult(False, "cannot disconnect MVS cameras while recording")
        for role in list(self.capture_roles):
            self._terminate_worker_locked(role, graceful=graceful)
        return AdapterResult(True, "MVS camera workers disconnected")


__all__ = ["MvsCameraAdapter"]
