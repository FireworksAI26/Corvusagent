from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.domain.access import (
    AccessBundle,
    AgentGrant,
    CapabilityEffect,
    CapabilityGrant,
    DelegationGrant,
)
from corvus.domain.audit import SigningKeyStatus, WorkspaceSigningKeyVersion
from corvus.domain.request import IdempotencyEnvelope, IdempotencyStatus
from corvus.domain.scope import AudiencePolicySnapshot
from corvus.infrastructure.db import (
    M1_CURRENT_REVISION,
    M1_REGISTRY_REVISION,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from corvus.infrastructure.repositories.authorization_inputs import (
    AuthorizationInputRepository,
    AuthorizationInputRepositoryError,
)
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def test_authorization_input_migration_is_repeatable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = current_revision(database)
    second = upgrade_database(database)

    assert first == M1_CURRENT_REVISION
    assert second == M1_CURRENT_REVISION


def test_authorization_input_migration_downgrades_and_reapplies(tmp_path: Path) -> None:
    database = _database(tmp_path)

    assert downgrade_database(database, M1_REGISTRY_REVISION) == M1_REGISTRY_REVISION
    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert current_revision(database) == M1_CURRENT_REVISION
    TraceStore(database).engine.dispose()


def test_audience_access_agent_and_delegation_round_trip(tmp_path: Path) -> None:
    repository = AuthorizationInputRepository(_database(tmp_path))
    workspace_id = uuid4()
    principal_id = uuid4()
    scope_id = uuid4()
    audience = AudiencePolicySnapshot(
        workspace_id=workspace_id,
        visibility="personal",
        owner_principal_id=principal_id,
        scope_digest="1" * 64,
        policy_version=1,
        policy_digest="2" * 64,
        created_by=principal_id,
        created_at=_NOW,
    )
    bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=principal_id,
        scope_kind="project",
        scope_id=scope_id,
        issued_by=principal_id,
        policy_digest="3" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    grant = CapabilityGrant(
        bundle_id=bundle.id,
        workspace_id=workspace_id,
        resource_kind="project",
        resource_id=scope_id,
        action="project.read",
        effect=CapabilityEffect.ALLOW,
        created_at=_NOW,
    )
    agent_id = uuid4()
    agent_grant = AgentGrant(
        workspace_id=workspace_id,
        agent_id=agent_id,
        capability_bundle_id=bundle.id,
        autonomy_level=3,
        issued_by=principal_id,
        created_at=_NOW,
    )
    delegation = DelegationGrant(
        parent_agent_grant_id=agent_grant.id,
        child_agent_id=uuid4(),
        capabilities=frozenset({"project.read"}),
        budget_json={"tokens": 1000},
        depth_limit=0,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )

    repository.append_audience_snapshot(audience)
    repository.append_access_bundle(bundle, [grant])
    repository.append_agent_grant(agent_grant)
    repository.append_delegation_grant(delegation)
    repository.close()

    reopened = AuthorizationInputRepository(database=tmp_path / "corvus.db")
    assert reopened.get_audience_snapshot(workspace_id, audience.id) == audience
    assert reopened.get_access_bundle(workspace_id, bundle.id) == (bundle, [grant])
    assert reopened.get_agent_grant(workspace_id, agent_grant.id) == agent_grant
    assert reopened.get_delegation_grant(workspace_id, delegation.id) == delegation


def test_cross_workspace_capability_grant_is_rejected(tmp_path: Path) -> None:
    repository = AuthorizationInputRepository(_database(tmp_path))
    workspace_id = uuid4()
    bundle = AccessBundle(
        workspace_id=workspace_id,
        principal_id=uuid4(),
        scope_kind="workspace",
        scope_id=workspace_id,
        issued_by=uuid4(),
        policy_digest="4" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    wrong_grant = CapabilityGrant(
        bundle_id=bundle.id,
        workspace_id=uuid4(),
        resource_kind="workspace",
        resource_id=workspace_id,
        action="workspace.read",
        effect=CapabilityEffect.ALLOW,
        created_at=_NOW,
    )

    with pytest.raises(
        AuthorizationInputRepositoryError,
        match="cross_workspace_capability_grant",
    ):
        repository.append_access_bundle(bundle, [wrong_grant])


def test_signing_key_history_requires_exact_predecessor(tmp_path: Path) -> None:
    repository = AuthorizationInputRepository(_database(tmp_path))
    workspace_id = uuid4()
    first = WorkspaceSigningKeyVersion(
        workspace_id=workspace_id,
        key_epoch=1,
        algorithm="ed25519",
        public_key="public-key-1",
        non_exportable_private_key_ref="keyring://workspace/signing/1",
        status=SigningKeyStatus.ROTATED,
        valid_from=_NOW,
        valid_until=_NOW + timedelta(hours=1),
        attestation_digest="5" * 64,
        created_at=_NOW,
    )
    repository.append_signing_key(first)
    second = WorkspaceSigningKeyVersion(
        workspace_id=workspace_id,
        key_epoch=2,
        algorithm="ed25519",
        public_key="public-key-2",
        non_exportable_private_key_ref="keyring://workspace/signing/2",
        status=SigningKeyStatus.ACTIVE,
        valid_from=_NOW + timedelta(hours=1),
        predecessor_digest=repository.signing_key_digest(first),
        attestation_digest="6" * 64,
        created_at=_NOW + timedelta(hours=1),
    )
    repository.append_signing_key(second)

    assert repository.list_signing_keys(workspace_id) == [first, second]

    skipped = second.model_copy(update={"id": uuid4(), "key_epoch": 4})
    with pytest.raises(
        AuthorizationInputRepositoryError,
        match="signing_key_epoch_skipped",
    ):
        repository.append_signing_key(skipped)


def test_idempotency_claim_complete_and_payload_mismatch(tmp_path: Path) -> None:
    repository = AuthorizationInputRepository(_database(tmp_path))
    workspace_id = uuid4()
    requester_id = uuid4()
    transport_id = uuid4()
    agent_id = uuid4()
    agent_grant_id = uuid4()
    envelope = IdempotencyEnvelope(
        workspace_id=workspace_id,
        requester_id=requester_id,
        transport_principal_id=transport_id,
        agent_id=agent_id,
        agent_grant_id=agent_grant_id,
        operation="project.create",
        idempotency_key="create-project-1",
        request_context_digest="7" * 64,
        payload_digest="8" * 64,
        status=IdempotencyStatus.IN_PROGRESS,
        created_at=_NOW,
    )

    repository.claim_idempotency(envelope)
    assert repository.get_idempotency(envelope.composite_identity) == envelope

    with pytest.raises(
        AuthorizationInputRepositoryError,
        match="idempotency_payload_mismatch",
    ):
        repository.claim_idempotency(
            envelope.model_copy(update={"id": uuid4(), "payload_digest": "9" * 64})
        )

    completed = envelope.model_copy(
        update={
            "status": IdempotencyStatus.SUCCEEDED,
            "result_digest": "a" * 64,
            "result_ref": "project://created",
            "completed_at": _NOW + timedelta(seconds=1),
        }
    )
    repository.complete_idempotency(completed)
    assert repository.get_idempotency(envelope.composite_identity) == completed

    with pytest.raises(
        AuthorizationInputRepositoryError,
        match="idempotency_not_in_progress",
    ):
        repository.complete_idempotency(completed)
