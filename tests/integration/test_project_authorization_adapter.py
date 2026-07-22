from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from corvus.application.authorization import AuthorizationRequest, KillSwitchScopeBinding
from corvus.application.projects import GetProjectQuery, InProcessProjectClient, ProjectService
from corvus.domain.access import AccessBundle, AgentGrant, CapabilityEffect, CapabilityGrant
from corvus.domain.client import ClientSurface
from corvus.domain.request import RequestContext
from corvus.infrastructure.project_authorization import (
    EvaluatingProjectAuthorizationAdapter,
    ProjectAuthorizationInputs,
)

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


class StaticInputs:
    def __init__(self, value: ProjectAuthorizationInputs) -> None:
        self.value = value

    def resolve(self, _request):
        return self.value


class GuardedStore:
    def __init__(self) -> None:
        self.get_called = False

    def create(self, _project) -> None:
        raise AssertionError("read test must not create")

    def get(self, _workspace_id, _project_id):
        self.get_called = True
        raise AssertionError("denied request reached project storage")


class RecordingAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


def test_project_service_uses_real_evaluator_and_audits_missing_authority_denial() -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    requester_id = uuid4()
    agent_id = uuid4()
    transport_id = uuid4()
    requester_bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=requester_id,
        scope_kind="project",
        scope_id=project_id,
        issued_by=requester_id,
        policy_digest="a" * 64,
        expires_at=_NOW + timedelta(hours=1),
    )
    requester_grant = CapabilityGrant(
        bundle_id=requester_bundle.id,
        workspace_id=workspace_id,
        resource_kind="project",
        resource_id=project_id,
        action="project.read",
        effect=CapabilityEffect.ALLOW,
    )
    agent_bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=agent_id,
        scope_kind="project",
        scope_id=project_id,
        issued_by=requester_id,
        policy_digest="b" * 64,
        expires_at=_NOW + timedelta(hours=1),
    )
    agent_capability = CapabilityGrant(
        bundle_id=agent_bundle.id,
        workspace_id=workspace_id,
        resource_kind="project",
        resource_id=project_id,
        action="project.read",
        effect=CapabilityEffect.ALLOW,
    )
    agent_grant = AgentGrant(
        workspace_id=workspace_id,
        agent_id=agent_id,
        capability_bundle_id=agent_bundle.id,
        autonomy_level=1,
        issued_by=requester_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    request_id = uuid4()
    client_context_id = uuid4()
    audience_id = uuid4()
    instance_id = uuid4()
    credential_id = uuid4()
    receipt_id = uuid4()
    manifest_id = uuid4()
    trust_anchor_id = uuid4()
    snapshot_id = uuid4()
    signing_key_id = uuid4()
    kill_switch_id = uuid4()
    scope_digest = "c" * 64
    audience_digest = "d" * 64
    authority_request = AuthorizationRequest(
        workspace_id=workspace_id,
        request_context_id=request_id,
        deployment_instance_id=instance_id,
        workspace_authority_epoch=1,
        workspace_authority_generation=4,
        authority_state_root="e" * 64,
        authority_epoch_credential_id=credential_id,
        authority_commit_receipt_id=receipt_id,
        authority_proof_digest="f" * 64,
        trust_anchor_id=trust_anchor_id,
        authority_manifest_version_id=manifest_id,
        authority_manifest_digest="0" * 64,
        kill_switch_snapshot_ids=(kill_switch_id,),
        kill_switch_snapshot_digest="1" * 64,
        kill_switch_scope_bindings=(
            KillSwitchScopeBinding(scope_kind="workspace", scope_id=workspace_id),
        ),
        audience_policy_snapshot_id=audience_id,
        audience_policy_digest=audience_digest,
        scope_digest=scope_digest,
        client_context_id=client_context_id,
        client_surface=ClientSurface.CLI,
        transport_principal_id=transport_id,
        requester_id=requester_id,
        acting_agent_id=agent_id,
        scope_kind="project",
        scope_id=project_id,
        resource_kind="project",
        resource_id=project_id,
        action="project.read",
        evaluated_at=_NOW,
    )
    context = RequestContext(
        id=request_id,
        deployment_profile_id=uuid4(),
        deployment_instance_id=instance_id,
        workspace_id=workspace_id,
        workspace_authority_epoch=1,
        workspace_authority_generation=4,
        authority_state_root="e" * 64,
        authority_epoch_credential_id=credential_id,
        authority_commit_receipt_id=receipt_id,
        authority_proof_digest="f" * 64,
        scope_kind="project",
        scope_id=project_id,
        scope_digest=scope_digest,
        audience_policy_snapshot_id=audience_id,
        audience_policy_digest=audience_digest,
        requester_id=requester_id,
        client_context_id=client_context_id,
        transport_principal_id=transport_id,
        agent_id=agent_id,
        agent_grant_id=agent_grant.id,
        access_bundle_id=requester_bundle.id,
        policy_digest=requester_bundle.policy_digest,
        authorization_snapshot_id=snapshot_id,
        authorization_snapshot_digest="2" * 64,
        authorization_signing_key_version_id=signing_key_id,
        idempotency_key="real-project-read",
        correlation_id=uuid4(),
    )
    inputs = StaticInputs(
        ProjectAuthorizationInputs(
            request=authority_request,
            authority_context=None,
            requester_bundle=requester_bundle,
            requester_grants=(requester_grant,),
            agent_grant=agent_grant,
            agent_bundle=agent_bundle,
            agent_capabilities=(agent_capability,),
        )
    )
    store = GuardedStore()
    audit = RecordingAudit()
    client = InProcessProjectClient(
        ProjectService(
            store=store,
            authorization=EvaluatingProjectAuthorizationAdapter(inputs=inputs),
            audit=audit,
        )
    )

    response = client.get_project(
        GetProjectQuery(
            context=context,
            client_surface=ClientSurface.CLI,
            project_id=project_id,
        )
    )

    assert response.ok is False
    assert response.reason_code == "authority_context_missing"
    assert store.get_called is False
    assert len(audit.events) == 1
    assert audit.events[0].decision == "deny"
    assert audit.events[0].authorization_snapshot_id == snapshot_id
