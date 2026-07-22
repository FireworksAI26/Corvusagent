from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corvus.application.ports import ProjectAuditEvent
from corvus.domain.audit import AuditReceipt
from corvus.infrastructure.repositories.audit import AuditRepository


class ProjectAuditAdapterError(RuntimeError):
    pass


class ProjectAuditReceiptContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    prior_authority_epoch: int = Field(ge=1)
    prior_authority_generation: int = Field(ge=0)
    prior_authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_authority_commit_receipt_id: UUID
    authority_commit_intent_id: UUID
    intended_mutation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_version_id: UUID


class ProjectAuditContextProvider(Protocol):
    def resolve(self, request_id: UUID) -> ProjectAuditReceiptContext: ...


class ProjectAuditSigner(Protocol):
    @property
    def signing_key_version_id(self) -> UUID: ...

    def sign(self, data: bytes) -> bytes: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def audit_receipt_hash(receipt: AuditReceipt) -> str:
    unsigned = receipt.model_dump(
        mode="json",
        exclude={"receipt_hash", "receipt_signature"},
    )
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


class SignedProjectAuditAdapter:
    def __init__(
        self,
        *,
        repository: AuditRepository,
        context_provider: ProjectAuditContextProvider | None,
        signer: ProjectAuditSigner,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.context_provider = context_provider
        self.signer = signer
        self.clock = clock

    def record(self, event: ProjectAuditEvent) -> None:
        if self.context_provider is None:
            raise ProjectAuditAdapterError("audit_context_provider_missing")
        self.record_with_context(event, self.context_provider.resolve(event.request_id))

    def record_with_context(
        self,
        event: ProjectAuditEvent,
        context: ProjectAuditReceiptContext,
    ) -> AuditReceipt:
        snapshot = self.repository.get_snapshot(
            workspace_id=event.workspace_id,
            snapshot_id=event.authorization_snapshot_id,
        )
        if snapshot is None:
            raise ProjectAuditAdapterError("authorization_snapshot_missing")
        expected_resource = f"project:{event.project_id}"
        if (
            snapshot.request_context_id != event.request_id
            or snapshot.workspace_id != event.workspace_id
            or snapshot.requester_id != event.requester_id
            or snapshot.scope_kind != "project"
            or snapshot.scope_id != event.project_id
            or snapshot.decision != event.decision
            or snapshot.reason_code != event.reason_code
            or snapshot.canonical_inputs_json.get("action") != event.action
            or snapshot.canonical_inputs_json.get("resource") != expected_resource
        ):
            raise ProjectAuditAdapterError("authorization_snapshot_event_mismatch")

        if context.request_context_id != event.request_id:
            raise ProjectAuditAdapterError("audit_context_request_mismatch")
        if (
            context.prior_authority_generation != snapshot.authority_generation
            or context.prior_authority_state_root != snapshot.authority_state_root
            or context.prior_authority_commit_receipt_id != snapshot.authority_commit_receipt_id
            or context.signing_key_version_id != snapshot.signing_key_version_id
            or self.signer.signing_key_version_id != context.signing_key_version_id
        ):
            raise ProjectAuditAdapterError("audit_context_authority_mismatch")

        receipts = self.repository.list_receipts(event.workspace_id)
        sequence = len(receipts) + 1
        previous_hash = "0" * 64 if not receipts else receipts[-1].receipt_hash
        sanitized_input_digest = hashlib.sha256(
            _canonical_json(event.model_dump(mode="json"))
        ).hexdigest()
        provisional = AuditReceipt(
            workspace_id=event.workspace_id,
            workspace_sequence=sequence,
            schema_version=1,
            prior_authority_epoch=context.prior_authority_epoch,
            prior_authority_generation=context.prior_authority_generation,
            prior_authority_state_root=context.prior_authority_state_root,
            prior_authority_commit_receipt_id=context.prior_authority_commit_receipt_id,
            authority_commit_intent_id=context.authority_commit_intent_id,
            intended_mutation_digest=context.intended_mutation_digest,
            request_context_id=event.request_id,
            authorization_snapshot_id=snapshot.id,
            authorization_snapshot_digest=snapshot.canonical_digest,
            action=event.action,
            resource=expected_resource,
            decision=event.decision,
            reason_code=event.reason_code,
            policy_digest=snapshot.policy_digest,
            sanitized_input_digest=sanitized_input_digest,
            signing_key_version_id=context.signing_key_version_id,
            previous_hash=previous_hash,
            receipt_hash="0" * 64,
            receipt_signature="pending",
            created_at=self.clock(),
        )
        receipt_hash = audit_receipt_hash(provisional)
        signature = base64.b64encode(self.signer.sign(bytes.fromhex(receipt_hash))).decode()
        receipt = provisional.model_copy(
            update={
                "receipt_hash": receipt_hash,
                "receipt_signature": signature,
            }
        )
        self.repository.append_receipt(receipt)
        return receipt
