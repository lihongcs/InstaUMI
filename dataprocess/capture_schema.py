#!/usr/bin/env python3
"""Shared capture-layout and role compatibility helpers for dataprocess."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


CAMERA_ROLE_BY_ADDRESS = {
    "F0:23:AE:41:0E:3A": "insta_right",
    "F0:23:AE:5C:55:F6": "insta_left",
}

TRACKER_ROLE_BY_MAC = {
    "23:30:ce:35:9c:ae": "htc_right",
    "23:31:ce:35:9c:ae": "htc_left",
}

CAMERA_ROLE_ALIASES = {
    "insta_right": "insta_right",
    "right": "insta_right",
    "camera_1": "insta_right",
    "insta_1": "insta_right",
    "insta_left": "insta_left",
    "left": "insta_left",
    "camera_2": "insta_left",
    "insta_2": "insta_left",
}

TRACKER_ROLE_ALIASES = {
    "htc_right": "htc_right",
    "right": "htc_right",
    "right_arm": "htc_right",
    "right_elbow": "htc_right",
    "tracker_1": "htc_right",
    "htc_left": "htc_left",
    "left": "htc_left",
    "left_arm": "htc_left",
    "left_elbow": "htc_left",
    "tracker_2": "htc_left",
}


def normalize_address(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_mac(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_camera_role(role: Any = None, address: Any = None) -> str | None:
    mapped = CAMERA_ROLE_BY_ADDRESS.get(normalize_address(address))
    if mapped:
        return mapped
    raw = str(role or "").strip().lower()
    return CAMERA_ROLE_ALIASES.get(raw)


def normalize_tracker_role(role: Any = None, mac: Any = None) -> str | None:
    # New capture files resolve the physical identity before writing ``role``.
    # The radio-slot-derived MAC can change after reconnecting, so a recognized
    # explicit role must win. MAC mapping is only a legacy fallback.
    raw = str(role or "").strip().lower()
    explicit = TRACKER_ROLE_ALIASES.get(raw)
    if explicit:
        return explicit
    mapped = TRACKER_ROLE_BY_MAC.get(normalize_mac(mac))
    if mapped:
        return mapped
    return None


def field(row: dict[str, Any], snake_name: str, camel_name: str | None = None, default: Any = None) -> Any:
    value = row.get(snake_name)
    if value is not None:
        return value
    if camel_name:
        value = row.get(camel_name)
        if value is not None:
            return value
    return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as src:
        for line_number, line in enumerate(src, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def find_timestamp_log(session_dir: Path) -> Path | None:
    canonical = session_dir / "timestamp_log.jsonl"
    if canonical.is_file():
        return canonical
    candidates = sorted(session_dir.glob("timestamp_log_*.jsonl"))
    return candidates[0] if candidates else None


def camera_role_directories(session_dir: Path) -> dict[str, Path]:
    insta_root = session_dir / "insta"
    candidates = {
        "insta_right": [insta_root / "insta_right", insta_root / "camera_1", session_dir / "insta_right"],
        "insta_left": [insta_root / "insta_left", insta_root / "camera_2", session_dir / "insta_left"],
    }
    result: dict[str, Path] = {}
    for role, paths in candidates.items():
        match = next((path for path in paths if path.is_dir()), None)
        result[role] = match or paths[0]
    return result


def media_files(directory: Path, suffixes: Iterable[str]) -> list[Path]:
    accepted = {suffix.lower() for suffix in suffixes}
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in accepted)
