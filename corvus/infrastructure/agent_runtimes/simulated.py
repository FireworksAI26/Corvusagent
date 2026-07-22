from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from pydantic import JsonValue

from corvus.domain.agent_runtime import (
    GENESIS_EVENT_DIGEST,
    AgentCapabilities,
    AgentRunEvent,
    AgentRunEventChainError,
    AgentRunEventType,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    AgentRunState,
    CancellationResult,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderHealth,
    ProviderStatus,
    compute_agent_run_event_digest,
    compute_agent_run_request_digest,
    compute_provider_binding_digest,
    validate_agent_run_event_chain,
)

_SIMULATED_HANDLE_NAMESPACE = UUID("895c0f76-81a2-4f35-bd94-df91f86d266e")
_PROOF_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_EVENT_STATES = {
    AgentRunEventType.COMPLETED: AgentRunState.COMPLETED,
    AgentRunEventType.FAILED: AgentRunState.FAILED,
    AgentRunEventType.CANCELLED: AgentRunState.CANCELLED,
}


class AgentRuntimeError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


@dataclass(frozen=True)
class SimulatedEventTemplate:
    event_type: AgentRunEventType
    redacted_payload: Mapping[str, JsonValue]
    provider_event_id: str | None = None
    tool_call_id: str | None = None
    effect_authorization_decision_id: UUID | None = None
    effect_authorization_decision_digest: str | None = None


@dataclass
class _RunRecord:
    handle: AgentRunHandle
    events: list[AgentRunEvent]
    request_digest: str
    immutable_request_digest: str
    cancellation_result: CancellationResult | None = None
    cancellation_proof: tuple[UUID, str] | None = None


class SimulatedAgentRuntime:
    def __init__(
        self,
        *,
        bindings: tuple[ProviderBinding, ...],
        event_templates: Mapping[UUID, tuple[SimulatedEventTemplate, ...]],
    ) -> None:
        if len({binding.id for binding in bindings}) != len(bindings):
            raise AgentRuntimeError(
                "duplicate_provider_binding",
                "provider binding identities must be unique",
            )
        self._bindings = {
            binding.id: binding for binding in sorted(bindings, key=lambda item: str(item.id))
        }
        unknown_template_ids = set(event_templates) - self._bindings.keys()
        if unknown_template_ids:
            raise AgentRuntimeError(
                "unknown_event_template_binding",
                "event template references an unknown provider binding",
            )
        self._event_templates = {
            binding_id: tuple(self._snapshot_template(template) for template in templates)
            for binding_id, templates in event_templates.items()
        }
        self._runs: dict[UUID, _RunRecord] = {}
        self._run_handle_ids: dict[tuple[UUID, UUID], UUID] = {}

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        return tuple(
            ProviderCandidate(
                binding=binding,
                binding_version=binding.version,
                binding_digest=compute_provider_binding_digest(binding),
            )
            for binding in self._bindings.values()
            if binding.workspace_id == query.workspace_id and binding.project_id == query.project_id
        )

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        return self._known_binding(binding).capabilities

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        known = self._known_binding(binding)
        return ProviderHealth(
            binding_id=known.id,
            binding_version=known.version,
            binding_digest=compute_provider_binding_digest(known),
            status=known.status,
            observed_at=known.health_checked_at,
        )

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        binding = self._binding_for_start(request)
        request_digest = compute_agent_run_request_digest(request)
        handle_id = uuid5(
            _SIMULATED_HANDLE_NAMESPACE,
            f"{request.run_id}:{request.provider_binding_id}:{request.idempotency_key}",
        )
        run_identity = (request.run_id, binding.id)
        existing_handle_id = self._run_handle_ids.get(run_identity)
        existing = self._runs.get(existing_handle_id) if existing_handle_id is not None else None
        if existing is not None:
            if existing.request_digest != request_digest:
                raise AgentRuntimeError(
                    "agent_run_idempotency_mismatch",
                    "idempotent start replay does not match the original request",
                )
            return AgentRunStartResult(handle=existing.handle, replayed=True)

        events, state = self._materialize_events(
            request=request,
            handle_id=handle_id,
            binding=binding,
        )
        handle = AgentRunHandle(
            id=handle_id,
            run_id=request.run_id,
            provider_binding_id=binding.id,
            created_at=binding.health_checked_at,
            provider_session_ref=f"simulated-session:{handle_id}",
            state=state,
        )
        self._runs[handle.id] = _RunRecord(
            handle=handle,
            events=events,
            request_digest=request_digest,
            immutable_request_digest=request.immutable_request_digest,
        )
        self._run_handle_ids[run_identity] = handle.id
        return AgentRunStartResult(handle=handle, replayed=False)

    async def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        if after_sequence < 0:
            raise AgentRuntimeError(
                "invalid_event_sequence_cursor",
                "event sequence cursor cannot be negative",
            )
        record = self._known_record(handle)
        if after_sequence > len(record.events):
            raise AgentRuntimeError(
                "invalid_event_sequence_cursor",
                "event sequence cursor exceeds the contiguous stream",
            )
        next_event_index = after_sequence
        while next_event_index < len(record.events):
            yield record.events[next_event_index]
            next_event_index += 1

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        if not isinstance(current_kill_switch_proof_id, UUID) or (
            not isinstance(current_kill_switch_proof_digest, str)
            or _PROOF_DIGEST_PATTERN.fullmatch(current_kill_switch_proof_digest) is None
        ):
            raise AgentRuntimeError(
                "current_kill_switch_proof_required",
                "cancellation requires a current kill-switch proof reference and digest",
            )
        record = self._known_record(handle)
        if record.cancellation_result is not None:
            if record.cancellation_proof != (
                current_kill_switch_proof_id,
                current_kill_switch_proof_digest,
            ):
                raise AgentRuntimeError(
                    "current_kill_switch_proof_mismatch",
                    "cancellation replay must use the original kill-switch proof",
                )
            return record.cancellation_result

        if record.handle.state is not AgentRunState.RUNNING:
            timestamp = self._next_event_timestamp(record)
            result = CancellationResult(
                handle_id=record.handle.id,
                handle=record.handle,
                accepted=False,
                terminal=True,
                reason_code="agent_run_already_terminal",
                timestamp=timestamp,
            )
            record.cancellation_result = result
            record.cancellation_proof = (
                current_kill_switch_proof_id,
                current_kill_switch_proof_digest,
            )
            return result

        self._close_open_tool_calls(
            record,
            current_kill_switch_proof_id,
            current_kill_switch_proof_digest,
        )
        payload: dict[str, JsonValue] = {
            "current_kill_switch_proof_id": str(current_kill_switch_proof_id),
            "current_kill_switch_proof_digest": current_kill_switch_proof_digest,
        }
        cancelled_event = self._append_event(
            record,
            event_type=AgentRunEventType.CANCELLED,
            payload=payload,
        )
        record.handle = record.handle.model_copy(update={"state": AgentRunState.CANCELLED})
        result = CancellationResult(
            handle_id=record.handle.id,
            handle=record.handle,
            accepted=True,
            terminal=True,
            reason_code="agent_run_cancelled",
            timestamp=cancelled_event.timestamp,
        )
        record.cancellation_result = result
        record.cancellation_proof = (
            current_kill_switch_proof_id,
            current_kill_switch_proof_digest,
        )
        return result

    def _close_open_tool_calls(
        self,
        record: _RunRecord,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> None:
        open_tools: dict[str, tuple[UUID, str, bool]] = {}
        for event in record.events:
            tool_call_id = event.tool_call_id
            if event.event_type is AgentRunEventType.TOOL_REQUESTED and tool_call_id is not None:
                decision_id = event.effect_authorization_decision_id
                decision_digest = event.effect_authorization_decision_digest
                if decision_id is None or decision_digest is None:
                    raise AgentRuntimeError(
                        "effect_authorization_decision_required",
                        "open tool call lacks its authorization decision",
                    )
                open_tools[tool_call_id] = (decision_id, decision_digest, False)
            elif event.event_type is AgentRunEventType.TOOL_STARTED and tool_call_id is not None:
                open_tool = open_tools.get(tool_call_id)
                if open_tool is not None:
                    open_tools[tool_call_id] = (*open_tool[:2], True)
            elif (
                event.event_type
                in {
                    AgentRunEventType.TOOL_BLOCKED,
                    AgentRunEventType.TOOL_RESULT,
                }
                and tool_call_id is not None
            ):
                open_tools.pop(tool_call_id, None)
        for tool_call_id in sorted(open_tools):
            decision_id, decision_digest, tool_started = open_tools[tool_call_id]
            self._append_event(
                record,
                event_type=(
                    AgentRunEventType.TOOL_RESULT
                    if tool_started
                    else AgentRunEventType.TOOL_BLOCKED
                ),
                payload={
                    "current_kill_switch_proof_id": str(current_kill_switch_proof_id),
                    "current_kill_switch_proof_digest": current_kill_switch_proof_digest,
                    "reason": "agent_run_cancelled",
                    "status": "cancelled" if tool_started else "blocked",
                },
                tool_call_id=tool_call_id,
                effect_authorization_decision_id=decision_id,
                effect_authorization_decision_digest=decision_digest,
            )

    def _append_event(
        self,
        record: _RunRecord,
        *,
        event_type: AgentRunEventType,
        payload: dict[str, JsonValue],
        tool_call_id: str | None = None,
        effect_authorization_decision_id: UUID | None = None,
        effect_authorization_decision_digest: str | None = None,
    ) -> AgentRunEvent:
        sequence = len(record.events) + 1
        timestamp = self._next_event_timestamp(record)
        previous_digest = record.events[-1].event_digest if record.events else GENESIS_EVENT_DIGEST
        event_digest = compute_agent_run_event_digest(
            run_id=record.handle.run_id,
            handle_id=record.handle.id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            redacted_payload=payload,
            provider_event_id=None,
            previous_event_digest=previous_digest,
            tool_call_id=tool_call_id,
            effect_authorization_decision_id=effect_authorization_decision_id,
            effect_authorization_decision_digest=effect_authorization_decision_digest,
        )
        event = AgentRunEvent(
            run_id=record.handle.run_id,
            handle_id=record.handle.id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            redacted_payload=payload,
            tool_call_id=tool_call_id,
            effect_authorization_decision_id=effect_authorization_decision_id,
            effect_authorization_decision_digest=effect_authorization_decision_digest,
            previous_event_digest=previous_digest,
            event_digest=event_digest,
        )
        record.events.append(event)
        return event

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        record = self._known_record(handle)
        if request_with_fresh_proofs.run_id != record.handle.run_id:
            raise AgentRuntimeError(
                "resume_run_substitution",
                "resume request must preserve the run identity",
            )
        if request_with_fresh_proofs.provider_binding_id != record.handle.provider_binding_id:
            raise AgentRuntimeError(
                "resume_provider_binding_substitution",
                "resume request must preserve the provider binding identity",
            )
        if request_with_fresh_proofs.resume_handle_id != record.handle.id:
            raise AgentRuntimeError(
                "resume_handle_substitution",
                "resume request must reference the current handle",
            )
        if request_with_fresh_proofs.immutable_request_digest != record.immutable_request_digest:
            raise AgentRuntimeError(
                "resume_request_substitution",
                "resume may refresh proofs only",
            )
        if record.handle.state is not AgentRunState.RUNNING:
            raise AgentRuntimeError(
                "agent_run_terminal",
                "terminal agent runs cannot be resumed",
            )
        self._validate_binding_scope(
            request_with_fresh_proofs,
            self._bindings[record.handle.provider_binding_id],
        )
        return record.handle

    def _known_binding(self, binding: ProviderBinding) -> ProviderBinding:
        known = self._bindings.get(binding.id)
        if known is None:
            raise AgentRuntimeError(
                "unknown_provider_binding",
                "provider binding is not registered",
            )
        if compute_provider_binding_digest(known) != compute_provider_binding_digest(binding):
            raise AgentRuntimeError(
                "provider_binding_substitution",
                "provider binding fields do not match the registered binding",
            )
        return known

    @staticmethod
    def _snapshot_template(template: SimulatedEventTemplate) -> SimulatedEventTemplate:
        return SimulatedEventTemplate(
            event_type=template.event_type,
            redacted_payload=deepcopy(dict(template.redacted_payload)),
            provider_event_id=template.provider_event_id,
            tool_call_id=template.tool_call_id,
            effect_authorization_decision_id=(template.effect_authorization_decision_id),
            effect_authorization_decision_digest=(template.effect_authorization_decision_digest),
        )

    def _binding_for_start(self, request: AgentRunRequest) -> ProviderBinding:
        binding = self._bindings.get(request.provider_binding_id)
        if binding is None:
            raise AgentRuntimeError(
                "unknown_provider_binding",
                "provider binding is not registered",
            )
        if binding.status is not ProviderStatus.AVAILABLE:
            raise AgentRuntimeError(
                "provider_binding_unavailable",
                "provider binding is not available",
            )
        if request.model != binding.model:
            raise AgentRuntimeError(
                "provider_binding_model_mismatch",
                "requested model does not match the provider binding",
            )
        if (
            request.provider_binding_version != binding.version
            or request.provider_binding_digest != compute_provider_binding_digest(binding)
        ):
            raise AgentRuntimeError(
                "provider_binding_digest_mismatch",
                "requested provider binding receipt is stale or substituted",
            )
        self._validate_binding_scope(request, binding)
        return binding

    @staticmethod
    def _validate_binding_scope(request: AgentRunRequest, binding: ProviderBinding) -> None:
        if request.workspace_id != binding.workspace_id:
            raise AgentRuntimeError(
                "provider_binding_workspace_mismatch",
                "provider binding belongs to another workspace",
            )
        if request.project_id != binding.project_id:
            raise AgentRuntimeError(
                "provider_binding_project_mismatch",
                "provider binding belongs to another project scope",
            )

    def _materialize_events(
        self,
        *,
        request: AgentRunRequest,
        handle_id: UUID,
        binding: ProviderBinding,
    ) -> tuple[list[AgentRunEvent], AgentRunState]:
        events: list[AgentRunEvent] = []
        previous_digest = GENESIS_EVENT_DIGEST
        state = AgentRunState.RUNNING
        terminal_seen = False
        started_seen = False
        provider_event_ids: set[str] = set()
        requested_tools: set[str] = set()
        started_tools: set[str] = set()
        finished_tools: set[str] = set()
        templates = self._event_templates.get(binding.id, ())
        if not templates:
            templates = (
                SimulatedEventTemplate(
                    event_type=AgentRunEventType.STARTED,
                    redacted_payload={"state": "running"},
                ),
            )
        for sequence, template in enumerate(templates, start=1):
            if terminal_seen:
                raise AgentRuntimeError(
                    "event_after_terminal",
                    "event template contains an event after a terminal event",
                )
            if sequence == 1 and template.event_type is not AgentRunEventType.STARTED:
                raise AgentRuntimeError(
                    "event_stream_requires_started",
                    "the first event must be started",
                )
            if template.event_type is AgentRunEventType.STARTED:
                if started_seen:
                    raise AgentRuntimeError(
                        "duplicate_started_event",
                        "an event stream may contain one started event",
                    )
                started_seen = True
            if template.provider_event_id is not None:
                if template.provider_event_id in provider_event_ids:
                    raise AgentRuntimeError(
                        "duplicate_provider_event_id",
                        "provider event identities must be unique",
                    )
                provider_event_ids.add(template.provider_event_id)
            self._validate_tool_transition(
                template,
                requested=requested_tools,
                started=started_tools,
                finished=finished_tools,
            )
            timestamp = binding.health_checked_at + timedelta(microseconds=sequence - 1)
            payload = dict(template.redacted_payload)
            event_digest = compute_agent_run_event_digest(
                run_id=request.run_id,
                handle_id=handle_id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=template.event_type,
                redacted_payload=payload,
                provider_event_id=template.provider_event_id,
                previous_event_digest=previous_digest,
                tool_call_id=template.tool_call_id,
                effect_authorization_decision_id=(template.effect_authorization_decision_id),
                effect_authorization_decision_digest=(
                    template.effect_authorization_decision_digest
                ),
            )
            event = AgentRunEvent(
                run_id=request.run_id,
                handle_id=handle_id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=template.event_type,
                redacted_payload=payload,
                provider_event_id=template.provider_event_id,
                tool_call_id=template.tool_call_id,
                effect_authorization_decision_id=(template.effect_authorization_decision_id),
                effect_authorization_decision_digest=(
                    template.effect_authorization_decision_digest
                ),
                previous_event_digest=previous_digest,
                event_digest=event_digest,
            )
            events.append(event)
            previous_digest = event.event_digest
            terminal_state = _TERMINAL_EVENT_STATES.get(event.event_type)
            if terminal_state is not None:
                state = terminal_state
                terminal_seen = True
        try:
            state = validate_agent_run_event_chain(events)
        except AgentRunEventChainError as exc:
            raise AgentRuntimeError(
                exc.reason_code,
                "event stream violates the shared agent-run chain contract",
            ) from exc
        return events, state

    @staticmethod
    def _validate_tool_transition(
        template: SimulatedEventTemplate,
        *,
        requested: set[str],
        started: set[str],
        finished: set[str],
    ) -> None:
        event_type = template.event_type
        tool_call_id = template.tool_call_id
        tool_events = {
            AgentRunEventType.TOOL_REQUESTED,
            AgentRunEventType.TOOL_BLOCKED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_RESULT,
        }
        if event_type not in tool_events:
            return
        if tool_call_id is None:
            raise AgentRuntimeError(
                "tool_event_prerequisite_missing",
                "tool events require a tool call identity",
            )
        if event_type is AgentRunEventType.TOOL_REQUESTED:
            if tool_call_id in requested:
                raise AgentRuntimeError(
                    "tool_event_prerequisite_missing",
                    "tool request identity cannot be replayed",
                )
            requested.add(tool_call_id)
            return
        if tool_call_id not in requested or tool_call_id in finished:
            raise AgentRuntimeError(
                "tool_event_prerequisite_missing",
                "tool event lacks its requested prerequisite",
            )
        if event_type is AgentRunEventType.TOOL_STARTED:
            if tool_call_id in started:
                raise AgentRuntimeError(
                    "tool_event_prerequisite_missing",
                    "tool start identity cannot be replayed",
                )
            started.add(tool_call_id)
        elif event_type is AgentRunEventType.TOOL_BLOCKED:
            if tool_call_id in started:
                raise AgentRuntimeError(
                    "tool_event_prerequisite_missing",
                    "a started tool call must finish with a tool result",
                )
            finished.add(tool_call_id)
        elif tool_call_id not in started:
            raise AgentRuntimeError(
                "tool_event_prerequisite_missing",
                "tool result requires a started tool call",
            )
        else:
            finished.add(tool_call_id)

    def _known_record(self, handle: AgentRunHandle) -> _RunRecord:
        record = self._runs.get(handle.id)
        if record is None:
            raise AgentRuntimeError(
                "unknown_agent_run_handle",
                "agent run handle is not registered",
            )
        if (
            handle.run_id != record.handle.run_id
            or handle.provider_binding_id != record.handle.provider_binding_id
        ):
            raise AgentRuntimeError(
                "agent_run_handle_substitution",
                "agent run handle fields do not match the registered handle",
            )
        return record

    @staticmethod
    def _next_event_timestamp(record: _RunRecord) -> datetime:
        return record.handle.created_at + timedelta(microseconds=len(record.events))
