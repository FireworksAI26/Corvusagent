from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.application.ports import AgentRuntimePort
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEventType,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunState,
    ExecutableIdentity,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderFamily,
    ProviderHealth,
    ProviderStatus,
    ProviderTransport,
    compute_provider_binding_digest,
    validate_agent_run_event_chain,
)
from corvus.infrastructure.agent_runtimes import (
    AgentRuntimeError,
    SimulatedAgentRuntime,
    SimulatedEventTemplate,
)

_BASE_TIME = datetime(2026, 7, 15, tzinfo=UTC)
_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _binding(tmp_path: Path, **updates: object) -> ProviderBinding:
    values: dict[str, object] = {
        "workspace_id": uuid4(),
        "family": ProviderFamily.CODEX,
        "transport": ProviderTransport.LOCAL_CLI,
        "status": ProviderStatus.AVAILABLE,
        "executable_identity": ExecutableIdentity(
            executable_path=(tmp_path / "codex.exe").resolve(),
            version="1.2.3",
            sha256_digest="a" * 64,
        ),
        "model": "gpt-5.6-sol",
        "capabilities": AgentCapabilities(),
        "health_checked_at": _BASE_TIME,
        "version": 1,
        "data_egress_disclosure": "Prompts leave the local process.",
        "server_storage_disclosure": "Provider retention policy applies.",
    }
    values.update(updates)
    return ProviderBinding(**values)


def _request(binding: ProviderBinding, **updates: object) -> AgentRunRequest:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": binding.workspace_id,
        "project_id": binding.project_id,
        "workflow_id": uuid4(),
        "work_item_id": uuid4(),
        "provider_binding_id": binding.id,
        "provider_binding_version": binding.version,
        "provider_binding_digest": compute_provider_binding_digest(binding),
        "model": binding.model,
        "effort": "high",
        "prompt": "Review the repository.",
        "untrusted_context_ref_ids": (),
        "authorization_proof_id": uuid4(),
        "authorization_proof_digest": "1" * 64,
        "autonomy_grant_id": uuid4(),
        "autonomy_grant_digest": "2" * 64,
        "credential_grant_ids": (),
        "credential_proof_id": uuid4(),
        "credential_proof_digest": "3" * 64,
        "budget_proof_id": uuid4(),
        "budget_proof_digest": "4" * 64,
        "kill_switch_proof_id": uuid4(),
        "kill_switch_proof_digest": "5" * 64,
        "sandbox_profile": "workspace-write",
        "filesystem_envelope": ("repository.read",),
        "network_envelope": (),
        "tool_envelope": (),
        "requested_effect_classes": frozenset(),
        "provider_spend_limit": 0,
        "corvus_budget_limit": 0,
        "budget_unit": "usd_micros",
        "budget_requested_amount": 1,
        "approval_limit": 0,
        "max_retries": 0,
        "max_turns": 1,
        "deadline": _FUTURE,
        "max_output_tokens": 4000,
        "max_output_bytes": 100_000,
        "idempotency_key": "run:001",
    }
    values.update(updates)
    return AgentRunRequest(**values)


def _runtime(
    bindings: tuple[ProviderBinding, ...],
    templates: dict[object, tuple[SimulatedEventTemplate, ...]],
) -> SimulatedAgentRuntime:
    return SimulatedAgentRuntime(bindings=bindings, event_templates=templates)


@pytest.mark.asyncio
async def test_simulator_discovers_starts_and_replays_deterministically(tmp_path: Path) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    second = _binding(
        tmp_path,
        id=uuid4(),
        workspace_id=workspace_id,
        project_id=project_id,
        family=ProviderFamily.CLAUDE,
    )
    first = _binding(
        tmp_path,
        id=uuid4(),
        workspace_id=workspace_id,
        project_id=project_id,
    )
    template = (
        SimulatedEventTemplate(AgentRunEventType.STARTED, {"state": "running"}),
        SimulatedEventTemplate(AgentRunEventType.MESSAGE_DELTA, {"text": "hello"}),
        SimulatedEventTemplate(AgentRunEventType.COMPLETED, {"result": "ok"}),
    )
    runtime = _runtime((second, first), {first.id: template, second.id: template})

    assert isinstance(runtime, AgentRuntimePort)
    query = ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
    assert await runtime.discover(query) == tuple(
        ProviderCandidate(
            binding=item,
            binding_version=item.version,
            binding_digest=compute_provider_binding_digest(item),
        )
        for item in sorted((first, second), key=lambda item: str(item.id))
    )
    assert runtime.capabilities(first) == first.capabilities
    assert await runtime.health(first) == ProviderHealth(
        binding_id=first.id,
        binding_version=first.version,
        binding_digest=compute_provider_binding_digest(first),
        status=ProviderStatus.AVAILABLE,
        observed_at=first.health_checked_at,
    )

    request = _request(first)
    started = await runtime.start(request)
    handle = started.handle
    events = [event async for event in runtime.events(handle)]
    replayed = [event async for event in runtime.events(handle, after_sequence=1)]

    assert handle.state is AgentRunState.COMPLETED
    assert [event.sequence for event in events] == [1, 2, 3]
    assert replayed == events[1:]
    assert events[0].previous_event_digest == "0" * 64
    assert events[1].previous_event_digest == events[0].event_digest
    assert events[2].previous_event_digest == events[1].event_digest
    assert not started.replayed


@pytest.mark.asyncio
async def test_active_event_iterator_includes_events_appended_before_completion(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    handle = (await runtime.start(_request(binding))).handle
    stream = runtime.events(handle)

    first = await anext(stream)
    await runtime.cancel(handle, uuid4(), "f" * 64)
    remaining = [event async for event in stream]

    assert first.event_type is AgentRunEventType.STARTED
    assert [event.event_type for event in remaining] == [AgentRunEventType.CANCELLED]


@pytest.mark.asyncio
async def test_binding_lookup_accepts_volatile_health_refresh(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    refreshed = binding.model_copy(
        update={
            "status": ProviderStatus.UNHEALTHY,
            "health_checked_at": binding.health_checked_at + timedelta(seconds=1),
        }
    )

    assert runtime.capabilities(refreshed) == binding.capabilities
    health = await runtime.health(refreshed)
    assert health.status is ProviderStatus.AVAILABLE
    assert health.observed_at == binding.health_checked_at


@pytest.mark.asyncio
async def test_simulator_rejects_tool_blocked_after_tool_started(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    decision_id = uuid4()
    decision_digest = "d" * 64
    runtime = _runtime(
        (binding,),
        {
            binding.id: (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_REQUESTED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=decision_id,
                    effect_authorization_decision_digest=decision_digest,
                ),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_STARTED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=decision_id,
                    effect_authorization_decision_digest=decision_digest,
                ),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_BLOCKED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=decision_id,
                    effect_authorization_decision_digest=decision_digest,
                ),
            )
        },
    )

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(_request(binding))

    assert exc_info.value.reason_code == "tool_event_prerequisite_missing"


@pytest.mark.asyncio
async def test_start_rejects_same_run_with_new_idempotency_key(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    request = _request(binding)
    first = await runtime.start(request)

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(request.model_copy(update={"idempotency_key": "run:002"}))

    assert exc_info.value.reason_code == "agent_run_idempotency_mismatch"
    assert [event async for event in runtime.events(first.handle)]


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_records_one_terminal_event(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime(
        (binding,),
        {binding.id: (SimulatedEventTemplate(AgentRunEventType.STARTED, {"state": "running"}),)},
    )
    handle = (await runtime.start(_request(binding))).handle
    proof_id = uuid4()
    proof_digest = "f" * 64

    first = await runtime.cancel(handle, proof_id, proof_digest)
    second = await runtime.cancel(handle, proof_id, proof_digest)
    events = [event async for event in runtime.events(handle)]

    assert first == second
    assert first.accepted and first.terminal
    assert first.reason_code == "agent_run_cancelled"
    assert [event.event_type for event in events].count(AgentRunEventType.CANCELLED) == 1
    assert events[-1].redacted_payload["current_kill_switch_proof_id"] == str(proof_id)
    assert events[-1].redacted_payload["current_kill_switch_proof_digest"] == proof_digest

    with pytest.raises(AgentRuntimeError) as mismatch:
        await runtime.cancel(handle, proof_id, "e" * 64)

    assert mismatch.value.reason_code == "current_kill_switch_proof_mismatch"


@pytest.mark.asyncio
async def test_cancel_requires_current_kill_switch_proof(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    handle = (await runtime.start(_request(binding))).handle

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.cancel(handle, None, None)  # type: ignore[arg-type]

    assert exc_info.value.reason_code == "current_kill_switch_proof_required"


@pytest.mark.asyncio
async def test_empty_template_cancel_preserves_started_then_cancelled_lifecycle(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    handle = (await runtime.start(_request(binding))).handle

    before_cancel = [event async for event in runtime.events(handle)]
    await runtime.cancel(handle, uuid4(), "f" * 64)
    after_cancel = [event async for event in runtime.events(handle)]

    assert [event.event_type for event in before_cancel] == [AgentRunEventType.STARTED]
    assert [event.event_type for event in after_cancel] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CANCELLED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_started", "closing_event_type"),
    [
        (False, AgentRunEventType.TOOL_BLOCKED),
        (True, AgentRunEventType.TOOL_RESULT),
    ],
)
async def test_cancel_closes_open_tool_calls_before_terminal_event(
    tmp_path: Path,
    tool_started: bool,
    closing_event_type: AgentRunEventType,
) -> None:
    binding = _binding(tmp_path)
    decision_id = uuid4()
    decision_digest = "d" * 64
    templates = [
        SimulatedEventTemplate(AgentRunEventType.STARTED, {"state": "running"}),
        SimulatedEventTemplate(
            AgentRunEventType.TOOL_REQUESTED,
            {"tool": "repository.search"},
            tool_call_id="tool-1",
            effect_authorization_decision_id=decision_id,
            effect_authorization_decision_digest=decision_digest,
        ),
    ]
    if tool_started:
        templates.append(
            SimulatedEventTemplate(
                AgentRunEventType.TOOL_STARTED,
                {"tool": "repository.search"},
                tool_call_id="tool-1",
                effect_authorization_decision_id=decision_id,
                effect_authorization_decision_digest=decision_digest,
            )
        )
    runtime = _runtime((binding,), {binding.id: tuple(templates)})
    handle = (await runtime.start(_request(binding))).handle

    await runtime.cancel(handle, uuid4(), "f" * 64)
    events = [event async for event in runtime.events(handle)]

    assert events[-2].event_type is closing_event_type
    assert events[-2].tool_call_id == "tool-1"
    assert events[-2].effect_authorization_decision_id == decision_id
    assert events[-1].event_type is AgentRunEventType.CANCELLED
    assert validate_agent_run_event_chain(events) is AgentRunState.CANCELLED


@pytest.mark.asyncio
async def test_simulator_snapshots_mutable_event_templates(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    payload = {"state": "original"}
    runtime = _runtime(
        (binding,),
        {binding.id: (SimulatedEventTemplate(AgentRunEventType.STARTED, payload),)},
    )
    payload["state"] = "mutated"

    handle = (await runtime.start(_request(binding))).handle
    events = [event async for event in runtime.events(handle)]

    assert events[0].redacted_payload == {"state": "original"}


@pytest.mark.asyncio
async def test_simulator_never_yields_mutable_payload_aliases(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime(
        (binding,),
        {
            binding.id: (
                SimulatedEventTemplate(
                    AgentRunEventType.STARTED,
                    {"nested": {"state": "running"}},
                ),
            )
        },
    )

    handle = (await runtime.start(_request(binding))).handle
    event = [item async for item in runtime.events(handle)][0]
    nested = event.redacted_payload["nested"]
    assert isinstance(nested, Mapping)
    canary = "plaintext"

    with pytest.raises(TypeError):
        nested["access_token"] = canary  # type: ignore[index]


@pytest.mark.asyncio
async def test_start_identical_replay_returns_stable_handle(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    request = _request(binding)

    first = await runtime.start(request)
    second = await runtime.start(request)

    assert second.handle == first.handle
    assert not first.replayed
    assert second.replayed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"prompt": "Use a substituted prompt."},
        {"authorization_proof_id": uuid4(), "authorization_proof_digest": "f" * 64},
    ],
)
async def test_start_rejects_substituted_idempotent_replay(
    tmp_path: Path,
    substitution: dict[str, object],
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    request = _request(binding)
    await runtime.start(request)

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(request.model_copy(update=substitution))

    assert exc_info.value.reason_code == "agent_run_idempotency_mismatch"


@pytest.mark.asyncio
async def test_start_rejects_binding_model_substitution(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(_request(binding, model="substituted-model"))

    assert exc_info.value.reason_code == "provider_binding_model_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"provider_binding_version": 999},
        {"provider_binding_digest": "f" * 64},
    ],
)
async def test_start_rejects_binding_version_or_digest_substitution(
    tmp_path: Path,
    substitution: dict[str, object],
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(_request(binding, **substitution))

    assert exc_info.value.reason_code == "provider_binding_digest_mismatch"


@pytest.mark.asyncio
async def test_resume_rejects_substitution_and_terminal_handle(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    other = _binding(tmp_path)
    runtime = _runtime(
        (binding, other),
        {
            binding.id: (SimulatedEventTemplate(AgentRunEventType.STARTED, {"state": "running"}),),
            other.id: (),
        },
    )
    request = _request(binding)
    handle = (await runtime.start(request)).handle

    with pytest.raises(AgentRuntimeError) as run_exc:
        await runtime.resume(
            handle,
            request.model_copy(update={"run_id": uuid4(), "resume_handle_id": handle.id}),
        )
    assert run_exc.value.reason_code == "resume_run_substitution"

    with pytest.raises(AgentRuntimeError) as provider_exc:
        await runtime.resume(
            handle,
            request.model_copy(
                update={
                    "provider_binding_id": other.id,
                    "provider_binding_version": other.version,
                    "provider_binding_digest": compute_provider_binding_digest(other),
                    "resume_handle_id": handle.id,
                }
            ),
        )
    assert provider_exc.value.reason_code == "resume_provider_binding_substitution"

    before = [event async for event in runtime.events(handle)]
    resumed = await runtime.resume(
        handle,
        request.model_copy(
            update={
                "authorization_proof_id": uuid4(),
                "authorization_proof_digest": "a" * 64,
                "resume_handle_id": handle.id,
            }
        ),
    )
    after = [event async for event in runtime.events(resumed)]
    assert resumed == handle
    assert after == before

    await runtime.cancel(handle, uuid4(), "f" * 64)
    with pytest.raises(AgentRuntimeError) as terminal_exc:
        await runtime.resume(
            handle,
            request.model_copy(update={"resume_handle_id": handle.id}),
        )
    assert terminal_exc.value.reason_code == "agent_run_terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"model": "substituted-model"},
        {"prompt": "Substituted prompt."},
        {"messages": ("substituted",), "prompt": None},
        {"sandbox_profile": "unsafe"},
        {"network_envelope": ("*",)},
        {"max_output_bytes": 1},
        {"idempotency_key": "substituted"},
    ],
)
async def test_resume_rejects_non_refreshable_request_substitution(
    tmp_path: Path,
    substitution: dict[str, object],
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime((binding,), {binding.id: ()})
    request = _request(binding)
    handle = (await runtime.start(request)).handle

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.resume(
            handle,
            request.model_copy(update={**substitution, "resume_handle_id": handle.id}),
        )

    assert exc_info.value.reason_code == "resume_request_substitution"


@pytest.mark.asyncio
async def test_event_lifecycle_rejects_replay_cursor_and_tool_prerequisite_errors(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    cases = (
        (
            (SimulatedEventTemplate(AgentRunEventType.MESSAGE_DELTA, {"text": "early"}),),
            "event_stream_requires_started",
        ),
        (
            (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
            ),
            "duplicate_started_event",
        ),
        (
            (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}, "start-1"),
                SimulatedEventTemplate(AgentRunEventType.MESSAGE_DELTA, {}, "start-1"),
            ),
            "duplicate_provider_event_id",
        ),
        (
            (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_STARTED,
                    {},
                    tool_call_id="tool-1",
                ),
            ),
            "tool_event_prerequisite_missing",
        ),
    )
    for templates, reason_code in cases:
        runtime = _runtime((binding,), {binding.id: templates})
        with pytest.raises(AgentRuntimeError) as exc_info:
            await runtime.start(_request(binding))
        assert exc_info.value.reason_code == reason_code

    runtime = _runtime(
        (binding,),
        {binding.id: (SimulatedEventTemplate(AgentRunEventType.STARTED, {}),)},
    )
    handle = (await runtime.start(_request(binding))).handle
    with pytest.raises(AgentRuntimeError) as cursor_exc:
        _ = [event async for event in runtime.events(handle, after_sequence=2)]
    assert cursor_exc.value.reason_code == "invalid_event_sequence_cursor"


@pytest.mark.asyncio
async def test_effect_authorization_receipt_is_preserved_in_event_chain(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    decision_id = uuid4()
    decision_digest = "d" * 64
    runtime = _runtime(
        (binding,),
        {
            binding.id: (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_REQUESTED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=decision_id,
                    effect_authorization_decision_digest=decision_digest,
                ),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_BLOCKED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=decision_id,
                    effect_authorization_decision_digest=decision_digest,
                ),
            )
        },
    )

    handle = (await runtime.start(_request(binding))).handle
    events = [event async for event in runtime.events(handle)]

    assert all(
        event.effect_authorization_decision_id == decision_id
        and event.effect_authorization_decision_digest == decision_digest
        for event in events[1:]
    )
    assert events[2].previous_event_digest == events[1].event_digest


@pytest.mark.asyncio
async def test_simulator_rejects_tool_effect_authorization_substitution(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    runtime = _runtime(
        (binding,),
        {
            binding.id: (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {}),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_REQUESTED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=uuid4(),
                    effect_authorization_decision_digest="d" * 64,
                ),
                SimulatedEventTemplate(
                    AgentRunEventType.TOOL_STARTED,
                    {},
                    tool_call_id="tool-1",
                    effect_authorization_decision_id=uuid4(),
                    effect_authorization_decision_digest="e" * 64,
                ),
            )
        },
    )

    with pytest.raises(AgentRuntimeError) as exc_info:
        await runtime.start(_request(binding))

    assert exc_info.value.reason_code == "tool_effect_authorization_mismatch"


@pytest.mark.asyncio
async def test_unknown_unavailable_and_post_terminal_resources_have_stable_errors(
    tmp_path: Path,
) -> None:
    available = _binding(tmp_path)
    unavailable = _binding(tmp_path, status=ProviderStatus.UNAVAILABLE)
    runtime = _runtime(
        (available, unavailable),
        {
            available.id: (
                SimulatedEventTemplate(AgentRunEventType.STARTED, {"state": "running"}),
                SimulatedEventTemplate(AgentRunEventType.COMPLETED, {"result": "ok"}),
                SimulatedEventTemplate(AgentRunEventType.MESSAGE_DELTA, {"text": "late"}),
            ),
            unavailable.id: (),
        },
    )

    with pytest.raises(AgentRuntimeError) as unknown_binding:
        await runtime.start(_request(available, provider_binding_id=uuid4()))
    assert unknown_binding.value.reason_code == "unknown_provider_binding"

    with pytest.raises(AgentRuntimeError) as unavailable_binding:
        await runtime.start(_request(unavailable))
    assert unavailable_binding.value.reason_code == "provider_binding_unavailable"

    unknown_handle = AgentRunHandle(
        run_id=uuid4(),
        provider_binding_id=available.id,
        created_at=_BASE_TIME,
        state=AgentRunState.RUNNING,
    )
    with pytest.raises(AgentRuntimeError) as unknown_handle_exc:
        _ = [event async for event in runtime.events(unknown_handle)]
    assert unknown_handle_exc.value.reason_code == "unknown_agent_run_handle"

    with pytest.raises(AgentRuntimeError) as terminal_template:
        await runtime.start(_request(available))
    assert terminal_template.value.reason_code == "event_after_terminal"
