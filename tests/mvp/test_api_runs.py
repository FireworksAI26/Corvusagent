from __future__ import annotations

import secrets
import shutil
import time
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from corvus.mvp.api import create_app
from corvus.mvp.change_review import ChangeReviewService
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.run_coordinator import ProviderRunEvent, RunCoordinator
from corvus.mvp.run_models import StartRunRequest
from corvus.mvp.run_store import RunStore
from corvus.mvp.safety import build_safety_preview
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager


class ImmediateBackend:
    async def start(self, *, run_id: str, cwd: Path, request: StartRunRequest, prompt: str) -> str:
        del cwd, request, prompt
        return run_id

    async def events(self, handle: str):  # type: ignore[no-untyped-def]
        del handle
        yield ProviderRunEvent("provider.started", {"message": "Working"})
        yield ProviderRunEvent("provider.completed", {"message": "Done"})

    async def cancel(self, handle: str) -> bool:
        del handle
        return True


def _git() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> None:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_durable_run_api_starts_lists_retries_and_discards(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    (source / "README.md").write_text("initial\n", encoding="utf-8")
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
    coordinator = RunCoordinator(
        RunStore(store),
        repositories,
        worktrees,
        ChangeReviewService(git),
        ImmediateBackend(),
    )
    token = secrets.token_urlsafe(32)
    client = TestClient(
        create_app(
            database=database,
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            repository_workspace=repositories,
            worktree_manager=worktrees,
            run_coordinator=coordinator,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    csrf = cast(str, client.get("/api/auth/session").json()["csrf_token"])
    preview = build_safety_preview(provider="codex", mode="chat", mcp_enabled=False)

    response = client.post(
        "/api/local/runs",
        json={
            "repository_id": repository.id,
            "task": "Inspect the repository",
            "mode": "chat",
            "effort": "high",
            "safety_digest": preview.policy_digest,
            "output_policy": "report_only",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    for _ in range(50):
        record = client.get(f"/api/local/runs/{run_id}").json()
        if record["status"] == "completed":
            break
        time.sleep(0.01)
    assert record["status"] == "completed"
    assert client.get("/api/local/runs").json()[0]["id"] == run_id
    assert [
        event["event_type"] for event in client.get(f"/api/local/runs/{run_id}/events").json()
    ] == [
        "provider.started",
        "provider.completed",
    ]

    retried = client.post(
        f"/api/local/runs/{run_id}/retry",
        headers={"X-CSRF-Token": csrf},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["retry_of_run_id"] == run_id

    discarded = client.post(
        f"/api/local/runs/{run_id}/discard",
        headers={"X-CSRF-Token": csrf},
    )
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["status"] == "discarded"
