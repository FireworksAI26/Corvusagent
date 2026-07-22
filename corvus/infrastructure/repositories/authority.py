from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from corvus.database import DatabaseState, classify_database
from corvus.domain.deployment import (
    AuthorityCommitIntent,
    AuthorityCommitState,
    AuthorityEpochCredential,
    AuthorityEpochCredentialStatus,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorStatus,
    DeploymentInstance,
    DeploymentInstanceLease,
    DeploymentInstanceStatus,
    DeploymentProfile,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class AuthorityRepositoryError(RuntimeError):
    pass


_ALLOWED_COMMIT_TRANSITIONS: dict[AuthorityCommitState, frozenset[AuthorityCommitState]] = {
    AuthorityCommitState.PREPARED: frozenset(
        {AuthorityCommitState.ANCHOR_RESERVED, AuthorityCommitState.QUARANTINED}
    ),
    AuthorityCommitState.ANCHOR_RESERVED: frozenset(
        {AuthorityCommitState.DB_COMMITTED, AuthorityCommitState.QUARANTINED}
    ),
    AuthorityCommitState.DB_COMMITTED: frozenset(
        {AuthorityCommitState.ANCHOR_FINALIZED, AuthorityCommitState.QUARANTINED}
    ),
    AuthorityCommitState.ANCHOR_FINALIZED: frozenset(),
    AuthorityCommitState.QUARANTINED: frozenset(),
}


class AuthorityRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise AuthorityRepositoryError(f"database_revision_mismatch:{revision or 'unstamped'}")
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise AuthorityRepositoryError(f"database_state_mismatch:{status.state.value}")
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

    def add_deployment_profile(self, profile: DeploymentProfile) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO deployment_profiles "
                    "(id, version, created_at, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        str(profile.id),
                        profile.version,
                        profile.created_at.isoformat(),
                        profile.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("deployment_profile_identity_conflict") from exc

    def get_deployment_profile(self, profile_id: UUID) -> DeploymentProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM deployment_profiles WHERE id = ?",
                (str(profile_id),),
            ).fetchone()
        return None if row is None else DeploymentProfile.model_validate_json(row[0])

    def add_deployment_instance(self, instance: DeploymentInstance) -> None:
        try:
            with self._transaction() as connection:
                profile = connection.execute(
                    "SELECT 1 FROM deployment_profiles WHERE id = ?",
                    (str(instance.deployment_profile_id),),
                ).fetchone()
                if profile is None:
                    raise AuthorityRepositoryError("deployment_instance_profile_missing")
                connection.execute(
                    "INSERT INTO deployment_instances "
                    "(id, deployment_profile_id, status, device_binding_digest, "
                    "activated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(instance.id),
                        str(instance.deployment_profile_id),
                        instance.status.value,
                        instance.device_binding_digest,
                        instance.activated_at.isoformat(),
                        instance.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("deployment_instance_identity_conflict") from exc

    def get_deployment_instance(self, instance_id: UUID) -> DeploymentInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM deployment_instances WHERE id = ?",
                (str(instance_id),),
            ).fetchone()
        return None if row is None else DeploymentInstance.model_validate_json(row[0])

    def add_epoch_credential(self, credential: AuthorityEpochCredential) -> None:
        try:
            with self._transaction() as connection:
                instance = self._instance(connection, credential.deployment_instance_id)
                if instance is None:
                    raise AuthorityRepositoryError("epoch_credential_instance_missing")
                if credential.device_binding_digest != instance.device_binding_digest:
                    raise AuthorityRepositoryError("epoch_credential_device_binding_mismatch")
                connection.execute(
                    "INSERT INTO authority_epoch_credentials "
                    "(id, workspace_id, authority_epoch, deployment_instance_id, status, "
                    "device_binding_digest, issued_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(credential.id),
                        str(credential.workspace_id),
                        credential.authority_epoch,
                        str(credential.deployment_instance_id),
                        credential.status.value,
                        credential.device_binding_digest,
                        credential.issued_at.isoformat(),
                        credential.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("epoch_credential_identity_conflict") from exc

    def get_epoch_credential(
        self,
        *,
        workspace_id: UUID,
        credential_id: UUID,
    ) -> AuthorityEpochCredential | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_epoch_credentials "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(credential_id)),
            ).fetchone()
        return None if row is None else AuthorityEpochCredential.model_validate_json(row[0])

    def add_trust_anchor(self, anchor: AuthorityTrustAnchor) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO authority_trust_anchors "
                    "(id, workspace_id, kind, status, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(anchor.id),
                        str(anchor.workspace_id),
                        anchor.kind.value,
                        anchor.status.value,
                        anchor.created_at.isoformat(),
                        anchor.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("authority_trust_anchor_identity_conflict") from exc

    def get_trust_anchor(
        self,
        *,
        workspace_id: UUID,
        trust_anchor_id: UUID,
    ) -> AuthorityTrustAnchor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_trust_anchors "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(trust_anchor_id)),
            ).fetchone()
        return None if row is None else AuthorityTrustAnchor.model_validate_json(row[0])

    def acquire_lease(self, lease: DeploymentInstanceLease) -> None:
        try:
            with self._transaction() as connection:
                instance = self._instance(connection, lease.deployment_instance_id)
                if instance is None or instance.status is not DeploymentInstanceStatus.ACTIVE:
                    raise AuthorityRepositoryError("lease_instance_not_active")
                active = connection.execute(
                    "SELECT id FROM deployment_instance_leases "
                    "WHERE workspace_id = ? AND authority_epoch = ? AND released_at IS NULL",
                    (str(lease.workspace_id), lease.authority_epoch),
                ).fetchone()
                if active is not None:
                    raise AuthorityRepositoryError("same_epoch_instance_lease_conflict")
                latest = connection.execute(
                    "SELECT MAX(fencing_token) FROM deployment_instance_leases "
                    "WHERE workspace_id = ? AND authority_epoch = ?",
                    (str(lease.workspace_id), lease.authority_epoch),
                ).fetchone()
                latest_token = 0 if latest is None or latest[0] is None else int(latest[0])
                if lease.fencing_token <= latest_token:
                    raise AuthorityRepositoryError("lease_fencing_token_not_advanced")
                if lease.fencing_token != latest_token + 1:
                    raise AuthorityRepositoryError("lease_fencing_token_skipped")
                connection.execute(
                    "INSERT INTO deployment_instance_leases "
                    "(id, workspace_id, authority_epoch, deployment_instance_id, lock_name, "
                    "fencing_token, acquired_at, released_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(lease.id),
                        str(lease.workspace_id),
                        lease.authority_epoch,
                        str(lease.deployment_instance_id),
                        lease.lock_name,
                        lease.fencing_token,
                        lease.acquired_at.isoformat(),
                        None,
                        lease.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("same_epoch_instance_lease_conflict") from exc

    def release_lease(
        self,
        lease: DeploymentInstanceLease,
        *,
        expected_fencing_token: int,
    ) -> None:
        if lease.released_at is None or lease.released_at < lease.acquired_at:
            raise AuthorityRepositoryError("lease_release_time_invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, fencing_token, released_at "
                "FROM deployment_instance_leases WHERE workspace_id = ? AND id = ?",
                (str(lease.workspace_id), str(lease.id)),
            ).fetchone()
            if row is None or row[2] is not None:
                raise AuthorityRepositoryError("lease_release_state_conflict")
            current = DeploymentInstanceLease.model_validate_json(row[0])
            expected = current.model_copy(update={"released_at": lease.released_at})
            if (
                row[1] != expected_fencing_token
                or lease.fencing_token != expected_fencing_token
                or lease != expected
            ):
                raise AuthorityRepositoryError("lease_fencing_token_mismatch")
            cursor = connection.execute(
                "UPDATE deployment_instance_leases "
                "SET released_at = ?, payload_json = ? "
                "WHERE workspace_id = ? AND id = ? AND fencing_token = ? "
                "AND released_at IS NULL",
                (
                    lease.released_at.isoformat(),
                    lease.model_dump_json(),
                    str(lease.workspace_id),
                    str(lease.id),
                    expected_fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorityRepositoryError("lease_release_state_conflict")

    def get_active_lease(
        self,
        workspace_id: UUID,
        authority_epoch: int,
    ) -> DeploymentInstanceLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM deployment_instance_leases "
                "WHERE workspace_id = ? AND authority_epoch = ? AND released_at IS NULL",
                (str(workspace_id), authority_epoch),
            ).fetchone()
        return None if row is None else DeploymentInstanceLease.model_validate_json(row[0])

    def add_workspace_authority(self, authority: WorkspaceAuthority) -> None:
        try:
            with self._transaction() as connection:
                self._validate_authority_references(connection, authority)
                connection.execute(
                    "INSERT INTO workspace_authorities "
                    "(id, workspace_id, deployment_profile_id, deployment_instance_id, epoch, "
                    "authority_generation, authority_state_root, authority_epoch_credential_id, "
                    "trust_anchor_id, active_lease_id, state, version, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(authority.id),
                        str(authority.workspace_id),
                        str(authority.deployment_profile_id),
                        str(authority.deployment_instance_id),
                        authority.epoch,
                        authority.authority_generation,
                        authority.authority_state_root,
                        str(authority.authority_epoch_credential_id),
                        str(authority.trust_anchor_id),
                        None
                        if authority.active_lease_id is None
                        else str(authority.active_lease_id),
                        authority.state.value,
                        authority.version,
                        authority.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("workspace_authority_identity_conflict") from exc

    def get_workspace_authority(self, workspace_id: UUID) -> WorkspaceAuthority | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspace_authorities WHERE workspace_id = ?",
                (str(workspace_id),),
            ).fetchone()
        return None if row is None else WorkspaceAuthority.model_validate_json(row[0])

    def quarantine_workspace_authority(
        self,
        *,
        workspace_id: UUID,
        expected_generation: int,
        expected_state_root: str,
    ) -> None:
        with self._transaction() as connection:
            authority = self._workspace_authority(connection, workspace_id)
            if authority is None or (
                authority.authority_generation,
                authority.authority_state_root,
            ) != (expected_generation, expected_state_root):
                raise AuthorityRepositoryError("workspace_authority_quarantine_state_mismatch")
            if authority.state is WorkspaceAuthorityState.RESTORE_QUARANTINE:
                return
            if authority.state is not WorkspaceAuthorityState.ACTIVE:
                raise AuthorityRepositoryError("workspace_authority_quarantine_state_invalid")
            quarantined = authority.model_copy(
                update={
                    "state": WorkspaceAuthorityState.RESTORE_QUARANTINE,
                    "version": authority.version + 1,
                }
            )
            cursor = connection.execute(
                "UPDATE workspace_authorities SET state = ?, version = ?, payload_json = ? "
                "WHERE workspace_id = ? AND authority_generation = ? "
                "AND authority_state_root = ? AND state = ? AND version = ?",
                (
                    quarantined.state.value,
                    quarantined.version,
                    quarantined.model_dump_json(),
                    str(workspace_id),
                    expected_generation,
                    expected_state_root,
                    authority.state.value,
                    authority.version,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorityRepositoryError("workspace_authority_quarantine_state_conflict")

    def prepare_commit(self, intent: AuthorityCommitIntent) -> None:
        if intent.state is not AuthorityCommitState.PREPARED:
            raise AuthorityRepositoryError("authority_commit_initial_state_invalid")
        try:
            with self._transaction() as connection:
                authority = self._workspace_authority(connection, intent.workspace_id)
                if authority is None or (
                    authority.epoch,
                    authority.deployment_instance_id,
                    authority.authority_generation,
                    authority.authority_state_root,
                ) != (
                    intent.epoch,
                    intent.deployment_instance_id,
                    intent.prior_generation,
                    intent.prior_state_root,
                ):
                    raise AuthorityRepositoryError("authority_commit_prior_state_mismatch")
                inflight = connection.execute(
                    "SELECT id FROM authority_commit_intents WHERE workspace_id = ? "
                    "AND state NOT IN ('anchor_finalized', 'quarantined')",
                    (str(intent.workspace_id),),
                ).fetchone()
                if inflight is not None:
                    raise AuthorityRepositoryError("authority_commit_in_progress")
                connection.execute(
                    "INSERT INTO authority_commit_intents "
                    "(id, workspace_id, epoch, deployment_instance_id, prior_generation, "
                    "next_generation, prior_state_root, mutation_digest, proposed_state_root, "
                    "state, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(intent.id),
                        str(intent.workspace_id),
                        intent.epoch,
                        str(intent.deployment_instance_id),
                        intent.prior_generation,
                        intent.next_generation,
                        intent.prior_state_root,
                        intent.mutation_digest,
                        intent.proposed_state_root,
                        intent.state.value,
                        intent.created_at.isoformat(),
                        intent.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityRepositoryError("authority_commit_in_progress") from exc

    def get_commit_intent(
        self,
        *,
        workspace_id: UUID,
        intent_id: UUID,
    ) -> AuthorityCommitIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_commit_intents "
                "WHERE workspace_id = ? AND id = ?",
                (str(workspace_id), str(intent_id)),
            ).fetchone()
        return None if row is None else AuthorityCommitIntent.model_validate_json(row[0])

    def advance_commit(
        self,
        intent: AuthorityCommitIntent,
        *,
        expected_state: AuthorityCommitState,
    ) -> None:
        if intent.state not in _ALLOWED_COMMIT_TRANSITIONS[expected_state]:
            raise AuthorityRepositoryError("authority_commit_transition_invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, state FROM authority_commit_intents "
                "WHERE workspace_id = ? AND id = ?",
                (str(intent.workspace_id), str(intent.id)),
            ).fetchone()
            if row is None or row[1] != expected_state.value:
                raise AuthorityRepositoryError("authority_commit_state_conflict")
            current = AuthorityCommitIntent.model_validate_json(row[0])
            if current.model_copy(update={"state": intent.state}) != intent:
                raise AuthorityRepositoryError("authority_commit_identity_mismatch")
            if intent.state is AuthorityCommitState.DB_COMMITTED:
                self._advance_workspace_authority(connection, intent)
            cursor = connection.execute(
                "UPDATE authority_commit_intents SET state = ?, payload_json = ? "
                "WHERE workspace_id = ? AND id = ? AND state = ?",
                (
                    intent.state.value,
                    intent.model_dump_json(),
                    str(intent.workspace_id),
                    str(intent.id),
                    expected_state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise AuthorityRepositoryError("authority_commit_state_conflict")

    @staticmethod
    def _instance(
        connection: sqlite3.Connection,
        instance_id: UUID,
    ) -> DeploymentInstance | None:
        row = connection.execute(
            "SELECT payload_json FROM deployment_instances WHERE id = ?",
            (str(instance_id),),
        ).fetchone()
        return None if row is None else DeploymentInstance.model_validate_json(row[0])

    @staticmethod
    def _workspace_authority(
        connection: sqlite3.Connection,
        workspace_id: UUID,
    ) -> WorkspaceAuthority | None:
        row = connection.execute(
            "SELECT payload_json FROM workspace_authorities WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
        return None if row is None else WorkspaceAuthority.model_validate_json(row[0])

    def _validate_authority_references(
        self,
        connection: sqlite3.Connection,
        authority: WorkspaceAuthority,
    ) -> None:
        profile = connection.execute(
            "SELECT id FROM deployment_profiles WHERE id = ?",
            (str(authority.deployment_profile_id),),
        ).fetchone()
        instance = self._instance(connection, authority.deployment_instance_id)
        if (
            profile is None
            or instance is None
            or (
                instance.deployment_profile_id != authority.deployment_profile_id
                or instance.status is not DeploymentInstanceStatus.ACTIVE
            )
        ):
            raise AuthorityRepositoryError("authority_deployment_instance_mismatch")
        credential_row = connection.execute(
            "SELECT payload_json FROM authority_epoch_credentials WHERE id = ?",
            (str(authority.authority_epoch_credential_id),),
        ).fetchone()
        credential = (
            None
            if credential_row is None
            else AuthorityEpochCredential.model_validate_json(credential_row[0])
        )
        if credential is None or (
            credential.workspace_id != authority.workspace_id
            or credential.authority_epoch != authority.epoch
            or credential.deployment_instance_id != authority.deployment_instance_id
            or credential.device_binding_digest != instance.device_binding_digest
            or credential.status is not AuthorityEpochCredentialStatus.ACTIVE
        ):
            raise AuthorityRepositoryError("authority_epoch_credential_mismatch")
        anchor_row = connection.execute(
            "SELECT payload_json FROM authority_trust_anchors WHERE id = ?",
            (str(authority.trust_anchor_id),),
        ).fetchone()
        anchor = (
            None if anchor_row is None else AuthorityTrustAnchor.model_validate_json(anchor_row[0])
        )
        if anchor is None or (
            anchor.workspace_id != authority.workspace_id
            or anchor.status is not AuthorityTrustAnchorStatus.ACTIVE
        ):
            raise AuthorityRepositoryError("authority_trust_anchor_mismatch")
        if authority.active_lease_id is None:
            return
        lease_row = connection.execute(
            "SELECT payload_json FROM deployment_instance_leases "
            "WHERE id = ? AND released_at IS NULL",
            (str(authority.active_lease_id),),
        ).fetchone()
        lease = (
            None if lease_row is None else DeploymentInstanceLease.model_validate_json(lease_row[0])
        )
        if lease is None or (
            lease.workspace_id != authority.workspace_id
            or lease.authority_epoch != authority.epoch
            or lease.deployment_instance_id != authority.deployment_instance_id
        ):
            raise AuthorityRepositoryError("authority_active_lease_mismatch")

    def _advance_workspace_authority(
        self,
        connection: sqlite3.Connection,
        intent: AuthorityCommitIntent,
    ) -> None:
        authority = self._workspace_authority(connection, intent.workspace_id)
        if authority is None or (
            authority.epoch,
            authority.deployment_instance_id,
            authority.authority_generation,
            authority.authority_state_root,
        ) != (
            intent.epoch,
            intent.deployment_instance_id,
            intent.prior_generation,
            intent.prior_state_root,
        ):
            raise AuthorityRepositoryError("authority_commit_prior_state_mismatch")
        advanced = authority.model_copy(
            update={
                "authority_generation": intent.next_generation,
                "authority_state_root": intent.proposed_state_root,
                "version": authority.version + 1,
            }
        )
        cursor = connection.execute(
            "UPDATE workspace_authorities SET authority_generation = ?, "
            "authority_state_root = ?, version = ?, payload_json = ? "
            "WHERE workspace_id = ? AND authority_generation = ? "
            "AND authority_state_root = ? AND version = ?",
            (
                advanced.authority_generation,
                advanced.authority_state_root,
                advanced.version,
                advanced.model_dump_json(),
                str(intent.workspace_id),
                authority.authority_generation,
                authority.authority_state_root,
                authority.version,
            ),
        )
        if cursor.rowcount != 1:
            raise AuthorityRepositoryError("authority_commit_state_conflict")

    def close(self) -> None:
        return None
