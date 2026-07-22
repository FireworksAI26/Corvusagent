from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from corvus.domain.account import ExperienceKind
from corvus.domain.identity import RecordStatus, WorkspaceKind


def _now_utc() -> datetime:
    return datetime.now(UTC)


class _SyncModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class AccountProfilePayload(_SyncModel):
    experience_kind: ExperienceKind


class WorkspaceProfilePayload(_SyncModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_kind: WorkspaceKind | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise PydanticCustomError(
                "workspace_name_invalid", "{reason_code}", {"reason_code": "workspace_name_invalid"}
            )
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> WorkspaceProfilePayload:
        if self.name is None and self.workspace_kind is None:
            raise PydanticCustomError(
                "workspace_profile_empty",
                "{reason_code}",
                {"reason_code": "workspace_profile_empty"},
            )
        return self


class SyncMutation(_SyncModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    kind: Literal["account_profile", "workspace_profile"]
    operation: Literal["set_experience", "update"]
    entity_id: UUID
    expected_version: int = Field(ge=1)
    payload: AccountProfilePayload | WorkspaceProfilePayload

    @model_validator(mode="after")
    def validate_discriminator(self) -> SyncMutation:
        valid = (
            self.kind == "account_profile"
            and self.operation == "set_experience"
            and isinstance(self.payload, AccountProfilePayload)
        ) or (
            self.kind == "workspace_profile"
            and self.operation == "update"
            and isinstance(self.payload, WorkspaceProfilePayload)
        )
        if not valid:
            raise PydanticCustomError(
                "sync_command_unknown", "{reason_code}", {"reason_code": "sync_command_unknown"}
            )
        return self


class AccountProfile(_SyncModel):
    entity_id: UUID
    experience_kind: ExperienceKind
    version: int = Field(ge=1)


class WorkspaceProfile(_SyncModel):
    entity_id: UUID
    name: str = Field(min_length=1, max_length=200)
    workspace_kind: WorkspaceKind
    status: RecordStatus
    version: int = Field(ge=1)


class SyncMutationResult(_SyncModel):
    idempotency_key: str
    kind: Literal["account_profile", "workspace_profile"]
    operation: Literal["set_experience", "update"]
    entity_id: UUID
    entity_version: int = Field(ge=1)
    sequence: int = Field(ge=1)
    profile: AccountProfile | WorkspaceProfile


class SyncApplyResult(_SyncModel):
    acknowledged_cursor: int = Field(ge=0)
    results: tuple[SyncMutationResult, ...] = ()


class WorkspaceChange(_SyncModel):
    workspace_id: UUID
    workspace_version: int = Field(ge=1)
    sequence: int = Field(ge=1)
    previous_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["account_profile", "workspace_profile"]
    operation: Literal["set_experience", "update"]
    entity_id: UUID
    entity_version: int = Field(ge=1)
    payload: AccountProfile | WorkspaceProfile
    account_id: UUID
    principal_id: UUID
    membership_version: int = Field(ge=1)
    device_id: UUID
    device_version: int = Field(ge=1)
    created_at: datetime = Field(default_factory=_now_utc)


class SyncPage(_SyncModel):
    requested_cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    high_watermark: int = Field(ge=0)
    earliest_retained_sequence: int = Field(ge=1)
    changes: tuple[WorkspaceChange, ...] = ()
    has_more: bool


class SyncConflictDetail(_SyncModel):
    code: Literal["sync_version_conflict"] = "sync_version_conflict"
    mutation_index: int = Field(ge=0)
    submitted_expected_version: int = Field(ge=1)
    current_version: int = Field(ge=1)
    current_profile: dict[str, Any]


class SyncProtocolError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: dict[str, Any] | SyncConflictDetail | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class SyncConflictError(SyncProtocolError):
    def __init__(self, detail: SyncConflictDetail) -> None:
        super().__init__(detail.code, detail)
        self.conflict_detail = detail
