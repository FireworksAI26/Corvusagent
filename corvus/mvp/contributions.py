from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from corvus.mvp.change_review import ChangeReviewService, ChangeSet
from corvus.mvp.git_process import GitProcess
from corvus.mvp.github_cli import GitHubCliError, GitHubPullRequest
from corvus.mvp.models import MvpModel
from corvus.mvp.secret_scan import SecretScanner, SecretScanResult
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager


class ContributionConflict(RuntimeError):
    pass


class GitHubContributionClient(Protocol):
    def create_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> str: ...

    def list_pull_requests(self, repo: str) -> tuple[GitHubPullRequest, ...]: ...


class ContributionRecord(MvpModel):
    id: str
    run_id: str
    repository_id: str
    branch: str
    base_branch: str
    selected_paths: tuple[str, ...]
    confirmation_digest: str
    message: str
    title: str
    body: str
    draft: bool
    change_digest: str
    secret_scan: SecretScanResult
    commit_sha: str | None
    remote_ref: str | None
    pr_number: int | None
    pr_url: str | None
    state: Literal["preparing", "branch_created", "committed", "pushed", "published"]
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ContributionService:
    def __init__(
        self,
        store: SqliteStore,
        git: GitProcess,
        worktrees: WorktreeManager,
        review: ChangeReviewService,
        scanner: SecretScanner,
        github: GitHubContributionClient,
        *,
        confirmation_secret: bytes,
    ) -> None:
        if len(confirmation_secret) < 16:
            raise ValueError("contribution_confirmation_secret_too_short")
        self.store = store
        self.git = git
        self.worktrees = worktrees
        self.review = review
        self.scanner = scanner
        self.github = github
        self._confirmation_secret = confirmation_secret

    def prepare(
        self,
        run_id: str,
        *,
        selected_paths: tuple[str, ...],
        message: str,
        title: str,
        body: str,
        draft: bool,
    ) -> ContributionRecord:
        if not draft:
            raise ContributionConflict("contribution_draft_required")
        message = self._required_text(message, "contribution_message_invalid", 200)
        title = self._required_text(title, "contribution_title_invalid", 200)
        body = self._required_text(body, "contribution_body_invalid", 20_000)
        if not selected_paths or len(selected_paths) > 500:
            raise ContributionConflict("contribution_paths_invalid")
        lease = self.worktrees.get(run_id)
        if lease.status != "active":
            raise ContributionConflict("contribution_worktree_inactive")
        with self.store.connect() as connection:
            repository = connection.execute(
                "SELECT * FROM mvp_repositories WHERE id = ?",
                (lease.repository_id,),
            ).fetchone()
        if repository is None:
            raise ContributionConflict("contribution_repository_missing")
        remote_slug = (
            str(repository["remote_slug"]) if repository["remote_slug"] is not None else None
        )
        if remote_slug is None:
            raise ContributionConflict("contribution_github_remote_required")
        base_branch = (
            str(repository["default_branch"])
            if repository["default_branch"] is not None
            else "main"
        )
        normalized_selected = tuple(dict.fromkeys(selected_paths))
        existing = self._for_run(lease.run_id)
        if existing is not None:
            if (
                existing.selected_paths != normalized_selected
                or existing.message != message
                or existing.title != title
                or existing.body != body
                or existing.draft != draft
            ):
                raise ContributionConflict("contribution_prepare_conflict")
            return self._resume_prepare(lease.root, lease.base_sha, existing)
        all_changes = self.review.snapshot(lease.root)
        changed_paths = {item.path for item in all_changes.files}
        if set(normalized_selected) - changed_paths:
            raise ContributionConflict("contribution_paths_not_changed")
        selected_changes = self._selected_changes(lease.root, normalized_selected)
        if not selected_changes.files:
            raise ContributionConflict("contribution_paths_invalid")
        scan = self._scan_selected(lease.root, selected_changes)
        if scan.status == "blocked":
            raise ContributionConflict("contribution_secret_scan_blocked")
        request_payload = {
            "run_id": lease.run_id,
            "paths": normalized_selected,
            "message": message,
            "title": title,
            "body": body,
            "draft": draft,
            "change_digest": selected_changes.digest,
            "scan_digest": scan.digest,
        }
        request_digest = self._json_digest(request_payload)
        confirmation_digest = hmac.new(
            self._confirmation_secret,
            request_digest.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        branch = self._branch(lease.run_id, title)
        now = datetime.now(UTC)
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO mvp_contributions "
                    "(id, run_id, repository_id, branch, base_branch, selected_paths_json, "
                    "request_digest, confirmation_digest, message, title, body, draft, "
                    "change_digest, secret_scan_json, commit_sha, remote_ref, pr_number, "
                    "pr_url, state, last_error, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, "
                    "NULL, 'preparing', NULL, ?, ?)",
                    (
                        str(uuid4()),
                        lease.run_id,
                        lease.repository_id,
                        branch,
                        base_branch,
                        json.dumps(normalized_selected),
                        request_digest,
                        confirmation_digest,
                        message,
                        title,
                        body,
                        int(draft),
                        selected_changes.digest,
                        scan.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ContributionConflict("contribution_prepare_conflict") from exc
        return self._resume_prepare(
            lease.root,
            lease.base_sha,
            self._required_for_run(lease.run_id),
        )

    def _resume_prepare(
        self,
        root: Path,
        base_sha: str,
        record: ContributionRecord,
    ) -> ContributionRecord:
        if record.state in {"committed", "pushed", "published"}:
            return record
        if record.state == "preparing":
            self._revalidate_prepare(root, record)
            self._create_or_resume_branch(root, record.branch, base_sha)
            self._set_state(record.run_id, "branch_created")
            record = self._required_for_run(record.run_id)
        if record.state == "branch_created":
            selected_changes = self._revalidate_prepare(root, record)
            commit_sha = self._commit_selected(root, record, selected_changes)
            self._set_state(record.run_id, "committed", commit_sha=commit_sha)
        return self._required_for_run(record.run_id)

    def publish(self, run_id: str, *, expected_digest: str) -> ContributionRecord:
        record = self._required_for_run(run_id)
        if not hmac.compare_digest(record.confirmation_digest, expected_digest):
            raise ContributionConflict("contribution_confirmation_digest_mismatch")
        if record.state == "published":
            return record
        if record.state not in {"committed", "pushed"}:
            raise ContributionConflict("contribution_not_prepared")
        lease = self.worktrees.get(run_id)
        with self.store.connect() as connection:
            repository = connection.execute(
                "SELECT remote_slug FROM mvp_repositories WHERE id = ?",
                (record.repository_id,),
            ).fetchone()
        if repository is None or repository["remote_slug"] is None:
            raise ContributionConflict("contribution_github_remote_required")
        remote_slug = str(repository["remote_slug"])
        if record.state == "committed":
            if record.commit_sha is None:
                raise ContributionConflict("contribution_commit_missing")
            branch_head = self.git.run(
                lease.root,
                ("rev-parse", "--verify", f"refs/heads/{record.branch}"),
            )
            if (
                branch_head.returncode != 0
                or branch_head.stdout.decode("ascii", errors="strict").strip() != record.commit_sha
            ):
                raise ContributionConflict("contribution_commit_changed")
            pushed = self.git.run(
                lease.root,
                ("push", "origin", f"{record.commit_sha}:refs/heads/{record.branch}"),
                timeout=180,
            )
            if pushed.returncode != 0:
                self._set_error(run_id, "contribution_push_failed")
                raise ContributionConflict("contribution_push_failed")
            self._set_state(
                run_id,
                "pushed",
                remote_ref=f"refs/remotes/origin/{record.branch}",
            )
            record = self._required_for_run(run_id)
        if record.commit_sha is None:
            raise ContributionConflict("contribution_commit_missing")
        remote_head = self.git.run(
            lease.root,
            ("ls-remote", "--exit-code", "origin", f"refs/heads/{record.branch}"),
            timeout=60,
        )
        expected_remote = f"{record.commit_sha}\trefs/heads/{record.branch}"
        if (
            remote_head.returncode != 0
            or remote_head.stdout.decode("ascii", errors="strict").strip() != expected_remote
        ):
            self._set_error(run_id, "contribution_remote_changed")
            raise ContributionConflict("contribution_remote_changed")
        try:
            pr_url = self.github.create_pull_request(
                repo=remote_slug,
                head=record.branch,
                base=record.base_branch,
                title=record.title,
                body=record.body,
                draft=record.draft,
            )
            pr_number = self._pr_number(pr_url)
        except GitHubCliError:
            recovered = next(
                (
                    pull
                    for pull in self.github.list_pull_requests(remote_slug)
                    if pull.head_branch == record.branch and pull.base_branch == record.base_branch
                ),
                None,
            )
            if recovered is None:
                self._set_error(run_id, "contribution_pull_request_failed")
                raise ContributionConflict("contribution_pull_request_failed") from None
            pr_url = recovered.url
            pr_number = recovered.number
        self._set_state(
            run_id,
            "published",
            pr_url=pr_url,
            pr_number=pr_number,
        )
        return self._required_for_run(run_id)

    def get(self, run_id: str) -> ContributionRecord:
        return self._required_for_run(run_id)

    def changes(self, run_id: str) -> ChangeSet:
        lease = self.worktrees.get(run_id)
        if lease.status != "active":
            raise ContributionConflict("contribution_worktree_inactive")
        return self.review.snapshot(lease.root)

    def _create_or_resume_branch(self, root: Path, branch: str, base_sha: str) -> None:
        result = self.git.run(root, ("switch", "-c", branch, base_sha))
        if result.returncode == 0:
            return
        current = self.git.run(root, ("branch", "--show-current"))
        if (
            current.returncode != 0
            or current.stdout.decode("utf-8", errors="strict").strip() != branch
        ):
            raise ContributionConflict("contribution_branch_failed")
        head = self.git.run(root, ("rev-parse", "--verify", "HEAD"))
        if head.returncode != 0 or head.stdout.decode("ascii", errors="strict").strip() != base_sha:
            raise ContributionConflict("contribution_branch_base_changed")

    def _commit_selected(
        self,
        root: Path,
        record: ContributionRecord,
        changes: ChangeSet,
    ) -> str:
        stage_paths = self._stage_paths(changes)
        existing = self._existing_commit(root, record, stage_paths)
        if existing is not None:
            return existing
        reset = self.git.run(root, ("reset", "--mixed", "HEAD"))
        if reset.returncode != 0:
            raise ContributionConflict("contribution_stage_failed")
        added = self.git.run(root, ("add", "--", *stage_paths))
        if added.returncode != 0:
            raise ContributionConflict("contribution_stage_failed")
        staged = self.git.run(
            root,
            ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        )
        if staged.returncode != 0:
            raise ContributionConflict("contribution_stage_failed")
        staged_paths = {
            item for item in staged.stdout.decode("utf-8", errors="strict").split("\0") if item
        }
        if staged_paths != set(stage_paths):
            raise ContributionConflict("contribution_stage_selection_mismatch")
        # Re-read and re-scan after staging. This binds the bytes selected by `git add`
        # to the reviewed change set and catches filesystem watchers that mutate a file
        # between the first review and index staging.
        self._revalidate_prepare(root, record)
        committed = self.git.run(
            root,
            ("commit", "--no-verify", "-m", record.message),
            timeout=120,
        )
        if committed.returncode != 0:
            raise ContributionConflict("contribution_commit_failed")
        head = self.git.run(root, ("rev-parse", "--verify", "HEAD"))
        if head.returncode != 0:
            raise ContributionConflict("contribution_commit_failed")
        return head.stdout.decode("ascii", errors="strict").strip()

    def _existing_commit(
        self,
        root: Path,
        record: ContributionRecord,
        stage_paths: tuple[str, ...],
    ) -> str | None:
        head = self.git.run(root, ("rev-parse", "--verify", "HEAD"))
        if head.returncode != 0:
            return None
        head_sha = head.stdout.decode("ascii", errors="strict").strip()
        with self.store.connect() as connection:
            lease = connection.execute(
                "SELECT base_sha FROM mvp_worktree_leases WHERE run_id = ?",
                (record.run_id,),
            ).fetchone()
        if lease is None or head_sha == str(lease["base_sha"]):
            return None
        message = self.git.run(root, ("log", "-1", "--format=%B"))
        paths = self.git.run(
            root,
            (
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                "HEAD",
            ),
        )
        if message.returncode != 0 or paths.returncode != 0:
            return None
        changed = {
            item for item in paths.stdout.decode("utf-8", errors="strict").split("\0") if item
        }
        if message.stdout.decode("utf-8", errors="strict").strip() != record.message:
            return None
        if changed != set(stage_paths):
            return None
        return head_sha

    def _selected_changes(self, root: Path, selected_paths: tuple[str, ...]) -> ChangeSet:
        changes = self.review.snapshot(root, selected_paths=selected_paths)
        if {item.path for item in changes.files} != set(selected_paths):
            raise ContributionConflict("contribution_paths_not_changed")
        return changes

    def _scan_selected(self, root: Path, changes: ChangeSet) -> SecretScanResult:
        paths = tuple(item.path for item in changes.files)
        deleted = tuple(item.path for item in changes.files if item.status == "deleted")
        deleted_contents: dict[str, bytes] = {}
        for path in deleted:
            result = self.git.run(root, ("show", f"HEAD:{path}"))
            if result.returncode != 0:
                raise ContributionConflict("contribution_deleted_content_unavailable")
            deleted_contents[path] = result.stdout
        return self.scanner.scan(
            root,
            paths,
            deleted_paths=deleted,
            deleted_contents=deleted_contents,
        )

    def _revalidate_prepare(self, root: Path, record: ContributionRecord) -> ChangeSet:
        changes = self._selected_changes(root, record.selected_paths)
        scan = self._scan_selected(root, changes)
        if scan.status == "blocked":
            raise ContributionConflict("contribution_secret_scan_blocked")
        if (
            changes.digest != record.change_digest
            or scan.status != record.secret_scan.status
            or scan.scanned_paths != record.secret_scan.scanned_paths
            or scan.findings != record.secret_scan.findings
        ):
            raise ContributionConflict("contribution_review_changed")
        return changes

    @staticmethod
    def _stage_paths(changes: ChangeSet) -> tuple[str, ...]:
        paths: list[str] = []
        for item in changes.files:
            paths.append(item.path)
            if item.status == "renamed" and item.previous_path is not None:
                paths.append(item.previous_path)
        return tuple(dict.fromkeys(paths))

    def _for_run(self, run_id: str) -> ContributionRecord | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_contributions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def _required_for_run(self, run_id: str) -> ContributionRecord:
        record = self._for_run(run_id)
        if record is None:
            raise ContributionConflict("contribution_not_found")
        return record

    def _set_state(
        self,
        run_id: str,
        state: Literal["branch_created", "committed", "pushed", "published"],
        *,
        commit_sha: str | None = None,
        remote_ref: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE mvp_contributions SET state = ?, commit_sha = COALESCE(?, commit_sha), "
                "remote_ref = COALESCE(?, remote_ref), pr_url = COALESCE(?, pr_url), "
                "pr_number = COALESCE(?, pr_number), last_error = NULL, updated_at = ? "
                "WHERE run_id = ?",
                (state, commit_sha, remote_ref, pr_url, pr_number, now, run_id),
            )

    def _set_error(self, run_id: str, error: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE mvp_contributions SET last_error = ?, updated_at = ? WHERE run_id = ?",
                (error, datetime.now(UTC).isoformat(), run_id),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ContributionRecord:
        return ContributionRecord(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            repository_id=str(row["repository_id"]),
            branch=str(row["branch"]),
            base_branch=str(row["base_branch"]),
            selected_paths=tuple(cast(list[str], json.loads(str(row["selected_paths_json"])))),
            confirmation_digest=str(row["confirmation_digest"]),
            message=str(row["message"]),
            title=str(row["title"]),
            body=str(row["body"]),
            draft=bool(row["draft"]),
            change_digest=str(row["change_digest"]),
            secret_scan=SecretScanResult.model_validate_json(str(row["secret_scan_json"])),
            commit_sha=str(row["commit_sha"]) if row["commit_sha"] is not None else None,
            remote_ref=str(row["remote_ref"]) if row["remote_ref"] is not None else None,
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            pr_url=str(row["pr_url"]) if row["pr_url"] is not None else None,
            state=cast(
                Literal["preparing", "branch_created", "committed", "pushed", "published"],
                str(row["state"]),
            ),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _branch(run_id: str, title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32] or "change"
        return f"corvus/{run_id[:8]}-{slug}"

    @staticmethod
    def _required_text(value: str, error: str, limit: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > limit or "\0" in normalized:
            raise ContributionConflict(error)
        return normalized

    @staticmethod
    def _json_digest(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _pr_number(url: str) -> int:
        match = re.fullmatch(r"https://github\.com/[^/]+/[^/]+/pull/([1-9][0-9]*)", url)
        if match is None:
            raise ContributionConflict("contribution_pull_request_url_invalid")
        return int(match.group(1))
