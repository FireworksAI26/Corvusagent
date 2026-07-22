from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from corvus.application.ports import (
    ProjectAuthorizationDecision,
    ProjectCreateLifecycleError,
)
from corvus.application.projects import (
    CreateProjectCommand,
    GetProjectQuery,
    InProcessProjectClient,
    ProjectResponse,
    ProjectService,
)
from corvus.domain.client import ClientSurface
from corvus.domain.identity import Project
from corvus.domain.request import (
    IdempotencyEnvelope,
    IdempotencyStatus,
    RequestContext,
    validate_idempotency_replay,
)

_NOW = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)
_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
_PROJECT_ID = UUID("10000000-0000-0000-0000-000000000002")
_REQUESTER_ID = UUID("10000000-0000-0000-0000-000000000003")
_AGENT_ID = UUID("10000000-0000-0000-0000-000000000004")
_CLIENT_CONTEXT_ID = UUID("10000000-0000-0000-0000-000000000005")
_TRANSPORT_ID = UUID("10000000-0000-0000-0000-000000000006")


class ContractStore:
    def __init__(self) -> None:
        self.projects: dict[tuple[UUID, UUID], Project] = {}
        self.create_count = 0

    def create(self, project: Project) -> None:
        self.projects[(project.workspace_id, project.id)] = project
        self.create_count += 1

    def get(self, workspace_id: UUID, project_id: UUID) -> Project | None:
        return self.projects.get((workspace_id, project_id))


class ContractAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class MemoryIdempotency:
    def __init__(self) -> None:
        self.values: dict[tuple[object, ...], IdempotencyEnvelope] = {}

    def claim_idempotency(self, envelope: IdempotencyEnvelope) -> IdempotencyEnvelope:
        existing = self.values.get(envelope.composite_identity)
        if existing is not None:
            validate_idempotency_replay(
                existing,
                request_context_digest=envelope.request_context_digest,
                payload_digest=envelope.payload_digest,
            )
            return existing
        self.values[envelope.composite_identity] = envelope
        return envelope

    def complete_idempotency(self, envelope: IdempotencyEnvelope) -> None:
        assert envelope.status is IdempotencyStatus.SUCCEEDED
        self.values[envelope.composite_identity] = envelope


class SurfaceAuthorization:
    def __init__(self) -> None:
        self.enabled = {ClientSurface.CLI, ClientSurface.DESKTOP}
        self.expected_transport = _TRANSPORT_ID
        self.revoked = False
        self.requests = []

    def authorize(self, request):
        self.requests.append(request)
        if request.client_surface not in self.enabled:
            allowed, reason = False, "client_surface_disabled"
        elif request.transport_principal_id != self.expected_transport:
            allowed, reason = False, "transport_principal_mismatch"
        elif self.revoked:
            allowed, reason = False, "requester_grant_revoked"
        else:
            allowed, reason = True, "authorized"
        return ProjectAuthorizationDecision(
            allowed=allowed,
            reason_code=reason,
            authorization_snapshot_id=request.context.authorization_snapshot_id,
        )


class IdempotentCreateLifecycle:
    def __init__(self, store: ContractStore, audit: ContractAudit) -> None:
        self.store = store
        self.audit = audit
        self.requests: dict[UUID, Project] = {}

    def create(self, project: Project, event) -> None:
        existing = self.requests.get(event.request_id)
        if existing is not None:
            if existing != project:
                raise ProjectCreateLifecycleError("project_replay_mismatch")
            return
        self.audit.record(event)
        self.store.create(project)
        self.requests[event.request_id] = project


def _project(*, name: str = "Contract Corvus") -> Project:
    return Project(
        id=_PROJECT_ID,
        workspace_id=_WORKSPACE_ID,
        name=name,
        root_locator="workspace://contract-corvus",
        privacy="private",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _context(*, request_id: UUID, project_id: UUID, transport_id: UUID) -> RequestContext:
    return RequestContext(
        id=request_id,
        deployment_profile_id=UUID("10000000-0000-0000-0000-000000000010"),
        deployment_instance_id=UUID("10000000-0000-0000-0000-000000000011"),
        workspace_id=_WORKSPACE_ID,
        workspace_authority_epoch=1,
        workspace_authority_generation=4,
        authority_state_root="a" * 64,
        authority_epoch_credential_id=UUID("10000000-0000-0000-0000-000000000012"),
        authority_commit_receipt_id=UUID("10000000-0000-0000-0000-000000000013"),
        authority_proof_digest="b" * 64,
        scope_kind="project",
        scope_id=project_id,
        scope_digest="f" * 64,
        audience_policy_snapshot_id=UUID("10000000-0000-0000-0000-000000000014"),
        audience_policy_digest="c" * 64,
        requester_id=_REQUESTER_ID,
        client_context_id=_CLIENT_CONTEXT_ID,
        transport_principal_id=transport_id,
        agent_id=_AGENT_ID,
        agent_grant_id=UUID("10000000-0000-0000-0000-000000000015"),
        access_bundle_id=UUID("10000000-0000-0000-0000-000000000016"),
        policy_digest="d" * 64,
        authorization_snapshot_id=UUID("10000000-0000-0000-0000-000000000017"),
        authorization_snapshot_digest="e" * 64,
        authorization_signing_key_version_id=UUID("10000000-0000-0000-0000-000000000018"),
        idempotency_key="project-contract-v1",
        correlation_id=UUID("10000000-0000-0000-0000-000000000019"),
    )


def _command(
    project: Project,
    *,
    surface: ClientSurface = ClientSurface.CLI,
    transport_id: UUID = _TRANSPORT_ID,
) -> CreateProjectCommand:
    return CreateProjectCommand(
        context=_context(
            request_id=UUID("10000000-0000-0000-0000-000000000007"),
            project_id=project.id,
            transport_id=transport_id,
        ),
        client_surface=surface,
        project=project,
    )


def _query(
    *,
    surface: ClientSurface,
    transport_id: UUID = _TRANSPORT_ID,
) -> GetProjectQuery:
    return GetProjectQuery(
        context=_context(
            request_id=UUID("10000000-0000-0000-0000-000000000008"),
            project_id=_PROJECT_ID,
            transport_id=transport_id,
        ),
        client_surface=surface,
        project_id=_PROJECT_ID,
    )


def _client() -> tuple[
    InProcessProjectClient,
    ContractStore,
    ContractAudit,
    SurfaceAuthorization,
    IdempotentCreateLifecycle,
]:
    store = ContractStore()
    audit = ContractAudit()
    authorization = SurfaceAuthorization()
    lifecycle = IdempotentCreateLifecycle(store, audit)
    idempotency = MemoryIdempotency()
    client = InProcessProjectClient(
        ProjectService(
            store=store,
            authorization=authorization,
            audit=audit,
            create_lifecycle=lifecycle,
            idempotency=idempotency,
            clock=lambda: _NOW,
        )
    )
    return client, store, audit, authorization, lifecycle


def test_command_query_and_response_envelopes_are_stable_and_secret_free() -> None:
    project = _project()
    command = _command(project)
    query = _query(surface=ClientSurface.CLI)
    response = ProjectResponse(
        request_id=command.request_id,
        ok=True,
        reason_code="project_created",
        project=project,
    )

    assert set(command.model_dump(mode="json")) == {
        "context",
        "client_surface",
        "project",
    }
    assert set(query.model_dump(mode="json")) == {
        "context",
        "client_surface",
        "project_id",
    }
    assert set(command.context.model_dump(mode="json")) == {
        "id",
        "deployment_profile_id",
        "deployment_instance_id",
        "workspace_id",
        "workspace_authority_epoch",
        "workspace_authority_generation",
        "authority_state_root",
        "authority_epoch_credential_id",
        "authority_commit_receipt_id",
        "authority_proof_digest",
        "scope_kind",
        "scope_id",
        "scope_digest",
        "audience_policy_snapshot_id",
        "audience_policy_digest",
        "requester_id",
        "client_context_id",
        "transport_principal_id",
        "agent_id",
        "agent_grant_id",
        "access_bundle_id",
        "execution_placement_id",
        "policy_digest",
        "authorization_snapshot_id",
        "authorization_snapshot_digest",
        "authorization_signing_key_version_id",
        "idempotency_key",
        "correlation_id",
    }
    assert set(response.model_dump(mode="json")) == {
        "request_id",
        "ok",
        "reason_code",
        "project",
    }
    assert command.model_dump(mode="json")["client_surface"] == "cli"
    assert GetProjectQuery.model_validate_json(query.model_dump_json()) == query
    assert CreateProjectCommand.model_validate_json(command.model_dump_json()) == command
    assert ProjectResponse.model_validate_json(response.model_dump_json()) == response

    forbidden = {
        "secret_value",
        "password",
        "access_token",
        "refresh_token",
        "private_key",
        "api_key",
    }
    schemas = (
        CreateProjectCommand.model_json_schema(),
        GetProjectQuery.model_json_schema(),
        ProjectResponse.model_json_schema(),
    )
    serialized_schema = " ".join(str(schema).lower() for schema in schemas)
    assert all(field not in serialized_schema for field in forbidden)


def test_context_project_substitution_is_rejected_before_service_execution() -> None:
    project = _project()
    foreign_context = _context(
        request_id=UUID("10000000-0000-0000-0000-000000000020"),
        project_id=project.id,
        transport_id=_TRANSPORT_ID,
    ).model_copy(update={"workspace_id": UUID("10000000-0000-0000-0000-000000000021")})

    with pytest.raises(ValidationError, match="project_request_context_mismatch"):
        CreateProjectCommand(
            context=foreign_context,
            client_surface=ClientSurface.CLI,
            project=project,
        )


def test_currently_authorized_replay_is_idempotent_and_rechecks_revocation() -> None:
    client, store, audit, authorization, lifecycle = _client()
    command = _command(_project())

    created = client.create_project(command)
    replayed = client.create_project(command)
    fresh_context = command.context.model_copy(
        update={
            "id": UUID("10000000-0000-0000-0000-000000000070"),
            "correlation_id": UUID("10000000-0000-0000-0000-000000000071"),
            "workspace_authority_generation": 5,
            "authority_state_root": "9" * 64,
            "authority_commit_receipt_id": UUID("10000000-0000-0000-0000-000000000072"),
            "authority_proof_digest": "8" * 64,
            "authorization_snapshot_id": UUID("10000000-0000-0000-0000-000000000073"),
            "authorization_snapshot_digest": "7" * 64,
        }
    )
    fresh_replay = client.create_project(command.model_copy(update={"context": fresh_context}))
    mismatched = client.create_project(
        command.model_copy(update={"project": _project(name="Substituted")})
    )
    authorization.revoked = True
    revoked = client.create_project(command)

    assert replayed == created
    assert fresh_replay.ok is True
    assert fresh_replay.project == created.project
    assert fresh_replay.request_id == fresh_context.id
    assert store.create_count == 1
    assert len(lifecycle.requests) == 1
    assert mismatched.ok is False
    assert mismatched.reason_code == "idempotency_payload_mismatch"
    assert revoked.ok is False
    assert revoked.reason_code == "requester_grant_revoked"
    assert len(audit.events) == 2
    assert audit.events[-1].decision == "deny"


def test_enabled_surfaces_are_equivalent_and_transport_claims_fail_closed() -> None:
    client, store, audit, authorization, _ = _client()
    store.create(_project())

    cli = client.get_project(_query(surface=ClientSurface.CLI))
    desktop = client.get_project(_query(surface=ClientSurface.DESKTOP))
    disabled = client.get_project(_query(surface=ClientSurface.WEB))
    mismatched = client.get_project(
        _query(
            surface=ClientSurface.CLI,
            transport_id=UUID("10000000-0000-0000-0000-000000000088"),
        )
    )

    assert (cli.ok, cli.reason_code, cli.project) == (
        desktop.ok,
        desktop.reason_code,
        desktop.project,
    )
    assert disabled.ok is False
    assert disabled.reason_code == "client_surface_disabled"
    assert mismatched.ok is False
    assert mismatched.reason_code == "transport_principal_mismatch"
    assert [request.client_surface for request in authorization.requests] == [
        ClientSurface.CLI,
        ClientSurface.DESKTOP,
        ClientSurface.WEB,
        ClientSurface.CLI,
    ]
    assert [event.decision for event in audit.events] == ["allow", "allow", "deny", "deny"]
