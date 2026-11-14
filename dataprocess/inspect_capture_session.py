#!/usr/bin/env python3
"""Audit a datacap session before transferring media into dataprocess."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from capture_schema import (
    CAMERA_ROLE_BY_ADDRESS,
    camera_role_directories,
    field,
    find_timestamp_log,
    load_jsonl,
    media_files,
    normalize_camera_role,
    normalize_tracker_role,
)
from uvc_capture_source import (
    UvcCaptureValidationError,
    detect_uvc_capture,
    validate_uvc_capture,
)


CORE_TRACKER_COLUMNS = {
    "timestamp",
    "role",
    "pos_x",
    "pos_y",
    "pos_z",
    "ori_x",
    "ori_y",
    "ori_z",
    "ori_w",
    "mac",
}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def event_time_ns(row: dict[str, Any]) -> int | None:
    value = field(row, "unix_time_ns", "unixTimeNs")
    if value is not None:
        return int(value)
    value_ms = field(row, "unix_time_ms", "unixTimeMs")
    return None if value_ms is None else int(float(value_ms) * 1_000_000.0)


def audit_timestamps(path: Path | None) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if path is None:
        return {"present": False}, ["missing timestamp_log.jsonl"], warnings
    try:
        rows = load_jsonl(path)
    except ValueError as exc:
        return {"present": True, "path": str(path)}, [str(exc)], warnings

    counts = Counter(str(row.get("type") or "unknown") for row in rows)
    starts: dict[int, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    for row in rows:
        event_type = row.get("type")
        index = field(row, "segment_index", "segmentIndex")
        if index is None:
            continue
        index = int(index)
        if event_type == "segment_start":
            starts[index] = row
        elif event_type == "segment_end" and index in starts:
            start = starts.pop(index)
            start_ns = event_time_ns(start)
            end_ns = event_time_ns(row)
            if start_ns is not None and end_ns is not None and end_ns > start_ns:
                completed.append({"segment_index": index, "duration_sec": (end_ns - start_ns) / 1e9})

    if counts["total_start"] != 1:
        errors.append(f"expected one total_start, found {counts['total_start']}")
    if counts["total_end"] != 1:
        errors.append(f"expected one total_end, found {counts['total_end']}")
    if counts["slam_timestamp"] != 2:
        warnings.append(f"expected two slam_timestamp events, found {counts['slam_timestamp']}")
    if starts:
        errors.append(f"segments missing end: {sorted(starts)}")
    if not completed:
        warnings.append("no complete segment_start/segment_end pairs")

    non_english_keys = sorted(
        {
            str(key)
            for row in rows
            for key in row
            if not str(key).isascii()
        }
    )
    if non_english_keys:
        warnings.append(f"non-English timestamp keys: {non_english_keys}")

    return (
        {
            "present": True,
            "path": str(path),
            "event_count": len(rows),
            "event_type_counts": dict(sorted(counts.items())),
            "complete_segments": completed,
            "effective_capture_duration_sec": sum(item["duration_sec"] for item in completed),
            "non_english_keys": non_english_keys,
        },
        errors,
        warnings,
    )


def audit_tracker(
    path: Path,
    expected_roles: tuple[str, ...] = ("htc_right", "htc_left"),
) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return {"present": False, "path": str(path)}, [f"missing {path.name}"], warnings

    role_times: dict[str, list[float]] = defaultdict(list)
    role_status: dict[str, Counter[int]] = defaultdict(Counter)
    role_status_raw: dict[str, Counter[int]] = defaultdict(Counter)
    unknown_rows = 0
    row_count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        columns = set(reader.fieldnames or [])
        missing = sorted(CORE_TRACKER_COLUMNS - columns)
        if missing:
            errors.append(f"{path.name} missing columns: {missing}")
        for row in reader:
            row_count += 1
            role = normalize_tracker_role(row.get("role"), row.get("mac"))
            if role is None:
                unknown_rows += 1
                continue
            try:
                timestamp = float(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            role_times[role].append(timestamp)
            try:
                raw_status = int(row.get("tracking_status_raw") or row.get("tracking_status") or 0)
                normalized_status = int(row.get("tracking_status") or (raw_status & 0x0F))
                role_status[role][normalized_status] += 1
                role_status_raw[role][raw_status] += 1
            except ValueError:
                pass

    role_summary: dict[str, Any] = {}
    for role in ("htc_right", "htc_left"):
        values = role_times.get(role, [])
        deltas = [b - a for a, b in zip(values, values[1:]) if b > a]
        role_summary[role] = {
            "rows": len(values),
            "first_unix_time_sec": values[0] if values else None,
            "last_unix_time_sec": values[-1] if values else None,
            "duration_sec": values[-1] - values[0] if len(values) >= 2 else None,
            "sample_rate_hz_median": None if not deltas else 1.0 / statistics.median(deltas),
            "sample_gap_ms_p95": None if not deltas else percentile(deltas, 0.95) * 1000.0,
            "sample_gap_ms_max": None if not deltas else max(deltas) * 1000.0,
            "tracking_status_counts": dict(sorted(role_status[role].items())),
            "tracking_status_raw_counts": dict(sorted(role_status_raw[role].items())),
        }
        if role in expected_roles and not values:
            errors.append(f"missing tracker rows for {role}")
        elif any(b < a for a, b in zip(values, values[1:])):
            errors.append(f"non-monotonic tracker timestamps for {role}")
    if unknown_rows:
        warnings.append(f"tracker rows with unknown physical role: {unknown_rows}")

    return {
        "present": True,
        "path": str(path),
        "rows": row_count,
        "roles": role_summary,
    }, errors, warnings


def audit_capture_trackers(
    session_dir: Path,
    expected_roles: tuple[str, ...] = ("htc_right", "htc_left"),
) -> tuple[dict[str, Any], list[str], list[str]]:
    role_paths = {
        "htc_right": session_dir / "htc_right.csv",
        "htc_left": session_dir / "htc_left.csv",
    }
    if not any(path.is_file() for path in role_paths.values()):
        return audit_tracker(session_dir / "tracker_data.csv", expected_roles)

    reports: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for role in expected_roles:
        path = role_paths[role]
        report, role_errors, role_warnings = audit_tracker(path, (role,))
        reports[role] = report
        errors.extend(role_errors)
        warnings.extend(role_warnings)
    return {
        "present": all(report["present"] for report in reports.values()),
        "paths": {role: str(role_paths[role]) for role in expected_roles},
        "rows": sum(int(report.get("rows", 0)) for report in reports.values()),
        "roles": {
            role: reports[role].get("roles", {}).get(role, {})
            for role in expected_roles
        },
    }, errors, warnings


def camera_role_for_event(row: dict[str, Any]) -> str | None:
    return normalize_camera_role(row.get("role"), row.get("address"))


def audit_ble(path: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return {"present": False, "path": str(path)}, errors, ["missing insta_ble_events.jsonl"]
    try:
        rows = load_jsonl(path)
    except ValueError as exc:
        return {"present": True, "path": str(path)}, [str(exc)], warnings

    actions: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    sequence_actions: dict[tuple[str, int], str] = {}
    for row in rows:
        action = str(row.get("action") or "")
        role = camera_role_for_event(row)
        sequence = row.get("sequence")
        if row.get("event") == "ble_command_dispatched" and role is not None and sequence is not None:
            sequence_actions[(role, int(sequence))] = action
        if action not in {"start", "stop"} and role is not None and sequence is not None:
            action = sequence_actions.get((role, int(sequence)), "")
        if action not in {"start", "stop"} or role is None:
            continue
        event = row.get("event")
        if event == "ble_command_dispatched" and row.get("dispatch_unix_ns") is not None:
            actions[action][role]["dispatch_unix_ns"] = int(row["dispatch_unix_ns"])
        elif event == "ble_write_complete" and row.get("complete_unix_ns") is not None:
            actions[action][role]["write_complete_unix_ns"] = int(row["complete_unix_ns"])
        elif event == "ble_notification" and sequence is not None and row.get("response_code") is not None:
            actions[action][role].setdefault("ack_unix_ns", int(row["unix_time_ns"]))

    action_summary: dict[str, Any] = {}
    for action in ("start", "stop"):
        cameras = actions.get(action, {})
        metrics: dict[str, Any] = {}
        for key in ("dispatch_unix_ns", "write_complete_unix_ns", "ack_unix_ns"):
            values = [entry[key] for entry in cameras.values() if key in entry]
            metrics[key.replace("_unix_ns", "_skew_ms")] = (
                None if len(values) < 2 else (max(values) - min(values)) / 1e6
            )
        per_camera = {}
        for role, entry in sorted(cameras.items()):
            dispatch = entry.get("dispatch_unix_ns")
            complete = entry.get("write_complete_unix_ns")
            ack = entry.get("ack_unix_ns")
            per_camera[role] = {
                **entry,
                "write_latency_ms": None if dispatch is None or complete is None else (complete - dispatch) / 1e6,
                "ack_rtt_ms": None if dispatch is None or ack is None else (ack - dispatch) / 1e6,
            }
        action_summary[action] = {**metrics, "cameras": per_camera}
        if action == "start" and metrics.get("dispatch_skew_ms") is not None and metrics["dispatch_skew_ms"] >= 10.0:
            errors.append(f"start dispatch skew is {metrics['dispatch_skew_ms']:.3f} ms, above 10 ms")
        if len(cameras) < 2:
            warnings.append(f"BLE {action} timing is available for only {len(cameras)}/2 cameras")

    return {
        "present": True,
        "path": str(path),
        "event_count": len(rows),
        "actions": action_summary,
        "interpretation": {
            "dispatch_skew": "Measured Linux host command scheduling difference between cameras.",
            "write_complete_skew": "Measured completion difference for local BLE writes.",
            "ack_skew": "Camera response timing; it is not the exact first-frame difference.",
        },
    }, errors, warnings


def audit_media(
    session_dir: Path,
    expected_roles: tuple[str, ...] = ("insta_right", "insta_left"),
) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    uvc_configured = (session_dir / "uvc_capture_manifest.json").exists()
    uvc_completed = (session_dir / "uvc_capture_complete.json").exists()
    if uvc_configured or uvc_completed:
        try:
            if not detect_uvc_capture(session_dir):
                raise UvcCaptureValidationError(
                    "USB capture has no uvc_capture_complete.json; treat it as incomplete"
                )
            source = validate_uvc_capture(session_dir)
        except (OSError, ValueError, UvcCaptureValidationError) as exc:
            errors.append(f"invalid or incomplete USB/UVC capture: {exc}")
            return (
                {
                    "source_family": "usb_uvc",
                    "capture_transport": "usb_uvc",
                    "postprocess_ready": False,
                    "completion_marker_present": uvc_completed,
                },
                errors,
                warnings,
            )
        actual_roles = tuple(source.roles)
        if actual_roles != expected_roles:
            errors.append(
                "USB/UVC roles differ from manifest capture profile: "
                f"expected={list(expected_roles)} actual={list(actual_roles)}"
            )
        roles = {
            role: {
                "directory": str(role_source.manifest_path.parent),
                "packet_stream": str(role_source.video_path),
                "packet_index": str(role_source.frames_path),
                "frame_count": len(role_source.records),
                "status": "ready",
            }
            for role, role_source in source.roles.items()
        }
        return (
            {
                "source_family": "usb_uvc",
                "capture_transport": "usb_uvc",
                "postprocess_ready": not errors,
                "completion_marker_present": True,
                "roles": roles,
                "normalized_source": dict(source.normalized_manifest),
            },
            errors,
            warnings,
        )

    role_dirs = camera_role_directories(session_dir)
    roles: dict[str, Any] = {}
    for role in expected_roles:
        directory = role_dirs[role]
        insv = media_files(directory, {".insv"})
        panorama = media_files(directory, {".mp4"})
        sidecars = media_files(directory, {".json"})
        roles[role] = {
            "directory": str(directory),
            "insv_files": [str(path) for path in insv],
            "panorama_files": [str(path) for path in panorama],
            "sidecar_files": [str(path) for path in sidecars],
            "status": "ready" if insv else "pending_transfer",
        }
        if not insv:
            warnings.append(f"{role} INSV files have not been transferred")
    all_insv_present = all(value["insv_files"] for value in roles.values())
    return (
        {
            "source_family": "camera_native_insv",
            "capture_transport": "bluez_direct",
            "roles": roles,
            "all_insv_present": all_insv_present,
            "postprocess_ready": all_insv_present,
        },
        errors,
        warnings,
    )


def capture_profile_roles(
    session_dir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        return ("insta_right", "insta_left"), ("htc_right", "htc_left")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = manifest.get("capture_profile", {})
        cameras = tuple(str(value) for value in profile.get("camera_roles", []))
        trackers = tuple(str(value) for value in profile.get("tracker_roles", []))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return ("insta_right", "insta_left"), ("htc_right", "htc_left")
    valid_cameras = tuple(
        role for role in cameras if role in {"insta_right", "insta_left"}
    )
    valid_trackers = tuple(
        role for role in trackers if role in {"htc_right", "htc_left"}
    )
    return (
        valid_cameras or ("insta_right", "insta_left"),
        valid_trackers or ("htc_right", "htc_left"),
    )


def build_report(session_dir: Path) -> dict[str, Any]:
    expected_cameras, expected_trackers = capture_profile_roles(session_dir)
    timestamp, timestamp_errors, timestamp_warnings = audit_timestamps(find_timestamp_log(session_dir))
    tracker, tracker_errors, tracker_warnings = audit_capture_trackers(
        session_dir, expected_trackers
    )
    ble, ble_errors, ble_warnings = audit_ble(session_dir / "insta_ble_events.jsonl")
    media, media_errors, media_warnings = audit_media(session_dir, expected_cameras)
    errors = timestamp_errors + tracker_errors + ble_errors + media_errors
    warnings = timestamp_warnings + tracker_warnings + ble_warnings + media_warnings

    total_start_ns = None
    if timestamp.get("present"):
        for row in load_jsonl(Path(timestamp["path"])):
            if row.get("type") == "total_start":
                total_start_ns = event_time_ns(row)
                break
    tracker_start_ns = None
    role_starts = [
        value.get("first_unix_time_sec")
        for value in tracker.get("roles", {}).values()
        if value.get("first_unix_time_sec") is not None
    ]
    if role_starts:
        tracker_start_ns = int(min(role_starts) * 1e9)

    postprocess_ready = not errors and bool(media.get("postprocess_ready"))
    if errors:
        status = "error"
    elif postprocess_ready:
        status = "ready"
    elif media.get("source_family") == "usb_uvc":
        status = "waiting_for_uvc_completion"
    else:
        status = "waiting_for_insv"
    usb_source = media.get("source_family") == "usb_uvc"
    return {
        "schema": "instaumi_capture_audit_v1",
        "session_dir": str(session_dir),
        "status": status,
        "core_capture_complete": not errors,
        "postprocess_ready": postprocess_ready,
        "roles": {
            "cameras": list(expected_cameras),
            "trackers": list(expected_trackers),
            "camera_role_by_address": CAMERA_ROLE_BY_ADDRESS,
        },
        "timestamps": timestamp,
        "tracker": tracker,
        "media": media,
        "sync": {
            "host_clock_domain": "linux_unix_and_monotonic",
            "htc_and_pedal_share_host_clock": True,
            "tracker_first_sample_after_total_start_ms": (
                None if total_start_ns is None or tracker_start_ns is None else (tracker_start_ns - total_start_ns) / 1e6
            ),
            "camera_command_timing": ble,
            "camera_frame_alignment_status": (
                "uvc_host_soe_timeline_validated_but_pair_skew_not_yet_admitted"
                if usb_source
                else "pending_insv_motion_calibration"
            ),
            "sub_10ms_frame_alignment_verified": False,
        },
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--require-media", action="store_true", help="Exit nonzero while either INSV role is missing.")
    args = parser.parse_args()
    session_dir = args.session_dir.expanduser().resolve()
    report = build_report(session_dir)
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    print(encoded)
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
    if report["errors"] or (args.require_media and not report["postprocess_ready"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
