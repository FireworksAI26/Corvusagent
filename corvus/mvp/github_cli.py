from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from corvus.mvp.git_process import ProcessResult
from corvus.safe_process import path_is_link_or_reparse

_GITHUB_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class GitHubCliError(RuntimeError):
    """A sanitized GitHub CLI error safe to return to the local UI."""


class CommandRunner(Protocol):
    def run(
        self,
        cwd: Path,
        args: tuple[str, ...],
        timeout: float = 30,
    ) -> ProcessResult: ...


@dataclass(frozen=True, slots=True)
class GitHubAuthStatus:
    hostname: str
    authenticated: bool


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    name: str
    slug: str
    url: str
    default_branch: str | None
    private: bool


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    number: int
    title: str
    state: str
    draft: bool
    url: str
    head_branch: str
    base_branch: str


@dataclass(frozen=True, slots=True)
class GitHubCheck:
    name: str
    state: str
    bucket: str
    link: str | None
    workflow: str | None


class GitHubCli:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        cwd: Path,
        authorization_runner: CommandRunner | None = None,
    ) -> None:
        self._runner = runner
        self._authorization_runner = authorization_runner
        try:
            self._cwd = cwd.expanduser().resolve(strict=True)
        except OSError as exc:
            raise GitHubCliError("GitHub CLI working directory is unavailable") from exc

    def auth_status(self) -> GitHubAuthStatus:
        return self._auth_status(self._runner)

    def _auth_status(self, runner: CommandRunner) -> GitHubAuthStatus:
        result = runner.run(
            self._cwd,
            ("auth", "status", "--hostname", "github.com", "--active"),
            15,
        )
        return GitHubAuthStatus(hostname="github.com", authenticated=result.returncode == 0)

    def authenticate(self) -> GitHubAuthStatus:
        authorization_runner = self._authorization_runner or self._runner
        existing = self._auth_status(authorization_runner)
        if existing.authenticated:
            self._runner = authorization_runner
            return existing
        result = authorization_runner.run(
            self._cwd,
            ("auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"),
            180,
        )
        if result.returncode != 0:
            raise GitHubCliError("github_authentication_failed")
        authenticated = self._auth_status(authorization_runner)
        if not authenticated.authenticated:
            raise GitHubCliError("github_authentication_failed")
        self._runner = authorization_runner
        return authenticated

    def clone_repository(self, repo: str, target: Path) -> None:
        repo = self.normalize_repository_reference(repo)
        try:
            parent = target.parent.resolve(strict=True)
            destination = target.resolve(strict=False)
        except OSError as exc:
            raise GitHubCliError("github_clone_target_invalid") from exc
        if path_is_link_or_reparse(parent) or destination.parent != parent or destination.exists():
            raise GitHubCliError("github_clone_target_invalid")
        result = self._runner.run(
            parent,
            (
                "repo",
                "clone",
                repo,
                str(destination),
                "--",
                "--config",
                "core.fsmonitor=false",
                "--config",
                f"core.hooksPath={Path(os.devnull)}",
            ),
            180,
        )
        if result.returncode != 0:
            raise GitHubCliError("github_clone_failed")
        try:
            cloned = destination.resolve(strict=True)
        except OSError as exc:
            raise GitHubCliError("github_clone_failed") from exc
        if not cloned.is_dir() or path_is_link_or_reparse(cloned) or cloned.parent != parent:
            raise GitHubCliError("github_clone_target_invalid")

    def list_repositories(self, *, limit: int = 100) -> tuple[GitHubRepository, ...]:
        if limit < 1 or limit > 1_000:
            raise GitHubCliError("repository list limit is invalid")
        payload = self._json_command(
            (
                "repo",
                "list",
                "--limit",
                str(limit),
                "--json",
                "name,nameWithOwner,url,defaultBranchRef,isPrivate",
            )
        )
        rows = self._require_list(payload)
        repositories: list[GitHubRepository] = []
        for row in rows:
            item = self._require_object(row)
            branch_value = item.get("defaultBranchRef")
            default_branch: str | None = None
            if branch_value is not None:
                branch = self._require_object(branch_value)
                default_branch = self._require_string(branch, "name")
            repositories.append(
                GitHubRepository(
                    name=self._require_string(item, "name"),
                    slug=self._require_string(item, "nameWithOwner"),
                    url=self._require_string(item, "url"),
                    default_branch=default_branch,
                    private=self._require_bool(item, "isPrivate"),
                )
            )
        return tuple(repositories)

    def list_pull_requests(self, repo: str) -> tuple[GitHubPullRequest, ...]:
        repo = self.normalize_repository_reference(repo)
        payload = self._json_command(
            (
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,title,state,isDraft,url,headRefName,baseRefName",
            )
        )
        pulls: list[GitHubPullRequest] = []
        for row in self._require_list(payload):
            item = self._require_object(row)
            pulls.append(
                GitHubPullRequest(
                    number=self._require_int(item, "number"),
                    title=self._require_string(item, "title"),
                    state=self._require_string(item, "state"),
                    draft=self._require_bool(item, "isDraft"),
                    url=self._require_string(item, "url"),
                    head_branch=self._require_string(item, "headRefName"),
                    base_branch=self._require_string(item, "baseRefName"),
                )
            )
        return tuple(pulls)

    def pull_request_checks(self, repo: str, number: int) -> tuple[GitHubCheck, ...]:
        repo = self.normalize_repository_reference(repo)
        if number <= 0:
            raise GitHubCliError("pull request number is invalid")
        payload = self._json_command(
            (
                "pr",
                "checks",
                str(number),
                "--repo",
                repo,
                "--json",
                "name,state,bucket,link,workflow",
            ),
            allowed_returncodes=(0, 8),
        )
        checks: list[GitHubCheck] = []
        for row in self._require_list(payload):
            item = self._require_object(row)
            checks.append(
                GitHubCheck(
                    name=self._require_string(item, "name"),
                    state=self._require_string(item, "state"),
                    bucket=self._require_string(item, "bucket"),
                    link=self._optional_string(item, "link"),
                    workflow=self._optional_string(item, "workflow"),
                )
            )
        return tuple(checks)

    def create_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> str:
        repo = self.normalize_repository_reference(repo)
        for value in (head, base, title):
            if not value or "\0" in value:
                raise GitHubCliError("pull request input is invalid")
        arguments = [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            arguments.append("--draft")
        result = self._runner.run(self._cwd, tuple(arguments), 60)
        if result.returncode != 0:
            raise GitHubCliError("GitHub CLI command failed")
        url = result.stdout.decode("utf-8", errors="strict").strip()
        if not url.startswith("https://github.com/") or "\n" in url:
            raise GitHubCliError("GitHub CLI returned an invalid pull request URL")
        return url

    def _json_command(
        self,
        args: tuple[str, ...],
        *,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> object:
        result = self._runner.run(self._cwd, args, 30)
        if result.returncode not in allowed_returncodes:
            raise GitHubCliError("GitHub CLI command failed")
        try:
            return json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubCliError("GitHub CLI returned invalid JSON") from exc

    @staticmethod
    def normalize_repository_reference(reference: str) -> str:
        value = reference.strip()
        if not value or "\0" in value or "\\" in value:
            raise GitHubCliError("GitHub repository identifier is invalid")
        if "://" in value:
            try:
                parsed = urlsplit(value)
                has_port = parsed.port is not None
            except ValueError as exc:
                raise GitHubCliError("GitHub repository identifier is invalid") from exc
            if (
                parsed.scheme != "https"
                or parsed.hostname != "github.com"
                or parsed.username is not None
                or parsed.password is not None
                or has_port
                or parsed.query
                or parsed.fragment
            ):
                raise GitHubCliError("GitHub repository identifier is invalid")
            parts = parsed.path.strip("/").split("/")
        else:
            parts = value.split("/")
        if len(parts) != 2:
            raise GitHubCliError("GitHub repository identifier is invalid")
        owner, repository = parts
        if repository.endswith(".git"):
            repository = repository[:-4]
        normalized = (owner, repository)
        if any(
            part in {"", ".", ".."} or _GITHUB_REPOSITORY_PART.fullmatch(part) is None
            for part in normalized
        ):
            raise GitHubCliError("GitHub repository identifier is invalid")
        return "/".join(normalized)

    @staticmethod
    def _require_list(value: object) -> list[object]:
        if not isinstance(value, list):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return cast(list[object], value)

    @staticmethod
    def _require_object(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return cast(dict[str, object], value)

    @staticmethod
    def _require_string(value: dict[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return item

    @staticmethod
    def _optional_string(value: dict[str, object], key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return item

    @staticmethod
    def _require_bool(value: dict[str, object], key: str) -> bool:
        item = value.get(key)
        if not isinstance(item, bool):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return item

    @staticmethod
    def _require_int(value: dict[str, object], key: str) -> int:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool):
            raise GitHubCliError("GitHub CLI returned an invalid JSON shape")
        return item
