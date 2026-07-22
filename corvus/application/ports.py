from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    CancellationResult,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderHealth,
    compute_agent_run_request_digest,
)
from corvus.domain.client import ClientSurface
from corvus.domain.conversations import (
    AgentRunRecord,
    AttachmentRef,
    Message,
    RunArtifact,
    RunEventRecord,
    Thread,
)
from corvus.domain.identity import Project
from corvus.domain.request import IdempotencyEnvelope, RequestContext


class _ProjectContextBound(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RequestContext
    client_surface: ClientSurface

    @model_validator(mode="after")
    def validate_transport_binding(self) -> _ProjectContextBound:
        if self.context.transport_principal_id is None:
            raise ValueError("project_transport_principal_missing")
        return self

    @property
    def request_id(self) -> UUID:
        return self.context.id

    @property
    def workspace_id(self) -> UUID:
        return self.context.workspace_id

    @property
    def requester_id(self) -> UUID:
        return self.context.requester_id

    @property
    def acting_agent_id(self) -> UUID:
        return self.context.agent_id

    @property
    def client_context_id(self) -> UUID:
        return self.context.client_context_id

    @property
    def transport_principal_id(self) -> UUID:
        principal_id = self.context.transport_principal_id
        if principal_id is None:  # pragma: no cover - guarded by model validation
            raise ValueError("project_transport_principal_missing")
        return principal_id


class ProjectAuthorizationRequest(_ProjectContextBound):
    action: Literal["project.create", "project.read"]
    project_id: UUID


class ProjectAuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str = Field(min_length=1, max_length=200)
    authorization_snapshot_id: UUID


class ProjectAuditEvent(_ProjectContextBound):
    authorization_snapshot_id: UUID
    action: Literal["project.create", "project.read"]
    project_id: UUID
    decision: Literal["allow", "deny"]
    reason_code: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_snapshot_binding(self) -> ProjectAuditEvent:
        if self.authorization_snapshot_id != self.context.authorization_snapshot_id:
            raise ValueError("project_authorization_snapshot_mismatch")
        return self

    @property
    def authority_proof_digest(self) -> str:
        return self.context.authority_proof_digest


class ProjectCreateLifecycleError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ProjectCreateLifecyclePort(Protocol):
    def create(self, project: Project, event: ProjectAuditEvent) -> None: ...


class ProjectStorePort(Protocol):
    def create(self, project: Project) -> None: ...

    def get(self, workspace_id: UUID, project_id: UUID) -> Project | None: ...


class ProjectAuthorizationPort(Protocol):
    def authorize(self, request: ProjectAuthorizationRequest) -> ProjectAuthorizationDecision: ...


class ProjectIdempotencyPort(Protocol):
    def claim_idempotency(self, envelope: IdempotencyEnvelope) -> IdempotencyEnvelope: ...

    def complete_idempotency(self, envelope: IdempotencyEnvelope) -> None: ...


class ProjectAuditPort(Protocol):
    def record(self, event: ProjectAuditEvent) -> None: ...


type ConversationMutationResult = (
    Thread | AttachmentRef | Message | AgentRunRecord | RunEventRecord | RunArtifact
)


class ConversationAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RequestContext
    client_surface: ClientSurface
    action: str = Field(pattern=r"^conversation\.[a-z_]+$", max_length=100)
    workspace_id: UUID
    resource_id: UUID

    @model_validator(mode="after")
    def validate_workspace(self) -> ConversationAuthorizationRequest:
        if self.workspace_id != self.context.workspace_id:
            raise ValueError("conversation_request_workspace_mismatch")
        return self


class ConversationAuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=200)
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConversationMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RequestContext
    client_surface: ClientSurface
    action: str = Field(pattern=r"^conversation\.[a-z_]+$", max_length=100)
    workspace_id: UUID
    resource_id: UUID
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_authority_binding(self) -> ConversationMutationRequest:
        if (
            self.workspace_id != self.context.workspace_id
            or self.authorization_snapshot_id != self.context.authorization_snapshot_id
            or self.authorization_snapshot_digest != self.context.authorization_snapshot_digest
        ):
            raise ValueError("conversation_mutation_authority_binding_mismatch")
        return self


class ConversationMutationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_receipt_id: UUID
    audit_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalized_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: ConversationMutationResult


class ConversationAuthorizationPort(Protocol):
    def authorize(
        self, request: ConversationAuthorizationRequest
    ) -> ConversationAuthorizationDecision: ...


class ConversationMutationLifecyclePort(Protocol):
    def execute(
        self,
        request: ConversationMutationRequest,
        mutation: Callable[[], ConversationMutationResult],
    ) -> ConversationMutationReceipt: ...


class AgentRunOperation(StrEnum):
    START = "agent_run.start"
    RESUME = "agent_run.resume"
    CANCEL = "agent_run.cancel"


def _raise_agent_run_contract_error(reason_code: str) -> None:
    raise PydanticCustomError(
        "invalid_agent_run_authorization_request",
        "reason_code={reason_code}",
        {"reason_code": reason_code},
    )


def _validate_agent_run_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _raise_agent_run_contract_error("agent_run_timestamp_must_be_timezone_aware")
    return value


class AgentRunAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    context: RequestContext
    client_surface: ClientSurface
    operation: AgentRunOperation
    request: AgentRunRequest
    handle: AgentRunHandle | None = None
    canonical_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_kill_switch_proof_id: UUID | None = None
    current_kill_switch_proof_digest: str | None = None

    @model_validator(mode="after")
    def validate_authority_binding(self) -> AgentRunAuthorizationRequest:
        if self.context.workspace_id != self.request.workspace_id:
            _raise_agent_run_contract_error("agent_run_workspace_mismatch")
        if self.context.scope_kind == "project":
            if self.context.scope_id != self.request.project_id:
                _raise_agent_run_contract_error("agent_run_project_scope_mismatch")
        elif self.context.scope_kind == "workspace":
            if (
                self.context.scope_id != self.context.workspace_id
                or self.request.project_id is not None
            ):
                _raise_agent_run_contract_error("agent_run_workspace_scope_mismatch")
        else:
            _raise_agent_run_contract_error("agent_run_scope_kind_unsupported")
        if self.context.idempotency_key != self.request.idempotency_key:
            _raise_agent_run_contract_error("agent_run_idempotency_key_mismatch")
        if (
            self.context.authorization_snapshot_id != self.request.authorization_proof_id
            or self.context.authorization_snapshot_digest != self.request.authorization_proof_digest
        ):
            _raise_agent_run_contract_error("agent_run_authorization_snapshot_mismatch")
        return self

    @model_validator(mode="after")
    def validate_operation_binding(self) -> AgentRunAuthorizationRequest:
        if self.canonical_request_digest != compute_agent_run_request_digest(self.request):
            _raise_agent_run_contract_error("agent_run_request_digest_mismatch")
        if self.handle is not None:
            if self.handle.run_id != self.request.run_id:
                _raise_agent_run_contract_error("agent_run_handle_run_mismatch")
            if self.handle.provider_binding_id != self.request.provider_binding_id:
                _raise_agent_run_contract_error("agent_run_handle_provider_mismatch")
        if self.operation is AgentRunOperation.START:
            if self.handle is not None:
                _raise_agent_run_contract_error("agent_run_handle_unsolicited")
            if self.request.resume_handle_id is not None:
                _raise_agent_run_contract_error("agent_run_resume_handle_unsolicited")
            if (
                self.current_kill_switch_proof_id is not None
                or self.current_kill_switch_proof_digest is not None
            ):
                _raise_agent_run_contract_error("agent_run_current_kill_switch_proof_unsolicited")
        elif self.operation is AgentRunOperation.RESUME:
            if self.handle is None or self.request.resume_handle_id != self.handle.id:
                _raise_agent_run_contract_error("agent_run_resume_handle_mismatch")
            if (
                self.current_kill_switch_proof_id is not None
                or self.current_kill_switch_proof_digest is not None
            ):
                _raise_agent_run_contract_error("agent_run_current_kill_switch_proof_unsolicited")
        elif self.operation is AgentRunOperation.CANCEL:
            if self.request.resume_handle_id is not None:
                _raise_agent_run_contract_error("agent_run_resume_handle_unsolicited")
            if self.handle is None:
                _raise_agent_run_contract_error("agent_run_handle_run_mismatch")
            if (
                self.current_kill_switch_proof_id is None
                or self.current_kill_switch_proof_digest is None
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    self.current_kill_switch_proof_digest,
                )
                is None
            ):
                _raise_agent_run_contract_error("agent_run_current_kill_switch_proof_required")
        return self


class AgentRunAuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=200)
    authorization_snapshot_id: UUID
    canonical_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_version: int = Field(ge=1)
    provider_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    autonomy_grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kill_switch_proof_id: UUID
    kill_switch_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_evaluated_at(self) -> AgentRunAuthorizationDecision:
        _validate_agent_run_timestamp(self.evaluated_at)
        return self


class AgentRunAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    context: RequestContext
    client_surface: ClientSurface
    operation: AgentRunOperation
    run_id: UUID
    handle_id: UUID | None = None
    provider_binding_id: UUID
    provider_binding_version: int = Field(ge=1)
    provider_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    autonomy_grant_id: UUID
    autonomy_grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_proof_id: UUID | None = None
    credential_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_proof_id: UUID | None = None
    budget_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kill_switch_proof_id: UUID
    kill_switch_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: Literal["authorization", "outcome"]
    outcome: Literal["allow", "deny", "success", "failure"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=200)
    timestamp: datetime

    @model_validator(mode="after")
    def validate_audit_binding(self) -> AgentRunAuditEvent:
        if (
            self.authorization_snapshot_id != self.context.authorization_snapshot_id
            or self.authorization_snapshot_digest != self.context.authorization_snapshot_digest
        ):
            _raise_agent_run_contract_error("agent_run_authorization_snapshot_mismatch")
        valid_phase_outcomes = {
            "authorization": {"allow", "deny"},
            "outcome": {"success", "failure"},
        }
        if self.outcome not in valid_phase_outcomes[self.phase]:
            _raise_agent_run_contract_error("agent_run_audit_phase_outcome_mismatch")
        proof_pairs = (
            (self.credential_proof_id, self.credential_proof_digest),
            (self.budget_proof_id, self.budget_proof_digest),
        )
        if any(
            (proof_id is None) != (proof_digest is None) for proof_id, proof_digest in proof_pairs
        ):
            _raise_agent_run_contract_error("agent_run_audit_proof_pair_incomplete")
        _validate_agent_run_timestamp(self.timestamp)
        return self


def compute_agent_run_audit_event_digest(event: AgentRunAuditEvent) -> str:
    encoded = json.dumps(
        event.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_agent_run_audit_receipt_digest(
    *,
    sequence: int,
    previous_receipt_digest: str,
    event_digest: str,
    acknowledged: bool,
) -> str:
    encoded = json.dumps(
        {
            "event_digest": event_digest,
            "previous_receipt_digest": previous_receipt_digest,
            "sequence": sequence,
            "acknowledged": acknowledged,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AgentRunAuditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    previous_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledged: bool

    @model_validator(mode="after")
    def validate_receipt_digest(self) -> AgentRunAuditReceipt:
        expected = compute_agent_run_audit_receipt_digest(
            sequence=self.sequence,
            previous_receipt_digest=self.previous_receipt_digest,
            event_digest=self.event_digest,
            acknowledged=self.acknowledged,
        )
        if self.receipt_digest != expected:
            _raise_agent_run_contract_error("agent_run_audit_receipt_digest_mismatch")
        return self


@runtime_checkable
class AgentRunAuthorizationPort(Protocol):
    def authorize(
        self,
        request: AgentRunAuthorizationRequest,
    ) -> AgentRunAuthorizationDecision: ...


@runtime_checkable
class AgentRunAuditPort(Protocol):
    def record(self, event: AgentRunAuditEvent) -> AgentRunAuditReceipt: ...


@runtime_checkable
class AgentRuntimePort(Protocol):
    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]: ...

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities: ...

    async def health(self, binding: ProviderBinding) -> ProviderHealth: ...

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult: ...

    def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]: ...

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult: ...

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle: ...
