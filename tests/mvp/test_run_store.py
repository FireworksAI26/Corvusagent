from __future__ import annotations

from pathlib import Path

import pytest

from corvus.mvp.run_models import RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore, RunStoreConflict, RunStoreNotFound
from corvus.mvp.store import SqliteStore


def _repository(store: SqliteStore, tenant_id: str = "tenant-a") -> str:
    # RunStore only requires an existing tenant-scoped repository row; Git behavior is tested elsewhere.
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO mvp_repositories "
            "(id, tenant_id, canonical_path, display_name, remote_slug, default_branch, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 'main', ?, ?)",
            (
                "11111111-1111-4111-8111-111111111111",
                tenant_id,
                "C:/test/repository",
                "Repository",
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T00:00:00+00:00",
            ),
        )
    return "11111111-1111-4111-8111-111111111111"


def _request(repository_id: str) -> StartRunRequest:
    return StartRunRequest(
        repository_id=repository_id,
        task="Implement the durable run store",
        provider="codex",
        model="gpt-5.6-codex",
        effort="high",
        mode="build",
        safety_digest="a" * 64,
        output_policy="prepare_contribution",
    )


def test_persists_runs_events_and_evidence_across_restart(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)

    created = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)
    running = runs.transition("tenant-a", created.id, RunStatus.RUNNING)
    first = runs.append_event(created.id, "provider.started", {"message": "Started"})
    second = runs.append_event(created.id, "provider.output", {"message": "Working"})
    evidence = runs.add_evidence(created.id, "test", "12 tests passed", "c" * 64)

    restarted = RunStore(SqliteStore(tmp_path / "corvus.sqlite3"))
    loaded = restarted.get("tenant-a", created.id)
    assert loaded.status == RunStatus.RUNNING
    assert running.started_at is not None
    assert [event.sequence for event in restarted.events("tenant-a", created.id)] == [1, 2]
    assert first.sequence == 1 and second.sequence == 2
    assert restarted.evidence("tenant-a", created.id) == (evidence,)


def test_event_pages_are_bounded_and_resume_after_the_last_sequence(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    created = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)
    for index in range(12):
        runs.append_event(created.id, "provider.output", {"index": index})

    first_page = runs.events("tenant-a", created.id, limit=5)
    second_page = runs.events("tenant-a", created.id, after=first_page[-1].sequence, limit=5)

    assert [event.sequence for event in first_page] == [1, 2, 3, 4, 5]
    assert [event.sequence for event in second_page] == [6, 7, 8, 9, 10]


def test_run_and_evidence_pages_are_bounded(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    first = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)
    second = runs.create("tenant-a", _request(repository_id), base_sha="c" * 40)
    expected_run_ids = {first.id, second.id}
    for index in range(3):
        runs.add_evidence(first.id, "test", f"Evidence {index}", f"{index + 1}" * 64)

    first_run_page = runs.list("tenant-a", limit=1, offset=0)
    second_run_page = runs.list("tenant-a", limit=1, offset=1)
    first_evidence_page = runs.evidence("tenant-a", first.id, limit=2, offset=0)
    second_evidence_page = runs.evidence("tenant-a", first.id, limit=2, offset=2)

    assert {first_run_page[0].id, second_run_page[0].id} == expected_run_ids
    assert len(first_evidence_page) == 2
    assert len(second_evidence_page) == 1


def test_evidence_pages_use_durable_id_for_equal_timestamp_ties(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    created = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)
    created_at = "2026-07-21T00:00:00+00:00"
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO mvp_run_evidence "
            "(id, run_id, kind, summary, digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ("evidence-b", created.id, "test", "Evidence B", "b" * 64, created_at),
                ("evidence-a", created.id, "test", "Evidence A", "a" * 64, created_at),
            ),
        )

    first_page = runs.evidence("tenant-a", created.id, limit=1, offset=0)
    second_page = runs.evidence("tenant-a", created.id, limit=1, offset=1)

    assert [evidence.id for evidence in (*first_page, *second_page)] == [
        "evidence-a",
        "evidence-b",
    ]


@pytest.mark.parametrize(
    ("limit", "offset"),
    ((0, 0), (1_001, 0), (-1, 0), (1, -1)),
)
def test_run_and_evidence_pages_reject_invalid_bounds(
    tmp_path: Path,
    limit: int,
    offset: int,
) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    created = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)

    with pytest.raises(RunStoreConflict, match="^run_list_page_invalid$"):
        runs.list("tenant-a", limit=limit, offset=offset)
    with pytest.raises(RunStoreConflict, match="^run_evidence_page_invalid$"):
        runs.evidence("tenant-a", created.id, limit=limit, offset=offset)


def test_accepts_sha256_base_object_id(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)

    created = RunStore(store).create("tenant-a", _request(repository_id), base_sha="c" * 64)

    assert created.base_sha == "c" * 64


@pytest.mark.parametrize(
    "base_sha",
    (
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "g" * 64,
    ),
)
def test_rejects_noncanonical_base_object_id(tmp_path: Path, base_sha: str) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)

    with pytest.raises(RunStoreConflict, match="^run_base_sha_invalid$"):
        RunStore(store).create("tenant-a", _request(repository_id), base_sha=base_sha)


def test_rejects_invalid_transitions_and_keeps_terminal_state(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    created = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)

    with pytest.raises(RunStoreConflict, match="run_transition_invalid"):
        runs.transition("tenant-a", created.id, RunStatus.PUBLISHED)

    runs.transition("tenant-a", created.id, RunStatus.RUNNING)
    failed = runs.transition("tenant-a", created.id, RunStatus.FAILED)
    assert failed.finished_at is not None
    with pytest.raises(RunStoreConflict, match="run_transition_invalid"):
        runs.transition("tenant-a", created.id, RunStatus.RUNNING)


def test_retry_links_a_new_run_without_mutating_original(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    original = runs.create("tenant-a", _request(repository_id), base_sha="b" * 40)
    runs.transition("tenant-a", original.id, RunStatus.FAILED)

    retry = runs.retry("tenant-a", original.id)

    assert retry.id != original.id
    assert retry.retry_of_run_id == original.id
    assert retry.status == RunStatus.PREPARING
    assert runs.get("tenant-a", original.id).status == RunStatus.FAILED


def test_occurrence_is_unique_and_tenant_scope_is_enforced(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repository_id = _repository(store)
    runs = RunStore(store)
    request = _request(repository_id).model_copy(
        update={"schedule_id": "schedule-1", "occurrence_key": "2026-07-18T09:00:00Z"}
    )
    created = runs.create("tenant-a", request, base_sha="b" * 40)

    with pytest.raises(RunStoreConflict, match="run_occurrence_exists"):
        runs.create("tenant-a", request, base_sha="b" * 40)
    with pytest.raises(RunStoreNotFound, match="run_not_found"):
        runs.get("tenant-b", created.id)
    assert runs.list("tenant-b") == ()
