from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.request import (
    IdempotencyContractError,
    IdempotencyEnvelope,
    IdempotencyStatus,
    RequestContext,
    validate_idempotency_replay,
)


def _request_context() -> RequestContext:
    return RequestContext(
        deployment_profile_id=uuid4(),
        deployment_instance_id=uuid4(),
        workspace_id=uuid4(),
        workspace_authority_epoch=3,
        workspace_authority_generation=11,
        authority_state_root="a" * 64,
        authority_epoch_credential_id=uuid4(),
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="b" * 64,
        scope_kind="project",
        scope_id=uuid4(),
        scope_digest="f" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_policy_digest="c" * 64,
        requester_id=uuid4(),
        client_context_id=uuid4(),
        transport_principal_id=uuid4(),
        agent_id=uuid4(),
        agent_grant_id=uuid4(),
        access_bundle_id=uuid4(),
        execution_placement_id=uuid4(),
        policy_digest="d" * 64,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="e" * 64,
        authorization_signing_key_version_id=uuid4(),
        idempotency_key="create-project:001",
        correlation_id=uuid4(),
    )


def test_request_context_is_fully_authority_bound_and_immutable() -> None:
    context = _request_context()

    assert context.workspace_authority_generation == 11
    with pytest.raises(ValidationError, match="frozen_instance"):
        context.workspace_authority_generation = 12


def test_idempotency_replay_rejects_payload_or_context_substitution() -> None:
    context = _request_context()
    envelope = IdempotencyEnvelope(
        workspace_id=context.workspace_id,
        requester_id=context.requester_id,
        transport_principal_id=context.transport_principal_id,
        agent_id=context.agent_id,
        agent_grant_id=context.agent_grant_id,
        operation="project.create",
        idempotency_key=context.idempotency_key,
        request_context_digest="f" * 64,
        payload_digest="0" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
    )

    with pytest.raises(IdempotencyContractError) as context_exc:
        validate_idempotency_replay(
            envelope,
            request_context_digest="1" * 64,
            payload_digest="0" * 64,
        )
    assert context_exc.value.reason_code == "idempotency_context_mismatch"

    with pytest.raises(IdempotencyContractError) as payload_exc:
        validate_idempotency_replay(
            envelope,
            request_context_digest="f" * 64,
            payload_digest="2" * 64,
        )
    assert payload_exc.value.reason_code == "idempotency_payload_mismatch"


def test_in_progress_idempotency_envelope_cannot_claim_result() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IdempotencyEnvelope(
            workspace_id=uuid4(),
            requester_id=uuid4(),
            transport_principal_id=uuid4(),
            agent_id=uuid4(),
            agent_grant_id=uuid4(),
            operation="project.create",
            idempotency_key="create-project:002",
            request_context_digest="a" * 64,
            payload_digest="b" * 64,
            status=IdempotencyStatus.IN_PROGRESS,
            result_digest="c" * 64,
            result_ref="project://created",
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "in_progress_idempotency_cannot_have_result"
    )
