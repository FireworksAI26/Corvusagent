from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.domain.deployment import (
    AuthorityCloseCertificate,
    AuthorityHandoff,
    AuthorityHandoffActivation,
    AuthorityHandoffState,
    EpochKeyDisposition,
    RestoreDecision,
    RestoreValidationReceipt,
)
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.handoff import (
    HandoffRepository,
    HandoffRepositoryError,
)
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _repository(tmp_path: Path) -> HandoffRepository:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return HandoffRepository(database)


def _close(workspace_id):
    return AuthorityCloseCertificate(
        workspace_id=workspace_id,
        closed_epoch=1,
        source_deployment_id=uuid4(),
        source_deployment_instance_id=uuid4(),
        target_deployment_id=uuid4(),
        epoch_credential_digest="1" * 64,
        destruction_or_revocation_attestation_digest="2" * 64,
        final_authority_generation=4,
        final_state_root="3" * 64,
        workspace_signing_key_version_id=uuid4(),
        workspace_signature="workspace-signature",
        anchor_receipt_digest="4" * 64,
        epoch_key_disposition=EpochKeyDisposition.REVOKED,
        externally_anchored_at=_NOW,
    )


def test_close_handoff_activation_round_trip_and_ordering(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace_id = uuid4()
    close = _close(workspace_id)
    handoff = AuthorityHandoff(
        workspace_id=workspace_id,
        from_deployment_id=close.source_deployment_id,
        from_deployment_instance_id=close.source_deployment_instance_id,
        to_deployment_id=close.target_deployment_id,
        to_deployment_instance_id=uuid4(),
        from_epoch=1,
        to_epoch=2,
        export_artifact_digest="5" * 64,
        source_checkpoint_digest="6" * 64,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="7" * 64,
        source_signing_key_version_id=close.workspace_signing_key_version_id,
        close_certificate_id=close.id,
        target_epoch_credential_id=uuid4(),
        state=AuthorityHandoffState.PREPARED,
        prepared_at=_NOW - timedelta(minutes=1),
    )
    activation = AuthorityHandoffActivation(
        workspace_id=workspace_id,
        target_deployment_instance_id=handoff.to_deployment_instance_id,
        authority_epoch=2,
        source_close_certificate_id=close.id,
        source_close_certificate_digest=repository.canonical_digest(close),
        authority_epoch_credential_id=handoff.target_epoch_credential_id,
        exclusive_lease_or_local_anchor_receipt_digest="8" * 64,
        activated_at=_NOW + timedelta(minutes=1),
    )

    repository.append_close_certificate(close)
    repository.prepare_handoff(handoff)
    with pytest.raises(HandoffRepositoryError, match="handoff_source_not_closed"):
        repository.append_activation(activation)

    source_closed = handoff.model_copy(
        update={"state": AuthorityHandoffState.SOURCE_CLOSED_ANCHORED}
    )
    repository.advance_handoff(source_closed, expected_state=AuthorityHandoffState.PREPARED)
    repository.append_activation(activation)
    target_active = source_closed.model_copy(
        update={
            "state": AuthorityHandoffState.TARGET_ACTIVE,
            "completed_at": _NOW + timedelta(minutes=2),
        }
    )
    repository.advance_handoff(
        target_active,
        expected_state=AuthorityHandoffState.SOURCE_CLOSED_ANCHORED,
    )

    assert repository.get_close_certificate(workspace_id, close.id) == close
    assert repository.get_activation(workspace_id, activation.id) == activation
    assert repository.get_handoff(workspace_id, handoff.id) == target_active


def test_restore_defaults_to_read_queue_and_takeover_requires_new_epoch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace_id = uuid4()
    quarantined = RestoreValidationReceipt(
        workspace_id=workspace_id,
        restored_database_digest="9" * 64,
        observed_epoch=1,
        observed_generation=4,
        observed_state_root="a" * 64,
        trust_anchor_id=uuid4(),
        reason_code="restored_database_quarantined",
        validated_at=_NOW,
    )
    takeover = RestoreValidationReceipt(
        workspace_id=workspace_id,
        restored_database_digest="9" * 64,
        observed_epoch=1,
        observed_generation=4,
        observed_state_root="a" * 64,
        trust_anchor_id=quarantined.trust_anchor_id,
        former_instance_revocation_digest="b" * 64,
        takeover_lease_or_local_anchor_receipt_digest="c" * 64,
        takeover_epoch=2,
        decision=RestoreDecision.EXCLUSIVE_TAKEOVER_NEW_EPOCH,
        reason_code="exclusive_takeover_authorized",
        validated_at=_NOW + timedelta(minutes=1),
    )

    repository.append_restore_receipt(quarantined)
    repository.append_restore_receipt(takeover)

    assert repository.list_restore_receipts(workspace_id) == [quarantined, takeover]
    assert quarantined.decision is RestoreDecision.READ_QUEUE_ONLY
