from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from corvus.application.ports import (
    ProjectAuditEvent,
    ProjectAuditPort,
    ProjectAuthorizationPort,
    ProjectAuthorizationRequest,
    ProjectCreateLifecycleError,
    ProjectCreateLifecyclePort,
    ProjectIdempotencyPort,
    ProjectStorePort,
)
from corvus.domain.client import ClientSurface
from corvus.domain.identity import Project
from corvus.domain.request import IdempotencyEnvelope, IdempotencyStatus, RequestContext
from corvus.infrastructure.repositories.projects import ProjectRepository

_IDEMPOTENCY_NAMESPACE = UUID("cf955f62-6f36-4187-93e7-a84ac95ceab4")
_TRANSIENT_CONTEXT_FIELDS = {
    "id",
    "correlation_id",
    "workspace_authority_generation",
    "authority_state_root",
    "authority_commit_receipt_id",
    "authority_proof_digest",
    "authorization_snapshot_id",
    "authorization_snapshot_digest",
    "authorization_signing_key_version_id",
}


def _canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_context_digest(context: RequestContext) -> str:
    return _canonical_digest(context.model_dump(mode="json", exclude=_TRANSIENT_CONTEXT_FIELDS))


def _project_idempotency_envelope(
    context: RequestContext,
    project: Project,
    *,
    created_at: datetime,
) -> IdempotencyEnvelope:
    transport_principal_id = context.transport_principal_id
    if transport_principal_id is None:  # pragma: no cover - command validation
        raise ValueError("project_transport_principal_missing")
    identity_material = ":".join(
        (
            str(context.workspace_id),
            str(context.requester_id),
            str(transport_principal_id),
            str(context.agent_id),
            str(context.agent_grant_id),
            "project.create",
            context.idempotency_key,
        )
    )
    return IdempotencyEnvelope(
        id=uuid5(_IDEMPOTENCY_NAMESPACE, identity_material),
        workspace_id=context.workspace_id,
        requester_id=context.requester_id,
        transport_principal_id=transport_principal_id,
        agent_id=context.agent_id,
        agent_grant_id=context.agent_grant_id,
        operation="project.create",
        idempotency_key=context.idempotency_key,
        request_context_digest=_idempotency_context_digest(context),
        payload_digest=_canonical_digest(project),
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=created_at,
    )


class CreateProjectCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RequestContext
    client_surface: ClientSurface
    project: Project

    @model_validator(mode="after")
    def validate_context_binding(self) -> CreateProjectCommand:
        if (
            self.context.workspace_id != self.project.workspace_id
            or self.context.scope_kind != "project"
            or self.context.scope_id != self.project.id
        ):
            raise ValueError("project_request_context_mismatch")
        if self.context.transport_principal_id is None:
            raise ValueError("project_transport_principal_missing")
        return self

    @property
    def request_id(self) -> UUID:
        return self.context.id

    @property
    def workspace_id(self) -> UUID:
        return self.context.workspace_id


class GetProjectQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RequestContext
    client_surface: ClientSurface
    project_id: UUID

    @model_validator(mode="after")
    def validate_context_binding(self) -> GetProjectQuery:
        if self.context.scope_kind != "project" or self.context.scope_id != self.project_id:
            raise ValueError("project_request_context_mismatch")
        if self.context.transport_principal_id is None:
            raise ValueError("project_transport_principal_missing")
        return self

    @property
    def request_id(self) -> UUID:
        return self.context.id

    @property
    def workspace_id(self) -> UUID:
        return self.context.workspace_id


class ProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    ok: bool
    reason_code: str
    project: Project | None = None


class ProjectRepositoryAdapter:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def create(self, project: Project) -> None:
        self.repository.add(project)

    def get(self, workspace_id: UUID, project_id: UUID) -> Project | None:
        return self.repository.get(workspace_id=workspace_id, project_id=project_id)


class ProjectService:
    def __init__(
        self,
        *,
        store: ProjectStorePort,
        authorization: ProjectAuthorizationPort,
        audit: ProjectAuditPort,
        create_lifecycle: ProjectCreateLifecyclePort | None = None,
        idempotency: ProjectIdempotencyPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authorization = authorization
        self.audit = audit
        self.create_lifecycle = create_lifecycle
        self.idempotency = idempotency
        self.clock = clock or (lambda: datetime.now(UTC))

    def create(self, command: CreateProjectCommand) -> ProjectResponse:
        return self._execute(
            context=command.context,
            client_surface=command.client_surface,
            action="project.create",
            project_id=command.project.id,
            project=command.project,
        )

    def get(self, query: GetProjectQuery) -> ProjectResponse:
        return self._execute(
            context=query.context,
            client_surface=query.client_surface,
            action="project.read",
            project_id=query.project_id,
            project=None,
        )

    def _execute(
        self,
        *,
        context: RequestContext,
        client_surface: ClientSurface,
        action: Literal["project.create", "project.read"],
        project_id: UUID,
        project: Project | None,
    ) -> ProjectResponse:
        request = ProjectAuthorizationRequest(
            context=context,
            client_surface=client_surface,
            action=action,
            project_id=project_id,
        )
        try:
            decision = self.authorization.authorize(request)
        except Exception:
            return ProjectResponse(
                request_id=context.id,
                ok=False,
                reason_code="authorization_unavailable",
            )
        if decision.authorization_snapshot_id != context.authorization_snapshot_id:
            return ProjectResponse(
                request_id=context.id,
                ok=False,
                reason_code="authorization_snapshot_mismatch",
            )
        audit_event = ProjectAuditEvent(
            context=context,
            client_surface=client_surface,
            authorization_snapshot_id=decision.authorization_snapshot_id,
            action=action,
            project_id=project_id,
            decision="allow" if decision.allowed else "deny",
            reason_code=decision.reason_code,
        )
        if not decision.allowed:
            try:
                self.audit.record(audit_event)
            except Exception:
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="audit_persistence_failed",
                )
            return ProjectResponse(
                request_id=context.id,
                ok=False,
                reason_code=decision.reason_code,
            )
        result: Project | None
        if action == "project.create":
            if project is None:  # pragma: no cover - internal invariant
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="project_missing",
                )
            if self.create_lifecycle is None:
                try:
                    self.audit.record(audit_event)
                except Exception:
                    return ProjectResponse(
                        request_id=context.id,
                        ok=False,
                        reason_code="audit_persistence_failed",
                    )
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="project_authority_lifecycle_unavailable",
                )
            idempotency: IdempotencyEnvelope | None = None
            if self.idempotency is not None:
                try:
                    proposed = _project_idempotency_envelope(
                        context,
                        project,
                        created_at=self.clock(),
                    )
                    idempotency = self.idempotency.claim_idempotency(proposed)
                except Exception as exc:
                    reason = str(exc)
                    if "idempotency_payload_mismatch" in reason:
                        reason = "idempotency_payload_mismatch"
                    elif "idempotency_context_mismatch" in reason:
                        reason = "idempotency_context_mismatch"
                    else:
                        reason = "idempotency_unavailable"
                    return ProjectResponse(
                        request_id=context.id,
                        ok=False,
                        reason_code=reason,
                    )
                if idempotency.status is IdempotencyStatus.SUCCEEDED:
                    cached = self.store.get(context.workspace_id, project_id)
                    if cached is None or idempotency.result_digest != _canonical_digest(cached):
                        return ProjectResponse(
                            request_id=context.id,
                            ok=False,
                            reason_code="idempotency_result_mismatch",
                        )
                    return ProjectResponse(
                        request_id=context.id,
                        ok=True,
                        reason_code="project_created",
                        project=cached,
                    )
                if idempotency.status is IdempotencyStatus.FAILED:
                    return ProjectResponse(
                        request_id=context.id,
                        ok=False,
                        reason_code=idempotency.result_ref or "idempotency_previous_failure",
                    )
            try:
                self.create_lifecycle.create(project, audit_event)
            except ProjectCreateLifecycleError as exc:
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code=exc.reason_code,
                )
            except Exception:
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="project_persistence_failed",
                )
            if idempotency is not None and self.idempotency is not None:
                completed = idempotency.model_copy(
                    update={
                        "status": IdempotencyStatus.SUCCEEDED,
                        "result_digest": _canonical_digest(project),
                        "result_ref": f"project:{project.id}",
                        "completed_at": self.clock(),
                    }
                )
                try:
                    self.idempotency.complete_idempotency(completed)
                except Exception:
                    return ProjectResponse(
                        request_id=context.id,
                        ok=False,
                        reason_code="idempotency_completion_failed",
                    )
            result = project
        else:
            try:
                self.audit.record(audit_event)
            except Exception:
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="audit_persistence_failed",
                )
            try:
                result = self.store.get(context.workspace_id, project_id)
            except Exception:
                return ProjectResponse(
                    request_id=context.id,
                    ok=False,
                    reason_code="project_persistence_failed",
                )
        if result is None:
            return ProjectResponse(
                request_id=context.id,
                ok=False,
                reason_code="project_not_found",
            )
        return ProjectResponse(
            request_id=context.id,
            ok=True,
            reason_code="project_created" if action == "project.create" else "project_found",
            project=result,
        )


class InProcessProjectClient:
    def __init__(self, service: ProjectService) -> None:
        self.service = service

    def create_project(self, command: CreateProjectCommand) -> ProjectResponse:
        return self.service.create(command)

    def get_project(self, query: GetProjectQuery) -> ProjectResponse:
        return self.service.get(query)
