from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

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
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.authority import (
    AuthorityRepository,
    AuthorityRepositoryError,
)
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _AuthorityFixture:
    workspace_id: UUID
    profile: DeploymentProfile
    instance: DeploymentInstance
    credential: AuthorityEpochCredential
    trust_anchor: AuthorityTrustAnchor
    lease: DeploymentInstanceLease
    authority: WorkspaceAuthority


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _profile() -> DeploymentProfile:
    return DeploymentProfile(
        authority_mode=AuthorityMode.EMBEDDED_LOCAL,
        auth_profile=AuthProfile.LOCAL_OS,
        network_profile=NetworkProfile.IN_PROCESS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters=frozenset({"cli", "desktop"}),
        protocol_version="v2alpha1",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _instance(profile: DeploymentProfile, *, marker: str) -> DeploymentInstance:
    return DeploymentInstance(
        deployment_profile_id=profile.id,
        instance_public_key=f"instance-public-key-{marker}",
        non_exportable_activation_key_ref=f"keyring://corvus/instance/{marker}",
        device_binding_digest=marker * 64,
        activated_at=_NOW,
    )


def _fixture(
    repository: AuthorityRepository, *, workspace_id: UUID | None = None
) -> _AuthorityFixture:
    resolved_workspace_id = workspace_id or uuid4()
    profile = _profile()
    instance = _instance(profile, marker="a")
    credential = AuthorityEpochCredential(
        workspace_id=resolved_workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        public_key="epoch-public-key",
        non_exportable_private_key_ref="keyring://corvus/epoch/1",
        device_binding_digest=instance.device_binding_digest,
        issued_at=_NOW,
    )
    trust_anchor = AuthorityTrustAnchor(
        workspace_id=resolved_workspace_id,
        kind=AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION,
        local_lock_name=fixed_workspace_lock_name(resolved_workspace_id, 1),
        sealed_generation_ref="keyring://corvus/sealed-generation/1",
        device_binding_digest=instance.device_binding_digest,
        policy_digest="b" * 64,
        created_at=_NOW,
    )
    lease = DeploymentInstanceLease(
        workspace_id=resolved_workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        lock_name=fixed_workspace_lock_name(resolved_workspace_id, 1),
        fencing_token=1,
        acquired_at=_NOW,
    )
    authority = WorkspaceAuthority(
        workspace_id=resolved_workspace_id,
        deployment_profile_id=profile.id,
        deployment_instance_id=instance.id,
        epoch=1,
        authority_generation=4,
        authority_state_root="c" * 64,
        authority_epoch_credential_id=credential.id,
        trust_anchor_id=trust_anchor.id,
        active_lease_id=lease.id,
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=_NOW,
    )
    repository.add_deployment_profile(profile)
    repository.add_deployment_instance(instance)
    repository.add_epoch_credential(credential)
    repository.add_trust_anchor(trust_anchor)
    repository.acquire_lease(lease)
    repository.add_workspace_authority(authority)
    return _AuthorityFixture(
        workspace_id=resolved_workspace_id,
        profile=profile,
        instance=instance,
        credential=credential,
        trust_anchor=trust_anchor,
        lease=lease,
        authority=authority,
    )


def test_authority_records_round_trip_and_are_workspace_scoped(tmp_path: Path) -> None:
    repository = AuthorityRepository(_database(tmp_path))
    fixture = _fixture(repository)

    assert repository.get_deployment_profile(fixture.profile.id) == fixture.profile
    assert repository.get_deployment_instance(fixture.instance.id) == fixture.instance
    assert (
        repository.get_epoch_credential(
            workspace_id=fixture.workspace_id,
            credential_id=fixture.credential.id,
        )
        == fixture.credential
    )
    assert (
        repository.get_trust_anchor(
            workspace_id=fixture.workspace_id,
            trust_anchor_id=fixture.trust_anchor.id,
        )
        == fixture.trust_anchor
    )
    assert repository.get_active_lease(fixture.workspace_id, 1) == fixture.lease
    assert repository.get_workspace_authority(fixture.workspace_id) == fixture.authority
    other_workspace_id = uuid4()
    assert (
        repository.get_epoch_credential(
            workspace_id=other_workspace_id,
            credential_id=fixture.credential.id,
        )
        is None
    )
    assert repository.get_active_lease(other_workspace_id, 1) is None
    assert repository.get_workspace_authority(other_workspace_id) is None


def test_concurrent_same_epoch_clone_is_fenced_and_takeover_advances_token(
    tmp_path: Path,
) -> None:
    repository = AuthorityRepository(_database(tmp_path))
    workspace_id = uuid4()
    profile = _profile()
    first_instance = _instance(profile, marker="a")
    second_instance = _instance(profile, marker="b")
    repository.add_deployment_profile(profile)
    repository.add_deployment_instance(first_instance)
    repository.add_deployment_instance(second_instance)
    lock_name = fixed_workspace_lock_name(workspace_id, 1)
    candidates = (
        DeploymentInstanceLease(
            workspace_id=workspace_id,
            authority_epoch=1,
            deployment_instance_id=first_instance.id,
            lock_name=lock_name,
            fencing_token=1,
            acquired_at=_NOW,
        ),
        DeploymentInstanceLease(
            workspace_id=workspace_id,
            authority_epoch=1,
            deployment_instance_id=second_instance.id,
            lock_name=lock_name,
            fencing_token=1,
            acquired_at=_NOW,
        ),
    )
    barrier = Barrier(2)

    def acquire(lease: DeploymentInstanceLease) -> str:
        barrier.wait()
        try:
            repository.acquire_lease(lease)
        except AuthorityRepositoryError as exc:
            return str(exc)
        return "acquired"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(acquire, candidates))

    assert outcomes == ["acquired", "same_epoch_instance_lease_conflict"]
    winner = repository.get_active_lease(workspace_id, 1)
    assert winner is not None
    released = winner.model_copy(update={"released_at": _NOW + timedelta(minutes=1)})
    repository.release_lease(released, expected_fencing_token=1)
    stale = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=first_instance.id,
        lock_name=lock_name,
        fencing_token=1,
        acquired_at=_NOW + timedelta(minutes=2),
    )
    with pytest.raises(AuthorityRepositoryError, match="lease_fencing_token_not_advanced"):
        repository.acquire_lease(stale)
    takeover_instance = (
        second_instance if winner.deployment_instance_id == first_instance.id else first_instance
    )
    takeover = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=takeover_instance.id,
        lock_name=lock_name,
        fencing_token=2,
        acquired_at=_NOW + timedelta(minutes=2),
    )
    repository.acquire_lease(takeover)

    assert repository.get_active_lease(workspace_id, 1) == takeover


def test_workspace_authority_rejects_cross_workspace_credential(tmp_path: Path) -> None:
    repository = AuthorityRepository(_database(tmp_path))
    workspace_id = uuid4()
    profile = _profile()
    instance = _instance(profile, marker="a")
    repository.add_deployment_profile(profile)
    repository.add_deployment_instance(instance)
    foreign_credential = AuthorityEpochCredential(
        workspace_id=uuid4(),
        authority_epoch=1,
        deployment_instance_id=instance.id,
        public_key="foreign-epoch-public-key",
        non_exportable_private_key_ref="keyring://corvus/epoch/foreign",
        device_binding_digest=instance.device_binding_digest,
        issued_at=_NOW,
    )
    repository.add_epoch_credential(foreign_credential)
    trust_anchor = AuthorityTrustAnchor(
        workspace_id=workspace_id,
        kind=AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION,
        local_lock_name=fixed_workspace_lock_name(workspace_id, 1),
        sealed_generation_ref="keyring://corvus/sealed-generation/1",
        device_binding_digest=instance.device_binding_digest,
        policy_digest="b" * 64,
        created_at=_NOW,
    )
    repository.add_trust_anchor(trust_anchor)
    lease = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        lock_name=fixed_workspace_lock_name(workspace_id, 1),
        fencing_token=1,
        acquired_at=_NOW,
    )
    repository.acquire_lease(lease)
    substituted = WorkspaceAuthority(
        workspace_id=workspace_id,
        deployment_profile_id=profile.id,
        deployment_instance_id=instance.id,
        epoch=1,
        authority_generation=0,
        authority_state_root="c" * 64,
        authority_epoch_credential_id=foreign_credential.id,
        trust_anchor_id=trust_anchor.id,
        active_lease_id=lease.id,
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=_NOW,
    )

    with pytest.raises(AuthorityRepositoryError, match="authority_epoch_credential_mismatch"):
        repository.add_workspace_authority(substituted)


def test_commit_intent_recovery_advances_authority_exactly_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = AuthorityRepository(database)
    fixture = _fixture(repository)
    intent = AuthorityCommitIntent(
        workspace_id=fixture.workspace_id,
        epoch=fixture.authority.epoch,
        deployment_instance_id=fixture.instance.id,
        prior_generation=fixture.authority.authority_generation,
        next_generation=fixture.authority.authority_generation + 1,
        prior_state_root=fixture.authority.authority_state_root,
        mutation_digest="d" * 64,
        proposed_state_root="e" * 64,
        state=AuthorityCommitState.PREPARED,
        created_at=_NOW + timedelta(minutes=1),
    )
    repository.prepare_commit(intent)
    duplicate = intent.model_copy(update={"id": uuid4(), "mutation_digest": "f" * 64})
    with pytest.raises(AuthorityRepositoryError, match="authority_commit_in_progress"):
        repository.prepare_commit(duplicate)
    reserved = intent.model_copy(update={"state": AuthorityCommitState.ANCHOR_RESERVED})
    repository.advance_commit(reserved, expected_state=AuthorityCommitState.PREPARED)
    repository.close()

    reopened = AuthorityRepository(database)
    assert (
        reopened.get_commit_intent(
            workspace_id=fixture.workspace_id,
            intent_id=intent.id,
        )
        == reserved
    )
    database_committed = reserved.model_copy(update={"state": AuthorityCommitState.DB_COMMITTED})
    reopened.advance_commit(
        database_committed,
        expected_state=AuthorityCommitState.ANCHOR_RESERVED,
    )
    advanced = reopened.get_workspace_authority(fixture.workspace_id)
    assert advanced is not None
    assert advanced.authority_generation == intent.next_generation
    assert advanced.authority_state_root == intent.proposed_state_root
    assert advanced.version == fixture.authority.version + 1
    finalized = database_committed.model_copy(
        update={"state": AuthorityCommitState.ANCHOR_FINALIZED}
    )
    reopened.advance_commit(finalized, expected_state=AuthorityCommitState.DB_COMMITTED)

    with pytest.raises(AuthorityRepositoryError, match="authority_commit_state_conflict"):
        reopened.advance_commit(
            database_committed,
            expected_state=AuthorityCommitState.ANCHOR_RESERVED,
        )
    assert reopened.get_workspace_authority(fixture.workspace_id) == advanced
    assert (
        reopened.get_commit_intent(
            workspace_id=fixture.workspace_id,
            intent_id=intent.id,
        )
        == finalized
    )


def test_authority_repository_rejects_forged_head_with_missing_fencing_index(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX uq_deployment_instance_leases_active_workspace_epoch")
        connection.execute(
            "CREATE INDEX uq_deployment_instance_leases_active_workspace_epoch "
            "ON deployment_instance_leases (workspace_id, authority_epoch)"
        )

    with pytest.raises(AuthorityRepositoryError, match="database_state_mismatch:partial"):
        AuthorityRepository(database)
