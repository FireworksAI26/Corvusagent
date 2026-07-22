from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from corvus.mvp.models import MvpModel
from corvus.mvp.store import SqliteStore


class ScheduleError(RuntimeError):
    pass


_CLAIM_LEASE = timedelta(minutes=5)


class Recurrence(MvpModel):
    kind: Literal["once", "hourly", "daily", "weekdays", "weekly"]
    local_time: time | None = None
    weekdays: tuple[int, ...] = Field(default=(), max_length=7)
    once_at: datetime | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Recurrence:
        if self.kind == "once":
            if self.once_at is None or self.once_at.tzinfo is None:
                raise ValueError("schedule_once_requires_aware_instant")
        elif self.kind != "hourly" and self.local_time is None:
            raise ValueError("schedule_local_time_required")
        if self.kind == "weekly" and not self.weekdays:
            raise ValueError("schedule_weekly_days_required")
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("schedule_weekday_invalid")
        return self

    def next_after(self, moment: datetime, timezone: str) -> datetime | None:
        if moment.tzinfo is None:
            raise ScheduleError("schedule_moment_timezone_required")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ScheduleError("schedule_timezone_invalid") from exc
        instant = moment.astimezone(UTC)
        if self.kind == "once":
            assert self.once_at is not None
            target = self.once_at.astimezone(UTC)
            return target if target > instant else None
        if self.kind == "hourly":
            local = instant.astimezone(zone)
            target = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return target.astimezone(UTC)
        local_now = instant.astimezone(zone)
        assert self.local_time is not None
        allowed = (
            {0, 1, 2, 3, 4}
            if self.kind == "weekdays"
            else set(self.weekdays)
            if self.kind == "weekly"
            else set(range(7))
        )
        for offset in range(0, 15):
            date = local_now.date() + timedelta(days=offset)
            if date.weekday() not in allowed:
                continue
            naive = datetime.combine(date, self.local_time)
            target = _resolve_local(naive, zone)
            if target.astimezone(UTC) > instant:
                return target.astimezone(UTC)
        raise ScheduleError("schedule_next_occurrence_unavailable")


class ScheduleCreateRequest(MvpModel):
    name: str = Field(min_length=1, max_length=200)
    repository_id: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=262_144)
    recurrence: Recurrence
    timezone: str = Field(min_length=1, max_length=100)
    provider: Literal["codex"] = "codex"
    model: str | None = Field(default=None, min_length=1, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh"] = "high"
    mode: Literal["chat", "build"] = "build"
    safety_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_version_id: str | None = None
    output_policy: Literal["report_only", "prepare_changes", "prepare_contribution"] = (
        "prepare_changes"
    )


class ScheduleRecord(MvpModel):
    id: str
    tenant_id: str
    name: str
    status: Literal["active", "paused", "archived"]
    revision_id: str
    version: int
    repository_id: str
    task: str
    recurrence: Recurrence
    timezone: str
    provider: Literal["codex"]
    model: str | None
    effort: Literal["low", "medium", "high", "xhigh"]
    mode: Literal["chat", "build"]
    safety_digest: str
    skill_version_id: str | None
    output_policy: Literal["report_only", "prepare_changes", "prepare_contribution"]
    next_run_at: datetime | None
    last_run_status: Literal["started", "skipped"] | None = None
    last_run_reason: str | None = None
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ScheduleClaim(MvpModel):
    schedule: ScheduleRecord
    scheduled_for: datetime


class ScheduleStore:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def create(
        self, tenant_id: str, request: ScheduleCreateRequest, *, now: datetime | None = None
    ) -> ScheduleRecord:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        next_run = request.recurrence.next_after(
            current - timedelta(microseconds=1), request.timezone
        )
        schedule_id = str(uuid4())
        revision_id = str(uuid4())
        with self.store.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM mvp_repositories WHERE tenant_id = ? AND id = ?",
                    (tenant_id, request.repository_id),
                ).fetchone()
                is None
            ):
                raise ScheduleError("repository_not_found")
            if (
                request.skill_version_id is not None
                and connection.execute(
                    "SELECT 1 FROM mvp_portable_skill_versions WHERE tenant_id = ? AND id = ? AND status = 'active'",
                    (tenant_id, request.skill_version_id),
                ).fetchone()
                is None
            ):
                raise ScheduleError("schedule_active_skill_required")
            connection.execute(
                "INSERT INTO mvp_schedules(id, tenant_id, name, status, current_revision, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 1, ?, ?)",
                (schedule_id, tenant_id, request.name, current.isoformat(), current.isoformat()),
            )
            connection.execute(
                "INSERT INTO mvp_schedule_revisions "
                "(id, schedule_id, version, repository_id, task, recurrence_json, timezone, provider, "
                "model, effort, mode, safety_digest, skill_version_id, output_policy, next_run_at, created_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    schedule_id,
                    request.repository_id,
                    request.task,
                    request.recurrence.model_dump_json(),
                    request.timezone,
                    request.provider,
                    request.model,
                    request.effort,
                    request.mode,
                    request.safety_digest,
                    request.skill_version_id,
                    request.output_policy,
                    next_run.isoformat() if next_run else None,
                    current.isoformat(),
                ),
            )
        return self.get(tenant_id, schedule_id)

    def get(self, tenant_id: str, schedule_id: str) -> ScheduleRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT s.*, r.id AS revision_id, r.version, r.repository_id, r.task, "
                "r.recurrence_json, r.timezone, r.provider, r.model, r.effort, r.mode, "
                "r.safety_digest, r.skill_version_id, r.output_policy, r.next_run_at, "
                "(SELECT occurrence.status FROM mvp_schedule_occurrences occurrence "
                "WHERE occurrence.schedule_revision_id = r.id "
                "AND occurrence.status IN ('started', 'skipped') "
                "ORDER BY occurrence.scheduled_for DESC LIMIT 1) AS last_run_status, "
                "(SELECT occurrence.reason_code FROM mvp_schedule_occurrences occurrence "
                "WHERE occurrence.schedule_revision_id = r.id "
                "AND occurrence.status IN ('started', 'skipped') "
                "ORDER BY occurrence.scheduled_for DESC LIMIT 1) AS last_run_reason, "
                "(SELECT occurrence.scheduled_for FROM mvp_schedule_occurrences occurrence "
                "WHERE occurrence.schedule_revision_id = r.id "
                "AND occurrence.status IN ('started', 'skipped') "
                "ORDER BY occurrence.scheduled_for DESC LIMIT 1) AS last_run_at "
                "FROM mvp_schedules s JOIN mvp_schedule_revisions r "
                "ON r.schedule_id = s.id AND r.version = s.current_revision "
                "WHERE s.tenant_id = ? AND s.id = ?",
                (tenant_id, schedule_id),
            ).fetchone()
        if row is None:
            raise ScheduleError("schedule_not_found")
        return self._record(row)

    def list(
        self, tenant_id: str, *, limit: int = 1000, offset: int = 0
    ) -> tuple[ScheduleRecord, ...]:
        with self.store.connect() as connection:
            ids = connection.execute(
                "SELECT id FROM mvp_schedules WHERE tenant_id = ? "
                "ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (tenant_id, limit, offset),
            ).fetchall()
        return tuple(self.get(tenant_id, str(row["id"])) for row in ids)

    def set_status(
        self, tenant_id: str, schedule_id: str, status: Literal["active", "paused", "archived"]
    ) -> ScheduleRecord:
        with self.store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE mvp_schedules SET status = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
                (status, datetime.now(UTC).isoformat(), tenant_id, schedule_id),
            )
            if cursor.rowcount != 1:
                raise ScheduleError("schedule_not_found")
        return self.get(tenant_id, schedule_id)

    def claim_due(self, now: datetime, *, limit: int = 10) -> tuple[ScheduleClaim, ...]:
        current = now.astimezone(UTC)
        claims: list[ScheduleClaim] = []
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT s.tenant_id, s.id, r.id AS revision_id, r.next_run_at, r.recurrence_json, r.timezone "
                "FROM mvp_schedules s JOIN mvp_schedule_revisions r "
                "ON r.schedule_id = s.id AND r.version = s.current_revision "
                "WHERE s.status = 'active' AND r.next_run_at IS NOT NULL AND r.next_run_at <= ? "
                "AND NOT EXISTS (SELECT 1 FROM mvp_runs active_run "
                "WHERE active_run.schedule_id = s.id AND active_run.status IN "
                "('preparing', 'running', 'review_required', 'contribution_ready', 'publishing')) "
                "ORDER BY r.next_run_at LIMIT ?",
                (current.isoformat(), limit),
            ).fetchall()
            for row in rows:
                scheduled_for = datetime.fromisoformat(str(row["next_run_at"]))
                try:
                    connection.execute(
                        "INSERT INTO mvp_schedule_occurrences "
                        "(schedule_revision_id, scheduled_for, run_id, status, claimed_at) "
                        "VALUES (?, ?, NULL, 'claimed', ?)",
                        (row["revision_id"], scheduled_for.isoformat(), current.isoformat()),
                    )
                except sqlite3.IntegrityError:
                    occurrence = connection.execute(
                        "SELECT run_id, status, claimed_at FROM mvp_schedule_occurrences "
                        "WHERE schedule_revision_id = ? AND scheduled_for = ?",
                        (row["revision_id"], scheduled_for.isoformat()),
                    ).fetchone()
                    if (
                        occurrence is None
                        or occurrence["run_id"] is not None
                        or str(occurrence["status"]) != "claimed"
                        or datetime.fromisoformat(str(occurrence["claimed_at"]))
                        > current - _CLAIM_LEASE
                    ):
                        continue
                    connection.execute(
                        "UPDATE mvp_schedule_occurrences SET claimed_at = ? "
                        "WHERE schedule_revision_id = ? AND scheduled_for = ?",
                        (current.isoformat(), row["revision_id"], scheduled_for.isoformat()),
                    )
                claims.append(
                    ScheduleClaim(
                        schedule=self.get(str(row["tenant_id"]), str(row["id"])),
                        scheduled_for=scheduled_for,
                    )
                )
        return tuple(claims)

    def attach_run(
        self,
        claim: ScheduleClaim,
        run_id: str | None,
        status: str = "started",
        reason_code: str | None = None,
    ) -> None:
        normalized_reason = reason_code[:100] if reason_code else None
        with self.store.transaction() as connection:
            occurrence = connection.execute(
                "SELECT claimed_at FROM mvp_schedule_occurrences "
                "WHERE schedule_revision_id = ? AND scheduled_for = ? "
                "AND run_id IS NULL AND status = 'claimed'",
                (claim.schedule.revision_id, claim.scheduled_for.isoformat()),
            ).fetchone()
            if occurrence is None:
                return
            cursor = connection.execute(
                "UPDATE mvp_schedule_occurrences SET run_id = ?, status = ?, reason_code = ? "
                "WHERE schedule_revision_id = ? AND scheduled_for = ? "
                "AND run_id IS NULL AND status = 'claimed'",
                (
                    run_id,
                    status,
                    normalized_reason,
                    claim.schedule.revision_id,
                    claim.scheduled_for.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return
            next_run = claim.schedule.recurrence.next_after(
                datetime.fromisoformat(str(occurrence["claimed_at"])),
                claim.schedule.timezone,
            )
            connection.execute(
                "UPDATE mvp_schedule_revisions SET next_run_at = ? WHERE id = ?",
                (next_run.isoformat() if next_run else None, claim.schedule.revision_id),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ScheduleRecord:
        return ScheduleRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            name=str(row["name"]),
            status=cast(Any, str(row["status"])),
            revision_id=str(row["revision_id"]),
            version=int(row["version"]),
            repository_id=str(row["repository_id"]),
            task=str(row["task"]),
            recurrence=Recurrence.model_validate_json(str(row["recurrence_json"])),
            timezone=str(row["timezone"]),
            provider="codex",
            model=str(row["model"]) if row["model"] is not None else None,
            effort=cast(Any, str(row["effort"])),
            mode=cast(Any, str(row["mode"])),
            safety_digest=str(row["safety_digest"]),
            skill_version_id=str(row["skill_version_id"])
            if row["skill_version_id"] is not None
            else None,
            output_policy=cast(Any, str(row["output_policy"])),
            next_run_at=datetime.fromisoformat(str(row["next_run_at"]))
            if row["next_run_at"]
            else None,
            last_run_status=cast(Any, str(row["last_run_status"]))
            if row["last_run_status"]
            else None,
            last_run_reason=str(row["last_run_reason"]) if row["last_run_reason"] else None,
            last_run_at=datetime.fromisoformat(str(row["last_run_at"]))
            if row["last_run_at"]
            else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )


def _resolve_local(naive: datetime, zone: ZoneInfo) -> datetime:
    candidate = naive.replace(tzinfo=zone, fold=0)
    if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
        return candidate
    for offset in range(1, 181):
        adjusted = naive + timedelta(minutes=offset)
        candidate = adjusted.replace(tzinfo=zone, fold=0)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == adjusted:
            return candidate
    raise ScheduleError("schedule_dst_resolution_failed")
