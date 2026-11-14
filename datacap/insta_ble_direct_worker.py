#!/usr/bin/python3
"""Direct BlueZ BLE-central control for one or more Insta360 cameras.

The camera exposes service BE80, command characteristic BE81 and notification
characteristic BE82.  This worker keeps those connections alive and exposes a
small line protocol to datacap.  Real camera control and ``--simulate`` are
deliberately separate modes.
"""

from __future__ import annotations

import argparse
import json
import signal
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dbus
import dbus.mainloop.glib
from gi.repository import GLib


BLUEZ = "org.bluez"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHRC = "org.bluez.GattCharacteristic1"

BT_BASE = "0000{}-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = BT_BASE.format("be80")
WRITE_UUID = BT_BASE.format("be81")
NOTIFY_UUID = BT_BASE.format("be82")

PHONE_COMMAND_START_CAPTURE = 4
PHONE_COMMAND_STOP_CAPTURE = 5
PHONE_COMMAND_GET_CURRENT_CAPTURE_STATUS = 15
RESPONSE_OK = 200
PROTOCOL_PREFIX = "DATACAP_BLE_DIRECT_JSON "


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any]) -> None:
    payload.setdefault("unix_time_ns", time.time_ns())
    payload.setdefault("monotonic_ns", time.monotonic_ns())
    print(PROTOCOL_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def phone_command(command: int, sequence: int, body: bytes = b"") -> bytes:
    """Build the packet used by the Insta360 app protocol over BE81."""

    payload = (
        b"\x04\x00\x00"
        + struct.pack("<H", command)
        + b"\x02"
        + struct.pack("<I", sequence)[:3]
        + b"\x80\x00\x00"
        + body
    )
    return struct.pack("<I", len(payload) + 4) + payload


class EventLog:
    def __init__(self, role_by_address: dict[str, str] | None = None) -> None:
        self.path: Path | None = None
        self.lock = threading.Lock()
        self.role_by_address = {
            str(address).upper(): str(role)
            for address, role in (role_by_address or {}).items()
        }

    def select(self, raw_path: str) -> None:
        path = Path(raw_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.write("session_log_selected", path=str(path))

    def write(self, event: str, **fields: Any) -> None:
        address = str(fields.get("address", "")).upper()
        if address and "role" not in fields and address in self.role_by_address:
            fields["role"] = self.role_by_address[address]
        record = {
            "event": event,
            "iso_time": utc_now(),
            "unix_time_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        with self.lock:
            if self.path is None:
                return
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass
class PendingCommand:
    action: str
    command: int
    sequence: int
    dispatch_unix_ns: int
    dispatch_monotonic_ns: int
    local_done: threading.Event = field(default_factory=threading.Event)
    ack_done: threading.Event = field(default_factory=threading.Event)
    local_ok: bool = False
    local_error: str = ""
    local_complete_unix_ns: int | None = None
    local_complete_monotonic_ns: int | None = None
    ack_unix_ns: int | None = None
    ack_monotonic_ns: int | None = None
    response_code: int | None = None


@dataclass
class Camera:
    address: str
    name: str
    role: str
    device_path: str
    write_path: str
    notify_path: str
    connected: bool = True
    notifying: bool = True
    recording_expected: bool = False
    rx_buffer: bytearray = field(default_factory=bytearray)
    last_packet_hex: str = ""
    last_response_code: int | None = None
    last_response_sequence: int | None = None


class DirectRuntime:
    def __init__(
        self,
        bus: dbus.SystemBus,
        adapter_name: str,
        expected_cameras: int,
        require_all: bool,
        addresses: list[str],
        role_by_address: dict[str, str],
        name_prefixes: list[str],
        discovery_timeout_sec: float,
        ack_timeout_sec: float,
    ) -> None:
        self.bus = bus
        self.adapter_name = adapter_name
        self.adapter_path = f"/org/bluez/{adapter_name}"
        self.expected_cameras = max(1, expected_cameras)
        self.require_all = require_all
        self.addresses = {item.upper() for item in addresses if item.strip()}
        self.role_by_address = {
            str(address).upper(): str(role)
            for address, role in role_by_address.items()
        }
        self.name_prefixes = tuple(name_prefixes or ["X5 "])
        self.discovery_timeout_sec = max(1.0, discovery_timeout_sec)
        self.ack_timeout_sec = max(0.0, ack_timeout_sec)
        self.cameras: dict[str, Camera] = {}
        self.pending: dict[tuple[str, int], PendingCommand] = {}
        self.sequence = 512
        self.lock = threading.RLock()
        self.event_log = EventLog(self.role_by_address)
        self.loop: GLib.MainLoop | None = None
        self.discovery_active = False
        self.bus.add_signal_receiver(
            self._properties_changed,
            dbus_interface=DBUS_PROP,
            signal_name="PropertiesChanged",
            path_keyword="path",
        )

    def initialize(self) -> None:
        adapter_obj = self.bus.get_object(BLUEZ, self.adapter_path)
        props = dbus.Interface(adapter_obj, DBUS_PROP)
        if not bool(props.Get(ADAPTER_IFACE, "Powered")):
            props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
        self.start_discovery()

    def start_discovery(self) -> None:
        if self.discovery_active:
            return
        adapter = dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)
        try:
            adapter.SetDiscoveryFilter(dbus.Dictionary({"Transport": dbus.String("le")}, signature="sv"))
        except Exception:
            pass
        try:
            adapter.StartDiscovery()
        except dbus.DBusException as exc:
            if "InProgress" not in str(exc):
                raise
        self.discovery_active = True

    def stop_discovery(self) -> None:
        if not self.discovery_active:
            return
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE).StopDiscovery()
        except Exception:
            pass
        self.discovery_active = False

    def managed_objects(self) -> dict[Any, Any]:
        return dbus.Interface(self.bus.get_object(BLUEZ, "/"), DBUS_OM).GetManagedObjects()

    def candidate_devices(self, respect_addresses: bool = True) -> list[tuple[str, str, str, bool]]:
        found: list[tuple[str, str, str, bool]] = []
        for path, interfaces in self.managed_objects().items():
            props = interfaces.get(DEVICE_IFACE)
            if not props:
                continue
            address = str(props.get("Address", "")).upper()
            name = str(props.get("Name", props.get("Alias", "")))
            if self.addresses and respect_addresses:
                matches = address in self.addresses
            else:
                matches = any(name.startswith(prefix) for prefix in self.name_prefixes)
            if matches:
                found.append((address, name, str(path), bool(props.get("Connected", False))))
        found.sort(key=lambda item: (item[0] not in self.addresses, item[0]))
        return found

    def scan(self, timeout_sec: float = 5.0) -> dict[str, Any]:
        self.start_discovery()
        started = time.monotonic()
        deadline = time.monotonic() + max(0.5, timeout_sec)
        discovered: dict[str, dict[str, Any]] = {}
        emit({
            "ok": True,
            "action": "scan_progress",
            "message": f"Scanning for X5 cameras; target: {self.expected_cameras}",
            "discovered_cameras": [],
            **self.snapshot(),
        })
        while time.monotonic() < deadline:
            previous = set(discovered)
            for address, name, path, connected in self.candidate_devices(respect_addresses=False):
                discovered[address] = {
                    "address": address,
                    "name": name,
                    "role": self.role_by_address.get(address, "insta_unassigned"),
                    "device_path": path,
                    "connected": connected,
                    "selected": not self.addresses or address in self.addresses,
                }
            if set(discovered) != previous:
                current = sorted(discovered.values(), key=lambda item: item["address"])
                emit({
                    "ok": True,
                    "action": "scan_progress",
                    "message": f"Discovered {len(current)} X5 camera(s)",
                    "discovered_cameras": current,
                    **self.snapshot(),
                })
            selected_count = sum(1 for item in discovered.values() if item["selected"])
            if selected_count >= self.expected_cameras and time.monotonic() - started >= 0.6:
                break
            time.sleep(0.2)
        devices = sorted(discovered.values(), key=lambda item: item["address"])
        self.stop_discovery()
        result = {
            "ok": bool(devices),
            "action": "scan",
            "message": f"found {len(devices)} matching Insta360 camera(s)",
            "discovered_cameras": devices,
            **self.snapshot(),
        }
        self.event_log.write("ble_scan_result", **result)
        return result

    def connect_expected(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.discovery_timeout_sec
        errors: list[str] = []
        self.start_discovery()
        emit({
            "ok": True,
            "action": "connect_progress",
            "message": f"Connecting {self.expected_cameras} X5 camera(s)",
            **self.snapshot(),
        })
        while time.monotonic() < deadline:
            self._drop_disconnected()
            if len(self.ready_cameras()) >= self.expected_cameras:
                self.stop_discovery()
                break
            with self.lock:
                ready_addresses = {
                    address
                    for address, camera in self.cameras.items()
                    if camera.connected and camera.notifying
                }
            candidates = [
                item for item in self.candidate_devices()
                if item[0] not in ready_addresses
            ]
            if not candidates:
                self.start_discovery()
                time.sleep(0.2)
                continue

            # Connecting while LE discovery is active is slow and can make a
            # second BlueZ Connect() block until the default 25 s D-Bus timeout.
            # Stop scanning and connect every missing camera concurrently.
            self.stop_discovery()
            workers = min(len(candidates), max(1, self.expected_cameras))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._connect_candidate, candidate, deadline): candidate
                    for candidate in candidates
                }
                for future in as_completed(futures):
                    address, name, _path, _connected = futures[future]
                    try:
                        future.result()
                        emit({
                            "ok": True,
                            "action": "connect_progress",
                            "message": f"Connected {name}; {len(self.ready_cameras())}/{self.expected_cameras} ready",
                            "current_address": address,
                            **self.snapshot(),
                        })
                    except Exception as exc:
                        error = f"{address}: {exc}"
                        if error not in errors:
                            errors.append(error)
                        emit({
                            "ok": False,
                            "action": "connect_progress",
                            "message": f"Connection retry: {error}",
                            "current_address": address,
                            "errors": errors[-5:],
                            **self.snapshot(),
                        })
            if len(self.ready_cameras()) >= self.expected_cameras:
                self.stop_discovery()
                break
            self.start_discovery()
            time.sleep(0.2)
        snapshot = self.snapshot()
        enough = len(self.ready_cameras()) >= (self.expected_cameras if self.require_all else 1)
        return {
            "ok": enough,
            "action": "connect",
            "message": (
                f"connected {snapshot['connected_cameras']}/{self.expected_cameras} Insta360 camera(s)"
                if enough
                else f"only {snapshot['connected_cameras']}/{self.expected_cameras} Insta360 camera(s) connected"
            ),
            "errors": errors[-10:],
            **snapshot,
        }

    def _connect_candidate(
        self,
        candidate: tuple[str, str, str, bool],
        deadline: float,
    ) -> None:
        address, name, path, connected = candidate
        emit({
            "ok": True,
            "action": "connect_progress",
            "message": f"Connecting {name} ({address}) in parallel",
            "current_address": address,
            **self.snapshot(),
        })
        if not connected:
            remaining = max(1.0, deadline - time.monotonic())
            dbus.Interface(self.bus.get_object(BLUEZ, path), DEVICE_IFACE).Connect(
                timeout=min(8.0, remaining)
            )
        self._wait_services(path, deadline)
        write_path, notify_path = self._find_characteristics(path)
        notify = dbus.Interface(self.bus.get_object(BLUEZ, notify_path), GATT_CHRC)
        try:
            notify.StartNotify(timeout=min(5.0, max(1.0, deadline - time.monotonic())))
        except dbus.DBusException as exc:
            if "Already notifying" not in str(exc):
                raise
        with self.lock:
            self.cameras[address] = Camera(
                address,
                name,
                self.role_by_address.get(address, "insta_unassigned"),
                path,
                write_path,
                notify_path,
            )
        self.event_log.write(
            "camera_connected",
            address=address,
            name=name,
            write_path=write_path,
            notify_path=notify_path,
        )

    def _wait_services(self, device_path: str, deadline: float) -> None:
        props = dbus.Interface(self.bus.get_object(BLUEZ, device_path), DBUS_PROP)
        while time.monotonic() < deadline:
            if bool(props.Get(DEVICE_IFACE, "ServicesResolved")):
                return
            time.sleep(0.1)
        raise TimeoutError("GATT services were not resolved")

    def _find_characteristics(self, device_path: str) -> tuple[str, str]:
        write_path = ""
        notify_path = ""
        for path, interfaces in self.managed_objects().items():
            path_text = str(path)
            if not path_text.startswith(device_path + "/"):
                continue
            props = interfaces.get(GATT_CHRC)
            if not props:
                continue
            uuid = str(props.get("UUID", "")).lower()
            if uuid == WRITE_UUID:
                write_path = path_text
            elif uuid == NOTIFY_UUID:
                notify_path = path_text
        if not write_path or not notify_path:
            raise RuntimeError(f"camera does not expose {SERVICE_UUID} BE81/BE82")
        return write_path, notify_path

    def ready_cameras(self) -> list[Camera]:
        with self.lock:
            return [camera for camera in self.cameras.values() if camera.connected and camera.notifying]

    def _drop_disconnected(self) -> None:
        objects = self.managed_objects()
        with self.lock:
            for camera in self.cameras.values():
                props = objects.get(dbus.ObjectPath(camera.device_path), {}).get(DEVICE_IFACE, {})
                camera.connected = bool(props.get("Connected", False))

    def next_sequence(self) -> int:
        with self.lock:
            value = self.sequence
            self.sequence = 512 if value >= 0xFFFFFE else value + 1
            return value

    def dispatch(self, action: str, command: int, body: bytes = b"") -> dict[str, Any]:
        cameras = self.ready_cameras()
        required = self.expected_cameras if self.require_all else 1
        if len(cameras) < required:
            return {
                "ok": False,
                "action": action,
                "message": f"only {len(cameras)}/{self.expected_cameras} required camera(s) are ready",
                **self.snapshot(),
            }

        sequence = self.next_sequence()
        packet = phone_command(command, sequence, body)
        entries: dict[str, PendingCommand] = {}
        # Resolve every local DBus proxy before the first write so proxy setup
        # does not inflate the measured or actual inter-camera dispatch skew.
        characteristics = {
            camera.address: dbus.Interface(
                self.bus.get_object(BLUEZ, camera.write_path), GATT_CHRC
            )
            for camera in cameras
        }

        for camera in cameras:
            pending = PendingCommand(
                action=action,
                command=command,
                sequence=sequence,
                dispatch_unix_ns=time.time_ns(),
                dispatch_monotonic_ns=time.monotonic_ns(),
            )
            entries[camera.address] = pending
            with self.lock:
                self.pending[(camera.address, sequence)] = pending

            def completed(address: str = camera.address, item: PendingCommand = pending) -> None:
                item.local_ok = True
                item.local_complete_unix_ns = time.time_ns()
                item.local_complete_monotonic_ns = time.monotonic_ns()
                item.local_done.set()
                self.event_log.write(
                    "ble_write_complete",
                    action=action,
                    address=address,
                    sequence=sequence,
                    ok=True,
                    dispatch_unix_ns=item.dispatch_unix_ns,
                    complete_unix_ns=item.local_complete_unix_ns,
                )

            def failed(error: Exception, address: str = camera.address, item: PendingCommand = pending) -> None:
                item.local_ok = False
                item.local_error = str(error)
                item.local_complete_unix_ns = time.time_ns()
                item.local_complete_monotonic_ns = time.monotonic_ns()
                item.local_done.set()
                self.event_log.write(
                    "ble_write_complete",
                    action=action,
                    address=address,
                    sequence=sequence,
                    ok=False,
                    error=item.local_error,
                )

            characteristic = characteristics[camera.address]
            # Timestamp the actual async write dispatch, not DBus proxy lookup
            # and callback construction. This makes inter-camera dispatch skew
            # a faithful host-side metric (camera-internal start remains async).
            pending.dispatch_unix_ns = time.time_ns()
            pending.dispatch_monotonic_ns = time.monotonic_ns()
            characteristic.WriteValue(
                dbus.Array(packet, signature="y"),
                dbus.Dictionary({}, signature="sv"),
                reply_handler=completed,
                error_handler=failed,
            )
            self.event_log.write(
                "ble_command_dispatched",
                action=action,
                command=command,
                address=camera.address,
                name=camera.name,
                sequence=sequence,
                packet_hex=packet.hex(" "),
                dispatch_unix_ns=pending.dispatch_unix_ns,
                dispatch_monotonic_ns=pending.dispatch_monotonic_ns,
            )

        local_deadline = time.monotonic() + 4.0
        for pending in entries.values():
            pending.local_done.wait(max(0.0, local_deadline - time.monotonic()))
        if self.ack_timeout_sec > 0:
            ack_deadline = time.monotonic() + self.ack_timeout_sec
            for pending in entries.values():
                pending.ack_done.wait(max(0.0, ack_deadline - time.monotonic()))

        details = [self._pending_result(address, item) for address, item in entries.items()]
        local_ok = all(item.local_ok for item in entries.values())
        if local_ok and action in {"start", "stop"}:
            expected_state = action == "start"
            with self.lock:
                for camera in cameras:
                    camera.recording_expected = expected_state

        dispatch_times = [item.dispatch_monotonic_ns for item in entries.values()]
        ack_times = [item.ack_monotonic_ns for item in entries.values() if item.ack_monotonic_ns is not None]
        result = {
            "ok": local_ok,
            "action": action,
            "message": (
                f"{action} written to {len(entries)} camera(s)"
                if local_ok
                else f"{action} failed for one or more cameras"
            ),
            "sequence": sequence,
            "packet_hex": packet.hex(" "),
            "camera_results": details,
            "host_dispatch_skew_ms": self._spread_ms(dispatch_times),
            "camera_ack_skew_ms": self._spread_ms(ack_times) if len(ack_times) > 1 else None,
            "state_verified": len(ack_times) == len(entries),
            **self.snapshot(),
        }
        self.event_log.write("ble_command_result", **result)
        with self.lock:
            for address in entries:
                self.pending.pop((address, sequence), None)
        return result

    @staticmethod
    def _spread_ms(values: list[int]) -> float:
        return (max(values) - min(values)) / 1_000_000.0 if values else 0.0

    def _pending_result(self, address: str, pending: PendingCommand) -> dict[str, Any]:
        rtt_ms = None
        if pending.ack_monotonic_ns is not None:
            rtt_ms = (pending.ack_monotonic_ns - pending.dispatch_monotonic_ns) / 1_000_000.0
        local_write_ms = None
        if pending.local_complete_monotonic_ns is not None:
            local_write_ms = (
                pending.local_complete_monotonic_ns - pending.dispatch_monotonic_ns
            ) / 1_000_000.0
        return {
            "address": address,
            "role": self.role_by_address.get(address, "insta_unassigned"),
            "local_write_ok": pending.local_ok,
            "local_error": pending.local_error or None,
            "dispatch_unix_ns": pending.dispatch_unix_ns,
            "dispatch_monotonic_ns": pending.dispatch_monotonic_ns,
            "local_complete_unix_ns": pending.local_complete_unix_ns,
            "local_write_ms": local_write_ms,
            "ack_unix_ns": pending.ack_unix_ns,
            "ack_rtt_ms": rtt_ms,
            "response_code": pending.response_code,
        }

    def _properties_changed(self, interface: str, changed: dict[str, Any], _invalidated: Any, path: str = "") -> None:
        path = str(path)
        if interface == DEVICE_IFACE and "Connected" in changed:
            with self.lock:
                for camera in self.cameras.values():
                    if camera.device_path == path:
                        camera.connected = bool(changed["Connected"])
                        if not camera.connected:
                            camera.notifying = False
                            self.event_log.write("camera_disconnected", address=camera.address, name=camera.name)
        if interface != GATT_CHRC or "Value" not in changed:
            return
        with self.lock:
            camera = next((item for item in self.cameras.values() if item.notify_path == path), None)
        if camera is not None:
            self._feed_notification(camera, bytes(changed["Value"]))

    def _feed_notification(self, camera: Camera, chunk: bytes) -> None:
        with self.lock:
            camera.rx_buffer.extend(chunk)
            while len(camera.rx_buffer) >= 4:
                packet_len = struct.unpack("<I", camera.rx_buffer[:4])[0]
                if packet_len < 7 or packet_len > 1024 * 1024:
                    camera.rx_buffer.clear()
                    self.event_log.write("ble_invalid_notification", address=camera.address, chunk_hex=chunk.hex(" "))
                    return
                if len(camera.rx_buffer) < packet_len:
                    return
                packet = bytes(camera.rx_buffer[:packet_len])
                del camera.rx_buffer[:packet_len]
                self._handle_packet(camera, packet)

    def _handle_packet(self, camera: Camera, packet: bytes) -> None:
        camera.last_packet_hex = packet.hex(" ")
        response_code = None
        sequence = None
        if len(packet) >= 16 and packet[4:7] == b"\x04\x00\x00":
            response_code = struct.unpack("<H", packet[7:9])[0]
            sequence = int.from_bytes(packet[10:13], "little")
            pending = self.pending.get((camera.address, sequence))
            if pending is not None:
                camera.last_response_code = response_code
                camera.last_response_sequence = sequence
                pending.response_code = response_code
                pending.ack_unix_ns = time.time_ns()
                pending.ack_monotonic_ns = time.monotonic_ns()
                pending.ack_done.set()
        self.event_log.write(
            "ble_notification",
            address=camera.address,
            name=camera.name,
            packet_hex=packet.hex(" "),
            response_code=response_code,
            sequence=sequence,
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            cameras = [
                {
                    "address": camera.address,
                    "name": camera.name,
                    "role": camera.role,
                    "connected": camera.connected,
                    "notifying": camera.notifying,
                    "recording_expected": camera.recording_expected,
                    "last_response_code": camera.last_response_code,
                    "last_response_sequence": camera.last_response_sequence,
                }
                for camera in self.cameras.values()
            ]
        connected = sum(1 for camera in cameras if camera["connected"] and camera["notifying"])
        return {
            "backend": "bluez_direct_camera_ble",
            "transport": "bluetooth_le",
            "protocol": "insta360_be80_phone_command",
            "direct_camera_ble": True,
            "adapter": self.adapter_name,
            "expected_cameras": self.expected_cameras,
            "require_all": self.require_all,
            "connected_cameras": connected,
            "connected": connected > 0,
            "ready": connected >= (self.expected_cameras if self.require_all else 1),
            "recording_expected": any(camera["recording_expected"] for camera in cameras),
            "cameras": cameras,
            "command_semantics": "explicit_start_stop",
        }

    def disconnect(self) -> None:
        self.stop_discovery()
        with self.lock:
            cameras = list(self.cameras.values())
        for camera in cameras:
            try:
                dbus.Interface(self.bus.get_object(BLUEZ, camera.device_path), DEVICE_IFACE).Disconnect()
            except Exception:
                pass


def run_simulated(expected_cameras: int, camera_roles: list[str]) -> int:
    roles = list(camera_roles or ["insta_right", "insta_left"])
    role_by_address = {
        f"SIM:{index + 1:02d}": roles[index] if index < len(roles) else "insta_unassigned"
        for index in range(max(1, expected_cameras))
    }
    log = EventLog(role_by_address)
    recording = False
    cameras = [
        {
            "address": f"SIM:{index + 1:02d}",
            "name": f"X5 SIM{index + 1}",
            "role": role_by_address[f"SIM:{index + 1:02d}"],
            "connected": True,
            "notifying": True,
        }
        for index in range(max(1, expected_cameras))
    ]

    def snapshot() -> dict[str, Any]:
        return {
            "backend": "simulated_bluez_direct_camera_ble",
            "transport": "simulated_bluetooth_le",
            "protocol": "insta360_be80_phone_command",
            "direct_camera_ble": True,
            "expected_cameras": expected_cameras,
            "connected_cameras": len(cameras),
            "connected": True,
            "ready": True,
            "recording_expected": recording,
            "state_verified": True,
            "cameras": cameras,
            "command_semantics": "explicit_start_stop",
        }

    emit({"ok": True, "action": "ready", "message": "simulated direct BLE worker ready", **snapshot()})
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        action, _, argument = line.partition(" ")
        action = action.upper()
        if action == "SESSION":
            log.select(json.loads(argument))
            emit({"ok": True, "action": "session", "message": "session log selected", **snapshot()})
        elif action == "CONNECT":
            emit({"ok": True, "action": "connect", "message": "simulated cameras connected", **snapshot()})
        elif action == "SCAN":
            emit(
                {
                    "ok": True,
                    "action": "scan",
                    "message": f"found {len(cameras)} simulated camera(s)",
                    "discovered_cameras": cameras,
                    **snapshot(),
                }
            )
        elif action in {"START", "STOP", "STATUS"}:
            if action == "START":
                recording = True
            elif action == "STOP":
                recording = False
            key = action.lower()
            now = time.time_ns()
            results = [
                {
                    "address": camera["address"],
                    "role": camera["role"],
                    "local_write_ok": True,
                    "dispatch_unix_ns": now + index * 100_000,
                    "local_complete_unix_ns": now + index * 100_000 + 1_000_000,
                    "ack_unix_ns": now + index * 100_000 + 2_000_000,
                    "ack_rtt_ms": 2.0,
                    "response_code": RESPONSE_OK,
                }
                for index, camera in enumerate(cameras)
            ]
            log.write("simulated_command", action=key, camera_results=results)
            emit(
                {
                    "ok": True,
                    "action": key,
                    "message": f"simulated {key}",
                    "camera_results": results,
                    "host_dispatch_skew_ms": 0.1 * max(0, len(cameras) - 1),
                    "camera_ack_skew_ms": 0.1 * max(0, len(cameras) - 1),
                    **snapshot(),
                }
            )
        elif action == "QUIT":
            recording = False
            emit({"ok": True, "action": "quit", "message": "simulated worker stopped", **snapshot()})
            return 0
        else:
            emit({"ok": False, "action": action.lower(), "message": f"unknown command: {action}", **snapshot()})
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Direct BlueZ control for Insta360 X5 cameras")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--expected-cameras", type=int, default=1)
    parser.add_argument("--require-all", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--address", action="append", default=[])
    parser.add_argument("--camera-role", action="append", default=[])
    parser.add_argument("--name-prefix", action="append", default=[])
    parser.add_argument("--discovery-timeout-sec", type=float, default=15.0)
    parser.add_argument("--ack-timeout-sec", type=float, default=3.0)
    parser.add_argument("--simulate", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    role_by_address = {}
    for item in args.camera_role:
        address, separator, role = str(item).partition("=")
        if not separator or not address.strip() or not role.strip():
            raise ValueError(f"invalid --camera-role value: {item!r}")
        role_by_address[address.strip().upper()] = role.strip()
    if args.simulate:
        ordered_roles = [
            role_by_address.get(f"SIM:{index + 1:02d}", role)
            for index, role in enumerate(["insta_right", "insta_left"])
        ]
        return run_simulated(args.expected_cameras, ordered_roles)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    runtime = DirectRuntime(
        bus=bus,
        adapter_name=args.adapter,
        expected_cameras=args.expected_cameras,
        require_all=args.require_all,
        addresses=args.address,
        role_by_address=role_by_address,
        name_prefixes=args.name_prefix or ["X5 "],
        discovery_timeout_sec=args.discovery_timeout_sec,
        ack_timeout_sec=args.ack_timeout_sec,
    )
    loop = GLib.MainLoop()
    runtime.loop = loop

    try:
        runtime.initialize()
    except Exception as exc:
        emit({"ok": False, "action": "ready", "message": f"BlueZ initialization failed: {exc}"})
        return 2

    emit({"ok": True, "action": "ready", "message": "direct BLE worker scanning for X5 cameras", **runtime.snapshot()})

    def publish_health() -> bool:
        try:
            runtime._drop_disconnected()
            payload = {"ok": True, "action": "telemetry", "message": "camera telemetry", **runtime.snapshot()}
        except Exception as exc:
            with runtime.lock:
                for camera in runtime.cameras.values():
                    camera.connected = False
                    camera.notifying = False
            payload = {
                "ok": False,
                "action": "telemetry",
                "message": f"camera health check failed: {type(exc).__name__}: {exc}",
                **runtime.snapshot(),
            }
        emit(payload)
        return True

    GLib.timeout_add_seconds(1, publish_health)

    def command_reader() -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            action, _, argument = line.partition(" ")
            action = action.upper()
            try:
                if action == "SESSION":
                    runtime.event_log.select(json.loads(argument))
                    emit({"ok": True, "action": "session", "message": "session log selected", **runtime.snapshot()})
                elif action == "CONNECT":
                    emit(runtime.connect_expected())
                elif action == "SCAN":
                    try:
                        scan_timeout = float(argument) if argument else 5.0
                    except ValueError:
                        scan_timeout = 5.0
                    emit(runtime.scan(scan_timeout))
                elif action == "START":
                    emit(runtime.dispatch("start", PHONE_COMMAND_START_CAPTURE, b"\x08\x01"))
                elif action == "STOP":
                    emit(runtime.dispatch("stop", PHONE_COMMAND_STOP_CAPTURE))
                elif action == "STATUS":
                    result = runtime.dispatch("status", PHONE_COMMAND_GET_CURRENT_CAPTURE_STATUS)
                    emit(result)
                elif action == "QUIT":
                    if runtime.snapshot().get("recording_expected"):
                        emit(runtime.dispatch("stop", PHONE_COMMAND_STOP_CAPTURE))
                    runtime.disconnect()
                    emit({"ok": True, "action": "quit", "message": "direct BLE worker stopped", **runtime.snapshot()})
                    GLib.idle_add(loop.quit)
                    return
                else:
                    emit({"ok": False, "action": action.lower(), "message": f"unknown command: {action}", **runtime.snapshot()})
            except Exception as exc:
                emit({"ok": False, "action": action.lower(), "message": str(exc), **runtime.snapshot()})
        GLib.idle_add(loop.quit)

    threading.Thread(target=command_reader, name="InstaBLEDirectCommands", daemon=True).start()

    def stop_signal(_signum: int, _frame: Any) -> None:
        if runtime.snapshot().get("recording_expected"):
            try:
                runtime.dispatch("stop", PHONE_COMMAND_STOP_CAPTURE)
            except Exception:
                pass
        runtime.disconnect()
        loop.quit()

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
