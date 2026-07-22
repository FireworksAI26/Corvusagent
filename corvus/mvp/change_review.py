from __future__ import annotations

import difflib
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from corvus.mvp.git_process import GitProcess
from corvus.mvp.models import MvpModel
from corvus.safe_process import path_is_link_or_reparse


class ChangeReviewError(RuntimeError):
    pass


class ChangedFile(MvpModel):
    path: str
    previous_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed", "untracked"]
    binary: bool
    patch: str | None
    patch_truncated: bool


class ChangeSet(MvpModel):
    files: tuple[ChangedFile, ...]
    digest: str
    captured_at: datetime


class ChangeReviewService:
    def __init__(self, git: GitProcess, *, max_patch_bytes: int = 256 * 1024) -> None:
        if max_patch_bytes <= 0:
            raise ValueError("change_review_patch_limit_invalid")
        self.git = git
        self._max_patch_bytes = max_patch_bytes

    def snapshot(
        self,
        worktree: Path,
        *,
        selected_paths: tuple[str, ...] | None = None,
    ) -> ChangeSet:
        root = self._root(worktree)
        result = self.git.run(
            root,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        )
        if result.returncode != 0:
            raise ChangeReviewError("change_review_status_failed")
        entries = self._parse_status(result.stdout)
        selected = None
        if selected_paths is not None:
            selected = {
                self._relative_path(root, value, allow_missing=True) for value in selected_paths
            }
        files: list[ChangedFile] = []
        for status, path, previous_path in entries:
            if selected is not None and path not in selected:
                continue
            binary, patch, truncated = self._patch(root, status, path)
            files.append(
                ChangedFile(
                    path=path,
                    previous_path=previous_path,
                    status=status,
                    binary=binary,
                    patch=patch,
                    patch_truncated=truncated,
                )
            )
        files.sort(key=lambda item: item.path)
        captured_at = datetime.now(UTC)
        digest_payload = [
            {
                "path": item.path,
                "previous_path": item.previous_path,
                # An untracked file becomes staged-added without changing its
                # contribution semantics. Keep the digest stable across that
                # safe staging transition so post-stage revalidation compares
                # content rather than Git's transient index label.
                "status": "added" if item.status == "untracked" else item.status,
                "binary": item.binary,
                # Bind the complete file/blob identity. Patch rendering differs
                # for an untracked file before and after `git add`, and may be
                # truncated for display, while content_identity remains exact.
                "content_identity": self._content_identity(root, item.status, item.path),
            }
            for item in files
        ]
        digest = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ChangeSet(files=tuple(files), digest=digest, captured_at=captured_at)

    def _patch(
        self,
        root: Path,
        status: Literal["added", "modified", "deleted", "renamed", "untracked"],
        relative: str,
    ) -> tuple[bool, str | None, bool]:
        path = root / Path(PurePosixPath(relative))
        if path_is_link_or_reparse(path):
            raise ChangeReviewError("change_review_path_invalid")
        if path.exists() and path.is_file():
            try:
                with path.open("rb") as stream:
                    sample = stream.read(8192)
            except OSError as exc:
                raise ChangeReviewError("change_review_file_unreadable") from exc
            binary = b"\0" in sample
        else:
            binary = False
        if binary:
            return True, None, False
        if status == "untracked":
            try:
                with path.open("rb") as stream:
                    raw = stream.read(self._max_patch_bytes + 1)
                input_truncated = len(raw) > self._max_patch_bytes
                bounded = raw[: self._max_patch_bytes]
                text = bounded.decode("utf-8", errors="ignore" if input_truncated else "strict")
                content = text.splitlines(keepends=True)
            except (OSError, UnicodeDecodeError) as exc:
                raise ChangeReviewError("change_review_file_unreadable") from exc
            patch_text = "".join(
                difflib.unified_diff(
                    [],
                    content,
                    fromfile="/dev/null",
                    tofile=f"b/{relative}",
                )
            )
            patch, patch_truncated = self._bounded_patch(patch_text.encode("utf-8"))
            return False, patch, input_truncated or patch_truncated
        try:
            with tempfile.TemporaryDirectory(prefix="corvus-diff-") as directory:
                output = Path(directory) / "patch.diff"
                result = self.git.run(
                    root,
                    (
                        "diff",
                        f"--output={output}",
                        "--no-ext-diff",
                        "--binary",
                        "--unified=3",
                        "HEAD",
                        "--",
                        relative,
                    ),
                )
                if result.returncode != 0:
                    raise ChangeReviewError("change_review_diff_failed")
                with output.open("rb") as stream:
                    raw = stream.read(self._max_patch_bytes + 1)
        except OSError as exc:
            raise ChangeReviewError("change_review_diff_failed") from exc
        if b"GIT binary patch" in raw or b"Binary files " in raw:
            return True, None, False
        patch, truncated = self._bounded_patch(raw)
        return False, patch, truncated

    def _content_identity(
        self,
        root: Path,
        status: Literal["added", "modified", "deleted", "renamed", "untracked"],
        relative: str,
    ) -> str:
        if status == "deleted":
            result = self.git.run(root, ("rev-parse", "--verify", f"HEAD:{relative}"))
            if result.returncode != 0:
                raise ChangeReviewError("change_review_deleted_blob_invalid")
            value = result.stdout.decode("ascii", errors="strict").strip()
            if len(value) not in {40, 64} or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ChangeReviewError("change_review_deleted_blob_invalid")
            return f"git:{value}"
        target = root.joinpath(*PurePosixPath(relative).parts)
        if not target.is_file() or path_is_link_or_reparse(target):
            raise ChangeReviewError("change_review_file_unreadable")
        digest = hashlib.sha256()
        try:
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ChangeReviewError("change_review_file_unreadable") from exc
        return f"sha256:{digest.hexdigest()}"

    def _bounded_patch(self, value: bytes) -> tuple[str, bool]:
        truncated = len(value) > self._max_patch_bytes
        bounded = value[: self._max_patch_bytes]
        return bounded.decode("utf-8", errors="ignore"), truncated

    @staticmethod
    def _parse_status(
        value: bytes,
    ) -> list[
        tuple[
            Literal["added", "modified", "deleted", "renamed", "untracked"],
            str,
            str | None,
        ]
    ]:
        try:
            tokens = value.decode("utf-8", errors="strict").split("\0")
        except UnicodeDecodeError as exc:
            raise ChangeReviewError("change_review_status_invalid") from exc
        entries: list[
            tuple[
                Literal["added", "modified", "deleted", "renamed", "untracked"],
                str,
                str | None,
            ]
        ] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == "":
                continue
            if len(token) < 4 or token[2] != " ":
                raise ChangeReviewError("change_review_status_invalid")
            code = token[:2]
            path = token[3:]
            previous_path = None
            if "R" in code or "C" in code:
                if index >= len(tokens) or not tokens[index]:
                    raise ChangeReviewError("change_review_status_invalid")
                previous_path = tokens[index]
                index += 1
                status: Literal["added", "modified", "deleted", "renamed", "untracked"] = "renamed"
            elif code == "??":
                status = "untracked"
            elif "D" in code:
                status = "deleted"
            elif "A" in code:
                status = "added"
            else:
                status = "modified"
            entries.append((status, path, previous_path))
        return entries

    @staticmethod
    def _root(worktree: Path) -> Path:
        try:
            root = worktree.resolve(strict=True)
        except OSError as exc:
            raise ChangeReviewError("change_review_worktree_invalid") from exc
        if not root.is_dir() or path_is_link_or_reparse(root):
            raise ChangeReviewError("change_review_worktree_invalid")
        return root

    @staticmethod
    def _relative_path(root: Path, value: str, *, allow_missing: bool) -> str:
        if not value or "\0" in value or "\\" in value:
            raise ChangeReviewError("change_review_path_invalid")
        pure = PurePosixPath(value)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ChangeReviewError("change_review_path_invalid")
        target = root.joinpath(*pure.parts)
        if target.exists():
            if path_is_link_or_reparse(target):
                raise ChangeReviewError("change_review_path_invalid")
            try:
                if not target.resolve(strict=True).is_relative_to(root):
                    raise ChangeReviewError("change_review_path_invalid")
            except OSError as exc:
                raise ChangeReviewError("change_review_path_invalid") from exc
        elif not allow_missing:
            raise ChangeReviewError("change_review_path_invalid")
        return pure.as_posix()
