from __future__ import annotations

import argparse
import fcntl
import json
import math
import mimetypes
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .insta_adapter import (
    AdapterResult,
    BlueZDirectInstaAdapter,
    BlueZRemoteInstaAdapter,
    CameraSDKInstaAdapter,
    CompositeCaptureAdapter,
    DryRunInstaAdapter,
    InstaAdapter,
    UsbWebcamInstaAdapter,
)
from .mvs_adapter import MvsCameraAdapter
from .gripper_binary_preview import GripperBinaryPreviewReader
from .gripper_episode_estimation import (
    GripperEpisodeEstimationJob,
    GripperEstimationBusyError,
    GripperEstimationNotReadyError,
    discover_gripper_capture_sources,
)
from .offline_annotation import OfflineActiveViewAnnotationStore
from .pedal import FootPedalReader, resolve_input_device
from .session import DataCaptureSession, InputTimestamp
from .tracker_adapter import PyVUTTrackerAdapter


DATACAP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("INSTADATACAP_STATE_DIR", DATACAP_DIR)).expanduser().resolve()
RUNTIME_SETTINGS_PATH = STATE_DIR / "runtime_settings.json"
SWITCHABLE_CAMERA_TRANSPORTS = ("bluez_direct", "usb_webcam", "mvs")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_config() -> dict[str, Any]:
    sdk_libs = sorted((DATACAP_DIR / ".sdk").glob("CameraSDK*-Linux/lib"))
    output_root = Path(
        os.environ.get(
            "INSTADATACAP_DATA_DIR",
            str(Path.home() / "InstaDataCap"),
        )
    ).expanduser().resolve()
    data_python = os.environ.get("INSTADATACAP_DATA_PYTHON", "").strip() or sys.executable
    return {
        "task_name": "Untitled task",
        "output_root": str(output_root),
        "web": {"host": "127.0.0.1", "port": 8788},
        "data_preview": {
            "python": data_python,
            "timeout_sec": 30.0,
            "max_frames": 200000,
            # This is a preview/export suggestion only.  Raw capture and the
            # default Zarr conversion retain continuous calibrated metres.
            "gripper_binary_threshold_m": 0.02,
            "gripper_sampler": str(
                DATACAP_DIR.parent
                / "dataprocess"
                / "_03_gripper_estimation"
                / "sample_gripper_threshold.py"
            ),
            "gripper_job_output_directory": "gripper_threshold_sampling",
            "gripper_job_log_lines": 200,
        },
        "pedal": {
            "device": "auto",
            "name_contains": "",
            "key_code": None,
            "triple_window_sec": 1.2,
            "action_cooldown_sec": 0.8,
            "debounce_sec": 0.08,
            "long_press_sec": 2.0,
        },
        "insta": {
            "mode": "bluez_direct",
            "connect_on_start": False,
            "python": "/usr/bin/python3",
            "worker": str(DATACAP_DIR / "insta_ble_direct_worker.py"),
            "adapter": "hci0",
            "expected_cameras": 1,
            "require_all": True,
            "addresses": [],
            "camera_roles": ["insta_right", "insta_left"],
            "role_by_address": {},
            "name_prefixes": ["X5 "],
            "ready_timeout_sec": 10.0,
            "discovery_timeout_sec": 15.0,
            "command_timeout_sec": 8.0,
            "ack_timeout_sec": 3.0,
            "allow_unsubscribed": False,
            "camera_sdk_worker": str(DATACAP_DIR / "bin" / "insta_camera_worker"),
            "sdk_lib_dir": str(sdk_libs[0]) if sdk_libs else str(DATACAP_DIR / ".sdk" / "CAMERA_SDK/lib"),
            "resolution": "5.7k60",
            "bitrate_mbps": 120,
            "serial": "",
            # USB Webcam mode is opt-in.  Each role must use a stable physical
            # video-index0 path; /dev/videoN and metadata index1 are unsafe for
            # persistent left/right assignment.
            "usb_webcam_worker": str(
                DATACAP_DIR / "camera_usb" / "bin" / "insta_uvc_worker"
            ),
            "usb_devices": {},
            "usb_preview_dir": str(Path("/dev/shm") / f"instadatacap-uvc-{os.getuid()}"),
            "usb_ready_timeout_sec": 15.0,
            "usb_stop_timeout_sec": 120.0,
            "view_mode": "panorama",
            # Hikrobot MVS mode reuses the same ExUMI camera process used by
            # the PC MVS teleoperation recorder.  Serial assignment is
            # explicit so left/right identity never depends on enumeration
            # order.
            "mvs_python": sys.executable,
            "mvs_worker": str(DATACAP_DIR / "camera_mvs" / "mvs_worker.py"),
            "mvs_exumi_root": str(Path.home() / "exumi"),
            "mvs_serials": {},
            "mvs_preview_dir": str(
                Path("/dev/shm") / f"instadatacap-mvs-{os.getuid()}"
            ),
            "mvs_fps": 30.0,
            "mvs_exposure_us": 20000,
            "mvs_width": 224,
            "mvs_height": 224,
            "mvs_raw_width": 1536,
            "mvs_raw_height": 1536,
            "mvs_ready_timeout_sec": 15.0,
            "mvs_stop_timeout_sec": 30.0,
            "mvs_require_usb3": True,
        },
        "tracker": {
            "enabled": True,
            "required": True,
            "simulate": False,
            "python": sys.executable,
            "worker": str(DATACAP_DIR / "tracker_recorder.py"),
            "pyvut_root": str(Path.home() / ".local" / "share" / "instadatacap" / "pyvut"),
            "mode": "DONGLE_USB",
            "wifi_info_path": str(Path.home() / ".config" / "instadatacap" / "wifi_info.json"),
            "require_pose": True,
            "expected_trackers": 2,
            "ready_timeout_sec": 10.0,
            "command_timeout_sec": 15.0,
            "rate_hz": 60.0,
            "simulated_trackers": 2,
            "roles": ["htc_right", "htc_left"],
            "role_by_mac": {},
            "role_config_path": str(Path.home() / ".config" / "htc" / "roles.json"),
            "preserve_existing_connection": True,
            "monitor_while_idle": True,
            "release_on_stop": True,
            "health_restart_cooldown_sec": 30.0,
            "auto_release_idle_omniimu": False,
            "omniimu_state_url": "http://127.0.0.1:8780/api/state",
            "omniimu_shutdown_url": "http://127.0.0.1:8780/api/shutdown",
        },
    }


def load_config(path: Path | None, dry_run: bool) -> dict[str, Any]:
    config = default_config()
    if path is not None:
        config = deep_merge(config, json.loads(path.read_text(encoding="utf-8")))
    if RUNTIME_SETTINGS_PATH.is_file():
        try:
            saved = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
            for key in ("task_name", "output_root", "ui_tracker_view"):
                if key in saved:
                    config[key] = saved[key]
            saved_transport = saved.get("camera_transport")
            if path is None and saved_transport in SWITCHABLE_CAMERA_TRANSPORTS:
                config["insta"]["mode"] = saved_transport
                # MVS is a PC-owned live acquisition backend: keep its two
                # serial-bound workers alive from application startup so the
                # operator always has a preview before beginning an episode.
                # BLE/UVC retain the explicit-connect behavior.
                config["insta"]["connect_on_start"] = saved_transport == "mvs"
        except (OSError, ValueError, TypeError) as exc:
            print(f"[datacap] ignored invalid runtime settings: {exc}", flush=True)
    if dry_run:
        config["insta"]["mode"] = "dry_run"
        config["tracker"]["enabled"] = True
        config["tracker"]["simulate"] = True
        config["tracker"]["python"] = sys.executable
    return config


class CaptureRuntime:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.ui_tracker_view = self._normalize_tracker_view(config.get("ui_tracker_view"))
        self.monitor_tracker_while_idle = bool(
            config.get("tracker", {}).get("monitor_while_idle", False)
        )
        tracker_config = config.get("tracker", {})
        self.auto_release_idle_omniimu = bool(
            tracker_config.get("auto_release_idle_omniimu", False)
        )
        self.omniimu_state_url = str(
            tracker_config.get("omniimu_state_url", "http://127.0.0.1:8780/api/state")
        )
        self.omniimu_shutdown_url = str(
            tracker_config.get("omniimu_shutdown_url", "http://127.0.0.1:8780/api/shutdown")
        )
        self.htc_lock_path = Path(
            os.environ.get(
                "HTC_DONGLE_LOCK_FILE",
                f"/tmp/instaumi_htc_dongle_{os.getuid()}.lock",
            )
        ).expanduser().resolve()
        self.connect_insta_on_start = bool(
            config.get("insta", {}).get("connect_on_start", False)
        )
        self.insta_adapter: InstaAdapter | None = None
        self.tracker_adapter: PyVUTTrackerAdapter | None = None
        # Serializes capture start against disk-heavy post-processing requests.
        # A pedal start and a web request therefore cannot overlap even when
        # they arrive from different threads.
        self.capture_postprocess_lock = threading.RLock()
        # Keeps live camera actions and an operator-requested transport swap
        # from observing different Insta adapter instances mid-operation.
        self.camera_adapter_lock = threading.RLock()
        self.runtime_settings_lock = threading.Lock()
        self.adapter = self._build_adapter()
        pedal = config["pedal"]
        initial_expected_trackers = (
            self.tracker_adapter.expected_trackers if self.tracker_adapter is not None else 2
        )
        initial_expected_cameras = int(getattr(self.insta_adapter, "expected_cameras", 2))
        initial_arm_mode = "single_arm" if initial_expected_trackers == 1 else "dual_arm"
        self.session = DataCaptureSession(
            output_root=Path(config["output_root"]).expanduser().resolve(),
            adapter=self.adapter,
            task_name=str(config.get("task_name", "Untitled task")),
            triple_start_window_sec=float(pedal["triple_window_sec"]),
            action_cooldown_sec=float(pedal["action_cooldown_sec"]),
            debounce_sec=float(pedal["debounce_sec"]),
            arm_mode=initial_arm_mode,
            expected_trackers=initial_expected_trackers,
            expected_cameras=initial_expected_cameras,
        )
        self.offline_annotations = OfflineActiveViewAnnotationStore()
        preview_config = config.get("data_preview", {})
        try:
            self.gripper_binary_threshold_m = float(
                preview_config.get("gripper_binary_threshold_m", 0.02)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("data_preview.gripper_binary_threshold_m must be numeric") from exc
        if (
            not math.isfinite(self.gripper_binary_threshold_m)
            or self.gripper_binary_threshold_m <= 0.0
            or self.gripper_binary_threshold_m > 0.1
        ):
            raise ValueError(
                "data_preview.gripper_binary_threshold_m must be finite and in (0, 0.1] metres"
            )
        self.gripper_preview_reader = GripperBinaryPreviewReader(
            str(preview_config.get("python", sys.executable)),
            timeout_sec=float(preview_config.get("timeout_sec", 30.0)),
            max_frames=int(preview_config.get("max_frames", 200000)),
        )
        self.gripper_estimation_job = GripperEpisodeEstimationJob(
            python_executable=str(preview_config.get("python", sys.executable)),
            sampler_script=str(
                preview_config.get(
                    "gripper_sampler",
                    DATACAP_DIR.parent
                    / "dataprocess"
                    / "_03_gripper_estimation"
                    / "sample_gripper_threshold.py",
                )
            ),
            preview_reader=self.gripper_preview_reader,
            output_directory_name=str(
                preview_config.get(
                    "gripper_job_output_directory",
                    "gripper_threshold_sampling",
                )
            ),
            max_log_lines=int(preview_config.get("gripper_job_log_lines", 200)),
        )
        self._configure_arm_adapters(initial_arm_mode)
        self.pedal_reader: FootPedalReader | None = None
        self.pedal_status: dict[str, Any] = {
            "connected": False,
            "device": None,
            "message": "foot pedal not started",
        }
        self.last_pedal_message = ""
        self.device_action_lock = threading.Lock()
        self.device_action: dict[str, Any] = {
            "running": False,
            "action": None,
            "message": "Waiting for a camera action",
            "ok": None,
        }
        self.device_action_thread: threading.Thread | None = None
        # Supported camera adapters serialize lifecycle calls with close().
        # Once adapter.close() returns, only the action thread's in-memory
        # result publication remains, so this join is intentionally short.
        self.device_action_shutdown_join_sec = 2.0
        self.device_logs: list[dict[str, Any]] = []
        self.tracker_monitor_lock = threading.Lock()
        self.tracker_monitor_starting = False
        # An explicit disconnect is a software-ownership lease for an external
        # consumer such as Flexiv teleop.  Keep the DataCap maintenance loop
        # from reopening the same exclusive HID receiver until connect_tracker
        # explicitly returns ownership.  This never changes RF pairing/maps.
        self.tracker_monitor_suspended = False
        self.tracker_last_health_restart_monotonic = 0.0
        self.tracker_health_restart_cooldown_sec = float(
            tracker_config.get("health_restart_cooldown_sec", 30.0)
        )
        self.stop_event = threading.Event()
        self.maintenance_thread: threading.Thread | None = None

    def start(self) -> None:
        self._start_tracker_monitor_async()
        if self.connect_insta_on_start and isinstance(
            self.insta_adapter,
            (BlueZDirectInstaAdapter, UsbWebcamInstaAdapter, MvsCameraAdapter),
        ):
            self._start_device_action("connect_insta")
        self.maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="Datacap-Maintenance",
            daemon=True,
        )
        self.maintenance_thread.start()
        pedal_config = self.config["pedal"]
        selector = str(pedal_config.get("device", "auto"))
        if selector.lower() in {"none", "off", "disabled"}:
            self.pedal_status["message"] = "foot pedal disabled; web controls remain available"
            return
        device = resolve_input_device(selector, str(pedal_config.get("name_contains", "")))
        if device is None:
            self.pedal_status["message"] = "no foot pedal found; web controls remain available"
            return
        self.pedal_reader = FootPedalReader(
            device,
            self._on_pedal,
            key_code=pedal_config.get("key_code"),
            long_press_sec=float(pedal_config.get("long_press_sec", 2.0)),
            on_status=self._on_pedal_status,
        )
        self.pedal_reader.start()

    def close(self) -> None:
        # Reject new web/device work before waiting for any already accepted
        # request. The HTTP server normally stops accepting requests first, but
        # this also makes direct/runtime callers deterministic during teardown.
        self.stop_event.set()
        errors: list[Exception] = []
        try:
            self.gripper_estimation_job.close()
        except Exception as exc:
            errors.append(exc)
        try:
            if self.maintenance_thread is not None:
                self.maintenance_thread.join(timeout=2.0)
        except Exception as exc:
            errors.append(exc)
        try:
            if self.pedal_reader is not None:
                self.pedal_reader.stop()
        except Exception as exc:
            errors.append(exc)
        # Transport switches and capture start are protected by the same lock.
        # Closing under it prevents publication of a replacement adapter after
        # the composite has already been closed.
        with getattr(self, "capture_postprocess_lock", nullcontext()):
            try:
                if self.session.running:
                    print("[datacap] stopping active capture before shutdown", flush=True)
                    self.session.end_total(InputTimestamp.now("app_shutdown"))
            except Exception as exc:
                errors.append(exc)
            try:
                # Every supported camera adapter serializes scan/connect with
                # close() on its lifecycle lock. BlueZ additionally cancels a
                # pending response wait. Thus return from this call establishes
                # that the action thread no longer owns camera hardware.
                self.adapter.close()
            except Exception as exc:
                errors.append(exc)

            with getattr(self, "device_action_lock", nullcontext()):
                action_thread = getattr(self, "device_action_thread", None)
            if action_thread is not None and action_thread is not threading.current_thread():
                try:
                    join_timeout = float(
                        getattr(self, "device_action_shutdown_join_sec", 2.0)
                    )
                    action_thread.join(timeout=join_timeout)
                    if action_thread.is_alive():
                        errors.append(
                            TimeoutError(
                                "Camera action thread did not finish its in-memory shutdown "
                                f"within {join_timeout:.1f}s"
                            )
                        )
                except RuntimeError as exc:
                    errors.append(exc)
        # A maintenance poll may have been blocked in adapter.status() during
        # the first join. It now observes stop_event (checked again immediately
        # after that poll) and must exit without reconnecting a closed adapter.
        try:
            maintenance_is_alive = getattr(
                self.maintenance_thread, "is_alive", None
            )
            if (
                self.maintenance_thread is not None
                and self.maintenance_thread is not threading.current_thread()
                and callable(maintenance_is_alive)
                and maintenance_is_alive()
            ):
                self.maintenance_thread.join(timeout=2.0)
                if maintenance_is_alive():
                    errors.append(
                        TimeoutError(
                            "DataCap maintenance thread did not stop within 2.0s"
                        )
                    )
        except RuntimeError as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError(
                "DataCap shutdown completed with cleanup errors: "
                + "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            ) from errors[0]

    def _maintenance_loop(self) -> None:
        """Recover idle device workers without interfering with an active capture."""

        while not self.stop_event.wait(2.0):
            session_status = self.session.status()
            if self.stop_event.is_set():
                break
            if session_status["running"] or session_status["transitioning"]:
                continue
            self._maintain_tracker_monitor()
            with self.camera_adapter_lock:
                insta_adapter = self.insta_adapter
                if not isinstance(insta_adapter, BlueZDirectInstaAdapter):
                    continue
                try:
                    result = insta_adapter.maintain_connection()
                except Exception as exc:
                    result = AdapterResult(False, f"Automatic camera reconnect failed: {type(exc).__name__}: {exc}")
                if result is not None and not result.ok:
                    now_ns = time.time_ns()
                    with self.device_action_lock:
                        self.device_logs.append(
                            {
                                "action": "auto_reconnect",
                                "message": result.message,
                                "ok": False,
                                "unix_time_ns": now_ns,
                            }
                        )

    def status(self) -> dict[str, Any]:
        with self.device_action_lock:
            device_action = dict(self.device_action)
            device_logs = list(self.device_logs[-20:])
        session_status = self.session.status()
        with self.camera_adapter_lock:
            camera_transport = self._camera_transport_mode(self.insta_adapter)
        switchable_transport = camera_transport in SWITCHABLE_CAMERA_TRANSPORTS
        switch_locked = bool(
            session_status.get("running")
            or session_status.get("starting")
            or session_status.get("stopping")
            or device_action.get("running")
            or not switchable_transport
        )
        session_status["active_view_annotations"] = self.offline_annotations.status()
        session_status["gripper_preview_estimate"] = self.gripper_estimation_job.status(
            include_preview=False,
            include_logs=False,
        )
        session_status["gripper_binary_preview"] = {
            "read_only": True,
            "configured_threshold_m": self.gripper_binary_threshold_m,
            "preferred_dataset_format": "zarr",
            "default_conversion_gripper_mode": "continuous",
        }
        return {
            **session_status,
            "pedal": dict(self.pedal_status),
            "last_pedal_message": self.last_pedal_message,
            "device_action": device_action,
            "device_logs": device_logs,
            "camera_transport": camera_transport,
            "available_camera_transports": list(SWITCHABLE_CAMERA_TRANSPORTS),
            "can_switch_camera_transport": not switch_locked,
            "camera_transport_switch_locked": switch_locked,
            "ui_tracker_view": dict(self.ui_tracker_view) if self.ui_tracker_view else None,
        }

    def tracker_status(self) -> dict[str, Any]:
        """Return only lightweight live tracker state for trajectory polling."""

        if self.tracker_adapter is None:
            return {}
        return {
            **self.tracker_adapter.status(),
            "monitor_suspended": bool(self.tracker_monitor_suspended),
        }

    def pedal_live_status(self) -> dict[str, Any]:
        return {
            **dict(self.pedal_status),
            **self.session.pedal_live_status(),
        }

    def camera_preview(self, role: str) -> tuple[bytes, str] | None:
        """Return one adapter-owned preview without opening camera hardware again."""

        with getattr(self, "camera_adapter_lock", nullcontext()):
            insta_adapter = self.insta_adapter
            if not isinstance(
                insta_adapter, (UsbWebcamInstaAdapter, MvsCameraAdapter)
            ):
                return None
            preview = insta_adapter.latest_preview_bytes(role)
        if preview is None:
            return None
        payload, _metadata = preview
        # The worker promises a standalone browser JPEG. Refuse stale, partial,
        # or non-JPEG content even if it was atomically published at the owned
        # path; the source MJPEG packets themselves intentionally have a
        # different contract and must never be served by this endpoint.
        if (
            len(payload) < 4
            or payload[:2] != b"\xff\xd8"
            or payload[-2:] != b"\xff\xd9"
        ):
            return None
        return payload, "image/jpeg"

    def gripper_binary_preview(self, value: Any) -> dict[str, Any]:
        """Read one post-processing trace without touching capture or dataset files."""

        with self.capture_postprocess_lock:
            self._require_postprocess_idle()
            if not isinstance(value, dict):
                raise ValueError("invalid gripper preview request")
            root = str(value.get("root", "")).strip()
            if not root:
                raise ValueError(
                    "enter a capture, _03 gripper preview manifest, Zarr shard/collection, "
                    "legacy LeRobot dataset, or episode Parquet"
                )
            episode_value = value.get("episode")
            if episode_value in (None, ""):
                episode_index = None
            else:
                try:
                    episode_index = int(episode_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("episode must be a non-negative integer") from exc
                if episode_index < 0:
                    raise ValueError("episode must be a non-negative integer")
            preview = self.gripper_preview_reader.load(root, episode_index)
            preview["configured_binary_threshold_m"] = self.gripper_binary_threshold_m
            preview["preferred_export_format"] = "zarr"
            preview["default_export_gripper_mode"] = "continuous"
            return preview

    def discover_gripper_preview_captures(self, value: Any) -> dict[str, Any]:
        """List raw captures/episodes for the automatic read-only preview UI."""

        with self.capture_postprocess_lock:
            self._require_postprocess_idle()
            if not isinstance(value, dict):
                raise ValueError("invalid gripper capture discovery request")
            root = str(value.get("root", "")).strip()
            if not root:
                raise ValueError("raw capture or parent directory is required")
            return discover_gripper_capture_sources(root)

    @staticmethod
    def _parse_gripper_estimation_request(value: Any) -> tuple[str, int]:
        if not isinstance(value, dict):
            raise ValueError("invalid gripper estimation request")
        root = str(value.get("root", "")).strip()
        if not root:
            raise ValueError("capture root is required")
        episode_value = value.get("episode")
        if isinstance(episode_value, bool):
            raise ValueError("episode must be a non-negative integer")
        try:
            episode = int(episode_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("episode must be a non-negative integer") from exc
        if episode < 0 or str(episode) != str(episode_value).strip():
            raise ValueError("episode must be a non-negative integer")
        return root, episode

    def start_gripper_preview_estimate(self, value: Any) -> dict[str, Any]:
        root, episode = self._parse_gripper_estimation_request(value)
        # Set the manager's running state while the same RLock that guards web
        # and pedal capture starts is held. The worker never owns this RLock.
        with self.capture_postprocess_lock:
            self._require_postprocess_idle()
            return self.gripper_estimation_job.start(root, episode)

    def gripper_preview_estimate_status(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("invalid gripper estimation status request")
        job_id = str(value.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("job_id is required")
        status = self.gripper_estimation_job.status(
            job_id,
            include_preview=False,
            include_logs=True,
        )
        if status["status"] == "succeeded":
            return self.gripper_estimation_job.result(job_id)
        return status

    def _require_postprocess_idle(self) -> None:
        if self.session.running or self.session.starting or self.session.stopping:
            raise ValueError("finish the active capture before using post-processing tools")

    def action(self, action: str, value: Any = "") -> str:
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("InstaDataCap is shutting down; no new action was started")
        timestamp = InputTimestamp.now(f"web_{action}")
        camera_action_aliases = {
            "scan_insta": "scan_insta",
            "scan_cameras": "scan_insta",
            "connect_insta": "connect_insta",
            "connect_cameras": "connect_insta",
            "disconnect_insta": "disconnect_insta",
            "disconnect_cameras": "disconnect_insta",
        }
        if action in camera_action_aliases:
            return self._start_device_action(camera_action_aliases[action])
        if action == "set_camera_transport":
            requested = value.get("mode") if isinstance(value, dict) else value
            return self._set_camera_transport(str(requested))
        if action == "set_camera_view_mode":
            with self.camera_adapter_lock:
                if not isinstance(self.insta_adapter, UsbWebcamInstaAdapter):
                    return "Camera view mode is available only in USB Webcam mode"
                insta_adapter = self.insta_adapter
                requested = value.get("view_mode") if isinstance(value, dict) else value
            # Switching the native UVC profile stops and reopens both streams.
            # Do not hold camera_adapter_lock while waiting for hardware; status
            # and the managed-window health probe must remain responsive.
            result = insta_adapter.set_view_mode(str(requested))
            if not result.ok:
                raise RuntimeError(result.message)
            with self.camera_adapter_lock:
                if self.insta_adapter is insta_adapter:
                    self.config["insta"]["view_mode"] = insta_adapter.view_mode
            return result.message
        if action == "connect_tracker":
            return self._connect_tracker_monitor()
        if action == "disconnect_tracker":
            return self._disconnect_tracker_monitor()
        if action == "recover_tracker":
            return self._recover_tracker_monitor()
        if action == "release_other_tracker":
            return self._release_other_tracker_service()
        if action == "start":
            if not self.capture_postprocess_lock.acquire(blocking=False):
                return "Post-processing is busy; capture was not started"
            try:
                if self.gripper_estimation_job.running:
                    return "Gripper estimation is busy; capture was not started"
                with self.device_action_lock:
                    if self.device_action.get("running"):
                        return "Camera action is still running; capture was not started"
                if self.offline_annotations.active:
                    self.offline_annotations.close()
                message = self.session.start_total(timestamp)
            finally:
                self.capture_postprocess_lock.release()
            if not self.session.running:
                self._start_tracker_monitor_async()
            return message
        if action == "end":
            message = self.session.end_total(timestamp)
            self._start_tracker_monitor_async()
            return message
        if action == "slam":
            return self.session.add_slam(timestamp)
        if action == "segment":
            return self.session.toggle_segment(timestamp)
        if action == "undo_timestamp":
            return self.session.undo_last_timestamp(timestamp)
        if action == "delete_slam_timestamp":
            return self.session.delete_slam_timestamp(str(value), timestamp)
        if action == "clear_timestamps":
            return self.session.clear_timestamps(timestamp)
        if action == "add_target_annotation":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.add(value, timestamp)
        if action == "delete_target_annotation":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.delete(str(value), timestamp)
        if action == "clear_target_annotations":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                camera_role = str(value).strip() or None
                return self.offline_annotations.clear_current_episode(timestamp, camera_role)
        if action == "open_offline_annotation_workspace":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.open_workspace(str(value))
        if action == "close_offline_annotation_workspace":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.close()
        if action == "navigate_offline_annotation":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.navigate(value)
        if action == "complete_offline_annotation_episode":
            with self.capture_postprocess_lock:
                self._require_postprocess_idle()
                return self.offline_annotations.complete_current_episode(timestamp)
        if action == "capture_settings":
            # Serialize the session/settings snapshot with camera transport
            # publication. Otherwise a concurrent settings request can read
            # the old adapter, then overwrite a just-persisted transport choice.
            with self.capture_postprocess_lock:
                if self.session.running or self.session.starting or self.session.stopping:
                    return "Task name and output path are locked while capturing"
                if not isinstance(value, dict):
                    raise ValueError("Invalid capture settings payload")
                message = self.session.set_capture_settings(
                    str(value.get("task_name", "")),
                    str(value.get("output_root", "")),
                )
                self._save_runtime_settings()
                return message
        if action == "save_tracker_view":
            with self.capture_postprocess_lock:
                normalized = self._normalize_tracker_view(value)
                if normalized is None:
                    raise ValueError("Invalid HTC 3D view settings")
                self.ui_tracker_view = normalized
                self._save_runtime_settings()
                return "HTC 3D view saved"
        if action == "new_capture_task":
            with self.capture_postprocess_lock:
                message = self.session.start_new_capture_task()
                self.last_pedal_message = ""
                return message
        if action == "mode":
            return self.session.set_mode(str(value))
        if action == "arm_mode":
            with self.capture_postprocess_lock:
                if self.session.running or self.session.starting or self.session.stopping:
                    return "Arm mode is locked while capturing"
                with self.device_action_lock:
                    if self.device_action.get("running"):
                        return "Capture mode is locked while a camera action is running"
                if value not in {"single_arm", "dual_arm"}:
                    return "Unknown capture mode"
                if self.tracker_adapter is None:
                    return "HTC Tracker is disabled"
                with self.session.lock:
                    current_arm_mode = self.session.arm_mode
                if value == current_arm_mode:
                    label = "single-arm" if value == "single_arm" else "dual-arm"
                    return f"Capture mode is already {label}"
                expected_trackers = 1 if value == "single_arm" else 2
                expected_cameras = 1 if value == "single_arm" else 2
                try:
                    self._configure_arm_adapters(value)
                except (ValueError, RuntimeError) as exc:
                    return f"Failed to change capture mode: {exc}"
                self.last_pedal_message = ""
                return self.session.set_arm_mode(
                    value,
                    expected_trackers,
                    expected_cameras,
                    start_new_task_on_change=True,
                )
        raise ValueError(f"unknown action: {action}")

    def target_reference_image(self, asset_id: str) -> tuple[Path, str] | None:
        return self.offline_annotations.target_reference_image(asset_id)

    def _connect_tracker_monitor(self) -> str:
        """Acquire the HTC receiver while idle without starting a recording."""

        if self.session.running or self.session.starting or self.session.stopping:
            return "HTC Tracker connection is managed by the active capture"
        if self.tracker_adapter is None:
            return "HTC Tracker is disabled"
        with self.tracker_monitor_lock:
            self.tracker_monitor_suspended = False
            current = self.tracker_adapter.status()
            if current.get("connected"):
                return current.get("tracking_label", "HTC Tracker is already connected")
            if self.tracker_monitor_starting:
                return "HTC Tracker connection is already in progress"
            self.tracker_monitor_starting = True
        try:
            result = self._start_tracker_monitor_with_safe_handoff()
            if not result.ok:
                raise RuntimeError(result.message)
            return result.message
        finally:
            with self.tracker_monitor_lock:
                self.tracker_monitor_starting = False

    def _disconnect_tracker_monitor(self) -> str:
        """Release the HTC receiver so omniimu or teleop can acquire it."""

        if self.session.running or self.session.starting or self.session.stopping:
            return "HTC Tracker cannot be released while capturing"
        if self.tracker_adapter is None:
            return "HTC Tracker is disabled"
        with self.tracker_monitor_lock:
            # Publish suspension before close(), so a concurrent maintenance
            # pass cannot reopen the receiver after this request returns.
            self.tracker_monitor_suspended = True
        self.tracker_adapter.close()
        return "HTC Tracker receiver released and monitor suspended"

    def _recover_tracker_monitor(self) -> str:
        """Explicitly reopen RF receive/pairing without clearing stored identities."""

        if self.session.running or self.session.starting or self.session.stopping:
            return "HTC Tracker cannot be reconnected while capturing"
        if self.tracker_adapter is None:
            return "HTC Tracker is disabled"
        with self.tracker_monitor_lock:
            self.tracker_monitor_suspended = False
            if self.tracker_monitor_starting:
                return "HTC Tracker recovery is already in progress"
            self.tracker_monitor_starting = True
        try:
            result = self.tracker_adapter.recover_monitor(
                Path("/tmp/instaumi_datacap_tracker_monitor"),
                enable_pairing=True,
            )
            if not result.ok:
                raise RuntimeError(result.message)
            return (
                f"{result.message}; stored pairing identities were not cleared. "
                "Wake and move both Trackers while RF reconnect is active."
            )
        finally:
            with self.tracker_monitor_lock:
                self.tracker_monitor_starting = False

    def _release_other_tracker_service(self) -> str:
        """Terminate the exact external process holding HTC, then acquire it."""

        if self.session.running or self.session.starting or self.session.stopping:
            return "Other HTC services cannot be stopped while DataCap is capturing"
        if self.tracker_adapter is None:
            return "HTC Tracker is disabled"
        with self.tracker_monitor_lock:
            self.tracker_monitor_suspended = False
            current = self.tracker_adapter.status()
            if current.get("connected"):
                return "HTC is already owned by InstaDataCap"
            if self.tracker_monitor_starting:
                return "HTC connection is already in progress; try again in a moment"
            self.tracker_monitor_starting = True
        try:
            holder = self._htc_lock_holder()
            if holder is None:
                released = "No other process currently owns HTC"
            else:
                owner = str(holder.get("owner", "unknown HTC service"))
                pid = self._validated_htc_holder_pid(holder)
                own_process = self.tracker_adapter.process
                if own_process is not None and own_process.pid == pid:
                    self.tracker_adapter.close()
                    if not self._wait_for_htc_release(pid, 3.0):
                        raise RuntimeError("InstaDataCap's stale HTC worker did not exit")
                    released = f"Restarted InstaDataCap HTC worker (PID {pid})"
                elif owner == "omniimu":
                    self._shutdown_omniimu_for_manual_handoff(pid)
                    released = f"Stopped {owner} (PID {pid})"
                else:
                    self._terminate_htc_holder(pid, holder)
                    released = f"Stopped {owner} (PID {pid})"

            result = self.tracker_adapter.start_monitor(
                Path("/tmp/instaumi_datacap_tracker_monitor")
            )
            if not result.ok:
                raise RuntimeError(
                    f"{released}, but InstaDataCap could not acquire HTC: {result.message}"
                )
            return f"{released}; {result.message}"
        finally:
            with self.tracker_monitor_lock:
                self.tracker_monitor_starting = False

    def _htc_lock_holder(self) -> dict[str, Any] | None:
        """Return live lock metadata, ignoring stale contents in an unlocked file."""

        if not self.htc_lock_path.exists():
            return None
        try:
            fd = os.open(self.htc_lock_path, os.O_RDWR)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
                try:
                    holder = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "HTC is locked but the owner metadata is invalid; refusing to guess a PID"
                    ) from exc
                if not isinstance(holder, dict):
                    raise RuntimeError("HTC lock owner metadata is not an object")
                return holder
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return None
        finally:
            os.close(fd)

    @staticmethod
    def _validated_htc_holder_pid(holder: dict[str, Any]) -> int:
        """Validate lock PID, same-user ownership and an HTC-specific command line."""

        try:
            pid = int(holder.get("pid", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("HTC lock does not contain a valid owner PID") from exc
        if pid <= 1 or pid == os.getpid():
            raise RuntimeError(f"Refusing unsafe HTC owner PID {pid}")
        proc_dir = Path("/proc") / str(pid)
        try:
            status = (proc_dir / "status").read_text(encoding="utf-8", errors="replace")
            cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError as exc:
            raise RuntimeError(f"HTC owner PID {pid} is no longer running") from exc
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        try:
            real_uid = int(uid_line.split()[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Cannot verify the user owning HTC PID {pid}") from exc
        if real_uid != os.getuid():
            raise RuntimeError(
                f"HTC PID {pid} belongs to UID {real_uid}; InstaDataCap will not terminate it"
            )
        allowed_commands = (
            "tracker_recorder.py",
            "htc_worker.py",
            "htc_tracker.py",
            "local_htc_pose_worker.py",
        )
        if not any(marker in cmdline for marker in allowed_commands):
            raise RuntimeError(
                f"PID {pid} holds the HTC lock but is not a recognized HTC worker; refusing termination"
            )
        return pid

    def _wait_for_htc_release(self, holder_pid: int, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self._htc_lock_holder() is None:
                return True
            if not (Path("/proc") / str(holder_pid)).exists():
                time.sleep(0.1)
            else:
                time.sleep(0.1)
        return self._htc_lock_holder() is None

    def _shutdown_omniimu_for_manual_handoff(self, holder_pid: int) -> None:
        """Use OmniIMU's graceful app shutdown before considering a worker signal."""

        try:
            response = self._read_json_url(self.omniimu_shutdown_url, method="POST")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Could not request OmniIMU safe shutdown: {exc}") from exc
        if not response.get("ok"):
            raise RuntimeError(f"OmniIMU rejected safe shutdown: {response}")
        if not self._wait_for_htc_release(holder_pid, 10.0):
            raise RuntimeError("OmniIMU did not release HTC within 10 seconds")

    def _terminate_htc_holder(self, pid: int, original_holder: dict[str, Any]) -> None:
        """SIGTERM, then SIGKILL only the same verified lock holder if necessary."""

        os.kill(pid, signal.SIGTERM)
        if self._wait_for_htc_release(pid, 5.0):
            return
        current = self._htc_lock_holder()
        if current is None:
            return
        if int(current.get("pid", 0)) != pid or current.get("owner") != original_holder.get("owner"):
            raise RuntimeError("HTC ownership changed while waiting; refusing to terminate the new owner")
        os.kill(pid, signal.SIGKILL)
        if not self._wait_for_htc_release(pid, 2.0):
            raise RuntimeError(f"HTC worker PID {pid} did not release the receiver")

    @staticmethod
    def _camera_roles_for_arm_mode(mode: str) -> list[str]:
        return ["insta_right"] if mode == "single_arm" else ["insta_right", "insta_left"]

    @staticmethod
    def _configure_insta_capture_roles(adapter: InstaAdapter | None, mode: str) -> None:
        camera_roles = CaptureRuntime._camera_roles_for_arm_mode(mode)
        if isinstance(
            adapter,
            (BlueZDirectInstaAdapter, UsbWebcamInstaAdapter, MvsCameraAdapter),
        ):
            adapter.set_capture_roles(camera_roles)

    def _configure_arm_adapters(self, mode: str) -> None:
        tracker_roles = ["htc_right"] if mode == "single_arm" else ["htc_right", "htc_left"]
        with self.camera_adapter_lock:
            self._configure_insta_capture_roles(self.insta_adapter, mode)
        if self.tracker_adapter is not None:
            self.tracker_adapter.set_capture_roles(tracker_roles)

    @staticmethod
    def _normalize_tracker_view(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        try:
            azimuth = float(value["azimuth"])
            elevation = float(value["elevation"])
            distance = float(value["distance"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(item) for item in (azimuth, elevation, distance)):
            return None
        return {
            "version": 1,
            "azimuth": azimuth,
            "elevation": max(-1.2, min(1.2, elevation)),
            "distance": max(1.1, min(12.0, distance)),
            "locked": bool(value.get("locked", False)),
        }

    def _save_runtime_settings(self, *, camera_transport: str | None = None) -> None:
        if camera_transport is None:
            with self.camera_adapter_lock:
                selected_transport = self._camera_transport_mode(self.insta_adapter)
        else:
            selected_transport = camera_transport
        payload = {
            "version": 1,
            "task_name": self.session.task_name,
            "output_root": str(self.session.output_root),
            "ui_tracker_view": dict(self.ui_tracker_view) if self.ui_tracker_view else None,
            "camera_transport": selected_transport,
        }
        with self.runtime_settings_lock:
            RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = RUNTIME_SETTINGS_PATH.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(RUNTIME_SETTINGS_PATH)

    @staticmethod
    def _camera_transport_mode(adapter: InstaAdapter | None) -> str:
        if isinstance(adapter, UsbWebcamInstaAdapter):
            return "usb_webcam"
        if isinstance(adapter, MvsCameraAdapter):
            return "mvs"
        if isinstance(adapter, BlueZDirectInstaAdapter):
            return "bluez_direct"
        if isinstance(adapter, BlueZRemoteInstaAdapter):
            return "bluez_remote"
        if isinstance(adapter, CameraSDKInstaAdapter):
            return "camera_sdk"
        if isinstance(adapter, DryRunInstaAdapter):
            return "dry_run"
        return "unknown"

    def _set_camera_transport(self, requested: str) -> str:
        if self.stop_event.is_set():
            raise RuntimeError(
                "InstaDataCap is shutting down; camera connection was not changed"
            )
        target = str(requested).strip().lower()
        if target not in SWITCHABLE_CAMERA_TRANSPORTS:
            raise ValueError(
                "Camera connection must be 'bluez_direct', 'usb_webcam', or 'mvs'"
            )

        with self.capture_postprocess_lock:
            if self.session.running or self.session.starting or self.session.stopping:
                raise RuntimeError("Camera connection is locked while capturing")
            with self.camera_adapter_lock:
                current_adapter = self.insta_adapter
                current = self._camera_transport_mode(current_adapter)
            if current == target:
                label = {
                    "bluez_direct": "Bluetooth Control",
                    "usb_webcam": "Wired USB",
                    "mvs": "MVS Cameras",
                }[target]
                return f"Camera connection is already {label}"
            if current not in SWITCHABLE_CAMERA_TRANSPORTS:
                raise RuntimeError(
                    f"Runtime camera connection {current!r} cannot be changed from the UI"
                )

            with self.device_action_lock:
                if self.device_action.get("running"):
                    raise RuntimeError(
                        f"Camera action in progress: {self.device_action.get('message', '')}"
                    )
                started_ns = time.time_ns()
                self.device_action = {
                    "running": True,
                    "action": "set_camera_transport",
                    "message": f"Switching camera connection to {target}",
                    "ok": None,
                    "started_unix_ns": started_ns,
                }

            candidate: InstaAdapter | None = None
            previous: InstaAdapter | None = None
            settings_snapshot: bytes | None = None
            settings_existed = False
            settings_committed = False
            published = False
            ok = False
            message = f"Camera connection switch to {target} did not complete"
            try:
                candidate = self._create_insta_adapter(target)
                with self.session.lock:
                    arm_mode = self.session.arm_mode
                self._configure_insta_capture_roles(candidate, arm_mode)

                validation: AdapterResult | None = None
                if isinstance(candidate, UsbWebcamInstaAdapter):
                    if not candidate.worker_path.is_file() or not os.access(
                        candidate.worker_path, os.X_OK
                    ):
                        raise RuntimeError(
                            f"USB webcam worker is not executable: {candidate.worker_path}"
                        )
                    # scan_cameras() performs stat/identity checks only. It must
                    # never open a V4L2 node or start a preview worker.
                    validation = candidate.scan_cameras()
                    if not validation.ok:
                        raise RuntimeError(validation.message)
                elif isinstance(candidate, MvsCameraAdapter):
                    # Selecting MVS validates the complete software/runtime
                    # contract but leaves hardware enumeration to the explicit
                    # Scan action, matching Bluetooth's idle selection model.
                    candidate._validate_runtime()

                # Snapshot persistence before the commit. A rare publication
                # failure must not leave the next app start selecting a
                # transport that was never made live in this process.
                with self.runtime_settings_lock:
                    settings_existed = RUNTIME_SETTINGS_PATH.is_file()
                    if settings_existed:
                        settings_snapshot = RUNTIME_SETTINGS_PATH.read_bytes()

                # Commit persistence before publishing the candidate. A normal
                # settings-write failure therefore leaves every live reference
                # and the previous finished-session boundary untouched.
                self._save_runtime_settings(camera_transport=target)
                settings_committed = True
                with self.camera_adapter_lock:
                    previous = self.adapter.replace_adapter("insta", candidate)
                    try:
                        self.insta_adapter = candidate
                        self.config["insta"]["mode"] = target
                        published = True
                    except Exception:
                        # Keep all live references coherent even if an unusual
                        # custom config mapping rejects its update.
                        self.adapter.replace_adapter("insta", previous)
                        self.insta_adapter = current_adapter
                        self.config["insta"]["mode"] = current
                        raise
                candidate = None

                close_warning = ""
                if previous is not None:
                    try:
                        previous.close()
                    except Exception as exc:
                        close_warning = (
                            f"; previous adapter cleanup warning: {type(exc).__name__}: {exc}"
                        )

                boundary_message = ""
                if self.session.session_dir is not None:
                    try:
                        boundary_message = "; " + self.session.start_new_capture_task()
                    except Exception as exc:
                        boundary_message = (
                            "; finished-session boundary warning: "
                            f"{type(exc).__name__}: {exc}"
                        )
                label = {
                    "bluez_direct": "Bluetooth Control",
                    "usb_webcam": "Wired USB",
                    "mvs": "MVS Cameras",
                }[target]
                detail = (
                    "; stable USB paths validated without opening camera streams"
                    if validation is not None
                    else "; Bluetooth worker remains stopped until Scan/Connect"
                    if target == "bluez_direct"
                    else "; MVS workers remain stopped until Scan/Connect"
                )
                message = f"Camera connection switched to {label}{detail}{boundary_message}{close_warning}"
                ok = True
                return message
            except Exception as exc:
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        pass
                if settings_committed and not published:
                    try:
                        with self.runtime_settings_lock:
                            if settings_existed and settings_snapshot is not None:
                                temporary = RUNTIME_SETTINGS_PATH.with_suffix(
                                    ".json.rollback.tmp"
                                )
                                temporary.write_bytes(settings_snapshot)
                                temporary.replace(RUNTIME_SETTINGS_PATH)
                            else:
                                RUNTIME_SETTINGS_PATH.unlink(missing_ok=True)
                    except Exception as restore_exc:
                        exc = RuntimeError(
                            f"{exc}; failed to restore runtime settings: "
                            f"{type(restore_exc).__name__}: {restore_exc}"
                        )
                ok = False
                message = f"Camera connection switch failed: {exc}"
                raise RuntimeError(message) from exc
            finally:
                finished_ns = time.time_ns()
                with self.device_action_lock:
                    self.device_action = {
                        "running": False,
                        "action": "set_camera_transport",
                        "message": message,
                        "ok": ok,
                        "finished_unix_ns": finished_ns,
                    }
                    self.device_logs.append(
                        {
                            "action": "set_camera_transport",
                            "message": message,
                            "ok": ok,
                            "unix_time_ns": finished_ns,
                        }
                    )
                self.last_pedal_message = message

    def _start_device_action(self, action: str) -> str:
        with self.capture_postprocess_lock:
            if self.stop_event.is_set():
                return "InstaDataCap is shutting down; no camera action was started"
            if self.session.running or self.session.starting or self.session.stopping:
                return "Cameras cannot be scanned or connected while capturing"
            with self.camera_adapter_lock:
                insta_adapter = self.insta_adapter
                if insta_adapter is None:
                    return "No Insta camera adapter is configured"
                is_usb = isinstance(insta_adapter, UsbWebcamInstaAdapter)
                is_mvs = isinstance(insta_adapter, MvsCameraAdapter)
            labels = {
                "scan_insta": (
                    "MVS SDK enumeration" if is_mvs else
                    "USB camera refresh" if is_usb else "Bluetooth scan"
                ),
                "connect_insta": (
                    "MVS preview start" if is_mvs else
                    "USB preview start" if is_usb else "Camera connection"
                ),
                "disconnect_insta": (
                    "MVS preview stop" if is_mvs else
                    "USB preview stop" if is_usb else "Camera disconnection"
                ),
            }
            label = labels[action]
            with self.device_action_lock:
                if self.device_action.get("running"):
                    return f"Camera action in progress: {self.device_action.get('message', '')}"
                started_ns = time.time_ns()
                self.device_action = {
                    "running": True,
                    "action": action,
                    "message": f"{label} in progress",
                    "ok": None,
                    "started_unix_ns": started_ns,
                }
                self.device_logs.append(
                    {
                        "action": action,
                        "message": f"Started {label.lower()}",
                        "ok": True,
                        "unix_time_ns": started_ns,
                    }
                )
                action_thread = threading.Thread(
                    target=self._run_device_action,
                    args=(action, label),
                    name=f"Datacap-{action}",
                    daemon=True,
                )
                self.device_action_thread = action_thread
                try:
                    action_thread.start()
                except Exception as exc:
                    self.device_action_thread = None
                    failed_ns = time.time_ns()
                    message = (
                        f"{label} failed to start: {type(exc).__name__}: {exc}"
                    )
                    self.device_action = {
                        "running": False,
                        "action": action,
                        "message": message,
                        "ok": False,
                        "finished_unix_ns": failed_ns,
                    }
                    self.device_logs.append(
                        {
                            "action": action,
                            "message": message,
                            "ok": False,
                            "unix_time_ns": failed_ns,
                        }
                    )
                    self.last_pedal_message = message
                    raise RuntimeError(message) from exc
        self.last_pedal_message = f"{label} started; see the camera action log"
        return self.last_pedal_message

    def _run_device_action(self, action: str, label: str) -> None:
        try:
            with self.camera_adapter_lock:
                insta_adapter = self.insta_adapter
            if insta_adapter is None:
                raise RuntimeError("No Insta camera adapter is configured")
            # device_action.running prevents a transport swap until this call
            # completes. Do not hold camera_adapter_lock across a potentially
            # slow BLE/UVC operation; readers that only need the stable runtime
            # adapter reference should not queue behind the whole operation.
            if action == "scan_insta":
                result = insta_adapter.scan_cameras()
            elif action == "connect_insta":
                result = insta_adapter.connect_cameras()
            elif isinstance(insta_adapter, (UsbWebcamInstaAdapter, MvsCameraAdapter)):
                result = insta_adapter.disconnect_cameras()
            else:
                raise RuntimeError("The configured camera adapter cannot be disconnected here")
            ok = result.ok
            message = result.message
        except Exception as exc:
            ok = False
            message = f"{label} failed: {type(exc).__name__}: {exc}"
        finished_ns = time.time_ns()
        with self.device_action_lock:
            self.device_action = {
                "running": False,
                "action": action,
                "message": message,
                "ok": ok,
                "finished_unix_ns": finished_ns,
            }
            self.device_logs.append(
                {
                    "action": action,
                    "message": message,
                    "ok": ok,
                    "unix_time_ns": finished_ns,
                }
            )
            if self.device_action_thread is threading.current_thread():
                self.device_action_thread = None
        self.last_pedal_message = message

    def _start_tracker_monitor_async(self) -> None:
        if (
            not self.monitor_tracker_while_idle
            or self.tracker_adapter is None
            or self.session.running
            or self.session.starting
            or self.session.stopping
        ):
            return
        with self.tracker_monitor_lock:
            if self.tracker_monitor_suspended or self.tracker_monitor_starting:
                return
            current = self.tracker_adapter.status()
            if current.get("connected"):
                return
            self.tracker_monitor_starting = True
        threading.Thread(
            target=self._run_tracker_monitor,
            name="Datacap-TrackerMonitor",
            daemon=True,
        ).start()

    def _maintain_tracker_monitor(self) -> None:
        """Probe the live HID loop and recover only a failed receiver worker.

        A healthy receiver with zero RF trackers is deliberately left alone:
        powered-off/sleeping trackers must not trigger USB reset or an implicit
        pairing operation.  Pairing recovery remains an explicit UI action.
        """

        if (
            not self.monitor_tracker_while_idle
            or self.tracker_adapter is None
            or self.tracker_monitor_suspended
            or self.session.running
            or self.session.starting
            or self.session.stopping
        ):
            return
        current = self.tracker_adapter.status()
        if current.get("receiver_open"):
            current = self.tracker_adapter.probe_health(timeout_sec=1.5)
        if current.get("connected"):
            return
        if current.get("tracking_state") == "receiver_error":
            now = time.monotonic()
            if (
                now - self.tracker_last_health_restart_monotonic
                < self.tracker_health_restart_cooldown_sec
            ):
                return
            self.tracker_last_health_restart_monotonic = now
            print(
                "[tracker-monitor] unhealthy VIVE HID polling loop; "
                "restarting the DataCap worker without USB reset",
                flush=True,
            )
        self._start_tracker_monitor_async()

    def _run_tracker_monitor(self) -> None:
        try:
            with self.tracker_monitor_lock:
                suspended_before_start = self.tracker_monitor_suspended
            if (
                self.tracker_adapter is not None
                and not self.session.running
                and not suspended_before_start
            ):
                result = self._start_tracker_monitor_with_safe_handoff()
                with self.tracker_monitor_lock:
                    suspended_after_start = self.tracker_monitor_suspended
                if suspended_after_start:
                    # disconnect_tracker may have raced a monitor startup that
                    # was already in progress. Close the late winner before
                    # acknowledging the external ownership lease.
                    self.tracker_adapter.close()
                    result = AdapterResult(
                        True,
                        "HTC monitor startup cancelled by external ownership lease",
                    )
                print(f"[tracker-monitor] {result.message}", flush=True)
        finally:
            with self.tracker_monitor_lock:
                self.tracker_monitor_starting = False

    def _start_tracker_monitor_with_safe_handoff(self) -> AdapterResult:
        """Acquire HTC, safely closing an idle OmniIMU owner when configured."""

        if self.tracker_adapter is None:
            return AdapterResult(False, "HTC Tracker is disabled")
        runtime_dir = Path("/tmp/instaumi_datacap_tracker_monitor")
        result = self.tracker_adapter.start_monitor(runtime_dir)
        if result.ok or not self._release_idle_omniimu_owner(result.message):
            return result
        return self.tracker_adapter.start_monitor(runtime_dir)

    @staticmethod
    def _omniimu_active_reasons(state: dict[str, Any]) -> list[str]:
        """List OmniIMU phases that make automatic shutdown unsafe."""

        sections = {
            "total capture": (state.get("control") or {}, ("active", "armed")),
            "warmup": (state.get("warmup") or {}, ("running",)),
            "rigid calibration": (
                state.get("rigid_alignment") or {},
                ("armed", "capture_active", "capture_pending"),
            ),
            "Flexiv capture": (
                state.get("flexiv_capture") or {},
                ("armed", "recording"),
            ),
            "segment capture": (
                state.get("segment_capture") or {},
                ("recording",),
            ),
        }
        return [
            label
            for label, (section, flags) in sections.items()
            if any(bool(section.get(flag)) for flag in flags)
        ]

    @staticmethod
    def _omniimu_state_is_verifiable(state: dict[str, Any]) -> bool:
        """Require every capture subsystem before interpreting OmniIMU as idle."""

        required_flags = {
            "control": ("active", "armed"),
            "warmup": ("running",),
            "rigid_alignment": ("capture_active", "capture_pending"),
            "flexiv_capture": ("armed", "recording"),
            "segment_capture": ("recording",),
        }
        if state.get("ok") is not True:
            return False
        return all(
            isinstance(state.get(section_name), dict)
            and all(flag in state[section_name] for flag in flags)
            for section_name, flags in required_flags.items()
        )

    @staticmethod
    def _read_json_url(url: str, *, method: str = "GET") -> dict[str, Any]:
        payload = b"{}" if method == "POST" else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=1.5) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("response is not a JSON object")
        return result

    def _release_idle_omniimu_owner(self, tracker_error: str) -> bool:
        """Ask OmniIMU to release HTC only after proving that capture is idle."""

        if (
            not self.auto_release_idle_omniimu
            or "already in use" not in tracker_error
            or '"owner": "omniimu"' not in tracker_error
        ):
            return False
        try:
            state = self._read_json_url(self.omniimu_state_url)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(
                "[tracker-handoff] OmniIMU owns HTC but its state cannot be verified; "
                f"refusing automatic shutdown: {exc}",
                flush=True,
            )
            return False
        if not self._omniimu_state_is_verifiable(state):
            print(
                "[tracker-handoff] OmniIMU state is incomplete; refusing automatic shutdown",
                flush=True,
            )
            return False
        reasons = self._omniimu_active_reasons(state)
        if reasons:
            print(
                "[tracker-handoff] OmniIMU owns HTC and is active "
                f"({', '.join(reasons)}); refusing automatic shutdown",
                flush=True,
            )
            return False
        try:
            response = self._read_json_url(self.omniimu_shutdown_url, method="POST")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"[tracker-handoff] OmniIMU safe shutdown failed: {exc}", flush=True)
            return False
        if not response.get("ok"):
            print(
                f"[tracker-handoff] OmniIMU rejected safe shutdown: {response}",
                flush=True,
            )
            return False

        # The HTTP reply is sent before OmniIMU's worker processes finish.
        # Do not race the next pyvut open against that normal shutdown.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                self._read_json_url(self.omniimu_state_url)
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(0.25)
                print(
                    "[tracker-handoff] idle OmniIMU released HTC; DataCap is acquiring it",
                    flush=True,
                )
                return True
            time.sleep(0.1)
        print(
            "[tracker-handoff] OmniIMU accepted shutdown but did not exit within 8s",
            flush=True,
        )
        return False

    def _create_insta_adapter(self, mode: str | None = None) -> InstaAdapter:
        insta = self.config["insta"]
        insta_mode = str(mode or insta.get("mode", "bluez_direct"))
        if insta_mode == "dry_run":
            return DryRunInstaAdapter()
        elif insta_mode == "bluez_direct":
            return BlueZDirectInstaAdapter(
                worker_path=Path(insta.get("worker", DATACAP_DIR / "insta_ble_direct_worker.py")),
                python_executable=str(insta.get("python", "/usr/bin/python3")),
                adapter=str(insta.get("adapter", "hci0")),
                expected_cameras=int(insta.get("expected_cameras", 1)),
                require_all=bool(insta.get("require_all", True)),
                addresses=[str(item) for item in insta.get("addresses", [])],
                camera_roles=[str(role) for role in insta.get("camera_roles", [])],
                role_by_address={
                    str(address): str(role)
                    for address, role in insta.get("role_by_address", {}).items()
                },
                name_prefixes=[str(item) for item in insta.get("name_prefixes", ["X5 "])],
                ready_timeout_sec=float(insta.get("ready_timeout_sec", 10.0)),
                discovery_timeout_sec=float(insta.get("discovery_timeout_sec", 15.0)),
                command_timeout_sec=float(insta.get("command_timeout_sec", 8.0)),
                ack_timeout_sec=float(insta.get("ack_timeout_sec", 3.0)),
            )
        elif insta_mode == "bluez_remote":
            return BlueZRemoteInstaAdapter(
                worker_path=Path(insta.get("worker", DATACAP_DIR / "insta_ble_worker.py")),
                python_executable=str(insta.get("python", "/usr/bin/python3")),
                adapter=str(insta.get("adapter", "hci0")),
                expected_cameras=int(insta.get("expected_cameras", 2)),
                ready_timeout_sec=float(insta.get("ready_timeout_sec", 10.0)),
                command_timeout_sec=float(insta.get("command_timeout_sec", 5.0)),
                allow_unsubscribed=bool(insta.get("allow_unsubscribed", False)),
            )
        elif insta_mode == "camera_sdk":
            return CameraSDKInstaAdapter(
                worker_path=Path(insta.get("camera_sdk_worker", insta.get("worker", ""))),
                sdk_lib_dir=Path(insta["sdk_lib_dir"]),
                resolution=str(insta.get("resolution", "5.7k60")),
                bitrate_mbps=int(insta.get("bitrate_mbps", 120)),
                serial=str(insta.get("serial", "")),
            )
        elif insta_mode == "usb_webcam":
            raw_devices = insta.get("usb_devices", {})
            if not isinstance(raw_devices, dict):
                raise ValueError("insta.usb_devices must map camera roles to stable device paths")
            return UsbWebcamInstaAdapter(
                devices={str(role): Path(str(device)) for role, device in raw_devices.items()},
                worker_path=Path(
                    insta.get(
                        "usb_webcam_worker",
                        DATACAP_DIR / "camera_usb" / "bin" / "insta_uvc_worker",
                    )
                ),
                preview_dir=Path(
                    insta.get(
                        "usb_preview_dir",
                        Path("/dev/shm") / f"instadatacap-uvc-{os.getuid()}",
                    )
                ),
                view_mode=str(insta.get("view_mode", "panorama")),
                required_camera_model=str(
                    insta.get("required_camera_model", "")
                ).strip()
                or None,
                ready_timeout_sec=float(insta.get("usb_ready_timeout_sec", 15.0)),
                command_timeout_sec=float(insta.get("command_timeout_sec", 8.0)),
                stop_timeout_sec=float(insta.get("usb_stop_timeout_sec", 120.0)),
            )
        elif insta_mode == "mvs":
            raw_serials = insta.get("mvs_serials", {})
            if not isinstance(raw_serials, dict):
                raise ValueError("insta.mvs_serials must map camera roles to MVS serials")
            return MvsCameraAdapter(
                serials={str(role): str(serial) for role, serial in raw_serials.items()},
                worker_path=Path(
                    insta.get(
                        "mvs_worker",
                        DATACAP_DIR / "camera_mvs" / "mvs_worker.py",
                    )
                ),
                python_executable=str(
                    insta.get(
                        "mvs_python",
                        sys.executable,
                    )
                ),
                exumi_root=Path(insta.get("mvs_exumi_root", str(Path.home() / "exumi"))),
                preview_dir=Path(
                    insta.get(
                        "mvs_preview_dir",
                        Path("/dev/shm") / f"instadatacap-mvs-{os.getuid()}",
                    )
                ),
                fps=float(insta.get("mvs_fps", 30.0)),
                exposure_us=int(insta.get("mvs_exposure_us", 20000)),
                width=int(insta.get("mvs_width", 224)),
                height=int(insta.get("mvs_height", 224)),
                raw_width=int(insta.get("mvs_raw_width", 1536)),
                raw_height=int(insta.get("mvs_raw_height", 1536)),
                ready_timeout_sec=float(insta.get("mvs_ready_timeout_sec", 15.0)),
                command_timeout_sec=float(insta.get("command_timeout_sec", 8.0)),
                stop_timeout_sec=float(insta.get("mvs_stop_timeout_sec", 30.0)),
                require_usb3=bool(insta.get("mvs_require_usb3", True)),
            )
        else:
            raise ValueError(
                f"unsupported insta.mode={insta_mode!r}; use mvs, usb_webcam, bluez_direct, "
                "bluez_remote, camera_sdk, or dry_run"
            )

    def _build_adapter(self) -> InstaAdapter:
        adapters: dict[str, InstaAdapter] = {
            "insta": self._create_insta_adapter(),
        }

        tracker = self.config["tracker"]
        if tracker.get("enabled"):
            wifi_info = str(tracker.get("wifi_info_path", "")).strip()
            tracker_adapter = PyVUTTrackerAdapter(
                worker_script=Path(tracker.get("worker", DATACAP_DIR / "tracker_recorder.py")),
                pyvut_root=Path(tracker["pyvut_root"]).expanduser(),
                python_executable=str(tracker.get("python", sys.executable)),
                mode=str(tracker.get("mode", "DONGLE_USB")),
                wifi_info_path=Path(wifi_info).expanduser() if wifi_info else None,
                require_pose=bool(tracker.get("require_pose", True)),
                ready_timeout_sec=float(tracker.get("ready_timeout_sec", 10.0)),
                command_timeout_sec=float(tracker.get("command_timeout_sec", 15.0)),
                simulate=bool(tracker.get("simulate", False)),
                required=bool(tracker.get("required", True)),
                expected_trackers=int(tracker.get("expected_trackers", 1)),
                rate_hz=float(tracker.get("rate_hz", 60.0)),
                simulated_trackers=int(tracker.get("simulated_trackers", 2)),
                roles=[str(role) for role in tracker.get("roles", [])],
                role_by_mac={
                    str(mac): str(role)
                    for mac, role in tracker.get("role_by_mac", {}).items()
                },
                role_config_path=Path(
                    tracker.get(
                        "role_config_path",
                        Path.home() / ".config" / "htc" / "roles.json",
                    )
                ),
                independent_slam=bool(tracker.get("independent_slam", True)),
                preserve_existing_connection=bool(
                    tracker.get("preserve_existing_connection", True)
                ),
                release_on_stop=bool(tracker.get("release_on_stop", True)),
            )
            adapters["tracker"] = tracker_adapter
            self.tracker_adapter = tracker_adapter
        self.insta_adapter = adapters.get("insta")
        return CompositeCaptureAdapter(adapters)

    def _on_pedal(self, event: Any) -> None:
        source = "linux_usb_pedal_long" if event.kind == "long" else "linux_usb_pedal"
        timestamp = InputTimestamp.from_pedal(event.monotonic_time, source=source)
        if not self.capture_postprocess_lock.acquire(blocking=False):
            result = "Post-processing is busy; pedal input ignored and capture was not changed"
        else:
            try:
                if self.gripper_estimation_job.running:
                    result = (
                        "Gripper estimation is busy; pedal input ignored and "
                        "capture was not changed"
                    )
                    self.last_pedal_message = result
                    print(f"[pedal:{event.kind}] {result}", flush=True)
                    return
                if event.kind == "long":
                    result = self.session.handle_pedal_long_press(timestamp)
                else:
                    result = self.session.handle_pedal_press(timestamp)
                if self.session.running and self.offline_annotations.active:
                    self.offline_annotations.close()
            finally:
                self.capture_postprocess_lock.release()
        self.last_pedal_message = result
        if not self.session.running and "failed to start" in result:
            self._start_tracker_monitor_async()
        print(f"[pedal:{event.kind}] {result}", flush=True)

    def _on_pedal_status(self, status: dict[str, Any]) -> None:
        self.pedal_status = status
        print(f"[pedal] {status['message']}: {status['device']}", flush=True)


class DatacapHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    runtime: CaptureRuntime
    static_dir: Path


class RequestHandler(BaseHTTPRequestHandler):
    server: DatacapHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        """Keep 10-20 Hz polling requests out of the persistent service log."""

        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json({"ok": True, "status": self.server.runtime.status()})
            return
        if path == "/api/tracker-status":
            self._send_json({"ok": True, "tracker": self.server.runtime.tracker_status()})
            return
        if path == "/api/pedal-status":
            self._send_json({"ok": True, "pedal": self.server.runtime.pedal_live_status()})
            return
        if path.startswith("/api/camera-preview/") and path.endswith(".jpg"):
            role = path[len("/api/camera-preview/") : -len(".jpg")]
            if role not in {"insta_right", "insta_left"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            result = self.server.runtime.camera_preview(role)
            if result is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload, content_type = result
            self._send_bytes(payload, content_type)
            return
        if path.startswith("/api/annotation-frame/"):
            asset_id = path.rsplit("/", 1)[-1]
            if len(asset_id) != 64 or any(char not in "0123456789abcdef" for char in asset_id):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            result = self.server.runtime.target_reference_image(asset_id)
            if result is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target, content_type = result
            self._send_bytes(target.read_bytes(), content_type)
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (self.server.static_dir / relative).resolve()
        if self.server.static_dir not in target.parents and target != self.server.static_dir:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/shutdown":
            self._send_json({"ok": True, "message": "InstaDataCap is closing"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/api/gripper-preview":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64 * 1024:
                    raise ValueError("Gripper preview request must be between 1 byte and 64 KiB")
                body = json.loads(self.rfile.read(length))
                preview = self.server.runtime.gripper_binary_preview(body)
                self._send_json({"ok": True, "preview": preview})
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/gripper-preview-discover":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64 * 1024:
                    raise ValueError(
                        "Gripper discovery request must be between 1 byte and 64 KiB"
                    )
                body = json.loads(self.rfile.read(length))
                discovery = self.server.runtime.discover_gripper_preview_captures(body)
                self._send_json({"ok": True, "discovery": discovery})
            except Exception as exc:
                self._send_json(
                    {"ok": False, "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        if path in {
            "/api/gripper-preview-estimate/start",
            "/api/gripper-preview-estimate/status",
        }:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64 * 1024:
                    raise ValueError(
                        "Gripper estimation request must be between 1 byte and 64 KiB"
                    )
                body = json.loads(self.rfile.read(length))
                if path.endswith("/start"):
                    job = self.server.runtime.start_gripper_preview_estimate(body)
                    response_status = HTTPStatus.ACCEPTED
                else:
                    job = self.server.runtime.gripper_preview_estimate_status(body)
                    response_status = HTTPStatus.OK
                self._send_json({"ok": True, "job": job}, status=response_status)
            except (GripperEstimationBusyError, GripperEstimationNotReadyError) as exc:
                self._send_json(
                    {"ok": False, "message": str(exc)},
                    status=HTTPStatus.CONFLICT,
                )
            except Exception as exc:
                self._send_json(
                    {"ok": False, "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        if path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                raise ValueError("Action request is too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            message = self.server.runtime.action(str(body.get("action", "")), body.get("value", ""))
            self._send_json({"ok": True, "message": message, "status": self.server.runtime.status()})
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="InstaUMI Ubuntu data capture web app")
    parser.add_argument("--config", type=Path, help="datacap JSON config")
    parser.add_argument("--dry-run", action="store_true", help="simulate Insta and pyvut")
    parser.add_argument("--host", help="override web listen host")
    parser.add_argument("--port", type=int, help="override web listen port")
    args = parser.parse_args(argv)

    config = load_config(args.config, args.dry_run)
    host = args.host or str(config["web"]["host"])
    port = args.port or int(config["web"]["port"])
    runtime = CaptureRuntime(config)
    runtime.start()
    server = DatacapHTTPServer((host, port), RequestHandler)
    server.runtime = runtime
    server.static_dir = DATACAP_DIR / "static"

    stopping = threading.Event()

    def stop_server(_signum: int, _frame: Any) -> None:
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    print(f"[datacap] web app: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
