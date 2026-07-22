from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain import agent_runtime
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunEventChainError,
    AgentRunEventType,
    AgentRunRequest,
    AutonomyGrant,
    AutonomyProfile,
    CapabilitySupport,
    ExecutableIdentity,
    ProviderBinding,
    ProviderFamily,
    ProviderStatus,
    ProviderTransport,
    capability_enabled,
    compute_agent_run_event_digest,
    compute_agent_run_immutable_digest,
    compute_agent_run_request_digest,
    compute_agent_run_runtime_limit_digest,
    compute_autonomy_grant_digest,
    compute_provider_binding_digest,
    validate_agent_run_event_chain,
)
from corvus.security import SecretRedactor

_DIGEST = "a" * 64
_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _capabilities() -> AgentCapabilities:
    return AgentCapabilities()


def _executable(tmp_path: Path) -> ExecutableIdentity:
    return ExecutableIdentity(
        executable_path=(tmp_path / "codex.exe").resolve(),
        version="1.2.3",
        sha256_digest=_DIGEST,
    )


def _binding(tmp_path: Path, **updates: object) -> ProviderBinding:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "family": ProviderFamily.CODEX,
        "transport": ProviderTransport.LOCAL_CLI,
        "status": ProviderStatus.AVAILABLE,
        "executable_identity": _executable(tmp_path),
        "model": "gpt-5.6-sol",
        "capabilities": _capabilities(),
        "health_checked_at": datetime(2026, 7, 15, tzinfo=UTC),
        "version": 1,
        "data_egress_disclosure": "Prompts leave the local process.",
        "server_storage_disclosure": "Provider retention policy applies.",
    }
    values.update(updates)
    return ProviderBinding(**values)


def _grant(tmp_path: Path, **updates: object) -> AutonomyGrant:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "profile": AutonomyProfile.REVIEW_FIRST,
        "allowed_roots": (tmp_path.resolve(),),
        "allowed_effect_classes": frozenset({"repository.read"}),
        "denied_effect_classes": frozenset({"shell.execute"}),
        "allowed_sandbox_profiles": frozenset({"workspace-write"}),
        "allowed_tool_ids": frozenset({"repository.search"}),
        "allowed_network_destinations": ("api.openai.com:443",),
        "credential_grant_ids": (uuid4(),),
        "wall_clock_deadline": _FUTURE,
        "provider_spend_ceiling": 5,
        "corvus_budget_ceiling": 10,
        "max_turns": 8,
        "max_output_tokens": 4000,
        "max_output_bytes": 100_000,
        "max_retries": 2,
        "approval_ceiling": 1,
        "always_block_effects": frozenset({"authority.bypass"}),
        "notification_policy": "notify_on_approval",
        "summary_policy": "final_summary_required",
        "issuer_principal_id": uuid4(),
        "issued_at": datetime(2026, 7, 15, tzinfo=UTC),
        "expires_at": _FUTURE,
        "policy_digest": _DIGEST,
    }
    values.update(updates)
    return AutonomyGrant(**values)


def _request(**updates: object) -> AgentRunRequest:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "workflow_id": uuid4(),
        "work_item_id": uuid4(),
        "provider_binding_id": uuid4(),
        "provider_binding_version": 1,
        "provider_binding_digest": "6" * 64,
        "model": "gpt-5.6-sol",
        "effort": "high",
        "prompt": "Review the repository.",
        "untrusted_context_ref_ids": (uuid4(),),
        "authorization_proof_id": uuid4(),
        "authorization_proof_digest": "1" * 64,
        "autonomy_grant_id": uuid4(),
        "autonomy_grant_digest": "2" * 64,
        "credential_grant_ids": (uuid4(),),
        "credential_proof_id": uuid4(),
        "credential_proof_digest": "3" * 64,
        "budget_proof_id": uuid4(),
        "budget_proof_digest": "4" * 64,
        "kill_switch_proof_id": uuid4(),
        "kill_switch_proof_digest": "5" * 64,
        "sandbox_profile": "workspace-write",
        "filesystem_envelope": ("repository.read",),
        "network_envelope": ("api.openai.com:443",),
        "tool_envelope": ("repository.search",),
        "requested_effect_classes": frozenset({"repository.read"}),
        "provider_spend_limit": 5,
        "corvus_budget_limit": 10,
        "budget_unit": "usd_micros",
        "budget_requested_amount": 1,
        "approval_limit": 1,
        "max_retries": 2,
        "max_turns": 8,
        "deadline": _FUTURE,
        "max_output_tokens": 4000,
        "max_output_bytes": 100_000,
        "idempotency_key": "run:001",
    }
    values.update(updates)
    return AgentRunRequest(**values)


def _event(**updates: object) -> AgentRunEvent:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "handle_id": uuid4(),
        "sequence": 1,
        "timestamp": datetime(2026, 7, 15, tzinfo=UTC),
        "event_type": AgentRunEventType.STARTED,
        "redacted_payload": {"message": "started"},
        "previous_event_digest": "0" * 64,
    }
    values.update(updates)
    values["event_digest"] = compute_agent_run_event_digest(
        run_id=values["run_id"],
        handle_id=values["handle_id"],
        sequence=values["sequence"],
        timestamp=values["timestamp"],
        event_type=values["event_type"],
        redacted_payload=values["redacted_payload"],
        provider_event_id=values.get("provider_event_id"),
        previous_event_digest=values["previous_event_digest"],
        tool_call_id=values.get("tool_call_id"),
        effect_authorization_decision_id=values.get("effect_authorization_decision_id"),
        effect_authorization_decision_digest=values.get("effect_authorization_decision_digest"),
    )
    return AgentRunEvent(**values)


def test_agent_run_runtime_limit_digest_contract_is_public() -> None:
    digest_function = getattr(agent_runtime, "compute_agent_run_runtime_limit_digest", None)

    assert digest_function is not None


def test_autonomy_grant_digest_serialization_sorts_frozensets(tmp_path: Path) -> None:
    allowed_effects = frozenset(f"repository.read.{index}" for index in range(16))
    denied_effects = frozenset(f"shell.execute.{index}" for index in range(16))
    sandbox_profiles = frozenset(f"sandbox.{index}" for index in range(16))
    tool_ids = frozenset(f"repository.search.{index}" for index in range(16))
    blocked_effects = frozenset(f"authority.bypass.{index}" for index in range(16))
    grant = _grant(
        tmp_path,
        allowed_effect_classes=allowed_effects,
        denied_effect_classes=denied_effects,
        allowed_sandbox_profiles=sandbox_profiles,
        allowed_tool_ids=tool_ids,
        always_block_effects=blocked_effects,
    )

    payload = grant.model_dump(mode="json")
    python_payload = grant.model_dump()

    assert payload["allowed_effect_classes"] == sorted(allowed_effects)
    assert payload["denied_effect_classes"] == sorted(denied_effects)
    assert payload["allowed_sandbox_profiles"] == sorted(sandbox_profiles)
    assert payload["allowed_tool_ids"] == sorted(tool_ids)
    assert payload["always_block_effects"] == sorted(blocked_effects)
    assert python_payload["allowed_effect_classes"] == allowed_effects


def test_autonomy_grant_digest_normalizes_decimal_scale_and_timezone_offset(
    tmp_path: Path,
) -> None:
    grant = _grant(
        tmp_path,
        provider_spend_ceiling=Decimal("5.0"),
        corvus_budget_ceiling=Decimal("10.00"),
    )
    offset = timezone(timedelta(hours=8))
    equivalent = grant.model_copy(
        update={
            "provider_spend_ceiling": Decimal("5.000"),
            "corvus_budget_ceiling": Decimal("10.0"),
            "issued_at": grant.issued_at.astimezone(offset),
            "expires_at": grant.expires_at.astimezone(offset),
            "wall_clock_deadline": grant.wall_clock_deadline.astimezone(offset),
        }
    )

    assert grant == equivalent
    assert compute_autonomy_grant_digest(grant) == compute_autonomy_grant_digest(equivalent)


def test_canonical_decimal_keeps_large_exponents_compact() -> None:
    expected = "1E+10000"
    canonical = agent_runtime._canonical_decimal(Decimal(expected))

    assert len(canonical) == len(expected)
    assert canonical == expected


def test_canonical_decimal_is_independent_of_decimal_context() -> None:
    canonical_values: list[str] = []
    for precision in (2, 28, 100):
        with localcontext() as context:
            context.prec = precision
            canonical_values.append(agent_runtime._canonical_decimal(Decimal("1.234")))

    assert len(set(canonical_values)) == 1


def test_canonical_decimal_preserves_digits_beyond_context_precision() -> None:
    with localcontext() as context:
        context.prec = 2
        first = agent_runtime._canonical_decimal(Decimal("1.234"))
        second = agent_runtime._canonical_decimal(Decimal("1.235"))

    assert first != second


def test_agent_run_digests_normalize_decimal_scale_and_timezone_offset() -> None:
    request = _request(
        provider_spend_limit=Decimal("5.0"),
        corvus_budget_limit=Decimal("10.00"),
    )
    equivalent = request.model_copy(
        update={
            "provider_spend_limit": Decimal("5.000"),
            "corvus_budget_limit": Decimal("10.0"),
            "deadline": request.deadline.astimezone(timezone(timedelta(hours=8))),
        }
    )

    assert request == equivalent
    for digest_function in (
        compute_agent_run_request_digest,
        compute_agent_run_runtime_limit_digest,
        compute_agent_run_immutable_digest,
    ):
        assert digest_function(request) == digest_function(equivalent)


def test_agent_run_event_digest_normalizes_timezone_offset() -> None:
    timestamp = datetime(2026, 7, 15, tzinfo=UTC)
    values = {
        "run_id": uuid4(),
        "handle_id": uuid4(),
        "sequence": 1,
        "event_type": AgentRunEventType.STARTED,
        "redacted_payload": {"state": "running"},
        "provider_event_id": None,
        "previous_event_digest": "0" * 64,
    }

    utc_digest = compute_agent_run_event_digest(timestamp=timestamp, **values)
    offset_digest = compute_agent_run_event_digest(
        timestamp=timestamp.astimezone(timezone(timedelta(hours=8))),
        **values,
    )

    assert utc_digest == offset_digest


def test_agent_run_digest_serialization_sorts_requested_effects() -> None:
    requested_effects = frozenset(f"repository.read.{index}" for index in range(16))
    request = _request(requested_effect_classes=requested_effects)

    payload = request.model_dump(mode="json")
    python_payload = request.model_dump()

    assert payload["requested_effect_classes"] == sorted(requested_effects)
    assert python_payload["requested_effect_classes"] == requested_effects


def test_autonomy_grant_requires_a_max_output_byte_ceiling() -> None:
    assert "max_output_bytes" in AutonomyGrant.model_fields


def test_autonomy_and_run_contracts_expose_enforceable_limits(tmp_path: Path) -> None:
    grant = AutonomyGrant.model_validate(
        {
            **_grant(tmp_path).model_dump(),
            "allowed_sandbox_profiles": frozenset({"workspace-write"}),
            "allowed_tool_ids": frozenset({"repository.search"}),
        }
    )
    request = AgentRunRequest.model_validate(
        {
            **_request().model_dump(exclude_computed_fields=True),
            "filesystem_envelope": (str(tmp_path.resolve()),),
            "requested_effect_classes": frozenset({"repository.read"}),
            "provider_spend_limit": 5,
            "corvus_budget_limit": 10,
            "approval_limit": 1,
            "max_retries": 2,
            "max_turns": 8,
        }
    )

    assert grant.allowed_sandbox_profiles == frozenset({"workspace-write"})
    assert grant.allowed_tool_ids == frozenset({"repository.search"})
    assert request.requested_effect_classes == frozenset({"repository.read"})


@pytest.mark.parametrize("proof_kind", ["credential", "budget"])
def test_optional_wrapper_proofs_must_be_paired(proof_kind: str) -> None:
    paired_none = {
        f"{proof_kind}_proof_id": None,
        f"{proof_kind}_proof_digest": None,
    }
    assert _request(**paired_none)

    with pytest.raises(ValidationError) as exc_info:
        _request(**{f"{proof_kind}_proof_id": None})

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        f"agent_run_{proof_kind}_proof_pair_incomplete"
    )


def test_provider_binding_enforces_transport_identity_xor(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as missing:
        _binding(tmp_path, executable_identity=None)
    assert missing.value.errors()[0]["ctx"]["reason_code"] == (
        "local_cli_requires_executable_identity"
    )

    with pytest.raises(ValidationError) as both:
        _binding(tmp_path, credential_ref_id=uuid4())
    assert both.value.errors()[0]["ctx"]["reason_code"] == (
        "local_cli_forbids_credential_reference"
    )

    http_binding = _binding(
        tmp_path,
        transport=ProviderTransport.HTTP_API,
        executable_identity=None,
        credential_ref_id=uuid4(),
    )
    assert http_binding.credential_ref_id is not None


def test_provider_binding_rejects_relative_executable_and_invalid_fallbacks(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="executable_path_must_be_absolute"):
        ExecutableIdentity(
            executable_path=Path("codex.exe"),
            version="1.2.3",
            sha256_digest=_DIGEST,
        )

    noncanonical = tmp_path.resolve() / "child" / ".." / "codex.exe"
    with pytest.raises(ValidationError, match="executable_path_must_be_canonical"):
        ExecutableIdentity(
            executable_path=noncanonical,
            version="1.2.3",
            sha256_digest=_DIGEST,
        )

    binding_id = uuid4()
    with pytest.raises(ValidationError, match="provider_binding_cannot_fallback_to_self"):
        _binding(tmp_path, id=binding_id, fallback_binding_ids=(binding_id,))
    duplicate = uuid4()
    with pytest.raises(ValidationError, match="duplicate_fallback_binding_id"):
        _binding(tmp_path, fallback_binding_ids=(duplicate, duplicate))


def test_text_capability_defaults_supported_and_other_capabilities_stay_unverified() -> None:
    capabilities = AgentCapabilities()

    assert capabilities.text is CapabilitySupport.SUPPORTED
    for field_name in AgentCapabilities.model_fields.keys() - {"text"}:
        assert getattr(capabilities, field_name) is CapabilitySupport.UNVERIFIED
    assert capability_enabled(CapabilitySupport.SUPPORTED)
    assert not capability_enabled(CapabilitySupport.UNVERIFIED)
    assert not capability_enabled(CapabilitySupport.UNSUPPORTED)


def test_provider_binding_digest_covers_version_executable_and_credential_identity(
    tmp_path: Path,
) -> None:
    local = _binding(tmp_path)
    changed_version = local.model_copy(update={"version": 2})
    changed_executable = local.model_copy(
        update={
            "executable_identity": local.executable_identity.model_copy(
                update={"sha256_digest": "b" * 64}
            )
            if local.executable_identity is not None
            else None
        }
    )
    remote = _binding(
        tmp_path,
        transport=ProviderTransport.HTTP_API,
        executable_identity=None,
        credential_ref_id=uuid4(),
    )

    assert compute_provider_binding_digest(local) != compute_provider_binding_digest(
        changed_version
    )
    assert compute_provider_binding_digest(local) != compute_provider_binding_digest(
        changed_executable
    )
    assert compute_provider_binding_digest(remote) != compute_provider_binding_digest(
        remote.model_copy(update={"credential_ref_id": uuid4()})
    )


def test_provider_binding_digest_ignores_volatile_health_observations(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    refreshed_health = binding.model_copy(
        update={
            "status": ProviderStatus.UNHEALTHY,
            "health_checked_at": binding.health_checked_at + timedelta(minutes=5),
        }
    )

    assert compute_provider_binding_digest(binding) == compute_provider_binding_digest(
        refreshed_health
    )


def test_immutable_request_digest_allows_only_explicit_proof_refresh() -> None:
    request = _request()
    refreshed = request.model_copy(
        update={
            "authorization_proof_id": uuid4(),
            "authorization_proof_digest": "a" * 64,
            "autonomy_grant_id": uuid4(),
            "autonomy_grant_digest": "b" * 64,
            "credential_grant_ids": (uuid4(),),
            "credential_proof_id": uuid4(),
            "credential_proof_digest": "c" * 64,
            "budget_proof_id": uuid4(),
            "budget_proof_digest": "d" * 64,
            "kill_switch_proof_id": uuid4(),
            "kill_switch_proof_digest": "e" * 64,
            "resume_handle_id": uuid4(),
        }
    )

    assert compute_agent_run_immutable_digest(request) == compute_agent_run_immutable_digest(
        refreshed
    )
    assert request.immutable_request_digest == compute_agent_run_immutable_digest(request)

    for field, value in (
        ("model", "substituted-model"),
        ("prompt", "Substituted prompt."),
        ("sandbox_profile", "unsafe"),
        ("filesystem_envelope", ("filesystem.all",)),
        ("max_output_tokens", 9999),
        ("deadline", datetime(2031, 1, 1, tzinfo=UTC)),
        ("idempotency_key", "substituted"),
    ):
        assert compute_agent_run_immutable_digest(request) != (
            compute_agent_run_immutable_digest(request.model_copy(update={field: value}))
        )


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        (
            {"allowed_roots": (Path("relative/root"),)},
            "autonomy_root_must_be_absolute",
        ),
        (
            {
                "allowed_effect_classes": frozenset({"shell.execute"}),
                "denied_effect_classes": frozenset({"shell.execute"}),
            },
            "autonomy_effect_classes_overlap",
        ),
        (
            {"allowed_effect_classes": frozenset({"authority.bypass"})},
            "always_block_effect_cannot_be_allowed",
        ),
        ({"policy_digest": "not-a-digest"}, None),
        (
            {
                "issued_at": datetime(2026, 7, 15, tzinfo=UTC),
                "expires_at": datetime(2026, 7, 15, tzinfo=UTC),
            },
            "autonomy_grant_expired_at_issue",
        ),
    ],
)
def test_autonomy_grant_rejects_invalid_paths_effects_digest_and_expiry(
    tmp_path: Path,
    updates: dict[str, object],
    reason_code: str | None,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _grant(tmp_path, **updates)

    if reason_code is not None:
        assert exc_info.value.errors()[0]["ctx"]["reason_code"] == reason_code


def test_autonomy_grant_rejects_noncanonical_root(tmp_path: Path) -> None:
    noncanonical = tmp_path.resolve() / "child" / ".."

    with pytest.raises(ValidationError, match="autonomy_root_must_be_canonical"):
        _grant(tmp_path, allowed_roots=(noncanonical,))


def test_request_rejects_blank_idempotency_and_malformed_digest() -> None:
    with pytest.raises(ValidationError):
        _request(idempotency_key="   ")
    with pytest.raises(ValidationError):
        _request(authorization_proof_digest="ABC")


def test_request_allows_timezone_aware_historical_deadline_for_archival() -> None:
    historical_deadline = datetime(2025, 1, 1, tzinfo=UTC)

    request = _request(deadline=historical_deadline)

    assert request.deadline == historical_deadline


def test_request_requires_exactly_one_prompt_shape_and_has_no_secret_field() -> None:
    with pytest.raises(ValidationError, match="agent_run_requires_messages_or_prompt"):
        _request(prompt=None)
    with pytest.raises(ValidationError, match="agent_run_message_blank"):
        _request(prompt=None, messages=("   ",))
    with pytest.raises(ValidationError, match="agent_run_message_too_long"):
        _request(prompt=None, messages=("x" * 100_001,))
    with pytest.raises(ValidationError, match="agent_run_messages_too_large"):
        _request(prompt=None, messages=("x" * 100_000,) * 11)
    with pytest.raises(ValidationError):
        _request(prompt=None, messages=("message",) * 101)
    with pytest.raises(ValidationError, match="agent_run_prompt_shape_ambiguous"):
        _request(messages=("hello",))
    with pytest.raises(ValidationError) as secret:
        AgentRunRequest.model_validate({**_request().model_dump(), "secret_value": "plaintext"})
    assert secret.value.errors()[0]["type"] == "extra_forbidden"
    assert "secret" not in AgentRunRequest.model_fields
    assert "secret_value" not in AgentRunRequest.model_fields


def test_request_rejects_credential_digest_without_proof_identity() -> None:
    values = _request().model_dump(exclude_computed_fields=True)
    values["credential_proof_id"] = None

    with pytest.raises(ValidationError) as exc_info:
        AgentRunRequest.model_validate(values)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "agent_run_credential_proof_pair_incomplete"
    )


def test_event_requires_positive_sequence_valid_digest_and_safe_payload() -> None:
    event = _event()
    assert event.sequence == 1

    with pytest.raises(ValidationError):
        _event(sequence=0)
    with pytest.raises(ValidationError, match="agent_run_event_digest_mismatch"):
        AgentRunEvent(**{**event.model_dump(), "event_digest": "f" * 64})
    with pytest.raises(ValidationError, match="agent_run_event_payload_contains_secret_key"):
        _event(redacted_payload={"nested": {"access_token": "redacted"}})
    with pytest.raises(ValidationError, match="agent_run_event_payload_contains_secret_key"):
        _event(redacted_payload={"sk-1234567890abcdef": "ok"})


def test_event_allows_numeric_token_usage_fields_without_classifying_as_secret() -> None:
    event = _event(
        event_type=AgentRunEventType.USAGE,
        redacted_payload={
            "input_tokens": 12,
            "output_tokens": 8,
            "max_output_tokens": 100,
            "tokens_used": 20,
            "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 0},
            "completion_tokens_details": {
                "reasoning_tokens": 3,
                "accepted_prediction_tokens": 2,
                "rejected_prediction_tokens": 1,
            },
        },
    )

    assert event.redacted_payload["input_tokens"] == 12


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "api_key=sk-1234567890abcdef"},
        {"nested": {"items": ["safe", "token=secret-shaped-value"]}},
        {"message": "Bearer abcdefghijklmnop"},
        {"message": "Basic dXNlcjpwYXNzd29yZA=="},
        {"message": "Authorization: Bearer abcdefghijklmnop"},
        {"message": ("Authorization: Digest username=lucas,response=abcdef1234567890")},
        {"message": "cookie=session_id=abcdefghijklmnop"},
        {"message": "credential=abcdefghijklmnop"},
        {"message": "passphrase=abcdefghijklmnop"},
    ],
)
def test_event_rejects_secret_bearing_values_under_benign_keys(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="agent_run_event_payload_contains_secret_value"):
        _event(redacted_payload=payload)


def test_secret_payload_value_scan_does_not_thaw_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(value: object) -> object:
        raise AssertionError(f"unexpected payload thaw: {value!r}")

    monkeypatch.setattr(agent_runtime, "_thaw_json", fail_if_called)

    assert not agent_runtime._contains_secret_payload_value({"message": "safe"})
    assert agent_runtime._contains_secret_payload_value(
        {"message": "passphrase=my secret passphrase"}
    )


def test_secret_payload_scans_reuse_module_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedRedactorConstruction:
        def __init__(self) -> None:
            raise AssertionError("payload scan constructed a per-call redactor")

    monkeypatch.setattr(agent_runtime, "SecretRedactor", UnexpectedRedactorConstruction)

    assert not agent_runtime._contains_secret_payload_key({"message": "safe"})
    assert not agent_runtime._contains_secret_payload_value({"message": "safe"})


@pytest.mark.parametrize(
    "event_type",
    [
        AgentRunEventType.TOOL_REQUESTED,
        AgentRunEventType.TOOL_BLOCKED,
        AgentRunEventType.TOOL_STARTED,
        AgentRunEventType.TOOL_RESULT,
        AgentRunEventType.APPROVAL_REQUIRED,
    ],
)
def test_effect_events_require_digest_bound_authorization_decision(
    event_type: AgentRunEventType,
) -> None:
    tool_call_id = None if event_type is AgentRunEventType.APPROVAL_REQUIRED else "tool-1"
    with pytest.raises(ValidationError, match="effect_authorization_decision_required"):
        _event(event_type=event_type, tool_call_id=tool_call_id)

    decision_id = uuid4()
    decision_digest = "d" * 64
    event = _event(
        event_type=event_type,
        tool_call_id=tool_call_id,
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest=decision_digest,
    )
    assert event.effect_authorization_decision_id == decision_id

    with pytest.raises(ValidationError, match="agent_run_event_digest_mismatch"):
        AgentRunEvent.model_validate(
            {
                **event.model_dump(),
                "effect_authorization_decision_digest": "e" * 64,
            }
        )


@pytest.mark.parametrize(
    "follow_up_type",
    [
        AgentRunEventType.TOOL_BLOCKED,
        AgentRunEventType.TOOL_STARTED,
        AgentRunEventType.TOOL_RESULT,
        AgentRunEventType.APPROVAL_REQUIRED,
    ],
)
def test_event_chain_rejects_tool_decision_reference_substitution(
    follow_up_type: AgentRunEventType,
) -> None:
    run_id = uuid4()
    handle_id = uuid4()
    first_decision_id = uuid4()
    second_decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    requested = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=first_decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=started.event_digest,
    )
    events = [started, requested]
    if follow_up_type is AgentRunEventType.TOOL_RESULT:
        tool_started = _event(
            run_id=run_id,
            handle_id=handle_id,
            sequence=3,
            timestamp=started.timestamp + timedelta(microseconds=2),
            event_type=AgentRunEventType.TOOL_STARTED,
            tool_call_id="tool-1",
            effect_authorization_decision_id=first_decision_id,
            effect_authorization_decision_digest="d" * 64,
            previous_event_digest=requested.event_digest,
        )
        events.append(tool_started)
    previous = events[-1]
    substituted = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=len(events) + 1,
        timestamp=started.timestamp + timedelta(microseconds=len(events)),
        event_type=follow_up_type,
        tool_call_id="tool-1",
        effect_authorization_decision_id=second_decision_id,
        effect_authorization_decision_digest="e" * 64,
        previous_event_digest=previous.event_digest,
    )

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((*events, substituted))

    assert exc_info.value.reason_code == "tool_effect_authorization_mismatch"


def test_event_chain_allows_standalone_and_matching_tool_approvals() -> None:
    run_id = uuid4()
    handle_id = uuid4()
    decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    standalone_approval = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.APPROVAL_REQUIRED,
        effect_authorization_decision_id=uuid4(),
        effect_authorization_decision_digest="c" * 64,
        previous_event_digest=started.event_digest,
    )
    requested = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=3,
        timestamp=started.timestamp + timedelta(microseconds=2),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=standalone_approval.event_digest,
    )
    tool_approval = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=4,
        timestamp=started.timestamp + timedelta(microseconds=3),
        event_type=AgentRunEventType.APPROVAL_REQUIRED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=requested.event_digest,
    )

    state = validate_agent_run_event_chain((started, standalone_approval, requested, tool_approval))

    assert state.value == "running"


def test_event_chain_rejects_tool_approval_after_tool_result() -> None:
    run_id = uuid4()
    handle_id = uuid4()
    decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    requested = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=started.event_digest,
    )
    tool_started = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=3,
        timestamp=started.timestamp + timedelta(microseconds=2),
        event_type=AgentRunEventType.TOOL_STARTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=requested.event_digest,
    )
    result = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=4,
        timestamp=started.timestamp + timedelta(microseconds=3),
        event_type=AgentRunEventType.TOOL_RESULT,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=tool_started.event_digest,
    )
    late_approval = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=5,
        timestamp=started.timestamp + timedelta(microseconds=4),
        event_type=AgentRunEventType.APPROVAL_REQUIRED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=result.event_digest,
    )

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((started, requested, tool_started, result, late_approval))

    assert exc_info.value.reason_code == "tool_event_prerequisite_missing"


@pytest.mark.parametrize("tool_is_started", [False, True])
def test_event_chain_rejects_terminal_with_unresolved_tool_call(tool_is_started: bool) -> None:
    run_id = uuid4()
    handle_id = uuid4()
    decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    requested = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=started.event_digest,
    )
    events = [started, requested]
    if tool_is_started:
        tool_started = _event(
            run_id=run_id,
            handle_id=handle_id,
            sequence=3,
            timestamp=started.timestamp + timedelta(microseconds=2),
            event_type=AgentRunEventType.TOOL_STARTED,
            tool_call_id="tool-1",
            effect_authorization_decision_id=decision_id,
            effect_authorization_decision_digest="d" * 64,
            previous_event_digest=requested.event_digest,
        )
        events.append(tool_started)
    previous = events[-1]
    terminal = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=len(events) + 1,
        timestamp=started.timestamp + timedelta(microseconds=len(events)),
        event_type=AgentRunEventType.COMPLETED,
        previous_event_digest=previous.event_digest,
    )

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((*events, terminal))

    assert exc_info.value.reason_code == "terminal_with_unresolved_tool_call"


def test_event_chain_rejects_decision_reference_reuse_across_tool_calls() -> None:
    run_id = uuid4()
    handle_id = uuid4()
    decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    first = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=started.event_digest,
    )
    reused = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=3,
        timestamp=started.timestamp + timedelta(microseconds=2),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-2",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=first.event_digest,
    )

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((started, first, reused))

    assert exc_info.value.reason_code == "tool_effect_authorization_reused"


def test_event_chain_rejects_tool_blocked_after_tool_started() -> None:
    run_id = uuid4()
    handle_id = uuid4()
    decision_id = uuid4()
    started = _event(run_id=run_id, handle_id=handle_id)
    requested = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(microseconds=1),
        event_type=AgentRunEventType.TOOL_REQUESTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=started.event_digest,
    )
    tool_started = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=3,
        timestamp=started.timestamp + timedelta(microseconds=2),
        event_type=AgentRunEventType.TOOL_STARTED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=requested.event_digest,
    )
    blocked = _event(
        run_id=run_id,
        handle_id=handle_id,
        sequence=4,
        timestamp=started.timestamp + timedelta(microseconds=3),
        event_type=AgentRunEventType.TOOL_BLOCKED,
        tool_call_id="tool-1",
        effect_authorization_decision_id=decision_id,
        effect_authorization_decision_digest="d" * 64,
        previous_event_digest=tool_started.event_digest,
    )

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((started, requested, tool_started, blocked))

    assert exc_info.value.reason_code == "tool_event_prerequisite_missing"


@pytest.mark.parametrize(
    "secret_key",
    [
        "api_key_value",
        "API-Key-Value",
        "password_hash",
        "authorization_header",
        "cookie_value",
        "refreshTokenValue",
        "credential_value",
        "privateKeyPem",
        "signing-key-material",
        "passphrase_text",
        "tokens",
    ],
)
def test_event_rejects_common_secret_key_variants(secret_key: str) -> None:
    with pytest.raises(ValidationError, match="agent_run_event_payload_contains_secret_key"):
        _event(redacted_payload={"nested": [{secret_key: "redacted"}]})
    assert SecretRedactor().redact_value({secret_key: "plaintext"}) == {secret_key: "[REDACTED]"}


def test_event_payload_rejects_top_level_mutation_after_validation() -> None:
    event = _event(redacted_payload={"message": "started"})
    canary = "plaintext"

    with pytest.raises(TypeError):
        event.redacted_payload["access_token"] = canary  # type: ignore[index]


def test_event_payload_rejects_nested_mutation_after_validation() -> None:
    event = _event(redacted_payload={"nested": {"message": "started"}})
    nested = event.redacted_payload["nested"]
    assert isinstance(nested, Mapping)
    canary = "plaintext"

    with pytest.raises(TypeError):
        nested["access_token"] = canary  # type: ignore[index]


def test_event_payload_serialization_and_digest_replay_are_stable() -> None:
    event = _event(
        redacted_payload={
            "nested": {"message": "started"},
            "items": [{"sequence": 1}, {"sequence": 2}],
        }
    )

    dumped = event.model_dump(mode="json")
    serialized = json.loads(event.model_dump_json())
    replayed = AgentRunEvent.model_validate(dumped)

    assert serialized == dumped
    assert replayed.event_digest == event.event_digest
    assert replayed == event


def test_event_chain_recomputes_digests_before_trusting_links() -> None:
    started = _event()
    completed = _event(
        run_id=started.run_id,
        handle_id=started.handle_id,
        sequence=2,
        timestamp=started.timestamp + timedelta(seconds=1),
        event_type=AgentRunEventType.COMPLETED,
        previous_event_digest=started.event_digest,
    )
    forged_digest = "f" * 64
    tampered_started = started.model_copy(
        update={
            "redacted_payload": {"message": "tampered"},
            "event_digest": forged_digest,
        }
    )
    relinked_completed = completed.model_copy(update={"previous_event_digest": forged_digest})

    with pytest.raises(AgentRunEventChainError) as exc_info:
        validate_agent_run_event_chain((tampered_started, relinked_completed))

    assert exc_info.value.reason_code == "agent_run_event_digest_mismatch"
