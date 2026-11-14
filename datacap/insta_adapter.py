from __future__ import annotations

import json
import math
import os
import queue
import signal
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _ble_event_log_path(session_dir: Path, capture_layout: str = "legacy") -> Path:
    """Return a layout-specific location for the shared BLE event log."""

    path = (
        session_dir / "metadata" / "insta" / "ble_events.jsonl"
        if capture_layout == "omniimu_v2"
        else session_dir / "insta_ble_events.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AdapterResult:
    ok: bool
    message: str
    extra: dict[str, Any] | None = None


class InstaAdapter:
    def scan_cameras(self) -> AdapterResult:
        return AdapterResult(
            False, f"{self.__class__.__name__} does not support Bluetooth scanning"
        )

    def connect_cameras(self) -> AdapterResult:
        return AdapterResult(
            False, f"{self.__class__.__name__} does not support Bluetooth connection"
        )

    def prepare(self, session_dir: Path) -> AdapterResult:
        raise NotImplementedError

    def start_recording(self, session_dir: Path) -> AdapterResult:
        raise NotImplementedError

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        raise NotImplementedError

    def start_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        """Start optional episode-scoped media.

        Most adapters (for example the continuously sampled HTC recorder) do
        not need an extra episode lifecycle.  Camera backends that promise one
        independently sealed directory per episode override this method.
        """

        del session_dir, episode_index
        return AdapterResult(True, "episode-scoped recording is not required")

    def stop_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        del session_dir, episode_index
        return AdapterResult(True, "episode-scoped recording is not required")

    def reset_visualization(self, session_dir: Path) -> AdapterResult:
        del session_dir
        return AdapterResult(
            True, "visualization reset is not required for this adapter"
        )

    def status(self) -> dict[str, Any]:
        return {"type": self.__class__.__name__}

    def close(self) -> None:
        pass


class DryRunInstaAdapter(InstaAdapter):
    def __init__(self) -> None:
        self.recording = False

    def prepare(self, session_dir: Path) -> AdapterResult:
        return AdapterResult(
            True, "dry-run Insta adapter prepared", {"session_dir": str(session_dir)}
        )

    def start_recording(self, session_dir: Path) -> AdapterResult:
        self.recording = True
        return AdapterResult(
            True, "dry-run Insta recording started", {"session_dir": str(session_dir)}
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        self.recording = False
        return AdapterResult(
            True, "dry-run Insta recording stopped", {"session_dir": str(session_dir)}
        )

    def status(self) -> dict[str, Any]:
        return {"type": "dry_run", "connected": True, "recording": self.recording}


class BlueZDirectInstaAdapter(InstaAdapter):
    """Persistent adapter for Ubuntu acting as a BLE central to BE80 cameras."""

    protocol_prefix = "DATACAP_BLE_DIRECT_JSON "

    def __init__(
        self,
        worker_path: Path,
        python_executable: str = "/usr/bin/python3",
        adapter: str = "hci0",
        expected_cameras: int = 1,
        require_all: bool = True,
        addresses: list[str] | None = None,
        camera_roles: list[str] | None = None,
        role_by_address: dict[str, str] | None = None,
        name_prefixes: list[str] | None = None,
        ready_timeout_sec: float = 10.0,
        discovery_timeout_sec: float = 15.0,
        command_timeout_sec: float = 8.0,
        ack_timeout_sec: float = 3.0,
        simulate_worker: bool = False,
        capture_layout: str = "legacy",
    ) -> None:
        self.worker_path = worker_path.resolve()
        self.python_executable = python_executable
        self.adapter_name = adapter
        self.expected_cameras = max(1, expected_cameras)
        self.require_all = require_all
        self.configured_addresses = [
            str(address).upper() for address in (addresses or [])
        ]
        self.addresses = list(self.configured_addresses)
        self.camera_roles = list(camera_roles or ["insta_right", "insta_left"])
        self.capture_roles = list(self.camera_roles[: self.expected_cameras])
        self.role_by_address = {
            str(address).upper(): str(role)
            for address, role in (role_by_address or {}).items()
        }
        self.name_prefixes = list(name_prefixes or ["X3 "])
        self.ready_timeout_sec = ready_timeout_sec
        self.discovery_timeout_sec = discovery_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.ack_timeout_sec = ack_timeout_sec
        self.simulate_worker = simulate_worker
        if capture_layout not in {"legacy", "omniimu_v2"}:
            raise ValueError(f"unsupported Insta capture layout: {capture_layout}")
        self.capture_layout = capture_layout
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.output_tail: list[str] = []
        self.worker_state: dict[str, Any] = {}
        self.worker_events: list[dict[str, Any]] = []
        self.recording = False
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.closing_event = threading.Event()
        self.auto_reconnect_armed = False
        self.reconnect_failures = 0
        self.next_reconnect_monotonic = 0.0

    def set_capture_roles(self, roles: list[str]) -> None:
        """Select the physical cameras used by the next capture session."""

        selected = [str(role).strip() for role in roles if str(role).strip()]
        if not selected:
            raise ValueError("at least one Insta camera role is required")
        with self.lock:
            if self.recording:
                raise RuntimeError("cannot change Insta camera roles while recording")
            mapped_addresses = [
                address
                for address, role in self.role_by_address.items()
                if role in selected
            ]
            if len(selected) == 1 and not mapped_addresses:
                raise ValueError(
                    f"camera role {selected[0]!r} has no configured Bluetooth address"
                )
            self.capture_roles = selected
            self.expected_cameras = len(selected)
            self.addresses = mapped_addresses or list(self.configured_addresses)
            # Worker configuration is fixed at launch. Restarting here is
            # intentional and happens only while total capture is idle.
            if self.process is not None:
                self.close()

    def scan_cameras(self) -> AdapterResult:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                launched = self._launch()
                if not launched.ok:
                    return launched
            response = self._request("SCAN 5", "scan", 8.0)
            if response is None:
                return AdapterResult(
                    False,
                    "Bluetooth scan timed out",
                    {"output_tail": self.output_tail[-30:]},
                )
            response = self._decorate_camera_payload(response)
            return AdapterResult(
                bool(response.get("ok")),
                response.get("message", "Bluetooth scan complete"),
                response,
            )

    def connect_cameras(self) -> AdapterResult:
        with self.lock:
            self.auto_reconnect_armed = True
            if self.process is None or self.process.poll() is not None:
                launched = self._launch()
                if not launched.ok:
                    self._schedule_reconnect(False)
                    return launched
            response = self._request(
                "CONNECT",
                "connect",
                self.discovery_timeout_sec + self.command_timeout_sec,
            )
            if response is None:
                self._schedule_reconnect(False)
                return AdapterResult(
                    False,
                    "Bluetooth connection timed out",
                    {"output_tail": self.output_tail[-30:]},
                )
            response = self._decorate_camera_payload(response)
            result = AdapterResult(
                bool(response.get("ok")),
                response.get("message", "Bluetooth connection complete"),
                response,
            )
            self._schedule_reconnect(result.ok)
            return result

    def prepare(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            self.auto_reconnect_armed = True
            if self.process is None or self.process.poll() is not None:
                launched = self._launch()
                if not launched.ok:
                    self._schedule_reconnect(False)
                    return launched
            session = self._request(
                "SESSION "
                + json.dumps(
                    str(_ble_event_log_path(session_dir, self.capture_layout)),
                    ensure_ascii=False,
                ),
                "session",
                self.command_timeout_sec,
            )
            if session is None or not session.get("ok"):
                self._schedule_reconnect(False)
                return AdapterResult(
                    False,
                    "direct BLE worker did not accept the session log",
                    session or {"output_tail": self.output_tail[-30:]},
                )
            connected = self._request(
                "CONNECT",
                "connect",
                self.discovery_timeout_sec + self.command_timeout_sec,
            )
            if connected is None:
                self._schedule_reconnect(False)
                return AdapterResult(
                    False,
                    "direct BLE camera discovery/connection timed out",
                    {"output_tail": self.output_tail[-30:]},
                )
            connected = self._decorate_camera_payload(connected)
            result = AdapterResult(
                bool(connected.get("ok")),
                connected.get("message", "direct BLE cameras prepared"),
                connected,
            )
            if result.ok:
                self._schedule_reconnect(True)
                self._prepare_transfer_layout(session_dir, connected)
            else:
                self._schedule_reconnect(False)
            return result

    def maintain_connection(self) -> AdapterResult | None:
        """Recover an explicitly connected camera set while capture is idle."""

        with self.lock:
            if self.recording or not self.auto_reconnect_armed:
                return None
            if time.monotonic() < self.next_reconnect_monotonic:
                return None
            current = self.status()
            if current.get("ready"):
                self._schedule_reconnect(True)
                return None
            # Reserve the next attempt before releasing the lock so concurrent
            # maintenance/status calls cannot start duplicate BlueZ connects.
            self.next_reconnect_monotonic = time.monotonic() + 2.0
        return self.connect_cameras()

    def _schedule_reconnect(self, ok: bool) -> None:
        if ok:
            self.reconnect_failures = 0
            self.next_reconnect_monotonic = 0.0
            return
        self.reconnect_failures += 1
        delay = min(30.0, float(2 ** min(self.reconnect_failures, 5)))
        self.next_reconnect_monotonic = time.monotonic() + delay

    def start_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("START", "start", self.command_timeout_sec)
        if response is None:
            return AdapterResult(
                False,
                "direct BLE start command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        response = self._decorate_camera_payload(response)
        if response.get("ok"):
            self.recording = True
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "direct BLE start response"),
            response,
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("STOP", "stop", self.command_timeout_sec)
        if response is None:
            return AdapterResult(
                False,
                "direct BLE stop command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        response = self._decorate_camera_payload(response)
        if response.get("ok"):
            self.recording = False
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "direct BLE stop response"),
            response,
        )

    def status(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        with self.state_lock:
            state = dict(self.worker_state)
            recent_logs = list(self.worker_events[-20:])
        state = self._decorate_camera_payload(state)
        status = {
            "type": "bluez_direct_simulated"
            if self.simulate_worker
            else "bluez_direct_camera_ble",
            "transport": "simulated_bluetooth_le"
            if self.simulate_worker
            else "bluetooth_le",
            "protocol": "insta360_be80_phone_command",
            "direct_camera_ble": True,
            "connected": alive and bool(state.get("connected", False)),
            "ready": alive and bool(state.get("ready", False)),
            "recording": self.recording,
            "worker": str(self.worker_path),
            "recent_logs": recent_logs,
            **state,
        }
        status["expected_cameras"] = self.expected_cameras
        status["capture_roles"] = list(self.capture_roles)
        status["auto_reconnect_armed"] = self.auto_reconnect_armed
        status["reconnect_failures"] = self.reconnect_failures
        for key in ("cameras", "discovered_cameras"):
            status[key] = [
                camera
                for camera in status.get(key, [])
                if camera.get("role") in self.capture_roles
            ]
        if not alive:
            status["connected"] = False
            status["ready"] = False
            status["recording"] = False
            status["connected_cameras"] = 0
            status["cameras"] = []
            status["discovered_cameras"] = [
                {**camera, "connected": False}
                for camera in status["discovered_cameras"]
            ]
        return status

    def close(self) -> None:
        # A CONNECT request can legitimately wait for the full discovery
        # timeout while holding ``self.lock``. Signal cancellation before
        # acquiring that lock so application shutdown never waits behind a
        # camera discovery attempt.
        self.closing_event.set()
        with self.lock:
            process = self.process
            if process is None:
                return
            if process.poll() is None and self.recording:
                try:
                    self._request("STOP", "stop", self.command_timeout_sec)
                except Exception:
                    pass
            self.process = None
            self.recording = False
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write("QUIT\n")
                        process.stdin.flush()
                    process.wait(timeout=4.0)
                except Exception:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except Exception:
                        process.kill()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def _launch(self) -> AdapterResult:
        self.closing_event.clear()
        if not self.worker_path.is_file():
            return AdapterResult(
                False, f"direct BLE worker not found: {self.worker_path}"
            )
        command = [
            self.python_executable,
            str(self.worker_path),
            "--adapter",
            self.adapter_name,
            "--expected-cameras",
            str(self.expected_cameras),
            "--discovery-timeout-sec",
            str(self.discovery_timeout_sec),
            "--ack-timeout-sec",
            str(self.ack_timeout_sec),
            "--require-all" if self.require_all else "--no-require-all",
        ]
        for address in self.addresses:
            command.extend(["--address", address])
        for address, role in self.role_by_address.items():
            command.extend(["--camera-role", f"{address}={role}"])
        for prefix in self.name_prefixes:
            command.extend(["--name-prefix", prefix])
        if self.simulate_worker:
            command.append("--simulate")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self.process = None
            return AdapterResult(
                False, f"failed to launch direct BLE worker: {exc}", {"cmd": command}
            )
        threading.Thread(
            target=self._relay_output, name="InstaBLEDirectOutput", daemon=True
        ).start()
        response = self._wait_response("ready", self.ready_timeout_sec)
        if response is None:
            self.close()
            return AdapterResult(
                False,
                "direct BLE worker did not become ready",
                {"cmd": command, "output_tail": self.output_tail[-30:]},
            )
        if not response.get("ok"):
            self.close()
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "direct BLE worker ready"),
            response,
        )

    def _camera_role(self, camera: dict[str, Any], index: int) -> str:
        address = str(camera.get("address", "")).upper()
        mapped = self.role_by_address.get(address)
        if mapped:
            return mapped
        if index < len(self.capture_roles):
            return self.capture_roles[index]
        return "insta_unassigned"

    def _decorate_camera_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(payload)
        for field in ("cameras", "discovered_cameras", "camera_results"):
            values = payload.get(field)
            if not isinstance(values, list):
                continue
            decorated[field] = [
                {**item, "role": self._camera_role(item, index)}
                if isinstance(item, dict)
                else item
                for index, item in enumerate(values)
            ]
        return decorated

    def _prepare_transfer_layout(
        self, session_dir: Path, connected: dict[str, Any]
    ) -> None:
        if self.capture_layout == "omniimu_v2":
            manifest_dir = session_dir / "raw"
            destination_root = Path("raw")
            schema = "instaumi-camera-transfer-v3"
        else:
            manifest_dir = session_dir / "insta"
            destination_root = Path("insta")
            schema = "instaumi-camera-transfer-v2"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        cameras = list(connected.get("cameras", []))
        destinations: list[dict[str, Any]] = []
        assigned_roles: set[str] = set()
        for index, camera in enumerate(cameras):
            role = str(camera.get("role") or self._camera_role(camera, index))
            if role not in self.capture_roles:
                continue
            if role in assigned_roles:
                raise ValueError(f"duplicate Insta camera role: {role}")
            assigned_roles.add(role)
            # Raw media belongs to the physical Tracker/camera, not to an
            # episode and not to a processed trajectory directory.
            relative = destination_root / role
            (session_dir / relative).mkdir(parents=True, exist_ok=True)
            destinations.append(
                {
                    "role": role,
                    "name": camera.get("name"),
                    "address": camera.get("address"),
                    "destination": str(relative),
                    "copy_patterns": ["*.insv", "*.lrv", "*.mp4", "*.sync.json"],
                }
            )
        manifest = {
            "schema": schema,
            "session_dir": str(session_dir),
            "camera_count": len(destinations),
            "cameras": destinations,
            "postprocess_root": str(session_dir),
            "postprocess_program": str(
                Path(__file__).resolve().parents[1] / "dataprocess"
            ),
            "note": "Copy each camera's files into its destination directory after total end.",
        }
        (manifest_dir / "transfer_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _request(
        self, command: str, action: str, timeout: float
    ) -> dict[str, Any] | None:
        with self.lock:
            if (
                self.process is None
                or self.process.poll() is not None
                or self.process.stdin is None
            ):
                return None
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except Exception:
                return None
            return self._wait_response(action, timeout)

    def _relay_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if not line.startswith(self.protocol_prefix):
                self.output_tail.append(line)
                if len(self.output_tail) > 200:
                    del self.output_tail[:50]
                print(f"[insta-ble-direct] {line}", flush=True)
                continue
            raw_payload = line[len(self.protocol_prefix) :]
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                print(f"[insta-ble-direct] {line}", flush=True)
                continue
            with self.state_lock:
                self.worker_state = payload
                if payload.get("action") == "telemetry":
                    continue
                self.worker_events.append(payload)
                if len(self.worker_events) > 80:
                    del self.worker_events[:20]
            self.output_tail.append(line)
            if len(self.output_tail) > 200:
                del self.output_tail[:50]
            print(f"[insta-ble-direct] {line}", flush=True)
            self.output_queue.put(raw_payload)

    def _wait_response(self, action: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.closing_event.is_set():
                return None
            try:
                raw = self.output_queue.get(
                    timeout=min(0.1, max(0.01, deadline - time.monotonic()))
                )
                payload = json.loads(raw)
            except queue.Empty:
                continue
            except json.JSONDecodeError:
                continue
            if payload.get("action") == action:
                return payload
        return None


class BlueZRemoteInstaAdapter(InstaAdapter):
    """Persistent adapter for the independent BlueZ GPS-remote worker.

    This is deliberately named ``Remote``: it is a BLE peripheral emulating an
    Insta360 GPS Action Remote, not the proprietary iOS/Android direct-camera
    BLE client used by ``instaapp``.
    """

    protocol_prefix = "DATACAP_BLE_JSON "

    def __init__(
        self,
        worker_path: Path,
        python_executable: str = "/usr/bin/python3",
        adapter: str = "hci0",
        expected_cameras: int = 2,
        ready_timeout_sec: float = 10.0,
        command_timeout_sec: float = 5.0,
        allow_unsubscribed: bool = False,
        simulate_worker: bool = False,
    ) -> None:
        self.worker_path = worker_path.resolve()
        self.python_executable = python_executable
        self.adapter_name = adapter
        self.expected_cameras = max(1, expected_cameras)
        self.ready_timeout_sec = ready_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.allow_unsubscribed = allow_unsubscribed
        self.simulate_worker = simulate_worker
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.output_tail: list[str] = []
        self.worker_state: dict[str, Any] = {}
        self.recording = False
        self.lock = threading.RLock()

    def prepare(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                launched = self._launch()
                if not launched.ok:
                    return launched
            response = self._request(
                "SESSION "
                + json.dumps(str(_ble_event_log_path(session_dir)), ensure_ascii=False),
                "session",
                self.command_timeout_sec,
            )
            if response is None:
                return AdapterResult(
                    False,
                    "BlueZ worker did not accept the session log",
                    {"output_tail": self.output_tail[-30:]},
                )
            return AdapterResult(
                bool(response.get("ok")),
                response.get("message", "BlueZ remote prepared"),
                response,
            )

    def start_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("START", "start", self.command_timeout_sec)
        if response is None:
            return AdapterResult(
                False,
                "BlueZ start command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        if response.get("ok"):
            self.recording = True
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "BlueZ start response"),
            response,
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("STOP", "stop", self.command_timeout_sec)
        if response is None:
            return AdapterResult(
                False,
                "BlueZ stop command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        if response.get("ok"):
            self.recording = False
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "BlueZ stop response"),
            response,
        )

    def status(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        state = dict(self.worker_state)
        return {
            "type": "bluez_remote_simulated"
            if self.simulate_worker
            else "bluez_gps_action_remote",
            "transport": "simulated_bluetooth_le"
            if self.simulate_worker
            else "bluetooth_le",
            "protocol": "gps_action_remote_emulation",
            "direct_camera_ble": False,
            "connected": alive and bool(state.get("connected", False)),
            "ready": alive and bool(state.get("ready", False)),
            "recording": self.recording,
            "state_verified": False,
            "worker": str(self.worker_path),
            **state,
        }

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            self.recording = False
            if process is None:
                return
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write("QUIT\n")
                        process.stdin.flush()
                    process.wait(timeout=3.0)
                except Exception:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except Exception:
                        process.kill()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def _launch(self) -> AdapterResult:
        if not self.worker_path.is_file():
            return AdapterResult(
                False, f"BlueZ Insta worker not found: {self.worker_path}"
            )
        command = [
            self.python_executable,
            str(self.worker_path),
            "--adapter",
            self.adapter_name,
            "--expected-cameras",
            str(self.expected_cameras),
        ]
        if self.allow_unsubscribed:
            command.append("--allow-unsubscribed")
        if self.simulate_worker:
            command.append("--simulate")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self.process = None
            return AdapterResult(
                False, f"failed to launch BlueZ Insta worker: {exc}", {"cmd": command}
            )
        threading.Thread(
            target=self._relay_output, name="InstaBLEWorkerOutput", daemon=True
        ).start()
        response = self._wait_response("ready", self.ready_timeout_sec)
        if response is None:
            self.close()
            return AdapterResult(
                False,
                "BlueZ Insta worker did not become ready",
                {"cmd": command, "output_tail": self.output_tail[-30:]},
            )
        if not response.get("ok"):
            self.close()
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "BlueZ worker ready"),
            response,
        )

    def _request(
        self, command: str, action: str, timeout: float
    ) -> dict[str, Any] | None:
        with self.lock:
            if (
                self.process is None
                or self.process.poll() is not None
                or self.process.stdin is None
            ):
                return None
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except Exception:
                return None
            return self._wait_response(action, timeout)

    def _relay_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            self.output_tail.append(line)
            if len(self.output_tail) > 200:
                del self.output_tail[:50]
            print(f"[insta-ble] {line}", flush=True)
            if not line.startswith(self.protocol_prefix):
                continue
            raw_payload = line[len(self.protocol_prefix) :]
            try:
                self.worker_state = json.loads(raw_payload)
            except json.JSONDecodeError:
                continue
            self.output_queue.put(raw_payload)

    def _wait_response(self, action: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.output_queue.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
                payload = json.loads(raw)
            except queue.Empty:
                return None
            except json.JSONDecodeError:
                continue
            if payload.get("action") == action:
                return payload
        return None


class CameraSDKInstaAdapter(InstaAdapter):
    """Persistent adapter for the C++ CameraSDK worker.

    CameraSDK requires the same process to retain the open camera session between
    StartRecording and StopRecording. The worker therefore stays alive while
    Python sends line-based commands to it.
    """

    protocol_prefix = "DATACAP_JSON "

    def __init__(
        self,
        worker_path: Path,
        sdk_lib_dir: Path,
        resolution: str = "5.7k60",
        bitrate_mbps: int = 120,
        serial: str = "",
        timeout_sec: float = 20.0,
    ) -> None:
        self.worker_path = worker_path.resolve()
        self.sdk_lib_dir = sdk_lib_dir.resolve()
        self.resolution = resolution
        self.bitrate_mbps = bitrate_mbps
        self.serial = serial
        self.timeout_sec = timeout_sec
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.output_tail: list[str] = []
        self.lock = threading.RLock()
        self.camera_info: dict[str, Any] = {}
        self.recording = False

    def prepare(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return AdapterResult(
                    True, "CameraSDK worker already ready", self.camera_info
                )
            if not self.worker_path.exists():
                return AdapterResult(
                    False,
                    f"CameraSDK worker not found: {self.worker_path}",
                    {"hint": "run datacap/camera_sdk/build_worker.sh on Ubuntu"},
                )
            if not self.sdk_lib_dir.exists():
                return AdapterResult(
                    False, f"CameraSDK library directory not found: {self.sdk_lib_dir}"
                )

            command = [
                str(self.worker_path),
                "--resolution",
                self.resolution,
                "--bitrate-mbps",
                str(self.bitrate_mbps),
            ]
            if self.serial:
                command.extend(["--serial", self.serial])

            env = os.environ.copy()
            old_ld_path = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = str(self.sdk_lib_dir) + (
                ":" + old_ld_path if old_ld_path else ""
            )
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(session_dir),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
            except Exception as exc:
                self.process = None
                return AdapterResult(
                    False, f"failed to launch CameraSDK worker: {exc}", {"cmd": command}
                )

            threading.Thread(
                target=self._relay_output, name="InstaWorkerOutput", daemon=True
            ).start()
            response = self._wait_response("ready", self.timeout_sec)
            if response is None:
                self.close()
                return AdapterResult(
                    False,
                    "CameraSDK worker did not become ready",
                    {"output_tail": self.output_tail[-30:]},
                )
            self.camera_info = response
            return AdapterResult(
                bool(response.get("ok")),
                response.get("message", "camera ready"),
                response,
            )

    def start_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("START")
        if response is None:
            return AdapterResult(
                False,
                "CameraSDK start command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        self.recording = bool(response.get("ok"))
        return AdapterResult(
            self.recording, response.get("message", "Insta start response"), response
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        response = self._request("STOP")
        if response is None:
            return AdapterResult(
                False,
                "CameraSDK stop command timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        if response.get("ok"):
            self.recording = False
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "Insta stop response"),
            response,
        )

    def status(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {
            "type": "camera_sdk",
            "connected": alive and bool(self.camera_info.get("ok")),
            "recording": self.recording,
            "camera": self.camera_info,
            "worker": str(self.worker_path),
        }

    def close(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            self.recording = False
            if process is None:
                return
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write("QUIT\n")
                        process.stdin.flush()
                    process.wait(timeout=3.0)
                except Exception:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except Exception:
                        process.kill()

    def _request(self, command: str) -> dict[str, Any] | None:
        with self.lock:
            if (
                self.process is None
                or self.process.poll() is not None
                or self.process.stdin is None
            ):
                return None
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except Exception:
                return None
            return self._wait_response(command.lower(), self.timeout_sec)

    def _relay_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            self.output_tail.append(line)
            if len(self.output_tail) > 200:
                del self.output_tail[:50]
            print(f"[insta-sdk] {line}", flush=True)
            if line.startswith(self.protocol_prefix):
                self.output_queue.put(line[len(self.protocol_prefix) :])

    def _wait_response(self, action: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.output_queue.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
                payload = json.loads(raw)
            except queue.Empty:
                return None
            except json.JSONDecodeError:
                continue
            if payload.get("action") == action:
                return payload
        return None


class UsbWebcamInstaAdapter(InstaAdapter):
    """Explicit, fail-closed Insta360 X3/X5 USB Webcam adapter.

    The adapter never discovers or opens arbitrary V4L2 nodes. ``devices`` is
    an operator-provided role-to-stable-path mapping, and the only device-side
    check performed by :meth:`scan_cameras` is ``stat``/``realpath`` identity
    validation. One external worker owns each selected device and publishes a
    small preview while idle. ``view_mode`` selects one of the two native UVC
    profiles on the camera itself. Neither preview nor recording reprojects the
    pixels on the PC.
    """

    protocol_prefix = "DATACAP_UVC_JSON "
    protocol_name = "instadatacap-uvc-worker-v1"
    response_actions = frozenset({"ready", "start", "stop", "status", "quit"})
    preview_stale_after_ms = 1500.0
    supported_roles = frozenset({"insta_right", "insta_left"})
    supported_view_modes = frozenset({"panorama", "flat"})
    source_profiles = {
        "panorama": {
            "view_mode": "panorama",
            "width": 2880,
            "height": 1440,
            "fps": 30,
            "pixel_format": "MJPG",
            "semantic": "in_camera_2to1_panorama",
            "projection": "equirectangular_2to1",
            "layout": "stitched_2to1",
            "kind": "uvc_equirectangular_mjpeg",
            "source": "2880x1440 MJPG30 native_panorama",
            "camera_processing": "in_camera_stitch_and_stabilization_baked_into_pixels",
        },
        "flat": {
            "view_mode": "flat",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "pixel_format": "MJPG",
            "semantic": "in_camera_native_flat",
            "projection": "native_flat_profile",
            "layout": "dual_fisheye_vertical_stack",
            "kind": "uvc_native_flat_mjpeg",
            "source": "1920x1080 MJPG30 native_flat",
            "camera_processing": "native_dual_fisheye_no_host_projection",
        },
    }

    def __init__(
        self,
        *,
        devices: dict[str, str | Path],
        worker_path: Path,
        preview_dir: Path,
        view_mode: str = "panorama",
        required_camera_model: str | None = None,
        flat_projection: dict[str, Any] | None = None,
        ready_timeout_sec: float = 15.0,
        command_timeout_sec: float = 8.0,
        stop_timeout_sec: float = 120.0,
    ) -> None:
        if not isinstance(devices, dict) or not devices:
            raise ValueError(
                "USB webcam devices must be a non-empty role-to-path mapping"
            )

        normalized_devices: dict[str, Path] = {}
        configured_paths: set[str] = set()
        for raw_role, raw_path in devices.items():
            role = self._normalize_role(raw_role)
            if role in normalized_devices:
                raise ValueError(f"duplicate USB webcam role: {role}")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise ValueError(
                    f"USB webcam path for {role} must be explicit and absolute: {path}"
                )
            normalized = os.path.abspath(os.fspath(path))
            if normalized in configured_paths:
                raise ValueError(f"duplicate configured USB webcam path: {normalized}")
            configured_paths.add(normalized)
            normalized_devices[role] = Path(normalized)

        if (
            ready_timeout_sec <= 0.0
            or command_timeout_sec <= 0.0
            or stop_timeout_sec <= 0.0
        ):
            raise ValueError("USB webcam worker timeouts must be positive")

        self.devices = normalized_devices
        self.worker_path = worker_path.expanduser().resolve()
        self.preview_dir = preview_dir.expanduser().resolve()
        self.ready_timeout_sec = float(ready_timeout_sec)
        self.command_timeout_sec = float(command_timeout_sec)
        self.stop_timeout_sec = float(stop_timeout_sec)
        self.view_mode = self._normalize_view_mode(view_mode)
        self.required_camera_model = (
            str(required_camera_model).strip() if required_camera_model is not None else ""
        )
        if self.required_camera_model not in {"", "Insta360 X3", "Insta360 X5"}:
            raise ValueError(
                "USB webcam required_camera_model must be 'Insta360 X3' or "
                "'Insta360 X5'"
            )
        if self.required_camera_model == "Insta360 X3" and self.view_mode != "flat":
            raise ValueError(
                "Insta360 X3 USB Webcam requires view_mode='flat'; its UVC "
                "stream is not an in-camera stitched panorama"
            )
        # Accepted only so an existing config can be upgraded without failing
        # at startup. Flat mode is now a direct UVC profile and these legacy PC
        # reprojection parameters are deliberately ignored.
        del flat_projection
        self.capture_roles = list(normalized_devices)
        self.expected_cameras = len(self.capture_roles)

        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.output_queues: dict[str, queue.Queue[str]] = {}
        self.output_tails: dict[str, list[str]] = {}
        self.worker_states: dict[str, dict[str, Any]] = {}
        self.worker_generations: dict[str, int] = {}
        self.worker_locks = {role: threading.RLock() for role in normalized_devices}
        self.device_identities: dict[str, dict[str, Any]] = {}
        self.worker_started_unix_ns: dict[str, int] = {}
        self.active_session_dir: Path | None = None
        self.recording = False
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        # Status polling and application shutdown must remain responsive while
        # connect_cameras() waits for the first preview frame under ``lock``.
        self.status_cache_lock = threading.Lock()
        self.status_cache: dict[str, Any] = {}
        self.closing_event = threading.Event()

    @classmethod
    def _normalize_role(cls, value: Any) -> str:
        role = str(value).strip().lower()
        if role not in cls.supported_roles:
            expected = ", ".join(sorted(cls.supported_roles))
            raise ValueError(
                f"unsupported USB webcam role {value!r}; expected one of {expected}"
            )
        return role

    @classmethod
    def _normalize_view_mode(cls, value: Any) -> str:
        mode = str(value).strip().lower()
        if mode not in cls.supported_view_modes:
            raise ValueError("USB webcam view_mode must be 'panorama' or 'flat'")
        return mode

    def _source_profile(self) -> dict[str, Any]:
        return dict(self.source_profiles[self.view_mode])

    def _source_contract(self) -> dict[str, Any]:
        profile = self._source_profile()
        contract = {
            "view_mode": profile["view_mode"],
            "width": profile["width"],
            "height": profile["height"],
            "pixel_format": profile["pixel_format"],
            "fps": profile["fps"],
            "semantic": profile["semantic"],
            "projection": profile["projection"],
            "layout": profile["layout"],
            "kind": profile["kind"],
            "compression": "lossy_camera_mjpeg",
            "camera_processing": profile["camera_processing"],
        }
        runtime = self._runtime_camera_contract_locked()
        contract.update(runtime)
        return contract

    def _runtime_camera_contract_locked(self) -> dict[str, Any]:
        """Return model/FlowState facts reported by the live owning workers."""

        with self.state_lock:
            states = [dict(self.worker_states.get(role, {})) for role in self.capture_roles]
        models = {
            str(state.get("camera_model", "")).strip()
            for state in states
            if str(state.get("camera_model", "")).strip()
        }
        camera_model = next(iter(models)) if len(models) == 1 else "unknown"
        if camera_model == "Insta360 X3":
            stabilization = "unsupported_in_x3_webcam"
            flowstate_supported = False
            flowstate_status = "unsupported_in_x3_webcam"
        elif camera_model == "Insta360 X5" and self.view_mode == "panorama":
            stabilization = "baked_in"
            flowstate_supported = True
            flowstate_status = (
                "in_camera_processing_baked_into_uvc_pixels_"
                "not_controllable_or_verified_via_uvc"
            )
        else:
            stabilization = "not_verified"
            flowstate_supported = None
            flowstate_status = "not_verified"
        return {
            "camera_model": camera_model,
            "stabilization": stabilization,
            "flowstate_supported_in_webcam": flowstate_supported,
            "flowstate_verified": False,
            "flowstate_status": flowstate_status,
        }

    def set_capture_roles(self, roles: list[str]) -> None:
        selected = [self._normalize_role(role) for role in roles]
        if not selected:
            raise ValueError("at least one USB webcam role is required")
        if len(set(selected)) != len(selected):
            raise ValueError("USB webcam capture roles must be unique")
        missing = [role for role in selected if role not in self.devices]
        if missing:
            raise ValueError(
                f"USB webcam roles have no configured device: {', '.join(missing)}"
            )
        with self.lock:
            if self.recording:
                raise RuntimeError("cannot change USB webcam roles while recording")
            if selected == self.capture_roles:
                return
            # Role selection is an operator action made while idle. Closing all
            # workers prevents an unselected camera from remaining open and
            # mirrors the existing BLE adapter's fail-closed reconfiguration.
            self._disconnect_locked()
            self.capture_roles = selected
            self.expected_cameras = len(selected)
            self.device_identities = {}

    def set_view_mode(self, view_mode: str) -> AdapterResult:
        normalized_mode = self._normalize_view_mode(view_mode)
        if (
            self.required_camera_model == "Insta360 X3"
            and normalized_mode != "flat"
        ):
            return AdapterResult(
                False,
                "Insta360 X3 Webcam exposes only the native flat dual-fisheye "
                "profile; in-camera panorama is unavailable",
                {"view_mode": self.view_mode},
            )
        with self.lock:
            if self.recording:
                raise RuntimeError("cannot change USB webcam view mode while recording")
            profile = dict(self.source_profiles[normalized_mode])
            if normalized_mode == self.view_mode:
                return AdapterResult(
                    True,
                    f"USB webcam already uses native {normalized_mode} profile",
                    {"view_mode": normalized_mode, "source_profile": profile},
                )
            workers_were_running = any(
                self._worker_alive_locked(role) for role in self.capture_roles
            )
            if workers_were_running:
                disconnected = self._disconnect_locked()
                if not disconnected.ok:
                    return AdapterResult(
                        False,
                        "could not stop the current USB profile: "
                        + disconnected.message,
                        disconnected.extra,
                    )
            self.view_mode = normalized_mode
            with self.status_cache_lock:
                self.status_cache = {}
            if workers_were_running:
                connected = self.connect_cameras()
                if not connected.ok:
                    return AdapterResult(
                        False,
                        f"native {normalized_mode} profile selected, but preview restart failed: "
                        + connected.message,
                        {"view_mode": normalized_mode, "source_profile": profile},
                    )
            return AdapterResult(
                True,
                f"USB webcam now reads the native {normalized_mode} profile directly",
                {"view_mode": normalized_mode, "source_profile": profile},
            )

    def scan_cameras(self) -> AdapterResult:
        """Validate only configured filesystem identities; never open a node."""

        with self.lock:
            cameras: list[dict[str, Any]] = []
            errors: list[str] = []
            identities: dict[str, dict[str, Any]] = {}
            self.device_identities = {}
            realpath_owner: dict[str, str] = {}
            inode_owner: dict[tuple[int, int], str] = {}
            device_node_owner: dict[int, str] = {}

            for role in self.capture_roles:
                configured = self.devices[role]
                camera: dict[str, Any] = {
                    "role": role,
                    "device": str(configured),
                    "configured_device": str(configured),
                    "connected": self._worker_alive_locked(role),
                    "preview_path": str(self._preview_path(role)),
                }
                raw_video_node = (
                    configured.parent == Path("/dev")
                    and configured.name.startswith("video")
                    and configured.name.removeprefix("video").isdigit()
                )
                if raw_video_node:
                    error = (
                        f"{role}: raw {configured} is not a stable device identity; "
                        "configure an explicit /dev/v4l/by-id or /dev/v4l/by-path video-index0 path"
                    )
                    camera["available"] = False
                    camera["error"] = error
                    cameras.append(camera)
                    errors.append(error)
                    continue
                if "video-index1" in configured.name:
                    error = (
                        f"{role}: {configured} is video-index1 metadata/secondary node; "
                        "configure the video-index0 capture node"
                    )
                    camera["available"] = False
                    camera["error"] = error
                    cameras.append(camera)
                    errors.append(error)
                    continue
                try:
                    metadata = os.stat(configured, follow_symlinks=True)
                    realpath = os.path.realpath(configured)
                except OSError as exc:
                    camera["available"] = False
                    camera["error"] = f"{type(exc).__name__}: {exc}"
                    cameras.append(camera)
                    errors.append(
                        f"{role}: cannot stat configured device {configured}: {exc}"
                    )
                    continue

                if not stat.S_ISCHR(metadata.st_mode):
                    error = f"{role}: configured USB webcam path is not a character device: {configured}"
                    camera["available"] = False
                    camera["error"] = error
                    cameras.append(camera)
                    errors.append(error)
                    continue

                inode_key = (int(metadata.st_dev), int(metadata.st_ino))
                node_key = int(metadata.st_rdev)
                duplicate_role = realpath_owner.get(realpath) or inode_owner.get(
                    inode_key
                )
                if duplicate_role is None:
                    duplicate_role = device_node_owner.get(node_key)
                if duplicate_role is not None:
                    error = (
                        f"{role}: configured device resolves to the same node as "
                        f"{duplicate_role}: {realpath}"
                    )
                    camera["available"] = False
                    camera["error"] = error
                    cameras.append(camera)
                    errors.append(error)
                    continue

                realpath_owner[realpath] = role
                inode_owner[inode_key] = role
                device_node_owner[node_key] = role
                identity = {
                    "configured_device": str(configured),
                    "realpath": realpath,
                    "st_dev": int(metadata.st_dev),
                    "st_ino": int(metadata.st_ino),
                    "st_rdev": int(metadata.st_rdev),
                    "file_type": "character_device",
                }
                identities[role] = identity
                camera.update(identity)
                camera["available"] = True
                cameras.append(camera)

            self.device_identities = identities
            if errors:
                return AdapterResult(
                    False,
                    "USB webcam identity validation failed: " + "; ".join(errors),
                    {
                        "transport": "usb_uvc",
                        "cameras": cameras,
                        "errors": errors,
                    },
                )
            return AdapterResult(
                True,
                f"validated {len(cameras)} configured USB webcam device(s) without opening them",
                {
                    "transport": "usb_uvc",
                    "cameras": cameras,
                },
            )

    def connect_cameras(self) -> AdapterResult:
        if self.closing_event.is_set():
            return AdapterResult(False, "USB webcam adapter is closing")
        with self.lock:
            if self.closing_event.is_set():
                return AdapterResult(False, "USB webcam adapter is closing")
            scanned = self.scan_cameras()
            if not scanned.ok:
                return scanned
            if not self.worker_path.is_file() or not os.access(
                self.worker_path, os.X_OK
            ):
                return AdapterResult(
                    False,
                    f"USB webcam worker is not executable: {self.worker_path}",
                )
            try:
                self.preview_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return AdapterResult(
                    False, f"cannot create USB preview directory: {exc}"
                )

            for role in self.capture_roles:
                if self._worker_alive_locked(role) and self._worker_ready(role):
                    continue
                self._terminate_worker_locked(role, graceful=False)
                launched = self._spawn_worker_locked(role)
                if not launched.ok:
                    # A partially launched preview set has not entered the
                    # command loop contract. Do not wait for an idle QUIT
                    # response from a failed worker.
                    self._disconnect_locked(graceful=False)
                    return launched

            responses: dict[str, dict[str, Any]] = {}
            errors: list[str] = []
            for role in self.capture_roles:
                if self._worker_ready(role):
                    responses[role] = dict(self.worker_states.get(role, {}))
                    continue
                response = self._wait_response(role, "ready", self.ready_timeout_sec)
                if response is None:
                    errors.append(f"{role}: worker did not become ready")
                    continue
                responses[role] = response
                if not response.get("ok"):
                    errors.append(
                        f"{role}: {response.get('message', 'worker rejected USB device')}"
                    )
            if errors:
                # A worker that rejected readiness may already be exiting and
                # no longer reading stdin. Failure cleanup must be bounded and
                # must not use the recording finalization timeout.
                self._disconnect_locked(graceful=False)
                return AdapterResult(
                    False,
                    "USB webcam connection failed: " + "; ".join(errors),
                    {"workers": responses},
                )
            camera_models = {
                str(response.get("camera_model", "")).strip()
                for response in responses.values()
                if str(response.get("camera_model", "")).strip()
            }
            if len(camera_models) != 1:
                self._disconnect_locked(graceful=False)
                return AdapterResult(
                    False,
                    "USB webcam roles must report one identical camera model; "
                    f"got {sorted(camera_models) or ['missing']}",
                    {"workers": responses},
                )
            detected_model = next(iter(camera_models))
            if (
                self.required_camera_model
                and detected_model != self.required_camera_model
            ):
                self._disconnect_locked(graceful=False)
                return AdapterResult(
                    False,
                    "USB webcam model mismatch: required "
                    f"{self.required_camera_model}, got {detected_model}",
                    {"workers": responses},
                )
            return AdapterResult(
                True,
                f"{len(responses)} USB webcam preview worker(s) ready",
                {"workers": responses},
            )

    def disconnect_cameras(self) -> AdapterResult:
        with self.lock:
            return self._disconnect_locked()

    def prepare(self, session_dir: Path) -> AdapterResult:
        del session_dir
        with self.lock:
            if self._all_selected_workers_ready_locked():
                return AdapterResult(
                    True,
                    "USB webcam workers already ready",
                    {"cameras": self.status()["cameras"]},
                )
        return self.connect_cameras()

    def start_recording(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            if self.recording:
                return AdapterResult(True, "USB webcam recording was already started")
            if not self._all_selected_workers_ready_locked():
                return AdapterResult(False, "USB webcam workers are not all ready")
            try:
                self._write_capture_manifest_locked(session_dir)
            except Exception as exc:
                return AdapterResult(
                    False,
                    f"failed to write USB capture manifest: {type(exc).__name__}: {exc}",
                )

            command = "START\t" + json.dumps(
                str(session_dir.resolve()), ensure_ascii=False
            )
            start_results = self._parallel_requests_locked(command, "start")
            succeeded = [
                role
                for role, response in start_results.items()
                if response is not None and bool(response.get("ok"))
            ]
            failed = [role for role in self.capture_roles if role not in succeeded]
            if failed:
                rollback = self._parallel_requests_locked(
                    "STOP",
                    "stop",
                    roles=succeeded,
                    timeout=self.stop_timeout_sec,
                )
                self.recording = False
                self.active_session_dir = None
                return AdapterResult(
                    False,
                    "USB webcam recording start failed for " + ", ".join(failed),
                    {
                        "start": start_results,
                        "rollback": rollback,
                    },
                )
            self.recording = True
            self.active_session_dir = session_dir.expanduser().resolve()
            return AdapterResult(
                True,
                f"USB webcam panorama recording started for {len(succeeded)} camera(s)",
                {"workers": start_results},
            )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            session_dir = session_dir.expanduser().resolve()
            required_roles = list(self.capture_roles)
            roles = [
                role for role in self.capture_roles if self._worker_alive_locked(role)
            ]
            stop_results = (
                self._parallel_requests_locked(
                    "STOP",
                    "stop",
                    roles=roles,
                    timeout=self.stop_timeout_sec,
                )
                if roles
                else {}
            )
            failed = [role for role in required_roles if role not in roles]
            failed.extend(
                role
                for role in roles
                if stop_results.get(role) is None or not stop_results[role].get("ok")
            )
            if failed:
                return AdapterResult(
                    False,
                    "USB webcam recording stop failed for " + ", ".join(failed),
                    {
                        "workers": stop_results,
                        "completion_marker": None,
                        "completion_status": "incomplete",
                    },
                )
            self.recording = False
            try:
                complete_path = self._write_capture_complete_locked(
                    session_dir,
                    stop_results,
                )
            except Exception as exc:
                self.active_session_dir = None
                return AdapterResult(
                    False,
                    "USB webcam workers stopped, but the capture completion marker "
                    f"could not be committed: {type(exc).__name__}: {exc}. "
                    "Treat the USB capture as incomplete and rely on the overall "
                    "session manifest for recovery/audit.",
                    {
                        "workers": stop_results,
                        "completion_marker": None,
                        "completion_status": "incomplete",
                    },
                )
            self.active_session_dir = None
            return AdapterResult(
                True,
                f"USB webcam recording stopped for {len(roles)} camera(s); preview remains active",
                {
                    "workers": stop_results,
                    "completion_marker": str(complete_path),
                    "completion_status": "complete",
                },
            )

    def status(self) -> dict[str, Any]:
        # Starting two UVC streams can take the full ready timeout.  Serving a
        # cached snapshot keeps /api/status and the managed-window health probe
        # alive instead of blocking behind that hardware operation.
        if not self.lock.acquire(timeout=0.1):
            profile = self._source_profile()
            with self.status_cache_lock:
                cached = json.loads(json.dumps(self.status_cache)) if self.status_cache else None
            if cached is not None:
                cached["status_busy"] = True
                return cached
            return {
                "type": "usb_webcam",
                "transport": "usb_uvc",
                "protocol": self.protocol_name,
                "expected_cameras": self.expected_cameras,
                "connected_cameras": 0,
                "ready_cameras": 0,
                "capture_roles": list(self.capture_roles),
                "connected": False,
                "ready": False,
                "preview_running": False,
                "preview_stale_after_ms": self.preview_stale_after_ms,
                "stale": False,
                "recording": False,
                "recording_requested": self.recording,
                "view_mode": self.view_mode,
                "required_camera_model": self.required_camera_model or None,
                "source_profile": profile,
                "source_contract": {
                    **profile,
                    "camera_model": "unknown",
                    "stabilization": "not_verified",
                    "flowstate_supported_in_webcam": None,
                    "flowstate_verified": False,
                    "flowstate_status": "not_verified",
                },
                "record_native_profile": True,
                "record_panorama_master": self.view_mode == "panorama",
                "source": profile["source"],
                "status_busy": True,
                "cameras": [
                    {
                        "role": role,
                        "device": str(self.devices[role]),
                        "configured_device": str(self.devices[role]),
                        "connected": False,
                        "ready": False,
                        "recording": False,
                        "preview_running": False,
                        "preview_available": False,
                        "stale": False,
                        "width": profile["width"],
                        "height": profile["height"],
                        "fps": profile["fps"],
                    }
                    for role in self.capture_roles
                ],
            }
        try:
            profile = self._source_profile()
            cameras: list[dict[str, Any]] = []
            connected_count = 0
            ready_count = 0
            for role in self.capture_roles:
                alive = self._worker_alive_locked(role)
                ready = alive and self._worker_ready(role)
                if alive:
                    connected_count += 1
                if ready:
                    ready_count += 1
                with self.state_lock:
                    worker_state = dict(self.worker_states.get(role, {}))
                identity = dict(self.device_identities.get(role, {}))
                preview_info = self._preview_info_locked(role)
                preview_path = (
                    preview_info["path"] if preview_info is not None else None
                )
                sequence = worker_state.get(
                    "sequence",
                    worker_state.get("latest_sequence"),
                )
                source_frame_age_ms = worker_state.get("latest_source_age_ms")
                if source_frame_age_ms is None:
                    source_ns = worker_state.get(
                        "soe_monotonic_ns",
                        worker_state.get("latest_soe_monotonic_ns"),
                    )
                    try:
                        source_frame_age_ms = max(
                            0.0,
                            (time.monotonic_ns() - int(source_ns)) / 1_000_000.0,
                        )
                    except (TypeError, ValueError):
                        source_frame_age_ms = None
                try:
                    source_frame_age_ms = (
                        max(0.0, float(source_frame_age_ms))
                        if source_frame_age_ms is not None
                        else None
                    )
                except (TypeError, ValueError):
                    source_frame_age_ms = None
                preview_available = preview_path is not None
                preview_age_ms = (
                    float(preview_info["age_ms"])
                    if preview_info is not None
                    else None
                )
                frame_age_ms = (
                    preview_age_ms
                    if preview_age_ms is not None
                    else source_frame_age_ms
                )
                stale = alive and (
                    not ready
                    or not preview_available
                    or (
                        preview_age_ms is not None
                        and preview_age_ms > self.preview_stale_after_ms
                    )
                )
                cameras.append(
                    {
                        "role": role,
                        "device": str(self.devices[role]),
                        "configured_device": str(self.devices[role]),
                        **identity,
                        "connected": alive,
                        "ready": ready,
                        "recording": bool(self.recording and alive),
                        "preview_running": ready,
                        "preview_available": preview_available,
                        "preview_path": str(preview_path)
                        if preview_path is not None
                        else None,
                        "preview_version": preview_info["version"]
                        if preview_info is not None
                        else None,
                        "preview_mtime_unix_ns": preview_info["mtime_ns"]
                        if preview_info is not None
                        else None,
                        "preview_age_ms": preview_age_ms,
                        "sequence": sequence,
                        "frame_seq": sequence,
                        "frame_age_ms": frame_age_ms,
                        "source_frame_age_ms": source_frame_age_ms,
                        "stale": stale,
                        "worker_generation": worker_state.get("generation"),
                        "width": worker_state.get("width", profile["width"]),
                        "height": worker_state.get("height", profile["height"]),
                        "fps": worker_state.get("fps", profile["fps"]),
                        "worker_state": worker_state,
                    }
                )
            all_ready = (
                ready_count == self.expected_cameras and self.expected_cameras > 0
            )
            any_stale = any(bool(camera["stale"]) for camera in cameras)
            status = {
                "type": "usb_webcam",
                "transport": "usb_uvc",
                "protocol": self.protocol_name,
                "expected_cameras": self.expected_cameras,
                "connected_cameras": connected_count,
                "ready_cameras": ready_count,
                "capture_roles": list(self.capture_roles),
                "connected": connected_count == self.expected_cameras
                and self.expected_cameras > 0,
                "ready": all_ready,
                "preview_running": all_ready,
                "preview_stale_after_ms": self.preview_stale_after_ms,
                "stale": any_stale,
                "recording": bool(self.recording and all_ready),
                "recording_requested": self.recording,
                "view_mode": self.view_mode,
                "required_camera_model": self.required_camera_model or None,
                "source_profile": profile,
                "source_contract": self._source_contract(),
                "record_native_profile": True,
                "record_panorama_master": self.view_mode == "panorama",
                "source": profile["source"],
                "cameras": cameras,
            }
            with self.status_cache_lock:
                self.status_cache = json.loads(json.dumps(status))
            return status
        finally:
            self.lock.release()

    def latest_preview_path(self, role: str) -> Path | None:
        try:
            normalized_role = self._normalize_role(role)
        except ValueError:
            return None
        with self.lock:
            info = self._preview_info_locked(normalized_role)
            return info["path"] if info is not None else None

    def latest_preview_bytes(
        self,
        role: str,
        *,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> tuple[bytes, dict[str, Any]] | None:
        """Read one preview only while its owning worker generation is live."""

        try:
            normalized_role = self._normalize_role(role)
        except ValueError:
            return None
        with self.lock:
            info = self._preview_info_locked(normalized_role)
            if info is None:
                return None
            path = info["path"]
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError:
                return None
            try:
                try:
                    metadata = os.fstat(fd)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_mtime_ns != info["mtime_ns"]
                        or metadata.st_size <= 0
                        or metadata.st_size > max_bytes
                    ):
                        return None
                    payload = bytearray()
                    while len(payload) < metadata.st_size:
                        chunk = os.read(
                            fd,
                            min(1024 * 1024, metadata.st_size - len(payload)),
                        )
                        if not chunk:
                            break
                        payload.extend(chunk)
                    if len(payload) != metadata.st_size:
                        return None
                finally:
                    os.close(fd)
            except OSError:
                # A preview publication can race a worker exit or filesystem
                # error. Treat it exactly like an unavailable frame; never let
                # an HTTP preview request escape as a server exception.
                return None

            # The process can exit while the file is being read. Revalidate the
            # same generation after the read so HTTP never serves an orphaned
            # image from a dead or superseded worker.
            current = self._preview_info_locked(normalized_role)
            if (
                current is None
                or current["generation"] != info["generation"]
                or current["mtime_ns"] != info["mtime_ns"]
            ):
                return None
            return bytes(payload), {
                key: value for key, value in current.items() if key != "path"
            }

    def close(self) -> None:
        # Signal before taking ``lock`` so a pending first-frame wait can abort
        # promptly.  The adapter is never reused after close().
        self.closing_event.set()
        with self.lock:
            self._disconnect_locked()

    def _preview_path(self, role: str) -> Path:
        return self.preview_dir / f"{role}.jpg"

    def _remove_preview_file_locked(self, role: str) -> None:
        """Remove only the adapter-owned path itself, never a symlink target."""

        preview_path = self._preview_path(role)
        try:
            metadata = os.lstat(preview_path)
        except FileNotFoundError:
            return
        except OSError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            return
        try:
            preview_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _preview_info_locked(
        self,
        role: str,
        *,
        require_fresh: bool = True,
    ) -> dict[str, Any] | None:
        if role not in self.devices or not self._worker_alive_locked(role):
            return None
        with self.state_lock:
            generation = self.worker_generations.get(role)
            state = dict(self.worker_states.get(role, {}))
        if (
            generation is None
            or state.get("generation") != generation
            or state.get("protocol") != self.protocol_name
            or not bool(state.get("ready"))
        ):
            return None
        preview_path = self._preview_path(role)
        if preview_path.parent != self.preview_dir:
            return None
        try:
            metadata = os.lstat(preview_path)
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        started_ns = int(state.get("started_unix_ns") or 0)
        if started_ns and metadata.st_mtime_ns < started_ns:
            return None
        age_ms = max(0.0, (time.time_ns() - metadata.st_mtime_ns) / 1_000_000.0)
        if require_fresh and age_ms > self.preview_stale_after_ms:
            return None
        return {
            "path": preview_path,
            "generation": generation,
            "mtime_ns": int(metadata.st_mtime_ns),
            "size": int(metadata.st_size),
            "age_ms": age_ms,
            "version": f"{generation}-{metadata.st_mtime_ns}-{metadata.st_size}",
        }

    def _worker_alive_locked(self, role: str) -> bool:
        process = self.processes.get(role)
        return process is not None and process.poll() is None

    def _worker_ready(self, role: str) -> bool:
        with self.state_lock:
            state = self.worker_states.get(role, {})
            generation = self.worker_generations.get(role)
            return bool(
                generation is not None
                and state.get("generation") == generation
                and state.get("protocol") == self.protocol_name
                and state.get("ready", False)
            )

    def _all_selected_workers_ready_locked(self) -> bool:
        return all(
            self._worker_alive_locked(role) and self._worker_ready(role)
            for role in self.capture_roles
        )

    def _spawn_worker_locked(self, role: str) -> AdapterResult:
        if self.closing_event.is_set():
            return AdapterResult(False, "USB webcam adapter is closing")
        preview_path = self._preview_path(role)
        self._remove_preview_file_locked(role)
        command = [
            str(self.worker_path),
            "--device",
            str(self.devices[role]),
            "--role",
            role,
            "--preview-path",
            str(preview_path),
            "--view-mode",
            self.view_mode,
        ]
        started_unix_ns = time.time_ns()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            return AdapterResult(
                False,
                f"failed to launch USB webcam worker for {role}: {exc}",
                {"cmd": command},
            )
        with self.state_lock:
            generation = self.worker_generations.get(role, 0) + 1
            self.worker_generations[role] = generation
        output_queue: queue.Queue[str] = queue.Queue()
        output_tail: list[str] = []
        self.processes[role] = process
        self.output_queues[role] = output_queue
        self.output_tails[role] = output_tail
        with self.state_lock:
            self.worker_states[role] = {
                "role": role,
                "generation": generation,
                "started_unix_ns": started_unix_ns,
                "protocol": None,
                "ready": False,
                "recording": False,
            }
        self.worker_started_unix_ns[role] = started_unix_ns
        threading.Thread(
            target=self._relay_worker_output,
            args=(role, process, generation, output_queue, output_tail),
            name=f"InstaUVC-{role}-g{generation}-output",
            daemon=True,
        ).start()
        return AdapterResult(
            True,
            f"USB webcam worker launched for {role}",
            {"cmd": command, "generation": generation},
        )

    def _relay_worker_output(
        self,
        role: str,
        process: subprocess.Popen[str],
        generation: int,
        output_queue: queue.Queue[str],
        output_tail: list[str],
    ) -> None:
        if process.stdout is None:
            return
        protocol_failed = False
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            output_tail.append(line)
            if len(output_tail) > 200:
                del output_tail[:50]
            print(f"[insta-uvc:{role}:g{generation}] {line}", flush=True)
            if not line.startswith(self.protocol_prefix):
                continue
            raw_payload = line[len(self.protocol_prefix) :]
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {}
            action = str(payload.get("action", ""))
            reported_protocol = payload.get("protocol")
            reported_role = payload.get("role")
            payload_role = (
                reported_role.strip().lower()
                if isinstance(reported_role, str)
                else None
            )
            protocol_error = None
            if reported_protocol != self.protocol_name:
                protocol_error = (
                    f"worker protocol mismatch: expected {self.protocol_name!r}, "
                    f"got {reported_protocol!r}"
                )
            elif payload_role is None:
                protocol_error = "worker response is missing an explicit string role"
            elif payload_role != role:
                protocol_error = (
                    f"worker role mismatch: expected {role!r}, got {payload_role!r}"
                )
            else:
                shape_error = self._successful_response_shape_error(action, payload)
                if shape_error is not None:
                    protocol_error = f"invalid {action} response: {shape_error}"
            if protocol_error is not None:
                synthetic_action = action if action in self.response_actions else "ready"
                synthetic = {
                    "protocol": self.protocol_name,
                    "reported_protocol": reported_protocol,
                    "action": synthetic_action,
                    "ok": False,
                    "message": protocol_error,
                    "role": role,
                    "generation": generation,
                    "desynchronized": True,
                    "protocol_error": protocol_error,
                }
                with self.state_lock:
                    if self.worker_generations.get(role) == generation:
                        current = dict(self.worker_states.get(role, {}))
                        current.update(synthetic)
                        current["ready"] = False
                        current["recording"] = False
                        self.worker_states[role] = current
                        if synthetic_action in self.response_actions:
                            output_queue.put(json.dumps(synthetic, sort_keys=True))
                if not protocol_failed:
                    protocol_failed = True
                    self._best_effort_abort_then_signal(process, wait=False)
                continue
            payload["role"] = role
            payload["generation"] = generation
            queued_payload = json.dumps(payload, sort_keys=True)
            with self.state_lock:
                if self.worker_generations.get(role) != generation:
                    continue
                current = dict(self.worker_states.get(role, {}))
                current.update(payload)
                if action == "ready":
                    current["ready"] = bool(payload.get("ok"))
                elif action == "start" and payload.get("ok"):
                    current["recording"] = True
                elif action in {"stop", "quit"} and payload.get("ok"):
                    current["recording"] = False
                self.worker_states[role] = current
                if action in self.response_actions:
                    output_queue.put(queued_payload)
        # Cleanup must be generation-checked while holding the lifecycle lock;
        # otherwise a late EOF from an old process could unlink the new worker's
        # freshly published JPEG.
        with self.lock:
            with self.state_lock:
                is_current = self.worker_generations.get(role) == generation
                if is_current:
                    current = dict(self.worker_states.get(role, {}))
                    current["ready"] = False
                    current["recording"] = False
                    current["worker_exit_code"] = process.poll()
                    self.worker_states[role] = current
            if is_current:
                self._remove_preview_file_locked(role)

    def _request_worker(
        self,
        role: str,
        command: str,
        expected_action: str,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        with self.worker_locks[role]:
            process = self.processes.get(role)
            if process is None or process.poll() is not None or process.stdin is None:
                return None
            with self.state_lock:
                generation = self.worker_generations.get(role)
            output_queue = self.output_queues.get(role)
            if generation is None or output_queue is None:
                return None
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._mark_worker_desynchronized(
                    role,
                    f"failed to send {expected_action} command",
                    process=process,
                    generation=generation,
                )
                return None
            response = self._wait_response(
                role,
                expected_action,
                self.command_timeout_sec if timeout is None else timeout,
                generation=generation,
                output_queue=output_queue,
            )
            if response is None:
                self._mark_worker_desynchronized(
                    role,
                    f"timed out waiting for {expected_action} response",
                    process=process,
                    generation=generation,
                )
            elif response.get("desynchronized"):
                self._mark_worker_desynchronized(
                    role,
                    str(response.get("protocol_error") or "invalid worker response"),
                    process=process,
                    generation=generation,
                )
            return response

    def _successful_response_shape_error(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> str | None:
        """Validate the minimum shape of successful lifecycle responses."""

        if payload.get("ok") is not True:
            return None
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return "message must be a non-empty string"
        if action == "ready":
            profile = self._source_profile()
            if payload.get("ready") is not True:
                return "ready must be true"
            if payload.get("connected") is not True:
                return "connected must be true"
            for key in ("width", "height", "fps"):
                expected = profile[key]
                value = payload.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or float(value) != float(expected)
                ):
                    return f"{key} must be {expected}"
            for key in ("view_mode", "semantic", "projection", "layout"):
                if payload.get(key) != profile[key]:
                    return f"{key} must be {profile[key]!r}"
            if payload.get("pixel_format") != profile["pixel_format"]:
                return "pixel_format must be 'MJPG'"
        elif action == "start":
            if payload.get("recording") is not True:
                return "recording must be true"
            if payload.get("recording_lifecycle") != "recording":
                return "recording_lifecycle must be 'recording'"
        elif action == "stop":
            if payload.get("recording") is not False:
                return "recording must be false"
            if payload.get("recording_lifecycle") != "complete":
                return "recording_lifecycle must be 'complete'"
            manifest_value = payload.get("manifest_path")
            if (
                not isinstance(manifest_value, str)
                or not manifest_value
                or not Path(manifest_value).is_absolute()
            ):
                return "manifest_path must be a non-empty absolute path"
        return None

    def _mark_worker_desynchronized(
        self,
        role: str,
        reason: str,
        *,
        process: subprocess.Popen[str] | None = None,
        generation: int | None = None,
    ) -> None:
        """Kill a worker whose command/response ordering can no longer be trusted."""

        with self.state_lock:
            current_generation = self.worker_generations.get(role)
            if generation is None:
                generation = current_generation
            if generation != current_generation:
                return
            current = dict(self.worker_states.get(role, {}))
            current.update(
                {
                    "ready": False,
                    "recording": False,
                    "desynchronized": True,
                    "protocol_error": reason,
                }
            )
            self.worker_states[role] = current
        if process is None:
            process = self.processes.get(role)
        if process is None or process.poll() is not None:
            return
        self._best_effort_abort_then_signal(process, wait=True)

    @staticmethod
    def _best_effort_abort_then_signal(
        process: subprocess.Popen[str],
        *,
        wait: bool,
    ) -> None:
        """Request fail-closed ABORT, then terminate without reusing the protocol."""

        if process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.write("ABORT\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        if not wait:
            return
        try:
            process.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

    def _wait_response(
        self,
        role: str,
        action: str,
        timeout: float,
        *,
        generation: int | None = None,
        output_queue: queue.Queue[str] | None = None,
    ) -> dict[str, Any] | None:
        if generation is None:
            with self.state_lock:
                generation = self.worker_generations.get(role)
        if output_queue is None:
            output_queue = self.output_queues.get(role)
        if output_queue is None:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.closing_event.is_set():
                return None
            try:
                raw = output_queue.get(
                    timeout=min(0.1, max(0.01, deadline - time.monotonic()))
                )
                payload = json.loads(raw)
            except queue.Empty:
                process = self.processes.get(role)
                if process is None or process.poll() is not None:
                    return None
                continue
            except json.JSONDecodeError:
                continue
            if payload.get("protocol") != self.protocol_name:
                continue
            reported_role = payload.get("role")
            if (
                not isinstance(reported_role, str)
                or reported_role.strip().lower() != role
            ):
                continue
            if payload.get("generation") != generation:
                continue
            if payload.get("action") == action:
                payload["role"] = role
                shape_error = self._successful_response_shape_error(action, payload)
                if shape_error is not None:
                    return {
                        **payload,
                        "ok": False,
                        "ready": False,
                        "recording": False,
                        "desynchronized": True,
                        "protocol_error": f"invalid {action} response: {shape_error}",
                        "message": f"invalid {action} response: {shape_error}",
                    }
                return payload
        return None

    def _parallel_requests_locked(
        self,
        command: str,
        expected_action: str,
        *,
        roles: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        selected = list(self.capture_roles if roles is None else roles)
        if not selected:
            return {}
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = {
                role: pool.submit(
                    self._request_worker,
                    role,
                    command,
                    expected_action,
                    timeout,
                )
                for role in selected
            }
            return {role: future.result() for role, future in futures.items()}

    def _disconnect_locked(self, *, graceful: bool = True) -> AdapterResult:
        active_roles = [
            role for role in self.devices if self._worker_alive_locked(role)
        ]
        errors: list[str] = []
        stop_results: dict[str, dict[str, Any] | None] = {}
        completion_marker: str | None = None
        if self.recording:
            stop_results = (
                self._parallel_requests_locked(
                    "STOP",
                    "stop",
                    roles=active_roles,
                    timeout=self.stop_timeout_sec,
                )
                if active_roles
                else {}
            )
            errors.extend(
                f"{role}: STOP failed"
                for role, response in stop_results.items()
                if response is None or not response.get("ok")
            )
            missing_roles = [
                role for role in self.capture_roles if role not in active_roles
            ]
            errors.extend(f"{role}: worker is not running" for role in missing_roles)
            if not errors and self.active_session_dir is not None:
                try:
                    completion_marker = str(
                        self._write_capture_complete_locked(
                            self.active_session_dir,
                            stop_results,
                        )
                    )
                except Exception as exc:
                    errors.append(
                        "USB completion marker failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
        for role in list(self.processes):
            terminated = self._terminate_worker_locked(
                role,
                graceful=graceful and not self.closing_event.is_set(),
            )
            if not terminated:
                errors.append(f"{role}: worker required forced termination")
        self.recording = False
        self.active_session_dir = None
        return AdapterResult(
            not errors,
            (
                "USB webcam workers disconnected"
                if not errors
                else "USB webcam workers disconnected with cleanup warnings: "
                + "; ".join(errors)
            ),
            {
                "stop": stop_results,
                "errors": errors,
                "completion_marker": completion_marker,
            },
        )

    def _terminate_worker_locked(self, role: str, *, graceful: bool) -> bool:
        process = self.processes.pop(role, None)
        if process is None:
            return True
        clean = True
        if process.poll() is None and graceful:
            # STOP may legitimately need the long recording-finalization
            # timeout. Idle QUIT must not: a failed preview worker can remain
            # alive while no longer servicing CommandLoop.
            idle_quit_timeout = min(self.command_timeout_sec, 2.0)
            # The process remains in ``processes`` only while requests are made.
            self.processes[role] = process
            response = self._request_worker(
                role,
                "QUIT",
                "quit",
                timeout=idle_quit_timeout,
            )
            self.processes.pop(role, None)
            clean = response is not None and bool(response.get("ok"))
            try:
                process.wait(timeout=min(idle_quit_timeout, 2.0))
            except subprocess.TimeoutExpired:
                clean = False
        if process.poll() is None:
            clean = False
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
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
        with self.state_lock:
            current = dict(self.worker_states.get(role, {}))
            current["ready"] = False
            current["recording"] = False
            self.worker_states[role] = current
        self._remove_preview_file_locked(role)
        return clean

    def _write_capture_manifest_locked(self, session_dir: Path) -> None:
        session_dir = session_dir.expanduser().resolve()
        if not session_dir.is_dir():
            raise FileNotFoundError(
                f"USB capture session directory does not exist: {session_dir}"
            )
        identities = {
            role: {
                "configured_device": str(self.devices[role]),
                "realpath": self.device_identities.get(role, {}).get(
                    "realpath",
                    os.path.realpath(self.devices[role]),
                ),
            }
            for role in self.capture_roles
        }
        profile = self._source_profile()
        source_contract = self._source_contract()
        source_contract.update(
            {
                "frame_clock": "V4L2 CLOCK_MONOTONIC start-of-exposure",
                "frame_clock_raw_mapping": (
                    "per-frame midpoint-bracket CLOCK_MONOTONIC to "
                    "CLOCK_MONOTONIC_RAW estimate"
                ),
                "gyro": "unavailable",
            }
        )
        manifest = {
            "schema": "instaumi_usb_webcam_capture_v1",
            "status": "configured",
            "complete": False,
            "capture_transport": "usb_uvc",
            "worker_protocol": self.protocol_name,
            "completion_marker": "uvc_capture_complete.json",
            "source": profile["source"],
            "source_contract": source_contract,
            "record_native_profile": True,
            "record_panorama_master": self.view_mode == "panorama",
            "requested_view_mode": self.view_mode,
            "roles": list(self.capture_roles),
            "devices": identities,
            "stabilization": source_contract["stabilization"],
            "sensor_to_driver_latency": "unknown",
            "created_unix_time_ns": time.time_ns(),
        }
        destination = session_dir / "uvc_capture_manifest.json"
        self._atomic_write_json_exclusive(destination, manifest)

    def _write_capture_complete_locked(
        self,
        session_dir: Path,
        stop_results: dict[str, dict[str, Any] | None],
    ) -> Path:
        session_dir = session_dir.expanduser().resolve()
        if self.active_session_dir is None or session_dir != self.active_session_dir:
            raise RuntimeError(
                "STOP session does not match the active USB capture session: "
                f"active={self.active_session_dir} requested={session_dir}"
            )

        workers: dict[str, dict[str, Any]] = {}
        for role in self.capture_roles:
            response = stop_results.get(role)
            if response is None or not response.get("ok"):
                raise RuntimeError(f"{role} has no successful STOP response")
            if response.get("protocol") != self.protocol_name:
                raise RuntimeError(f"{role} STOP response has the wrong worker protocol")
            if response.get("recording_lifecycle") != "complete":
                raise RuntimeError(
                    f"{role} STOP lifecycle is not complete: "
                    f"{response.get('recording_lifecycle')!r}"
                )
            manifest_value = response.get("manifest_path")
            if not isinstance(manifest_value, str) or not manifest_value:
                raise RuntimeError(f"{role} STOP response has no final manifest_path")

            expected_manifest = session_dir / "uvc" / role / "manifest.json"
            reported_manifest = Path(manifest_value).expanduser()
            try:
                actual_manifest = reported_manifest.resolve(strict=True)
                expected_resolved = expected_manifest.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(f"{role} final worker manifest is missing: {exc}") from exc
            if actual_manifest != expected_resolved:
                raise RuntimeError(
                    f"{role} final manifest escaped its assigned session path: "
                    f"reported={actual_manifest} expected={expected_resolved}"
                )
            metadata = os.lstat(expected_manifest)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"{role} final worker manifest is not a regular file")
            try:
                worker_manifest = json.loads(
                    expected_manifest.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"{role} final worker manifest cannot be validated: {exc}"
                ) from exc
            if worker_manifest.get("schema") != "instadatacap.usb_webcam.capture.v1":
                raise RuntimeError(f"{role} final worker manifest schema is invalid")
            if worker_manifest.get("role") != role:
                raise RuntimeError(f"{role} final worker manifest role does not match")
            profile = self._source_profile()
            worker_source = worker_manifest.get("source")
            if not isinstance(worker_source, dict):
                worker_source = worker_manifest.get("source_contract")
            if not isinstance(worker_source, dict):
                raise RuntimeError(
                    f"{role} final worker manifest has no source profile"
                )
            worker_values = {
                "view_mode": worker_source.get("view_mode"),
                "width": worker_source.get(
                    "width", worker_source.get("negotiated_width")
                ),
                "height": worker_source.get(
                    "height", worker_source.get("negotiated_height")
                ),
                "fps": worker_source.get("fps", worker_source.get("negotiated_fps")),
                "pixel_format": worker_source.get(
                    "pixel_format", worker_source.get("negotiated_fourcc")
                ),
                "semantic": worker_source.get("semantic"),
                "projection": worker_source.get("projection"),
                "layout": worker_source.get("layout"),
            }
            for key, value in worker_values.items():
                expected = profile[key]
                if key == "fps":
                    try:
                        matches = abs(float(value) - float(expected)) < 1e-6
                    except (TypeError, ValueError):
                        matches = False
                else:
                    matches = value == expected
                if not matches:
                    raise RuntimeError(
                        f"{role} final worker manifest source {key} does not match "
                        f"the selected {self.view_mode} profile"
                    )

            recording_summary = worker_manifest.get("recording")
            if not isinstance(recording_summary, dict):
                raise RuntimeError(f"{role} final worker manifest has no recording summary")
            frame_count = recording_summary.get("frame_count")
            payload_bytes = recording_summary.get("payload_bytes")
            if (
                isinstance(frame_count, bool)
                or not isinstance(frame_count, int)
                or isinstance(payload_bytes, bool)
                or not isinstance(payload_bytes, int)
            ):
                raise RuntimeError(
                    f"{role} final worker manifest has invalid frame/byte counts"
                )
            if frame_count <= 0 or payload_bytes <= 0:
                raise RuntimeError(f"{role} final worker recording is empty")

            files = worker_manifest.get("files")
            if not isinstance(files, dict):
                raise RuntimeError(f"{role} final worker manifest has no files section")
            artifact_paths: dict[str, str] = {}
            for key in ("video", "frames"):
                filename = files.get(key)
                if not isinstance(filename, str) or not filename:
                    raise RuntimeError(f"{role} final manifest has no {key} file")
                artifact = expected_manifest.parent / filename
                artifact_metadata = os.lstat(artifact)
                if not stat.S_ISREG(artifact_metadata.st_mode):
                    raise RuntimeError(f"{role} final {key} artifact is not regular")
                resolved_artifact = artifact.resolve(strict=True)
                if resolved_artifact.parent != expected_resolved.parent:
                    raise RuntimeError(f"{role} final {key} artifact escaped its role directory")
                if key == "video" and artifact_metadata.st_size != payload_bytes:
                    raise RuntimeError(
                        f"{role} final video size {artifact_metadata.st_size} does not "
                        f"match manifest payload_bytes {payload_bytes}"
                    )
                if key == "frames" and artifact_metadata.st_size <= 0:
                    raise RuntimeError(f"{role} final frames artifact is empty")
                artifact_paths[key] = str(resolved_artifact.relative_to(session_dir))

            self._validate_frames_sidecar(
                role=role,
                frames_path=expected_manifest.parent / files["frames"],
                frame_count=frame_count,
                payload_bytes=payload_bytes,
                recording_summary=recording_summary,
            )

            workers[role] = {
                "generation": response.get("generation"),
                "manifest": str(actual_manifest.relative_to(session_dir)),
                "video": artifact_paths["video"],
                "frames": artifact_paths["frames"],
                "frame_count": frame_count,
                "payload_bytes": payload_bytes,
                "first_sequence": recording_summary.get("first_sequence"),
                "last_sequence": recording_summary.get("last_sequence"),
                "first_soe_monotonic_ns": recording_summary.get(
                    "first_soe_monotonic_ns"
                ),
                "last_soe_monotonic_ns": recording_summary.get(
                    "last_soe_monotonic_ns"
                ),
                "first_soe_monotonic_raw_ns": recording_summary.get(
                    "first_soe_monotonic_raw_ns"
                ),
                "last_soe_monotonic_raw_ns": recording_summary.get(
                    "last_soe_monotonic_raw_ns"
                ),
                "min_monotonic_to_raw_offset_ns": recording_summary.get(
                    "min_monotonic_to_raw_offset_ns"
                ),
                "max_monotonic_to_raw_offset_ns": recording_summary.get(
                    "max_monotonic_to_raw_offset_ns"
                ),
                "max_monotonic_to_raw_error_bound_ns": recording_summary.get(
                    "max_monotonic_to_raw_error_bound_ns"
                ),
            }

        profile = self._source_profile()
        source_contract = self._source_contract()
        source_contract.update(
            {
                "frame_boundaries": "frames.jsonl file_offset_bytes/payload_bytes",
                "frame_clock": "V4L2 SOE mapped to CLOCK_MONOTONIC_RAW per frame",
                "sensor_to_driver_latency": "unknown",
                "preview_is_capture_source": False,
            }
        )
        complete = {
            "schema": "instaumi_usb_webcam_capture_complete_v1",
            "status": "complete",
            "complete": True,
            "capture_transport": "usb_uvc",
            "worker_protocol": self.protocol_name,
            "capture_contract": "uvc_capture_manifest.json",
            "source": profile["source"],
            "requested_view_mode": self.view_mode,
            "record_native_profile": True,
            "record_panorama_master": self.view_mode == "panorama",
            "source_contract": source_contract,
            "roles": list(self.capture_roles),
            "workers": workers,
            "completed_unix_time_ns": time.time_ns(),
        }
        destination = session_dir / "uvc_capture_complete.json"
        self._atomic_write_json_exclusive(destination, complete)
        return destination

    @staticmethod
    def _validate_frames_sidecar(
        *,
        role: str,
        frames_path: Path,
        frame_count: int,
        payload_bytes: int,
        recording_summary: dict[str, Any],
    ) -> None:
        """Stream-validate packet boundaries before publishing completion."""

        expected_offset = 0
        previous_sequence: int | None = None
        previous_soe_ns: int | None = None
        previous_soe_raw_ns: int | None = None
        first_sequence: int | None = None
        last_sequence: int | None = None
        first_soe_ns: int | None = None
        last_soe_ns: int | None = None
        first_soe_raw_ns: int | None = None
        last_soe_raw_ns: int | None = None
        rows = 0
        try:
            with frames_path.open("r", encoding="utf-8") as sidecar:
                for line_number, line in enumerate(sidecar, start=1):
                    if line_number > frame_count:
                        raise RuntimeError(
                            f"{role} frames sidecar has more rows than manifest frame_count"
                        )
                    if not line.endswith("\n"):
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} is truncated"
                        )
                    if len(line) > 1024 * 1024:
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} exceeds 1 MiB"
                        )
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} is invalid JSON"
                        ) from exc
                    if not isinstance(row, dict):
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} is not an object"
                        )

                    values: dict[str, int] = {}
                    for key in (
                        "frame_index",
                        "sequence",
                        "payload_bytes",
                        "file_offset_bytes",
                        "soe_monotonic_ns",
                        "dequeue_monotonic_ns",
                        "enqueue_monotonic_ns",
                        "record_monotonic_ns",
                    ):
                        value = row.get(key)
                        if isinstance(value, bool) or not isinstance(value, int):
                            raise RuntimeError(
                                f"{role} frames sidecar row {line_number} has invalid {key}"
                            )
                        values[key] = value
                    if values["frame_index"] != rows:
                        raise RuntimeError(
                            f"{role} frames sidecar frame_index is not contiguous at row "
                            f"{line_number}"
                        )
                    if values["sequence"] < 0 or values["sequence"] > 0xFFFFFFFF:
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} has invalid sequence"
                        )
                    if values["payload_bytes"] <= 0:
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} has empty payload"
                        )
                    if values["file_offset_bytes"] != expected_offset:
                        raise RuntimeError(
                            f"{role} frames sidecar offset mismatch at row {line_number}"
                        )
                    if previous_sequence is not None and values["sequence"] != (
                        previous_sequence + 1
                    ) & 0xFFFFFFFF:
                        raise RuntimeError(
                            f"{role} frames sidecar sequence gap at row {line_number}"
                        )
                    timestamps = (
                        values["soe_monotonic_ns"],
                        values["dequeue_monotonic_ns"],
                        values["enqueue_monotonic_ns"],
                        values["record_monotonic_ns"],
                    )
                    if timestamps[0] <= 0 or tuple(sorted(timestamps)) != timestamps:
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} has invalid "
                            "monotonic timestamp ordering"
                        )
                    if previous_soe_ns is not None and timestamps[0] <= previous_soe_ns:
                        raise RuntimeError(
                            f"{role} frames sidecar SOE is not strictly increasing at "
                            f"row {line_number}"
                        )
                    raw_clock_keys = (
                        "soe_monotonic_raw_ns",
                        "monotonic_to_raw_offset_ns",
                        "monotonic_to_raw_error_bound_ns",
                    )
                    raw_clock_present = [key in row for key in raw_clock_keys]
                    if any(raw_clock_present) and not all(raw_clock_present):
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} has a partial "
                            "MONOTONIC_RAW mapping"
                        )
                    current_soe_raw_ns: int | None = None
                    if all(raw_clock_present):
                        raw_values: dict[str, int] = {}
                        for key in raw_clock_keys:
                            value = row.get(key)
                            if isinstance(value, bool) or not isinstance(value, int):
                                raise RuntimeError(
                                    f"{role} frames sidecar row {line_number} has invalid {key}"
                                )
                            raw_values[key] = value
                        current_soe_raw_ns = raw_values["soe_monotonic_raw_ns"]
                        if current_soe_raw_ns != (
                            values["soe_monotonic_ns"]
                            + raw_values["monotonic_to_raw_offset_ns"]
                        ):
                            raise RuntimeError(
                                f"{role} frames sidecar row {line_number} has an inconsistent "
                                "MONOTONIC_RAW estimate"
                            )
                        if raw_values["monotonic_to_raw_error_bound_ns"] < 0:
                            raise RuntimeError(
                                f"{role} frames sidecar row {line_number} has a negative "
                                "clock mapping error bound"
                            )
                        if (
                            previous_soe_raw_ns is not None
                            and current_soe_raw_ns <= previous_soe_raw_ns
                        ):
                            raise RuntimeError(
                                f"{role} frames sidecar CLOCK_MONOTONIC_RAW SOE is not "
                                f"strictly increasing at row {line_number}"
                            )
                    source_interval_ms = row.get("source_interval_ms")
                    if (
                        isinstance(source_interval_ms, bool)
                        or not isinstance(source_interval_ms, (int, float))
                        or not math.isfinite(source_interval_ms)
                        or source_interval_ms <= 0.0
                    ):
                        raise RuntimeError(
                            f"{role} frames sidecar row {line_number} has invalid "
                            "source_interval_ms"
                        )
                    if first_sequence is None:
                        first_sequence = values["sequence"]
                        first_soe_ns = timestamps[0]
                        first_soe_raw_ns = current_soe_raw_ns
                    last_sequence = values["sequence"]
                    last_soe_ns = timestamps[0]
                    last_soe_raw_ns = current_soe_raw_ns
                    previous_sequence = values["sequence"]
                    previous_soe_ns = timestamps[0]
                    previous_soe_raw_ns = current_soe_raw_ns
                    expected_offset += values["payload_bytes"]
                    rows += 1
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"{role} frames sidecar cannot be validated: {exc}"
            ) from exc

        if rows != frame_count:
            raise RuntimeError(
                f"{role} frames sidecar row count {rows} does not match "
                f"manifest frame_count {frame_count}"
            )
        if expected_offset != payload_bytes:
            raise RuntimeError(
                f"{role} frames sidecar payload total {expected_offset} does not "
                f"match manifest payload_bytes {payload_bytes}"
            )
        for key, actual in (
            ("first_sequence", first_sequence),
            ("last_sequence", last_sequence),
            ("first_soe_monotonic_ns", first_soe_ns),
            ("last_soe_monotonic_ns", last_soe_ns),
        ):
            expected = recording_summary.get(key)
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise RuntimeError(
                    f"{role} final worker manifest has invalid {key}"
                )
            if expected != actual:
                raise RuntimeError(
                    f"{role} frames sidecar {key} {actual} does not match "
                    f"manifest value {expected}"
                )
        if first_soe_raw_ns is not None:
            for key, actual in (
                ("first_soe_monotonic_raw_ns", first_soe_raw_ns),
                ("last_soe_monotonic_raw_ns", last_soe_raw_ns),
            ):
                expected = recording_summary.get(key)
                if isinstance(expected, bool) or not isinstance(expected, int):
                    raise RuntimeError(
                        f"{role} final worker manifest has invalid {key}"
                    )
                if expected != actual:
                    raise RuntimeError(
                        f"{role} frames sidecar {key} {actual} does not match "
                        f"manifest value {expected}"
                    )

    @staticmethod
    def _atomic_write_json_exclusive(
        destination: Path,
        payload: dict[str, Any],
    ) -> None:
        directory = destination.parent
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(
                f"refusing to overwrite existing USB capture marker: {destination}"
            )
        temporary = directory / (
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            # link() is an atomic no-overwrite publication on the same
            # filesystem. Unlike os.replace(), it cannot silently replace a
            # completion/configuration marker created by another process.
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"refusing to overwrite existing USB capture marker: {destination}"
                ) from exc
            temporary.unlink()
            directory_fd = os.open(
                directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class CompositeCaptureAdapter(InstaAdapter):
    """Starts and stops named capture systems concurrently."""

    def __init__(self, adapters: dict[str, InstaAdapter]) -> None:
        self.adapters = adapters
        self._adapters_lock = threading.RLock()

    def replace_adapter(self, name: str, adapter: InstaAdapter) -> InstaAdapter | None:
        """Atomically replace one idle child without replacing this composite."""

        with self._adapters_lock:
            previous = self.adapters.get(name)
            self.adapters[name] = adapter
            return previous

    def prepare(self, session_dir: Path) -> AdapterResult:
        return self._parallel("prepare", session_dir)

    def start_recording(self, session_dir: Path) -> AdapterResult:
        result = self._parallel("start_recording", session_dir)
        if result.ok:
            return result
        details = result.extra or {}
        started = [
            name
            for name, adapter_result in details.items()
            if isinstance(adapter_result, dict) and adapter_result.get("ok")
        ]
        rollback: dict[str, dict[str, Any]] = {}
        if started:
            with self._adapters_lock:
                rollback_adapters = {
                    name: self.adapters[name]
                    for name in started
                    if name in self.adapters
                }
            with ThreadPoolExecutor(max_workers=len(started)) as pool:
                futures = {
                    name: pool.submit(adapter.stop_recording, session_dir)
                    for name, adapter in rollback_adapters.items()
                }
                rollback = {
                    name: future.result().__dict__ for name, future in futures.items()
                }
        return AdapterResult(
            False,
            result.message
            + ("; rolled back: " + ", ".join(started) if started else ""),
            {"start": details, "rollback": rollback},
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        return self._parallel("stop_recording", session_dir)

    def start_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        result = self._parallel_args("start_episode", session_dir, episode_index)
        if result.ok:
            return result
        details = result.extra or {}
        started = [
            name
            for name, adapter_result in details.items()
            if isinstance(adapter_result, dict) and adapter_result.get("ok")
        ]
        if started:
            with self._adapters_lock:
                rollback_adapters = {
                    name: self.adapters[name]
                    for name in started
                    if name in self.adapters
                }
            with ThreadPoolExecutor(max_workers=len(rollback_adapters)) as pool:
                futures = {
                    name: pool.submit(
                        adapter.stop_episode, session_dir, episode_index
                    )
                    for name, adapter in rollback_adapters.items()
                }
                rollback = {
                    name: future.result().__dict__ for name, future in futures.items()
                }
            return AdapterResult(
                False,
                result.message + "; rolled back episode adapters: " + ", ".join(started),
                {"start": details, "rollback": rollback},
            )
        return result

    def stop_episode(self, session_dir: Path, episode_index: int) -> AdapterResult:
        return self._parallel_args("stop_episode", session_dir, episode_index)

    def reset_visualization(self, session_dir: Path) -> AdapterResult:
        return self._parallel("reset_visualization", session_dir)

    def status(self) -> dict[str, Any]:
        with self._adapters_lock:
            adapters = dict(self.adapters)
        return {name: adapter.status() for name, adapter in adapters.items()}

    def close(self) -> None:
        with self._adapters_lock:
            adapters = list(self.adapters.values())
        for adapter in adapters:
            adapter.close()

    def _parallel(self, method: str, session_dir: Path) -> AdapterResult:
        return self._parallel_args(method, session_dir)

    def _parallel_args(self, method: str, *args: Any) -> AdapterResult:
        with self._adapters_lock:
            adapters = dict(self.adapters)
        if not adapters:
            return AdapterResult(True, f"no adapters configured for {method}")
        with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
            futures = {
                name: pool.submit(getattr(adapter, method), *args)
                for name, adapter in adapters.items()
            }
            results: dict[str, AdapterResult] = {}
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = AdapterResult(
                        False,
                        f"{method} exception: {type(exc).__name__}: {exc}",
                    )
        ok = all(result.ok for result in results.values())
        messages = "; ".join(
            f"{name}: {result.message}" for name, result in results.items()
        )
        return AdapterResult(
            ok, messages, {name: result.__dict__ for name, result in results.items()}
        )


class SubprocessInstaAdapter(InstaAdapter):
    """Compatibility adapter for external prepare/start/stop commands."""

    def __init__(
        self,
        prepare_cmd: list[str] | None = None,
        start_cmd: list[str] | None = None,
        stop_cmd: list[str] | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        self.prepare_cmd = prepare_cmd
        self.start_cmd = start_cmd
        self.stop_cmd = stop_cmd
        self.timeout_sec = timeout_sec

    def prepare(self, session_dir: Path) -> AdapterResult:
        return self._run(self.prepare_cmd, session_dir, "prepare")

    def start_recording(self, session_dir: Path) -> AdapterResult:
        return self._run(self.start_cmd, session_dir, "start")

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        return self._run(self.stop_cmd, session_dir, "stop")

    def _run(
        self, cmd: list[str] | None, session_dir: Path, action: str
    ) -> AdapterResult:
        if not cmd:
            return AdapterResult(True, f"no Insta {action} command configured")
        expanded = [part.replace("{session_dir}", str(session_dir)) for part in cmd]
        try:
            proc = subprocess.run(
                expanded,
                cwd=str(session_dir),
                text=True,
                capture_output=True,
                timeout=self.timeout_sec,
                check=False,
            )
        except Exception as exc:
            return AdapterResult(
                False, f"Insta {action} command failed: {exc}", {"cmd": expanded}
            )
        extra = {
            "cmd": expanded,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
        if proc.returncode != 0:
            return AdapterResult(
                False, f"Insta {action} command exited with {proc.returncode}", extra
            )
        return AdapterResult(True, f"Insta {action} command completed", extra)


def load_subprocess_adapter(config_path: Path) -> SubprocessInstaAdapter:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return SubprocessInstaAdapter(
        prepare_cmd=data.get("prepare_cmd"),
        start_cmd=data.get("start_cmd"),
        stop_cmd=data.get("stop_cmd"),
        timeout_sec=float(data.get("timeout_sec", 30.0)),
    )
