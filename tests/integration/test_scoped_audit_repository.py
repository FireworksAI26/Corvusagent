from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from corvus.domain.audit import (
    AuditAnchorBindingState,
    AuditAnchorRecoveryCheckpoint,
    AuditReceipt,
    AuditResultBinding,
    AuthorizationDecisionSnapshot,
    authorization_snapshot_digest,
)
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.audit import AuditRepository, AuditRepositoryError
from corvus.store import TraceStore


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _snapshot(workspace_id: UUID) -> AuthorizationDecisionSnapshot:
    canonical_inputs = {"action": "project.create", "resource": "project"}
    source_versions = {"access_bundle": 3, "agent_grant": 2}
    return AuthorizationDecisionSnapshot(
        workspace_id=workspace_id,
        request_context_id=uuid4(),
        deployment_instance_id=uuid4(),
        authority_epoch_credential_id=uuid4(),
        authority_generation=7,
        authority_state_root="a" * 64,
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="b" * 64,
        membership_version_ids=(uuid4(),),
        membership_digest="c" * 64,
        scope_kind="project",
        scope_id=uuid4(),
        scope_digest="d" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_digest="e" * 64,
        requester_id=uuid4(),
        transport_principal_id=uuid4(),
        access_bundle_id=uuid4(),
        access_bundle_version_digest="f" * 64,
        agent_grant_id=uuid4(),
        agent_delegation_digest="0" * 64,
        policy_digest="1" * 64,
        autonomy_policy_digest="2" * 64,
        budget_snapshot_ids=(uuid4(),),
        budget_snapshot_digest="3" * 64,
        kill_switch_snapshot_ids=(uuid4(),),
        kill_switch_snapshot_digest="4" * 64,
        decision="allow",
        reason_code="authorized",
        canonical_inputs_json=canonical_inputs,
        source_record_version_map=source_versions,
        canonical_digest=authorization_snapshot_digest(canonical_inputs, source_versions),
        signing_key_version_id=uuid4(),
        snapshot_signature="signed-snapshot",
        created_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )


def _receipt(
    workspace_id: UUID,
    snapshot: AuthorizationDecisionSnapshot,
    *,
    sequence: int,
    previous_hash: str,
    receipt_hash: str,
    intent_id: UUID | None = None,
) -> AuditReceipt:
    return AuditReceipt(
        workspace_id=workspace_id,
        workspace_sequence=sequence,
        schema_version=1,
        prior_authority_epoch=1,
        prior_authority_generation=7,
        prior_authority_state_root="a" * 64,
        prior_authority_commit_receipt_id=snapshot.authority_commit_receipt_id,
        authority_commit_intent_id=intent_id or uuid4(),
        intended_mutation_digest="5" * 64,
        request_context_id=snapshot.request_context_id,
        authorization_snapshot_id=snapshot.id,
        authorization_snapshot_digest=snapshot.canonical_digest,
        action="project.create",
        resource="project",
        decision="allow",
        reason_code="authorized",
        policy_digest=snapshot.policy_digest,
        sanitized_input_digest="6" * 64,
        signing_key_version_id=snapshot.signing_key_version_id,
        previous_hash=previous_hash,
        receipt_hash=receipt_hash,
        receipt_signature="signed-receipt",
        created_at=datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC),
    )


def _binding(receipt: AuditReceipt) -> AuditResultBinding:
    return AuditResultBinding(
        workspace_id=receipt.workspace_id,
        audit_receipt_id=receipt.id,
        audit_receipt_hash=receipt.receipt_hash,
        authority_commit_intent_id=receipt.authority_commit_intent_id,
        prepared_result_digest="7" * 64,
        finalized_authority_epoch=1,
        finalized_authority_generation=8,
        finalized_authority_state_root="8" * 64,
        authority_commit_receipt_id=uuid4(),
        authority_commit_receipt_digest="9" * 64,
        signing_key_version_id=receipt.signing_key_version_id,
        binding_hash="a" * 64,
        binding_signature="signed-binding",
        created_at=datetime(2026, 7, 14, 12, 0, 2, tzinfo=UTC),
    )


def test_signed_snapshots_are_immutable_and_workspace_scoped(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = AuditRepository(database)
    workspace_id = uuid4()
    snapshot = _snapshot(workspace_id)

    repository.append_snapshot(snapshot)

    assert repository.get_snapshot(workspace_id=workspace_id, snapshot_id=snapshot.id) == snapshot
    assert repository.get_snapshot(workspace_id=uuid4(), snapshot_id=snapshot.id) is None
    with pytest.raises(AuditRepositoryError, match="authorization_snapshot_identity_conflict"):
        repository.append_snapshot(snapshot)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="authorization snapshots are immutable"):
            connection.execute(
                "UPDATE authorization_decision_snapshots SET payload_json = '{}' WHERE id = ?",
                (str(snapshot.id),),
            )


def test_receipts_require_contiguous_workspace_sequence_and_hash_chain(tmp_path: Path) -> None:
    repository = AuditRepository(_database(tmp_path))
    workspace_id = uuid4()
    snapshot = _snapshot(workspace_id)
    repository.append_snapshot(snapshot)
    first = _receipt(
        workspace_id,
        snapshot,
        sequence=1,
        previous_hash="0" * 64,
        receipt_hash="1" * 64,
    )
    skipped = _receipt(
        workspace_id,
        snapshot,
        sequence=3,
        previous_hash=first.receipt_hash,
        receipt_hash="3" * 64,
    )

    repository.append_receipt(first)

    with pytest.raises(AuditRepositoryError, match="audit_sequence_mismatch:expected=2"):
        repository.append_receipt(skipped)
    wrong_chain = _receipt(
        workspace_id,
        snapshot,
        sequence=2,
        previous_hash="f" * 64,
        receipt_hash="2" * 64,
    )
    with pytest.raises(AuditRepositoryError, match="audit_previous_hash_mismatch"):
        repository.append_receipt(wrong_chain)
    second = _receipt(
        workspace_id,
        snapshot,
        sequence=2,
        previous_hash=first.receipt_hash,
        receipt_hash="2" * 64,
    )
    repository.append_receipt(second)

    assert repository.list_receipts(workspace_id) == [first, second]
    assert repository.list_receipts(uuid4()) == []


def test_concurrent_receipt_writers_cannot_claim_the_same_sequence(tmp_path: Path) -> None:
    repository = AuditRepository(_database(tmp_path))
    workspace_id = uuid4()
    snapshot = _snapshot(workspace_id)
    repository.append_snapshot(snapshot)
    candidates = (
        _receipt(
            workspace_id,
            snapshot,
            sequence=1,
            previous_hash="0" * 64,
            receipt_hash="1" * 64,
        ),
        _receipt(
            workspace_id,
            snapshot,
            sequence=1,
            previous_hash="0" * 64,
            receipt_hash="2" * 64,
        ),
    )
    barrier = Barrier(2)

    def append(receipt: AuditReceipt) -> str:
        barrier.wait()
        try:
            repository.append_receipt(receipt)
        except AuditRepositoryError as exc:
            return str(exc)
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(append, candidates))

    assert outcomes == ["audit_sequence_mismatch:expected=2", "committed"]
    assert len(repository.list_receipts(workspace_id)) == 1


def test_result_binding_requires_the_exact_workspace_receipt(tmp_path: Path) -> None:
    repository = AuditRepository(_database(tmp_path))
    workspace_id = uuid4()
    snapshot = _snapshot(workspace_id)
    receipt = _receipt(
        workspace_id,
        snapshot,
        sequence=1,
        previous_hash="0" * 64,
        receipt_hash="1" * 64,
    )
    repository.append_snapshot(snapshot)
    repository.append_receipt(receipt)
    binding = _binding(receipt)

    repository.append_result_binding(binding)

    assert (
        repository.get_result_binding(workspace_id=workspace_id, binding_id=binding.id) == binding
    )
    assert repository.get_result_binding(workspace_id=uuid4(), binding_id=binding.id) is None
    substituted = binding.model_copy(update={"id": uuid4(), "audit_receipt_hash": "e" * 64})
    with pytest.raises(AuditRepositoryError, match="audit_result_binding_receipt_mismatch"):
        repository.append_result_binding(substituted)


def test_recovery_checkpoint_advances_atomically_and_survives_reopen(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = AuditRepository(database)
    workspace_id = uuid4()
    snapshot = _snapshot(workspace_id)
    receipt = _receipt(
        workspace_id,
        snapshot,
        sequence=1,
        previous_hash="0" * 64,
        receipt_hash="1" * 64,
    )
    repository.append_snapshot(snapshot)
    repository.append_receipt(receipt)
    checkpoint = AuditAnchorRecoveryCheckpoint(
        workspace_id=workspace_id,
        audit_receipt_id=receipt.id,
        authority_commit_intent_id=receipt.authority_commit_intent_id,
        prepared_result_digest="7" * 64,
        state=AuditAnchorBindingState.PREPARED,
        updated_at=datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC),
    )
    repository.prepare_recovery(checkpoint)
    finalized = checkpoint.model_copy(
        update={
            "state": AuditAnchorBindingState.AUTHORITY_FINALIZED,
            "updated_at": datetime(2026, 7, 14, 12, 0, 2, tzinfo=UTC),
        }
    )
    repository.advance_recovery(
        finalized,
        expected_state=AuditAnchorBindingState.PREPARED,
    )
    repository.close()

    reopened = AuditRepository(database)
    assert (
        reopened.get_recovery_checkpoint(
            workspace_id=workspace_id,
            checkpoint_id=checkpoint.id,
        )
        == finalized
    )
    binding = _binding(receipt)
    reopened.append_result_binding(binding)
    persisted = finalized.model_copy(
        update={
            "state": AuditAnchorBindingState.BINDING_PERSISTED,
            "result_binding_id": binding.id,
            "updated_at": datetime(2026, 7, 14, 12, 0, 3, tzinfo=UTC),
        }
    )
    reopened.advance_recovery(
        persisted,
        expected_state=AuditAnchorBindingState.AUTHORITY_FINALIZED,
    )
    complete = persisted.model_copy(
        update={
            "state": AuditAnchorBindingState.COMPLETE,
            "updated_at": datetime(2026, 7, 14, 12, 0, 4, tzinfo=UTC),
        }
    )
    reopened.advance_recovery(
        complete,
        expected_state=AuditAnchorBindingState.BINDING_PERSISTED,
    )

    with pytest.raises(AuditRepositoryError, match="audit_recovery_state_conflict"):
        reopened.advance_recovery(
            finalized,
            expected_state=AuditAnchorBindingState.PREPARED,
        )
    assert (
        reopened.get_recovery_checkpoint(
            workspace_id=workspace_id,
            checkpoint_id=checkpoint.id,
        )
        == complete
    )


def test_repository_rejects_forged_head_with_missing_immutability_trigger(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER authorization_decision_snapshots_no_update")

    with pytest.raises(AuditRepositoryError, match="database_state_mismatch:partial"):
        AuditRepository(database)
