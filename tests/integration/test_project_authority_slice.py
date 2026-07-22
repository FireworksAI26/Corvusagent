from __future__ import annotations

from uuid import UUID, uuid4

from corvus.application.ports import (
    ProjectAuthorizationDecision,
    ProjectCreateLifecycleError,
)
from corvus.application.projects import (
    CreateProjectCommand,
    GetProjectQuery,
    InProcessProjectClient,
    ProjectService,
)
from corvus.domain.client import ClientSurface
from corvus.domain.identity import Project
from corvus.domain.request import RequestContext


class FakeStore:
    def __init__(self) -> None:
        self.projects = {}
        self.create_calls = 0

    def create(self, project: Project) -> None:
        self.create_calls += 1
        self.projects[(project.workspace_id, project.id)] = project

    def get(self, workspace_id, project_id):
        return self.projects.get((workspace_id, project_id))


class FakeAuthorization:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow

    def authorize(self, request):
        return ProjectAuthorizationDecision(
            allowed=self.allow,
            reason_code="authorized" if self.allow else "no_requester_grant",
            authorization_snapshot_id=request.context.authorization_snapshot_id,
        )


class FakeAudit:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events = []

    def record(self, event) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


class FakeCreateLifecycle:
    def __init__(self, store: FakeStore, audit: FakeAudit) -> None:
        self.store = store
        self.audit = audit

    def create(self, project: Project, event) -> None:
        try:
            self.audit.record(event)
        except Exception as exc:
            raise ProjectCreateLifecycleError("audit_persistence_failed") from exc
        self.store.create(project)


def _context(project: Project, *, request_id: UUID | None = None) -> RequestContext:
    return RequestContext(
        id=request_id or uuid4(),
        deployment_profile_id=uuid4(),
        deployment_instance_id=uuid4(),
        workspace_id=project.workspace_id,
        workspace_authority_epoch=1,
        workspace_authority_generation=4,
        authority_state_root="a" * 64,
        authority_epoch_credential_id=uuid4(),
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="b" * 64,
        scope_kind="project",
        scope_id=project.id,
        scope_digest="f" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_policy_digest="c" * 64,
        requester_id=uuid4(),
        client_context_id=uuid4(),
        transport_principal_id=uuid4(),
        agent_id=uuid4(),
        agent_grant_id=uuid4(),
        access_bundle_id=uuid4(),
        policy_digest="d" * 64,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="e" * 64,
        authorization_signing_key_version_id=uuid4(),
        idempotency_key="project-authority-slice",
        correlation_id=uuid4(),
    )


def _command(project: Project) -> CreateProjectCommand:
    return CreateProjectCommand(
        context=_context(project),
        client_surface=ClientSurface.CLI,
        project=project,
    )


def test_create_and_read_share_one_authorized_audited_path() -> None:
    store = FakeStore()
    audit = FakeAudit()
    client = InProcessProjectClient(
        ProjectService(
            store=store,
            authorization=FakeAuthorization(),
            audit=audit,
            create_lifecycle=FakeCreateLifecycle(store, audit),
        )
    )
    project = Project(
        workspace_id=uuid4(),
        name="Corvus",
        root_locator="workspace://corvus",
        privacy="private",
    )

    created = client.create_project(_command(project))
    read = client.get_project(
        GetProjectQuery(
            context=_context(project),
            client_surface=ClientSurface.CLI,
            project_id=project.id,
        )
    )

    assert created.ok is True
    assert read.ok is True
    assert created.project == project
    assert read.project == project
    assert [event.action for event in audit.events] == ["project.create", "project.read"]


def test_denial_is_audited_and_does_not_mutate() -> None:
    store = FakeStore()
    audit = FakeAudit()
    client = InProcessProjectClient(
        ProjectService(store=store, authorization=FakeAuthorization(allow=False), audit=audit)
    )
    project = Project(
        workspace_id=uuid4(),
        name="Denied",
        root_locator="workspace://denied",
        privacy="private",
    )

    response = client.create_project(_command(project))

    assert response.ok is False
    assert response.reason_code == "no_requester_grant"
    assert response.project is None
    assert store.create_calls == 0
    assert audit.events[0].decision == "deny"


def test_audit_failure_fails_closed_before_mutation() -> None:
    store = FakeStore()
    audit = FakeAudit(fail=True)
    client = InProcessProjectClient(
        ProjectService(
            store=store,
            authorization=FakeAuthorization(),
            audit=audit,
            create_lifecycle=FakeCreateLifecycle(store, audit),
        )
    )
    project = Project(
        workspace_id=uuid4(),
        name="No audit",
        root_locator="workspace://no-audit",
        privacy="private",
    )

    response = client.create_project(_command(project))

    assert response.ok is False
    assert response.reason_code == "audit_persistence_failed"
    assert store.create_calls == 0


def test_allowed_create_without_authority_lifecycle_fails_closed() -> None:
    store = FakeStore()
    audit = FakeAudit()
    client = InProcessProjectClient(
        ProjectService(store=store, authorization=FakeAuthorization(), audit=audit)
    )
    project = Project(
        workspace_id=uuid4(),
        name="No authority lifecycle",
        root_locator="workspace://no-authority-lifecycle",
        privacy="private",
    )

    response = client.create_project(_command(project))

    assert response.ok is False
    assert response.reason_code == "project_authority_lifecycle_unavailable"
    assert store.create_calls == 0
    assert len(audit.events) == 1
    assert audit.events[0].decision == "allow"
    assert audit.events[0].reason_code == "authorized"
