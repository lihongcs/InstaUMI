from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import uuid
from typing import Any

from .session import InputTimestamp


WORKSPACE_SCHEMA = "instaumi.bimanual_panorama_target.workspace.v1"
INDEX_SCHEMA = "instaumi.bimanual_panorama_target.grounding_index.v1"
# Keep the annotation sidecar directly consumable by the established SAM 2
# tracker.  That tracker filters by camera_role, so one file may safely carry
# independent left- and right-camera clicks.
INSTANCE_SCHEMA = "instaumi.active_view.target_instances.v1"
WORKSPACE_FILENAME = "workspace_bimanual_panorama_target.json"
FISHEYE_WORKSPACE_FILENAME = "workspace_bimanual_target.json"
PANORAMA_WIDTH = 2048
PANORAMA_HEIGHT = 1024
CONVERSION_COMPLETE_SCHEMA = "instaumi-bimanual-conversion-complete-v1"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def conversion_plan_candidates(root: Path) -> list[Path]:
    """Return conversion-plan locations directly associated with ``root``."""

    candidates: set[Path] = set()
    direct = root / "conversion_plan.json"
    if direct.is_file():
        candidates.add(direct.resolve())
    for child_name in ("lerobot", "active_view_lerobot"):
        candidate = root / child_name / "conversion_plan.json"
        if candidate.is_file():
            candidates.add(candidate.resolve())
    for candidate in root.glob("*_lerobot/conversion_plan.json"):
        if candidate.is_file():
            candidates.add(candidate.resolve())
    return sorted(candidates)


def describe_missing_workspace(root: Path) -> str:
    raw_markers = [
        root / "manifest.json",
        root / "timestamp_log.jsonl",
        root / "insta",
    ]
    converted = conversion_plan_candidates(root)
    if any(path.exists() for path in raw_markers) or converted:
        details: list[str] = []
        if converted:
            details.append(f"LeRobot dataset found: {converted[0].parent}")
        found = "; ".join(details)
        suffix = f" ({found})" if found else ""
        return (
            "This is a raw capture directory, but it does not yet contain a complete "
            f"bimanual episode mapping{suffix}. Wait for both Insta transfers and the "
            "LeRobot conversion plan to finish, then open this raw directory again. "
            "DataCap reads files from disk only and never connects to or scans cameras for annotation."
        )
    return (
        f"Directory does not contain {WORKSPACE_FILENAME}: {root}. "
        "Enter a raw capture directory or a bimanual panorama annotation directory."
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def validate_hex_digest(value: Any, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"invalid {label}: {digest!r}")
    return digest


def chapter_for_source_frame(
    chapters: list[dict[str, Any]], source_index: int
) -> dict[str, Any] | None:
    return next(
        (
            chapter
            for chapter in chapters
            if int(chapter["global_start"]) <= source_index
            < int(chapter["global_end_exclusive"])
        ),
        None,
    )


def png_geometry(path: Path) -> tuple[int, int]:
    """Read the PNG IHDR geometry without adding a Pillow runtime dependency."""

    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"MediaSDK did not generate a valid PNG: {path}")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def export_panorama_frame(
    chapter: dict[str, Any],
    source_index: int,
    destination: Path,
    *,
    exporter: Path,
    model_dir: str | None,
) -> None:
    """Stitch one transferred INSV frame into a 2:1 panorama, never touching cameras."""

    local_index = source_index - int(chapter["global_start"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_dir = Path(
        tempfile.mkdtemp(prefix=f".{destination.stem}.mediasdk.", dir=destination.parent)
    )
    command = [
        str(exporter),
        "--input",
        str(chapter["path"]),
        "--output-dir",
        str(output_dir),
        "--frames",
        str(local_index),
        "--width",
        str(PANORAMA_WIDTH),
        "--height",
        str(PANORAMA_HEIGHT),
        "--flowstate",
        "off",
        "--direction-lock",
        "off",
        "--stitch-type",
        "optflow",
        "--stitch-fusion",
        "off",
        "--cuda",
        "on",
        "--timeout-sec",
        "120",
    ]
    if model_dir:
        command += ["--model-dir", model_dir]
    runtime_env = dict(os.environ)
    runtime_env.pop("LD_PRELOAD", None)
    runtime_env.pop("LD_AUDIT", None)
    try:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
                env=runtime_env,
                cwd="/",
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Insta360 MediaSDK panorama exporter does not exist: {exporter}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Insta360 MediaSDK timed out while generating the offline panorama") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "MediaSDK failed").strip().splitlines()[-1]
            raise ValueError(f"Insta360 MediaSDK failed to generate the offline panorama: {detail}") from exc
        if "[done]" not in (completed.stdout + completed.stderr):
            raise ValueError("Insta360 MediaSDK did not report a completed panorama export")
        outputs = list(output_dir.glob("*.png"))
        if len(outputs) != 1:
            raise ValueError(f"Unexpected Insta360 MediaSDK panorama count: {len(outputs)}")
        if png_geometry(outputs[0]) != (PANORAMA_WIDTH, PANORAMA_HEIGHT):
            raise ValueError("Insta360 MediaSDK panorama is not 2048x1024")
        os.replace(outputs[0], destination)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def task_name_from_capture(capture_root: Path) -> str:
    manifest_path = capture_root / "manifest.json"
    if manifest_path.is_file():
        try:
            capture = read_json(manifest_path)
            return str(capture.get("task_name") or capture.get("session_id") or capture_root.name)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return capture_root.name


def find_conversion_plan(capture_root: Path) -> Path | None:
    candidates = conversion_plan_candidates(capture_root)
    if len(candidates) > 1:
        raise ValueError(
            "Multiple LeRobot conversion plans were found; enter the intended LeRobot directory directly."
        )
    return candidates[0] if candidates else None


def completed_insv_chapters(
    conversion_plan: dict[str, Any], hand: str
) -> list[dict[str, Any]]:
    image_sources = ((conversion_plan.get("inputs") or {}).get("image_sources") or {})
    source = image_sources.get(hand) or {}
    recipe = source.get("recipe") or {}
    if str(recipe.get("front") or "") != "decode_insv_primary_stream_then_openpi_resize":
        raise ValueError(f"{hand} hand conversion plan does not identify the INSV front fisheye")
    available: list[dict[str, Any]] = []
    for raw in source.get("insv_chapters") or []:
        chapter = dict(raw)
        try:
            start = int(chapter["global_start"])
            end = int(chapter["global_end_exclusive"])
            width = int(chapter["width"])
            height = int(chapter["height"])
            frame_count = int(chapter["frame_count"])
            Fraction(str(chapter["frame_rate"]))
            path = Path(str(chapter["path"])).expanduser().resolve(strict=True)
            expected_size = int((chapter.get("identity") or {}).get("size_bytes") or 0)
        except (KeyError, TypeError, ValueError, OSError, ZeroDivisionError):
            continue
        if (
            start < 0
            or end <= start
            or frame_count != end - start
            or width != height
            or width < 512
            or (expected_size > 0 and path.stat().st_size != expected_size)
        ):
            continue
        chapter.update(
            {
                "global_start": start,
                "global_end_exclusive": end,
                "path": str(path),
                "media": {
                    "frame_rate": str(chapter["frame_rate"]),
                    "width": width,
                    "height": height,
                },
            }
        )
        available.append(chapter)
    available.sort(key=lambda item: int(item["global_start"]))
    return available


def panorama_exporter(
    conversion_plan: dict[str, Any], hand: str
) -> tuple[Path, str | None]:
    image_sources = ((conversion_plan.get("inputs") or {}).get("image_sources") or {})
    source = image_sources.get(hand) or {}
    identity = source.get("mediasdk_exporter") or {}
    try:
        path = Path(str(identity["path"])).expanduser().resolve(strict=True)
        expected_size = int(identity["size_bytes"])
        expected_sha256 = validate_hex_digest(identity["sha256"], f"{hand} MediaSDK exporter hash")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"The {hand}-hand conversion plan has no usable MediaSDK panorama exporter") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"MediaSDK panorama exporter is not executable: {path}")
    if path.stat().st_size != expected_size or sha256_file(path) != expected_sha256:
        raise ValueError(f"MediaSDK panorama exporter does not match the conversion plan: {path}")
    recipe = source.get("recipe") or {}
    model_dir = recipe.get("model_dir")
    return path, str(model_dir) if model_dir else None


def prepare_capture_annotation_workspace(capture_root: Path) -> Path:
    """Create paired left/right equirectangular annotations for one random episode."""

    workspace_root = capture_root.with_name(capture_root.name + "_hand_panorama_annotations")
    workspace_path = workspace_root / WORKSPACE_FILENAME
    if workspace_path.is_file():
        return workspace_root
    if workspace_root.exists():
        raise ValueError(
            f"Annotation directory already exists but is incomplete: {workspace_root}. "
            "It was not modified to avoid overwriting existing results."
        )

    conversion_plan_path = find_conversion_plan(capture_root)
    if conversion_plan_path is None:
        raise ValueError(describe_missing_workspace(capture_root))
    conversion_plan = read_json(conversion_plan_path)
    plan_hash = validate_hex_digest(conversion_plan.get("plan_hash"), "conversion plan hash")
    completion_path = conversion_plan_path.parent / "conversion_complete.json"
    if not completion_path.is_file():
        raise ValueError(
            "LeRobot conversion is not complete yet. Wait for conversion_complete.json "
            "before creating the post-processing annotation workspace."
        )
    completion = read_json(completion_path)
    if (
        completion.get("schema") != CONVERSION_COMPLETE_SCHEMA
        or completion.get("plan_hash") != plan_hash
        or int(completion.get("episodes") or 0) <= 0
        or int(completion.get("frames") or 0) <= 0
    ):
        raise ValueError(
            f"LeRobot completion marker is invalid or does not match the conversion plan: {completion_path}"
        )
    frame_feature = (conversion_plan.get("features") or {}).get("source.camera_frame_index") or {}
    if list(frame_feature.get("names") or []) != ["right", "left"]:
        raise ValueError("Conversion plan does not explicitly define bimanual frame order as [right, left]")
    chapters_by_hand = {
        hand: completed_insv_chapters(conversion_plan, hand)
        for hand in ("left", "right")
    }
    exporters_by_hand = {
        hand: panorama_exporter(conversion_plan, hand)
        for hand in ("left", "right")
    }
    eligible: list[dict[str, Any]] = []
    for raw_episode in conversion_plan.get("episodes") or []:
        try:
            episode = int(raw_episode["episode_index"])
            first_pair = list(raw_episode["first_pair"])
            right_index = int(first_pair[0])
            left_index = int(first_pair[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        left_chapter = chapter_for_source_frame(chapters_by_hand["left"], left_index)
        right_chapter = chapter_for_source_frame(chapters_by_hand["right"], right_index)
        if left_chapter is not None and right_chapter is not None:
            eligible.append(
                {
                    "episode_index": episode,
                    "left": {"source_index": left_index, "chapter": left_chapter},
                    "right": {"source_index": right_index, "chapter": right_chapter},
                }
            )
    if not eligible:
        raise ValueError(
            "The conversion plan exists, but no episode has both camera streams fully available. "
            "Wait for both Insta transfers and try again; DataCap will not connect to cameras."
        )

    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{workspace_root.name}.preparing.", dir=workspace_root.parent)
    )
    try:
        selected = secrets.choice(eligible)
        episode = int(selected["episode_index"])
        frames_root = staging / "grounding" / "reference_frames"
        assets: list[dict[str, Any]] = []
        for hand in ("left", "right"):
            camera_role = f"insta_{hand}"
            source_index = int(selected[hand]["source_index"])
            chapter = selected[hand]["chapter"]
            frame_path = frames_root / (
                f"episode_{episode:06d}_{camera_role}_panorama.png"
            )
            exporter, model_dir = exporters_by_hand[hand]
            export_panorama_frame(
                chapter,
                source_index,
                frame_path,
                exporter=exporter,
                model_dir=model_dir,
            )
            frame_hash = sha256_file(frame_path)
            asset_id = hashlib.sha256(
                f"{plan_hash}:{episode}:{camera_role}:{source_index}:{frame_hash}".encode()
            ).hexdigest()
            assets.append(
                {
                    "asset_id": asset_id,
                    "episode_index": episode,
                    "hand_role": hand,
                    "camera_role": camera_role,
                    "source_frame_index": source_index,
                    "reference_projection": "equirectangular",
                    "horizontal_fov_deg": 360.0,
                    "vertical_fov_deg": 180.0,
                    "reference_width": PANORAMA_WIDTH,
                    "reference_height": PANORAMA_HEIGHT,
                    "media_type": "image/png",
                    "path": str(frame_path.relative_to(staging)),
                    "sha256": frame_hash,
                    "panorama_exporter_path": str(exporter),
                    "panorama_exporter_sha256": sha256_file(exporter),
                    "panorama_recipe": (
                        "Insta360 MediaSDK VideoStitcher OPTFLOW; 2048x1024 RGBA PNG; "
                        "flowstate=off; direction_lock=off; stitch_fusion=off; cuda=on"
                    ),
                }
            )
        index = {
            "schema": INDEX_SCHEMA,
            "created_at": iso_now(),
            "workspace_root": str(workspace_root),
            "conversion_plan_path": str(conversion_plan_path),
            "source_manifest_hash": plan_hash,
            "camera_roles": ["insta_left", "insta_right"],
            "episode_count": 1,
            "eligible_episode_count": len(eligible),
            "selection": "one_random_completed_bimanual_episode_fixed_at_workspace_creation",
            "views": "paired_left_right_360_degree_equirectangular",
            "episodes": assets,
        }
        atomic_json(staging / "grounding" / "annotation_index_active_view.json", index)
        arguments = conversion_plan.get("arguments") or {}
        workspace = {
            "schema": WORKSPACE_SCHEMA,
            "status": "ready_for_grounding",
            "created_at": iso_now(),
            "workspace_root": str(workspace_root),
            "source_capture_root": str(capture_root),
            "source_lerobot": str(conversion_plan_path.parent),
            "conversion_complete_path": str(completion_path),
            "source_read_only": True,
            "camera_roles": ["insta_left", "insta_right"],
            "task": str(arguments.get("task") or task_name_from_capture(capture_root)),
            "source_manifest_hash": plan_hash,
            "selection": {
                "method": "uniform_random_completed_bimanual_episode",
                "episode_index": episode,
                "eligible_episode_count": len(eligible),
                "source_frame_indices": {
                    "insta_left": int(selected["left"]["source_index"]),
                    "insta_right": int(selected["right"]["source_index"]),
                },
                "fixed": True,
            },
            "paths": {
                "grounding_index": "grounding/annotation_index_active_view.json",
                "target_instances": "grounding/target_instances_active_view.json",
            },
            "offline_only": True,
            "camera_access": "none",
            "view_contract": "separate_left_right_360_degree_equirectangular",
        }
        workspace["workspace_manifest_hash"] = canonical_hash(workspace)
        atomic_json(staging / WORKSPACE_FILENAME, workspace)
        atomic_json(
            staging / "grounding" / "target_instances_active_view.json",
            {
                "schema": INSTANCE_SCHEMA,
                "task_name": workspace["task"],
                "session_id": capture_root.name,
                "workspace_manifest_hash": workspace["workspace_manifest_hash"],
                "source_manifest_hash": plan_hash,
                "annotation_mode": "offline_random_bimanual_panorama",
                "selection_contract": "fixed random episode; left and right cameras annotated separately",
                "score_contract": "equal-weight mean of per-instance normalized visibility",
                "optional": True,
                "empty_is_valid": True,
                "reference_assets": assets,
                "instances": [],
                "reviewed_episode_indices": [],
                "updated_at": iso_now(),
            },
        )
        (staging / "grounding" / "target_instances_audit_active_view.jsonl").touch()
        os.replace(staging, workspace_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return workspace_root


class OfflineActiveViewAnnotationStore:
    """Mutable Grounding points over immutable processed active-view reference frames."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.close()

    def close(self) -> str:
        with getattr(self, "lock", threading.RLock()):
            self.workspace_root: Path | None = None
            self.workspace: dict[str, Any] = {}
            self.index: dict[str, Any] = {}
            self.episode_assets: list[dict[str, Any]] = []
            self.episode_indices: list[int] = []
            self.assets_by_id: dict[str, dict[str, Any]] = {}
            self.current_position = 0
            self.target_path: Path | None = None
            self.audit_path: Path | None = None
            self.instances: list[dict[str, Any]] = []
            self.reviewed_episodes: set[int] = set()
        return "Closed the post-processing annotation workspace"

    @property
    def active(self) -> bool:
        return self.workspace_root is not None

    def open_workspace(self, value: str | Path) -> str:
        selected = Path(value).expanduser().resolve(strict=True)
        if selected.is_file() and selected.name == WORKSPACE_FILENAME:
            root = selected.parent
            generated_from_capture = False
        elif selected.is_dir() and (selected / WORKSPACE_FILENAME).is_file():
            root = selected
            generated_from_capture = False
        elif selected.is_file() and selected.name == FISHEYE_WORKSPACE_FILENAME:
            prior = read_json(selected)
            source = Path(str(prior.get("source_capture_root", ""))).expanduser().resolve(strict=True)
            root = prepare_capture_annotation_workspace(source)
            generated_from_capture = True
        elif selected.is_dir() and (selected / FISHEYE_WORKSPACE_FILENAME).is_file():
            prior = read_json(selected / FISHEYE_WORKSPACE_FILENAME)
            source = Path(str(prior.get("source_capture_root", ""))).expanduser().resolve(strict=True)
            root = prepare_capture_annotation_workspace(source)
            generated_from_capture = True
        elif selected.is_file() and selected.name == "workspace_active_view.json":
            raise ValueError("This is a legacy single-camera 360-degree workspace; open the raw capture directory to generate paired panoramas")
        elif selected.is_dir() and (selected / "workspace_active_view.json").is_file():
            raise ValueError("This is a legacy single-camera 360-degree workspace; open the raw capture directory to generate paired panoramas")
        elif selected.is_dir():
            root = prepare_capture_annotation_workspace(selected)
            generated_from_capture = True
        else:
            raise ValueError(f"Enter a raw capture directory, annotation directory, or {WORKSPACE_FILENAME}")
        if not root.is_dir():
            raise ValueError(f"Enter a raw capture directory, annotation directory, or {WORKSPACE_FILENAME}")
        workspace_path = root / WORKSPACE_FILENAME
        if not workspace_path.is_file():
            raise ValueError(describe_missing_workspace(root))
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        if workspace.get("schema") != WORKSPACE_SCHEMA or workspace.get("status") != "ready_for_grounding":
            raise ValueError("workspace is not a completed independent active-view workspace")
        if Path(str(workspace.get("workspace_root", ""))).resolve() != root:
            raise ValueError("workspace root provenance does not match the selected directory")
        expected_workspace_hash = str(workspace.get("workspace_manifest_hash", ""))
        hash_payload = dict(workspace)
        hash_payload.pop("workspace_manifest_hash", None)
        if expected_workspace_hash != canonical_hash(hash_payload):
            raise ValueError("active-view workspace manifest hash mismatch")
        source_root = Path(str(workspace["source_capture_root"])).expanduser().resolve()
        if inside(root, source_root) or inside(source_root, root):
            raise ValueError("active-view workspace overlaps its read-only source capture tree")
        paths = workspace.get("paths") or {}
        index_path = (root / str(paths["grounding_index"])).resolve(strict=True)
        target_path = (root / str(paths["target_instances"])).resolve(strict=True)
        if not inside(index_path, root) or not inside(target_path, root):
            raise ValueError("Grounding paths escape the selected active-view workspace")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("schema") != INDEX_SCHEMA:
            raise ValueError("unsupported active-view Grounding index")
        if index.get("source_manifest_hash") != workspace.get("source_manifest_hash"):
            raise ValueError("Grounding index and workspace source provenance differ")
        assets: list[dict[str, Any]] = []
        for raw in index.get("episodes") or []:
            asset = dict(raw)
            path = (root / str(asset["path"])).resolve(strict=True)
            if not inside(path, root):
                raise ValueError(f"Grounding frame escapes workspace: {path}")
            asset["path"] = str(path)
            asset_id = str(asset.get("asset_id", ""))
            if len(asset_id) != 64 or any(character not in "0123456789abcdef" for character in asset_id):
                raise ValueError(f"invalid Grounding asset id: {asset_id!r}")
            if asset.get("camera_role") not in {"insta_left", "insta_right"}:
                raise ValueError("Bimanual annotation camera_role must be insta_left or insta_right")
            width = int(asset.get("reference_width") or 0)
            height = int(asset.get("reference_height") or 0)
            if (
                asset.get("reference_projection") != "equirectangular"
                or width != 2 * height
                or width < 1024
            ):
                raise ValueError("Bimanual annotation requires a 2:1 equirectangular 360-degree panorama")
            assets.append(asset)
        if not assets:
            raise ValueError("active-view workspace has no processed episode frames")
        assets.sort(key=lambda item: int(item["episode_index"]))
        target = json.loads(target_path.read_text(encoding="utf-8"))
        if target.get("schema") != INSTANCE_SCHEMA:
            raise ValueError("unsupported target-instance sidecar")
        if target.get("workspace_manifest_hash") != expected_workspace_hash:
            raise ValueError("target-instance sidecar belongs to another active-view workspace")
        if target.get("source_manifest_hash") != workspace.get("source_manifest_hash"):
            raise ValueError("target-instance source provenance mismatch")
        known_episodes = {int(asset["episode_index"]) for asset in assets}
        for episode in known_episodes:
            roles = {
                str(asset["camera_role"])
                for asset in assets
                if int(asset["episode_index"]) == episode
            }
            if roles != {"insta_left", "insta_right"}:
                raise ValueError(f"Episode {episode} does not contain both left- and right-hand panoramas")
        instances = [dict(item) for item in target.get("instances") or []]
        for item in instances:
            if int(item["episode_index"]) not in known_episodes:
                raise ValueError("target-instance sidecar references an unknown episode")
        raw_reviewed = target.get("reviewed_episode_indices")
        reviewed_episodes = (
            {int(value) for value in raw_reviewed}
            if isinstance(raw_reviewed, list)
            else {int(item["episode_index"]) for item in instances}
        )
        if not reviewed_episodes <= known_episodes:
            raise ValueError("target-instance sidecar marks an unknown episode as reviewed")

        with self.lock:
            self.workspace_root = root
            self.workspace = workspace
            self.index = index
            self.episode_assets = assets
            self.episode_indices = sorted(known_episodes)
            self.assets_by_id = {str(item["asset_id"]): item for item in assets}
            self.current_position = 0
            self.target_path = target_path
            self.audit_path = target_path.with_name("target_instances_audit_active_view.jsonl")
            self.audit_path.touch(exist_ok=True)
            self.instances = instances
            self.reviewed_episodes = reviewed_episodes
        if generated_from_capture:
            episode = int(assets[0]["episode_index"])
            return (
                f"Randomly selected Episode {episode}; separate left- and right-hand 360-degree "
                f"panoramas are loaded. Annotations are saved automatically to {root} (no camera connection)."
            )
        return f"Opened offline left/right panorama annotation workspace: {root}"

    def status(self) -> dict[str, Any]:
        with self.lock:
            if not self.active:
                return {
                    "mode": "postprocess",
                    "offline": None,
                    "count": 0,
                    "items": [],
                    "assets": [],
                    "default_episode_index": None,
                }
            episode = self.episode_indices[self.current_position]
            current_assets = [
                dict(asset)
                for asset in self.episode_assets
                if int(asset["episode_index"]) == episode
            ]
            current_items = [dict(item) for item in self.instances if int(item["episode_index"]) == episode]
            annotated_episodes = len({int(item["episode_index"]) for item in self.instances})
            return {
                "mode": "offline",
                "optional": True,
                "empty_is_valid": True,
                "count": len(self.instances),
                "items": current_items,
                "assets": current_assets,
                "default_episode_index": episode,
                "path": str(self.target_path) if self.target_path else None,
                "offline": {
                    "workspace_root": str(self.workspace_root),
                    "task": self.workspace.get("task"),
                    "camera_roles": ["insta_left", "insta_right"],
                    "view_contract": self.workspace.get("view_contract"),
                    "episode_index": episode,
                    "episode_position": self.current_position,
                    "episode_count": len(self.episode_indices),
                    "annotated_episode_count": annotated_episodes,
                    "reviewed_episode_count": len(self.reviewed_episodes),
                    "current_episode_reviewed": episode in self.reviewed_episodes,
                    "target_instance_count": len(self.instances),
                    "can_previous": self.current_position > 0,
                    "can_next": self.current_position + 1 < len(self.episode_indices),
                },
            }

    def navigate(self, value: str | int) -> str:
        with self.lock:
            if not self.active:
                raise ValueError("no offline active-view workspace is open")
            if value == "previous":
                self.current_position = max(0, self.current_position - 1)
            elif value == "next":
                self.current_position = min(len(self.episode_indices) - 1, self.current_position + 1)
            else:
                episode = int(value)
                matching = [
                    index
                    for index, known_episode in enumerate(self.episode_indices)
                    if known_episode == episode
                ]
                if not matching:
                    raise ValueError(f"episode {episode} is absent from the Grounding index")
                self.current_position = matching[0]
            episode = self.episode_indices[self.current_position]
            return f"Offline Grounding episode {episode}"

    def complete_current_episode(self, timestamp: InputTimestamp) -> str:
        with self.lock:
            if not self.active:
                raise ValueError("no offline active-view workspace is open")
            episode = self.episode_indices[self.current_position]
            was_reviewed = episode in self.reviewed_episodes
            self.reviewed_episodes.add(episode)
            self._persist(
                "episode_review_completed",
                {
                    "episode_index": episode,
                    "target_instance_count": sum(
                        int(item["episode_index"]) == episode for item in self.instances
                    ),
                    "recorded_unix_time_ns": timestamp.unix_time_ns,
                    "already_reviewed": was_reviewed,
                },
            )
            if self.current_position + 1 < len(self.episode_indices):
                self.current_position += 1
                next_episode = self.episode_indices[self.current_position]
                return f"Episode {episode} confirmed; showing episode {next_episode}"
            return f"Episode {episode} confirmed; all episodes have been reviewed"

    def target_reference_image(self, asset_id: str) -> tuple[Path, str] | None:
        with self.lock:
            asset = self.assets_by_id.get(str(asset_id))
            if asset is None:
                return None
            path = Path(str(asset["path"])).resolve()
            if not path.is_file() or sha256_file(path) != str(asset["sha256"]):
                return None
            return path, str(asset["media_type"])

    def add(self, value: dict[str, Any], timestamp: InputTimestamp) -> str:
        if not isinstance(value, dict):
            raise ValueError("Target annotation payload must be an object")
        with self.lock:
            if not self.active:
                raise ValueError("no offline active-view workspace is open")
            current_episode = self.episode_indices[self.current_position]
            asset_id = str(value.get("asset_id", ""))
            current = self.assets_by_id.get(asset_id)
            if current is None or int(current["episode_index"]) != current_episode:
                raise ValueError("click does not belong to the currently displayed processed frame")
            x_norm = float(value["x_norm"])
            y_norm = float(value["y_norm"])
            if not 0.0 <= x_norm <= 1.0 or not 0.0 <= y_norm <= 1.0:
                raise ValueError("Target coordinates must be inside [0,1]")
            if int(value.get("reference_width") or 0) != int(current["reference_width"]) or int(
                value.get("reference_height") or 0
            ) != int(current["reference_height"]):
                raise ValueError("browser image dimensions do not match the processed Grounding frame")
            annotation_id = uuid.uuid4().hex
            record = {
                "annotation_id": annotation_id,
                "target_instance_id": annotation_id,
                "label": "target_object",
                "point_label": 1,
                "hand_role": str(current["hand_role"]),
                "camera_role": str(current["camera_role"]),
                "reference_asset_id": asset_id,
                "reference_projection": "equirectangular",
                "horizontal_fov_deg": 360.0,
                "vertical_fov_deg": 180.0,
                "reference_width": int(current["reference_width"]),
                "reference_height": int(current["reference_height"]),
                "x_norm": x_norm,
                "y_norm": y_norm,
                "source_frame_index": int(current["source_frame_index"]),
                "capture_segment_index": int(current["episode_index"]) + 1,
                "episode_index": int(current["episode_index"]),
                "capture_running": False,
                "annotation_mode": "offline_bimanual_panorama_workspace",
                "created_unix_time_ns": timestamp.unix_time_ns,
                "created_monotonic_time_ns": timestamp.monotonic_time_ns,
                "created_monotonic_raw_time_ns": timestamp.monotonic_raw_time_ns or None,
                "input_source": timestamp.source,
            }
            self.instances.append(record)
            self._persist("target_instance_added", record)
            return f"Added offline target for episode {current['episode_index']}"

    def delete(self, annotation_id: str, timestamp: InputTimestamp) -> str:
        del timestamp
        with self.lock:
            selected = next(
                (item for item in self.instances if item.get("annotation_id") == annotation_id),
                None,
            )
            if selected is None:
                return "Target annotation not found"
            self.instances = [
                item for item in self.instances if item.get("annotation_id") != annotation_id
            ]
            self._persist("target_instance_deleted", selected)
            return f"Deleted offline target from episode {selected['episode_index']}"

    def clear_current_episode(
        self, timestamp: InputTimestamp, camera_role: str | None = None
    ) -> str:
        del timestamp
        with self.lock:
            if not self.active:
                raise ValueError("no offline active-view workspace is open")
            episode = self.episode_indices[self.current_position]
            if camera_role is not None and camera_role not in {"insta_left", "insta_right"}:
                raise ValueError("camera_role must be insta_left or insta_right")
            removed = [
                item
                for item in self.instances
                if int(item["episode_index"]) == episode
                and (camera_role is None or item.get("camera_role") == camera_role)
            ]
            if not removed:
                hand = "left hand" if camera_role == "insta_left" else "right hand" if camera_role else ""
                return f"Episode {episode} has no {hand} target annotations"
            removed_ids = {str(item["annotation_id"]) for item in removed}
            self.instances = [
                item
                for item in self.instances
                if str(item.get("annotation_id")) not in removed_ids
            ]
            self._persist(
                "episode_target_instances_cleared",
                {"episode_index": episode, "camera_role": camera_role, "items": removed},
            )
            hand = "left-hand" if camera_role == "insta_left" else "right-hand" if camera_role else "left/right"
            return f"Cleared {hand} targets from Episode {episode}"

    def _persist(self, action: str, payload: Any) -> None:
        assert self.target_path is not None and self.audit_path is not None
        target_payload = {
            "schema": INSTANCE_SCHEMA,
            "task_name": self.workspace.get("task"),
            "session_id": None,
            "workspace_manifest_hash": self.workspace.get("workspace_manifest_hash"),
            "source_manifest_hash": self.workspace.get("source_manifest_hash"),
            "annotation_mode": "offline_bimanual_panorama_workspace",
            "selection_contract": "fixed random episode; left and right hands annotated separately",
            "score_contract": "equal-weight mean of per-instance normalized visibility",
            "optional": True,
            "empty_is_valid": True,
            "reference_assets": self.episode_assets,
            "instances": self.instances,
            "reviewed_episode_indices": sorted(self.reviewed_episodes),
            "updated_at": iso_now(),
        }
        atomic_json(self.target_path, target_payload)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema": "instaumi.active_view.target_instances.audit.v1",
                        "audit_action": action,
                        "recorded_at": iso_now(),
                        "workspace_manifest_hash": self.workspace.get("workspace_manifest_hash"),
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
