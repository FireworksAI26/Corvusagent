from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _now_utc() -> datetime:
    return datetime.now(UTC)


class RecordStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    ARCHIVED = "archived"


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE = "service"
    CHANNEL = "channel"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class WorkspaceKind(StrEnum):
    INDIVIDUAL = "individual"
    TEAM = "team"


class _IdentityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Workspace(_IdentityContract):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=200)
    workspace_kind: WorkspaceKind = WorkspaceKind.INDIVIDUAL
    status: RecordStatus = RecordStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)


class Principal(_IdentityContract):
    id: UUID = Field(default_factory=uuid4)
    kind: PrincipalKind
    external_provider: str = Field(min_length=1, max_length=200)
    external_subject: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=_now_utc)


class WorkspaceMembership(_IdentityContract):
    workspace_id: UUID
    principal_id: UUID
    role: str = Field(min_length=1, max_length=100)
    status: MembershipStatus = MembershipStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)


class Project(_IdentityContract):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    name: str = Field(min_length=1, max_length=200)
    root_locator: str = Field(min_length=1, max_length=2048)
    privacy: str = Field(min_length=1, max_length=100)
    status: RecordStatus = RecordStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)


class AgentIdentity(_IdentityContract):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=100)
    model_route: str = Field(min_length=1, max_length=200)
    skill_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AgentStatus = AgentStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)
