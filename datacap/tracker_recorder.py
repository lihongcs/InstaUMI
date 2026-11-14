#!/usr/bin/env python3
"""Persistent pyvut recorder used by the Ubuntu datacap controller."""

from __future__ import annotations

import argparse
import csv
import fcntl
import inspect
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from .htc_pose_transform import (
        HTC_OFFSET_MM,
        TRANSFORM_NAME,
        transform_htc_pose_to_eef,
    )
except ImportError:  # Direct worker execution: python datacap/tracker_recorder.py
    from htc_pose_transform import (  # type: ignore[no-redef]
        HTC_OFFSET_MM,
        TRANSFORM_NAME,
        transform_htc_pose_to_eef,
    )


PROTOCOL_PREFIX = "DATACAP_TRACKER_JSON "
EMIT_LOCK = threading.Lock()
CSV_FIELDS = [
    "timestamp",
    "role",
    "pos_x",
    "pos_y",
    "pos_z",
    "ori_x",
    "ori_y",
    "ori_z",
    "ori_w",
    "host_unix_time_ns",
    "host_monotonic_time_ns",
    "tracker_timestamp_ms",
    "tracker_index",
    "mac",
    "buttons",
    "tracking_status",
    "tracking_status_raw",
    "position_x",
    "position_y",
    "position_z",
    "rotation_w",
    "rotation_x",
    "rotation_y",
    "rotation_z",
    "acceleration_x",
    "acceleration_y",
    "acceleration_z",
    "angular_velocity_x",
    "angular_velocity_y",
    "angular_velocity_z",
    # Appended in schema v2 so every legacy column above keeps its name and
    # order.  Empty values mean the selected pyvut backend did not expose the
    # field; no clock is substituted for another one.
    "hid_read_host_realtime_ns",
    "hid_read_host_monotonic_ns",
    "hid_read_host_monotonic_raw_ns",
    "source_packet_index_raw",
    "source_hmd_us_raw",
    "source_pose_index_raw",
    "source_pose_data_index_raw",
    "source_pose_timestamp_raw",
    "source_netsync_raw_json",
]

TRACKER_TIMESTAMP_SCHEMA = {
    "version": 2,
    "legacy_fields": {
        "timestamp": {
            "origin": "datacap recorder callback",
            "clock_domain": "host CLOCK_REALTIME",
            "unit": "seconds",
        },
        "host_unix_time_ns": {
            "origin": "datacap recorder callback",
            "clock_domain": "host CLOCK_REALTIME",
            "unit": "nanoseconds",
        },
        "host_monotonic_time_ns": {
            "origin": "datacap recorder callback",
            "clock_domain": "host CLOCK_MONOTONIC",
            "unit": "nanoseconds",
        },
        "tracker_timestamp_ms": {
            "origin": "legacy pyvut Python pose parse time",
            "clock_domain": "host CLOCK_REALTIME",
            "unit": "milliseconds",
            "warning": "not a native tracker/device timestamp",
        },
    },
    "hid_receive_fields": {
        "hid_read_host_realtime_ns": {
            "origin": "immediately after hidapi.read returned",
            "clock_domain": "host CLOCK_REALTIME",
            "unit": "nanoseconds",
        },
        "hid_read_host_monotonic_ns": {
            "origin": "immediately after hidapi.read returned",
            "clock_domain": "host CLOCK_MONOTONIC",
            "unit": "nanoseconds",
        },
        "hid_read_host_monotonic_raw_ns": {
            "origin": "immediately after hidapi.read returned",
            "clock_domain": "host CLOCK_MONOTONIC_RAW",
            "unit": "nanoseconds",
            "availability": "empty when the platform does not expose CLOCK_MONOTONIC_RAW",
        },
    },
    "device_raw_fields": {
        "source_packet_index_raw": {
            "source_label": "pkt_idx",
            "clock_domain": "not established as a clock",
            "unit": "unknown counter",
        },
        "source_hmd_us_raw": {
            "source_label": "hmd_us (reverse-engineered name)",
            "clock_domain": "unknown",
            "unit": "unknown",
        },
        "source_pose_index_raw": {
            "source_label": "pose slot index in Tracker USB report",
            "clock_domain": "not a clock",
            "unit": "index",
        },
        "source_pose_data_index_raw": {
            "source_label": "index byte in decoded pose payload",
            "clock_domain": "not a clock",
            "unit": "unknown identifier",
        },
        "source_pose_timestamp_raw": {
            "source_label": "pose_timestamp",
            "clock_domain": "unknown",
            "unit": "unknown",
        },
        "source_netsync_raw_json": {
            "source_label": "seven raw netsync integer pairs",
            "clock_domain": "unknown",
            "unit": "unknown",
            "encoding": "compact JSON array of [a,b] pairs",
        },
    },
    "backend_availability": {
        "DONGLE_USB": (
            "pkt_idx and HID receive host clocks are available; hmd_us, "
            "pose_timestamp, pose slot index, and netsync are absent from the "
            "observed RF pose report"
        ),
        "TRACKER_USB": "all listed raw report fields are retained when present",
    },
}

TRACKER_OUTPUT_NAMES = {
    "htc_left": "htc_left.csv",
    "htc_right": "htc_right.csv",
}


def pose_timestamp_csv_values(pose: Any) -> dict[str, Any]:
    """Return lossless optional timing fields for one pyvut pose.

    This helper also keeps old/third-party TrackerPose-like objects compatible:
    missing attributes become empty CSV cells rather than causing recording to
    fail or being synthesized from callback time.
    """

    def optional_int(name: str) -> int | None:
        value = getattr(pose, name, None)
        return None if value is None else int(value)

    raw_pairs = getattr(pose, "source_netsync_raw", ()) or ()
    netsync_json = ""
    if raw_pairs:
        netsync_json = json.dumps(
            [[int(pair[0]), int(pair[1])] for pair in raw_pairs],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return {
        "hid_read_host_realtime_ns": optional_int("hid_read_host_realtime_ns"),
        "hid_read_host_monotonic_ns": optional_int("hid_read_host_monotonic_ns"),
        "hid_read_host_monotonic_raw_ns": optional_int(
            "hid_read_host_monotonic_raw_ns"
        ),
        "source_packet_index_raw": optional_int("source_packet_index_raw"),
        "source_hmd_us_raw": optional_int("source_hmd_us_raw"),
        "source_pose_index_raw": optional_int("source_pose_index_raw"),
        "source_pose_data_index_raw": optional_int("source_pose_data_index_raw"),
        "source_pose_timestamp_raw": optional_int("source_pose_timestamp_raw"),
        "source_netsync_raw_json": netsync_json,
    }


def physical_tracker_id(mac: str) -> str:
    """Return a Tracker identity that is stable across temporary RF slots."""

    parts = str(mac).strip().lower().split(":")
    if len(parts) != 6:
        return str(mac).strip().lower()
    try:
        values = [int(part, 16) for part in parts]
    except ValueError:
        return str(mac).strip().lower()
    # The low nibble of byte 1 is the dongle RF slot (0..4), not the
    # physical Tracker identity. It may change whenever a Tracker reconnects.
    values[1] &= 0xF0
    return ":".join(f"{value:02x}" for value in values)


def tracker_rf_slot(mac: str) -> int | None:
    """Return the temporary dongle RF slot encoded in a pyvut MAC."""

    parts = str(mac).strip().lower().split(":")
    if len(parts) != 6:
        return None
    try:
        return int(parts[1], 16) & 0x0F
    except ValueError:
        return None


def normalize_role_map(mapping: dict[str, Any]) -> dict[str, str]:
    """Normalize configured MACs and prefer an explicit slot-zero identity."""

    normalized: dict[str, str] = {}
    priorities: dict[str, int] = {}
    for raw_mac, raw_role in mapping.items():
        mac = str(raw_mac).strip().lower()
        role = str(raw_role).strip()
        identity = physical_tracker_id(mac)
        parts = mac.split(":")
        try:
            slot = int(parts[1], 16) & 0x0F if len(parts) == 6 else -1
        except ValueError:
            slot = -1
        priority = 1 if slot == 0 else 0
        if identity not in normalized or priority > priorities[identity]:
            normalized[identity] = role
            priorities[identity] = priority
    return normalized


DEFAULT_HTC_ROLE_CONFIG = Path.home() / ".config" / "htc" / "roles.json"
DEFAULT_HTC_LOCK_FILE = Path(
    os.environ.get("HTC_DONGLE_LOCK_FILE", f"/tmp/instaumi_htc_dongle_{os.getuid()}.lock")
)


class HtcDongleLock:
    """Process-wide ownership lock for the exclusive VIVE USB dongle."""

    def __init__(self, path: Path, owner: str) -> None:
        self.path = path.expanduser().resolve()
        self.owner = owner
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
            finally:
                os.close(fd)
            raise RuntimeError(
                "HTC dongle is already in use "
                f"({holder or 'another HTC process'}). Stop that HTC task and retry."
            ) from exc
        metadata = json.dumps(
            {
                "owner": self.owner,
                "pid": os.getpid(),
                "started_unix_time_ns": time.time_ns(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        os.ftruncate(fd, 0)
        os.write(fd, metadata.encode("utf-8"))
        os.fsync(fd)
        self.fd = fd

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_shared_roles(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return physical-ID and hardware-serial role maps from the shared config."""

    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}, {}
    roles = payload.get("roles", {}) if isinstance(payload, dict) else {}
    if not isinstance(roles, dict):
        return {}, {}
    by_physical_id: dict[str, str] = {}
    by_serial: dict[str, str] = {}
    for raw_role, raw_identity in roles.items():
        role = str(raw_role).strip()
        if not role or not isinstance(raw_identity, dict):
            continue
        raw_physical_id = str(raw_identity.get("physical_id", "")).strip()
        serial = str(raw_identity.get("hardware_serial", "")).strip().lower()
        if raw_physical_id:
            by_physical_id[physical_tracker_id(raw_physical_id)] = role
        if serial:
            by_serial[serial] = role
    return by_physical_id, by_serial


def emit(action: str, ok: bool, message: str, **extra: Any) -> None:
    payload = {
        "protocol": "datacap-pyvut-v1",
        "action": action,
        "ok": ok,
        "message": message,
        **extra,
    }
    line = PROTOCOL_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with EMIT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class TrackerRecorder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output = args.output.resolve()
        self.roles = [role.strip() for role in args.roles.split(",") if role.strip()]
        self.required_roles = [role.strip() for role in args.required_roles.split(",") if role.strip()]
        self.role_by_mac = normalize_role_map(json.loads(args.role_map_json))
        shared_by_mac, self.shared_role_by_serial = load_shared_roles(args.role_config)
        self.role_by_mac.update(
            {
                identity: role
                for identity, role in shared_by_mac.items()
                if role in self.roles
            }
        )
        self.identity_cache_path = args.identity_cache.expanduser().resolve()
        self.role_by_serial = self._load_role_by_serial()
        self.dongle_lock = HtcDongleLock(args.lock_file, args.lock_owner)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pose_received = threading.Event()
        self.api: Any = None
        self.simulator_thread: threading.Thread | None = None
        self.files: dict[str, Any] = {}
        self.writers: dict[str, csv.DictWriter] = {}
        self.recording = False
        self.total_samples = 0
        self.session_samples = 0
        self.active_trackers: set[int] = set()
        self.tracker_states: dict[str, dict[str, Any]] = {}
        # Ultimate Tracker pose MACs are synthetic. Usually their non-slot
        # bytes distinguish devices, but some reconnects make two live RF
        # slots report the same normalized identity. Observe concurrent raw
        # MACs and then bind those slots to the stable pyvut index order.
        self.identity_observations: dict[str, dict[str, tuple[int, int]]] = {}
        self.role_by_rf_slot: dict[int, str] = {}
        self.first_host_unix_time_ns: int | None = None
        self.last_host_unix_time_ns: int | None = None
        self.last_flush_monotonic = 0.0
        self.last_telemetry_monotonic = 0.0
        self.last_error = ""

    def initialize(self) -> bool:
        try:
            if self.args.simulate:
                self.simulator_thread = threading.Thread(
                    target=self._simulate_loop,
                    name="SimulatedPyVUT",
                    daemon=True,
                )
                self.simulator_thread.start()
            else:
                self.dongle_lock.acquire()
                pyvut_root = self.args.pyvut_root.resolve()
                sys.path.insert(0, str(pyvut_root))
                from pyvut import UltimateTrackerAPI

                wifi_path = str(self.args.wifi_info.resolve()) if self.args.wifi_info else None
                tracker_options = {
                    "mode": self.args.mode,
                    "wifi_info_path": wifi_path,
                    "debug": self.args.debug,
                    "independent_slam": self.args.independent_slam,
                    "enable_pairing_on_start": not self.args.preserve_existing_connection,
                }
                try:
                    inspect.signature(UltimateTrackerAPI).bind(**tracker_options)
                except TypeError as exc:
                    raise RuntimeError(
                        "Tracker backend is incompatible with DataCap. Use a backend "
                        "that supports independent SLAM and explicit pairing control."
                    ) from exc
                if any(
                    not hasattr(UltimateTrackerAPI, attribute)
                    for attribute in ("polling_alive", "poll_error")
                ):
                    raise RuntimeError("Tracker backend must expose polling_alive and poll_error")
                self.api = UltimateTrackerAPI(**tracker_options)
                self.api.add_pose_callback(self.on_pose)
                self.api.start()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.api is not None:
                try:
                    self.api.stop()
                except Exception:
                    pass
                self.api = None
            self.dongle_lock.release()
            emit("ready", False, f"Tracker initialization failed: {self.last_error}", error=self.last_error)
            return False

        if self.args.require_pose:
            deadline = time.monotonic() + self.args.ready_timeout_sec
            while time.monotonic() < deadline:
                with self.lock:
                    readiness = self._result(True, "tracker initialization readiness")
                if readiness["ready_trackers"] >= self.args.expected_trackers:
                    break
                self.pose_received.wait(min(0.25, max(0.0, deadline - time.monotonic())))
                self.pose_received.clear()
            with self.lock:
                readiness = self._result(True, "tracker initialization readiness")
                active_count = readiness["active_trackers"]
                tracker_indices = readiness["tracker_indices"]
            if readiness["ready_trackers"] < self.args.expected_trackers:
                self.last_error = (
                    f"expected {self.args.expected_trackers} trackers, received poses from "
                    f"{active_count} within {self.args.ready_timeout_sec:.1f}s"
                )
                self.shutdown()
                emit(
                    "ready",
                    False,
                    self.last_error,
                    expected_trackers=self.args.expected_trackers,
                    active_trackers=active_count,
                    tracker_indices=tracker_indices,
                )
                return False

        ready = self._result(
            True,
            "simulated tracker ready" if self.args.simulate else "pyvut tracker connected",
        )
        ready["require_pose"] = self.args.require_pose
        emit("ready", **ready)
        return True

    def on_pose(self, pose: Any) -> None:
        host_unix_ns = time.time_ns()
        host_monotonic_ns = time.monotonic_ns()
        source_timing = pose_timestamp_csv_values(pose)
        index = int(pose.tracker_index)
        mac = str(pose.mac).lower()
        tracking_status_raw = int(pose.tracking_status)
        # Recent Ultimate firmware uses bit 0x10 as an additional flag; the
        # low nibble remains the pose state (2=full 6DoF, 3/4=tracking lost).
        tracking_status = tracking_status_raw & 0x0F
        position = [float(value) for value in pose.position]
        rotation = [float(value) for value in pose.rotation]
        hardware_serial = str(getattr(pose, "hardware_serial", "") or "").strip()
        with self.lock:
            role = self._role_for(
                index,
                mac,
                hardware_serial=hardware_serial,
                observed_unix_ns=host_unix_ns,
            )
        # pyvut uses WXYZ; the calibration utility intentionally uses XYZW.
        quat_xyzw = [rotation[1], rotation[2], rotation[3], rotation[0]]
        eef_position_array, eef_quat_xyzw, eef_pose6_array = transform_htc_pose_to_eef(
            position,
            quat_xyzw,
        )
        eef_position = [float(value) for value in eef_position_array]
        eef_rotation = [
            float(eef_quat_xyzw[3]),
            float(eef_quat_xyzw[0]),
            float(eef_quat_xyzw[1]),
            float(eef_quat_xyzw[2]),
        ]
        eef_pose6 = [float(value) for value in eef_pose6_array]
        telemetry: dict[str, Any] | None = None
        with self.lock:
            self.total_samples += 1
            self.active_trackers.add(index)
            # Dongle MAC addresses encode temporary RF slots and may change
            # after reconnecting. Keep one live state per logical arm role so
            # an old slot cannot remain as a third ghost tracker.
            self.tracker_states[role] = {
                "tracker_index": index,
                "mac": mac,
                "physical_id": physical_tracker_id(mac),
                "rf_slot": tracker_rf_slot(mac),
                "hardware_serial": hardware_serial,
                "role": role,
                "role_source": (
                    "hardware_serial"
                    if hardware_serial.lower() in self.role_by_serial
                    else "rf_slot_duplicate_identity_fallback"
                    if tracker_rf_slot(mac) in self.role_by_rf_slot
                    else "configured_physical_id"
                ),
                "tracking_status": tracking_status,
                "tracking_status_raw": tracking_status_raw,
                "position": position,
                "rotation": rotation,
                "end_effector_position": eef_position,
                "end_effector_rotation": eef_rotation,
                "end_effector_pose6": eef_pose6,
                "pose_transform": TRANSFORM_NAME,
                "last_host_unix_time_ns": host_unix_ns,
                "tracker_timestamp_ms": int(pose.timestamp_ms),
                **source_timing,
            }
            self._learn_serial_roles_if_unambiguous(host_unix_ns)
            self.pose_received.set()
            selected_for_capture = not self.required_roles or role in self.required_roles
            writer = self.writers.get(role)
            if self.recording and writer is not None and selected_for_capture:
                acceleration = list(pose.acceleration)
                angular_velocity = list(pose.angular_velocity)
                writer.writerow(
                    {
                        "host_unix_time_ns": host_unix_ns,
                        "host_monotonic_time_ns": host_monotonic_ns,
                        "timestamp": f"{host_unix_ns / 1_000_000_000.0:.9f}",
                        "role": role,
                        "pos_x": position[0],
                        "pos_y": position[1],
                        "pos_z": position[2],
                        "ori_x": rotation[1],
                        "ori_y": rotation[2],
                        "ori_z": rotation[3],
                        "ori_w": rotation[0],
                        "tracker_timestamp_ms": int(pose.timestamp_ms),
                        "tracker_index": index,
                        "mac": mac,
                        "buttons": int(pose.buttons),
                        "tracking_status": tracking_status,
                        "tracking_status_raw": tracking_status_raw,
                        "position_x": position[0],
                        "position_y": position[1],
                        "position_z": position[2],
                        "rotation_w": rotation[0],
                        "rotation_x": rotation[1],
                        "rotation_y": rotation[2],
                        "rotation_z": rotation[3],
                        "acceleration_x": acceleration[0],
                        "acceleration_y": acceleration[1],
                        "acceleration_z": acceleration[2],
                        "angular_velocity_x": angular_velocity[0],
                        "angular_velocity_y": angular_velocity[1],
                        "angular_velocity_z": angular_velocity[2],
                        **source_timing,
                    }
                )
                self.session_samples += 1
                if self.first_host_unix_time_ns is None:
                    self.first_host_unix_time_ns = host_unix_ns
                self.last_host_unix_time_ns = host_unix_ns
            now = time.monotonic()
            if self.files and (
                self.session_samples % 100 == 0 or now - self.last_flush_monotonic >= 0.5
            ):
                for output_file in self.files.values():
                    output_file.flush()
                self.last_flush_monotonic = now
            if now - self.last_telemetry_monotonic >= 0.1:
                telemetry = self._result(True, "tracker telemetry")
                self.last_telemetry_monotonic = now
        if telemetry is not None:
            emit("telemetry", **telemetry)

    def start_recording(self) -> dict[str, Any]:
        with self.lock:
            if self.recording:
                return self._result(True, "tracker recording was already running")
            readiness = self._result(True, "tracker readiness")
            if readiness["ready_trackers"] < self.args.expected_trackers:
                return self._result(
                    False,
                    f"only {readiness['ready_trackers']}/{self.args.expected_trackers} trackers are ready",
                )
            output_roles = self.required_roles or self.roles
            unsupported_roles = [role for role in output_roles if role not in TRACKER_OUTPUT_NAMES]
            if unsupported_roles:
                return self._result(
                    False,
                    f"no output filename configured for tracker role(s): {', '.join(unsupported_roles)}",
                )
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.files = {}
            self.writers = {}
            try:
                for role in output_roles:
                    path = self.output.parent / TRACKER_OUTPUT_NAMES[role]
                    output_file = path.open("w", encoding="utf-8", newline="")
                    writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
                    writer.writeheader()
                    output_file.flush()
                    self.files[role] = output_file
                    self.writers[role] = writer
            except Exception:
                for output_file in self.files.values():
                    output_file.close()
                self.files = {}
                self.writers = {}
                raise
            self.session_samples = 0
            self.first_host_unix_time_ns = None
            self.last_host_unix_time_ns = None
            self.last_flush_monotonic = time.monotonic()
            self.recording = True
            return self._result(True, "tracker recording started")

    def select_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retarget the persistent receiver to a new capture file without reconnecting USB."""

        with self.lock:
            if self.recording:
                return self._result(False, "cannot change tracker session while recording")
            raw_output = str(payload.get("output", "")).strip()
            if not raw_output:
                return self._result(False, "tracker session output is required")
            self.output = Path(raw_output).expanduser().resolve()
            if payload.get("expected_trackers") is not None:
                self.args.expected_trackers = max(1, int(payload["expected_trackers"]))
            roles = payload.get("roles")
            if isinstance(roles, list):
                self.roles = [str(role).strip() for role in roles if str(role).strip()]
            required_roles = payload.get("required_roles")
            if isinstance(required_roles, list):
                self.required_roles = [
                    str(role).strip()
                    for role in required_roles
                    if str(role).strip()
                ]
            role_by_mac = payload.get("role_by_mac")
            if isinstance(role_by_mac, dict):
                self.role_by_mac = normalize_role_map(role_by_mac)
                remapped_states: dict[str, dict[str, Any]] = {}
                for state in self.tracker_states.values():
                    role = self._role_for(
                        int(state["tracker_index"]),
                        str(state["mac"]),
                        hardware_serial=str(state.get("hardware_serial", "")),
                        observe=False,
                    )
                    state["role"] = role
                    state["role_source"] = (
                        "hardware_serial"
                        if str(state.get("hardware_serial", "")).lower() in self.role_by_serial
                        else "rf_slot_duplicate_identity_fallback"
                        if tracker_rf_slot(str(state["mac"])) in self.role_by_rf_slot
                        else "configured_physical_id"
                    )
                    remapped_states[role] = state
                self.tracker_states = remapped_states
            return self._result(True, "tracker session selected")

    def stop_recording(self) -> dict[str, Any]:
        with self.lock:
            if not self.recording:
                return self._result(True, "tracker recording was already stopped")
            self.recording = False
            for output_file in self.files.values():
                output_file.flush()
                output_file.close()
            self.files = {}
            self.writers = {}
            metadata = self._result(True, "tracker recording stopped")
            metadata["timestamp_schema"] = TRACKER_TIMESTAMP_SCHEMA
            meta_path = self.output.with_name("tracker_meta.json")
            meta_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return metadata

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self._result(True, "tracker status")

    def reset_visualization(self) -> dict[str, Any]:
        """Acknowledge the UI reset; the fixed transform has no origin latch."""

        return self._result(True, "tracker visualization uses the fixed per-frame transform")

    def shutdown(self) -> None:
        self.stop_recording()
        self.stop_event.set()
        if self.api is not None:
            try:
                self.api.stop()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.api = None
        self.dongle_lock.release()
        if self.simulator_thread is not None:
            self.simulator_thread.join(timeout=2.0)
            self.simulator_thread = None

    def _result(self, ok: bool, message: str) -> dict[str, Any]:
        now_ns = time.time_ns()
        if self.args.simulate:
            poll_thread_alive = bool(
                self.simulator_thread is not None and self.simulator_thread.is_alive()
            )
            poll_error = ""
        elif self.api is None:
            poll_thread_alive = False
            poll_error = self.last_error
        else:
            poll_thread_alive = bool(self.api.polling_alive)
            error = self.api.poll_error
            poll_error = "" if error is None else f"{type(error).__name__}: {error}"
        receiver_healthy = bool(poll_thread_alive and not poll_error)
        if not receiver_healthy:
            ok = False
            message = poll_error or "VIVE HID polling thread is not running"
        rf_pair_states: list[int] = []
        if self.api is not None:
            try:
                comms = self.api.tracker_group.comms
                rf_pair_states = [int(value) for value in comms.pair_state]
            except (AttributeError, TypeError, ValueError):
                rf_pair_states = []
        rf_paired_slots = [
            index for index, state in enumerate(rf_pair_states)
            if state & 0x0008
        ]
        tracker_states: list[dict[str, Any]] = []
        for state in self.tracker_states.values():
            item = dict(state)
            item["age_ms"] = max(
                0.0,
                (now_ns - int(item["last_host_unix_time_ns"])) / 1_000_000.0,
            )
            tracker_states.append(item)
        tracker_states.sort(key=lambda item: (item["role"], item["mac"]))
        if self.required_roles:
            tracker_states = [
                item for item in tracker_states
                if item["role"] in self.required_roles
            ]
        recent_states = [item for item in tracker_states if item["age_ms"] <= 1500.0]
        ready_states = [item for item in recent_states if item["tracking_status"] == 2]
        return {
            "ok": ok,
            "message": message,
            "backend": "simulated" if self.args.simulate else "pyvut",
            "mode": self.args.mode,
            "connected": receiver_healthy,
            "receiver_open": bool(self.args.simulate or self.api is not None),
            "receiver_healthy": receiver_healthy,
            "poll_thread_alive": poll_thread_alive,
            "poll_error": poll_error,
            "pairing_enabled_on_start": bool(
                self.args.mode == "DONGLE_USB"
                and not self.args.preserve_existing_connection
            ),
            "recording": self.recording,
            "sample_count": self.session_samples,
            "total_sample_count": self.total_samples,
            "active_trackers": len(tracker_states),
            "recent_trackers": len(recent_states),
            "ready_trackers": len(ready_states),
            "expected_trackers": self.args.expected_trackers,
            "required_roles": list(self.required_roles),
            "tracker_indices": sorted(int(item["tracker_index"]) for item in tracker_states),
            "tracker_states": tracker_states,
            "rf_pair_states": rf_pair_states,
            "rf_paired_slots": rf_paired_slots,
            "identity_cache": str(self.identity_cache_path),
            "serial_roles_learned": len(self.role_by_serial),
            "pose_transform": {
                "name": TRANSFORM_NAME,
                "sensor": "htc",
                "applies_to_all_trackers": True,
                "offset_mm": [float(value) for value in HTC_OFFSET_MM],
                "offset_frame": "world",
                "right_rotation": "Ry(+90deg)",
                "mount_y_degrees": 0.0,
                "world_axes": {
                    "positive_x": "back",
                    "positive_y": "left",
                    "positive_z": "down",
                },
                "raw_pose_preserved": True,
            },
            "quaternion_schema": {
                "version": 2,
                "firmware_payload_order": "xyzw",
                "tracker_pose_api_order": "wxyz",
                "csv_ori_order": "xyzw",
                "csv_ori_equals_firmware_payload": True,
            },
            "first_host_unix_time_ns": self.first_host_unix_time_ns,
            "last_host_unix_time_ns": self.last_host_unix_time_ns,
            "outputs": {
                role: str(self.output.parent / filename)
                for role, filename in TRACKER_OUTPUT_NAMES.items()
                if not self.required_roles or role in self.required_roles
            },
            "error": self.last_error,
        }

    def _role_for(
        self,
        tracker_index: int,
        mac: str,
        *,
        hardware_serial: str = "",
        observed_unix_ns: int | None = None,
        observe: bool = True,
    ) -> str:
        identity = physical_tracker_id(mac)
        slot = tracker_rf_slot(mac)
        serial_key = str(hardware_serial).strip().lower()

        if serial_key and serial_key in self.role_by_serial:
            return self.role_by_serial[serial_key]

        if observe:
            now_ns = int(observed_unix_ns or time.time_ns())
            observations = self.identity_observations.setdefault(identity, {})
            observations[mac.lower()] = (int(tracker_index), now_ns)
            cutoff_ns = now_ns - 1_500_000_000
            stale_macs = [
                raw_mac for raw_mac, (_index, seen_ns) in observations.items()
                if seen_ns < cutoff_ns
            ]
            for raw_mac in stale_macs:
                observations.pop(raw_mac, None)

            # The same normalized synthetic identity is arriving from two
            # live tracker indices. Bind each RF slot using the configured
            # pyvut order (index 0=right, index 1=left) instead of collapsing
            # both streams into role_by_mac's single physical-ID entry.
            if len({item[0] for item in observations.values()}) > 1:
                for raw_mac, (observed_index, _seen_ns) in observations.items():
                    observed_slot = tracker_rf_slot(raw_mac)
                    if observed_slot is not None and observed_index < len(self.roles):
                        self.role_by_rf_slot[observed_slot] = self.roles[observed_index]

        if slot is not None and slot in self.role_by_rf_slot:
            return self.role_by_rf_slot[slot]

        mapped = self.role_by_mac.get(identity)
        if mapped:
            return mapped
        if tracker_index < len(self.roles):
            return self.roles[tracker_index]
        return f"tracker_{tracker_index + 1}"

    def _load_role_by_serial(self) -> dict[str, str]:
        role_by_serial: dict[str, str] = {}
        try:
            payload = json.loads(self.identity_cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            mapping = {}
        else:
            mapping = payload.get("role_by_serial", payload) if isinstance(payload, dict) else {}
        if isinstance(mapping, dict):
            role_by_serial.update(
                {
                    str(serial).strip().lower(): str(role).strip()
                    for serial, role in mapping.items()
                    if str(serial).strip() and str(role).strip() in self.roles
                }
            )
        # The explicitly managed shared config is authoritative over the
        # recorder's older learned identity cache.
        role_by_serial.update(
            {
                serial: role
                for serial, role in self.shared_role_by_serial.items()
                if role in self.roles
            }
        )
        return role_by_serial

    def _learn_serial_roles_if_unambiguous(self, now_ns: int) -> None:
        """Persist serial roles only after two distinct configured IDs agree."""

        recent = [
            state for state in self.tracker_states.values()
            if now_ns - int(state.get("last_host_unix_time_ns", 0)) <= 1_500_000_000
            and int(state.get("tracking_status", 0)) == 2
            and state.get("role") in self.required_roles
            and str(state.get("hardware_serial", "")).strip()
        ]
        if len(recent) < self.args.expected_trackers:
            return
        selected = {str(state["role"]): state for state in recent}
        expected_roles = set(self.required_roles[: self.args.expected_trackers])
        if set(selected) != expected_roles:
            return
        physical_ids = {str(state.get("physical_id", "")) for state in selected.values()}
        serials = {
            str(state.get("hardware_serial", "")).strip().lower()
            for state in selected.values()
        }
        if len(physical_ids) != len(selected) or len(serials) != len(selected):
            return

        learned = {
            str(state["hardware_serial"]).strip().lower(): role
            for role, state in selected.items()
        }
        if all(self.role_by_serial.get(serial) == role for serial, role in learned.items()):
            return
        self.role_by_serial.update(learned)
        try:
            self.identity_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.identity_cache_path.with_suffix(self.identity_cache_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(
                    {"version": 1, "role_by_serial": self.role_by_serial},
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.identity_cache_path)
        except OSError as exc:
            self.last_error = f"failed to save tracker identity cache: {exc}"

    def _simulate_loop(self) -> None:
        interval = 1.0 / max(1.0, self.args.rate_hz)
        started = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            elapsed = now - started
            for index in range(self.args.simulated_trackers):
                phase = elapsed + index * 0.4
                pose = SimpleNamespace(
                    tracker_index=index,
                    mac=f"sim:00:00:00:00:{index:02x}",
                    buttons=0,
                    tracking_status=2,
                    timestamp_ms=int(elapsed * 1000),
                    position=[0.15 * math.sin(phase), 0.15 * math.cos(phase), 0.3 + index * 0.1],
                    rotation=[1.0, 0.0, 0.0, 0.0],
                    acceleration=[0.0, 0.0, 0.0],
                    angular_velocity=[0.0, 0.0, 0.0],
                )
                self.on_pose(pose)
            self.stop_event.wait(max(0.0, interval - (time.monotonic() - now)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record pyvut poses for datacap")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pyvut-root", type=Path, default=Path(__file__).resolve().parents[1] / "pyvut")
    parser.add_argument("--mode", choices=["DONGLE_USB", "TRACKER_USB"], default="DONGLE_USB")
    parser.add_argument("--wifi-info", type=Path)
    parser.add_argument("--require-pose", action="store_true")
    parser.add_argument("--ready-timeout-sec", type=float, default=10.0)
    parser.add_argument("--expected-trackers", type=int, default=1)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable verbose pyvut receiver diagnostics",
    )
    parser.add_argument("--rate-hz", type=float, default=60.0)
    parser.add_argument("--simulated-trackers", type=int, default=2)
    parser.add_argument(
        "--independent-slam",
        action="store_true",
        help="run every dongle-connected tracker as an independent SLAM host",
    )
    parser.add_argument(
        "--preserve-existing-connection",
        action="store_true",
        help="do not put the dongle back into pairing mode when the worker starts",
    )
    parser.add_argument("--roles", default="")
    parser.add_argument("--required-roles", default="")
    parser.add_argument("--role-map-json", default="{}")
    parser.add_argument(
        "--identity-cache",
        type=Path,
        default=Path.home() / ".config" / "instadatacap" / "tracker_identities.json",
        help="persistent hardware-serial to left/right role cache",
    )
    parser.add_argument(
        "--role-config",
        type=Path,
        default=DEFAULT_HTC_ROLE_CONFIG,
        help="shared HTC hardware identity and left/right role config",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_HTC_LOCK_FILE,
        help="shared exclusive VIVE dongle ownership lock",
    )
    parser.add_argument("--lock-owner", default="instadatacap")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    recorder = TrackerRecorder(args)
    if not recorder.initialize():
        return 2
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            action, _, argument = line.partition(" ")
            command = action.upper()
            if command == "START":
                emit("start", **recorder.start_recording())
            elif command == "STOP":
                emit("stop", **recorder.stop_recording())
            elif command == "STATUS":
                emit("status", **recorder.status())
            elif command == "RESET_VISUALIZATION":
                emit("reset_visualization", **recorder.reset_visualization())
            elif command == "SESSION":
                try:
                    payload = json.loads(argument)
                    if not isinstance(payload, dict):
                        raise ValueError("SESSION payload must be a JSON object")
                    emit("session", **recorder.select_session(payload))
                except Exception as exc:
                    emit("session", False, f"invalid tracker session: {exc}")
            elif command == "QUIT":
                recorder.shutdown()
                emit("quit", True, "tracker worker closing")
                return 0
            elif command:
                emit("unknown", False, f"unknown command: {command}")
    except KeyboardInterrupt:
        return 0
    finally:
        recorder.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
