from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ExecutionKind(StrEnum):
    LOCAL_RUNNER = "local_runner"
    CLOUD_WORKER = "cloud_worker"
    CONNECTOR = "connector"


class ExecutionStatus(StrEnum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    REVOKED = "revoked"


class ExecutionPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    kind: ExecutionKind
    runner_id: UUID | None = None
    connector_id: UUID | None = None
    sandbox_profile: str = Field(min_length=1)
    data_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ExecutionStatus = ExecutionStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def validate_discriminator_fields(self) -> ExecutionPlacement:
        if self.kind is ExecutionKind.CONNECTOR:
            if self.connector_id is None or self.runner_id is not None:
                raise PydanticCustomError(
                    "invalid_execution_identity",
                    "reason_code={reason_code}",
                    {"reason_code": "connector_requires_only_connector_id"},
                )
        elif self.runner_id is None or self.connector_id is not None:
            raise PydanticCustomError(
                "invalid_execution_identity",
                "reason_code={reason_code}",
                {"reason_code": "runner_placement_requires_only_runner_id"},
            )
        return self
