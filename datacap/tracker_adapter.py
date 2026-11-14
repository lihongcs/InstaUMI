from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .insta_adapter import AdapterResult, InstaAdapter


class PyVUTTrackerAdapter(InstaAdapter):
    protocol_prefix = "DATACAP_TRACKER_JSON "

    def __init__(
        self,
        worker_script: Path,
        pyvut_root: Path,
        python_executable: str,
        mode: str = "DONGLE_USB",
        wifi_info_path: Path | None = None,
        require_pose: bool = True,
        ready_timeout_sec: float = 10.0,
        command_timeout_sec: float = 15.0,
        simulate: bool = False,
        required: bool = True,
        expected_trackers: int = 1,
        rate_hz: float = 60.0,
        simulated_trackers: int = 2,
        roles: list[str] | None = None,
        role_by_mac: dict[str, str] | None = None,
        role_config_path: Path | None = None,
        independent_slam: bool = True,
        preserve_existing_connection: bool = True,
        release_on_stop: bool = True,
    ) -> None:
        self.worker_script = worker_script.resolve()
        self.pyvut_root = pyvut_root.resolve()
        self.python_executable = python_executable
        self.mode = mode
        self.wifi_info_path = wifi_info_path.resolve() if wifi_info_path else None
        self.require_pose = require_pose
        self.ready_timeout_sec = ready_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.simulate = simulate
        self.required = required
        self.expected_trackers = max(1, int(expected_trackers))
        self.rate_hz = rate_hz
        self.simulated_trackers = simulated_trackers
        self.roles = list(roles or [])
        self.required_roles = list(self.roles[: self.expected_trackers])
        self.role_by_mac = dict(role_by_mac or {})
        self.role_config_path = (
            role_config_path.expanduser().resolve() if role_config_path else None
        )
        self.independent_slam = bool(independent_slam)
        self.preserve_existing_connection = bool(preserve_existing_connection)
        self.release_on_stop = bool(release_on_stop)
        self.process: subprocess.Popen[str] | None = None
        self.response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.output_tail: list[str] = []
        self.latest: dict[str, Any] = {}
        self.output_path: Path | None = None
        self.recording = False
        self.last_error = ""
        self.last_start_pairing_enabled = False
        self.recovery_count = 0
        self.worker_started_monotonic = 0.0
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()

    def prepare(self, session_dir: Path) -> AdapterResult:
        with self.lock:
            with self.state_lock:
                latest_ok = bool(self.latest.get("ok"))
            if self.process is not None and self.process.poll() is None and latest_ok:
                self.output_path = session_dir / "htc_right.csv"
                response = self._request(
                    "SESSION "
                    + json.dumps(
                        {
                            "output": str(self.output_path),
                            "expected_trackers": self.expected_trackers,
                            "roles": self.roles,
                            "required_roles": self.required_roles,
                            "role_by_mac": self.role_by_mac,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    response_action="session",
                )
                if response is None or not response.get("ok"):
                    return self._optional(
                        AdapterResult(
                            False,
                            response.get("message", "tracker session selection failed")
                            if response
                            else "tracker session selection timed out",
                            response,
                        ),
                        "prepare",
                    )
                deadline = time.monotonic() + self.ready_timeout_sec
                while time.monotonic() < deadline:
                    current = self.status()
                    if current["ready_trackers"] >= self.expected_trackers:
                        return AdapterResult(
                            True,
                            f"reused persistent tracker receiver; {self.expected_trackers} tracker(s) ready",
                            current,
                        )
                    time.sleep(0.05)
                current = self.status()
                return self._optional(
                    AdapterResult(
                        False,
                        f"persistent receiver has only {current['ready_trackers']}/{self.expected_trackers} ready tracker(s)",
                        current,
                    ),
                    "prepare",
                )

            self.close()
            self.last_error = ""
            self.output_tail = []
            self.response_queue = queue.Queue()
            executable = self._resolve_python()
            if executable is None:
                return self._optional(
                    AdapterResult(False, f"tracker Python not found: {self.python_executable}"),
                    "prepare",
                )
            if not self.worker_script.is_file():
                return self._optional(
                    AdapterResult(False, f"tracker worker not found: {self.worker_script}"),
                    "prepare",
                )
            if not self.simulate and not (self.pyvut_root / "pyvut" / "__init__.py").is_file():
                return self._optional(
                    AdapterResult(False, f"Compatible pyvut backend not found at {self.pyvut_root}. Set tracker.pyvut_root to your local checkout."),
                    "prepare",
                )

            self.output_path = session_dir / "htc_right.csv"
            command = [
                executable,
                str(self.worker_script),
                "--output",
                str(self.output_path),
                "--pyvut-root",
                str(self.pyvut_root),
                "--mode",
                self.mode,
                "--ready-timeout-sec",
                str(self.ready_timeout_sec),
                "--expected-trackers",
                str(self.expected_trackers),
                "--rate-hz",
                str(self.rate_hz),
                "--simulated-trackers",
                str(self.simulated_trackers),
                "--roles",
                ",".join(self.roles),
                "--required-roles",
                ",".join(self.required_roles),
                "--role-map-json",
                json.dumps(self.role_by_mac, ensure_ascii=False, sort_keys=True),
                "--lock-owner",
                "instadatacap",
            ]
            if self.role_config_path is not None:
                command.extend(["--role-config", str(self.role_config_path)])
            if self.wifi_info_path is not None:
                command.extend(["--wifi-info", str(self.wifi_info_path)])
            if self.require_pose:
                command.append("--require-pose")
            if self.simulate:
                command.append("--simulate")
            if self.independent_slam and self.mode == "DONGLE_USB":
                command.append("--independent-slam")
            if self.preserve_existing_connection and self.mode == "DONGLE_USB":
                command.append("--preserve-existing-connection")
            try:
                tracker_environment = os.environ.copy()
                # Camera SDK installations such as MVS can prepend an older
                # libusb through LD_LIBRARY_PATH. The Ubuntu hidapi backend
                # then loads that incompatible library and cannot initialize
                # a VIVE Wireless Dongle. Keep the camera process environment
                # untouched, but let the tracker worker use system USB libs.
                tracker_environment.pop("LD_LIBRARY_PATH", None)
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
                    env=tracker_environment,
                )
                self.worker_started_monotonic = time.monotonic()
                self.last_start_pairing_enabled = bool(
                    self.mode == "DONGLE_USB"
                    and not self.preserve_existing_connection
                )
            except Exception as exc:
                self.process = None
                return self._optional(
                    AdapterResult(False, f"failed to launch tracker worker: {exc}", {"cmd": command}),
                    "prepare",
                )
            threading.Thread(target=self._relay_output, name="PyVUTWorkerOutput", daemon=True).start()
            response = self._wait_response("ready", self.ready_timeout_sec + 8.0)
            if response is None or not response.get("ok"):
                message = (
                    response.get("message", "tracker worker failed")
                    if response
                    else "tracker worker did not become ready"
                )
                self.last_error = message
                details = response or {"output_tail": self.output_tail[-30:]}
                self.close()
                return self._optional(AdapterResult(False, message, details), "prepare")
            with self.state_lock:
                self.latest = dict(response)
            return AdapterResult(True, response.get("message", "tracker ready"), dict(response))

    def set_expected_trackers(self, count: int) -> None:
        """Set the next session's required tracker count while idle."""

        with self.lock:
            self.expected_trackers = max(1, int(count))
            self.required_roles = list(self.roles[: self.expected_trackers])

    def set_capture_roles(self, roles: list[str]) -> None:
        """Require and record only the selected physical tracker roles."""

        selected = [str(role).strip() for role in roles if str(role).strip()]
        if not selected:
            raise ValueError("at least one HTC tracker role is required")
        with self.lock:
            if self.recording:
                raise RuntimeError("cannot change HTC tracker roles while recording")
            unknown = [role for role in selected if role not in self.roles]
            if unknown:
                raise ValueError(f"unknown HTC tracker role(s): {', '.join(unknown)}")
            self.required_roles = selected
            self.expected_trackers = len(selected)

    def start_monitor(self, runtime_dir: Path) -> AdapterResult:
        """Open pyvut for live readiness status without writing capture rows."""

        runtime_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            original_require_pose = self.require_pose
            self.require_pose = False
            try:
                return self.prepare(runtime_dir)
            finally:
                self.require_pose = original_require_pose

    def recover_monitor(self, runtime_dir: Path, *, enable_pairing: bool) -> AdapterResult:
        """Restart an idle receiver, optionally enabling the RF PairDevice path.

        PairDevice does not invoke the separate clear-pairing or factory-reset
        commands.  This method is deliberately explicit and is never called
        while a capture is active.
        """

        with self.lock:
            if self.recording:
                return AdapterResult(False, "cannot recover HTC while recording")
            self.close()
            original_preserve = self.preserve_existing_connection
            if enable_pairing and self.mode == "DONGLE_USB":
                self.preserve_existing_connection = False
            try:
                result = self.start_monitor(runtime_dir)
            finally:
                self.preserve_existing_connection = original_preserve
            if result.ok:
                self.recovery_count += 1
                mode = "RF reconnect/pairing enabled" if enable_pairing else "passive reconnect"
                result.message = f"HTC receiver restarted ({mode})"
                result.details = self.status()
            return result

    def probe_health(self, timeout_sec: float = 2.0) -> dict[str, Any]:
        """Refresh worker health without changing RF or capture state."""

        response = self._request("STATUS", timeout_sec=timeout_sec)
        if response is None:
            with self.state_lock:
                self.latest.update(
                    {
                        "ok": False,
                        "receiver_healthy": False,
                        "poll_thread_alive": False,
                        "poll_error": "tracker health probe timed out",
                        "message": "tracker health probe timed out",
                    }
                )
        return self.status()

    def start_recording(self, session_dir: Path) -> AdapterResult:
        if self.process is None:
            return self._optional(AdapterResult(False, "tracker is not prepared"), "start")
        response = self._request("START")
        if response is None:
            return self._optional(
                AdapterResult(False, "tracker start command timed out", {"output_tail": self.output_tail[-30:]}),
                "start",
            )
        self.recording = bool(response.get("ok"))
        return self._optional(
            AdapterResult(self.recording, response.get("message", "tracker start response"), response),
            "start",
        )

    def stop_recording(self, session_dir: Path) -> AdapterResult:
        if self.process is None:
            if not self.recording:
                return AdapterResult(True, "tracker recording was already stopped")
            return self._optional(AdapterResult(False, "tracker worker is unavailable"), "stop")
        try:
            response = self._request("STOP")
            if response is None:
                result = AdapterResult(
                    False,
                    "tracker stop command timed out",
                    {"output_tail": self.output_tail[-30:]},
                )
            else:
                self.recording = False
                result = AdapterResult(
                    bool(response.get("ok")),
                    response.get("message", "tracker stop response"),
                    response,
                )
        finally:
            if self.release_on_stop:
                self.close()
        if self.release_on_stop:
            result.message = f"{result.message}; HTC dongle released"
        return self._optional(result, "stop")

    def reset_visualization(self, session_dir: Path) -> AdapterResult:
        del session_dir
        if self.process is None:
            return AdapterResult(True, "tracker visualization is not running")
        response = self._request("RESET_VISUALIZATION", response_action="reset_visualization")
        if response is None:
            return AdapterResult(
                False,
                "tracker visualization reset timed out",
                {"output_tail": self.output_tail[-30:]},
            )
        return AdapterResult(
            bool(response.get("ok")),
            response.get("message", "tracker visualization reset response"),
            response,
        )

    def status(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        with self.state_lock:
            latest = dict(self.latest)
        tracker_states = [
            item
            for item in latest.get("tracker_states", [])
            if not self.required_roles or item.get("role") in self.required_roles
        ]
        now_ns = time.time_ns()

        def current_age_ms(item: dict[str, Any]) -> float:
            last_ns = int(item.get("last_host_unix_time_ns", 0))
            if last_ns <= 0:
                return 999999.0
            return max(0.0, (now_ns - last_ns) / 1_000_000.0)

        tracker_states = [
            {**item, "age_ms": current_age_ms(item)}
            for item in tracker_states
        ]
        recent_trackers = sum(1 for item in tracker_states if item["age_ms"] <= 1500.0)
        ready_trackers = sum(
            1
            for item in tracker_states
            if item["age_ms"] <= 1500.0
            and int(item.get("tracking_status", 0)) == 2
        )
        receiver_healthy = bool(
            alive
            and latest.get("receiver_healthy", latest.get("ok", False))
            and latest.get("ok")
        )
        if not alive:
            tracking_state = "disconnected"
            tracking_label = "Disconnected"
        elif not receiver_healthy:
            tracking_state = "receiver_error"
            tracking_label = "Receiver read loop stopped"
        elif ready_trackers >= self.expected_trackers:
            tracking_state = "ready"
            tracking_label = "Ready"
        elif ready_trackers > 0:
            tracking_state = "partial"
            tracking_label = f"{ready_trackers}/{self.expected_trackers} ready"
        elif tracker_states:
            tracking_state = "tracking_lost"
            tracking_label = "Tracking lost"
        elif self.expected_trackers > 0:
            tracking_state = "waiting"
            tracking_label = f"0/{self.expected_trackers} ready"
        else:
            tracking_state = "connected"
            tracking_label = "Connected"
        return {
            "type": "simulated_tracker" if self.simulate else "pyvut",
            "connected": receiver_healthy,
            "receiver_open": alive and bool(latest.get("receiver_open", True)),
            "receiver_healthy": receiver_healthy,
            "poll_thread_alive": bool(latest.get("poll_thread_alive", receiver_healthy)),
            "poll_error": str(latest.get("poll_error", "")),
            "tracking_state": tracking_state,
            "tracking_label": tracking_label,
            "recording": self.recording,
            "mode": self.mode,
            "required": self.required,
            "expected_trackers": self.expected_trackers,
            "required_roles": list(self.required_roles),
            "sample_count": int(latest.get("sample_count", 0)),
            "active_trackers": len(tracker_states),
            "recent_trackers": recent_trackers,
            "ready_trackers": ready_trackers,
            "tracker_indices": sorted(
                int(item["tracker_index"])
                for item in tracker_states
                if item.get("tracker_index") is not None
            ),
            "tracker_states": tracker_states,
            "rf_pair_states": list(latest.get("rf_pair_states", [])),
            "rf_paired_slots": list(latest.get("rf_paired_slots", [])),
            # Historical API key retained for the live UI; this is the number
            # of paired RF slots, not proof of recent pose packets.
            "rf_connected_trackers": len(latest.get("rf_paired_slots", [])),
            "pairing_enabled_on_start": bool(
                latest.get("pairing_enabled_on_start", self.last_start_pairing_enabled)
            ),
            "recovery_count": self.recovery_count,
            "worker_age_sec": (
                max(0.0, time.monotonic() - self.worker_started_monotonic)
                if alive and self.worker_started_monotonic > 0
                else 0.0
            ),
            "output": str(self.output_path) if self.output_path else None,
            "outputs": dict(latest.get("outputs", {})),
            "message": (
                latest.get("message", self.last_error or "tracker not prepared")
                if alive
                else self.last_error or "tracker worker is not running"
            ),
            "error": self.last_error,
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
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def _resolve_python(self) -> str | None:
        path = Path(self.python_executable).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(self.python_executable)

    def _request(
        self,
        command: str,
        response_action: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any] | None:
        with self.lock:
            if self.process is None or self.process.poll() is not None or self.process.stdin is None:
                return None
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except Exception:
                return None
            action = response_action or command.partition(" ")[0].lower()
            return self._wait_response(
                action,
                self.command_timeout_sec if timeout_sec is None else float(timeout_sec),
            )

    def _relay_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            payload: dict[str, Any] | None = None
            if line.startswith(self.protocol_prefix):
                try:
                    payload = json.loads(line[len(self.protocol_prefix) :])
                except json.JSONDecodeError:
                    payload = None
            if payload is not None:
                with self.state_lock:
                    self.latest.update(payload)
                # Telemetry is a latest-value stream. Queuing and printing every
                # sample causes unbounded memory growth and unnecessary console
                # I/O; command replies still use the response queue normally.
                if payload.get("action") == "telemetry":
                    continue
                self.response_queue.put(payload)
                # Health probes run every two seconds. Keep their replies for
                # _wait_response(), but do not flood journald/output_tail with
                # a full status snapshot each time.
                if payload.get("action") == "status":
                    continue
            self.output_tail.append(line)
            if len(self.output_tail) > 200:
                del self.output_tail[:50]
            print(f"[pyvut] {line}", flush=True)

    def _wait_response(self, action: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload = self.response_queue.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if payload.get("action") == action:
                return payload
        return None

    def _optional(self, result: AdapterResult, action: str) -> AdapterResult:
        if result.ok or self.required:
            return result
        return AdapterResult(
            True,
            f"optional tracker {action} skipped: {result.message}",
            {"available": False, "error": result.__dict__},
        )
