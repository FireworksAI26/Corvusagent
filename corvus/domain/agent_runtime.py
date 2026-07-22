from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from corvus.security import SecretRedactor, is_sensitive_field_name

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GENESIS_EVENT_DIGEST = "0" * 64
_MAX_AGENT_RUN_INPUT_CHARACTERS = 1_000_000
_MAX_AGENT_RUN_MESSAGE_CHARACTERS = 100_000
_MAX_AGENT_RUN_MESSAGES = 100
_PAYLOAD_SECRET_REDACTOR = SecretRedactor()


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _canonical_datetime(value: datetime) -> str:
    if not _is_timezone_aware(value):
        raise ValueError("canonical_digest_timestamp_must_be_timezone_aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("canonical_digest_decimal_must_be_finite")
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("canonical_digest_decimal_exponent_invalid")
    coefficient = list(digits)
    while coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    sign_prefix = "-" if sign else ""
    coefficient_text = "".join(str(digit) for digit in coefficient)
    if exponent == 0:
        return f"{sign_prefix}{coefficient_text}"
    return f"{sign_prefix}{coefficient_text}E{exponent:+d}"


def canonicalize_digest_value(value: object) -> object:
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, Enum):
        return canonicalize_digest_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): canonicalize_digest_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize_digest_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [canonicalize_digest_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return value


def _compute_canonical_digest(value: object) -> str:
    encoded = json.dumps(
        canonicalize_digest_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _raise_contract_error(error_type: str, reason_code: str) -> None:
    raise PydanticCustomError(
        error_type,
        "reason_code={reason_code}",
        {"reason_code": reason_code},
    )


class ProviderFamily(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"
    CURSOR = "cursor"
    XAI = "xai"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class ProviderTransport(StrEnum):
    LOCAL_CLI = "local_cli"
    HTTP_API = "http_api"


class ProviderStatus(StrEnum):
    AVAILABLE = "available"
    NEEDS_LOGIN = "needs_login"
    UNAVAILABLE = "unavailable"
    PREVIEW = "preview"
    UNHEALTHY = "unhealthy"


class CapabilitySupport(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


def capability_enabled(support: CapabilitySupport) -> bool:
    return support is CapabilitySupport.SUPPORTED


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    text: CapabilitySupport = CapabilitySupport.SUPPORTED
    structured_output: CapabilitySupport = CapabilitySupport.UNVERIFIED
    streaming: CapabilitySupport = CapabilitySupport.UNVERIFIED
    images: CapabilitySupport = CapabilitySupport.UNVERIFIED
    tools: CapabilitySupport = CapabilitySupport.UNVERIFIED
    repository_read: CapabilitySupport = CapabilitySupport.UNVERIFIED
    repository_write: CapabilitySupport = CapabilitySupport.UNVERIFIED
    shell: CapabilitySupport = CapabilitySupport.UNVERIFIED
    mcp: CapabilitySupport = CapabilitySupport.UNVERIFIED
    session_resume: CapabilitySupport = CapabilitySupport.UNVERIFIED
    usage_cost_reporting: CapabilitySupport = CapabilitySupport.UNVERIFIED
    provider_side_budget: CapabilitySupport = CapabilitySupport.UNVERIFIED
    provider_side_cancellation: CapabilitySupport = CapabilitySupport.UNVERIFIED


class ExecutableIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    executable_path: Path
    version: str = Field(min_length=1, max_length=200)
    sha256_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("executable_path")
    @classmethod
    def validate_absolute_executable_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("executable_path_must_be_absolute")
        if value != value.resolve(strict=False):
            raise ValueError("executable_path_must_be_canonical")
        return value


class ProviderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID | None = None
    family: ProviderFamily
    transport: ProviderTransport
    status: ProviderStatus
    executable_identity: ExecutableIdentity | None = None
    credential_ref_id: UUID | None = None
    model: str = Field(min_length=1, max_length=200)
    capabilities: AgentCapabilities
    health_checked_at: datetime
    version: int = Field(ge=1)
    fallback_binding_ids: tuple[UUID, ...] = ()
    data_egress_disclosure: str = Field(min_length=1, max_length=2000)
    server_storage_disclosure: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_transport_identity(self) -> ProviderBinding:
        if self.transport is ProviderTransport.LOCAL_CLI:
            if self.executable_identity is None:
                _raise_contract_error(
                    "invalid_provider_binding_identity",
                    "local_cli_requires_executable_identity",
                )
            if self.credential_ref_id is not None:
                _raise_contract_error(
                    "invalid_provider_binding_identity",
                    "local_cli_forbids_credential_reference",
                )
        else:
            if self.credential_ref_id is None:
                _raise_contract_error(
                    "invalid_provider_binding_identity",
                    "http_api_requires_credential_reference",
                )
            if self.executable_identity is not None:
                _raise_contract_error(
                    "invalid_provider_binding_identity",
                    "http_api_forbids_executable_identity",
                )
        return self

    @model_validator(mode="after")
    def validate_fallbacks(self) -> ProviderBinding:
        if self.id in self.fallback_binding_ids:
            raise ValueError("provider_binding_cannot_fallback_to_self")
        if len(set(self.fallback_binding_ids)) != len(self.fallback_binding_ids):
            raise ValueError("duplicate_fallback_binding_id")
        return self

    @field_validator("health_checked_at")
    @classmethod
    def validate_health_timestamp(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("provider_health_timestamp_must_be_timezone_aware")
        return value


def compute_provider_binding_digest(binding: ProviderBinding) -> str:
    return _compute_canonical_digest(
        binding.model_dump(mode="python", exclude={"health_checked_at", "status"})
    )


class ProviderDiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    project_id: UUID | None = None


class ProviderCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: ProviderBinding
    binding_version: int = Field(ge=1)
    binding_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_binding_receipt(self) -> ProviderCandidate:
        if (
            self.binding_version != self.binding.version
            or self.binding_digest != compute_provider_binding_digest(self.binding)
        ):
            _raise_contract_error(
                "invalid_provider_candidate",
                "provider_binding_digest_mismatch",
            )
        return self


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    binding_id: UUID
    binding_version: int = Field(ge=1)
    binding_digest: str = Field(pattern=_SHA256_PATTERN)
    status: ProviderStatus
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("provider_health_timestamp_must_be_timezone_aware")
        return value


class AutonomyProfile(StrEnum):
    REVIEW_FIRST = "review_first"
    AUTO_WITHIN_LIMITS = "auto_within_limits"
    FULL_AUTO_WHILE_AWAY = "full_auto_while_away"


class AutonomyGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID | None = None
    profile: AutonomyProfile
    allowed_roots: tuple[Path, ...] = Field(min_length=1)
    allowed_effect_classes: frozenset[str]
    denied_effect_classes: frozenset[str]
    allowed_sandbox_profiles: frozenset[str] = Field(min_length=1)
    allowed_tool_ids: frozenset[str]
    allowed_network_destinations: tuple[str, ...]
    credential_grant_ids: tuple[UUID, ...]
    wall_clock_deadline: datetime
    provider_spend_ceiling: Decimal = Field(ge=0)
    corvus_budget_ceiling: Decimal = Field(ge=0)
    max_turns: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    approval_ceiling: int = Field(ge=0)
    always_block_effects: frozenset[str]
    notification_policy: str = Field(min_length=1, max_length=200)
    summary_policy: str = Field(min_length=1, max_length=200)
    issuer_principal_id: UUID
    issued_at: datetime
    expires_at: datetime
    policy_digest: str = Field(pattern=_SHA256_PATTERN)
    revoked_at: datetime | None = None

    @field_serializer(
        "allowed_effect_classes",
        "denied_effect_classes",
        "allowed_sandbox_profiles",
        "allowed_tool_ids",
        "always_block_effects",
        when_used="json",
    )
    def serialize_digest_frozensets(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @field_validator("allowed_roots")
    @classmethod
    def validate_allowed_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        for root in roots:
            if not root.is_absolute():
                _raise_contract_error(
                    "invalid_autonomy_root",
                    "autonomy_root_must_be_absolute",
                )
            if root != root.resolve(strict=False):
                raise ValueError("autonomy_root_must_be_canonical")
        return roots

    @model_validator(mode="after")
    def validate_effect_classes(self) -> AutonomyGrant:
        if self.allowed_effect_classes & self.denied_effect_classes:
            _raise_contract_error(
                "invalid_autonomy_effects",
                "autonomy_effect_classes_overlap",
            )
        if self.allowed_effect_classes & self.always_block_effects:
            _raise_contract_error(
                "invalid_autonomy_effects",
                "always_block_effect_cannot_be_allowed",
            )
        return self

    @model_validator(mode="after")
    def validate_lifetime(self) -> AutonomyGrant:
        timestamps = (self.issued_at, self.expires_at, self.wall_clock_deadline)
        if any(not _is_timezone_aware(value) for value in timestamps):
            _raise_contract_error(
                "naive_autonomy_timestamp",
                "autonomy_timestamp_must_be_timezone_aware",
            )
        if self.revoked_at is not None and not _is_timezone_aware(self.revoked_at):
            _raise_contract_error(
                "naive_autonomy_timestamp",
                "autonomy_timestamp_must_be_timezone_aware",
            )
        if self.expires_at <= self.issued_at:
            _raise_contract_error(
                "invalid_autonomy_lifetime",
                "autonomy_grant_expired_at_issue",
            )
        if self.wall_clock_deadline > self.expires_at:
            _raise_contract_error(
                "invalid_autonomy_lifetime",
                "autonomy_deadline_exceeds_expiry",
            )
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            _raise_contract_error(
                "invalid_autonomy_lifetime",
                "autonomy_revocation_precedes_issue",
            )
        return self


def compute_autonomy_grant_digest(grant: AutonomyGrant) -> str:
    return _compute_canonical_digest(grant.model_dump(mode="python"))


class AgentRunEventType(StrEnum):
    STARTED = "started"
    REASONING_DELTA = "reasoning_delta"
    MESSAGE_DELTA = "message_delta"
    TOOL_REQUESTED = "tool_requested"
    TOOL_BLOCKED = "tool_blocked"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    APPROVAL_REQUIRED = "approval_required"
    CHECKPOINT = "checkpoint"
    ARTIFACT = "artifact"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    workflow_id: UUID | None = None
    work_item_id: UUID | None = None
    provider_binding_id: UUID
    provider_binding_version: int = Field(ge=1)
    provider_binding_digest: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=100)
    messages: tuple[str, ...] | None = Field(default=None, max_length=_MAX_AGENT_RUN_MESSAGES)
    prompt: str | None = Field(default=None, max_length=_MAX_AGENT_RUN_INPUT_CHARACTERS)
    untrusted_context_ref_ids: tuple[UUID, ...] = ()
    authorization_proof_id: UUID
    authorization_proof_digest: str = Field(pattern=_SHA256_PATTERN)
    autonomy_grant_id: UUID
    autonomy_grant_digest: str = Field(pattern=_SHA256_PATTERN)
    credential_grant_ids: tuple[UUID, ...]
    credential_proof_id: UUID | None = None
    credential_proof_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    budget_proof_id: UUID | None = None
    budget_proof_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    kill_switch_proof_id: UUID
    kill_switch_proof_digest: str = Field(pattern=_SHA256_PATTERN)
    sandbox_profile: str = Field(min_length=1, max_length=200)
    filesystem_envelope: tuple[str, ...]
    network_envelope: tuple[str, ...]
    tool_envelope: tuple[str, ...]
    requested_effect_classes: frozenset[str]
    provider_spend_limit: Decimal = Field(ge=0)
    corvus_budget_limit: Decimal = Field(ge=0)
    budget_unit: str = Field(min_length=1, max_length=100)
    budget_requested_amount: int = Field(ge=1)
    approval_limit: int = Field(ge=0)
    max_retries: int = Field(ge=0)
    max_turns: int = Field(ge=1)
    deadline: datetime
    max_output_tokens: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=512)
    resume_handle_id: UUID | None = None

    @field_serializer("requested_effect_classes", when_used="json")
    def serialize_requested_effect_classes(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def immutable_request_digest(self) -> str:
        return compute_agent_run_immutable_digest(self)

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return value
        if any(not message.strip() for message in value):
            raise ValueError("agent_run_message_blank")
        if any(len(message) > _MAX_AGENT_RUN_MESSAGE_CHARACTERS for message in value):
            raise ValueError("agent_run_message_too_long")
        if sum(len(message) for message in value) > _MAX_AGENT_RUN_INPUT_CHARACTERS:
            raise ValueError("agent_run_messages_too_large")
        return value

    @model_validator(mode="after")
    def validate_prompt_shape(self) -> AgentRunRequest:
        has_messages = self.messages is not None and len(self.messages) > 0
        has_prompt = self.prompt is not None and bool(self.prompt.strip())
        if not has_messages and not has_prompt:
            raise ValueError("agent_run_requires_messages_or_prompt")
        if has_messages and has_prompt:
            raise ValueError("agent_run_prompt_shape_ambiguous")
        return self

    @model_validator(mode="after")
    def validate_optional_proof_pairs(self) -> AgentRunRequest:
        proof_pairs = (
            ("credential", self.credential_proof_id, self.credential_proof_digest),
            ("budget", self.budget_proof_id, self.budget_proof_digest),
        )
        for proof_kind, proof_id, proof_digest in proof_pairs:
            if (proof_id is None) != (proof_digest is None):
                _raise_contract_error(
                    f"invalid_agent_run_{proof_kind}_proof",
                    f"agent_run_{proof_kind}_proof_pair_incomplete",
                )
        return self

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent_run_idempotency_key_blank")
        return value

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("agent_run_deadline_must_be_timezone_aware")
        return value


def compute_agent_run_request_digest(request: AgentRunRequest) -> str:
    return _compute_canonical_digest(request.model_dump(mode="python"))


def compute_agent_run_runtime_limit_digest(request: AgentRunRequest) -> str:
    payload = request.model_dump(
        mode="python",
        include={
            "model",
            "effort",
            "provider_spend_limit",
            "corvus_budget_limit",
            "deadline",
            "max_turns",
            "max_retries",
            "approval_limit",
            "max_output_tokens",
            "max_output_bytes",
            "sandbox_profile",
            "filesystem_envelope",
            "network_envelope",
            "tool_envelope",
            "requested_effect_classes",
        },
    )
    return _compute_canonical_digest(payload)


_REFRESHABLE_AGENT_RUN_FIELDS = frozenset(
    {
        "authorization_proof_id",
        "authorization_proof_digest",
        "autonomy_grant_id",
        "autonomy_grant_digest",
        "credential_grant_ids",
        "credential_proof_id",
        "credential_proof_digest",
        "budget_proof_id",
        "budget_proof_digest",
        "kill_switch_proof_id",
        "kill_switch_proof_digest",
        "resume_handle_id",
    }
)


def compute_agent_run_immutable_digest(request: AgentRunRequest) -> str:
    return _compute_canonical_digest(
        request.model_dump(
            mode="python",
            exclude=set(_REFRESHABLE_AGENT_RUN_FIELDS),
            exclude_computed_fields=True,
        )
    )


class AgentRunHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    provider_binding_id: UUID
    created_at: datetime
    provider_session_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    state: AgentRunState

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("agent_run_handle_timestamp_must_be_timezone_aware")
        return value


class AgentRunStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: AgentRunHandle
    replayed: bool


def _contains_secret_payload_key(value: object) -> bool:
    def contains_secret(item: object) -> bool:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                if (
                    is_sensitive_field_name(key_text)
                    or _PAYLOAD_SECRET_REDACTOR.redact(key_text) != key_text
                ):
                    return True
                if contains_secret(child):
                    return True
        elif isinstance(item, (list, tuple)):
            return any(contains_secret(child) for child in item)
        return False

    return contains_secret(value)


def _contains_secret_payload_value(value: object) -> bool:
    def contains_secret(item: object) -> bool:
        if isinstance(item, Mapping):
            return any(contains_secret(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(contains_secret(child) for child in item)
        return bool(_PAYLOAD_SECRET_REDACTOR.redact_value(item) != item)

    return contains_secret(value)


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        frozen = MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
        return cast(JsonValue, frozen)
    if isinstance(value, list):
        return cast(JsonValue, tuple(_freeze_json(item) for item in value))
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def compute_agent_run_event_digest(
    *,
    run_id: UUID,
    handle_id: UUID,
    sequence: int,
    timestamp: datetime,
    event_type: AgentRunEventType,
    redacted_payload: Mapping[str, JsonValue],
    provider_event_id: str | None,
    previous_event_digest: str,
    tool_call_id: str | None = None,
    effect_authorization_decision_id: UUID | None = None,
    effect_authorization_decision_digest: str | None = None,
) -> str:
    return _compute_canonical_digest(
        {
            "event_type": event_type.value,
            "effect_authorization_decision_digest": effect_authorization_decision_digest,
            "effect_authorization_decision_id": (
                str(effect_authorization_decision_id)
                if effect_authorization_decision_id is not None
                else None
            ),
            "handle_id": str(handle_id),
            "previous_event_digest": previous_event_digest,
            "provider_event_id": provider_event_id,
            "tool_call_id": tool_call_id,
            "redacted_payload": _thaw_json(redacted_payload),
            "run_id": str(run_id),
            "sequence": sequence,
            "timestamp": _canonical_datetime(timestamp),
        },
    )


class AgentRunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    run_id: UUID
    handle_id: UUID
    sequence: int = Field(ge=1)
    timestamp: datetime
    event_type: AgentRunEventType
    redacted_payload: Mapping[str, JsonValue]
    provider_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=512)
    effect_authorization_decision_id: UUID | None = None
    effect_authorization_decision_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    previous_event_digest: str = Field(pattern=_SHA256_PATTERN)
    event_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("redacted_payload", mode="after")
    @classmethod
    def freeze_redacted_payload(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        frozen = _freeze_json(dict(value))
        return cast(Mapping[str, JsonValue], frozen)

    @field_serializer("redacted_payload")
    def serialize_redacted_payload(
        self,
        value: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        thawed = _thaw_json(value)
        return cast(dict[str, JsonValue], thawed)

    @model_validator(mode="after")
    def validate_payload_and_digest(self) -> AgentRunEvent:
        if not _is_timezone_aware(self.timestamp):
            raise ValueError("agent_run_event_timestamp_must_be_timezone_aware")
        if _contains_secret_payload_key(self.redacted_payload):
            raise ValueError("agent_run_event_payload_contains_secret_key")
        if _contains_secret_payload_value(self.redacted_payload):
            raise ValueError("agent_run_event_payload_contains_secret_value")
        tool_events = {
            AgentRunEventType.TOOL_REQUESTED,
            AgentRunEventType.TOOL_BLOCKED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_RESULT,
        }
        if self.event_type in tool_events and self.tool_call_id is None:
            raise ValueError("agent_run_tool_call_id_required")
        effect_events = tool_events | {AgentRunEventType.APPROVAL_REQUIRED}
        if self.event_type in effect_events and (
            self.effect_authorization_decision_id is None
            or self.effect_authorization_decision_digest is None
        ):
            raise ValueError("effect_authorization_decision_required")
        if self.event_type not in effect_events and (
            self.effect_authorization_decision_id is not None
            or self.effect_authorization_decision_digest is not None
        ):
            raise ValueError("effect_authorization_decision_unsolicited")
        expected = compute_agent_run_event_digest(
            run_id=self.run_id,
            handle_id=self.handle_id,
            sequence=self.sequence,
            timestamp=self.timestamp,
            event_type=self.event_type,
            redacted_payload=self.redacted_payload,
            provider_event_id=self.provider_event_id,
            previous_event_digest=self.previous_event_digest,
            tool_call_id=self.tool_call_id,
            effect_authorization_decision_id=self.effect_authorization_decision_id,
            effect_authorization_decision_digest=self.effect_authorization_decision_digest,
        )
        if self.event_digest != expected:
            raise ValueError("agent_run_event_digest_mismatch")
        return self


class AgentRunEventChainError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def validate_agent_run_event_chain(events: Sequence[AgentRunEvent]) -> AgentRunState:
    if not events or events[0].event_type is not AgentRunEventType.STARTED:
        raise AgentRunEventChainError("event_stream_requires_started")
    run_id = events[0].run_id
    handle_id = events[0].handle_id
    previous_digest = _GENESIS_EVENT_DIGEST
    provider_event_ids: set[str] = set()
    requested_tools: dict[str, tuple[UUID, str]] = {}
    decision_tool_calls: dict[tuple[UUID, str], str] = {}
    started_tools: set[str] = set()
    finished_tools: set[str] = set()
    state = AgentRunState.RUNNING
    terminal_seen = False
    started_seen = False
    terminal_states = {
        AgentRunEventType.COMPLETED: AgentRunState.COMPLETED,
        AgentRunEventType.FAILED: AgentRunState.FAILED,
        AgentRunEventType.CANCELLED: AgentRunState.CANCELLED,
    }

    for expected_sequence, event in enumerate(events, start=1):
        if terminal_seen:
            raise AgentRunEventChainError("event_after_terminal")
        expected_digest = compute_agent_run_event_digest(
            run_id=event.run_id,
            handle_id=event.handle_id,
            sequence=event.sequence,
            timestamp=event.timestamp,
            event_type=event.event_type,
            redacted_payload=event.redacted_payload,
            provider_event_id=event.provider_event_id,
            previous_event_digest=event.previous_event_digest,
            tool_call_id=event.tool_call_id,
            effect_authorization_decision_id=event.effect_authorization_decision_id,
            effect_authorization_decision_digest=event.effect_authorization_decision_digest,
        )
        if event.event_digest != expected_digest:
            raise AgentRunEventChainError("agent_run_event_digest_mismatch")
        if (
            event.sequence != expected_sequence
            or event.run_id != run_id
            or event.handle_id != handle_id
            or event.previous_event_digest != previous_digest
        ):
            raise AgentRunEventChainError("event_chain_binding_mismatch")
        if event.event_type is AgentRunEventType.STARTED:
            if started_seen:
                raise AgentRunEventChainError("duplicate_started_event")
            started_seen = True
        if event.provider_event_id is not None:
            if event.provider_event_id in provider_event_ids:
                raise AgentRunEventChainError("duplicate_provider_event_id")
            provider_event_ids.add(event.provider_event_id)

        tool_call_id = event.tool_call_id
        if event.event_type is AgentRunEventType.TOOL_REQUESTED:
            if tool_call_id is None or tool_call_id in requested_tools:
                raise AgentRunEventChainError("tool_event_prerequisite_missing")
            decision_reference = _effect_authorization_reference(event)
            prior_tool_call_id = decision_tool_calls.get(decision_reference)
            if prior_tool_call_id is not None and prior_tool_call_id != tool_call_id:
                raise AgentRunEventChainError("tool_effect_authorization_reused")
            requested_tools[tool_call_id] = decision_reference
            decision_tool_calls[decision_reference] = tool_call_id
        elif event.event_type in {
            AgentRunEventType.TOOL_BLOCKED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_RESULT,
        }:
            if (
                tool_call_id is None
                or tool_call_id not in requested_tools
                or tool_call_id in finished_tools
            ):
                raise AgentRunEventChainError("tool_event_prerequisite_missing")
            if _effect_authorization_reference(event) != requested_tools[tool_call_id]:
                raise AgentRunEventChainError("tool_effect_authorization_mismatch")
            if event.event_type is AgentRunEventType.TOOL_STARTED:
                if tool_call_id in started_tools:
                    raise AgentRunEventChainError("tool_event_prerequisite_missing")
                started_tools.add(tool_call_id)
            elif event.event_type is AgentRunEventType.TOOL_BLOCKED:
                if tool_call_id in started_tools:
                    raise AgentRunEventChainError("tool_event_prerequisite_missing")
                finished_tools.add(tool_call_id)
            elif tool_call_id not in started_tools:
                raise AgentRunEventChainError("tool_event_prerequisite_missing")
            else:
                finished_tools.add(tool_call_id)
        elif event.event_type is AgentRunEventType.APPROVAL_REQUIRED and tool_call_id is not None:
            if tool_call_id not in requested_tools or tool_call_id in finished_tools:
                raise AgentRunEventChainError("tool_event_prerequisite_missing")
            if _effect_authorization_reference(event) != requested_tools[tool_call_id]:
                raise AgentRunEventChainError("tool_effect_authorization_mismatch")

        previous_digest = event.event_digest
        terminal_state = terminal_states.get(event.event_type)
        if terminal_state is not None:
            if requested_tools.keys() - finished_tools:
                raise AgentRunEventChainError("terminal_with_unresolved_tool_call")
            state = terminal_state
            terminal_seen = True
    return state


def _effect_authorization_reference(event: AgentRunEvent) -> tuple[UUID, str]:
    decision_id = event.effect_authorization_decision_id
    decision_digest = event.effect_authorization_decision_digest
    if decision_id is None or decision_digest is None:
        raise AgentRunEventChainError("effect_authorization_decision_required")
    return decision_id, decision_digest


class CancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handle_id: UUID
    handle: AgentRunHandle | None = None
    accepted: bool
    terminal: bool
    reason_code: str = Field(min_length=1, max_length=200)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("cancellation_timestamp_must_be_timezone_aware")
        return value

    @model_validator(mode="after")
    def validate_handle_state(self) -> CancellationResult:
        if self.handle is None:
            return self
        if self.handle.id != self.handle_id:
            raise ValueError("cancellation_handle_id_mismatch")
        terminal_states = {
            AgentRunState.CANCELLED,
            AgentRunState.COMPLETED,
            AgentRunState.FAILED,
        }
        if self.terminal != (self.handle.state in terminal_states):
            raise ValueError("cancellation_handle_state_mismatch")
        return self


GENESIS_EVENT_DIGEST = _GENESIS_EVENT_DIGEST
