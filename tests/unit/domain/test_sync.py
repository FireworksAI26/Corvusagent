from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID

import pytest
from pydantic import ValidationError

from corvus.domain.sync import SyncMutation
from corvus.security import SecurityError, canonical_json_bytes, reject_sensitive_payload

_ACCOUNT_ID = UUID("00000000-0000-4000-8000-000000000101")


def test_sync_mutation_accepts_only_the_two_typed_profile_commands() -> None:
    account = SyncMutation.model_validate(
        {
            "idempotency_key": "account-profile-1",
            "kind": "account_profile",
            "operation": "set_experience",
            "entity_id": str(_ACCOUNT_ID),
            "expected_version": 1,
            "payload": {"experience_kind": "developer"},
        }
    )
    workspace = SyncMutation.model_validate(
        {
            "idempotency_key": "workspace-profile-1",
            "kind": "workspace_profile",
            "operation": "update",
            "entity_id": str(_ACCOUNT_ID),
            "expected_version": 2,
            "payload": {"name": "Corvus", "workspace_kind": "team"},
        }
    )

    assert account.payload.model_dump(mode="json") == {"experience_kind": "developer"}
    assert workspace.payload.model_dump(mode="json", exclude_none=True) == {
        "name": "Corvus",
        "workspace_kind": "team",
    }

    invalid = (
        {**account.model_dump(mode="json"), "kind": "thread"},
        {**account.model_dump(mode="json"), "operation": "replace"},
        {**account.model_dump(mode="json"), "payload": {"experience_kind": "everyday", "x": 1}},
        {**workspace.model_dump(mode="json"), "payload": {}},
    )
    for payload in invalid:
        with pytest.raises(ValidationError):
            SyncMutation.model_validate(payload)


def test_sync_mutation_bounds_keys_versions_and_payload_text() -> None:
    base = {
        "idempotency_key": "key",
        "kind": "workspace_profile",
        "operation": "update",
        "entity_id": str(_ACCOUNT_ID),
        "expected_version": 1,
        "payload": {"name": "Corvus"},
    }

    for payload in (
        {**base, "idempotency_key": ""},
        {**base, "idempotency_key": "x" * 201},
        {**base, "expected_version": 0},
        {**base, "payload": {"name": " "}},
        {**base, "payload": {"name": "x" * 201}},
    ):
        with pytest.raises(ValidationError):
            SyncMutation.model_validate(payload)


def test_recursive_sensitive_payload_rejection_fails_closed() -> None:
    cycle: list[object] = []
    cycle.append(cycle)

    rejected = (
        {"nested": [{"refresh_token": "canary"}]},
        {"nested": ["Authorization: Bearer abcdefghijklmnop"]},
        {"value": math.inf},
        {"value": object()},
        cycle,
    )
    for value in rejected:
        with pytest.raises(SecurityError):
            reject_sensitive_payload(value)

    reject_sensitive_payload({"token_count": 12, "name": "ordinary"})


class _Kind(StrEnum):
    VALUE = "value"


class _SecretKind(StrEnum):
    VALUE = "Authorization: Bearer abcdefghijklmnop"


def test_canonical_json_normalizes_supported_protocol_types_stably() -> None:
    timestamp = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    left = {"z": _Kind.VALUE, "id": _ACCOUNT_ID, "when": timestamp}
    right = {"when": timestamp, "id": _ACCOUNT_ID, "z": "value"}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes({"when": timestamp}) == canonical_json_bytes(
        {"when": timestamp.astimezone(timezone(timedelta(hours=8)))}
    )
    assert canonical_json_bytes(left) == (
        b'{"id":"00000000-0000-4000-8000-000000000101",'
        b'"when":"2026-07-16T12:00:00+00:00","z":"value"}'
    )


def test_recursive_sensitive_payload_rejection_inspects_enum_values() -> None:
    with pytest.raises(SecurityError, match="sensitive payload value rejected"):
        reject_sensitive_payload({"kind": _SecretKind.VALUE})
