from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from corvus.database import DatabaseState, classify_database
from corvus.domain.access import AccessBundle, CapabilityEffect, CapabilityGrant
from corvus.domain.identity import (
    AgentIdentity,
    MembershipStatus,
    Principal,
    Workspace,
    WorkspaceMembership,
)
from corvus.domain.scope import (
    ChannelScope,
    ConversationScope,
    ProjectScope,
    ThreadScope,
    WorkspaceScope,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision

type Scope = WorkspaceScope | ProjectScope | ChannelScope | ThreadScope | ConversationScope

_ROLE_ACTION_CEILINGS: Mapping[str, frozenset[str]] = {
    "owner": frozenset({"workspace.manage", "project.create", "project.read"}),
    "admin": frozenset({"workspace.manage", "project.create", "project.read"}),
    "manager": frozenset({"project.create", "project.read"}),
    "member": frozenset({"project.create", "project.read"}),
    "viewer": frozenset({"project.read"}),
}


class IdentityScopeRepositoryError(RuntimeError):
    pass


def _digest(value: object) -> str:
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


def _scope_identity(scope: Scope) -> tuple[str, UUID]:
    if isinstance(scope, WorkspaceScope):
        return scope.kind, scope.workspace_id
    if isinstance(scope, ProjectScope):
        return scope.kind, scope.project_id
    if isinstance(scope, ChannelScope):
        return scope.kind, scope.channel_id
    if isinstance(scope, ThreadScope):
        return scope.kind, scope.thread_id
    return scope.kind, scope.conversation_id


def _scope_parent(scope: Scope) -> tuple[str | None, UUID | None]:
    if isinstance(scope, ConversationScope):
        return scope.parent_scope_kind, scope.parent_scope_id
    if isinstance(scope, ThreadScope):
        return "channel", scope.channel_id
    if isinstance(scope, ChannelScope) and scope.project_id is not None:
        return "project", scope.project_id
    if isinstance(scope, ThreadScope) and scope.project_id is not None:
        return "project", scope.project_id
    return None, None


def _parse_scope(payload: str) -> Scope:
    raw = json.loads(payload)
    kind = raw.get("kind")
    if kind == "workspace":
        return WorkspaceScope.model_validate(raw)
    if kind == "project":
        return ProjectScope.model_validate(raw)
    if kind == "channel":
        return ChannelScope.model_validate(raw)
    if kind == "thread":
        return ThreadScope.model_validate(raw)
    if kind == "conversation":
        return ConversationScope.model_validate(raw)
    raise IdentityScopeRepositoryError("scope_kind_unknown")


class IdentityScopeRepository:
    def __init__(self, database: Path | Engine) -> None:
        self._owns_engine = isinstance(database, Path)
        if isinstance(database, Path):
            revision = current_revision(database)
            if revision != M1_CURRENT_REVISION:
                raise IdentityScopeRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
            status = classify_database(database)
            if status.state is not DatabaseState.CURRENT:
                raise IdentityScopeRepositoryError(f"database_state_mismatch:{status.state.value}")
            self.engine = create_engine(f"sqlite:///{database}")
        else:
            self.engine = database
            with self.engine.connect() as connection:
                revision = MigrationContext.configure(connection).get_current_revision()
            if revision != M1_CURRENT_REVISION:
                raise IdentityScopeRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
        if self.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise IdentityScopeRepositoryError("unsupported_repository_dialect")

    @staticmethod
    def _enable_sqlite_foreign_keys(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self._enable_sqlite_foreign_keys(connection)
            yield connection

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            self._enable_sqlite_foreign_keys(connection)
            yield connection

    def append_workspace(self, workspace: Workspace) -> None:
        self._insert(
            "INSERT INTO identity_workspaces "
            "(id, version, name, workspace_kind, status, created_at, updated_at, payload_json) "
            "VALUES (:id, :version, :name, :workspace_kind, :status, :created_at, "
            ":updated_at, :payload_json)",
            {
                "id": str(workspace.id),
                "version": workspace.version,
                "name": workspace.name,
                "workspace_kind": workspace.workspace_kind.value,
                "status": workspace.status.value,
                "created_at": workspace.created_at.isoformat(),
                "updated_at": workspace.updated_at.isoformat(),
                "payload_json": workspace.model_dump_json(),
            },
            "workspace_identity_conflict",
        )

    def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        payload = self._one(
            "SELECT payload_json FROM identity_workspaces WHERE id = :id "
            "ORDER BY version DESC LIMIT 1",
            {"id": str(workspace_id)},
        )
        return None if payload is None else Workspace.model_validate_json(payload)

    def append_principal(self, principal: Principal) -> None:
        self._insert(
            "INSERT INTO principals "
            "(id, kind, external_provider, external_subject, created_at, payload_json) "
            "VALUES (:id, :kind, :external_provider, :external_subject, :created_at, "
            ":payload_json)",
            {
                "id": str(principal.id),
                "kind": principal.kind.value,
                "external_provider": principal.external_provider,
                "external_subject": principal.external_subject,
                "created_at": principal.created_at.isoformat(),
                "payload_json": principal.model_dump_json(),
            },
            "principal_identity_conflict",
        )

    def get_principal(self, principal_id: UUID) -> Principal | None:
        payload = self._one(
            "SELECT payload_json FROM principals WHERE id = :id",
            {"id": str(principal_id)},
        )
        return None if payload is None else Principal.model_validate_json(payload)

    def append_membership(self, membership: WorkspaceMembership) -> None:
        self._insert(
            "INSERT INTO workspace_memberships "
            "(workspace_id, principal_id, version, role, status, created_at, updated_at, "
            "payload_json) VALUES (:workspace_id, :principal_id, :version, :role, :status, "
            ":created_at, :updated_at, :payload_json)",
            {
                "workspace_id": str(membership.workspace_id),
                "principal_id": str(membership.principal_id),
                "version": membership.version,
                "role": membership.role,
                "status": membership.status.value,
                "created_at": membership.created_at.isoformat(),
                "updated_at": membership.updated_at.isoformat(),
                "payload_json": membership.model_dump_json(),
            },
            "membership_identity_conflict",
        )

    def get_membership(
        self,
        workspace_id: UUID,
        principal_id: UUID,
    ) -> WorkspaceMembership | None:
        payload = self._one(
            "SELECT payload_json FROM workspace_memberships "
            "WHERE workspace_id = :workspace_id AND principal_id = :principal_id "
            "ORDER BY version DESC LIMIT 1",
            {"workspace_id": str(workspace_id), "principal_id": str(principal_id)},
        )
        return None if payload is None else WorkspaceMembership.model_validate_json(payload)

    def get_membership_access(
        self,
        workspace_id: UUID,
        principal_id: UUID,
    ) -> tuple[tuple[AccessBundle, tuple[CapabilityGrant, ...]], ...]:
        membership = self.get_membership(workspace_id, principal_id)
        if membership is None or membership.status is not MembershipStatus.ACTIVE:
            return ()
        role_ceiling = _ROLE_ACTION_CEILINGS.get(membership.role.strip().casefold())
        if role_ceiling is None:
            raise IdentityScopeRepositoryError("membership_role_capability_mismatch")
        with self._connection() as connection:
            bundle_payloads = connection.scalars(
                text(
                    "SELECT payload_json FROM access_bundles "
                    "WHERE workspace_id = :workspace_id AND principal_id = :principal_id "
                    "AND scope_kind = 'workspace' AND scope_id = :workspace_id "
                    "ORDER BY created_at, id"
                ),
                {"workspace_id": str(workspace_id), "principal_id": str(principal_id)},
            ).all()
            result: list[tuple[AccessBundle, tuple[CapabilityGrant, ...]]] = []
            for bundle_payload in bundle_payloads:
                bundle = AccessBundle.model_validate_json(bundle_payload)
                grant_payloads = connection.scalars(
                    text(
                        "SELECT payload_json FROM capability_grants "
                        "WHERE workspace_id = :workspace_id AND bundle_id = :bundle_id "
                        "ORDER BY grant_digest"
                    ),
                    {"workspace_id": str(workspace_id), "bundle_id": str(bundle.id)},
                ).all()
                grants = tuple(
                    CapabilityGrant.model_validate_json(payload) for payload in grant_payloads
                )
                if any(
                    grant.effect is CapabilityEffect.ALLOW and grant.action not in role_ceiling
                    for grant in grants
                ):
                    raise IdentityScopeRepositoryError("membership_role_capability_mismatch")
                result.append((bundle, grants))
        return tuple(result)

    def append_agent(self, agent: AgentIdentity) -> None:
        self._insert(
            "INSERT INTO agent_identities "
            "(id, workspace_id, version, name, role, model_route, status, created_at, "
            "updated_at, payload_json) VALUES (:id, :workspace_id, :version, :name, :role, "
            ":model_route, :status, :created_at, :updated_at, :payload_json)",
            {
                "id": str(agent.id),
                "workspace_id": str(agent.workspace_id),
                "version": agent.version,
                "name": agent.name,
                "role": agent.role,
                "model_route": agent.model_route,
                "status": agent.status.value,
                "created_at": agent.created_at.isoformat(),
                "updated_at": agent.updated_at.isoformat(),
                "payload_json": agent.model_dump_json(),
            },
            "agent_identity_conflict",
        )

    def get_agent(self, workspace_id: UUID, agent_id: UUID) -> AgentIdentity | None:
        payload = self._one(
            "SELECT payload_json FROM agent_identities WHERE workspace_id = :workspace_id "
            "AND id = :id ORDER BY version DESC LIMIT 1",
            {"workspace_id": str(workspace_id), "id": str(agent_id)},
        )
        return None if payload is None else AgentIdentity.model_validate_json(payload)

    def append_scope(self, scope: Scope) -> None:
        kind, scope_id = _scope_identity(scope)
        parent_kind, parent_id = _scope_parent(scope)
        self._insert(
            "INSERT INTO scopes "
            "(workspace_id, kind, scope_id, parent_scope_kind, parent_scope_id, "
            "scope_digest, payload_json) VALUES (:workspace_id, :kind, :scope_id, "
            ":parent_scope_kind, :parent_scope_id, :scope_digest, :payload_json)",
            {
                "workspace_id": str(scope.workspace_id),
                "kind": kind,
                "scope_id": str(scope_id),
                "parent_scope_kind": parent_kind,
                "parent_scope_id": None if parent_id is None else str(parent_id),
                "scope_digest": _digest(scope),
                "payload_json": scope.model_dump_json(),
            },
            "scope_identity_conflict",
        )

    def get_scope(self, workspace_id: UUID, kind: str, scope_id: UUID) -> Scope | None:
        payload = self._one(
            "SELECT payload_json FROM scopes WHERE workspace_id = :workspace_id "
            "AND kind = :kind AND scope_id = :scope_id",
            {"workspace_id": str(workspace_id), "kind": kind, "scope_id": str(scope_id)},
        )
        return None if payload is None else _parse_scope(payload)

    def _insert(
        self,
        statement: str,
        values: Mapping[str, object],
        reason: str,
    ) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(text(statement), values)
        except IntegrityError as exc:
            raise IdentityScopeRepositoryError(reason) from exc

    def _one(self, statement: str, values: Mapping[str, object]) -> str | None:
        with self._connection() as connection:
            return cast(str | None, connection.scalar(text(statement), values))

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()
