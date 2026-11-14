#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from fractions import Fraction
from pathlib import Path


def ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration:stream_side_data",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    data = json.loads(proc.stdout)
    if not data.get("streams"):
        raise RuntimeError(f"no video stream: {path}")
    return data["streams"][0]


def parse_fps(text: str) -> Fraction:
    if not text or text == "0/0":
        raise ValueError(f"invalid fps: {text!r}")
    return Fraction(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", default="60000/1001")
    args = parser.parse_args()

    stream = ffprobe(args.video)
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))
    fps = parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate"))
    target_fps = parse_fps(args.fps)
    side_data = stream.get("side_data_list", [])
    has_equirect = any(item.get("side_data_type") == "Spherical Mapping" for item in side_data)

    print(
        json.dumps(
            {
                "video": str(args.video),
                "codec": stream.get("codec_name"),
                "width": width,
                "height": height,
                "fps": str(fps),
                "nb_frames": stream.get("nb_frames"),
                "duration": stream.get("duration"),
                "has_equirectangular_metadata": has_equirect,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    if (width, height) != (args.width, args.height):
        raise SystemExit(f"resolution mismatch: got {width}x{height}, expected {args.width}x{args.height}")
    if abs(float(fps) - float(target_fps)) > 1e-3:
        raise SystemExit(f"fps mismatch: got {fps}, expected {target_fps}")


if __name__ == "__main__":
    main()
