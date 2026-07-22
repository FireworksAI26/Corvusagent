from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from corvus.application.ports import (
    ConversationAuthorizationDecision,
    ConversationAuthorizationPort,
    ConversationAuthorizationRequest,
    ConversationMutationLifecyclePort,
    ConversationMutationReceipt,
    ConversationMutationRequest,
    ConversationMutationResult,
)
from corvus.domain.client import ClientSurface
from corvus.domain.conversations import (
    AgentRunRecord,
    AttachmentRef,
    Message,
    MessageAuthorKind,
    RunArtifact,
    RunEventPage,
    RunEventRecord,
    Thread,
)
from corvus.domain.request import RequestContext
from corvus.security import canonical_json_bytes

__all__ = [
    "ConversationAuthorizationDecision",
    "ConversationAuthorizationRequest",
    "ConversationMutationReceipt",
    "ConversationMutationRequest",
    "ConversationResponse",
    "ConversationService",
]

_AUTHORITY_BINDING_MISMATCH = "conversation_authority_binding_mismatch"


def _digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    ok: bool
    reason_code: str
    thread: Thread | None = None
    threads: tuple[Thread, ...] = ()
    attachment: AttachmentRef | None = None
    message: Message | None = None
    messages: tuple[Message, ...] = ()
    run: AgentRunRecord | None = None
    event: RunEventRecord | None = None
    event_page: RunEventPage | None = None
    artifact: RunArtifact | None = None
    artifacts: tuple[RunArtifact, ...] = ()
    mutation_receipt: ConversationMutationReceipt | None = None


class ConversationService:
    def __init__(
        self,
        *,
        repository: Any,
        authorization: ConversationAuthorizationPort,
        mutation_lifecycle: ConversationMutationLifecyclePort | None = None,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.mutation_lifecycle = mutation_lifecycle

    @staticmethod
    def _failure(context: RequestContext, reason_code: str) -> ConversationResponse:
        return ConversationResponse(request_id=context.id, ok=False, reason_code=reason_code)

    @staticmethod
    def _thread_scope_matches(context: RequestContext, thread_id: UUID) -> bool:
        return context.scope_kind in {"thread", "conversation"} and context.scope_id == thread_id

    @staticmethod
    def _workspace_scope_matches(context: RequestContext) -> bool:
        return context.scope_kind == "workspace" and context.scope_id == context.workspace_id

    def _authorize(
        self,
        *,
        context: RequestContext,
        client_surface: ClientSurface,
        action: str,
        resource_id: UUID,
        non_enumerating: bool,
    ) -> tuple[ConversationAuthorizationDecision | None, ConversationResponse | None]:
        try:
            decision = self.authorization.authorize(
                ConversationAuthorizationRequest(
                    context=context,
                    client_surface=client_surface,
                    action=action,
                    workspace_id=context.workspace_id,
                    resource_id=resource_id,
                )
            )
        except Exception:
            reason = "conversation_not_found" if non_enumerating else "authorization_unavailable"
            return None, self._failure(context, reason)
        if (
            decision.authorization_snapshot_id != context.authorization_snapshot_id
            or decision.authorization_snapshot_digest != context.authorization_snapshot_digest
        ):
            reason = (
                "conversation_not_found"
                if non_enumerating
                else "conversation_authorization_snapshot_mismatch"
            )
            return None, self._failure(context, reason)
        if not decision.allowed:
            reason = "conversation_not_found" if non_enumerating else decision.reason_code
            return None, self._failure(context, reason)
        return decision, None

    def _mutate(
        self,
        *,
        context: RequestContext,
        client_surface: ClientSurface,
        action: str,
        resource_id: UUID,
        payload: object,
        mutation: Callable[[], ConversationMutationResult],
        result_field: str,
        reason_code: str,
    ) -> ConversationResponse:
        if self.mutation_lifecycle is None:
            return self._failure(context, "conversation_authority_lifecycle_unavailable")
        decision, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action=action,
            resource_id=resource_id,
            non_enumerating=False,
        )
        if failure is not None:
            return failure
        if decision is None:  # pragma: no cover - paired return invariant
            return self._failure(context, "authorization_unavailable")
        payload_digest = _digest(payload)
        request = ConversationMutationRequest(
            context=context,
            client_surface=client_surface,
            action=action,
            workspace_id=context.workspace_id,
            resource_id=resource_id,
            authorization_snapshot_id=decision.authorization_snapshot_id,
            authorization_snapshot_digest=decision.authorization_snapshot_digest,
            payload_digest=payload_digest,
        )
        try:
            receipt = self.mutation_lifecycle.execute(request, mutation)
        except Exception as exc:
            failure_reason = getattr(exc, "reason_code", None)
            if not isinstance(failure_reason, str):
                failure_reason = str(exc) if str(exc).startswith("conversation_") else None
            return self._failure(
                context,
                failure_reason or "conversation_persistence_failed",
            )
        if (
            receipt.prior_state_root != context.authority_state_root
            or receipt.finalized_result_digest != payload_digest
        ):
            return self._failure(context, "conversation_authority_receipt_mismatch")
        fields: dict[str, object] = {
            "request_id": context.id,
            "ok": True,
            "reason_code": reason_code,
            "mutation_receipt": receipt,
            result_field: receipt.result,
        }
        return ConversationResponse.model_validate(fields)

    def create_thread(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        thread: Thread,
    ) -> ConversationResponse:
        if (
            thread.workspace_id != context.workspace_id
            or context.scope_kind not in {"thread", "conversation"}
            or context.scope_id != thread.id
        ):
            return self._failure(context, "conversation_request_scope_mismatch")
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.create_thread",
            resource_id=thread.id,
            payload=thread,
            mutation=lambda: self.repository.create_thread(thread),
            result_field="thread",
            reason_code="thread_created",
        )

    def get_thread(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        thread_id: UUID,
    ) -> ConversationResponse:
        if context.scope_kind in {"thread", "conversation"} and context.scope_id != thread_id:
            return self._failure(context, "conversation_not_found")
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.read_thread",
            resource_id=thread_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            thread = self.repository.get_thread(
                context.workspace_id, thread_id, context.requester_id
            )
        except Exception:
            return self._failure(context, "conversation_not_found")
        if thread is None:
            return self._failure(context, "conversation_not_found")
        return ConversationResponse(
            request_id=context.id,
            ok=True,
            reason_code="thread_found",
            thread=thread,
        )

    def list_threads(
        self, context: RequestContext, client_surface: ClientSurface
    ) -> ConversationResponse:
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.list_threads",
            resource_id=context.workspace_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            threads = self.repository.list_threads(context.workspace_id, context.requester_id)
        except Exception:
            return self._failure(context, "conversation_not_found")
        return ConversationResponse(
            request_id=context.id, ok=True, reason_code="threads_listed", threads=threads
        )

    def archive_thread(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        *,
        thread_id: UUID,
        expected_version: int,
        membership_version: int,
        updated_at: datetime,
    ) -> ConversationResponse:
        payload = {
            "thread_id": thread_id,
            "expected_version": expected_version,
            "membership_version": membership_version,
            "updated_at": updated_at,
        }
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.archive_thread",
            resource_id=thread_id,
            payload=payload,
            mutation=lambda: self.repository.archive_thread(
                workspace_id=context.workspace_id,
                thread_id=thread_id,
                expected_version=expected_version,
                requester_principal_id=context.requester_id,
                requester_membership_version=membership_version,
                updated_at=updated_at,
            ),
            result_field="thread",
            reason_code="thread_archived",
        )

    def register_attachment(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        attachment: AttachmentRef,
    ) -> ConversationResponse:
        if (
            attachment.workspace_id != context.workspace_id
            or attachment.owner_principal_id != context.requester_id
            or not self._workspace_scope_matches(context)
        ):
            return self._failure(context, _AUTHORITY_BINDING_MISMATCH)
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.register_attachment",
            resource_id=attachment.id,
            payload=attachment,
            mutation=lambda: self.repository.register_attachment(attachment),
            result_field="attachment",
            reason_code="attachment_registered",
        )

    def append_message(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        message: Message,
        *,
        requester_membership_version: int,
    ) -> ConversationResponse:
        principal_author_matches = (
            message.author_kind == MessageAuthorKind.PRINCIPAL
            and message.author_principal_id == context.requester_id
            and message.author_membership_version == requester_membership_version
        )
        agent_author_matches = (
            message.author_kind == MessageAuthorKind.AGENT
            and context.agent_id is not None
            and message.author_agent_id == context.agent_id
        )
        system_author_matches = message.author_kind == MessageAuthorKind.SYSTEM
        if (
            message.workspace_id != context.workspace_id
            or not self._thread_scope_matches(context, message.thread_id)
            or not (principal_author_matches or agent_author_matches or system_author_matches)
        ):
            return self._failure(context, _AUTHORITY_BINDING_MISMATCH)
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.append_message",
            resource_id=message.thread_id,
            payload=message,
            mutation=lambda: self.repository.append_message(
                message,
                requester_principal_id=context.requester_id,
                requester_membership_version=requester_membership_version,
            ),
            result_field="message",
            reason_code="message_appended",
        )

    def list_messages(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        thread_id: UUID,
    ) -> ConversationResponse:
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.list_messages",
            resource_id=thread_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            messages = self.repository.list_messages(
                context.workspace_id, thread_id, context.requester_id
            )
        except Exception:
            return self._failure(context, "conversation_not_found")
        return ConversationResponse(
            request_id=context.id,
            ok=True,
            reason_code="messages_listed",
            messages=messages,
        )

    def create_run(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        run: AgentRunRecord,
    ) -> ConversationResponse:
        if (
            run.workspace_id != context.workspace_id
            or not self._thread_scope_matches(context, run.thread_id)
            or run.requester_principal_id != context.requester_id
            or run.authorization_snapshot_id != context.authorization_snapshot_id
            or run.authorization_snapshot_digest != context.authorization_snapshot_digest
        ):
            return self._failure(context, _AUTHORITY_BINDING_MISMATCH)
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.create_run",
            resource_id=run.id,
            payload=run,
            mutation=lambda: self.repository.create_run(run),
            result_field="run",
            reason_code="run_created",
        )

    def get_run(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        run_id: UUID,
    ) -> ConversationResponse:
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.read_run",
            resource_id=run_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            run = self.repository.get_run(context.workspace_id, run_id, context.requester_id)
        except Exception:
            return self._failure(context, "conversation_not_found")
        if run is None:
            return self._failure(context, "conversation_not_found")
        return ConversationResponse(
            request_id=context.id, ok=True, reason_code="run_found", run=run
        )

    def append_event(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        record: RunEventRecord,
        *,
        requester_membership_version: int,
    ) -> ConversationResponse:
        if record.workspace_id != context.workspace_id or not self._thread_scope_matches(
            context, record.thread_id
        ):
            return self._failure(context, _AUTHORITY_BINDING_MISMATCH)
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.append_event",
            resource_id=record.run_id,
            payload=record,
            mutation=lambda: self.repository.append_event(
                record,
                requester_principal_id=context.requester_id,
                requester_membership_version=requester_membership_version,
            ),
            result_field="event",
            reason_code="event_appended",
        )

    def page_events(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        run_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> ConversationResponse:
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.page_events",
            resource_id=run_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            page = self.repository.page_events(
                context.workspace_id,
                run_id,
                context.requester_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except Exception as exc:
            reason = getattr(exc, "reason_code", "conversation_not_found")
            return self._failure(context, reason)
        return ConversationResponse(
            request_id=context.id,
            ok=True,
            reason_code="events_paged",
            event_page=page,
        )

    def record_artifact(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        artifact: RunArtifact,
        *,
        requester_membership_version: int,
    ) -> ConversationResponse:
        if artifact.workspace_id != context.workspace_id or not self._workspace_scope_matches(
            context
        ):
            return self._failure(context, _AUTHORITY_BINDING_MISMATCH)
        return self._mutate(
            context=context,
            client_surface=client_surface,
            action="conversation.record_artifact",
            resource_id=artifact.id,
            payload=artifact,
            mutation=lambda: self.repository.record_artifact(
                artifact,
                requester_principal_id=context.requester_id,
                requester_membership_version=requester_membership_version,
            ),
            result_field="artifact",
            reason_code="artifact_recorded",
        )

    def list_artifacts(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        run_id: UUID,
    ) -> ConversationResponse:
        _, failure = self._authorize(
            context=context,
            client_surface=client_surface,
            action="conversation.list_artifacts",
            resource_id=run_id,
            non_enumerating=True,
        )
        if failure is not None:
            return failure
        try:
            artifacts = self.repository.list_artifacts(
                context.workspace_id, run_id, context.requester_id
            )
        except Exception:
            return self._failure(context, "conversation_not_found")
        return ConversationResponse(
            request_id=context.id,
            ok=True,
            reason_code="artifacts_listed",
            artifacts=artifacts,
        )
