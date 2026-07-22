from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from corvus.application.conversations import (
    ConversationAuthorizationDecision,
    ConversationMutationReceipt,
    ConversationService,
)
from corvus.domain.agent_runtime import (
    AgentRunEvent,
    AgentRunEventType,
    compute_agent_run_event_digest,
)
from corvus.domain.client import ClientSurface
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
from corvus.domain.request import RequestContext
from corvus.infrastructure.repositories.conversations import (
    ConversationRepository,
    ConversationRepositoryError,
)
from tests.integration.test_conversation_repository import (
    _database,
    _event,
    _identity,
    _seed_run,
)

NOW = datetime(2026, 7, 17, 4, 5, 6, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000001")


def _context(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    scope_kind: str = "thread",
    scope_id: UUID | None = None,
    requester_id: UUID = PRINCIPAL_ID,
    agent_id: UUID | None = None,
) -> RequestContext:
    return RequestContext(
        deployment_profile_id=uuid4(),
        deployment_instance_id=uuid4(),
        workspace_id=workspace_id,
        workspace_authority_epoch=1,
        workspace_authority_generation=7,
        authority_state_root="a" * 64,
        authority_epoch_credential_id=uuid4(),
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="b" * 64,
        scope_kind=scope_kind,
        scope_id=scope_id or uuid4(),
        scope_digest="c" * 64,
        audience_policy_snapshot_id=uuid4(),
        audience_policy_digest="d" * 64,
        requester_id=requester_id,
        client_context_id=uuid4(),
        transport_principal_id=PRINCIPAL_ID,
        agent_id=agent_id or uuid4(),
        agent_grant_id=uuid4(),
        access_bundle_id=uuid4(),
        policy_digest="e" * 64,
        authorization_snapshot_id=uuid4(),
        authorization_snapshot_digest="f" * 64,
        authorization_signing_key_version_id=uuid4(),
        idempotency_key="thread-create",
        correlation_id=uuid4(),
    )


def _thread(context: RequestContext) -> Thread:
    return Thread(
        id=context.scope_id,
        workspace_id=context.workspace_id,
        workspace_version=1,
        creator_principal_id=context.requester_id,
        creator_membership_version=1,
        title="Security thread",
        status=ThreadStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )


class _Authorization:
    def __init__(self, *, allowed: bool = True, snapshot_mismatch: bool = False) -> None:
        self.allowed = allowed
        self.snapshot_mismatch = snapshot_mismatch

    def authorize(self, request: object) -> ConversationAuthorizationDecision:
        context = request.context  # type: ignore[attr-defined]
        return ConversationAuthorizationDecision(
            allowed=self.allowed,
            reason_code="allowed" if self.allowed else "denied",
            authorization_snapshot_id=(
                uuid4() if self.snapshot_mismatch else context.authorization_snapshot_id
            ),
            authorization_snapshot_digest=context.authorization_snapshot_digest,
        )


class _Repository:
    def __init__(self, thread: Thread | None = None) -> None:
        self.thread = thread
        self.create_calls = 0

    def create_thread(self, thread: Thread) -> Thread:
        self.create_calls += 1
        self.thread = thread
        return thread

    def get_thread(self, workspace_id: UUID, thread_id: UUID, principal_id: UUID) -> Thread | None:
        if self.thread is None:
            return None
        if self.thread.workspace_id != workspace_id or self.thread.id != thread_id:
            return None
        return self.thread


class _MutationRepository:
    def __init__(self) -> None:
        self.calls = 0

    def _record(self, value: object) -> object:
        self.calls += 1
        return value

    def register_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        return self._record(attachment)  # type: ignore[return-value]

    def append_message(self, message: Message, **_kwargs: object) -> Message:
        return self._record(message)  # type: ignore[return-value]

    def create_run(self, run: AgentRunRecord) -> AgentRunRecord:
        return self._record(run)  # type: ignore[return-value]

    def append_event(self, record: RunEventRecord, **_kwargs: object) -> RunEventRecord:
        return self._record(record)  # type: ignore[return-value]

    def record_artifact(self, artifact: RunArtifact, **_kwargs: object) -> RunArtifact:
        return self._record(artifact)  # type: ignore[return-value]


class _Lifecycle:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: object, mutation: object) -> ConversationMutationReceipt:
        self.calls += 1
        result = mutation()  # type: ignore[operator]
        return ConversationMutationReceipt(
            prior_state_root=request.context.authority_state_root,  # type: ignore[attr-defined]
            proposed_state_root="1" * 64,
            audit_receipt_id=uuid4(),
            audit_receipt_digest="2" * 64,
            finalized_result_digest=request.payload_digest,  # type: ignore[attr-defined]
            result=result,
        )


def _attachment(context: RequestContext, **updates: object) -> AttachmentRef:
    values: dict[str, object] = {
        "workspace_id": context.workspace_id,
        "owner_principal_id": context.requester_id,
        "owner_membership_version": 1,
        "display_name": "input.txt",
        "media_type": "text/plain",
        "byte_size": 5,
        "content_digest": "1" * 64,
        "created_at": NOW,
    }
    values.update(updates)
    return AttachmentRef(**values)


def _message(context: RequestContext, **updates: object) -> Message:
    content = "authority-bound message"
    values: dict[str, object] = {
        "workspace_id": context.workspace_id,
        "thread_id": context.scope_id,
        "sequence": 1,
        "content": content,
        "content_digest": compute_content_digest(content),
        "idempotency_key": "authority-message",
        "author_kind": MessageAuthorKind.PRINCIPAL,
        "author_principal_id": context.requester_id,
        "author_membership_version": 1,
        "created_at": NOW,
    }
    values.update(updates)
    return Message(**values)


def _run(context: RequestContext, **updates: object) -> AgentRunRecord:
    values: dict[str, object] = {
        "workspace_id": context.workspace_id,
        "thread_id": context.scope_id,
        "message_sequence": 1,
        "requester_principal_id": context.requester_id,
        "requester_membership_version": 1,
        "authorization_snapshot_id": context.authorization_snapshot_id,
        "authorization_snapshot_digest": context.authorization_snapshot_digest,
        "provider_binding_id": uuid4(),
        "provider_binding_version": 1,
        "provider_binding_digest": "2" * 64,
        "canonical_request_digest": "3" * 64,
        "idempotency_key": "authority-run",
        "created_at": NOW,
    }
    values.update(updates)
    return AgentRunRecord(**values)


def _run_event(context: RequestContext, run_id: UUID, **updates: object) -> RunEventRecord:
    workspace_id = updates.pop("workspace_id", context.workspace_id)
    thread_id = updates.pop("thread_id", context.scope_id)
    event = AgentRunEvent(
        run_id=run_id,
        handle_id=run_id,
        sequence=1,
        timestamp=NOW,
        event_type=AgentRunEventType.STARTED,
        redacted_payload={"event": "started"},
        provider_event_id="authority-started",
        previous_event_digest="0" * 64,
        event_digest=compute_agent_run_event_digest(
            run_id=run_id,
            handle_id=run_id,
            sequence=1,
            timestamp=NOW,
            event_type=AgentRunEventType.STARTED,
            redacted_payload={"event": "started"},
            provider_event_id="authority-started",
            previous_event_digest="0" * 64,
            tool_call_id=None,
            effect_authorization_decision_id=None,
            effect_authorization_decision_digest=None,
        ),
    )
    return RunEventRecord(
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        event=event,
        **updates,
    )


def _artifact(context: RequestContext, **updates: object) -> RunArtifact:
    workspace_id = updates.pop("workspace_id", context.workspace_id)
    artifact_id = updates.pop("id", uuid4())
    run_id = updates.pop("run_id", uuid4())
    content_digest = "4" * 64
    return RunArtifact(
        id=artifact_id,
        workspace_id=workspace_id,
        run_id=run_id,
        producing_event_sequence=1,
        display_name="result.txt",
        media_type="text/plain",
        byte_size=1,
        content_digest=content_digest,
        lineage_digest=compute_lineage_digest(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            run_id=run_id,
            producing_event_sequence=1,
            content_digest=content_digest,
            parent_artifact_ids=(),
        ),
        created_at=NOW,
        **updates,
    )


def test_service_fails_closed_before_write_without_authority_lifecycle() -> None:
    context = _context()
    repository = _Repository()
    service = ConversationService(repository=repository, authorization=_Authorization())
    response = service.create_thread(context, ClientSurface.CLI, _thread(context))
    assert response.ok is False
    assert response.reason_code == "conversation_authority_lifecycle_unavailable"
    assert repository.create_calls == 0


def test_service_binds_authorization_snapshot_and_lifecycle_receipt() -> None:
    context = _context()
    repository = _Repository()
    lifecycle = _Lifecycle()
    service = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    )
    response = service.create_thread(context, ClientSurface.DESKTOP, _thread(context))
    assert response.ok is True
    assert response.thread == _thread(context)
    assert lifecycle.calls == 1
    assert repository.create_calls == 1

    mismatched_repository = _Repository()
    mismatched = ConversationService(
        repository=mismatched_repository,
        authorization=_Authorization(snapshot_mismatch=True),
        mutation_lifecycle=_Lifecycle(),
    ).create_thread(context, ClientSurface.CLI, _thread(context))
    assert mismatched.ok is False
    assert mismatched.reason_code == "conversation_authorization_snapshot_mismatch"
    assert mismatched_repository.create_calls == 0


@pytest.mark.parametrize(
    "mutation_family",
    ["attachment", "message", "run", "event", "artifact"],
)
def test_mutations_reject_cross_workspace_authority_transplants_before_lifecycle(
    mutation_family: str,
) -> None:
    context = _context()
    foreign_workspace_id = uuid4()
    repository = _MutationRepository()
    lifecycle = _Lifecycle()
    service = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    )
    if mutation_family == "attachment":
        context = context.model_copy(
            update={"scope_kind": "workspace", "scope_id": context.workspace_id}
        )
        response = service.register_attachment(
            context,
            ClientSurface.CLI,
            _attachment(context, workspace_id=foreign_workspace_id),
        )
    elif mutation_family == "message":
        response = service.append_message(
            context,
            ClientSurface.CLI,
            _message(context, workspace_id=foreign_workspace_id),
            requester_membership_version=1,
        )
    elif mutation_family == "run":
        response = service.create_run(
            context,
            ClientSurface.CLI,
            _run(context, workspace_id=foreign_workspace_id),
        )
    elif mutation_family == "event":
        response = service.append_event(
            context,
            ClientSurface.CLI,
            _run_event(context, uuid4(), workspace_id=foreign_workspace_id),
            requester_membership_version=1,
        )
    else:
        context = context.model_copy(
            update={"scope_kind": "workspace", "scope_id": context.workspace_id}
        )
        response = service.record_artifact(
            context,
            ClientSurface.CLI,
            _artifact(context, workspace_id=foreign_workspace_id),
            requester_membership_version=1,
        )
    assert response.ok is False
    assert response.reason_code == "conversation_authority_binding_mismatch"
    assert lifecycle.calls == 0
    assert repository.calls == 0


@pytest.mark.parametrize("mutation_family", ["message", "run", "event"])
def test_thread_bound_mutations_reject_scope_transplants_before_lifecycle(
    mutation_family: str,
) -> None:
    context = _context()
    foreign_thread_id = uuid4()
    repository = _MutationRepository()
    lifecycle = _Lifecycle()
    service = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    )
    if mutation_family == "message":
        response = service.append_message(
            context,
            ClientSurface.CLI,
            _message(context, thread_id=foreign_thread_id),
            requester_membership_version=1,
        )
    elif mutation_family == "run":
        response = service.create_run(
            context,
            ClientSurface.CLI,
            _run(context, thread_id=foreign_thread_id),
        )
    else:
        response = service.append_event(
            context,
            ClientSurface.CLI,
            _run_event(context, uuid4(), thread_id=foreign_thread_id),
            requester_membership_version=1,
        )
    assert response.reason_code == "conversation_authority_binding_mismatch"
    assert lifecycle.calls == repository.calls == 0


@pytest.mark.parametrize("mutation_family", ["attachment", "artifact"])
def test_workspace_bound_mutations_reject_unrelated_scope_before_lifecycle(
    mutation_family: str,
) -> None:
    context = _context(scope_kind="project")
    repository = _MutationRepository()
    lifecycle = _Lifecycle()
    service = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    )
    if mutation_family == "attachment":
        response = service.register_attachment(context, ClientSurface.CLI, _attachment(context))
    else:
        response = service.record_artifact(
            context,
            ClientSurface.CLI,
            _artifact(context),
            requester_membership_version=1,
        )
    assert response.reason_code == "conversation_authority_binding_mismatch"
    assert lifecycle.calls == repository.calls == 0


def test_mutations_reject_owner_author_requester_and_snapshot_transplants() -> None:
    context = _context()
    repository = _MutationRepository()
    lifecycle = _Lifecycle()
    service = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    )
    workspace_context = context.model_copy(
        update={"scope_kind": "workspace", "scope_id": context.workspace_id}
    )
    responses = (
        service.register_attachment(
            workspace_context,
            ClientSurface.CLI,
            _attachment(workspace_context, owner_principal_id=uuid4()),
        ),
        service.append_message(
            context,
            ClientSurface.CLI,
            _message(context, author_principal_id=uuid4()),
            requester_membership_version=1,
        ),
        service.append_message(
            context,
            ClientSurface.CLI,
            _message(context, author_membership_version=2),
            requester_membership_version=1,
        ),
        service.create_run(
            context,
            ClientSurface.CLI,
            _run(context, requester_principal_id=uuid4()),
        ),
        service.create_run(
            context,
            ClientSurface.CLI,
            _run(context, authorization_snapshot_id=uuid4()),
        ),
        service.create_run(
            context,
            ClientSurface.CLI,
            _run(context, authorization_snapshot_digest="9" * 64),
        ),
    )
    assert {response.reason_code for response in responses} == {
        "conversation_authority_binding_mismatch"
    }
    assert lifecycle.calls == repository.calls == 0


def test_agent_message_author_must_equal_context_agent() -> None:
    context = _context(agent_id=uuid4())
    repository = _MutationRepository()
    lifecycle = _Lifecycle()
    message = _message(
        context,
        author_kind=MessageAuthorKind.AGENT,
        author_principal_id=None,
        author_membership_version=None,
        author_agent_id=uuid4(),
        author_agent_version=1,
    )
    response = ConversationService(
        repository=repository,
        authorization=_Authorization(),
        mutation_lifecycle=lifecycle,
    ).append_message(
        context,
        ClientSurface.CLI,
        message,
        requester_membership_version=1,
    )
    assert response.reason_code == "conversation_authority_binding_mismatch"
    assert lifecycle.calls == repository.calls == 0


def test_reads_are_non_enumerating_across_denial_and_workspace_transplant() -> None:
    context = _context()
    thread = _thread(context)
    denied = ConversationService(
        repository=_Repository(thread),
        authorization=_Authorization(allowed=False),
    ).get_thread(context, ClientSurface.WEB, thread.id)
    assert denied.ok is False
    assert denied.reason_code == "conversation_not_found"

    other_context = _context(workspace_id=uuid4())
    transplanted = ConversationService(
        repository=_Repository(thread),
        authorization=_Authorization(),
    ).get_thread(other_context, ClientSurface.WEB, thread.id)
    assert transplanted.ok is False
    assert transplanted.reason_code == "conversation_not_found"


@pytest.mark.parametrize(
    ("statement", "value"),
    [
        (
            "UPDATE agent_run_events SET payload_json = ? "
            "WHERE workspace_id = ? AND run_id = ? AND sequence = 1",
            '{"event":"tampered"}',
        ),
        (
            "UPDATE agent_run_events SET previous_event_digest = ? "
            "WHERE workspace_id = ? AND run_id = ? AND sequence = 1",
            "9" * 64,
        ),
    ],
)
def test_persisted_event_payload_or_chain_tampering_fails_with_stable_integrity_error(
    tmp_path: Path,
    statement: str,
    value: str,
) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    _, _, run, _ = _seed_run(repository, workspace, principal)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER agent_run_events_no_update")
        connection.execute(
            statement,
            (value, str(workspace.id), str(run.id)),
        )
    with pytest.raises(ConversationRepositoryError, match="run_event_integrity_invalid"):
        repository.page_events(
            workspace.id,
            run.id,
            principal.id,
            after_sequence=0,
            limit=10,
        )
    repository.close()


def test_page_events_rejects_recomputed_digest_disconnected_before_requested_slice(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    repository = ConversationRepository(database)
    thread, _, run, started = _seed_run(repository, workspace, principal)
    second = repository.append_event(
        _event(
            workspace,
            thread,
            run,
            event_type=AgentRunEventType.MESSAGE_DELTA,
            sequence=2,
            previous_digest=started.event.event_digest,
            provider_event_id="second",
        ),
        requester_principal_id=principal.id,
        requester_membership_version=1,
    )
    forged_previous = "9" * 64
    forged_digest = compute_agent_run_event_digest(
        run_id=second.event.run_id,
        handle_id=second.event.handle_id,
        sequence=second.event.sequence,
        timestamp=second.event.timestamp,
        event_type=second.event.event_type,
        redacted_payload=second.event.redacted_payload,
        provider_event_id=second.event.provider_event_id,
        previous_event_digest=forged_previous,
        tool_call_id=second.event.tool_call_id,
        effect_authorization_decision_id=second.event.effect_authorization_decision_id,
        effect_authorization_decision_digest=second.event.effect_authorization_decision_digest,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER agent_run_events_no_update")
        connection.execute(
            "UPDATE agent_run_events SET previous_event_digest = ?, event_digest = ? "
            "WHERE workspace_id = ? AND run_id = ? AND sequence = 2",
            (forged_previous, forged_digest, str(workspace.id), str(run.id)),
        )
    with pytest.raises(ConversationRepositoryError, match="run_event_integrity_invalid"):
        repository.page_events(
            workspace.id,
            run.id,
            principal.id,
            after_sequence=1,
            limit=1,
        )
    repository.close()


def test_changed_provider_replay_and_cursor_workspace_transplant_are_denied(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    workspace, principal = _identity(database)
    other_workspace, other_principal = _identity(database, suffix="security-other")
    repository = ConversationRepository(database)
    thread, _, run, started = _seed_run(repository, workspace, principal)
    changed = _event(
        workspace,
        thread,
        run,
        event_type=AgentRunEventType.MESSAGE_DELTA,
        sequence=2,
        previous_digest=started.event.event_digest,
        provider_event_id="started",
    )
    with pytest.raises(
        ConversationRepositoryError,
        match="conversation_idempotency_payload_mismatch",
    ):
        repository.append_event(
            changed,
            requester_principal_id=principal.id,
            requester_membership_version=1,
        )
    with pytest.raises(ConversationRepositoryError, match="run_not_found"):
        repository.page_events(
            other_workspace.id,
            run.id,
            other_principal.id,
            after_sequence=0,
            limit=10,
        )
    repository.close()
