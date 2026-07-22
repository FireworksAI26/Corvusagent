from __future__ import annotations

from pathlib import Path

import pytest

from corvus.mvp.git_process import ProcessResult
from corvus.mvp.github_cli import GitHubCli, GitHubCliError


class FakeRunner:
    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[Path, tuple[str, ...], float]] = []

    def run(
        self,
        cwd: Path,
        args: tuple[str, ...],
        timeout: float = 30,
    ) -> ProcessResult:
        self.calls.append((cwd, args, timeout))
        return self.results.pop(0)


def test_github_cli_parses_repository_json(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            0,
            b'[{"name":"corvus","nameWithOwner":"team/corvus","url":"https://github.com/team/corvus","defaultBranchRef":{"name":"main"},"isPrivate":true}]',
            b"",
        )
    )

    repositories = GitHubCli(runner, cwd=tmp_path).list_repositories(limit=12)

    assert repositories[0].slug == "team/corvus"
    assert repositories[0].default_branch == "main"
    assert repositories[0].private is True
    assert runner.calls[0][1] == (
        "repo",
        "list",
        "--limit",
        "12",
        "--json",
        "name,nameWithOwner,url,defaultBranchRef,isPrivate",
    )


def test_github_cli_parses_pull_requests_and_checks(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            0,
            b'[{"number":7,"title":"MVP","state":"OPEN","isDraft":true,"url":"https://github.com/team/corvus/pull/7","headRefName":"feature","baseRefName":"main"}]',
            b"",
        ),
        ProcessResult(
            0,
            b'[{"name":"tests","state":"SUCCESS","bucket":"pass","link":"https://example.test/check","workflow":"CI"}]',
            b"",
        ),
    )
    client = GitHubCli(runner, cwd=tmp_path)

    pulls = client.list_pull_requests("team/corvus")
    checks = client.pull_request_checks("team/corvus", 7)

    assert pulls[0].number == 7
    assert pulls[0].draft is True
    assert checks[0].bucket == "pass"
    assert runner.calls[1][1][:5] == ("pr", "checks", "7", "--repo", "team/corvus")


def test_github_cli_auth_status_is_return_code_only(tmp_path: Path) -> None:
    runner = FakeRunner(ProcessResult(1, b"", b"token ghp_secret expired"))

    status = GitHubCli(runner, cwd=tmp_path).auth_status()

    assert status.authenticated is False
    assert status.hostname == "github.com"


def test_github_cli_authorizes_an_existing_host_session_only_after_explicit_sign_in(
    tmp_path: Path,
) -> None:
    isolated = FakeRunner(ProcessResult(1, b"", b"not logged in"))
    host = FakeRunner(
        ProcessResult(0, b"github.com authenticated", b""),
        ProcessResult(0, b"github.com authenticated", b""),
    )
    client = GitHubCli(isolated, cwd=tmp_path, authorization_runner=host)

    assert client.auth_status().authenticated is False
    assert client.authenticate().authenticated is True
    assert client.auth_status().authenticated is True
    assert isolated.calls[0][1][:3] == ("auth", "status", "--hostname")
    assert [call[1][1] for call in host.calls] == ["status", "status"]


def test_github_cli_runs_web_login_when_the_host_session_is_missing(tmp_path: Path) -> None:
    isolated = FakeRunner()
    host = FakeRunner(
        ProcessResult(1, b"", b"not logged in"),
        ProcessResult(0, b"", b""),
        ProcessResult(0, b"github.com authenticated", b""),
    )
    client = GitHubCli(isolated, cwd=tmp_path, authorization_runner=host)

    status = client.authenticate()

    assert status.authenticated is True
    assert host.calls[1][1] == (
        "auth",
        "login",
        "--hostname",
        "github.com",
        "--git-protocol",
        "https",
        "--web",
    )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("team/corvus", "team/corvus"),
        (" https://github.com/team/corvus ", "team/corvus"),
        ("https://github.com/team/corvus.git/", "team/corvus"),
    ],
)
def test_github_cli_normalizes_slug_or_https_repository_url(
    reference: str,
    expected: str,
) -> None:
    assert GitHubCli.normalize_repository_reference(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/team/corvus",
        "https://github.com/team/corvus/issues",
        "https://user@github.com/team/corvus",
        "https://github.com/team/../corvus",
        "team/../corvus",
        "team\\corvus",
    ],
)
def test_github_cli_rejects_noncanonical_or_traversing_repository_references(
    reference: str,
) -> None:
    with pytest.raises(GitHubCliError, match="identifier is invalid"):
        GitHubCli.normalize_repository_reference(reference)


def test_github_cli_requires_valid_json(tmp_path: Path) -> None:
    runner = FakeRunner(ProcessResult(0, b"not-json", b""))

    with pytest.raises(GitHubCliError, match="invalid JSON"):
        GitHubCli(runner, cwd=tmp_path).list_repositories()


def test_github_cli_redacts_command_failure_stderr(tmp_path: Path) -> None:
    runner = FakeRunner(ProcessResult(1, b"", b"token ghp_secret expired"))

    with pytest.raises(GitHubCliError) as raised:
        GitHubCli(runner, cwd=tmp_path).list_repositories()

    assert "ghp_secret" not in str(raised.value)


def test_github_cli_creates_draft_pull_request_with_exact_arguments(tmp_path: Path) -> None:
    runner = FakeRunner(ProcessResult(0, b"https://github.com/team/corvus/pull/8\n", b""))

    url = GitHubCli(runner, cwd=tmp_path).create_pull_request(
        repo="team/corvus",
        head="corvus/run-1",
        base="main",
        title="Ship MVP",
        body="Evidence and notes",
        draft=True,
    )

    assert url == "https://github.com/team/corvus/pull/8"
    assert runner.calls[0][1] == (
        "pr",
        "create",
        "--repo",
        "team/corvus",
        "--head",
        "corvus/run-1",
        "--base",
        "main",
        "--title",
        "Ship MVP",
        "--body",
        "Evidence and notes",
        "--draft",
    )
