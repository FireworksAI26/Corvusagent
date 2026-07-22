from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from corvus.database import DatabaseState, classify_database
from corvus.domain.audit import (
    AuditAnchorBindingState,
    AuditAnchorRecoveryCheckpoint,
    AuditReceipt,
    AuditResultBinding,
    AuthorizationDecisionSnapshot,
)
from corvus.infrastructure.audit_history import (
    AuditHistoryHeads,
    advance_audit_history_head,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class AuditRepositoryError(RuntimeError):
    pass


_ALLOWED_RECOVERY_TRANSITIONS: dict[
    AuditAnchorBindingState,
    frozenset[AuditAnchorBindingState],
] = {
    AuditAnchorBindingState.PREPARED: frozenset(
        {
            AuditAnchorBindingState.AUTHORITY_FINALIZED,
            AuditAnchorBindingState.QUARANTINED,
        }
    ),
    AuditAnchorBindingState.AUTHORITY_FINALIZED: frozenset(
        {
            AuditAnchorBindingState.BINDING_PERSISTED,
            AuditAnchorBindingState.QUARANTINED,
        }
    ),
    AuditAnchorBindingState.BINDING_PERSISTED: frozenset(
        {
            AuditAnchorBindingState.COMPLETE,
            AuditAnchorBindingState.QUARANTINED,
        }
    ),
    AuditAnchorBindingState.COMPLETE: frozenset(),
    AuditAnchorBindingState.QUARANTINED: frozenset(),
}


class AuditRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise AuditRepositoryError(f"database_revision_mismatch:{revision or 'unstamped'}")
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise AuditRepositoryError(f"database_state_mismatch:{status.state.value}")
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_snapshot(self, snapshot: AuthorizationDecisionSnapshot) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO authorization_decision_snapshots "
                    "(id, workspace_id, request_context_id, signing_key_version_id, "
                    "canonical_digest, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(snapshot.id),
                        str(snapshot.workspace_id),
                        str(snapshot.request_context_id),
                        str(snapshot.signing_key_version_id),
                        snapshot.canonical_digest,
                        snapshot.created_at.isoformat(),
                        snapshot.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuditRepositoryError("authorization_snapshot_identity_conflict") from exc

    def get_snapshot(
        self,
        *,
        workspace_id: UUID,
        snapshot_id: UUID,
    ) -> AuthorizationDecisionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authorization_decision_snapshots "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(snapshot_id)),
            ).fetchone()
        return None if row is None else AuthorizationDecisionSnapshot.model_validate_json(row[0])

    def append_receipt(self, receipt: AuditReceipt) -> None:
        try:
            with self._transaction() as connection:
                snapshot = connection.execute(
                    "SELECT canonical_digest FROM authorization_decision_snapshots "
                    "WHERE workspace_id = ? AND id = ?",
                    (str(receipt.workspace_id), str(receipt.authorization_snapshot_id)),
                ).fetchone()
                if snapshot is None or snapshot[0] != receipt.authorization_snapshot_digest:
                    raise AuditRepositoryError("audit_receipt_snapshot_mismatch")
                latest = connection.execute(
                    "SELECT workspace_sequence, receipt_hash FROM audit_receipts "
                    "WHERE workspace_id = ? ORDER BY workspace_sequence DESC LIMIT 1",
                    (str(receipt.workspace_id),),
                ).fetchone()
                expected_sequence = 1 if latest is None else int(latest[0]) + 1
                if receipt.workspace_sequence != expected_sequence:
                    raise AuditRepositoryError(
                        f"audit_sequence_mismatch:expected={expected_sequence}"
                    )
                if latest is not None and receipt.previous_hash != latest[1]:
                    raise AuditRepositoryError("audit_previous_hash_mismatch")
                connection.execute(
                    "INSERT INTO audit_receipts "
                    "(id, workspace_id, workspace_sequence, authorization_snapshot_id, "
                    "authority_commit_intent_id, previous_hash, receipt_hash, created_at, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(receipt.id),
                        str(receipt.workspace_id),
                        receipt.workspace_sequence,
                        str(receipt.authorization_snapshot_id),
                        str(receipt.authority_commit_intent_id),
                        receipt.previous_hash,
                        receipt.receipt_hash,
                        receipt.created_at.isoformat(),
                        receipt.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuditRepositoryError("audit_receipt_identity_conflict") from exc

    def list_receipts(self, workspace_id: UUID) -> list[AuditReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM audit_receipts WHERE workspace_id = ? "
                "ORDER BY workspace_sequence",
                (str(workspace_id),),
            ).fetchall()
        return [AuditReceipt.model_validate_json(row[0]) for row in rows]

    def append_result_binding(self, binding: AuditResultBinding) -> None:
        try:
            with self._transaction() as connection:
                receipt = connection.execute(
                    "SELECT receipt_hash, authority_commit_intent_id FROM audit_receipts "
                    "WHERE workspace_id = ? AND id = ?",
                    (str(binding.workspace_id), str(binding.audit_receipt_id)),
                ).fetchone()
                if receipt is None or receipt != (
                    binding.audit_receipt_hash,
                    str(binding.authority_commit_intent_id),
                ):
                    raise AuditRepositoryError("audit_result_binding_receipt_mismatch")
                connection.execute(
                    "INSERT INTO audit_result_bindings "
                    "(id, workspace_id, audit_receipt_id, audit_receipt_hash, "
                    "authority_commit_intent_id, binding_hash, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(binding.id),
                        str(binding.workspace_id),
                        str(binding.audit_receipt_id),
                        binding.audit_receipt_hash,
                        str(binding.authority_commit_intent_id),
                        binding.binding_hash,
                        binding.created_at.isoformat(),
                        binding.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuditRepositoryError("audit_result_binding_identity_conflict") from exc

    def get_result_binding(
        self,
        *,
        workspace_id: UUID,
        binding_id: UUID,
    ) -> AuditResultBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM audit_result_bindings WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(binding_id)),
            ).fetchone()
        return None if row is None else AuditResultBinding.model_validate_json(row[0])

    def prepare_recovery(self, checkpoint: AuditAnchorRecoveryCheckpoint) -> None:
        if checkpoint.state is not AuditAnchorBindingState.PREPARED:
            raise AuditRepositoryError("audit_recovery_initial_state_invalid")
        if checkpoint.result_binding_id is not None:
            raise AuditRepositoryError("audit_recovery_binding_before_persistence")
        try:
            with self._transaction() as connection:
                receipt = connection.execute(
                    "SELECT authority_commit_intent_id FROM audit_receipts "
                    "WHERE workspace_id = ? AND id = ?",
                    (str(checkpoint.workspace_id), str(checkpoint.audit_receipt_id)),
                ).fetchone()
                if receipt != (str(checkpoint.authority_commit_intent_id),):
                    raise AuditRepositoryError("audit_recovery_receipt_mismatch")
                connection.execute(
                    "INSERT INTO audit_anchor_recovery_checkpoints "
                    "(id, workspace_id, audit_receipt_id, authority_commit_intent_id, "
                    "prepared_result_digest, state, result_binding_id, updated_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(checkpoint.id),
                        str(checkpoint.workspace_id),
                        str(checkpoint.audit_receipt_id),
                        str(checkpoint.authority_commit_intent_id),
                        checkpoint.prepared_result_digest,
                        checkpoint.state.value,
                        None,
                        checkpoint.updated_at.isoformat(),
                        checkpoint.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuditRepositoryError("audit_recovery_identity_conflict") from exc

    def get_recovery_checkpoint(
        self,
        *,
        workspace_id: UUID,
        checkpoint_id: UUID,
    ) -> AuditAnchorRecoveryCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM audit_anchor_recovery_checkpoints "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(checkpoint_id)),
            ).fetchone()
        return None if row is None else AuditAnchorRecoveryCheckpoint.model_validate_json(row[0])

    def advance_recovery(
        self,
        checkpoint: AuditAnchorRecoveryCheckpoint,
        *,
        expected_state: AuditAnchorBindingState,
    ) -> None:
        if checkpoint.state not in _ALLOWED_RECOVERY_TRANSITIONS[expected_state]:
            raise AuditRepositoryError("audit_recovery_transition_invalid")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT audit_receipt_id, authority_commit_intent_id, "
                "prepared_result_digest, state FROM audit_anchor_recovery_checkpoints "
                "WHERE workspace_id = ? AND id = ?",
                (str(checkpoint.workspace_id), str(checkpoint.id)),
            ).fetchone()
            if current is None or current[3] != expected_state.value:
                raise AuditRepositoryError("audit_recovery_state_conflict")
            if current[:3] != (
                str(checkpoint.audit_receipt_id),
                str(checkpoint.authority_commit_intent_id),
                checkpoint.prepared_result_digest,
            ):
                raise AuditRepositoryError("audit_recovery_identity_mismatch")
            self._validate_recovery_binding(connection, checkpoint)
            cursor = connection.execute(
                "UPDATE audit_anchor_recovery_checkpoints SET state = ?, "
                "result_binding_id = ?, updated_at = ?, payload_json = ? "
                "WHERE workspace_id = ? AND id = ? AND state = ?",
                (
                    checkpoint.state.value,
                    None
                    if checkpoint.result_binding_id is None
                    else str(checkpoint.result_binding_id),
                    checkpoint.updated_at.isoformat(),
                    checkpoint.model_dump_json(),
                    str(checkpoint.workspace_id),
                    str(checkpoint.id),
                    expected_state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise AuditRepositoryError("audit_recovery_state_conflict")

    @staticmethod
    def _validate_recovery_binding(
        connection: sqlite3.Connection,
        checkpoint: AuditAnchorRecoveryCheckpoint,
    ) -> None:
        binding_required = checkpoint.state in {
            AuditAnchorBindingState.BINDING_PERSISTED,
            AuditAnchorBindingState.COMPLETE,
        }
        if checkpoint.state is AuditAnchorBindingState.QUARANTINED:
            binding_required = checkpoint.result_binding_id is not None
        if not binding_required:
            if checkpoint.result_binding_id is not None:
                raise AuditRepositoryError("audit_recovery_binding_too_early")
            return
        if checkpoint.result_binding_id is None:
            raise AuditRepositoryError("audit_recovery_binding_missing")
        binding = connection.execute(
            "SELECT audit_receipt_id, authority_commit_intent_id, payload_json "
            "FROM audit_result_bindings WHERE workspace_id = ? AND id = ?",
            (str(checkpoint.workspace_id), str(checkpoint.result_binding_id)),
        ).fetchone()
        if binding is None:
            raise AuditRepositoryError("audit_recovery_binding_mismatch")
        parsed = AuditResultBinding.model_validate_json(binding[2])
        if (
            binding[:2]
            != (
                str(checkpoint.audit_receipt_id),
                str(checkpoint.authority_commit_intent_id),
            )
            or parsed.prepared_result_digest != checkpoint.prepared_result_digest
        ):
            raise AuditRepositoryError("audit_recovery_binding_mismatch")

    def current_history_heads(self, workspace_id: UUID) -> AuditHistoryHeads:
        checkpoint_head = "0" * 64
        binding_head = "0" * 64
        with self._connect() as connection:
            checkpoints = connection.execute(
                "SELECT payload_json FROM audit_anchor_recovery_checkpoints "
                "WHERE workspace_id = ? ORDER BY id",
                (str(workspace_id),),
            ).fetchall()
            bindings = connection.execute(
                "SELECT binding_hash FROM audit_result_bindings WHERE workspace_id = ? ORDER BY id",
                (str(workspace_id),),
            ).fetchall()
        for (payload_json,) in checkpoints:
            digest = hashlib.sha256(str(payload_json).encode("utf-8")).hexdigest()
            checkpoint_head = advance_audit_history_head(checkpoint_head, digest)
        for (binding_hash,) in bindings:
            binding_head = advance_audit_history_head(binding_head, str(binding_hash))
        return AuditHistoryHeads(
            checkpoint_history_head=checkpoint_head,
            result_binding_history_head=binding_head,
        )

    def close(self) -> None:
        return None
