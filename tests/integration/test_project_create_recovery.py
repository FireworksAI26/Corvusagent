from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corvus.application.ports import (
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
)
from corvus.application.projects import (
    CreateProjectCommand,
    ProjectRepositoryAdapter,
    ProjectService,
)
from corvus.database import M1_AUTHORITY_FAMILY_NAMES
from corvus.domain.audit import (
    AuditResultBinding,
    AuthorizationDecisionSnapshot,
    authorization_snapshot_digest,
)
from corvus.domain.client import ClientSurface
from corvus.domain.deployment import (
    AuthorityCommitIntent,
    AuthorityCommitState,
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
from corvus.infrastructure.authority_root import AuthorityRootCalculator
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.project_audit import SignedProjectAuditAdapter
from corvus.infrastructure.project_authority import ManifestProjectAuthorityMutationPlanner
from corvus.infrastructure.project_recovery import (
    AuthorityCommitReceiptEvidence,
    RecoverableProjectCreateLifecycle,
    audit_result_binding_hash,
)
from corvus.infrastructure.repositories.audit import AuditRepository
from corvus.infrastructure.repositories.authority import AuthorityRepository
from corvus.infrastructure.repositories.projects import ProjectRepository
from corvus.infrastructure.repositories.registry import RegistryManifestRepository
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _authority(repository: AuthorityRepository, workspace_id: UUID) -> WorkspaceAuthority:
    profile = DeploymentProfile(
        authority_mode=AuthorityMode.EMBEDDED_LOCAL,
        auth_profile=AuthProfile.LOCAL_OS,
        network_profile=NetworkProfile.IN_PROCESS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters=frozenset({"cli"}),
        protocol_version="v2alpha1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    instance = DeploymentInstance(
        deployment_profile_id=profile.id,
        instance_public_key="instance-public-key",
        non_exportable_activation_key_ref="keyring://corvus/instance/current",
        device_binding_digest="a" * 64,
        activated_at=_NOW,
    )
    credential = AuthorityEpochCredential(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        public_key="epoch-public-key",
        non_exportable_private_key_ref="keyring://corvus/epoch/current",
        device_binding_digest=instance.device_binding_digest,
        issued_at=_NOW,
    )
    anchor = AuthorityTrustAnchor(
        workspace_id=workspace_id,
        kind=AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION,
        local_lock_name=fixed_workspace_lock_name(workspace_id, 1),
        sealed_generation_ref="keyring://corvus/sealed-generation/current",
        device_binding_digest=instance.device_binding_digest,
        policy_digest="b" * 64,
        created_at=_NOW,
    )
    lease = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        lock_name=fixed_workspace_lock_name(workspace_id, 1),
        fencing_token=1,
        acquired_at=_NOW,
    )
    authority = WorkspaceAuthority(
        workspace_id=workspace_id,
        deployment_profile_id=profile.id,
        deployment_instance_id=instance.id,
        epoch=1,
        authority_generation=4,
        authority_state_root="c" * 64,
        authority_epoch_credential_id=credential.id,
        trust_anchor_id=anchor.id,
        active_lease_id=lease.id,
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=_NOW,
    )
    repository.add_deployment_profile(profile)
    repository.add_deployment_instance(instance)
    repository.add_epoch_credential(credential)
    repository.add_trust_anchor(anchor)
    repository.acquire_lease(lease)
    repository.add_workspace_authority(authority)
    return authority


def _snapshot(
    *,
    request_id: UUID,
    project: Project,
    requester_id: UUID,
    signing_key_id: UUID,
    private_key: Ed25519PrivateKey,
    authority: WorkspaceAuthority,
) -> AuthorizationDecisionSnapshot:
    canonical_inputs = {
        "action": "project.create",
        "resource": f"project:{project.id}",
    }
    source_versions = {"access_bundle": 1, "agent_grant": 1}
    canonical_digest = authorization_snapshot_digest(canonical_inputs, source_versions)
    unsigned = AuthorizationDecisionSnapshot(
        workspace_id=project.workspace_id,
        request_context_id=request_id,
        deployment_instance_id=authority.deployment_instance_id,
        authority_epoch_credential_id=authority.authority_epoch_credential_id,
        authority_generation=authority.authority_generation,
        authority_state_root=authority.authority_state_root,
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="d" * 64,
        membership_version_ids=(uuid4(),),
        membership_digest="e" * 64,
        scope_kind="project",
        scope_id=project.id,
        scope_digest="f" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_digest="0" * 64,
        requester_id=requester_id,
        transport_principal_id=uuid4(),
        access_bundle_id=uuid4(),
        access_bundle_version_digest="1" * 64,
        agent_grant_id=uuid4(),
        agent_delegation_digest="2" * 64,
        policy_digest="3" * 64,
        autonomy_policy_digest="4" * 64,
        budget_snapshot_ids=(uuid4(),),
        budget_snapshot_digest="5" * 64,
        kill_switch_snapshot_ids=(uuid4(),),
        kill_switch_snapshot_digest="6" * 64,
        decision="allow",
        reason_code="authorized",
        canonical_inputs_json=canonical_inputs,
        source_record_version_map=source_versions,
        canonical_digest=canonical_digest,
        signing_key_version_id=signing_key_id,
        snapshot_signature="pending",
        created_at=_NOW,
    )
    signature = base64.b64encode(private_key.sign(unsigned.canonical_digest.encode())).decode()
    return unsigned.model_copy(update={"snapshot_signature": signature})


class Ed25519Signer:
    def __init__(self, key_id: UUID, private_key: Ed25519PrivateKey) -> None:
        self.signing_key_version_id = key_id
        self.private_key = private_key

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


class FixedAuthorization:
    def __init__(self, snapshot: AuthorizationDecisionSnapshot) -> None:
        self.snapshot = snapshot

    def authorize(self, request: ProjectAuthorizationRequest) -> ProjectAuthorizationDecision:
        assert request.context.id == self.snapshot.request_context_id
        assert request.context.authorization_snapshot_id == self.snapshot.id
        return ProjectAuthorizationDecision(
            allowed=True,
            reason_code="authorized",
            authorization_snapshot_id=request.context.authorization_snapshot_id,
        )


class IdempotentAnchor:
    def __init__(self) -> None:
        self.reserved: set[UUID] = set()
        self.finalized: set[UUID] = set()

    def reserve(self, intent: AuthorityCommitIntent) -> None:
        self.reserved.add(intent.id)

    def finalize(self, intent: AuthorityCommitIntent) -> AuthorityCommitReceiptEvidence:
        self.finalized.add(intent.id)
        return AuthorityCommitReceiptEvidence(
            id=UUID("00000000-0000-0000-0000-000000000099"),
            digest="9" * 64,
        )


class FailCommitmentOnce:
    def __init__(self, repository: RegistryManifestRepository) -> None:
        self.repository = repository
        self.failed = False

    def list_leaf_commitments(self, *, workspace_id: UUID, authority_generation: int):
        return self.repository.list_leaf_commitments(
            workspace_id=workspace_id,
            authority_generation=authority_generation,
        )

    def append_leaf_commitments(self, **kwargs) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected commitment persistence failure")
        self.repository.append_leaf_commitments(**kwargs)


class CorruptCommitmentRead:
    def __init__(self, repository: RegistryManifestRepository) -> None:
        self.repository = repository

    def list_leaf_commitments(self, *, workspace_id: UUID, authority_generation: int):
        commitments = self.repository.list_leaf_commitments(
            workspace_id=workspace_id,
            authority_generation=authority_generation,
        )
        if commitments:
            commitments[0] = commitments[0].model_copy(update={"leaf_digest": "0" * 64})
        return commitments

    def append_leaf_commitments(self, **kwargs) -> None:
        self.repository.append_leaf_commitments(**kwargs)


class FailDbCommitOnce:
    def __init__(self, repository: AuthorityRepository) -> None:
        self.repository = repository
        self.failed = False

    def get_workspace_authority(self, workspace_id: UUID):
        return self.repository.get_workspace_authority(workspace_id)

    def get_commit_intent(self, *, workspace_id: UUID, intent_id: UUID):
        return self.repository.get_commit_intent(workspace_id=workspace_id, intent_id=intent_id)

    def prepare_commit(self, intent: AuthorityCommitIntent) -> None:
        self.repository.prepare_commit(intent)

    def advance_commit(
        self,
        intent: AuthorityCommitIntent,
        *,
        expected_state: AuthorityCommitState,
    ) -> None:
        if intent.state is AuthorityCommitState.DB_COMMITTED and not self.failed:
            self.failed = True
            raise RuntimeError("injected authority DB commit failure")
        self.repository.advance_commit(intent, expected_state=expected_state)


class CrashAfterCall:
    def __init__(
        self,
        target: object,
        method_name: str,
        predicate: Callable[[tuple[object, ...], dict[str, object]], bool] | None = None,
    ) -> None:
        self.target = target
        self.method_name = method_name
        self.predicate = predicate
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.target, name)
        if name != self.method_name or not callable(attribute):
            return attribute

        def crash_after(*args: object, **kwargs: object) -> object:
            result = attribute(*args, **kwargs)
            matches = self.predicate is None or self.predicate(args, kwargs)
            if matches and not self.failed:
                self.failed = True
                raise RuntimeError(f"injected crash after {self.method_name}")
            return result

        return crash_after


def _request_context(
    *,
    request_id: UUID,
    project: Project,
    requester_id: UUID,
    acting_agent_id: UUID,
    authority: WorkspaceAuthority,
    snapshot: AuthorizationDecisionSnapshot,
    idempotency_key: str,
) -> RequestContext:
    return RequestContext(
        id=request_id,
        deployment_profile_id=authority.deployment_profile_id,
        deployment_instance_id=snapshot.deployment_instance_id,
        workspace_id=project.workspace_id,
        workspace_authority_epoch=authority.epoch,
        workspace_authority_generation=snapshot.authority_generation,
        authority_state_root=snapshot.authority_state_root,
        authority_epoch_credential_id=snapshot.authority_epoch_credential_id,
        authority_commit_receipt_id=snapshot.authority_commit_receipt_id,
        authority_proof_digest=snapshot.authority_proof_digest,
        scope_kind=snapshot.scope_kind,
        scope_id=snapshot.scope_id,
        scope_digest=snapshot.scope_digest,
        audience_policy_snapshot_id=snapshot.audience_policy_snapshot_id,
        audience_policy_digest=snapshot.audience_digest,
        requester_id=requester_id,
        client_context_id=uuid4(),
        transport_principal_id=snapshot.transport_principal_id,
        agent_id=acting_agent_id,
        agent_grant_id=snapshot.agent_grant_id,
        access_bundle_id=snapshot.access_bundle_id,
        policy_digest=snapshot.policy_digest,
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=snapshot.canonical_digest,
        authorization_signing_key_version_id=snapshot.signing_key_version_id,
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
    )


@dataclass(frozen=True)
class ProjectCreateScenario:
    database: Path
    authority_repository: AuthorityRepository
    commitment_repository: RegistryManifestRepository
    audit_repository: AuditRepository
    project_repository: ProjectRepository
    project: Project
    authority: WorkspaceAuthority
    command: CreateProjectCommand
    anchor: IdempotentAnchor
    signer: Ed25519Signer
    snapshot: AuthorizationDecisionSnapshot


def _scenario(tmp_path: Path) -> ProjectCreateScenario:
    database = _database(tmp_path)
    authority_repository = AuthorityRepository(database)
    commitment_repository = RegistryManifestRepository(database)
    audit_repository = AuditRepository(database)
    project_repository = ProjectRepository(database)
    project = Project(
        workspace_id=uuid4(),
        name="Crash-point Corvus",
        root_locator="workspace://crash-point-corvus",
        privacy="private",
    )
    authority = _authority(authority_repository, project.workspace_id)
    request_id = uuid4()
    requester_id = uuid4()
    signing_key_id = uuid4()
    private_key = Ed25519PrivateKey.generate()
    signer = Ed25519Signer(signing_key_id, private_key)
    snapshot = _snapshot(
        request_id=request_id,
        project=project,
        requester_id=requester_id,
        signing_key_id=signing_key_id,
        private_key=private_key,
        authority=authority,
    )
    audit_repository.append_snapshot(snapshot)
    acting_agent_id = uuid4()
    return ProjectCreateScenario(
        database=database,
        authority_repository=authority_repository,
        commitment_repository=commitment_repository,
        audit_repository=audit_repository,
        project_repository=project_repository,
        project=project,
        authority=authority,
        command=CreateProjectCommand(
            context=_request_context(
                request_id=request_id,
                project=project,
                requester_id=requester_id,
                acting_agent_id=acting_agent_id,
                authority=authority,
                snapshot=snapshot,
                idempotency_key="project-create-recovery",
            ),
            client_surface=ClientSurface.CLI,
            project=project,
        ),
        anchor=IdempotentAnchor(),
        signer=signer,
        snapshot=snapshot,
    )


def _service(
    scenario: ProjectCreateScenario,
    *,
    authority_repository: object | None = None,
    commitment_repository: object | None = None,
    audit_repository: object | None = None,
    project_repository: object | None = None,
    anchor: object | None = None,
) -> ProjectService:
    lifecycle = _lifecycle(
        database=scenario.database,
        authority=authority_repository or scenario.authority_repository,
        commitment_repository=commitment_repository or scenario.commitment_repository,
        audit_repository=audit_repository or scenario.audit_repository,
        project_repository=project_repository or scenario.project_repository,
        anchor=anchor or scenario.anchor,
        signer=scenario.signer,
    )
    return ProjectService(
        store=ProjectRepositoryAdapter(scenario.project_repository),
        authorization=FixedAuthorization(scenario.snapshot),
        audit=lifecycle.audit_adapter,
        create_lifecycle=lifecycle,
    )


def _lifecycle(
    *,
    database: Path,
    authority,
    commitment_repository,
    audit_repository: AuditRepository,
    project_repository: ProjectRepository,
    anchor: IdempotentAnchor,
    signer: Ed25519Signer,
) -> RecoverableProjectCreateLifecycle:
    return RecoverableProjectCreateLifecycle(
        authority_repository=authority,
        commitment_repository=commitment_repository,
        audit_repository=audit_repository,
        project_repository=project_repository,
        audit_adapter=SignedProjectAuditAdapter(
            repository=audit_repository,
            context_provider=None,
            signer=signer,
            clock=lambda: _NOW,
        ),
        anchor=anchor,
        mutation_planner=ManifestProjectAuthorityMutationPlanner(database),
        signer=signer,
        clock=lambda: _NOW,
    )


def test_project_create_recovers_after_project_commit_before_authority_commit(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    authority_repository = AuthorityRepository(database)
    audit_repository = AuditRepository(database)
    project_repository = ProjectRepository(database)
    project = Project(
        workspace_id=uuid4(),
        name="Recoverable Corvus",
        root_locator="workspace://recoverable-corvus",
        privacy="private",
    )
    authority = _authority(authority_repository, project.workspace_id)
    request_id = uuid4()
    requester_id = uuid4()
    signing_key_id = uuid4()
    private_key = Ed25519PrivateKey.generate()
    signer = Ed25519Signer(signing_key_id, private_key)
    snapshot = _snapshot(
        request_id=request_id,
        project=project,
        requester_id=requester_id,
        signing_key_id=signing_key_id,
        private_key=private_key,
        authority=authority,
    )
    audit_repository.append_snapshot(snapshot)
    acting_agent_id = uuid4()
    command = CreateProjectCommand(
        context=_request_context(
            request_id=request_id,
            project=project,
            requester_id=requester_id,
            acting_agent_id=acting_agent_id,
            authority=authority,
            snapshot=snapshot,
            idempotency_key="recoverable-project-create",
        ),
        client_surface=ClientSurface.CLI,
        project=project,
    )
    anchor = IdempotentAnchor()
    commitment_repository = RegistryManifestRepository(database)
    fail_commitments = FailCommitmentOnce(commitment_repository)
    commitment_lifecycle = _lifecycle(
        database=database,
        authority=authority_repository,
        commitment_repository=fail_commitments,
        audit_repository=audit_repository,
        project_repository=project_repository,
        anchor=anchor,
        signer=signer,
    )
    commitment_service = ProjectService(
        store=ProjectRepositoryAdapter(project_repository),
        authorization=FixedAuthorization(snapshot),
        audit=commitment_lifecycle.audit_adapter,
        create_lifecycle=commitment_lifecycle,
    )

    commitment_failed = commitment_service.create(command)

    assert commitment_failed.ok is False
    assert commitment_failed.reason_code == "authority_commitment_persistence_failed"
    unchanged = authority_repository.get_workspace_authority(project.workspace_id)
    assert unchanged == authority
    assert (
        commitment_repository.list_leaf_commitments(
            workspace_id=project.workspace_id,
            authority_generation=authority.authority_generation + 1,
        )
        == []
    )

    fail_once = FailDbCommitOnce(authority_repository)
    failing_lifecycle = _lifecycle(
        database=database,
        authority=fail_once,
        commitment_repository=commitment_repository,
        audit_repository=audit_repository,
        project_repository=project_repository,
        anchor=anchor,
        signer=signer,
    )
    failing_service = ProjectService(
        store=ProjectRepositoryAdapter(project_repository),
        authorization=FixedAuthorization(snapshot),
        audit=failing_lifecycle.audit_adapter,
        create_lifecycle=failing_lifecycle,
    )

    failed = failing_service.create(command)

    assert failed.ok is False
    assert failed.reason_code == "authority_commit_failed"

    assert (
        project_repository.get(
            workspace_id=project.workspace_id,
            project_id=project.id,
        )
        is None
    )
    assert (
        project_repository.get_staged(workspace_id=project.workspace_id, project_id=project.id)
        == project
    )
    assert len(audit_repository.list_receipts(project.workspace_id)) == 1
    planned_commitments = commitment_repository.list_leaf_commitments(
        workspace_id=project.workspace_id,
        authority_generation=authority.authority_generation + 1,
    )
    assert len(planned_commitments) == len(M1_AUTHORITY_FAMILY_NAMES)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state FROM authority_commit_intents WHERE workspace_id = ?",
            (str(project.workspace_id),),
        ).fetchone() == (AuthorityCommitState.ANCHOR_RESERVED.value,)
        assert connection.execute(
            "SELECT state FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(project.workspace_id),),
        ).fetchone() == ("prepared",)

    lifecycle = _lifecycle(
        database=database,
        authority=authority_repository,
        commitment_repository=commitment_repository,
        audit_repository=audit_repository,
        project_repository=project_repository,
        anchor=anchor,
        signer=signer,
    )
    service = ProjectService(
        store=ProjectRepositoryAdapter(project_repository),
        authorization=FixedAuthorization(snapshot),
        audit=lifecycle.audit_adapter,
        create_lifecycle=lifecycle,
    )
    recovered = service.create(command)
    replayed = service.create(command)

    assert recovered.ok is True
    assert replayed.ok is True
    assert (
        project_repository.get(workspace_id=project.workspace_id, project_id=project.id) == project
    )

    advanced = authority_repository.get_workspace_authority(project.workspace_id)
    assert advanced is not None
    assert advanced.authority_generation == authority.authority_generation + 1
    verified = AuthorityRootCalculator(database).calculate(
        workspace_id=project.workspace_id,
        authority_generation=advanced.authority_generation,
        external_proof_digests={
            "audit_anchor_recovery_checkpoints": snapshot.authority_proof_digest,
            "audit_result_bindings": snapshot.authority_proof_digest,
            "authority_registry_freshness_proofs": snapshot.authority_proof_digest,
        },
    )
    assert verified.root_digest == advanced.authority_state_root
    assert list(verified.commitments) == planned_commitments
    assert len(audit_repository.list_receipts(project.workspace_id)) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state FROM authority_commit_intents WHERE workspace_id = ?",
            (str(project.workspace_id),),
        ).fetchone() == (AuthorityCommitState.ANCHOR_FINALIZED.value,)
        assert connection.execute(
            "SELECT state FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(project.workspace_id),),
        ).fetchone() == ("complete",)
        binding_row = connection.execute(
            "SELECT payload_json FROM audit_result_bindings"
        ).fetchone()
        assert binding_row is not None
        binding = AuditResultBinding.model_validate_json(binding_row[0])
        assert connection.execute("SELECT COUNT(*) FROM audit_result_bindings").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
    assert binding.binding_hash == audit_result_binding_hash(binding)
    private_key.public_key().verify(
        base64.b64decode(binding.binding_signature),
        bytes.fromhex(binding.binding_hash),
    )
    assert len(anchor.reserved) == 1
    assert len(anchor.finalized) == 1


def _state_is(expected: str) -> Callable[[tuple[object, ...], dict[str, object]], bool]:
    def matches(args: tuple[object, ...], _kwargs: dict[str, object]) -> bool:
        state = getattr(args[0], "state", None)
        return getattr(state, "value", None) == expected

    return matches


@pytest.mark.parametrize(
    ("component", "method_name", "persisted_state", "reason_code"),
    [
        ("audit", "append_receipt", None, "audit_persistence_failed"),
        ("authority", "prepare_commit", None, "authority_prepare_failed"),
        ("anchor", "reserve", None, "authority_reservation_failed"),
        (
            "authority",
            "advance_commit",
            AuthorityCommitState.ANCHOR_RESERVED.value,
            "authority_reservation_failed",
        ),
        ("audit", "prepare_recovery", None, "audit_recovery_prepare_failed"),
        ("project", "add_idempotent", None, "project_persistence_failed"),
        (
            "commitment",
            "append_leaf_commitments",
            None,
            "authority_commitment_persistence_failed",
        ),
        (
            "authority",
            "advance_commit",
            AuthorityCommitState.DB_COMMITTED.value,
            "authority_commit_failed",
        ),
        ("anchor", "finalize", None, "authority_finalize_failed"),
        (
            "authority",
            "advance_commit",
            AuthorityCommitState.ANCHOR_FINALIZED.value,
            "authority_finalize_failed",
        ),
        (
            "audit",
            "advance_recovery",
            "authority_finalized",
            "audit_recovery_finalize_failed",
        ),
        ("audit", "append_result_binding", None, "audit_result_binding_failed"),
        (
            "audit",
            "advance_recovery",
            "binding_persisted",
            "audit_binding_checkpoint_failed",
        ),
        ("audit", "advance_recovery", "complete", "audit_completion_failed"),
    ],
    ids=[
        "receipt",
        "prepared-intent",
        "external-reservation",
        "reserved-intent",
        "recovery-checkpoint",
        "project",
        "commitments",
        "database-authority",
        "external-finalization",
        "finalized-intent",
        "finalized-checkpoint",
        "result-binding",
        "binding-checkpoint",
        "complete-checkpoint",
    ],
)
def test_project_create_recovers_after_every_durable_crash_point(
    tmp_path: Path,
    component: str,
    method_name: str,
    persisted_state: str | None,
    reason_code: str,
) -> None:
    scenario = _scenario(tmp_path)
    target_by_component = {
        "authority": scenario.authority_repository,
        "commitment": scenario.commitment_repository,
        "audit": scenario.audit_repository,
        "project": scenario.project_repository,
        "anchor": scenario.anchor,
    }
    predicate = None if persisted_state is None else _state_is(persisted_state)
    crashing = CrashAfterCall(
        target_by_component[component],
        method_name,
        predicate,
    )
    service_kwargs: dict[str, object] = {}
    if component == "authority":
        service_kwargs["authority_repository"] = crashing
    elif component == "commitment":
        service_kwargs["commitment_repository"] = crashing
    elif component == "audit":
        service_kwargs["audit_repository"] = crashing
    elif component == "project":
        service_kwargs["project_repository"] = crashing
    else:
        service_kwargs["anchor"] = crashing

    failed = _service(scenario, **service_kwargs).create(scenario.command)

    assert failed.ok is False
    assert failed.reason_code == reason_code
    assert crashing.failed is True

    service = _service(scenario)
    recovered = service.create(scenario.command)
    replayed = service.create(scenario.command)

    assert recovered.ok is True
    assert replayed.ok is True
    advanced = scenario.authority_repository.get_workspace_authority(scenario.project.workspace_id)
    assert advanced is not None
    assert advanced.authority_generation == scenario.authority.authority_generation + 1
    verified = AuthorityRootCalculator(scenario.database).calculate(
        workspace_id=scenario.project.workspace_id,
        authority_generation=advanced.authority_generation,
        external_proof_digests={
            "audit_anchor_recovery_checkpoints": scenario.snapshot.authority_proof_digest,
            "audit_result_bindings": scenario.snapshot.authority_proof_digest,
            "authority_registry_freshness_proofs": scenario.snapshot.authority_proof_digest,
        },
    )
    assert verified.root_digest == advanced.authority_state_root
    commitments = scenario.commitment_repository.list_leaf_commitments(
        workspace_id=scenario.project.workspace_id,
        authority_generation=advanced.authority_generation,
    )
    assert list(verified.commitments) == commitments
    assert len(commitments) == len(M1_AUTHORITY_FAMILY_NAMES)
    assert len(scenario.audit_repository.list_receipts(scenario.project.workspace_id)) == 1
    with sqlite3.connect(scenario.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        assert connection.execute(
            "SELECT state FROM authority_commit_intents WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == (AuthorityCommitState.ANCHOR_FINALIZED.value,)
        assert connection.execute(
            "SELECT state FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == ("complete",)
        assert connection.execute("SELECT COUNT(*) FROM audit_result_bindings").fetchone() == (1,)
    assert len(scenario.anchor.reserved) == 1
    assert len(scenario.anchor.finalized) == 1


def test_commitment_replay_mismatch_quarantines_inflight_authority(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    failed = _service(
        scenario,
        authority_repository=FailDbCommitOnce(scenario.authority_repository),
    ).create(scenario.command)
    assert failed.reason_code == "authority_commit_failed"

    quarantined = _service(
        scenario,
        commitment_repository=CorruptCommitmentRead(scenario.commitment_repository),
    ).create(scenario.command)
    blocked = _service(scenario).create(scenario.command)

    assert quarantined.ok is False
    assert quarantined.reason_code == "authority_commitment_replay_mismatch"
    assert blocked.ok is False
    assert blocked.reason_code == "authority_commit_quarantined"
    assert (
        scenario.authority_repository.get_workspace_authority(scenario.project.workspace_id)
        == scenario.authority
    )
    with sqlite3.connect(scenario.database) as connection:
        assert connection.execute(
            "SELECT state FROM authority_commit_intents WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == (AuthorityCommitState.QUARANTINED.value,)
        assert connection.execute(
            "SELECT state FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == ("quarantined",)
        assert connection.execute("SELECT COUNT(*) FROM audit_result_bindings").fetchone() == (0,)


def test_post_commitment_mismatch_quarantines_advanced_workspace(
    tmp_path: Path,
) -> None:
    scenario = _scenario(tmp_path)
    crash_after_database_commit = CrashAfterCall(
        scenario.authority_repository,
        "advance_commit",
        _state_is(AuthorityCommitState.DB_COMMITTED.value),
    )
    failed = _service(
        scenario,
        authority_repository=crash_after_database_commit,
    ).create(scenario.command)
    assert failed.reason_code == "authority_commit_failed"

    advanced = scenario.authority_repository.get_workspace_authority(scenario.project.workspace_id)
    assert advanced is not None
    assert advanced.authority_generation == scenario.authority.authority_generation + 1
    assert advanced.state is WorkspaceAuthorityState.ACTIVE

    quarantined = _service(
        scenario,
        commitment_repository=CorruptCommitmentRead(scenario.commitment_repository),
    ).create(scenario.command)
    blocked = _service(scenario).create(scenario.command)

    assert quarantined.ok is False
    assert quarantined.reason_code == "authority_commitment_replay_mismatch"
    assert blocked.ok is False
    assert blocked.reason_code == "workspace_authority_quarantined"
    quarantined_authority = scenario.authority_repository.get_workspace_authority(
        scenario.project.workspace_id
    )
    assert quarantined_authority is not None
    assert quarantined_authority.authority_generation == advanced.authority_generation
    assert quarantined_authority.authority_state_root == advanced.authority_state_root
    assert quarantined_authority.state is WorkspaceAuthorityState.RESTORE_QUARANTINE
    with sqlite3.connect(scenario.database) as connection:
        assert connection.execute(
            "SELECT state FROM authority_commit_intents WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == (AuthorityCommitState.QUARANTINED.value,)
        assert connection.execute(
            "SELECT state FROM audit_anchor_recovery_checkpoints WHERE workspace_id = ?",
            (str(scenario.project.workspace_id),),
        ).fetchone() == ("quarantined",)
        assert connection.execute("SELECT COUNT(*) FROM audit_result_bindings").fetchone() == (0,)
