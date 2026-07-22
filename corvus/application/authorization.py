from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from corvus.domain.access import (
    AccessBundle,
    AgentGrant,
    CapabilityEffect,
    CapabilityGrant,
    CredentialRef,
    CredentialStatus,
    DelegationGrant,
)
from corvus.domain.audit import (
    AuthorizationDecisionSnapshot,
    WorkspaceSigningKeyVersion,
    authorization_snapshot_digest,
    validate_signing_time,
)
from corvus.domain.client import ClientContext, ClientSurface
from corvus.domain.deployment import (
    AuthorityContractError,
    AuthorityEpochCredential,
    AuthorityEpochCredentialStatus,
    AuthorityRegistryFreshnessProof,
    AuthorityRegistryTrustState,
    AuthorityRegistryVerifierKeyVersion,
    AuthorityStateRootLeafFamily,
    AuthorityStateRootManifestVersion,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorKind,
    AuthorityTrustAnchorStatus,
    DeploymentInstance,
    DeploymentInstanceLease,
    DeploymentInstanceStatus,
    ManifestStatus,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
    fixed_workspace_lock_name,
    validate_authority_root_manifest,
    validate_registry_freshness_proof,
    validate_registry_trust_transition,
    validate_registry_verifier_time,
)
from corvus.domain.execution import ExecutionPlacement, ExecutionStatus
from corvus.domain.scope import AudiencePolicySnapshot

_AGENT_RUN_RESOURCE_KIND = "agent_run"
_AGENT_RUN_CANCEL_ACTION = "agent_run.cancel"


class AuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class KillSwitchScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_kind: Literal["workspace", "agent", "workflow", "run"]
    scope_id: UUID


class KillSwitchSnapshotEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    workspace_id: UUID
    scope_kind: Literal["workspace", "agent", "workflow", "run"]
    scope_id: UUID
    state: Literal["armed", "stopping", "stopped", "clear"]
    version: int = Field(ge=1)
    updated_at: datetime


class KillSwitchVerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    workspace_id: UUID
    acting_agent_id: UUID
    action: str = Field(min_length=1, max_length=200)
    kill_switch_snapshot_ids: tuple[UUID, ...]
    kill_switch_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_scope_bindings: tuple[KillSwitchScopeBinding, ...]
    entries: tuple[KillSwitchSnapshotEntry, ...]
    observed_at: datetime
    expires_at: datetime
    hierarchy_exhaustive: bool
    finalized: bool


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    request_context_id: UUID
    deployment_instance_id: UUID
    workspace_authority_epoch: int = Field(ge=1)
    workspace_authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_epoch_credential_id: UUID
    authority_commit_receipt_id: UUID
    authority_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_anchor_id: UUID
    registry_trust_metadata_version: int | None = Field(default=None, ge=1)
    registry_history_head_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_freshness_proof_id: UUID | None = None
    registry_freshness_sequence: int | None = Field(default=None, ge=1)
    authority_manifest_version_id: UUID
    authority_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_placement_id: UUID | None = None
    provider_connection_id: UUID | None = None
    credential_ref_id: UUID | None = None
    credential_version_id: UUID | None = None
    credential_grant_id: UUID | None = None
    budget_snapshot_ids: tuple[UUID, ...] = ()
    budget_snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_limit_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_unit: str | None = Field(default=None, min_length=1, max_length=100)
    budget_requested_amount: int | None = Field(default=None, ge=1)
    kill_switch_snapshot_ids: tuple[UUID, ...]
    kill_switch_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    kill_switch_scope_bindings: tuple[KillSwitchScopeBinding, ...]
    audience_policy_snapshot_id: UUID
    audience_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_context_id: UUID
    client_surface: ClientSurface
    transport_principal_id: UUID
    requester_id: UUID
    acting_agent_id: UUID
    scope_kind: Literal["workspace", "project", "channel", "thread", "conversation"]
    scope_id: UUID
    resource_kind: str = Field(min_length=1, max_length=100)
    resource_id: UUID
    action: str = Field(min_length=1, max_length=200)
    evaluated_at: datetime


class AuthorizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    decision: AuthorizationDecision
    reason_code: str = Field(min_length=1, max_length=200)
    actions: frozenset[str] = Field(default_factory=frozenset)


class AuthorizationSnapshotExpectedInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_version_id: UUID
    verified_at: datetime


class AuthorizationSnapshotVerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_version_id: UUID
    finalized: bool


class AuthorityCommitProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    deployment_instance_id: UUID
    authority_epoch_credential_id: UUID
    authority_epoch: int = Field(ge=1)
    authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_commit_receipt_id: UUID
    authority_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalized: bool


class RegistryVerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_id: UUID
    trust_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    freshness_proof_id: UUID
    freshness_response_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_key_version_id: UUID
    offline_root_public_keys: tuple[str, ...]
    trust_state_signatures: tuple[str, ...]
    signature_threshold: int = Field(ge=1)
    finalized: bool


class AuthorityRuntimePossessionProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    workspace_id: UUID
    deployment_instance_id: UUID
    authority_epoch_credential_id: UUID
    authority_epoch: int = Field(ge=1)
    authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    device_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock_name: str = Field(min_length=1, max_length=200)
    nonce_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    deployment_instance_signature: str = Field(min_length=1)
    epoch_credential_signature: str = Field(min_length=1)


class CredentialVerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    workspace_id: UUID
    provider_connection_id: UUID
    credential_ref_id: UUID
    credential_ref_version: int = Field(ge=1)
    credential_version_id: UUID
    credential_grant_id: UUID
    acting_agent_id: UUID
    execution_placement_id: UUID
    operation: str = Field(min_length=1, max_length=200)
    rotation_epoch: int = Field(ge=1)
    nonce_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    use_limit: int = Field(ge=1)
    use_count: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime
    credential_version_active: bool
    credential_grant_active: bool
    finalized: bool


class BudgetRuntimeVerificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    workspace_id: UUID
    scope_kind: Literal["workspace", "project", "channel", "thread", "conversation"]
    scope_id: UUID
    action: str = Field(min_length=1, max_length=200)
    budget_snapshot_ids: tuple[UUID, ...]
    budget_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_limit_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit: str = Field(min_length=1, max_length=100)
    requested_amount: int = Field(ge=1)
    canonical_account_ids: tuple[UUID, ...]
    canonical_account_closure_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_account_count: int = Field(ge=1)
    minimum_remaining_amount: int = Field(ge=0)
    runtime_limit_ms: int = Field(ge=1)
    runtime_consumed_ms: int = Field(ge=0)
    period_starts_at: datetime
    period_ends_at: datetime
    observed_at: datetime
    expires_at: datetime
    hierarchy_verified: bool
    period_non_overlapping_verified: bool
    all_accounts_active: bool
    finalized: bool


class AuthorityEvaluationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_instance: DeploymentInstance | None
    workspace_authority: WorkspaceAuthority
    epoch_credential: AuthorityEpochCredential | None
    active_lease: DeploymentInstanceLease | None
    commit_proof: AuthorityCommitProof | None
    trust_anchor: AuthorityTrustAnchor | None
    previous_registry_trust_state: AuthorityRegistryTrustState | None = None
    registry_trust_state: AuthorityRegistryTrustState | None = None
    registry_freshness_proof: AuthorityRegistryFreshnessProof | None = None
    registry_verifier_key: AuthorityRegistryVerifierKeyVersion | None = None
    registry_verification_proof: RegistryVerificationProof | None = None
    minimum_registry_sequence: int = Field(default=0, ge=0)
    expected_registry_nonce_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authority_manifest: AuthorityStateRootManifestVersion | None
    authority_manifest_families: tuple[AuthorityStateRootLeafFamily, ...] = ()
    mutable_authority_families: frozenset[str] = frozenset()
    execution_placement: ExecutionPlacement | None = None
    credential_ref: CredentialRef | None = None
    credential_verification_proof: CredentialVerificationProof | None = None
    budget_runtime_verification_proof: BudgetRuntimeVerificationProof | None = None
    kill_switch_verification_proof: KillSwitchVerificationProof | None = None
    audience_policy_snapshot: AudiencePolicySnapshot | None = None
    requester_role_ids: frozenset[UUID] = frozenset()
    client_context: ClientContext | None = None
    enabled_client_surfaces: frozenset[ClientSurface] = frozenset()
    expected_credential_rotation_epoch: int | None = Field(default=None, ge=1)
    expected_credential_nonce_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runtime_possession_proof: AuthorityRuntimePossessionProof | None


def _snapshot_digest(
    snapshot: AuthorizationDecisionSnapshot,
    *,
    exclude: set[str],
) -> str:
    encoded = json.dumps(
        snapshot.model_dump(mode="json", exclude=exclude),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authorization_snapshot_record_digest(
    snapshot: AuthorizationDecisionSnapshot,
) -> str:
    return _snapshot_digest(snapshot, exclude={"snapshot_signature"})


def authorization_snapshot_bound_input_digest(
    snapshot: AuthorizationDecisionSnapshot,
) -> str:
    return _snapshot_digest(
        snapshot,
        exclude={
            "id",
            "created_at",
            "signing_key_version_id",
            "snapshot_signature",
        },
    )


def _decode_ed25519_public_key(encoded: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ed25519_public_key_invalid") from exc
    if len(raw) != 32:
        raise ValueError("ed25519_public_key_invalid")
    return Ed25519PublicKey.from_public_bytes(raw)


def _verify_ed25519_signature(*, public_key: str, signature: str, message: bytes) -> bool:
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
        _decode_ed25519_public_key(public_key).verify(decoded_signature, message)
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def authority_public_key_set_digest(values: tuple[str, ...]) -> str:
    if len(set(values)) != len(values):
        raise ValueError("duplicate_signature_key")
    encoded = json.dumps(
        sorted(values),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def registry_threshold_signature_set_digest(
    public_keys: tuple[str, ...],
    signatures: tuple[str, ...],
) -> str:
    if len(public_keys) != len(signatures) or len(set(public_keys)) != len(public_keys):
        raise ValueError("registry_signature_set_invalid")
    encoded = json.dumps(
        sorted(zip(public_keys, signatures, strict=True)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authority_runtime_possession_digest(proof: AuthorityRuntimePossessionProof) -> str:
    encoded = json.dumps(
        proof.model_dump(
            mode="json",
            exclude={"deployment_instance_signature", "epoch_credential_signature"},
        ),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_authorization_decision_snapshot(
    snapshot: AuthorizationDecisionSnapshot,
    *,
    expected: AuthorizationSnapshotExpectedInputs,
    signing_key: WorkspaceSigningKeyVersion,
    verification_proof: AuthorizationSnapshotVerificationProof,
    verified_at: datetime,
) -> AuthorizationResult:
    try:
        canonical_digest = authorization_snapshot_digest(
            snapshot.canonical_inputs_json,
            snapshot.source_record_version_map,
        )
    except (TypeError, ValueError):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_inputs_not_canonical",
        )
    if canonical_digest != snapshot.canonical_digest:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_digest_mismatch",
        )
    if expected.verified_at != verified_at:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_expected_time_mismatch",
        )
    if snapshot.created_at > verified_at:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_not_yet_current",
        )
    if (
        snapshot.id != expected.authorization_snapshot_id
        or snapshot.signing_key_version_id != expected.signing_key_version_id
        or signing_key.id != expected.signing_key_version_id
        or signing_key.workspace_id != snapshot.workspace_id
    ):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_signing_key_mismatch",
        )
    try:
        validate_signing_time(signing_key, snapshot.created_at)
    except ValueError:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_signing_key_invalid",
        )
    try:
        record_digest = authorization_snapshot_record_digest(snapshot)
        bound_input_digest = authorization_snapshot_bound_input_digest(snapshot)
    except (TypeError, ValueError):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_inputs_not_canonical",
        )
    if record_digest != expected.authorization_snapshot_digest:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_record_digest_mismatch",
        )
    if bound_input_digest != expected.bound_input_digest:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_bound_inputs_mismatch",
        )
    if (
        verification_proof.authorization_snapshot_id != snapshot.id
        or verification_proof.authorization_snapshot_digest != record_digest
        or verification_proof.bound_input_digest != bound_input_digest
        or verification_proof.signing_key_version_id != signing_key.id
    ):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_verification_proof_mismatch",
        )
    if not verification_proof.finalized:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_verification_not_finalized",
        )
    if signing_key.algorithm.lower() != "ed25519" or not _verify_ed25519_signature(
        public_key=signing_key.public_key,
        signature=snapshot.snapshot_signature,
        message=bytes.fromhex(record_digest),
    ):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="authorization_snapshot_signature_invalid",
        )
    return AuthorizationResult(
        decision=AuthorizationDecision.ALLOW,
        reason_code="authorization_snapshot_verified",
    )


def _registry_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext,
    anchor: AuthorityTrustAnchor,
) -> str | None:
    if anchor.kind is not AuthorityTrustAnchorKind.REGISTRY_GENERATION:
        return None
    if anchor.anchor_registry_id is None or anchor.pinned_registry_root_digest is None:
        return "registry_trust_anchor_incomplete"
    if (
        request.registry_trust_metadata_version is None
        or request.registry_history_head_digest is None
        or request.registry_freshness_proof_id is None
        or request.registry_freshness_sequence is None
    ):
        return "registry_authority_claims_missing"

    trust_state = context.registry_trust_state
    if trust_state is None:
        return "registry_trust_state_missing"
    if trust_state.registry_id != anchor.anchor_registry_id:
        return "registry_identity_mismatch"
    if trust_state.metadata_version < request.registry_trust_metadata_version:
        return "stale_registry_trust_state"
    if (
        trust_state.metadata_version != request.registry_trust_metadata_version
        or trust_state.complete_history_head_digest != request.registry_history_head_digest
    ):
        return "registry_trust_state_mismatch"
    if trust_state.issued_at > request.evaluated_at:
        return "registry_trust_state_not_yet_valid"
    if trust_state.expires_at <= request.evaluated_at:
        return "registry_trust_state_expired"

    previous = context.previous_registry_trust_state
    if trust_state.metadata_version > 1:
        if previous is None:
            return "registry_trust_state_predecessor_missing"
        try:
            validate_registry_trust_transition(
                previous,
                trust_state,
                now=request.evaluated_at,
            )
        except AuthorityContractError as exc:
            return exc.reason_code
    elif trust_state.previous_metadata_digest is not None:
        return "registry_metadata_prefix_mismatch"

    verifier = context.registry_verifier_key
    if verifier is None:
        return "registry_verifier_missing"
    if verifier.registry_id != trust_state.registry_id:
        return "registry_verifier_registry_mismatch"
    if verifier.key_version < trust_state.latest_verifier_key_version:
        return "registry_verifier_version_rollback"
    if verifier.key_version != trust_state.latest_verifier_key_version:
        return "registry_verifier_version_mismatch"
    try:
        validate_registry_verifier_time(verifier, now=request.evaluated_at)
    except AuthorityContractError as exc:
        return exc.reason_code

    freshness = context.registry_freshness_proof
    if freshness is None:
        return "registry_freshness_proof_missing"
    if freshness.id != request.registry_freshness_proof_id:
        return "registry_freshness_proof_mismatch"
    if freshness.registry_id != trust_state.registry_id:
        return "registry_freshness_registry_mismatch"
    if freshness.trust_state_metadata_version < trust_state.metadata_version:
        return "stale_registry_freshness_proof"
    if freshness.trust_state_metadata_version != trust_state.metadata_version:
        return "registry_freshness_trust_state_mismatch"
    if freshness.complete_history_head_digest != trust_state.complete_history_head_digest:
        return "registry_freshness_history_prefix_mismatch"
    if freshness.verifier_key_version_id != verifier.id:
        return "registry_freshness_verifier_mismatch"
    if freshness.registry_sequence != request.registry_freshness_sequence:
        return "registry_freshness_sequence_mismatch"
    if freshness.issued_at > request.evaluated_at:
        return "registry_freshness_proof_not_yet_valid"
    if freshness.expires_at <= request.evaluated_at:
        return "registry_freshness_proof_expired"
    if context.expected_registry_nonce_digest is None:
        return "registry_freshness_nonce_missing"
    verification = context.registry_verification_proof
    if verification is None:
        return "registry_verification_proof_missing"
    if not verification.finalized:
        return "registry_verification_not_finalized"
    if (
        verification.registry_id != trust_state.registry_id
        or verification.trust_state_digest != trust_state.canonical_digest
        or verification.freshness_proof_id != freshness.id
        or verification.freshness_response_digest != freshness.response_digest
        or verification.verifier_key_version_id != verifier.id
    ):
        return "registry_verification_proof_mismatch"
    try:
        keyset_digest = authority_public_key_set_digest(verification.offline_root_public_keys)
        if len(verification.offline_root_public_keys) != len(verification.trust_state_signatures):
            raise ValueError("registry_signature_set_invalid")
    except ValueError:
        return "registry_trust_signature_set_invalid"
    required_threshold = len(verification.offline_root_public_keys) // 2 + 1
    if (
        keyset_digest != anchor.pinned_registry_root_digest
        or keyset_digest != trust_state.threshold_signature_set_digest
        or verification.signature_threshold != required_threshold
    ):
        return "registry_trust_signature_set_invalid"
    verified_signatures = sum(
        _verify_ed25519_signature(
            public_key=public_key,
            signature=signature,
            message=bytes.fromhex(trust_state.canonical_digest),
        )
        for public_key, signature in zip(
            verification.offline_root_public_keys,
            verification.trust_state_signatures,
            strict=True,
        )
    )
    if verified_signatures < verification.signature_threshold:
        return "registry_trust_signatures_unverified"
    if verifier.algorithm.lower() != "ed25519" or not _verify_ed25519_signature(
        public_key=verifier.public_key,
        signature=freshness.registry_signature,
        message=bytes.fromhex(freshness.response_digest),
    ):
        return "registry_freshness_signature_unverified"
    try:
        validate_registry_freshness_proof(
            freshness,
            trust_state,
            now=request.evaluated_at,
            minimum_sequence=context.minimum_registry_sequence,
            expected_nonce_digest=context.expected_registry_nonce_digest,
        )
    except AuthorityContractError as exc:
        return exc.reason_code
    return None


def _manifest_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext,
) -> str | None:
    manifest = context.authority_manifest
    if manifest is None:
        return "authority_manifest_missing"
    if (
        manifest.id != request.authority_manifest_version_id
        or manifest.manifest_digest != request.authority_manifest_digest
    ):
        return "authority_manifest_mismatch"
    if manifest.status is not ManifestStatus.ACTIVE:
        return "authority_manifest_inactive"
    if manifest.created_at > request.evaluated_at:
        return "authority_manifest_not_yet_active"
    families = list(context.authority_manifest_families)
    if any(
        family.manifest_version_id != manifest.id
        or family.canonicalization_version != manifest.canonicalization_version
        for family in families
    ):
        return "authority_manifest_family_binding_mismatch"
    if len({family.family_name for family in families}) != len(families) or len(
        {family.ordinal for family in families}
    ) != len(families):
        return "authority_manifest_family_set_ambiguous"
    try:
        validate_authority_root_manifest(
            manifest,
            families,
            mutable_authority_families=set(context.mutable_authority_families),
        )
    except AuthorityContractError as exc:
        return exc.reason_code
    return None


def _placement_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    placement = context.execution_placement
    if request.execution_placement_id is None:
        return "execution_placement_unsolicited" if placement is not None else None
    if placement is None:
        return "execution_placement_missing"
    if placement.id != request.execution_placement_id:
        return "execution_placement_mismatch"
    if placement.status is ExecutionStatus.REVOKED:
        return "execution_placement_revoked"
    if placement.status is ExecutionStatus.UNAVAILABLE:
        return "execution_placement_unavailable"
    if placement.created_at > request.evaluated_at:
        return "execution_placement_not_yet_active"
    return None


def _credential_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    claims = (
        request.provider_connection_id,
        request.credential_ref_id,
        request.credential_version_id,
        request.credential_grant_id,
    )
    if all(claim is None for claim in claims):
        if (
            context.credential_ref is not None
            or context.credential_verification_proof is not None
            or context.expected_credential_rotation_epoch is not None
            or context.expected_credential_nonce_digest is not None
        ):
            return "credential_evidence_unsolicited"
        return None
    if any(claim is None for claim in claims):
        return "credential_claims_incomplete"
    if request.execution_placement_id is None:
        return "credential_placement_missing"

    provider_connection_id = request.provider_connection_id
    credential_ref_id = request.credential_ref_id
    credential_version_id = request.credential_version_id
    credential_grant_id = request.credential_grant_id
    if (
        provider_connection_id is None
        or credential_ref_id is None
        or credential_version_id is None
        or credential_grant_id is None
    ):
        return "credential_claims_incomplete"

    credential_ref = context.credential_ref
    if credential_ref is None:
        return "credential_ref_missing"
    if (
        credential_ref.id != credential_ref_id
        or credential_ref.workspace_id != request.workspace_id
        or credential_ref.provider_connection_id != provider_connection_id
    ):
        return "credential_ref_mismatch"
    if (
        credential_ref.owner_principal_id is not None
        and credential_ref.owner_principal_id != request.requester_id
    ):
        return "credential_owner_mismatch"
    if credential_ref.status is CredentialStatus.REVOKED:
        return "credential_ref_revoked"
    if credential_ref.status is CredentialStatus.EXPIRED:
        return "credential_ref_expired"
    if credential_ref.expires_at is not None and credential_ref.expires_at <= request.evaluated_at:
        return "credential_ref_expired"
    if (
        credential_ref.created_at > request.evaluated_at
        or credential_ref.updated_at > request.evaluated_at
    ):
        return "credential_ref_not_yet_current"

    proof = context.credential_verification_proof
    if proof is None:
        return "credential_verification_proof_missing"
    if (
        proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.provider_connection_id != provider_connection_id
        or proof.credential_ref_id != credential_ref_id
        or proof.credential_ref_version != credential_ref.version
        or proof.credential_version_id != credential_version_id
        or proof.credential_grant_id != credential_grant_id
        or proof.acting_agent_id != request.acting_agent_id
        or proof.execution_placement_id != request.execution_placement_id
        or proof.operation != request.action
    ):
        return "credential_verification_proof_mismatch"
    if not proof.finalized:
        return "credential_verification_not_finalized"
    if not proof.credential_version_active:
        return "credential_version_inactive"
    if not proof.credential_grant_active:
        return "credential_grant_inactive"
    if proof.issued_at > request.evaluated_at:
        return "credential_verification_proof_not_yet_valid"
    if proof.expires_at <= request.evaluated_at:
        return "credential_verification_proof_expired"
    if (
        context.expected_credential_rotation_epoch is None
        or context.expected_credential_nonce_digest is None
    ):
        return "credential_verification_expectations_missing"
    if proof.rotation_epoch != context.expected_credential_rotation_epoch:
        return "credential_rotation_epoch_mismatch"
    if proof.nonce_digest != context.expected_credential_nonce_digest:
        return "credential_nonce_mismatch"
    if proof.use_count >= proof.use_limit:
        return "credential_grant_use_limit_exhausted"
    return None


def _budget_runtime_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    has_snapshot_ids = bool(request.budget_snapshot_ids)
    remaining_claims = (
        request.budget_snapshot_digest,
        request.runtime_limit_digest,
        request.budget_unit,
        request.budget_requested_amount,
    )
    if not has_snapshot_ids and all(claim is None for claim in remaining_claims):
        return (
            "budget_runtime_evidence_unsolicited"
            if context.budget_runtime_verification_proof is not None
            else None
        )
    if not has_snapshot_ids or any(claim is None for claim in remaining_claims):
        return "budget_runtime_claims_incomplete"
    if len(set(request.budget_snapshot_ids)) != len(request.budget_snapshot_ids):
        return "budget_snapshot_set_ambiguous"

    budget_snapshot_digest = request.budget_snapshot_digest
    runtime_limit_digest = request.runtime_limit_digest
    budget_unit = request.budget_unit
    budget_requested_amount = request.budget_requested_amount
    if (
        budget_snapshot_digest is None
        or runtime_limit_digest is None
        or budget_unit is None
        or budget_requested_amount is None
    ):
        return "budget_runtime_claims_incomplete"

    proof = context.budget_runtime_verification_proof
    if proof is None:
        return "budget_runtime_verification_proof_missing"
    if (
        proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.scope_kind != request.scope_kind
        or proof.scope_id != request.scope_id
        or proof.action != request.action
        or proof.budget_snapshot_ids != request.budget_snapshot_ids
        or proof.budget_snapshot_digest != budget_snapshot_digest
        or proof.runtime_limit_digest != runtime_limit_digest
        or proof.unit != budget_unit
        or proof.requested_amount != budget_requested_amount
    ):
        return "budget_runtime_verification_proof_mismatch"
    if not proof.finalized:
        return "budget_runtime_verification_not_finalized"
    if proof.observed_at > request.evaluated_at:
        return "budget_runtime_verification_proof_not_yet_valid"
    if proof.expires_at <= request.evaluated_at:
        return "budget_runtime_verification_proof_expired"
    if not proof.hierarchy_verified:
        return "budget_hierarchy_unverified"
    if not proof.period_non_overlapping_verified:
        return "budget_period_overlap_unverified"
    if not proof.all_accounts_active:
        return "budget_account_inactive"
    if not (
        proof.period_starts_at <= request.evaluated_at < proof.period_ends_at
        and proof.period_starts_at < proof.period_ends_at
    ):
        return "budget_period_inactive"
    if len(proof.canonical_account_ids) != proof.expected_account_count:
        return "budget_account_closure_incomplete"
    if len(set(proof.canonical_account_ids)) != len(proof.canonical_account_ids):
        return "budget_account_closure_ambiguous"
    if proof.minimum_remaining_amount < budget_requested_amount:
        return "budget_exhausted"
    if proof.runtime_consumed_ms >= proof.runtime_limit_ms:
        return "runtime_exhausted"
    return None


def _kill_switch_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    snapshot_ids = request.kill_switch_snapshot_ids
    bindings = request.kill_switch_scope_bindings
    if not snapshot_ids or not bindings or len(snapshot_ids) != len(bindings):
        return "kill_switch_claims_incomplete"
    if len(set(snapshot_ids)) != len(snapshot_ids):
        return "kill_switch_snapshot_set_ambiguous"
    binding_keys = tuple((binding.scope_kind, binding.scope_id) for binding in bindings)
    if len(set(binding_keys)) != len(binding_keys):
        return "kill_switch_scope_set_ambiguous"
    required_workspace = ("workspace", request.workspace_id)
    required_agent = ("agent", request.acting_agent_id)
    if required_workspace not in binding_keys or required_agent not in binding_keys:
        return "kill_switch_hierarchy_incomplete"

    proof = context.kill_switch_verification_proof
    if proof is None:
        return "kill_switch_verification_proof_missing"
    if (
        proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.acting_agent_id != request.acting_agent_id
        or proof.action != request.action
        or proof.kill_switch_snapshot_ids != snapshot_ids
        or proof.kill_switch_snapshot_digest != request.kill_switch_snapshot_digest
        or proof.required_scope_bindings != bindings
    ):
        return "kill_switch_verification_proof_mismatch"
    if not proof.finalized:
        return "kill_switch_verification_not_finalized"
    if proof.observed_at > request.evaluated_at:
        return "kill_switch_verification_proof_not_yet_valid"
    if proof.expires_at <= request.evaluated_at:
        return "kill_switch_verification_proof_expired"
    if not proof.hierarchy_exhaustive:
        return "kill_switch_hierarchy_unverified"
    if len(proof.entries) != len(snapshot_ids):
        return "kill_switch_snapshot_set_incomplete"
    entry_ids = tuple(entry.snapshot_id for entry in proof.entries)
    if len(set(entry_ids)) != len(entry_ids):
        return "kill_switch_snapshot_set_ambiguous"
    for snapshot_id, binding, entry in zip(
        snapshot_ids,
        bindings,
        proof.entries,
        strict=True,
    ):
        if (
            entry.snapshot_id != snapshot_id
            or entry.workspace_id != request.workspace_id
            or entry.scope_kind != binding.scope_kind
            or entry.scope_id != binding.scope_id
        ):
            return "kill_switch_snapshot_binding_mismatch"
        if entry.updated_at > request.evaluated_at:
            return "kill_switch_snapshot_not_yet_current"
        if entry.state != "clear":
            return f"kill_switch_{entry.state}"
    return None


def _audience_client_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    audience = context.audience_policy_snapshot
    client = context.client_context
    if audience is None:
        return "audience_policy_snapshot_missing"
    if client is None:
        return "client_context_missing"
    if (
        audience.id != request.audience_policy_snapshot_id
        or audience.workspace_id != request.workspace_id
        or audience.policy_digest != request.audience_policy_digest
        or audience.scope_digest != request.scope_digest
    ):
        return "audience_policy_snapshot_mismatch"
    if audience.created_at > request.evaluated_at:
        return "audience_policy_snapshot_not_yet_current"
    audience_allows = False
    if audience.visibility == "personal":
        audience_allows = audience.owner_principal_id == request.requester_id
    elif audience.visibility == "explicit_principals":
        audience_allows = request.requester_id in audience.principal_ids
    elif audience.visibility == "role":
        audience_allows = bool(audience.role_ids & context.requester_role_ids)
    elif audience.visibility in {"project", "channel", "thread"}:
        if audience.visibility != request.scope_kind:
            return "audience_scope_mismatch"
        audience_allows = True
    elif audience.visibility == "workspace":
        audience_allows = True
    if not audience_allows:
        return "audience_principal_denied"
    if request.client_surface not in context.enabled_client_surfaces:
        return "client_surface_disabled"
    if client.id != request.client_context_id:
        return "client_context_mismatch"
    if client.surface is not request.client_surface:
        return "client_surface_mismatch"
    if client.transport_principal_id != request.transport_principal_id:
        return "transport_principal_mismatch"
    if client.issued_at > request.evaluated_at:
        return "client_context_not_yet_active"
    if client.expires_at is not None and request.evaluated_at >= client.expires_at:
        return "client_context_expired"
    return None


def _runtime_possession_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext,
    instance: DeploymentInstance,
    credential: AuthorityEpochCredential,
    anchor: AuthorityTrustAnchor,
) -> str | None:
    proof = context.runtime_possession_proof
    if proof is None:
        return "authority_runtime_possession_proof_missing"
    expected_lock_name = fixed_workspace_lock_name(
        request.workspace_id,
        request.workspace_authority_epoch,
    )
    if (
        proof.request_context_id != request.request_context_id
        or proof.workspace_id != request.workspace_id
        or proof.deployment_instance_id != instance.id
        or proof.authority_epoch_credential_id != credential.id
        or proof.authority_epoch != request.workspace_authority_epoch
        or proof.authority_generation != request.workspace_authority_generation
        or proof.authority_state_root != request.authority_state_root
        or proof.device_binding_digest != instance.device_binding_digest
    ):
        return "authority_runtime_possession_proof_mismatch"
    if proof.lock_name != expected_lock_name:
        return "workspace_os_lock_not_held"
    if (
        anchor.kind is AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION
        and anchor.local_lock_name != proof.lock_name
    ):
        return "workspace_os_lock_not_held"
    if (
        proof.issued_at > request.evaluated_at
        or proof.expires_at <= request.evaluated_at
        or proof.expires_at <= proof.issued_at
        or proof.expires_at - proof.issued_at > timedelta(seconds=30)
    ):
        return "authority_runtime_possession_proof_expired"
    message = bytes.fromhex(authority_runtime_possession_digest(proof))
    if not _verify_ed25519_signature(
        public_key=instance.instance_public_key,
        signature=proof.deployment_instance_signature,
        message=message,
    ):
        return "deployment_instance_key_unavailable"
    if not _verify_ed25519_signature(
        public_key=credential.public_key,
        signature=proof.epoch_credential_signature,
        message=message,
    ):
        return "authority_epoch_key_unavailable"
    return None


def _authority_denial_reason(
    request: AuthorizationRequest,
    context: AuthorityEvaluationContext | None,
) -> str | None:
    if context is None:
        return "authority_context_missing"
    instance = context.deployment_instance
    if instance is None:
        return "deployment_instance_missing"
    authority = context.workspace_authority
    if authority.workspace_id != request.workspace_id:
        return "cross_workspace_authority"
    if authority.state is WorkspaceAuthorityState.RESTORE_QUARANTINE:
        return "restore_quarantine"
    if authority.state is not WorkspaceAuthorityState.ACTIVE:
        return "workspace_authority_inactive"
    if instance.status is not DeploymentInstanceStatus.ACTIVE:
        return "deployment_instance_inactive"
    if (
        authority.deployment_profile_id != instance.deployment_profile_id
        or authority.deployment_instance_id != instance.id
        or request.deployment_instance_id != instance.id
    ):
        return "deployment_instance_mismatch"
    if request.workspace_authority_epoch < authority.epoch:
        return "stale_authority_epoch"
    if request.workspace_authority_epoch != authority.epoch:
        return "authority_epoch_mismatch"
    if request.workspace_authority_generation < authority.authority_generation:
        return "stale_authority_generation"
    if request.workspace_authority_generation != authority.authority_generation:
        return "authority_generation_mismatch"
    if request.authority_state_root != authority.authority_state_root:
        return "authority_state_root_mismatch"
    if request.authority_epoch_credential_id != authority.authority_epoch_credential_id:
        return "authority_epoch_credential_mismatch"
    credential = context.epoch_credential
    if credential is None:
        return "authority_epoch_credential_missing"
    if credential.status is AuthorityEpochCredentialStatus.REVOKED:
        return "authority_epoch_credential_revoked"
    if credential.status is AuthorityEpochCredentialStatus.DESTROYED:
        return "authority_epoch_credential_destroyed"
    if (
        credential.id != authority.authority_epoch_credential_id
        or credential.workspace_id != request.workspace_id
        or credential.authority_epoch != authority.epoch
        or credential.deployment_instance_id != instance.id
    ):
        return "authority_epoch_credential_mismatch"
    if credential.device_binding_digest != instance.device_binding_digest:
        return "authority_device_binding_mismatch"
    proof = context.commit_proof
    if proof is None:
        return "authority_commit_proof_missing"
    if not proof.finalized:
        return "authority_commit_not_finalized"
    if proof.workspace_id != request.workspace_id:
        return "cross_workspace_authority_proof"
    if proof.deployment_instance_id != instance.id:
        return "authority_proof_instance_mismatch"
    if proof.authority_epoch_credential_id != credential.id:
        return "authority_proof_credential_mismatch"
    if proof.authority_epoch < authority.epoch:
        return "stale_authority_epoch"
    if proof.authority_epoch != authority.epoch:
        return "authority_epoch_mismatch"
    if proof.authority_generation < authority.authority_generation:
        return "stale_authority_generation"
    if proof.authority_generation != authority.authority_generation:
        return "authority_generation_mismatch"
    if proof.authority_state_root != authority.authority_state_root:
        return "authority_state_root_mismatch"
    if proof.authority_commit_receipt_id != request.authority_commit_receipt_id:
        return "authority_commit_receipt_mismatch"
    if proof.authority_proof_digest != request.authority_proof_digest:
        return "authority_proof_digest_mismatch"
    anchor = context.trust_anchor
    if anchor is None:
        return "authority_trust_anchor_missing"
    if authority.trust_anchor_id != request.trust_anchor_id or anchor.id != request.trust_anchor_id:
        return "authority_trust_anchor_mismatch"
    if anchor.workspace_id != request.workspace_id:
        return "cross_workspace_trust_anchor"
    if anchor.status is not AuthorityTrustAnchorStatus.ACTIVE:
        return "authority_trust_anchor_inactive"
    if anchor.created_at > request.evaluated_at:
        return "authority_trust_anchor_not_yet_active"
    if anchor.kind is AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION and (
        anchor.local_lock_name is None
        or anchor.sealed_generation_ref is None
        or anchor.device_binding_digest is None
    ):
        return "sealed_local_trust_anchor_incomplete"
    registry_denial = _registry_denial_reason(request, context, anchor)
    if registry_denial is not None:
        return registry_denial
    manifest_denial = _manifest_denial_reason(request, context)
    if manifest_denial is not None:
        return manifest_denial
    runtime_denial = _runtime_possession_denial_reason(
        request,
        context,
        instance,
        credential,
        anchor,
    )
    if runtime_denial is not None:
        return runtime_denial
    lease = context.active_lease
    if lease is None:
        return "authority_lease_missing"
    if authority.active_lease_id != lease.id:
        return "authority_lease_mismatch"
    if lease.released_at is not None:
        return "authority_lease_released"
    if lease.acquired_at > request.evaluated_at:
        return "authority_lease_not_yet_active"
    if (
        lease.workspace_id != request.workspace_id
        or lease.authority_epoch != authority.epoch
        or lease.deployment_instance_id != instance.id
    ):
        return "authority_lease_mismatch"
    if authority.activated_at > request.evaluated_at:
        return "workspace_authority_not_yet_active"
    return None


def _bundle_is_current(bundle: AccessBundle, *, at: datetime) -> bool:
    return bundle.revoked_at is None and (bundle.expires_at is None or at < bundle.expires_at)


def _grant_targets_request(
    grant: CapabilityGrant,
    *,
    bundle: AccessBundle,
    request: AuthorizationRequest,
) -> bool:
    return (
        grant.bundle_id == bundle.id
        and grant.workspace_id == request.workspace_id
        and grant.resource_kind == request.resource_kind
        and grant.resource_id == request.resource_id
        and grant.action == request.action
    )


def _grant_matches(
    grant: CapabilityGrant,
    *,
    bundle: AccessBundle,
    request: AuthorizationRequest,
) -> bool:
    return _grant_targets_request(grant, bundle=bundle, request=request) and (
        grant.effect is CapabilityEffect.ALLOW
    )


def evaluate_capability_intersection(
    request: AuthorizationRequest,
    *,
    authority_context: AuthorityEvaluationContext | None,
    requester_bundle: AccessBundle,
    requester_grants: list[CapabilityGrant],
    agent_grant: AgentGrant | None,
    agent_bundle: AccessBundle,
    agent_capabilities: list[CapabilityGrant],
    delegation_grants: list[DelegationGrant],
) -> AuthorizationResult:
    authority_denial = _authority_denial_reason(request, authority_context)
    if authority_denial is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code=authority_denial,
        )
    placement_denial = _placement_denial_reason(request, authority_context)
    if placement_denial is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code=placement_denial,
        )
    credential_denial = _credential_denial_reason(request, authority_context)
    if credential_denial is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code=credential_denial,
        )
    is_agent_run_cancellation = (
        request.resource_kind == _AGENT_RUN_RESOURCE_KIND
        and request.action == _AGENT_RUN_CANCEL_ACTION
    )
    if not is_agent_run_cancellation:
        budget_runtime_denial = _budget_runtime_denial_reason(request, authority_context)
        if budget_runtime_denial is not None:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code=budget_runtime_denial,
            )
    kill_switch_denial = _kill_switch_denial_reason(request, authority_context)
    if kill_switch_denial is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code=kill_switch_denial,
        )
    if agent_grant is None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="no_agent_grant",
        )
    workspace_ids = {
        requester_bundle.workspace_id,
        agent_bundle.workspace_id,
        agent_grant.workspace_id,
        *(grant.workspace_id for grant in requester_grants),
        *(grant.workspace_id for grant in agent_capabilities),
    }
    if workspace_ids != {request.workspace_id}:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="cross_workspace_grant",
        )
    requested_scope = (request.scope_kind, request.scope_id)
    if any(
        (bundle.scope_kind, bundle.scope_id) != requested_scope
        for bundle in (requester_bundle, agent_bundle)
    ):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="scope_mismatch",
        )
    if requester_bundle.revoked_at is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="requester_grant_revoked",
        )
    if (
        requester_bundle.expires_at is not None
        and request.evaluated_at >= requester_bundle.expires_at
    ):
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="requester_grant_expired",
        )
    if agent_bundle.revoked_at is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="agent_bundle_revoked",
        )
    if agent_bundle.expires_at is not None and request.evaluated_at >= agent_bundle.expires_at:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="agent_bundle_expired",
        )
    if agent_grant.revoked_at is not None:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="agent_grant_revoked",
        )
    if agent_grant.expires_at is not None and request.evaluated_at >= agent_grant.expires_at:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="agent_grant_expired",
        )
    delegation: DelegationGrant | None = None
    if len(delegation_grants) > 1:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="delegation_chain_unverifiable",
        )
    if delegation_grants:
        delegation = delegation_grants[0]
        if delegation.parent_agent_grant_id != agent_grant.id:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_parent_mismatch",
            )
        if delegation.child_agent_id != request.acting_agent_id:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_child_mismatch",
            )
        if delegation.revoked_at is not None:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_revoked",
            )
        if request.evaluated_at < delegation.issued_at:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_not_yet_active",
            )
        if request.evaluated_at >= delegation.expires_at:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_expired",
            )
        if delegation.depth_limit < 1:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_depth_exceeded",
            )
        if request.action not in delegation.capabilities:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code="delegation_overreach",
            )
    expected_bundle_principal_id = (
        agent_grant.agent_id if delegation is not None else request.acting_agent_id
    )
    bundles_match = (
        requester_bundle.workspace_id == request.workspace_id
        and requester_bundle.principal_id == request.requester_id
        and requester_bundle.scope_kind == request.scope_kind
        and requester_bundle.scope_id == request.scope_id
        and agent_bundle.workspace_id == request.workspace_id
        and agent_bundle.principal_id == expected_bundle_principal_id
        and agent_bundle.scope_kind == request.scope_kind
        and agent_bundle.scope_id == request.scope_id
        and agent_grant.workspace_id == request.workspace_id
        and agent_grant.agent_id == expected_bundle_principal_id
        and agent_grant.capability_bundle_id == agent_bundle.id
    )
    grants_current = (
        _bundle_is_current(requester_bundle, at=request.evaluated_at)
        and _bundle_is_current(agent_bundle, at=request.evaluated_at)
        and agent_grant.revoked_at is None
        and (agent_grant.expires_at is None or request.evaluated_at < agent_grant.expires_at)
    )
    requester_allows = any(
        _grant_matches(grant, bundle=requester_bundle, request=request)
        for grant in requester_grants
    )
    agent_allows = any(
        _grant_matches(grant, bundle=agent_bundle, request=request) for grant in agent_capabilities
    )
    explicit_deny = any(
        _grant_targets_request(grant, bundle=bundle, request=request)
        and grant.effect is CapabilityEffect.DENY
        for bundle, grants in (
            (requester_bundle, requester_grants),
            (agent_bundle, agent_capabilities),
        )
        for grant in grants
    )
    if explicit_deny:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="explicit_deny",
        )
    if not requester_allows:
        return AuthorizationResult(
            decision=AuthorizationDecision.DENY,
            reason_code="no_requester_grant",
        )
    if bundles_match and grants_current and requester_allows and agent_allows:
        audience_client_denial = _audience_client_denial_reason(request, authority_context)
        if audience_client_denial is not None:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                reason_code=audience_client_denial,
            )
        return AuthorizationResult(
            decision=AuthorizationDecision.ALLOW,
            reason_code=(
                "delegated_capability_intersection"
                if delegation is not None
                else "exact_capability_intersection"
            ),
            actions=frozenset({request.action}),
        )
    return AuthorizationResult(
        decision=AuthorizationDecision.DENY,
        reason_code="capability_intersection_missing",
    )
