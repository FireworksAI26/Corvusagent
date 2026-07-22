from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.mvp.core import DomainConflict, DomainNotFound
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager


def _git_process() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _repository(tmp_path: Path) -> tuple[Path, GitProcess]:
    root = tmp_path / "source"
    root.mkdir(parents=True)
    git = _git_process()
    _run(git, root, "init", "--initial-branch=main")
    _run(git, root, "config", "user.email", "corvus@example.test")
    _run(git, root, "config", "user.name", "Corvus Tests")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _run(git, root, "add", "--", "README.md")
    _run(git, root, "commit", "-m", "initial")
    _run(git, root, "remote", "add", "origin", "git@github.com:team/corvus.git")
    return root, git


def _service(tmp_path: Path, git: GitProcess) -> RepositoryWorkspaceService:
    return RepositoryWorkspaceService(SqliteStore(tmp_path / "corvus.sqlite3"), git)


@pytest.mark.parametrize(
    ("remote", "expected"),
    (
        ("https://github.com/team/corvus.git", "team/corvus"),
        ("ssh://git@github.com/team/corvus.git", "team/corvus"),
        ("git@github.com:team/corvus.git", "team/corvus"),
        ("file://github.com/team/corvus.git", None),
        ("git://github.com/team/corvus.git", None),
        ("https://token@github.com/team/corvus.git", None),
    ),
)
def test_github_remote_slug_accepts_only_explicit_https_or_ssh_forms(
    remote: str, expected: str | None
) -> None:
    assert RepositoryWorkspaceService._github_slug(remote) == expected


def test_registers_git_root_and_refreshes_real_state(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    nested = root / "src"
    nested.mkdir()
    service = _service(tmp_path, git)

    registered = service.register_local("local", nested, "Corvus")

    assert registered.display_name == "Corvus"
    assert registered.path == os.fspath(root.resolve())
    assert registered.remote_slug == "team/corvus"
    assert registered.default_branch == "main"
    assert registered.snapshot.branch == "main"
    assert registered.snapshot.clean is True
    assert len(registered.snapshot.head_sha) == 40

    (root / "README.md").write_text("changed\n", encoding="utf-8")
    refreshed = service.refresh("local", registered.id)
    assert refreshed.snapshot.clean is False
    assert refreshed.snapshot.health == "healthy"

    _run(git, root, "remote", "set-url", "origin", "git@github.com:team/renamed.git")
    _run(git, root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    refreshed = service.refresh("local", registered.id)
    assert refreshed.remote_slug == "team/renamed"
    assert refreshed.default_branch == "trunk"


def test_duplicate_registration_conflicts_and_tenants_are_isolated(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    service = _service(tmp_path, git)
    registered = service.register_local("tenant-a", root, "Corvus")

    with pytest.raises(DomainConflict, match="repository_already_registered"):
        service.register_local("tenant-a", root, "Again")
    with pytest.raises(DomainNotFound, match="repository_not_found"):
        service.get("tenant-b", registered.id)


def test_registration_rejects_non_git_and_link_paths(tmp_path: Path) -> None:
    git = _git_process()
    service = _service(tmp_path, git)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").write_text("gitdir: missing\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not_a_git_repository"):
        service.register_local("local", plain, "Plain")

    root, _ = _repository(tmp_path / "linked-source")
    link = tmp_path / "linked"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError, match="repository_path_links_forbidden"):
        service.register_local("local", link, "Linked")


def test_remove_only_removes_registration_not_checkout(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    service = _service(tmp_path, git)
    registered = service.register_local("local", root, "Corvus")

    service.remove("local", registered.id)

    assert root.is_dir()
    assert (root / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert service.list("local") == ()


def test_remove_translates_dependent_worktree_conflict(tmp_path: Path) -> None:
    root, git = _repository(tmp_path)
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    service = RepositoryWorkspaceService(store, git)
    registered = service.register_local("local", root, "Corvus")
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "worktrees",
        ownership_secret=b"repository-remove-test",
    )
    manager.create(registered, str(uuid4()), registered.snapshot.head_sha)

    with pytest.raises(DomainConflict, match="repository_in_use"):
        service.remove("local", registered.id)


def test_refresh_reports_missing_checkout_without_disclosing_git_error(
    tmp_path: Path,
) -> None:
    root, git = _repository(tmp_path)
    service = _service(tmp_path, git)
    registered = service.register_local("local", root, "Corvus")
    moved = tmp_path / "moved"
    root.rename(moved)

    refreshed = service.refresh("local", registered.id)

    assert refreshed.snapshot.health == "missing"
    assert refreshed.snapshot.clean is False
