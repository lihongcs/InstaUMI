#!/usr/bin/python3
"""BlueZ GATT peripheral emulating an Insta360 GPS Action Remote.

This process intentionally owns all Bluetooth/D-Bus work.  The datacap web
process only exchanges line commands with it, so a BlueZ failure cannot take
down the capture state machine.

The GATT layout and shutter payload are based on Patrick Chwalek's MIT-licensed
``insta360_ble_esp32`` reference implementation.  See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib


BLUEZ = "org.bluez"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
GATT_MANAGER = "org.bluez.GattManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
ADV_IFACE = "org.bluez.LEAdvertisement1"

APP_PATH = "/com/instaumi/datacap/instable"
ADV_PATH = APP_PATH + "/advertisement0"

BT_BASE = "0000{}-0000-1000-8000-00805f9b34fb"
REMOTE_SERVICE_UUID = BT_BASE.format("ce80")
REMOTE_STATUS_UUID = BT_BASE.format("ce81")
REMOTE_NOTIFY_UUID = BT_BASE.format("ce82")
REMOTE_VERSION_UUID = BT_BASE.format("ce83")
SECONDARY_SERVICE_UUID = "0000d0ff-3c17-d293-8e48-14fe2e4da212"

SHUTTER_PAYLOAD = bytes.fromhex("fc ef fe 86 00 03 01 02 00")
PROTOCOL_PREFIX = "DATACAP_BLE_JSON "


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class NotSupported(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any]) -> None:
    payload.setdefault("unix_time_ns", time.time_ns())
    payload.setdefault("monotonic_ns", time.monotonic_ns())
    print(PROTOCOL_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


class EventLog:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.lock = threading.Lock()

    def select(self, path: str) -> None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.write("session_log_selected", path=str(target))

    def write(self, event: str, **fields: Any) -> None:
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


class RuntimeState:
    def __init__(self, bus: dbus.SystemBus, adapter_path: str, expected_cameras: int, allow_unsubscribed: bool) -> None:
        self.bus = bus
        self.adapter_path = adapter_path
        self.expected_cameras = max(1, expected_cameras)
        self.allow_unsubscribed = allow_unsubscribed
        self.gatt_registered = False
        self.advertisement_registered = False
        self.notifying = False
        self.recording_expected = False
        self.known_devices: set[str] = set()
        self.last_status_hex = ""
        self.last_trigger_unix_time_ns: int | None = None
        self.event_log = EventLog()
        self.notify_characteristic: RemoteNotifyCharacteristic | None = None

    def note_access(self, options: dict[str, Any], operation: str, uuid: str, value: bytes | None = None) -> None:
        device = str(options.get("device", ""))
        if device:
            self.known_devices.add(device)
        self.event_log.write(
            "gatt_access",
            device=device or None,
            operation=operation,
            characteristic_uuid=uuid,
            value_hex=value.hex(" ") if value is not None else None,
        )

    def connected_devices(self) -> list[dict[str, Any]]:
        try:
            objects = dbus.Interface(self.bus.get_object(BLUEZ, "/"), DBUS_OM).GetManagedObjects()
        except Exception:
            return []
        devices: list[dict[str, Any]] = []
        for path, interfaces in objects.items():
            props = interfaces.get(DEVICE_IFACE)
            if not props or not bool(props.get("Connected", False)):
                continue
            path_text = str(path)
            if self.known_devices and path_text not in self.known_devices:
                continue
            devices.append(
                {
                    "path": path_text,
                    "address": str(props.get("Address", "")),
                    "name": str(props.get("Name", props.get("Alias", ""))),
                    "paired": bool(props.get("Paired", False)),
                }
            )
        return devices

    def snapshot(self) -> dict[str, Any]:
        devices = self.connected_devices()
        connected_estimate = len(devices)
        if self.notifying and connected_estimate == 0:
            connected_estimate = 1
        ready = self.gatt_registered and self.advertisement_registered
        return {
            "backend": "bluez_gps_action_remote",
            "transport": "bluetooth_le",
            "adapter": self.adapter_path,
            "advertised_name": "Insta360 GPS Remote",
            "ready": ready,
            "connected": self.notifying or bool(devices),
            "notifying": self.notifying,
            "connected_cameras_estimate": connected_estimate,
            "expected_cameras": self.expected_cameras,
            "devices": devices,
            "recording_expected": self.recording_expected,
            "state_verified": False,
            "last_status_hex": self.last_status_hex,
            "last_trigger_unix_time_ns": self.last_trigger_unix_time_ns,
            "command_semantics": "shutter_toggle",
        }

    def can_trigger(self) -> tuple[bool, str]:
        snapshot = self.snapshot()
        if not snapshot["ready"]:
            return False, "BlueZ GATT service is not ready"
        if self.allow_unsubscribed:
            return True, "unsubscribed trigger allowed by configuration"
        if not self.notifying:
            return False, "no Insta360 camera has subscribed to the BLE shutter characteristic"
        count = int(snapshot["connected_cameras_estimate"])
        if count < self.expected_cameras:
            return False, f"only {count}/{self.expected_cameras} expected cameras are connected"
        return True, "camera subscription ready"


class Application(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, state: RuntimeState) -> None:
        self.path = APP_PATH
        self.services: list[Service] = []
        super().__init__(bus, self.path)
        self.add_service(RemoteService(bus, 0, state))
        self.add_service(SecondaryService(bus, 1, state))

    def add_service(self, service: "Service") -> None:
        self.services.append(service)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self) -> dict[dbus.ObjectPath, dict[str, dict[str, Any]]]:
        objects: dict[dbus.ObjectPath, dict[str, dict[str, Any]]] = {}
        for service in self.services:
            objects[service.get_path()] = service.get_properties()
            for characteristic in service.characteristics:
                objects[characteristic.get_path()] = characteristic.get_properties()
        return objects


class Service(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str) -> None:
        self.path = f"{APP_PATH}/service{index}"
        self.uuid = uuid
        self.characteristics: list[Characteristic] = []
        super().__init__(bus, self.path)

    def add(self, characteristic: "Characteristic") -> None:
        self.characteristics.append(characteristic)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def get_properties(self) -> dict[str, dict[str, Any]]:
        return {
            GATT_SERVICE: {
                "UUID": dbus.String(self.uuid),
                "Primary": dbus.Boolean(True),
                "Characteristics": dbus.Array([item.get_path() for item in self.characteristics], signature="o"),
            }
        }

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, Any]:
        if interface != GATT_SERVICE:
            raise InvalidArgs()
        return self.get_properties()[GATT_SERVICE]


class Characteristic(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str, flags: list[str], service: Service, state: RuntimeState) -> None:
        self.path = f"{service.path}/char{index}"
        self.uuid = uuid
        self.flags = flags
        self.service = service
        self.state = state
        super().__init__(bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def get_properties(self) -> dict[str, dict[str, Any]]:
        return {
            GATT_CHRC: {
                "Service": self.service.get_path(),
                "UUID": dbus.String(self.uuid),
                "Flags": dbus.Array(self.flags, signature="s"),
                "Descriptors": dbus.Array([], signature="o"),
            }
        }

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, Any]:
        if interface != GATT_CHRC:
            raise InvalidArgs()
        return self.get_properties()[GATT_CHRC]

    @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options: dict[str, Any]) -> dbus.Array:
        raise NotSupported()

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
    def WriteValue(self, value: dbus.Array, options: dict[str, Any]) -> None:
        raise NotSupported()

    @dbus.service.method(GATT_CHRC)
    def StartNotify(self) -> None:
        raise NotSupported()

    @dbus.service.method(GATT_CHRC)
    def StopNotify(self) -> None:
        raise NotSupported()

    @dbus.service.signal(DBUS_PROP, signature="sa{sv}as")
    def PropertiesChanged(self, interface: str, changed: dict[str, Any], invalidated: list[str]) -> None:
        pass


class StaticReadCharacteristic(Characteristic):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str, service: Service, state: RuntimeState, value: bytes = b"") -> None:
        super().__init__(bus, index, uuid, ["read"], service, state)
        self.value = value

    def ReadValue(self, options: dict[str, Any]) -> dbus.Array:
        self.state.note_access(options, "read", self.uuid)
        offset = int(options.get("offset", 0))
        return dbus.Array([dbus.Byte(byte) for byte in self.value[offset:]], signature="y")


class StorageWriteCharacteristic(Characteristic):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str, service: Service, state: RuntimeState, status_channel: bool = False) -> None:
        super().__init__(bus, index, uuid, ["write"], service, state)
        self.value = b""
        self.status_channel = status_channel

    def WriteValue(self, value: dbus.Array, options: dict[str, Any]) -> None:
        self.value = bytes(int(byte) for byte in value)
        self.state.note_access(options, "write", self.uuid, self.value)
        if self.status_channel:
            self.state.last_status_hex = self.value.hex(" ")
            self.state.event_log.write("camera_status", value_hex=self.state.last_status_hex)
            emit({"action": "state", "ok": True, **self.state.snapshot()})


class RemoteNotifyCharacteristic(Characteristic):
    def __init__(self, bus: dbus.SystemBus, index: int, service: Service, state: RuntimeState) -> None:
        super().__init__(bus, index, REMOTE_NOTIFY_UUID, ["notify"], service, state)
        self.value = b"\x00"

    def StartNotify(self) -> None:
        self.state.notifying = True
        self.state.event_log.write("camera_subscribed")
        emit({"action": "state", "ok": True, "message": "camera subscribed", **self.state.snapshot()})

    def StopNotify(self) -> None:
        self.state.notifying = False
        self.state.event_log.write("camera_unsubscribed")
        emit({"action": "state", "ok": True, "message": "camera unsubscribed", **self.state.snapshot()})

    def send_shutter(self) -> None:
        self.value = SHUTTER_PAYLOAD
        self.PropertiesChanged(
            GATT_CHRC,
            {"Value": dbus.Array([dbus.Byte(byte) for byte in self.value], signature="y")},
            [],
        )


class RemoteService(Service):
    def __init__(self, bus: dbus.SystemBus, index: int, state: RuntimeState) -> None:
        super().__init__(bus, index, REMOTE_SERVICE_UUID)
        self.add(StorageWriteCharacteristic(bus, 0, REMOTE_STATUS_UUID, self, state, status_channel=True))
        notify = RemoteNotifyCharacteristic(bus, 1, self, state)
        self.add(notify)
        self.add(StaticReadCharacteristic(bus, 2, REMOTE_VERSION_UUID, self, state, bytes.fromhex("01 02")))
        state.notify_characteristic = notify


class SecondaryService(Service):
    def __init__(self, bus: dbus.SystemBus, index: int, state: RuntimeState) -> None:
        super().__init__(bus, index, SECONDARY_SERVICE_UUID)
        definitions = [
            ("ffd1", "write", b""),
            ("ffd2", "read", b""),
            ("ffd3", "read", bytes.fromhex("01 90 1e 30")),
            ("ffd4", "read", bytes.fromhex("01 20 00 18")),
            ("ffd5", "read", b""),
            ("ffd8", "write", b""),
            ("fff1", "read", b""),
            ("fff2", "write", b""),
            ("ffe0", "read", b""),
        ]
        for char_index, (short_uuid, access, value) in enumerate(definitions):
            uuid = BT_BASE.format(short_uuid)
            if access == "read":
                self.add(StaticReadCharacteristic(bus, char_index, uuid, self, state, value))
            else:
                self.add(StorageWriteCharacteristic(bus, char_index, uuid, self, state))


class Advertisement(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus) -> None:
        self.path = ADV_PATH
        super().__init__(bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def get_properties(self) -> dict[str, dict[str, Any]]:
        return {
            ADV_IFACE: {
                "Type": dbus.String("peripheral"),
                "ServiceUUIDs": dbus.Array([REMOTE_SERVICE_UUID, SECONDARY_SERVICE_UUID], signature="s"),
                "LocalName": dbus.String("Insta360 GPS Remote"),
                "Discoverable": dbus.Boolean(True),
                "DiscoverableTimeout": dbus.UInt16(0),
            }
        }

    @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, Any]:
        if interface != ADV_IFACE:
            raise InvalidArgs()
        return self.get_properties()[ADV_IFACE]

    @dbus.service.method(ADV_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        print("[insta-ble] advertisement released by BlueZ", flush=True)


def find_adapter(bus: dbus.SystemBus, requested: str) -> str | None:
    objects = dbus.Interface(bus.get_object(BLUEZ, "/"), DBUS_OM).GetManagedObjects()
    requested_path = requested if requested.startswith("/") else f"/org/bluez/{requested}"
    interfaces = objects.get(dbus.ObjectPath(requested_path), objects.get(requested_path, {}))
    if GATT_MANAGER in interfaces and ADV_MANAGER in interfaces:
        return requested_path
    return None


def handle_capture_command(state: RuntimeState, action: str) -> None:
    desired = action == "start"
    if state.recording_expected == desired:
        emit(
            {
                "action": action,
                "ok": True,
                "message": f"camera already expected {'recording' if desired else 'stopped'}; no toggle sent",
                "idempotent": True,
                **state.snapshot(),
            }
        )
        return
    allowed, reason = state.can_trigger()
    if not allowed or state.notify_characteristic is None:
        emit({"action": action, "ok": False, "message": reason, **state.snapshot()})
        return
    requested_ns = time.time_ns()
    state.event_log.write(
        "shutter_toggle_requested",
        requested_action=action,
        payload_hex=SHUTTER_PAYLOAD.hex(" "),
        state_verified=False,
    )
    state.notify_characteristic.send_shutter()
    state.recording_expected = desired
    state.last_trigger_unix_time_ns = time.time_ns()
    state.event_log.write(
        "shutter_toggle_emitted",
        requested_action=action,
        requested_unix_time_ns=requested_ns,
        completed_unix_time_ns=state.last_trigger_unix_time_ns,
        state_verified=False,
    )
    emit(
        {
            "action": action,
            "ok": True,
            "message": f"BLE shutter toggle sent for requested {action}; camera recording state is unverified",
            "requested_unix_time_ns": requested_ns,
            "completed_unix_time_ns": state.last_trigger_unix_time_ns,
            **state.snapshot(),
        }
    )


def run_simulated(args: argparse.Namespace) -> int:
    recording = False
    log = EventLog()

    def snapshot() -> dict[str, Any]:
        return {
            "backend": "bluez_gps_action_remote_simulated",
            "transport": "simulated_bluetooth_le",
            "ready": True,
            "connected": True,
            "notifying": True,
            "connected_cameras_estimate": args.expected_cameras,
            "expected_cameras": args.expected_cameras,
            "recording_expected": recording,
            "state_verified": False,
            "command_semantics": "simulated_shutter_toggle",
            "simulated": True,
        }

    emit({"action": "ready", "ok": True, "message": "simulated BlueZ remote ready", **snapshot()})
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("SESSION "):
            try:
                log.select(str(json.loads(line[8:])))
                emit({"action": "session", "ok": True, "message": "BLE event log selected", **snapshot()})
            except Exception as exc:
                emit({"action": "session", "ok": False, "message": str(exc), **snapshot()})
        elif line == "START":
            recording = True
            log.write("simulated_start")
            emit({"action": "start", "ok": True, "message": "simulated BLE start", **snapshot()})
        elif line == "STOP":
            recording = False
            log.write("simulated_stop")
            emit({"action": "stop", "ok": True, "message": "simulated BLE stop", **snapshot()})
        elif line == "STATUS":
            emit({"action": "status", "ok": True, **snapshot()})
        elif line == "QUIT":
            emit({"action": "quit", "ok": True, "message": "simulated BLE worker stopped", **snapshot()})
            return 0
        else:
            emit({"action": "error", "ok": False, "message": f"unknown command: {line}"})
    return 0


def run_bluez(args: argparse.Namespace) -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = find_adapter(bus, args.adapter)
    if adapter_path is None:
        emit({"action": "ready", "ok": False, "message": f"BlueZ adapter lacks GATT/advertising support: {args.adapter}"})
        return 2

    adapter_object = bus.get_object(BLUEZ, adapter_path)
    properties = dbus.Interface(adapter_object, DBUS_PROP)
    properties.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))

    state = RuntimeState(bus, adapter_path, args.expected_cameras, args.allow_unsubscribed)
    application = Application(bus, state)
    advertisement = Advertisement(bus)
    gatt_manager = dbus.Interface(adapter_object, GATT_MANAGER)
    adv_manager = dbus.Interface(adapter_object, ADV_MANAGER)
    loop = GLib.MainLoop()
    registration_failed = False

    def maybe_ready() -> None:
        if state.gatt_registered and state.advertisement_registered:
            state.event_log.write("bluez_remote_ready", adapter=adapter_path)
            emit({"action": "ready", "ok": True, "message": "BlueZ Insta360 GPS remote ready; pair/connect cameras from the camera menu", **state.snapshot()})

    def gatt_ok() -> None:
        state.gatt_registered = True
        print("[insta-ble] GATT application registered", flush=True)
        maybe_ready()

    def adv_ok() -> None:
        state.advertisement_registered = True
        print("[insta-ble] LE advertisement registered", flush=True)
        maybe_ready()

    def registration_error(component: str, error: Exception) -> None:
        nonlocal registration_failed
        registration_failed = True
        emit({"action": "ready", "ok": False, "message": f"failed to register BlueZ {component}: {error}"})
        GLib.idle_add(loop.quit)

    def handle_line(line: str) -> bool:
        line = line.strip()
        if not line:
            return False
        if line.startswith("SESSION "):
            try:
                state.event_log.select(str(json.loads(line[8:])))
                emit({"action": "session", "ok": True, "message": "BLE event log selected", **state.snapshot()})
            except Exception as exc:
                emit({"action": "session", "ok": False, "message": str(exc), **state.snapshot()})
        elif line == "START":
            handle_capture_command(state, "start")
        elif line == "STOP":
            handle_capture_command(state, "stop")
        elif line == "STATUS":
            emit({"action": "status", "ok": True, **state.snapshot()})
        elif line == "QUIT":
            emit({"action": "quit", "ok": True, "message": "BlueZ BLE worker stopping", **state.snapshot()})
            loop.quit()
        else:
            emit({"action": "error", "ok": False, "message": f"unknown command: {line}", **state.snapshot()})
        return False

    def stdin_reader() -> None:
        for raw in sys.stdin:
            GLib.idle_add(handle_line, raw)
        GLib.idle_add(loop.quit)

    def stop_signal(_signum: int, _frame: Any) -> None:
        GLib.idle_add(loop.quit)

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    threading.Thread(target=stdin_reader, name="InstaBLECommands", daemon=True).start()

    gatt_manager.RegisterApplication(
        application.get_path(),
        {},
        reply_handler=gatt_ok,
        error_handler=lambda error: registration_error("GATT application", error),
    )
    adv_manager.RegisterAdvertisement(
        advertisement.get_path(),
        {},
        reply_handler=adv_ok,
        error_handler=lambda error: registration_error("advertisement", error),
    )

    try:
        loop.run()
    finally:
        if state.advertisement_registered:
            try:
                adv_manager.UnregisterAdvertisement(advertisement.get_path())
            except Exception as exc:
                print(f"[insta-ble] advertisement cleanup: {exc}", flush=True)
        if state.gatt_registered:
            try:
                gatt_manager.UnregisterApplication(application.get_path())
            except Exception as exc:
                print(f"[insta-ble] GATT cleanup: {exc}", flush=True)
    return 2 if registration_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Insta360 GPS Action Remote emulator for BlueZ")
    parser.add_argument("--adapter", default="hci0", help="BlueZ adapter name or object path")
    parser.add_argument("--expected-cameras", type=int, default=1)
    parser.add_argument("--allow-unsubscribed", action="store_true", help="allow lab triggers before a camera subscribes")
    parser.add_argument("--simulate", action="store_true", help="test the worker protocol without BlueZ")
    args = parser.parse_args(argv)
    if args.simulate:
        return run_simulated(args)
    return run_bluez(args)


if __name__ == "__main__":
    raise SystemExit(main())
