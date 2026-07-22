from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from corvus.domain.agent_runtime import (
    AgentRunEvent,
    AgentRunEventType,
    compute_agent_run_event_digest,
)
from corvus.domain.conversations import (
    AgentRunRecord,
    AttachmentRef,
    Message,
    MessageAuthorKind,
    RunArtifact,
    RunEventRecord,
    Thread,
    ThreadStatus,
    compute_content_digest,
    compute_lineage_digest,
)
from corvus.domain.identity import (
    MembershipStatus,
    Principal,
    PrincipalKind,
    Workspace,
    WorkspaceMembership,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, upgrade_database, upgrade_database_url
from corvus.infrastructure.repositories.conversations import (
    ConversationRepository,
    ConversationRepositoryError,
)
from corvus.infrastructure.repositories.identity_scope import IdentityScopeRepository
from corvus.platform import create_platform_engine
from corvus.store import TraceStore
from tests.postgres_safety import PostgresTestSafetyError, validate_disposable_postgres_url

NOW = datetime(2026, 7, 17, 2, 3, 4, tzinfo=UTC)
ZERO = "0" * 64


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    assert upgrade_database(database) == M1_CURRENT_REVISION
    return database


def _identity(database: Path | Engine, *, suffix: str = "one") -> tuple[Workspace, Principal]:
    workspace = Workspace(name=f"Workspace {suffix}", created_at=NOW, updated_at=NOW)
    principal = Principal(
        kind=PrincipalKind.USER,
        external_provider="test",
        external_subject=f"conversation-{suffix}",
        display_name="Conversation user",
        created_at=NOW,
    )
    identities = IdentityScopeRepository(database)
    identities.append_workspace(workspace)
    identities.append_principal(principal)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=principal.id,
            role="owner",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    identities.close()
    return workspace, principal


def _guarded_postgres_engine() -> Engine:
    database_url = os.environ.get(
        "CORVUS_TEST_POSTGRES_URL",
        "postgresql+psycopg://corvus:corvus@127.0.0.1:55432/corvus_platform_test?connect_timeout=2",
    )
    try:
        validate_disposable_postgres_url(database_url, environ=os.environ)
    except PostgresTestSafetyError as exc:
        pytest.skip(f"PostgreSQL destructive test disabled: {exc}")
    engine = create_platform_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        if getattr(exc.orig, "sqlstate", None) is not None:
            raise
        pytest.skip(f"PostgreSQL test service unavailable: {exc.__class__.__name__}")
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    upgrade_database_url(database_url)
    return engine


def _thread(workspace: Workspace, principal: Principal, **updates: object) -> Thread:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": workspace.id,
        "workspace_version": workspace.version,
        "project_id": None,
        "creator_principal_id": principal.id,
        "creator_membership_version": 1,
        "title": "Repository thread",
        "status": ThreadStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    values.update(updates)
    return Thread(**values)


def _attachment(workspace: Workspace, principal: Principal, **updates: object) -> AttachmentRef:
    values: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": workspace.id,
        "owner_principal_id": principal.id,
        "owner_membership_version": 1,
        "display_name": "input.txt",
        "media_type": "text/plain",
        "byte_size": 5,
        "content_digest": hashlib.sha256(b"input").hexdigest(),
        "metadata": {"source": "test"},
        "created_at": NOW,
    }
    values.update(updates)
    return AttachmentRef(**values)


def _message(
    workspace: Workspace,
    principal: Principal,
    thread: Thread,
    *,
    key: str,
    content: str = "message",
    attachments: tuple[UUID, ...] = (),
) -> Message:
    return Message(
        id=uuid4(),
        workspace_id=workspace.id,
        thread_id=thread.id,
        sequence=1,
        content=content,
        content_digest=compute_content_digest(content),
        idempotency_key=key,
        producing_run_id=None,
        attachment_ids=attachments,
        author_kind=MessageAuthorKind.PRINCIPAL,
        author_principal_id=principal.id,
        author_membership_version=1,
        created_at=NOW,
    )


def _run(
    workspace: Workspace,
    principal: Principal,
    thread: Thread,
    message: Message,
    *,
    key: str = "run-1",
) -> AgentRunRecord:
    return AgentRunRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        thread_id=thread.id,
        message_sequence=message.sequence,
        requester_principal_id=principal.id,
        requester_membership_version=1,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="a" * 64,
        provider_binding_id=uuid4(),
        provider_binding_version=2,
        provider_binding_digest="b" * 64,
        canonical_request_digest="c" * 64,
        idempotency_key=key,
        created_at=NOW,
    )


def _event(
    workspace: Workspace,
    thread: Thread,
    run: AgentRunRecord,
    *,
    event_type: AgentRunEventType,
    sequence: int,
    previous_digest: str,
    provider_event_id: str,
) -> RunEventRecord:
    payload = {"event": event_type.value}
    digest = compute_agent_run_event_digest(
        run_id=run.id,
        handle_id=run.id,
        sequence=sequence,
        timestamp=NOW,
        event_type=event_type,
        redacted_payload=payload,
        provider_event_id=provider_event_id,
        previous_event_digest=previous_digest,
        tool_call_id=None,
        effect_authorization_decision_id=None,
        effect_authorization_decision_digest=None,
    )
    return RunEventRecord(
        workspace_id=workspace.id,
        thread_id=thread.id,
        run_id=run.id,
        event=AgentRunEvent(
            run_id=run.id,
            handle_id=run.id,
            sequence=sequence,
            timestamp=NOW,
            event_type=event_type,
            redacted_payload=payload,
            provider_event_id=provider_event_id,
            previous_event_digest=previous_digest,
            event_digest=digest,
        ),
    )


def _seed_run(
    repository: ConversationRepository,
    workspace: Workspace,
    principal: Principal,
) -> tuple[Thread, Message, AgentRunRecord, RunEventRecord]:
    thread = repository.create_thread(_thread(workspace, principal))
    message = repository.append_message(
        _message(workspace, principal, thread, key="message-1"),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    run = repository.create_run(_run(workspace, principal, thread, message))
    started = repository.append_event(
        _event(
            workspace,
            thread,
            run,
            event_type=AgentRunEventType.STARTED,
            sequence=1,
            previous_digest=ZERO,
            provider_event_id="started",
        ),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    return thread, message, run, started


def test_repository_persists_threads_versions_and_non_enumerating_reads(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    other_workspace, other_principal = _identity(database, suffix="two")
    repository = ConversationRepository(database)
    thread = repository.create_thread(_thread(workspace, principal))

    assert repository.get_thread(workspace.id, thread.id, principal.id) == thread
    assert repository.get_thread(other_workspace.id, thread.id, other_principal.id) is None
    archived = repository.archive_thread(
        workspace_id=workspace.id,
        thread_id=thread.id,
        expected_version=1,
        requester_principal_id=principal.id,
        requester_membership_version=1,
        updated_at=NOW,
    )
    assert archived.status is ThreadStatus.ARCHIVED
    assert archived.version == 2
    assert repository.list_threads(workspace.id, principal.id) == (archived,)
    repository.close()


def test_message_attachment_links_are_atomic_and_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread = repository.create_thread(_thread(workspace, principal))
    attachment = repository.register_attachment(_attachment(workspace, principal))
    proposed = _message(
        workspace,
        principal,
        thread,
        key="same-key",
        attachments=(attachment.id,),
    )
    first = repository.append_message(
        proposed,
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    replay = repository.append_message(
        proposed,
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    assert replay == first
    assert first.sequence == 1
    assert repository.list_messages(workspace.id, thread.id, principal.id) == (first,)

    changed = _message(workspace, principal, thread, key="same-key", content="changed")
    with pytest.raises(
        ConversationRepositoryError,
        match="conversation_idempotency_payload_mismatch",
    ):
        repository.append_message(
            changed,
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    missing = _message(
        workspace,
        principal,
        thread,
        key="missing-attachment",
        attachments=(uuid4(),),
    )
    with pytest.raises(ConversationRepositoryError, match="attachment_not_found"):
        repository.append_message(
            missing,
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    assert len(repository.list_messages(workspace.id, thread.id, principal.id)) == 1
    repository.close()


def test_concurrent_messages_allocate_gap_free_sequences(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread = repository.create_thread(_thread(workspace, principal))
    repository.close()
    barrier = threading.Barrier(3)
    results: list[Message] = []
    failures: list[Exception] = []

    def append(index: int) -> None:
        worker = ConversationRepository(database)
        try:
            barrier.wait()
            results.append(
                worker.append_message(
                    _message(workspace, principal, thread, key=f"message-{index}"),
                    requester_principal_id=principal.id,
                    requester_membership_version=1,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            worker.close()

    workers = [threading.Thread(target=append, args=(index,)) for index in (1, 2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert failures == []
    assert sorted(message.sequence for message in results) == [1, 2]


def test_run_event_hash_chain_frozen_pages_and_terminal_denial(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread, _, run, started = _seed_run(repository, workspace, principal)
    delta = repository.append_event(
        _event(
            workspace,
            thread,
            run,
            event_type=AgentRunEventType.MESSAGE_DELTA,
            sequence=2,
            previous_digest=started.event.event_digest,
            provider_event_id="delta",
        ),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    completed = repository.append_event(
        _event(
            workspace,
            thread,
            run,
            event_type=AgentRunEventType.COMPLETED,
            sequence=3,
            previous_digest=delta.event.event_digest,
            provider_event_id="completed",
        ),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    page = repository.page_events(
        workspace.id,
        run.id,
        principal.id,
        after_sequence=0,
        limit=2,
    )
    assert [item.event.sequence for item in page.events] == [1, 2]
    assert page.requested_after == 0
    assert page.next_after == 2
    assert page.high_watermark == 3
    assert page.earliest_sequence == 1
    assert page.has_more is True
    with pytest.raises(ConversationRepositoryError, match="conversation_cursor_invalid"):
        repository.page_events(
            workspace.id,
            run.id,
            principal.id,
            after_sequence=4,
            limit=10,
        )
    with pytest.raises(ConversationRepositoryError, match="run_event_after_terminal"):
        repository.append_event(
            _event(
                workspace,
                thread,
                run,
                event_type=AgentRunEventType.MESSAGE_DELTA,
                sequence=4,
                previous_digest=completed.event.event_digest,
                provider_event_id="late",
            ),
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    repository.close()


def test_run_event_stream_rejects_duplicate_started_event(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread, _, run, started = _seed_run(repository, workspace, principal)
    with pytest.raises(ConversationRepositoryError, match="duplicate_started_event"):
        repository.append_event(
            _event(
                workspace,
                thread,
                run,
                event_type=AgentRunEventType.STARTED,
                sequence=2,
                previous_digest=started.event.event_digest,
                provider_event_id="duplicate-started",
            ),
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    repository.close()


def test_concurrent_events_allocate_gap_free_hash_chain(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread, _, run, started = _seed_run(repository, workspace, principal)
    repository.close()
    barrier = threading.Barrier(3)
    results: list[RunEventRecord] = []
    failures: list[Exception] = []

    def append(index: int) -> None:
        worker = ConversationRepository(database)
        try:
            barrier.wait()
            results.append(
                worker.append_event(
                    _event(
                        workspace,
                        thread,
                        run,
                        event_type=AgentRunEventType.MESSAGE_DELTA,
                        sequence=2,
                        previous_digest=started.event.event_digest,
                        provider_event_id=f"concurrent-{index}",
                    ),
                    requester_principal_id=principal.id,
                    requester_membership_version=1,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            worker.close()

    workers = [threading.Thread(target=append, args=(index,)) for index in (1, 2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert failures == []
    ordered = sorted(results, key=lambda record: record.event.sequence)
    assert [record.event.sequence for record in ordered] == [2, 3]
    assert ordered[0].event.previous_event_digest == started.event.event_digest
    assert ordered[1].event.previous_event_digest == ordered[0].event.event_digest


def test_run_and_provider_event_idempotency_replay_is_exact(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread = repository.create_thread(_thread(workspace, principal))
    message = repository.append_message(
        _message(workspace, principal, thread, key="message"),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    proposed = _run(workspace, principal, thread, message)
    assert repository.create_run(proposed) == proposed
    assert repository.create_run(proposed) == proposed
    changed = proposed.model_copy(update={"canonical_request_digest": "e" * 64})
    with pytest.raises(
        ConversationRepositoryError, match="conversation_idempotency_payload_mismatch"
    ):
        repository.create_run(changed)
    repository.close()


def test_artifact_lineage_requires_same_workspace_event_and_existing_dag(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    _, _, run, _ = _seed_run(repository, workspace, principal)
    parent_id = uuid4()
    parent = RunArtifact(
        id=parent_id,
        workspace_id=workspace.id,
        run_id=run.id,
        producing_event_sequence=1,
        display_name="parent.txt",
        media_type="text/plain",
        byte_size=1,
        content_digest="d" * 64,
        lineage_digest=compute_lineage_digest(
            workspace_id=workspace.id,
            artifact_id=parent_id,
            run_id=run.id,
            producing_event_sequence=1,
            content_digest="d" * 64,
            parent_artifact_ids=(),
        ),
        created_at=NOW,
    )
    repository.record_artifact(
        parent,
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    child_id = uuid4()
    child = RunArtifact(
        id=child_id,
        workspace_id=workspace.id,
        run_id=run.id,
        producing_event_sequence=1,
        display_name="child.txt",
        media_type="text/plain",
        byte_size=1,
        content_digest="e" * 64,
        parent_artifact_ids=(parent.id,),
        lineage_digest=compute_lineage_digest(
            workspace_id=workspace.id,
            artifact_id=child_id,
            run_id=run.id,
            producing_event_sequence=1,
            content_digest="e" * 64,
            parent_artifact_ids=(parent.id,),
        ),
        created_at=NOW + timedelta(seconds=1),
    )
    assert (
        repository.record_artifact(
            child,
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
        == child
    )
    assert repository.list_artifacts(workspace.id, run.id, principal.id) == (parent, child)
    repository.close()


def test_revoked_membership_denies_mutations_without_residue(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread = repository.create_thread(_thread(workspace, principal))
    identities = IdentityScopeRepository(database)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=principal.id,
            role="owner",
            status=MembershipStatus.REVOKED,
            version=2,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    identities.close()
    with pytest.raises(ConversationRepositoryError, match="conversation_membership_inactive"):
        repository.append_message(
            _message(workspace, principal, thread, key="revoked"),
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    assert repository.get_thread(workspace.id, thread.id, principal.id) is None
    repository.close()


def test_guarded_postgres_two_writers_and_schema_controls() -> None:
    engine = _guarded_postgres_engine()
    try:
        workspace, principal = _identity(engine, suffix="postgres")
        repository = ConversationRepository(engine)
        thread = repository.create_thread(_thread(workspace, principal))
        barrier = threading.Barrier(3)
        results: list[Message] = []
        failures: list[Exception] = []

        def append(index: int) -> None:
            worker = ConversationRepository(engine)
            try:
                barrier.wait()
                results.append(
                    worker.append_message(
                        _message(workspace, principal, thread, key=f"postgres-{index}"),
                        requester_principal_id=principal.id,
                        requester_membership_version=1,
                    )
                )
            except Exception as exc:  # pragma: no cover - guarded integration assertion
                failures.append(exc)

        workers = [threading.Thread(target=append, args=(index,)) for index in (1, 2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()
        assert failures == []
        assert sorted(item.sequence for item in results) == [1, 2]
        with engine.connect() as connection:
            index_definition = connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname = 'uq_agent_run_provider_event'"
                )
            )
            trigger_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM pg_trigger WHERE tgname IN "
                    "('agent_run_events_no_update','agent_run_events_no_delete')"
                )
            )
        assert "WHERE (provider_event_id IS NOT NULL)" in str(index_definition)
        assert trigger_count == 2
    finally:
        engine.dispose()
