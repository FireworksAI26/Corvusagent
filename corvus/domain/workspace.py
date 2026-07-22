from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _now_utc() -> datetime:
    return datetime.now(UTC)


class CollaborationMode(StrEnum):
    INDIVIDUAL = "individual"
    TEAM = "team"


class KillSwitchState(StrEnum):
    OPERATIONAL = "operational"
    PAUSED = "paused"
    HALTED = "halted"


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    workspace_id: UUID = Field(default_factory=uuid4)
    collaboration_mode: CollaborationMode
    autonomy_ceiling: int = Field(default=3, ge=0, le=5)
    shadow_policy_id: UUID = Field(default_factory=uuid4)
    budget_policy_id: UUID = Field(default_factory=uuid4)
    memory_policy_id: UUID = Field(default_factory=uuid4)
    kill_switch_state: KillSwitchState = KillSwitchState.OPERATIONAL
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
