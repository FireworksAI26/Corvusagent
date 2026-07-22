from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from corvus.mvp.api import create_app
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.store import SqliteStore


def _git_process() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> None:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_repository_api_requires_session_and_mutation_auth(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git = _git_process()
    _run(git, root, "init", "--initial-branch=main")
    _run(git, root, "config", "user.email", "corvus@example.test")
    _run(git, root, "config", "user.name", "Corvus Tests")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _run(git, root, "add", "--", "README.md")
    _run(git, root, "commit", "-m", "initial")

    database = tmp_path / "corvus.sqlite3"
    token = secrets.token_urlsafe(32)
    workspace = RepositoryWorkspaceService(SqliteStore(database), git)
    client = TestClient(
        create_app(
            database=database,
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            repository_workspace=workspace,
        )
    )

    assert client.get("/api/local/repositories").status_code == 401
    assert (
        client.post(
            "/api/local/repositories", json={"path": str(root), "display_name": "Repo"}
        ).status_code
        == 401
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    session = client.get("/api/auth/session").json()
    csrf = cast(str, session["csrf_token"])
    assert (
        client.post(
            "/api/local/repositories", json={"path": str(root), "display_name": "Repo"}
        ).status_code
        == 403
    )

    created = client.post(
        "/api/local/repositories",
        json={"path": str(root), "display_name": "Repo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    repository_id = created.json()["id"]
    assert client.get("/api/local/repositories").json()[0]["id"] == repository_id
    assert client.get("/api/local/repositories?limit=1&offset=1").json() == []
    assert client.get("/api/local/repositories?limit=0&offset=0").status_code == 422

    refreshed = client.post(
        f"/api/local/repositories/{repository_id}/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["snapshot"]["health"] == "healthy"

    removed = client.delete(
        f"/api/local/repositories/{repository_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert removed.status_code == 204
    assert root.exists()
