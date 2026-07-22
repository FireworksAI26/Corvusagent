from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import Field

from corvus.models import StrictModel, now_utc


class ConversationError(RuntimeError):
    """Base error for conversation runtime operations."""


class ConversationNotFoundError(ConversationError):
    """Raised when a chat identifier is unknown."""


class ConversationClosedError(ConversationError):
    """Raised when a chat or the runtime no longer accepts work."""


class ConversationLimitError(ConversationError):
    """Raised before a configured resource bound would be exceeded."""


class SubagentsDisabledError(ConversationError):
    """Raised when a runner requests a subagent without explicit permission."""


class SubagentLimitError(ConversationLimitError):
    """Raised when a subagent count, depth, or budget bound would be exceeded."""


class ConversationStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLED = "cancelled"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationLimits(StrictModel):
    """Hard bounds for all work retained or executed by a runtime."""

    max_chats: int = Field(default=32, ge=1, le=1_000)
    max_concurrent_runs: int = Field(default=4, ge=1, le=64)
    max_queued_messages: int = Field(default=32, ge=1, le=1_000)
    max_messages_per_chat: int = Field(default=200, ge=2, le=10_000)
    max_runs_per_chat: int = Field(default=1_000, ge=1, le=100_000)
    max_message_chars: int = Field(default=32_000, ge=1, le=1_000_000)
    max_output_chars: int = Field(default=100_000, ge=1, le=2_000_000)
    max_event_payload_chars: int = Field(default=128_000, ge=128, le=2_000_000)
    max_events_per_run: int = Field(default=2_048, ge=1, le=100_000)
    event_history_limit: int = Field(default=4_096, ge=16, le=100_000)
    run_timeout_seconds: float = Field(default=3_600.0, gt=0.0, le=86_400.0)


class SubagentPolicy(StrictModel):
    """Explicit, finite authority delegated to runner-created subagents."""

    enabled: bool = False
    max_concurrency: int = Field(default=2, ge=1, le=32)
    max_per_run: int = Field(default=4, ge=1, le=64)
    max_cost_usd: Decimal = Field(default=Decimal("3.00"), gt=Decimal("0"))
    timeout_seconds: float = Field(default=900.0, gt=0.0, le=86_400.0)


class ConversationMessage(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    chat_id: UUID
    role: MessageRole
    content: str
    run_id: UUID | None = None
    created_at: datetime = Field(default_factory=now_utc)


class ConversationEvent(StrictModel):
    schema_version: int = 1
    sequence: int
    event_type: str
    chat_id: UUID | None = None
    run_id: UUID | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class ConversationSnapshot(StrictModel):
    id: UUID
    title: str
    status: ConversationStatus
    active_run_id: UUID | None
    queued_messages: int
    submitted_runs: int
    messages: list[ConversationMessage]
    created_at: datetime


class SubagentResult(StrictModel):
    run_id: UUID
    ok: bool
    output: str | None = None
    error: str | None = None
    reserved_cost_usd: Decimal
    timed_out: bool = False


PublishCallback = Callable[[str, Mapping[str, object]], Awaitable[ConversationEvent]]
SpawnCallback = Callable[["AgentRunContext", str, Decimal], Awaitable[SubagentResult]]


class AgentRunContext:
    """A bounded interface exposed to the injected conversation runner."""

    def __init__(
        self,
        *,
        chat_id: UUID,
        run_id: UUID,
        root_run_id: UUID,
        parent_run_id: UUID | None,
        message_id: UUID | None,
        prompt: str,
        history: tuple[ConversationMessage, ...],
        is_subagent: bool,
        publish: PublishCallback,
        spawn: SpawnCallback,
        max_events: int,
    ) -> None:
        self.chat_id = chat_id
        self.run_id = run_id
        self.root_run_id = root_run_id
        self.parent_run_id = parent_run_id
        self.message_id = message_id
        self.prompt = prompt
        self.history = history
        self.is_subagent = is_subagent
        self._publish = publish
        self._spawn = spawn
        self._max_events = max_events
        self._event_count = 0

    async def emit(
        self,
        event_type: str,
        payload: Mapping[str, object] | None = None,
    ) -> ConversationEvent:
        """Publish a runner event, namespaced so it cannot spoof lifecycle events."""

        normalized = event_type.strip()
        if not normalized or len(normalized) > 80:
            raise ConversationLimitError("runner event type must contain 1 to 80 characters")
        if any(not (character.isalnum() or character in "._-") for character in normalized):
            raise ConversationError("runner event type contains unsupported characters")
        self._event_count += 1
        if self._event_count > self._max_events:
            raise ConversationLimitError("runner event limit exceeded")
        return await self._publish(f"agent.{normalized}", payload or {})

    async def spawn_subagent(
        self,
        prompt: str,
        *,
        cost_limit_usd: Decimal = Decimal("1.00"),
    ) -> SubagentResult:
        """Run one non-recursive subagent within the root run's reserved budget."""

        return await self._spawn(self, prompt, cost_limit_usd)


class ConversationRunner(Protocol):
    async def __call__(self, context: AgentRunContext) -> str | None:
        """Execute one main-agent or subagent turn."""


@dataclass(slots=True)
class _RunScope:
    root_run_id: UUID
    reserved_cost_usd: Decimal = Decimal("0")
    subagent_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[SubagentResult]] = field(default_factory=set)


@dataclass(slots=True)
class _Chat:
    id: UUID
    title: str
    created_at: datetime
    inbox: asyncio.Queue[ConversationMessage]
    messages: deque[ConversationMessage]
    context_messages: deque[ConversationMessage]
    status: ConversationStatus = ConversationStatus.IDLE
    active_run_id: UUID | None = None
    submitted_runs: int = 0
    worker: asyncio.Task[None] | None = None
    scope: _RunScope | None = None


class ConversationRuntime:
    """Coordinates multiple bounded, independently cancellable agent chats."""

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        limits: ConversationLimits | None = None,
        subagents: SubagentPolicy | None = None,
    ) -> None:
        self.runner = runner
        self.limits = limits or ConversationLimits()
        self.subagents = subagents or SubagentPolicy()
        self._chats: dict[UUID, _Chat] = {}
        self._main_semaphore = asyncio.Semaphore(self.limits.max_concurrent_runs)
        self._subagent_semaphore = asyncio.Semaphore(self.subagents.max_concurrency)
        self._events: deque[ConversationEvent] = deque(maxlen=self.limits.event_history_limit)
        self._event_sequence = 0
        self._event_condition = asyncio.Condition()
        self._idle_condition = asyncio.Condition()
        self._state_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def create_chat(self, title: str | None = None) -> ConversationSnapshot:
        self._ensure_open()
        cleaned_title = (title or "New chat").strip()
        if not cleaned_title:
            raise ConversationError("chat title must not be empty")
        if len(cleaned_title) > 200:
            raise ConversationLimitError("chat title exceeds 200 characters")
        async with self._state_lock:
            self._ensure_open()
            if len(self._chats) >= self.limits.max_chats:
                raise ConversationLimitError("chat limit exceeded")
            chat = _Chat(
                id=uuid4(),
                title=cleaned_title,
                created_at=now_utc(),
                inbox=asyncio.Queue(maxsize=self.limits.max_queued_messages),
                messages=deque(maxlen=self.limits.max_messages_per_chat),
                context_messages=deque(maxlen=self.limits.max_messages_per_chat),
            )
            self._chats[chat.id] = chat
            chat.worker = asyncio.create_task(
                self._chat_worker(chat),
                name=f"corvus-chat-{chat.id}",
            )
        await self._publish("conversation.created", chat.id, None, {"title": chat.title})
        return self._snapshot(chat)

    async def send_message(self, chat_id: UUID, text: str) -> ConversationMessage:
        self._ensure_open()
        content = text.strip()
        if not content:
            raise ConversationError("message must not be empty")
        if len(content) > self.limits.max_message_chars:
            raise ConversationLimitError("message exceeds configured character limit")
        chat = self._get_chat(chat_id)
        if chat.status is ConversationStatus.CANCELLED:
            raise ConversationClosedError("chat is cancelled")
        if chat.submitted_runs >= self.limits.max_runs_per_chat:
            raise ConversationLimitError("run limit for this chat exceeded")
        message = ConversationMessage(chat_id=chat_id, role=MessageRole.USER, content=content)
        try:
            chat.inbox.put_nowait(message)
        except asyncio.QueueFull as exc:
            raise ConversationLimitError("message queue is full") from exc
        chat.submitted_runs += 1
        chat.messages.append(message)
        queued_behind_active_run = chat.active_run_id is not None
        await self._publish(
            "message.received",
            chat_id,
            chat.active_run_id,
            {
                "message_id": str(message.id),
                "queued": queued_behind_active_run,
                "queue_position": chat.inbox.qsize(),
            },
        )
        await self._notify_idle_state_changed()
        return message

    async def cancel_chat(self, chat_id: UUID) -> None:
        chat = self._get_chat(chat_id)
        await self._cancel_chat(chat, reason="cancelled by user")

    async def set_subagents_enabled(self, enabled: bool) -> None:
        """Grant or revoke subagent authority for subsequent spawn requests.

        Revocation also cancels every currently registered child task. Main runs
        remain active and can report or recover from their cancelled child calls.
        """

        self._ensure_open()
        async with self._state_lock:
            if self.subagents.enabled is enabled:
                return
            self.subagents.enabled = enabled
            scopes = [chat.scope for chat in self._chats.values() if chat.scope is not None]
        if not enabled:
            for scope in scopes:
                await self._cancel_scope_tasks(scope)
        await self._publish(
            "delegation.granted" if enabled else "delegation.revoked",
            None,
            None,
            {
                "max_concurrency": self.subagents.max_concurrency,
                "max_per_run": self.subagents.max_per_run,
                "max_cost_usd": str(self.subagents.max_cost_usd),
            },
        )

    async def get_chat(self, chat_id: UUID) -> ConversationSnapshot:
        return self._snapshot(self._get_chat(chat_id))

    async def list_chats(self) -> list[ConversationSnapshot]:
        return [
            self._snapshot(chat)
            for chat in sorted(self._chats.values(), key=lambda item: item.created_at)
        ]

    async def wait_idle(self, chat_id: UUID, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        chat = self._get_chat(chat_id)

        def idle() -> bool:
            return chat.active_run_id is None and chat.inbox.empty()

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._idle_condition:
                    await self._idle_condition.wait_for(idle)
        except TimeoutError as exc:
            raise TimeoutError(f"chat {chat_id} did not become idle") from exc

    async def event_history(
        self,
        *,
        chat_id: UUID | None = None,
        after_sequence: int = 0,
    ) -> list[ConversationEvent]:
        async with self._event_condition:
            return [
                event
                for event in self._events
                if event.sequence > after_sequence and (chat_id is None or event.chat_id == chat_id)
            ]

    async def events(
        self,
        *,
        chat_id: UUID | None = None,
        after_sequence: int = 0,
    ) -> AsyncIterator[ConversationEvent]:
        """Yield retained and live events; emit a gap marker after retention overflow."""

        cursor = max(after_sequence, 0)
        while True:
            batch: list[ConversationEvent] = []
            gap: ConversationEvent | None = None
            async with self._event_condition:
                while True:
                    oldest = self._events[0].sequence if self._events else self._event_sequence + 1
                    if cursor < oldest - 1:
                        gap = ConversationEvent(
                            sequence=oldest - 1,
                            event_type="stream.gap",
                            chat_id=chat_id,
                            payload={
                                "after_sequence": cursor,
                                "oldest_available": oldest,
                            },
                        )
                        cursor = oldest - 1
                    batch = [
                        event
                        for event in self._events
                        if event.sequence > cursor and (chat_id is None or event.chat_id == chat_id)
                    ]
                    if gap is not None or batch or self._closed:
                        break
                    # Skip irrelevant events for a filtered stream before sleeping.
                    cursor = self._event_sequence
                    await self._event_condition.wait()
            if gap is not None:
                yield gap
            for event in batch:
                cursor = max(cursor, event.sequence)
                yield event
            if self._closed and not batch:
                return

    async def close(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        chats = list(self._chats.values())
        for chat in chats:
            await self._cancel_chat(chat, reason="runtime closed")
        await self._publish("runtime.closed", None, None, {})
        self._closed = True
        self._closing = False
        async with self._event_condition:
            self._event_condition.notify_all()

    async def _chat_worker(self, chat: _Chat) -> None:
        try:
            while True:
                message = await chat.inbox.get()
                try:
                    await self._execute_message(chat, message)
                finally:
                    chat.inbox.task_done()
                    await self._notify_idle_state_changed()
        except asyncio.CancelledError:
            raise

    async def _execute_message(self, chat: _Chat, message: ConversationMessage) -> None:
        run_id = uuid4()
        scope = _RunScope(root_run_id=run_id)
        chat.active_run_id = run_id
        chat.scope = scope
        chat.status = ConversationStatus.RUNNING
        chat.context_messages.append(message)
        await self._publish(
            "run.queued",
            chat.id,
            run_id,
            {"message_id": str(message.id)},
        )
        await self._notify_idle_state_changed()
        try:
            async with self._main_semaphore:
                await self._publish("run.started", chat.id, run_id, {})
                context = self._make_context(
                    chat=chat,
                    run_id=run_id,
                    root_run_id=run_id,
                    parent_run_id=None,
                    message_id=message.id,
                    prompt=message.content,
                    history=tuple(chat.context_messages),
                    is_subagent=False,
                    scope=scope,
                )
                try:
                    async with asyncio.timeout(self.limits.run_timeout_seconds):
                        output = await self.runner(context)
                    if output is not None:
                        self._validate_text(output, self.limits.max_output_chars, "runner output")
                        assistant_message = ConversationMessage(
                            chat_id=chat.id,
                            role=MessageRole.ASSISTANT,
                            content=output,
                            run_id=run_id,
                        )
                        chat.messages.append(assistant_message)
                        chat.context_messages.append(assistant_message)
                        await self._publish(
                            "assistant.message",
                            chat.id,
                            run_id,
                            {
                                "message_id": str(assistant_message.id),
                                "content": output,
                            },
                        )
                    await self._publish("run.completed", chat.id, run_id, {})
                except TimeoutError:
                    await self._publish(
                        "run.timed_out",
                        chat.id,
                        run_id,
                        {"timeout_seconds": self.limits.run_timeout_seconds},
                    )
                except asyncio.CancelledError:
                    await self._publish("run.cancelled", chat.id, run_id, {})
                    raise
                except Exception as exc:
                    await self._publish(
                        "run.failed",
                        chat.id,
                        run_id,
                        {"error": self._bounded_error(exc)},
                    )
        finally:
            await self._cancel_scope_tasks(scope)
            chat.active_run_id = None
            chat.scope = None
            if chat.status is not ConversationStatus.CANCELLED:
                chat.status = ConversationStatus.IDLE
            await self._notify_idle_state_changed()

    def _make_context(
        self,
        *,
        chat: _Chat,
        run_id: UUID,
        root_run_id: UUID,
        parent_run_id: UUID | None,
        message_id: UUID | None,
        prompt: str,
        history: tuple[ConversationMessage, ...],
        is_subagent: bool,
        scope: _RunScope,
    ) -> AgentRunContext:
        async def publish(
            event_type: str,
            payload: Mapping[str, object],
        ) -> ConversationEvent:
            return await self._publish(event_type, chat.id, run_id, payload)

        async def spawn(
            parent: AgentRunContext,
            subagent_prompt: str,
            cost_limit_usd: Decimal,
        ) -> SubagentResult:
            return await self._spawn_subagent(
                chat,
                scope,
                parent,
                subagent_prompt,
                cost_limit_usd,
            )

        return AgentRunContext(
            chat_id=chat.id,
            run_id=run_id,
            root_run_id=root_run_id,
            parent_run_id=parent_run_id,
            message_id=message_id,
            prompt=prompt,
            history=history,
            is_subagent=is_subagent,
            publish=publish,
            spawn=spawn,
            max_events=self.limits.max_events_per_run,
        )

    async def _spawn_subagent(
        self,
        chat: _Chat,
        scope: _RunScope,
        parent: AgentRunContext,
        prompt: str,
        cost_limit_usd: Decimal,
    ) -> SubagentResult:
        if parent.is_subagent:
            raise SubagentLimitError("recursive subagents are not allowed")
        cleaned_prompt = prompt.strip()
        self._validate_text(cleaned_prompt, self.limits.max_message_chars, "subagent prompt")
        if cost_limit_usd <= 0:
            raise SubagentLimitError("subagent cost limit must be positive")
        async with self._state_lock:
            if not self.subagents.enabled:
                raise SubagentsDisabledError(
                    "subagents are disabled; explicit enablement is required"
                )
            async with scope.lock:
                if scope.subagent_count >= self.subagents.max_per_run:
                    raise SubagentLimitError("subagent count limit exceeded")
                new_total = scope.reserved_cost_usd + cost_limit_usd
                if new_total > self.subagents.max_cost_usd:
                    raise SubagentLimitError("subagent cost budget exceeded")
                scope.subagent_count += 1
                scope.reserved_cost_usd = new_total
                run_id = uuid4()
                execution = asyncio.create_task(
                    self._perform_subagent(
                        chat,
                        scope,
                        parent,
                        run_id,
                        cleaned_prompt,
                        cost_limit_usd,
                    ),
                    name=f"corvus-subagent-{run_id}",
                )
                scope.tasks.add(execution)
        try:
            return await execution
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        finally:
            async with scope.lock:
                scope.tasks.discard(execution)

    async def _perform_subagent(
        self,
        chat: _Chat,
        scope: _RunScope,
        parent: AgentRunContext,
        run_id: UUID,
        prompt: str,
        cost_limit_usd: Decimal,
    ) -> SubagentResult:
        async with self._subagent_semaphore:
            await self._publish(
                "subagent.started",
                chat.id,
                run_id,
                {
                    "parent_run_id": str(parent.run_id),
                    "cost_limit_usd": str(cost_limit_usd),
                },
            )
            context = self._make_context(
                chat=chat,
                run_id=run_id,
                root_run_id=scope.root_run_id,
                parent_run_id=parent.run_id,
                message_id=None,
                prompt=prompt,
                history=parent.history,
                is_subagent=True,
                scope=scope,
            )
            try:
                async with asyncio.timeout(self.subagents.timeout_seconds):
                    output = await self.runner(context)
                if output is not None:
                    self._validate_text(output, self.limits.max_output_chars, "subagent output")
                await self._publish(
                    "subagent.completed",
                    chat.id,
                    run_id,
                    {"has_output": output is not None},
                )
                return SubagentResult(
                    run_id=run_id,
                    ok=True,
                    output=output,
                    reserved_cost_usd=cost_limit_usd,
                )
            except TimeoutError:
                await self._publish(
                    "subagent.timed_out",
                    chat.id,
                    run_id,
                    {"timeout_seconds": self.subagents.timeout_seconds},
                )
                return SubagentResult(
                    run_id=run_id,
                    ok=False,
                    error="subagent timed out",
                    reserved_cost_usd=cost_limit_usd,
                    timed_out=True,
                )
            except asyncio.CancelledError:
                await self._publish("subagent.cancelled", chat.id, run_id, {})
                raise
            except Exception as exc:
                error = self._bounded_error(exc)
                await self._publish("subagent.failed", chat.id, run_id, {"error": error})
                return SubagentResult(
                    run_id=run_id,
                    ok=False,
                    error=error,
                    reserved_cost_usd=cost_limit_usd,
                )

    async def _cancel_scope_tasks(self, scope: _RunScope) -> None:
        async with scope.lock:
            tasks = list(scope.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_chat(self, chat: _Chat, *, reason: str) -> None:
        if chat.status is ConversationStatus.CANCELLED:
            return
        chat.status = ConversationStatus.CANCELLED
        dropped = 0
        while True:
            try:
                chat.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                chat.inbox.task_done()
                dropped += 1
        worker = chat.worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if chat.scope is not None:
            await self._cancel_scope_tasks(chat.scope)
        await self._publish(
            "conversation.cancelled",
            chat.id,
            chat.active_run_id,
            {"reason": reason, "dropped_messages": dropped},
        )
        await self._notify_idle_state_changed()

    async def _publish(
        self,
        event_type: str,
        chat_id: UUID | None,
        run_id: UUID | None,
        payload: Mapping[str, object],
    ) -> ConversationEvent:
        materialized_payload = dict(payload)
        self._validate_payload(materialized_payload)
        async with self._event_condition:
            self._event_sequence += 1
            event = ConversationEvent(
                sequence=self._event_sequence,
                event_type=event_type,
                chat_id=chat_id,
                run_id=run_id,
                payload=materialized_payload,
            )
            self._events.append(event)
            self._event_condition.notify_all()
            return event

    async def _notify_idle_state_changed(self) -> None:
        async with self._idle_condition:
            self._idle_condition.notify_all()

    def _get_chat(self, chat_id: UUID) -> _Chat:
        try:
            return self._chats[chat_id]
        except KeyError as exc:
            raise ConversationNotFoundError(f"unknown chat: {chat_id}") from exc

    @staticmethod
    def _snapshot(chat: _Chat) -> ConversationSnapshot:
        return ConversationSnapshot(
            id=chat.id,
            title=chat.title,
            status=chat.status,
            active_run_id=chat.active_run_id,
            queued_messages=chat.inbox.qsize(),
            submitted_runs=chat.submitted_runs,
            messages=list(chat.messages),
            created_at=chat.created_at,
        )

    def _validate_payload(self, payload: Mapping[str, object]) -> None:
        try:
            encoded = json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ConversationError("event payload is not serializable") from exc
        if len(encoded) > self.limits.max_event_payload_chars:
            raise ConversationLimitError("event payload exceeds configured character limit")

    @staticmethod
    def _validate_text(text: str, maximum: int, label: str) -> None:
        if not text:
            raise ConversationError(f"{label} must not be empty")
        if len(text) > maximum:
            raise ConversationLimitError(f"{label} exceeds configured character limit")

    def _bounded_error(self, exc: Exception) -> str:
        rendered = f"{type(exc).__name__}: {exc}"
        return rendered[: min(self.limits.max_event_payload_chars // 2, 8_000)]

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise ConversationClosedError("conversation runtime is closed")
