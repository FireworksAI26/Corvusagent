from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from corvus.application.agent_runtime import AgentRuntimeCoordinator
from corvus.application.ports import (
    AgentRunAuditEvent,
    AgentRunAuditPort,
    AgentRunAuditReceipt,
    AgentRunAuthorizationDecision,
    AgentRunAuthorizationPort,
    AgentRunAuthorizationRequest,
    AgentRunOperation,
    AgentRuntimePort,
    compute_agent_run_audit_event_digest,
    compute_agent_run_audit_receipt_digest,
)
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
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
    compute_agent_run_request_digest,
    compute_provider_binding_digest,
)
from corvus.domain.client import ClientSurface
from corvus.domain.request import RequestContext
from corvus.infrastructure.agent_runtimes import AgentRuntimeError, SimulatedAgentRuntime

_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _binding(
    tmp_path: Path,
    *,
    workspace_id: UUID,
    project_id: UUID | None,
) -> ProviderBinding:
    return ProviderBinding(
        workspace_id=workspace_id,
        project_id=project_id,
        family=ProviderFamily.CODEX,
        transport=ProviderTransport.LOCAL_CLI,
        status=ProviderStatus.AVAILABLE,
        executable_identity=ExecutableIdentity(
            executable_path=(tmp_path / "codex.exe").resolve(),
            version="1.2.3",
            sha256_digest="a" * 64,
        ),
        model="gpt-5.6-sol",
        capabilities=AgentCapabilities(
            repository_read=CapabilitySupport.SUPPORTED,
            session_resume=CapabilitySupport.SUPPORTED,
            provider_side_cancellation=CapabilitySupport.SUPPORTED,
        ),
        health_checked_at=_NOW,
        version=1,
        data_egress_disclosure="Prompts leave the local process.",
        server_storage_disclosure="Provider retention policy applies.",
    )


def _request(binding: ProviderBinding, **updates: object) -> AgentRunRequest:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": binding.workspace_id,
        "project_id": binding.project_id,
        "provider_binding_id": binding.id,
        "provider_binding_version": binding.version,
        "provider_binding_digest": compute_provider_binding_digest(binding),
        "model": binding.model,
        "effort": "high",
        "prompt": "Review the repository.",
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


def _context(request: AgentRunRequest, **updates: object) -> RequestContext:
    values: dict[str, object] = {
        "deployment_profile_id": uuid4(),
        "deployment_instance_id": uuid4(),
        "workspace_id": request.workspace_id,
        "workspace_authority_epoch": 1,
        "workspace_authority_generation": 0,
        "authority_state_root": "a" * 64,
        "authority_epoch_credential_id": uuid4(),
        "authority_commit_receipt_id": uuid4(),
        "authority_proof_digest": "b" * 64,
        "scope_kind": "project" if request.project_id is not None else "workspace",
        "scope_id": request.project_id or request.workspace_id,
        "scope_digest": "c" * 64,
        "audience_policy_snapshot_id": uuid4(),
        "audience_policy_digest": "d" * 64,
        "requester_id": uuid4(),
        "client_context_id": uuid4(),
        "transport_principal_id": uuid4(),
        "agent_id": uuid4(),
        "agent_grant_id": uuid4(),
        "access_bundle_id": uuid4(),
        "policy_digest": "e" * 64,
        "authorization_snapshot_id": request.authorization_proof_id,
        "authorization_snapshot_digest": request.authorization_proof_digest,
        "authorization_signing_key_version_id": uuid4(),
        "idempotency_key": request.idempotency_key,
        "correlation_id": uuid4(),
    }
    values.update(updates)
    return RequestContext(**values)


def _decision(
    request: AgentRunAuthorizationRequest,
    **updates: object,
) -> AgentRunAuthorizationDecision:
    values: dict[str, object] = {
        "allowed": True,
        "reason_code": "agent_run_authorized",
        "authorization_snapshot_id": request.context.authorization_snapshot_id,
        "canonical_request_digest": request.canonical_request_digest,
        "immutable_request_digest": request.request.immutable_request_digest,
        "provider_binding_version": request.request.provider_binding_version,
        "provider_binding_digest": request.request.provider_binding_digest,
        "autonomy_grant_digest": request.request.autonomy_grant_digest,
        "credential_proof_digest": request.request.credential_proof_digest,
        "budget_proof_digest": request.request.budget_proof_digest,
        "kill_switch_proof_id": (
            request.current_kill_switch_proof_id or request.request.kill_switch_proof_id
        ),
        "kill_switch_proof_digest": (
            request.current_kill_switch_proof_digest or request.request.kill_switch_proof_digest
        ),
        "evaluated_at": _NOW,
    }
    values.update(updates)
    return AgentRunAuthorizationDecision(**values)


class _Authorizer:
    def __init__(
        self,
        order: list[str],
        *,
        decision_updates: dict[str, object] | None = None,
        malformed_decision_updates: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.order = order
        self.decision_updates = decision_updates or {}
        self.malformed_decision_updates = malformed_decision_updates or {}
        self.error = error
        self.requests: list[AgentRunAuthorizationRequest] = []

    def authorize(
        self,
        request: AgentRunAuthorizationRequest,
    ) -> AgentRunAuthorizationDecision:
        self.order.append("authorization")
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        decision = _decision(request, **self.decision_updates)
        return decision.model_copy(update=self.malformed_decision_updates)


class _AuditSink:
    def __init__(
        self,
        order: list[str],
        *,
        fail_on_phase: str | None = None,
        acknowledged: bool = True,
        unacknowledged_on_phase: str | None = None,
        forge_acknowledgement_on_phase: str | None = None,
        break_continuity_on_phase: str | None = None,
        continuity_break: str = "sequence",
        malformed_receipt_on_phase: str | None = None,
        malformed_receipt: object = None,
    ) -> None:
        self.order = order
        self.fail_on_phase = fail_on_phase
        self.acknowledged = acknowledged
        self.unacknowledged_on_phase = unacknowledged_on_phase
        self.forge_acknowledgement_on_phase = forge_acknowledgement_on_phase
        self.break_continuity_on_phase = break_continuity_on_phase
        self.continuity_break = continuity_break
        self.malformed_receipt_on_phase = malformed_receipt_on_phase
        self.malformed_receipt = malformed_receipt
        self.events: list[AgentRunAuditEvent] = []
        self.receipts: list[AgentRunAuditReceipt] = []

    def record(self, event: AgentRunAuditEvent) -> AgentRunAuditReceipt:
        self.order.append(f"audit:{event.phase}")
        if event.phase == self.fail_on_phase:
            raise RuntimeError("audit unavailable")
        self.events.append(event)
        sequence = len(self.receipts) + 1
        previous_digest = self.receipts[-1].receipt_digest if self.receipts else "0" * 64
        event_digest = compute_agent_run_audit_event_digest(event)
        acknowledged = self.acknowledged and event.phase != self.unacknowledged_on_phase
        receipt = AgentRunAuditReceipt(
            sequence=sequence,
            previous_receipt_digest=previous_digest,
            event_digest=event_digest,
            receipt_digest=compute_agent_run_audit_receipt_digest(
                sequence=sequence,
                previous_receipt_digest=previous_digest,
                event_digest=event_digest,
                acknowledged=acknowledged,
            ),
            acknowledged=acknowledged,
        )
        if event.phase == self.break_continuity_on_phase:
            if self.continuity_break == "sequence":
                sequence += 1
            else:
                previous_digest = "f" * 64
            receipt = AgentRunAuditReceipt(
                sequence=sequence,
                previous_receipt_digest=previous_digest,
                event_digest=event_digest,
                receipt_digest=compute_agent_run_audit_receipt_digest(
                    sequence=sequence,
                    previous_receipt_digest=previous_digest,
                    event_digest=event_digest,
                    acknowledged=acknowledged,
                ),
                acknowledged=acknowledged,
            )
        if event.phase == self.forge_acknowledgement_on_phase:
            valid_false_receipt = AgentRunAuditReceipt(
                sequence=receipt.sequence,
                previous_receipt_digest=receipt.previous_receipt_digest,
                event_digest=receipt.event_digest,
                receipt_digest=compute_agent_run_audit_receipt_digest(
                    sequence=receipt.sequence,
                    previous_receipt_digest=receipt.previous_receipt_digest,
                    event_digest=receipt.event_digest,
                    acknowledged=False,
                ),
                acknowledged=False,
            )
            receipt = valid_false_receipt.model_copy(update={"acknowledged": True})
        self.receipts.append(receipt)
        if event.phase == self.malformed_receipt_on_phase:
            return self.malformed_receipt  # type: ignore[return-value]
        return receipt


class _RecordingRuntime:
    def __init__(
        self,
        runtime: SimulatedAgentRuntime,
        order: list[str],
        *,
        failures: dict[str, str] | None = None,
        start_handle_updates: dict[str, object] | None = None,
        resume_handle_updates: dict[str, object] | None = None,
        cancellation_handle_id: UUID | None = None,
        cancellation_reason_code: str | None = None,
        cancellation_result_override: CancellationResult | None = None,
        health_status: ProviderStatus | None = None,
        capabilities_override: AgentCapabilities | None = None,
        discover_error: Exception | None = None,
        capabilities_error: Exception | None = None,
        null_results: frozenset[str] | None = None,
        malformed_results: frozenset[str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.order = order
        self.failures = failures or {}
        self.start_handle_updates = start_handle_updates or {}
        self.resume_handle_updates = resume_handle_updates or {}
        self.cancellation_handle_id = cancellation_handle_id
        self.cancellation_reason_code = cancellation_reason_code
        self.cancellation_result_override = cancellation_result_override
        self.health_status = health_status
        self.capabilities_override = capabilities_override
        self.discover_error = discover_error
        self.capabilities_error = capabilities_error
        self.null_results = null_results or frozenset()
        self.malformed_results = malformed_results or frozenset()

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        self.order.append("runtime:discover")
        if self.discover_error is not None:
            raise self.discover_error
        return await self.runtime.discover(query)

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        self.order.append("runtime:capabilities")
        if self.capabilities_error is not None:
            raise self.capabilities_error
        if self.capabilities_override is not None:
            return self.capabilities_override
        return self.runtime.capabilities(binding)

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        self.order.append("runtime:health")
        result = await self.runtime.health(binding)
        if self.health_status is None:
            return result
        return result.model_copy(update={"status": self.health_status})

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        self.order.append("runtime:start")
        self._raise_if_failed("start")
        if "start" in self.null_results:
            return None  # type: ignore[return-value]
        if "start" in self.malformed_results:
            return object()  # type: ignore[return-value]
        if "start_nested" in self.malformed_results:
            return AgentRunStartResult.model_construct(handle=object(), replayed=False)
        result = await self.runtime.start(request)
        return result.model_copy(
            update={
                "handle": result.handle.model_copy(update=self.start_handle_updates),
            }
        )

    def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        return self.runtime.events(handle, after_sequence)

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        self.order.append("runtime:cancel")
        self._raise_if_failed("cancel")
        if "cancel" in self.null_results:
            return None  # type: ignore[return-value]
        if "cancel" in self.malformed_results:
            return object()  # type: ignore[return-value]
        if self.cancellation_result_override is not None:
            return self.cancellation_result_override
        result = await self.runtime.cancel(
            handle,
            current_kill_switch_proof_id,
            current_kill_switch_proof_digest,
        )
        if self.cancellation_handle_id is None and self.cancellation_reason_code is None:
            return result
        updates: dict[str, object] = {}
        if self.cancellation_handle_id is not None:
            updates["handle_id"] = self.cancellation_handle_id
        if self.cancellation_reason_code is not None:
            updates["reason_code"] = self.cancellation_reason_code
        return result.model_copy(update=updates)

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        self.order.append("runtime:resume")
        self._raise_if_failed("resume")
        if "resume" in self.null_results:
            return None  # type: ignore[return-value]
        if "resume" in self.malformed_results:
            return object()  # type: ignore[return-value]
        result = await self.runtime.resume(handle, request_with_fresh_proofs)
        return result.model_copy(update=self.resume_handle_updates)

    def _raise_if_failed(self, operation: str) -> None:
        reason_code = self.failures.get(operation)
        if reason_code is not None:
            raise AgentRuntimeError(reason_code, "injected runtime failure")


def _coordinator(
    *,
    runtime: _RecordingRuntime,
    order: list[str],
    authorizer: _Authorizer | None = None,
    audit: _AuditSink | None = None,
) -> tuple[AgentRuntimeCoordinator, _Authorizer, _AuditSink]:
    selected_authorizer = authorizer or _Authorizer(order)
    selected_audit = audit or _AuditSink(order)
    coordinator = AgentRuntimeCoordinator(
        runtime=runtime,
        authorization=selected_authorizer,
        audit=selected_audit,
        clock=lambda: _NOW,
    )
    return coordinator, selected_authorizer, selected_audit


def _authorization_request(
    context: RequestContext,
    request: AgentRunRequest,
    **updates: object,
) -> AgentRunAuthorizationRequest:
    values: dict[str, object] = {
        "context": context,
        "client_surface": ClientSurface.CLI,
        "operation": AgentRunOperation.START,
        "request": request,
        "canonical_request_digest": compute_agent_run_request_digest(request),
    }
    values.update(updates)
    return AgentRunAuthorizationRequest(**values)


@pytest.mark.asyncio
async def test_start_authorizes_audits_executes_and_audits_outcome(tmp_path: Path) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=project_id)
    request = _request(binding)
    context = _context(request)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    audit = _AuditSink(order)
    coordinator = AgentRuntimeCoordinator(
        runtime=runtime,
        authorization=_Authorizer(order),
        audit=audit,
        clock=lambda: _NOW,
    )

    result = await coordinator.start(context, ClientSurface.CLI, request)

    assert result.ok
    assert result.reason_code == "agent_run_started"
    assert result.handle is not None
    assert not result.identical_start_replayed
    assert not result.audit_pending
    assert order == [
        "authorization",
        "audit:authorization",
        "runtime:discover",
        "runtime:capabilities",
        "runtime:health",
        "runtime:start",
        "audit:outcome",
    ]
    assert [event.outcome for event in audit.events] == ["allow", "success"]


@pytest.mark.parametrize(
    ("context_updates", "reason_code"),
    [
        ({"workspace_id": uuid4()}, "agent_run_workspace_mismatch"),
        ({"scope_id": uuid4()}, "agent_run_project_scope_mismatch"),
        ({"idempotency_key": "substituted"}, "agent_run_idempotency_key_mismatch"),
        (
            {"authorization_snapshot_id": uuid4()},
            "agent_run_authorization_snapshot_mismatch",
        ),
        (
            {"authorization_snapshot_digest": "f" * 64},
            "agent_run_authorization_snapshot_mismatch",
        ),
    ],
)
def test_authorization_request_rejects_context_substitution(
    tmp_path: Path,
    context_updates: dict[str, object],
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    context = _context(request, **context_updates)

    with pytest.raises(ValidationError) as exc_info:
        _authorization_request(context, request)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == reason_code


def test_authorization_request_enforces_workspace_scope_without_project_smuggling(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=None)
    request = _request(binding, project_id=uuid4())
    context = _context(request, scope_kind="workspace", scope_id=workspace_id)

    with pytest.raises(ValidationError) as exc_info:
        _authorization_request(context, request)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "agent_run_workspace_scope_mismatch"
    )


@pytest.mark.parametrize("scope_kind", ["channel", "thread", "conversation"])
def test_authorization_request_rejects_scope_without_project_ancestry_resolver(
    tmp_path: Path,
    scope_kind: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    context = _context(request, scope_kind=scope_kind, scope_id=uuid4())

    with pytest.raises(ValidationError) as exc_info:
        _authorization_request(context, request)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == ("agent_run_scope_kind_unsupported")


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"canonical_request_digest": "f" * 64}, "agent_run_request_digest_mismatch"),
        (
            {
                "operation": AgentRunOperation.RESUME,
                "handle": AgentRunHandle(
                    run_id=uuid4(),
                    provider_binding_id=uuid4(),
                    created_at=_NOW,
                    state="running",
                ),
            },
            "agent_run_handle_run_mismatch",
        ),
        (
            {
                "operation": AgentRunOperation.CANCEL,
                "current_kill_switch_proof_id": uuid4(),
                "current_kill_switch_proof_digest": "INVALID",
            },
            "agent_run_current_kill_switch_proof_required",
        ),
    ],
)
def test_authorization_request_rejects_request_handle_and_cancel_substitution(
    tmp_path: Path,
    updates: dict[str, object],
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    if updates.get("operation") is AgentRunOperation.CANCEL:
        updates = {
            **updates,
            "handle": AgentRunHandle(
                run_id=request.run_id,
                provider_binding_id=request.provider_binding_id,
                created_at=_NOW,
                state="running",
            ),
        }

    with pytest.raises(ValidationError) as exc_info:
        _authorization_request(_context(request), request, **updates)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == reason_code


@pytest.mark.parametrize(
    ("case", "reason_code"),
    [
        ("start_handle", "agent_run_handle_unsolicited"),
        ("start_resume_handle", "agent_run_resume_handle_unsolicited"),
        ("start_kill_switch", "agent_run_current_kill_switch_proof_unsolicited"),
        ("resume_kill_switch", "agent_run_current_kill_switch_proof_unsolicited"),
        ("cancel_resume_handle", "agent_run_resume_handle_unsolicited"),
    ],
)
def test_authorization_request_rejects_operation_specific_field_smuggling(
    tmp_path: Path,
    case: str,
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    run_id = uuid4()
    handle_id = uuid4()
    request = _request(
        binding,
        run_id=run_id,
        resume_handle_id=(
            handle_id
            if case in {"start_resume_handle", "resume_kill_switch", "cancel_resume_handle"}
            else None
        ),
    )
    handle = AgentRunHandle(
        id=handle_id,
        run_id=run_id,
        provider_binding_id=request.provider_binding_id,
        created_at=_NOW,
        state="running",
    )
    updates: dict[str, object] = {}
    if case == "start_handle":
        updates["handle"] = handle
    elif case == "start_kill_switch":
        updates.update(
            current_kill_switch_proof_id=uuid4(),
            current_kill_switch_proof_digest="f" * 64,
        )
    elif case == "resume_kill_switch":
        updates.update(
            operation=AgentRunOperation.RESUME,
            handle=handle,
            current_kill_switch_proof_id=uuid4(),
            current_kill_switch_proof_digest="f" * 64,
        )
    elif case == "cancel_resume_handle":
        updates.update(
            operation=AgentRunOperation.CANCEL,
            handle=handle,
            current_kill_switch_proof_id=uuid4(),
            current_kill_switch_proof_digest="f" * 64,
        )

    with pytest.raises(ValidationError) as exc_info:
        _authorization_request(_context(request), request, **updates)

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == reason_code


@pytest.mark.asyncio
async def test_authorization_exception_never_calls_audit_or_runtime(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(order, error=RuntimeError("unavailable"))
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_unavailable"
    assert order == ["authorization"]


@pytest.mark.asyncio
async def test_malformed_authorization_decision_never_calls_audit_or_runtime(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(
        order,
        malformed_decision_updates={"allowed": "true"},
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_unavailable"
    assert order == ["authorization"]


@pytest.mark.asyncio
async def test_authorization_clock_failure_never_calls_audit_or_runtime(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator = AgentRuntimeCoordinator(
        runtime=runtime,
        authorization=_Authorizer(order),
        audit=_AuditSink(order),
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_unavailable"
    assert order == ["authorization"]


@pytest.mark.asyncio
async def test_naive_authorization_clock_never_calls_audit_or_runtime(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator = AgentRuntimeCoordinator(
        runtime=runtime,
        authorization=_Authorizer(order),
        audit=_AuditSink(order),
        clock=lambda: datetime(2026, 7, 15),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_unavailable"
    assert order == ["authorization"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason_code",
    [
        "stale_authorization_snapshot",
        "stale_autonomy_grant",
        "stale_credential_proof",
        "agent_run_over_budget",
        "agent_run_kill_switch_active",
    ],
)
async def test_authorization_denial_propagates_without_runtime(
    tmp_path: Path,
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(
        order,
        decision_updates={"allowed": False, "reason_code": reason_code},
    )
    coordinator, _, audit = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == reason_code
    assert order == ["authorization", "audit:authorization"]
    assert audit.events[0].outcome == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision_updates", "reason_code"),
    [
        (
            {"authorization_snapshot_id": uuid4()},
            "agent_run_authorization_snapshot_mismatch",
        ),
        ({"canonical_request_digest": "f" * 64}, "agent_run_request_digest_mismatch"),
        ({"autonomy_grant_digest": "f" * 64}, "agent_run_autonomy_grant_digest_mismatch"),
        (
            {"credential_proof_digest": "f" * 64},
            "agent_run_credential_proof_digest_mismatch",
        ),
        ({"budget_proof_digest": "f" * 64}, "agent_run_budget_proof_digest_mismatch"),
        ({"kill_switch_proof_id": uuid4()}, "agent_run_kill_switch_proof_id_mismatch"),
        (
            {"kill_switch_proof_digest": "f" * 64},
            "agent_run_kill_switch_proof_digest_mismatch",
        ),
        ({"provider_binding_version": 999}, "provider_binding_digest_mismatch"),
        ({"provider_binding_digest": "f" * 64}, "provider_binding_digest_mismatch"),
        ({"immutable_request_digest": "f" * 64}, "resume_request_substitution"),
    ],
)
async def test_receipt_mismatch_denies_before_runtime(
    tmp_path: Path,
    decision_updates: dict[str, object],
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(order, decision_updates=decision_updates)
    coordinator, _, audit = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == reason_code
    assert order == ["authorization", "audit:authorization"]
    assert audit.events[0].outcome == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evaluated_at",
    [_NOW + timedelta(microseconds=1), _FUTURE],
    ids=["future_of_coordinator_clock", "at_request_deadline"],
)
async def test_authorization_decision_rejects_invalid_evaluation_time_before_runtime(
    tmp_path: Path,
    evaluated_at: datetime,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(order, decision_updates={"evaluated_at": evaluated_at})
    coordinator, _, audit = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_time_invalid"
    assert order == ["authorization", "audit:authorization"]
    assert audit.events[0].outcome == "deny"


@pytest.mark.asyncio
async def test_coordinator_rejects_run_after_request_deadline(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding, deadline=_NOW - timedelta(seconds=1))
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    authorizer = _Authorizer(
        order,
        decision_updates={"evaluated_at": _NOW - timedelta(seconds=2)},
    )
    coordinator, _, audit = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_authorization_time_invalid"
    assert "runtime:start" not in order
    assert audit.events[0].outcome == "deny"


@pytest.mark.asyncio
async def test_cancel_reaches_runtime_after_request_deadline(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding, deadline=_NOW - timedelta(seconds=1))
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order)
    authorizer = _Authorizer(
        order,
        decision_updates={"evaluated_at": _NOW - timedelta(seconds=2)},
    )
    coordinator, _, audit = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert result.ok
    assert "runtime:cancel" in order
    assert audit.events[-1].outcome == "success"


@pytest.mark.asyncio
async def test_authorization_audit_failure_prevents_runtime(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    audit = _AuditSink(order, fail_on_phase="authorization")
    coordinator, _, _ = _coordinator(runtime=runtime, order=order, audit=audit)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_audit_unavailable"
    assert order == ["authorization", "audit:authorization"]


@pytest.mark.asyncio
async def test_unacknowledged_authorization_audit_prevents_runtime(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(order, acknowledged=False),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_audit_unavailable"
    assert "runtime:discover" not in order


def test_audit_receipt_rejects_acknowledgement_tampering() -> None:
    event_digest = "a" * 64
    receipt = AgentRunAuditReceipt(
        sequence=1,
        previous_receipt_digest="0" * 64,
        event_digest=event_digest,
        receipt_digest=compute_agent_run_audit_receipt_digest(
            sequence=1,
            previous_receipt_digest="0" * 64,
            event_digest=event_digest,
            acknowledged=False,
        ),
        acknowledged=False,
    )

    with pytest.raises(ValidationError):
        AgentRunAuditReceipt.model_validate({**receipt.model_dump(), "acknowledged": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "resume", "cancel"])
@pytest.mark.parametrize("forged_phase", ["authorization", "outcome"])
async def test_coordinator_rejects_forged_acknowledgement_receipts(
    tmp_path: Path,
    operation: str,
    forged_phase: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order)
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(
            order,
            forge_acknowledgement_on_phase=forged_phase,
        ),
    )

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
        expected_primary_reason = "agent_run_started"
    elif operation == "resume":
        resumed = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(
            _context(resumed),
            ClientSurface.CLI,
            handle,
            resumed,
        )
        expected_primary_reason = "agent_run_resumed"
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )
        expected_primary_reason = "agent_run_cancelled"

    if forged_phase == "authorization":
        assert result.reason_code == "agent_run_audit_unavailable"
        assert f"runtime:{operation}" not in order
    else:
        assert result.reason_code == "agent_run_audit_pending"
        assert result.audit_pending
        assert result.primary_reason_code == expected_primary_reason
        assert result.primary_outcome == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("continuity_break", ["sequence", "previous_digest"])
async def test_outcome_receipt_must_continue_the_authorization_receipt(
    tmp_path: Path,
    continuity_break: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(
            order,
            break_continuity_on_phase="outcome",
            continuity_break=continuity_break,
        ),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert result.reason_code == "agent_run_audit_pending"
    assert result.audit_pending
    assert result.primary_reason_code == "agent_run_started"
    assert result.primary_outcome == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["authorization", "outcome"])
@pytest.mark.parametrize("malformed_receipt", [None, object()])
async def test_malformed_audit_receipts_fail_closed(
    tmp_path: Path,
    phase: str,
    malformed_receipt: object,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(
            order,
            malformed_receipt_on_phase=phase,
            malformed_receipt=malformed_receipt,
        ),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    if phase == "authorization":
        assert result.reason_code == "agent_run_audit_unavailable"
        assert "runtime:start" not in order
    else:
        assert result.reason_code == "agent_run_audit_pending"
        assert result.audit_pending
        assert result.primary_reason_code == "agent_run_started"
        assert result.primary_outcome == "success"


@pytest.mark.asyncio
async def test_outcome_audit_failure_returns_retryable_pending_result(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(order, fail_on_phase="outcome"),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_audit_pending"
    assert result.audit_pending
    assert result.handle is not None
    assert result.primary_reason_code == "agent_run_started"
    assert result.primary_outcome == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"prompt": "Changed prompt."},
        {
            "authorization_proof_id": uuid4(),
            "authorization_proof_digest": "f" * 64,
        },
    ],
)
async def test_start_replay_returns_stable_handle_and_changed_request_fails(
    tmp_path: Path,
    substitution: dict[str, object],
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    first = await coordinator.start(_context(request), ClientSurface.CLI, request)
    replay = await coordinator.start(_context(request), ClientSurface.CLI, request)
    changed = request.model_copy(update=substitution)
    mismatch = await coordinator.start(_context(changed), ClientSurface.CLI, changed)

    assert first.handle == replay.handle
    assert replay.identical_start_replayed
    assert not mismatch.ok
    assert mismatch.reason_code == "agent_run_idempotency_mismatch"


@pytest.mark.asyncio
async def test_invalid_context_binding_skips_all_ports(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(
        _context(request, workspace_id=uuid4()),
        ClientSurface.CLI,
        request,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_workspace_mismatch"
    assert order == []


@pytest.mark.asyncio
async def test_provider_preflight_rejects_stale_binding_receipt(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding).model_copy(update={"provider_binding_digest": "f" * 64})
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "provider_binding_digest_mismatch"
    assert "runtime:start" not in order
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_provider_preflight_rejects_missing_candidate(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(), event_templates={}),
        order,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_provider_unavailable"
    assert order == [
        "authorization",
        "audit:authorization",
        "runtime:discover",
        "audit:outcome",
    ]
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_provider_preflight_rejects_ambiguous_matching_candidates(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []

    class _AmbiguousDiscoveryRuntime(_RecordingRuntime):
        async def discover(
            self,
            query: ProviderDiscoveryQuery,
        ) -> tuple[ProviderCandidate, ...]:
            candidates = await super().discover(query)
            return (candidates[0], candidates[0])

    runtime = _AmbiguousDiscoveryRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={}),
        order,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_provider_unavailable"
    assert "runtime:capabilities" not in order
    assert "runtime:start" not in order
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_provider_preflight_rejects_unverified_requested_tool_capability(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding, tool_envelope=("repository.search",))
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_capability_unavailable"
    assert "runtime:capabilities" in order
    assert "runtime:start" not in order
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_provider_preflight_rejects_runtime_capability_downgrade(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    supported = binding.capabilities.model_copy(update={"tools": CapabilitySupport.SUPPORTED})
    binding = binding.model_copy(update={"capabilities": supported})
    request = _request(binding, tool_envelope=("repository.search",))
    reported = supported.model_copy(update={"tools": CapabilitySupport.UNSUPPORTED})
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
        capabilities_override=reported,
    )
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_capability_unavailable"
    assert "runtime:start" not in order


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_updates", "capability_updates"),
    [
        ({}, {"repository_read": CapabilitySupport.UNVERIFIED}),
        (
            {"requested_effect_classes": frozenset({"repository.write"})},
            {"repository_write": CapabilitySupport.UNVERIFIED},
        ),
        (
            {"requested_effect_classes": frozenset({"shell.execute"})},
            {"shell": CapabilitySupport.UNVERIFIED},
        ),
        (
            {"tool_envelope": ("mcp.invoke",)},
            {
                "tools": CapabilitySupport.SUPPORTED,
                "mcp": CapabilitySupport.UNVERIFIED,
            },
        ),
        (
            {"provider_spend_limit": 1},
            {"provider_side_budget": CapabilitySupport.UNVERIFIED},
        ),
    ],
)
async def test_provider_preflight_maps_requested_envelopes_to_verified_capabilities(
    tmp_path: Path,
    request_updates: dict[str, object],
    capability_updates: dict[str, CapabilitySupport],
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    binding = binding.model_copy(
        update={"capabilities": binding.capabilities.model_copy(update=capability_updates)}
    )
    request = _request(binding, **request_updates)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_capability_unavailable"
    assert "runtime:start" not in order


@pytest.mark.asyncio
async def test_provider_preflight_requires_verified_resume_capability(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    binding = binding.model_copy(
        update={
            "capabilities": binding.capabilities.model_copy(
                update={"session_resume": CapabilitySupport.UNVERIFIED}
            )
        }
    )
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    resumed = request.model_copy(update={"resume_handle_id": handle.id})
    order: list[str] = []
    coordinator, _, _ = _coordinator(
        runtime=_RecordingRuntime(simulator, order),
        order=order,
    )

    result = await coordinator.resume(
        _context(resumed),
        ClientSurface.CLI,
        handle,
        resumed,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_capability_unavailable"
    assert "runtime:resume" not in order


@pytest.mark.asyncio
async def test_provider_preflight_audit_failure_returns_pending_result(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding).model_copy(update={"provider_binding_digest": "f" * 64})
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(order, fail_on_phase="outcome"),
    )

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert result.reason_code == "agent_run_audit_pending"
    assert result.audit_pending
    assert result.primary_reason_code == "provider_binding_digest_mismatch"
    assert result.primary_outcome == "failure"


@pytest.mark.asyncio
async def test_resume_rejects_handle_substitution_before_ports(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    substituted = request.model_copy(update={"resume_handle_id": uuid4()})
    order: list[str] = []
    coordinator, _, _ = _coordinator(
        runtime=_RecordingRuntime(simulator, order),
        order=order,
    )

    result = await coordinator.resume(
        _context(substituted),
        ClientSurface.CLI,
        handle,
        substituted,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_resume_handle_mismatch"
    assert order == []


@pytest.mark.asyncio
async def test_resume_requires_bound_receipt_for_matching_fresh_request(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    fresh = request.model_copy(
        update={
            "authorization_proof_id": uuid4(),
            "authorization_proof_digest": "6" * 64,
            "autonomy_grant_digest": "7" * 64,
            "credential_proof_digest": "8" * 64,
            "budget_proof_digest": "9" * 64,
            "kill_switch_proof_id": uuid4(),
            "kill_switch_proof_digest": "a" * 64,
            "resume_handle_id": handle.id,
        }
    )
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order)
    mismatched_authorizer = _Authorizer(
        order,
        decision_updates={"credential_proof_digest": "f" * 64},
    )
    denied_coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=mismatched_authorizer,
    )

    denied = await denied_coordinator.resume(
        _context(fresh),
        ClientSurface.CLI,
        handle,
        fresh,
    )

    assert not denied.ok
    assert denied.reason_code == "agent_run_credential_proof_digest_mismatch"
    assert "runtime:resume" not in order

    order.clear()
    accepted_coordinator, _, audit = _coordinator(runtime=runtime, order=order)
    accepted = await accepted_coordinator.resume(
        _context(fresh),
        ClientSurface.CLI,
        handle,
        fresh,
    )

    assert accepted.ok
    assert accepted.reason_code == "agent_run_resumed"
    assert accepted.handle == handle
    assert order == [
        "authorization",
        "audit:authorization",
        "runtime:discover",
        "runtime:capabilities",
        "runtime:health",
        "runtime:resume",
        "audit:outcome",
    ]
    assert all(event.handle_id == handle.id for event in audit.events)
    assert audit.events[-1].outcome == "success"


@pytest.mark.asyncio
async def test_cancel_uses_explicit_current_proof_and_is_idempotent(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    current_proof_id = uuid4()
    current_proof_digest = "f" * 64
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order)
    coordinator, authorizer, audit = _coordinator(runtime=runtime, order=order)

    first = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        current_proof_id,
        current_proof_digest,
    )
    second = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        current_proof_id,
        current_proof_digest,
    )

    assert first.ok and second.ok
    assert first.cancellation_result == second.cancellation_result
    assert first.handle is not None and first.handle.state is AgentRunState.CANCELLED
    assert second.handle is not None and second.handle.state is AgentRunState.CANCELLED
    assert all(
        item.current_kill_switch_proof_id == current_proof_id
        and item.current_kill_switch_proof_digest == current_proof_digest
        for item in authorizer.requests
    )
    assert all(event.kill_switch_proof_id == current_proof_id for event in audit.events)
    assert all(event.kill_switch_proof_digest == current_proof_digest for event in audit.events)
    assert all(event.handle_id == handle.id for event in audit.events)


@pytest.mark.asyncio
async def test_cancel_reaches_runtime_when_provider_health_is_unhealthy(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order, health_status=ProviderStatus.UNHEALTHY)
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert result.ok
    assert "runtime:cancel" in order
    assert "runtime:health" not in order


@pytest.mark.asyncio
async def test_cancel_requires_only_verified_cancellation_capability(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    binding = binding.model_copy(
        update={
            "capabilities": AgentCapabilities(
                provider_side_cancellation=CapabilitySupport.SUPPORTED
            )
        }
    )
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    coordinator, _, _ = _coordinator(
        runtime=_RecordingRuntime(simulator, order),
        order=order,
    )

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert result.ok
    assert "runtime:cancel" in order


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_probe", ["discover", "capabilities"])
async def test_cancel_bypasses_fallible_provider_introspection(
    tmp_path: Path,
    failing_probe: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        discover_error=(
            RuntimeError("discovery unavailable") if failing_probe == "discover" else None
        ),
        capabilities_error=(
            RuntimeError("capabilities unavailable") if failing_probe == "capabilities" else None
        ),
    )
    coordinator, _, _ = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert result.ok
    assert "runtime:cancel" in order
    assert "runtime:discover" not in order
    assert "runtime:capabilities" not in order


@pytest.mark.asyncio
async def test_cancel_rejected_nonterminal_result_records_failure(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    rejected = CancellationResult(
        handle_id=handle.id,
        accepted=False,
        terminal=False,
        reason_code="provider_cancel_rejected",
        timestamp=_NOW,
    )
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        cancellation_result_override=rejected,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.reason_code == "provider_cancel_rejected"
    assert result.handle == handle
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_cancel_terminal_result_requires_canonical_terminal_handle(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    terminal_without_handle = CancellationResult(
        handle_id=handle.id,
        accepted=False,
        terminal=True,
        reason_code="agent_run_already_terminal",
        timestamp=_NOW,
    )
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        cancellation_result_override=terminal_without_handle,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_cancellation_handle_missing"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_cancel_rejects_substituted_nested_handle_identity(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    substituted_handle = handle.model_copy(update={"id": uuid4(), "state": AgentRunState.CANCELLED})
    unvalidated_result = CancellationResult.model_construct(
        handle_id=handle.id,
        handle=substituted_handle,
        accepted=True,
        terminal=True,
        reason_code="agent_run_cancelled",
        timestamp=_NOW,
    )
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        cancellation_result_override=unvalidated_result,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_cancellation_handle_mismatch"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_cancel_revalidates_terminal_result_handle_state(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    unvalidated_result = CancellationResult.model_construct(
        handle_id=handle.id,
        handle=handle,
        accepted=True,
        terminal=True,
        reason_code="agent_run_cancelled",
        timestamp=_NOW,
    )
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        cancellation_result_override=unvalidated_result,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_cancel_failed"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_audit_fails", [False, True])
async def test_cancel_maps_unsafe_runtime_reason_to_a_stable_failure(
    tmp_path: Path,
    outcome_audit_fails: bool,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        cancellation_reason_code="unsafe reason: provider payload",
    )
    audit = _AuditSink(order, fail_on_phase="outcome" if outcome_audit_fails else None)
    coordinator, _, _ = _coordinator(runtime=runtime, order=order, audit=audit)

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.cancellation_result is not None
    assert result.cancellation_result.reason_code == "unsafe reason: provider payload"
    if outcome_audit_fails:
        assert result.reason_code == "agent_run_audit_pending"
        assert result.audit_pending
        assert result.primary_reason_code == "agent_run_cancellation_reason_invalid"
        assert result.primary_outcome == "failure"
    else:
        assert result.reason_code == "agent_run_cancellation_reason_invalid"
        assert audit.events[-1].outcome == "failure"
        assert audit.events[-1].reason_code == "agent_run_cancellation_reason_invalid"


@pytest.mark.asyncio
async def test_cancel_rejects_unapproved_current_proof_before_runtime(tmp_path: Path) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order)
    authorizer = _Authorizer(
        order,
        decision_updates={"kill_switch_proof_id": request.kill_switch_proof_id},
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        authorizer=authorizer,
    )

    result = await coordinator.cancel(
        _context(request),
        ClientSurface.CLI,
        handle,
        request,
        uuid4(),
        "f" * 64,
    )

    assert not result.ok
    assert result.reason_code == "agent_run_kill_switch_proof_id_mismatch"
    assert "runtime:cancel" not in order


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [
        ("start", "agent_run_start_failed"),
        ("resume", "agent_run_resume_failed"),
        ("cancel", "agent_run_cancel_failed"),
    ],
)
async def test_runtime_exceptions_have_operation_specific_errors_and_outcome_audit(
    tmp_path: Path,
    operation: str,
    expected_reason: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        failures={operation: "provider_failed"},
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed_request = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(
            _context(resumed_request),
            ClientSurface.CLI,
            handle,
            resumed_request,
        )
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert not result.ok
    assert result.reason_code == expected_reason
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [
        ("start", "agent_run_start_failed"),
        ("resume", "agent_run_resume_failed"),
        ("cancel", "agent_run_cancel_failed"),
    ],
)
async def test_null_runtime_results_fail_closed_with_operation_specific_errors(
    tmp_path: Path,
    operation: str,
    expected_reason: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        null_results=frozenset({operation}),
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed_request = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(
            _context(resumed_request),
            ClientSurface.CLI,
            handle,
            resumed_request,
        )
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert not result.ok
    assert result.reason_code == expected_reason
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [
        ("start", "agent_run_start_failed"),
        ("resume", "agent_run_resume_failed"),
        ("cancel", "agent_run_cancel_failed"),
    ],
)
async def test_malformed_runtime_results_fail_closed_with_operation_specific_errors(
    tmp_path: Path,
    operation: str,
    expected_reason: str,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        malformed_results=frozenset({operation}),
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed_request = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(
            _context(resumed_request),
            ClientSurface.CLI,
            handle,
            resumed_request,
        )
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert not result.ok
    assert result.reason_code == expected_reason
    assert audit.events[-1].phase == "outcome"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
async def test_start_revalidates_nested_handle_before_dereference(tmp_path: Path) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
        malformed_results=frozenset({"start_nested"}),
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    result = await coordinator.start(_context(request), ClientSurface.CLI, request)

    assert not result.ok
    assert result.reason_code == "agent_run_start_failed"
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "primary_reason"),
    [
        ("start", "agent_run_start_failed"),
        ("resume", "agent_run_resume_failed"),
        ("cancel", "agent_run_cancel_failed"),
    ],
)
async def test_runtime_exception_with_missing_outcome_audit_is_pending(
    tmp_path: Path,
    operation: str,
    primary_reason: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(simulator, order, failures={operation: "provider_failed"})
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(order, fail_on_phase="outcome"),
    )

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(_context(resumed), ClientSurface.CLI, handle, resumed)
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert result.reason_code == "agent_run_audit_pending"
    assert result.audit_pending
    assert result.primary_reason_code == primary_reason
    assert result.primary_outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [
        ("start", "agent_run_start_handle_mismatch"),
        ("resume", "agent_run_resume_handle_mismatch"),
        ("cancel", "agent_run_cancellation_handle_mismatch"),
    ],
)
async def test_runtime_return_identity_substitution_is_rejected_before_success_audit(
    tmp_path: Path,
    operation: str,
    expected_reason: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        start_handle_updates={"run_id": uuid4()} if operation == "start" else None,
        resume_handle_updates={"id": uuid4()} if operation == "resume" else None,
        cancellation_handle_id=uuid4() if operation == "cancel" else None,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(_context(resumed), ClientSurface.CLI, handle, resumed)
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert not result.ok
    assert result.reason_code == expected_reason
    assert audit.events[-1].outcome == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "primary_reason"),
    [
        ("start", "agent_run_start_handle_mismatch"),
        ("resume", "agent_run_resume_handle_mismatch"),
        ("cancel", "agent_run_cancellation_handle_mismatch"),
    ],
)
async def test_identity_mismatch_with_unacknowledged_outcome_audit_is_pending(
    tmp_path: Path,
    operation: str,
    primary_reason: str,
) -> None:
    binding = _binding(tmp_path, workspace_id=uuid4(), project_id=uuid4())
    request = _request(binding)
    simulator = SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()})
    handle = (await simulator.start(request)).handle
    order: list[str] = []
    runtime = _RecordingRuntime(
        simulator,
        order,
        start_handle_updates={"run_id": uuid4()} if operation == "start" else None,
        resume_handle_updates={"id": uuid4()} if operation == "resume" else None,
        cancellation_handle_id=uuid4() if operation == "cancel" else None,
    )
    coordinator, _, _ = _coordinator(
        runtime=runtime,
        order=order,
        audit=_AuditSink(order, unacknowledged_on_phase="outcome"),
    )

    if operation == "start":
        result = await coordinator.start(_context(request), ClientSurface.CLI, request)
    elif operation == "resume":
        resumed = request.model_copy(update={"resume_handle_id": handle.id})
        result = await coordinator.resume(_context(resumed), ClientSurface.CLI, handle, resumed)
    else:
        result = await coordinator.cancel(
            _context(request),
            ClientSurface.CLI,
            handle,
            request,
            uuid4(),
            "f" * 64,
        )

    assert result.reason_code == "agent_run_audit_pending"
    assert result.audit_pending
    assert result.primary_reason_code == primary_reason
    assert result.primary_outcome == "failure"


def test_audit_schema_contains_no_sensitive_payload_fields() -> None:
    schema_text = str(AgentRunAuditEvent.model_json_schema()).casefold()

    for forbidden in (
        "prompt",
        "messages",
        "provider_output",
        "credential_value",
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
    ):
        assert forbidden not in schema_text


def test_agent_application_ports_remain_structurally_runtime_checkable() -> None:
    order: list[str] = []

    assert isinstance(_Authorizer(order), AgentRunAuthorizationPort)
    assert isinstance(_AuditSink(order), AgentRunAuditPort)
    assert isinstance(
        _RecordingRuntime(
            SimulatedAgentRuntime(bindings=(), event_templates={}),
            order,
        ),
        AgentRuntimePort,
    )


def test_authorization_decision_rejects_unstable_reason_and_naive_timestamp(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    authorization_request = _authorization_request(_context(request), request)

    with pytest.raises(ValidationError):
        _decision(authorization_request, reason_code="Not Stable")
    with pytest.raises(ValidationError, match="agent_run_timestamp_must_be_timezone_aware"):
        _decision(
            authorization_request,
            evaluated_at=datetime(2026, 7, 15, 12, 0),
        )


@pytest.mark.asyncio
async def test_audit_event_strictly_binds_snapshot_phase_reason_and_timestamp(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    binding = _binding(tmp_path, workspace_id=workspace_id, project_id=uuid4())
    request = _request(binding)
    context = _context(request)
    order: list[str] = []
    runtime = _RecordingRuntime(
        SimulatedAgentRuntime(bindings=(binding,), event_templates={binding.id: ()}),
        order,
    )
    coordinator, _, audit = _coordinator(runtime=runtime, order=order)
    await coordinator.start(context, ClientSurface.CLI, request)
    event = audit.events[0]

    assert event.authorization_snapshot_digest == context.authorization_snapshot_digest
    with pytest.raises(ValidationError, match="agent_run_audit_phase_outcome_mismatch"):
        AgentRunAuditEvent.model_validate(
            {**event.model_dump(), "phase": "authorization", "outcome": "success"}
        )
    with pytest.raises(ValidationError):
        AgentRunAuditEvent.model_validate({**event.model_dump(), "reason_code": "Not Stable"})
    with pytest.raises(ValidationError, match="agent_run_timestamp_must_be_timezone_aware"):
        AgentRunAuditEvent.model_validate(
            {**event.model_dump(), "timestamp": datetime(2026, 7, 15, 12, 0)}
        )
