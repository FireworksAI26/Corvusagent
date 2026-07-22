from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import corvus.mvp.api as api_module
from corvus.mvp.api import create_app
from corvus.mvp.change_review import ChangeReviewService
from corvus.mvp.contributions import ContributionService
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.run_models import RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore
from corvus.mvp.secret_scan import SecretScanner
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager
from tests.mvp.test_contributions import FakeGitHub


def _git() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args), timeout=120)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def test_contribution_api_reviews_prepares_confirms_and_publishes(tmp_path: Path) -> None:
    git = _git()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(git, remote, "init", "--bare")
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    (source / "README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    _run(git, source, "remote", "add", "origin", str(remote))
    _run(git, source, "push", "-u", "origin", "main")
    database = tmp_path / "corvus.sqlite3"
    store = SqliteStore(database)
    repositories = RepositoryWorkspaceService(store, git)
    repository = repositories.register_local("local", source, "Source")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE mvp_repositories SET remote_slug = 'team/corvus' WHERE id = ?",
            (repository.id,),
        )
    repository = repositories.get("local", repository.id)
    worktrees = WorktreeManager(
        store,
        git,
        root=tmp_path / "worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    github = FakeGitHub()
    contributions = ContributionService(
        store,
        git,
        worktrees,
        ChangeReviewService(git),
        SecretScanner(),
        github,
        confirmation_secret=b"contribution-confirmation-secret",
    )
    token = secrets.token_urlsafe(32)
    client = TestClient(
        create_app(
            database=database,
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            repository_workspace=repositories,
            worktree_manager=worktrees,
            contribution_service=contributions,
        )
    )
    assert client.get("/api/local/repositories").status_code == 401
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    csrf = cast(str, client.get("/api/auth/session").json()["csrf_token"])

    disposable = client.post(
        f"/api/local/repositories/{repository.id}/worktrees",
        headers={"X-CSRF-Token": csrf},
    )
    assert disposable.status_code == 201
    disposable_run_id = disposable.json()["run_id"]
    disposed = client.delete(
        f"/api/local/worktrees/{disposable_run_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert disposed.status_code == 204
    assert worktrees.get(disposable_run_id).status == "discarded"

    created = client.post(
        f"/api/local/repositories/{repository.id}/worktrees",
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE mvp_repositories SET remote_slug = 'team/corvus' WHERE id = ?",
            (repository.id,),
        )
    lease = worktrees.get(run_id)
    runs = RunStore(store)
    runs.create(
        "local",
        StartRunRequest(
            repository_id=repository.id,
            task="Add feature",
            safety_digest="a" * 64,
            output_policy="prepare_contribution",
        ),
        base_sha=repository.snapshot.head_sha,
        run_id=run_id,
    )
    runs.transition("local", run_id, RunStatus.RUNNING)
    managed_discard = client.delete(
        f"/api/local/worktrees/{run_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert managed_discard.status_code == 409
    assert managed_discard.json()["error"]["message"] == ("worktree_managed_by_durable_run")
    assert worktrees.get(run_id).status == "active"
    (lease.root / "feature.txt").write_text("feature\n", encoding="utf-8")

    active_run_prepare = client.post(
        f"/api/local/runs/{run_id}/contribution/prepare",
        json={
            "selected_paths": ["feature.txt"],
            "message": "Add feature",
            "title": "Add feature",
            "body": "Reviewed by Corvus.",
            "draft": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert active_run_prepare.status_code == 409
    assert active_run_prepare.json()["error"]["message"] == ("contribution_run_not_reviewable")
    assert runs.get("local", run_id).status == RunStatus.RUNNING

    runs.transition("local", run_id, RunStatus.REVIEW_REQUIRED)

    changes = client.get(f"/api/local/runs/{run_id}/changes")
    assert changes.status_code == 200
    assert changes.json()["files"][0]["path"] == "feature.txt"

    unsafe_publish = client.post(
        f"/api/local/runs/{run_id}/contribution/prepare",
        json={
            "selected_paths": ["feature.txt"],
            "message": "Add feature",
            "title": "Add feature",
            "body": "Reviewed by Corvus.",
            "draft": False,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert unsafe_publish.status_code == 422

    service_prepared = contributions.prepare(
        run_id,
        selected_paths=("feature.txt",),
        message="Add feature",
        title="Add feature",
        body="Reviewed by Corvus.",
        draft=True,
    )
    premature_publish = client.post(
        f"/api/local/runs/{run_id}/contribution/publish",
        json={"expected_digest": service_prepared.confirmation_digest},
        headers={"X-CSRF-Token": csrf},
    )
    assert premature_publish.status_code == 409
    assert premature_publish.json()["error"]["message"] == ("contribution_run_not_publishable")
    assert runs.get("local", run_id).status == RunStatus.REVIEW_REQUIRED

    prepared = client.post(
        f"/api/local/runs/{run_id}/contribution/prepare",
        json={
            "selected_paths": ["feature.txt"],
            "message": "Add feature",
            "title": "Add feature",
            "body": "Reviewed by Corvus.",
            "draft": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["state"] == "committed"
    assert runs.get("local", run_id).status == RunStatus.CONTRIBUTION_READY

    mismatch = client.post(
        f"/api/local/runs/{run_id}/contribution/publish",
        json={"expected_digest": "0" * 64},
        headers={"X-CSRF-Token": csrf},
    )
    assert mismatch.status_code == 409
    runs.transition("local", run_id, RunStatus.PUBLISHING)

    published = client.post(
        f"/api/local/runs/{run_id}/contribution/publish",
        json={"expected_digest": prepared.json()["confirmation_digest"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200, published.text
    assert published.json()["pr_number"] == 17
    assert runs.get("local", run_id).status == RunStatus.PUBLISHED


def test_changes_endpoint_works_without_github_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    database = tmp_path / "corvus.sqlite3"
    store = SqliteStore(database)
    repositories = RepositoryWorkspaceService(store, git)
    repository = repositories.register_local("local", source, "Source")
    worktrees = WorktreeManager(
        store,
        git,
        root=tmp_path / "worktrees",
        ownership_secret=b"worktree-test-secret",
    )
    run_id = str(uuid4())
    lease = worktrees.create(repository, run_id, repository.snapshot.head_sha)
    lease.root.joinpath("feature.txt").write_text("feature\n", encoding="utf-8")
    monkeypatch.setattr(
        api_module,
        "_build_git_process",
        lambda executable: git if executable.startswith("git") else None,
    )
    token = secrets.token_urlsafe(32)
    client = TestClient(
        create_app(
            database=database,
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            repository_workspace=repositories,
            worktree_manager=worktrees,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200

    changes = client.get(f"/api/local/runs/{run_id}/changes")

    assert changes.status_code == 200, changes.text
    assert changes.json()["files"][0]["path"] == "feature.txt"
