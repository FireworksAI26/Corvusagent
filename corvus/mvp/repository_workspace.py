from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from corvus.mvp.core import DomainConflict, DomainNotFound
from corvus.mvp.git_process import GitProcess, ProcessResult
from corvus.mvp.models import MvpModel
from corvus.mvp.store import SqliteStore
from corvus.safe_process import path_is_link_or_reparse

_GITHUB_SCP_REMOTE = re.compile(
    r"^(?:[^@\s]+@)?github\.com:(?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?$",
    re.IGNORECASE,
)


class RepositorySnapshot(MvpModel):
    branch: str
    head_sha: str
    clean: bool
    ahead: int
    behind: int
    health: str
    refreshed_at: datetime


class RepositoryRecord(MvpModel):
    id: str
    tenant_id: str
    display_name: str
    path: str
    remote_slug: str | None
    default_branch: str | None
    created_at: datetime
    updated_at: datetime
    snapshot: RepositorySnapshot


class RepositoryWorkspaceService:
    def __init__(self, store: SqliteStore, git: GitProcess) -> None:
        self.store = store
        self.git = git

    def register_local(
        self,
        tenant_id: str,
        path: Path,
        display_name: str,
    ) -> RepositoryRecord:
        name = display_name.strip()
        if not name or len(name) > 200:
            raise ValueError("repository_display_name_invalid")
        selected = path.expanduser().absolute()
        if self._has_link_component(selected):
            raise ValueError("repository_path_links_forbidden")
        try:
            selected = selected.resolve(strict=True)
        except OSError as exc:
            raise ValueError("repository_path_unavailable") from exc
        if not selected.is_dir():
            raise ValueError("repository_path_unavailable")

        root_result = self.git.run(selected, ("rev-parse", "--show-toplevel"))
        if root_result.returncode != 0:
            raise ValueError("not_a_git_repository")
        root_text = self._decode_single_line(root_result, "git_repository_root_invalid")
        try:
            root = Path(root_text).resolve(strict=True)
        except OSError as exc:
            raise ValueError("git_repository_root_invalid") from exc
        if not root.is_dir() or self._has_link_component(root):
            raise ValueError("repository_path_links_forbidden")

        remote_slug = self._remote_slug(root)
        default_branch = self._default_branch(root)
        now = datetime.now(UTC)
        repository_id = str(uuid4())
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO mvp_repositories "
                    "(id, tenant_id, canonical_path, display_name, remote_slug, default_branch, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repository_id,
                        tenant_id,
                        os.fspath(root),
                        name,
                        remote_slug,
                        default_branch,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DomainConflict("repository_already_registered") from exc
        return self.refresh(tenant_id, repository_id)

    def list(
        self, tenant_id: str, *, limit: int = 1000, offset: int = 0
    ) -> tuple[RepositoryRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT r.*, s.branch, s.head_sha, s.clean, s.ahead, s.behind, s.health, "
                "s.refreshed_at FROM mvp_repositories r "
                "JOIN mvp_repository_snapshots s ON s.repository_id = r.id "
                "WHERE r.tenant_id = ? ORDER BY r.updated_at DESC, r.id LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def get(self, tenant_id: str, repository_id: str) -> RepositoryRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT r.*, s.branch, s.head_sha, s.clean, s.ahead, s.behind, s.health, "
                "s.refreshed_at FROM mvp_repositories r "
                "JOIN mvp_repository_snapshots s ON s.repository_id = r.id "
                "WHERE r.tenant_id = ? AND r.id = ?",
                (tenant_id, repository_id),
            ).fetchone()
        if row is None:
            raise DomainNotFound("repository_not_found")
        return self._record(row)

    def refresh(self, tenant_id: str, repository_id: str) -> RepositoryRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_repositories WHERE tenant_id = ? AND id = ?",
                (tenant_id, repository_id),
            ).fetchone()
            previous = connection.execute(
                "SELECT * FROM mvp_repository_snapshots WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
        if row is None:
            raise DomainNotFound("repository_not_found")
        root = Path(str(row["canonical_path"]))
        now = datetime.now(UTC)
        if not root.is_dir() or path_is_link_or_reparse(root):
            branch = str(previous["branch"]) if previous is not None else ""
            head_sha = str(previous["head_sha"]) if previous is not None else ""
            clean = False
            ahead = 0
            behind = 0
            health = "missing"
            remote_slug = str(row["remote_slug"]) if row["remote_slug"] is not None else None
            default_branch = (
                str(row["default_branch"]) if row["default_branch"] is not None else None
            )
        else:
            head = self._required_git(root, ("rev-parse", "--verify", "HEAD"))
            branch_result = self.git.run(root, ("branch", "--show-current"))
            branch = (
                self._decode_single_line(branch_result, "git_branch_invalid")
                if branch_result.returncode == 0
                else ""
            )
            status_result = self.git.run(root, ("status", "--porcelain=v1", "-z"))
            if status_result.returncode != 0:
                raise ValueError("git_status_failed")
            clean = status_result.stdout == b""
            ahead, behind = self._ahead_behind(root)
            head_sha = head
            health = "healthy"
            remote_slug = self._remote_slug(root)
            default_branch = self._default_branch(root)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_repository_snapshots "
                "(repository_id, branch, head_sha, clean, ahead, behind, health, refreshed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repository_id) DO UPDATE SET branch = excluded.branch, "
                "head_sha = excluded.head_sha, clean = excluded.clean, ahead = excluded.ahead, "
                "behind = excluded.behind, health = excluded.health, "
                "refreshed_at = excluded.refreshed_at",
                (
                    repository_id,
                    branch,
                    head_sha,
                    int(clean),
                    ahead,
                    behind,
                    health,
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE mvp_repositories SET remote_slug = ?, default_branch = ?, "
                "updated_at = ? WHERE id = ? AND tenant_id = ?",
                (remote_slug, default_branch, now.isoformat(), repository_id, tenant_id),
            )
        return self.get(tenant_id, repository_id)

    def remove(self, tenant_id: str, repository_id: str) -> None:
        try:
            with self.store.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM mvp_repositories WHERE tenant_id = ? AND id = ?",
                    (tenant_id, repository_id),
                )
                if cursor.rowcount != 1:
                    raise DomainNotFound("repository_not_found")
        except sqlite3.IntegrityError as exc:
            raise DomainConflict("repository_in_use") from exc

    def _remote_slug(self, root: Path) -> str | None:
        result = self.git.run(root, ("remote", "get-url", "origin"))
        if result.returncode != 0:
            return None
        return self._github_slug(self._decode_single_line(result, "git_remote_invalid"))

    def _default_branch(self, root: Path) -> str | None:
        remote = self.git.run(
            root,
            ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
        )
        if remote.returncode == 0:
            value = self._decode_single_line(remote, "git_default_branch_invalid")
            return value.removeprefix("origin/") or None
        current = self.git.run(root, ("branch", "--show-current"))
        if current.returncode != 0:
            return None
        return self._decode_single_line(current, "git_default_branch_invalid") or None

    def _ahead_behind(self, root: Path) -> tuple[int, int]:
        result = self.git.run(
            root,
            ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"),
        )
        if result.returncode != 0:
            return 0, 0
        parts = result.stdout.decode("ascii", errors="strict").strip().split()
        if len(parts) != 2 or any(not part.isdigit() for part in parts):
            raise ValueError("git_ahead_behind_invalid")
        return int(parts[0]), int(parts[1])

    def _required_git(self, root: Path, args: tuple[str, ...]) -> str:
        result = self.git.run(root, args)
        if result.returncode != 0:
            raise ValueError("git_repository_refresh_failed")
        return self._decode_single_line(result, "git_output_invalid")

    @staticmethod
    def _decode_single_line(result: ProcessResult, error: str) -> str:
        try:
            value = result.stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ValueError(error) from exc
        if not value or "\0" in value or "\n" in value or "\r" in value:
            raise ValueError(error)
        return value

    @staticmethod
    def _github_slug(remote: str) -> str | None:
        scp_match = _GITHUB_SCP_REMOTE.fullmatch(remote)
        if scp_match is not None:
            return scp_match.group("slug")
        parsed = urlparse(remote)
        if (
            parsed.scheme.lower() not in {"https", "ssh"}
            or parsed.hostname is None
            or parsed.hostname.lower() != "github.com"
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        if parsed.scheme.lower() == "https" and parsed.username is not None:
            return None
        slug = parsed.path.strip("/").removesuffix(".git")
        if len(slug.split("/")) != 2:
            return None
        return slug

    @staticmethod
    def _has_link_component(path: Path) -> bool:
        current = path
        components: list[Path] = []
        while current != current.parent:
            components.append(current)
            current = current.parent
        return any(
            component.exists() and path_is_link_or_reparse(component) for component in components
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> RepositoryRecord:
        return RepositoryRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            display_name=str(row["display_name"]),
            path=str(row["canonical_path"]),
            remote_slug=str(row["remote_slug"]) if row["remote_slug"] is not None else None,
            default_branch=(
                str(row["default_branch"]) if row["default_branch"] is not None else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            snapshot=RepositorySnapshot(
                branch=str(row["branch"]),
                head_sha=str(row["head_sha"]),
                clean=bool(row["clean"]),
                ahead=int(row["ahead"]),
                behind=int(row["behind"]),
                health=str(row["health"]),
                refreshed_at=datetime.fromisoformat(str(row["refreshed_at"])),
            ),
        )
