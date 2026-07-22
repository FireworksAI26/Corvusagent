from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corvus.application.authorization import (
    AuthorityCommitProof,
    AuthorityEvaluationContext,
    AuthorizationRequest,
    AuthorizationSnapshotExpectedInputs,
    AuthorizationSnapshotVerificationProof,
    KillSwitchScopeBinding,
    KillSwitchSnapshotEntry,
    KillSwitchVerificationProof,
    authorization_snapshot_bound_input_digest,
    authorization_snapshot_record_digest,
)
from corvus.application.projects import (
    CreateProjectCommand,
    InProcessProjectClient,
    ProjectRepositoryAdapter,
    ProjectService,
)
from corvus.database import M1_AUTHORITY_FAMILY_NAMES
from corvus.domain.access import (
    AccessBundle,
    AgentGrant,
    CapabilityEffect,
    CapabilityGrant,
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
    AuthorityMode,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorKind,
    AuthProfile,
    DeploymentInstance,
    DeploymentInstanceLease,
    DeploymentProfile,
    NetworkProfile,
    StorageProfile,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
    fixed_workspace_lock_name,
)
from corvus.domain.identity import Project
from corvus.domain.request import RequestContext
from corvus.domain.scope import AudiencePolicySnapshot
from corvus.infrastructure.authority_root import AuthorityRootCalculator
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.local_authority import SealedLocalAuthorityAnchor
from corvus.infrastructure.project_audit import SignedProjectAuditAdapter
from corvus.infrastructure.project_authority import ManifestProjectAuthorityMutationPlanner
from corvus.infrastructure.project_authorization import (
    VerifiedProjectAuthorizationAdapter,
    VerifiedProjectAuthorizationInputs,
)
from corvus.infrastructure.project_recovery import RecoverableProjectCreateLifecycle
from corvus.infrastructure.repositories.audit import AuditRepository
from corvus.infrastructure.repositories.authority import AuthorityRepository
from corvus.infrastructure.repositories.authorization_inputs import AuthorizationInputRepository
from corvus.infrastructure.repositories.projects import ProjectRepository
from corvus.infrastructure.repositories.registry import RegistryManifestRepository
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
_ACTIVE_MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000012")


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value


class StaticVerifiedInputs:
    def __init__(self, value: VerifiedProjectAuthorizationInputs) -> None:
        self.value = value

    def resolve(self, _request) -> VerifiedProjectAuthorizationInputs:
        return self.value


class BootstrapAwareLiveRootVerifier:
    def __init__(self, database: Path, bootstrap_generation: int) -> None:
        self.calculator = AuthorityRootCalculator(database)
        self.bootstrap_generation = bootstrap_generation

    def verify_live_root(
        self,
        *,
        workspace_id: UUID,
        authority_generation: int,
        expected_root: str,
    ) -> object:
        commitments = self.calculator.registry.list_leaf_commitments(
            workspace_id=workspace_id,
            authority_generation=authority_generation,
        )
        if not commitments and authority_generation == self.bootstrap_generation:
            return object()
        return self.calculator.verify_live_root(
            workspace_id=workspace_id,
            authority_generation=authority_generation,
            expected_root=expected_root,
        )


class WorkspaceSigner:
    def __init__(self, key_id: UUID, private_key: Ed25519PrivateKey) -> None:
        self.signing_key_version_id = key_id
        self.private_key = private_key

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


def _private_value(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def _public_value(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def test_inprocess_client_uses_real_authority_snapshot_recovery_and_persistence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    authorities = AuthorityRepository(database)
    audits = AuditRepository(database)
    authorization_inputs = AuthorizationInputRepository(database)
    projects = ProjectRepository(database)
    commitments = RegistryManifestRepository(database)
    manifest = commitments.get_manifest(_ACTIVE_MANIFEST_ID)
    assert manifest is not None
    manifest_families = tuple(commitments.list_manifest_families(manifest.id))
    assert {family.family_name for family in manifest_families} == M1_AUTHORITY_FAMILY_NAMES

    workspace_id = uuid4()
    requester_id = uuid4()
    agent_id = uuid4()
    project = Project(
        workspace_id=workspace_id,
        name="Real Vertical Corvus",
        root_locator="workspace://real-vertical-corvus",
        privacy="private",
        created_at=_NOW,
        updated_at=_NOW,
    )
    profile = DeploymentProfile(
        authority_mode=AuthorityMode.EMBEDDED_LOCAL,
        auth_profile=AuthProfile.LOCAL_OS,
        network_profile=NetworkProfile.IN_PROCESS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters=frozenset({"cli"}),
        protocol_version="v2alpha1",
        created_at=_NOW - timedelta(minutes=10),
        updated_at=_NOW - timedelta(minutes=10),
    )
    instance_key = Ed25519PrivateKey.generate()
    epoch_key = Ed25519PrivateKey.generate()
    instance_ref = "keyring://vertical/instance"
    epoch_ref = "keyring://vertical/epoch"
    state_ref = "keyring://vertical/state"
    instance = DeploymentInstance(
        deployment_profile_id=profile.id,
        instance_public_key=_public_value(instance_key),
        non_exportable_activation_key_ref=instance_ref,
        device_binding_digest="a" * 64,
        activated_at=_NOW - timedelta(minutes=9),
    )
    credential = AuthorityEpochCredential(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        public_key=_public_value(epoch_key),
        non_exportable_private_key_ref=epoch_ref,
        device_binding_digest=instance.device_binding_digest,
        issued_at=_NOW - timedelta(minutes=8),
    )
    trust_anchor = AuthorityTrustAnchor(
        workspace_id=workspace_id,
        kind=AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION,
        local_lock_name=fixed_workspace_lock_name(workspace_id, 1),
        sealed_generation_ref=state_ref,
        device_binding_digest=instance.device_binding_digest,
        policy_digest="b" * 64,
        created_at=_NOW - timedelta(minutes=7),
    )
    lease = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        lock_name=fixed_workspace_lock_name(workspace_id, 1),
        fencing_token=1,
        acquired_at=_NOW - timedelta(minutes=6),
    )
    authority = WorkspaceAuthority(
        workspace_id=workspace_id,
        deployment_profile_id=profile.id,
        deployment_instance_id=instance.id,
        epoch=1,
        authority_generation=4,
        authority_state_root="c" * 64,
        authority_epoch_credential_id=credential.id,
        trust_anchor_id=trust_anchor.id,
        active_lease_id=lease.id,
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=_NOW - timedelta(minutes=5),
    )
    authorities.add_deployment_profile(profile)
    authorities.add_deployment_instance(instance)
    authorities.add_epoch_credential(credential)
    authorities.add_trust_anchor(trust_anchor)
    authorities.acquire_lease(lease)
    authorities.add_workspace_authority(authority)

    secrets = MemorySecretStore()
    secrets.set(instance_ref, _private_value(instance_key))
    secrets.set(epoch_ref, _private_value(epoch_key))
    anchor = SealedLocalAuthorityAnchor(
        authority=authority,
        trust_anchor=trust_anchor,
        deployment_instance=instance,
        epoch_credential=credential,
        secret_store=secrets,
        live_root_verifier=BootstrapAwareLiveRootVerifier(database, authority.authority_generation),
    )
    anchor.bootstrap()

    requester_bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=requester_id,
        scope_kind="project",
        scope_id=project.id,
        issued_by=requester_id,
        policy_digest="d" * 64,
        expires_at=_NOW + timedelta(hours=1),
    )
    requester_grant = CapabilityGrant(
        bundle_id=requester_bundle.id,
        workspace_id=workspace_id,
        resource_kind="project",
        resource_id=project.id,
        action="project.create",
        effect=CapabilityEffect.ALLOW,
    )
    agent_bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=agent_id,
        scope_kind="project",
        scope_id=project.id,
        issued_by=requester_id,
        policy_digest="e" * 64,
        expires_at=_NOW + timedelta(hours=1),
    )
    agent_capability = CapabilityGrant(
        bundle_id=agent_bundle.id,
        workspace_id=workspace_id,
        resource_kind="project",
        resource_id=project.id,
        action="project.create",
        effect=CapabilityEffect.ALLOW,
    )
    agent_grant = AgentGrant(
        workspace_id=workspace_id,
        agent_id=agent_id,
        capability_bundle_id=agent_bundle.id,
        autonomy_level=2,
        issued_by=requester_id,
        expires_at=_NOW + timedelta(hours=1),
    )
    request_id = uuid4()
    client_context_id = uuid4()
    transport_principal_id = requester_id
    kill_ids = (uuid4(), uuid4())
    kill_bindings = (
        KillSwitchScopeBinding(scope_kind="workspace", scope_id=workspace_id),
        KillSwitchScopeBinding(scope_kind="agent", scope_id=agent_id),
    )
    audience = AudiencePolicySnapshot(
        workspace_id=workspace_id,
        visibility="explicit_principals",
        principal_ids=frozenset({requester_id}),
        scope_digest="f" * 64,
        policy_version=1,
        policy_digest="0" * 64,
        created_by=requester_id,
        created_at=_NOW - timedelta(minutes=1),
    )
    authority_receipt_id = uuid4()
    authority_proof_digest = "1" * 64
    request = AuthorizationRequest(
        workspace_id=workspace_id,
        request_context_id=request_id,
        deployment_instance_id=instance.id,
        workspace_authority_epoch=authority.epoch,
        workspace_authority_generation=authority.authority_generation,
        authority_state_root=authority.authority_state_root,
        authority_epoch_credential_id=credential.id,
        authority_commit_receipt_id=authority_receipt_id,
        authority_proof_digest=authority_proof_digest,
        trust_anchor_id=trust_anchor.id,
        authority_manifest_version_id=manifest.id,
        authority_manifest_digest=manifest.manifest_digest,
        kill_switch_snapshot_ids=kill_ids,
        kill_switch_snapshot_digest="2" * 64,
        kill_switch_scope_bindings=kill_bindings,
        audience_policy_snapshot_id=audience.id,
        audience_policy_digest=audience.policy_digest,
        scope_digest=audience.scope_digest,
        client_context_id=client_context_id,
        client_surface=ClientSurface.CLI,
        transport_principal_id=transport_principal_id,
        requester_id=requester_id,
        acting_agent_id=agent_id,
        scope_kind="project",
        scope_id=project.id,
        resource_kind="project",
        resource_id=project.id,
        action="project.create",
        evaluated_at=_NOW,
    )
    runtime_proof = anchor.issue_runtime_proof(
        request,
        nonce_digest="3" * 64,
        expires_at=_NOW + timedelta(seconds=15),
    )
    kill_entries = tuple(
        KillSwitchSnapshotEntry(
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            scope_kind=binding.scope_kind,
            scope_id=binding.scope_id,
            state="clear",
            version=1,
            updated_at=_NOW - timedelta(seconds=1),
        )
        for snapshot_id, binding in zip(kill_ids, kill_bindings, strict=True)
    )
    authority_context = AuthorityEvaluationContext(
        deployment_instance=instance,
        workspace_authority=authority,
        epoch_credential=credential,
        active_lease=lease,
        commit_proof=AuthorityCommitProof(
            workspace_id=workspace_id,
            deployment_instance_id=instance.id,
            authority_epoch_credential_id=credential.id,
            authority_epoch=authority.epoch,
            authority_generation=authority.authority_generation,
            authority_state_root=authority.authority_state_root,
            authority_commit_receipt_id=authority_receipt_id,
            authority_proof_digest=authority_proof_digest,
            finalized=True,
        ),
        trust_anchor=trust_anchor,
        authority_manifest=manifest,
        authority_manifest_families=manifest_families,
        mutable_authority_families=frozenset(M1_AUTHORITY_FAMILY_NAMES),
        kill_switch_verification_proof=KillSwitchVerificationProof(
            request_context_id=request_id,
            workspace_id=workspace_id,
            acting_agent_id=agent_id,
            action="project.create",
            kill_switch_snapshot_ids=kill_ids,
            kill_switch_snapshot_digest=request.kill_switch_snapshot_digest,
            required_scope_bindings=kill_bindings,
            entries=kill_entries,
            observed_at=_NOW - timedelta(seconds=1),
            expires_at=_NOW + timedelta(minutes=1),
            hierarchy_exhaustive=True,
            finalized=True,
        ),
        audience_policy_snapshot=audience,
        requester_role_ids=frozenset(),
        client_context=ClientContext(
            id=client_context_id,
            surface=ClientSurface.CLI,
            transport_principal_id=transport_principal_id,
            session_id=uuid4(),
            origin="contract",
            issued_at=_NOW - timedelta(minutes=1),
            expires_at=_NOW + timedelta(minutes=1),
        ),
        enabled_client_surfaces=frozenset(ClientSurface),
        runtime_possession_proof=runtime_proof,
    )

    signing_private_key = Ed25519PrivateKey.generate()
    signing_key = WorkspaceSigningKeyVersion(
        workspace_id=workspace_id,
        key_epoch=1,
        algorithm="ed25519",
        public_key=_public_value(signing_private_key),
        non_exportable_private_key_ref="keyring://vertical/signing",
        status=SigningKeyStatus.ACTIVE,
        valid_from=_NOW - timedelta(minutes=2),
        attestation_digest="4" * 64,
        created_at=_NOW - timedelta(minutes=2),
    )
    canonical_inputs = {
        "action": request.action,
        "resource": f"project:{project.id}",
    }
    source_versions = {"access_bundle": 1, "agent_grant": 1}
    snapshot = AuthorizationDecisionSnapshot(
        workspace_id=workspace_id,
        request_context_id=request_id,
        deployment_instance_id=instance.id,
        authority_epoch_credential_id=credential.id,
        authority_generation=authority.authority_generation,
        authority_state_root=authority.authority_state_root,
        authority_commit_receipt_id=authority_receipt_id,
        authority_proof_digest=authority_proof_digest,
        membership_version_ids=(uuid4(),),
        membership_digest="5" * 64,
        scope_kind="project",
        scope_id=project.id,
        scope_digest=request.scope_digest,
        audience_policy_snapshot_id=audience.id,
        audience_digest=audience.policy_digest,
        requester_id=requester_id,
        transport_principal_id=transport_principal_id,
        access_bundle_id=requester_bundle.id,
        access_bundle_version_digest="6" * 64,
        agent_grant_id=agent_grant.id,
        agent_delegation_digest="7" * 64,
        policy_digest=requester_bundle.policy_digest,
        autonomy_policy_digest="8" * 64,
        budget_snapshot_ids=(),
        budget_snapshot_digest="9" * 64,
        kill_switch_snapshot_ids=kill_ids,
        kill_switch_snapshot_digest=request.kill_switch_snapshot_digest,
        decision="allow",
        reason_code="exact_capability_intersection",
        canonical_inputs_json=canonical_inputs,
        source_record_version_map=source_versions,
        canonical_digest=authorization_snapshot_digest(canonical_inputs, source_versions),
        signing_key_version_id=signing_key.id,
        snapshot_signature="pending",
        created_at=_NOW - timedelta(seconds=1),
    )
    record_digest = authorization_snapshot_record_digest(snapshot)
    snapshot = snapshot.model_copy(
        update={
            "snapshot_signature": base64.b64encode(
                signing_private_key.sign(bytes.fromhex(record_digest))
            ).decode("ascii")
        }
    )
    expected = AuthorizationSnapshotExpectedInputs(
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=record_digest,
        bound_input_digest=authorization_snapshot_bound_input_digest(snapshot),
        signing_key_version_id=signing_key.id,
        verified_at=_NOW,
    )
    verification = AuthorizationSnapshotVerificationProof(
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=record_digest,
        bound_input_digest=expected.bound_input_digest,
        signing_key_version_id=signing_key.id,
        finalized=True,
    )
    audits.append_snapshot(snapshot)

    project_context = RequestContext(
        id=request_id,
        deployment_profile_id=profile.id,
        deployment_instance_id=instance.id,
        workspace_id=workspace_id,
        workspace_authority_epoch=authority.epoch,
        workspace_authority_generation=authority.authority_generation,
        authority_state_root=authority.authority_state_root,
        authority_epoch_credential_id=credential.id,
        authority_commit_receipt_id=authority_receipt_id,
        authority_proof_digest=authority_proof_digest,
        scope_kind="project",
        scope_id=project.id,
        scope_digest=request.scope_digest,
        audience_policy_snapshot_id=audience.id,
        audience_policy_digest=audience.policy_digest,
        requester_id=requester_id,
        client_context_id=client_context_id,
        transport_principal_id=transport_principal_id,
        agent_id=agent_id,
        agent_grant_id=agent_grant.id,
        access_bundle_id=requester_bundle.id,
        policy_digest=requester_bundle.policy_digest,
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=record_digest,
        authorization_signing_key_version_id=signing_key.id,
        idempotency_key="real-vertical-project-create",
        correlation_id=uuid4(),
    )
    inputs = StaticVerifiedInputs(
        VerifiedProjectAuthorizationInputs(
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
    )
    audit_signer = WorkspaceSigner(signing_key.id, signing_private_key)
    audit_adapter = SignedProjectAuditAdapter(
        repository=audits,
        context_provider=None,
        signer=audit_signer,
        clock=lambda: _NOW,
    )
    lifecycle = RecoverableProjectCreateLifecycle(
        authority_repository=authorities,
        commitment_repository=commitments,
        audit_repository=audits,
        project_repository=projects,
        audit_adapter=audit_adapter,
        anchor=anchor,
        mutation_planner=ManifestProjectAuthorityMutationPlanner(database),
        signer=audit_signer,
        clock=lambda: _NOW,
    )
    client = InProcessProjectClient(
        ProjectService(
            store=ProjectRepositoryAdapter(projects),
            authorization=VerifiedProjectAuthorizationAdapter(inputs=inputs, snapshots=audits),
            audit=audit_adapter,
            create_lifecycle=lifecycle,
            idempotency=authorization_inputs,
            clock=lambda: _NOW,
        )
    )
    command = CreateProjectCommand(
        context=project_context,
        client_surface=ClientSurface.CLI,
        project=project,
    )

    created = client.create_project(command)
    replayed = client.create_project(command)

    assert created.ok is True
    assert replayed == created
    assert projects.get(workspace_id=workspace_id, project_id=project.id) == project
    assert len(audits.list_receipts(workspace_id)) == 1
    with sqlite3.connect(database) as connection:
        checkpoint_payloads = connection.execute(
            "SELECT payload_json FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchall()
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM audit_result_bindings WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
    assert len(checkpoint_payloads) == 1
    assert '"state":"complete"' in checkpoint_payloads[0][0]
    assert binding_count == (1,)
    advanced = authorities.get_workspace_authority(workspace_id)
    assert advanced is not None
    assert advanced.authority_generation == authority.authority_generation + 1
    assert len(
        commitments.list_leaf_commitments(
            workspace_id=workspace_id,
            authority_generation=advanced.authority_generation,
        )
    ) == len(M1_AUTHORITY_FAMILY_NAMES)
    fresh_request = request.model_copy(
        update={
            "workspace_authority_generation": advanced.authority_generation,
            "authority_state_root": advanced.authority_state_root,
        }
    )
    fresh_proof = anchor.issue_runtime_proof(
        fresh_request,
        nonce_digest="f" * 64,
        expires_at=_NOW + timedelta(minutes=5),
    )
    assert fresh_proof.authority_generation == advanced.authority_generation
    assert anchor.current_audit_history_heads() == audits.current_history_heads(workspace_id)
