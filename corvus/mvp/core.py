from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4, uuid5

from corvus.mvp.models import (
    ApprovalRecord,
    ArtifactRecord,
    BudgetAccount,
    CheckpointRecord,
    ConversationEntry,
    EffectBinding,
    EffectRecord,
    OutcomeContract,
    Project,
    RecoveryResult,
    WorkClaim,
    Workflow,
    WorkflowStatus,
    WorkItem,
    WorkItemDefinition,
    WorkItemStatus,
)
from corvus.mvp.store import SqliteStore

_ID_NAMESPACE: Final = UUID("2f65f2df-f1d2-4ba5-8c6e-d84624aa5e67")
_DEFAULT_LEASE_DURATION: Final = timedelta(seconds=30)


class DomainConflict(RuntimeError):
    pass


class DomainNotFound(LookupError):
    pass


class BudgetExceeded(DomainConflict):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class CorvusService:
    def __init__(self, store: SqliteStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or _now_utc

    @classmethod
    def open(
        cls,
        database: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> CorvusService:
        return cls(SqliteStore(database), clock=clock)

    def create_project(self, *, name: str, tenant_id: str = "local") -> Project:
        if not name.strip():
            raise ValueError("project_name_required")
        project = Project(
            id=str(uuid4()), tenant_id=tenant_id, name=name.strip(), created_at=self.clock()
        )
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_projects(id, tenant_id, name, created_at) VALUES (?, ?, ?, ?)",
                (project.id, project.tenant_id, project.name, project.created_at.isoformat()),
            )
            connection.execute(
                "INSERT INTO mvp_budgets(project_id, limit_units, reserved_units, settled_units) "
                "VALUES (?, 0, 0, 0)",
                (project.id,),
            )
        return project

    def create_outcome(
        self,
        *,
        project_id: str,
        title: str,
        acceptance_criteria: Sequence[str],
    ) -> OutcomeContract:
        criteria = tuple(item.strip() for item in acceptance_criteria if item.strip())
        if not title.strip() or not criteria:
            raise ValueError("outcome_title_and_criteria_required")
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM mvp_outcomes "
                "WHERE project_id = ? AND title = ?",
                (project_id, title.strip()),
            ).fetchone()
            outcome = OutcomeContract(
                id=str(uuid4()),
                project_id=project_id,
                version=int(row["version"]),
                title=title.strip(),
                acceptance_criteria=criteria,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO mvp_outcomes(id, project_id, version, title, "
                "acceptance_criteria_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    outcome.id,
                    project_id,
                    outcome.version,
                    outcome.title,
                    _json(criteria),
                    now.isoformat(),
                ),
            )
        return outcome

    def list_outcomes(self, project_id: str) -> tuple[OutcomeContract, ...]:
        with self.store.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM mvp_outcomes WHERE project_id = ? "
                "ORDER BY created_at ASC, version ASC",
                (project_id,),
            ).fetchall()
        return tuple(self._outcome(row) for row in rows)

    def create_workflow(
        self,
        *,
        outcome_id: str,
        name: str,
        items: Sequence[WorkItemDefinition],
    ) -> Workflow:
        definitions = tuple(items)
        self._validate_graph(definitions)
        now = self.clock()
        workflow = Workflow(
            id=str(uuid4()),
            outcome_id=outcome_id,
            name=name.strip(),
            status=WorkflowStatus.DRAFT,
            created_at=now,
            updated_at=now,
        )
        with self.store.transaction() as connection:
            self._require_outcome(connection, outcome_id)
            connection.execute(
                "INSERT INTO mvp_workflows(id, outcome_id, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    workflow.id,
                    outcome_id,
                    workflow.name,
                    workflow.status.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for definition in definitions:
                connection.execute(
                    "INSERT INTO mvp_work_items(id, workflow_id, item_key, title, status, "
                    "depends_on_json, cost_units, requires_approval, effect_json, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        workflow.id,
                        definition.key,
                        definition.title,
                        WorkItemStatus.PENDING.value,
                        _json(definition.depends_on),
                        definition.cost_units,
                        int(definition.requires_approval),
                        _json(definition.effect.model_dump(mode="json"))
                        if definition.effect
                        else None,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            self._append_event(connection, workflow.id, "workflow.created", {"name": name})
        return workflow

    def start_workflow(self, workflow_id: str) -> Workflow:
        now = self.clock()
        with self.store.transaction() as connection:
            workflow = self._require_workflow(connection, workflow_id)
            if workflow.status not in {WorkflowStatus.DRAFT, WorkflowStatus.PAUSED}:
                raise DomainConflict(f"workflow_cannot_start:{workflow.status.value}")
            connection.execute(
                "UPDATE mvp_workflows SET status = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.RUNNING.value, now.isoformat(), workflow_id),
            )
            self._refresh_ready_items(connection, workflow_id, now)
            self._append_event(connection, workflow_id, "workflow.started", {})
        return self.get_workflow(workflow_id)

    def pause_workflow(self, workflow_id: str) -> Workflow:
        return self._set_workflow_status(workflow_id, WorkflowStatus.PAUSED)

    def cancel_workflow(self, workflow_id: str) -> Workflow:
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_workflow(connection, workflow_id)
            connection.execute(
                "UPDATE mvp_workflows SET status = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.CANCELLED.value, now.isoformat(), workflow_id),
            )
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE workflow_id = ? "
                "AND status NOT IN (?, ?)",
                (
                    WorkItemStatus.CANCELLED.value,
                    now.isoformat(),
                    workflow_id,
                    WorkItemStatus.SUCCEEDED.value,
                    WorkItemStatus.CANCELLED.value,
                ),
            )
            self._release_open_reservations(connection, workflow_id, now)
            self._append_event(connection, workflow_id, "workflow.cancelled", {})
        return self.get_workflow(workflow_id)

    def get_workflow(self, workflow_id: str) -> Workflow:
        with self.store.connect() as connection:
            return self._require_workflow(connection, workflow_id)

    def list_workflows(self, outcome_id: str) -> tuple[Workflow, ...]:
        with self.store.connect() as connection:
            self._require_outcome(connection, outcome_id)
            rows = connection.execute(
                "SELECT * FROM mvp_workflows WHERE outcome_id = ? ORDER BY created_at ASC",
                (outcome_id,),
            ).fetchall()
        return tuple(self._workflow(row) for row in rows)

    def get_work_item(self, workflow_id: str, key: str) -> WorkItem:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_work_items WHERE workflow_id = ? AND item_key = ?",
                (workflow_id, key),
            ).fetchone()
            if row is None:
                raise DomainNotFound("work_item_not_found")
            return self._work_item(row)

    def list_work_items(self, workflow_id: str) -> tuple[WorkItem, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_work_items WHERE workflow_id = ? ORDER BY item_key",
                (workflow_id,),
            ).fetchall()
        return self._topological_items(tuple(self._work_item(row) for row in rows))

    @staticmethod
    def _topological_items(items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
        remaining = {item.key: item for item in items}
        resolved: set[str] = set()
        ordered: list[WorkItem] = []
        while remaining:
            ready_keys = sorted(
                key for key, item in remaining.items() if set(item.depends_on).issubset(resolved)
            )
            if not ready_keys:
                raise DomainConflict("persisted_workflow_dependency_cycle")
            for key in ready_keys:
                ordered.append(remaining.pop(key))
                resolved.add(key)
        return tuple(ordered)

    def claim_next(
        self,
        workflow_id: str,
        *,
        worker_id: str,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> WorkClaim | None:
        now = self.clock()
        expires_at = now + lease_duration
        with self.store.transaction() as connection:
            workflow = self._require_workflow(connection, workflow_id)
            if workflow.status is not WorkflowStatus.RUNNING:
                return None
            if self._kill_switch_enabled(connection, workflow_id):
                raise DomainConflict("workflow_kill_switch_enabled")
            self._refresh_ready_items(connection, workflow_id, now)
            row = connection.execute(
                "SELECT * FROM mvp_work_items WHERE workflow_id = ? AND status = ? "
                "ORDER BY item_key LIMIT 1",
                (workflow_id, WorkItemStatus.READY.value),
            ).fetchone()
            if row is None:
                return None
            fence = int(row["lease_fence"]) + 1
            attempt_id = str(uuid4())
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, lease_owner = ?, lease_expires_at = ?, "
                "lease_fence = ?, attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?",
                (
                    WorkItemStatus.RUNNING.value,
                    worker_id,
                    expires_at.isoformat(),
                    fence,
                    now.isoformat(),
                    row["id"],
                ),
            )
            connection.execute(
                "INSERT INTO mvp_attempts(id, work_item_id, worker_id, lease_fence, status, "
                "started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (attempt_id, row["id"], worker_id, fence, now.isoformat()),
            )
            self._append_event(
                connection,
                workflow_id,
                "work_item.claimed",
                {"key": row["item_key"], "worker_id": worker_id, "lease_fence": fence},
            )
            return WorkClaim(
                attempt_id=attempt_id,
                work_item_id=row["id"],
                workflow_id=workflow_id,
                key=row["item_key"],
                worker_id=worker_id,
                lease_fence=fence,
                lease_expires_at=expires_at,
            )

    def complete_claim(
        self,
        claim: WorkClaim,
        *,
        result: dict[str, Any] | None = None,
    ) -> WorkItem:
        output = result or {"executor": "deterministic-local", "key": claim.key}
        now = self.clock()
        with self.store.transaction() as connection:
            row = self._require_claim(connection, claim)
            self._complete_claim(connection, row, claim, output, now)
        return self.get_work_item(claim.workflow_id, claim.key)

    def run_next(self, workflow_id: str, *, worker_id: str) -> WorkItem | None:
        claim = self.claim_next(workflow_id, worker_id=worker_id)
        if claim is None:
            return None
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_work_items WHERE id = ?", (claim.work_item_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - claim invariant
                raise DomainNotFound("work_item_not_found")
            item = self._work_item(row)
        if item.effect is None:
            return self.complete_claim(claim)
        effect = self._ensure_effect(item)
        if item.requires_approval and effect.approval_id is None:
            self._wait_for_approval(claim, effect)
            return self.get_work_item(workflow_id, item.key)
        effect_result = self._execute_effect_for_claim(effect.id)
        return self.complete_claim(claim, result=effect_result)

    def recover_workflow(self, workflow_id: str) -> RecoveryResult:
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_workflow(connection, workflow_id)
            rows = connection.execute(
                "SELECT * FROM mvp_work_items WHERE workflow_id = ? AND status = ? "
                "AND lease_expires_at < ?",
                (workflow_id, WorkItemStatus.RUNNING.value, now.isoformat()),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE mvp_work_items SET status = ?, lease_owner = NULL, "
                    "lease_expires_at = NULL, error = ?, updated_at = ? WHERE id = ?",
                    (
                        WorkItemStatus.READY.value,
                        "stale_lease_recovered",
                        now.isoformat(),
                        row["id"],
                    ),
                )
                connection.execute(
                    "UPDATE mvp_attempts SET status = 'interrupted', error = ?, finished_at = ? "
                    "WHERE work_item_id = ? AND status = 'running'",
                    ("stale_lease_recovered", now.isoformat(), row["id"]),
                )
                self._append_event(
                    connection,
                    workflow_id,
                    "work_item.recovered",
                    {"key": row["item_key"], "prior_lease_fence": row["lease_fence"]},
                )
            self._refresh_ready_items(connection, workflow_id, now)
        return RecoveryResult(workflow_id=workflow_id, recovered_items=len(rows))

    def set_budget(self, project_id: str, *, limit_units: int) -> BudgetAccount:
        if limit_units < 0:
            raise ValueError("budget_limit_must_be_non_negative")
        with self.store.transaction() as connection:
            current = self._budget(connection, project_id)
            if current.reserved_units + current.settled_units > limit_units:
                raise BudgetExceeded("budget_limit_below_committed_amount")
            connection.execute(
                "UPDATE mvp_budgets SET limit_units = ? WHERE project_id = ?",
                (limit_units, project_id),
            )
        return self.get_budget(project_id)

    def get_budget(self, project_id: str) -> BudgetAccount:
        with self.store.connect() as connection:
            return self._budget(connection, project_id)

    def approve_effect(self, effect_id: str, *, actor_id: str) -> ApprovalRecord:
        now = self.clock()
        with self.store.transaction() as connection:
            effect = self._require_effect(connection, effect_id)
            existing = connection.execute(
                "SELECT * FROM mvp_approvals WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if existing is not None:
                return self._approval(existing)
            approval_id = str(uuid4())
            connection.execute(
                "INSERT INTO mvp_approvals(id, effect_id, actor_id, status, created_at) "
                "VALUES (?, ?, ?, 'approved', ?)",
                (approval_id, effect_id, actor_id, now.isoformat()),
            )
            connection.execute(
                "UPDATE mvp_effects SET approval_id = ?, status = 'approved', updated_at = ? "
                "WHERE id = ?",
                (approval_id, now.isoformat(), effect_id),
            )
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, updated_at = ? WHERE id = ?",
                (WorkItemStatus.READY.value, now.isoformat(), effect.work_item_id),
            )
            self._append_event(
                connection,
                effect.workflow_id,
                "effect.approved",
                {"effect_id": effect_id, "actor_id": actor_id},
            )
            row = connection.execute(
                "SELECT * FROM mvp_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - insert invariant
                raise DomainConflict("approval_persistence_failed")
            return self._approval(row)

    def reject_effect(self, effect_id: str, *, actor_id: str) -> ApprovalRecord:
        now = self.clock()
        with self.store.transaction() as connection:
            effect = self._require_effect(connection, effect_id)
            existing = connection.execute(
                "SELECT * FROM mvp_approvals WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if existing is not None:
                if existing["status"] != "rejected":
                    raise DomainConflict("effect_approval_already_decided")
                return self._approval(existing)
            approval_id = str(uuid4())
            connection.execute(
                "INSERT INTO mvp_approvals(id, effect_id, actor_id, status, created_at) "
                "VALUES (?, ?, ?, 'rejected', ?)",
                (approval_id, effect_id, actor_id, now.isoformat()),
            )
            connection.execute(
                "UPDATE mvp_effects SET approval_id = ?, status = 'rejected', updated_at = ? "
                "WHERE id = ?",
                (approval_id, now.isoformat(), effect_id),
            )
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, updated_at = ? WHERE id = ?",
                (WorkItemStatus.FAILED.value, now.isoformat(), effect.work_item_id),
            )
            connection.execute(
                "UPDATE mvp_workflows SET status = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.FAILED.value, now.isoformat(), effect.workflow_id),
            )
            self._release_open_reservations(connection, effect.workflow_id, now)
            self._append_event(
                connection,
                effect.workflow_id,
                "effect.rejected",
                {"effect_id": effect_id, "actor_id": actor_id},
            )
            row = connection.execute(
                "SELECT * FROM mvp_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - insert invariant
                raise DomainConflict("approval_persistence_failed")
            return self._approval(row)

    def execute_effect(self, effect_id: str) -> EffectRecord:
        self._execute_effect_for_claim(effect_id)
        return self.get_effect(effect_id)

    def get_effect(self, effect_id: str) -> EffectRecord:
        with self.store.connect() as connection:
            return self._require_effect(connection, effect_id)

    def list_effects(self, workflow_id: str) -> tuple[EffectRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_effects WHERE workflow_id = ? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            return tuple(self._effect(row) for row in rows)

    def list_artifacts(self, workflow_id: str) -> tuple[ArtifactRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_artifacts WHERE workflow_id = ? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            return tuple(
                ArtifactRecord(
                    id=row["id"],
                    workflow_id=row["workflow_id"],
                    work_item_id=row["work_item_id"],
                    digest=row["digest"],
                    media_type=row["media_type"],
                    content=json.loads(row["content_json"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            )

    def list_checkpoints(self, workflow_id: str) -> tuple[CheckpointRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_checkpoints WHERE workflow_id = ? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            return tuple(
                CheckpointRecord(
                    id=row["id"],
                    workflow_id=row["workflow_id"],
                    work_item_id=row["work_item_id"],
                    state=row["state"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            )

    def list_conversation_entries(self, workflow_id: str) -> tuple[ConversationEntry, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_conversation_entries WHERE workflow_id = ? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            return tuple(
                ConversationEntry(
                    id=row["id"],
                    workflow_id=row["workflow_id"],
                    work_item_id=row["work_item_id"],
                    role=row["role"],
                    content=row["content"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            )

    def set_kill_switch(self, *, scope_kind: str, scope_id: str, enabled: bool) -> None:
        if scope_kind not in {"global", "project", "workflow"}:
            raise ValueError("invalid_kill_switch_scope")
        if scope_kind == "global" and scope_id != "global":
            raise ValueError("global_kill_switch_scope_id_invalid")
        now = self.clock()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_kill_switches(scope_kind, scope_id, enabled, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(scope_kind, scope_id) DO UPDATE SET "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (scope_kind, scope_id, int(enabled), now.isoformat()),
            )

    def heartbeat(
        self,
        claim: WorkClaim,
        *,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> WorkClaim:
        now = self.clock()
        expires_at = now + lease_duration
        with self.store.transaction() as connection:
            self._require_claim(connection, claim)
            connection.execute(
                "UPDATE mvp_work_items SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at.isoformat(), now.isoformat(), claim.work_item_id),
            )
            self._append_event(
                connection,
                claim.workflow_id,
                "work_item.lease_renewed",
                {"key": claim.key, "lease_fence": claim.lease_fence},
            )
        return claim.model_copy(update={"lease_expires_at": expires_at})

    def fail_claim(self, claim: WorkClaim, *, error: str) -> WorkItem:
        if not error.strip():
            raise ValueError("attempt_error_required")
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_claim(connection, claim)
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (
                    WorkItemStatus.FAILED.value,
                    error.strip(),
                    now.isoformat(),
                    claim.work_item_id,
                ),
            )
            connection.execute(
                "UPDATE mvp_attempts SET status = 'failed', error = ?, finished_at = ? "
                "WHERE id = ?",
                (error.strip(), now.isoformat(), claim.attempt_id),
            )
            self._append_event(
                connection,
                claim.workflow_id,
                "work_item.failed",
                {"key": claim.key, "error": error.strip()},
            )
        return self.get_work_item(claim.workflow_id, claim.key)

    def retry_work_item(self, workflow_id: str, key: str) -> WorkItem:
        now = self.clock()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_work_items WHERE workflow_id = ? AND item_key = ?",
                (workflow_id, key),
            ).fetchone()
            if row is None:
                raise DomainNotFound("work_item_not_found")
            if row["status"] != WorkItemStatus.FAILED.value:
                raise DomainConflict("work_item_not_failed")
            dependencies = set(json.loads(row["depends_on_json"]))
            succeeded = {
                item["item_key"]
                for item in connection.execute(
                    "SELECT item_key FROM mvp_work_items WHERE workflow_id = ? AND status = ?",
                    (workflow_id, WorkItemStatus.SUCCEEDED.value),
                ).fetchall()
            }
            status = (
                WorkItemStatus.READY if dependencies.issubset(succeeded) else WorkItemStatus.PENDING
            )
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, error = NULL, updated_at = ? WHERE id = ?",
                (status.value, now.isoformat(), row["id"]),
            )
            self._append_event(connection, workflow_id, "work_item.retried", {"key": key})
        return self.get_work_item(workflow_id, key)

    def list_attempts(self, workflow_id: str) -> tuple[dict[str, Any], ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT a.* FROM mvp_attempts a JOIN mvp_work_items i ON i.id = a.work_item_id "
                "WHERE i.workflow_id = ? ORDER BY a.started_at",
                (workflow_id,),
            ).fetchall()
            return tuple({key: row[key] for key in row.keys()} for row in rows)

    def list_events(self, workflow_id: str, *, after_id: int = 0) -> tuple[dict[str, Any], ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_events WHERE workflow_id = ? AND id > ? ORDER BY id",
                (workflow_id, after_id),
            ).fetchall()
            return tuple(
                {
                    "id": int(row["id"]),
                    "workflow_id": row["workflow_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            )

    def list_lineage(self, workflow_id: str) -> tuple[dict[str, Any], ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_lineage WHERE workflow_id = ? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
            return tuple({key: row[key] for key in row.keys()} for row in rows)

    def _ensure_effect(self, item: WorkItem) -> EffectRecord:
        if item.effect is None:  # pragma: no cover - caller invariant
            raise ValueError("effect_required")
        effect_id = str(uuid5(_ID_NAMESPACE, f"effect:{item.workflow_id}:{item.id}"))
        idempotency_key = hashlib.sha256(
            _json(
                {
                    "workflow_id": item.workflow_id,
                    "work_item_id": item.id,
                    "binding": item.effect.model_dump(mode="json"),
                }
            ).encode("utf-8")
        ).hexdigest()
        now = self.clock()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM mvp_effects WHERE work_item_id = ?", (item.id,)
            ).fetchone()
            if existing is not None:
                return self._effect(existing)
            connection.execute(
                "INSERT INTO mvp_effects(id, workflow_id, work_item_id, idempotency_key, "
                "binding_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    effect_id,
                    item.workflow_id,
                    item.id,
                    idempotency_key,
                    _json(item.effect.model_dump(mode="json")),
                    "pending_approval" if item.requires_approval else "ready",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._reserve_budget(connection, effect_id, item, now)
            self._append_event(
                connection,
                item.workflow_id,
                "effect.proposed",
                {"effect_id": effect_id, "idempotency_key": idempotency_key},
            )
            row = connection.execute(
                "SELECT * FROM mvp_effects WHERE id = ?", (effect_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - insert invariant
                raise DomainConflict("effect_persistence_failed")
            return self._effect(row)

    def _wait_for_approval(self, claim: WorkClaim, effect: EffectRecord) -> None:
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_claim(connection, claim)
            connection.execute(
                "UPDATE mvp_work_items SET status = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (WorkItemStatus.WAITING_APPROVAL.value, now.isoformat(), claim.work_item_id),
            )
            connection.execute(
                "UPDATE mvp_attempts SET status = 'waiting_approval', finished_at = ? WHERE id = ?",
                (now.isoformat(), claim.attempt_id),
            )
            self._append_event(
                connection,
                claim.workflow_id,
                "effect.approval_required",
                {"effect_id": effect.id, "key": claim.key},
            )

    def _execute_effect_for_claim(self, effect_id: str) -> dict[str, Any]:
        now = self.clock()
        with self.store.transaction() as connection:
            effect = self._require_effect(connection, effect_id)
            if effect.execution_count:
                raise DomainConflict("effect_already_executed")
            item_row = connection.execute(
                "SELECT requires_approval FROM mvp_work_items WHERE id = ?",
                (effect.work_item_id,),
            ).fetchone()
            if item_row is None:
                raise DomainNotFound("work_item_not_found")
            approval_row = connection.execute(
                "SELECT * FROM mvp_approvals WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if bool(item_row["requires_approval"]) and (
                approval_row is None or approval_row["status"] != "approved"
            ):
                raise DomainConflict("effect_approval_required")
            result = {
                "adapter": f"local-{effect.binding.kind}",
                "target": effect.binding.target,
                "payload_digest": hashlib.sha256(
                    _json(effect.binding.payload).encode("utf-8")
                ).hexdigest(),
            }
            cursor = connection.execute(
                "UPDATE mvp_effects SET status = 'executed', execution_count = 1, "
                "result_json = ?, updated_at = ? WHERE id = ? AND execution_count = 0",
                (_json(result), now.isoformat(), effect_id),
            )
            if cursor.rowcount != 1:
                raise DomainConflict("effect_already_executed")
            if approval_row is not None:
                connection.execute(
                    "UPDATE mvp_approvals SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                    (now.isoformat(), approval_row["id"]),
                )
            self._settle_budget(connection, effect_id, now)
            self._append_event(
                connection,
                effect.workflow_id,
                "effect.executed",
                {"effect_id": effect_id, "result": result},
            )
            return result

    def _complete_claim(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        claim: WorkClaim,
        output: dict[str, Any],
        now: datetime,
    ) -> None:
        output_json = _json(output)
        artifact_id = str(uuid4())
        checkpoint_id = str(uuid4())
        digest = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE mvp_work_items SET status = ?, result_json = ?, error = NULL, "
            "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (WorkItemStatus.SUCCEEDED.value, output_json, now.isoformat(), row["id"]),
        )
        connection.execute(
            "UPDATE mvp_attempts SET status = 'succeeded', result_json = ?, finished_at = ? "
            "WHERE id = ?",
            (output_json, now.isoformat(), claim.attempt_id),
        )
        connection.execute(
            "INSERT INTO mvp_checkpoints(id, workflow_id, work_item_id, state, created_at) "
            "VALUES (?, ?, ?, 'succeeded', ?)",
            (checkpoint_id, claim.workflow_id, row["id"], now.isoformat()),
        )
        connection.execute(
            "INSERT INTO mvp_artifacts(id, workflow_id, work_item_id, digest, media_type, "
            "content_json, created_at) VALUES (?, ?, ?, ?, 'application/json', ?, ?)",
            (artifact_id, claim.workflow_id, row["id"], digest, output_json, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO mvp_lineage(id, workflow_id, source_type, source_id, target_type, "
            "target_id, created_at) VALUES (?, ?, 'attempt', ?, 'artifact', ?, ?)",
            (
                str(uuid4()),
                claim.workflow_id,
                claim.attempt_id,
                artifact_id,
                now.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO mvp_conversation_entries(id, workflow_id, work_item_id, role, "
            "content, created_at) VALUES (?, ?, ?, 'assistant', ?, ?)",
            (
                str(uuid4()),
                claim.workflow_id,
                row["id"],
                f"Completed {row['item_key']}",
                now.isoformat(),
            ),
        )
        self._append_event(
            connection,
            claim.workflow_id,
            "work_item.succeeded",
            {"key": row["item_key"], "artifact_id": artifact_id},
        )
        self._refresh_ready_items(connection, claim.workflow_id, now)
        incomplete = connection.execute(
            "SELECT COUNT(*) AS count FROM mvp_work_items WHERE workflow_id = ? AND status != ?",
            (claim.workflow_id, WorkItemStatus.SUCCEEDED.value),
        ).fetchone()
        if incomplete is not None and int(incomplete["count"]) == 0:
            connection.execute(
                "UPDATE mvp_workflows SET status = ?, updated_at = ? WHERE id = ?",
                (WorkflowStatus.SUCCEEDED.value, now.isoformat(), claim.workflow_id),
            )
            self._append_event(connection, claim.workflow_id, "workflow.succeeded", {})

    def _reserve_budget(
        self,
        connection: sqlite3.Connection,
        effect_id: str,
        item: WorkItem,
        now: datetime,
    ) -> None:
        project_row = connection.execute(
            "SELECT o.project_id FROM mvp_workflows w JOIN mvp_outcomes o ON o.id = w.outcome_id "
            "WHERE w.id = ?",
            (item.workflow_id,),
        ).fetchone()
        if project_row is None:
            raise DomainNotFound("project_not_found")
        budget = self._budget(connection, project_row["project_id"])
        if item.cost_units > budget.available_units:
            raise BudgetExceeded("budget_reservation_exceeds_available")
        reservation_id = str(uuid5(_ID_NAMESPACE, f"budget:{effect_id}"))
        connection.execute(
            "INSERT INTO mvp_budget_reservations(id, effect_id, project_id, amount_units, "
            "status, created_at) VALUES (?, ?, ?, ?, 'reserved', ?)",
            (
                reservation_id,
                effect_id,
                project_row["project_id"],
                item.cost_units,
                now.isoformat(),
            ),
        )
        connection.execute(
            "UPDATE mvp_budgets SET reserved_units = reserved_units + ? WHERE project_id = ?",
            (item.cost_units, project_row["project_id"]),
        )

    def _settle_budget(self, connection: sqlite3.Connection, effect_id: str, now: datetime) -> None:
        row = connection.execute(
            "SELECT * FROM mvp_budget_reservations WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        if row is None or row["status"] == "settled":
            return
        connection.execute(
            "UPDATE mvp_budget_reservations SET status = 'settled', settled_at = ? WHERE id = ?",
            (now.isoformat(), row["id"]),
        )
        connection.execute(
            "UPDATE mvp_budgets SET reserved_units = reserved_units - ?, "
            "settled_units = settled_units + ? WHERE project_id = ?",
            (row["amount_units"], row["amount_units"], row["project_id"]),
        )

    def _release_open_reservations(
        self, connection: sqlite3.Connection, workflow_id: str, now: datetime
    ) -> None:
        rows = connection.execute(
            "SELECT r.* FROM mvp_budget_reservations r JOIN mvp_effects e ON e.id = r.effect_id "
            "WHERE e.workflow_id = ? AND r.status = 'reserved'",
            (workflow_id,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE mvp_budget_reservations SET status = 'released', settled_at = ? "
                "WHERE id = ?",
                (now.isoformat(), row["id"]),
            )
            connection.execute(
                "UPDATE mvp_budgets SET reserved_units = reserved_units - ? WHERE project_id = ?",
                (row["amount_units"], row["project_id"]),
            )

    def _refresh_ready_items(
        self, connection: sqlite3.Connection, workflow_id: str, now: datetime
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM mvp_work_items WHERE workflow_id = ? AND status = ?",
            (workflow_id, WorkItemStatus.PENDING.value),
        ).fetchall()
        succeeded = {
            row["item_key"]
            for row in connection.execute(
                "SELECT item_key FROM mvp_work_items WHERE workflow_id = ? AND status = ?",
                (workflow_id, WorkItemStatus.SUCCEEDED.value),
            ).fetchall()
        }
        for row in rows:
            dependencies = set(json.loads(row["depends_on_json"]))
            if dependencies.issubset(succeeded):
                connection.execute(
                    "UPDATE mvp_work_items SET status = ?, updated_at = ? WHERE id = ?",
                    (WorkItemStatus.READY.value, now.isoformat(), row["id"]),
                )

    def _require_claim(self, connection: sqlite3.Connection, claim: WorkClaim) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mvp_work_items WHERE id = ?", (claim.work_item_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("work_item_not_found")
        if int(row["lease_fence"]) != claim.lease_fence:
            raise DomainConflict("stale_lease_fence")
        if row["lease_owner"] != claim.worker_id or row["status"] != WorkItemStatus.RUNNING.value:
            raise DomainConflict("lease_not_owned")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _validate_graph(items: Sequence[WorkItemDefinition]) -> None:
        if not items:
            raise ValueError("workflow_requires_work_items")
        keys = [item.key for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_work_item_key")
        key_set = set(keys)
        for item in items:
            if not set(item.depends_on).issubset(key_set):
                raise ValueError("unknown_work_item_dependency")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {item.key: item.depends_on for item in items}

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("workflow_dependency_cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO mvp_events(workflow_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (workflow_id, event_type, _json(payload), _now_utc().isoformat()),
        )

    @staticmethod
    def _require_project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mvp_projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("project_not_found")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_outcome(connection: sqlite3.Connection, outcome_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM mvp_outcomes WHERE id = ?", (outcome_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("outcome_not_found")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _outcome(row: sqlite3.Row) -> OutcomeContract:
        return OutcomeContract(
            id=row["id"],
            project_id=row["project_id"],
            version=int(row["version"]),
            title=row["title"],
            acceptance_criteria=tuple(json.loads(row["acceptance_criteria_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _workflow(row: sqlite3.Row) -> Workflow:
        return Workflow(
            id=row["id"],
            outcome_id=row["outcome_id"],
            name=row["name"],
            status=WorkflowStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _require_workflow(self, connection: sqlite3.Connection, workflow_id: str) -> Workflow:
        row = connection.execute(
            "SELECT * FROM mvp_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("workflow_not_found")
        return self._workflow(row)

    def _require_effect(self, connection: sqlite3.Connection, effect_id: str) -> EffectRecord:
        row = connection.execute("SELECT * FROM mvp_effects WHERE id = ?", (effect_id,)).fetchone()
        if row is None:
            raise DomainNotFound("effect_not_found")
        return self._effect(row)

    @staticmethod
    def _work_item(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            id=row["id"],
            workflow_id=row["workflow_id"],
            key=row["item_key"],
            title=row["title"],
            status=WorkItemStatus(row["status"]),
            depends_on=tuple(json.loads(row["depends_on_json"])),
            cost_units=int(row["cost_units"]),
            requires_approval=bool(row["requires_approval"]),
            effect=EffectBinding.model_validate_json(row["effect_json"])
            if row["effect_json"]
            else None,
            lease_owner=row["lease_owner"],
            lease_expires_at=_parse_datetime(row["lease_expires_at"]),
            lease_fence=int(row["lease_fence"]),
            attempt_count=int(row["attempt_count"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )

    @staticmethod
    def _effect(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            work_item_id=row["work_item_id"],
            idempotency_key=row["idempotency_key"],
            binding=EffectBinding.model_validate_json(row["binding_json"]),
            status=row["status"],
            approval_id=row["approval_id"],
            execution_count=int(row["execution_count"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )

    @staticmethod
    def _approval(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            effect_id=row["effect_id"],
            actor_id=row["actor_id"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            consumed_at=_parse_datetime(row["consumed_at"]),
        )

    @staticmethod
    def _budget(connection: sqlite3.Connection, project_id: str) -> BudgetAccount:
        row = connection.execute(
            "SELECT * FROM mvp_budgets WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise DomainNotFound("budget_not_found")
        return BudgetAccount(
            project_id=project_id,
            limit_units=int(row["limit_units"]),
            reserved_units=int(row["reserved_units"]),
            settled_units=int(row["settled_units"]),
        )

    def _set_workflow_status(self, workflow_id: str, status: WorkflowStatus) -> Workflow:
        now = self.clock()
        with self.store.transaction() as connection:
            self._require_workflow(connection, workflow_id)
            connection.execute(
                "UPDATE mvp_workflows SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now.isoformat(), workflow_id),
            )
            self._append_event(connection, workflow_id, f"workflow.{status.value}", {})
        return self.get_workflow(workflow_id)

    @staticmethod
    def _kill_switch_enabled(connection: sqlite3.Connection, workflow_id: str) -> bool:
        row = connection.execute(
            "SELECT enabled FROM mvp_kill_switches WHERE "
            "(scope_kind = 'global' AND scope_id = 'global') OR "
            "(scope_kind = 'workflow' AND scope_id = ?) ORDER BY enabled DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        return bool(row["enabled"]) if row else False
