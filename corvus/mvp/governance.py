from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import keyring
from pydantic import Field

from corvus.mvp.core import DomainConflict, DomainNotFound
from corvus.mvp.models import MvpModel
from corvus.mvp.store import SqliteStore


def _now_utc() -> datetime:
    return datetime.now(UTC)


type AutonomyMode = Literal["shadow", "supervised", "autonomous"]


@dataclass(frozen=True)
class SecretLease:
    credential_ref: str
    _value: str

    def reveal(self) -> str:
        """Return the secret only to the effect-boundary caller."""
        return self._value

    def __repr__(self) -> str:
        return f"SecretLease(credential_ref={self.credential_ref!r}, value=<redacted>)"


class LocalSecretBroker:
    def resolve(self, credential_ref: str) -> SecretLease:
        if credential_ref.startswith("env://"):
            name = credential_ref.removeprefix("env://")
            value = os.environ.get(name)
        elif credential_ref.startswith("keyring://"):
            locator = credential_ref.removeprefix("keyring://")
            if "/" not in locator:
                raise ValueError("keyring_reference_requires_service_and_account")
            service, account = locator.split("/", 1)
            value = keyring.get_password(service, account)
        else:
            raise ValueError("credential_reference_required")
        if not value:
            raise DomainNotFound("credential_value_unavailable")
        return SecretLease(credential_ref=credential_ref, _value=value)


class Team(MvpModel):
    id: str
    project_id: str
    name: str
    created_at: datetime


class TeamMember(MvpModel):
    team_id: str
    principal_id: str
    role: Literal["owner", "operator", "viewer"]
    created_at: datetime


class ProviderConnection(MvpModel):
    id: str
    project_id: str
    provider: str
    credential_ref: str
    status: str
    created_at: datetime


class CredentialGrant(MvpModel):
    id: str
    provider_connection_id: str
    principal_id: str
    capability: str
    credential_ref: str
    granted_by: str
    created_at: datetime


class OAuthFlow(MvpModel):
    state: str
    provider_connection_id: str
    redirect_uri: str
    code_verifier: str = Field(repr=False)
    status: str
    created_at: datetime


class DeviceFlow(MvpModel):
    device_code: str = Field(repr=False)
    user_code: str
    provider_connection_id: str
    status: Literal["pending", "approved", "connected", "expired"]
    expires_at: datetime
    polling_interval_seconds: int


class RestoreQuarantineRecord(MvpModel):
    id: str
    project_id: str
    source_digest: str
    status: Literal["quarantined", "reviewed_import_candidate"]
    reason: str
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None


class AutonomyDecision(MvpModel):
    id: str
    project_id: str
    principal_id: str
    capability: str
    mode: AutonomyMode
    requested_execution: bool
    executed: bool
    created_at: datetime


class AutonomyPolicy(MvpModel):
    project_id: str
    principal_id: str
    capability: str
    mode: AutonomyMode
    evidence_count: int
    updated_at: datetime


class MemoryEntry(MvpModel):
    id: str
    project_id: str
    scope: str
    version: int
    content: str
    provenance: str
    status: str
    created_at: datetime


class RetrievedMemory(MvpModel):
    entry_id: str
    trusted: bool
    context: str
    provenance: str


class SkillVersion(MvpModel):
    id: str
    project_id: str
    name: str
    version: int
    content: str
    status: str
    created_at: datetime


class Routine(MvpModel):
    id: str
    project_id: str
    name: str
    skill_version_id: str
    created_at: datetime


class RoutineRun(MvpModel):
    id: str
    routine_id: str
    skill_version_id: str
    actor_id: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None


class GovernanceService:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    @classmethod
    def open(cls, database: Path) -> GovernanceService:
        return cls(SqliteStore(database))

    def create_team(self, *, project_id: str, name: str, owner_id: str) -> Team:
        now = _now_utc()
        team = Team(id=str(uuid4()), project_id=project_id, name=name.strip(), created_at=now)
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            connection.execute(
                "INSERT INTO mvp_teams(id, project_id, name, created_at) VALUES (?, ?, ?, ?)",
                (team.id, project_id, team.name, now.isoformat()),
            )
            connection.execute(
                "INSERT INTO mvp_team_members(team_id, principal_id, role, created_at) "
                "VALUES (?, ?, 'owner', ?)",
                (team.id, owner_id, now.isoformat()),
            )
        return team

    def list_teams(self, project_id: str) -> tuple[Team, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_teams WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(
            Team(
                id=row["id"],
                project_id=row["project_id"],
                name=row["name"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def get_team(self, team_id: str) -> Team:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM mvp_teams WHERE id = ?", (team_id,)).fetchone()
        if row is None:
            raise DomainNotFound("team_not_found")
        return Team(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_team_members(self, team_id: str) -> tuple[TeamMember, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_team_members WHERE team_id = ? ORDER BY created_at",
                (team_id,),
            ).fetchall()
        if not rows:
            raise DomainNotFound("team_not_found")
        return tuple(
            TeamMember(
                team_id=row["team_id"],
                principal_id=row["principal_id"],
                role=row["role"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def add_member(
        self,
        team_id: str,
        *,
        actor_id: str,
        principal_id: str,
        role: Literal["owner", "operator", "viewer"],
    ) -> None:
        now = _now_utc()
        with self.store.transaction() as connection:
            actor = connection.execute(
                "SELECT role FROM mvp_team_members WHERE team_id = ? AND principal_id = ?",
                (team_id, actor_id),
            ).fetchone()
            if actor is None or actor["role"] != "owner":
                raise DomainConflict("team_owner_required")
            connection.execute(
                "INSERT INTO mvp_team_members(team_id, principal_id, role, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(team_id, principal_id) DO UPDATE SET "
                "role = excluded.role",
                (team_id, principal_id, role, now.isoformat()),
            )

    def create_provider_connection(
        self,
        *,
        project_id: str,
        provider: str,
        credential_ref: str,
    ) -> ProviderConnection:
        if not self._valid_credential_ref(credential_ref):
            raise ValueError("credential_reference_required")
        now = _now_utc()
        record = ProviderConnection(
            id=str(uuid4()),
            project_id=project_id,
            provider=provider,
            credential_ref=credential_ref,
            status="configured",
            created_at=now,
        )
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            connection.execute(
                "INSERT INTO mvp_provider_connections(id, project_id, provider, credential_ref, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    project_id,
                    provider,
                    credential_ref,
                    record.status,
                    now.isoformat(),
                ),
            )
        return record

    def list_provider_connections(self, project_id: str) -> tuple[ProviderConnection, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_provider_connections WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(self._provider(row) for row in rows)

    def grant_provider_capability(
        self,
        *,
        provider_connection_id: str,
        actor_id: str,
        principal_id: str,
        capability: str,
    ) -> CredentialGrant:
        now = _now_utc()
        with self.store.transaction() as connection:
            provider = connection.execute(
                "SELECT * FROM mvp_provider_connections WHERE id = ?",
                (provider_connection_id,),
            ).fetchone()
            if provider is None:
                raise DomainNotFound("provider_connection_not_found")
            self._require_project_owner(connection, provider["project_id"], actor_id)
            grant = CredentialGrant(
                id=str(uuid4()),
                provider_connection_id=provider_connection_id,
                principal_id=principal_id,
                capability=capability,
                credential_ref=provider["credential_ref"],
                granted_by=actor_id,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_credential_grants(id, provider_connection_id, principal_id, "
                "capability, credential_ref, granted_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.id,
                    provider_connection_id,
                    principal_id,
                    capability,
                    grant.credential_ref,
                    actor_id,
                    now.isoformat(),
                ),
            )
            return grant

    def begin_oauth(self, provider_connection_id: str, *, redirect_uri: str) -> OAuthFlow:
        now = _now_utc()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            self._require_provider(connection, provider_connection_id)
            connection.execute(
                "INSERT INTO mvp_oauth_flows(state, provider_connection_id, redirect_uri, "
                "verifier_digest, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (state, provider_connection_id, redirect_uri, digest, now.isoformat()),
            )
        return OAuthFlow(
            state=state,
            provider_connection_id=provider_connection_id,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            status="pending",
            created_at=now,
        )

    def complete_oauth(
        self,
        state: str,
        *,
        authorization_code: str,
        code_verifier: str,
    ) -> ProviderConnection:
        if not authorization_code:
            raise DomainConflict("simulated_oauth_code_invalid")
        now = _now_utc()
        digest = hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            flow = connection.execute(
                "SELECT * FROM mvp_oauth_flows WHERE state = ?", (state,)
            ).fetchone()
            if flow is None:
                raise DomainNotFound("oauth_state_not_found")
            if flow["status"] != "pending" or not secrets.compare_digest(
                digest, flow["verifier_digest"]
            ):
                raise DomainConflict("oauth_pkce_verification_failed")
            connection.execute(
                "UPDATE mvp_oauth_flows SET status = 'completed', completed_at = ? WHERE state = ?",
                (now.isoformat(), state),
            )
            connection.execute(
                "UPDATE mvp_provider_connections SET status = 'connected' WHERE id = ?",
                (flow["provider_connection_id"],),
            )
            provider = self._require_provider(connection, flow["provider_connection_id"])
            return self._provider(provider)

    def begin_device_flow(self, provider_connection_id: str) -> DeviceFlow:
        now = _now_utc()
        expires_at = now + timedelta(minutes=10)
        device_code = secrets.token_urlsafe(32)
        user_code = secrets.token_hex(4).upper()
        with self.store.transaction() as connection:
            self._require_provider(connection, provider_connection_id)
            connection.execute(
                "INSERT INTO mvp_device_flows(device_code, user_code, provider_connection_id, "
                "status, expires_at, polling_interval_seconds, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, 5, ?)",
                (
                    device_code,
                    user_code,
                    provider_connection_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return DeviceFlow(
            device_code=device_code,
            user_code=user_code,
            provider_connection_id=provider_connection_id,
            status="pending",
            expires_at=expires_at,
            polling_interval_seconds=5,
        )

    def approve_device_flow(self, user_code: str, *, actor_id: str) -> DeviceFlow:
        now = _now_utc()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT f.*, p.project_id FROM mvp_device_flows f "
                "JOIN mvp_provider_connections p ON p.id = f.provider_connection_id "
                "WHERE f.user_code = ?",
                (user_code,),
            ).fetchone()
            if row is None:
                raise DomainNotFound("device_flow_not_found")
            self._require_project_owner(connection, row["project_id"], actor_id)
            if datetime.fromisoformat(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE mvp_device_flows SET status = 'expired' WHERE device_code = ?",
                    (row["device_code"],),
                )
                raise DomainConflict("device_flow_expired")
            if row["status"] not in {"pending", "approved"}:
                raise DomainConflict("device_flow_not_pending")
            connection.execute(
                "UPDATE mvp_device_flows SET status = 'approved', approved_by = ? "
                "WHERE device_code = ?",
                (actor_id, row["device_code"]),
            )
            values = dict(row)
            values["status"] = "approved"
            return self._device_flow(values)

    def poll_device_flow(self, device_code: str) -> DeviceFlow:
        now = _now_utc()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_device_flows WHERE device_code = ?", (device_code,)
            ).fetchone()
            if row is None:
                raise DomainNotFound("device_flow_not_found")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE mvp_device_flows SET status = 'expired' WHERE device_code = ?",
                    (device_code,),
                )
                values = dict(row)
                values["status"] = "expired"
                return self._device_flow(values)
            if row["status"] == "approved":
                connection.execute(
                    "UPDATE mvp_device_flows SET status = 'connected', completed_at = ? "
                    "WHERE device_code = ?",
                    (now.isoformat(), device_code),
                )
                connection.execute(
                    "UPDATE mvp_provider_connections SET status = 'connected' WHERE id = ?",
                    (row["provider_connection_id"],),
                )
                values = dict(row)
                values["status"] = "connected"
                return self._device_flow(values)
            return self._device_flow(row)

    def evaluate_autonomy(
        self,
        *,
        project_id: str,
        principal_id: str,
        capability: str,
        requested_execution: bool,
    ) -> AutonomyDecision:
        now = _now_utc()
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT mode FROM mvp_autonomy_policies WHERE project_id = ? AND "
                "principal_id = ? AND capability = ?",
                (project_id, principal_id, capability),
            ).fetchone()
            mode = cast(AutonomyMode, row["mode"] if row else "shadow")
            executed = bool(requested_execution and mode == "autonomous")
            decision = AutonomyDecision(
                id=str(uuid4()),
                project_id=project_id,
                principal_id=principal_id,
                capability=capability,
                mode=mode,
                requested_execution=requested_execution,
                executed=executed,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_autonomy_decisions(id, project_id, principal_id, capability, "
                "mode, requested_execution, executed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.id,
                    project_id,
                    principal_id,
                    capability,
                    mode,
                    int(requested_execution),
                    int(executed),
                    now.isoformat(),
                ),
            )
            return decision

    def record_autonomy_evidence(self, decision_id: str, *, successful: bool) -> None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM mvp_autonomy_decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if row is None:
                raise DomainNotFound("autonomy_decision_not_found")
            connection.execute(
                "INSERT INTO mvp_autonomy_evidence(id, decision_id, successful, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(uuid4()), decision_id, int(successful), _now_utc().isoformat()),
            )

    def promote_autonomy(
        self,
        *,
        project_id: str,
        principal_id: str,
        capability: str,
        minimum_successes: int,
    ) -> AutonomyPolicy:
        now = _now_utc()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM mvp_autonomy_evidence e "
                "JOIN mvp_autonomy_decisions d ON d.id = e.decision_id WHERE "
                "d.project_id = ? AND d.principal_id = ? AND d.capability = ? "
                "AND e.successful = 1",
                (project_id, principal_id, capability),
            ).fetchone()
            successes = int(row["count"] if row else 0)
            if successes < minimum_successes:
                raise DomainConflict("insufficient_autonomy_evidence")
            mode: AutonomyMode = "supervised"
            connection.execute(
                "INSERT INTO mvp_autonomy_policies(project_id, principal_id, capability, mode, "
                "evidence_count, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT("
                "project_id, principal_id, capability) DO UPDATE SET mode = excluded.mode, "
                "evidence_count = excluded.evidence_count, updated_at = excluded.updated_at",
                (project_id, principal_id, capability, mode, successes, now.isoformat()),
            )
            return AutonomyPolicy(
                project_id=project_id,
                principal_id=principal_id,
                capability=capability,
                mode=mode,
                evidence_count=successes,
                updated_at=now,
            )

    def store_memory(
        self,
        *,
        project_id: str,
        scope: str,
        content: str,
        provenance: str,
    ) -> MemoryEntry:
        now = _now_utc()
        entry = MemoryEntry(
            id=str(uuid4()),
            project_id=project_id,
            scope=scope,
            version=1,
            content=content,
            provenance=provenance,
            status="active",
            created_at=now,
        )
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            connection.execute(
                "INSERT INTO mvp_memory_entries(id, project_id, scope, version, content, "
                "provenance, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    project_id,
                    scope,
                    entry.version,
                    content,
                    provenance,
                    entry.status,
                    now.isoformat(),
                ),
            )
        return entry

    def retrieve_memory(self, *, project_id: str, query: str) -> tuple[RetrievedMemory, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_memory_entries WHERE project_id = ? AND status = 'active' "
                "AND lower(content) LIKE ? ORDER BY created_at DESC",
                (project_id, f"%{query.lower()}%"),
            ).fetchall()
            return tuple(
                RetrievedMemory(
                    entry_id=row["id"],
                    trusted=False,
                    context=f"[UNTRUSTED MEMORY DATA]\n{row['content']}",
                    provenance=row["provenance"],
                )
                for row in rows
            )

    def list_memory_entries(self, project_id: str) -> tuple[MemoryEntry, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_memory_entries WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(
            MemoryEntry(
                id=row["id"],
                project_id=row["project_id"],
                scope=row["scope"],
                version=int(row["version"]),
                content=row["content"],
                provenance=row["provenance"],
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def create_skill(self, *, project_id: str, name: str, content: str) -> SkillVersion:
        now = _now_utc()
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM mvp_skill_versions "
                "WHERE project_id = ? AND name = ?",
                (project_id, name),
            ).fetchone()
            skill = SkillVersion(
                id=str(uuid4()),
                project_id=project_id,
                name=name,
                version=int(row["version"]),
                content=content,
                status="draft",
                created_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_skill_versions(id, project_id, name, version, content, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    skill.id,
                    project_id,
                    name,
                    skill.version,
                    content,
                    skill.status,
                    now.isoformat(),
                ),
            )
            return skill

    def activate_skill(self, skill_version_id: str) -> SkillVersion:
        with self.store.transaction() as connection:
            row = self._require_skill(connection, skill_version_id)
            connection.execute(
                "UPDATE mvp_skill_versions SET status = 'active' WHERE id = ?",
                (skill_version_id,),
            )
            values = dict(row)
            values["status"] = "active"
            return self._skill(values)

    def get_skill(self, skill_version_id: str) -> SkillVersion:
        with self.store.connect() as connection:
            return self._skill(self._require_skill(connection, skill_version_id))

    def list_skills(self, project_id: str) -> tuple[SkillVersion, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_skill_versions WHERE project_id = ? ORDER BY name, version",
                (project_id,),
            ).fetchall()
        return tuple(self._skill(row) for row in rows)

    def create_routine(
        self,
        *,
        project_id: str,
        name: str,
        skill_version_id: str,
    ) -> Routine:
        now = _now_utc()
        with self.store.transaction() as connection:
            skill = self._require_skill(connection, skill_version_id)
            if skill["project_id"] != project_id or skill["status"] != "active":
                raise DomainConflict("routine_requires_active_project_skill")
            routine = Routine(
                id=str(uuid4()),
                project_id=project_id,
                name=name,
                skill_version_id=skill_version_id,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_routines(id, project_id, name, skill_version_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (routine.id, project_id, name, skill_version_id, now.isoformat()),
            )
            return routine

    def run_routine(self, routine_id: str, *, actor_id: str) -> RoutineRun:
        now = _now_utc()
        with self.store.transaction() as connection:
            routine = connection.execute(
                "SELECT * FROM mvp_routines WHERE id = ?", (routine_id,)
            ).fetchone()
            if routine is None:
                raise DomainNotFound("routine_not_found")
            self._require_project_member(connection, routine["project_id"], actor_id)
            skill = self._require_skill(connection, routine["skill_version_id"])
            if skill["status"] != "active":
                raise DomainConflict("routine_skill_inactive")
            run = RoutineRun(
                id=str(uuid4()),
                routine_id=routine_id,
                skill_version_id=skill["id"],
                actor_id=actor_id,
                status="succeeded",
                created_at=now,
                finished_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_routine_runs(id, routine_id, skill_version_id, actor_id, status, "
                "created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    routine_id,
                    run.skill_version_id,
                    actor_id,
                    run.status,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return run

    def get_routine(self, routine_id: str) -> Routine:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_routines WHERE id = ?", (routine_id,)
            ).fetchone()
        if row is None:
            raise DomainNotFound("routine_not_found")
        return self._routine(row)

    def list_routines(self, project_id: str) -> tuple[Routine, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_routines WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(self._routine(row) for row in rows)

    def quarantine_restore(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> RestoreQuarantineRecord:
        now = _now_utc()
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            existing = connection.execute(
                "SELECT * FROM mvp_restore_quarantine WHERE source_digest = ?", (digest,)
            ).fetchone()
            if existing is not None:
                return self._restore(existing)
            record_id = str(uuid4())
            connection.execute(
                "INSERT INTO mvp_restore_quarantine(id, project_id, source_digest, payload_json, "
                "status, reason, created_at) VALUES (?, ?, ?, ?, 'quarantined', ?, ?)",
                (
                    record_id,
                    project_id,
                    digest,
                    payload_json,
                    "restore_cannot_replace_authority",
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM mvp_restore_quarantine WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - insert invariant
                raise DomainNotFound("restore_quarantine_not_found")
            return self._restore(row)

    def promote_quarantined_restore(
        self,
        restore_id: str,
        *,
        actor_id: str,
    ) -> RestoreQuarantineRecord:
        now = _now_utc()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_restore_quarantine WHERE id = ?", (restore_id,)
            ).fetchone()
            if row is None:
                raise DomainNotFound("restore_quarantine_not_found")
            self._require_project_owner(connection, row["project_id"], actor_id)
            connection.execute(
                "UPDATE mvp_restore_quarantine SET status = 'reviewed_import_candidate', "
                "reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                (now.isoformat(), actor_id, restore_id),
            )
            values = dict(row)
            values.update(
                status="reviewed_import_candidate",
                reviewed_at=now.isoformat(),
                reviewed_by=actor_id,
            )
            return self._restore(values)

    @staticmethod
    def _valid_credential_ref(value: str) -> bool:
        return value.startswith("env://") or value.startswith("keyring://")

    @staticmethod
    def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
        row = connection.execute(
            "SELECT id FROM mvp_projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("project_not_found")

    @staticmethod
    def _require_provider(connection: sqlite3.Connection, provider_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mvp_provider_connections WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("provider_connection_not_found")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_skill(connection: sqlite3.Connection, skill_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mvp_skill_versions WHERE id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("skill_version_not_found")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_project_owner(
        connection: sqlite3.Connection, project_id: str, actor_id: str
    ) -> None:
        row = connection.execute(
            "SELECT m.role FROM mvp_team_members m JOIN mvp_teams t ON t.id = m.team_id "
            "WHERE t.project_id = ? AND m.principal_id = ? AND m.role = 'owner' LIMIT 1",
            (project_id, actor_id),
        ).fetchone()
        if row is None:
            raise DomainConflict("team_owner_required")

    @staticmethod
    def _require_project_member(
        connection: sqlite3.Connection, project_id: str, actor_id: str
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM mvp_team_members m JOIN mvp_teams t ON t.id = m.team_id "
            "WHERE t.project_id = ? AND m.principal_id = ? LIMIT 1",
            (project_id, actor_id),
        ).fetchone()
        if row is None:
            raise DomainConflict("team_membership_required")

    @staticmethod
    def _provider(row: sqlite3.Row) -> ProviderConnection:
        return ProviderConnection(
            id=row["id"],
            project_id=row["project_id"],
            provider=row["provider"],
            credential_ref=row["credential_ref"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _skill(row: sqlite3.Row | dict[str, Any]) -> SkillVersion:
        return SkillVersion(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            version=int(row["version"]),
            content=row["content"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _routine(row: sqlite3.Row | dict[str, Any]) -> Routine:
        return Routine(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            skill_version_id=row["skill_version_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _device_flow(row: sqlite3.Row | dict[str, Any]) -> DeviceFlow:
        return DeviceFlow(
            device_code=row["device_code"],
            user_code=row["user_code"],
            provider_connection_id=row["provider_connection_id"],
            status=row["status"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            polling_interval_seconds=int(row["polling_interval_seconds"]),
        )

    @staticmethod
    def _restore(row: sqlite3.Row | dict[str, Any]) -> RestoreQuarantineRecord:
        return RestoreQuarantineRecord(
            id=row["id"],
            project_id=row["project_id"],
            source_digest=row["source_digest"],
            status=row["status"],
            reason=row["reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None,
            reviewed_by=row["reviewed_by"],
        )
