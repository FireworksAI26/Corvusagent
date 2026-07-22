from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def _now_utc() -> datetime:
    return datetime.now(UTC)


class RequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    deployment_profile_id: UUID
    deployment_instance_id: UUID
    workspace_id: UUID
    workspace_authority_epoch: int = Field(ge=1)
    workspace_authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_epoch_credential_id: UUID
    authority_commit_receipt_id: UUID
    authority_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_kind: Literal["workspace", "project", "channel", "thread", "conversation"]
    scope_id: UUID
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audience_policy_snapshot_id: UUID
    audience_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requester_id: UUID
    client_context_id: UUID
    transport_principal_id: UUID | None = None
    agent_id: UUID
    agent_grant_id: UUID
    access_bundle_id: UUID
    execution_placement_id: UUID | None = None
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_signing_key_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=512)
    correlation_id: UUID


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class IdempotencyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    requester_id: UUID
    transport_principal_id: UUID
    agent_id: UUID
    agent_grant_id: UUID
    operation: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=512)
    request_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: IdempotencyStatus
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    created_at: datetime = Field(default_factory=_now_utc)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> IdempotencyEnvelope:
        has_result = self.result_digest is not None or self.result_ref is not None
        if self.status is IdempotencyStatus.IN_PROGRESS and (
            has_result or self.completed_at is not None
        ):
            raise PydanticCustomError(
                "invalid_in_progress_idempotency",
                "reason_code={reason_code}",
                {"reason_code": "in_progress_idempotency_cannot_have_result"},
            )
        if self.status is IdempotencyStatus.SUCCEEDED and (
            self.result_digest is None or self.result_ref is None or self.completed_at is None
        ):
            raise PydanticCustomError(
                "incomplete_successful_idempotency",
                "reason_code={reason_code}",
                {"reason_code": "successful_idempotency_requires_committed_result"},
            )
        if self.status is IdempotencyStatus.FAILED and self.completed_at is None:
            raise PydanticCustomError(
                "incomplete_failed_idempotency",
                "reason_code={reason_code}",
                {"reason_code": "failed_idempotency_requires_completion_time"},
            )
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise PydanticCustomError(
                "invalid_idempotency_chronology",
                "reason_code={reason_code}",
                {"reason_code": "idempotency_completion_precedes_creation"},
            )
        return self

    @property
    def composite_identity(self) -> tuple[UUID, UUID, UUID, UUID, UUID, str, str]:
        return (
            self.workspace_id,
            self.requester_id,
            self.transport_principal_id,
            self.agent_id,
            self.agent_grant_id,
            self.operation,
            self.idempotency_key,
        )


def validate_idempotency_replay(
    envelope: IdempotencyEnvelope,
    *,
    request_context_digest: str,
    payload_digest: str,
) -> None:
    if envelope.request_context_digest != request_context_digest:
        raise IdempotencyContractError(
            "idempotency_context_mismatch",
            "idempotency replay belongs to another request context",
        )
    if envelope.payload_digest != payload_digest:
        raise IdempotencyContractError(
            "idempotency_payload_mismatch",
            "idempotency key was reused with another payload",
        )
