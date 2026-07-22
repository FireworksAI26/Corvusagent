from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from corvus.mvp.models import MvpModel
from corvus.safe_process import path_is_link_or_reparse

_SCANNER_VERSION = "corvus-secrets-v1"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_KNOWN_PATTERNS = (
    ("github_token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


class SecretScanError(RuntimeError):
    pass


class SecretFinding(MvpModel):
    path: str
    line: int | None
    kind: str
    severity: Literal["warning", "blocked"]


class SecretScanResult(MvpModel):
    status: Literal["not_scanned", "passed", "warning", "blocked"]
    scanner_version: str
    scanned_paths: tuple[str, ...]
    findings: tuple[SecretFinding, ...]
    completed_at: datetime | None
    digest: str | None


class SecretScanner:
    def not_scanned(self, paths: tuple[str, ...]) -> SecretScanResult:
        normalized = tuple(self._path_shape(path) for path in paths)
        return SecretScanResult(
            status="not_scanned",
            scanner_version=_SCANNER_VERSION,
            scanned_paths=normalized,
            findings=(),
            completed_at=None,
            digest=None,
        )

    def scan(
        self,
        worktree: Path,
        paths: tuple[str, ...],
        *,
        deleted_paths: tuple[str, ...] = (),
        deleted_contents: dict[str, bytes] | None = None,
    ) -> SecretScanResult:
        root = self._root(worktree)
        deleted = {self._path_shape(path) for path in deleted_paths}
        deleted_blobs = deleted_contents or {}
        normalized_paths: list[str] = []
        findings: list[SecretFinding] = []
        content_digests: dict[str, str] = {}
        for requested in dict.fromkeys(paths):
            relative = self._path_shape(requested)
            if ".git" in PurePosixPath(relative).parts:
                continue
            target = root.joinpath(*PurePosixPath(relative).parts)
            if path_is_link_or_reparse(target):
                raise SecretScanError("secret_scan_path_link_forbidden")
            if not target.exists():
                if relative not in deleted:
                    raise SecretScanError("secret_scan_path_invalid")
                try:
                    canonical_parent = target.parent.resolve(strict=True)
                except OSError as exc:
                    raise SecretScanError("secret_scan_path_invalid") from exc
                if not canonical_parent.is_relative_to(root):
                    raise SecretScanError("secret_scan_path_invalid")
                content = deleted_blobs.get(relative)
                if content is None:
                    raise SecretScanError("secret_scan_deleted_content_required")
                normalized_paths.append(relative)
                content_digests[relative] = hashlib.sha256(content).hexdigest()
                findings.extend(self._scan_content(relative, content))
                continue
            try:
                canonical = target.resolve(strict=True)
            except OSError as exc:
                raise SecretScanError("secret_scan_path_invalid") from exc
            if not canonical.is_relative_to(root) or not canonical.is_file():
                raise SecretScanError("secret_scan_path_invalid")
            normalized_paths.append(relative)
            try:
                content_digest, content = self._read_bounded(canonical)
            except OSError as exc:
                raise SecretScanError("secret_scan_path_invalid") from exc
            content_digests[relative] = content_digest
            findings.extend(self._scan_content(relative, content))
        status: Literal["passed", "warning", "blocked"]
        if any(finding.severity == "blocked" for finding in findings):
            status = "blocked"
        elif findings:
            status = "warning"
        else:
            status = "passed"
        completed_at = datetime.now(UTC)
        payload = {
            "scanner_version": _SCANNER_VERSION,
            "paths": normalized_paths,
            "content_digests": content_digests,
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "completed_at": completed_at.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return SecretScanResult(
            status=status,
            scanner_version=_SCANNER_VERSION,
            scanned_paths=tuple(normalized_paths),
            findings=tuple(findings),
            completed_at=completed_at,
            digest=digest,
        )

    @staticmethod
    def _read_bounded(path: Path) -> tuple[str, bytes]:
        digest = hashlib.sha256()
        sample = bytearray()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                remaining = _MAX_FILE_BYTES + 1 - len(sample)
                if remaining > 0:
                    sample.extend(chunk[:remaining])
        return digest.hexdigest(), bytes(sample)

    @staticmethod
    def _scan_content(path: str, content: bytes) -> list[SecretFinding]:
        if len(content) > _MAX_FILE_BYTES:
            return [
                SecretFinding(
                    path=path,
                    line=None,
                    kind="large_file_not_scanned",
                    severity="warning",
                )
            ]
        if b"\0" in content:
            return [
                SecretFinding(
                    path=path,
                    line=None,
                    kind="binary_not_scanned",
                    severity="warning",
                )
            ]
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return [
                SecretFinding(
                    path=path,
                    line=None,
                    kind="non_utf8_not_scanned",
                    severity="warning",
                )
            ]
        return SecretScanner._scan_text(path, text)

    @staticmethod
    def _scan_text(path: str, text: str) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            known_match = False
            for kind, pattern in _KNOWN_PATTERNS:
                if pattern.search(line) is not None:
                    findings.append(
                        SecretFinding(
                            path=path,
                            line=line_number,
                            kind=kind,
                            severity="blocked",
                        )
                    )
                    known_match = True
            if known_match:
                continue
            if any(SecretScanner._entropy(token) >= 4.3 for token in _ENTROPY_TOKEN.findall(line)):
                findings.append(
                    SecretFinding(
                        path=path,
                        line=line_number,
                        kind="high_entropy_value",
                        severity="warning",
                    )
                )
        return findings

    @staticmethod
    def _entropy(value: str) -> float:
        counts = Counter(value)
        length = len(value)
        return -sum((count / length) * math.log2(count / length) for count in counts.values())

    @staticmethod
    def _root(worktree: Path) -> Path:
        try:
            root = worktree.resolve(strict=True)
        except OSError as exc:
            raise SecretScanError("secret_scan_worktree_invalid") from exc
        if not root.is_dir() or path_is_link_or_reparse(root):
            raise SecretScanError("secret_scan_worktree_invalid")
        return root

    @staticmethod
    def _path_shape(value: str) -> str:
        if not value or "\0" in value or "\\" in value:
            raise SecretScanError("secret_scan_path_invalid")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SecretScanError("secret_scan_path_invalid")
        return path.as_posix()
