from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from corvus.mvp.change_review import ChangeReviewError, ChangeReviewService
from corvus.mvp.git_process import GitProcess


def _git() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _repository(tmp_path: Path) -> tuple[Path, GitProcess]:
    root = tmp_path / "repo"
    root.mkdir()
    git = _git()
    _run(git, root, "init", "--initial-branch=main")
    _run(git, root, "config", "user.email", "corvus@example.test")
    _run(git, root, "config", "user.name", "Corvus Tests")
    (root / "modified.txt").write_text("before\n", encoding="utf-8")
    (root / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (root / "old-name.txt").write_text("rename me\n", encoding="utf-8")
    _run(git, root, "add", "--", ".")
    _run(git, root, "commit", "-m", "initial")
    return root, git


def test_snapshot_reports_real_file_kinds_patches_and_binary_state(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    (root / "modified.txt").write_text("after\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    _run(git, root, "mv", "old-name.txt", "new-name.txt")
    (root / "added.txt").write_text("added\n", encoding="utf-8")
    _run(git, root, "add", "--", "added.txt")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\0\x01\x02")

    change_set = ChangeReviewService(git).snapshot(root)
    by_path = {item.path: item for item in change_set.files}

    assert by_path["modified.txt"].status == "modified"
    assert "-before" in (by_path["modified.txt"].patch or "")
    assert by_path["deleted.txt"].status == "deleted"
    assert by_path["new-name.txt"].status == "renamed"
    assert by_path["new-name.txt"].previous_path == "old-name.txt"
    assert by_path["added.txt"].status == "added"
    assert by_path["untracked.txt"].status == "untracked"
    assert "+untracked" in (by_path["untracked.txt"].patch or "")
    assert by_path["binary.bin"].binary is True
    assert by_path["binary.bin"].patch is None
    assert change_set.digest


def test_snapshot_filters_selected_paths_and_refuses_escape(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    (root / "modified.txt").write_text("after\n", encoding="utf-8")
    (root / "untracked.txt").write_text("new\n", encoding="utf-8")
    service = ChangeReviewService(git)

    selected = service.snapshot(root, selected_paths=("modified.txt",))
    assert [item.path for item in selected.files] == ["modified.txt"]

    with pytest.raises(ChangeReviewError, match="path_invalid"):
        service.snapshot(root, selected_paths=("../outside.txt",))


def test_patch_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    (root / "modified.txt").write_text("line\n" * 100, encoding="utf-8")

    change_set = ChangeReviewService(git, max_patch_bytes=64).snapshot(root)

    assert change_set.files[0].patch_truncated is True
    assert len((change_set.files[0].patch or "").encode()) <= 64


def test_large_tracked_patch_bypasses_process_output_limit_and_is_truncated(
    tmp_path: Path,
) -> None:
    root, git = _repository(tmp_path)
    (root / "modified.txt").write_text("changed line\n" * 250_000, encoding="utf-8")

    change_set = ChangeReviewService(git, max_patch_bytes=64).snapshot(root)
    modified = next(item for item in change_set.files if item.path == "modified.txt")

    assert modified.patch_truncated is True
    assert len((modified.patch or "").encode()) <= 64


def test_digest_binds_full_binary_content_and_untracked_reads_are_bounded(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    binary = root / "large.bin"
    binary.write_bytes(b"\0" + b"a" * 4096)
    first = ChangeReviewService(git, max_patch_bytes=64).snapshot(root)
    binary.write_bytes(b"\0" + b"a" * 4095 + b"b")
    second = ChangeReviewService(git, max_patch_bytes=64).snapshot(root)
    assert first.digest != second.digest

    generated = root / "generated.txt"
    generated.write_text("line\n" * 10000, encoding="utf-8")
    reviewed = ChangeReviewService(git, max_patch_bytes=64).snapshot(
        root, selected_paths=("generated.txt",)
    )
    assert reviewed.files[0].patch_truncated is True
    assert len((reviewed.files[0].patch or "").encode()) <= 64


def test_snapshot_rejects_untracked_symlink_before_reading_target(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do-not-disclose\n", encoding="utf-8")
    link = root / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(ChangeReviewError, match="path_invalid"):
        ChangeReviewService(git).snapshot(root)
