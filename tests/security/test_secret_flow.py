from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.models import RunPhase
from corvus.security import SecretRedactor
from corvus.store import TraceStore


def test_bounded_text_redacts_before_truncation_and_records_safe_metadata() -> None:
    canary = "corvus-canary-value-5831"
    redactor = SecretRedactor([canary])
    source = ("A" * 20) + canary + ("B" * 100)
    fully_redacted = redactor.redact(source)

    result = redactor.bound_text(source, max_characters=40)

    assert result.truncated is True
    assert result.text.endswith("[TRUNCATED]")
    assert len(result.text) <= 40
    assert canary not in result.text
    assert result.original_chars == len(fully_redacted)
    assert result.original_bytes == len(fully_redacted.encode("utf-8"))
    assert result.original_sha256 == hashlib.sha256(fully_redacted.encode("utf-8")).hexdigest()
    assert result.captured_chars == len(result.text)
    assert result.captured_bytes == len(result.text.encode("utf-8"))
    assert result.captured_sha256 == hashlib.sha256(result.text.encode("utf-8")).hexdigest()


def test_bounded_text_preserves_short_redacted_text() -> None:
    redactor = SecretRedactor(["registered-canary"])

    result = redactor.bound_text("value=registered-canary", max_characters=100)

    assert result.text == "value=[REDACTED]"
    assert result.truncated is False
    assert result.original_sha256 == result.captured_sha256


def test_bounded_text_requires_positive_character_limit() -> None:
    with pytest.raises(ValueError, match="max_characters must be positive"):
        SecretRedactor().bound_text("hello", max_characters=0)


def test_trace_store_redacts_structured_secret_fields_before_serialization(
    tmp_path: Path,
) -> None:
    canary = "corvus-canary-value-3378"
    store = TraceStore(tmp_path / "corvus.db", redactor=SecretRedactor([canary]))
    run_id = uuid4()

    event = store.append(
        run_id,
        "security.canary",
        RunPhase.UNDERSTAND,
        {
            "Authorization": f"Bearer {canary}",
            "nested": [{"session-cookie": canary}],
            "safe": "keyring_service",
        },
    )

    assert event.payload == {
        "Authorization": "[REDACTED]",
        "nested": [{"session-cookie": "[REDACTED]"}],
        "safe": "keyring_service",
    }
    persisted = list(store.events(run_id))[0]
    assert persisted.payload == event.payload
    store.engine.dispose()
