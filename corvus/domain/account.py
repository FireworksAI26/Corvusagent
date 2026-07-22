from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from corvus.domain.identity import RecordStatus


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _contract_error(reason_code: str) -> PydanticCustomError:
    return PydanticCustomError(reason_code, "{reason_code}", {"reason_code": reason_code})


def _require_aware(value: datetime, reason_code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _contract_error(reason_code)
    return value


def normalize_identity_email(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("identity_email_invalid")
    local_part, domain = normalized.split("@", maxsplit=1)
    if not local_part or not domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("identity_email_invalid")
    return normalized


class ExperienceKind(StrEnum):
    EVERYDAY = "everyday"
    DEVELOPER = "developer"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class _AccountContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Account(_AccountContract):
    id: UUID = Field(default_factory=uuid4)
    principal_id: UUID
    normalized_email: str = Field(min_length=3, max_length=320)
    experience_kind: ExperienceKind | None = None
    status: RecordStatus = RecordStatus.ACTIVE
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)

    @field_validator("normalized_email")
    @classmethod
    def validate_normalized_email(cls, value: str) -> str:
        try:
            normalized = normalize_identity_email(value)
        except ValueError as exc:
            raise _contract_error("identity_email_invalid") from exc
        if normalized != value:
            raise _contract_error("identity_email_must_be_normalized")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> Account:
        _require_aware(self.created_at, "account_created_at_must_be_timezone_aware")
        _require_aware(self.updated_at, "account_updated_at_must_be_timezone_aware")
        return self


class ExternalIdentity(_AccountContract):
    id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=512)
    normalized_email: str = Field(min_length=3, max_length=320)
    email_verified: bool
    created_at: datetime = Field(default_factory=_now_utc)

    @field_validator("normalized_email")
    @classmethod
    def validate_normalized_email(cls, value: str) -> str:
        try:
            normalized = normalize_identity_email(value)
        except ValueError as exc:
            raise _contract_error("identity_email_invalid") from exc
        if normalized != value:
            raise _contract_error("identity_email_must_be_normalized")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "external_identity_created_at_must_be_timezone_aware")


class DeviceRegistration(_AccountContract):
    id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    name: str = Field(min_length=1, max_length=200)
    public_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DeviceStatus = DeviceStatus.ACTIVE
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_revocation(self) -> DeviceRegistration:
        _require_aware(self.created_at, "device_created_at_must_be_timezone_aware")
        _require_aware(self.updated_at, "device_updated_at_must_be_timezone_aware")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, "device_revoked_at_must_be_timezone_aware")
        if self.status == DeviceStatus.REVOKED and self.revoked_at is None:
            raise _contract_error("device_revoked_at_required")
        if self.status == DeviceStatus.ACTIVE and self.revoked_at is not None:
            raise _contract_error("active_device_cannot_have_revoked_at")
        return self


class SessionRecord(_AccountContract):
    id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    device_id: UUID
    device_version: int = Field(default=1, ge=1)
    version: int = Field(default=1, ge=1)
    token_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    predecessor_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: SessionStatus = SessionStatus.ACTIVE
    issued_at: datetime = Field(default_factory=_now_utc)
    expires_at: datetime
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lineage(self) -> SessionRecord:
        _require_aware(self.issued_at, "session_issued_at_must_be_timezone_aware")
        _require_aware(self.expires_at, "session_expires_at_must_be_timezone_aware")
        if self.expires_at <= self.issued_at:
            raise _contract_error("session_expiry_must_follow_issue")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, "session_revoked_at_must_be_timezone_aware")
        if self.status == SessionStatus.ACTIVE:
            if self.token_digest is None:
                raise _contract_error("active_session_digest_required")
            if self.revoked_at is not None:
                raise _contract_error("active_session_cannot_have_revoked_at")
        else:
            if self.revoked_at is None:
                raise _contract_error("session_revoked_at_required")
            if self.token_digest is not None:
                raise _contract_error("revoked_session_cannot_issue_digest")
            if self.predecessor_digest is None:
                raise _contract_error("revoked_session_predecessor_required")
        if self.version == 1 and self.predecessor_digest is not None:
            raise _contract_error("initial_session_cannot_have_predecessor")
        if self.version > 1 and self.predecessor_digest is None:
            raise _contract_error("session_predecessor_required")
        return self
