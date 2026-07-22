from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now_utc() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class AutonomyLevel(IntEnum):
    ADVISE = 0
    OBSERVE = 1
    SANDBOX = 2
    PROPOSE = 3
    APPLY = 4
    DELEGATE = 5


class RunPhase(StrEnum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    BUILD = "build"
    VERIFY = "verify"
    PACKAGE = "package"
    APPROVE = "approve"
    DELIVER = "deliver"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class CriterionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNTESTED = "untested"


class UserRequest(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    text: str = Field(min_length=1)
    project_id: UUID
    created_at: datetime = Field(default_factory=now_utc)


class Session(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    identity_id: str
    autonomy: AutonomyLevel = AutonomyLevel.PROPOSE
    created_at: datetime = Field(default_factory=now_utc)


class Project(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    root: Path
    identity_id: str
    privacy: Literal["local_only", "prefer_local", "allow_cloud"] = "prefer_local"


class Evidence(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    artifact_digest: str
    media_type: str
    description: str
    trace_id: UUID
    created_at: datetime = Field(default_factory=now_utc)


class AcceptanceCriterion(StrictModel):
    id: str
    description: str
    required: bool = True
    status: CriterionStatus = CriterionStatus.UNTESTED
    verification_method: str
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=now_utc)


class PlanStep(StrictModel):
    id: str
    title: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "passed", "failed", "blocked"] = "pending"
    verification_commands: list[list[str]] = Field(default_factory=list)


class ExecutionPlan(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    acceptance_criteria: list[AcceptanceCriterion]
    steps: list[PlanStep]
    risks: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)


class Budget(StrictModel):
    max_cost_usd: Decimal = Decimal("3.00")
    max_runtime_seconds: int = 3600
    max_repair_attempts: int = 5
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class FilesystemPolicy(StrictModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class NetworkPolicy(StrictModel):
    allow_domains: list[str] = Field(default_factory=list)


class SandboxPolicy(StrictModel):
    network_default: bool = False
    cpu_limit: float = 2.0
    memory_mb: int = 4096
    pids_limit: int = 256


class Policy(StrictModel):
    autonomy: AutonomyLevel = AutonomyLevel.PROPOSE
    filesystem: FilesystemPolicy = Field(default_factory=FilesystemPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    confirm: list[str] = Field(default_factory=lambda: ["apply_to_host"])
    budgets: Budget = Field(default_factory=Budget)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)


class PermissionDecision(StrictModel):
    allowed: bool
    action: str
    reason: str
    policy_source: str
    requires_confirmation: bool = False


class ToolDefinition(StrictModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    minimum_autonomy: AutonomyLevel
    idempotent: bool = False


class ToolCall(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    tool: str
    arguments: dict[str, Any]
    idempotency_key: str
    started_at: datetime = Field(default_factory=now_utc)


class ToolResult(StrictModel):
    call_id: UUID
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    finished_at: datetime = Field(default_factory=now_utc)


class ModelProvider(StrictModel):
    name: str
    kind: Literal[
        "openai",
        "openai_compatible",
        "openrouter",
        "anthropic",
        "gemini",
        "ollama",
        "codex_cli",
    ]
    base_url: str = ""
    model: str = ""
    keyring_service: str | None = None
    capabilities: set[str] = Field(default_factory=lambda: {"text"})
    local: bool = False
    executable: Path | None = None
    executable_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    thinking_preset: Literal["fast", "smart", "high", "super_high", "ultra"] | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    thinking_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_transport(self) -> ModelProvider:
        if self.kind == "codex_cli":
            if self.executable is None:
                raise ValueError("Codex CLI providers require an executable path")
            if self.keyring_service is not None:
                raise ValueError("Codex CLI credentials are owned by Codex, not Corvus keyring")
            return self
        if not self.base_url:
            raise ValueError("HTTP providers require a base URL")
        if not self.model:
            raise ValueError("HTTP providers require a model")
        if self.executable is not None:
            raise ValueError("HTTP providers cannot define a Codex executable")
        if self.executable_sha256 is not None:
            raise ValueError("HTTP providers cannot define a Codex executable digest")
        return self


class ModelRoute(StrictModel):
    role: Literal["planner", "coder", "reviewer", "vision", "summarizer"]
    providers: list[str]
    require_local: bool = False
    max_cost_usd: Decimal | None = None


class ModelMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ModelRequest(StrictModel):
    messages: list[ModelMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float = 0.0
    max_output_tokens: int | None = None


class ModelChunk(StrictModel):
    type: Literal["text", "tool_call", "usage", "done", "error"]
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class Sandbox(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    backend: str = "docker"
    container_id: str | None = None
    image: str
    network_enabled: bool = False
    created_at: datetime = Field(default_factory=now_utc)


class Artifact(StrictModel):
    digest: str
    relative_path: str
    media_type: str
    size: int


class VerificationResult(StrictModel):
    criterion_id: str
    status: CriterionStatus
    method: str
    evidence: list[Evidence] = Field(default_factory=list)
    output: str = ""
    duration_seconds: float = 0.0


class DeliveryBundle(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    destination: Path
    artifacts: list[Artifact]
    changed_files: list[str]
    baseline_hashes: dict[str, str | None]
    manifest_digest: str
    created_at: datetime = Field(default_factory=now_utc)


class Approval(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    bundle_id: UUID
    destination: Path
    manifest_digest: str
    approved_files: list[str]
    expires_at: datetime
    approved_at: datetime = Field(default_factory=now_utc)

    @field_validator("expires_at")
    @classmethod
    def expiry_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("approval expiry must be timezone-aware")
        return value


class ApprovalGrant(Approval):
    nonce: str


class DelegationGrant(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    policy_digest: str
    budget: Budget
    expires_at: datetime
    allowed_project_ids: list[UUID]


class Checkpoint(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    delivery_id: UUID
    baseline_hashes: dict[str, str | None]
    backup_digest: str
    git_ref: str | None = None


class RunTrace(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    request_id: UUID
    phase: RunPhase = RunPhase.UNDERSTAND
    previous_event_hash: str = "0" * 64
    created_at: datetime = Field(default_factory=now_utc)


class RunEvent(StrictModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    sequence: int
    event_type: str
    phase: RunPhase
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str
    event_hash: str
    created_at: datetime = Field(default_factory=now_utc)


class MemoryRecord(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    identity_id: str
    kind: Literal["working", "episodic", "semantic", "procedural", "artifact"]
    content: str
    source: str
    confidence: float = Field(ge=0.0, le=1.0)
    pinned: bool = False
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class SkillVersion(StrictModel):
    version: int
    content: str
    permissions: list[str]
    evaluation: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "active", "retired"] = "draft"
    created_at: datetime = Field(default_factory=now_utc)


class Skill(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    versions: list[SkillVersion] = Field(default_factory=list)


class CapabilityMetric(StrictModel):
    subject_type: Literal["model", "provider", "tool", "skill", "task"]
    subject_id: str
    metric: str
    value: float
    sample_count: int
    recorded_at: datetime = Field(default_factory=now_utc)


class ArtifactManifest(StrictModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    artifacts: list[Artifact]
    generated_at: datetime = Field(default_factory=now_utc)
    digest: str = ""
