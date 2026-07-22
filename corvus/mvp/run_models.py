from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from corvus.mvp.models import MvpModel


class RunStatus(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    CONTRIBUTION_READY = "contribution_ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    DISCARDED = "discarded"


class StartRunRequest(MvpModel):
    repository_id: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=262_144)
    provider: Literal["codex"] = "codex"
    model: str | None = Field(default=None, min_length=1, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    mode: Literal["chat", "build"] = "build"
    safety_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_version_id: str | None = None
    schedule_id: str | None = None
    occurrence_key: str | None = None
    output_policy: Literal[
        "report_only",
        "prepare_changes",
        "prepare_contribution",
    ] = "prepare_changes"

    @model_validator(mode="after")
    def occurrence_requires_schedule(self) -> StartRunRequest:
        if (self.schedule_id is None) != (self.occurrence_key is None):
            raise ValueError("run_schedule_occurrence_pair_required")
        return self


class RunRecord(MvpModel):
    id: str
    tenant_id: str
    repository_id: str
    base_sha: str
    task: str
    provider: Literal["codex"]
    model: str | None
    effort: Literal["low", "medium", "high", "xhigh"]
    mode: Literal["chat", "build"]
    safety_digest: str
    skill_version_id: str | None
    schedule_id: str | None
    occurrence_key: str | None
    output_policy: Literal["report_only", "prepare_changes", "prepare_contribution"]
    retry_of_run_id: str | None
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunEvent(MvpModel):
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunEvidence(MvpModel):
    id: str
    run_id: str
    kind: str
    summary: str
    digest: str
    created_at: datetime
