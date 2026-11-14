#!/usr/bin/env python3
"""Compare BLE control latency with actual Insta360 media frame-0 times."""

from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from pathlib import Path
from typing import Any


INSTAUMI_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(INSTAUMI_DIR))

from time_sync_utils import load_sidecar_frame0  # noqa: E402


def parse_named_path(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("use LABEL=/path/to/file.sync.json")
    return label.strip(), Path(raw_path).expanduser().resolve()


def load_ble_log(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    commands: list[dict[str, Any]] = []
    result_index: dict[tuple[int, str], dict[str, Any]] = {}
    command_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.get("event") != "ble_command_result":
            continue
        camera_results = []
        for camera in row.get("camera_results", []):
            camera_results.append(
                {
                    "address": camera.get("address"),
                    "dispatch_unix_ns": camera.get("dispatch_unix_ns"),
                    "local_write_ms": camera.get("local_write_ms"),
                    "ack_unix_ns": camera.get("ack_unix_ns"),
                    "ack_rtt_ms": camera.get("ack_rtt_ms"),
                    "response_code": camera.get("response_code"),
                }
            )
        command = {
            "action": row.get("action"),
            "sequence": row.get("sequence"),
            "host_dispatch_skew_ms": row.get("host_dispatch_skew_ms"),
            "camera_ack_skew_ms": row.get("camera_ack_skew_ms"),
            "state_verified": row.get("state_verified"),
            "camera_results": camera_results,
        }
        commands.append(command)
        if command["sequence"] is not None:
            sequence = int(command["sequence"])
            command_index[sequence] = command
            for camera in camera_results:
                result_index[(sequence, str(camera.get("address")))] = camera

    # A STOP response may arrive after the live worker's bounded ACK wait while
    # the camera finalizes its file.  Recover that late, sequence-matched 200 OK
    # from the raw notification records instead of losing the real RTT.
    for row in rows:
        if row.get("event") != "ble_notification" or row.get("response_code") != 200:
            continue
        sequence = row.get("sequence")
        address = str(row.get("address"))
        if sequence is None:
            continue
        camera = result_index.get((int(sequence), address))
        if camera is None or camera.get("ack_unix_ns") is not None:
            continue
        ack_unix_ns = row.get("unix_time_ns")
        dispatch_unix_ns = camera.get("dispatch_unix_ns")
        camera["ack_unix_ns"] = ack_unix_ns
        camera["response_code"] = 200
        if ack_unix_ns is not None and dispatch_unix_ns is not None:
            camera["ack_rtt_ms"] = (int(ack_unix_ns) - int(dispatch_unix_ns)) / 1_000_000.0

    for sequence, command in command_index.items():
        results = command["camera_results"]
        command["state_verified"] = bool(results) and all(item.get("response_code") == 200 for item in results)
        ack_times = [int(item["ack_unix_ns"]) for item in results if item.get("ack_unix_ns") is not None]
        if len(ack_times) > 1:
            command["camera_ack_skew_ms"] = (max(ack_times) - min(ack_times)) / 1_000_000.0
    return {
        "path": str(path),
        "commands": commands,
        "note": "BLE dispatch/ACK measures control latency; it is not the media frame alignment result.",
    }


def load_cameras(values: list[tuple[str, Path]]) -> dict[str, dict[str, Any]]:
    cameras: dict[str, dict[str, Any]] = {}
    for label, path in values:
        result = load_sidecar_frame0(path)
        if result is None:
            cameras[label] = {
                "ok": False,
                "sidecar": str(path),
                "error": "no usable absolute frame-0 time in sidecar",
            }
        else:
            cameras[label] = {"ok": True, "sidecar": str(path), **result}
    return cameras


def compare_cameras(
    cameras: dict[str, dict[str, Any]],
    fps: float,
    target_ms: float,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    usable = [(label, data) for label, data in cameras.items() if data.get("ok")]
    for (left_label, left), (right_label, right) in combinations(usable, 2):
        offset_ms = float(right["frame0_unix_ms"]) - float(left["frame0_unix_ms"])
        frame_ms = 1000.0 / fps
        pairs.append(
            {
                "left": left_label,
                "right": right_label,
                "right_minus_left_ms": offset_ms,
                "absolute_offset_ms": abs(offset_ms),
                "offset_frames": offset_ms / frame_ms,
                "fps": fps,
                "within_half_frame": abs(offset_ms) <= frame_ms / 2.0,
                "within_target_ms": abs(offset_ms) <= target_ms,
                "target_ms": target_ms,
                "interpretation": (
                    f"{right_label} starts {abs(offset_ms):.3f} ms "
                    + ("later" if offset_ms >= 0 else "earlier")
                    + f" than {left_label}"
                ),
            }
        )
    return pairs


def align_cameras_to_ubuntu(
    cameras: dict[str, dict[str, Any]],
    ble_control: dict[str, Any] | None,
) -> dict[str, Any]:
    """Describe the UTC-to-video mapping and control-to-first-frame delay."""

    start_command = None
    if ble_control:
        start_command = next(
            (item for item in ble_control.get("commands", []) if item.get("action") == "start"),
            None,
        )
    control_results = list(start_command.get("camera_results", [])) if start_command else []
    aligned: dict[str, Any] = {}
    for index, (label, camera) in enumerate(cameras.items()):
        if not camera.get("ok"):
            aligned[label] = {"ok": False, "error": camera.get("error", "frame-0 unavailable")}
            continue
        frame0_ms = float(camera["frame0_unix_ms"])
        item: dict[str, Any] = {
            "ok": True,
            "frame0_unix_ms": frame0_ms,
            "frame0_source": camera.get("source"),
            "frame0_confidence": camera.get("confidence"),
            "ubuntu_event_to_video_time_formula": "video_time_ms = ubuntu_event_unix_ms - frame0_unix_ms",
        }
        if index < len(control_results):
            control = control_results[index]
            dispatch_ns = control.get("dispatch_unix_ns")
            ack_ns = control.get("ack_unix_ns")
            item["ble_address"] = control.get("address")
            item["start_dispatch_unix_ns"] = dispatch_ns
            item["start_ack_unix_ns"] = ack_ns
            if dispatch_ns is not None:
                item["frame0_minus_start_dispatch_ms"] = frame0_ms - int(dispatch_ns) / 1_000_000.0
            if ack_ns is not None:
                item["frame0_minus_start_ack_ms"] = frame0_ms - int(ack_ns) / 1_000_000.0
        aligned[label] = item
    return {
        "cameras": aligned,
        "note": (
            "frame0_minus_start_dispatch_ms is camera startup delay, not clock error. "
            "The event-to-video formula is valid only when frame0_source is an absolute, synchronized clock; "
            "validate sub-10 ms alignment with X5 Timecode or a shared flash/audio event."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate actual dual-X5 frame-0 offset from MediaSDK *.sync.json files, "
            "and report BLE command latency separately."
        )
    )
    parser.add_argument("--camera", action="append", type=parse_named_path, default=[], metavar="LABEL=SYNC_JSON")
    parser.add_argument("--ble-log", type=Path, help="session insta_ble_events.jsonl")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--target-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fps <= 0 or args.target_ms <= 0:
        raise SystemExit("--fps and --target-ms must be positive")
    if not args.camera and args.ble_log is None:
        raise SystemExit("provide at least one --camera or --ble-log")
    cameras = load_cameras(args.camera)
    ble_control = load_ble_log(args.ble_log.resolve() if args.ble_log else None)
    pairwise_offsets = compare_cameras(cameras, args.fps, args.target_ms)
    report = {
        "schema": "instaumi-dual-camera-sync-v1",
        "target_alignment_ms": args.target_ms,
        "frame0_source_priority": [
            "firstGpsTimestampMs",
            "absolute firstFrameTimestampMs",
            "createUnixTimeMs + relative firstFrameTimestampMs",
            "createUnixTimeMs (low precision)",
        ],
        "cameras": cameras,
        "pairwise_media_offsets": pairwise_offsets,
        "all_camera_pairs_within_target": bool(pairwise_offsets)
        and all(item["within_target_ms"] for item in pairwise_offsets),
        "ubuntu_media_alignment": align_cameras_to_ubuntu(cameras, ble_control),
        "ble_control": ble_control,
        "accuracy_note": (
            "Use pairwise_media_offsets as the post-capture alignment result. "
            "For guaranteed production sync, enable X5 Timecode or record a shared flash/audio sync event; "
            "BLE ACK timing alone cannot prove sensor-level simultaneity."
        ),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    return 0 if all(data.get("ok") for data in cameras.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
