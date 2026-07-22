from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from alembic.runtime.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, text

from corvus.database import DatabaseState, classify_database
from corvus.domain.account import DeviceRegistration, DeviceStatus
from corvus.domain.identity import MembershipStatus, Workspace
from corvus.domain.sync import (
    AccountProfile,
    AccountProfilePayload,
    SyncApplyResult,
    SyncConflictDetail,
    SyncConflictError,
    SyncMutation,
    SyncMutationResult,
    SyncPage,
    SyncProtocolError,
    WorkspaceChange,
    WorkspaceProfile,
    WorkspaceProfilePayload,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision
from corvus.security import SecurityError, canonical_json_bytes, reject_sensitive_payload

_GENESIS_DIGEST = "0" * 64
_WORKSPACE_UPDATE_ROLES = frozenset({"owner", "admin", "manager"})


class SyncRepository:
    def __init__(self, database: Path | Engine) -> None:
        self._owns_engine = isinstance(database, Path)
        if isinstance(database, Path):
            revision = current_revision(database)
            if revision != M1_CURRENT_REVISION:
                raise SyncProtocolError("database_revision_mismatch")
            if classify_database(database).state is not DatabaseState.CURRENT:
                raise SyncProtocolError("database_state_mismatch")
            self.engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 30})
        else:
            self.engine = database
            with self.engine.connect() as connection:
                revision = MigrationContext.configure(connection).get_current_revision()
            if revision != M1_CURRENT_REVISION:
                raise SyncProtocolError("database_revision_mismatch")
        if self.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise SyncProtocolError("unsupported_repository_dialect")

    @staticmethod
    def _prepare(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    @contextmanager
    def _write_transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            self._prepare(connection)
            transaction = None
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                transaction = connection.begin()
            try:
                yield connection
            except BaseException:
                if transaction is None:
                    connection.rollback()
                else:
                    transaction.rollback()
                raise
            else:
                if transaction is None:
                    connection.commit()
                else:
                    transaction.commit()

    @contextmanager
    def _read_transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            with connection.begin():
                self._prepare(connection)
                yield connection

    @staticmethod
    def _authorize(
        connection: Connection,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
    ) -> tuple[Workspace, str, int]:
        account_principal = connection.scalar(
            text("SELECT principal_id FROM accounts WHERE id = :account_id"),
            {"account_id": str(account_id)},
        )
        if account_principal != str(principal_id):
            raise SyncProtocolError("workspace_not_found")
        device_payload = connection.scalar(
            text(
                "SELECT payload_json FROM device_registrations "
                "WHERE account_id = :account_id AND id = :device_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"account_id": str(account_id), "device_id": str(device_id)},
        )
        if device_payload is None:
            raise SyncProtocolError("device_not_found")
        device = DeviceRegistration.model_validate_json(device_payload)
        if device.status is not DeviceStatus.ACTIVE or device.version != device_version:
            raise SyncProtocolError("device_not_found")
        membership = connection.execute(
            text(
                "SELECT role, status, version FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND principal_id = :principal_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"workspace_id": str(workspace_id), "principal_id": str(principal_id)},
        ).one_or_none()
        if membership is None or membership[1] != MembershipStatus.ACTIVE.value:
            raise SyncProtocolError("workspace_not_found")
        workspace_payload = connection.scalar(
            text(
                "SELECT payload_json FROM identity_workspaces WHERE id = :workspace_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"workspace_id": str(workspace_id)},
        )
        if workspace_payload is None:
            raise SyncProtocolError("workspace_not_found")
        return (
            Workspace.model_validate_json(workspace_payload),
            str(membership[0]).casefold(),
            int(membership[2]),
        )

    @staticmethod
    def _lock_workspace_profile(connection: Connection, workspace_id: UUID) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "SELECT id FROM identity_workspaces "
                    "WHERE id = :workspace_id AND version = 1 FOR UPDATE"
                ),
                {"workspace_id": str(workspace_id)},
            ).one_or_none()

    @staticmethod
    def _lock_account_profile(connection: Connection, account_id: UUID) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT id FROM accounts WHERE id = :account_id FOR UPDATE"),
                {"account_id": str(account_id)},
            ).one_or_none()

    @staticmethod
    def _has_workspace_sync_history(connection: Connection, workspace_id: UUID) -> bool:
        return bool(
            connection.scalar(
                text(
                    "SELECT CASE WHEN "
                    "EXISTS (SELECT 1 FROM workspace_changes WHERE workspace_id = :workspace_id) "
                    "OR EXISTS (SELECT 1 FROM outbox_events WHERE workspace_id = :workspace_id) "
                    "OR EXISTS (SELECT 1 FROM device_sync_acknowledgements "
                    "WHERE workspace_id = :workspace_id) "
                    "OR EXISTS (SELECT 1 FROM platform_idempotency "
                    "WHERE workspace_id = :workspace_id) THEN 1 ELSE 0 END"
                ),
                {"workspace_id": str(workspace_id)},
            )
        )

    @classmethod
    def _head(
        cls,
        connection: Connection,
        *,
        workspace: Workspace,
        now: datetime,
        create: bool,
    ) -> tuple[int, int, str, int]:
        if create:
            connection.execute(
                text(
                    "INSERT INTO workspace_sync_heads "
                    "(workspace_id, workspace_version, current_sequence, retention_floor, "
                    "chain_digest, version, updated_at) VALUES (:workspace_id, "
                    ":workspace_version, 0, 0, :digest, 1, :updated_at) "
                    "ON CONFLICT (workspace_id) DO NOTHING"
                ),
                {
                    "workspace_id": str(workspace.id),
                    "workspace_version": workspace.version,
                    "digest": _GENESIS_DIGEST,
                    "updated_at": now.isoformat(),
                },
            )
        if connection.dialect.name == "postgresql" and create:
            head_statement = text(
                "SELECT current_sequence, retention_floor, chain_digest, version "
                "FROM workspace_sync_heads WHERE workspace_id = :workspace_id FOR UPDATE"
            )
        else:
            head_statement = text(
                "SELECT current_sequence, retention_floor, chain_digest, version "
                "FROM workspace_sync_heads WHERE workspace_id = :workspace_id"
            )
        row = connection.execute(
            head_statement,
            {"workspace_id": str(workspace.id)},
        ).one_or_none()
        if row is None:
            if cls._has_workspace_sync_history(connection, workspace.id):
                raise SyncProtocolError("sync_change_integrity_invalid")
            return 0, 0, _GENESIS_DIGEST, 0
        return int(row[0]), int(row[1]), str(row[2]), int(row[3])

    @classmethod
    def _validate_head_tail(
        cls,
        connection: Connection,
        *,
        workspace_id: UUID,
        current_sequence: int,
        chain_digest: str,
    ) -> None:
        if current_sequence == 0:
            if chain_digest != _GENESIS_DIGEST or cls._has_workspace_sync_history(
                connection, workspace_id
            ):
                raise SyncProtocolError("sync_change_integrity_invalid")
            return
        tail = connection.execute(
            text(
                "SELECT workspace_id, workspace_version, sequence, previous_digest, "
                "change_digest, kind, operation, entity_id, entity_version, payload_json, "
                "account_id, principal_id, membership_version, device_id, device_version, "
                "created_at FROM workspace_changes "
                "WHERE workspace_id = :workspace_id AND sequence = :sequence"
            ),
            {"workspace_id": str(workspace_id), "sequence": current_sequence},
        ).one_or_none()
        if tail is None or tail.change_digest != chain_digest:
            raise SyncProtocolError("sync_change_integrity_invalid")
        if current_sequence == 1:
            expected_previous = _GENESIS_DIGEST
        else:
            expected_previous = connection.scalar(
                text(
                    "SELECT change_digest FROM workspace_changes "
                    "WHERE workspace_id = :workspace_id AND sequence = :sequence"
                ),
                {"workspace_id": str(workspace_id), "sequence": current_sequence - 1},
            )
            if expected_previous is None:
                raise SyncProtocolError("sync_change_integrity_invalid")
        cls._validated_change(tail, str(expected_previous))

    @staticmethod
    def _latest_ack(connection: Connection, workspace_id: UUID, device_id: UUID) -> tuple[int, int]:
        row = connection.execute(
            text(
                "SELECT version, acknowledged_sequence FROM device_sync_acknowledgements "
                "WHERE workspace_id = :workspace_id AND device_id = :device_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"workspace_id": str(workspace_id), "device_id": str(device_id)},
        ).one_or_none()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    @classmethod
    def _acknowledge(
        cls,
        connection: Connection,
        *,
        workspace: Workspace,
        account_id: UUID,
        principal_id: UUID,
        membership_version: int,
        device_id: UUID,
        device_version: int,
        acknowledged_cursor: int,
        high_watermark: int,
        now: datetime,
    ) -> int:
        version, current = cls._latest_ack(connection, workspace.id, device_id)
        if acknowledged_cursor > high_watermark:
            raise SyncProtocolError("sync_acknowledgement_ahead")
        if acknowledged_cursor < current:
            raise SyncProtocolError("sync_acknowledgement_rewind")
        if acknowledged_cursor > current:
            connection.execute(
                text(
                    "INSERT INTO device_sync_acknowledgements "
                    "(workspace_id, workspace_version, device_id, version, account_id, "
                    "principal_id, membership_version, device_version, acknowledged_sequence, "
                    "created_at) VALUES (:workspace_id, :workspace_version, :device_id, "
                    ":version, :account_id, :principal_id, :membership_version, "
                    ":device_version, :acknowledged_sequence, :created_at)"
                ),
                {
                    "workspace_id": str(workspace.id),
                    "workspace_version": workspace.version,
                    "device_id": str(device_id),
                    "version": version + 1,
                    "account_id": str(account_id),
                    "principal_id": str(principal_id),
                    "membership_version": membership_version,
                    "device_version": device_version,
                    "acknowledged_sequence": acknowledged_cursor,
                    "created_at": now.isoformat(),
                },
            )
        return acknowledged_cursor

    @staticmethod
    def _idempotency_scope(workspace_id: UUID, device_id: UUID) -> str:
        return f"{workspace_id}:{device_id}"

    @classmethod
    def _replay(
        cls,
        connection: Connection,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        membership_version: int,
        device_id: UUID,
        device_version: int,
        mutation: SyncMutation,
        request_digest: str,
    ) -> SyncMutationResult | None:
        row = connection.execute(
            text(
                "SELECT request_digest, result_json, principal_id, membership_version, "
                "device_version FROM platform_idempotency "
                "WHERE account_id = :account_id AND scope_key = :scope_key "
                "AND operation = :operation AND idempotency_key = :idempotency_key "
                "AND workspace_id = :workspace_id AND device_id = :device_id"
            ),
            {
                "account_id": str(account_id),
                "scope_key": cls._idempotency_scope(workspace_id, device_id),
                "operation": f"{mutation.kind}.{mutation.operation}",
                "idempotency_key": mutation.idempotency_key,
                "workspace_id": str(workspace_id),
                "device_id": str(device_id),
            },
        ).one_or_none()
        if row is None:
            return None
        if (
            row[0] != request_digest
            or row[2] != str(principal_id)
            or int(row[3]) != membership_version
            or int(row[4]) != device_version
        ):
            raise SyncProtocolError("idempotency_payload_mismatch")
        try:
            return SyncMutationResult.model_validate_json(row[1])
        except ValueError as exc:
            raise SyncProtocolError("idempotency_result_invalid") from exc

    @classmethod
    def _record_idempotency(
        cls,
        connection: Connection,
        *,
        workspace: Workspace,
        account_id: UUID,
        principal_id: UUID,
        membership_version: int,
        device_id: UUID,
        device_version: int,
        mutation: SyncMutation,
        request_digest: str,
        result: SyncMutationResult,
        now: datetime,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO platform_idempotency "
                "(account_id, principal_id, scope_key, workspace_id, workspace_version, "
                "membership_version, device_id, device_version, operation, idempotency_key, "
                "request_digest, result_json, created_at) VALUES (:account_id, :principal_id, "
                ":scope_key, :workspace_id, :workspace_version, :membership_version, "
                ":device_id, :device_version, :operation, :idempotency_key, :request_digest, "
                ":result_json, :created_at)"
            ),
            {
                "account_id": str(account_id),
                "principal_id": str(principal_id),
                "scope_key": cls._idempotency_scope(workspace.id, device_id),
                "workspace_id": str(workspace.id),
                "workspace_version": workspace.version,
                "membership_version": membership_version,
                "device_id": str(device_id),
                "device_version": device_version,
                "operation": f"{mutation.kind}.{mutation.operation}",
                "idempotency_key": mutation.idempotency_key,
                "request_digest": request_digest,
                "result_json": result.model_dump_json(),
                "created_at": now.isoformat(),
            },
        )

    @staticmethod
    def _account_profile(connection: Connection, account_id: UUID) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT experience_kind, version FROM account_onboarding_versions "
                "WHERE account_id = :account_id ORDER BY version DESC LIMIT 1"
            ),
            {"account_id": str(account_id)},
        ).one_or_none()
        if row is None:
            row = connection.execute(
                text("SELECT experience_kind, version FROM accounts WHERE id = :account_id"),
                {"account_id": str(account_id)},
            ).one_or_none()
        if row is None:
            raise SyncProtocolError("account_profile_not_found")
        return {
            "entity_id": str(account_id),
            "experience_kind": row[0],
            "version": int(row[1]),
        }

    @staticmethod
    def _workspace_profile(workspace: Workspace) -> dict[str, Any]:
        return {
            "entity_id": str(workspace.id),
            "name": workspace.name,
            "workspace_kind": workspace.workspace_kind.value,
            "status": workspace.status.value,
            "version": workspace.version,
        }

    @classmethod
    def _apply_entity(
        cls,
        connection: Connection,
        *,
        mutation: SyncMutation,
        mutation_index: int,
        workspace: Workspace,
        role: str,
        account_id: UUID,
        now: datetime,
    ) -> tuple[dict[str, Any], Workspace]:
        if mutation.kind == "account_profile":
            if mutation.entity_id != account_id or not isinstance(
                mutation.payload, AccountProfilePayload
            ):
                raise SyncProtocolError("account_profile_not_found")
            current = cls._account_profile(connection, account_id)
            if current["version"] != mutation.expected_version:
                raise SyncConflictError(
                    SyncConflictDetail(
                        mutation_index=mutation_index,
                        submitted_expected_version=mutation.expected_version,
                        current_version=cast(int, current["version"]),
                        current_profile=current,
                    )
                )
            version = mutation.expected_version + 1
            payload = {
                "account_id": str(account_id),
                "experience_kind": mutation.payload.experience_kind.value,
                "version": version,
                "updated_at": now.isoformat(),
            }
            connection.execute(
                text(
                    "INSERT INTO account_onboarding_versions "
                    "(account_id, version, experience_kind, updated_at, payload_json) "
                    "VALUES (:account_id, :version, :experience_kind, :updated_at, :payload_json)"
                ),
                {**payload, "payload_json": json.dumps(payload, separators=(",", ":"))},
            )
            return {
                "entity_id": str(account_id),
                "experience_kind": mutation.payload.experience_kind.value,
                "version": version,
            }, workspace

        if mutation.entity_id != workspace.id or not isinstance(
            mutation.payload, WorkspaceProfilePayload
        ):
            raise SyncProtocolError("workspace_not_found")
        current_profile = cls._workspace_profile(workspace)
        if workspace.version != mutation.expected_version:
            raise SyncConflictError(
                SyncConflictDetail(
                    mutation_index=mutation_index,
                    submitted_expected_version=mutation.expected_version,
                    current_version=workspace.version,
                    current_profile=current_profile,
                )
            )
        if role not in _WORKSPACE_UPDATE_ROLES:
            raise SyncProtocolError("workspace_update_forbidden")
        updated = workspace.model_copy(
            update={
                "name": mutation.payload.name or workspace.name,
                "workspace_kind": mutation.payload.workspace_kind or workspace.workspace_kind,
                "version": workspace.version + 1,
                "updated_at": now,
            }
        )
        connection.execute(
            text(
                "INSERT INTO identity_workspaces "
                "(id, version, name, workspace_kind, status, created_at, updated_at, payload_json) "
                "VALUES (:id, :version, :name, :workspace_kind, :status, :created_at, "
                ":updated_at, :payload_json)"
            ),
            {
                "id": str(updated.id),
                "version": updated.version,
                "name": updated.name,
                "workspace_kind": updated.workspace_kind.value,
                "status": updated.status.value,
                "created_at": updated.created_at.isoformat(),
                "updated_at": updated.updated_at.isoformat(),
                "payload_json": updated.model_dump_json(),
            },
        )
        return cls._workspace_profile(updated), updated

    @staticmethod
    def _change_digest(body: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json_bytes(body)).hexdigest()

    @classmethod
    def _append_change(
        cls,
        connection: Connection,
        *,
        workspace: Workspace,
        sequence: int,
        previous_digest: str,
        mutation: SyncMutation,
        profile: dict[str, Any],
        account_id: UUID,
        principal_id: UUID,
        membership_version: int,
        device_id: UUID,
        device_version: int,
        now: datetime,
    ) -> tuple[SyncMutationResult, str]:
        body = {
            "workspace_id": workspace.id,
            "workspace_version": workspace.version,
            "sequence": sequence,
            "previous_digest": previous_digest,
            "kind": mutation.kind,
            "operation": mutation.operation,
            "entity_id": mutation.entity_id,
            "entity_version": profile["version"],
            "payload": profile,
            "account_id": account_id,
            "principal_id": principal_id,
            "membership_version": membership_version,
            "device_id": device_id,
            "device_version": device_version,
            "created_at": now,
        }
        digest = cls._change_digest(body)
        payload_json = canonical_json_bytes(profile).decode("utf-8")
        connection.execute(
            text(
                "INSERT INTO workspace_changes "
                "(workspace_id, workspace_version, sequence, previous_digest, change_digest, "
                "kind, operation, entity_id, entity_version, payload_json, account_id, "
                "principal_id, membership_version, device_id, device_version, created_at) "
                "VALUES (:workspace_id, "
                ":workspace_version, :sequence, :previous_digest, :change_digest, :kind, "
                ":operation, :entity_id, :entity_version, :payload_json, :account_id, "
                ":principal_id, :membership_version, :device_id, :device_version, :created_at)"
            ),
            {
                "workspace_id": str(workspace.id),
                "workspace_version": workspace.version,
                "sequence": sequence,
                "previous_digest": previous_digest,
                "change_digest": digest,
                "kind": mutation.kind,
                "operation": mutation.operation,
                "entity_id": str(mutation.entity_id),
                "entity_version": profile["version"],
                "payload_json": payload_json,
                "account_id": str(account_id),
                "principal_id": str(principal_id),
                "membership_version": membership_version,
                "device_id": str(device_id),
                "device_version": device_version,
                "created_at": json.loads(canonical_json_bytes(now)),
            },
        )
        event = {**body, "change_digest": digest}
        connection.execute(
            text(
                "INSERT INTO outbox_events "
                "(workspace_id, sequence, change_digest, event_kind, payload_json, created_at) "
                "VALUES (:workspace_id, :sequence, :change_digest, 'workspace.change', "
                ":payload_json, :created_at)"
            ),
            {
                "workspace_id": str(workspace.id),
                "sequence": sequence,
                "change_digest": digest,
                "payload_json": canonical_json_bytes(event).decode("utf-8"),
                "created_at": now.isoformat(),
            },
        )
        profile_model: AccountProfile | WorkspaceProfile
        if mutation.kind == "account_profile":
            profile_model = AccountProfile.model_validate(profile)
        else:
            profile_model = WorkspaceProfile.model_validate(profile)
        return (
            SyncMutationResult(
                idempotency_key=mutation.idempotency_key,
                kind=mutation.kind,
                operation=mutation.operation,
                entity_id=mutation.entity_id,
                entity_version=cast(int, profile["version"]),
                sequence=sequence,
                profile=profile_model,
            ),
            digest,
        )

    def apply(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        acknowledged_cursor: int,
        mutations: tuple[SyncMutation, ...],
        now: datetime,
    ) -> SyncApplyResult:
        for mutation in mutations:
            reject_sensitive_payload(mutation.model_dump(mode="python"))
        with self._write_transaction() as connection:
            self._authorize(
                connection,
                workspace_id=workspace_id,
                account_id=account_id,
                principal_id=principal_id,
                device_id=device_id,
                device_version=device_version,
            )
            self._lock_workspace_profile(connection, workspace_id)
            if any(mutation.kind == "account_profile" for mutation in mutations):
                self._lock_account_profile(connection, account_id)
            workspace, role, membership_version = self._authorize(
                connection,
                workspace_id=workspace_id,
                account_id=account_id,
                principal_id=principal_id,
                device_id=device_id,
                device_version=device_version,
            )
            sequence, _retention_floor, chain_digest, head_version = self._head(
                connection, workspace=workspace, now=now, create=False
            )
            if mutations and head_version == 0:
                sequence, _retention_floor, chain_digest, head_version = self._head(
                    connection, workspace=workspace, now=now, create=True
                )
            self._validate_head_tail(
                connection,
                workspace_id=workspace_id,
                current_sequence=sequence,
                chain_digest=chain_digest,
            )
            initial_sequence = sequence
            acknowledged = self._acknowledge(
                connection,
                workspace=workspace,
                account_id=account_id,
                principal_id=principal_id,
                membership_version=membership_version,
                device_id=device_id,
                device_version=device_version,
                acknowledged_cursor=acknowledged_cursor,
                high_watermark=sequence,
                now=now,
            )
            results: list[SyncMutationResult] = []
            for index, mutation in enumerate(mutations):
                request_digest = hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "workspace_id": workspace_id,
                            "account_id": account_id,
                            "principal_id": principal_id,
                            "membership_version": membership_version,
                            "device_id": device_id,
                            "device_version": device_version,
                            "mutation": mutation.model_dump(mode="python"),
                        }
                    )
                ).hexdigest()
                replay = self._replay(
                    connection,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    principal_id=principal_id,
                    membership_version=membership_version,
                    device_id=device_id,
                    device_version=device_version,
                    mutation=mutation,
                    request_digest=request_digest,
                )
                if replay is not None:
                    results.append(replay)
                    continue
                profile, workspace = self._apply_entity(
                    connection,
                    mutation=mutation,
                    mutation_index=index,
                    workspace=workspace,
                    role=role,
                    account_id=account_id,
                    now=now,
                )
                sequence += 1
                result, chain_digest = self._append_change(
                    connection,
                    workspace=workspace,
                    sequence=sequence,
                    previous_digest=chain_digest,
                    mutation=mutation,
                    profile=profile,
                    account_id=account_id,
                    principal_id=principal_id,
                    membership_version=membership_version,
                    device_id=device_id,
                    device_version=device_version,
                    now=now,
                )
                self._record_idempotency(
                    connection,
                    workspace=workspace,
                    account_id=account_id,
                    principal_id=principal_id,
                    membership_version=membership_version,
                    device_id=device_id,
                    device_version=device_version,
                    mutation=mutation,
                    request_digest=request_digest,
                    result=result,
                    now=now,
                )
                results.append(result)
            if sequence > initial_sequence:
                connection.execute(
                    text(
                        "UPDATE workspace_sync_heads SET workspace_version = :workspace_version, "
                        "current_sequence = :sequence, chain_digest = :chain_digest, "
                        "version = :version, updated_at = :updated_at "
                        "WHERE workspace_id = :workspace_id"
                    ),
                    {
                        "workspace_id": str(workspace.id),
                        "workspace_version": workspace.version,
                        "sequence": sequence,
                        "chain_digest": chain_digest,
                        "version": head_version + 1,
                        "updated_at": now.isoformat(),
                    },
                )
            return SyncApplyResult(acknowledged_cursor=acknowledged, results=tuple(results))

    @classmethod
    def _validated_change(cls, row: Any, expected_previous: str | None) -> WorkspaceChange:
        try:

            def reject_constant(_value: str) -> None:
                raise ValueError("payload_nonfinite")

            def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("payload_duplicate_key")
                    result[key] = value
                return result

            if not isinstance(row.payload_json, str):
                raise ValueError("payload_not_text")
            payload = json.loads(
                row.payload_json,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            if not isinstance(payload, dict):
                raise ValueError("payload_not_object")
            if canonical_json_bytes(payload).decode("utf-8") != row.payload_json:
                raise ValueError("payload_not_canonical")
            reject_sensitive_payload(payload)
            if row.kind == "account_profile" and row.operation == "set_experience":
                profile: AccountProfile | WorkspaceProfile = AccountProfile.model_validate(payload)
            elif row.kind == "workspace_profile" and row.operation == "update":
                profile = WorkspaceProfile.model_validate(payload)
            else:
                raise ValueError("change_kind_operation_invalid")
            typed_payload = profile.model_dump(mode="json")
            if typed_payload != payload:
                raise ValueError("profile_not_canonical")
            if (
                typed_payload["entity_id"] != row.entity_id
                or typed_payload["version"] != row.entity_version
            ):
                raise ValueError("payload_column_mismatch")
            workspace_id = UUID(row.workspace_id)
            entity_id = UUID(row.entity_id)
            account_id = UUID(row.account_id)
            principal_id = UUID(row.principal_id)
            device_id = UUID(row.device_id)
            if any(
                canonical != raw
                for canonical, raw in (
                    (str(workspace_id), row.workspace_id),
                    (str(entity_id), row.entity_id),
                    (str(account_id), row.account_id),
                    (str(principal_id), row.principal_id),
                    (str(device_id), row.device_id),
                )
            ):
                raise ValueError("identifier_not_canonical")
            if row.kind == "account_profile" and entity_id != account_id:
                raise ValueError("account_profile_entity_mismatch")
            if row.kind == "workspace_profile" and (
                entity_id != workspace_id or row.entity_version != row.workspace_version
            ):
                raise ValueError("workspace_profile_entity_mismatch")
            created_at = datetime.fromisoformat(row.created_at)
            canonical_created_at = json.loads(canonical_json_bytes(created_at))
            if canonical_created_at != row.created_at:
                raise ValueError("created_at_not_canonical")
            body = {
                "workspace_id": workspace_id,
                "workspace_version": row.workspace_version,
                "sequence": row.sequence,
                "previous_digest": row.previous_digest,
                "kind": row.kind,
                "operation": row.operation,
                "entity_id": entity_id,
                "entity_version": row.entity_version,
                "payload": typed_payload,
                "account_id": account_id,
                "principal_id": principal_id,
                "membership_version": row.membership_version,
                "device_id": device_id,
                "device_version": row.device_version,
                "created_at": created_at,
            }
            if cls._change_digest(body) != row.change_digest:
                raise ValueError("change_digest_mismatch")
            if expected_previous is not None and row.previous_digest != expected_previous:
                raise ValueError("change_chain_mismatch")
            return WorkspaceChange(
                workspace_id=workspace_id,
                workspace_version=row.workspace_version,
                sequence=row.sequence,
                previous_digest=row.previous_digest,
                change_digest=row.change_digest,
                kind=row.kind,
                operation=row.operation,
                entity_id=entity_id,
                entity_version=row.entity_version,
                payload=profile,
                account_id=account_id,
                principal_id=principal_id,
                membership_version=row.membership_version,
                device_id=device_id,
                device_version=row.device_version,
                created_at=created_at,
            )
        except (SecurityError, ValidationError, TypeError, ValueError, KeyError) as exc:
            raise SyncProtocolError("sync_change_integrity_invalid") from exc

    def page(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        cursor: int,
        limit: int,
    ) -> SyncPage:
        with self._read_transaction() as connection:
            workspace, _role, _membership_version = self._authorize(
                connection,
                workspace_id=workspace_id,
                account_id=account_id,
                principal_id=principal_id,
                device_id=device_id,
                device_version=device_version,
            )
            high_watermark, retention_floor, head_digest, _version = self._head(
                connection,
                workspace=workspace,
                now=datetime.now().astimezone(),
                create=False,
            )
            self._validate_head_tail(
                connection,
                workspace_id=workspace_id,
                current_sequence=high_watermark,
                chain_digest=head_digest,
            )
            if cursor > high_watermark:
                raise SyncProtocolError("sync_cursor_ahead")
            if cursor < retention_floor:
                raise SyncProtocolError(
                    "sync_resync_required",
                    {
                        "earliest_available": retention_floor + 1,
                        "latest_sequence": high_watermark,
                        "resume_cursor": retention_floor,
                        "resources": [
                            "/api/v2/session",
                            f"/api/v2/workspaces/{workspace_id}",
                        ],
                    },
                )
            rows = connection.execute(
                text(
                    "SELECT workspace_id, workspace_version, sequence, previous_digest, "
                    "change_digest, kind, operation, entity_id, entity_version, payload_json, "
                    "account_id, principal_id, membership_version, device_id, device_version, "
                    "created_at "
                    "FROM workspace_changes WHERE workspace_id = :workspace_id "
                    "AND sequence > :cursor AND sequence <= :high_watermark "
                    "ORDER BY sequence LIMIT :limit"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "cursor": cursor,
                    "high_watermark": high_watermark,
                    "limit": limit,
                },
            ).all()
            expected_previous: str | None = None
            if cursor == 0 and retention_floor == 0:
                expected_previous = _GENESIS_DIGEST
            elif cursor > retention_floor:
                expected_previous = connection.scalar(
                    text(
                        "SELECT change_digest FROM workspace_changes "
                        "WHERE workspace_id = :workspace_id AND sequence = :cursor"
                    ),
                    {"workspace_id": str(workspace_id), "cursor": cursor},
                )
                if expected_previous is None:
                    raise SyncProtocolError("sync_change_integrity_invalid")
            changes: list[WorkspaceChange] = []
            for row in rows:
                change = self._validated_change(row, expected_previous)
                changes.append(change)
                expected_previous = change.change_digest
            next_cursor = cursor if not changes else changes[-1].sequence
            return SyncPage(
                requested_cursor=cursor,
                next_cursor=next_cursor,
                high_watermark=high_watermark,
                earliest_retained_sequence=retention_floor + 1,
                changes=tuple(changes),
                has_more=next_cursor < high_watermark,
            )

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()
