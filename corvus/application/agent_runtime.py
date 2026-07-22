from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    CancellationResult,
    CapabilitySupport,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderHealth,
    ProviderStatus,
    compute_agent_run_request_digest,
)
from corvus.domain.client import ClientSurface
from corvus.domain.request import RequestContext

_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_INVALID_CANCELLATION_REASON = "agent_run_cancellation_reason_invalid"
_MISSING_CANCELLATION_HANDLE = "agent_run_cancellation_handle_missing"
_CAPABILITY_UNAVAILABLE_REASON = "agent_run_capability_unavailable"
_EFFECT_CAPABILITY_PREFIXES = (
    ("mcp", "mcp"),
    ("repository.read", "repository_read"),
    ("repository.write", "repository_write"),
    ("shell", "shell"),
)


@dataclass(frozen=True)
class _AuthorizedAgentRun:
    request: AgentRunAuthorizationRequest
    receipt: AgentRunAuditReceipt


def _revalidate_handle(value: object) -> AgentRunHandle:
    if not isinstance(value, AgentRunHandle):
        raise TypeError("agent_run_handle_invalid")
    return AgentRunHandle.model_validate(
        {field_name: getattr(value, field_name) for field_name in AgentRunHandle.model_fields}
    )


def _revalidate_start_result(value: object) -> AgentRunStartResult:
    if not isinstance(value, AgentRunStartResult) or not isinstance(value.replayed, bool):
        raise TypeError("agent_run_start_result_invalid")
    return AgentRunStartResult(
        handle=_revalidate_handle(value.handle),
        replayed=value.replayed,
    )


def _revalidate_candidate(value: object) -> ProviderCandidate:
    if not isinstance(value, ProviderCandidate):
        raise TypeError("agent_run_provider_candidate_invalid")
    return ProviderCandidate.model_validate(value.model_dump(mode="python"))


def _revalidate_capabilities(value: object) -> AgentCapabilities:
    if not isinstance(value, AgentCapabilities):
        raise TypeError("agent_run_provider_capabilities_invalid")
    return AgentCapabilities.model_validate(value.model_dump(mode="python"))


def _revalidate_health(value: object) -> ProviderHealth:
    if not isinstance(value, ProviderHealth):
        raise TypeError("agent_run_provider_health_invalid")
    return ProviderHealth.model_validate(value.model_dump(mode="python"))


def _revalidate_cancellation_result(value: object) -> CancellationResult:
    if (
        not isinstance(value, CancellationResult)
        or not isinstance(value.accepted, bool)
        or not isinstance(value.terminal, bool)
    ):
        raise TypeError("agent_run_cancellation_result_invalid")
    return CancellationResult(
        handle_id=value.handle_id,
        handle=_revalidate_handle(value.handle) if value.handle is not None else None,
        accepted=value.accepted,
        terminal=value.terminal,
        reason_code=value.reason_code,
        timestamp=value.timestamp,
    )


def _validation_contains_reason(error: ValidationError, reason_code: str) -> bool:
    return any(reason_code in str(item.get("ctx", {}).get("error", "")) for item in error.errors())


class AgentRunOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    request_context_id: UUID
    operation: AgentRunOperation
    ok: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=200)
    handle: AgentRunHandle | None = None
    cancellation_result: CancellationResult | None = None
    identical_start_replayed: bool = False
    audit_pending: bool = False
    primary_reason_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        max_length=200,
    )
    primary_outcome: Literal["success", "failure"] | None = None


class AgentRuntimeCoordinator:
    def __init__(
        self,
        *,
        runtime: AgentRuntimePort,
        authorization: AgentRunAuthorizationPort,
        audit: AgentRunAuditPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._runtime = runtime
        self._authorization = authorization
        self._audit = audit
        self._clock = clock

    async def start(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        request: AgentRunRequest,
    ) -> AgentRunOperationResult:
        authorization, failure = self._authorization_gate(
            context=context,
            client_surface=client_surface,
            operation=AgentRunOperation.START,
            request=request,
        )
        if failure is not None:
            return failure
        if authorization is None:
            return self._failure(
                context,
                AgentRunOperation.START,
                "agent_run_authorization_unavailable",
            )
        try:
            preflight_reason = await self._provider_preflight(
                authorization.request,
                operation=AgentRunOperation.START,
            )
            if preflight_reason is not None:
                return self._post_authorization_failure(
                    authorization,
                    reason_code=preflight_reason,
                )
            start_result = _revalidate_start_result(await self._runtime.start(request))
        except Exception as exc:
            reason_code = (
                "agent_run_idempotency_mismatch"
                if getattr(exc, "reason_code", None) == "agent_run_idempotency_mismatch"
                else "agent_run_start_failed"
            )
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
            )
        handle = start_result.handle
        if (
            handle.run_id != request.run_id
            or handle.provider_binding_id != request.provider_binding_id
        ):
            reason_code = "agent_run_start_handle_mismatch"
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
                handle=handle,
            )
        if not self._record_outcome(
            authorization,
            outcome="success",
            reason_code="agent_run_started",
            handle=handle,
        ):
            return self._audit_pending(
                context,
                AgentRunOperation.START,
                handle=handle,
                identical_start_replayed=start_result.replayed,
                primary_reason_code="agent_run_started",
                primary_outcome="success",
            )
        return AgentRunOperationResult(
            request_context_id=context.id,
            operation=AgentRunOperation.START,
            ok=True,
            reason_code="agent_run_started",
            handle=handle,
            identical_start_replayed=start_result.replayed,
        )

    async def resume(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunOperationResult:
        authorization, failure = self._authorization_gate(
            context=context,
            client_surface=client_surface,
            operation=AgentRunOperation.RESUME,
            request=request_with_fresh_proofs,
            handle=handle,
        )
        if failure is not None:
            return failure
        if authorization is None:
            return self._failure(
                context,
                AgentRunOperation.RESUME,
                "agent_run_authorization_unavailable",
            )
        try:
            preflight_reason = await self._provider_preflight(
                authorization.request,
                operation=AgentRunOperation.RESUME,
            )
            if preflight_reason is not None:
                return self._post_authorization_failure(
                    authorization,
                    reason_code=preflight_reason,
                    handle=handle,
                )
            resumed_handle = _revalidate_handle(
                await self._runtime.resume(
                    handle,
                    request_with_fresh_proofs,
                )
            )
        except Exception:
            reason_code = "agent_run_resume_failed"
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
                handle=handle,
            )
        if (
            resumed_handle.id != handle.id
            or resumed_handle.run_id != handle.run_id
            or resumed_handle.provider_binding_id != handle.provider_binding_id
        ):
            reason_code = "agent_run_resume_handle_mismatch"
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
                handle=resumed_handle,
            )
        if not self._record_outcome(
            authorization,
            outcome="success",
            reason_code="agent_run_resumed",
            handle=resumed_handle,
        ):
            return self._audit_pending(
                context,
                AgentRunOperation.RESUME,
                handle=resumed_handle,
                primary_reason_code="agent_run_resumed",
                primary_outcome="success",
            )
        return AgentRunOperationResult(
            request_context_id=context.id,
            operation=AgentRunOperation.RESUME,
            ok=True,
            reason_code="agent_run_resumed",
            handle=resumed_handle,
        )

    async def cancel(
        self,
        context: RequestContext,
        client_surface: ClientSurface,
        handle: AgentRunHandle,
        request: AgentRunRequest,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> AgentRunOperationResult:
        authorization, failure = self._authorization_gate(
            context=context,
            client_surface=client_surface,
            operation=AgentRunOperation.CANCEL,
            request=request,
            handle=handle,
            current_kill_switch_proof_id=current_kill_switch_proof_id,
            current_kill_switch_proof_digest=current_kill_switch_proof_digest,
        )
        if failure is not None:
            return failure
        if authorization is None:
            return self._failure(
                context,
                AgentRunOperation.CANCEL,
                "agent_run_authorization_unavailable",
            )
        try:
            cancellation_result = _revalidate_cancellation_result(
                await self._runtime.cancel(
                    handle,
                    current_kill_switch_proof_id,
                    current_kill_switch_proof_digest,
                )
            )
        except Exception as exc:
            reason_code = (
                "agent_run_cancellation_handle_mismatch"
                if isinstance(exc, ValidationError)
                and _validation_contains_reason(exc, "cancellation_handle_id_mismatch")
                else "agent_run_cancel_failed"
            )
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
                handle=handle,
            )
        if cancellation_result.handle_id != handle.id:
            reason_code = "agent_run_cancellation_handle_mismatch"
            return self._post_authorization_failure(
                authorization,
                reason_code=reason_code,
                handle=handle,
            )
        if _REASON_CODE_PATTERN.fullmatch(cancellation_result.reason_code) is None:
            return self._post_authorization_failure(
                authorization,
                reason_code=_INVALID_CANCELLATION_REASON,
                handle=handle,
                cancellation_result=cancellation_result,
            )
        if not cancellation_result.accepted and not cancellation_result.terminal:
            return self._post_authorization_failure(
                authorization,
                reason_code=cancellation_result.reason_code,
                handle=handle,
                cancellation_result=cancellation_result,
            )
        if cancellation_result.terminal and cancellation_result.handle is None:
            return self._post_authorization_failure(
                authorization,
                reason_code=_MISSING_CANCELLATION_HANDLE,
                handle=handle,
                cancellation_result=cancellation_result,
            )
        result_handle = cancellation_result.handle or handle
        if (
            result_handle.id != handle.id
            or result_handle.run_id != handle.run_id
            or result_handle.provider_binding_id != handle.provider_binding_id
        ):
            return self._post_authorization_failure(
                authorization,
                reason_code="agent_run_cancellation_handle_mismatch",
                handle=handle,
            )
        if not self._record_outcome(
            authorization,
            outcome="success",
            reason_code=cancellation_result.reason_code,
            handle=result_handle,
        ):
            return self._audit_pending(
                context,
                AgentRunOperation.CANCEL,
                handle=result_handle,
                cancellation_result=cancellation_result,
                primary_reason_code=cancellation_result.reason_code,
                primary_outcome="success",
            )
        return AgentRunOperationResult(
            request_context_id=context.id,
            operation=AgentRunOperation.CANCEL,
            ok=True,
            reason_code=cancellation_result.reason_code,
            handle=result_handle,
            cancellation_result=cancellation_result,
        )

    def _authorization_gate(
        self,
        *,
        context: RequestContext,
        client_surface: ClientSurface,
        operation: AgentRunOperation,
        request: AgentRunRequest,
        handle: AgentRunHandle | None = None,
        current_kill_switch_proof_id: UUID | None = None,
        current_kill_switch_proof_digest: str | None = None,
    ) -> tuple[_AuthorizedAgentRun | None, AgentRunOperationResult | None]:
        try:
            authorization_request = AgentRunAuthorizationRequest(
                context=context,
                client_surface=client_surface,
                operation=operation,
                request=request,
                handle=handle,
                canonical_request_digest=compute_agent_run_request_digest(request),
                current_kill_switch_proof_id=current_kill_switch_proof_id,
                current_kill_switch_proof_digest=current_kill_switch_proof_digest,
            )
        except ValidationError as exc:
            return None, self._denial_from_validation(context, operation, exc)
        try:
            raw_decision = self._authorization.authorize(authorization_request)
            if not isinstance(raw_decision, AgentRunAuthorizationDecision) or not isinstance(
                raw_decision.allowed, bool
            ):
                raise TypeError("agent_run_authorization_decision_invalid")
            decision = AgentRunAuthorizationDecision.model_validate(
                {
                    field_name: getattr(raw_decision, field_name)
                    for field_name in AgentRunAuthorizationDecision.model_fields
                }
            )
            coordinator_time = self._clock()
            if coordinator_time.tzinfo is None or coordinator_time.utcoffset() is None:
                raise ValueError("coordinator_clock_must_be_timezone_aware")
        except Exception:
            return None, self._failure(
                context,
                operation,
                "agent_run_authorization_unavailable",
            )
        binding_mismatch = self._decision_binding_mismatch(
            authorization_request,
            decision,
            coordinator_time=coordinator_time,
        )
        authorization_outcome: Literal["allow", "deny"] = (
            "allow" if decision.allowed and binding_mismatch is None else "deny"
        )
        authorization_reason = binding_mismatch or decision.reason_code
        try:
            event = self._audit_event(
                authorization_request,
                phase="authorization",
                outcome=authorization_outcome,
                reason_code=authorization_reason,
            )
            receipt = self._audit.record(event)
        except Exception:
            return None, self._failure(
                context,
                operation,
                "agent_run_audit_unavailable",
            )
        if not self._receipt_acknowledges_event(receipt, event):
            return None, self._failure(
                context,
                operation,
                "agent_run_audit_unavailable",
            )
        if binding_mismatch is not None or not decision.allowed:
            return None, self._failure(context, operation, authorization_reason)
        return _AuthorizedAgentRun(request=authorization_request, receipt=receipt), None

    @staticmethod
    def _decision_binding_mismatch(
        request: AgentRunAuthorizationRequest,
        decision: AgentRunAuthorizationDecision,
        *,
        coordinator_time: datetime,
    ) -> str | None:
        expected = request.request
        checks: tuple[tuple[bool, str], ...] = (
            (
                decision.authorization_snapshot_id == request.context.authorization_snapshot_id,
                "agent_run_authorization_snapshot_mismatch",
            ),
            (
                decision.canonical_request_digest == request.canonical_request_digest,
                "agent_run_request_digest_mismatch",
            ),
            (
                decision.immutable_request_digest == expected.immutable_request_digest,
                "resume_request_substitution",
            ),
            (
                decision.provider_binding_version == expected.provider_binding_version,
                "provider_binding_digest_mismatch",
            ),
            (
                decision.provider_binding_digest == expected.provider_binding_digest,
                "provider_binding_digest_mismatch",
            ),
            (
                decision.autonomy_grant_digest == expected.autonomy_grant_digest,
                "agent_run_autonomy_grant_digest_mismatch",
            ),
            (
                decision.credential_proof_digest == expected.credential_proof_digest,
                "agent_run_credential_proof_digest_mismatch",
            ),
            (
                decision.budget_proof_digest == expected.budget_proof_digest,
                "agent_run_budget_proof_digest_mismatch",
            ),
            (
                decision.kill_switch_proof_id
                == (request.current_kill_switch_proof_id or expected.kill_switch_proof_id),
                "agent_run_kill_switch_proof_id_mismatch",
            ),
            (
                decision.kill_switch_proof_digest
                == (request.current_kill_switch_proof_digest or expected.kill_switch_proof_digest),
                "agent_run_kill_switch_proof_digest_mismatch",
            ),
            (
                decision.evaluated_at <= coordinator_time
                and (
                    request.operation is AgentRunOperation.CANCEL
                    or coordinator_time < expected.deadline
                ),
                "agent_run_authorization_time_invalid",
            ),
        )
        return next((reason_code for matches, reason_code in checks if not matches), None)

    async def _provider_preflight(
        self,
        request: AgentRunAuthorizationRequest,
        *,
        operation: AgentRunOperation,
    ) -> str | None:
        run_request = request.request
        try:
            query = ProviderDiscoveryQuery(
                workspace_id=run_request.workspace_id,
                project_id=run_request.project_id,
            )
            raw_candidates = await self._runtime.discover(query)
            if not isinstance(raw_candidates, tuple):
                return "agent_run_provider_unavailable"
            candidates = tuple(_revalidate_candidate(item) for item in raw_candidates)
            if any(
                item.binding.workspace_id != query.workspace_id
                or item.binding.project_id != query.project_id
                for item in candidates
            ):
                return "agent_run_provider_unavailable"
            matching = tuple(
                item for item in candidates if item.binding.id == run_request.provider_binding_id
            )
            if len(matching) != 1:
                return "agent_run_provider_unavailable"
            candidate = matching[0]
            if (
                candidate.binding_version != run_request.provider_binding_version
                or candidate.binding_digest != run_request.provider_binding_digest
            ):
                return "provider_binding_digest_mismatch"
            runtime_capabilities = _revalidate_capabilities(
                self._runtime.capabilities(candidate.binding)
            )
            required_capabilities = self._required_capabilities(run_request, operation)
            if any(
                getattr(candidate.binding.capabilities, capability)
                is not CapabilitySupport.SUPPORTED
                or getattr(runtime_capabilities, capability) is not CapabilitySupport.SUPPORTED
                for capability in required_capabilities
            ):
                return _CAPABILITY_UNAVAILABLE_REASON
            if operation is AgentRunOperation.CANCEL:
                return None
            health = _revalidate_health(await self._runtime.health(candidate.binding))
            if (
                health.binding_id != run_request.provider_binding_id
                or health.binding_version != run_request.provider_binding_version
                or health.binding_digest != run_request.provider_binding_digest
            ):
                return "provider_binding_digest_mismatch"
            if health.status is not ProviderStatus.AVAILABLE:
                return "agent_run_provider_unavailable"
        except Exception:
            return "agent_run_provider_unavailable"
        return None

    @staticmethod
    def _required_capabilities(
        request: AgentRunRequest,
        operation: AgentRunOperation,
    ) -> frozenset[str]:
        if operation is AgentRunOperation.CANCEL:
            return frozenset({"provider_side_cancellation"})
        required = {"text"}
        if request.filesystem_envelope:
            required.add("repository_read")
        if request.tool_envelope:
            required.add("tools")
        effect_names = (*request.requested_effect_classes, *request.tool_envelope)
        for effect_name in effect_names:
            for prefix, capability in _EFFECT_CAPABILITY_PREFIXES:
                if effect_name == prefix or effect_name.startswith(f"{prefix}."):
                    required.add(capability)
        if request.provider_spend_limit > 0:
            required.add("provider_side_budget")
        if operation is AgentRunOperation.RESUME:
            required.add("session_resume")
        return frozenset(required)

    def _post_authorization_failure(
        self,
        authorization: _AuthorizedAgentRun,
        *,
        reason_code: str,
        handle: AgentRunHandle | None = None,
        cancellation_result: CancellationResult | None = None,
    ) -> AgentRunOperationResult:
        if not self._record_outcome(
            authorization,
            outcome="failure",
            reason_code=reason_code,
            handle=handle,
        ):
            return self._audit_pending(
                authorization.request.context,
                authorization.request.operation,
                handle=handle,
                cancellation_result=cancellation_result,
                primary_reason_code=reason_code,
                primary_outcome="failure",
            )
        return AgentRunOperationResult(
            request_context_id=authorization.request.context.id,
            operation=authorization.request.operation,
            ok=False,
            reason_code=reason_code,
            handle=handle,
            cancellation_result=cancellation_result,
            primary_reason_code=reason_code,
            primary_outcome="failure",
        )

    def _record_outcome(
        self,
        authorization: _AuthorizedAgentRun,
        *,
        outcome: Literal["success", "failure"],
        reason_code: str,
        handle: AgentRunHandle | None = None,
    ) -> bool:
        try:
            event = self._audit_event(
                authorization.request,
                phase="outcome",
                outcome=outcome,
                reason_code=reason_code,
                handle=handle,
            )
            receipt = self._audit.record(event)
        except Exception:
            return False
        return self._receipt_acknowledges_event(
            receipt,
            event,
            previous_receipt=authorization.receipt,
        )

    @staticmethod
    def _receipt_acknowledges_event(
        receipt: object,
        event: AgentRunAuditEvent,
        *,
        previous_receipt: AgentRunAuditReceipt | None = None,
    ) -> bool:
        if not isinstance(receipt, AgentRunAuditReceipt):
            return False
        expected_event_digest = compute_agent_run_audit_event_digest(event)
        expected_receipt_digest = compute_agent_run_audit_receipt_digest(
            sequence=receipt.sequence,
            previous_receipt_digest=receipt.previous_receipt_digest,
            event_digest=receipt.event_digest,
            acknowledged=receipt.acknowledged,
        )
        continuity_matches = previous_receipt is None or (
            receipt.sequence == previous_receipt.sequence + 1
            and hmac.compare_digest(
                receipt.previous_receipt_digest,
                previous_receipt.receipt_digest,
            )
        )
        return (
            receipt.acknowledged
            and continuity_matches
            and hmac.compare_digest(receipt.event_digest, expected_event_digest)
            and hmac.compare_digest(receipt.receipt_digest, expected_receipt_digest)
        )

    @staticmethod
    def _audit_pending(
        context: RequestContext,
        operation: AgentRunOperation,
        *,
        handle: AgentRunHandle | None = None,
        cancellation_result: CancellationResult | None = None,
        identical_start_replayed: bool = False,
        primary_reason_code: str,
        primary_outcome: Literal["success", "failure"],
    ) -> AgentRunOperationResult:
        return AgentRunOperationResult(
            request_context_id=context.id,
            operation=operation,
            ok=False,
            reason_code="agent_run_audit_pending",
            handle=handle,
            cancellation_result=cancellation_result,
            identical_start_replayed=identical_start_replayed,
            audit_pending=True,
            primary_reason_code=primary_reason_code,
            primary_outcome=primary_outcome,
        )

    @staticmethod
    def _failure(
        context: RequestContext,
        operation: AgentRunOperation,
        reason_code: str,
    ) -> AgentRunOperationResult:
        return AgentRunOperationResult(
            request_context_id=context.id,
            operation=operation,
            ok=False,
            reason_code=reason_code,
        )

    def _denial_from_validation(
        self,
        context: RequestContext,
        operation: AgentRunOperation,
        error: ValidationError,
    ) -> AgentRunOperationResult:
        first_error = error.errors()[0]
        error_context = first_error.get("ctx") or {}
        reason_code = str(error_context.get("reason_code", "agent_run_request_invalid"))
        return self._failure(context, operation, reason_code)

    def _audit_event(
        self,
        request: AgentRunAuthorizationRequest,
        *,
        phase: Literal["authorization", "outcome"],
        outcome: Literal["allow", "deny", "success", "failure"],
        reason_code: str,
        handle: AgentRunHandle | None = None,
    ) -> AgentRunAuditEvent:
        run_request = request.request
        return AgentRunAuditEvent(
            context=request.context,
            client_surface=request.client_surface,
            operation=request.operation,
            run_id=run_request.run_id,
            handle_id=(
                handle.id
                if handle is not None
                else request.handle.id
                if request.handle is not None
                else None
            ),
            provider_binding_id=run_request.provider_binding_id,
            provider_binding_version=run_request.provider_binding_version,
            provider_binding_digest=run_request.provider_binding_digest,
            authorization_snapshot_id=request.context.authorization_snapshot_id,
            authorization_snapshot_digest=(request.context.authorization_snapshot_digest),
            canonical_request_digest=request.canonical_request_digest,
            immutable_request_digest=run_request.immutable_request_digest,
            autonomy_grant_id=run_request.autonomy_grant_id,
            autonomy_grant_digest=run_request.autonomy_grant_digest,
            credential_proof_id=run_request.credential_proof_id,
            credential_proof_digest=run_request.credential_proof_digest,
            budget_proof_id=run_request.budget_proof_id,
            budget_proof_digest=run_request.budget_proof_digest,
            kill_switch_proof_id=(
                request.current_kill_switch_proof_id or run_request.kill_switch_proof_id
            ),
            kill_switch_proof_digest=(
                request.current_kill_switch_proof_digest or run_request.kill_switch_proof_digest
            ),
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            timestamp=self._clock(),
        )
