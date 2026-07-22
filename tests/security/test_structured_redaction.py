from __future__ import annotations

import base64
import json

import pytest

from corvus.security import SecretRedactor, SecurityError


def test_structured_redaction_masks_secret_keys_and_registered_encodings() -> None:
    canary = "corvus-canary-value-9274"
    encoded = base64.b64encode(canary.encode("utf-8")).decode("ascii")
    hexadecimal = canary.encode("utf-8").hex()
    redactor = SecretRedactor([canary])
    value = {
        "API-Key": canary,
        "keyring_service": "corvus-keyring",
        "nested": (
            {"refresh_token": "nested-token"},
            {"note": f"plain={canary} b64={encoded} hex={hexadecimal}"},
        ),
        "session-cookie": "cookie-value",
        "values": {"ordinary", canary},
    }

    redacted = redactor.redact_value(value)
    serialized = redactor.redact_json(value)

    expected_mask = redactor.redact(canary)
    assert redacted["API-Key"] == expected_mask
    assert redacted["session-cookie"] == expected_mask
    assert redacted["nested"][0]["refresh_token"] == expected_mask
    assert redacted["keyring_service"] == "corvus-keyring"
    assert redacted["values"] == [expected_mask, "ordinary"]
    assert json.loads(serialized) == redacted
    assert canary not in serialized
    assert encoded not in serialized
    assert hexadecimal not in serialized


def test_structured_redaction_is_deterministic_and_json_safe() -> None:
    redactor = SecretRedactor()
    left = {"set": {"z", "a"}, "tuple": (1, True, None)}
    right = {"tuple": (1, True, None), "set": {"a", "z"}}

    assert redactor.redact_json(left) == redactor.redact_json(right)
    assert redactor.redact_value(left) == {
        "set": ["a", "z"],
        "tuple": [1, True, None],
    }


def test_structured_redaction_rejects_cycles_fail_closed() -> None:
    redactor = SecretRedactor()
    cycle: list[object] = []
    cycle.append(cycle)

    with pytest.raises(SecurityError, match="cyclic"):
        redactor.redact_value(cycle)


def test_registered_only_redaction_preserves_ordinary_source_patterns() -> None:
    canary = "corvus-canary-value-7753"
    redactor = SecretRedactor([canary])
    source = "token = os.environ['TOKEN']\nvalue = '" + canary + "'"

    redacted = redactor.redact_registered(source)

    assert "token = os.environ['TOKEN']" in redacted
    assert canary not in redacted
    assert "[REDACTED]" in redacted
