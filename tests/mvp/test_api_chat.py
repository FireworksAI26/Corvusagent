from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from corvus.mvp.api_chat import ApiChatBackend, ApiChatError

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_openai_api_chat_streams_each_text_delta_without_buffering() -> None:
    body = "\n".join(
        (
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            "",
            'data: {"type":"response.output_text.delta","delta":" now"}',
            "",
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"input_tokens_details":{"cached_tokens":4},"output_tokens":3}}}',
            "",
        )
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    handle = await backend.start(
        run_id=uuid4(),
        prompt="Say hello",
        model="gpt-5.6-sol",
        effort="high",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-chat-1",
    )
    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == [
        "started",
        "message",
        "message",
        "usage",
        "completed",
    ]
    assert [event.payload.get("text") for event in events[1:3]] == ["Hello", " now"]
    assert events[3].payload == {
        "input_tokens": 12,
        "cached_input_tokens": 4,
        "output_tokens": 3,
    }
    assert "sk-test-never-log" not in repr(backend)


@pytest.mark.asyncio
async def test_api_chat_stops_an_unbounded_provider_stream_at_the_output_limit() -> None:
    body = "\n\n".join(
        (
            'data: {"type":"response.output_text.delta","delta":"123"}',
            'data: {"type":"response.output_text.delta","delta":"456"}',
            'data: {"type":"response.completed"}',
        )
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        max_output_bytes=5,
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Count",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-output-limit",
    )

    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "message", "failed"]
    assert events[-1].payload == {"reason_code": "provider_output_limit"}


@pytest.mark.asyncio
async def test_api_chat_rejects_an_oversized_event_before_json_parsing() -> None:
    oversized_delta = "x" * 70_000
    body = f'data: {{"type":"response.output_text.delta","delta":"{oversized_delta}"}}\n\n'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body.encode(), request=request)
    )
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        max_output_bytes=5,
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Bound the provider frame",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-frame-limit",
    )

    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "failed"]
    assert events[-1].payload == {"reason_code": "provider_output_limit"}


@pytest.mark.asyncio
async def test_api_chat_deadline_does_not_cancel_consumer_while_yield_is_suspended() -> None:
    body = "\n\n".join(
        (
            'data: {"type":"response.output_text.delta","delta":"first"}',
            'data: {"type":"response.completed"}',
        )
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
        absolute_deadline_seconds=0.01,
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Keep cancellation inside the provider read",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-yield-deadline",
    )
    events = backend.events(handle)

    assert (await anext(events)).type == "started"
    assert (await anext(events)).type == "message"
    await asyncio.sleep(0.02)

    failed = await anext(events)
    assert failed.type == "failed"
    assert failed.payload == {"reason_code": "provider_deadline_exceeded"}


@pytest.mark.parametrize(
    ("provider", "line", "expected"),
    (
        (
            "anthropic",
            'data: {"type":"message_delta","usage":{"output_tokens":9}}',
            {"output_tokens": 9},
        ),
        (
            "gemini",
            'data: {"usageMetadata":{"promptTokenCount":20,"cachedContentTokenCount":5,"candidatesTokenCount":7}}',
            {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 7},
        ),
        (
            "xai",
            'data: {"usage":{"prompt_tokens":18,"prompt_tokens_details":{"cached_tokens":2},"completion_tokens":6}}',
            {"input_tokens": 18, "cached_input_tokens": 2, "output_tokens": 6},
        ),
    ),
)
def test_api_chat_normalizes_provider_usage(
    provider: Any, line: str, expected: dict[str, int]
) -> None:
    backend = ApiChatBackend(provider=provider, credential="test-secret", clock=lambda: NOW)

    assert backend._usage(line) == expected


@pytest.mark.asyncio
async def test_api_chat_rejects_build_and_mcp_without_a_sandbox_adapter() -> None:
    backend = ApiChatBackend(
        provider="anthropic",
        credential="test-secret-value",
        clock=lambda: NOW,
    )

    with pytest.raises(ApiChatError, match="provider_mode_unavailable"):
        await backend.start(
            run_id=uuid4(),
            prompt="Change files",
            model="claude-sonnet-4-5",
            effort="high",
            mode="build",
            mcp_enabled=True,
            idempotency_key="api-build-denied",
        )


@pytest.mark.asyncio
async def test_openai_api_chat_normalizes_default_effort_and_rejects_max() -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(httpx.Response(200, request=request, content=request.content).json())
        return httpx.Response(200, text='data: {"type":"response.completed"}\n', request=request)

    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Reason carefully",
        model="gpt-5.6-sol",
        effort="normal",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-normal-effort",
    )

    _events = [event async for event in backend.events(handle)]

    assert bodies[0]["reasoning"] == {"effort": "medium"}
    with pytest.raises(ApiChatError, match="provider_effort_unavailable"):
        await backend.start(
            run_id=uuid4(),
            prompt="Reason past supported bounds",
            model="gpt-5.6-sol",
            effort="max",
            mode="chat",
            mcp_enabled=False,
            idempotency_key="api-max-effort",
        )


@pytest.mark.asyncio
async def test_openai_api_chat_omits_reasoning_for_non_reasoning_models() -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(httpx.Response(200, request=request, content=request.content).json())
        return httpx.Response(200, text='data: {"type":"response.completed"}\n', request=request)

    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Answer without a reasoning control",
        model="gpt-4.1",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-non-reasoning-model",
    )

    _events = [event async for event in backend.events(handle)]

    assert "reasoning" not in bodies[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_type", ["response.failed", "response.incomplete"])
async def test_openai_api_chat_treats_provider_terminal_failures_as_failed(
    terminal_type: str,
) -> None:
    body = f'data: {{"type":"{terminal_type}"}}\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Fail truthfully",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key=f"api-{terminal_type}",
    )

    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "failed"]
    assert events[-1].payload == {"reason_code": "provider_request_failed"}


@pytest.mark.asyncio
async def test_api_chat_fails_when_stream_closes_without_terminal_marker() -> None:
    body = 'data: {"type":"response.output_text.delta","delta":"partial"}\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Do not accept partial output",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-incomplete-eof",
    )

    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "message", "failed"]
    assert events[-1].payload == {"reason_code": "provider_request_failed"}


@pytest.mark.asyncio
async def test_api_chat_keeps_text_from_a_terminal_provider_event() -> None:
    body = 'data: {"candidates":[{"content":{"parts":[{"text":"final"}]},"finishReason":"STOP"}]}\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    backend = ApiChatBackend(
        provider="gemini",
        credential="test-secret-value",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Keep the final chunk",
        model="gemini-2.5-pro",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-terminal-delta",
    )

    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "message", "completed"]
    assert events[1].payload == {"text": "final"}


@pytest.mark.asyncio
async def test_api_chat_cancel_before_events_does_not_open_provider_stream() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='data: {"type":"response.completed"}\n', request=request)

    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Do not send this prompt",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-preflight-cancel",
    )

    assert await backend.cancel(handle) is True
    events = [event async for event in backend.events(handle)]

    assert [event.type for event in events] == ["started", "cancelled"]
    assert requests == []


@pytest.mark.asyncio
async def test_api_chat_cancel_closes_a_stalled_provider_stream() -> None:
    class _StalledResponse:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        async def __aenter__(self) -> _StalledResponse:
            return self

        async def __aexit__(self, *_args: object) -> None:
            await self.aclose()

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self, *, chunk_size: int | None = None):
            del chunk_size
            self.started.set()
            await self.closed.wait()
            if False:
                yield b""

        async def aclose(self) -> None:
            self.closed.set()

    class _StalledClient:
        def __init__(self, response: _StalledResponse) -> None:
            self.response = response

        async def __aenter__(self) -> _StalledClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(self, *_args: object, **_kwargs: object) -> _StalledResponse:
            return self.response

    response = _StalledResponse()
    backend = ApiChatBackend(
        provider="openai",
        credential="sk-test-never-log",
        clock=lambda: NOW,
        http_client_factory=lambda: _StalledClient(response),  # type: ignore[arg-type]
    )
    handle = await backend.start(
        run_id=uuid4(),
        prompt="Wait for a slow response",
        model="gpt-5.6-sol",
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="api-cancel-stream",
    )
    events = backend.events(handle)
    assert (await anext(events)).type == "started"
    next_event = asyncio.create_task(anext(events))
    await response.started.wait()

    assert await backend.cancel(handle) is True

    assert (await asyncio.wait_for(next_event, timeout=0.2)).type == "cancelled"
    assert response.closed.is_set()
