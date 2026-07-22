from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

import httpx

from corvus.mvp.local_chat import (
    LocalChatBackendEvent,
    LocalChatBackendHandle,
)

ApiProvider = Literal[
    "openai", "anthropic", "gemini", "xai", "openrouter", "together",
    "fireworks", "groq", "mistral", "deepseek"
]
_HTTP_TIMEOUT_SECONDS = 120.0
_ABSOLUTE_DEADLINE_SECONDS = 180.0
_MAX_OUTPUT_BYTES = 100_000
_MAX_EVENT_OVERHEAD_BYTES = 65_536
_STREAM_CHUNK_BYTES = 16_384
_ENDPOINTS: dict[ApiProvider, str] = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "together": "https://api.together.xyz/v1/chat/completions",
    "fireworks": "https://api.fireworks.ai/inference/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}


class ApiChatError(RuntimeError):
    pass


class _ProviderStreamLimit(RuntimeError):
    pass


async def _bounded_response_lines(
    response: httpx.Response,
    *,
    max_line_bytes: int,
) -> AsyncIterator[str]:
    """Decode provider lines while keeping any incomplete frame strictly bounded."""

    buffered = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
        chunk_start = 0
        while chunk_start < len(chunk):
            newline = chunk.find(b"\n", chunk_start)
            chunk_end = len(chunk) if newline < 0 else newline
            buffered.extend(chunk[chunk_start:chunk_end])
            if len(buffered) > max_line_bytes:
                raise _ProviderStreamLimit("provider_event_limit")
            if newline < 0:
                break
            if buffered.endswith(b"\r"):
                del buffered[-1]
            yield buffered.decode("utf-8")
            buffered.clear()
            chunk_start = newline + 1
    if buffered:
        if buffered.endswith(b"\r"):
            del buffered[-1]
        yield buffered.decode("utf-8")


@dataclass(frozen=True, slots=True)
class _ApiRequest:
    prompt: str
    model: str
    effort: str


class ApiChatBackend:
    """Streaming Chat-only backend. It never receives filesystem or MCP authority."""

    def __init__(
        self,
        *,
        provider: ApiProvider,
        credential: str,
        clock: Callable[[], datetime],
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
        absolute_deadline_seconds: float = _ABSOLUTE_DEADLINE_SECONDS,
    ) -> None:
        if not credential.strip():
            raise ApiChatError("provider_credential_missing")
        self._provider = provider
        self._credential = credential.strip()
        self._clock = clock
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False)
        )
        if max_output_bytes < 1 or absolute_deadline_seconds <= 0:
            raise ValueError("provider_stream_limits_invalid")
        self._max_output_bytes = max_output_bytes
        self._absolute_deadline_seconds = absolute_deadline_seconds
        self._requests: dict[UUID, _ApiRequest] = {}
        self._cancelled: set[UUID] = set()
        self._active_responses: dict[UUID, httpx.Response] = {}

    def __repr__(self) -> str:
        return f"<ApiChatBackend provider={self._provider}>"

    async def start(
        self,
        *,
        run_id: UUID,
        prompt: str,
        model: str | None,
        effort: str,
        mode: str,
        mcp_enabled: bool,
        idempotency_key: str,
    ) -> LocalChatBackendHandle:
        del idempotency_key
        if mode != "chat" or mcp_enabled:
            raise ApiChatError("provider_mode_unavailable")
        if model is None or not model.strip():
            raise ApiChatError("provider_model_required")
        if self._provider == "openai":
            effort = "medium" if effort == "normal" else effort
            if effort not in {"low", "medium", "high", "xhigh"}:
                raise ApiChatError("provider_effort_unavailable")
        handle = LocalChatBackendHandle(id=run_id, run_id=run_id)
        self._requests[handle.id] = _ApiRequest(
            prompt=prompt,
            model=model.strip(),
            effort=effort,
        )
        return handle

    async def events(
        self,
        handle: LocalChatBackendHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[LocalChatBackendEvent]:
        request = self._requests.get(handle.id)
        if request is None:
            raise ApiChatError("provider_handle_unknown")
        sequence = 1
        if sequence > after_sequence:
            yield LocalChatBackendEvent(sequence, self._clock(), "started", {"status": "started"})
        if handle.id in self._cancelled:
            sequence += 1
            if sequence > after_sequence:
                yield LocalChatBackendEvent(
                    sequence, self._clock(), "cancelled", {"status": "cancelled"}
                )
            return
        request_failed = False
        failure_reason = "provider_request_failed"
        stream_completed = False
        output_bytes = 0
        deadline = asyncio.get_running_loop().time() + self._absolute_deadline_seconds
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout_at(deadline):
                    client = await stack.enter_async_context(self._http_client_factory())
                    response = await stack.enter_async_context(
                        client.stream(
                            "POST",
                            self._url(request.model),
                            headers=self._headers(),
                            json=self._body(request),
                        )
                    )
                    response.raise_for_status()
                self._active_responses[handle.id] = response
                try:
                    lines = aiter(
                        _bounded_response_lines(
                            response,
                            max_line_bytes=(self._max_output_bytes + _MAX_EVENT_OVERHEAD_BYTES),
                        )
                    )
                    while handle.id not in self._cancelled:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError
                        try:
                            async with asyncio.timeout_at(deadline):
                                line = await anext(lines)
                        except StopAsyncIteration:
                            break
                        terminal_state = self._terminal_state(line)
                        if terminal_state == "failed":
                            request_failed = True
                            break
                        usage = self._usage(line)
                        if usage:
                            sequence += 1
                            if sequence > after_sequence:
                                usage_payload: dict[str, object] = dict(usage)
                                yield LocalChatBackendEvent(
                                    sequence, self._clock(), "usage", usage_payload
                                )
                        text = self._text_delta(line)
                        if text:
                            output_bytes += len(text.encode("utf-8"))
                            if output_bytes > self._max_output_bytes:
                                request_failed = True
                                failure_reason = "provider_output_limit"
                                break
                            sequence += 1
                            if sequence > after_sequence:
                                yield LocalChatBackendEvent(
                                    sequence, self._clock(), "message", {"text": text}
                                )
                        if terminal_state == "completed":
                            stream_completed = True
                            break
                finally:
                    self._active_responses.pop(handle.id, None)
        except TimeoutError:
            request_failed = True
            failure_reason = "provider_deadline_exceeded"
        except _ProviderStreamLimit:
            request_failed = True
            failure_reason = "provider_output_limit"
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            request_failed = True
        if handle.id in self._cancelled:
            sequence += 1
            if sequence > after_sequence:
                yield LocalChatBackendEvent(
                    sequence, self._clock(), "cancelled", {"status": "cancelled"}
                )
            return
        if request_failed or not stream_completed:
            sequence += 1
            if sequence > after_sequence:
                yield LocalChatBackendEvent(
                    sequence,
                    self._clock(),
                    "failed",
                    {"reason_code": failure_reason},
                )
            return
        sequence += 1
        if sequence > after_sequence:
            yield LocalChatBackendEvent(
                sequence, self._clock(), "completed", {"status": "completed"}
            )

    async def cancel(self, handle: LocalChatBackendHandle) -> bool:
        if handle.id not in self._requests:
            raise ApiChatError("provider_handle_unknown")
        self._cancelled.add(handle.id)
        response = self._active_responses.get(handle.id)
        if response is not None:
            await response.aclose()
        return True

    def artifact(self, handle: LocalChatBackendHandle) -> None:
        if handle.id not in self._requests:
            raise ApiChatError("provider_handle_unknown")
        return None

    def _url(self, model: str) -> str:
        if self._provider == "gemini":
            return f"{_ENDPOINTS['gemini']}/models/{model}:streamGenerateContent?alt=sse"
        return _ENDPOINTS[self._provider]

    def _headers(self) -> dict[str, str]:
        if self._provider == "anthropic":
            return {
                "x-api-key": self._credential,
                "anthropic-version": "2023-06-01",
                "Accept": "text/event-stream",
            }
        if self._provider == "gemini":
            return {"x-goog-api-key": self._credential, "Accept": "text/event-stream"}
        return {
            "Authorization": f"Bearer {self._credential}",
            "Accept": "text/event-stream",
        }

    def _body(self, request: _ApiRequest) -> dict[str, object]:
        if self._provider == "openai":
            body: dict[str, object] = {
                "model": request.model,
                "input": request.prompt,
                "stream": True,
            }
            if _supports_openai_reasoning_effort(request.model):
                body["reasoning"] = {"effort": request.effort}
            return body
        if self._provider == "anthropic":
            return {
                "model": request.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": request.prompt}],
                "stream": True,
            }
        if self._provider == "gemini":
            return {"contents": [{"role": "user", "parts": [{"text": request.prompt}]}]}
        return {
            "model": request.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": True,
        }

    def _text_delta(self, line: str) -> str | None:
        if not line.startswith("data:"):
            return None
        raw = line.removeprefix("data:").strip()
        if not raw or raw == "[DONE]":
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if self._provider == "openai":
            return (
                cast(str | None, payload.get("delta"))
                if payload.get("type") == "response.output_text.delta"
                else None
            )
        if self._provider == "anthropic":
            delta = payload.get("delta")
            return cast(str | None, delta.get("text")) if isinstance(delta, dict) else None
        if self._provider == "gemini":
            return _gemini_text(payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        delta = choices[0].get("delta")
        return cast(str | None, delta.get("content")) if isinstance(delta, dict) else None

    def _terminal_state(self, line: str) -> Literal["completed", "failed"] | None:
        if not line.startswith("data:"):
            return None
        raw = line.removeprefix("data:").strip()
        if not raw:
            return None
        if raw == "[DONE]":
            return "completed" if self._provider in {"xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"} else None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if self._provider == "openai":
            event_type = payload.get("type")
            if event_type == "response.completed":
                return "completed"
            if event_type in {"response.failed", "response.incomplete"}:
                return "failed"
            return None
        if self._provider == "anthropic":
            event_type = payload.get("type")
            if event_type == "message_stop":
                return "completed"
            return "failed" if event_type == "error" else None
        if self._provider == "gemini":
            candidates = payload.get("candidates")
            if (
                not isinstance(candidates, list)
                or not candidates
                or not isinstance(candidates[0], dict)
            ):
                return None
            reason = candidates[0].get("finishReason")
            if not isinstance(reason, str) or not reason:
                return None
            return "completed" if reason == "STOP" else "failed"
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        reason = choices[0].get("finish_reason")
        if not isinstance(reason, str) or not reason:
            return None
        return "completed" if reason == "stop" else "failed"

    def _usage(self, line: str) -> dict[str, int] | None:
        if not line.startswith("data:"):
            return None
        raw = line.removeprefix("data:").strip()
        if not raw or raw == "[DONE]":
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if self._provider == "openai":
            response = payload.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            if not isinstance(usage, dict):
                return None
            details = usage.get("input_tokens_details")
            return _normalized_usage(
                input_tokens=usage.get("input_tokens"),
                cached_input_tokens=details.get("cached_tokens")
                if isinstance(details, dict)
                else None,
                output_tokens=usage.get("output_tokens"),
            )
        if self._provider == "anthropic":
            message = payload.get("message")
            usage = message.get("usage") if isinstance(message, dict) else payload.get("usage")
            if not isinstance(usage, dict):
                return None
            return _normalized_usage(
                input_tokens=usage.get("input_tokens"),
                cached_input_tokens=usage.get("cache_read_input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        if self._provider == "gemini":
            usage = payload.get("usageMetadata")
            if not isinstance(usage, dict):
                return None
            return _normalized_usage(
                input_tokens=usage.get("promptTokenCount"),
                cached_input_tokens=usage.get("cachedContentTokenCount"),
                output_tokens=usage.get("candidatesTokenCount"),
            )
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        details = usage.get("prompt_tokens_details")
        return _normalized_usage(
            input_tokens=usage.get("prompt_tokens"),
            cached_input_tokens=details.get("cached_tokens") if isinstance(details, dict) else None,
            output_tokens=usage.get("completion_tokens"),
        )


def _supports_openai_reasoning_effort(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith(("gpt-5", "gpt-oss-", "codex-")) or bool(
        re.fullmatch(r"o\d+(?:[-.].+)?", normalized)
    )


def _normalized_usage(
    *, input_tokens: object, cached_input_tokens: object, output_tokens: object
) -> dict[str, int] | None:
    normalized = {
        key: value
        for key, value in (
            ("input_tokens", input_tokens),
            ("cached_input_tokens", cached_input_tokens),
            ("output_tokens", output_tokens),
        )
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    return normalized or None


def _gemini_text(payload: dict[str, Any]) -> str | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return None
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    text = parts[0].get("text")
    return text if isinstance(text, str) else None
