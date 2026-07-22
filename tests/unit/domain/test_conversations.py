from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

import pytest
from pydantic import ValidationError

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

WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")
THREAD_ID = UUID("20000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("40000000-0000-4000-8000-000000000001")
RUN_ID = UUID("50000000-0000-4000-8000-000000000001")
HANDLE_ID = UUID("60000000-0000-4000-8000-000000000001")
MESSAGE_ID = UUID("70000000-0000-4000-8000-000000000001")
ATTACHMENT_ID = UUID("80000000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("90000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC)
DIGEST = "a" * 64


def _thread(**updates: object) -> Thread:
    values: dict[str, object] = {
        "id": THREAD_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_version": 1,
        "project_id": None,
        "creator_principal_id": PRINCIPAL_ID,
        "creator_membership_version": 1,
        "title": "Production incident",
        "status": ThreadStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    values.update(updates)
    return Thread(**values)


def _attachment(**updates: object) -> AttachmentRef:
    values: dict[str, object] = {
        "id": ATTACHMENT_ID,
        "workspace_id": WORKSPACE_ID,
        "owner_principal_id": PRINCIPAL_ID,
        "owner_membership_version": 1,
        "display_name": "evidence.json",
        "media_type": "application/json",
        "byte_size": 12,
        "content_digest": hashlib.sha256(b"evidence").hexdigest(),
        "metadata": {"source": "composer", "page": 1},
        "created_at": NOW,
    }
    values.update(updates)
    return AttachmentRef(**values)


def _message(**updates: object) -> Message:
    content = "Investigate the failed deployment."
    values: dict[str, object] = {
        "id": MESSAGE_ID,
        "workspace_id": WORKSPACE_ID,
        "thread_id": THREAD_ID,
        "sequence": 1,
        "content": content,
        "content_digest": compute_content_digest(content),
        "idempotency_key": "message-1",
        "producing_run_id": None,
        "attachment_ids": (ATTACHMENT_ID,),
        "author_kind": MessageAuthorKind.PRINCIPAL,
        "author_principal_id": PRINCIPAL_ID,
        "author_membership_version": 1,
        "author_agent_id": None,
        "author_agent_version": None,
        "created_at": NOW,
    }
    values.update(updates)
    return Message(**values)


def _run(**updates: object) -> AgentRunRecord:
    values: dict[str, object] = {
        "id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "thread_id": THREAD_ID,
        "message_sequence": 1,
        "requester_principal_id": PRINCIPAL_ID,
        "requester_membership_version": 1,
        "authorization_snapshot_id": UUID("a0000000-0000-4000-8000-000000000001"),
        "authorization_snapshot_digest": "b" * 64,
        "provider_binding_id": UUID("b0000000-0000-4000-8000-000000000001"),
        "provider_binding_version": 1,
        "provider_binding_digest": "c" * 64,
        "canonical_request_digest": "d" * 64,
        "idempotency_key": "run-1",
        "parent_run_id": None,
        "root_run_id": None,
        "created_at": NOW,
    }
    values.update(updates)
    return AgentRunRecord(**values)


def _agent_event(*, event_type: AgentRunEventType = AgentRunEventType.STARTED) -> AgentRunEvent:
    payload = {"message": "started"}
    digest = compute_agent_run_event_digest(
        run_id=RUN_ID,
        handle_id=HANDLE_ID,
        sequence=1,
        timestamp=NOW,
        event_type=event_type,
        redacted_payload=payload,
        provider_event_id="provider-1",
        previous_event_digest="0" * 64,
        tool_call_id=None,
        effect_authorization_decision_id=None,
        effect_authorization_decision_digest=None,
    )
    return AgentRunEvent(
        run_id=RUN_ID,
        handle_id=HANDLE_ID,
        sequence=1,
        timestamp=NOW,
        event_type=event_type,
        redacted_payload=payload,
        provider_event_id="provider-1",
        previous_event_digest="0" * 64,
        event_digest=digest,
    )


def test_conversation_models_are_frozen_and_reject_extra_fields() -> None:
    thread = _thread()
    with pytest.raises(ValidationError):
        thread.title = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _thread(unexpected="field")


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_thread_requires_aware_timestamps(field: str) -> None:
    with pytest.raises(ValidationError, match="conversation_timestamp_must_be_timezone_aware"):
        _thread(**{field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_thread_rejects_blank_or_oversized_title(title: str) -> None:
    with pytest.raises(ValidationError, match="thread_title"):
        _thread(title=title)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"display_name": "../secret.txt"}, "attachment_name_must_be_safe_leaf"),
        ({"display_name": "C:\\secret.txt"}, "attachment_name_must_be_safe_leaf"),
        ({"media_type": "not a media type"}, "attachment_media_type_invalid"),
        ({"byte_size": -1}, "greater_than_equal"),
        ({"content_digest": "A" * 64}, "string_pattern_mismatch"),
        ({"metadata": {"path": "C:/secret"}}, "sensitive_payload"),
        ({"metadata": {"nested": {"locator": "https://example.test/blob"}}}, "sensitive_payload"),
        ({"metadata": {"token": "abcd"}}, "sensitive_payload"),
        ({"metadata": {"score": float("nan")}}, "canonical"),
    ],
)
def test_attachment_metadata_is_safe_bounded_and_canonical(
    updates: dict[str, object], reason: str
) -> None:
    with pytest.raises((ValidationError, ValueError), match=reason):
        _attachment(**updates)


def test_attachment_metadata_is_recursively_frozen() -> None:
    attachment = _attachment(metadata={"nested": {"items": [1, 2]}})
    assert isinstance(attachment.metadata, MappingProxyType)
    with pytest.raises(TypeError):
        attachment.metadata["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize(
    "updates",
    [
        {"content": ""},
        {"content": " "},
        {"content": "x" * 100_001},
        {"content_digest": DIGEST},
    ],
)
def test_message_rejects_blank_oversized_or_digest_mismatched_content(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="message_(content|digest)"):
        _message(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"author_principal_id": None},
        {"author_agent_id": AGENT_ID, "author_agent_version": 1},
        {
            "author_kind": MessageAuthorKind.AGENT,
            "author_principal_id": None,
            "author_membership_version": None,
        },
        {
            "author_kind": MessageAuthorKind.SYSTEM,
            "author_principal_id": PRINCIPAL_ID,
            "author_membership_version": 1,
        },
    ],
)
def test_message_author_discriminator_requires_exactly_one_versioned_identity(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="message_author_binding_invalid"):
        _message(**updates)


def test_message_rejects_duplicate_attachment_order_and_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="message_attachment_ids_duplicate"):
        _message(attachment_ids=(ATTACHMENT_ID, ATTACHMENT_ID))
    with pytest.raises(ValidationError, match="conversation_timestamp_must_be_timezone_aware"):
        _message(created_at=NOW.replace(tzinfo=None))


def test_run_binding_is_immutable_and_requires_canonical_digests_and_lineage() -> None:
    run = _run(root_run_id=RUN_ID)
    assert run.root_run_id == RUN_ID
    with pytest.raises(ValidationError):
        _run(provider_binding_digest="f" * 63)
    with pytest.raises(ValidationError, match="run_parent_cannot_equal_run"):
        _run(parent_run_id=RUN_ID)
    with pytest.raises(ValidationError, match="run_root_required_for_parent"):
        _run(parent_run_id=UUID("50000000-0000-4000-8000-000000000002"))


def test_run_event_wraps_exact_workspace_thread_run_and_terminal_rules() -> None:
    record = RunEventRecord(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        run_id=RUN_ID,
        event=_agent_event(),
    )
    assert record.event.run_id == record.run_id
    with pytest.raises(ValidationError, match="run_event_run_mismatch"):
        RunEventRecord(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            run_id=UUID("50000000-0000-4000-8000-000000000002"),
            event=_agent_event(),
        )
    with pytest.raises(ValidationError, match="run_event_stream_must_start"):
        RunEventRecord(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            event=_agent_event(event_type=AgentRunEventType.COMPLETED),
        )


def test_artifact_lineage_is_canonical_unique_and_acyclic_at_model_boundary() -> None:
    parent = UUID("90000000-0000-4000-8000-000000000002")
    artifact = RunArtifact(
        id=ARTIFACT_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        producing_event_sequence=1,
        display_name="result.patch",
        media_type="text/x-diff",
        byte_size=123,
        content_digest="e" * 64,
        parent_artifact_ids=(parent,),
        lineage_digest=compute_lineage_digest(
            workspace_id=WORKSPACE_ID,
            artifact_id=ARTIFACT_ID,
            run_id=RUN_ID,
            producing_event_sequence=1,
            content_digest="e" * 64,
            parent_artifact_ids=(parent,),
        ),
        created_at=NOW,
    )
    assert artifact.parent_artifact_ids == (parent,)
    with pytest.raises(ValidationError, match="artifact_lineage_digest_mismatch"):
        RunArtifact(**{**artifact.model_dump(), "lineage_digest": DIGEST})
    with pytest.raises(ValidationError, match="artifact_lineage_self_cycle"):
        RunArtifact(**{**artifact.model_dump(), "parent_artifact_ids": (ARTIFACT_ID,)})
    with pytest.raises(ValidationError, match="artifact_parent_ids_duplicate"):
        RunArtifact(**{**artifact.model_dump(), "parent_artifact_ids": (parent, parent)})
