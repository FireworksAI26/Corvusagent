from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from corvus.mvp.run_models import (
    RunEvent,
    RunEvidence,
    RunRecord,
    RunStatus,
    StartRunRequest,
)
from corvus.mvp.store import SqliteStore


class RunStoreConflict(RuntimeError):
    pass


class RunStoreNotFound(RuntimeError):
    pass


_TERMINAL = {
    RunStatus.PUBLISHED,
    RunStatus.COMPLETED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.FAILED,
    RunStatus.DISCARDED,
}
_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.REVIEW_REQUIRED,
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.REVIEW_REQUIRED: frozenset(
        {RunStatus.CONTRIBUTION_READY, RunStatus.COMPLETED, RunStatus.DISCARDED, RunStatus.FAILED}
    ),
    RunStatus.CONTRIBUTION_READY: frozenset({RunStatus.PUBLISHING, RunStatus.DISCARDED}),
    RunStatus.PUBLISHING: frozenset(
        {RunStatus.PUBLISHED, RunStatus.CONTRIBUTION_READY, RunStatus.FAILED}
    ),
    RunStatus.COMPLETED: frozenset({RunStatus.DISCARDED}),
    RunStatus.CANCELLED: frozenset({RunStatus.DISCARDED}),
    RunStatus.INTERRUPTED: frozenset({RunStatus.DISCARDED}),
    RunStatus.FAILED: frozenset({RunStatus.DISCARDED}),
    RunStatus.PUBLISHED: frozenset(),
    RunStatus.DISCARDED: frozenset(),
}


class RunStore:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def create(
        self,
        tenant_id: str,
        request: StartRunRequest,
        *,
        base_sha: str,
        run_id: str | None = None,
        retry_of_run_id: str | None = None,
    ) -> RunRecord:
        if len(base_sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in base_sha
        ):
            raise RunStoreConflict("run_base_sha_invalid")
        with self.store.connect() as connection:
            repository = connection.execute(
                "SELECT 1 FROM mvp_repositories WHERE id = ? AND tenant_id = ?",
                (request.repository_id, tenant_id),
            ).fetchone()
            if retry_of_run_id is not None:
                retry_source = connection.execute(
                    "SELECT 1 FROM mvp_runs WHERE id = ? AND tenant_id = ?",
                    (retry_of_run_id, tenant_id),
                ).fetchone()
                if retry_source is None:
                    raise RunStoreNotFound("run_not_found")
        if repository is None:
            raise RunStoreNotFound("repository_not_found")
        identifier = run_id or str(uuid4())
        now = datetime.now(UTC)
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO mvp_runs "
                    "(id, tenant_id, repository_id, base_sha, task, provider, model, effort, "
                    "mode, safety_digest, skill_version_id, schedule_id, occurrence_key, "
                    "output_policy, retry_of_run_id, status, created_at, updated_at, started_at, "
                    "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, NULL, NULL)",
                    (
                        identifier,
                        tenant_id,
                        request.repository_id,
                        base_sha,
                        request.task,
                        request.provider,
                        request.model,
                        request.effort,
                        request.mode,
                        request.safety_digest,
                        request.skill_version_id,
                        request.schedule_id,
                        request.occurrence_key,
                        request.output_policy,
                        retry_of_run_id,
                        RunStatus.PREPARING.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if request.schedule_id is not None:
                raise RunStoreConflict("run_occurrence_exists") from exc
            raise RunStoreConflict("run_already_exists") from exc
        return self.get(tenant_id, identifier)

    def get(self, tenant_id: str, run_id: str) -> RunRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_runs WHERE tenant_id = ? AND id = ?",
                (tenant_id, run_id),
            ).fetchone()
        if row is None:
            raise RunStoreNotFound("run_not_found")
        return self._record(row)

    def for_occurrence(self, tenant_id: str, schedule_id: str, occurrence_key: str) -> RunRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_runs WHERE tenant_id = ? AND schedule_id = ? "
                "AND occurrence_key = ?",
                (tenant_id, schedule_id, occurrence_key),
            ).fetchone()
        if row is None:
            raise RunStoreNotFound("run_occurrence_not_found")
        return self._record(row)

    def list(self, tenant_id: str, *, limit: int = 1000, offset: int = 0) -> tuple[RunRecord, ...]:
        if limit < 1 or limit > 1_000 or offset < 0:
            raise RunStoreConflict("run_list_page_invalid")
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_runs WHERE tenant_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def transition(self, tenant_id: str, run_id: str, target: RunStatus) -> RunRecord:
        current = self.get(tenant_id, run_id)
        if target not in _TRANSITIONS[current.status]:
            raise RunStoreConflict("run_transition_invalid")
        now = datetime.now(UTC)
        started_at = (
            now.isoformat() if target == RunStatus.RUNNING and current.started_at is None else None
        )
        finished_at = now.isoformat() if target in _TERMINAL else None
        with self.store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE mvp_runs SET status = ?, updated_at = ?, "
                "started_at = COALESCE(started_at, ?), finished_at = COALESCE(finished_at, ?) "
                "WHERE id = ? AND tenant_id = ? AND status = ?",
                (
                    target.value,
                    now.isoformat(),
                    started_at,
                    finished_at,
                    run_id,
                    tenant_id,
                    current.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RunStoreConflict("run_transition_conflict")
        return self.get(tenant_id, run_id)

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> RunEvent:
        if not event_type or len(event_type) > 100:
            raise RunStoreConflict("run_event_type_invalid")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise RunStoreConflict("run_event_payload_too_large")
        created_at = datetime.now(UTC)
        with self.store.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM mvp_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if exists is None:
                raise RunStoreNotFound("run_not_found")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM mvp_run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                "INSERT INTO mvp_run_events "
                "(run_id, sequence, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, event_type, encoded, created_at.isoformat()),
            )
        return RunEvent(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            created_at=created_at,
        )

    def events(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 500,
    ) -> tuple[RunEvent, ...]:
        if limit < 1 or limit > 1_000:
            raise RunStoreConflict("run_event_limit_invalid")
        self.get(tenant_id, run_id)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_run_events WHERE run_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return tuple(
            RunEvent(
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                payload=cast(dict[str, Any], json.loads(str(row["payload_json"]))),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    def add_evidence(self, run_id: str, kind: str, summary: str, digest: str) -> RunEvidence:
        if not kind or not summary or len(summary) > 2_000:
            raise RunStoreConflict("run_evidence_invalid")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise RunStoreConflict("run_evidence_digest_invalid")
        evidence = RunEvidence(
            id=str(uuid4()),
            run_id=run_id,
            kind=kind,
            summary=summary,
            digest=digest,
            created_at=datetime.now(UTC),
        )
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO mvp_run_evidence "
                    "(id, run_id, kind, summary, digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        evidence.id,
                        evidence.run_id,
                        evidence.kind,
                        evidence.summary,
                        evidence.digest,
                        evidence.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RunStoreNotFound("run_not_found") from exc
        return evidence

    def evidence(
        self,
        tenant_id: str,
        run_id: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[RunEvidence, ...]:
        if limit < 1 or limit > 1_000 or offset < 0:
            raise RunStoreConflict("run_evidence_page_invalid")
        self.get(tenant_id, run_id)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_run_evidence WHERE run_id = ? "
                "ORDER BY created_at, id LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
        return tuple(
            RunEvidence(
                id=str(row["id"]),
                run_id=str(row["run_id"]),
                kind=str(row["kind"]),
                summary=str(row["summary"]),
                digest=str(row["digest"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    def retry(self, tenant_id: str, run_id: str) -> RunRecord:
        original = self.get(tenant_id, run_id)
        request = StartRunRequest(
            repository_id=original.repository_id,
            task=original.task,
            provider="codex",
            model=original.model,
            effort=original.effort,
            mode=original.mode,
            safety_digest=original.safety_digest,
            skill_version_id=original.skill_version_id,
            output_policy=original.output_policy,
        )
        return self.create(
            tenant_id,
            request,
            base_sha=original.base_sha,
            retry_of_run_id=original.id,
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            repository_id=str(row["repository_id"]),
            base_sha=str(row["base_sha"]),
            task=str(row["task"]),
            provider="codex",
            model=str(row["model"]) if row["model"] is not None else None,
            effort=cast(Any, str(row["effort"])),
            mode=cast(Any, str(row["mode"])),
            safety_digest=str(row["safety_digest"]),
            skill_version_id=(
                str(row["skill_version_id"]) if row["skill_version_id"] is not None else None
            ),
            schedule_id=str(row["schedule_id"]) if row["schedule_id"] is not None else None,
            occurrence_key=(
                str(row["occurrence_key"]) if row["occurrence_key"] is not None else None
            ),
            output_policy=cast(Any, str(row["output_policy"])),
            retry_of_run_id=(
                str(row["retry_of_run_id"]) if row["retry_of_run_id"] is not None else None
            ),
            status=RunStatus(str(row["status"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            started_at=(
                datetime.fromisoformat(str(row["started_at"]))
                if row["started_at"] is not None
                else None
            ),
            finished_at=(
                datetime.fromisoformat(str(row["finished_at"]))
                if row["finished_at"] is not None
                else None
            ),
        )
