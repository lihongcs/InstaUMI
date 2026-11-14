#!/usr/bin/env python3
"""Safe helpers for running data processing on a fast local workspace.

The source data is always treated as read-only.  Cached inputs and published
directories become visible only after their contents have been flushed and
verified.  The module intentionally uses only the Python standard library so
that every data-processing stage can import it without changing environments.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence


CACHE_SCHEMA = "instaumi.fast_workspace.cache.v2"
# The sharding/key layout did not change.  Keeping its directory name stable
# lets v2 replace an invalid v1 entry in place instead of temporarily retaining
# two complete copies of each multi-gigabyte video.
CACHE_LAYOUT = "v1"
PUBLISH_SCHEMA = "instaumi.fast_workspace.publish.v1"
PUBLISH_MANIFEST = ".fast_workspace_publish.json"
DEFAULT_SAMPLE_BYTES = 1024 * 1024
DEFAULT_COPY_BUFFER_BYTES = 8 * 1024 * 1024
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


class FastWorkspaceError(RuntimeError):
    """Base error for fast-workspace operations."""


class InsufficientSpaceError(FastWorkspaceError):
    """Raised before copying when the destination cannot hold the result."""


class SourceChangedError(FastWorkspaceError):
    """Raised when an input changes while it is being identified or copied."""


class VerificationError(FastWorkspaceError):
    """Raised when a copied file does not match its source."""


class AtomicPublishUnavailableError(FastWorkspaceError):
    """Raised if the platform cannot atomically rename without replacement."""


@dataclass(frozen=True)
class StorageInfo:
    """Best-effort information about the block device backing ``path``."""

    path: str
    device_major: int | None
    device_minor: int | None
    rotational: bool
    confident: bool
    sysfs_path: str | None
    reason: str


@dataclass(frozen=True)
class SpaceInfo:
    """Space available at the filesystem that will contain a destination."""

    path: str
    filesystem_anchor: str
    total_bytes: int
    free_bytes: int
    required_bytes: int
    reserve_bytes: int
    sufficient: bool


@dataclass(frozen=True)
class FileIdentity:
    """Stable cache identity; the source path is deliberately part of the key."""

    path: str
    size_bytes: int
    mtime_ns: int
    sample_sha256: str
    sample_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PublishResult:
    """Description of a successfully and atomically published directory."""

    destination: Path
    manifest: Path
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _FileSnapshot:
    relative_path: str
    size_bytes: int
    mtime_ns: int
    mode: int
    device: int
    inode: int


@dataclass(frozen=True)
class _DirectorySnapshot:
    relative_path: str
    mtime_ns: int
    mode: int


@dataclass(frozen=True)
class _TreeSnapshot:
    directories: tuple[_DirectorySnapshot, ...]
    files: tuple[_FileSnapshot, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing ancestor for {path}")
        candidate = parent
    return candidate


def _rotational_from_sysfs(
    device_major: int,
    device_minor: int,
    *,
    sys_dev_block_root: Path = Path("/sys/dev/block"),
) -> tuple[bool | None, Path | None]:
    link = sys_dev_block_root / f"{device_major}:{device_minor}"
    if not link.exists():
        return None, None
    current = link.resolve()
    # A partition has no queue directory of its own.  Walk towards its parent
    # disk; device-mapper and md devices normally expose queue here directly.
    for candidate in (current, *current.parents):
        rotational_file = candidate / "queue" / "rotational"
        if rotational_file.is_file():
            value = rotational_file.read_text(encoding="ascii").strip()
            if value == "0":
                return False, rotational_file
            if value == "1":
                return True, rotational_file
            return None, rotational_file
        if candidate == sys_dev_block_root.parent:
            break
    return None, None


def inspect_storage(path: os.PathLike[str] | str) -> StorageInfo:
    """Inspect whether ``path`` is backed by rotational storage.

    Unknown devices (tmpfs, unusual virtual filesystems, unavailable sysfs, or
    permission errors) conservatively report ``rotational=True`` and
    ``confident=False``.  Callers therefore never skip useful staging merely
    because device inspection failed.
    """

    requested = Path(path).expanduser().absolute()
    try:
        anchor = _existing_ancestor(requested)
        device = anchor.stat().st_dev
        device_major, device_minor = os.major(device), os.minor(device)
        rotational, source = _rotational_from_sysfs(device_major, device_minor)
        if rotational is None:
            return StorageInfo(
                path=str(requested),
                device_major=device_major,
                device_minor=device_minor,
                rotational=True,
                confident=False,
                sysfs_path=str(source) if source else None,
                reason="rotational flag unavailable; conservatively treating storage as rotational",
            )
        return StorageInfo(
            path=str(requested),
            device_major=device_major,
            device_minor=device_minor,
            rotational=rotational,
            confident=True,
            sysfs_path=str(source),
            reason="read Linux sysfs queue/rotational",
        )
    except (OSError, ValueError) as exc:
        return StorageInfo(
            path=str(requested),
            device_major=None,
            device_minor=None,
            rotational=True,
            confident=False,
            sysfs_path=None,
            reason=f"storage inspection failed ({type(exc).__name__}); conservatively treating storage as rotational",
        )


def is_rotational(path: os.PathLike[str] | str) -> bool:
    """Return the conservative rotational-storage decision for ``path``."""

    return inspect_storage(path).rotational


def shares_physical_storage(
    first: os.PathLike[str] | str,
    second: os.PathLike[str] | str,
) -> bool:
    """Return true only when two paths are confidently on the same device.

    The same filesystem is definitive.  Separate partitions are also treated
    as the same physical device when sysfs resolves both to the same queue
    ``rotational`` file.  Unknown storage deliberately returns false so an
    automatic cache is never skipped on an unproven assumption.
    """

    try:
        first_anchor = _existing_ancestor(Path(first))
        second_anchor = _existing_ancestor(Path(second))
        if first_anchor.stat().st_dev == second_anchor.stat().st_dev:
            return True
        first_info = inspect_storage(first_anchor)
        second_info = inspect_storage(second_anchor)
        return bool(
            first_info.confident
            and second_info.confident
            and first_info.sysfs_path
            and first_info.sysfs_path == second_info.sysfs_path
        )
    except (OSError, ValueError):
        return False


def space_preflight(
    destination: os.PathLike[str] | str,
    required_bytes: int,
    *,
    reserve_bytes: int = 0,
) -> SpaceInfo:
    """Return a non-mutating free-space check for ``destination``."""

    if required_bytes < 0 or reserve_bytes < 0:
        raise ValueError("required_bytes and reserve_bytes must be non-negative")
    requested = Path(destination).expanduser().absolute()
    anchor = _existing_ancestor(requested)
    usage = shutil.disk_usage(anchor)
    return SpaceInfo(
        path=str(requested),
        filesystem_anchor=str(anchor),
        total_bytes=usage.total,
        free_bytes=usage.free,
        required_bytes=required_bytes,
        reserve_bytes=reserve_bytes,
        sufficient=usage.free >= required_bytes + reserve_bytes,
    )


def require_free_space(
    destination: os.PathLike[str] | str,
    required_bytes: int,
    *,
    reserve_bytes: int = 0,
) -> SpaceInfo:
    """Run :func:`space_preflight` and raise if the copy should not start."""

    result = space_preflight(destination, required_bytes, reserve_bytes=reserve_bytes)
    if not result.sufficient:
        needed = result.required_bytes + result.reserve_bytes
        raise InsufficientSpaceError(
            f"insufficient space at {result.filesystem_anchor}: "
            f"need {needed} bytes ({result.required_bytes} data + "
            f"{result.reserve_bytes} reserve), have {result.free_bytes} bytes free"
        )
    return result


def _stat_signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"expected a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def _sample_sha256(path: Path, sample_bytes: int) -> str:
    if sample_bytes <= 0:
        raise ValueError("sample_bytes must be positive")
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(b"instaumi-fast-workspace-sample-v1\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    with path.open("rb", buffering=0) as handle:
        head_size = min(size, sample_bytes)
        digest.update(handle.read(head_size))
        if size > sample_bytes:
            tail_start = max(head_size, size - sample_bytes)
            handle.seek(tail_start)
            digest.update(handle.read(size - tail_start))
    return digest.hexdigest()


def identify_file(
    source: os.PathLike[str] | str,
    *,
    sample_bytes: int = DEFAULT_SAMPLE_BYTES,
) -> FileIdentity:
    """Identify a source by resolved path, size, mtime, and head/tail hash."""

    path = Path(source).expanduser().resolve(strict=True)
    before = _stat_signature(path)
    sample_hash = _sample_sha256(path, sample_bytes)
    after = _stat_signature(path)
    if before != after:
        raise SourceChangedError(f"source changed while being identified: {path}")
    return FileIdentity(
        path=str(path),
        size_bytes=before[2],
        mtime_ns=before[3],
        sample_sha256=sample_hash,
        sample_bytes=sample_bytes,
    )


def _identity_key(identity: FileIdentity) -> str:
    encoded = json.dumps(
        identity.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, *, buffer_bytes: int = DEFAULT_COPY_BUFFER_BYTES) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            chunk = handle.read(buffer_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _assert_source_matches_identity(path: Path, identity: FileIdentity, *, context: str) -> None:
    """Reject a source whose stable metadata no longer matches a frozen identity."""

    try:
        resolved = path.expanduser().resolve(strict=True)
        signature = _stat_signature(resolved)
    except (OSError, ValueError) as exc:
        raise SourceChangedError(f"source became unavailable {context}: {path}") from exc
    if (
        str(resolved) != identity.path
        or signature[2] != identity.size_bytes
        or signature[3] != identity.mtime_ns
    ):
        raise SourceChangedError(f"source changed {context}: {resolved}")


def _sha256_file_stable(
    path: Path,
    *,
    expected_identity: FileIdentity | None = None,
    buffer_bytes: int = DEFAULT_COPY_BUFFER_BYTES,
) -> str:
    """Hash a regular file and reject observable changes during the read."""

    try:
        resolved = path.expanduser().resolve(strict=True)
        before = _stat_signature(resolved)
    except (OSError, ValueError) as exc:
        if expected_identity is not None:
            raise SourceChangedError(f"source became unavailable before full hash: {path}") from exc
        raise
    if expected_identity is not None:
        _assert_source_matches_identity(resolved, expected_identity, context="before full hash")
    try:
        digest = _sha256_file(resolved, buffer_bytes=buffer_bytes)
        after = _stat_signature(resolved)
    except (OSError, ValueError) as exc:
        if expected_identity is not None:
            raise SourceChangedError(f"source became unavailable during full hash: {resolved}") from exc
        raise
    if before != after:
        raise SourceChangedError(f"file changed during full hash: {resolved}")
    if expected_identity is not None:
        _assert_source_matches_identity(resolved, expected_identity, context="during full hash")
    return digest


def _require_same_identity(expected: FileIdentity, actual: FileIdentity, *, context: str) -> None:
    if actual != expected:
        raise SourceChangedError(
            f"source identity changed {context}: expected={expected.to_dict()}, actual={actual.to_dict()}"
        )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_fsynced(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _copy_with_hash(
    source: Path,
    destination: Path,
    *,
    expected_identity: FileIdentity | None = None,
    buffer_bytes: int = DEFAULT_COPY_BUFFER_BYTES,
) -> tuple[int, str]:
    before = _stat_signature(source)
    if expected_identity is not None and (
        str(source.resolve()) != expected_identity.path
        or before[2] != expected_identity.size_bytes
        or before[3] != expected_identity.mtime_ns
    ):
        raise SourceChangedError(f"source changed before copy: {source}")

    digest = hashlib.sha256()
    copied = 0
    with source.open("rb", buffering=0) as src, destination.open("xb", buffering=0) as dst:
        while True:
            chunk = src.read(buffer_bytes)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())

    after = _stat_signature(source)
    if before != after:
        raise SourceChangedError(f"source changed during copy: {source}")
    if copied != before[2]:
        raise VerificationError(
            f"short copy for {source}: expected {before[2]} bytes, copied {copied}"
        )
    return copied, digest.hexdigest()


def _load_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_cache_entry(
    entry: Path,
    identity: FileIdentity,
    *,
    verify_full_hash: bool,
    expected_source_sha256: str | None = None,
) -> Path | None:
    if entry.is_symlink() or not entry.is_dir():
        return None
    manifest = _load_json(entry / "manifest.json")
    if not manifest or manifest.get("schema") != CACHE_SCHEMA:
        return None
    if manifest.get("source_identity") != identity.to_dict():
        return None
    if manifest.get("size_bytes") != identity.size_bytes:
        return None
    cache_name = manifest.get("cache_file")
    if not isinstance(cache_name, str) or Path(cache_name).name != cache_name:
        return None
    cached = entry / cache_name
    try:
        cached_info = cached.stat()
        if not stat.S_ISREG(cached_info.st_mode) or cached_info.st_size != identity.size_bytes:
            return None
        if _sample_sha256(cached, identity.sample_bytes) != identity.sample_sha256:
            return None
        if verify_full_hash:
            expected_hash = manifest.get("sha256")
            if not isinstance(expected_hash, str):
                return None
            source_path = Path(identity.path)
            if expected_source_sha256 is None:
                source_hash = _sha256_file_stable(source_path, expected_identity=identity)
            else:
                _assert_source_matches_identity(
                    source_path,
                    identity,
                    context="between identity freeze and cache-hit validation",
                )
                source_hash = expected_source_sha256
            cached_hash = _sha256_file_stable(cached)
            if source_hash != expected_hash or cached_hash != expected_hash:
                return None
    except (OSError, ValueError):
        return None
    return cached


def _verify_full_cache_pair(
    source: Path,
    cached: Path,
    identity: FileIdentity,
    frozen_source_sha256: str,
) -> None:
    """Fully re-read one frozen source/cache pair and validate its manifest."""

    source_hash = _sha256_file_stable(source, expected_identity=identity)
    if source_hash != frozen_source_sha256:
        raise SourceChangedError(
            f"source content changed after identity freeze: {source}"
        )

    cached_hash = _sha256_file_stable(cached)
    manifest = _load_json(cached.parent / "manifest.json")
    if not manifest or manifest.get("schema") != CACHE_SCHEMA:
        raise VerificationError(f"cache manifest is missing or invalid: {cached.parent}")
    if manifest.get("source_identity") != identity.to_dict():
        raise VerificationError(f"cache manifest source identity mismatch: {cached.parent}")
    if manifest.get("size_bytes") != identity.size_bytes:
        raise VerificationError(f"cache manifest size mismatch: {cached.parent}")
    if manifest.get("cache_file") != cached.name:
        raise VerificationError(f"cache manifest payload mismatch: {cached.parent}")
    manifest_hash = manifest.get("sha256")
    if not isinstance(manifest_hash, str):
        raise VerificationError(f"cache manifest SHA-256 is missing: {cached.parent}")
    if manifest_hash != frozen_source_sha256 or cached_hash != frozen_source_sha256:
        raise VerificationError(
            f"full source/cache/manifest SHA-256 mismatch: source={source}, cached={cached}"
        )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cache_input_file_frozen(
    identity: FileIdentity,
    fast_work_root: os.PathLike[str] | str,
    *,
    reserve_bytes: int = 0,
    verify_full_hash_on_hit: bool = False,
    frozen_source_sha256: str | None = None,
) -> Path:
    source_path = Path(identity.path)
    root = Path(fast_work_root).expanduser().absolute()
    key = _identity_key(identity)
    shard = root / "input-cache" / CACHE_LAYOUT / key[:2]
    entry = shard / key
    lock = root / "input-cache" / "locks" / f"{key}.lock"

    with _exclusive_lock(lock):
        _assert_source_matches_identity(
            source_path,
            identity,
            context="between identity freeze and cache lookup",
        )
        hit = _valid_cache_entry(
            entry,
            identity,
            verify_full_hash=verify_full_hash_on_hit,
            expected_source_sha256=frozen_source_sha256,
        )
        if hit is not None:
            return hit

        # Invalid entries are generated cache data, never source data.  Remove
        # them only while holding the identity-specific lock.
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.exists():
            shutil.rmtree(entry)

        overhead = max(1024 * 1024, identity.size_bytes // 100)
        require_free_space(shard, identity.size_bytes + overhead, reserve_bytes=reserve_bytes)
        shard.mkdir(parents=True, exist_ok=True)
        partial = shard / f".{key}.partial-{os.getpid()}-{uuid.uuid4().hex}"
        partial.mkdir(mode=0o700)
        suffix = source_path.suffix
        cache_name = f"payload{suffix}" if suffix else "payload"
        cached = partial / cache_name
        try:
            copied, full_hash = _copy_with_hash(
                source_path, cached, expected_identity=identity
            )
            if frozen_source_sha256 is not None and full_hash != frozen_source_sha256:
                raise SourceChangedError(
                    f"source content changed between identity freeze and copy: {source_path}"
                )
            if _sample_sha256(cached, identity.sample_bytes) != identity.sample_sha256:
                raise VerificationError(f"cached sample does not match source: {source_path}")
            # The streaming digest describes bytes read from the source.  Read
            # the new NVMe copy once before publishing it so storage/write
            # corruption anywhere in the file cannot become a valid cache hit.
            if _sha256_file(cached) != full_hash:
                raise VerificationError(f"cached file hash does not match source: {source_path}")
            manifest = {
                "schema": CACHE_SCHEMA,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_identity": identity.to_dict(),
                "cache_file": cache_name,
                "size_bytes": copied,
                "sha256": full_hash,
            }
            _write_json_fsynced(partial / "manifest.json", manifest)
            _fsync_directory(partial)
            os.rename(partial, entry)
            _fsync_directory(shard)
        except BaseException:
            if partial.exists():
                shutil.rmtree(partial)
            raise

        # The copy path already compared the streaming source SHA-256 with a
        # complete read-back of the cached payload.  The public single/batch
        # wrappers perform their final frozen-pair audit after this returns.
        hit = _valid_cache_entry(entry, identity, verify_full_hash=False)
        if hit is None:
            raise VerificationError(f"new cache entry failed validation: {entry}")
        return hit


def cache_input_file(
    source: os.PathLike[str] | str,
    fast_work_root: os.PathLike[str] | str,
    *,
    sample_bytes: int = DEFAULT_SAMPLE_BYTES,
    reserve_bytes: int = 0,
    verify_full_hash_on_hit: bool = False,
) -> Path:
    """Copy one immutable input into an identity-addressed local cache.

    The first copy is fully SHA-256 hashed while streaming.  A normal hit
    verifies its manifest, size, and head/tail hash.  Strict verification
    freezes a complete source SHA-256 and, before returning, fully re-reads the
    current source and cached payload and compares both with the manifest.
    Partial entries never become hits.
    """

    identity = identify_file(source, sample_bytes=sample_bytes)
    source_path = Path(identity.path)
    frozen_hash = (
        _sha256_file_stable(source_path, expected_identity=identity)
        if verify_full_hash_on_hit
        else None
    )
    cached = _cache_input_file_frozen(
        identity,
        fast_work_root,
        reserve_bytes=reserve_bytes,
        verify_full_hash_on_hit=verify_full_hash_on_hit,
        frozen_source_sha256=frozen_hash,
    )
    final_identity = identify_file(source_path, sample_bytes=identity.sample_bytes)
    _require_same_identity(identity, final_identity, context="while caching")
    if frozen_hash is not None:
        _verify_full_cache_pair(source_path, cached, identity, frozen_hash)
    return cached


def cache_input_files(
    sources: Iterable[os.PathLike[str] | str],
    fast_work_root: os.PathLike[str] | str,
    *,
    sample_bytes: int = DEFAULT_SAMPLE_BYTES,
    reserve_bytes: int = 0,
    verify_full_hash_on_hit: bool = False,
) -> list[Path]:
    """Cache a frozen set of files sequentially and return paths in input order.

    All source identities are captured before the first cache operation and
    checked again after the final one.  In strict mode, full source hashes are
    frozen up front and every source/cache/manifest triple is fully re-read at
    the end, so a long multi-file staging pass cannot silently mix generations.
    """

    source_values = list(sources)
    identities = [identify_file(path, sample_bytes=sample_bytes) for path in source_values]
    source_paths = [Path(identity.path) for identity in identities]
    frozen_hashes: list[str | None]
    if verify_full_hash_on_hit:
        frozen_hashes = [
            _sha256_file_stable(path, expected_identity=identity)
            for path, identity in zip(source_paths, identities)
        ]
    else:
        frozen_hashes = [None] * len(identities)

    cached_paths = [
        _cache_input_file_frozen(
            identity,
            fast_work_root,
            reserve_bytes=reserve_bytes,
            verify_full_hash_on_hit=verify_full_hash_on_hit,
            frozen_source_sha256=frozen_hash,
        )
        for identity, frozen_hash in zip(identities, frozen_hashes)
    ]

    final_identities = [
        identify_file(path, sample_bytes=identity.sample_bytes)
        for path, identity in zip(source_paths, identities)
    ]
    for expected, actual in zip(identities, final_identities):
        _require_same_identity(expected, actual, context="during multi-file caching")

    if verify_full_hash_on_hit:
        for source_path, cached_path, identity, frozen_hash in zip(
            source_paths,
            cached_paths,
            identities,
            frozen_hashes,
        ):
            assert frozen_hash is not None
            _verify_full_cache_pair(source_path, cached_path, identity, frozen_hash)
    return cached_paths


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _snapshot_tree(root: Path) -> _TreeSnapshot:
    if not root.is_dir():
        raise NotADirectoryError(root)
    directories: list[_DirectorySnapshot] = []
    files: list[_FileSnapshot] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current_text, directory_names, file_names in os.walk(
        root, followlinks=False, onerror=raise_walk_error
    ):
        current = Path(current_text)
        directory_names.sort()
        file_names.sort()
        relative_current = current.relative_to(root)
        current_info = current.stat()
        directories.append(
            _DirectorySnapshot(
                relative_path=relative_current.as_posix(),
                mtime_ns=current_info.st_mtime_ns,
                mode=stat.S_IMODE(current_info.st_mode),
            )
        )
        for name in tuple(directory_names):
            child = current / name
            if child.is_symlink():
                raise ValueError(f"publish source may not contain symlinks: {child}")
        for name in file_names:
            child = current / name
            if child.is_symlink():
                raise ValueError(f"publish source may not contain symlinks: {child}")
            info = child.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"publish source contains a non-regular file: {child}")
            relative = child.relative_to(root).as_posix()
            if relative == PUBLISH_MANIFEST:
                raise ValueError(f"publish source uses reserved path: {PUBLISH_MANIFEST}")
            files.append(
                _FileSnapshot(
                    relative_path=relative,
                    size_bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    mode=stat.S_IMODE(info.st_mode),
                    device=info.st_dev,
                    inode=info.st_ino,
                )
            )
    return _TreeSnapshot(tuple(directories), tuple(files))


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a directory while guaranteeing no replacement."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:
        raise AtomicPublishUnavailableError(
            "renameat2(RENAME_NOREPLACE) is unavailable; refusing a non-atomic publish"
        ) from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), str(destination))
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise AtomicPublishUnavailableError(
            "filesystem does not support renameat2(RENAME_NOREPLACE); refusing publish"
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def atomic_exchange_directories(first: os.PathLike[str] | str, second: os.PathLike[str] | str) -> None:
    """Atomically exchange two existing sibling directories without fallback.

    This is the only safe overwrite primitive used by the pipeline: if Linux
    or the destination filesystem cannot provide ``RENAME_EXCHANGE``, the
    operation fails before either directory name is changed.
    """

    first_path = Path(first).expanduser().absolute()
    second_path = Path(second).expanduser().absolute()
    first_parent = first_path.parent.resolve(strict=True)
    second_parent = second_path.parent.resolve(strict=True)
    first_path = first_parent / first_path.name
    second_path = second_parent / second_path.name
    if first_parent != second_parent:
        raise ValueError(
            f"atomic directory exchange requires sibling paths: {first_path}, {second_path}"
        )
    if first_path == second_path:
        raise ValueError(f"atomic directory exchange requires distinct paths: {first_path}")
    for path in (first_path, second_path):
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"atomic directory exchange requires real directories: {path}")

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:
        raise AtomicPublishUnavailableError(
            "renameat2(RENAME_EXCHANGE) is unavailable; refusing overwrite"
        ) from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(first_path),
        _AT_FDCWD,
        os.fsencode(second_path),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EXDEV):
        raise AtomicPublishUnavailableError(
            "filesystem does not support renameat2(RENAME_EXCHANGE); refusing overwrite"
        )
    raise OSError(error_number, os.strerror(error_number), f"{first_path} <-> {second_path}")


def _verify_source_snapshot(source: Path, expected: _FileSnapshot) -> None:
    current = source.stat()
    signature = (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino)
    wanted = (expected.size_bytes, expected.mtime_ns, expected.device, expected.inode)
    if signature != wanted:
        raise SourceChangedError(f"publish source changed during copy: {source}")


def publish_directory(
    work_directory: os.PathLike[str] | str,
    final_directory: os.PathLike[str] | str,
    *,
    reserve_bytes: int = 0,
    copy_buffer_bytes: int = DEFAULT_COPY_BUFFER_BYTES,
    logical_final_directory: os.PathLike[str] | str | None = None,
) -> PublishResult:
    """Verify and atomically publish a completed work directory.

    ``final_directory`` must not exist.  Files are copied sequentially into a
    sibling partial directory, SHA-256 verified from the destination, flushed,
    recorded in a publish manifest, and finally renamed with Linux
    ``RENAME_NOREPLACE``.  Failures remove only the private partial directory.
    ``logical_final_directory`` records the eventual public path when this
    operation intentionally lands at a sibling overwrite candidate first.
    """

    if copy_buffer_bytes <= 0:
        raise ValueError("copy_buffer_bytes must be positive")
    source_root = Path(work_directory).expanduser().resolve(strict=True)
    final = Path(final_directory).expanduser().absolute()
    final_parent = final.parent
    if not final_parent.is_dir():
        raise FileNotFoundError(f"publish parent does not exist: {final_parent}")
    final = final_parent.resolve(strict=True) / final.name
    if final.exists() or final.is_symlink():
        raise FileExistsError(f"publish target already exists: {final}")
    if _is_relative_to(final, source_root):
        raise ValueError("final_directory may not be inside work_directory")
    if _is_relative_to(source_root, final):
        raise ValueError("work_directory may not be inside final_directory")
    logical_final = final
    if logical_final_directory is not None:
        logical_value = Path(logical_final_directory).expanduser().absolute()
        logical_parent = logical_value.parent.resolve(strict=True)
        logical_final = logical_parent / logical_value.name

    snapshot = _snapshot_tree(source_root)
    overhead = max(1024 * 1024, snapshot.total_bytes // 100)
    require_free_space(final_parent, snapshot.total_bytes + overhead, reserve_bytes=reserve_bytes)

    partial = final_parent / f".{final.name}.partial-{os.getpid()}-{uuid.uuid4().hex}"
    partial.mkdir(mode=0o700)
    published = False
    file_records: list[dict[str, object]] = []
    try:
        for directory in snapshot.directories:
            if directory.relative_path == ".":
                continue
            (partial / directory.relative_path).mkdir()

        for item in snapshot.files:
            source = source_root / item.relative_path
            destination = partial / item.relative_path
            copied, source_hash = _copy_with_hash(
                source, destination, buffer_bytes=copy_buffer_bytes
            )
            _verify_source_snapshot(source, item)
            destination_info = destination.stat()
            if destination_info.st_size != item.size_bytes or copied != item.size_bytes:
                raise VerificationError(f"published file size mismatch: {destination}")
            destination_hash = _sha256_file(destination, buffer_bytes=copy_buffer_bytes)
            if destination_hash != source_hash:
                raise VerificationError(f"published file hash mismatch: {destination}")
            shutil.copystat(source, destination, follow_symlinks=False)
            _fsync_file(destination)
            file_records.append(
                {
                    "path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "mtime_ns": item.mtime_ns,
                    "sha256": source_hash,
                }
            )

        manifest_value = {
            "schema": PUBLISH_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(source_root),
            "destination_directory": str(logical_final),
            "file_count": len(file_records),
            "total_bytes": snapshot.total_bytes,
            "files": file_records,
        }
        if logical_final != final:
            manifest_value["published_via_candidate"] = str(final)
        _write_json_fsynced(partial / PUBLISH_MANIFEST, manifest_value)

        # Restore directory metadata after all children (including the
        # manifest) have been created, then flush from leaves to root.
        for directory in reversed(snapshot.directories):
            source_dir = source_root if directory.relative_path == "." else source_root / directory.relative_path
            destination_dir = partial if directory.relative_path == "." else partial / directory.relative_path
            shutil.copystat(source_dir, destination_dir, follow_symlinks=False)
            _fsync_directory(destination_dir)

        if _snapshot_tree(source_root) != snapshot:
            raise SourceChangedError(f"publish source tree changed during copy: {source_root}")
        _rename_noreplace(partial, final)
        published = True
        _fsync_directory(final_parent)
    finally:
        if not published and partial.exists():
            shutil.rmtree(partial)

    return PublishResult(
        destination=final,
        manifest=final / PUBLISH_MANIFEST,
        file_count=len(snapshot.files),
        total_bytes=snapshot.total_bytes,
    )


class FastWorkspace:
    """Configured convenience wrapper used by individual pipeline stages."""

    def __init__(self, root: os.PathLike[str] | str, *, reserve_bytes: int = 0) -> None:
        self.root = Path(root).expanduser().absolute()
        self.reserve_bytes = reserve_bytes

    def cache_file(self, source: os.PathLike[str] | str, **kwargs: object) -> Path:
        kwargs.setdefault("reserve_bytes", self.reserve_bytes)
        return cache_input_file(source, self.root, **kwargs)

    def cache_files(
        self, sources: Sequence[os.PathLike[str] | str], **kwargs: object
    ) -> list[Path]:
        kwargs.setdefault("reserve_bytes", self.reserve_bytes)
        return cache_input_files(sources, self.root, **kwargs)

    def publish(
        self,
        work_directory: os.PathLike[str] | str,
        final_directory: os.PathLike[str] | str,
        **kwargs: object,
    ) -> PublishResult:
        kwargs.setdefault("reserve_bytes", self.reserve_bytes)
        return publish_directory(work_directory, final_directory, **kwargs)

    @staticmethod
    def storage_info(path: os.PathLike[str] | str) -> StorageInfo:
        return inspect_storage(path)
