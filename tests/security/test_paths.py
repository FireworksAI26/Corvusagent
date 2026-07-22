from pathlib import Path

import pytest

from corvus.security import SecurityError, atomic_write, resolve_under


def test_resolve_under_rejects_absolute_and_parent_paths(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    with pytest.raises(SecurityError, match="relative"):
        resolve_under(root, str((tmp_path / "outside.txt").resolve()))
    with pytest.raises(SecurityError, match="traversal"):
        resolve_under(root, "../outside.txt")


def test_resolve_under_rejects_symlink_components_when_supported(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(SecurityError, match="link or reparse-point"):
        resolve_under(tmp_path, "link/secret.txt")


def test_atomic_write_replaces_content_without_leaving_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.txt"

    atomic_write(target, b"first")
    atomic_write(target, b"second")

    assert target.read_bytes() == b"second"
    assert not list(target.parent.glob(".*.corvus-*.tmp"))
