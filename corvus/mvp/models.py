from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MvpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkItemStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EffectBinding(MvpModel):
    kind: Literal["provider", "filesystem"]
    target: str = Field(min_length=1, max_length=2048)
    payload: dict[str, Any]


class WorkItemDefinition(MvpModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    depends_on: tuple[str, ...] = ()
    cost_units: int = Field(default=0, ge=0)
    requires_approval: bool = False
    effect: EffectBinding | None = None

    @model_validator(mode="after")
    def validate_effect_requirements(self) -> WorkItemDefinition:
        if self.requires_approval and self.effect is None:
            raise ValueError("approval_requires_effect")
        if self.cost_units and self.effect is None:
            raise ValueError("budget_cost_requires_effect")
        if self.key in self.depends_on:
            raise ValueError("work_item_cannot_depend_on_itself")
        return self


class Project(MvpModel):
    id: str
    tenant_id: str
    name: str
    created_at: datetime


class OutcomeContract(MvpModel):
    id: str
    project_id: str
    version: int = Field(ge=1)
    title: str
    acceptance_criteria: tuple[str, ...]
    created_at: datetime


class Workflow(MvpModel):
    id: str
    outcome_id: str
    name: str
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime


class WorkItem(MvpModel):
    id: str
    workflow_id: str
    key: str
    title: str
    status: WorkItemStatus
    depends_on: tuple[str, ...]
    cost_units: int
    requires_approval: bool
    effect: EffectBinding | None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_fence: int = 0
    attempt_count: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class WorkClaim(MvpModel):
    attempt_id: str
    work_item_id: str
    workflow_id: str
    key: str
    worker_id: str
    lease_fence: int
    lease_expires_at: datetime


class RecoveryResult(MvpModel):
    workflow_id: str
    recovered_items: int = Field(ge=0)


class EffectRecord(MvpModel):
    id: str
    workflow_id: str
    work_item_id: str
    idempotency_key: str
    binding: EffectBinding
    status: str
    approval_id: str | None = None
    execution_count: int = Field(ge=0)
    result: dict[str, Any] | None = None


class ApprovalRecord(MvpModel):
    id: str
    effect_id: str
    actor_id: str
    status: Literal["approved", "rejected"]
    created_at: datetime
    consumed_at: datetime | None = None


class BudgetAccount(MvpModel):
    project_id: str
    limit_units: int = Field(ge=0)
    reserved_units: int = Field(ge=0)
    settled_units: int = Field(ge=0)

    @property
    def available_units(self) -> int:
        return self.limit_units - self.reserved_units - self.settled_units


class ArtifactRecord(MvpModel):
    id: str
    workflow_id: str
    work_item_id: str
    digest: str
    media_type: str
    content: dict[str, Any]
    created_at: datetime


class CheckpointRecord(MvpModel):
    id: str
    workflow_id: str
    work_item_id: str
    state: str
    created_at: datetime


class ConversationEntry(MvpModel):
    id: str
    workflow_id: str
    work_item_id: str | None
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    created_at: datetime
