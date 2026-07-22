from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from corvus.application.authorization import (
    AuthorityEvaluationContext,
    AuthorizationDecision,
    AuthorizationRequest,
    evaluate_capability_intersection,
    verify_authorization_decision_snapshot,
)
from corvus.application.ports import (
    AgentRunAuthorizationDecision,
    AgentRunAuthorizationRequest,
    AgentRunOperation,
)
from corvus.domain.agent_runtime import (
    AutonomyGrant,
    CapabilitySupport,
    ProviderBinding,
    ProviderStatus,
    ProviderTransport,
    canonicalize_digest_value,
    compute_agent_run_runtime_limit_digest,
    compute_autonomy_grant_digest,
    compute_provider_binding_digest,
)
from corvus.infrastructure.project_authorization import (
    AuthorizationSnapshotRepository,
    CapabilityIntersectionEvaluator,
    VerifiedProjectAuthorizationInputs,
)

_AGENT_RUN_CAPABILITY_UNAVAILABLE = "agent_run_capability_unavailable"


class VerifiedAgentRunAuthorizationInputs(VerifiedProjectAuthorizationInputs):
    autonomy_grant: AutonomyGrant
    provider_binding: ProviderBinding
    kill_switch_proof_id: UUID
    kill_switch_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _canonical_evidence_receipt(kind: str, payload: dict[str, object]) -> tuple[UUID, str]:
    encoded = json.dumps(
        canonicalize_digest_value(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return uuid5(NAMESPACE_URL, f"corvus:{kind}-evidence:{digest}"), digest


def canonical_credential_evidence_receipt(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> tuple[UUID, str] | None:
    claim_values = (
        request.provider_connection_id,
        request.credential_ref_id,
        request.credential_version_id,
        request.credential_grant_id,
    )
    proof = context.credential_verification_proof if context is not None else None
    credential_ref = context.credential_ref if context is not None else None
    evidence_present = context is not None and any(
        value is not None
        for value in (
            credential_ref,
            proof,
            context.expected_credential_rotation_epoch,
            context.expected_credential_nonce_digest,
        )
    )
    if not any(value is not None for value in claim_values):
        if evidence_present:
            raise ValueError("credential_evidence_mismatch")
        return None
    if (
        any(value is None for value in claim_values)
        or request.execution_placement_id is None
        or context is None
    ):
        raise ValueError("credential_evidence_mismatch")
    if credential_ref is None or proof is None:
        raise ValueError("credential_evidence_mismatch")
    if (
        credential_ref.id != request.credential_ref_id
        or credential_ref.provider_connection_id != request.provider_connection_id
        or proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.provider_connection_id != request.provider_connection_id
        or proof.credential_ref_id != request.credential_ref_id
        or proof.credential_ref_version != credential_ref.version
        or proof.credential_version_id != request.credential_version_id
        or proof.credential_grant_id != request.credential_grant_id
        or proof.acting_agent_id != request.acting_agent_id
        or proof.execution_placement_id != request.execution_placement_id
        or proof.operation != request.action
        or proof.rotation_epoch != context.expected_credential_rotation_epoch
        or proof.nonce_digest != context.expected_credential_nonce_digest
    ):
        raise ValueError("credential_evidence_mismatch")
    return _canonical_evidence_receipt(
        "credential",
        {
            "claims": {
                "execution_placement_id": str(request.execution_placement_id),
                "provider_connection_id": str(request.provider_connection_id),
                "credential_ref_id": str(request.credential_ref_id),
                "credential_version_id": str(request.credential_version_id),
                "credential_grant_id": str(request.credential_grant_id),
            },
            "credential_ref": credential_ref.model_dump(mode="python"),
            "verification_proof": proof.model_dump(mode="python"),
            "expected_rotation_epoch": context.expected_credential_rotation_epoch,
            "expected_nonce_digest": context.expected_credential_nonce_digest,
        },
    )


def canonical_budget_evidence_receipt(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> tuple[UUID, str] | None:
    claim_values = (
        request.budget_snapshot_ids or None,
        request.budget_snapshot_digest,
        request.runtime_limit_digest,
        request.budget_unit,
        request.budget_requested_amount,
    )
    proof = context.budget_runtime_verification_proof if context is not None else None
    if not any(value is not None for value in claim_values):
        if proof is not None:
            raise ValueError("budget_evidence_mismatch")
        return None
    if any(value is None for value in claim_values) or proof is None:
        raise ValueError("budget_evidence_mismatch")
    if (
        proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.scope_kind != request.scope_kind
        or proof.scope_id != request.scope_id
        or proof.action != request.action
        or proof.budget_snapshot_ids != request.budget_snapshot_ids
        or proof.budget_snapshot_digest != request.budget_snapshot_digest
        or proof.runtime_limit_digest != request.runtime_limit_digest
        or proof.unit != request.budget_unit
        or proof.requested_amount != request.budget_requested_amount
    ):
        raise ValueError("budget_evidence_mismatch")
    return _canonical_evidence_receipt(
        "budget",
        {
            "claims": {
                "budget_snapshot_ids": [str(value) for value in request.budget_snapshot_ids],
                "budget_snapshot_digest": request.budget_snapshot_digest,
                "runtime_limit_digest": request.runtime_limit_digest,
                "budget_unit": request.budget_unit,
                "budget_requested_amount": request.budget_requested_amount,
            },
            "verification_proof": proof.model_dump(mode="python"),
        },
    )


class VerifiedAgentRunAuthorizationInputProvider(Protocol):
    def resolve(
        self, request: AgentRunAuthorizationRequest
    ) -> VerifiedAgentRunAuthorizationInputs: ...


class VerifiedAgentRunAuthorizationAdapter:
    def __init__(
        self,
        *,
        inputs: VerifiedAgentRunAuthorizationInputProvider,
        snapshots: AuthorizationSnapshotRepository,
        evaluator: CapabilityIntersectionEvaluator = evaluate_capability_intersection,
    ) -> None:
        self.inputs = inputs
        self.snapshots = snapshots
        self.evaluator = evaluator

    def authorize(self, request: AgentRunAuthorizationRequest) -> AgentRunAuthorizationDecision:
        resolved = self.inputs.resolve(request)
        binding_denial = self._binding_denial(request, resolved)
        if binding_denial is not None:
            return self._decision(request, resolved, allowed=False, reason_code=binding_denial)

        result = self.evaluator(
            resolved.request,
            authority_context=resolved.authority_context,
            requester_bundle=resolved.requester_bundle,
            requester_grants=list(resolved.requester_grants),
            agent_grant=resolved.agent_grant,
            agent_bundle=resolved.agent_bundle,
            agent_capabilities=list(resolved.agent_capabilities),
            delegation_grants=list(resolved.delegation_grants),
        )
        if result.decision is not AuthorizationDecision.ALLOW:
            return self._decision(
                request,
                resolved,
                allowed=False,
                reason_code=result.reason_code,
            )

        snapshot_denial = self._snapshot_denial(request, resolved, result.reason_code)
        if snapshot_denial is not None:
            return self._decision(
                request,
                resolved,
                allowed=False,
                reason_code=snapshot_denial,
            )
        return self._decision(
            request,
            resolved,
            allowed=True,
            reason_code=result.reason_code,
        )

    @staticmethod
    def _binding_denial(
        agent_request: AgentRunAuthorizationRequest,
        inputs: VerifiedAgentRunAuthorizationInputs,
    ) -> str | None:
        context = agent_request.context
        request = inputs.request
        run_request = agent_request.request
        if context.transport_principal_id is None:
            return "agent_run_transport_principal_missing"
        if (
            request.workspace_id != context.workspace_id
            or request.request_context_id != context.id
            or request.deployment_instance_id != context.deployment_instance_id
            or request.workspace_authority_epoch != context.workspace_authority_epoch
            or request.workspace_authority_generation != context.workspace_authority_generation
            or request.authority_state_root != context.authority_state_root
            or request.authority_epoch_credential_id != context.authority_epoch_credential_id
            or request.authority_commit_receipt_id != context.authority_commit_receipt_id
            or request.authority_proof_digest != context.authority_proof_digest
            or request.scope_kind != context.scope_kind
            or request.scope_id != context.scope_id
            or request.scope_digest != context.scope_digest
            or request.audience_policy_snapshot_id != context.audience_policy_snapshot_id
            or request.audience_policy_digest != context.audience_policy_digest
            or request.requester_id != context.requester_id
            or request.client_context_id != context.client_context_id
            or request.client_surface is not agent_request.client_surface
            or request.transport_principal_id != context.transport_principal_id
            or request.acting_agent_id != context.agent_id
            or request.resource_kind != "agent_run"
            or request.resource_id != run_request.run_id
            or request.action != agent_request.operation.value
            or inputs.requester_bundle.id != context.access_bundle_id
            or inputs.requester_bundle.policy_digest != context.policy_digest
        ):
            return "agent_run_authorization_input_mismatch"
        if inputs.agent_grant is not None and inputs.agent_grant.id != context.agent_grant_id:
            return "agent_run_agent_grant_mismatch"

        is_cancellation = agent_request.operation is AgentRunOperation.CANCEL
        autonomy = inputs.autonomy_grant
        filesystem_paths = tuple(Path(value) for value in run_request.filesystem_envelope)
        filesystem_authorized = all(
            path.is_absolute()
            and path == path.resolve(strict=False)
            and any(path.is_relative_to(root) for root in autonomy.allowed_roots)
            for path in filesystem_paths
        )
        if (
            autonomy.id != run_request.autonomy_grant_id
            or autonomy.workspace_id != run_request.workspace_id
            or autonomy.project_id != run_request.project_id
            or compute_autonomy_grant_digest(autonomy) != run_request.autonomy_grant_digest
            or autonomy.revoked_at is not None
            or autonomy.issued_at > request.evaluated_at
            or (not is_cancellation and autonomy.expires_at <= request.evaluated_at)
            or (not is_cancellation and run_request.deadline <= request.evaluated_at)
            or autonomy.wall_clock_deadline < run_request.deadline
            or autonomy.expires_at < run_request.deadline
            or autonomy.credential_grant_ids != run_request.credential_grant_ids
            or run_request.sandbox_profile not in autonomy.allowed_sandbox_profiles
            or not filesystem_authorized
            or not set(run_request.network_envelope).issubset(autonomy.allowed_network_destinations)
            or not set(run_request.tool_envelope).issubset(autonomy.allowed_tool_ids)
            or not run_request.requested_effect_classes.issubset(autonomy.allowed_effect_classes)
            or bool(
                run_request.requested_effect_classes
                & (autonomy.denied_effect_classes | autonomy.always_block_effects)
            )
            or run_request.provider_spend_limit > autonomy.provider_spend_ceiling
            or run_request.corvus_budget_limit > autonomy.corvus_budget_ceiling
            or run_request.approval_limit > autonomy.approval_ceiling
            or run_request.max_retries > autonomy.max_retries
            or run_request.max_turns > autonomy.max_turns
            or autonomy.max_output_tokens < run_request.max_output_tokens
            or autonomy.max_output_bytes < run_request.max_output_bytes
        ):
            return "stale_autonomy_grant"

        provider = inputs.provider_binding
        if (
            provider.id != run_request.provider_binding_id
            or provider.workspace_id != run_request.workspace_id
            or provider.project_id != run_request.project_id
            or provider.model != run_request.model
            or provider.version != run_request.provider_binding_version
            or compute_provider_binding_digest(provider) != run_request.provider_binding_digest
        ):
            return "provider_binding_digest_mismatch"
        if not is_cancellation and provider.status is not ProviderStatus.AVAILABLE:
            return "agent_run_provider_unavailable"
        if (
            is_cancellation
            and provider.capabilities.provider_side_cancellation is not CapabilitySupport.SUPPORTED
        ):
            return _AGENT_RUN_CAPABILITY_UNAVAILABLE

        canonical_credential_grant_ids = (
            (request.credential_grant_id,) if request.credential_grant_id is not None else ()
        )
        if (
            run_request.credential_grant_ids != canonical_credential_grant_ids
            or (
                provider.transport is ProviderTransport.HTTP_API
                and (
                    provider.credential_ref_id is None
                    or provider.credential_ref_id != request.credential_ref_id
                    or request.credential_grant_id is None
                )
            )
            or (
                provider.transport is ProviderTransport.LOCAL_CLI
                and (request.credential_ref_id is not None or canonical_credential_grant_ids)
            )
        ):
            return "stale_credential_proof"

        if not is_cancellation and (
            request.runtime_limit_digest is None
            or request.budget_unit is None
            or request.budget_requested_amount is None
            or run_request.budget_unit != request.budget_unit
            or run_request.budget_requested_amount != request.budget_requested_amount
            or compute_agent_run_runtime_limit_digest(run_request) != request.runtime_limit_digest
        ):
            return "agent_run_over_budget"

        current_kill_id = (
            agent_request.current_kill_switch_proof_id or run_request.kill_switch_proof_id
        )
        current_kill_digest = (
            agent_request.current_kill_switch_proof_digest or run_request.kill_switch_proof_digest
        )
        try:
            credential_receipt = canonical_credential_evidence_receipt(
                request,
                inputs.authority_context,
            )
        except ValueError:
            return "stale_credential_proof"
        if credential_receipt != (
            (run_request.credential_proof_id, run_request.credential_proof_digest)
            if run_request.credential_proof_id is not None
            and run_request.credential_proof_digest is not None
            else None
        ):
            return "stale_credential_proof"
        if not is_cancellation:
            try:
                budget_receipt = canonical_budget_evidence_receipt(
                    request,
                    inputs.authority_context,
                )
            except ValueError:
                return "agent_run_over_budget"
            if budget_receipt != (
                (run_request.budget_proof_id, run_request.budget_proof_digest)
                if run_request.budget_proof_id is not None
                and run_request.budget_proof_digest is not None
                else None
            ):
                return "agent_run_over_budget"
        if (
            inputs.kill_switch_proof_id != current_kill_id
            or inputs.kill_switch_proof_digest != current_kill_digest
        ):
            return "agent_run_kill_switch_active"
        return None

    def _snapshot_denial(
        self,
        request: AgentRunAuthorizationRequest,
        inputs: VerifiedAgentRunAuthorizationInputs,
        current_reason_code: str,
    ) -> str | None:
        snapshot = inputs.snapshot
        context = request.context
        persisted = self.snapshots.get_snapshot(
            workspace_id=context.workspace_id,
            snapshot_id=context.authorization_snapshot_id,
        )
        if persisted != snapshot:
            return "authorization_snapshot_missing"
        if (
            snapshot.id != context.authorization_snapshot_id
            or inputs.snapshot_expected.authorization_snapshot_digest
            != context.authorization_snapshot_digest
            or inputs.signing_key.id != context.authorization_signing_key_version_id
            or snapshot.request_context_id != context.id
            or snapshot.workspace_id != context.workspace_id
            or snapshot.scope_kind != context.scope_kind
            or snapshot.scope_id != context.scope_id
            or snapshot.requester_id != context.requester_id
            or snapshot.transport_principal_id != context.transport_principal_id
            or snapshot.decision != AuthorizationDecision.ALLOW.value
            or snapshot.reason_code != current_reason_code
        ):
            return "authorization_snapshot_decision_mismatch"
        verification = verify_authorization_decision_snapshot(
            snapshot,
            expected=inputs.snapshot_expected,
            signing_key=inputs.signing_key,
            verification_proof=inputs.snapshot_verification,
            verified_at=inputs.snapshot_expected.verified_at,
        )
        if verification.decision is not AuthorizationDecision.ALLOW:
            return verification.reason_code
        return None

    @staticmethod
    def _decision(
        request: AgentRunAuthorizationRequest,
        inputs: VerifiedAgentRunAuthorizationInputs,
        *,
        allowed: bool,
        reason_code: str,
    ) -> AgentRunAuthorizationDecision:
        run_request = request.request
        return AgentRunAuthorizationDecision(
            allowed=allowed,
            reason_code=reason_code,
            authorization_snapshot_id=request.context.authorization_snapshot_id,
            canonical_request_digest=request.canonical_request_digest,
            immutable_request_digest=run_request.immutable_request_digest,
            provider_binding_version=run_request.provider_binding_version,
            provider_binding_digest=run_request.provider_binding_digest,
            autonomy_grant_digest=run_request.autonomy_grant_digest,
            credential_proof_digest=run_request.credential_proof_digest,
            budget_proof_digest=run_request.budget_proof_digest,
            kill_switch_proof_id=(
                request.current_kill_switch_proof_id or run_request.kill_switch_proof_id
            ),
            kill_switch_proof_digest=(
                request.current_kill_switch_proof_digest or run_request.kill_switch_proof_digest
            ),
            evaluated_at=inputs.request.evaluated_at,
        )
