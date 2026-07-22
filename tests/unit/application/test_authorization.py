from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corvus.application.authorization import (
    AuthorityCommitProof,
    AuthorityEvaluationContext,
    AuthorityRuntimePossessionProof,
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationSnapshotExpectedInputs,
    AuthorizationSnapshotVerificationProof,
    BudgetRuntimeVerificationProof,
    CredentialVerificationProof,
    KillSwitchScopeBinding,
    KillSwitchSnapshotEntry,
    KillSwitchVerificationProof,
    RegistryVerificationProof,
    authority_public_key_set_digest,
    authority_runtime_possession_digest,
    authorization_snapshot_bound_input_digest,
    authorization_snapshot_record_digest,
    evaluate_capability_intersection,
    verify_authorization_decision_snapshot,
)
from corvus.application.ports import (
    AgentRunAuthorizationRequest,
    AgentRunOperation,
    ProjectAuthorizationRequest,
)
from corvus.domain.access import (
    AccessBundle,
    AgentGrant,
    CapabilityEffect,
    CapabilityGrant,
    CredentialKind,
    CredentialRef,
    CredentialStatus,
    DelegationGrant,
)
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunState,
    AutonomyGrant,
    AutonomyProfile,
    CapabilitySupport,
    ExecutableIdentity,
    ProviderBinding,
    ProviderFamily,
    ProviderStatus,
    ProviderTransport,
    compute_agent_run_request_digest,
    compute_agent_run_runtime_limit_digest,
    compute_autonomy_grant_digest,
    compute_provider_binding_digest,
)
from corvus.domain.audit import (
    AuthorizationDecisionSnapshot,
    SigningKeyStatus,
    WorkspaceSigningKeyVersion,
    authorization_snapshot_digest,
)
from corvus.domain.client import ClientContext, ClientSurface
from corvus.domain.deployment import (
    AuthorityEpochCredential,
    AuthorityEpochCredentialStatus,
    AuthorityRegistryFreshnessProof,
    AuthorityRegistryTrustState,
    AuthorityRegistryVerifierKeyVersion,
    AuthorityStateRootLeafFamily,
    AuthorityStateRootManifestVersion,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorKind,
    CoverageKind,
    DeploymentInstance,
    DeploymentInstanceLease,
    RegistryVerifierKeyStatus,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
    fixed_workspace_lock_name,
)
from corvus.domain.execution import ExecutionKind, ExecutionPlacement, ExecutionStatus
from corvus.domain.request import RequestContext
from corvus.domain.scope import AudiencePolicySnapshot
from corvus.infrastructure.agent_run_authorization import (
    VerifiedAgentRunAuthorizationAdapter,
    VerifiedAgentRunAuthorizationInputs,
    canonical_budget_evidence_receipt,
    canonical_credential_evidence_receipt,
)
from corvus.infrastructure.project_authorization import (
    EvaluatingProjectAuthorizationAdapter,
    ProjectAuthorizationInputs,
    VerifiedProjectAuthorizationAdapter,
    VerifiedProjectAuthorizationInputs,
)


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _signature_b64(private_key: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def _exact_allow_case() -> tuple[
    AuthorizationRequest,
    AccessBundle,
    CapabilityGrant,
    AgentGrant,
    AccessBundle,
    CapabilityGrant,
]:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    workspace_id = uuid4()
    requester_id = uuid4()
    agent_id = uuid4()
    project_id = uuid4()
    requester_bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=requester_id,
        scope_kind="project",
        scope_id=project_id,
        issued_by=uuid4(),
        policy_digest="a" * 64,
        expires_at=now + timedelta(hours=1),
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
        issued_by=uuid4(),
        policy_digest="b" * 64,
        expires_at=now + timedelta(hours=1),
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
        autonomy_level=2,
        issued_by=requester_id,
        expires_at=now + timedelta(hours=1),
    )
    kill_switch_snapshot_ids = (uuid4(), uuid4())
    kill_switch_scope_bindings = (
        KillSwitchScopeBinding(scope_kind="workspace", scope_id=workspace_id),
        KillSwitchScopeBinding(scope_kind="agent", scope_id=agent_id),
    )
    audience_policy_snapshot_id = uuid4()
    client_context_id = uuid4()
    request = AuthorizationRequest(
        workspace_id=workspace_id,
        request_context_id=uuid4(),
        deployment_instance_id=uuid4(),
        workspace_authority_epoch=3,
        workspace_authority_generation=4,
        authority_state_root="2" * 64,
        authority_epoch_credential_id=uuid4(),
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="3" * 64,
        trust_anchor_id=uuid4(),
        registry_trust_metadata_version=4,
        registry_history_head_digest="6" * 64,
        registry_freshness_proof_id=uuid4(),
        registry_freshness_sequence=10,
        authority_manifest_version_id=uuid4(),
        authority_manifest_digest="7" * 64,
        kill_switch_snapshot_ids=kill_switch_snapshot_ids,
        kill_switch_snapshot_digest="e" * 64,
        kill_switch_scope_bindings=kill_switch_scope_bindings,
        audience_policy_snapshot_id=audience_policy_snapshot_id,
        audience_policy_digest="f" * 64,
        scope_digest="0" * 64,
        client_context_id=client_context_id,
        client_surface=ClientSurface.CLI,
        transport_principal_id=requester_id,
        requester_id=requester_id,
        acting_agent_id=agent_id,
        scope_kind="project",
        scope_id=project_id,
        resource_kind="project",
        resource_id=project_id,
        action="project.read",
        evaluated_at=now,
    )
    return (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
    )


def _valid_authority_context(request: AuthorizationRequest) -> AuthorityEvaluationContext:
    deployment_profile_id = uuid4()
    instance_private_key = Ed25519PrivateKey.generate()
    epoch_private_key = Ed25519PrivateKey.generate()
    deployment_instance = DeploymentInstance(
        id=request.deployment_instance_id,
        deployment_profile_id=deployment_profile_id,
        instance_public_key=_public_key_b64(instance_private_key),
        non_exportable_activation_key_ref="keyring://corvus/instance/current",
        device_binding_digest="1" * 64,
        activated_at=request.evaluated_at - timedelta(minutes=5),
    )
    epoch = request.workspace_authority_epoch
    epoch_credential = AuthorityEpochCredential(
        id=request.authority_epoch_credential_id,
        workspace_id=request.workspace_id,
        authority_epoch=epoch,
        deployment_instance_id=deployment_instance.id,
        public_key=_public_key_b64(epoch_private_key),
        non_exportable_private_key_ref="keyring://corvus/epoch/current",
        device_binding_digest=deployment_instance.device_binding_digest,
        issued_at=request.evaluated_at - timedelta(minutes=4),
    )
    lease = DeploymentInstanceLease(
        workspace_id=request.workspace_id,
        authority_epoch=epoch,
        deployment_instance_id=deployment_instance.id,
        lock_name=fixed_workspace_lock_name(request.workspace_id, epoch),
        fencing_token=1,
        acquired_at=request.evaluated_at - timedelta(minutes=3),
    )
    authority = WorkspaceAuthority(
        workspace_id=request.workspace_id,
        deployment_profile_id=deployment_profile_id,
        deployment_instance_id=deployment_instance.id,
        epoch=epoch,
        authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        authority_epoch_credential_id=epoch_credential.id,
        trust_anchor_id=request.trust_anchor_id,
        active_lease_id=lease.id,
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=request.evaluated_at - timedelta(minutes=2),
    )
    commit_proof = AuthorityCommitProof(
        workspace_id=request.workspace_id,
        deployment_instance_id=deployment_instance.id,
        authority_epoch_credential_id=epoch_credential.id,
        authority_epoch=epoch,
        authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        authority_commit_receipt_id=request.authority_commit_receipt_id,
        authority_proof_digest=request.authority_proof_digest,
        finalized=True,
    )
    registry_id = uuid4()
    offline_root_private_key = Ed25519PrivateKey.generate()
    registry_verifier_private_key = Ed25519PrivateKey.generate()
    offline_root_public_keys = (_public_key_b64(offline_root_private_key),)
    offline_root_keyset_digest = authority_public_key_set_digest(offline_root_public_keys)
    assert request.registry_trust_metadata_version is not None
    assert request.registry_history_head_digest is not None
    assert request.registry_freshness_proof_id is not None
    assert request.registry_freshness_sequence is not None
    previous_registry_trust_state = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=3,
        latest_verifier_key_version=2,
        complete_history_head_digest="4" * 64,
        issued_at=request.evaluated_at - timedelta(minutes=10),
        expires_at=request.evaluated_at + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest=offline_root_keyset_digest,
    )
    registry_trust_state = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=request.registry_trust_metadata_version,
        latest_verifier_key_version=3,
        complete_history_head_digest=request.registry_history_head_digest,
        issued_at=request.evaluated_at - timedelta(minutes=5),
        expires_at=request.evaluated_at + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest=offline_root_keyset_digest,
        previous_metadata_digest=previous_registry_trust_state.canonical_digest,
    )
    registry_verifier_key = AuthorityRegistryVerifierKeyVersion(
        registry_id=registry_id,
        key_version=registry_trust_state.latest_verifier_key_version,
        public_key=_public_key_b64(registry_verifier_private_key),
        status=RegistryVerifierKeyStatus.ACTIVE,
        valid_from=request.evaluated_at - timedelta(days=1),
        threshold_attestation_digest="9" * 64,
    )
    registry_freshness_proof = AuthorityRegistryFreshnessProof(
        id=request.registry_freshness_proof_id,
        registry_id=registry_id,
        trust_state_metadata_version=registry_trust_state.metadata_version,
        complete_history_head_digest=registry_trust_state.complete_history_head_digest,
        registry_sequence=request.registry_freshness_sequence,
        challenge_nonce_digest="a" * 64,
        response_digest="b" * 64,
        issued_at=request.evaluated_at - timedelta(seconds=5),
        expires_at=request.evaluated_at + timedelta(minutes=5),
        verifier_key_version_id=registry_verifier_key.id,
        registry_signature="pending",
    )
    registry_freshness_proof = registry_freshness_proof.model_copy(
        update={
            "registry_signature": _signature_b64(
                registry_verifier_private_key,
                bytes.fromhex(registry_freshness_proof.response_digest),
            )
        }
    )
    trust_state_signatures = (
        _signature_b64(
            offline_root_private_key,
            bytes.fromhex(registry_trust_state.canonical_digest),
        ),
    )
    registry_verification_proof = RegistryVerificationProof(
        registry_id=registry_id,
        trust_state_digest=registry_trust_state.canonical_digest,
        freshness_proof_id=registry_freshness_proof.id,
        freshness_response_digest=registry_freshness_proof.response_digest,
        verifier_key_version_id=registry_verifier_key.id,
        offline_root_public_keys=offline_root_public_keys,
        trust_state_signatures=trust_state_signatures,
        signature_threshold=1,
        finalized=True,
    )
    trust_anchor = AuthorityTrustAnchor(
        id=request.trust_anchor_id,
        workspace_id=request.workspace_id,
        kind=AuthorityTrustAnchorKind.REGISTRY_GENERATION,
        anchor_registry_id=registry_id,
        pinned_registry_root_digest=offline_root_keyset_digest,
        policy_digest="d" * 64,
        created_at=request.evaluated_at - timedelta(days=1),
    )
    authority_manifest = AuthorityStateRootManifestVersion(
        id=request.authority_manifest_version_id,
        schema_version=1,
        canonicalization_version=1,
        manifest_digest=request.authority_manifest_digest,
        created_at=request.evaluated_at - timedelta(days=1),
    )
    authority_manifest_families = (
        AuthorityStateRootLeafFamily(
            manifest_version_id=authority_manifest.id,
            ordinal=1,
            family_name="workspace_memberships",
            coverage_kind=CoverageKind.IN_ROOT,
            canonicalization_version=1,
        ),
        AuthorityStateRootLeafFamily(
            manifest_version_id=authority_manifest.id,
            ordinal=2,
            family_name="access_bundles",
            coverage_kind=CoverageKind.IN_ROOT,
            canonicalization_version=1,
        ),
    )
    kill_switch_entries = tuple(
        KillSwitchSnapshotEntry(
            snapshot_id=snapshot_id,
            workspace_id=request.workspace_id,
            scope_kind=binding.scope_kind,
            scope_id=binding.scope_id,
            state="clear",
            version=1,
            updated_at=request.evaluated_at - timedelta(seconds=1),
        )
        for snapshot_id, binding in zip(
            request.kill_switch_snapshot_ids,
            request.kill_switch_scope_bindings,
            strict=True,
        )
    )
    kill_switch_verification_proof = KillSwitchVerificationProof(
        request_context_id=request.request_context_id,
        workspace_id=request.workspace_id,
        acting_agent_id=request.acting_agent_id,
        action=request.action,
        kill_switch_snapshot_ids=request.kill_switch_snapshot_ids,
        kill_switch_snapshot_digest=request.kill_switch_snapshot_digest,
        required_scope_bindings=request.kill_switch_scope_bindings,
        entries=kill_switch_entries,
        observed_at=request.evaluated_at - timedelta(seconds=1),
        expires_at=request.evaluated_at + timedelta(minutes=5),
        hierarchy_exhaustive=True,
        finalized=True,
    )
    audience_policy_snapshot = AudiencePolicySnapshot(
        id=request.audience_policy_snapshot_id,
        workspace_id=request.workspace_id,
        visibility="explicit_principals",
        principal_ids=frozenset({request.requester_id}),
        scope_digest=request.scope_digest,
        policy_version=1,
        policy_digest=request.audience_policy_digest,
        created_by=request.requester_id,
        created_at=request.evaluated_at - timedelta(minutes=1),
    )
    client_context = ClientContext(
        id=request.client_context_id,
        surface=request.client_surface,
        transport_principal_id=request.transport_principal_id,
        session_id=uuid4(),
        origin="test-client",
        issued_at=request.evaluated_at - timedelta(minutes=1),
        expires_at=request.evaluated_at + timedelta(minutes=5),
    )
    runtime_possession_proof = AuthorityRuntimePossessionProof(
        request_context_id=request.request_context_id,
        workspace_id=request.workspace_id,
        deployment_instance_id=deployment_instance.id,
        authority_epoch_credential_id=epoch_credential.id,
        authority_epoch=epoch,
        authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        device_binding_digest=deployment_instance.device_binding_digest,
        lock_name=fixed_workspace_lock_name(request.workspace_id, epoch),
        nonce_digest="f" * 64,
        issued_at=request.evaluated_at - timedelta(seconds=1),
        expires_at=request.evaluated_at + timedelta(seconds=15),
        deployment_instance_signature="pending",
        epoch_credential_signature="pending",
    )
    runtime_message = bytes.fromhex(authority_runtime_possession_digest(runtime_possession_proof))
    runtime_possession_proof = runtime_possession_proof.model_copy(
        update={
            "deployment_instance_signature": _signature_b64(
                instance_private_key,
                runtime_message,
            ),
            "epoch_credential_signature": _signature_b64(
                epoch_private_key,
                runtime_message,
            ),
        }
    )
    return AuthorityEvaluationContext(
        deployment_instance=deployment_instance,
        workspace_authority=authority,
        epoch_credential=epoch_credential,
        active_lease=lease,
        commit_proof=commit_proof,
        trust_anchor=trust_anchor,
        previous_registry_trust_state=previous_registry_trust_state,
        registry_trust_state=registry_trust_state,
        registry_freshness_proof=registry_freshness_proof,
        registry_verifier_key=registry_verifier_key,
        registry_verification_proof=registry_verification_proof,
        minimum_registry_sequence=9,
        expected_registry_nonce_digest="a" * 64,
        authority_manifest=authority_manifest,
        authority_manifest_families=authority_manifest_families,
        mutable_authority_families=frozenset({"workspace_memberships", "access_bundles"}),
        kill_switch_verification_proof=kill_switch_verification_proof,
        audience_policy_snapshot=audience_policy_snapshot,
        requester_role_ids=frozenset(),
        client_context=client_context,
        enabled_client_surfaces=frozenset(ClientSurface),
        runtime_possession_proof=runtime_possession_proof,
    )


def test_exact_requester_and_agent_grants_allow() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"
    assert result.actions == frozenset({"project.read"})


def test_project_authorization_adapter_uses_real_intersection_and_rechecks_current_state() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    authority_context = _valid_authority_context(request)
    assert authority_context.deployment_instance is not None
    context = RequestContext(
        id=request.request_context_id,
        deployment_profile_id=authority_context.deployment_instance.deployment_profile_id,
        deployment_instance_id=request.deployment_instance_id,
        workspace_id=request.workspace_id,
        workspace_authority_epoch=request.workspace_authority_epoch,
        workspace_authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        authority_epoch_credential_id=request.authority_epoch_credential_id,
        authority_commit_receipt_id=request.authority_commit_receipt_id,
        authority_proof_digest=request.authority_proof_digest,
        scope_kind=request.scope_kind,
        scope_id=request.scope_id,
        scope_digest=request.scope_digest,
        audience_policy_snapshot_id=request.audience_policy_snapshot_id,
        audience_policy_digest=request.audience_policy_digest,
        requester_id=request.requester_id,
        client_context_id=request.client_context_id,
        transport_principal_id=request.transport_principal_id,
        agent_id=request.acting_agent_id,
        agent_grant_id=agent_grant.id,
        access_bundle_id=requester_bundle.id,
        policy_digest=requester_bundle.policy_digest,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="1" * 64,
        authorization_signing_key_version_id=uuid4(),
        idempotency_key="project-authorization-adapter",
        correlation_id=uuid4(),
    )
    project_request = ProjectAuthorizationRequest(
        context=context,
        client_surface=request.client_surface,
        action="project.read",
        project_id=request.resource_id,
    )
    resolved = ProjectAuthorizationInputs(
        request=request,
        authority_context=authority_context,
        requester_bundle=requester_bundle,
        requester_grants=(requester_grant,),
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=(agent_capability,),
    )

    class InputsProvider:
        def __init__(self, value: ProjectAuthorizationInputs) -> None:
            self.value = value

        def resolve(self, received: ProjectAuthorizationRequest) -> ProjectAuthorizationInputs:
            assert received == project_request
            return self.value

    provider = InputsProvider(resolved)
    adapter = EvaluatingProjectAuthorizationAdapter(inputs=provider)

    allowed = adapter.authorize(project_request)
    provider.value = resolved.model_copy(
        update={
            "requester_bundle": requester_bundle.model_copy(
                update={"revoked_at": request.evaluated_at}
            )
        }
    )
    revoked = adapter.authorize(project_request)

    assert allowed.allowed is True
    assert allowed.reason_code == "exact_capability_intersection"
    assert allowed.authorization_snapshot_id == context.authorization_snapshot_id
    assert revoked.allowed is False
    assert revoked.reason_code == "requester_grant_revoked"


def test_missing_requester_grant_denies_with_reason() -> None:
    request, requester_bundle, _, agent_grant, agent_bundle, agent_capability = _exact_allow_case()

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "no_requester_grant"
    assert result.actions == frozenset()


def test_explicit_deny_overrides_matching_allows() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    requester_deny = CapabilityGrant(
        bundle_id=requester_bundle.id,
        workspace_id=request.workspace_id,
        resource_kind=request.resource_kind,
        resource_id=request.resource_id,
        action=request.action,
        effect=CapabilityEffect.DENY,
    )

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant, requester_deny],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "explicit_deny"
    assert result.actions == frozenset()


def test_missing_agent_grant_denies_without_inheriting_requester_authority() -> None:
    request, requester_bundle, requester_grant, _, agent_bundle, agent_capability = (
        _exact_allow_case()
    )

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=None,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "no_agent_grant"
    assert result.actions == frozenset()


def test_cross_workspace_agent_grant_denies_before_capability_matching() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    foreign_workspace_id = uuid4()
    foreign_bundle = agent_bundle.model_copy(update={"workspace_id": foreign_workspace_id})
    foreign_agent_grant = agent_grant.model_copy(update={"workspace_id": foreign_workspace_id})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=foreign_agent_grant,
        agent_bundle=foreign_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "cross_workspace_grant"
    assert result.actions == frozenset()


def test_requester_bundle_expiry_has_zero_grace() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    expired_bundle = requester_bundle.model_copy(update={"expires_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=expired_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "requester_grant_expired"
    assert result.actions == frozenset()


def test_project_scoped_bundle_cannot_broaden_to_another_project() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    other_project_id = uuid4()
    requester_bundle = requester_bundle.model_copy(update={"scope_id": other_project_id})
    agent_bundle = agent_bundle.model_copy(update={"scope_id": other_project_id})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "scope_mismatch"
    assert result.actions == frozenset()


def test_requester_bundle_revocation_fails_closed_with_reason() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    revoked_bundle = requester_bundle.model_copy(update={"revoked_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=revoked_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "requester_grant_revoked"
    assert result.actions == frozenset()


def test_agent_bundle_expiry_has_zero_grace() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    expired_bundle = agent_bundle.model_copy(update={"expires_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=expired_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "agent_bundle_expired"
    assert result.actions == frozenset()


def test_agent_bundle_revocation_fails_closed_with_reason() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    revoked_bundle = agent_bundle.model_copy(update={"revoked_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=revoked_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "agent_bundle_revoked"
    assert result.actions == frozenset()


def test_agent_grant_expiry_has_zero_grace() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    expired_grant = agent_grant.model_copy(update={"expires_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=expired_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "agent_grant_expired"
    assert result.actions == frozenset()


def test_agent_grant_revocation_fails_closed_with_reason() -> None:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    revoked_grant = agent_grant.model_copy(update={"revoked_at": request.evaluated_at})

    result = evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=revoked_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "agent_grant_revoked"
    assert result.actions == frozenset()


def _delegated_allow_case() -> tuple[
    AuthorizationRequest,
    AccessBundle,
    CapabilityGrant,
    AgentGrant,
    AccessBundle,
    CapabilityGrant,
    DelegationGrant,
]:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = (
        _exact_allow_case()
    )
    child_agent_id = uuid4()
    child_kill_switch_bindings = (
        request.kill_switch_scope_bindings[0],
        KillSwitchScopeBinding(scope_kind="agent", scope_id=child_agent_id),
    )
    delegated_request = request.model_copy(
        update={
            "acting_agent_id": child_agent_id,
            "kill_switch_scope_bindings": child_kill_switch_bindings,
        }
    )
    delegation = DelegationGrant(
        parent_agent_grant_id=agent_grant.id,
        child_agent_id=child_agent_id,
        capabilities=frozenset({request.action}),
        budget_json={"max_cost_usd": 1.0},
        depth_limit=1,
        issued_at=request.evaluated_at - timedelta(minutes=1),
        expires_at=request.evaluated_at + timedelta(minutes=30),
    )
    return (
        delegated_request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    )


def _evaluate_delegated_case(
    *,
    request: AuthorizationRequest,
    requester_bundle: AccessBundle,
    requester_grant: CapabilityGrant,
    agent_grant: AgentGrant,
    agent_bundle: AccessBundle,
    agent_capability: CapabilityGrant,
    delegation_grants: list[DelegationGrant],
) -> AuthorizationResult:
    return evaluate_capability_intersection(
        request,
        authority_context=_valid_authority_context(request),
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=delegation_grants,
    )


def test_exact_one_hop_delegation_allows_only_the_delegated_action() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "delegated_capability_intersection"
    assert result.actions == frozenset({request.action})


def test_delegation_rejects_parent_grant_substitution() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"parent_agent_grant_id": uuid4()})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_parent_mismatch"


def test_delegation_rejects_child_agent_substitution() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"child_agent_id": uuid4()})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_child_mismatch"


def test_delegation_cannot_broaden_parent_capabilities() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"capabilities": frozenset({"project.write"})})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_overreach"


def test_delegation_expiry_has_zero_grace() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"expires_at": request.evaluated_at})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_expired"


def test_revoked_delegation_fails_closed() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"revoked_at": request.evaluated_at})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_revoked"


def test_delegation_cannot_be_used_before_issuance() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(
        update={"issued_at": request.evaluated_at + timedelta(seconds=1)}
    )

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_not_yet_active"


def test_zero_depth_delegation_cannot_authorize_a_child() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    delegation = delegation.model_copy(update={"depth_limit": 0})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_depth_exceeded"


def test_unlinked_multi_hop_delegation_chain_fails_closed() -> None:
    (
        request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
        delegation,
    ) = _delegated_allow_case()
    second_delegation = delegation.model_copy(update={"id": uuid4()})

    result = _evaluate_delegated_case(
        request=request,
        requester_bundle=requester_bundle,
        requester_grant=requester_grant,
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capability=agent_capability,
        delegation_grants=[delegation, second_delegation],
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "delegation_chain_unverifiable"


def _evaluate_direct_case(
    case: tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ],
    authority_context: AuthorityEvaluationContext | None,
) -> AuthorizationResult:
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = case
    return evaluate_capability_intersection(
        request,
        authority_context=authority_context,
        requester_bundle=requester_bundle,
        requester_grants=[requester_grant],
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=[agent_capability],
        delegation_grants=[],
    )


def test_missing_authority_context_fails_closed() -> None:
    result = _evaluate_direct_case(_exact_allow_case(), None)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_context_missing"


def test_missing_deployment_instance_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"deployment_instance": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "deployment_instance_missing"


def test_missing_deployment_instance_key_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.runtime_possession_proof is not None
    proof = context.runtime_possession_proof.model_copy(
        update={"deployment_instance_signature": "invalid"}
    )
    context = context.model_copy(update={"runtime_possession_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "deployment_instance_key_unavailable"


def test_missing_epoch_credential_key_binding_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.runtime_possession_proof is not None
    proof = context.runtime_possession_proof.model_copy(
        update={"epoch_credential_signature": "invalid"}
    )
    context = context.model_copy(update={"runtime_possession_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_epoch_key_unavailable"


def test_missing_os_lock_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.runtime_possession_proof is not None
    proof = context.runtime_possession_proof.model_copy(update={"lock_name": "wrong-lock"})
    context = context.model_copy(update={"runtime_possession_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "workspace_os_lock_not_held"


def test_missing_runtime_possession_proof_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"runtime_possession_proof": None}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_runtime_possession_proof_missing"


def test_missing_authority_lease_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"active_lease": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_lease_missing"


def test_released_authority_lease_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.active_lease is not None
    released_lease = context.active_lease.model_copy(update={"released_at": case[0].evaluated_at})
    context = context.model_copy(update={"active_lease": released_lease})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_lease_released"


def test_wrong_deployment_instance_binding_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    authority = context.workspace_authority.model_copy(update={"deployment_instance_id": uuid4()})
    context = context.model_copy(update={"workspace_authority": authority})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "deployment_instance_mismatch"


def test_restore_quarantine_cannot_authorize_mutation() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    authority = context.workspace_authority.model_copy(
        update={"state": WorkspaceAuthorityState.RESTORE_QUARANTINE}
    )
    context = context.model_copy(update={"workspace_authority": authority})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "restore_quarantine"


def test_revoked_epoch_credential_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.epoch_credential is not None
    credential = context.epoch_credential.model_copy(
        update={
            "status": AuthorityEpochCredentialStatus.REVOKED,
            "revoked_at": case[0].evaluated_at,
        }
    )
    context = context.model_copy(update={"epoch_credential": credential})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_epoch_credential_revoked"


def test_missing_authority_commit_proof_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"commit_proof": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_commit_proof_missing"


def test_non_finalized_authority_commit_proof_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(update={"finalized": False})
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_commit_not_finalized"


def test_stale_external_authority_generation_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(
        update={"authority_generation": case[0].workspace_authority_generation - 1}
    )
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "stale_authority_generation"


def test_external_authority_state_root_mismatch_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(update={"authority_state_root": "9" * 64})
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_state_root_mismatch"


def test_authority_commit_receipt_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(update={"authority_commit_receipt_id": uuid4()})
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_commit_receipt_mismatch"


def test_authority_proof_deployment_instance_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(update={"deployment_instance_id": uuid4()})
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_proof_instance_mismatch"


def test_authority_proof_digest_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.commit_proof is not None
    proof = context.commit_proof.model_copy(update={"authority_proof_digest": "8" * 64})
    context = context.model_copy(update={"commit_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_proof_digest_mismatch"


def test_missing_trust_anchor_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"trust_anchor": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_trust_anchor_missing"


def test_stale_registry_trust_state_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_trust_state is not None
    stale = context.registry_trust_state.model_copy(update={"metadata_version": 3})
    context = context.model_copy(update={"registry_trust_state": stale})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "stale_registry_trust_state"


def test_expired_registry_trust_state_has_zero_grace() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_trust_state is not None
    expired = context.registry_trust_state.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"registry_trust_state": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_trust_state_expired"


def test_registry_trust_state_prefix_replay_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_trust_state is not None
    replayed = context.registry_trust_state.model_copy(
        update={"previous_metadata_digest": "f" * 64}
    )
    context = context.model_copy(update={"registry_trust_state": replayed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_metadata_prefix_mismatch"


def test_stale_registry_freshness_proof_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_freshness_proof is not None
    stale = context.registry_freshness_proof.model_copy(update={"trust_state_metadata_version": 3})
    context = context.model_copy(update={"registry_freshness_proof": stale})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "stale_registry_freshness_proof"


def test_expired_registry_freshness_proof_has_zero_grace() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_freshness_proof is not None
    expired = context.registry_freshness_proof.model_copy(
        update={"expires_at": case[0].evaluated_at}
    )
    context = context.model_copy(update={"registry_freshness_proof": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_freshness_proof_expired"


def test_registry_freshness_sequence_replay_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"minimum_registry_sequence": 10})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_sequence_replay"


def test_registry_verifier_rollback_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_verifier_key is not None
    rolled_back = context.registry_verifier_key.model_copy(update={"key_version": 2})
    context = context.model_copy(update={"registry_verifier_key": rolled_back})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_verifier_version_rollback"


def test_revoked_registry_verifier_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_verifier_key is not None
    revoked = context.registry_verifier_key.model_copy(
        update={
            "status": RegistryVerifierKeyStatus.REVOKED,
            "revoked_at": case[0].evaluated_at,
        }
    )
    context = context.model_copy(update={"registry_verifier_key": revoked})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_verifier_revoked_at_verification_time"


def test_compromised_registry_verifier_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_verifier_key is not None
    compromised = context.registry_verifier_key.model_copy(
        update={
            "status": RegistryVerifierKeyStatus.COMPROMISED,
            "compromise_effective_at": case[0].evaluated_at,
        }
    )
    context = context.model_copy(update={"registry_verifier_key": compromised})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_verifier_compromised_at_verification_time"


def test_missing_registry_signature_verification_proof_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"registry_verification_proof": None}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_verification_proof_missing"


def test_unverified_registry_trust_state_signatures_fail_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_verification_proof is not None
    proof = context.registry_verification_proof.model_copy(
        update={"trust_state_signatures": ("invalid",)}
    )
    context = context.model_copy(update={"registry_verification_proof": proof})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_trust_signatures_unverified"


def test_registry_threshold_cannot_be_lowered_by_the_proof() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_verification_proof is not None
    assert context.registry_trust_state is not None
    assert context.trust_anchor is not None
    private_keys = tuple(Ed25519PrivateKey.generate() for _ in range(3))
    public_keys = tuple(_public_key_b64(key) for key in private_keys)
    keyset_digest = authority_public_key_set_digest(public_keys)
    trust_state = context.registry_trust_state.model_copy(
        update={"threshold_signature_set_digest": keyset_digest}
    )
    invalid_signature = base64.b64encode(b"x" * 64).decode("ascii")
    proof = context.registry_verification_proof.model_copy(
        update={
            "trust_state_digest": trust_state.canonical_digest,
            "offline_root_public_keys": public_keys,
            "trust_state_signatures": (
                _signature_b64(private_keys[0], bytes.fromhex(trust_state.canonical_digest)),
                invalid_signature,
                invalid_signature,
            ),
            "signature_threshold": 1,
        }
    )
    context = context.model_copy(
        update={
            "registry_trust_state": trust_state,
            "registry_verification_proof": proof,
            "trust_anchor": context.trust_anchor.model_copy(
                update={"pinned_registry_root_digest": keyset_digest}
            ),
        }
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_trust_signature_set_invalid"


def test_unverified_registry_freshness_signature_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.registry_freshness_proof is not None
    freshness = context.registry_freshness_proof.model_copy(
        update={"registry_signature": "invalid"}
    )
    context = context.model_copy(update={"registry_freshness_proof": freshness})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "registry_freshness_signature_unverified"


def test_missing_authority_manifest_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(update={"authority_manifest": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_manifest_missing"


def test_authority_manifest_digest_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    assert context.authority_manifest is not None
    substituted = context.authority_manifest.model_copy(update={"manifest_digest": "f" * 64})
    context = context.model_copy(update={"authority_manifest": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authority_manifest_mismatch"


def test_unlisted_mutable_authority_family_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"authority_manifest_families": ()}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "unlisted_authority_family"


def _placement_bound_case() -> tuple[
    tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ],
    AuthorityEvaluationContext,
    ExecutionPlacement,
]:
    case = _exact_allow_case()
    placement = ExecutionPlacement(
        kind=ExecutionKind.LOCAL_RUNNER,
        runner_id=uuid4(),
        sandbox_profile="strict-build",
        data_policy_digest="e" * 64,
        created_at=case[0].evaluated_at - timedelta(minutes=1),
    )
    request = case[0].model_copy(update={"execution_placement_id": placement.id})
    bound_case = (
        request,
        case[1],
        case[2],
        case[3],
        case[4],
        case[5],
    )
    context = _valid_authority_context(request).model_copy(
        update={"execution_placement": placement}
    )
    return bound_case, context, placement


def test_exact_active_execution_placement_allows() -> None:
    case, context, _ = _placement_bound_case()

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def test_missing_execution_placement_fails_closed() -> None:
    case, context, _ = _placement_bound_case()
    context = context.model_copy(update={"execution_placement": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_missing"


def test_execution_placement_substitution_fails_closed() -> None:
    case, context, placement = _placement_bound_case()
    substituted = placement.model_copy(update={"id": uuid4()})
    context = context.model_copy(update={"execution_placement": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_mismatch"


def test_unavailable_execution_placement_fails_closed() -> None:
    case, context, placement = _placement_bound_case()
    unavailable = placement.model_copy(update={"status": ExecutionStatus.UNAVAILABLE})
    context = context.model_copy(update={"execution_placement": unavailable})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_unavailable"


def test_revoked_execution_placement_fails_closed() -> None:
    case, context, placement = _placement_bound_case()
    revoked = placement.model_copy(update={"status": ExecutionStatus.REVOKED})
    context = context.model_copy(update={"execution_placement": revoked})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_revoked"


def test_future_execution_placement_fails_closed() -> None:
    case, context, placement = _placement_bound_case()
    future = placement.model_copy(
        update={"created_at": case[0].evaluated_at + timedelta(seconds=1)}
    )
    context = context.model_copy(update={"execution_placement": future})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_not_yet_active"


def test_unsolicited_execution_placement_fails_closed() -> None:
    case = _exact_allow_case()
    placement = ExecutionPlacement(
        kind=ExecutionKind.LOCAL_RUNNER,
        runner_id=uuid4(),
        sandbox_profile="strict-build",
        data_policy_digest="e" * 64,
        created_at=case[0].evaluated_at - timedelta(minutes=1),
    )
    context = _valid_authority_context(case[0]).model_copy(
        update={"execution_placement": placement}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "execution_placement_unsolicited"


def _credential_bound_case() -> tuple[
    tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ],
    AuthorityEvaluationContext,
    CredentialRef,
    CredentialVerificationProof,
]:
    case, context, placement = _placement_bound_case()
    request_context_id = uuid4()
    provider_connection_id = uuid4()
    credential_ref_id = uuid4()
    credential_version_id = uuid4()
    credential_grant_id = uuid4()
    request = case[0].model_copy(
        update={
            "request_context_id": request_context_id,
            "provider_connection_id": provider_connection_id,
            "credential_ref_id": credential_ref_id,
            "credential_version_id": credential_version_id,
            "credential_grant_id": credential_grant_id,
        }
    )
    bound_case = (request, case[1], case[2], case[3], case[4], case[5])
    context = _valid_authority_context(request).model_copy(
        update={"execution_placement": placement}
    )
    credential_ref = CredentialRef(
        id=credential_ref_id,
        workspace_id=request.workspace_id,
        owner_principal_id=request.requester_id,
        provider_connection_id=provider_connection_id,
        kind=CredentialKind.OS_KEYRING,
        opaque_locator="keyring://corvus/provider/current",
        scopes=frozenset({"model.invoke"}),
        expires_at=request.evaluated_at + timedelta(hours=1),
        version=3,
        created_at=request.evaluated_at - timedelta(hours=1),
        updated_at=request.evaluated_at - timedelta(minutes=1),
    )
    proof = CredentialVerificationProof(
        request_context_id=request.request_context_id,
        workspace_id=request.workspace_id,
        provider_connection_id=provider_connection_id,
        credential_ref_id=credential_ref_id,
        credential_ref_version=credential_ref.version,
        credential_version_id=credential_version_id,
        credential_grant_id=credential_grant_id,
        acting_agent_id=request.acting_agent_id,
        execution_placement_id=placement.id,
        operation=request.action,
        rotation_epoch=4,
        nonce_digest="f" * 64,
        use_limit=2,
        use_count=0,
        issued_at=request.evaluated_at - timedelta(minutes=1),
        expires_at=request.evaluated_at + timedelta(minutes=5),
        credential_version_active=True,
        credential_grant_active=True,
        finalized=True,
    )
    context = context.model_copy(
        update={
            "credential_ref": credential_ref,
            "credential_verification_proof": proof,
            "expected_credential_rotation_epoch": proof.rotation_epoch,
            "expected_credential_nonce_digest": proof.nonce_digest,
        }
    )
    return bound_case, context, credential_ref, proof


def test_exact_current_credential_binding_allows() -> None:
    case, context, _, _ = _credential_bound_case()

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def test_credential_evidence_receipt_is_canonical_and_semantically_bound() -> None:
    case, context, credential_ref, proof = _credential_bound_case()
    request = case[0]
    offset = timezone(timedelta(hours=8))
    equivalent_context = context.model_copy(
        update={
            "credential_ref": credential_ref.model_copy(
                update={
                    "created_at": credential_ref.created_at.astimezone(offset),
                    "updated_at": credential_ref.updated_at.astimezone(offset),
                    "expires_at": credential_ref.expires_at.astimezone(offset),
                }
            ),
            "credential_verification_proof": proof.model_copy(
                update={
                    "issued_at": proof.issued_at.astimezone(offset),
                    "expires_at": proof.expires_at.astimezone(offset),
                }
            ),
        }
    )

    first = canonical_credential_evidence_receipt(request, context)
    second = canonical_credential_evidence_receipt(request, equivalent_context)

    assert first == second
    assert first is not None
    with pytest.raises(ValueError, match="credential_evidence_mismatch"):
        canonical_credential_evidence_receipt(
            request.model_copy(update={"credential_grant_id": uuid4()}),
            context,
        )


def test_partial_credential_claims_fail_closed() -> None:
    case, context, _, _ = _credential_bound_case()
    request = case[0].model_copy(update={"credential_version_id": None})
    case = (request, case[1], case[2], case[3], case[4], case[5])

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_claims_incomplete"


def test_credential_without_request_placement_fails_closed() -> None:
    case, context, _, _ = _credential_bound_case()
    request = case[0].model_copy(update={"execution_placement_id": None})
    case = (request, case[1], case[2], case[3], case[4], case[5])
    context = context.model_copy(update={"execution_placement": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_placement_missing"


def test_missing_credential_ref_fails_closed() -> None:
    case, context, _, _ = _credential_bound_case()
    context = context.model_copy(update={"credential_ref": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_ref_missing"


def test_provider_connection_substitution_fails_closed() -> None:
    case, context, credential_ref, _ = _credential_bound_case()
    substituted = credential_ref.model_copy(update={"provider_connection_id": uuid4()})
    context = context.model_copy(update={"credential_ref": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_ref_mismatch"


def test_revoked_credential_ref_fails_closed() -> None:
    case, context, credential_ref, _ = _credential_bound_case()
    revoked = credential_ref.model_copy(update={"status": CredentialStatus.REVOKED})
    context = context.model_copy(update={"credential_ref": revoked})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_ref_revoked"


def test_expired_credential_ref_fails_closed() -> None:
    case, context, credential_ref, _ = _credential_bound_case()
    expired = credential_ref.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"credential_ref": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_ref_expired"


def test_missing_credential_verification_proof_fails_closed() -> None:
    case, context, _, _ = _credential_bound_case()
    context = context.model_copy(update={"credential_verification_proof": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_missing"


def test_credential_proof_request_substitution_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    substituted = proof.model_copy(update={"request_context_id": uuid4()})
    context = context.model_copy(update={"credential_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_mismatch"


def test_credential_proof_agent_substitution_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    substituted = proof.model_copy(update={"acting_agent_id": uuid4()})
    context = context.model_copy(update={"credential_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_mismatch"


def test_credential_proof_placement_substitution_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    substituted = proof.model_copy(update={"execution_placement_id": uuid4()})
    context = context.model_copy(update={"credential_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_mismatch"


def test_credential_proof_operation_overreach_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    substituted = proof.model_copy(update={"operation": "project.write"})
    context = context.model_copy(update={"credential_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_mismatch"


def test_exhausted_credential_grant_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    exhausted = proof.model_copy(update={"use_count": proof.use_limit})
    context = context.model_copy(update={"credential_verification_proof": exhausted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_grant_use_limit_exhausted"


def test_expired_credential_verification_proof_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    expired = proof.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"credential_verification_proof": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_proof_expired"


def test_inactive_credential_version_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    inactive = proof.model_copy(update={"credential_version_active": False})
    context = context.model_copy(update={"credential_verification_proof": inactive})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_version_inactive"


def test_inactive_credential_grant_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    inactive = proof.model_copy(update={"credential_grant_active": False})
    context = context.model_copy(update={"credential_verification_proof": inactive})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_grant_inactive"


def test_unfinalized_credential_verification_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    unfinalized = proof.model_copy(update={"finalized": False})
    context = context.model_copy(update={"credential_verification_proof": unfinalized})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_verification_not_finalized"


def test_credential_owner_substitution_fails_closed() -> None:
    case, context, credential_ref, _ = _credential_bound_case()
    substituted = credential_ref.model_copy(update={"owner_principal_id": uuid4()})
    context = context.model_copy(update={"credential_ref": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_owner_mismatch"


def test_future_credential_ref_fails_closed() -> None:
    case, context, credential_ref, _ = _credential_bound_case()
    future = credential_ref.model_copy(
        update={"updated_at": case[0].evaluated_at + timedelta(seconds=1)}
    )
    context = context.model_copy(update={"credential_ref": future})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_ref_not_yet_current"


def test_credential_rotation_epoch_mismatch_fails_closed() -> None:
    case, context, _, proof = _credential_bound_case()
    context = context.model_copy(
        update={"expected_credential_rotation_epoch": proof.rotation_epoch + 1}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_rotation_epoch_mismatch"


def test_credential_nonce_mismatch_fails_closed() -> None:
    case, context, _, _ = _credential_bound_case()
    context = context.model_copy(update={"expected_credential_nonce_digest": "0" * 64})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_nonce_mismatch"


def test_unsolicited_credential_evidence_fails_closed() -> None:
    _, _, credential_ref, proof = _credential_bound_case()
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={
            "credential_ref": credential_ref,
            "credential_verification_proof": proof,
            "expected_credential_rotation_epoch": proof.rotation_epoch,
            "expected_credential_nonce_digest": proof.nonce_digest,
        }
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "credential_evidence_unsolicited"


def _budget_bound_case() -> tuple[
    tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ],
    AuthorityEvaluationContext,
    BudgetRuntimeVerificationProof,
]:
    case = _exact_allow_case()
    snapshot_ids = (uuid4(), uuid4())
    budget_snapshot_digest = "1" * 64
    runtime_limit_digest = "2" * 64
    budget_unit = "micro_usd"
    budget_requested_amount = 5
    request = case[0].model_copy(
        update={
            "budget_snapshot_ids": snapshot_ids,
            "budget_snapshot_digest": budget_snapshot_digest,
            "runtime_limit_digest": runtime_limit_digest,
            "budget_unit": budget_unit,
            "budget_requested_amount": budget_requested_amount,
        }
    )
    bound_case = (request, case[1], case[2], case[3], case[4], case[5])
    account_ids = (uuid4(), uuid4(), uuid4())
    proof = BudgetRuntimeVerificationProof(
        request_context_id=request.request_context_id,
        workspace_id=request.workspace_id,
        scope_kind=request.scope_kind,
        scope_id=request.scope_id,
        action=request.action,
        budget_snapshot_ids=snapshot_ids,
        budget_snapshot_digest=budget_snapshot_digest,
        runtime_limit_digest=runtime_limit_digest,
        unit=budget_unit,
        requested_amount=budget_requested_amount,
        canonical_account_ids=account_ids,
        canonical_account_closure_digest="3" * 64,
        expected_account_count=len(account_ids),
        minimum_remaining_amount=10,
        runtime_limit_ms=60_000,
        runtime_consumed_ms=1_000,
        period_starts_at=request.evaluated_at - timedelta(hours=1),
        period_ends_at=request.evaluated_at + timedelta(hours=1),
        observed_at=request.evaluated_at - timedelta(seconds=1),
        expires_at=request.evaluated_at + timedelta(minutes=5),
        hierarchy_verified=True,
        period_non_overlapping_verified=True,
        all_accounts_active=True,
        finalized=True,
    )
    context = _valid_authority_context(request).model_copy(
        update={"budget_runtime_verification_proof": proof}
    )
    return bound_case, context, proof


def test_exact_budget_and_runtime_evidence_allows() -> None:
    case, context, _ = _budget_bound_case()

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def test_budget_evidence_receipt_is_canonical_and_semantically_bound() -> None:
    case, context, proof = _budget_bound_case()
    request = case[0]
    offset = timezone(timedelta(hours=8))
    equivalent_context = context.model_copy(
        update={
            "budget_runtime_verification_proof": proof.model_copy(
                update={
                    "period_starts_at": proof.period_starts_at.astimezone(offset),
                    "period_ends_at": proof.period_ends_at.astimezone(offset),
                    "observed_at": proof.observed_at.astimezone(offset),
                    "expires_at": proof.expires_at.astimezone(offset),
                }
            )
        }
    )

    first = canonical_budget_evidence_receipt(request, context)
    second = canonical_budget_evidence_receipt(request, equivalent_context)

    assert first == second
    assert first is not None
    with pytest.raises(ValueError, match="budget_evidence_mismatch"):
        canonical_budget_evidence_receipt(
            request.model_copy(update={"budget_requested_amount": 999}),
            context,
        )


def test_partial_budget_claims_fail_closed() -> None:
    case, context, _ = _budget_bound_case()
    request = case[0].model_copy(update={"runtime_limit_digest": None})
    case = (request, case[1], case[2], case[3], case[4], case[5])

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_claims_incomplete"


def test_missing_budget_runtime_proof_fails_closed() -> None:
    case, context, _ = _budget_bound_case()
    context = context.model_copy(update={"budget_runtime_verification_proof": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_verification_proof_missing"


def test_budget_snapshot_substitution_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    substituted = proof.model_copy(update={"budget_snapshot_digest": "4" * 64})
    context = context.model_copy(update={"budget_runtime_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_verification_proof_mismatch"


def test_unfinalized_budget_verification_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    unfinalized = proof.model_copy(update={"finalized": False})
    context = context.model_copy(update={"budget_runtime_verification_proof": unfinalized})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_verification_not_finalized"


def test_unverified_budget_hierarchy_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    unverified = proof.model_copy(update={"hierarchy_verified": False})
    context = context.model_copy(update={"budget_runtime_verification_proof": unverified})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_hierarchy_unverified"


def test_overlapping_budget_period_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    unverified = proof.model_copy(update={"period_non_overlapping_verified": False})
    context = context.model_copy(update={"budget_runtime_verification_proof": unverified})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_period_overlap_unverified"


def test_inactive_budget_account_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    inactive = proof.model_copy(update={"all_accounts_active": False})
    context = context.model_copy(update={"budget_runtime_verification_proof": inactive})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_account_inactive"


def test_incomplete_budget_account_closure_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    incomplete = proof.model_copy(
        update={"expected_account_count": proof.expected_account_count + 1}
    )
    context = context.model_copy(update={"budget_runtime_verification_proof": incomplete})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_account_closure_incomplete"


def test_duplicate_budget_accounts_fail_closed() -> None:
    case, context, proof = _budget_bound_case()
    duplicated = proof.model_copy(
        update={"canonical_account_ids": (proof.canonical_account_ids[0],) * 3}
    )
    context = context.model_copy(update={"budget_runtime_verification_proof": duplicated})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_account_closure_ambiguous"


def test_expired_budget_runtime_proof_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    expired = proof.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"budget_runtime_verification_proof": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_verification_proof_expired"


def test_inactive_budget_period_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    inactive = proof.model_copy(update={"period_ends_at": case[0].evaluated_at})
    context = context.model_copy(update={"budget_runtime_verification_proof": inactive})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_period_inactive"


def test_budget_exhaustion_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    exhausted = proof.model_copy(update={"minimum_remaining_amount": 4})
    context = context.model_copy(update={"budget_runtime_verification_proof": exhausted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_exhausted"


def test_runtime_exhaustion_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    exhausted = proof.model_copy(update={"runtime_consumed_ms": proof.runtime_limit_ms})
    context = context.model_copy(update={"budget_runtime_verification_proof": exhausted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "runtime_exhausted"


def test_agent_run_cancellation_ignores_exhausted_budget_runtime() -> None:
    case, context, proof = _budget_bound_case()
    request = case[0].model_copy(
        update={"resource_kind": "agent_run", "action": AgentRunOperation.CANCEL.value}
    )
    requester_grant = case[2].model_copy(
        update={
            "resource_kind": request.resource_kind,
            "resource_id": request.resource_id,
            "action": request.action,
        }
    )
    agent_capability = case[5].model_copy(
        update={
            "resource_kind": request.resource_kind,
            "resource_id": request.resource_id,
            "action": request.action,
        }
    )
    cancellation_case = (
        request,
        case[1],
        requester_grant,
        case[3],
        case[4],
        agent_capability,
    )
    exhausted = proof.model_copy(
        update={
            "action": request.action,
            "runtime_consumed_ms": proof.runtime_limit_ms,
        }
    )
    assert context.kill_switch_verification_proof is not None
    cancellation_context = context.model_copy(
        update={
            "budget_runtime_verification_proof": exhausted,
            "kill_switch_verification_proof": context.kill_switch_verification_proof.model_copy(
                update={"action": request.action}
            ),
        }
    )

    result = _evaluate_direct_case(cancellation_case, cancellation_context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def test_unsolicited_budget_evidence_fails_closed() -> None:
    _, _, proof = _budget_bound_case()
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"budget_runtime_verification_proof": proof}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_evidence_unsolicited"


def test_duplicate_budget_snapshot_claims_fail_closed() -> None:
    case, context, proof = _budget_bound_case()
    duplicate_id = case[0].budget_snapshot_ids[0]
    request = case[0].model_copy(update={"budget_snapshot_ids": (duplicate_id, duplicate_id)})
    case = (request, case[1], case[2], case[3], case[4], case[5])
    duplicated = proof.model_copy(update={"budget_snapshot_ids": request.budget_snapshot_ids})
    context = context.model_copy(update={"budget_runtime_verification_proof": duplicated})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_snapshot_set_ambiguous"


def test_future_budget_runtime_proof_fails_closed() -> None:
    case, context, proof = _budget_bound_case()
    future = proof.model_copy(update={"observed_at": case[0].evaluated_at + timedelta(seconds=1)})
    context = context.model_copy(update={"budget_runtime_verification_proof": future})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "budget_runtime_verification_proof_not_yet_valid"


def _kill_switch_case() -> tuple[
    tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ],
    AuthorityEvaluationContext,
    KillSwitchVerificationProof,
]:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    proof = context.kill_switch_verification_proof
    assert proof is not None
    return case, context, proof


def test_missing_kill_switch_proof_fails_closed() -> None:
    case, context, _ = _kill_switch_case()
    context = context.model_copy(update={"kill_switch_verification_proof": None})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_verification_proof_missing"


def test_incomplete_kill_switch_claims_fail_closed() -> None:
    case, context, _ = _kill_switch_case()
    request = case[0].model_copy(update={"kill_switch_snapshot_ids": ()})
    case = (request, case[1], case[2], case[3], case[4], case[5])

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_claims_incomplete"


def test_kill_switch_snapshot_substitution_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    substituted = proof.model_copy(update={"kill_switch_snapshot_digest": "f" * 64})
    context = context.model_copy(update={"kill_switch_verification_proof": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_verification_proof_mismatch"


def test_missing_workspace_kill_switch_scope_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    agent_binding = case[0].kill_switch_scope_bindings[1]
    agent_snapshot_id = case[0].kill_switch_snapshot_ids[1]
    request = case[0].model_copy(
        update={
            "kill_switch_snapshot_ids": (agent_snapshot_id,),
            "kill_switch_scope_bindings": (agent_binding,),
        }
    )
    case = (request, case[1], case[2], case[3], case[4], case[5])
    changed = proof.model_copy(
        update={
            "kill_switch_snapshot_ids": (agent_snapshot_id,),
            "required_scope_bindings": (agent_binding,),
            "entries": (proof.entries[1],),
        }
    )
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_hierarchy_incomplete"


def test_agent_kill_switch_scope_substitution_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    foreign_agent = KillSwitchScopeBinding(scope_kind="agent", scope_id=uuid4())
    bindings = (case[0].kill_switch_scope_bindings[0], foreign_agent)
    request = case[0].model_copy(update={"kill_switch_scope_bindings": bindings})
    case = (request, case[1], case[2], case[3], case[4], case[5])
    changed = proof.model_copy(update={"required_scope_bindings": bindings})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_hierarchy_incomplete"


def test_duplicate_kill_switch_scope_claims_fail_closed() -> None:
    case, context, proof = _kill_switch_case()
    workspace_binding = case[0].kill_switch_scope_bindings[0]
    bindings = (workspace_binding, workspace_binding)
    request = case[0].model_copy(update={"kill_switch_scope_bindings": bindings})
    case = (request, case[1], case[2], case[3], case[4], case[5])
    changed = proof.model_copy(update={"required_scope_bindings": bindings})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_scope_set_ambiguous"


def test_duplicate_kill_switch_snapshot_claims_fail_closed() -> None:
    case, context, proof = _kill_switch_case()
    duplicate_id = case[0].kill_switch_snapshot_ids[0]
    snapshot_ids = (duplicate_id, duplicate_id)
    request = case[0].model_copy(update={"kill_switch_snapshot_ids": snapshot_ids})
    case = (request, case[1], case[2], case[3], case[4], case[5])
    changed = proof.model_copy(update={"kill_switch_snapshot_ids": snapshot_ids})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_snapshot_set_ambiguous"


def test_unfinalized_kill_switch_proof_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"finalized": False})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_verification_not_finalized"


def test_nonexhaustive_kill_switch_proof_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"hierarchy_exhaustive": False})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_hierarchy_unverified"


def test_expired_kill_switch_proof_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_verification_proof_expired"


def test_future_kill_switch_proof_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"observed_at": case[0].evaluated_at + timedelta(seconds=1)})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_verification_proof_not_yet_valid"


def test_incomplete_kill_switch_entries_fail_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"entries": proof.entries[:-1]})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_snapshot_set_incomplete"


def test_duplicate_kill_switch_entries_fail_closed() -> None:
    case, context, proof = _kill_switch_case()
    changed = proof.model_copy(update={"entries": (proof.entries[0], proof.entries[0])})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_snapshot_set_ambiguous"


def test_kill_switch_entry_scope_substitution_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    entry = proof.entries[0].model_copy(update={"scope_id": uuid4()})
    changed = proof.model_copy(update={"entries": (entry, proof.entries[1])})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_snapshot_binding_mismatch"


def test_future_kill_switch_entry_fails_closed() -> None:
    case, context, proof = _kill_switch_case()
    entry = proof.entries[0].model_copy(
        update={"updated_at": case[0].evaluated_at + timedelta(seconds=1)}
    )
    changed = proof.model_copy(update={"entries": (entry, proof.entries[1])})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "kill_switch_snapshot_not_yet_current"


@pytest.mark.parametrize("state", ["armed", "stopping", "stopped"])
def test_active_kill_switch_state_fails_closed(state: str) -> None:
    case, context, proof = _kill_switch_case()
    entry = proof.entries[0].model_copy(update={"state": state})
    changed = proof.model_copy(update={"entries": (entry, proof.entries[1])})
    context = context.model_copy(update={"kill_switch_verification_proof": changed})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == f"kill_switch_{state}"


def test_clear_workspace_agent_workflow_run_hierarchy_allows() -> None:
    case = _exact_allow_case()
    workflow_id = uuid4()
    run_id = uuid4()
    request = case[0].model_copy(
        update={
            "kill_switch_snapshot_ids": (*case[0].kill_switch_snapshot_ids, uuid4(), uuid4()),
            "kill_switch_scope_bindings": (
                *case[0].kill_switch_scope_bindings,
                KillSwitchScopeBinding(scope_kind="workflow", scope_id=workflow_id),
                KillSwitchScopeBinding(scope_kind="run", scope_id=run_id),
            ),
        }
    )
    case = (request, case[1], case[2], case[3], case[4], case[5])
    context = _valid_authority_context(request)

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def _authorization_snapshot_case(
    case: tuple[
        AuthorizationRequest,
        AccessBundle,
        CapabilityGrant,
        AgentGrant,
        AccessBundle,
        CapabilityGrant,
    ]
    | None = None,
) -> tuple[
    AuthorizationDecisionSnapshot,
    AuthorizationSnapshotExpectedInputs,
    WorkspaceSigningKeyVersion,
    AuthorizationSnapshotVerificationProof,
    datetime,
]:
    if case is None:
        case = _exact_allow_case()
    request, requester_bundle, _, agent_grant, _, _ = case
    canonical_inputs = {
        "action": request.action,
        "resource_id": str(request.resource_id),
        "workspace_id": str(request.workspace_id),
    }
    source_versions = {"access_bundle": 1, "agent_grant": 1}
    canonical_digest = authorization_snapshot_digest(canonical_inputs, source_versions)
    signing_private_key = Ed25519PrivateKey.generate()
    signing_key = WorkspaceSigningKeyVersion(
        workspace_id=request.workspace_id,
        key_epoch=1,
        algorithm="ed25519",
        public_key=_public_key_b64(signing_private_key),
        non_exportable_private_key_ref="keyring://corvus/workspace-signing/current",
        status=SigningKeyStatus.ACTIVE,
        valid_from=request.evaluated_at - timedelta(hours=1),
        attestation_digest="a" * 64,
    )
    snapshot = AuthorizationDecisionSnapshot(
        workspace_id=request.workspace_id,
        request_context_id=request.request_context_id,
        deployment_instance_id=request.deployment_instance_id,
        authority_epoch_credential_id=request.authority_epoch_credential_id,
        authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        authority_commit_receipt_id=request.authority_commit_receipt_id,
        authority_proof_digest=request.authority_proof_digest,
        membership_version_ids=(uuid4(),),
        membership_digest="b" * 64,
        scope_kind=request.scope_kind,
        scope_id=request.scope_id,
        scope_digest="c" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_digest="d" * 64,
        requester_id=request.requester_id,
        transport_principal_id=request.requester_id,
        access_bundle_id=requester_bundle.id,
        access_bundle_version_digest="e" * 64,
        agent_grant_id=agent_grant.id,
        agent_delegation_digest="f" * 64,
        execution_placement_id=request.execution_placement_id,
        provider_connection_id=request.provider_connection_id,
        credential_grant_id=request.credential_grant_id,
        credential_version_id=request.credential_version_id,
        policy_digest=requester_bundle.policy_digest,
        autonomy_policy_digest="0" * 64,
        budget_snapshot_ids=request.budget_snapshot_ids,
        budget_snapshot_digest=request.budget_snapshot_digest or "1" * 64,
        kill_switch_snapshot_ids=request.kill_switch_snapshot_ids,
        kill_switch_snapshot_digest=request.kill_switch_snapshot_digest,
        decision="allow",
        reason_code="exact_capability_intersection",
        canonical_inputs_json=canonical_inputs,
        source_record_version_map=source_versions,
        canonical_digest=canonical_digest,
        signing_key_version_id=signing_key.id,
        snapshot_signature="pending",
        created_at=request.evaluated_at - timedelta(seconds=1),
    )
    record_digest = authorization_snapshot_record_digest(snapshot)
    snapshot = snapshot.model_copy(
        update={
            "snapshot_signature": _signature_b64(
                signing_private_key,
                bytes.fromhex(record_digest),
            )
        }
    )
    bound_input_digest = authorization_snapshot_bound_input_digest(snapshot)
    expected = AuthorizationSnapshotExpectedInputs(
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=record_digest,
        bound_input_digest=bound_input_digest,
        signing_key_version_id=signing_key.id,
        verified_at=request.evaluated_at,
    )
    proof = AuthorizationSnapshotVerificationProof(
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=record_digest,
        bound_input_digest=bound_input_digest,
        signing_key_version_id=signing_key.id,
        finalized=True,
    )
    return snapshot, expected, signing_key, proof, request.evaluated_at


def test_exact_signed_authorization_snapshot_verifies() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "authorization_snapshot_verified"


def test_canonical_authorization_snapshot_tamper_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    tampered = snapshot.model_copy(update={"canonical_inputs_json": {"action": "admin"}})

    result = verify_authorization_decision_snapshot(
        tampered,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_digest_mismatch"


def test_top_level_authority_snapshot_tamper_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    tampered = snapshot.model_copy(update={"authority_state_root": "9" * 64})

    result = verify_authorization_decision_snapshot(
        tampered,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_record_digest_mismatch"


def test_resigned_stale_snapshot_inputs_fail_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    tampered = snapshot.model_copy(
        update={"authority_generation": snapshot.authority_generation + 1}
    )
    record_digest = authorization_snapshot_record_digest(tampered)
    expected = expected.model_copy(update={"authorization_snapshot_digest": record_digest})
    proof = proof.model_copy(update={"authorization_snapshot_digest": record_digest})

    result = verify_authorization_decision_snapshot(
        tampered,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_bound_inputs_mismatch"


def test_invalid_authorization_snapshot_signature_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    snapshot = snapshot.model_copy(update={"snapshot_signature": "invalid"})

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_signature_invalid"


def test_snapshot_proof_bound_input_substitution_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    proof = proof.model_copy(update={"bound_input_digest": "9" * 64})

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_verification_proof_mismatch"


def test_unfinalized_authorization_snapshot_proof_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    proof = proof.model_copy(update={"finalized": False})

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_verification_not_finalized"


def test_authorization_snapshot_signing_key_substitution_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    substituted = signing_key.model_copy(update={"id": uuid4()})

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=substituted,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_signing_key_mismatch"


def test_revoked_snapshot_signing_key_at_signing_time_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    revoked = signing_key.model_copy(
        update={
            "status": SigningKeyStatus.REVOKED,
            "revoked_at": snapshot.created_at,
        }
    )

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=revoked,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_signing_key_invalid"


def test_future_authorization_snapshot_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    future = snapshot.model_copy(update={"created_at": verified_at + timedelta(seconds=1)})
    record_digest = authorization_snapshot_record_digest(future)
    expected = expected.model_copy(update={"authorization_snapshot_digest": record_digest})
    proof = proof.model_copy(update={"authorization_snapshot_digest": record_digest})

    result = verify_authorization_decision_snapshot(
        future,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_not_yet_current"


def test_authorization_snapshot_verification_proof_substitution_fails_closed() -> None:
    snapshot, expected, signing_key, proof, verified_at = _authorization_snapshot_case()
    proof = proof.model_copy(update={"authorization_snapshot_id": uuid4()})

    result = verify_authorization_decision_snapshot(
        snapshot,
        expected=expected,
        signing_key=signing_key,
        verification_proof=proof,
        verified_at=verified_at,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "authorization_snapshot_verification_proof_mismatch"


def test_requester_outside_immutable_audience_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    audience = context.audience_policy_snapshot
    assert audience is not None
    denied_audience = audience.model_copy(update={"principal_ids": frozenset()})
    context = context.model_copy(update={"audience_policy_snapshot": denied_audience})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "audience_principal_denied"


def test_disabled_client_surface_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"enabled_client_surfaces": frozenset({ClientSurface.WEB})}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "client_surface_disabled"


def test_missing_audience_snapshot_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0]).model_copy(
        update={"audience_policy_snapshot": None}
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "audience_policy_snapshot_missing"


def test_audience_snapshot_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    audience = context.audience_policy_snapshot
    assert audience is not None
    substituted = audience.model_copy(update={"id": uuid4()})
    context = context.model_copy(update={"audience_policy_snapshot": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "audience_policy_snapshot_mismatch"


def test_future_audience_snapshot_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    audience = context.audience_policy_snapshot
    assert audience is not None
    future = audience.model_copy(update={"created_at": case[0].evaluated_at + timedelta(seconds=1)})
    context = context.model_copy(update={"audience_policy_snapshot": future})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "audience_policy_snapshot_not_yet_current"


def test_matching_role_audience_allows() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    audience = context.audience_policy_snapshot
    assert audience is not None
    role_id = uuid4()
    role_audience = audience.model_copy(
        update={
            "visibility": "role",
            "principal_ids": frozenset(),
            "role_ids": frozenset({role_id}),
        }
    )
    context = context.model_copy(
        update={
            "audience_policy_snapshot": role_audience,
            "requester_role_ids": frozenset({role_id}),
        }
    )

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason_code == "exact_capability_intersection"


def test_wrong_scoped_audience_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    audience = context.audience_policy_snapshot
    assert audience is not None
    wrong_scope = audience.model_copy(
        update={
            "visibility": "thread",
            "principal_ids": frozenset(),
        }
    )
    context = context.model_copy(update={"audience_policy_snapshot": wrong_scope})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "audience_scope_mismatch"


def test_transport_principal_substitution_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    client = context.client_context
    assert client is not None
    substituted = client.model_copy(update={"transport_principal_id": uuid4()})
    context = context.model_copy(update={"client_context": substituted})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "transport_principal_mismatch"


def test_expired_client_context_fails_closed() -> None:
    case = _exact_allow_case()
    context = _valid_authority_context(case[0])
    client = context.client_context
    assert client is not None
    expired = client.model_copy(update={"expires_at": case[0].evaluated_at})
    context = context.model_copy(update={"client_context": expired})

    result = _evaluate_direct_case(case, context)

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason_code == "client_context_expired"


def test_enabled_client_surfaces_have_identical_allow_decisions() -> None:
    base_case = _exact_allow_case()
    results: list[AuthorizationResult] = []
    for surface in ClientSurface:
        request = base_case[0].model_copy(update={"client_surface": surface})
        case = (request, *base_case[1:])
        results.append(_evaluate_direct_case(case, _valid_authority_context(request)))

    assert {result.decision for result in results} == {AuthorizationDecision.ALLOW}
    assert {result.reason_code for result in results} == {"exact_capability_intersection"}
    assert {result.actions for result in results} == {frozenset({base_case[0].action})}


def test_enabled_client_surfaces_have_identical_audience_denials() -> None:
    base_case = _exact_allow_case()
    results: list[AuthorizationResult] = []
    for surface in ClientSurface:
        request = base_case[0].model_copy(update={"client_surface": surface})
        case = (request, *base_case[1:])
        context = _valid_authority_context(request)
        audience = context.audience_policy_snapshot
        assert audience is not None
        denied = audience.model_copy(update={"principal_ids": frozenset()})
        context = context.model_copy(update={"audience_policy_snapshot": denied})
        results.append(_evaluate_direct_case(case, context))

    assert {result.decision for result in results} == {AuthorizationDecision.DENY}
    assert {result.reason_code for result in results} == {"audience_principal_denied"}


def test_verified_project_authorization_adapter_requires_persisted_crypto_evidence() -> None:
    case = _exact_allow_case()
    request, requester_bundle, requester_grant, agent_grant, agent_bundle, agent_capability = case
    authority_context = _valid_authority_context(request)
    snapshot, expected, signing_key, verification, _ = _authorization_snapshot_case(case)
    assert authority_context.deployment_instance is not None
    context = RequestContext(
        id=request.request_context_id,
        deployment_profile_id=authority_context.deployment_instance.deployment_profile_id,
        deployment_instance_id=request.deployment_instance_id,
        workspace_id=request.workspace_id,
        workspace_authority_epoch=request.workspace_authority_epoch,
        workspace_authority_generation=request.workspace_authority_generation,
        authority_state_root=request.authority_state_root,
        authority_epoch_credential_id=request.authority_epoch_credential_id,
        authority_commit_receipt_id=request.authority_commit_receipt_id,
        authority_proof_digest=request.authority_proof_digest,
        scope_kind=request.scope_kind,
        scope_id=request.scope_id,
        scope_digest=request.scope_digest,
        audience_policy_snapshot_id=request.audience_policy_snapshot_id,
        audience_policy_digest=request.audience_policy_digest,
        requester_id=request.requester_id,
        client_context_id=request.client_context_id,
        transport_principal_id=request.transport_principal_id,
        agent_id=request.acting_agent_id,
        agent_grant_id=agent_grant.id,
        access_bundle_id=requester_bundle.id,
        policy_digest=requester_bundle.policy_digest,
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=expected.authorization_snapshot_digest,
        authorization_signing_key_version_id=signing_key.id,
        idempotency_key="verified-project-authorization",
        correlation_id=uuid4(),
    )
    project_request = ProjectAuthorizationRequest(
        context=context,
        client_surface=request.client_surface,
        action="project.read",
        project_id=request.resource_id,
    )
    resolved = VerifiedProjectAuthorizationInputs(
        request=request,
        authority_context=authority_context,
        requester_bundle=requester_bundle,
        requester_grants=(requester_grant,),
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=(agent_capability,),
        snapshot=snapshot,
        snapshot_expected=expected,
        snapshot_verification=verification,
        signing_key=signing_key,
    )

    class Inputs:
        def __init__(self, value: VerifiedProjectAuthorizationInputs) -> None:
            self.value = value

        def resolve(
            self, received: ProjectAuthorizationRequest
        ) -> VerifiedProjectAuthorizationInputs:
            assert received == project_request
            return self.value

    class Snapshots:
        def __init__(self, value: AuthorizationDecisionSnapshot) -> None:
            self.value = value

        def get_snapshot(
            self,
            *,
            workspace_id: UUID,
            snapshot_id: UUID,
        ) -> AuthorizationDecisionSnapshot | None:
            assert workspace_id == request.workspace_id
            assert snapshot_id == snapshot.id
            return self.value

    inputs = Inputs(resolved)
    snapshots = Snapshots(snapshot)
    adapter = VerifiedProjectAuthorizationAdapter(inputs=inputs, snapshots=snapshots)

    allowed = adapter.authorize(project_request)
    tampered = snapshot.model_copy(update={"snapshot_signature": "invalid"})
    inputs.value = resolved.model_copy(update={"snapshot": tampered})
    snapshots.value = tampered
    denied = adapter.authorize(project_request)

    assert allowed.allowed is True
    assert allowed.reason_code == "exact_capability_intersection"
    assert denied.allowed is False
    assert denied.reason_code == "authorization_snapshot_signature_invalid"


def test_verified_agent_run_authorization_adapter_rechecks_canonical_current_state(
    tmp_path: Path,
) -> None:
    base, credential_context, credential_ref, credential_proof = _credential_bound_case()
    (
        base_request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
    ) = base
    run_id = uuid4()
    action = AgentRunOperation.START.value
    authorization_request = base_request.model_copy(
        update={
            "resource_kind": "agent_run",
            "resource_id": run_id,
            "action": action,
        }
    )
    requester_grant = requester_grant.model_copy(
        update={"resource_kind": "agent_run", "resource_id": run_id, "action": action}
    )
    agent_capability = agent_capability.model_copy(
        update={"resource_kind": "agent_run", "resource_id": run_id, "action": action}
    )
    binding = ProviderBinding(
        workspace_id=authorization_request.workspace_id,
        project_id=authorization_request.scope_id,
        family=ProviderFamily.CODEX,
        transport=ProviderTransport.HTTP_API,
        status=ProviderStatus.AVAILABLE,
        credential_ref_id=authorization_request.credential_ref_id,
        model="gpt-5.6-sol",
        capabilities=AgentCapabilities(),
        health_checked_at=authorization_request.evaluated_at,
        version=1,
        data_egress_disclosure="Prompts leave the local process.",
        server_storage_disclosure="Provider retention policy applies.",
    )
    autonomy_root = tmp_path.resolve()
    outside_root = (autonomy_root.parent / f"{autonomy_root.name}-outside").resolve()
    autonomy = AutonomyGrant(
        workspace_id=authorization_request.workspace_id,
        project_id=authorization_request.scope_id,
        profile=AutonomyProfile.REVIEW_FIRST,
        allowed_roots=(autonomy_root,),
        allowed_effect_classes=frozenset({"repository.read"}),
        denied_effect_classes=frozenset({"shell.execute"}),
        allowed_sandbox_profiles=frozenset({"review"}),
        allowed_tool_ids=frozenset({"repository.search"}),
        allowed_network_destinations=("api.openai.com:443",),
        credential_grant_ids=(authorization_request.credential_grant_id,),
        wall_clock_deadline=authorization_request.evaluated_at + timedelta(days=1),
        provider_spend_ceiling=Decimal("0"),
        corvus_budget_ceiling=Decimal("0"),
        max_turns=4,
        max_output_tokens=1024,
        max_output_bytes=4096,
        max_retries=0,
        approval_ceiling=0,
        always_block_effects=frozenset({"authority.bypass"}),
        notification_policy="notify",
        summary_policy="summary",
        issuer_principal_id=authorization_request.requester_id,
        issued_at=authorization_request.evaluated_at - timedelta(minutes=1),
        expires_at=authorization_request.evaluated_at + timedelta(days=2),
        policy_digest="b" * 64,
    )
    provisional_run_request = AgentRunRequest(
        run_id=run_id,
        workspace_id=authorization_request.workspace_id,
        project_id=authorization_request.scope_id,
        provider_binding_id=binding.id,
        provider_binding_version=binding.version,
        provider_binding_digest=compute_provider_binding_digest(binding),
        model=binding.model,
        effort="high",
        prompt="Review the repository.",
        authorization_proof_id=uuid4(),
        authorization_proof_digest="9" * 64,
        autonomy_grant_id=autonomy.id,
        autonomy_grant_digest=compute_autonomy_grant_digest(autonomy),
        credential_grant_ids=(authorization_request.credential_grant_id,),
        credential_proof_id=None,
        credential_proof_digest=None,
        budget_proof_id=None,
        budget_proof_digest=None,
        kill_switch_proof_id=authorization_request.kill_switch_snapshot_ids[0],
        kill_switch_proof_digest=authorization_request.kill_switch_snapshot_digest,
        sandbox_profile="review",
        filesystem_envelope=(str(autonomy_root),),
        network_envelope=(),
        tool_envelope=(),
        requested_effect_classes=frozenset(),
        provider_spend_limit=Decimal("0"),
        corvus_budget_limit=Decimal("0"),
        budget_unit="usd_micros",
        budget_requested_amount=1,
        approval_limit=0,
        max_retries=0,
        max_turns=4,
        deadline=autonomy.wall_clock_deadline,
        max_output_tokens=1024,
        max_output_bytes=4096,
        idempotency_key="verified-agent-run-authorization",
    )
    runtime_limit_digest = compute_agent_run_runtime_limit_digest(provisional_run_request)
    budget_snapshot_ids = (uuid4(),)
    authorization_request = authorization_request.model_copy(
        update={
            "budget_snapshot_ids": budget_snapshot_ids,
            "budget_snapshot_digest": "1" * 64,
            "runtime_limit_digest": runtime_limit_digest,
            "budget_unit": provisional_run_request.budget_unit,
            "budget_requested_amount": provisional_run_request.budget_requested_amount,
        }
    )
    credential_proof = credential_proof.model_copy(update={"operation": action})
    budget_proof = BudgetRuntimeVerificationProof(
        request_context_id=authorization_request.request_context_id,
        workspace_id=authorization_request.workspace_id,
        scope_kind=authorization_request.scope_kind,
        scope_id=authorization_request.scope_id,
        action=authorization_request.action,
        budget_snapshot_ids=budget_snapshot_ids,
        budget_snapshot_digest=authorization_request.budget_snapshot_digest,
        runtime_limit_digest=runtime_limit_digest,
        unit=authorization_request.budget_unit,
        requested_amount=authorization_request.budget_requested_amount,
        canonical_account_ids=(uuid4(),),
        canonical_account_closure_digest="3" * 64,
        expected_account_count=1,
        minimum_remaining_amount=10,
        runtime_limit_ms=60_000,
        runtime_consumed_ms=1_000,
        period_starts_at=authorization_request.evaluated_at - timedelta(hours=1),
        period_ends_at=authorization_request.evaluated_at + timedelta(hours=1),
        observed_at=authorization_request.evaluated_at - timedelta(seconds=1),
        expires_at=authorization_request.evaluated_at + timedelta(minutes=5),
        hierarchy_verified=True,
        period_non_overlapping_verified=True,
        all_accounts_active=True,
        finalized=True,
    )
    authority_context = _valid_authority_context(authorization_request).model_copy(
        update={
            "execution_placement": credential_context.execution_placement,
            "credential_ref": credential_ref,
            "credential_verification_proof": credential_proof,
            "expected_credential_rotation_epoch": credential_proof.rotation_epoch,
            "expected_credential_nonce_digest": credential_proof.nonce_digest,
            "budget_runtime_verification_proof": budget_proof,
        }
    )
    case = (
        authorization_request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
    )
    snapshot, expected, signing_key, verification, _ = _authorization_snapshot_case(case)
    assert authority_context.deployment_instance is not None
    context = RequestContext(
        id=authorization_request.request_context_id,
        deployment_profile_id=authority_context.deployment_instance.deployment_profile_id,
        deployment_instance_id=authorization_request.deployment_instance_id,
        workspace_id=authorization_request.workspace_id,
        workspace_authority_epoch=authorization_request.workspace_authority_epoch,
        workspace_authority_generation=authorization_request.workspace_authority_generation,
        authority_state_root=authorization_request.authority_state_root,
        authority_epoch_credential_id=authorization_request.authority_epoch_credential_id,
        authority_commit_receipt_id=authorization_request.authority_commit_receipt_id,
        authority_proof_digest=authorization_request.authority_proof_digest,
        scope_kind=authorization_request.scope_kind,
        scope_id=authorization_request.scope_id,
        scope_digest=authorization_request.scope_digest,
        audience_policy_snapshot_id=authorization_request.audience_policy_snapshot_id,
        audience_policy_digest=authorization_request.audience_policy_digest,
        requester_id=authorization_request.requester_id,
        client_context_id=authorization_request.client_context_id,
        transport_principal_id=authorization_request.transport_principal_id,
        agent_id=authorization_request.acting_agent_id,
        agent_grant_id=agent_grant.id,
        access_bundle_id=requester_bundle.id,
        policy_digest=requester_bundle.policy_digest,
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=expected.authorization_snapshot_digest,
        authorization_signing_key_version_id=signing_key.id,
        idempotency_key=provisional_run_request.idempotency_key,
        correlation_id=uuid4(),
    )
    credential_receipt = canonical_credential_evidence_receipt(
        authorization_request, authority_context
    )
    budget_receipt = canonical_budget_evidence_receipt(authorization_request, authority_context)
    assert credential_receipt is not None
    assert budget_receipt is not None
    run_request = provisional_run_request.model_copy(
        update={
            "authorization_proof_id": snapshot.id,
            "authorization_proof_digest": expected.authorization_snapshot_digest,
            "credential_proof_id": credential_receipt[0],
            "credential_proof_digest": credential_receipt[1],
            "budget_proof_id": budget_receipt[0],
            "budget_proof_digest": budget_receipt[1],
        }
    )
    agent_request = AgentRunAuthorizationRequest(
        context=context,
        client_surface=authorization_request.client_surface,
        operation=AgentRunOperation.START,
        request=run_request,
        canonical_request_digest=compute_agent_run_request_digest(run_request),
    )
    resolved = VerifiedAgentRunAuthorizationInputs(
        request=authorization_request,
        authority_context=authority_context,
        requester_bundle=requester_bundle,
        requester_grants=(requester_grant,),
        agent_grant=agent_grant,
        agent_bundle=agent_bundle,
        agent_capabilities=(agent_capability,),
        snapshot=snapshot,
        snapshot_expected=expected,
        snapshot_verification=verification,
        signing_key=signing_key,
        autonomy_grant=autonomy,
        provider_binding=binding,
        kill_switch_proof_id=run_request.kill_switch_proof_id,
        kill_switch_proof_digest=run_request.kill_switch_proof_digest,
    )

    class Inputs:
        value = resolved
        expected = agent_request

        def resolve(
            self, received: AgentRunAuthorizationRequest
        ) -> VerifiedAgentRunAuthorizationInputs:
            assert received == self.expected
            return self.value

    class Snapshots:
        value = snapshot

        def get_snapshot(
            self, *, workspace_id: UUID, snapshot_id: UUID
        ) -> AuthorizationDecisionSnapshot | None:
            assert workspace_id == authorization_request.workspace_id
            assert snapshot_id == self.value.id
            return self.value

    inputs = Inputs()
    snapshots = Snapshots()
    adapter = VerifiedAgentRunAuthorizationAdapter(inputs=inputs, snapshots=snapshots)

    cancellation_authorization_request = authorization_request.model_copy(
        update={"action": AgentRunOperation.CANCEL.value}
    )
    cancellation_credential_proof = credential_proof.model_copy(
        update={"operation": AgentRunOperation.CANCEL.value}
    )
    cancellation_authority_context = authority_context.model_copy(
        update={"credential_verification_proof": cancellation_credential_proof}
    )
    cancellation_credential_receipt = canonical_credential_evidence_receipt(
        cancellation_authorization_request,
        cancellation_authority_context,
    )
    assert cancellation_credential_receipt is not None
    expired_autonomy = autonomy.model_copy(
        update={
            "wall_clock_deadline": authorization_request.evaluated_at,
            "expires_at": authorization_request.evaluated_at,
        }
    )
    cancellation_run_request = run_request.model_copy(
        update={
            "deadline": authorization_request.evaluated_at,
            "autonomy_grant_digest": compute_autonomy_grant_digest(expired_autonomy),
            "credential_proof_id": cancellation_credential_receipt[0],
            "credential_proof_digest": cancellation_credential_receipt[1],
        }
    )
    cancellation_handle = AgentRunHandle(
        run_id=cancellation_run_request.run_id,
        provider_binding_id=cancellation_run_request.provider_binding_id,
        created_at=authorization_request.evaluated_at,
        state=AgentRunState.RUNNING,
    )
    cancellation_agent_request = AgentRunAuthorizationRequest(
        context=context,
        client_surface=cancellation_authorization_request.client_surface,
        operation=AgentRunOperation.CANCEL,
        request=cancellation_run_request,
        handle=cancellation_handle,
        canonical_request_digest=compute_agent_run_request_digest(cancellation_run_request),
        current_kill_switch_proof_id=cancellation_run_request.kill_switch_proof_id,
        current_kill_switch_proof_digest=cancellation_run_request.kill_switch_proof_digest,
    )
    cancellation_inputs = resolved.model_copy(
        update={
            "request": cancellation_authorization_request,
            "authority_context": cancellation_authority_context,
            "autonomy_grant": expired_autonomy,
        }
    )

    assert (
        adapter._binding_denial(cancellation_agent_request, cancellation_inputs)
        == "agent_run_capability_unavailable"
    )
    cancellation_binding = binding.model_copy(
        update={
            "capabilities": AgentCapabilities(
                provider_side_cancellation=CapabilitySupport.SUPPORTED
            )
        }
    )
    supported_cancellation_request = cancellation_run_request.model_copy(
        update={"provider_binding_digest": compute_provider_binding_digest(cancellation_binding)}
    )
    supported_cancellation_agent_request = cancellation_agent_request.model_copy(
        update={
            "request": supported_cancellation_request,
            "canonical_request_digest": compute_agent_run_request_digest(
                supported_cancellation_request
            ),
        }
    )
    supported_cancellation_inputs = cancellation_inputs.model_copy(
        update={"provider_binding": cancellation_binding}
    )

    assert (
        adapter._binding_denial(
            supported_cancellation_agent_request,
            supported_cancellation_inputs,
        )
        is None
    )

    allowed = adapter.authorize(agent_request)
    expired_deadline_request = run_request.model_copy(
        update={"deadline": authorization_request.evaluated_at}
    )
    inputs.expected = agent_request.model_copy(
        update={
            "request": expired_deadline_request,
            "canonical_request_digest": compute_agent_run_request_digest(expired_deadline_request),
        }
    )
    expired_deadline = adapter.authorize(inputs.expected)
    future_issued_autonomy = autonomy.model_copy(
        update={"issued_at": authorization_request.evaluated_at + timedelta(seconds=1)}
    )
    future_issued_request = run_request.model_copy(
        update={"autonomy_grant_digest": compute_autonomy_grant_digest(future_issued_autonomy)}
    )
    inputs.value = resolved.model_copy(update={"autonomy_grant": future_issued_autonomy})
    inputs.expected = agent_request.model_copy(
        update={
            "request": future_issued_request,
            "canonical_request_digest": compute_agent_run_request_digest(future_issued_request),
        }
    )
    future_issued_grant = adapter.authorize(inputs.expected)
    inputs.value = resolved
    inputs.expected = agent_request
    inputs.value = resolved.model_copy(
        update={
            "autonomy_grant": autonomy.model_copy(
                update={"revoked_at": authorization_request.evaluated_at}
            )
        }
    )
    stale_autonomy = adapter.authorize(agent_request)
    inputs.value = resolved.model_copy(
        update={"provider_binding": binding.model_copy(update={"version": 2})}
    )
    stale_provider = adapter.authorize(agent_request)
    inputs.value = resolved.model_copy(
        update={
            "requester_bundle": requester_bundle.model_copy(
                update={"revoked_at": authorization_request.evaluated_at}
            )
        }
    )
    stale_authority = adapter.authorize(agent_request)
    inputs.value = resolved
    wrapper_credential_request = AgentRunRequest.model_validate(
        {
            **run_request.model_dump(exclude_computed_fields=True),
            "credential_proof_id": uuid4(),
            "credential_proof_digest": "f" * 64,
        }
    )
    inputs.expected = agent_request.model_copy(
        update={
            "request": wrapper_credential_request,
            "canonical_request_digest": compute_agent_run_request_digest(
                wrapper_credential_request
            ),
        }
    )
    stale_credential = adapter.authorize(inputs.expected)
    wrapper_budget_request = AgentRunRequest.model_validate(
        {
            **run_request.model_dump(exclude_computed_fields=True),
            "budget_proof_id": uuid4(),
            "budget_proof_digest": "f" * 64,
        }
    )
    inputs.expected = agent_request.model_copy(
        update={
            "request": wrapper_budget_request,
            "canonical_request_digest": compute_agent_run_request_digest(wrapper_budget_request),
        }
    )
    stale_budget = adapter.authorize(inputs.expected)
    substituted_credential_grant_id = uuid4()
    substituted_autonomy = autonomy.model_copy(
        update={"credential_grant_ids": (substituted_credential_grant_id,)}
    )
    substituted_credential_request = run_request.model_copy(
        update={
            "autonomy_grant_digest": compute_autonomy_grant_digest(substituted_autonomy),
            "credential_grant_ids": (substituted_credential_grant_id,),
        }
    )
    inputs.value = resolved.model_copy(update={"autonomy_grant": substituted_autonomy})
    inputs.expected = agent_request.model_copy(
        update={
            "request": substituted_credential_request,
            "canonical_request_digest": compute_agent_run_request_digest(
                substituted_credential_request
            ),
        }
    )
    credential_grant_substitution = adapter.authorize(inputs.expected)
    substituted_binding = binding.model_copy(update={"credential_ref_id": uuid4()})
    substituted_binding_request = run_request.model_copy(
        update={"provider_binding_digest": compute_provider_binding_digest(substituted_binding)}
    )
    inputs.value = resolved.model_copy(update={"provider_binding": substituted_binding})
    inputs.expected = agent_request.model_copy(
        update={
            "request": substituted_binding_request,
            "canonical_request_digest": compute_agent_run_request_digest(
                substituted_binding_request
            ),
        }
    )
    provider_credential_substitution = adapter.authorize(inputs.expected)
    inputs.value = resolved
    budget_unit_request = run_request.model_copy(update={"budget_unit": "tokens"})
    inputs.expected = agent_request.model_copy(
        update={
            "request": budget_unit_request,
            "canonical_request_digest": compute_agent_run_request_digest(budget_unit_request),
        }
    )
    budget_unit_substitution = adapter.authorize(inputs.expected)
    budget_amount_request = run_request.model_copy(update={"budget_requested_amount": 2})
    inputs.expected = agent_request.model_copy(
        update={
            "request": budget_amount_request,
            "canonical_request_digest": compute_agent_run_request_digest(budget_amount_request),
        }
    )
    budget_amount_substitution = adapter.authorize(inputs.expected)
    runtime_limit_request = run_request.model_copy(update={"max_turns": 3})
    inputs.expected = agent_request.model_copy(
        update={
            "request": runtime_limit_request,
            "canonical_request_digest": compute_agent_run_request_digest(runtime_limit_request),
        }
    )
    runtime_limit_substitution = adapter.authorize(inputs.expected)
    output_byte_request = run_request.model_copy(update={"max_output_bytes": 4097})
    inputs.expected = agent_request.model_copy(
        update={
            "request": output_byte_request,
            "canonical_request_digest": compute_agent_run_request_digest(output_byte_request),
        }
    )
    output_byte_ceiling_exceeded = adapter.authorize(inputs.expected)
    no_budget_authorization = authorization_request.model_copy(
        update={
            "budget_snapshot_ids": (),
            "budget_snapshot_digest": None,
            "runtime_limit_digest": None,
            "budget_unit": None,
            "budget_requested_amount": None,
        }
    )
    no_budget_request = run_request.model_copy(
        update={"budget_proof_id": None, "budget_proof_digest": None}
    )
    inputs.value = resolved.model_copy(
        update={
            "request": no_budget_authorization,
            "authority_context": authority_context.model_copy(
                update={"budget_runtime_verification_proof": None}
            ),
        }
    )
    inputs.expected = agent_request.model_copy(
        update={
            "request": no_budget_request,
            "canonical_request_digest": compute_agent_run_request_digest(no_budget_request),
        }
    )
    missing_budget_evidence = adapter.authorize(inputs.expected)
    inputs.expected = agent_request
    inputs.value = resolved
    limit_substitutions = (
        {"sandbox_profile": "unrestricted"},
        {"filesystem_envelope": (str(outside_root),)},
        {"network_envelope": ("evil.example:443",)},
        {"tool_envelope": ("shell.exec",)},
        {"requested_effect_classes": frozenset({"shell.execute"})},
        {"requested_effect_classes": frozenset({"authority.bypass"})},
        {"provider_spend_limit": Decimal("1")},
        {"corvus_budget_limit": Decimal("1")},
        {"approval_limit": 1},
        {"max_retries": 1},
        {"max_turns": 5},
        {"max_output_tokens": 1025},
        {"deadline": autonomy.wall_clock_deadline + timedelta(seconds=1)},
    )
    limit_denials = []
    for substitution in limit_substitutions:
        substituted_request = run_request.model_copy(update=substitution)
        inputs.expected = agent_request.model_copy(
            update={
                "request": substituted_request,
                "canonical_request_digest": compute_agent_run_request_digest(substituted_request),
            }
        )
        limit_denials.append(adapter.authorize(inputs.expected))
    inputs.expected = agent_request
    assert authority_context.kill_switch_verification_proof is not None
    kill_proof = authority_context.kill_switch_verification_proof
    armed_entry = kill_proof.entries[0].model_copy(update={"state": "armed"})
    inputs.value = resolved.model_copy(
        update={
            "authority_context": authority_context.model_copy(
                update={
                    "kill_switch_verification_proof": kill_proof.model_copy(
                        update={"entries": (armed_entry, *kill_proof.entries[1:])}
                    )
                }
            )
        }
    )
    kill_switch_active = adapter.authorize(agent_request)

    local_authorization_request = authorization_request.model_copy(
        update={
            "provider_connection_id": None,
            "credential_ref_id": None,
            "credential_version_id": None,
            "credential_grant_id": None,
        }
    )
    local_authority_context = authority_context.model_copy(
        update={
            "credential_ref": None,
            "credential_verification_proof": None,
            "expected_credential_rotation_epoch": None,
            "expected_credential_nonce_digest": None,
        }
    )
    local_binding = ProviderBinding(
        workspace_id=authorization_request.workspace_id,
        project_id=authorization_request.scope_id,
        family=ProviderFamily.CODEX,
        transport=ProviderTransport.LOCAL_CLI,
        status=ProviderStatus.AVAILABLE,
        executable_identity=ExecutableIdentity(
            executable_path=autonomy_root / "codex.exe",
            version="1.0.0",
            sha256_digest="a" * 64,
        ),
        model=binding.model,
        capabilities=AgentCapabilities(),
        health_checked_at=authorization_request.evaluated_at,
        version=1,
        data_egress_disclosure="Prompts leave the local process.",
        server_storage_disclosure="Provider retention policy applies.",
    )
    local_autonomy = autonomy.model_copy(update={"credential_grant_ids": ()})
    local_case = (
        local_authorization_request,
        requester_bundle,
        requester_grant,
        agent_grant,
        agent_bundle,
        agent_capability,
    )
    (
        local_snapshot,
        local_expected,
        local_signing_key,
        local_verification,
        _,
    ) = _authorization_snapshot_case(local_case)
    local_context = context.model_copy(
        update={
            "authorization_snapshot_id": local_snapshot.id,
            "authorization_snapshot_digest": local_expected.authorization_snapshot_digest,
            "authorization_signing_key_version_id": local_signing_key.id,
        }
    )
    local_run_request = run_request.model_copy(
        update={
            "provider_binding_id": local_binding.id,
            "provider_binding_version": local_binding.version,
            "provider_binding_digest": compute_provider_binding_digest(local_binding),
            "authorization_proof_id": local_snapshot.id,
            "authorization_proof_digest": local_expected.authorization_snapshot_digest,
            "autonomy_grant_digest": compute_autonomy_grant_digest(local_autonomy),
            "credential_grant_ids": (),
            "credential_proof_id": None,
            "credential_proof_digest": None,
        }
    )
    local_agent_request = AgentRunAuthorizationRequest(
        context=local_context,
        client_surface=local_authorization_request.client_surface,
        operation=AgentRunOperation.START,
        request=local_run_request,
        canonical_request_digest=compute_agent_run_request_digest(local_run_request),
    )
    inputs.expected = local_agent_request
    inputs.value = resolved.model_copy(
        update={
            "request": local_authorization_request,
            "authority_context": local_authority_context,
            "snapshot": local_snapshot,
            "snapshot_expected": local_expected,
            "snapshot_verification": local_verification,
            "signing_key": local_signing_key,
            "autonomy_grant": local_autonomy,
            "provider_binding": local_binding,
        }
    )
    snapshots.value = local_snapshot
    credentialless_local_cli = adapter.authorize(local_agent_request)

    assert allowed.allowed is True
    assert allowed.reason_code == "exact_capability_intersection"
    assert allowed.immutable_request_digest == run_request.immutable_request_digest
    assert expired_deadline.allowed is False
    assert expired_deadline.reason_code == "stale_autonomy_grant"
    assert future_issued_grant.allowed is False
    assert future_issued_grant.reason_code == "stale_autonomy_grant"
    assert stale_autonomy.allowed is False
    assert stale_autonomy.reason_code == "stale_autonomy_grant"
    assert stale_provider.allowed is False
    assert stale_provider.reason_code == "provider_binding_digest_mismatch"
    assert stale_authority.reason_code == "requester_grant_revoked"
    assert stale_credential.reason_code == "stale_credential_proof"
    assert stale_budget.reason_code == "agent_run_over_budget"
    assert credential_grant_substitution.reason_code == "stale_credential_proof"
    assert provider_credential_substitution.reason_code == "stale_credential_proof"
    assert budget_unit_substitution.reason_code == "agent_run_over_budget"
    assert budget_amount_substitution.reason_code == "agent_run_over_budget"
    assert runtime_limit_substitution.reason_code == "agent_run_over_budget"
    assert output_byte_ceiling_exceeded.reason_code == "stale_autonomy_grant"
    assert missing_budget_evidence.reason_code == "agent_run_over_budget"
    assert {decision.reason_code for decision in limit_denials} == {"stale_autonomy_grant"}
    assert kill_switch_active.reason_code == "kill_switch_armed"
    assert credentialless_local_cli.allowed is True
    assert credentialless_local_cli.reason_code == "exact_capability_intersection"
