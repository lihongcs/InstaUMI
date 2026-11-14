from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .insta_adapter import AdapterResult, InstaAdapter


def unix_time_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def optional_clock_ns(clock_name: str) -> int | None:
    """Read an optional Linux clock without making capture depend on it."""

    clock_id = getattr(time, clock_name, None)
    if clock_id is None:
        return None
    try:
        return time.clock_gettime_ns(clock_id)
    except (OSError, ValueError):
        return None


def host_clock_snapshot() -> dict[str, int | None]:
    """Bracket a host clock sample so offline code can bound conversion error."""

    realtime_before_ns = time.time_ns()
    monotonic_ns = time.monotonic_ns()
    monotonic_raw_ns = optional_clock_ns("CLOCK_MONOTONIC_RAW")
    boottime_ns = optional_clock_ns("CLOCK_BOOTTIME")
    realtime_after_ns = time.time_ns()
    return {
        "realtime_unix_ns": (realtime_before_ns + realtime_after_ns) // 2,
        "realtime_before_unix_ns": realtime_before_ns,
        "realtime_after_unix_ns": realtime_after_ns,
        "realtime_bracket_ns": max(0, realtime_after_ns - realtime_before_ns),
        "monotonic_ns": monotonic_ns,
        "monotonic_raw_ns": monotonic_raw_ns,
        "boottime_ns": boottime_ns,
    }


def host_clock_provenance() -> dict[str, Any]:
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    clocksource_path = Path("/sys/devices/system/clocksource/clocksource0/current_clocksource")

    def read_optional(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    return {
        "schema": "instaumi_linux_clock_provenance_v1",
        "boot_id": read_optional(boot_id_path),
        "kernel_clocksource": read_optional(clocksource_path),
        "realtime_clock": "CLOCK_REALTIME/time.time_ns",
        "monotonic_clock": "CLOCK_MONOTONIC/time.monotonic_ns",
        "monotonic_raw_available": getattr(time, "CLOCK_MONOTONIC_RAW", None) is not None,
        "boottime_available": getattr(time, "CLOCK_BOOTTIME", None) is not None,
        "sample": host_clock_snapshot(),
    }


@dataclass
class InputTimestamp:
    unix_time_ms: int
    monotonic_time: float
    source: str
    handler_unix_time_ms: int
    latency_ms: float
    unix_time_ns: int = 0
    monotonic_time_ns: int = 0
    handler_unix_time_ns: int = 0
    handler_monotonic_time_ns: int = 0
    monotonic_raw_time_ns: int = 0
    handler_monotonic_raw_time_ns: int = 0

    def __post_init__(self) -> None:
        if self.unix_time_ns <= 0:
            self.unix_time_ns = int(self.unix_time_ms) * 1_000_000
        if self.monotonic_time_ns <= 0:
            self.monotonic_time_ns = int(round(self.monotonic_time * 1_000_000_000))
        if self.handler_unix_time_ns <= 0:
            self.handler_unix_time_ns = int(self.handler_unix_time_ms) * 1_000_000
        if self.handler_monotonic_time_ns <= 0:
            self.handler_monotonic_time_ns = self.monotonic_time_ns + int(round(self.latency_ms * 1_000_000))
        if self.handler_monotonic_raw_time_ns <= 0:
            self.handler_monotonic_raw_time_ns = optional_clock_ns("CLOCK_MONOTONIC_RAW") or 0
        if self.monotonic_raw_time_ns <= 0 and self.handler_monotonic_raw_time_ns > 0:
            self.monotonic_raw_time_ns = max(
                0,
                self.handler_monotonic_raw_time_ns - int(round(self.latency_ms * 1_000_000)),
            )

    @classmethod
    def now(cls, source: str = "console") -> "InputTimestamp":
        now_ns = time.time_ns()
        mono_ns = time.monotonic_ns()
        raw_ns = optional_clock_ns("CLOCK_MONOTONIC_RAW") or 0
        return cls(
            unix_time_ms=now_ns // 1_000_000,
            monotonic_time=mono_ns / 1_000_000_000.0,
            source=source,
            handler_unix_time_ms=now_ns // 1_000_000,
            latency_ms=0.0,
            unix_time_ns=now_ns,
            monotonic_time_ns=mono_ns,
            handler_unix_time_ns=now_ns,
            handler_monotonic_time_ns=mono_ns,
            monotonic_raw_time_ns=raw_ns,
            handler_monotonic_raw_time_ns=raw_ns,
        )

    @classmethod
    def from_pedal(
        cls,
        pedal_monotonic_time: float,
        source: str = "linux_usb_pedal",
    ) -> "InputTimestamp":
        handler_unix_ns = time.time_ns()
        handler_mono_ns = time.monotonic_ns()
        handler_raw_ns = optional_clock_ns("CLOCK_MONOTONIC_RAW") or 0
        handler_mono = handler_mono_ns / 1_000_000_000.0
        if abs(handler_mono - pedal_monotonic_time) > 3600.0:
            pedal_monotonic_time = handler_mono
        pedal_monotonic_ns = int(round(pedal_monotonic_time * 1_000_000_000))
        latency_ns = max(handler_mono_ns - pedal_monotonic_ns, 0)
        event_unix_ns = handler_unix_ns - latency_ns
        return cls(
            unix_time_ms=event_unix_ns // 1_000_000,
            monotonic_time=pedal_monotonic_time,
            source=source,
            handler_unix_time_ms=handler_unix_ns // 1_000_000,
            latency_ms=latency_ns / 1_000_000.0,
            unix_time_ns=event_unix_ns,
            monotonic_time_ns=pedal_monotonic_ns,
            handler_unix_time_ns=handler_unix_ns,
            handler_monotonic_time_ns=handler_mono_ns,
            monotonic_raw_time_ns=(max(0, handler_raw_ns - latency_ns) if handler_raw_ns else 0),
            handler_monotonic_raw_time_ns=handler_raw_ns,
        )


class DataCaptureSession:
    def __init__(
        self,
        output_root: Path,
        adapter: InstaAdapter,
        task_name: str = "Untitled task",
        triple_start_window_sec: float = 1.2,
        action_cooldown_sec: float = 0.8,
        debounce_sec: float = 0.08,
        arm_mode: str = "dual_arm",
        expected_trackers: int = 2,
        expected_cameras: int = 2,
    ) -> None:
        self.output_root = output_root
        self.adapter = adapter
        self.task_name = self._normalize_task_name(task_name)
        self.task_slug = self._task_slug(self.task_name)
        self.triple_start_window_sec = triple_start_window_sec
        self.action_cooldown_sec = action_cooldown_sec
        self.debounce_sec = debounce_sec
        self.arm_mode = arm_mode
        self.expected_trackers = max(1, int(expected_trackers))
        self.expected_cameras = max(1, int(expected_cameras))
        self.camera_roles = ["insta_right"] if arm_mode == "single_arm" else ["insta_right", "insta_left"]
        self.tracker_roles = ["htc_right"] if arm_mode == "single_arm" else ["htc_right", "htc_left"]
        self.lock = threading.RLock()
        self.session_id = ""
        self.session_dir: Path | None = None
        self.log_path: Path | None = None
        self.audit_log_path: Path | None = None
        self.compat_log_path: Path | None = None
        self.running = False
        self.starting = False
        self.stopping = False
        self.segment_running = False
        self.current_segment = 0
        self.completed_segments = 0
        self.slam_count = 0
        self.pedal_mode = "segment" if self._mvs_mode_locked() else "slam"
        self.last_pedal_press_mono = 0.0
        self.last_pedal_action_mono = 0.0
        self.pending_start_first: InputTimestamp | None = None
        self.pending_start_count = 0
        self.started_monotonic_time: float | None = None
        self.last_message = "Waiting to start capture"
        self.recent_events: list[dict[str, Any]] = []
        self.removed_events: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        with self.lock:
            adapter_status = self.adapter.status()
            mvs_mode = self._mvs_mode_from_status(adapter_status)
            now_monotonic = time.monotonic()
            duration_sec = 0.0
            if self.running and self.started_monotonic_time is not None:
                duration_sec = max(0.0, now_monotonic - self.started_monotonic_time)
            effective_duration_sec = self._effective_segment_duration_locked(now_monotonic)
            slam_events = [
                dict(event)
                for event in self.recent_events
                if event.get("type") == "slam_timestamp"
            ]
            pedal_start_count = 0
            pedal_start_remaining_sec = 0.0
            if not self.running and not self.starting and self.pending_start_first is not None:
                elapsed = max(0.0, time.monotonic() - self.pending_start_first.monotonic_time)
                if elapsed <= self.triple_start_window_sec:
                    pedal_start_count = self.pending_start_count
                    pedal_start_remaining_sec = max(0.0, self.triple_start_window_sec - elapsed)
            last_event = self.recent_events[-1] if self.recent_events else None
            can_undo = bool(
                self.running
                and not self.stopping
                and not mvs_mode
                and last_event
                and last_event.get("type") in {"slam_timestamp", "segment_start", "segment_end"}
            )
            return {
                "running": self.running,
                "starting": self.starting,
                "stopping": self.stopping,
                "transitioning": self.starting or self.stopping,
                "segment_running": self.segment_running,
                "current_segment": self.current_segment,
                "completed_segments": self.completed_segments,
                "slam_count": self.slam_count,
                "pedal_mode": self.pedal_mode,
                "slam_calibration_supported": not mvs_mode,
                "episode_directories_sealed": mvs_mode,
                "pedal_start_count": pedal_start_count,
                "pedal_start_remaining_sec": pedal_start_remaining_sec,
                "arm_mode": self.arm_mode,
                "expected_trackers": self.expected_trackers,
                "expected_cameras": self.expected_cameras,
                "camera_roles": list(self.camera_roles),
                "tracker_roles": list(self.tracker_roles),
                "session_id": self.session_id,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "task_name": self.task_name,
                "task_slug": self.task_slug,
                "output_root": str(self.output_root),
                "expected_session_dir": str(self._next_session_dir_locked(create=False)),
                "can_edit_capture_settings": not self.running and not self.starting and not self.stopping,
                "can_start_new_task": bool(
                    self.session_dir is not None
                    and not self.running
                    and not self.starting
                    and not self.stopping
                ),
                "duration_sec": duration_sec,
                "effective_duration_sec": effective_duration_sec,
                "last_message": self.last_message,
                "recent_events": list(self.recent_events[-12:]),
                "slam_events": slam_events,
                "can_delete_slam_timestamp": self.running and not self.stopping and not mvs_mode,
                "can_undo_timestamp": can_undo,
                "can_clear_timestamps": bool(
                    self.running
                    and not self.stopping
                    and not mvs_mode
                    and any(
                        event.get("type") in {"slam_timestamp", "segment_start", "segment_end"}
                        for event in self.recent_events
                    )
                ),
                "last_undoable_event": dict(last_event) if can_undo else None,
                "adapters": adapter_status,
            }

    def set_capture_settings(self, task_name: str, output_root: str) -> str:
        """Validate and update settings that determine the next session directory."""

        normalized_name = self._normalize_task_name(task_name)
        raw_root = str(output_root).strip()
        if not raw_root:
            raise ValueError("Output path is required")
        candidate = Path(raw_root).expanduser().resolve()

        with self.lock:
            if self.running or self.starting or self.stopping:
                return "Task name and output path are locked while capturing"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                if not candidate.is_dir():
                    raise OSError("Target is not a directory")
                probe = candidate / f".datacap_write_test_{uuid.uuid4().hex}"
                try:
                    probe.write_text("ok", encoding="utf-8")
                finally:
                    probe.unlink(missing_ok=True)
            except OSError as exc:
                raise ValueError(f"Output path is not writable: {exc}") from exc

            self.task_name = normalized_name
            self.task_slug = self._task_slug(normalized_name)
            self.output_root = candidate
            self.last_message = f"Capture settings saved: {self._next_session_dir_locked(create=False)}"
            return self.last_message

    def start_new_capture_task(self) -> str:
        """Detach the finished UI session without modifying its saved files or hardware."""

        with self.lock:
            if self.running or self.starting or self.stopping:
                return "End the current capture before starting a new task"
            previous_dir = self.session_dir
            if previous_dir is None:
                self.last_message = "A fresh capture task is already active"
                return self.last_message
            self._reset_for_new_capture_locked()
            self.last_message = (
                f"New capture task started; previous data remains at {previous_dir}"
            )
            return self.last_message

    def set_arm_mode(
        self,
        mode: str,
        expected_trackers: int,
        expected_cameras: int | None = None,
        start_new_task_on_change: bool = False,
    ) -> str:
        if mode not in {"single_arm", "dual_arm"}:
            return "arm mode must be 'single_arm' or 'dual_arm'"
        with self.lock:
            if self.running:
                return "Arm mode is locked while capturing"
            mode_changed = mode != self.arm_mode
            previous_dir = self.session_dir
            if mode_changed and start_new_task_on_change and previous_dir is not None:
                self._reset_for_new_capture_locked()
            self.arm_mode = mode
            self.expected_trackers = max(1, int(expected_trackers))
            self.expected_cameras = max(1, int(expected_cameras or expected_trackers))
            self.camera_roles = ["insta_right"] if mode == "single_arm" else ["insta_right", "insta_left"]
            self.tracker_roles = ["htc_right"] if mode == "single_arm" else ["htc_right", "htc_left"]
            label = "single-arm" if mode == "single_arm" else "dual-arm"
            roles = "Insta Right + HTC Right" if mode == "single_arm" else "Insta Left/Right + HTC Left/Right"
            self.last_message = f"Selected {label} capture: {roles}"
            if mode_changed and previous_dir is not None and start_new_task_on_change:
                self.last_message += f"; new task started, previous data remains at {previous_dir}"
            return self.last_message

    def _reset_for_new_capture_locked(self) -> None:
        """Clear only in-memory session state; caller must hold ``self.lock``."""

        # The previous manifest/logs were finalized by total end. Dropping
        # these references does not modify any file or hardware adapter.
        self.session_id = ""
        self.session_dir = None
        self.log_path = None
        self.audit_log_path = None
        self.compat_log_path = None
        self.segment_running = False
        self.current_segment = 0
        self.completed_segments = 0
        self.slam_count = 0
        self.pedal_mode = "segment" if self._mvs_mode_locked() else "slam"
        self.pending_start_first = None
        self.pending_start_count = 0
        self.last_pedal_press_mono = 0.0
        self.last_pedal_action_mono = 0.0
        self.started_monotonic_time = None
        self.recent_events = []
        self.removed_events = []

    def pedal_live_status(self) -> dict[str, Any]:
        """Return pedal workflow fields without querying hardware adapters."""

        with self.lock:
            start_count = 0
            remaining_sec = 0.0
            if not self.running and self.pending_start_first is not None:
                elapsed = max(0.0, time.monotonic() - self.pending_start_first.monotonic_time)
                if elapsed <= self.triple_start_window_sec:
                    start_count = self.pending_start_count
                    remaining_sec = max(0.0, self.triple_start_window_sec - elapsed)
            return {
                "start_count": start_count,
                "start_required": 3,
                "start_window_remaining_sec": remaining_sec,
                "capture_running": self.running,
                "pedal_mode": self.pedal_mode,
                "segment_running": self.segment_running,
                "slam_count": self.slam_count,
                "current_segment": self.current_segment,
                "completed_segments": self.completed_segments,
            }

    def set_mode(self, mode: str) -> str:
        if mode == "episode":
            mode = "segment"
        if mode not in {"slam", "segment"}:
            return "mode must be 'slam' or 'episode'"
        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if self._mvs_mode_locked() and mode == "slam":
                return "MVS mode has no SLAM marker operation; pedal mode is fixed to episode capture"
            self.pedal_mode = mode
            self._write_manifest_locked()
            self.last_message = f"pedal mode set to {mode}"
        return self.last_message

    def handle_pedal_press(self, input_ts: InputTimestamp) -> str:
        should_start = False
        with self.lock:
            if input_ts.monotonic_time - self.last_pedal_press_mono < self.debounce_sec:
                return "ignored pedal bounce"
            self.last_pedal_press_mono = input_ts.monotonic_time

            if self.starting or self.stopping:
                return "Capture start/stop is in progress; pedal input ignored"
            if not self.running:
                result = self._handle_triple_start_locked(input_ts)
                should_start = result is None
            else:
                self.pending_start_first = None
                self.pending_start_count = 0
                if input_ts.monotonic_time - self.last_pedal_action_mono < self.action_cooldown_sec:
                    return "ignored repeated pedal action"
                self.last_pedal_action_mono = input_ts.monotonic_time
                return self._single_action_locked(input_ts)

        if should_start:
            return self.start_total(input_ts)
        return result

    def handle_pedal_long_press(self, input_ts: InputTimestamp) -> str:
        with self.lock:
            self.pending_start_first = None
            self.pending_start_count = 0
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            should_end = self.running
        if should_end:
            return self.end_total(input_ts)
        return "Press the pedal three times within the start window"

    def start_total(self, input_ts: InputTimestamp) -> str:
        with self.lock:
            if self.running:
                return "total capture already running"
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            self.starting = True
            self.last_message = "Preparing cameras and Trackers"
            try:
                self.output_root.mkdir(parents=True, exist_ok=True)
                self.session_dir = self._next_session_dir_locked(create=True)
            except Exception as exc:
                self.starting = False
                self.last_message = f"Failed to create capture directory: {exc}"
                return self.last_message
            self.session_id = self.session_dir.name
            self.log_path = self.session_dir / "timestamp_log.jsonl"
            self.log_path.touch(exist_ok=False)
            self.audit_log_path = self.session_dir / "timestamp_audit_log.jsonl"
            self.audit_log_path.touch(exist_ok=False)
            self.compat_log_path = self.session_dir / f"timestamp_log_{self.session_id}.jsonl"
            try:
                os.link(self.log_path, self.compat_log_path)
            except OSError:
                try:
                    self.compat_log_path.symlink_to(self.log_path.name)
                except OSError:
                    self.compat_log_path = None
            self.segment_running = False
            self.current_segment = 0
            self.completed_segments = 0
            self.slam_count = 0
            self.pedal_mode = "segment" if self._mvs_mode_locked() else "slam"
            self.recent_events = []
            self.removed_events = []
            session_dir = self.session_dir

        prepare = self._call_adapter("prepare", session_dir)
        start = (
            self._call_adapter("start_recording", session_dir)
            if prepare.ok
            else AdapterResult(False, "capture start skipped because prepare failed")
        )
        with self.lock:
            self.running = prepare.ok and start.ok
            self.starting = False
            self.started_monotonic_time = input_ts.monotonic_time if self.running else None
            self._append_event_locked("total_start", "total_start", input_ts, adapter_prepare=prepare, adapter_start=start)
            self._write_manifest_locked()
            if not start.ok:
                self.last_message = f"failed to start total capture: {start.message}"
            else:
                adapter_status = self.adapter.status()
                insta_status = (
                    adapter_status.get("insta", {})
                    if isinstance(adapter_status, dict)
                    else {}
                )
                if insta_status.get("type") == "usb_webcam":
                    self.last_message = (
                        "total capture started; USB panorama masters and per-frame host-monotonic "
                        "timestamps are recording directly to this PC"
                    )
                elif insta_status.get("type") == "mvs":
                    self.last_message = (
                        "total capture started; MVS preview remains live and each episode will be "
                        "recorded into its own lossless 224x224 RGB directory"
                    )
                else:
                    self.last_message = (
                        "total capture started; rotate each rigid camera/Tracker assembly for 5-10 seconds "
                        "with non-periodic multi-axis motion now, every 10-15 minutes, and before stopping"
                    )
            message = self.last_message
        if not start.ok:
            try:
                self.adapter.close()
            except Exception:
                pass
        return message

    def end_total(self, input_ts: InputTimestamp) -> str:
        with self.lock:
            if not self.running:
                return "total capture is not running"
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            self.stopping = True
            self.last_message = "Stopping camera and Tracker recording"
            active_episode = self.current_segment if self.segment_running else None
            session_dir = self.session_dir or self.output_root

        if active_episode is not None:
            episode_stop = self._call_adapter(
                "stop_episode", session_dir, active_episode
            )
            if not episode_stop.ok:
                with self.lock:
                    self.stopping = False
                    self.last_message = (
                        f"failed to seal episode {active_episode}: {episode_stop.message}"
                    )
                    self._write_manifest_locked()
                    return self.last_message
            with self.lock:
                self._append_event_locked(
                    "segment_end",
                    "segment_end",
                    input_ts,
                    segment_index=self.current_segment,
                    note="auto end before total_end",
                    adapter_episode_stop=episode_stop,
                )
                self.completed_segments += 1
                self.segment_running = False

        stop = self._call_adapter("stop_recording", session_dir)
        hardware_stopped = stop.ok
        if stop.ok and self._mvs_mode_locked():
            try:
                episode_data = self._materialize_mvs_episode_data(session_dir)
            except Exception as exc:
                stop = AdapterResult(
                    False,
                    "MVS hardware stopped, but episode data sealing failed: "
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                stop = AdapterResult(
                    True,
                    stop.message,
                    {**(stop.extra or {}), "episode_data": episode_data},
                )
        with self.lock:
            event_type = "total_end" if stop.ok else "total_end_failed"
            event_title = "total_end" if stop.ok else "total_end_failed"
            self._append_event_locked(event_type, event_title, input_ts, adapter_stop=stop)
            if hardware_stopped:
                self.running = False
                self.started_monotonic_time = None
            self.stopping = False
            self._write_manifest_locked()
            self.last_message = "total capture ended" if stop.ok else f"capture stopped with incomplete output: {stop.message}"
            return self.last_message

    def add_slam(self, input_ts: InputTimestamp) -> str:
        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if not self.running:
                return "start total capture first"
            if self._mvs_mode_locked():
                return "MVS mode does not use SLAM calibration timestamps"
            if self.slam_count >= 2:
                return "already have 2 SLAM timestamps"
            self.slam_count += 1
            self._append_event_locked("slam_timestamp", f"slam_marker_{self.slam_count}", input_ts)
            if self.slam_count >= 2:
                self.pedal_mode = "segment"
                self._write_manifest_locked()
                self.last_message = "added SLAM timestamp; auto switched to episode capture mode"
                return self.last_message
            self._write_manifest_locked()
            self.last_message = "added SLAM timestamp"
            return self.last_message

    def toggle_segment(self, input_ts: InputTimestamp) -> str:
        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if not self.running:
                return "start total capture first"
            if self.segment_running:
                episode_index = self.current_segment
                episode_stop = self._call_adapter(
                    "stop_episode", self.session_dir or self.output_root, episode_index
                )
                if not episode_stop.ok:
                    self.last_message = (
                        f"episode {episode_index} could not be sealed: {episode_stop.message}"
                    )
                    return self.last_message
                self._append_event_locked(
                    "segment_end", "segment_end", input_ts,
                    segment_index=episode_index,
                    adapter_episode_stop=episode_stop,
                )
                self.segment_running = False
                self.completed_segments += 1
                self._write_manifest_locked()
                self.last_message = f"episode {self.current_segment} ended"
                return self.last_message
            episode_index = self.current_segment + 1
            episode_start = self._call_adapter(
                "start_episode", self.session_dir or self.output_root, episode_index
            )
            if not episode_start.ok:
                self.last_message = (
                    f"episode {episode_index} could not start: {episode_start.message}"
                )
                return self.last_message
            self.current_segment = episode_index
            self.segment_running = True
            visualization_reset = self._call_adapter("reset_visualization", self.session_dir)
            self._append_event_locked(
                "segment_start",
                "segment_start",
                input_ts,
                segment_index=self.current_segment,
                adapter_episode_start=episode_start,
                visualization_reset=visualization_reset,
            )
            self._write_manifest_locked()
            reset_note = "" if visualization_reset.ok else f"; visualization reset warning: {visualization_reset.message}"
            self.last_message = f"episode {self.current_segment} started{reset_note}"
            return self.last_message

    def undo_last_timestamp(self, input_ts: InputTimestamp) -> str:
        """Remove the latest reversible workflow timestamp and roll back state."""

        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if not self.running:
                return "Timestamps can only be undone during an active capture"
            if self._mvs_mode_locked():
                return "MVS episode folders are sealed and their timestamps cannot be undone"
            if not self.recent_events:
                return "There is no timestamp to undo"
            event = self.recent_events[-1]
            event_type = str(event.get("type", ""))
            if event_type not in {"slam_timestamp", "segment_start", "segment_end"}:
                return "The latest record cannot be undone"

            if event_type == "slam_timestamp":
                self.slam_count = max(0, self.slam_count - 1)
                self.pedal_mode = "slam"
            elif event_type == "segment_start":
                self.segment_running = False
                self.current_segment = max(0, self.current_segment - 1)
            elif event_type == "segment_end":
                self.segment_running = True
                self.current_segment = int(event.get("segment_index") or self.current_segment)
                self.completed_segments = max(0, self.completed_segments - 1)

            self.recent_events.pop()
            self._rewrite_log_locked(self.recent_events)
            self._append_removal_audit_locked(event, input_ts, "undo_last_timestamp")
            self.removed_events.append(
                {
                    "removed_event": event,
                    "removed_at_unix_time_ms": input_ts.unix_time_ms,
                    "removed_at_unix_time_ns": input_ts.unix_time_ns,
                    "removed_by": input_ts.source,
                }
            )
            self._write_manifest_locked()
            self.last_message = f"Undone: {event.get('title', 'latest timestamp')}"
            return self.last_message

    def delete_slam_timestamp(self, event_id: str, input_ts: InputTimestamp) -> str:
        """Delete one selected SLAM marker while retaining all other workflow events."""

        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if not self.running:
                return "SLAM markers can only be deleted during an active capture"
            if self._mvs_mode_locked():
                return "MVS mode has no SLAM timestamps"
            selected = next(
                (
                    event
                    for event in self.recent_events
                    if event.get("id") == event_id and event.get("type") == "slam_timestamp"
                ),
                None,
            )
            if selected is None:
                return "SLAM marker not found"

            removed_event = dict(selected)
            self.recent_events = [
                event for event in self.recent_events if event.get("id") != event_id
            ]
            remaining_slam = [
                event for event in self.recent_events if event.get("type") == "slam_timestamp"
            ]
            for index, event in enumerate(remaining_slam, start=1):
                event["title"] = f"slam_marker_{index}"
            self.slam_count = len(remaining_slam)
            if self.slam_count < 2:
                self.pedal_mode = "slam"

            self._rewrite_log_locked(self.recent_events)
            reason = "delete_selected_slam_timestamp"
            self._append_removal_audit_locked(removed_event, input_ts, reason)
            self.removed_events.append(
                {
                    "removed_event": removed_event,
                    "removed_at_unix_time_ms": input_ts.unix_time_ms,
                    "removed_at_unix_time_ns": input_ts.unix_time_ns,
                    "removed_by": input_ts.source,
                    "reason": reason,
                }
            )
            self._write_manifest_locked()
            self.last_message = f"Deleted: {removed_event.get('title', 'SLAM marker')}"
            return self.last_message

    def clear_timestamps(self, input_ts: InputTimestamp) -> str:
        """Clear SLAM/episode markers without deleting recorded hardware data."""

        with self.lock:
            if self.starting or self.stopping:
                return "Capture start/stop is in progress; please wait"
            if not self.running:
                return "Timestamps can only be cleared during an active capture"
            if self._mvs_mode_locked():
                return "MVS episode folders are sealed; clear timestamps is disabled"
            removable_types = {"slam_timestamp", "segment_start", "segment_end"}
            removed = [event for event in self.recent_events if event.get("type") in removable_types]
            if not removed:
                return "This session has no SLAM or episode timestamps to clear"

            self.recent_events = [
                event for event in self.recent_events if event.get("type") not in removable_types
            ]
            self._rewrite_log_locked(self.recent_events)
            for event in removed:
                self._append_removal_audit_locked(
                    event,
                    input_ts,
                    "clear_all_workflow_timestamps",
                )
                self.removed_events.append(
                    {
                        "removed_event": event,
                        "removed_at_unix_time_ms": input_ts.unix_time_ms,
                        "removed_at_unix_time_ns": input_ts.unix_time_ns,
                        "removed_by": input_ts.source,
                        "reason": "clear_all_workflow_timestamps",
                    }
                )
            self.slam_count = 0
            self.pedal_mode = "segment" if self._mvs_mode_locked() else "slam"
            self.segment_running = False
            self.current_segment = 0
            self.completed_segments = 0
            self._write_manifest_locked()
            self.last_message = f"Cleared {len(removed)} SLAM/episode timestamps; captured data was not deleted"
            return self.last_message

    def _handle_triple_start_locked(self, input_ts: InputTimestamp) -> str | None:
        if (
            self.pending_start_first is None
            or input_ts.monotonic_time - self.pending_start_first.monotonic_time > self.triple_start_window_sec
        ):
            self.pending_start_first = input_ts
            self.pending_start_count = 1
        else:
            self.pending_start_count += 1

        if self.pending_start_count >= 3:
            self.pending_start_first = None
            self.pending_start_count = 0
            return None
        return f"pedal start {self.pending_start_count}/3"

    def _effective_segment_duration_locked(self, now_monotonic: float) -> float:
        """Sum every valid episode start/end interval, including the active interval."""

        starts: dict[int, float] = {}
        duration = 0.0
        for event in self.recent_events:
            event_type = event.get("type")
            if event_type not in {"segment_start", "segment_end"}:
                continue
            raw_index = event.get("segment_index")
            if raw_index is None:
                continue
            segment_index = int(raw_index)
            event_monotonic = float(event.get("monotonic_time", 0.0))
            if event_type == "segment_start":
                starts[segment_index] = event_monotonic
            else:
                started = starts.pop(segment_index, None)
                if started is not None:
                    duration += max(0.0, event_monotonic - started)
        if self.running:
            for started in starts.values():
                duration += max(0.0, now_monotonic - started)
        return duration

    def _single_action_locked(self, input_ts: InputTimestamp) -> str:
        if self.pedal_mode == "slam":
            return self.add_slam(input_ts)
        return self.toggle_segment(input_ts)

    def _append_event_locked(self, event_type: str, title: str, input_ts: InputTimestamp, **extra: Any) -> None:
        if self.log_path is None:
            return
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "session_id": self.session_id,
            "type": event_type,
            "title": title,
            "unix_time_ms": input_ts.unix_time_ms,
            "unixTimeMs": input_ts.unix_time_ms,
            "unix_time_ns": input_ts.unix_time_ns,
            "unixTimeNs": input_ts.unix_time_ns,
            "iso_time": datetime.fromtimestamp(input_ts.unix_time_ns / 1_000_000_000, timezone.utc).isoformat(),
            "monotonic_time": input_ts.monotonic_time,
            "monotonicTime": input_ts.monotonic_time,
            "monotonic_time_ns": input_ts.monotonic_time_ns,
            "monotonicTimeNs": input_ts.monotonic_time_ns,
            "monotonic_raw_time_ns": input_ts.monotonic_raw_time_ns or None,
            "input_source": input_ts.source,
            "inputSource": input_ts.source,
            "input_handler_unix_time_ms": input_ts.handler_unix_time_ms,
            "inputHandlerUnixTimeMs": input_ts.handler_unix_time_ms,
            "input_handler_unix_time_ns": input_ts.handler_unix_time_ns,
            "inputHandlerUnixTimeNs": input_ts.handler_unix_time_ns,
            "input_handler_monotonic_time_ns": input_ts.handler_monotonic_time_ns,
            "input_handler_monotonic_raw_time_ns": input_ts.handler_monotonic_raw_time_ns or None,
            "input_latency_ms": input_ts.latency_ms,
            "inputLatencyMs": input_ts.latency_ms,
            "segment_index": extra.pop("segment_index", None),
            "note": extra.pop("note", None),
            "host_clock_snapshot": host_clock_snapshot(),
            "extra": self._serialize_extra(extra),
        }
        record["segmentIndex"] = record["segment_index"]
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            file.write("\n")
        self._append_audit_locked({"audit_action": "timestamp_recorded", **record})
        self.recent_events.append(record)

    def _write_manifest_locked(self) -> None:
        if self.session_dir is None:
            return
        status_snapshot = self.status()
        adapters_status = status_snapshot.get("adapters", {})
        insta_status = (
            adapters_status.get("insta", {})
            if isinstance(adapters_status, dict)
            else {}
        )
        if isinstance(insta_status, dict) and insta_status.get("type") == "usb_webcam":
            camera_transport = "usb_webcam"
            native_contract = insta_status.get("source_contract", {})
            if not isinstance(native_contract, dict):
                native_contract = {}
            camera_source_contract = {
                "schema": "instaumi_camera_source_contract_v1",
                "transport": "usb_uvc",
                "source_kind": native_contract.get(
                    "kind", "uvc_native_profile_mjpeg"
                ),
                "recording_location": "host_pc",
                "master_is_preview": False,
                "record_panorama_master": bool(
                    insta_status.get("record_panorama_master", False)
                ),
                "camera_model": native_contract.get("camera_model", "unknown"),
                "view_mode": native_contract.get(
                    "view_mode", insta_status.get("view_mode", "unknown")
                ),
                "width": native_contract.get("width"),
                "height": native_contract.get("height"),
                "fps": native_contract.get("fps", 30),
                "pixel_format": native_contract.get("pixel_format", "MJPG"),
                "projection": native_contract.get("projection", "unknown"),
                "layout": native_contract.get("layout", "unknown"),
                "compression": native_contract.get(
                    "compression", "lossy_camera_mjpeg"
                ),
                "stabilization": native_contract.get(
                    "stabilization", "not_verified"
                ),
                "flowstate_supported_in_webcam": native_contract.get(
                    "flowstate_supported_in_webcam"
                ),
                "flowstate_verified": native_contract.get(
                    "flowstate_verified", False
                ),
                "flowstate_status": native_contract.get(
                    "flowstate_status", "not_verified"
                ),
                "gyro": "unavailable",
                "frame_clock": "V4L2 CLOCK_MONOTONIC start-of-exposure",
                "sensor_to_driver_latency": "unknown",
                "completion_marker": "uvc_capture_complete.json",
            }
            clock_sync_protocol = {
                "schema": "instaumi_usb_uvc_host_clock_protocol_v1",
                "camera_to_tracker_method": (
                    "shared_host_CLOCK_MONOTONIC: V4L2 start-of-exposure timestamps "
                    "and host Tracker timestamps"
                ),
                "operator_sop": (
                    "No camera-gyro correlation motion is required in USB Webcam mode. "
                    "Before using a session, verify both per-camera frame indexes are "
                    "continuous and that no worker reported a stale frame or source gap."
                ),
                "required_windows": [],
                "raw_timestamps_are_immutable": True,
                "sensor_to_driver_latency": "unknown",
                "sub_10ms_requires_post_capture_validation": True,
            }
        elif isinstance(insta_status, dict) and insta_status.get("type") == "mvs":
            camera_transport = "mvs"
            native_contract = insta_status.get("source_contract", {})
            if not isinstance(native_contract, dict):
                native_contract = {}
            camera_source_contract = {
                "schema": "instaumi_camera_source_contract_v1",
                "transport": "mvs_sdk",
                "source_kind": native_contract.get("kind", "hikrobot_mvs_rgb"),
                "recording_location": "host_pc",
                "master_is_preview": False,
                "camera_model": "Hikrobot MV-CB060-10UC-S",
                "width": native_contract.get("width"),
                "height": native_contract.get("height"),
                "fps": native_contract.get("fps", 30),
                "pixel_format": native_contract.get("pixel_format", "RGB8"),
                "projection": native_contract.get("projection", "camera_native"),
                "layout": native_contract.get(
                    "layout", "one_independent_image_per_camera"
                ),
                "compression": native_contract.get(
                    "compression", "matroska_libx264rgb_crf0_rgb24_lossless"
                ),
                "exposure_us": native_contract.get("exposure_us"),
                "frame_clock": native_contract.get(
                    "frame_clock",
                    (
                        "mvs_sdk_host_ms_minus_half_exposure_monotonic_"
                        "with_labeled_host_fallback"
                    ),
                ),
                "gyro": "not_applicable",
                "completion_marker": "mvs_capture_complete.json",
                "episode_layout": (
                    "episodes/episode_NNNNNN/{insta_right,insta_left,"
                    "htc_right.csv,htc_left.csv,episode_data_complete.json}"
                ),
                "image_transform": (
                    "1536x1536 BGR8 -> 224x224 RGB8 INTER_AREA exactly once in capture"
                ),
            }
            clock_sync_protocol = {
                "schema": "instaumi_mvs_host_clock_protocol_v1",
                "camera_to_tracker_method": (
                    "shared host CLOCK_MONOTONIC: per-frame MVS SDK host timestamp "
                    "minus half exposure (or an explicitly labeled GetImageBuffer-return "
                    "fallback) and host Tracker timestamps"
                ),
                "operator_sop": (
                    "No camera-gyro correlation motion is required. Verify both MVS "
                    "frame indexes and capture timestamps are strictly increasing, and "
                    "audit timestamp_source_counts before using sub-frame alignment."
                ),
                "required_windows": [],
                "raw_timestamps_are_immutable": True,
                "sub_10ms_requires_post_capture_validation": True,
            }
        else:
            insta_type = str(insta_status.get("type", "")) if isinstance(insta_status, dict) else ""
            camera_transport = (
                "bluez_direct"
                if insta_type.startswith("bluez_direct")
                else "bluez_remote"
                if insta_type.startswith("bluez_remote")
                else "camera_sdk"
                if insta_type.startswith("camera_sdk")
                else "dry_run"
                if insta_type == "dry_run"
                else "unknown"
            )
            camera_source_contract = {
                "schema": "instaumi_camera_source_contract_v1",
                "transport": camera_transport,
                "source_kind": "camera_native_insv"
                if camera_transport.startswith("bluez")
                else camera_transport,
                "recording_location": "camera_storage"
                if camera_transport.startswith("bluez")
                else "adapter_defined",
                "transfer_to_pc_required": camera_transport.startswith("bluez"),
                "projection": "native_dual_fisheye_with_camera_metadata"
                if camera_transport.startswith("bluez")
                else "adapter_defined",
                "gyro": "native_camera_telemetry_expected"
                if camera_transport.startswith("bluez")
                else "adapter_defined",
                "raw_media_must_not_be_replaced_by_preview": True,
            }
            clock_sync_protocol = {
                "schema": "instaumi_motion_clock_sync_protocol_v1",
                "camera_to_tracker_method": "windowed_camera_gyro_to_htc_angular_velocity_correlation",
                "operator_sop": (
                    "After both cameras report recording, perform 5-10 seconds of non-periodic multi-axis "
                    "motion on each rigid camera/tracker assembly; repeat every 10-15 minutes during long "
                    "captures and before stopping so every automatic X5 chapter has calibration motion."
                ),
                "required_windows": ["session_start", "every_10_to_15_minutes", "session_end"],
                "raw_timestamps_are_immutable": True,
                "sub_10ms_requires_post_capture_validation": True,
            }
        manifest = {
            "session_id": self.session_id,
            "task_name": self.task_name,
            "task_slug": self.task_slug,
            "output_root": str(self.output_root),
            "updated_at": iso_now(),
            "capture_profile": {
                "arm_mode": self.arm_mode,
                "expected_trackers": self.expected_trackers,
                "expected_cameras": self.expected_cameras,
                "camera_roles": list(self.camera_roles),
                "tracker_roles": list(self.tracker_roles),
                "camera_transport": camera_transport,
            },
            "camera_source_contract": camera_source_contract,
            "clock_provenance": host_clock_provenance(),
            "clock_sync_protocol": clock_sync_protocol,
            "status": status_snapshot,
            "triple_start_window_sec": self.triple_start_window_sec,
            "action_cooldown_sec": self.action_cooldown_sec,
            "debounce_sec": self.debounce_sec,
            "timestamp_log": "timestamp_log.jsonl",
            "timestamp_audit_log": "timestamp_audit_log.jsonl",
            "timestamp_log_compat": self.compat_log_path.name if self.compat_log_path else None,
            "removed_timestamp_history": list(self.removed_events),
        }
        manifest_path = self.session_dir / "manifest.json"
        temporary = self.session_dir / ".manifest.json.tmp"
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)

    def _call_adapter(self, method: str, *args: Any) -> AdapterResult:
        try:
            return getattr(self.adapter, method)(*args)
        except Exception as exc:
            return AdapterResult(False, f"{method} exception: {type(exc).__name__}: {exc}")

    def _materialize_mvs_episode_data(self, session_dir: Path) -> dict[str, Any]:
        """Seal per-episode HTC slices after the continuous tracker files close."""

        ranges: dict[int, dict[str, Any]] = {}
        with self.lock:
            for event in self.recent_events:
                kind = str(event.get("type", ""))
                if kind not in {"segment_start", "segment_end"}:
                    continue
                episode_index = int(event.get("segment_index") or 0)
                if episode_index <= 0:
                    raise ValueError("MVS episode event has no positive segment_index")
                ranges.setdefault(episode_index, {})[kind] = dict(event)
        if not ranges:
            return {"episodes": 0, "markers": []}
        if any(set(value) != {"segment_start", "segment_end"} for value in ranges.values()):
            raise ValueError("MVS episode timestamps are incomplete")

        def digest(path: Path) -> str:
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            return value.hexdigest()

        markers: list[str] = []
        for episode_index in sorted(ranges):
            events = ranges[episode_index]
            start = float(events["segment_start"]["monotonic_time"])
            end = float(events["segment_end"]["monotonic_time"])
            if end <= start:
                raise ValueError(f"episode {episode_index} has invalid monotonic bounds")
            episode_root = session_dir / "episodes" / f"episode_{episode_index:06d}"
            if not episode_root.is_dir() or episode_root.is_symlink():
                raise ValueError(f"missing sealed MVS episode directory: {episode_root}")
            files: dict[str, Any] = {}
            for role in ("htc_right", "htc_left"):
                source = session_dir / f"{role}.csv"
                with source.open("r", encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    fieldnames = list(reader.fieldnames or ())
                    rows = list(reader)
                if len(rows) < 3 or "host_monotonic_time_ns" not in fieldnames:
                    raise ValueError(f"invalid continuous HTC source: {source}")
                use_hid = (
                    "hid_read_host_monotonic_ns" in fieldnames
                    and sum(bool(str(row.get("hid_read_host_monotonic_ns", "")).strip()) for row in rows)
                    / len(rows)
                    >= 0.95
                )
                clock = "hid_read_host_monotonic_ns" if use_hid else "host_monotonic_time_ns"
                times = [int(row[clock]) / 1_000_000_000.0 for row in rows]
                inside = [index for index, value in enumerate(times) if start <= value <= end]
                if not inside:
                    raise ValueError(f"episode {episode_index} contains no {role} samples")
                first = inside[0] - 1
                last = inside[-1] + 1
                if first < 0 or last >= len(rows):
                    raise ValueError(
                        f"episode {episode_index} lacks a {role} interpolation bracket"
                    )
                selected = rows[first : last + 1]
                target = episode_root / f"{role}.csv"
                if target.exists():
                    raise FileExistsError(f"refusing to overwrite episode HTC file: {target}")
                temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
                with temporary.open("x", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(selected)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
                files[role] = {
                    "path": target.name,
                    "rows": len(selected),
                    "source_first_row_index": first,
                    "source_last_row_index": last,
                    "clock_column": clock,
                    "sha256": digest(target),
                }
            marker = episode_root / "episode_data_complete.json"
            payload = {
                "schema": "instadatacap.mvs.episode_data_complete.v1",
                "complete": True,
                "episode_index": episode_index,
                "start_monotonic_time": start,
                "end_monotonic_time": end,
                "segment_start_event_id": events["segment_start"].get("id"),
                "segment_end_event_id": events["segment_end"].get("id"),
                "htc": files,
                "mvs_episode_complete": "mvs_episode_complete.json",
                "sealed_unix_ns": time.time_ns(),
            }
            if marker.exists():
                raise FileExistsError(f"refusing to overwrite episode marker: {marker}")
            temporary = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(marker)
            markers.append(str(marker))
        return {"episodes": len(markers), "markers": markers}

    @staticmethod
    def _mvs_mode_from_status(adapter_status: dict[str, Any]) -> bool:
        insta_status = adapter_status.get("insta", adapter_status)
        return isinstance(insta_status, dict) and insta_status.get("type") == "mvs"

    def _mvs_mode_locked(self) -> bool:
        return self._mvs_mode_from_status(self.adapter.status())

    @staticmethod
    def _normalize_task_name(task_name: str) -> str:
        name = str(task_name).strip()
        if not name:
            raise ValueError("Task name is required")
        if len(name) > 80:
            raise ValueError("Task name cannot exceed 80 characters")
        if any(ord(char) < 32 for char in name):
            raise ValueError("Task name cannot contain control characters")
        return name

    @staticmethod
    def _task_slug(task_name: str) -> str:
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", task_name, flags=re.UNICODE)
        slug = re.sub(r"_+", "_", slug).strip("_-")[:64]
        if not slug or slug in {".", ".."}:
            raise ValueError("Task name must contain a letter or number")
        return slug

    def _next_session_dir_locked(self, create: bool) -> Path:
        """Return or atomically create date_task, adding _02 on collision."""

        base_name = f"{datetime.now().strftime('%Y%m%d')}_{self.task_slug}"
        attempt = 1
        while True:
            suffix = "" if attempt == 1 else f"_{attempt:02d}"
            candidate = self.output_root / f"{base_name}{suffix}"
            if create:
                try:
                    candidate.mkdir(parents=True, exist_ok=False)
                    return candidate
                except FileExistsError:
                    attempt += 1
                    continue
            if not candidate.exists():
                return candidate
            attempt += 1

    def _rewrite_log_locked(self, events: list[dict[str, Any]]) -> None:
        if self.log_path is None:
            return
        # Rewrite the existing inode so the compatibility hard link continues
        # to point to exactly the same timestamp content.
        with self.log_path.open("w", encoding="utf-8") as file:
            for record in events:
                file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())

    def _append_audit_locked(self, record: dict[str, Any]) -> None:
        if self.audit_log_path is None:
            return
        with self.audit_log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            file.write("\n")

    def _append_removal_audit_locked(
        self,
        event: dict[str, Any],
        input_ts: InputTimestamp,
        reason: str,
    ) -> None:
        self._append_audit_locked(
            {
                "audit_action": "timestamp_removed",
                "reason": reason,
                "removed_event_id": event.get("id"),
                "removed_event_type": event.get("type"),
                "removed_event": event,
                "unix_time_ms": input_ts.unix_time_ms,
                "unix_time_ns": input_ts.unix_time_ns,
                "monotonic_time_ns": input_ts.monotonic_time_ns,
                "input_source": input_ts.source,
            }
        )

    def _serialize_extra(self, extra: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, AdapterResult):
                out[key] = asdict(value)
            else:
                out[key] = value
        return out
