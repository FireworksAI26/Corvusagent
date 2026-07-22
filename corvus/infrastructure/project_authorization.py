from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from corvus.application.authorization import (
    AuthorityEvaluationContext,
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationSnapshotExpectedInputs,
    AuthorizationSnapshotVerificationProof,
    evaluate_capability_intersection,
    verify_authorization_decision_snapshot,
)
from corvus.application.ports import (
    ProjectAuthorizationDecision,
    ProjectAuthorizationRequest,
)
from corvus.domain.access import AccessBundle, AgentGrant, CapabilityGrant, DelegationGrant
from corvus.domain.audit import AuthorizationDecisionSnapshot, WorkspaceSigningKeyVersion


class ProjectAuthorizationAdapterError(RuntimeError):
    pass


class ProjectAuthorizationInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: AuthorizationRequest
    authority_context: AuthorityEvaluationContext | None
    requester_bundle: AccessBundle
    requester_grants: tuple[CapabilityGrant, ...]
    agent_grant: AgentGrant | None
    agent_bundle: AccessBundle
    agent_capabilities: tuple[CapabilityGrant, ...]
    delegation_grants: tuple[DelegationGrant, ...] = ()


class ProjectAuthorizationInputProvider(Protocol):
    def resolve(self, request: ProjectAuthorizationRequest) -> ProjectAuthorizationInputs: ...


class VerifiedProjectAuthorizationInputs(ProjectAuthorizationInputs):
    snapshot: AuthorizationDecisionSnapshot
    snapshot_expected: AuthorizationSnapshotExpectedInputs
    snapshot_verification: AuthorizationSnapshotVerificationProof
    signing_key: WorkspaceSigningKeyVersion


class VerifiedProjectAuthorizationInputProvider(Protocol):
    def resolve(
        self, request: ProjectAuthorizationRequest
    ) -> VerifiedProjectAuthorizationInputs: ...


class AuthorizationSnapshotRepository(Protocol):
    def get_snapshot(
        self,
        *,
        workspace_id: UUID,
        snapshot_id: UUID,
    ) -> AuthorizationDecisionSnapshot | None: ...


class CapabilityIntersectionEvaluator(Protocol):
    def __call__(
        self,
        request: AuthorizationRequest,
        *,
        authority_context: AuthorityEvaluationContext | None,
        requester_bundle: AccessBundle,
        requester_grants: list[CapabilityGrant],
        agent_grant: AgentGrant | None,
        agent_bundle: AccessBundle,
        agent_capabilities: list[CapabilityGrant],
        delegation_grants: list[DelegationGrant],
    ) -> AuthorizationResult: ...


class EvaluatingProjectAuthorizationAdapter:
    def __init__(
        self,
        *,
        inputs: ProjectAuthorizationInputProvider,
        evaluator: CapabilityIntersectionEvaluator = evaluate_capability_intersection,
    ) -> None:
        self.inputs = inputs
        self.evaluator = evaluator

    def authorize(self, request: ProjectAuthorizationRequest) -> ProjectAuthorizationDecision:
        resolved = self.inputs.resolve(request)
        self._validate_bindings(request, resolved)
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
        return ProjectAuthorizationDecision(
            allowed=result.decision is AuthorizationDecision.ALLOW,
            reason_code=result.reason_code,
            authorization_snapshot_id=request.context.authorization_snapshot_id,
        )

    @staticmethod
    def _validate_bindings(
        project_request: ProjectAuthorizationRequest,
        inputs: ProjectAuthorizationInputs,
    ) -> None:
        context = project_request.context
        request = inputs.request
        transport_principal_id = context.transport_principal_id
        if transport_principal_id is None:
            raise ProjectAuthorizationAdapterError("project_transport_principal_missing")
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
            or request.client_surface is not project_request.client_surface
            or request.transport_principal_id != transport_principal_id
            or request.acting_agent_id != context.agent_id
            or request.resource_kind != "project"
            or request.resource_id != project_request.project_id
            or request.action != project_request.action
            or inputs.requester_bundle.id != context.access_bundle_id
            or inputs.requester_bundle.policy_digest != context.policy_digest
        ):
            raise ProjectAuthorizationAdapterError("project_authorization_input_mismatch")
        if inputs.agent_grant is not None and inputs.agent_grant.id != context.agent_grant_id:
            raise ProjectAuthorizationAdapterError("project_agent_grant_mismatch")


class VerifiedProjectAuthorizationAdapter:
    def __init__(
        self,
        *,
        inputs: VerifiedProjectAuthorizationInputProvider,
        snapshots: AuthorizationSnapshotRepository,
        evaluator: CapabilityIntersectionEvaluator = evaluate_capability_intersection,
    ) -> None:
        self.inputs = inputs
        self.snapshots = snapshots
        self.evaluator = evaluator

    def authorize(self, request: ProjectAuthorizationRequest) -> ProjectAuthorizationDecision:
        resolved = self.inputs.resolve(request)
        EvaluatingProjectAuthorizationAdapter._validate_bindings(request, resolved)
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
        snapshot = resolved.snapshot
        context = request.context
        persisted = self.snapshots.get_snapshot(
            workspace_id=context.workspace_id,
            snapshot_id=context.authorization_snapshot_id,
        )
        if persisted != snapshot:
            return self._deny(context.authorization_snapshot_id, "authorization_snapshot_missing")
        if (
            snapshot.id != context.authorization_snapshot_id
            or resolved.snapshot_expected.authorization_snapshot_digest
            != context.authorization_snapshot_digest
            or resolved.signing_key.id != context.authorization_signing_key_version_id
            or snapshot.request_context_id != context.id
            or snapshot.workspace_id != context.workspace_id
            or snapshot.scope_kind != context.scope_kind
            or snapshot.scope_id != context.scope_id
            or snapshot.requester_id != context.requester_id
            or snapshot.transport_principal_id != context.transport_principal_id
            or snapshot.decision != result.decision.value
            or snapshot.reason_code != result.reason_code
        ):
            return self._deny(
                context.authorization_snapshot_id,
                "authorization_snapshot_decision_mismatch",
            )
        verification = verify_authorization_decision_snapshot(
            snapshot,
            expected=resolved.snapshot_expected,
            signing_key=resolved.signing_key,
            verification_proof=resolved.snapshot_verification,
            verified_at=resolved.snapshot_expected.verified_at,
        )
        if verification.decision is not AuthorizationDecision.ALLOW:
            return self._deny(context.authorization_snapshot_id, verification.reason_code)
        return ProjectAuthorizationDecision(
            allowed=result.decision is AuthorizationDecision.ALLOW,
            reason_code=result.reason_code,
            authorization_snapshot_id=snapshot.id,
        )

    @staticmethod
    def _deny(snapshot_id: UUID, reason_code: str) -> ProjectAuthorizationDecision:
        return ProjectAuthorizationDecision(
            allowed=False,
            reason_code=reason_code,
            authorization_snapshot_id=snapshot_id,
        )
