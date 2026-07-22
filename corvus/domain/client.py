from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ClientSurface(StrEnum):
    CLI = "cli"
    DESKTOP = "desktop"
    WEB = "web"
    CHANNEL = "channel"


class ClientContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    surface: ClientSurface
    transport_principal_id: UUID | None = None
    session_id: UUID
    origin: str = Field(min_length=1, max_length=512)
    issued_at: datetime = Field(default_factory=_now_utc)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def validate_lifetime(self) -> ClientContext:
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise PydanticCustomError(
                "naive_timestamp",
                "reason_code={reason_code}",
                {"reason_code": "issued_at_must_be_timezone_aware"},
            )
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise PydanticCustomError(
                    "naive_timestamp",
                    "reason_code={reason_code}",
                    {"reason_code": "expires_at_must_be_timezone_aware"},
                )
            if self.expires_at <= self.issued_at:
                raise PydanticCustomError(
                    "invalid_lifetime",
                    "reason_code={reason_code}",
                    {"reason_code": "expires_at_must_follow_issued_at"},
                )
        return self
