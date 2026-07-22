from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

_SENSITIVE_FIELD_TOKENS = (
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "signingkey",
    "passphrase",
)
_SAFE_TOKEN_COUNTER_FIELDS = frozenset(
    {
        "acceptedpredictiontokens",
        "audiotokens",
        "cachedtokens",
        "cachedinputtokens",
        "cachedcontenttokencount",
        "candidatestokencount",
        "completiontokensdetails",
        "completiontokens",
        "inputtokens",
        "maxoutputtokens",
        "outputtokens",
        "prompttokensdetails",
        "prompttokens",
        "prompttokencount",
        "reasoningtokens",
        "rejectedpredictiontokens",
        "thoughtstokencount",
        "tokencount",
        "totaltokens",
        "totaltokencount",
        "tooluseprompttokencount",
        "tokensused",
    }
)


def is_sensitive_field_name(key: str) -> bool:
    """Return whether a structured field name may carry sensitive data."""

    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if normalized in _SAFE_TOKEN_COUNTER_FIELDS:
        return False
    return any(token in normalized for token in _SENSITIVE_FIELD_TOKENS)


def _normalize_json_value(value: Any, *, active: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SecurityError("non-finite numbers are not JSON-safe")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise SecurityError("naive datetimes are not canonical")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return _normalize_json_value(value.value, active=active)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise SecurityError("cyclic structured value rejected")
        active.add(identity)
        try:
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise SecurityError("JSON object keys must be strings")
                normalized[key] = _normalize_json_value(item, active=active)
            return normalized
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise SecurityError("cyclic structured value rejected")
        active.add(identity)
        try:
            return [_normalize_json_value(item, active=active) for item in value]
        finally:
            active.remove(identity)
    raise SecurityError(f"unsupported structured value type: {type(value).__name__}")


def reject_sensitive_payload(value: Any) -> None:
    """Reject secret-bearing or non-canonical client state before persistence."""

    redactor = SecretRedactor()
    active: set[int] = set()

    def inspect(item: Any) -> None:
        if isinstance(item, Enum):
            inspect(item.value)
            return
        if item is None or isinstance(item, (bool, int, float, UUID, datetime)):
            _normalize_json_value(item, active=set())
            return
        if isinstance(item, str):
            if redactor.redact(item) != item:
                raise SecurityError("sensitive payload value rejected")
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise SecurityError("cyclic structured value rejected")
            active.add(identity)
            try:
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise SecurityError("JSON object keys must be strings")
                    if is_sensitive_field_name(key):
                        raise SecurityError("sensitive payload field rejected")
                    inspect(nested)
            finally:
                active.remove(identity)
            return
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active:
                raise SecurityError("cyclic structured value rejected")
            active.add(identity)
            try:
                for nested in item:
                    inspect(nested)
            finally:
                active.remove(identity)
            return
        raise SecurityError(f"unsupported structured value type: {type(item).__name__}")

    inspect(value)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode supported protocol values using Corvus canonical JSON."""

    normalized = _normalize_json_value(value, active=set())
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class SecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundedRedactedText:
    text: str
    truncated: bool
    original_sha256: str
    captured_sha256: str
    original_bytes: int
    original_chars: int
    captured_bytes: int
    captured_chars: int


class SecretRedactor:
    _TOKEN_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"(?i)['\"](authorization)['\"]\s*[:=]\s*(?!['\"]?\[REDACTED\])"
            r"(['\"][^\r\n'\"]*(?:['\"]|$)|[^\r\n'\"]+)"
        ),
        re.compile(
            r"(?i)(authorization)\s*[:=]\s*(?!['\"]?\[REDACTED\])"
            r"(['\"][^\r\n'\"]*(?:['\"]|$)|[^\r\n'\"]+)"
        ),
        re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(
            r"(?i)['\"](api[_-]?key|token|secret|password|cookie|credential|passphrase|"
            r"private[_-]?key|signing[_-]?key)['\"]\s*[:=]\s*(?!['\"]?\[REDACTED\])"
            r"(['\"][^\r\n'\"]*(?:['\"]|$)|[^\r\n'\"]+)"
        ),
        re.compile(
            r"(?i)(api[_-]?key|token|secret|password|cookie|credential|"
            r"passphrase|private[_-]?key|signing[_-]?key)\s*[:=]\s*"
            r"(?!['\"]?\[REDACTED\])"
            r"(['\"][^\r\n'\"]*(?:['\"]|$)|[^\r\n'\"]+)"
        ),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    ]

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets: set[str] = set()
        for secret in secrets or []:
            self.register(secret)

    def register(self, secret: str) -> None:
        if len(secret) < 4:
            return
        self._secrets.add(secret)
        self._secrets.add(base64.b64encode(secret.encode()).decode())
        self._secrets.add(secret.encode().hex())

    def redact_registered(self, text: str) -> str:
        result = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            result = result.replace(secret, "[REDACTED]")
        return result

    def redact(self, text: str) -> str:
        result = self.redact_registered(text)
        for pattern in self._TOKEN_PATTERNS:
            if pattern.groups >= 2:
                result = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", result)
            else:
                result = pattern.sub("[REDACTED]", result)
        return result

    @classmethod
    def _is_secret_key(cls, key: str) -> bool:
        return is_sensitive_field_name(key)

    def redact_value(self, value: Any) -> Any:
        return self._redact_value(value, active=set())

    def _redact_value(self, value: Any, *, active: set[int]) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise SecurityError("non-finite numbers are not JSON-safe")
            return value
        if isinstance(value, str):
            return self.redact(value)

        if isinstance(value, Mapping):
            identity = id(value)
            if identity in active:
                raise SecurityError("cyclic structured value rejected")
            active.add(identity)
            try:
                result: dict[str, Any] = {}
                for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                    key = self.redact(str(raw_key))
                    if key in result:
                        raise SecurityError("mapping keys collide after JSON normalization")
                    result[key] = (
                        "[REDACTED]"
                        if self._is_secret_key(str(raw_key))
                        else self._redact_value(item, active=active)
                    )
                return result
            finally:
                active.remove(identity)

        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in active:
                raise SecurityError("cyclic structured value rejected")
            active.add(identity)
            try:
                return [self._redact_value(item, active=active) for item in value]
            finally:
                active.remove(identity)

        if isinstance(value, (set, frozenset)):
            identity = id(value)
            if identity in active:
                raise SecurityError("cyclic structured value rejected")
            active.add(identity)
            try:
                items = [self._redact_value(item, active=active) for item in value]
                return sorted(
                    items,
                    key=lambda item: json.dumps(
                        item,
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            finally:
                active.remove(identity)

        raise SecurityError(f"unsupported structured value type: {type(value).__name__}")

    def redact_json(self, value: Any) -> str:
        return json.dumps(
            self.redact_value(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def streaming_buffer_characters(self, *, maximum: int | None = None) -> int:
        """Return bounded lookbehind needed to redact secrets split across chunks."""

        if maximum is not None and maximum <= 0:
            raise ValueError("maximum must be positive")
        longest_registered = max((len(secret) for secret in self._secrets), default=0)
        # Keep full registered values available until they can be redacted. The
        # caller's aggregate input bound caps this buffer even for a malformed
        # configuration containing an unusually long registered secret.
        required = max(512, longest_registered + 64)
        return required if maximum is None else min(required, maximum)

    def streaming_safe_prefix_length(self, text: str, *, maximum_buffer: int) -> int:
        """Return a safe-to-redact prefix length without leaking partial tokens."""

        if maximum_buffer <= 0:
            raise ValueError("maximum_buffer must be positive")
        cut = len(text) - self.streaming_buffer_characters(maximum=maximum_buffer)
        if cut <= 0:
            return 0
        # A token assignment ending at the chunk boundary can continue in the
        # next chunk. Retain it even when its key precedes the normal suffix.
        for pattern in self._TOKEN_PATTERNS:
            for match in pattern.finditer(text):
                if match.end() == len(text):
                    cut = min(cut, match.start())
        return max(cut, 0)

    def bound_text(self, text: str, *, max_characters: int) -> BoundedRedactedText:
        if max_characters <= 0:
            raise ValueError("max_characters must be positive")
        redacted = self.redact(text)
        truncated = len(redacted) > max_characters
        if truncated:
            full_marker = "[TRUNCATED]"
            marker = full_marker if max_characters >= len(full_marker) else "…"
            captured = redacted[: max_characters - len(marker)] + marker
        else:
            captured = redacted
        original_bytes = redacted.encode("utf-8")
        captured_bytes = captured.encode("utf-8")
        return BoundedRedactedText(
            text=captured,
            truncated=truncated,
            original_sha256=hashlib.sha256(original_bytes).hexdigest(),
            captured_sha256=hashlib.sha256(captured_bytes).hexdigest(),
            original_bytes=len(original_bytes),
            original_chars=len(redacted),
            captured_bytes=len(captured_bytes),
            captured_chars=len(captured),
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def resolve_under(root: Path, relative: str, *, allow_missing_leaf: bool = True) -> Path:
    if not relative or Path(relative).is_absolute():
        raise SecurityError("path must be a non-empty relative path")
    normalized = Path(relative)
    if any(part in {"..", ""} for part in normalized.parts):
        raise SecurityError("path traversal is forbidden")
    root = root.resolve(strict=True)
    cursor = root
    missing_prefix = False
    for part in normalized.parts:
        cursor = cursor / part
        if not missing_prefix and (cursor.exists() or cursor.is_symlink()):
            if _is_link_or_reparse(cursor):
                raise SecurityError(f"link or reparse-point escape rejected: {relative}")
        elif not allow_missing_leaf:
            raise SecurityError(f"path component does not exist: {part}")
        else:
            missing_prefix = True
    try:
        cursor.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise SecurityError("path escapes approved root") from exc
    return cursor


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.corvus-{os.getpid()}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
