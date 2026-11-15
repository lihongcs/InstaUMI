#!/usr/bin/env python3
"""Export the two wrists' native X5 fisheye tracks as chapter MP4s."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from capture_schema import camera_role_directories, media_files


ROLES = ("insta_right", "insta_left")
MASKED_PROFILE = "front-back-180"
VIEWS = ("front", "back")
Box = tuple[float, float, float, float]


def load_masks(path: Path) -> dict[str, dict[str, list[Box]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != set(ROLES):
        raise ValueError("mask JSON must contain exactly insta_right and insta_left")
    masks = {}
    for role in ROLES:
        if not isinstance(raw[role], dict) or set(raw[role]) != set(VIEWS):
            raise ValueError(f"{role} masks must contain exactly front and back")
        masks[role] = {}
        for view in VIEWS:
            values = raw[role][view]
            if not isinstance(values, list) or not values:
                raise ValueError(f"{role}.{view} requires one or more opaque boxes")
            boxes = []
            for box in values:
                if not isinstance(box, list) or len(box) != 4 or any(
                    type(value) not in (int, float) or not 0 <= value <= 1 for value in box
                ):
                    raise ValueError(f"{role}.{view} expects [x0,y0,x1,y1] within [0,1]")
                x0, y0, x1, y1 = box
                if not (x0 < x1 and y0 < y1):
                    raise ValueError(f"{role}.{view} box needs positive width and height")
                boxes.append((x0, y0, x1, y1))
            masks[role][view] = boxes
    return masks


def probe_streams(binary: str, path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            binary, "-v", "error", "-show_streams",
            "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)["streams"]


def box_filter(box: Box, width: int, height: int) -> str:
    x0, y0, x1, y1 = box
    # Round outward to the chroma grid used by the MP4 encoder.
    left, top = 2 * math.floor(x0 * width / 2), 2 * math.floor(y0 * height / 2)
    right, bottom = 2 * math.ceil(x1 * width / 2), 2 * math.ceil(y1 * height / 2)
    return f"drawbox=x={left}:y={top}:w={right-left}:h={bottom-top}:color=black:t=fill"


def export_track(
    args: argparse.Namespace,
    source: Path,
    destination: Path,
    track: int,
    stream: dict[str, Any],
    boxes: list[Box],
) -> None:
    width, height = stream["width"], stream["height"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        args.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-n",
        "-noautorotate", "-i", str(source), "-map", f"0:v:{track}",
        "-map_metadata", "-1", "-map_metadata:s:v", "-1",
        "-map_chapters", "-1", "-an", "-sn", "-dn",
    ]
    if boxes:
        filters = ",".join(box_filter(box, width, height) for box in boxes)
        command += [
            "-vf", filters, "-vsync", "passthrough", "-c:v", "libx264",
            "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        ]
    else:
        command += ["-c:v", "copy"]
    subprocess.run([*command, "-movflags", "+faststart", str(destination)], check=True)

    streams = probe_streams(args.ffprobe, destination)
    if len(streams) != 1 or streams[0]["codec_type"] != "video":
        raise ValueError(f"{destination.name}: expected exactly one video stream")
    output = streams[0]
    if (output["width"], output["height"]) != (width, height):
        raise ValueError(f"{destination.name}: exported dimensions differ from source")
    expected_count = stream.get("nb_frames")
    if expected_count and expected_count != "N/A" and output.get("nb_frames") != expected_count:
        raise ValueError(f"{destination.name}: exported frame count differs from source")


def export_views(args: argparse.Namespace) -> None:
    capture = args.capture.resolve(strict=True)
    if not capture.is_dir() or any(
        (capture / marker).exists()
        for marker in ("uvc_capture_manifest.json", "uvc_capture_complete.json")
    ):
        raise ValueError("capture must be a bimanual INSV session")
    output = args.output.resolve()
    if os.path.lexists(args.output) or output.exists():
        raise FileExistsError(f"output already exists: {args.output}")
    role_dirs = camera_role_directories(capture)
    chapters = {role: media_files(role_dirs[role], {".insv"}) for role in ROLES}
    for role in ROLES:
        if not chapters[role]:
            raise ValueError(f"no INSV chapters found for {role}: {role_dirs[role]}")
        if role_dirs[role].resolve() in output.parents:
            raise ValueError("choose an output directory outside the original camera directories")

    views = VIEWS if args.profile == MASKED_PROFILE else ("front",)
    masks = load_masks(args.mask_json) if args.mask_json is not None else {}
    front_tracks = {
        "insta_right": args.right_front_stream,
        "insta_left": args.left_front_stream,
    }
    entries = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temp_name:
        temp = Path(temp_name) / "export"
        temp.mkdir()
        for role in ROLES:
            for chapter in chapters[role]:
                streams = [
                    stream for stream in probe_streams(args.ffprobe, chapter)
                    if stream["codec_type"] == "video"
                ]
                if len(streams) != 2 or any(
                    stream["width"] != stream["height"] or stream["height"] % 2
                    for stream in streams
                ):
                    raise ValueError(f"{chapter.name}: expected two square native fisheye tracks")
                source_rel = chapter.relative_to(role_dirs[role])
                for view in views:
                    track = front_tracks[role] if view == "front" else 1 - front_tracks[role]
                    output_rel = Path(role, source_rel.parent, f"{chapter.stem}.{view}-180.mp4")
                    stream = streams[track]
                    boxes = masks[role][view] if masks else []
                    export_track(args, chapter, temp / output_rel, track, stream, boxes)
                    entries.append({
                        "role": role,
                        "view": view,
                        "video": output_rel.as_posix(),
                        "source_chapter": source_rel.as_posix(),
                        "source_video_track": track,
                        "width": stream["width"],
                        "height": stream["height"],
                        "source_average_frame_rate": stream.get("avg_frame_rate"),
                        "opaque_mask_boxes_xyxy_normalized": boxes,
                    })
        manifest = {
            "schema": "instaumi.native_wrist_views.v1",
            "profile": args.profile,
            "projection": "native_circular_fisheye",
            "field_of_view_degrees_nominal": 180,
            "timing": "per_chapter; no cross-camera or tracker alignment",
            "face_masks": "opaque_manual_regions; review before sharing" if masks else "none",
            "videos": entries,
        }
        (temp / "native_views.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if os.path.lexists(output):
            raise FileExistsError(f"output appeared during export: {output}")
        temp.rename(output)
    print(f"Exported {len(entries)} native wrist videos to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path, help="new directory for chapter MP4s and manifest")
    parser.add_argument("--profile", choices=("front-180", MASKED_PROFILE), required=True)
    parser.add_argument("--mask-json", type=Path, help="required reviewed per-role masks for front-back-180")
    parser.add_argument("--right-front-stream", type=int, choices=(0, 1), default=0)
    parser.add_argument("--left-front-stream", type=int, choices=(0, 1), default=0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    if bool(args.mask_json) != (args.profile == MASKED_PROFILE):
        parser.error("--mask-json is required and accepted only for front-back-180")
    try:
        export_views(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"export failed: {exc}\n")


if __name__ == "__main__":
    main()
