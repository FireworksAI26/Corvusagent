from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.mvp.core import DomainConflict
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager, WorktreeOwnershipError


def _git_process() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _workspace(tmp_path: Path) -> tuple[GitProcess, SqliteStore, object, Path, str]:
    repository_root = tmp_path / "source"
    repository_root.mkdir()
    git = _git_process()
    _run(git, repository_root, "init", "--initial-branch=main")
    _run(git, repository_root, "config", "user.email", "corvus@example.test")
    _run(git, repository_root, "config", "user.name", "Corvus Tests")
    (repository_root / "README.md").write_text("initial\n", encoding="utf-8")
    _run(git, repository_root, "add", "--", "README.md")
    _run(git, repository_root, "commit", "-m", "initial")
    base_sha = _run(git, repository_root, "rev-parse", "HEAD")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local(
        "local", repository_root, "Source"
    )
    return git, store, repository, repository_root, base_sha


@pytest.mark.parametrize("base_sha", ("a" * 40, "b" * 64))
def test_git_object_id_validator_accepts_sha1_and_sha256(base_sha: str) -> None:
    assert WorktreeManager._valid_sha(base_sha)


@pytest.mark.parametrize(
    "base_sha",
    ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 64),
)
def test_git_object_id_validator_rejects_noncanonical_values(base_sha: str) -> None:
    assert not WorktreeManager._valid_sha(base_sha)


def test_create_checks_out_exact_sha_without_modifying_original(tmp_path: Path) -> None:
    git, store, repository, original, base_sha = _workspace(tmp_path)
    before_status = _run(git, original, "status", "--porcelain=v1")
    run_id = str(uuid4())
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "managed-worktrees",
        ownership_secret=b"worktree-test-secret",
    )

    lease = manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]

    assert lease.root.is_relative_to((tmp_path / "managed-worktrees").resolve())
    assert _run(git, lease.root, "rev-parse", "HEAD") == base_sha
    assert _run(git, lease.root, "branch", "--show-current") == ""
    (lease.root / "README.md").write_text("worktree change\n", encoding="utf-8")
    assert (original / "README.md").read_text(encoding="utf-8") == "initial\n"
    assert _run(git, original, "status", "--porcelain=v1") == before_status


def test_create_cleans_checkout_and_lease_when_activation_persistence_fails(
    tmp_path: Path,
) -> None:
    git, store, repository, _, base_sha = _workspace(tmp_path)
    run_id = str(uuid4())
    root = tmp_path / "managed-worktrees"
    manager = WorktreeManager(
        store,
        git,
        root=root,
        ownership_secret=b"worktree-test-secret",
    )
    with store.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_worktree_activation "
            "BEFORE UPDATE OF status ON mvp_worktree_leases "
            "WHEN NEW.status = 'active' "
            "BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"
        )

    with pytest.raises(WorktreeOwnershipError, match="worktree_activation_failed"):
        manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]

    assert not (root / repository.id / run_id).exists()  # type: ignore[union-attr]
    with store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM mvp_worktree_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row is None


def test_recover_interrupted_creation_removes_checkout_and_lease(tmp_path: Path) -> None:
    git, store, repository, original, base_sha = _workspace(tmp_path)
    run_id = str(uuid4())
    root = tmp_path / "managed-worktrees"
    manager = WorktreeManager(
        store,
        git,
        root=root,
        ownership_secret=b"worktree-test-secret",
    )
    target = root.resolve(strict=False) / repository.id / run_id  # type: ignore[union-attr]
    target.parent.mkdir(parents=True)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO mvp_worktree_leases "
            "(run_id, repository_id, root_path, base_sha, ownership_digest, status, "
            "created_at, discarded_at) VALUES (?, ?, ?, ?, ?, 'creating', ?, NULL)",
            (
                run_id,
                repository.id,  # type: ignore[union-attr]
                str(target),
                base_sha,
                manager._ownership_digest(
                    run_id,
                    repository.id,  # type: ignore[union-attr]
                    target,
                    base_sha,
                    "creating",
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
    _run(git, original, "worktree", "add", "--detach", str(target), base_sha)

    assert manager.recover_interrupted() == (run_id,)

    assert not target.exists()
    with store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM mvp_worktree_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row is None


def test_duplicate_run_is_refused_and_discard_requires_terminal_run(tmp_path: Path) -> None:
    git, store, repository, _, base_sha = _workspace(tmp_path)
    run_id = str(uuid4())
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "managed-worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    lease = manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]

    with pytest.raises(DomainConflict, match="worktree_run_already_exists"):
        manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]
    with pytest.raises(DomainConflict, match="worktree_run_still_active"):
        manager.discard(lease, run_terminal=False)

    manager.discard(lease, run_terminal=True)
    assert not lease.root.exists()
    assert manager.get(run_id).status == "discarded"
    with pytest.raises(DomainConflict, match="worktree_run_already_exists"):
        manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]


def test_discard_rejects_tampered_database_path(tmp_path: Path) -> None:
    git, store, repository, _, base_sha = _workspace(tmp_path)
    run_id = str(uuid4())
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "managed-worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    manager.create(repository, run_id, base_sha)  # type: ignore[arg-type]
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE mvp_worktree_leases SET root_path = ? WHERE run_id = ?",
            (str(outside), run_id),
        )

    with pytest.raises(WorktreeOwnershipError, match="(ownership|git_metadata)_invalid"):
        manager.get(run_id)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_get_rejects_tampered_worktree_git_metadata(tmp_path: Path) -> None:
    git, store, repository, _, base_sha = _workspace(tmp_path)
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "managed-worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    lease = manager.create(repository, str(uuid4()), base_sha)  # type: ignore[arg-type]
    fake_gitdir = tmp_path / "fake-gitdir"
    fake_gitdir.mkdir()
    metadata = lease.root / ".git"
    metadata.chmod(0o600)
    try:
        metadata.write_text(f"gitdir: {fake_gitdir}\n", encoding="utf-8")
    except PermissionError:
        pytest.skip("Git metadata is host-protected")

    with pytest.raises(WorktreeOwnershipError, match="ownership_invalid"):
        manager.get(lease.run_id)


def test_create_rejects_untrusted_sha_and_linked_root(tmp_path: Path) -> None:
    git, store, repository, _, _ = _workspace(tmp_path)
    root = tmp_path / "managed-worktrees"
    outside = tmp_path / "outside-root"
    outside.mkdir()
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    manager = WorktreeManager(
        store,
        git,
        root=root,
        ownership_secret=b"worktree-test-secret",
    )

    with pytest.raises(WorktreeOwnershipError, match="root_invalid"):
        manager.create(repository, str(uuid4()), "not-a-sha")  # type: ignore[arg-type]
