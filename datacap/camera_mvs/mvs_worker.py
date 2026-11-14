#!/usr/bin/env python3
"""One-camera Hikrobot MVS owner for InstaDataCap.

This process deliberately reuses ExUMI's validated ``MVSCameraProcess``
instead of opening the vendor SDK a second way.  It owns exactly one serial,
publishes an atomic JPEG for the web UI, and records RGB video plus the MVS
frame clocks into one independently sealed DataCap episode directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
from typing import Any


PREFIX = "DATACAP_MVS_JSON "
PROTOCOL = "instadatacap-mvs-worker-v1"


def emit(action: str, ok: bool = True, **payload: Any) -> None:
    message = {
        "protocol": PROTOCOL,
        "action": action,
        "ok": bool(ok),
        **payload,
    }
    print(PREFIX + json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--role", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument("--preview-path", default="")
    parser.add_argument("--exumi-root", default=str(Path.home() / "exumi"))
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--exposure-us", type=int, default=20_000)
    parser.add_argument("--width", type=int, default=224)
    parser.add_argument("--height", type=int, default=224)
    parser.add_argument("--raw-width", type=int, default=1536)
    parser.add_argument("--raw-height", type=int, default=1536)
    parser.add_argument("--preview-fps", type=float, default=10.0)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    return parser.parse_args()


def install_exumi_root(root: str) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(f"ExUMI root does not exist: {path}")
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path


def list_devices(exumi_root: str) -> int:
    install_exumi_root(exumi_root)
    from exCap.camera.mvs_cam import get_all_mvs_dev_serial

    serials = [str(value) for value in get_all_mvs_dev_serial() if str(value)]
    emit("list", True, serials=serials, count=len(serials))
    return 0


class Recording:
    def __init__(
        self,
        root: Path,
        role: str,
        serial: str,
        fps: float,
        exposure_us: int,
        width: int,
        height: int,
    ) -> None:
        import av

        self.root = root
        self.role = role
        self.serial = serial
        self.fps = fps
        self.exposure_s = exposure_us / 1_000_000.0
        self.width = width
        self.height = height
        self.frame_count = 0
        self.first_frame_index: int | None = None
        self.last_frame_index: int | None = None
        self.first_capture_monotonic: float | None = None
        self.last_capture_monotonic: float | None = None
        self.started_unix_ns = time.time_ns()
        self.timestamp_source_counts: dict[str, int] = {}
        self.rgb_sha256 = hashlib.sha256()
        # ``root`` is already the immutable episode directory.  Keeping the
        # two camera roles directly below it makes an episode self-contained
        # and prevents a later timestamp edit from silently repartitioning a
        # session-long camera file.
        self.directory = root / role
        self.directory.mkdir(parents=True, exist_ok=False)
        self.video_partial = self.directory / "video.partial.mkv"
        self.video_final = self.directory / "video.mkv"
        self.csv_partial = self.directory / "camera_timestamps.partial.csv"
        self.csv_final = self.directory / "camera_timestamps.csv"
        # libx264rgb CRF 0 is pixel-exact for RGB24 and Matroska preserves that
        # contract without forcing YUV chroma subsampling.  The old h264/
        # yuv420p/CRF18 path irreversibly changed the camera pixels before the
        # gripper detector and OpenPI Zarr conversion saw them.
        self.container = av.open(str(self.video_partial), mode="w", format="matroska")
        self.stream = self.container.add_stream(
            "libx264rgb", rate=max(1, int(round(fps)))
        )
        self.stream.width = width
        self.stream.height = height
        self.stream.pix_fmt = "rgb24"
        self.stream.options = {"crf": "0", "preset": "veryfast"}
        self.csv_file = self.csv_partial.open("x", newline="", encoding="utf-8")
        self.writer = csv.writer(self.csv_file)
        self.writer.writerow(
            [
                "record_frame_index",
                "camera_frame_index",
                "capture_monotonic_time",
                "capture_wall_time",
                "frame_available_monotonic_time",
                "receive_wall_time",
                "sdk_frame_number",
                "sdk_host_timestamp_ms",
                "sdk_device_timestamp_ticks",
                "timestamp_source",
            ]
        )

    @staticmethod
    def scalar(sample: dict[str, Any], key: str, kind: type = float) -> Any:
        import numpy as np

        return kind(np.asarray(sample[key]).reshape(-1)[-1])

    def append(self, sample: dict[str, Any]) -> None:
        import av
        import numpy as np

        image = np.asarray(sample["color"], dtype=np.uint8)
        if image.ndim == 4:
            image = image[-1]
        expected = (self.height, self.width, 3)
        if image.shape != expected:
            raise RuntimeError(f"MVS frame shape {image.shape} does not match {expected}")
        image = np.ascontiguousarray(image)
        self.rgb_sha256.update(image.tobytes(order="C"))
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        for packet in self.stream.encode(frame):
            self.container.mux(packet)

        camera_index = self.scalar(sample, "frame_index", int)
        capture = self.scalar(sample, "capture_monotonic")
        if self.last_frame_index is not None and camera_index <= self.last_frame_index:
            raise RuntimeError(
                f"non-increasing MVS frame index: previous={self.last_frame_index} current={camera_index}"
            )
        if self.last_capture_monotonic is not None and capture <= self.last_capture_monotonic:
            raise RuntimeError("non-increasing MVS capture_monotonic timestamp")
        if self.first_frame_index is None:
            self.first_frame_index = camera_index
            self.first_capture_monotonic = capture
        self.last_frame_index = camera_index
        self.last_capture_monotonic = capture
        sdk_host_timestamp_ms = self.scalar(sample, "sdk_host_timestamp_ms")
        capture_wall_time = self.scalar(sample, "capture_wall_time")
        # MVSCameraProcess exposes the clocks but not the source label stored by
        # MvCamera.read(). Reconstruct that label from the same-frame values: a
        # valid SDK host timestamp equals the wall-clock exposure midpoint plus
        # half the configured exposure (allowing millisecond quantization).
        sdk_capture_wall_time = sdk_host_timestamp_ms / 1000.0 - self.exposure_s / 2.0
        if (
            sdk_host_timestamp_ms > 0.0
            and math.isfinite(sdk_capture_wall_time)
            and abs(capture_wall_time - sdk_capture_wall_time) <= 0.050
        ):
            timestamp_source = "mvs_sdk_host_ms_minus_half_exposure"
        else:
            timestamp_source = "get_image_buffer_return_minus_half_exposure"
        self.timestamp_source_counts[timestamp_source] = (
            self.timestamp_source_counts.get(timestamp_source, 0) + 1
        )
        self.writer.writerow(
            [
                self.frame_count,
                camera_index,
                f"{capture:.9f}",
                f"{capture_wall_time:.9f}",
                f"{self.scalar(sample, 'frame_available_monotonic'):.9f}",
                f"{self.scalar(sample, 'receive_wall_time'):.9f}",
                self.scalar(sample, "sdk_frame_number", int),
                f"{sdk_host_timestamp_ms:.3f}",
                self.scalar(sample, "sdk_device_timestamp_ticks", int),
                timestamp_source,
            ]
        )
        self.frame_count += 1
        if self.frame_count % max(1, int(round(self.fps))) == 0:
            self.csv_file.flush()

    def close(self) -> dict[str, Any]:
        for packet in self.stream.encode():
            self.container.mux(packet)
        self.container.close()
        self.csv_file.flush()
        os.fsync(self.csv_file.fileno())
        self.csv_file.close()
        if self.frame_count <= 0:
            raise RuntimeError("MVS recording contains no frames")
        self.video_partial.replace(self.video_final)
        self.csv_partial.replace(self.csv_final)
        summary = {
            "schema": "instadatacap.mvs.camera_capture.v1",
            "role": self.role,
            "serial": self.serial,
            "frame_count": self.frame_count,
            "first_camera_frame_index": self.first_frame_index,
            "last_camera_frame_index": self.last_frame_index,
            "first_capture_monotonic": self.first_capture_monotonic,
            "last_capture_monotonic": self.last_capture_monotonic,
            "fps_request": self.fps,
            "exposure_us": int(round(self.exposure_s * 1_000_000.0)),
            "timestamp_source_counts": self.timestamp_source_counts,
            "width": self.width,
            "height": self.height,
            "video_container": "matroska",
            "video_codec": "libx264rgb",
            "video_pixel_format": "rgb24",
            "video_lossless": True,
            "rgb_sha256_c_order": self.rgb_sha256.hexdigest(),
            "video": self.video_final.name,
            "timestamps": self.csv_final.name,
            "started_unix_ns": self.started_unix_ns,
            "stopped_unix_ns": time.time_ns(),
        }
        temporary = self.directory / ".manifest.json.tmp"
        temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "manifest.json")
        return summary

    def abort(self) -> None:
        try:
            self.container.close()
        except Exception:
            pass
        try:
            self.csv_file.close()
        except Exception:
            pass


def run(args: argparse.Namespace) -> int:
    if not args.role or not args.serial or not args.preview_path:
        raise ValueError("--role, --serial and --preview-path are required")
    if not math.isfinite(args.fps) or args.fps <= 0:
        raise ValueError("--fps must be positive")
    if args.exposure_us <= 0 or min(args.width, args.height, args.raw_width, args.raw_height) <= 0:
        raise ValueError("exposure and image dimensions must be positive")
    if (args.raw_width, args.raw_height, args.width, args.height) != (1536, 1536, 224, 224):
        raise ValueError(
            "MVS worker image contract is fixed: 1536x1536 BGR8 -> 224x224 RGB8 "
            "INTER_AREA exactly once"
        )
    exumi_root = install_exumi_root(args.exumi_root)

    import numpy as np
    from multiprocessing.managers import SharedMemoryManager
    from umi.real_world.mvs_camera_process import MVSCameraProcess

    command_queue: queue.Queue[str] = queue.Queue()
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def read_commands() -> None:
        for line in sys.stdin:
            command_queue.put(line.rstrip("\r\n"))
        stop_event.set()

    preview_path = Path(args.preview_path).expanduser().resolve()
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    manager = SharedMemoryManager()
    manager.start()
    camera = MVSCameraProcess(
        shm_manager=manager,
        serial=args.serial,
        exposure=args.exposure_us,
        raw_w=args.raw_width,
        raw_h=args.raw_height,
        obs_image_resolution=(args.width, args.height),
        obs_float32=False,
        get_max_k=max(60, int(math.ceil(args.fps * 2.0))),
        put_fps=args.fps,
        enforce_put_fps=True,
        verbose=True,
        purpose=f"datacap-{args.role}",
    )
    recording: Recording | None = None
    last_frame_index = -1
    last_preview_monotonic = 0.0
    latest_state: dict[str, Any] = {}
    try:
        # Fork the ExUMI camera owner before starting any Python thread or
        # importing OpenCV in this parent. Forking the vendor SDK from a
        # multi-threaded/OpenCV-initialized process can deadlock before the
        # first frame even though direct MVSCameraStream access is healthy.
        camera.start(wait=True)
        first = camera.get()
        last_frame_index = int(np.asarray(first["frame_index"]).reshape(-1)[-1]) - 1
        import cv2

        reader = threading.Thread(
            target=read_commands,
            name="mvs-command-reader",
            daemon=True,
        )
        reader.start()
        emit(
            "ready",
            True,
            message="MVS camera is streaming",
            role=args.role,
            serial=args.serial,
            ready=True,
            recording=False,
            fps=args.fps,
            exposure_us=args.exposure_us,
            width=args.width,
            height=args.height,
            raw_width=args.raw_width,
            raw_height=args.raw_height,
            exumi_root=str(exumi_root),
        )
        while not stop_event.is_set():
            while True:
                try:
                    command = command_queue.get_nowait()
                except queue.Empty:
                    break
                if not command:
                    continue
                if command.startswith("START\t"):
                    if recording is not None:
                        emit("start", False, role=args.role, message="recording is already active")
                        continue
                    session_dir = Path(json.loads(command.split("\t", 1)[1])).expanduser().resolve()
                    try:
                        recording = Recording(
                            session_dir,
                            args.role,
                            args.serial,
                            args.fps,
                            args.exposure_us,
                            args.width,
                            args.height,
                        )
                    except Exception as exc:
                        emit("start", False, role=args.role, message=f"{type(exc).__name__}: {exc}")
                    else:
                        emit("start", True, role=args.role, serial=args.serial, recording=True)
                elif command == "STOP":
                    if recording is None:
                        emit("stop", False, role=args.role, message="recording is not active")
                        continue
                    active, recording = recording, None
                    try:
                        summary = active.close()
                    except Exception as exc:
                        active.abort()
                        emit("stop", False, role=args.role, message=f"{type(exc).__name__}: {exc}")
                    else:
                        emit("stop", True, role=args.role, recording=False, summary=summary)
                elif command == "STATUS":
                    emit(
                        "status",
                        True,
                        role=args.role,
                        serial=args.serial,
                        ready=camera.is_ready and camera.is_alive(),
                        recording=recording is not None,
                        **latest_state,
                    )
                elif command == "QUIT":
                    if recording is not None:
                        active, recording = recording, None
                        try:
                            active.close()
                        except Exception:
                            active.abort()
                    emit("quit", True, role=args.role, message="MVS worker stopping")
                    stop_event.set()
                else:
                    emit("error", False, role=args.role, message=f"unknown command: {command!r}")

            if not camera.is_alive():
                raise RuntimeError("MVS camera process exited during streaming")
            latest_sample = camera.get()
            latest_frame_index = int(
                np.asarray(latest_sample["frame_index"]).reshape(-1)[-1]
            )
            if latest_frame_index <= last_frame_index:
                time.sleep(0.001)
                continue
            gap = latest_frame_index - last_frame_index
            if gap > camera.ring_buffer.get_max_k:
                raise RuntimeError(
                    "MVS recording consumer fell behind the bounded frame history: "
                    f"previous={last_frame_index} latest={latest_frame_index} "
                    f"capacity={camera.ring_buffer.get_max_k}"
                )
            if gap == 1:
                samples = [latest_sample]
            else:
                # Read a small safety margin because the camera may publish a
                # new row between the latest-frame probe and this snapshot.
                count = camera.ring_buffer.count
                k = min(camera.ring_buffer.get_max_k, count, gap + 4)
                batch = camera.get(k=k)
                samples = [
                    {key: np.asarray(value)[index] for key, value in batch.items()}
                    for index in range(k)
                    if int(np.asarray(batch["frame_index"])[index]) > last_frame_index
                    and int(np.asarray(batch["frame_index"])[index]) <= latest_frame_index
                ]
            expected_index = last_frame_index + 1
            for sample in samples:
                frame_index = int(np.asarray(sample["frame_index"]).reshape(-1)[-1])
                if frame_index != expected_index:
                    raise RuntimeError(
                        "MVS frame gap before recording/preview: "
                        f"expected={expected_index} got={frame_index}"
                    )
                expected_index += 1
                last_frame_index = frame_index
                capture = float(
                    np.asarray(sample["capture_monotonic"]).reshape(-1)[-1]
                )
                available = float(
                    np.asarray(sample["frame_available_monotonic"]).reshape(-1)[-1]
                )
                latest_state = {
                    "frame_index": frame_index,
                    "capture_monotonic": capture,
                    "frame_available_monotonic": available,
                    "frame_age_ms": max(
                        0.0, (time.monotonic() - available) * 1000.0
                    ),
                    "sdk_frame_number": int(
                        np.asarray(sample["sdk_frame_number"]).reshape(-1)[-1]
                    ),
                }
                if recording is not None:
                    recording.append(sample)
                now = time.monotonic()
                if now - last_preview_monotonic >= 1.0 / max(0.1, args.preview_fps):
                    image_rgb = np.asarray(sample["color"], dtype=np.uint8)
                    if image_rgb.ndim == 4:
                        image_rgb = image_rgb[-1]
                    ok, encoded = cv2.imencode(
                        ".jpg",
                        np.ascontiguousarray(image_rgb[..., ::-1]),
                        [
                            cv2.IMWRITE_JPEG_QUALITY,
                            max(1, min(100, args.jpeg_quality)),
                        ],
                    )
                    if not ok:
                        raise RuntimeError(
                            "OpenCV could not encode MVS preview JPEG"
                        )
                    temporary = preview_path.with_name(
                        f".{preview_path.name}.{os.getpid()}.tmp"
                    )
                    temporary.write_bytes(encoded.tobytes())
                    temporary.replace(preview_path)
                    last_preview_monotonic = now
                    if frame_index == 0 or frame_index % max(
                        1, int(round(args.fps))
                    ) == 0:
                        emit(
                            "frame",
                            True,
                            role=args.role,
                            serial=args.serial,
                            ready=True,
                            recording=recording is not None,
                            preview_path=str(preview_path),
                            **latest_state,
                        )
    finally:
        if recording is not None:
            try:
                recording.close()
            except Exception:
                recording.abort()
        # A vendor SDK call can occasionally remain inside OpenDevice or
        # GetImageBuffer. Never let that make the DataCap service unkillable.
        # The adapter also owns the outer process group as a final backstop.
        if camera.pid is not None:
            camera.stop(wait=False)
            camera.join(timeout=3.0)
            if camera.is_alive():
                camera.terminate()
                camera.join(timeout=2.0)
            if camera.is_alive():
                camera.kill()
                camera.join(timeout=2.0)
        manager.shutdown()
        try:
            preview_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def main() -> None:
    args = parse_args()
    try:
        status = list_devices(args.exumi_root) if args.list_devices else run(args)
    except BaseException as exc:
        emit("fatal", False, message=f"{type(exc).__name__}: {exc}")
        raise
    raise SystemExit(status)


if __name__ == "__main__":
    main()
