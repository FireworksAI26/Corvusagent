from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class _ScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID


class WorkspaceScope(_ScopeContract):
    kind: Literal["workspace"] = "workspace"


class ProjectScope(_ScopeContract):
    kind: Literal["project"] = "project"
    project_id: UUID


class ChannelScope(_ScopeContract):
    kind: Literal["channel"] = "channel"
    channel_id: UUID
    project_id: UUID | None = None


class ThreadScope(_ScopeContract):
    kind: Literal["thread"] = "thread"
    channel_id: UUID
    thread_id: UUID
    project_id: UUID | None = None


ParentScope = Annotated[ProjectScope | ChannelScope | ThreadScope, Field(discriminator="kind")]


class ConversationScope(_ScopeContract):
    kind: Literal["conversation"] = "conversation"
    conversation_id: UUID
    parent: ParentScope

    @model_validator(mode="after")
    def validate_parent_workspace(self) -> ConversationScope:
        if self.parent.workspace_id != self.workspace_id:
            raise PydanticCustomError(
                "invalid_scope_parent",
                "reason_code={reason_code}",
                {"reason_code": "cross_workspace_scope_parent"},
            )
        return self

    @property
    def parent_scope_kind(self) -> str:
        return self.parent.kind

    @property
    def parent_scope_id(self) -> UUID:
        if isinstance(self.parent, ProjectScope):
            return self.parent.project_id
        if isinstance(self.parent, ChannelScope):
            return self.parent.channel_id
        return self.parent.thread_id


class AudiencePolicySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    visibility: Literal[
        "personal",
        "explicit_principals",
        "role",
        "project",
        "channel",
        "thread",
        "workspace",
    ]
    owner_principal_id: UUID | None = None
    principal_ids: frozenset[UUID] = Field(default_factory=frozenset)
    role_ids: frozenset[UUID] = Field(default_factory=frozenset)
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: int = Field(ge=1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_visibility(self) -> AudiencePolicySnapshot:
        if self.visibility == "personal" and self.owner_principal_id is None:
            raise PydanticCustomError(
                "invalid_audience",
                "reason_code={reason_code}",
                {"reason_code": "personal_visibility_requires_owner"},
            )
        return self
