from __future__ import annotations

import os
import re
import selectors
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


EV_KEY = 0x01
KEY_RELEASED = 0
KEY_PRESSED = 1
KEY_REPEATED = 2
INPUT_EVENT_STRUCT = "llHHI"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_STRUCT)


@dataclass
class InputDevice:
    event: str
    name: str
    handlers: str


@dataclass
class PedalEvent:
    monotonic_time: float
    key_code: int
    device: str
    kind: str = "short"
    duration_sec: float = 0.0


def list_input_devices() -> list[InputDevice]:
    devices: list[InputDevice] = []
    proc = Path("/proc/bus/input/devices")
    if not proc.exists():
        return devices

    current: dict[str, str] = {}
    for line in proc.read_text(errors="replace").splitlines():
        if not line.strip():
            devices.extend(_device_from_block(current))
            current = {}
            continue
        if line.startswith("N:"):
            match = re.search(r'Name="([^"]+)"', line)
            if match:
                current["name"] = match.group(1)
        elif line.startswith("H:"):
            current["handlers"] = line.split("=", 1)[-1].strip()
    devices.extend(_device_from_block(current))
    return devices


def _device_from_block(block: dict[str, str]) -> list[InputDevice]:
    handlers = block.get("handlers", "")
    name = block.get("name", "unknown")
    found = []
    for token in handlers.split():
        if token.startswith("event"):
            found.append(InputDevice(f"/dev/input/{token}", name, handlers))
    return found


def resolve_input_device(selector: str, name_contains: str = "") -> str | None:
    if selector and selector != "auto":
        return selector
    devices = list_input_devices()
    needle = name_contains.strip().lower()
    if needle:
        for device in devices:
            if needle in device.name.lower():
                return device.event
    for device in devices:
        name = device.name.lower()
        if "foot" in name or "pedal" in name:
            return device.event
    return None


class FootPedalReader:
    def __init__(
        self,
        device_path: str,
        on_press: Callable[[PedalEvent], None],
        key_code: int | None = None,
        long_press_sec: float = 2.0,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.device_path = device_path
        self.on_press = on_press
        self.key_code = key_code
        self.long_press_sec = max(0.1, long_press_sec)
        self.on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="FootPedalReader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        selector = selectors.DefaultSelector()
        pressed: dict[int, tuple[float, bool]] = {}
        try:
            fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            self._report_status(False, str(exc))
            selector.close()
            return
        try:
            self._report_status(True, "foot pedal connected", pressed=False, transition="connected")
            selector.register(fd, selectors.EVENT_READ)
            while not self._stop.is_set():
                events = selector.select(timeout=0.05)
                for _key, _mask in events:
                    try:
                        data = os.read(fd, INPUT_EVENT_SIZE * 32)
                    except BlockingIOError:
                        continue
                    self._process_input_data(data, pressed)
                self._fire_due_long_presses(pressed)
        finally:
            self._report_status(False, "foot pedal stopped")
            selector.close()
            os.close(fd)

    def _process_input_data(self, data: bytes, pressed: dict[int, tuple[float, bool]]) -> None:
        for offset in range(0, len(data) - INPUT_EVENT_SIZE + 1, INPUT_EVENT_SIZE):
            sec, usec, event_type, code, value = struct.unpack(
                INPUT_EVENT_STRUCT, data[offset : offset + INPUT_EVENT_SIZE]
            )
            if event_type != EV_KEY or (self.key_code is not None and code != self.key_code):
                continue
            event_time = float(sec) + float(usec) / 1_000_000.0
            if abs(time.monotonic() - event_time) > 3600.0:
                event_time = time.monotonic()
            if value == KEY_PRESSED:
                pressed.setdefault(code, (event_time, False))
                self._report_status(
                    True,
                    "foot pedal pressed",
                    pressed=True,
                    transition="pressed",
                    key_code=code,
                    event_monotonic_time=event_time,
                )
            elif value == KEY_RELEASED:
                started = pressed.pop(code, None)
                if started is None:
                    continue
                started_at, long_fired = started
                duration_sec = max(0.0, event_time - started_at)
                if not long_fired:
                    self.on_press(
                        PedalEvent(
                            monotonic_time=started_at,
                            key_code=code,
                            device=self.device_path,
                            kind="short",
                            duration_sec=duration_sec,
                        )
                    )
                self._report_status(
                    True,
                    "foot pedal released",
                    pressed=False,
                    transition="released",
                    kind="long" if long_fired else "short",
                    key_code=code,
                    duration_sec=duration_sec,
                    event_monotonic_time=event_time,
                )
            elif value == KEY_REPEATED:
                continue

    def _fire_due_long_presses(self, pressed: dict[int, tuple[float, bool]]) -> None:
        now = time.monotonic()
        for code, (started_at, long_fired) in list(pressed.items()):
            if long_fired or now - started_at < self.long_press_sec:
                continue
            pressed[code] = (started_at, True)
            self.on_press(
                PedalEvent(
                    monotonic_time=started_at,
                    key_code=code,
                    device=self.device_path,
                    kind="long",
                    duration_sec=now - started_at,
                )
            )
            self._report_status(
                True,
                "foot pedal long press triggered",
                pressed=True,
                transition="long_triggered",
                kind="long",
                key_code=code,
                duration_sec=now - started_at,
                event_monotonic_time=now,
            )

    def _report_status(self, connected: bool, message: str, **extra: Any) -> None:
        if self.on_status is not None:
            self.on_status(
                {
                    "connected": connected,
                    "device": self.device_path,
                    "message": message,
                    "transition_unix_time_ns": time.time_ns(),
                    **extra,
                }
            )
