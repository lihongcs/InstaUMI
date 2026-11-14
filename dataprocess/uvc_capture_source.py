#!/usr/bin/env python3
"""Fail-closed reader for completed InstaDataCap USB/UVC captures.

This module deliberately models UVC captures as a separate source family.  A
``video.mjpeg`` file is a byte-for-byte concatenation of V4L2 payloads; it is
not an INSV file and individual payloads are not assumed to be standalone JFIF
images.  ``frames.jsonl`` is authoritative for packet boundaries and timing.

Validation is pure offline I/O.  It never enumerates or opens a camera device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


COMPLETE_SCHEMA = "instaumi_usb_webcam_capture_complete_v1"
CAPTURE_SCHEMA = "instaumi_usb_webcam_capture_v1"
WORKER_SCHEMA = "instadatacap.usb_webcam.capture.v1"
NORMALIZED_SCHEMA = "instaumi.dataprocess.uvc_source.v1"
WORKER_PROTOCOL = "instadatacap-uvc-worker-v1"
KNOWN_ROLES = ("insta_right", "insta_left")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSONL_ROW_BYTES = 1024 * 1024
MAX_PACKET_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class UvcNativeProfile:
    """One exact, camera-produced UVC pixel profile.

    The profile describes pixels already produced by the camera.  It is not a
    request to crop, stitch, or reproject another profile on the host.
    """

    view_mode: str
    width: int
    height: int
    semantic: str
    projection: str
    layout: str
    kind: str
    source_label: str
    record_panorama_master: bool
    camera_processing: str

    def contract(self) -> dict[str, Any]:
        return {
            "view_mode": self.view_mode,
            "width": self.width,
            "height": self.height,
            "pixel_format": "MJPG",
            "fps": 30,
            "semantic": self.semantic,
            "projection": self.projection,
            "layout": self.layout,
            "kind": self.kind,
            "compression": "lossy_camera_mjpeg",
        }


PANORAMA_PROFILE = UvcNativeProfile(
    view_mode="panorama",
    width=2880,
    height=1440,
    semantic="in_camera_2to1_panorama",
    projection="equirectangular_2to1",
    layout="stitched_2to1",
    kind="uvc_equirectangular_mjpeg",
    source_label="2880x1440 MJPG30 native_panorama",
    record_panorama_master=True,
    camera_processing="in_camera_stitch_and_stabilization_baked_into_pixels",
)
FLAT_PROFILE = UvcNativeProfile(
    view_mode="flat",
    width=1920,
    height=1080,
    semantic="in_camera_native_flat",
    projection="native_flat_profile",
    layout="dual_fisheye_vertical_stack",
    kind="uvc_native_flat_mjpeg",
    source_label="1920x1080 MJPG30 native_flat",
    record_panorama_master=False,
    camera_processing=(
        "in_camera_native_usb_profile_baked_into_pixels_no_host_projection"
    ),
)
NATIVE_PROFILES = {
    profile.view_mode: profile for profile in (PANORAMA_PROFILE, FLAT_PROFILE)
}
LEGACY_PANORAMA_SOURCE_LABEL = "2880x1440 MJPG30 panorama_master"
LEGACY_PANORAMA_KIND = "panorama_master"


class UvcCaptureValidationError(ValueError):
    """The advertised UVC completion contract is incomplete or inconsistent."""


@dataclass(frozen=True)
class UvcFrameRecord:
    frame_index: int
    sequence: int
    soe_monotonic_ns: int
    dequeue_monotonic_ns: int
    enqueue_monotonic_ns: int
    record_monotonic_ns: int
    source_interval_ms: float
    payload_bytes: int
    file_offset_bytes: int
    soe_monotonic_raw_ns: int | None = None
    monotonic_to_raw_offset_ns: int | None = None
    monotonic_to_raw_error_bound_ns: int | None = None

    @property
    def has_monotonic_raw_mapping(self) -> bool:
        return self.soe_monotonic_raw_ns is not None

    @property
    def preferred_timestamp_ns(self) -> int:
        return (
            self.soe_monotonic_raw_ns
            if self.soe_monotonic_raw_ns is not None
            else self.soe_monotonic_ns
        )


@dataclass(frozen=True)
class UvcFramePacket:
    role: str
    record: UvcFrameRecord
    payload: bytes


DecoderHook = Callable[[str, UvcFrameRecord, bytes], Any]


@dataclass(frozen=True)
class UvcRoleSource:
    """One validated role and its authoritative packet table."""

    session_dir: Path
    role: str
    manifest_path: Path
    video_path: Path
    frames_path: Path
    records: tuple[UvcFrameRecord, ...]
    packet_sha256: tuple[str, ...]
    video_sha256: str
    frames_sha256: str
    worker_manifest_sha256: str

    @property
    def timeline_path(self) -> Path:
        """Path to the authoritative SOE/offset timeline."""

        return self.frames_path

    def read_packet(self, frame_index: int) -> UvcFramePacket:
        """Read one packet by validated index and verify its derived digest."""

        if isinstance(frame_index, bool) or not isinstance(frame_index, int):
            raise TypeError("frame_index must be an integer")
        if frame_index < 0 or frame_index >= len(self.records):
            raise IndexError(
                f"{self.role} frame_index {frame_index} outside "
                f"[0, {len(self.records)})"
            )
        _require_regular_file(self.video_path, f"{self.role} video.mjpeg")
        expected_size = sum(record.payload_bytes for record in self.records)
        if self.video_path.stat().st_size != expected_size:
            raise UvcCaptureValidationError(
                f"{self.role}: video.mjpeg size changed after validation"
            )
        record = self.records[frame_index]
        with self.video_path.open("rb") as stream:
            stream.seek(record.file_offset_bytes)
            payload = stream.read(record.payload_bytes)
        if len(payload) != record.payload_bytes:
            raise UvcCaptureValidationError(
                f"{self.role}: video.mjpeg truncated while reading frame {frame_index}"
            )
        if hashlib.sha256(payload).hexdigest() != self.packet_sha256[frame_index]:
            raise UvcCaptureValidationError(
                f"{self.role}: frame {frame_index} content changed after validation"
            )
        return UvcFramePacket(self.role, record, payload)

    def iter_packets(self) -> Iterator[UvcFramePacket]:
        """Read packets using validated offsets/sizes, without JPEG heuristics."""

        with self.video_path.open("rb") as stream:
            expected_offset = 0
            digest = hashlib.sha256()
            for record, expected_packet_sha256 in zip(
                self.records, self.packet_sha256, strict=True
            ):
                if record.file_offset_bytes != expected_offset:
                    raise UvcCaptureValidationError(
                        f"{self.role}: in-memory packet table offset changed at "
                        f"frame {record.frame_index}"
                    )
                payload = stream.read(record.payload_bytes)
                if len(payload) != record.payload_bytes:
                    raise UvcCaptureValidationError(
                        f"{self.role}: video.mjpeg truncated while reading frame "
                        f"{record.frame_index}: expected {record.payload_bytes} bytes, "
                        f"got {len(payload)}"
                    )
                digest.update(payload)
                if hashlib.sha256(payload).hexdigest() != expected_packet_sha256:
                    raise UvcCaptureValidationError(
                        f"{self.role}: frame {record.frame_index} content changed "
                        "after validation"
                    )
                expected_offset += len(payload)
                yield UvcFramePacket(self.role, record, payload)
            if stream.read(1):
                raise UvcCaptureValidationError(
                    f"{self.role}: video.mjpeg has bytes beyond the validated packet table"
                )
            if digest.hexdigest() != self.video_sha256:
                raise UvcCaptureValidationError(
                    f"{self.role}: video.mjpeg content changed after validation"
                )


@dataclass(frozen=True)
class UvcCaptureSource:
    """A completely validated single- or dual-role USB capture."""

    session_dir: Path
    roles: Mapping[str, UvcRoleSource]
    normalized_manifest: Mapping[str, Any]

    @property
    def timeline_paths(self) -> Mapping[str, Path]:
        return {role: source.timeline_path for role, source in self.roles.items()}

    @property
    def default_normalized_manifest_path(self) -> Path:
        """Suggested output path; validation itself never creates this file."""

        return self.session_dir / "uvc_source_manifest.json"

    def iter_packets(self, role: str) -> Iterator[UvcFramePacket]:
        try:
            source = self.roles[role]
        except KeyError as exc:
            raise KeyError(f"capture has no validated UVC role {role!r}") from exc
        return source.iter_packets()


def detect_uvc_capture(session_dir: Path | str) -> bool:
    """Return false only when no UVC marker exists; malformed markers fail closed."""

    session = Path(session_dir).expanduser().resolve()
    marker = session / "uvc_capture_complete.json"
    try:
        metadata = os.lstat(marker)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise UvcCaptureValidationError(
            f"UVC completion marker is not a regular file: {marker}"
        )
    payload, _ = _read_json_object(marker, "UVC completion marker")
    if payload.get("schema") != COMPLETE_SCHEMA or payload.get("capture_transport") != "usb_uvc":
        raise UvcCaptureValidationError(
            f"file named uvc_capture_complete.json does not declare {COMPLETE_SCHEMA}"
        )
    return True


def validate_uvc_capture(
    session_dir: Path | str,
    *,
    decoder_hook: DecoderHook | None = None,
) -> UvcCaptureSource:
    """Validate a completed UVC capture and build a deterministic source report.

    ``decoder_hook`` is optional and is invoked as ``hook(role, record,
    payload)`` for every authoritative packet.  The hook must raise or return
    exactly ``False`` to reject a packet.  Payloads are not required to be
    standalone JPEG files.
    """

    session = Path(session_dir).expanduser().resolve()
    _require_directory(session, "capture session")
    _require_directory(session / "uvc", "UVC role root")

    complete_path = session / "uvc_capture_complete.json"
    capture_path = session / "uvc_capture_manifest.json"
    complete, complete_sha256 = _read_json_object(complete_path, "UVC completion marker")
    capture, capture_sha256 = _read_json_object(capture_path, "UVC capture contract")
    profile, legacy_panorama_v1 = _validate_top_level_contracts(complete, capture)

    declared_roles = _parse_roles(complete.get("roles"), "completion marker")
    configured_roles = _parse_roles(capture.get("roles"), "capture contract")
    if declared_roles != configured_roles:
        raise UvcCaptureValidationError(
            "completion marker roles do not match the configured capture roles"
        )

    workers = _object(complete.get("workers"), "completion marker workers")
    if set(workers) != set(declared_roles):
        raise UvcCaptureValidationError(
            "completion marker worker keys do not exactly match declared roles"
        )

    normalized_roles: dict[str, Any] = {}
    role_sources: dict[str, UvcRoleSource] = {}
    for role in declared_roles:
        role_source, normalized = _validate_role(
            session=session,
            role=role,
            completion_worker=_object(workers[role], f"{role} completion worker"),
            decoder_hook=decoder_hook,
            profile=profile,
            allow_legacy_profile=legacy_panorama_v1,
        )
        role_sources[role] = role_source
        normalized_roles[role] = normalized

    canonical_roles = tuple(role for role in KNOWN_ROLES if role in role_sources)
    ordered_sources = {role: role_sources[role] for role in canonical_roles}
    ordered_normalized = {role: normalized_roles[role] for role in canonical_roles}
    pairing = _pairing_quality(ordered_sources)
    primary_timestamps = {
        role: (
            "soe_monotonic_raw_ns"
            if source.records[0].has_monotonic_raw_mapping
            else "soe_monotonic_ns"
        )
        for role, source in ordered_sources.items()
    }
    raw_role_count = sum(
        value == "soe_monotonic_raw_ns" for value in primary_timestamps.values()
    )
    if raw_role_count == len(primary_timestamps):
        raw_coverage = "all_roles"
        clock_confidence = "per_frame_bracketed_MONOTONIC_to_MONOTONIC_RAW_mapping"
    elif raw_role_count == 0:
        raw_coverage = "none_legacy_fallback"
        clock_confidence = "legacy_CLOCK_MONOTONIC_fallback"
    else:
        raw_coverage = "mixed_role_versions"
        clock_confidence = "role_specific_mixed_version_mapping"
    normalized_manifest: dict[str, Any] = {
        "schema": NORMALIZED_SCHEMA,
        "source_family": "usb_uvc",
        "capture_transport": "usb_uvc",
        "complete": True,
        "roles": list(canonical_roles),
        "view_mode": profile.view_mode,
        "semantic": profile.semantic,
        "projection": profile.projection,
        "layout": profile.layout,
        "native_profile_master": True,
        "panorama_master": profile.record_panorama_master,
        "source_profile": profile.contract()
        | {
            "source": profile.source_label,
            "legacy_panorama_v1": legacy_panorama_v1,
        },
        "encoded_stream": {
            "codec": "MJPEG",
            "storage": "verbatim_concatenated_v4l2_payloads",
            "packet_boundaries": "frames.jsonl_offset_and_payload_bytes",
            "standalone_jfif_payloads_required": False,
        },
        "insv_compatibility": {
            "is_insv": False,
            "may_use_insv_reader": False,
            "reason": (
                "UVC pixels contain camera-produced profile processing and no "
                "INSV media/gyro contract"
            ),
        },
        "timing_semantics": {
            "preferred_timestamp_by_role": primary_timestamps,
            "monotonic_raw_coverage": raw_coverage,
            "clock_confidence": clock_confidence,
            "host_monotonic_clock_domain_shared": True,
            "preferred_clock_domain_shared": raw_coverage != "mixed_role_versions",
            "record_monotonic_ns": (
                "writer-thread timestamp immediately before the packet write; "
                "not disk durability time"
            ),
            "measured_latency_components": [
                "soe_to_dequeue_ns",
                "dequeue_to_enqueue_ns",
                "enqueue_to_record_ns",
                "soe_to_record_ns",
            ],
            "unmeasured_latency_components": [
                "optical_exposure_to_v4l2_soe",
                "sensor_and_in_camera_profile_pipeline",
            ],
            "sensor_to_driver_latency": "unknown",
        },
        "source_contract": {
            **profile.contract(),
            "source": profile.source_label,
        },
        "image_quality_semantics": {
            "recording_uses_preview_pixels": False,
            "native_v4l2_payloads_preserved_without_transcode": True,
            "host_reprojection_applied_to_native_source": False,
            "lossy_source_codec": "MJPEG",
            "in_camera_processing_is_reversible": False,
            "missing_insv_detail_or_gyro_can_be_recovered": False,
        },
        "source_markers": {
            "completion": "uvc_capture_complete.json",
            "capture_contract": "uvc_capture_manifest.json",
            "completion_sha256": complete_sha256,
            "capture_contract_sha256": capture_sha256,
            "completed_unix_time_ns": _integer(
                complete.get("completed_unix_time_ns"),
                "completion marker completed_unix_time_ns",
                minimum=1,
            ),
        },
        "validation": {
            "status": "passed",
            "fail_closed": True,
            "all_declared_bytes_partitioned": True,
            "all_sequences_contiguous": True,
            "all_timestamps_monotonic": True,
            "decoder_hook": "all_packets_passed" if decoder_hook else "not_requested",
        },
        "roles_detail": ordered_normalized,
        "cross_role_pairing_quality": pairing,
    }
    return UvcCaptureSource(session, ordered_sources, normalized_manifest)


def write_normalized_manifest(
    source: UvcCaptureSource,
    destination: Path | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically publish the deterministic normalized manifest."""

    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(source.normalized_manifest, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite normalized manifest: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def _validate_top_level_contracts(
    complete: Mapping[str, Any], capture: Mapping[str, Any]
) -> tuple[UvcNativeProfile, bool]:
    if complete.get("schema") != COMPLETE_SCHEMA:
        raise UvcCaptureValidationError(f"completion marker schema must be {COMPLETE_SCHEMA}")
    if complete.get("status") != "complete" or complete.get("complete") is not True:
        raise UvcCaptureValidationError("UVC completion marker is not complete")
    if complete.get("capture_transport") != "usb_uvc":
        raise UvcCaptureValidationError("completion marker capture_transport is not usb_uvc")
    if complete.get("worker_protocol") != WORKER_PROTOCOL:
        raise UvcCaptureValidationError("completion marker worker protocol is unsupported")
    if complete.get("capture_contract") != "uvc_capture_manifest.json":
        raise UvcCaptureValidationError("completion marker points at an unexpected capture contract")

    if capture.get("schema") != CAPTURE_SCHEMA:
        raise UvcCaptureValidationError(f"capture contract schema must be {CAPTURE_SCHEMA}")
    if capture.get("status") != "configured" or capture.get("complete") is not False:
        raise UvcCaptureValidationError("capture contract configuration state is invalid")
    if capture.get("capture_transport") != "usb_uvc":
        raise UvcCaptureValidationError("capture contract capture_transport is not usb_uvc")
    if capture.get("worker_protocol") != WORKER_PROTOCOL:
        raise UvcCaptureValidationError("capture contract worker protocol is unsupported")
    if capture.get("completion_marker") != "uvc_capture_complete.json":
        raise UvcCaptureValidationError("capture contract completion marker is unexpected")
    source_contract = _object(capture.get("source_contract"), "capture source_contract")
    requested_mode = capture.get("requested_view_mode")
    contract_mode = source_contract.get("view_mode")
    mode = contract_mode if contract_mode is not None else requested_mode
    if mode not in NATIVE_PROFILES:
        raise UvcCaptureValidationError(
            "capture source profile view_mode must be 'panorama' or 'flat'"
        )
    profile = NATIVE_PROFILES[str(mode)]
    if requested_mode != profile.view_mode:
        raise UvcCaptureValidationError(
            "capture requested_view_mode disagrees with its native source profile"
        )
    if capture.get("view_mode") not in (None, profile.view_mode):
        raise UvcCaptureValidationError(
            "capture view_mode disagrees with requested_view_mode"
        )

    modern_only_keys = ("view_mode", "semantic", "layout", "compression")
    has_modern_contract = any(key in source_contract for key in modern_only_keys)
    legacy_panorama_v1 = (
        profile is PANORAMA_PROFILE
        and not has_modern_contract
        and source_contract.get("kind") == LEGACY_PANORAMA_KIND
        and capture.get("source") == LEGACY_PANORAMA_SOURCE_LABEL
        and "record_native_profile" not in capture
    )
    if legacy_panorama_v1:
        legacy_expected = {
            "width": PANORAMA_PROFILE.width,
            "height": PANORAMA_PROFILE.height,
            "pixel_format": "MJPG",
            "fps": 30,
            "projection": PANORAMA_PROFILE.projection,
            "kind": LEGACY_PANORAMA_KIND,
        }
        _require_exact_fields(
            source_contract, legacy_expected, "legacy panorama capture source_contract"
        )
    else:
        _validate_profile_contract(
            source_contract, profile, "capture source_contract"
        )
        if capture.get("source") != profile.source_label:
            raise UvcCaptureValidationError(
                f"capture source must be {profile.source_label!r}"
            )
        if capture.get("record_native_profile") is not True:
            raise UvcCaptureValidationError(
                "capture contract does not preserve the native UVC profile"
            )

    if capture.get("record_panorama_master") is not profile.record_panorama_master:
        raise UvcCaptureValidationError(
            "capture record_panorama_master disagrees with its native source profile"
        )

    completion_contract = complete.get("source_contract")
    if completion_contract is not None:
        _validate_profile_contract(
            _object(completion_contract, "completion source_contract"),
            profile,
            "completion source_contract",
        )
    elif not legacy_panorama_v1:
        raise UvcCaptureValidationError(
            "completion marker is missing its native source_contract"
        )
    for key, expected_value in (
        ("requested_view_mode", profile.view_mode),
        ("record_native_profile", True),
        ("record_panorama_master", profile.record_panorama_master),
        ("source", profile.source_label),
    ):
        if key in complete and complete.get(key) != expected_value:
            raise UvcCaptureValidationError(
                f"completion marker {key} disagrees with its native source profile"
            )
    stabilization = capture.get("stabilization")
    if profile.view_mode == "panorama":
        if stabilization != "baked_in":
            raise UvcCaptureValidationError(
                "panorama capture must retain its in-camera stabilization declaration"
            )
    elif stabilization not in {
        "baked_in",  # accepted for legacy flat captures
        "not_verified",
        "unsupported_in_x3_webcam",
    }:
        raise UvcCaptureValidationError(
            "flat UVC capture stabilization semantics are unsupported"
        )
    if capture.get("sensor_to_driver_latency") != "unknown":
        raise UvcCaptureValidationError("unexpected sensor-to-driver latency declaration")
    return profile, legacy_panorama_v1


def _require_exact_fields(
    value: Mapping[str, Any], expected: Mapping[str, Any], label: str
) -> None:
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise UvcCaptureValidationError(
                f"{label} {key} must be {expected_value!r}, got {value.get(key)!r}"
            )


def _validate_profile_contract(
    value: Mapping[str, Any], profile: UvcNativeProfile, label: str
) -> None:
    _require_exact_fields(value, profile.contract(), label)


def _validate_role(
    *,
    session: Path,
    role: str,
    completion_worker: Mapping[str, Any],
    decoder_hook: DecoderHook | None,
    profile: UvcNativeProfile,
    allow_legacy_profile: bool,
) -> tuple[UvcRoleSource, dict[str, Any]]:
    role_dir = session / "uvc" / role
    _require_directory(role_dir, f"{role} role directory")
    for partial_name in (
        "video.mjpeg.partial",
        "frames.jsonl.partial",
        "manifest.json.partial",
    ):
        partial_path = role_dir / partial_name
        if os.path.lexists(partial_path):
            raise UvcCaptureValidationError(
                f"{role} contains partial artifact despite a completion marker: "
                f"{partial_path}"
            )
    manifest_path = role_dir / "manifest.json"
    video_path = role_dir / "video.mjpeg"
    frames_path = role_dir / "frames.jsonl"
    expected_paths = {
        "manifest": manifest_path.relative_to(session).as_posix(),
        "video": video_path.relative_to(session).as_posix(),
        "frames": frames_path.relative_to(session).as_posix(),
    }
    for key, expected in expected_paths.items():
        if completion_worker.get(key) != expected:
            raise UvcCaptureValidationError(
                f"{role} completion worker {key} must be {expected!r}"
            )

    manifest, worker_manifest_sha256 = _read_json_object(
        manifest_path, f"{role} worker manifest"
    )
    if manifest.get("schema") != WORKER_SCHEMA:
        raise UvcCaptureValidationError(f"{role} worker manifest schema is unsupported")
    if manifest.get("role") != role:
        raise UvcCaptureValidationError(f"{role} worker manifest role mismatch")
    files = _object(manifest.get("files"), f"{role} worker files")
    if files.get("video") != "video.mjpeg" or files.get("frames") != "frames.jsonl":
        raise UvcCaptureValidationError(f"{role} worker file names are not canonical")
    if files.get("sole_completion_marker") != "manifest.json":
        raise UvcCaptureValidationError(f"{role} worker completion semantics are invalid")
    camera_processing = _validate_worker_source(
        role,
        manifest,
        profile=profile,
        allow_legacy_profile=allow_legacy_profile,
    )

    recording = _object(manifest.get("recording"), f"{role} recording summary")
    frame_count = _integer(recording.get("frame_count"), f"{role} frame_count", minimum=1)
    payload_bytes = _integer(
        recording.get("payload_bytes"), f"{role} payload_bytes", minimum=1
    )
    _validate_completion_worker_summary(
        role, completion_worker, recording, frame_count, payload_bytes
    )
    _validate_writer_summary(role, manifest, frame_count)

    _require_regular_file(video_path, f"{role} video.mjpeg")
    _require_regular_file(frames_path, f"{role} frames.jsonl")
    if video_path.stat().st_size != payload_bytes:
        raise UvcCaptureValidationError(
            f"{role} video.mjpeg size does not match manifest payload_bytes"
        )
    records, frames_sha256 = _load_and_validate_frames(
        role=role,
        frames_path=frames_path,
        frame_count=frame_count,
        payload_bytes=payload_bytes,
        recording=recording,
    )
    video_sha256, packet_sha256 = _validate_video_packets(
        role=role,
        video_path=video_path,
        records=records,
        decoder_hook=decoder_hook,
    )

    timing_quality = _timing_quality(records)
    source = _object(manifest.get("source"), f"{role} worker source")
    has_raw_clock = records[0].has_monotonic_raw_mapping
    _validate_timestamp_mapping_contract(role, manifest, has_raw_clock)
    preferred_timestamp = (
        "soe_monotonic_raw_ns" if has_raw_clock else "soe_monotonic_ns"
    )
    normalized = {
        "role": role,
        "view_mode": profile.view_mode,
        "semantic": profile.semantic,
        "projection": profile.projection,
        "layout": profile.layout,
        "kind": profile.kind,
        "compression": "lossy_camera_mjpeg",
        "width": profile.width,
        "height": profile.height,
        "fps_declared": 30,
        "fps_negotiated": float(source["negotiated_fps"]),
        "pixel_format": "MJPG",
        "camera_processing": camera_processing,
        "native_pixels_reprojected_by_reader": False,
        "frame_count": frame_count,
        "payload_bytes": payload_bytes,
        "first_sequence": records[0].sequence,
        "last_sequence": records[-1].sequence,
        "first_soe_monotonic_ns": records[0].soe_monotonic_ns,
        "last_soe_monotonic_ns": records[-1].soe_monotonic_ns,
        "first_preferred_timestamp_ns": records[0].preferred_timestamp_ns,
        "last_preferred_timestamp_ns": records[-1].preferred_timestamp_ns,
        "files": {
            "worker_manifest": {
                "path": expected_paths["manifest"],
                "sha256": worker_manifest_sha256,
            },
            "packet_stream": {
                "path": expected_paths["video"],
                "sha256": video_sha256,
                "bytes": payload_bytes,
            },
            "packet_index": {
                "path": expected_paths["frames"],
                "sha256": frames_sha256,
                "rows": frame_count,
            },
        },
        "timeline": {
            "path": expected_paths["frames"],
            "format": "jsonl",
            "primary_timestamp": preferred_timestamp,
            "primary_clock_domain": (
                "CLOCK_MONOTONIC_RAW" if has_raw_clock else "CLOCK_MONOTONIC"
            ),
            "clock_mapping_confidence": (
                "per_frame_bracketed_mapping"
                if has_raw_clock
                else "legacy_fallback_without_raw_mapping"
            ),
            "packet_offset_field": "file_offset_bytes",
            "packet_size_field": "payload_bytes",
        },
        "quality": timing_quality,
    }
    return (
        UvcRoleSource(
            session,
            role,
            manifest_path,
            video_path,
            frames_path,
            records,
            packet_sha256,
            video_sha256,
            frames_sha256,
            worker_manifest_sha256,
        ),
        normalized,
    )


def _validate_worker_source(
    role: str,
    manifest: Mapping[str, Any],
    *,
    profile: UvcNativeProfile,
    allow_legacy_profile: bool,
) -> str:
    source = _object(manifest.get("source"), f"{role} worker source")
    expected = {
        "negotiated_fourcc": "MJPG",
        "negotiated_width": profile.width,
        "negotiated_height": profile.height,
        "semantic": profile.semantic,
        "standalone_jfif_payload": False,
    }
    if not allow_legacy_profile:
        expected.update(
            {
                "width": profile.width,
                "height": profile.height,
                "view_mode": profile.view_mode,
                "projection": profile.projection,
                "layout": profile.layout,
            }
        )
    for key, value in expected.items():
        if source.get(key) != value:
            raise UvcCaptureValidationError(
                f"{role} worker source {key} must be {value!r}"
            )
    fps = _finite_number(source.get("negotiated_fps"), f"{role} negotiated_fps")
    if abs(fps - 30.0) > 0.05:
        raise UvcCaptureValidationError(f"{role} negotiated fps is not MJPG30")
    # The top-level capture/completion source_contract is the authority for
    # kind and compression.  The worker independently reports the negotiated
    # V4L2 geometry/fourcc/fps plus the camera pixel semantics above.  A worker
    # may additionally repeat the complete contract; validate it when present,
    # but do not require that redundant representation.
    profile_declarations: list[tuple[str, Mapping[str, Any]]] = []
    for label, value in (
        ("worker source_contract", manifest.get("source_contract")),
        ("worker source.source_contract", source.get("source_contract")),
    ):
        if value is not None:
            profile_declarations.append((label, _object(value, f"{role} {label}")))
    for label, declaration in profile_declarations:
        _validate_profile_contract(declaration, profile, f"{role} {label}")

    camera_processing = source.get("camera_processing")
    if camera_processing != profile.camera_processing:
        raise UvcCaptureValidationError(
            f"{role} worker camera_processing must be "
            f"{profile.camera_processing!r}"
        )
    host_reprojection = source.get("host_geometric_postprocessing")
    if host_reprojection is not False and not (
        allow_legacy_profile and host_reprojection is None
    ):
        raise UvcCaptureValidationError(
            f"{role} worker must declare host_geometric_postprocessing=false"
        )
    _integer(
        source.get("v4l2_mmap_buffer_count"),
        f"{role} v4l2_mmap_buffer_count",
        minimum=1,
    )
    timestamp = _object(manifest.get("timestamp_semantics"), f"{role} timestamp semantics")
    if timestamp.get("source") != "V4L2 monotonic start-of-exposure flag":
        raise UvcCaptureValidationError(f"{role} timestamp source is unsupported")
    if timestamp.get("hidden_sensor_to_driver_timestamp_ms") != "unknown":
        raise UvcCaptureValidationError(f"{role} hidden latency declaration is unexpected")
    preview = _object(manifest.get("preview"), f"{role} preview contract")
    if preview.get("participates_in_recording") is not False:
        raise UvcCaptureValidationError(f"{role} preview must not participate in recording")
    return camera_processing


def _validate_timestamp_mapping_contract(
    role: str,
    manifest: Mapping[str, Any],
    has_raw_clock: bool,
) -> None:
    timestamp = _object(manifest.get("timestamp_semantics"), f"{role} timestamp semantics")
    declared_raw = (
        timestamp.get("soe_monotonic_raw_ns_clock") is not None
        or timestamp.get("monotonic_to_raw_mapping") is not None
    )
    if has_raw_clock != declared_raw:
        raise UvcCaptureValidationError(
            f"{role} timestamp manifest and frame rows disagree on MONOTONIC_RAW mapping"
        )
    if not has_raw_clock:
        return
    if timestamp.get("soe_monotonic_ns_clock") != "CLOCK_MONOTONIC":
        raise UvcCaptureValidationError(
            f"{role} worker did not declare the source CLOCK_MONOTONIC domain"
        )
    if timestamp.get("soe_monotonic_raw_ns_clock") != (
        "CLOCK_MONOTONIC_RAW estimated per DQBUF"
    ):
        raise UvcCaptureValidationError(
            f"{role} worker MONOTONIC_RAW clock declaration is unsupported"
        )
    mapping = _object(
        timestamp.get("monotonic_to_raw_mapping"),
        f"{role} MONOTONIC_RAW mapping contract",
    )
    if mapping.get("per_frame_fields") != [
        "soe_monotonic_raw_ns",
        "monotonic_to_raw_offset_ns",
        "monotonic_to_raw_error_bound_ns",
    ]:
        raise UvcCaptureValidationError(
            f"{role} worker MONOTONIC_RAW per-frame field declaration is invalid"
        )


def _validate_completion_worker_summary(
    role: str,
    completion: Mapping[str, Any],
    recording: Mapping[str, Any],
    frame_count: int,
    payload_bytes: int,
) -> None:
    expected = {
        "frame_count": frame_count,
        "payload_bytes": payload_bytes,
        "first_sequence": _integer(recording.get("first_sequence"), f"{role} first_sequence", minimum=0),
        "last_sequence": _integer(recording.get("last_sequence"), f"{role} last_sequence", minimum=0),
        "first_soe_monotonic_ns": _integer(
            recording.get("first_soe_monotonic_ns"), f"{role} first_soe_monotonic_ns", minimum=1
        ),
        "last_soe_monotonic_ns": _integer(
            recording.get("last_soe_monotonic_ns"), f"{role} last_soe_monotonic_ns", minimum=1
        ),
    }
    for key, value in expected.items():
        if completion.get(key) != value:
            raise UvcCaptureValidationError(
                f"{role} completion worker {key} does not match worker manifest"
            )
    optional_raw_summary = (
        "first_soe_monotonic_raw_ns",
        "last_soe_monotonic_raw_ns",
        "min_monotonic_to_raw_offset_ns",
        "max_monotonic_to_raw_offset_ns",
        "max_monotonic_to_raw_error_bound_ns",
    )
    recording_present = [recording.get(key) is not None for key in optional_raw_summary]
    completion_present = [completion.get(key) is not None for key in optional_raw_summary]
    if any(recording_present) and not all(recording_present):
        raise UvcCaptureValidationError(
            f"{role} worker manifest has a partial MONOTONIC_RAW summary"
        )
    if any(completion_present) and not all(completion_present):
        raise UvcCaptureValidationError(
            f"{role} completion marker has a partial MONOTONIC_RAW summary"
        )
    if all(recording_present) != all(completion_present):
        raise UvcCaptureValidationError(
            f"{role} completion marker and worker disagree on MONOTONIC_RAW availability"
        )
    if all(recording_present):
        for key in optional_raw_summary:
            value = _integer(recording.get(key), f"{role} {key}")
            if completion.get(key) != value:
                raise UvcCaptureValidationError(
                    f"{role} completion worker {key} does not match worker manifest"
                )


def _validate_writer_summary(role: str, manifest: Mapping[str, Any], frame_count: int) -> None:
    recording = _object(manifest.get("recording"), f"{role} recording summary")
    if recording.get("packet_storage") != "verbatim_concatenated_v4l2_mjpeg_payloads":
        raise UvcCaptureValidationError(f"{role} packet storage semantics are unsupported")
    if _integer(recording.get("sequence_gaps"), f"{role} sequence_gaps", minimum=0) != 0:
        raise UvcCaptureValidationError(f"{role} worker reported sequence gaps")
    samples = _integer(
        recording.get("source_interval_samples"),
        f"{role} source_interval_samples",
        minimum=1,
    )
    if samples != frame_count:
        raise UvcCaptureValidationError(f"{role} source interval count does not match frames")
    maximum = _finite_number(
        recording.get("max_source_interval_ms"), f"{role} max_source_interval_ms"
    )
    allowed = _finite_number(
        recording.get("max_allowed_source_interval_ms"),
        f"{role} max_allowed_source_interval_ms",
    )
    if maximum <= 0 or allowed <= 0 or maximum > allowed + 0.001:
        raise UvcCaptureValidationError(f"{role} source cadence exceeded its admitted limit")

    writer = _object(manifest.get("bounded_writer"), f"{role} bounded writer")
    capacity = _integer(writer.get("capacity_frames"), f"{role} writer capacity", minimum=1)
    high_water = _integer(
        writer.get("queue_high_water_frames"), f"{role} writer high water", minimum=0
    )
    if high_water > capacity:
        raise UvcCaptureValidationError(f"{role} writer high water exceeds capacity")
    if _integer(writer.get("enqueued_frames"), f"{role} enqueued_frames", minimum=1) != frame_count:
        raise UvcCaptureValidationError(f"{role} writer enqueued count does not match frames")
    if _integer(writer.get("written_frames"), f"{role} written_frames", minimum=1) != frame_count:
        raise UvcCaptureValidationError(f"{role} writer written count does not match frames")
    if writer.get("queue_full_is_fatal") is not True or writer.get("write_error") is not None:
        raise UvcCaptureValidationError(f"{role} writer did not publish a clean fail-closed result")


def _load_and_validate_frames(
    *,
    role: str,
    frames_path: Path,
    frame_count: int,
    payload_bytes: int,
    recording: Mapping[str, Any],
) -> tuple[tuple[UvcFrameRecord, ...], str]:
    records: list[UvcFrameRecord] = []
    expected_offset = 0
    previous_sequence: int | None = None
    previous_soe_ns: int | None = None
    previous_soe_raw_ns: int | None = None
    raw_clock_mode: bool | None = None
    digest = hashlib.sha256()
    with frames_path.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            digest.update(raw_line)
            if len(raw_line) > MAX_JSONL_ROW_BYTES:
                raise UvcCaptureValidationError(
                    f"{role} frames.jsonl row {line_number} exceeds 1 MiB"
                )
            if not raw_line.endswith(b"\n"):
                raise UvcCaptureValidationError(
                    f"{role} frames.jsonl row {line_number} is truncated"
                )
            if line_number > frame_count:
                raise UvcCaptureValidationError(
                    f"{role} frames.jsonl has more rows than frame_count"
                )
            try:
                row = _strict_json_loads(raw_line, f"{role} frame row {line_number}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UvcCaptureValidationError(
                    f"{role} frames.jsonl row {line_number} is invalid JSON"
                ) from exc
            row = _object(row, f"{role} frame row {line_number}")
            values = {
                key: _integer(row.get(key), f"{role} row {line_number} {key}", minimum=0)
                for key in (
                    "frame_index",
                    "sequence",
                    "soe_monotonic_ns",
                    "dequeue_monotonic_ns",
                    "enqueue_monotonic_ns",
                    "record_monotonic_ns",
                    "payload_bytes",
                    "file_offset_bytes",
                )
            }
            source_interval_ms = _finite_number(
                row.get("source_interval_ms"), f"{role} row {line_number} source_interval_ms"
            )
            if source_interval_ms <= 0:
                raise UvcCaptureValidationError(
                    f"{role} row {line_number} source_interval_ms must be positive"
                )
            if values["frame_index"] != len(records):
                raise UvcCaptureValidationError(
                    f"{role} frame_index is not contiguous at row {line_number}"
                )
            if values["sequence"] > 0xFFFFFFFF:
                raise UvcCaptureValidationError(f"{role} sequence is out of uint32 range")
            if values["payload_bytes"] <= 0 or values["payload_bytes"] > MAX_PACKET_BYTES:
                raise UvcCaptureValidationError(
                    f"{role} row {line_number} payload size is outside worker bounds"
                )
            if values["file_offset_bytes"] != expected_offset:
                raise UvcCaptureValidationError(
                    f"{role} packet offset mismatch at row {line_number}"
                )
            timestamps = (
                values["soe_monotonic_ns"],
                values["dequeue_monotonic_ns"],
                values["enqueue_monotonic_ns"],
                values["record_monotonic_ns"],
            )
            if timestamps[0] <= 0 or tuple(sorted(timestamps)) != timestamps:
                raise UvcCaptureValidationError(
                    f"{role} monotonic timestamp order is invalid at row {line_number}"
                )
            if previous_sequence is not None and values["sequence"] != (previous_sequence + 1) & 0xFFFFFFFF:
                raise UvcCaptureValidationError(f"{role} sequence gap at row {line_number}")
            raw_clock_keys = (
                "soe_monotonic_raw_ns",
                "monotonic_to_raw_offset_ns",
                "monotonic_to_raw_error_bound_ns",
            )
            raw_clock_presence = tuple(key in row for key in raw_clock_keys)
            if any(raw_clock_presence) and not all(raw_clock_presence):
                raise UvcCaptureValidationError(
                    f"{role} row {line_number} has a partial MONOTONIC_RAW mapping"
                )
            row_has_raw_clock = all(raw_clock_presence)
            if raw_clock_mode is None:
                raw_clock_mode = row_has_raw_clock
            elif raw_clock_mode != row_has_raw_clock:
                raise UvcCaptureValidationError(
                    f"{role} frames.jsonl mixes mapped and legacy clock rows"
                )
            soe_raw_ns: int | None = None
            raw_offset_ns: int | None = None
            raw_error_bound_ns: int | None = None
            if row_has_raw_clock:
                soe_raw_ns = _integer(
                    row.get("soe_monotonic_raw_ns"),
                    f"{role} row {line_number} soe_monotonic_raw_ns",
                    minimum=1,
                )
                raw_offset_ns = _integer(
                    row.get("monotonic_to_raw_offset_ns"),
                    f"{role} row {line_number} monotonic_to_raw_offset_ns",
                )
                raw_error_bound_ns = _integer(
                    row.get("monotonic_to_raw_error_bound_ns"),
                    f"{role} row {line_number} monotonic_to_raw_error_bound_ns",
                    minimum=0,
                )
                if soe_raw_ns != values["soe_monotonic_ns"] + raw_offset_ns:
                    raise UvcCaptureValidationError(
                        f"{role} row {line_number} has an inconsistent MONOTONIC_RAW estimate"
                    )
                if previous_soe_raw_ns is not None and soe_raw_ns <= previous_soe_raw_ns:
                    raise UvcCaptureValidationError(
                        f"{role} CLOCK_MONOTONIC_RAW SOE is not strictly increasing "
                        f"at row {line_number}"
                    )
            if previous_soe_ns is not None:
                if timestamps[0] <= previous_soe_ns:
                    raise UvcCaptureValidationError(
                        f"{role} SOE timestamp is not strictly increasing at row {line_number}"
                    )
                actual_interval_ns = timestamps[0] - previous_soe_ns
                declared_interval_ns = round(source_interval_ms * 1_000_000)
                if abs(actual_interval_ns - declared_interval_ns) > 2_000:
                    raise UvcCaptureValidationError(
                        f"{role} source_interval_ms disagrees with SOE timestamps at row {line_number}"
                    )
            record = UvcFrameRecord(
                frame_index=values["frame_index"],
                sequence=values["sequence"],
                soe_monotonic_ns=values["soe_monotonic_ns"],
                dequeue_monotonic_ns=values["dequeue_monotonic_ns"],
                enqueue_monotonic_ns=values["enqueue_monotonic_ns"],
                record_monotonic_ns=values["record_monotonic_ns"],
                source_interval_ms=source_interval_ms,
                payload_bytes=values["payload_bytes"],
                file_offset_bytes=values["file_offset_bytes"],
                soe_monotonic_raw_ns=soe_raw_ns,
                monotonic_to_raw_offset_ns=raw_offset_ns,
                monotonic_to_raw_error_bound_ns=raw_error_bound_ns,
            )
            records.append(record)
            expected_offset += record.payload_bytes
            previous_sequence = record.sequence
            previous_soe_ns = record.soe_monotonic_ns
            previous_soe_raw_ns = record.soe_monotonic_raw_ns

    if len(records) != frame_count:
        raise UvcCaptureValidationError(
            f"{role} frames.jsonl row count {len(records)} does not match {frame_count}"
        )
    if expected_offset != payload_bytes:
        raise UvcCaptureValidationError(
            f"{role} packet sizes total {expected_offset}, expected {payload_bytes}"
        )
    expected_summary = {
        "first_sequence": records[0].sequence,
        "last_sequence": records[-1].sequence,
        "first_soe_monotonic_ns": records[0].soe_monotonic_ns,
        "last_soe_monotonic_ns": records[-1].soe_monotonic_ns,
    }
    for key, value in expected_summary.items():
        if recording.get(key) != value:
            raise UvcCaptureValidationError(f"{role} {key} does not match frames.jsonl")
    raw_summary_keys = (
        "first_soe_monotonic_raw_ns",
        "last_soe_monotonic_raw_ns",
        "min_monotonic_to_raw_offset_ns",
        "max_monotonic_to_raw_offset_ns",
        "max_monotonic_to_raw_error_bound_ns",
    )
    raw_summary_present = tuple(recording.get(key) is not None for key in raw_summary_keys)
    if records[0].has_monotonic_raw_mapping:
        if not all(raw_summary_present):
            raise UvcCaptureValidationError(
                f"{role} worker manifest is missing its MONOTONIC_RAW summary"
            )
        offsets = [
            record.monotonic_to_raw_offset_ns
            for record in records
            if record.monotonic_to_raw_offset_ns is not None
        ]
        errors = [
            record.monotonic_to_raw_error_bound_ns
            for record in records
            if record.monotonic_to_raw_error_bound_ns is not None
        ]
        expected_raw_summary = {
            "first_soe_monotonic_raw_ns": records[0].soe_monotonic_raw_ns,
            "last_soe_monotonic_raw_ns": records[-1].soe_monotonic_raw_ns,
            "min_monotonic_to_raw_offset_ns": min(offsets),
            "max_monotonic_to_raw_offset_ns": max(offsets),
            "max_monotonic_to_raw_error_bound_ns": max(errors),
        }
        for key, value in expected_raw_summary.items():
            if recording.get(key) != value:
                raise UvcCaptureValidationError(
                    f"{role} {key} does not match frames.jsonl"
                )
    elif any(raw_summary_present):
        raise UvcCaptureValidationError(
            f"{role} legacy frames have a MONOTONIC_RAW manifest summary"
        )
    reported_max = _finite_number(
        recording.get("max_source_interval_ms"), f"{role} max_source_interval_ms"
    )
    actual_reported_max = max(record.source_interval_ms for record in records)
    if abs(reported_max - actual_reported_max) > 0.001:
        raise UvcCaptureValidationError(
            f"{role} max_source_interval_ms does not match frames.jsonl"
        )
    start_monotonic_ns = _integer(
        recording.get("start_monotonic_time_ns"),
        f"{role} start_monotonic_time_ns",
        minimum=1,
    )
    stop_monotonic_ns = _integer(
        recording.get("stop_monotonic_time_ns"),
        f"{role} stop_monotonic_time_ns",
        minimum=1,
    )
    if start_monotonic_ns > records[0].soe_monotonic_ns:
        raise UvcCaptureValidationError(
            f"{role} first exposure predates recording start"
        )
    # STOP closes queue admission, then drains the writer.  The writer-side
    # timestamp may legitimately follow STOP, but every enqueue must predate it.
    if stop_monotonic_ns < records[-1].enqueue_monotonic_ns:
        raise UvcCaptureValidationError(
            f"{role} last packet enqueue follows recording stop"
        )
    start_unix_ns = _integer(
        recording.get("start_unix_time_ns"),
        f"{role} start_unix_time_ns",
        minimum=1,
    )
    stop_unix_ns = _integer(
        recording.get("stop_unix_time_ns"),
        f"{role} stop_unix_time_ns",
        minimum=1,
    )
    if stop_unix_ns < start_unix_ns:
        raise UvcCaptureValidationError(f"{role} Unix recording bounds are reversed")
    return tuple(records), digest.hexdigest()


def _validate_video_packets(
    *,
    role: str,
    video_path: Path,
    records: tuple[UvcFrameRecord, ...],
    decoder_hook: DecoderHook | None,
) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256()
    packet_digests: list[str] = []
    with video_path.open("rb") as stream:
        for record in records:
            if stream.tell() != record.file_offset_bytes:
                raise UvcCaptureValidationError(
                    f"{role} video offset disagrees at frame {record.frame_index}"
                )
            payload = stream.read(record.payload_bytes)
            if len(payload) != record.payload_bytes:
                raise UvcCaptureValidationError(
                    f"{role} video.mjpeg is truncated at frame {record.frame_index}"
                )
            digest.update(payload)
            packet_digests.append(hashlib.sha256(payload).hexdigest())
            if decoder_hook is not None:
                try:
                    accepted = decoder_hook(role, record, payload)
                except Exception as exc:
                    raise UvcCaptureValidationError(
                        f"{role} decoder hook failed at frame {record.frame_index}: {exc}"
                    ) from exc
                if accepted is False:
                    raise UvcCaptureValidationError(
                        f"{role} decoder hook rejected frame {record.frame_index}"
                    )
        if stream.read(1):
            raise UvcCaptureValidationError(
                f"{role} video.mjpeg contains trailing bytes not indexed by frames.jsonl"
            )
    return digest.hexdigest(), tuple(packet_digests)


def _timing_quality(records: tuple[UvcFrameRecord, ...]) -> dict[str, Any]:
    monotonic_intervals = [
        records[index].soe_monotonic_ns - records[index - 1].soe_monotonic_ns
        for index in range(1, len(records))
    ]
    preferred_intervals = [
        records[index].preferred_timestamp_ns
        - records[index - 1].preferred_timestamp_ns
        for index in range(1, len(records))
    ]
    duration_ns = (
        records[-1].preferred_timestamp_ns - records[0].preferred_timestamp_ns
    )
    effective_fps = 0.0
    if duration_ns > 0:
        effective_fps = round((len(records) - 1) * 1_000_000_000 / duration_ns, 6)
    return {
        "capture_admissible": True,
        "sequence_gaps": 0,
        "duration_soe_ns": duration_ns,
        "effective_fps_from_soe": effective_fps,
        "payload_bytes": _integer_distribution([record.payload_bytes for record in records]),
        "preferred_source_interval_ns": _integer_distribution(preferred_intervals),
        "source_interval_ns": _integer_distribution(preferred_intervals),
        "monotonic_source_interval_ns": _integer_distribution(monotonic_intervals),
        "soe_to_dequeue_ns": _integer_distribution(
            [record.dequeue_monotonic_ns - record.soe_monotonic_ns for record in records]
        ),
        "dequeue_to_enqueue_ns": _integer_distribution(
            [record.enqueue_monotonic_ns - record.dequeue_monotonic_ns for record in records]
        ),
        "enqueue_to_record_ns": _integer_distribution(
            [record.record_monotonic_ns - record.enqueue_monotonic_ns for record in records]
        ),
        "soe_to_record_ns": _integer_distribution(
            [record.record_monotonic_ns - record.soe_monotonic_ns for record in records]
        ),
        "monotonic_to_raw_mapping": (
            {
                "available": True,
                "offset_ns": _integer_distribution(
                    [
                        record.monotonic_to_raw_offset_ns
                        for record in records
                        if record.monotonic_to_raw_offset_ns is not None
                    ]
                ),
                "error_bound_ns": _integer_distribution(
                    [
                        record.monotonic_to_raw_error_bound_ns
                        for record in records
                        if record.monotonic_to_raw_error_bound_ns is not None
                    ]
                ),
            }
            if records[0].has_monotonic_raw_mapping
            else {
                "available": False,
                "confidence": "legacy_CLOCK_MONOTONIC_fallback",
            }
        ),
    }


def _pairing_quality(roles: Mapping[str, UvcRoleSource]) -> dict[str, Any]:
    if len(roles) != 2:
        return {
            "available": False,
            "reason": "single_role_capture",
            "semantic": "nearest_SOE_diagnostic_only",
        }
    reference_role, comparison_role = tuple(roles)
    raw_clock_shared = all(
        role_source.records[0].has_monotonic_raw_mapping
        for role_source in roles.values()
    )
    if raw_clock_shared:
        reference = [
            record.preferred_timestamp_ns
            for record in roles[reference_role].records
        ]
        comparison = [
            record.preferred_timestamp_ns
            for record in roles[comparison_role].records
        ]
        pairing_clock = "CLOCK_MONOTONIC_RAW"
    else:
        reference = [
            record.soe_monotonic_ns for record in roles[reference_role].records
        ]
        comparison = [
            record.soe_monotonic_ns for record in roles[comparison_role].records
        ]
        pairing_clock = "CLOCK_MONOTONIC_legacy_fallback"
    overlap_start = max(reference[0], comparison[0])
    overlap_stop = min(reference[-1], comparison[-1])
    skews: list[int] = []
    if overlap_stop >= overlap_start:
        for timestamp in reference:
            if timestamp < overlap_start or timestamp > overlap_stop:
                continue
            index = bisect_left(comparison, timestamp)
            candidates: list[int] = []
            if index < len(comparison):
                candidates.append(abs(comparison[index] - timestamp))
            if index:
                candidates.append(abs(comparison[index - 1] - timestamp))
            if candidates:
                skews.append(min(candidates))
    return {
        "available": bool(skews),
        "semantic": "nearest_SOE_diagnostic_only",
        "clock_domain": pairing_clock,
        "reference_role": reference_role,
        "comparison_role": comparison_role,
        "overlap_start_monotonic_ns": overlap_start,
        "overlap_stop_monotonic_ns": overlap_stop,
        "nearest_pair_skew_ns": _integer_distribution(skews),
    }


def _integer_distribution(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    ordered = sorted(values)
    count = len(ordered)

    def nearest_rank(percent: int) -> int:
        index = max(0, math.ceil(percent / 100 * count) - 1)
        return ordered[index]

    return {
        "count": count,
        "min": ordered[0],
        "p50": nearest_rank(50),
        "p95": nearest_rank(95),
        "p99": nearest_rank(99),
        "max": ordered[-1],
        "mean": (sum(ordered) + count // 2) // count,
    }


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any], str]:
    _require_regular_file(path, label)
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise UvcCaptureValidationError(f"{label} has invalid size {size}: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UvcCaptureValidationError(f"cannot read {label}: {path}: {exc}") from exc
    try:
        payload = _strict_json_loads(raw, label)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UvcCaptureValidationError(f"{label} is invalid JSON: {path}") from exc
    return _object(payload, label), hashlib.sha256(raw).hexdigest()


def _strict_json_loads(raw: bytes, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UvcCaptureValidationError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise UvcCaptureValidationError(
            f"{label} contains non-standard JSON number {value}"
        )

    return json.loads(
        raw,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise UvcCaptureValidationError(f"missing {label}: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise UvcCaptureValidationError(f"{label} is not a real directory: {path}")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise UvcCaptureValidationError(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise UvcCaptureValidationError(f"{label} is not a regular file: {path}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UvcCaptureValidationError(f"{label} must be a JSON object")
    return value


def _parse_roles(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise UvcCaptureValidationError(f"{label} roles must be a non-empty list")
    if len(value) > len(KNOWN_ROLES):
        raise UvcCaptureValidationError(f"{label} declares too many roles")
    roles: list[str] = []
    for role in value:
        if role not in KNOWN_ROLES:
            raise UvcCaptureValidationError(f"{label} contains unsupported role {role!r}")
        if role in roles:
            raise UvcCaptureValidationError(f"{label} contains duplicate role {role!r}")
        roles.append(role)
    return tuple(roles)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UvcCaptureValidationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise UvcCaptureValidationError(f"{label} must be at least {minimum}")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UvcCaptureValidationError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise UvcCaptureValidationError(f"{label} must be finite")
    return number


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a completed InstaDataCap USB/UVC capture offline"
    )
    parser.add_argument("session", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="atomically write the normalized source manifest instead of stdout",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace --output if it already exists",
    )
    args = parser.parse_args(argv)
    try:
        source = validate_uvc_capture(args.session)
        if args.output:
            destination = write_normalized_manifest(
                source, args.output, overwrite=args.overwrite
            )
            print(destination)
        else:
            json.dump(source.normalized_manifest, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
    except (OSError, UvcCaptureValidationError) as exc:
        print(f"UVC capture validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
