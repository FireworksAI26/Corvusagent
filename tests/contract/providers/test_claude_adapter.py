from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from corvus.domain.agent_runtime import (
    AgentRunEventType,
    AgentRunState,
    ProviderDiscoveryQuery,
    validate_agent_run_event_chain,
)
from corvus.infrastructure.agent_runtimes.claude import (
    ClaudeAdapterError,
    ClaudeCliAdapter,
    LocalClaudeTextRequest,
)
from corvus.infrastructure.agent_runtimes.process_session import (
    ProcessInvocation,
    ProcessSessionEvent,
    ProcessSessionEventKind,
)

NOW = datetime(2026, 7, 17, 4, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-4000-8000-000000000002")


class _Session:
    def __init__(self, events: tuple[ProcessSessionEvent, ...]) -> None:
        self._events = events

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        for event in self._events:
            if event.sequence > after_sequence:
                yield event

    async def cancel(self) -> bool:
        return True


class _Starter:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.invocation: ProcessInvocation | None = None

    async def __call__(self, invocation: ProcessInvocation) -> _Session:
        self.invocation = invocation
        return self.session


class _GatedSession:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        events = (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "system",
                    "subtype": "init",
                    "cwd": "C:/private/project",
                    "plugins": ["do-not-expose"],
                    "api_key": "do-not-expose",
                },
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": "do-not-expose-hidden-thinking",
                        },
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hello"},
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=4,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Hello",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                },
            ),
        )
        for event in events:
            if event.sequence <= after_sequence:
                continue
            if event.sequence == 2:
                await self.release.wait()
            yield event

    async def cancel(self) -> bool:
        self.release.set()
        return True


class _GatedStarter:
    def __init__(self) -> None:
        self.session = _GatedSession()
        self.invocation: ProcessInvocation | None = None

    async def __call__(self, invocation: ProcessInvocation) -> _GatedSession:
        self.invocation = invocation
        return self.session


def _adapter(
    tmp_path: Path,
    events: tuple[ProcessSessionEvent, ...],
) -> tuple[ClaudeCliAdapter, _Starter]:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"pinned-claude")
    starter = _Starter(_Session(events))
    return (
        ClaudeCliAdapter(
            executable=executable,
            version="2.1.209",
            scratch_root=tmp_path / "runs",
            clock=lambda: NOW,
            session_starter=starter,
        ),
        starter,
    )


async def _binding(adapter: ClaudeCliAdapter):
    candidates = await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID))
    assert len(candidates) == 1
    return candidates[0].binding


@pytest.mark.asyncio
async def test_claude_uses_pinned_direct_argv_and_explicit_safe_flags(tmp_path: Path) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "result", "subtype": "success", "is_error": False},
            ),
        ),
    )
    binding = await _binding(adapter)

    await adapter.start_local_text(
        binding,
        LocalClaudeTextRequest(
            run_id=uuid4(),
            prompt="Reply with hello.",
            idempotency_key="claude-safe-flags",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            model="sonnet",
            effort="high",
        ),
    )

    assert starter.invocation is not None
    invocation = starter.invocation
    assert invocation.executable == (tmp_path / "claude.exe").resolve()
    assert invocation.arguments == (
        "--print",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--no-session-persistence",
        "--safe-mode",
        "--tools=",
        "--permission-mode",
        "plan",
        "--effort",
        "high",
        "--model",
        "sonnet",
        "Reply with hello.",
    )
    assert invocation.environment == {"NO_COLOR": "1"}


@pytest.mark.asyncio
async def test_claude_refuses_the_managed_root_as_an_explicit_workspace(tmp_path: Path) -> None:
    adapter, _starter = _adapter(tmp_path, ())
    binding = await _binding(adapter)
    managed_root = tmp_path / "runs"
    managed_root.mkdir()

    with pytest.raises(ClaudeAdapterError, match="claude_workspace_unavailable"):
        await adapter.start_local_text(
            binding,
            LocalClaudeTextRequest(
                run_id=uuid4(),
                prompt="Inspect the workspace.",
                idempotency_key="claude-root-workspace",
                deadline=datetime(2026, 7, 18, tzinfo=UTC),
                workspace=managed_root,
            ),
        )


@pytest.mark.asyncio
async def test_claude_replaces_hidden_thinking_with_safe_status_and_streams_text(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"pinned-claude")
    starter = _GatedStarter()
    adapter = ClaudeCliAdapter(
        executable=executable,
        version="2.1.209",
        scratch_root=tmp_path / "runs",
        clock=lambda: NOW,
        session_starter=starter,
    )
    binding = await _binding(adapter)
    started = await adapter.start_local_text(
        binding,
        LocalClaudeTextRequest(
            run_id=uuid4(),
            prompt="Say hello.",
            idempotency_key="claude-streaming",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
        ),
    )

    stream = adapter.events(started.handle)
    first = await asyncio.wait_for(anext(stream), timeout=0.2)
    assert first.event_type is AgentRunEventType.STARTED

    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    starter.session.release.set()
    second = await asyncio.wait_for(pending, timeout=0.2)
    remaining = [event async for event in stream]
    events = [first, second, *remaining]

    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.MESSAGE_DELTA,
        AgentRunEventType.USAGE,
        AgentRunEventType.COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert validate_agent_run_event_chain(events) is AgentRunState.COMPLETED
    assert second.redacted_payload == {"activity": "provider", "status": "thinking"}
    assert events[2].redacted_payload == {"text": "Hello"}
    rendered = repr(events).lower()
    assert "private/project" not in rendered
    assert "do-not-expose" not in rendered
    assert "api_key" not in rendered
    assert "plugins" not in rendered
    assert "do-not-expose-hidden-thinking" not in rendered


@pytest.mark.asyncio
async def test_claude_rejects_a_spoofed_run_handle(tmp_path: Path) -> None:
    adapter, _starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "result", "subtype": "success", "is_error": False},
            ),
        ),
    )
    binding = await _binding(adapter)
    started = await adapter.start_local_text(
        binding,
        LocalClaudeTextRequest(
            run_id=uuid4(),
            prompt="Say hello.",
            idempotency_key="claude-handle-binding",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
        ),
    )
    spoofed = started.handle.model_copy(update={"run_id": uuid4()})

    with pytest.raises(ClaudeAdapterError, match="claude_run_unknown"):
        _ = [event async for event in adapter.events(spoofed)]


@pytest.mark.asyncio
async def test_claude_only_exposes_allowlisted_provider_status(tmp_path: Path) -> None:
    adapter, _starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "system", "subtype": "init"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "system", "subtype": "status", "status": "requesting"},
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "system",
                    "subtype": "status",
                    "status": "C:/private/status-detail",
                },
            ),
            ProcessSessionEvent(
                sequence=4,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "result", "subtype": "success", "is_error": False},
            ),
        ),
    )
    binding = await _binding(adapter)
    started = await adapter.start_local_text(
        binding,
        LocalClaudeTextRequest(
            run_id=uuid4(),
            prompt="Say hello.",
            idempotency_key="claude-status-allowlist",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
        ),
    )

    events = [event async for event in adapter.events(started.handle)]

    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.COMPLETED,
    ]
    assert events[1].redacted_payload == {"activity": "provider", "status": "requesting"}
    assert "private/status-detail" not in repr(events)
