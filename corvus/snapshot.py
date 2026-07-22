from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from corvus.security import SecurityError

_DEFAULT_MAX_FILES: Final = 10_000
_DEFAULT_MAX_FILE_BYTES: Final = 100 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES: Final = 1024 * 1024 * 1024
_PERMANENT_DIRECTORY_NAMES: Final = frozenset(
    {
        ".git",
        ".corvus",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".cache",
        "cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "build",
        "dist",
        "work",
        "outputs",
    }
)
_PERMANENT_FILE_PATTERNS: Final = (
    ".env",
    ".env.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*credential*",
    "*credentials*",
    "*secret*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
)


@dataclass(frozen=True)
class SnapshotPolicy:
    include: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    max_files: int = _DEFAULT_MAX_FILES
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_path_depth: int = 32
    max_name_bytes: int = 255

    def __post_init__(self) -> None:
        for name in (
            "max_files",
            "max_file_bytes",
            "max_total_bytes",
            "max_path_depth",
            "max_name_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, order=True)
class SnapshotFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotResult:
    destination: Path
    files: tuple[SnapshotFile, ...]
    total_bytes: int
    digest: str

    @property
    def metadata(self) -> tuple[SnapshotFile, ...]:
        return self.files


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0) or 0
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _reject_link_components(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            raise SecurityError(f"snapshot {label} path does not exist: {current}")
        if _is_link_or_reparse(current):
            raise SecurityError(f"snapshot {label} path contains a link or reparse point")


def _is_permanently_excluded(relative: Path, *, is_directory: bool) -> bool:
    if any(part.casefold() in _PERMANENT_DIRECTORY_NAMES for part in relative.parts):
        return True
    if is_directory:
        return False
    name = relative.name.casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _PERMANENT_FILE_PATTERNS)


def is_snapshot_path_permanently_excluded(relative: str | Path) -> bool:
    return _is_permanently_excluded(Path(relative), is_directory=False)


def _matches_rule(relative: Path, patterns: tuple[str, ...]) -> bool:
    path = relative.as_posix()
    return any(
        fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(relative.name, pattern)
        for pattern in patterns
    )


def _enforce_path_bounds(relative: Path, policy: SnapshotPolicy) -> None:
    if len(relative.parts) > policy.max_path_depth:
        raise ValueError(f"snapshot path exceeds path-depth limit: {relative.as_posix()}")
    if any(len(part.encode("utf-8")) > policy.max_name_bytes for part in relative.parts):
        raise ValueError(f"snapshot path exceeds component-name byte limit: {relative.as_posix()}")


def _canonical_digest(files: tuple[SnapshotFile, ...]) -> str:
    payload = [
        {"relative_path": item.relative_path, "sha256": item.sha256, "size": item.size}
        for item in files
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected: os.stat_result,
    max_bytes: int,
) -> SnapshotFile:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.corvus-{uuid4().hex}.tmp")
    digest = hashlib.sha256()
    size = 0
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(source, source_flags), "rb") as reader:
            opened = os.fstat(reader.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
                raise SecurityError("snapshot source changed between inspection and copy")
            with os.fdopen(os.open(temporary, output_flags, 0o600), "wb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("snapshot file exceeds per-file byte limit")
                writer.flush()
                os.fsync(writer.fileno())
            completed = os.fstat(reader.fileno())
            current = source.stat(follow_symlinks=False)
            stable_fields = ("st_size", "st_mtime_ns")
            if (
                not os.path.samestat(opened, completed)
                or not os.path.samestat(completed, current)
                or any(
                    getattr(opened, field) != getattr(completed, field)
                    or getattr(completed, field) != getattr(current, field)
                    for field in stable_fields
                )
            ):
                raise SecurityError("snapshot source changed while being copied")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return SnapshotFile(destination.name, size, digest.hexdigest())


def create_snapshot(
    source: Path, destination: Path, policy: SnapshotPolicy | None = None
) -> SnapshotResult:
    policy = policy or SnapshotPolicy()
    source = Path(source)
    destination = Path(destination)
    if not source.exists() or not source.is_dir() or _is_link_or_reparse(source):
        raise SecurityError("snapshot source must be a plain existing directory")
    _reject_link_components(source, label="source")
    if not destination.parent.exists() or not destination.parent.is_dir():
        raise SecurityError("snapshot destination parent must be a plain existing directory")
    _reject_link_components(destination.parent, label="destination")
    source_root = source.resolve(strict=True)
    destination_root = destination.resolve(strict=False)
    try:
        destination_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot destination must be outside source")
    if destination.exists() or destination.is_symlink():
        raise SecurityError("snapshot destination must be new")

    destination.mkdir()
    included: list[SnapshotFile] = []
    try:
        for root, directories, filenames in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            kept_directories: list[str] = []
            for name in sorted(directories):
                child = root_path / name
                relative = child.relative_to(source)
                _enforce_path_bounds(relative, policy)
                if _is_link_or_reparse(child):
                    raise SecurityError(f"link or reparse point rejected: {relative.as_posix()}")
                if not _is_permanently_excluded(relative, is_directory=True) and not _matches_rule(
                    relative, policy.ignore
                ):
                    kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(filenames):
                path = root_path / name
                relative = path.relative_to(source)
                _enforce_path_bounds(relative, policy)
                if _is_link_or_reparse(path):
                    raise SecurityError(f"link or reparse point rejected: {relative.as_posix()}")
                if _is_permanently_excluded(relative, is_directory=False):
                    continue
                if policy.include and not _matches_rule(relative, policy.include):
                    continue
                if _matches_rule(relative, policy.ignore):
                    continue
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise SecurityError(f"unsupported snapshot entry: {relative.as_posix()}")
                if info.st_size > policy.max_file_bytes:
                    raise ValueError(
                        f"snapshot file exceeds per-file byte limit: {relative.as_posix()}"
                    )
                if len(included) >= policy.max_files:
                    raise ValueError("snapshot exceeds file-count limit")
                if sum(item.size for item in included) + info.st_size > policy.max_total_bytes:
                    raise ValueError("snapshot exceeds total byte limit")
                copied = _copy_regular_file(
                    path,
                    destination / relative,
                    expected=info,
                    max_bytes=policy.max_file_bytes,
                )
                if sum(item.size for item in included) + copied.size > policy.max_total_bytes:
                    raise ValueError("snapshot exceeds total byte limit")
                included.append(SnapshotFile(relative.as_posix(), copied.size, copied.sha256))
        files = tuple(sorted(included))
        return SnapshotResult(
            destination, files, sum(item.size for item in files), _canonical_digest(files)
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


copy_snapshot = create_snapshot
