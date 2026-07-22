from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import corvus.mvp.api as api_module
from corvus.mvp.api import create_app
from corvus.mvp.git_process import GitProcess
from corvus.mvp.github_cli import GitHubCliError


class _InitializingGitHub:
    def __init__(self, git: GitProcess) -> None:
        self.git = git
        self.clones: list[tuple[str, Path]] = []

    def clone_repository(self, repository: str, target: Path) -> None:
        self.clones.append((repository, target))
        target.mkdir(mode=0o700)
        initialized = self.git.run(target, ("init", "--initial-branch=main", "."))
        committed = self.git.run(
            target,
            (
                "-c",
                "user.name=Corvus Test",
                "-c",
                "user.email=corvus-test@localhost",
                "commit",
                "--allow-empty",
                "-m",
                "Initial test commit",
            ),
        )
        assert initialized.returncode == 0
        assert committed.returncode == 0


class _FailingGitHub:
    def __init__(self) -> None:
        self.target: Path | None = None

    def clone_repository(self, _repository: str, target: Path) -> None:
        self.target = target
        target.mkdir(mode=0o700)
        raise GitHubCliError("github_clone_failed")


def _paired_client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    token = str(uuid4())
    client = TestClient(
        create_app(
            database=tmp_path / "github-projects.sqlite3",
            bootstrap_token=token,
            session_secret=b"s" * 32,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    session = client.get("/api/auth/session").json()
    return client, {"X-CSRF-Token": session["csrf_token"]}


def test_github_cli_builder_isolates_auth_and_adds_only_the_git_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gh_executable = tmp_path / "github-cli" / "gh.exe"
    gh_executable.parent.mkdir()
    gh_executable.touch()
    git_executable = tmp_path / "git" / "bin" / "git.exe"
    git_executable.parent.mkdir(parents=True)
    git_executable.touch()
    captured: list[dict[str, Any]] = []
    managed_runner = object()
    authorization_runner = object()

    def fake_trusted_cli(
        executable: Path,
        *,
        environment: dict[str, str] | None = None,
        additional_path_entries: tuple[Path, ...],
    ) -> object:
        captured.append(
            {
                "executable": executable,
                "environment": environment,
                "additional_path_entries": additional_path_entries,
            }
        )
        return managed_runner if len(captured) == 1 else authorization_runner

    monkeypatch.setattr(shutil, "which", lambda _name: os.fspath(gh_executable))
    monkeypatch.setattr(api_module, "TrustedCli", fake_trusted_cli)
    monkeypatch.setattr(
        api_module,
        "GitHubCli",
        lambda runner, *, cwd, authorization_runner: {
            "runner": runner,
            "authorization_runner": authorization_runner,
            "cwd": cwd,
        },
    )

    client = api_module._build_github_cli(tmp_path, os.fspath(git_executable))

    config_root = tmp_path / ".corvus-github-cli"
    assert cast(Any, client) == {
        "runner": managed_runner,
        "authorization_runner": authorization_runner,
        "cwd": tmp_path,
    }
    assert len(captured) == 2
    assert captured[0]["executable"] == gh_executable
    assert captured[0]["environment"] == {"GH_CONFIG_DIR": os.fspath(config_root.resolve())}
    assert captured[1]["environment"] is None
    assert all(item["additional_path_entries"] == (git_executable.parent,) for item in captured)
    assert config_root.is_dir()


def test_blank_and_github_projects_share_the_managed_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git_executable = shutil.which("git.exe" if os.name == "nt" else "git")
    if git_executable is None:
        pytest.skip("git is unavailable")
    github = _InitializingGitHub(GitProcess(Path(git_executable)))
    monkeypatch.setattr(api_module, "_build_github_cli", lambda *_args: github)
    client, headers = _paired_client(tmp_path)

    blank = client.post(
        "/api/local/projects",
        json={"name": "My demo project"},
        headers=headers,
    )
    connected = client.post(
        "/api/local/github/repositories",
        json={"slug": "https://github.com/team/corvus.git"},
        headers=headers,
    )

    assert blank.status_code == 200, blank.text
    assert connected.status_code == 200, connected.text
    managed_root = (tmp_path / "corvus-agent-projects").resolve()
    blank_path = Path(blank.json()["path"])
    connected_path = Path(connected.json()["path"])
    assert blank_path.parent == managed_root
    assert connected_path.parent == managed_root
    assert blank_path.name.startswith("My-demo-project-")
    assert connected_path.name.startswith("corvus-")
    assert blank_path != connected_path
    assert github.clones == [("team/corvus", connected_path)]


def test_failed_github_clone_returns_sanitized_error_and_removes_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    github = _FailingGitHub()
    monkeypatch.setattr(api_module, "_build_github_cli", lambda *_args: github)
    client, headers = _paired_client(tmp_path)

    response = client.post(
        "/api/local/github/repositories",
        json={"slug": "team/corvus"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "github_clone_failed"
    assert github.target is not None
    assert github.target.parent == (tmp_path / "corvus-agent-projects").resolve()
    assert not github.target.exists()
