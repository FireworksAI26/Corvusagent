from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from corvus.mvp.core import DomainNotFound
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.run_coordinator import RunCoordinatorConflict
from corvus.mvp.run_models import RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore, RunStoreConflict
from corvus.mvp.scheduler import LocalScheduler
from corvus.mvp.schedules import Recurrence, ScheduleCreateRequest, ScheduleStore
from corvus.mvp.store import SqliteStore
from tests.mvp.test_run_coordinator import _git, _run


def test_recurrence_handles_hourly_weekdays_and_once() -> None:
    friday = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)
    assert Recurrence(kind="hourly").next_after(friday, "UTC") == datetime(
        2026, 7, 17, 10, tzinfo=UTC
    )
    weekdays = Recurrence(kind="weekdays", local_time=time(9))
    assert weekdays.next_after(friday, "UTC") == datetime(2026, 7, 20, 9, tzinfo=UTC)
    once_at = datetime(2026, 7, 18, 12, tzinfo=UTC)
    assert Recurrence(kind="once", once_at=once_at).next_after(friday, "UTC") == once_at
    assert Recurrence(kind="once", once_at=once_at).next_after(once_at, "UTC") is None


def test_schedule_store_claims_each_occurrence_once(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    now = datetime(2026, 7, 18, 10, 15, tzinfo=UTC)
    schedule = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Hourly review",
            repository_id=repository.id,
            task="Review changes",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
        now=now,
    )
    assert schedule.next_run_at == datetime(2026, 7, 18, 11, tzinfo=UTC)

    first = schedules.claim_due(datetime(2026, 7, 18, 11, tzinfo=UTC))
    assert len(first) == 1
    assert first[0].scheduled_for == datetime(2026, 7, 18, 11, tzinfo=UTC)
    assert schedules.claim_due(datetime(2026, 7, 18, 11, tzinfo=UTC)) == ()
    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 11, tzinfo=UTC)
    schedules.attach_run(first[0], None, "skipped")
    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 12, tzinfo=UTC)


def test_schedule_reclaims_abandoned_occurrence_without_advancing_early(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    schedule = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Hourly recovery",
            repository_id=repository.id,
            task="Review changes",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
        now=datetime(2026, 7, 18, 10, 15, tzinfo=UTC),
    )

    original = schedules.claim_due(datetime(2026, 7, 18, 11, tzinfo=UTC))
    assert len(original) == 1
    assert schedules.claim_due(datetime(2026, 7, 18, 11, 4, tzinfo=UTC)) == ()

    recovered = schedules.claim_due(datetime(2026, 7, 18, 11, 5, tzinfo=UTC))
    assert len(recovered) == 1
    assert recovered[0].scheduled_for == original[0].scheduled_for
    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 11, tzinfo=UTC)
    schedules.attach_run(recovered[0], None, "skipped")
    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 12, tzinfo=UTC)


def test_schedule_claim_skips_missed_backlog_and_active_overlap(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    schedule = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Hourly review",
            repository_id=repository.id,
            task="Review changes",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
        now=datetime(2026, 7, 18, 10, 15, tzinfo=UTC),
    )

    claims = schedules.claim_due(datetime(2026, 7, 18, 15, 30, tzinfo=UTC))

    assert claims[0].scheduled_for == datetime(2026, 7, 18, 11, tzinfo=UTC)
    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 11, tzinfo=UTC)
    run = RunStore(store).create(
        "local",
        StartRunRequest(
            repository_id=repository.id,
            task="Review changes",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
            schedule_id=schedule.id,
            occurrence_key="active-occurrence",
        ),
        base_sha=repository.snapshot.head_sha,
    )
    schedules.attach_run(claims[0], run.id)

    assert schedules.get("local", schedule.id).next_run_at == datetime(2026, 7, 18, 16, tzinfo=UTC)
    assert schedules.claim_due(datetime(2026, 7, 18, 16, tzinfo=UTC)) == ()


@pytest.mark.asyncio
async def test_manual_schedule_run_has_lineage_and_rejects_active_overlap(
    tmp_path: Path,
) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    schedule = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Manual review",
            repository_id=repository.id,
            task="Review now",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
    )
    run_store = RunStore(store)

    class CreatingRuns:
        runs = run_store

        async def start(self, tenant_id: str, request: StartRunRequest):  # type: ignore[no-untyped-def]
            return run_store.create(
                tenant_id,
                request,
                base_sha=repository.snapshot.head_sha,
            )

    scheduler = LocalScheduler(schedules, CreatingRuns())  # type: ignore[arg-type]

    first = await scheduler.run_now("local", schedule.id)

    assert first.schedule_id == schedule.id
    assert first.occurrence_key is not None
    with pytest.raises(RunCoordinatorConflict, match="schedule_run_already_active"):
        await scheduler.run_now("local", schedule.id)


@pytest.mark.asyncio
async def test_scheduler_records_conflicted_occurrence_as_skipped_without_run_id(
    tmp_path: Path,
) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Hourly review",
            repository_id=repository.id,
            task="Review changes",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
        now=datetime(2026, 7, 18, 10, 15, tzinfo=UTC),
    )

    class ConflictingRuns:
        async def start(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RunCoordinatorConflict("repository_not_healthy")

    scheduler = LocalScheduler(schedules, ConflictingRuns())  # type: ignore[arg-type]

    assert await scheduler.tick(datetime(2026, 7, 18, 11, tzinfo=UTC)) == ()
    with store.connect() as connection:
        occurrence = connection.execute(
            "SELECT run_id, status, reason_code FROM mvp_schedule_occurrences"
        ).fetchone()
    assert occurrence is not None
    assert occurrence["run_id"] is None
    assert occurrence["status"] == "skipped"
    assert occurrence["reason_code"] == "repository_not_healthy"
    schedule = schedules.list("local")[0]
    assert schedule.last_run_status == "skipped"
    assert schedule.last_run_reason == "repository_not_healthy"
    assert schedule.last_run_at == datetime(2026, 7, 18, 11, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scheduler_recovers_run_created_before_occurrence_attachment(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    schedule = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Hourly review",
            repository_id=repository.id,
            task="Review changes",
            recurrence=Recurrence(kind="hourly"),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
        now=datetime(2026, 7, 18, 10, 15, tzinfo=UTC),
    )
    claim = schedules.claim_due(datetime(2026, 7, 18, 11, tzinfo=UTC))[0]
    occurrence_key = f"{schedule.revision_id}:{claim.scheduled_for.isoformat()}"
    run_store = RunStore(store)
    existing = run_store.create(
        "local",
        StartRunRequest(
            repository_id=repository.id,
            task=schedule.task,
            mode="chat",
            safety_digest=schedule.safety_digest,
            output_policy="report_only",
            schedule_id=schedule.id,
            occurrence_key=occurrence_key,
        ),
        base_sha=repository.snapshot.head_sha,
    )
    run_store.transition("local", existing.id, RunStatus.INTERRUPTED)

    class RecoveringRuns:
        runs = run_store

        async def start(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RunStoreConflict("run_occurrence_exists")

    scheduler = LocalScheduler(schedules, RecoveringRuns())  # type: ignore[arg-type]

    assert await scheduler.tick(datetime(2026, 7, 18, 11, 10, tzinfo=UTC)) == (existing.id,)
    with store.connect() as connection:
        occurrence = connection.execute(
            "SELECT run_id, status FROM mvp_schedule_occurrences"
        ).fetchone()
    assert occurrence is not None
    assert occurrence["run_id"] == existing.id
    assert occurrence["status"] == "started"


@pytest.mark.asyncio
async def test_scheduler_skips_stale_repository_and_continues_tick(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    due = datetime(2026, 7, 18, 11, tzinfo=UTC)
    for name in ("First", "Second"):
        schedules.create(
            "local",
            ScheduleCreateRequest(
                name=name,
                repository_id=repository.id,
                task="Review changes",
                recurrence=Recurrence(kind="hourly"),
                timezone="UTC",
                mode="chat",
                safety_digest="a" * 64,
                output_policy="report_only",
            ),
            now=datetime(2026, 7, 18, 10, 15, tzinfo=UTC),
        )

    class StaleThenHealthyRuns:
        def __init__(self) -> None:
            self.calls = 0

        async def start(self, tenant_id: str, request: StartRunRequest):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise DomainNotFound("repository_not_found")
            return RunStore(store).create(
                tenant_id,
                request,
                base_sha=repository.snapshot.head_sha,
            )

    runs = StaleThenHealthyRuns()
    scheduler = LocalScheduler(schedules, runs)  # type: ignore[arg-type]

    started = await scheduler.tick(due)

    assert len(started) == 1
    assert runs.calls == 2
    with store.connect() as connection:
        statuses = connection.execute(
            "SELECT status FROM mvp_schedule_occurrences ORDER BY status"
        ).fetchall()
    assert [row["status"] for row in statuses] == ["skipped", "started"]


def test_schedule_pause_resume_and_archive(tmp_path: Path) -> None:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    source.joinpath("README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository = RepositoryWorkspaceService(store, git).register_local("local", source, "Source")
    schedules = ScheduleStore(store)
    record = schedules.create(
        "local",
        ScheduleCreateRequest(
            name="Daily",
            repository_id=repository.id,
            task="Inspect",
            recurrence=Recurrence(kind="daily", local_time=time(9)),
            timezone="UTC",
            mode="chat",
            safety_digest="a" * 64,
            output_policy="report_only",
        ),
    )
    assert schedules.set_status("local", record.id, "paused").status == "paused"
    assert schedules.set_status("local", record.id, "active").status == "active"
    assert schedules.set_status("local", record.id, "archived").status == "archived"
