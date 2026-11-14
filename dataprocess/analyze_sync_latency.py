#!/usr/bin/env python3
"""Summarize Linux capture timing without treating BLE ACK as frame sync."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

from capture_schema import field, find_timestamp_log, load_jsonl, normalize_tracker_role
from inspect_capture_session import audit_ble, audit_tracker, audit_timestamps, build_report, event_time_ns


def summarize_legacy_clock(events: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [
        event
        for event in events
        if event.get("type") == "windows_clock_sync"
        and field(event, "external_clock_offset_ms", "externalClockOffsetMs") is not None
    ]
    if not samples:
        return {
            "clock_source": "linux_host_unified",
            "clock_sync_samples": 0,
            "external_clock_offset_ms": 0.0,
            "note": "Pedal, timestamp log, and HTC use the same Linux host clock; no iPad/Windows conversion is applied.",
        }
    offsets = [float(field(row, "external_clock_offset_ms", "externalClockOffsetMs")) for row in samples]
    rtts = [
        float(value)
        for row in samples
        if (value := field(row, "external_round_trip_ms", "externalRoundTripMs")) is not None
    ]
    best = min(
        samples,
        key=lambda row: float(field(row, "external_round_trip_ms", "externalRoundTripMs", float("inf")) or float("inf")),
    )
    return {
        "clock_source": "legacy_ipad_windows_clock_sync",
        "clock_sync_samples": len(samples),
        "best_external_clock_offset_ms": float(field(best, "external_clock_offset_ms", "externalClockOffsetMs")),
        "best_round_trip_ms": field(best, "external_round_trip_ms", "externalRoundTripMs"),
        "median_external_clock_offset_ms": statistics.median(offsets),
        "median_round_trip_ms": statistics.median(rtts) if rtts else None,
    }


def summarize_frame_clock(events: list[dict[str, Any]], timestamps_csv: Path | None) -> dict[str, Any] | None:
    if timestamps_csv is None:
        return None
    with timestamps_csv.open("r", encoding="utf-8", newline="") as src:
        first = next(csv.DictReader(src), None)
    if first is None:
        return {"rows": 0, "path": str(timestamps_csv)}
    total_start = next((event for event in events if event.get("type") == "total_start"), None)
    frame0_unix_ms = first.get("unix_time_ms")
    total_start_ns = None if total_start is None else event_time_ns(total_start)
    return {
        "path": str(timestamps_csv),
        "frame0_unix_time_ms": None if not frame0_unix_ms else float(frame0_unix_ms),
        "frame0_minus_total_start_ms": (
            None
            if not frame0_unix_ms or total_start_ns is None
            else float(frame0_unix_ms) - total_start_ns / 1e6
        ),
        "frame_alignment_verified": False,
        "note": "This is a processing time reference. Verify actual frame zero with motion calibration before claiming sub-10ms camera alignment.",
    }


def standalone_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.timestamp_log is None:
        raise ValueError("provide --session-dir or --timestamp-log")
    timestamp_path = args.timestamp_log.expanduser().resolve()
    events = load_jsonl(timestamp_path)
    timestamp, timestamp_errors, timestamp_warnings = audit_timestamps(timestamp_path)
    tracker = None
    tracker_errors: list[str] = []
    tracker_warnings: list[str] = []
    if args.htc_csv is not None:
        htc_path = args.htc_csv.expanduser().resolve()
        role = normalize_tracker_role(args.htc_role or htc_path.stem)
        expected_roles = (role,) if role is not None else ("htc_right", "htc_left")
        tracker, tracker_errors, tracker_warnings = audit_tracker(htc_path, expected_roles)
        if role is not None:
            tracker = {**tracker, "selected_role": role, "roles": {role: tracker.get("roles", {}).get(role)}}
    ble = None
    ble_errors: list[str] = []
    ble_warnings: list[str] = []
    if args.ble_log is not None:
        ble, ble_errors, ble_warnings = audit_ble(args.ble_log.expanduser().resolve())
    return {
        "schema": "instaumi_sync_analysis_v2",
        "timestamp_log": str(timestamp_path),
        "clock": summarize_legacy_clock(events),
        "timestamps": timestamp,
        "tracker": tracker,
        "camera_command_timing": ble,
        "frame_clock": summarize_frame_clock(
            events,
            None if args.frame_timestamps is None else args.frame_timestamps.expanduser().resolve(),
        ),
        "sub_10ms_frame_alignment_verified": False,
        "errors": timestamp_errors + tracker_errors + ble_errors,
        "warnings": timestamp_warnings + tracker_warnings + ble_warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument("--timestamp-log", type=Path, default=None)
    parser.add_argument("--ble-log", type=Path, default=None)
    parser.add_argument("--htc-csv", type=Path, default=None)
    parser.add_argument("--htc-role", default=None)
    parser.add_argument("--frame-timestamps", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if args.session_dir is not None:
        session_dir = args.session_dir.expanduser().resolve()
        report = build_report(session_dir)
        events_path = find_timestamp_log(session_dir)
        events = [] if events_path is None else load_jsonl(events_path)
        result = {
            "schema": "instaumi_sync_analysis_v2",
            "session_dir": str(session_dir),
            "clock": summarize_legacy_clock(events),
            "tracker": report["tracker"],
            "segments": report["timestamps"].get("complete_segments", []),
            "effective_capture_duration_sec": report["timestamps"].get("effective_capture_duration_sec"),
            "camera_command_timing": report["sync"]["camera_command_timing"],
            "frame_clock": summarize_frame_clock(
                events,
                None if args.frame_timestamps is None else args.frame_timestamps.expanduser().resolve(),
            ),
            "camera_frame_alignment_status": report["sync"]["camera_frame_alignment_status"],
            "sub_10ms_frame_alignment_verified": report["sync"]["sub_10ms_frame_alignment_verified"],
            "errors": report["errors"],
            "warnings": report["warnings"],
        }
    else:
        result = standalone_report(args)

    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    print(encoded)
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
