from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from corvus.application.ports import AgentRuntimePort
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    AgentRunState,
    CancellationResult,
    CapabilitySupport,
    ExecutableIdentity,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderFamily,
    ProviderHealth,
    ProviderStatus,
    ProviderTransport,
    compute_agent_run_event_digest,
    compute_provider_binding_digest,
)
from corvus.infrastructure.agent_runtimes.process_session import (
    ProcessInvocation,
    ProcessSession,
    ProcessSessionError,
    ProcessSessionEvent,
    ProcessSessionEventKind,
    ProcessSessionLimits,
)
from corvus.safe_process import path_is_link_or_reparse

_FIRST_EVENT_DIGEST = "0" * 64
_DEFAULT_MODEL = "sonnet"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_STDERR_BYTES = 64_000
_MAX_FRAME_BYTES = 1_000_000
_MAX_FRAMES = 10_000
_SAFE_STATUS_VALUES = frozenset({"idle", "requesting", "responding", "thinking", "working"})
_SAFE_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "cache_read_input_tokens"})

ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]


class ClaudeAdapterError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class LocalClaudeTextRequest:
    run_id: UUID
    prompt: str
    idempotency_key: str
    deadline: datetime
    model: str = _DEFAULT_MODEL
    effort: ClaudeEffort = "medium"
    max_output_bytes: int = 100_000
    workspace: Path | None = None


class _ProcessSessionLike(Protocol):
    def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]: ...

    async def cancel(self) -> bool: ...


SessionStarter = Callable[[ProcessInvocation], Awaitable[_ProcessSessionLike]]


async def _start_process_session(invocation: ProcessInvocation) -> _ProcessSessionLike:
    return await ProcessSession.start(invocation)


class _RunSession:
    def __init__(
        self,
        process: _ProcessSessionLike,
        *,
        handle: AgentRunHandle,
        idempotency_key: str,
    ) -> None:
        self.process = process
        self.handle = handle
        self.idempotency_key = idempotency_key
        self.events: list[AgentRunEvent] = []
        self.lock = asyncio.Lock()
        self.terminal_state: AgentRunState | None = None
        self.process_sequence = 0
        self.stream_complete = False


class ClaudeCliAdapter(AgentRuntimePort):
    """Text-only adapter for a pinned, locally authenticated Claude CLI."""

    def __init__(
        self,
        *,
        executable: Path,
        version: str,
        scratch_root: Path,
        clock: Callable[[], datetime],
        session_starter: SessionStarter = _start_process_session,
    ) -> None:
        self._executable = executable.resolve(strict=False)
        self._version = version
        self._scratch_root = scratch_root.resolve(strict=False)
        self._clock = clock
        self._session_starter = session_starter
        self._bindings: dict[UUID, ProviderBinding] = {}
        self._sessions: dict[UUID, _RunSession] = {}
        self._idempotency_handles: dict[str, AgentRunHandle] = {}

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        if not self._executable.is_file():
            return ()
        executable_digest = _sha256_file(self._executable)
        identity = ExecutableIdentity(
            executable_path=self._executable,
            version=self._version,
            sha256_digest=executable_digest,
        )
        binding_id = uuid5(
            NAMESPACE_URL,
            "corvus:claude:"
            f"{query.workspace_id}:{query.project_id or 'workspace'}:"
            f"{self._executable}:{executable_digest}",
        )
        binding = ProviderBinding(
            id=binding_id,
            workspace_id=query.workspace_id,
            project_id=query.project_id,
            family=ProviderFamily.CLAUDE,
            transport=ProviderTransport.LOCAL_CLI,
            status=ProviderStatus.AVAILABLE,
            executable_identity=identity,
            model=_DEFAULT_MODEL,
            capabilities=_text_only_capabilities(),
            health_checked_at=self._clock(),
            version=1,
            data_egress_disclosure="Prompts are sent through the user's local Claude login.",
            server_storage_disclosure="Anthropic retention follows the user's Claude account policy.",
        )
        self._bindings[binding.id] = binding
        return (
            ProviderCandidate(
                binding=binding,
                binding_version=binding.version,
                binding_digest=compute_provider_binding_digest(binding),
            ),
        )

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        self._require_binding(binding)
        return _text_only_capabilities()

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        self._require_binding(binding)
        status = ProviderStatus.AVAILABLE
        identity = binding.executable_identity
        if identity is None or not self._executable.is_file():
            status = ProviderStatus.UNAVAILABLE
        elif _sha256_file(self._executable) != identity.sha256_digest:
            status = ProviderStatus.UNHEALTHY
        return ProviderHealth(
            binding_id=binding.id,
            binding_version=binding.version,
            binding_digest=compute_provider_binding_digest(binding),
            status=status,
            observed_at=self._clock(),
        )

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        binding = self._binding_for_request(request)
        prompt = request.prompt or "\n".join(request.messages or ())
        return await self._start_text(
            binding=binding,
            request=LocalClaudeTextRequest(
                run_id=request.run_id,
                prompt=prompt,
                idempotency_key=request.idempotency_key,
                deadline=request.deadline,
                model=request.model or _DEFAULT_MODEL,
                effort=_normalize_effort(request.effort),
                max_output_bytes=request.max_output_bytes,
            ),
        )

    async def start_local_text(
        self,
        binding: ProviderBinding,
        request: LocalClaudeTextRequest,
    ) -> AgentRunStartResult:
        self._require_binding(binding)
        return await self._start_text(binding=binding, request=request)

    async def _start_text(
        self,
        *,
        binding: ProviderBinding,
        request: LocalClaudeTextRequest,
    ) -> AgentRunStartResult:
        replay = self._idempotency_handles.get(request.idempotency_key)
        if replay is not None:
            return AgentRunStartResult(handle=replay, replayed=True)
        if not request.prompt:
            raise ClaudeAdapterError("claude_prompt_required")
        if _MODEL_PATTERN.fullmatch(request.model) is None:
            raise ClaudeAdapterError("claude_model_invalid")
        if request.workspace is None:
            scratch = self._scratch_root / str(request.run_id)
            scratch.mkdir(parents=True, exist_ok=False)
        else:
            try:
                scratch = request.workspace.resolve(strict=True)
                approved_root = self._scratch_root.resolve(strict=True)
            except OSError as error:
                raise ClaudeAdapterError("claude_workspace_unavailable") from error
            if (
                not scratch.is_dir()
                or path_is_link_or_reparse(scratch)
                or not scratch.is_relative_to(approved_root)
                or scratch == approved_root
            ):
                raise ClaudeAdapterError("claude_workspace_unavailable")
        arguments = (
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
            request.effort,
            "--model",
            request.model,
            request.prompt,
        )
        limits = ProcessSessionLimits(
            max_stdout_bytes=request.max_output_bytes,
            max_stderr_bytes=min(request.max_output_bytes, _MAX_STDERR_BYTES),
            max_frame_bytes=min(request.max_output_bytes, _MAX_FRAME_BYTES),
            max_frames=_MAX_FRAMES,
            max_events=_MAX_FRAMES + 1,
            timeout_seconds=min(
                _MAX_TIMEOUT_SECONDS,
                max(1.0, (request.deadline - self._clock()).total_seconds()),
            ),
        )
        identity = binding.executable_identity
        if identity is None:
            raise ClaudeAdapterError("claude_binding_identity_missing")
        invocation = ProcessInvocation(
            executable=self._executable,
            executable_sha256=identity.sha256_digest,
            arguments=arguments,
            cwd=scratch,
            approved_roots=(self._scratch_root,),
            environment=MappingProxyType({"NO_COLOR": "1"}),
            limits=limits,
        )
        try:
            process = await self._session_starter(invocation)
        except ProcessSessionError as error:
            raise ClaudeAdapterError("claude_process_unavailable") from error
        handle = AgentRunHandle(
            run_id=request.run_id,
            provider_binding_id=binding.id,
            created_at=self._clock(),
            state=AgentRunState.RUNNING,
        )
        self._sessions[handle.id] = _RunSession(
            process,
            handle=handle,
            idempotency_key=request.idempotency_key,
        )
        self._idempotency_handles[request.idempotency_key] = handle
        return AgentRunStartResult(handle=handle, replayed=False)

    async def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        session = self._require_session(handle)
        async with session.lock:
            for event in session.events:
                if event.sequence > after_sequence:
                    yield event
            if session.stream_complete:
                return
            async for event in self._stream_events(handle, session):
                session.events.append(event)
                if event.sequence > after_sequence:
                    yield event
            session.stream_complete = True

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        del current_kill_switch_proof_id, current_kill_switch_proof_digest
        return await self.cancel_local(handle)

    async def cancel_local(self, handle: AgentRunHandle) -> CancellationResult:
        session = self._require_session(handle)
        if session.terminal_state is not None:
            terminal_handle = handle.model_copy(update={"state": session.terminal_state})
            return CancellationResult(
                handle_id=handle.id,
                handle=terminal_handle,
                accepted=False,
                terminal=True,
                reason_code="agent_run_already_terminal",
                timestamp=self._clock(),
            )
        await session.process.cancel()
        session.terminal_state = AgentRunState.CANCELLED
        cancelled_handle = handle.model_copy(update={"state": AgentRunState.CANCELLED})
        return CancellationResult(
            handle_id=handle.id,
            handle=cancelled_handle,
            accepted=True,
            terminal=True,
            reason_code="agent_run_cancelled",
            timestamp=self._clock(),
        )

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        del handle, request_with_fresh_proofs
        raise ClaudeAdapterError("claude_resume_unsupported")

    def _binding_for_request(self, request: AgentRunRequest) -> ProviderBinding:
        binding = self._bindings.get(request.provider_binding_id)
        if binding is None:
            raise ClaudeAdapterError("claude_binding_unknown")
        if compute_provider_binding_digest(binding) != request.provider_binding_digest:
            raise ClaudeAdapterError("claude_binding_digest_mismatch")
        return binding

    def _require_binding(self, binding: ProviderBinding) -> ProviderBinding:
        known = self._bindings.get(binding.id)
        if known is None or known != binding:
            raise ClaudeAdapterError("claude_binding_unknown")
        return known

    def _require_session(self, handle: AgentRunHandle) -> _RunSession:
        session = self._sessions.get(handle.id)
        if (
            session is None
            or handle.run_id != session.handle.run_id
            or handle.provider_binding_id != session.handle.provider_binding_id
        ):
            raise ClaudeAdapterError("claude_run_unknown")
        return session

    async def _stream_events(
        self,
        handle: AgentRunHandle,
        session: _RunSession,
    ) -> AsyncIterator[AgentRunEvent]:
        terminal = False
        started = bool(session.events)

        def event(event_type: AgentRunEventType, payload: dict[str, JsonValue]) -> AgentRunEvent:
            previous = session.events[-1].event_digest if session.events else _FIRST_EVENT_DIGEST
            sequence = len(session.events) + 1
            timestamp = self._clock()
            digest = compute_agent_run_event_digest(
                run_id=handle.run_id,
                handle_id=handle.id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=event_type,
                redacted_payload=payload,
                provider_event_id=None,
                previous_event_digest=previous,
                tool_call_id=None,
                effect_authorization_decision_id=None,
                effect_authorization_decision_digest=None,
            )
            return AgentRunEvent(
                run_id=handle.run_id,
                handle_id=handle.id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=event_type,
                redacted_payload=payload,
                previous_event_digest=previous,
                event_digest=digest,
            )

        async for process_event in session.process.events(session.process_sequence):
            session.process_sequence = max(session.process_sequence, process_event.sequence)
            if terminal:
                continue
            if (
                process_event.kind == ProcessSessionEventKind.FRAME
                and process_event.frame is not None
            ):
                if not started:
                    started_event = event(AgentRunEventType.STARTED, {"status": "started"})
                    started = True
                    yield started_event
                frame = process_event.frame
                frame_type = frame.get("type")
                if frame_type == "system" and frame.get("subtype") == "status":
                    status = frame.get("status")
                    if isinstance(status, str) and status in _SAFE_STATUS_VALUES:
                        yield event(
                            AgentRunEventType.CHECKPOINT,
                            {"activity": "provider", "status": status},
                        )
                elif frame_type == "stream_event":
                    provider_event = frame.get("event")
                    if isinstance(provider_event, Mapping):
                        delta = provider_event.get("delta")
                        if isinstance(delta, Mapping):
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    yield event(AgentRunEventType.MESSAGE_DELTA, {"text": text})
                            elif delta_type == "thinking_delta":
                                yield event(
                                    AgentRunEventType.CHECKPOINT,
                                    {"activity": "provider", "status": "thinking"},
                                )
                elif frame_type == "result":
                    if frame.get("is_error") is True or frame.get("subtype") != "success":
                        yield event(
                            AgentRunEventType.FAILED,
                            {"reason_code": "claude_process_failed"},
                        )
                        session.terminal_state = AgentRunState.FAILED
                        terminal = True
                        continue
                    usage = frame.get("usage")
                    if isinstance(usage, Mapping):
                        safe_usage: dict[str, JsonValue] = {
                            key: value
                            for key, value in usage.items()
                            if key in _SAFE_USAGE_FIELDS
                            and isinstance(value, int)
                            and not isinstance(value, bool)
                            and value >= 0
                        }
                        if safe_usage:
                            yield event(AgentRunEventType.USAGE, safe_usage)
                    yield event(AgentRunEventType.COMPLETED, {"status": "completed"})
                    session.terminal_state = AgentRunState.COMPLETED
                    terminal = True
            elif process_event.kind == ProcessSessionEventKind.CANCELLED:
                yield event(AgentRunEventType.CANCELLED, {"reason_code": "agent_run_cancelled"})
                session.terminal_state = AgentRunState.CANCELLED
                terminal = True
            elif process_event.kind == ProcessSessionEventKind.FAILED:
                yield event(
                    AgentRunEventType.FAILED,
                    {"reason_code": process_event.reason_code or "claude_process_failed"},
                )
                session.terminal_state = AgentRunState.FAILED
                terminal = True
            elif process_event.kind == ProcessSessionEventKind.EXITED and not terminal:
                state = (
                    AgentRunState.COMPLETED
                    if process_event.return_code == 0
                    else AgentRunState.FAILED
                )
                event_type = (
                    AgentRunEventType.COMPLETED
                    if state is AgentRunState.COMPLETED
                    else AgentRunEventType.FAILED
                )
                payload: dict[str, JsonValue] = (
                    {"status": "completed"}
                    if state is AgentRunState.COMPLETED
                    else {"reason_code": "claude_process_failed"}
                )
                yield event(event_type, payload)
                session.terminal_state = state
                terminal = True
        if not terminal:
            yield event(AgentRunEventType.FAILED, {"reason_code": "claude_stream_incomplete"})
            session.terminal_state = AgentRunState.FAILED


def _normalize_effort(value: str) -> ClaudeEffort:
    if value in {"low", "medium", "high", "xhigh", "max"}:
        return value  # type: ignore[return-value]
    if value == "normal":
        return "medium"
    return "medium"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_only_capabilities() -> AgentCapabilities:
    # AgentCapabilities.shell is metadata; this is not a process-spawning call.
    return AgentCapabilities(  # nosec B604
        text=CapabilitySupport.SUPPORTED,
        structured_output=CapabilitySupport.UNSUPPORTED,
        streaming=CapabilitySupport.SUPPORTED,
        images=CapabilitySupport.UNSUPPORTED,
        tools=CapabilitySupport.UNSUPPORTED,
        repository_read=CapabilitySupport.UNSUPPORTED,
        repository_write=CapabilitySupport.UNSUPPORTED,
        shell=CapabilitySupport.UNSUPPORTED,
        mcp=CapabilitySupport.UNSUPPORTED,
        session_resume=CapabilitySupport.SUPPORTED,
        usage_cost_reporting=CapabilitySupport.UNVERIFIED,
        provider_side_budget=CapabilitySupport.UNSUPPORTED,
        provider_side_cancellation=CapabilitySupport.SUPPORTED,
    )
