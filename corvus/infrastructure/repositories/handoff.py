from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from corvus.database import DatabaseState, classify_database
from corvus.domain.deployment import (
    AuthorityCloseCertificate,
    AuthorityContractError,
    AuthorityHandoff,
    AuthorityHandoffActivation,
    AuthorityHandoffState,
    RestoreValidationReceipt,
    validate_handoff_activation,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class HandoffRepositoryError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS = {
    AuthorityHandoffState.PREPARED: frozenset(
        {AuthorityHandoffState.SOURCE_CLOSED_ANCHORED, AuthorityHandoffState.ABORTED}
    ),
    AuthorityHandoffState.SOURCE_CLOSED_ANCHORED: frozenset(
        {AuthorityHandoffState.TARGET_ACTIVE, AuthorityHandoffState.ABORTED}
    ),
    AuthorityHandoffState.TARGET_ACTIVE: frozenset(),
    AuthorityHandoffState.ABORTED: frozenset(),
}


def _canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class HandoffRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise HandoffRepositoryError(f"database_revision_mismatch:{revision or 'unstamped'}")
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise HandoffRepositoryError(f"database_state_mismatch:{status.state.value}")
        self.database = database

    @staticmethod
    def canonical_digest(value: object) -> str:
        return _canonical_digest(value)

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

    def append_close_certificate(self, certificate: AuthorityCloseCertificate) -> None:
        if certificate.anchor_receipt_digest is None or certificate.externally_anchored_at is None:
            raise HandoffRepositoryError("handoff_close_not_anchored")
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO authority_close_certificates "
                    "(id, workspace_id, closed_epoch, source_deployment_instance_id, "
                    "target_deployment_id, final_authority_generation, final_state_root, "
                    "epoch_key_disposition, anchor_receipt_digest, externally_anchored_at, "
                    "canonical_digest, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(certificate.id),
                        str(certificate.workspace_id),
                        certificate.closed_epoch,
                        str(certificate.source_deployment_instance_id),
                        str(certificate.target_deployment_id),
                        certificate.final_authority_generation,
                        certificate.final_state_root,
                        certificate.epoch_key_disposition.value,
                        certificate.anchor_receipt_digest,
                        certificate.externally_anchored_at.isoformat(),
                        _canonical_digest(certificate),
                        certificate.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise HandoffRepositoryError("close_certificate_identity_conflict") from exc

    def get_close_certificate(
        self,
        workspace_id: UUID,
        certificate_id: UUID,
    ) -> AuthorityCloseCertificate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_close_certificates "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(certificate_id)),
            ).fetchone()
        return None if row is None else AuthorityCloseCertificate.model_validate_json(row[0])

    def prepare_handoff(self, handoff: AuthorityHandoff) -> None:
        if handoff.state is not AuthorityHandoffState.PREPARED:
            raise HandoffRepositoryError("handoff_initial_state_invalid")
        try:
            with self._transaction() as connection:
                close = self._close(connection, handoff.workspace_id, handoff.close_certificate_id)
                if close is None or (
                    close.closed_epoch,
                    close.source_deployment_id,
                    close.source_deployment_instance_id,
                    close.target_deployment_id,
                    close.workspace_signing_key_version_id,
                ) != (
                    handoff.from_epoch,
                    handoff.from_deployment_id,
                    handoff.from_deployment_instance_id,
                    handoff.to_deployment_id,
                    handoff.source_signing_key_version_id,
                ):
                    raise HandoffRepositoryError("handoff_close_certificate_mismatch")
                connection.execute(
                    "INSERT INTO authority_handoffs "
                    "(id, workspace_id, from_epoch, to_epoch, close_certificate_id, state, "
                    "prepared_at, completed_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(handoff.id),
                        str(handoff.workspace_id),
                        handoff.from_epoch,
                        handoff.to_epoch,
                        str(handoff.close_certificate_id),
                        handoff.state.value,
                        handoff.prepared_at.isoformat(),
                        None,
                        handoff.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise HandoffRepositoryError("handoff_identity_conflict") from exc

    def advance_handoff(
        self,
        handoff: AuthorityHandoff,
        *,
        expected_state: AuthorityHandoffState,
    ) -> None:
        if handoff.state not in _ALLOWED_TRANSITIONS[expected_state]:
            raise HandoffRepositoryError("handoff_transition_invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, state FROM authority_handoffs "
                "WHERE workspace_id = ? AND id = ?",
                (str(handoff.workspace_id), str(handoff.id)),
            ).fetchone()
            if row is None or row[1] != expected_state.value:
                raise HandoffRepositoryError("handoff_state_conflict")
            current = AuthorityHandoff.model_validate_json(row[0])
            expected = current.model_copy(
                update={"state": handoff.state, "completed_at": handoff.completed_at}
            )
            if handoff != expected:
                raise HandoffRepositoryError("handoff_identity_mismatch")
            if handoff.state is AuthorityHandoffState.TARGET_ACTIVE:
                activation = connection.execute(
                    "SELECT 1 FROM authority_handoff_activations "
                    "WHERE workspace_id = ? AND authority_epoch = ?",
                    (str(handoff.workspace_id), handoff.to_epoch),
                ).fetchone()
                if activation is None:
                    raise HandoffRepositoryError("handoff_activation_missing")
            cursor = connection.execute(
                "UPDATE authority_handoffs SET state = ?, completed_at = ?, payload_json = ? "
                "WHERE workspace_id = ? AND id = ? AND state = ?",
                (
                    handoff.state.value,
                    None if handoff.completed_at is None else handoff.completed_at.isoformat(),
                    handoff.model_dump_json(),
                    str(handoff.workspace_id),
                    str(handoff.id),
                    expected_state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffRepositoryError("handoff_state_conflict")

    def get_handoff(self, workspace_id: UUID, handoff_id: UUID) -> AuthorityHandoff | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_handoffs WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(handoff_id)),
            ).fetchone()
        return None if row is None else AuthorityHandoff.model_validate_json(row[0])

    def append_activation(self, activation: AuthorityHandoffActivation) -> None:
        with self._transaction() as connection:
            handoff_row = connection.execute(
                "SELECT payload_json, state FROM authority_handoffs "
                "WHERE workspace_id = ? AND close_certificate_id = ?",
                (str(activation.workspace_id), str(activation.source_close_certificate_id)),
            ).fetchone()
            if (
                handoff_row is None
                or handoff_row[1] != AuthorityHandoffState.SOURCE_CLOSED_ANCHORED.value
            ):
                raise HandoffRepositoryError("handoff_source_not_closed")
            handoff = AuthorityHandoff.model_validate_json(handoff_row[0])
            close = self._close(
                connection,
                activation.workspace_id,
                activation.source_close_certificate_id,
            )
            if close is None or activation.source_close_certificate_digest != _canonical_digest(
                close
            ):
                raise HandoffRepositoryError("handoff_close_certificate_digest_mismatch")
            if (
                activation.target_deployment_instance_id != handoff.to_deployment_instance_id
                or activation.authority_epoch_credential_id != handoff.target_epoch_credential_id
            ):
                raise HandoffRepositoryError("handoff_activation_target_mismatch")
            try:
                validate_handoff_activation(close, activation)
                connection.execute(
                    "INSERT INTO authority_handoff_activations "
                    "(id, workspace_id, target_deployment_instance_id, authority_epoch, "
                    "source_close_certificate_id, source_close_certificate_digest, "
                    "authority_epoch_credential_id, activated_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(activation.id),
                        str(activation.workspace_id),
                        str(activation.target_deployment_instance_id),
                        activation.authority_epoch,
                        str(activation.source_close_certificate_id),
                        activation.source_close_certificate_digest,
                        str(activation.authority_epoch_credential_id),
                        activation.activated_at.isoformat(),
                        activation.model_dump_json(),
                    ),
                )
            except AuthorityContractError as exc:
                raise HandoffRepositoryError(exc.reason_code) from exc
            except sqlite3.IntegrityError as exc:
                raise HandoffRepositoryError("handoff_activation_identity_conflict") from exc

    def get_activation(
        self,
        workspace_id: UUID,
        activation_id: UUID,
    ) -> AuthorityHandoffActivation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_handoff_activations "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(activation_id)),
            ).fetchone()
        return None if row is None else AuthorityHandoffActivation.model_validate_json(row[0])

    def append_restore_receipt(self, receipt: RestoreValidationReceipt) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO restore_validation_receipts "
                    "(id, workspace_id, restored_database_digest, observed_epoch, takeover_epoch, "
                    "decision, validated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(receipt.id),
                        str(receipt.workspace_id),
                        receipt.restored_database_digest,
                        receipt.observed_epoch,
                        receipt.takeover_epoch,
                        receipt.decision.value,
                        receipt.validated_at.isoformat(),
                        receipt.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise HandoffRepositoryError("restore_receipt_identity_conflict") from exc

    def list_restore_receipts(self, workspace_id: UUID) -> list[RestoreValidationReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM restore_validation_receipts "
                "WHERE workspace_id = ? ORDER BY validated_at, id",
                (str(workspace_id),),
            ).fetchall()
        return [RestoreValidationReceipt.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _close(
        connection: sqlite3.Connection,
        workspace_id: UUID,
        certificate_id: UUID,
    ) -> AuthorityCloseCertificate | None:
        row = connection.execute(
            "SELECT payload_json FROM authority_close_certificates "
            "WHERE workspace_id = ? AND id = ?",
            (str(workspace_id), str(certificate_id)),
        ).fetchone()
        return None if row is None else AuthorityCloseCertificate.model_validate_json(row[0])

    def close(self) -> None:
        return None
