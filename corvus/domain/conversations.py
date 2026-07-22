from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from corvus.domain.agent_runtime import AgentRunEvent, AgentRunEventType
from corvus.security import SecurityError, canonical_json_bytes, reject_sensitive_payload

SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_THREAD_TITLE_CHARACTERS = 200
MAX_MESSAGE_CONTENT_CHARACTERS = 100_000
MAX_IDEMPOTENCY_KEY_CHARACTERS = 512
MAX_SAFE_NAME_CHARACTERS = 255
MAX_MEDIA_TYPE_CHARACTERS = 255
MAX_METADATA_BYTES = 64 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024 * 1024
GENESIS_EVENT_DIGEST = "0" * 64

_MEDIA_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "blob",
        "body",
        "bytes",
        "content",
        "data",
        "file",
        "filepath",
        "locator",
        "path",
        "uri",
        "url",
    }
)


def _error(reason_code: str) -> None:
    raise PydanticCustomError(
        "conversation_contract_error",
        "reason_code={reason_code}",
        {"reason_code": reason_code},
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _error("conversation_timestamp_must_be_timezone_aware")
    return value


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(
            JsonValue,
            MappingProxyType({key: _freeze_json(item) for key, item in value.items()}),
        )
    if isinstance(value, list):
        return cast(JsonValue, tuple(_freeze_json(item) for item in value))
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(JsonValue, {str(key): _thaw_json(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return cast(JsonValue, [_thaw_json(item) for item in value])
    return cast(JsonValue, value)


def _validate_metadata(metadata: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    try:
        reject_sensitive_payload(metadata)
        encoded = canonical_json_bytes(metadata)
    except SecurityError as exc:
        message = str(exc)
        if "non-finite" in message or "canonical" in message or "cyclic" in message:
            _error("attachment_metadata_must_be_canonical")
        _error("attachment_metadata_sensitive_payload_rejected")

    def reject_locator_keys(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if _normalize_key(str(key)) in _FORBIDDEN_METADATA_KEYS:
                    _error("attachment_metadata_sensitive_payload_rejected")
                reject_locator_keys(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                reject_locator_keys(item)

    reject_locator_keys(metadata)
    if len(encoded) > MAX_METADATA_BYTES:
        _error("attachment_metadata_too_large")
    return cast(Mapping[str, JsonValue], _freeze_json(dict(metadata)))


def _safe_leaf_name(value: str, *, prefix: str) -> str:
    if not value.strip() or value != value.strip() or len(value) > MAX_SAFE_NAME_CHARACTERS:
        _error(f"{prefix}_name_must_be_safe_leaf")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        windows.is_absolute()
        or posix.is_absolute()
        or windows.drive
        or len(windows.parts) != 1
        or len(posix.parts) != 1
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or PurePath(value).name != value
    ):
        _error(f"{prefix}_name_must_be_safe_leaf")
    return value


def _media_type(value: str, *, prefix: str) -> str:
    if len(value) > MAX_MEDIA_TYPE_CHARACTERS or _MEDIA_TYPE_PATTERN.fullmatch(value) is None:
        _error(f"{prefix}_media_type_invalid")
    return value.casefold()


def compute_content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_lineage_digest(
    *,
    workspace_id: UUID,
    artifact_id: UUID,
    run_id: UUID,
    producing_event_sequence: int,
    content_digest: str,
    parent_artifact_ids: Sequence[UUID],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "artifact_id": artifact_id,
                "content_digest": content_digest,
                "parent_artifact_ids": sorted(str(item) for item in parent_artifact_ids),
                "producing_event_sequence": producing_event_sequence,
                "run_id": run_id,
                "workspace_id": workspace_id,
            }
        )
    ).hexdigest()


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageAuthorKind(StrEnum):
    PRINCIPAL = "principal"
    AGENT = "agent"
    SYSTEM = "system"


class _ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Thread(_ConversationModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    workspace_version: int = Field(ge=1)
    project_id: UUID | None = None
    creator_principal_id: UUID
    creator_membership_version: int = Field(ge=1)
    title: str
    status: ThreadStatus = ThreadStatus.ACTIVE
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            _error("thread_title_blank")
        if len(value) > MAX_THREAD_TITLE_CHARACTERS:
            _error("thread_title_too_long")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_chronology(self) -> Thread:
        if self.updated_at < self.created_at:
            _error("thread_updated_before_creation")
        return self


class AttachmentRef(_ConversationModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    owner_principal_id: UUID
    owner_membership_version: int = Field(ge=1)
    display_name: str
    media_type: str
    byte_size: int = Field(ge=0, le=MAX_ATTACHMENT_BYTES)
    content_digest: str = Field(pattern=SHA256_PATTERN)
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _safe_leaf_name(value, prefix="attachment")

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        return _media_type(value, prefix="attachment")

    @field_validator("metadata", mode="after")
    @classmethod
    def validate_metadata(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return _validate_metadata(value)

    @field_serializer("metadata")
    def serialize_metadata(self, value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _thaw_json(value))

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)


class Message(_ConversationModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    thread_id: UUID
    sequence: int = Field(ge=1)
    content: str
    content_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARACTERS)
    producing_run_id: UUID | None = None
    attachment_ids: tuple[UUID, ...] = ()
    author_kind: MessageAuthorKind
    author_principal_id: UUID | None = None
    author_membership_version: int | None = Field(default=None, ge=1)
    author_agent_id: UUID | None = None
    author_agent_version: int | None = Field(default=None, ge=1)
    created_at: datetime

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            _error("message_content_blank")
        if len(value) > MAX_MESSAGE_CONTENT_CHARACTERS:
            _error("message_content_too_long")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            _error("message_idempotency_key_blank")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_message(self) -> Message:
        if self.content_digest != compute_content_digest(self.content):
            _error("message_digest_mismatch")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            _error("message_attachment_ids_duplicate")
        principal_shape = (
            self.author_principal_id is not None and self.author_membership_version is not None
        )
        agent_shape = self.author_agent_id is not None and self.author_agent_version is not None
        partial_shape = (self.author_principal_id is None) != (
            self.author_membership_version is None
        ) or (self.author_agent_id is None) != (self.author_agent_version is None)
        valid = (
            self.author_kind == MessageAuthorKind.PRINCIPAL
            and principal_shape
            and not agent_shape
            or self.author_kind == MessageAuthorKind.AGENT
            and agent_shape
            and not principal_shape
            or self.author_kind == MessageAuthorKind.SYSTEM
            and not principal_shape
            and not agent_shape
        )
        if partial_shape or not valid:
            _error("message_author_binding_invalid")
        return self


class AgentRunRecord(_ConversationModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    thread_id: UUID
    message_sequence: int = Field(ge=1)
    requester_principal_id: UUID
    requester_membership_version: int = Field(ge=1)
    authorization_snapshot_id: UUID
    authorization_snapshot_digest: str = Field(pattern=SHA256_PATTERN)
    provider_binding_id: UUID
    provider_binding_version: int = Field(ge=1)
    provider_binding_digest: str = Field(pattern=SHA256_PATTERN)
    canonical_request_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARACTERS)
    parent_run_id: UUID | None = None
    root_run_id: UUID | None = None
    created_at: datetime

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            _error("run_idempotency_key_blank")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_lineage(self) -> AgentRunRecord:
        if self.parent_run_id == self.id:
            _error("run_parent_cannot_equal_run")
        if self.parent_run_id is not None and self.root_run_id is None:
            _error("run_root_required_for_parent")
        return self


class RunEventRecord(_ConversationModel):
    workspace_id: UUID
    thread_id: UUID
    run_id: UUID
    event: AgentRunEvent

    @model_validator(mode="after")
    def validate_event_binding(self) -> RunEventRecord:
        if self.event.run_id != self.run_id:
            _error("run_event_run_mismatch")
        if self.event.sequence == 1 and self.event.event_type is not AgentRunEventType.STARTED:
            _error("run_event_stream_must_start")
        return self


class RunEventPage(_ConversationModel):
    workspace_id: UUID
    run_id: UUID
    requested_after: int = Field(ge=0)
    next_after: int = Field(ge=0)
    high_watermark: int = Field(ge=0)
    earliest_sequence: int = Field(ge=1)
    events: tuple[RunEventRecord, ...]
    has_more: bool

    @model_validator(mode="after")
    def validate_page(self) -> RunEventPage:
        sequences = [record.event.sequence for record in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            _error("run_event_page_order_invalid")
        if any(
            record.workspace_id != self.workspace_id or record.run_id != self.run_id
            for record in self.events
        ):
            _error("run_event_page_binding_mismatch")
        if sequences and self.next_after != sequences[-1]:
            _error("run_event_page_next_invalid")
        return self


class RunArtifact(_ConversationModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    run_id: UUID
    producing_event_sequence: int = Field(ge=1)
    display_name: str
    media_type: str
    byte_size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    content_digest: str = Field(pattern=SHA256_PATTERN)
    parent_artifact_ids: tuple[UUID, ...] = ()
    lineage_digest: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _safe_leaf_name(value, prefix="artifact")

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        return _media_type(value, prefix="artifact")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def validate_lineage(self) -> RunArtifact:
        if self.id in self.parent_artifact_ids:
            _error("artifact_lineage_self_cycle")
        if len(set(self.parent_artifact_ids)) != len(self.parent_artifact_ids):
            _error("artifact_parent_ids_duplicate")
        expected = compute_lineage_digest(
            workspace_id=self.workspace_id,
            artifact_id=self.id,
            run_id=self.run_id,
            producing_event_sequence=self.producing_event_sequence,
            content_digest=self.content_digest,
            parent_artifact_ids=self.parent_artifact_ids,
        )
        if self.lineage_digest != expected:
            _error("artifact_lineage_digest_mismatch")
        return self
