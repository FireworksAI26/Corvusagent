from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.context import ContextOwner
from corvus.delivery import DeliveryManager
from corvus.interactive import AgentEvent, InteractiveAgent
from corvus.models import ModelChunk, ModelMessage, ModelRequest
from corvus.orchestration import AgentOrchestrator
from corvus.providers import (
    ProviderOutputLimitError,
    ProviderStreamLimits,
    collect_provider_stream,
)
from corvus.security import SecretRedactor
from corvus.store import TraceStore
from corvus.workflow import CodingWorkflow


class _TrackedStream(AsyncIterator[ModelChunk]):
    def __init__(self, chunks: list[ModelChunk], *, fail_close: bool = False) -> None:
        self.chunks = chunks
        self.fail_close = fail_close
        self.index = 0
        self.pulls = 0
        self.closed = False

    def __aiter__(self) -> _TrackedStream:
        return self

    async def __anext__(self) -> ModelChunk:
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.index]
        self.index += 1
        self.pulls += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("provider cancellation failed")


class _TrackedProvider:
    def __init__(self, chunks: list[ModelChunk], *, fail_close: bool = False) -> None:
        self._chunks = chunks
        self.fail_close = fail_close
        self.requests: list[ModelRequest] = []
        self.streams: list[_TrackedStream] = []

    def stream(self, request: ModelRequest) -> _TrackedStream:
        self.requests.append(request)
        stream = _TrackedStream(list(self._chunks), fail_close=self.fail_close)
        self.streams.append(stream)
        return stream


def _limits(
    *,
    chunks: int = 16,
    characters: int = 128,
    bytes_: int = 512,
    emitted_characters: int | None = None,
    emitted_bytes: int | None = None,
    persisted_characters: int | None = None,
    persisted_bytes: int | None = None,
) -> ProviderStreamLimits:
    return ProviderStreamLimits(
        max_chunks=chunks,
        max_characters=characters,
        max_bytes=bytes_,
        max_emitted_characters=emitted_characters or characters,
        max_emitted_bytes=emitted_bytes or bytes_,
        max_persisted_characters=persisted_characters or characters,
        max_persisted_bytes=persisted_bytes or bytes_,
    )


async def _ignore_event(_: AgentEvent) -> None:
    return None


@pytest.mark.asyncio
async def test_interactive_rejects_single_oversized_chunk_before_persistence(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "corvus.db")
    provider = _TrackedProvider(
        [ModelChunk(type="text", text="oversized"), ModelChunk(type="text", text="must-not-read")]
    )
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    run_id = uuid4()
    response = await InteractiveAgent(
        provider,  # type: ignore[arg-type]
        provenance=store,
        provider_stream_limits=_limits(characters=8),
    ).respond(
        "hello",
        [],
        emit,
        owner=ContextOwner.root(run_id),
    )

    owner = ContextOwner.legacy_run(run_id)
    assert response == "Model provider error: output_limit_exceeded"
    assert provider.streams[0].closed is True
    assert provider.streams[0].pulls == 1
    assert [event.type for event in events][-1] == "agent.error"
    assert events[-1].text == "output_limit_exceeded"
    assert not any(item["origin"] == "model" for item in store.external_contents(owner))
    store.engine.dispose()


@pytest.mark.asyncio
async def test_interactive_rejects_cumulative_and_utf8_byte_overflow_without_post_limit_delta(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "corvus.db")
    provider = _TrackedProvider(
        [ModelChunk(type="text", text="abc"), ModelChunk(type="text", text="dé")]
    )
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    response = await InteractiveAgent(
        provider,  # type: ignore[arg-type]
        provenance=store,
        provider_stream_limits=_limits(characters=16, bytes_=5),
    ).respond("hello", [], emit, owner=ContextOwner.legacy_run(uuid4()))

    assert response == "Model provider error: output_limit_exceeded"
    assert provider.streams[0].closed is True
    assert provider.streams[0].pulls == 2
    assert all(event.type != "agent.delta" for event in events)
    store.engine.dispose()


@pytest.mark.asyncio
async def test_incremental_collector_redacts_secret_split_across_chunks_and_returns_metadata() -> (
    None
):
    canary = "corvus-cross-chunk-canary-9301"
    provider = _TrackedProvider(
        [
            ModelChunk(type="text", text="before " + canary[:12]),
            ModelChunk(type="text", text=canary[12:] + " after"),
            ModelChunk(type="done"),
        ]
    )
    emitted: list[str] = []

    async def on_text(text: str) -> None:
        emitted.append(text)

    result = await collect_provider_stream(
        provider,  # type: ignore[arg-type]
        ModelRequest(messages=[ModelMessage(role="user", content="hello")]),
        redactor=SecretRedactor([canary]),
        limits=_limits(),
        on_text=on_text,
    )

    assert result.text == "before [REDACTED] after"
    assert "".join(emitted) == result.text
    assert canary not in result.text
    assert result.metadata["truncated"] is False
    assert result.metadata["captured_characters"] == len(result.text)
    assert result.metadata["captured_bytes"] == len(result.text.encode("utf-8"))


@pytest.mark.asyncio
async def test_orchestrator_keeps_output_limit_stable_when_stream_cancellation_fails(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "corvus.db")
    provider = _TrackedProvider(
        [ModelChunk(type="text", text="too-long"), ModelChunk(type="text", text="post-limit")],
        fail_close=True,
    )

    events = [
        event
        async for event in AgentOrchestrator(
            store,
            provider,  # type: ignore[arg-type]
            provider_stream_limits=_limits(characters=4),
        ).begin("plan", tmp_path)
    ]

    owner = ContextOwner.legacy_run(events[0].run_id)
    assert provider.streams[0].closed is True
    assert provider.streams[0].pulls == 1
    assert [event.event_type for event in events][-1] == "run.blocked"
    assert events[-1].payload["reason"] == "output_limit_exceeded"
    assert not any(item["origin"] == "model" for item in store.external_contents(owner))
    store.engine.dispose()


def test_workflow_bounds_retained_candidate_provider_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "base.txt").write_text("base", encoding="utf-8")
    store = TraceStore(tmp_path / "corvus.db")
    provider = _TrackedProvider([ModelChunk(type="text", text="too-long-candidate")])
    workflow = CodingWorkflow(
        store,
        provider,  # type: ignore[arg-type]
        DeliveryManager(tmp_path / "bundles", tmp_path / "backups"),
        tmp_path / "runs",
        provider_stream_limits=_limits(characters=4),
    )

    run_id, bundle = asyncio.run(workflow.execute("update base", project))

    owner = ContextOwner.legacy_run(run_id)
    blocked = [event for event in store.events(run_id) if event.event_type == "run.blocked"]
    assert bundle is None
    assert provider.streams[0].closed is True
    assert blocked[-1].payload["reason"] == "output_limit_exceeded"
    assert not any(item["origin"] == "model" for item in store.external_contents(owner))
    store.engine.dispose()


@pytest.mark.asyncio
async def test_collector_reports_stable_overflow_when_cancel_raises() -> None:
    provider = _TrackedProvider([ModelChunk(type="text", text="overflow")], fail_close=True)

    with pytest.raises(ProviderOutputLimitError, match="^output_limit_exceeded$"):
        await collect_provider_stream(
            provider,  # type: ignore[arg-type]
            ModelRequest(messages=[ModelMessage(role="user", content="hello")]),
            redactor=SecretRedactor(),
            limits=_limits(characters=4),
        )

    assert provider.streams[0].closed is True
    assert provider.streams[0].pulls == 1
