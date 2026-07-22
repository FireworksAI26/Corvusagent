from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def _now_utc() -> datetime:
    return datetime.now(UTC)


class AccessContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class CapabilityEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AccessBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    principal_id: UUID
    scope_kind: Literal["workspace", "project", "channel", "thread", "conversation"]
    scope_id: UUID
    issued_by: UUID
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_expiry(self) -> AccessBundle:
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise PydanticCustomError(
                "naive_timestamp",
                "reason_code={reason_code}",
                {"reason_code": "expiry_must_be_timezone_aware"},
            )
        return self


class CapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    bundle_id: UUID
    workspace_id: UUID
    resource_kind: str = Field(min_length=1, max_length=100)
    resource_id: UUID
    action: str = Field(min_length=1, max_length=200)
    effect: CapabilityEffect
    constraints_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now_utc)


def validate_access_bundle(
    bundle: AccessBundle,
    grants: list[CapabilityGrant],
) -> None:
    for grant in grants:
        if grant.bundle_id != bundle.id:
            raise AccessContractError(
                "capability_grant_bundle_mismatch",
                "capability grant belongs to another bundle",
            )
        if grant.workspace_id != bundle.workspace_id:
            raise AccessContractError(
                "cross_workspace_capability_grant",
                "capability grant belongs to another workspace",
            )


class AgentGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    agent_id: UUID
    capability_bundle_id: UUID
    autonomy_level: int = Field(ge=0, le=5)
    issued_by: UUID
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now_utc)


class DelegationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    parent_agent_grant_id: UUID
    child_agent_id: UUID
    capabilities: frozenset[str] = Field(min_length=1)
    budget_json: dict[str, Any]
    depth_limit: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifetime(self) -> DelegationGrant:
        for value, reason_code in (
            (self.issued_at, "delegation_issued_at_must_be_timezone_aware"),
            (self.expires_at, "delegation_expiry_must_be_timezone_aware"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise PydanticCustomError(
                    "naive_delegation_timestamp",
                    "reason_code={reason_code}",
                    {"reason_code": reason_code},
                )
        if self.expires_at <= self.issued_at:
            raise PydanticCustomError(
                "invalid_delegation_lifetime",
                "reason_code={reason_code}",
                {"reason_code": "delegation_expiry_must_follow_issue_time"},
            )
        return self


class EffectiveCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_context_id: UUID
    workspace_authority_epoch: int = Field(ge=1)
    workspace_authority_generation: int = Field(ge=0)
    authority_state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_commit_receipt_id: UUID
    actions: frozenset[str]
    unavailable_reason_codes: dict[str, str]
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    kill_switch_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_disjoint_actions(self) -> EffectiveCapabilities:
        if self.actions & self.unavailable_reason_codes.keys():
            raise PydanticCustomError(
                "contradictory_effective_capability",
                "reason_code={reason_code}",
                {"reason_code": "action_cannot_be_available_and_unavailable"},
            )
        return self


class CredentialKind(StrEnum):
    OS_KEYRING = "os_keyring"
    CLOUD_VAULT = "cloud_vault"
    PROVIDER_OAUTH = "provider_oauth"
    LOCAL_CONNECTOR = "local_connector"


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


_LOCATOR_PREFIX = {
    CredentialKind.OS_KEYRING: "keyring://",
    CredentialKind.CLOUD_VAULT: "vault://",
    CredentialKind.PROVIDER_OAUTH: "oauth://",
    CredentialKind.LOCAL_CONNECTOR: "connector://",
}


class CredentialRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    owner_principal_id: UUID | None = None
    provider_connection_id: UUID
    kind: CredentialKind
    opaque_locator: str = Field(min_length=1, max_length=2048)
    scopes: frozenset[str] = Field(default_factory=frozenset)
    status: CredentialStatus = CredentialStatus.ACTIVE
    expires_at: datetime | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def validate_opaque_locator(self) -> CredentialRef:
        prefix = _LOCATOR_PREFIX[self.kind]
        if not self.opaque_locator.startswith(prefix) or self.opaque_locator == prefix:
            raise PydanticCustomError(
                "plaintext_credential",
                "reason_code={reason_code}",
                {"reason_code": "credential_locator_must_be_opaque_reference"},
            )
        if any(marker in self.opaque_locator for marker in ("?", "#", "@")):
            raise PydanticCustomError(
                "plaintext_credential",
                "reason_code={reason_code}",
                {"reason_code": "credential_locator_must_be_opaque_reference"},
            )
        return self
