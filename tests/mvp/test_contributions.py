from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.mvp.change_review import ChangeReviewService
from corvus.mvp.contributions import (
    ContributionConflict,
    ContributionService,
)
from corvus.mvp.git_process import GitProcess
from corvus.mvp.github_cli import GitHubCliError, GitHubPullRequest
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.secret_scan import SecretScanner
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager


class FakeGitHub:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.pulls: tuple[GitHubPullRequest, ...] = ()
        self.fail_create = False

    def create_pull_request(self, **values: object) -> str:
        self.created.append(values)
        if self.fail_create:
            raise GitHubCliError("GitHub CLI command failed")
        return "https://github.com/team/corvus/pull/17"

    def list_pull_requests(self, repo: str) -> tuple[GitHubPullRequest, ...]:
        assert repo == "team/corvus"
        return self.pulls


def _git() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args), timeout=120)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _environment(
    tmp_path: Path,
    *,
    readme: str = "initial\n",
) -> tuple[ContributionService, object, GitProcess, FakeGitHub]:
    git = _git()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(git, remote, "init", "--bare")
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    (source / "README.md").write_text(readme, encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    _run(git, source, "remote", "add", "origin", str(remote))
    _run(git, source, "push", "-u", "origin", "main")
    base_sha = _run(git, source, "rev-parse", "HEAD")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE mvp_repositories SET remote_slug = 'team/corvus' WHERE id = ?",
            (repository.id,),
        )
    repository = RepositoryWorkspaceService(store, git).get("local", repository.id)
    manager = WorktreeManager(
        store,
        git,
        root=tmp_path / "worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    run_id = str(uuid4())
    lease = manager.create(repository, run_id, base_sha)
    fake_github = FakeGitHub()
    service = ContributionService(
        store,
        git,
        manager,
        ChangeReviewService(git),
        SecretScanner(),
        fake_github,
        confirmation_secret=b"contribution-confirmation-secret",
    )
    return service, lease, git, fake_github


def test_prepare_commits_only_selected_paths_and_is_idempotent(tmp_path: Path) -> None:
    service, lease, git, _ = _environment(tmp_path)
    (lease.root / "selected.txt").write_text("selected\n", encoding="utf-8")  # type: ignore[attr-defined]
    (lease.root / "left-behind.txt").write_text("leave me\n", encoding="utf-8")  # type: ignore[attr-defined]

    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("selected.txt",),
        message="Add selected file",
        title="Add selected file",
        body="Prepared and reviewed by Corvus.",
        draft=True,
    )
    resumed = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("selected.txt",),
        message="Add selected file",
        title="Add selected file",
        body="Prepared and reviewed by Corvus.",
        draft=True,
    )

    assert prepared.state == "committed"
    assert prepared.branch.startswith("corvus/")
    assert prepared.commit_sha == _run(git, lease.root, "rev-parse", "HEAD")  # type: ignore[attr-defined]
    assert prepared.secret_scan.status == "passed"
    assert prepared.confirmation_digest
    assert resumed.id == prepared.id
    assert _run(git, lease.root, "show", "--format=", "--name-only", "HEAD") == "selected.txt"  # type: ignore[attr-defined]
    assert (lease.root / "left-behind.txt").exists()  # type: ignore[attr-defined]


def test_prepare_rechecks_files_mutated_between_review_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, lease, git, _ = _environment(tmp_path)
    selected = lease.root / "selected.txt"  # type: ignore[attr-defined]
    selected.write_text("safe\n", encoding="utf-8")
    base_sha = _run(git, lease.root, "rev-parse", "HEAD")  # type: ignore[attr-defined]
    original_run = git.run
    mutated = False

    def mutate_before_add(cwd: Path, args: tuple[str, ...], timeout: float = 30):  # type: ignore[no-untyped-def]
        nonlocal mutated
        if not mutated and args[:2] == ("add", "--"):
            mutated = True
            selected.write_text("sk-proj-abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")
        return original_run(cwd, args, timeout)

    monkeypatch.setattr(git, "run", mutate_before_add)

    with pytest.raises(ContributionConflict, match="contribution_secret_scan_blocked"):
        service.prepare(
            lease.run_id,  # type: ignore[attr-defined]
            selected_paths=("selected.txt",),
            message="Add selected file",
            title="Add selected file",
            body="Prepared and reviewed by Corvus.",
            draft=True,
        )

    assert _run(git, lease.root, "rev-parse", "HEAD") == base_sha  # type: ignore[attr-defined]


def test_prepare_blocks_known_secret_and_digest_mismatch_blocks_publish(tmp_path: Path) -> None:
    service, lease, _, _ = _environment(tmp_path)
    (lease.root / "config.env").write_text(  # type: ignore[attr-defined]
        "TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8"
    )

    with pytest.raises(ContributionConflict, match="secret_scan_blocked"):
        service.prepare(
            lease.run_id,  # type: ignore[attr-defined]
            selected_paths=("config.env",),
            message="Unsafe",
            title="Unsafe",
            body="Unsafe",
            draft=True,
        )

    (lease.root / "config.env").write_text("SAFE=true\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("config.env",),
        message="Safe config",
        title="Safe config",
        body="Reviewed",
        draft=True,
    )
    with pytest.raises(ContributionConflict, match="confirmation_digest_mismatch"):
        service.publish(lease.run_id, expected_digest="0" * 64)  # type: ignore[attr-defined]
    assert prepared.state == "committed"


def test_prepare_commits_a_selected_deletion(tmp_path: Path) -> None:
    service, lease, git, _ = _environment(tmp_path)
    lease.root.joinpath("README.md").unlink()  # type: ignore[attr-defined]

    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("README.md",),
        message="Remove readme",
        title="Remove readme",
        body="Reviewed deletion.",
        draft=True,
    )

    assert prepared.state == "committed"
    assert prepared.secret_scan.scanned_paths == ("README.md",)
    assert _run(git, lease.root, "show", "--format=", "--name-status", "HEAD") == "D\tREADME.md"  # type: ignore[attr-defined]


def test_prepare_scans_the_deleted_blob_for_secrets(tmp_path: Path) -> None:
    service, lease, _, _ = _environment(
        tmp_path,
        readme="TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n",
    )
    lease.root.joinpath("README.md").unlink()  # type: ignore[attr-defined]

    with pytest.raises(ContributionConflict, match="secret_scan_blocked"):
        service.prepare(
            lease.run_id,  # type: ignore[attr-defined]
            selected_paths=("README.md",),
            message="Remove secret",
            title="Remove secret",
            body="Reviewed deletion.",
            draft=True,
        )


def test_prepare_stages_both_sides_of_a_selected_rename(tmp_path: Path) -> None:
    service, lease, git, _ = _environment(tmp_path)
    lease.root.joinpath("README.md").rename(lease.root / "GUIDE.md")  # type: ignore[attr-defined]
    _run(git, lease.root, "add", "-A")  # type: ignore[attr-defined]

    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("GUIDE.md",),
        message="Rename guide",
        title="Rename guide",
        body="Reviewed rename.",
        draft=True,
    )

    assert prepared.state == "committed"
    changed = _run(git, lease.root, "show", "--format=", "--name-status", "-M", "HEAD")  # type: ignore[attr-defined]
    assert "README.md" in changed
    assert "GUIDE.md" in changed


def test_resume_revalidates_prepared_content_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, lease, _, _ = _environment(tmp_path)
    target = lease.root / "config.env"  # type: ignore[attr-defined]
    target.write_text("SAFE=true\n", encoding="utf-8")
    original_resume = service._resume_prepare
    monkeypatch.setattr(service, "_resume_prepare", lambda _root, _sha, record: record)
    pending = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("config.env",),
        message="Add config",
        title="Add config",
        body="Reviewed config.",
        draft=True,
    )
    assert pending.state == "preparing"
    target.write_text("TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")
    monkeypatch.setattr(service, "_resume_prepare", original_resume)

    with pytest.raises(ContributionConflict, match="secret_scan_blocked"):
        service.prepare(
            lease.run_id,  # type: ignore[attr-defined]
            selected_paths=("config.env",),
            message="Add config",
            title="Add config",
            body="Reviewed config.",
            draft=True,
        )


def test_resume_rejects_a_commit_without_revalidated_content(tmp_path: Path) -> None:
    service, lease, _, _ = _environment(tmp_path)
    lease.root.joinpath("feature.txt").write_text("feature\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Reviewed feature.",
        draft=True,
    )
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE mvp_contributions SET state = 'branch_created', commit_sha = NULL "
            "WHERE run_id = ?",
            (lease.run_id,),  # type: ignore[attr-defined]
        )

    with pytest.raises(ContributionConflict, match="paths_not_changed"):
        service.prepare(
            lease.run_id,  # type: ignore[attr-defined]
            selected_paths=("feature.txt",),
            message="Add feature",
            title="Add feature",
            body="Reviewed feature.",
            draft=True,
        )
    assert prepared.commit_sha is not None


def test_publish_rejects_a_branch_advanced_after_review(tmp_path: Path) -> None:
    service, lease, git, _ = _environment(tmp_path)
    lease.root.joinpath("feature.txt").write_text("reviewed\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Reviewed feature.",
        draft=True,
    )
    lease.root.joinpath("feature.txt").write_text("changed later\n", encoding="utf-8")  # type: ignore[attr-defined]
    _run(git, lease.root, "add", "--", "feature.txt")  # type: ignore[attr-defined]
    _run(git, lease.root, "commit", "--amend", "--no-edit", "--no-verify")  # type: ignore[attr-defined]

    with pytest.raises(ContributionConflict, match="commit_changed"):
        service.publish(
            lease.run_id,  # type: ignore[attr-defined]
            expected_digest=prepared.confirmation_digest,
        )


def test_publish_non_force_pushes_and_creates_draft_pr_once(tmp_path: Path) -> None:
    service, lease, git, github = _environment(tmp_path)
    (lease.root / "feature.txt").write_text("feature\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Evidence attached.",
        draft=True,
    )

    published = service.publish(
        lease.run_id,  # type: ignore[attr-defined]
        expected_digest=prepared.confirmation_digest,
    )
    resumed = service.publish(
        lease.run_id,  # type: ignore[attr-defined]
        expected_digest=prepared.confirmation_digest,
    )

    assert published.state == "published"
    assert published.pr_url == "https://github.com/team/corvus/pull/17"
    assert published.pr_number == 17
    assert len(github.created) == 1
    assert github.created[0]["draft"] is True
    assert resumed.id == published.id
    assert _run(git, lease.root, "rev-parse", f"origin/{published.branch}") == published.commit_sha  # type: ignore[attr-defined]


def test_publish_recovers_existing_pr_after_partial_success(tmp_path: Path) -> None:
    service, lease, _, github = _environment(tmp_path)
    (lease.root / "feature.txt").write_text("feature\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Evidence attached.",
        draft=True,
    )
    github.fail_create = True
    github.pulls = (
        GitHubPullRequest(
            number=19,
            title="Add feature",
            state="OPEN",
            draft=True,
            url="https://github.com/team/corvus/pull/19",
            head_branch=prepared.branch,
            base_branch="main",
        ),
    )

    recovered = service.publish(
        lease.run_id,  # type: ignore[attr-defined]
        expected_digest=prepared.confirmation_digest,
    )

    assert recovered.state == "published"
    assert recovered.pr_number == 19


def test_publish_retry_rejects_remote_branch_changed_after_push(tmp_path: Path) -> None:
    service, lease, git, github = _environment(tmp_path)
    (lease.root / "feature.txt").write_text("reviewed\n", encoding="utf-8")  # type: ignore[attr-defined]
    prepared = service.prepare(
        lease.run_id,  # type: ignore[attr-defined]
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Evidence attached.",
        draft=True,
    )
    github.fail_create = True
    with pytest.raises(ContributionConflict, match="pull_request_failed"):
        service.publish(
            lease.run_id,  # type: ignore[attr-defined]
            expected_digest=prepared.confirmation_digest,
        )

    (lease.root / "feature.txt").write_text("unreviewed\n", encoding="utf-8")  # type: ignore[attr-defined]
    _run(git, lease.root, "add", "--", "feature.txt")  # type: ignore[attr-defined]
    _run(git, lease.root, "commit", "--amend", "--no-edit", "--no-verify")  # type: ignore[attr-defined]
    _run(git, lease.root, "push", "--force", "origin", f"HEAD:refs/heads/{prepared.branch}")  # type: ignore[attr-defined]
    github.fail_create = False

    with pytest.raises(ContributionConflict, match="remote_changed"):
        service.publish(
            lease.run_id,  # type: ignore[attr-defined]
            expected_digest=prepared.confirmation_digest,
        )
    assert len(github.created) == 1
