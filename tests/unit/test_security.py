from pathlib import Path

import pytest

from corvus.security import (
    SecretRedactor,
    SecurityError,
    is_sensitive_field_name,
    resolve_under,
)


def test_secret_redactor_removes_registered_keyed_and_bare_secret_values() -> None:
    canary = "corvus-unit-secret-canary"
    redactor = SecretRedactor([canary])

    redacted = redactor.redact_value(
        {
            "api_key": "sk-1234567890abcdef",
            "message": canary,
            "nested": [{"message": "Bearer abcdefghijklmnop"}],
        }
    )

    assert redacted == {
        "api_key": "[REDACTED]",
        "message": "[REDACTED]",
        "nested": [{"message": "[REDACTED]"}],
    }


def test_secret_redactor_removes_unquoted_multiword_credentials() -> None:
    redacted = SecretRedactor().redact("passphrase: my secret passphrase\nstatus: ready")

    assert redacted == "passphrase=[REDACTED]\nstatus: ready"


def test_secret_redactor_removes_quoted_json_style_credentials() -> None:
    redacted = SecretRedactor().redact(
        '{"password":"hunter2","authorization":"Basic abcdefghijklmnop",'
        '"cookie":"session=secret-value"}'
    )

    assert "hunter2" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "secret-value" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_sensitive_field_classification_preserves_token_usage_counters() -> None:
    assert not is_sensitive_field_name("input_tokens")
    assert not is_sensitive_field_name("max_output_tokens")
    assert not is_sensitive_field_name("tokens_used")
    assert not is_sensitive_field_name("prompt_tokens_details")
    assert not is_sensitive_field_name("completion_tokens_details")
    assert not is_sensitive_field_name("cached_tokens")
    assert not is_sensitive_field_name("audio_tokens")
    assert not is_sensitive_field_name("accepted_prediction_tokens")
    assert not is_sensitive_field_name("rejected_prediction_tokens")
    assert not is_sensitive_field_name("promptTokenCount")
    assert not is_sensitive_field_name("candidatesTokenCount")
    assert not is_sensitive_field_name("totalTokenCount")
    assert not is_sensitive_field_name("cachedContentTokenCount")
    assert not is_sensitive_field_name("thoughtsTokenCount")
    assert not is_sensitive_field_name("toolUsePromptTokenCount")
    assert is_sensitive_field_name("tokens")
    assert is_sensitive_field_name("access_token")
    assert is_sensitive_field_name("signing_key")


def test_resolve_under_rejects_absolute_and_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    with pytest.raises(SecurityError, match="relative"):
        resolve_under(root, str((tmp_path / "outside.txt").resolve()))
    with pytest.raises(SecurityError, match="traversal"):
        resolve_under(root, "../outside.txt")
