from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

from corvus.database import DatabaseState, classify_database
from corvus.domain.access import (
    AccessBundle,
    AgentGrant,
    CapabilityGrant,
    DelegationGrant,
    validate_access_bundle,
)
from corvus.domain.audit import WorkspaceSigningKeyVersion
from corvus.domain.request import (
    IdempotencyContractError,
    IdempotencyEnvelope,
    IdempotencyStatus,
    validate_idempotency_replay,
)
from corvus.domain.scope import AudiencePolicySnapshot
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class AuthorizationInputRepositoryError(RuntimeError):
    pass


def _canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuthorizationInputRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise AuthorizationInputRepositoryError(
                f"database_revision_mismatch:{revision or 'unstamped'}"
            )
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise AuthorizationInputRepositoryError(f"database_state_mismatch:{status.state.value}")
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

    def append_audience_snapshot(self, snapshot: AudiencePolicySnapshot) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO audience_policy_snapshots "
                    "(id, workspace_id, visibility, policy_version, policy_digest, created_at, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(snapshot.id),
                        str(snapshot.workspace_id),
                        snapshot.visibility,
                        snapshot.policy_version,
                        snapshot.policy_digest,
                        snapshot.created_at.isoformat(),
                        snapshot.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorizationInputRepositoryError("audience_snapshot_identity_conflict") from exc

    def get_audience_snapshot(
        self,
        workspace_id: UUID,
        snapshot_id: UUID,
    ) -> AudiencePolicySnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM audience_policy_snapshots "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(snapshot_id)),
            ).fetchone()
        return None if row is None else AudiencePolicySnapshot.model_validate_json(row[0])

    def append_access_bundle(
        self,
        bundle: AccessBundle,
        grants: list[CapabilityGrant],
    ) -> None:
        try:
            validate_access_bundle(bundle, grants)
        except ValueError as exc:
            reason = getattr(exc, "reason_code", "access_bundle_invalid")
            raise AuthorizationInputRepositoryError(str(reason)) from exc
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO access_bundles "
                    "(id, workspace_id, principal_id, scope_kind, scope_id, version, "
                    "policy_digest, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(bundle.id),
                        str(bundle.workspace_id),
                        str(bundle.principal_id),
                        bundle.scope_kind,
                        str(bundle.scope_id),
                        bundle.version,
                        bundle.policy_digest,
                        bundle.created_at.isoformat(),
                        bundle.model_dump_json(),
                    ),
                )
                for grant in grants:
                    connection.execute(
                        "INSERT INTO capability_grants "
                        "(grant_digest, bundle_id, workspace_id, resource_kind, resource_id, "
                        "action, effect, created_at, payload_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            _canonical_digest(grant),
                            str(grant.bundle_id),
                            str(grant.workspace_id),
                            grant.resource_kind,
                            str(grant.resource_id),
                            grant.action,
                            grant.effect.value,
                            grant.created_at.isoformat(),
                            grant.model_dump_json(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise AuthorizationInputRepositoryError("access_bundle_identity_conflict") from exc

    def get_access_bundle(
        self,
        workspace_id: UUID,
        bundle_id: UUID,
    ) -> tuple[AccessBundle, list[CapabilityGrant]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM access_bundles WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(bundle_id)),
            ).fetchone()
            if row is None:
                return None
            grant_rows = connection.execute(
                "SELECT payload_json FROM capability_grants "
                "WHERE workspace_id = ? AND bundle_id = ? ORDER BY grant_digest",
                (str(workspace_id), str(bundle_id)),
            ).fetchall()
        return (
            AccessBundle.model_validate_json(row[0]),
            [CapabilityGrant.model_validate_json(grant_row[0]) for grant_row in grant_rows],
        )

    def append_agent_grant(self, grant: AgentGrant) -> None:
        try:
            with self._transaction() as connection:
                bundle = connection.execute(
                    "SELECT workspace_id FROM access_bundles WHERE id = ?",
                    (str(grant.capability_bundle_id),),
                ).fetchone()
                if bundle != (str(grant.workspace_id),):
                    raise AuthorizationInputRepositoryError("agent_grant_bundle_workspace_mismatch")
                connection.execute(
                    "INSERT INTO agent_grants "
                    "(id, workspace_id, agent_id, capability_bundle_id, autonomy_level, "
                    "created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(grant.id),
                        str(grant.workspace_id),
                        str(grant.agent_id),
                        str(grant.capability_bundle_id),
                        grant.autonomy_level,
                        grant.created_at.isoformat(),
                        grant.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorizationInputRepositoryError("agent_grant_identity_conflict") from exc

    def get_agent_grant(self, workspace_id: UUID, grant_id: UUID) -> AgentGrant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_grants WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(grant_id)),
            ).fetchone()
        return None if row is None else AgentGrant.model_validate_json(row[0])

    def append_delegation_grant(self, grant: DelegationGrant) -> None:
        try:
            with self._transaction() as connection:
                parent = connection.execute(
                    "SELECT workspace_id FROM agent_grants WHERE id = ?",
                    (str(grant.parent_agent_grant_id),),
                ).fetchone()
                if parent is None:
                    raise AuthorizationInputRepositoryError("delegation_parent_grant_missing")
                connection.execute(
                    "INSERT INTO delegation_grants "
                    "(id, workspace_id, parent_agent_grant_id, child_agent_id, expires_at, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(grant.id),
                        parent[0],
                        str(grant.parent_agent_grant_id),
                        str(grant.child_agent_id),
                        grant.expires_at.isoformat(),
                        grant.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorizationInputRepositoryError("delegation_grant_identity_conflict") from exc

    def get_delegation_grant(
        self,
        workspace_id: UUID,
        grant_id: UUID,
    ) -> DelegationGrant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM delegation_grants WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(grant_id)),
            ).fetchone()
        return None if row is None else DelegationGrant.model_validate_json(row[0])

    @staticmethod
    def signing_key_digest(key: WorkspaceSigningKeyVersion) -> str:
        return _canonical_digest(key)

    def append_signing_key(self, key: WorkspaceSigningKeyVersion) -> None:
        try:
            with self._transaction() as connection:
                previous = connection.execute(
                    "SELECT key_epoch, canonical_digest FROM workspace_signing_key_versions "
                    "WHERE workspace_id = ? ORDER BY key_epoch DESC LIMIT 1",
                    (str(key.workspace_id),),
                ).fetchone()
                if previous is None:
                    if key.key_epoch != 1:
                        raise AuthorizationInputRepositoryError("signing_key_epoch_skipped")
                    if key.predecessor_digest is not None:
                        raise AuthorizationInputRepositoryError("signing_key_predecessor_mismatch")
                else:
                    if key.key_epoch != int(previous[0]) + 1:
                        raise AuthorizationInputRepositoryError("signing_key_epoch_skipped")
                    if key.predecessor_digest != previous[1]:
                        raise AuthorizationInputRepositoryError("signing_key_predecessor_mismatch")
                digest = self.signing_key_digest(key)
                connection.execute(
                    "INSERT INTO workspace_signing_key_versions "
                    "(id, workspace_id, key_epoch, status, valid_from, valid_until, "
                    "predecessor_digest, canonical_digest, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(key.id),
                        str(key.workspace_id),
                        key.key_epoch,
                        key.status.value,
                        key.valid_from.isoformat(),
                        None if key.valid_until is None else key.valid_until.isoformat(),
                        key.predecessor_digest,
                        digest,
                        key.created_at.isoformat(),
                        key.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorizationInputRepositoryError("signing_key_identity_conflict") from exc

    def list_signing_keys(self, workspace_id: UUID) -> list[WorkspaceSigningKeyVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM workspace_signing_key_versions "
                "WHERE workspace_id = ? ORDER BY key_epoch",
                (str(workspace_id),),
            ).fetchall()
        return [WorkspaceSigningKeyVersion.model_validate_json(row[0]) for row in rows]

    def claim_idempotency(self, envelope: IdempotencyEnvelope) -> IdempotencyEnvelope:
        if envelope.status is not IdempotencyStatus.IN_PROGRESS:
            raise AuthorizationInputRepositoryError("idempotency_claim_not_in_progress")
        with self._transaction() as connection:
            row = self._idempotency_row(connection, envelope.composite_identity)
            if row is not None:
                existing = IdempotencyEnvelope.model_validate_json(row[0])
                try:
                    validate_idempotency_replay(
                        existing,
                        request_context_digest=envelope.request_context_digest,
                        payload_digest=envelope.payload_digest,
                    )
                except IdempotencyContractError as exc:
                    raise AuthorizationInputRepositoryError(exc.reason_code) from exc
                return existing
            try:
                connection.execute(
                    "INSERT INTO idempotency_envelopes "
                    "(id, workspace_id, requester_id, transport_principal_id, agent_id, "
                    "agent_grant_id, operation, idempotency_key, request_context_digest, "
                    "payload_digest, status, result_digest, result_ref, created_at, completed_at, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(envelope.id),
                        str(envelope.workspace_id),
                        str(envelope.requester_id),
                        str(envelope.transport_principal_id),
                        str(envelope.agent_id),
                        str(envelope.agent_grant_id),
                        envelope.operation,
                        envelope.idempotency_key,
                        envelope.request_context_digest,
                        envelope.payload_digest,
                        envelope.status.value,
                        None,
                        None,
                        envelope.created_at.isoformat(),
                        None,
                        envelope.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthorizationInputRepositoryError("idempotency_identity_conflict") from exc
        return envelope

    def complete_idempotency(self, envelope: IdempotencyEnvelope) -> None:
        if envelope.status is IdempotencyStatus.IN_PROGRESS:
            raise AuthorizationInputRepositoryError("idempotency_completion_not_terminal")
        with self._transaction() as connection:
            row = self._idempotency_row(connection, envelope.composite_identity)
            if row is None:
                raise AuthorizationInputRepositoryError("idempotency_not_found")
            current = IdempotencyEnvelope.model_validate_json(row[0])
            if current.status is not IdempotencyStatus.IN_PROGRESS:
                raise AuthorizationInputRepositoryError("idempotency_not_in_progress")
            if (
                current.id != envelope.id
                or current.request_context_digest != envelope.request_context_digest
                or current.payload_digest != envelope.payload_digest
                or current.created_at != envelope.created_at
            ):
                raise AuthorizationInputRepositoryError("idempotency_completion_identity_mismatch")
            cursor = connection.execute(
                "UPDATE idempotency_envelopes SET status = ?, result_digest = ?, "
                "result_ref = ?, completed_at = ?, payload_json = ? "
                "WHERE id = ? AND status = ?",
                (
                    envelope.status.value,
                    envelope.result_digest,
                    envelope.result_ref,
                    None if envelope.completed_at is None else envelope.completed_at.isoformat(),
                    envelope.model_dump_json(),
                    str(envelope.id),
                    IdempotencyStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorizationInputRepositoryError("idempotency_not_in_progress")

    def get_idempotency(
        self,
        identity: tuple[UUID, UUID, UUID, UUID, UUID, str, str],
    ) -> IdempotencyEnvelope | None:
        with self._connect() as connection:
            row = self._idempotency_row(connection, identity)
        return None if row is None else IdempotencyEnvelope.model_validate_json(row[0])

    @staticmethod
    def _idempotency_row(
        connection: sqlite3.Connection,
        identity: tuple[UUID, UUID, UUID, UUID, UUID, str, str],
    ) -> tuple[str] | None:
        workspace_id, requester_id, transport_id, agent_id, agent_grant_id, operation, key = (
            identity
        )
        row = connection.execute(
            "SELECT payload_json FROM idempotency_envelopes WHERE workspace_id = ? "
            "AND requester_id = ? AND transport_principal_id = ? AND agent_id = ? "
            "AND agent_grant_id = ? AND operation = ? AND idempotency_key = ?",
            (
                str(workspace_id),
                str(requester_id),
                str(transport_id),
                str(agent_id),
                str(agent_grant_id),
                operation,
                key,
            ),
        ).fetchone()
        return cast(tuple[str] | None, row)

    def close(self) -> None:
        return None
