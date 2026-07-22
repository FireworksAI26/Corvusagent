from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def _now_utc() -> datetime:
    return datetime.now(UTC)


class AuthorityMode(StrEnum):
    EMBEDDED_LOCAL = "embedded_local"
    LOCAL_DAEMON = "local_daemon"
    SELF_HOSTED = "self_hosted"
    VENDOR_CLOUD = "vendor_cloud"


class AuthProfile(StrEnum):
    LOCAL_OS = "local_os"
    LOCAL_SESSION = "local_session"
    OIDC = "oidc"


class NetworkProfile(StrEnum):
    IN_PROCESS = "in_process"
    LOOPBACK = "loopback"
    NETWORK_TLS = "network_tls"


class StorageProfile(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class ConfigurationContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class DeploymentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    authority_mode: AuthorityMode
    auth_profile: AuthProfile
    network_profile: NetworkProfile
    storage_profile: StorageProfile
    enabled_adapters: frozenset[Literal["cli", "desktop", "web", "channel"]]
    protocol_version: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)


class DeploymentInstanceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    RETIRED = "retired"


class DeploymentInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    deployment_profile_id: UUID
    instance_public_key: str = Field(min_length=1)
    non_exportable_activation_key_ref: str = Field(min_length=1)
    device_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DeploymentInstanceStatus = DeploymentInstanceStatus.ACTIVE
    activated_at: datetime = Field(default_factory=_now_utc)
    revoked_at: datetime | None = None
    retired_at: datetime | None = None


class WorkspaceAuthorityState(StrEnum):
    ACTIVE = "active"
    HANDOFF_PENDING = "handoff_pending"
    CLOSED = "closed"
    RESTORE_QUARANTINE = "restore_quarantine"


class WorkspaceAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    deployment_profile_id: UUID
    deployment_instance_id: UUID
    epoch: int = Field(ge=1)
    authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_epoch_credential_id: UUID
    trust_anchor_id: UUID
    active_lease_id: UUID | None = None
    state: WorkspaceAuthorityState
    previous_epoch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    activated_at: datetime = Field(default_factory=_now_utc)
    closed_at: datetime | None = None
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_active_lease(self) -> WorkspaceAuthority:
        if (
            self.state
            in {
                WorkspaceAuthorityState.ACTIVE,
                WorkspaceAuthorityState.HANDOFF_PENDING,
            }
            and self.active_lease_id is None
        ):
            raise PydanticCustomError(
                "active_authority_without_lease",
                "reason_code={reason_code}",
                {"reason_code": "active_authority_requires_exclusive_lease"},
            )
        return self


class AuthorityTrustAnchorKind(StrEnum):
    REGISTRY_GENERATION = "registry_generation"
    SEALED_LOCAL_GENERATION = "sealed_local_generation"


class AuthorityTrustAnchorStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    RETIRED = "retired"


class AuthorityTrustAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    kind: AuthorityTrustAnchorKind
    anchor_registry_id: UUID | None = None
    pinned_registry_root_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    local_lock_name: str | None = Field(default=None, min_length=1, max_length=200)
    sealed_generation_ref: str | None = Field(default=None, min_length=1)
    device_binding_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AuthorityTrustAnchorStatus = AuthorityTrustAnchorStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)


class AuthorityEpochCredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    DESTROYED = "destroyed"


class AuthorityEpochCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    authority_epoch: int = Field(ge=1)
    deployment_instance_id: UUID
    public_key: str = Field(min_length=1)
    non_exportable_private_key_ref: str = Field(min_length=1)
    device_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AuthorityEpochCredentialStatus = AuthorityEpochCredentialStatus.ACTIVE
    issued_at: datetime = Field(default_factory=_now_utc)
    revoked_at: datetime | None = None
    destroyed_at: datetime | None = None


def validate_configuration_combination(
    profile: DeploymentProfile,
    workspace: object,
    placement: object,
) -> None:
    from corvus.domain.execution import ExecutionKind, ExecutionPlacement
    from corvus.domain.workspace import CollaborationMode, WorkspaceConfig

    if not isinstance(workspace, WorkspaceConfig):
        raise TypeError("workspace must be a WorkspaceConfig")
    if not isinstance(placement, ExecutionPlacement):
        raise TypeError("placement must be an ExecutionPlacement")
    if profile.authority_mode is AuthorityMode.EMBEDDED_LOCAL and (
        workspace.collaboration_mode is CollaborationMode.TEAM
    ):
        raise ConfigurationContractError(
            "embedded_local_requires_individual_workspace",
            "embedded-local authority cannot host a team workspace",
        )
    if (
        profile.authority_mode is AuthorityMode.VENDOR_CLOUD
        and profile.storage_profile is not StorageProfile.POSTGRESQL
    ):
        raise ConfigurationContractError(
            "vendor_cloud_requires_postgresql",
            "vendor-cloud authority requires PostgreSQL storage",
        )
    if (
        profile.authority_mode is AuthorityMode.EMBEDDED_LOCAL
        and placement.kind is not ExecutionKind.LOCAL_RUNNER
    ):
        raise ConfigurationContractError(
            "embedded_local_requires_local_runner",
            "embedded-local authority requires local-runner execution",
        )


class AuthorityCommitState(StrEnum):
    PREPARED = "prepared"
    ANCHOR_RESERVED = "anchor_reserved"
    DB_COMMITTED = "db_committed"
    ANCHOR_FINALIZED = "anchor_finalized"
    QUARANTINED = "quarantined"


class AuthorityCommitIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    epoch: int = Field(ge=1)
    deployment_instance_id: UUID
    prior_generation: int = Field(ge=0)
    next_generation: int = Field(ge=1)
    prior_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityCommitState
    created_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def validate_authority_advance(self) -> AuthorityCommitIntent:
        if self.proposed_state_root == self.prior_state_root:
            raise PydanticCustomError(
                "authority_root_not_advanced",
                "reason_code={reason_code}",
                {"reason_code": "authority_root_must_advance"},
            )
        if self.next_generation != self.prior_generation + 1:
            raise PydanticCustomError(
                "authority_generation_not_advanced",
                "reason_code={reason_code}",
                {"reason_code": "authority_generation_must_advance_once"},
            )
        return self


class AuthorityContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


def fixed_workspace_lock_name(workspace_id: UUID, authority_epoch: int) -> str:
    if authority_epoch < 1:
        raise AuthorityContractError(
            "invalid_authority_epoch",
            "the authority epoch must be positive",
        )
    material = f"corvus-workspace-lock-v1:{workspace_id}".encode()
    return f"corvus-workspace-{hashlib.sha256(material).hexdigest()}"


class DeploymentInstanceLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    authority_epoch: int = Field(ge=1)
    deployment_instance_id: UUID
    lock_name: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    released_at: datetime | None = None

    @model_validator(mode="after")
    def validate_fixed_lock_name(self) -> DeploymentInstanceLease:
        expected = fixed_workspace_lock_name(self.workspace_id, self.authority_epoch)
        if self.lock_name != expected:
            raise PydanticCustomError(
                "workspace_lock_name_mismatch",
                "reason_code={reason_code}",
                {"reason_code": "workspace_lock_name_mismatch"},
            )
        return self


def validate_exclusive_instance_lease(
    current: DeploymentInstanceLease,
    *,
    workspace_id: UUID,
    authority_epoch: int,
    claimant_instance_id: UUID,
    lock_name: str,
) -> None:
    expected = fixed_workspace_lock_name(workspace_id, authority_epoch)
    if lock_name != expected:
        raise AuthorityContractError(
            "workspace_lock_name_mismatch",
            "the claimant did not use the fixed workspace lock",
        )
    if (
        current.released_at is None
        and current.workspace_id == workspace_id
        and current.authority_epoch == authority_epoch
        and current.lock_name == lock_name
        and current.deployment_instance_id != claimant_instance_id
    ):
        raise AuthorityContractError(
            "same_epoch_instance_lease_conflict",
            "another deployment instance holds the workspace epoch lease",
        )


class RegistryVerifierKeyStatus(StrEnum):
    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"
    COMPROMISED = "compromised"


class AuthorityRegistryStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    RETIRED = "retired"


class AuthorityRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    endpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    offline_root_public_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AuthorityRegistryStatus = AuthorityRegistryStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)


class AuthorityRegistryVerifierKeyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    registry_id: UUID
    key_version: int = Field(ge=1)
    algorithm: str = Field(default="ed25519", min_length=1)
    public_key: str = Field(min_length=1)
    status: RegistryVerifierKeyStatus
    valid_from: datetime
    valid_until: datetime | None = None
    revoked_at: datetime | None = None
    compromise_effective_at: datetime | None = None
    predecessor_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    predecessor_signature: str | None = Field(default=None, min_length=1)
    offline_root_recovery_signature: str | None = Field(default=None, min_length=1)
    threshold_attestation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_registry_verifier_time(
    verifier: AuthorityRegistryVerifierKeyVersion,
    *,
    now: datetime,
) -> None:
    if now < verifier.valid_from:
        raise AuthorityContractError(
            "registry_verifier_not_yet_valid",
            "registry verifier key is not yet valid",
        )
    if verifier.valid_until is not None and now >= verifier.valid_until:
        raise AuthorityContractError(
            "registry_verifier_expired",
            "registry verifier key has expired",
        )
    if (
        verifier.status is RegistryVerifierKeyStatus.REVOKED
        and verifier.revoked_at is not None
        and now >= verifier.revoked_at
    ):
        raise AuthorityContractError(
            "registry_verifier_revoked_at_verification_time",
            "registry verifier key was revoked at verification time",
        )
    if (
        verifier.status is RegistryVerifierKeyStatus.COMPROMISED
        and verifier.compromise_effective_at is not None
        and now >= verifier.compromise_effective_at
    ):
        raise AuthorityContractError(
            "registry_verifier_compromised_at_verification_time",
            "registry verifier key was compromised at verification time",
        )


class AuthorityRegistryTrustState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_id: UUID
    metadata_version: int = Field(ge=1)
    latest_verifier_key_version: int = Field(ge=1)
    complete_history_head_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    offline_root_version: int = Field(ge=1)
    threshold_signature_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_metadata_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @property
    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class CoverageKind(StrEnum):
    IN_ROOT = "in_root"
    EXTERNAL_PROOF = "external_proof"


class ManifestStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class AuthorityStateRootManifestVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    schema_version: int = Field(ge=1)
    canonicalization_version: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ManifestStatus = ManifestStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)


class AuthorityStateRootLeafFamily(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    manifest_version_id: UUID
    ordinal: int = Field(ge=1)
    family_name: str = Field(min_length=1, max_length=200)
    coverage_kind: CoverageKind
    external_proof_kind: str | None = Field(default=None, max_length=200)
    canonicalization_version: int = Field(ge=1)


def canonical_authority_manifest_digest(
    *,
    schema_version: int,
    canonicalization_version: int,
    families: list[AuthorityStateRootLeafFamily],
) -> str:
    payload = {
        "schema_version": schema_version,
        "canonicalization_version": canonicalization_version,
        "families": [
            {
                "ordinal": family.ordinal,
                "family_name": family.family_name,
                "coverage_kind": family.coverage_kind.value,
                "external_proof_kind": family.external_proof_kind,
                "canonicalization_version": family.canonicalization_version,
            }
            for family in sorted(families, key=lambda item: item.ordinal)
        ],
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuthorityStateRootLeafCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version_id: UUID
    authority_generation: int = Field(ge=0)
    ordinal: int = Field(ge=1)
    family_name: str = Field(min_length=1, max_length=200)
    record_version: int = Field(ge=1)
    leaf_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_proof_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def canonical_authority_leaf_digest(
    *,
    family_name: str,
    canonicalization_version: int,
    records: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "canonicalization_version": canonicalization_version,
        "family_name": family_name,
        "records": list(records),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_authority_root_digest(
    *,
    workspace_id: UUID,
    manifest: AuthorityStateRootManifestVersion,
    authority_generation: int,
    commitments: Sequence[AuthorityStateRootLeafCommitment],
) -> str:
    ordered = sorted(commitments, key=lambda item: item.ordinal)
    if authority_generation < 0:
        raise ValueError("authority_generation must be non-negative")
    if (
        not ordered
        or len({item.family_name for item in ordered}) != len(ordered)
        or [item.ordinal for item in ordered] != list(range(1, len(ordered) + 1))
        or any(
            item.manifest_version_id != manifest.id
            or item.authority_generation != authority_generation
            for item in ordered
        )
    ):
        raise ValueError("authority commitments do not form one exhaustive ordered generation")
    payload = {
        "authority_generation": authority_generation,
        "commitments": [
            {
                "external_proof_digest": item.external_proof_digest,
                "family_name": item.family_name,
                "leaf_digest": item.leaf_digest,
                "ordinal": item.ordinal,
                "record_version": item.record_version,
            }
            for item in ordered
        ],
        "manifest_digest": manifest.manifest_digest,
        "manifest_version_id": str(manifest.id),
        "workspace_id": str(workspace_id),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuthorityStateRootCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    manifest: AuthorityStateRootManifestVersion
    authority_generation: int = Field(ge=0)
    commitments: tuple[AuthorityStateRootLeafCommitment, ...]
    observed_leaf_digests: dict[str, str]
    root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_calculation(self) -> AuthorityStateRootCalculation:
        commitment_by_name = {item.family_name: item for item in self.commitments}
        if (
            len(commitment_by_name) != len(self.commitments)
            or set(commitment_by_name) != set(self.observed_leaf_digests)
            or any(
                self.observed_leaf_digests[name] != commitment.leaf_digest
                for name, commitment in commitment_by_name.items()
            )
        ):
            raise PydanticCustomError(
                "authority_root_leaf_mismatch",
                "reason_code={reason_code}",
                {"reason_code": "authority_root_leaf_mismatch"},
            )
        try:
            expected = canonical_authority_root_digest(
                workspace_id=self.workspace_id,
                manifest=self.manifest,
                authority_generation=self.authority_generation,
                commitments=self.commitments,
            )
        except ValueError as exc:
            raise PydanticCustomError(
                "authority_root_commitment_set_invalid",
                "reason_code={reason_code}",
                {"reason_code": "authority_root_commitment_set_invalid"},
            ) from exc
        if self.root_digest != expected:
            raise PydanticCustomError(
                "authority_root_digest_mismatch",
                "reason_code={reason_code}",
                {"reason_code": "authority_root_digest_mismatch"},
            )
        return self


class AuthorityRegistryFreshnessProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    registry_id: UUID
    trust_state_metadata_version: int = Field(ge=1)
    complete_history_head_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sequence: int = Field(ge=1)
    challenge_nonce_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    verifier_key_version_id: UUID
    registry_signature: str = Field(min_length=1)


def validate_authority_root_manifest(
    manifest: AuthorityStateRootManifestVersion,
    families: list[AuthorityStateRootLeafFamily],
    *,
    mutable_authority_families: set[str],
) -> None:
    listed = {family.family_name for family in families}
    if not mutable_authority_families <= listed:
        raise AuthorityContractError(
            "unlisted_authority_family",
            "one or more mutable authority families are absent from the manifest",
        )


def validate_authority_family_commitments(
    manifest: AuthorityStateRootManifestVersion,
    families: list[AuthorityStateRootLeafFamily],
    commitments: list[AuthorityStateRootLeafCommitment],
    *,
    observed_leaf_digests: dict[str, str],
) -> None:
    family_by_name = {family.family_name: family for family in families}
    commitment_by_name = {commitment.family_name: commitment for commitment in commitments}
    expected_names = set(family_by_name)
    if (
        len(family_by_name) != len(families)
        or len(commitment_by_name) != len(commitments)
        or set(commitment_by_name) != expected_names
        or set(observed_leaf_digests) != expected_names
    ):
        raise AuthorityContractError(
            "authority_family_commitment_set_mismatch",
            "manifest families, commitments, and observed leaves must match exactly",
        )
    for family_name in sorted(expected_names):
        family = family_by_name[family_name]
        commitment = commitment_by_name[family_name]
        if (
            family.manifest_version_id != manifest.id
            or commitment.manifest_version_id != manifest.id
            or commitment.ordinal != family.ordinal
        ):
            raise AuthorityContractError(
                "authority_family_commitment_binding_mismatch",
                "a leaf commitment does not bind its exact manifest family",
            )
        if observed_leaf_digests[family_name] != commitment.leaf_digest:
            raise AuthorityContractError(
                "authority_family_rollback_detected",
                f"authority family {family_name!r} does not match its committed digest",
            )


def validate_registry_freshness_proof(
    proof: AuthorityRegistryFreshnessProof,
    trust_state: AuthorityRegistryTrustState,
    *,
    now: datetime,
    minimum_sequence: int,
    expected_nonce_digest: str,
) -> None:
    if proof.registry_sequence <= minimum_sequence:
        raise AuthorityContractError(
            "registry_sequence_replay",
            "registry freshness sequence did not advance",
        )
    if proof.challenge_nonce_digest != expected_nonce_digest:
        raise AuthorityContractError(
            "registry_nonce_mismatch",
            "registry freshness proof does not bind the caller nonce",
        )


def validate_registry_trust_transition(
    previous: AuthorityRegistryTrustState,
    current: AuthorityRegistryTrustState,
    *,
    now: datetime,
) -> None:
    if current.registry_id != previous.registry_id:
        raise AuthorityContractError(
            "registry_identity_mismatch",
            "registry trust metadata belongs to another registry",
        )
    if current.metadata_version != previous.metadata_version + 1:
        raise AuthorityContractError(
            "registry_metadata_version_skipped",
            "registry trust metadata must advance exactly one version",
        )
    if current.previous_metadata_digest != previous.canonical_digest:
        raise AuthorityContractError(
            "registry_metadata_prefix_mismatch",
            "registry trust metadata does not bind its exact predecessor",
        )
    if current.complete_history_head_digest == previous.complete_history_head_digest:
        raise AuthorityContractError(
            "registry_history_head_frozen",
            "registry history head did not advance",
        )
    if current.issued_at <= previous.issued_at:
        raise AuthorityContractError(
            "registry_metadata_time_not_advanced",
            "registry trust metadata issuance time did not advance",
        )
    if current.latest_verifier_key_version < previous.latest_verifier_key_version:
        raise AuthorityContractError(
            "registry_verifier_version_rollback",
            "registry verifier key version moved backward",
        )
    if current.latest_verifier_key_version > previous.latest_verifier_key_version + 1:
        raise AuthorityContractError(
            "registry_verifier_version_skipped",
            "registry verifier key version skipped rotation history",
        )
    if current.expires_at <= now:
        raise AuthorityContractError(
            "registry_trust_state_expired",
            "registry trust metadata is expired",
        )


class EpochKeyDisposition(StrEnum):
    PENDING = "pending"
    DESTROYED = "destroyed"
    REVOKED = "revoked"


class AuthorityCloseCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    closed_epoch: int = Field(ge=1)
    source_deployment_id: UUID
    source_deployment_instance_id: UUID
    target_deployment_id: UUID
    epoch_credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destruction_or_revocation_attestation_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    final_authority_generation: int = Field(ge=0)
    final_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_signing_key_version_id: UUID
    workspace_signature: str = Field(min_length=1)
    anchor_registry_id: UUID | None = None
    registry_sequence: int | None = Field(default=None, ge=1)
    registry_signing_key_version_id: UUID | None = None
    registry_signature: str | None = Field(default=None, min_length=1)
    local_anchor_receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    anchor_receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    epoch_key_disposition: EpochKeyDisposition = EpochKeyDisposition.PENDING
    externally_anchored_at: datetime | None = None

    @model_validator(mode="after")
    def validate_anchor_pair(self) -> AuthorityCloseCertificate:
        if (self.anchor_receipt_digest is None) != (self.externally_anchored_at is None):
            raise PydanticCustomError(
                "incomplete_close_anchor",
                "reason_code={reason_code}",
                {"reason_code": "close_anchor_digest_and_time_must_coexist"},
            )
        return self


class AuthorityHandoffActivation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    target_deployment_instance_id: UUID
    authority_epoch: int = Field(ge=2)
    source_close_certificate_id: UUID
    source_close_certificate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_epoch_credential_id: UUID
    exclusive_lease_or_local_anchor_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: datetime


def validate_handoff_activation(
    close: AuthorityCloseCertificate,
    activation: AuthorityHandoffActivation,
) -> None:
    if activation.workspace_id != close.workspace_id:
        raise AuthorityContractError(
            "handoff_workspace_mismatch",
            "the close certificate belongs to another workspace",
        )
    if activation.source_close_certificate_id != close.id:
        raise AuthorityContractError(
            "handoff_close_certificate_mismatch",
            "the activation does not bind the exact close certificate",
        )
    if activation.authority_epoch != close.closed_epoch + 1:
        raise AuthorityContractError(
            "handoff_epoch_not_advanced",
            "handoff activation must advance exactly one authority epoch",
        )
    if close.anchor_receipt_digest is None or close.externally_anchored_at is None:
        raise AuthorityContractError(
            "handoff_close_not_anchored",
            "the source authority close has not been externally anchored",
        )
    if (
        close.epoch_key_disposition
        not in {EpochKeyDisposition.DESTROYED, EpochKeyDisposition.REVOKED}
        or close.destruction_or_revocation_attestation_digest is None
    ):
        raise AuthorityContractError(
            "handoff_old_epoch_key_still_active",
            "the source epoch key lacks destruction or revocation evidence",
        )
    if activation.activated_at <= close.externally_anchored_at:
        raise AuthorityContractError(
            "handoff_activation_precedes_close",
            "the target cannot activate before the source authority closes",
        )


class AuthorityHandoffState(StrEnum):
    PREPARED = "prepared"
    SOURCE_CLOSED_ANCHORED = "source_closed_anchored"
    TARGET_ACTIVE = "target_active"
    ABORTED = "aborted"


class AuthorityHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    from_deployment_id: UUID
    from_deployment_instance_id: UUID
    to_deployment_id: UUID
    to_deployment_instance_id: UUID
    from_epoch: int = Field(ge=1)
    to_epoch: int = Field(ge=2)
    export_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_checkpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_signing_key_version_id: UUID
    close_certificate_id: UUID
    target_epoch_credential_id: UUID
    state: AuthorityHandoffState
    prepared_at: datetime = Field(default_factory=_now_utc)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_transition_binding(self) -> AuthorityHandoff:
        if self.to_epoch != self.from_epoch + 1:
            raise PydanticCustomError(
                "invalid_handoff_epoch",
                "reason_code={reason_code}",
                {"reason_code": "handoff_epoch_must_advance_once"},
            )
        if self.from_deployment_instance_id == self.to_deployment_instance_id:
            raise PydanticCustomError(
                "invalid_handoff_target",
                "reason_code={reason_code}",
                {"reason_code": "handoff_requires_distinct_target_instance"},
            )
        terminal = self.state in {
            AuthorityHandoffState.TARGET_ACTIVE,
            AuthorityHandoffState.ABORTED,
        }
        if terminal != (self.completed_at is not None):
            raise PydanticCustomError(
                "invalid_handoff_completion",
                "reason_code={reason_code}",
                {"reason_code": "handoff_terminal_state_completion_mismatch"},
            )
        if self.completed_at is not None and self.completed_at < self.prepared_at:
            raise PydanticCustomError(
                "invalid_handoff_chronology",
                "reason_code={reason_code}",
                {"reason_code": "handoff_completion_precedes_preparation"},
            )
        return self


class RestoreDecision(StrEnum):
    READ_QUEUE_ONLY = "read_queue_only"
    EXCLUSIVE_TAKEOVER_NEW_EPOCH = "exclusive_takeover_new_epoch"


class RestoreValidationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    restored_database_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_epoch: int = Field(ge=1)
    observed_generation: int = Field(ge=0)
    observed_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_anchor_id: UUID
    former_instance_revocation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    takeover_lease_or_local_anchor_receipt_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    takeover_epoch: int | None = Field(default=None, ge=2)
    decision: RestoreDecision = RestoreDecision.READ_QUEUE_ONLY
    reason_code: str = Field(min_length=1, max_length=200)
    validated_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def validate_takeover_evidence(self) -> RestoreValidationReceipt:
        if self.decision is RestoreDecision.EXCLUSIVE_TAKEOVER_NEW_EPOCH:
            if (
                self.former_instance_revocation_digest is None
                or self.takeover_lease_or_local_anchor_receipt_digest is None
            ):
                raise PydanticCustomError(
                    "unsafe_restore_takeover",
                    "reason_code={reason_code}",
                    {"reason_code": "takeover_requires_revocation_and_exclusive_receipt"},
                )
            if self.takeover_epoch != self.observed_epoch + 1:
                raise PydanticCustomError(
                    "unsafe_restore_takeover_epoch",
                    "reason_code={reason_code}",
                    {"reason_code": "takeover_epoch_must_advance_once"},
                )
        return self
