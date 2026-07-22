from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import corvus.snapshot as snapshot_module
from corvus.security import SecurityError
from corvus.snapshot import SnapshotPolicy, create_snapshot


def test_snapshot_excludes_sensitive_and_generated_paths_by_default(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    source.mkdir()
    (source / "keep.txt").write_text("safe", encoding="utf-8")
    excluded = {
        ".env": "API_KEY=canary",
        "credentials.json": "canary",
        "private.pem": "canary",
        ".git/config": "canary",
        ".corvus/state.json": "canary",
        ".venv/lib/site.py": "canary",
        "node_modules/pkg/index.js": "canary",
        ".pytest_cache/state": "canary",
        "build/result.bin": "canary",
        "dist/archive.whl": "canary",
        "work/scratch.txt": "canary",
        "outputs/model.txt": "canary",
    }
    for relative, value in excluded.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    policy = SnapshotPolicy()
    result = create_snapshot(source, destination, policy)

    assert (destination / "keep.txt").read_text(encoding="utf-8") == "safe"
    assert [item.relative_path for item in result.files] == ["keep.txt"]
    assert result.total_bytes == 4
    assert len(result.digest) == 64
    assert not any((destination / relative).exists() for relative in excluded)
    with pytest.raises(FrozenInstanceError):
        policy.max_files = 1  # type: ignore[misc]


def test_include_and_ignore_rules_only_narrow_the_safe_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    source.mkdir()
    (source / "keep.py").write_text("safe", encoding="utf-8")
    (source / "ignored.py").write_text("ordinary", encoding="utf-8")
    (source / "notes.txt").write_text("ordinary", encoding="utf-8")
    (source / ".env").write_text("API_KEY=canary", encoding="utf-8")

    result = create_snapshot(
        source,
        destination,
        SnapshotPolicy(include=("*.py", ".env"), ignore=("ignored.py",)),
    )

    assert [item.relative_path for item in result.files] == ["keep.py"]
    assert not (destination / ".env").exists()


@pytest.mark.parametrize(
    "field",
    ("max_files", "max_file_bytes", "max_total_bytes", "max_path_depth", "max_name_bytes"),
)
def test_snapshot_policy_requires_positive_resource_bounds(field: str) -> None:
    values = {field: 0}

    with pytest.raises(ValueError, match=field):
        SnapshotPolicy(**values)  # type: ignore[arg-type]


def test_snapshot_rejects_oversized_file_and_removes_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    source.mkdir()
    (source / "large.txt").write_bytes(b"12345")

    with pytest.raises(ValueError, match="per-file byte limit"):
        create_snapshot(source, destination, SnapshotPolicy(max_file_bytes=4))

    assert not destination.exists()


def test_snapshot_rejects_excess_file_count(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    source.mkdir()
    (source / "one.txt").write_text("1", encoding="utf-8")
    (source / "two.txt").write_text("2", encoding="utf-8")

    with pytest.raises(ValueError, match="file-count limit"):
        create_snapshot(source, destination, SnapshotPolicy(max_files=1))

    assert not destination.exists()


def test_snapshot_rejects_excess_total_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    source.mkdir()
    (source / "one.txt").write_bytes(b"123")
    (source / "two.txt").write_bytes(b"456")

    with pytest.raises(ValueError, match="total byte limit"):
        create_snapshot(source, destination, SnapshotPolicy(max_total_bytes=5))

    assert not destination.exists()


def test_snapshot_enforces_path_depth_and_component_name_bounds(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("safe", encoding="utf-8")

    with pytest.raises(ValueError, match="path-depth limit"):
        create_snapshot(
            source,
            tmp_path / "depth-snapshot",
            SnapshotPolicy(max_path_depth=2),
        )
    with pytest.raises(ValueError, match="component-name byte limit"):
        create_snapshot(
            source,
            tmp_path / "name-snapshot",
            SnapshotPolicy(max_name_bytes=3),
        )

    assert not (tmp_path / "depth-snapshot").exists()
    assert not (tmp_path / "name-snapshot").exists()


def test_snapshot_rejects_destination_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe", encoding="utf-8")
    destination = source / "snapshot"

    with pytest.raises(ValueError, match="outside source"):
        create_snapshot(source, destination)

    assert not destination.exists()


def test_snapshot_rejects_source_and_entry_symlinks_when_supported(tmp_path: Path) -> None:
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    (real_source / "safe.txt").write_text("safe", encoding="utf-8")
    source_link = tmp_path / "source-link"
    entry_link = real_source / "entry-link"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        source_link.symlink_to(real_source, target_is_directory=True)
        entry_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(SecurityError, match="plain existing directory"):
        create_snapshot(source_link, tmp_path / "source-link-snapshot")
    with pytest.raises(SecurityError, match="link or reparse point"):
        create_snapshot(real_source, tmp_path / "entry-link-snapshot")
    assert not (tmp_path / "source-link-snapshot").exists()
    assert not (tmp_path / "entry-link-snapshot").exists()


def test_snapshot_rejects_destination_through_symlink_parent_when_supported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    destination = linked_parent / "snapshot"
    with pytest.raises(SecurityError, match="destination.*link|reparse"):
        create_snapshot(source, destination)

    assert not destination.exists()
    assert not (outside / "snapshot").exists()


def test_snapshot_rejects_reparse_destination_parent_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe", encoding="utf-8")
    destination_parent = tmp_path / "destination-parent"
    destination_parent.mkdir()
    destination = destination_parent / "snapshot"
    original_link_check = snapshot_module._is_link_or_reparse

    def fake_link_check(path: Path) -> bool:
        return path == destination_parent or original_link_check(path)

    monkeypatch.setattr(snapshot_module, "_is_link_or_reparse", fake_link_check)

    with pytest.raises(SecurityError, match="destination.*link|reparse"):
        create_snapshot(source, destination)

    assert not destination.exists()


def test_snapshot_rejects_unsupported_special_file_and_cleans_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    special = source / "special"
    special.write_bytes(b"not-copied")
    destination = tmp_path / "snapshot"
    original_stat = Path.stat
    original_link_check = snapshot_module._is_link_or_reparse

    def fake_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path == special and not follow_symlinks:
            values = list(result)
            values[0] = stat.S_IFIFO
            return os.stat_result(values)
        return result

    def fake_link_check(path: Path) -> bool:
        return False if path == special else original_link_check(path)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(snapshot_module, "_is_link_or_reparse", fake_link_check)

    with pytest.raises(SecurityError, match="unsupported snapshot entry"):
        create_snapshot(source, destination)

    assert not destination.exists()


def test_snapshot_rejects_source_swap_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file.txt"
    source.write_text("approved", encoding="utf-8")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("swapped", encoding="utf-8")
    destination = tmp_path / "snapshot"
    original_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == source:
            source.unlink()
            replacement.replace(source)
            swapped = True
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(snapshot_module.os, "open", swapping_open)

    with pytest.raises(SecurityError, match="changed between inspection and copy"):
        create_snapshot(source_root, destination)

    assert swapped is True
    assert not destination.exists()


def test_snapshot_rejects_source_metadata_change_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file.txt"
    source.write_text("approved", encoding="utf-8")
    destination = tmp_path / "snapshot"
    original_stat = Path.stat
    source_stat_calls = 0

    def changing_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal source_stat_calls
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path == source and not follow_symlinks:
            source_stat_calls += 1
            if source_stat_calls >= 3:
                values = list(result)
                values[8] = result.st_mtime + 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", changing_stat)

    with pytest.raises(SecurityError, match="changed while being copied"):
        create_snapshot(source_root, destination)

    assert source_stat_calls >= 3
    assert not destination.exists()
