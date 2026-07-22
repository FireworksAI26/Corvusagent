import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from corvus.mvp.api_chat import ApiChatBackend, ApiProvider
from corvus.mvp.change_review import ChangeReviewService, ChangeSet
from corvus.mvp.contributions import (
    ContributionConflict,
    ContributionRecord,
    ContributionService,
)
from corvus.mvp.core import CorvusService, DomainConflict, DomainNotFound
from corvus.mvp.deployment import TenantScopedQueries
from corvus.mvp.git_process import GitProcess, GitProcessError
from corvus.mvp.github_cli import GitHubCli, GitHubCliError, GitHubRepository
from corvus.mvp.governance import (
    AutonomyDecision,
    GovernanceService,
    MemoryEntry,
    ProviderConnection,
    RetrievedMemory,
    Routine,
    RoutineRun,
    SkillVersion,
    Team,
    TeamMember,
)
from corvus.mvp.ingress import (
    ChannelEventEnvelope,
    ChannelEventRecord,
    ChannelIngressService,
    LocalEnvelopeSigner,
    OfflineConnectorService,
    OfflineIntentRecord,
)
from corvus.mvp.local_chat import (
    LocalChatConflict,
    LocalChatCursorError,
    LocalChatError,
    LocalChatNotFound,
    LocalChatService,
    build_default_local_chat_service,
    discover_codex_executable,
)
from corvus.mvp.mcp_config import McpConfigError, McpConfigService, McpServer
from corvus.mvp.models import (
    ApprovalRecord,
    ArtifactRecord,
    BudgetAccount,
    ConversationEntry,
    EffectRecord,
    OutcomeContract,
    Project,
    Workflow,
    WorkItem,
    WorkItemDefinition,
)
from corvus.mvp.preferences import (
    LocalPreferences,
    LocalPreferencesConflict,
    LocalPreferencesService,
)
from corvus.mvp.provider_credentials import (
    ProviderCredentialError,
    ProviderCredentialService,
    ProviderCredentialStatus,
    ProviderVerification,
)
from corvus.mvp.repository_workspace import RepositoryRecord, RepositoryWorkspaceService
from corvus.mvp.run_coordinator import (
    CodexWorkspaceBackend,
    RunCoordinator,
    RunCoordinatorConflict,
)
from corvus.mvp.run_models import RunEvent, RunEvidence, RunRecord, RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore, RunStoreConflict, RunStoreNotFound
from corvus.mvp.safety import build_safety_preview
from corvus.mvp.scheduler import LocalScheduler
from corvus.mvp.schedules import (
    ScheduleCreateRequest,
    ScheduleError,
    ScheduleRecord,
    ScheduleStore,
)
from corvus.mvp.secret_scan import SecretScanner
from corvus.mvp.skill_imports import (
    PortableSkillVersion,
    SkillCandidate,
    SkillImportError,
    SkillImportPreview,
    SkillImportService,
)
from corvus.mvp.store import SqliteStore
from corvus.mvp.trusted_cli import TrustedCli, TrustedCliError
from corvus.mvp.worktrees import WorktreeManager, WorktreeOwnershipError
from corvus.platform.api import IdentityApiDependencies, create_platform_router
from corvus.platform.api.dependencies import build_hosted_identity_dependencies_from_env
from corvus.safe_process import path_is_link_or_reparse

_MODEL_LABEL_MAX_ITEMS = 100
_MODEL_LABEL_MAX_LENGTH = 100
_MODEL_LABEL_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+:[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SESSION_COOKIE = "corvus_session"
_SESSION_LIFETIME = timedelta(hours=12)
_INSTANCE_CHALLENGE_HEADER = "X-Corvus-Challenge"
_INSTANCE_PROOF_HEADER = "X-Corvus-Instance-Proof"
_MINIMUM_INSTANCE_CHALLENGE_LENGTH = 16
_MAXIMUM_INSTANCE_CHALLENGE_LENGTH = 512
_GITHUB_CONFIG_DIRECTORY = ".corvus-github-cli"
_MANAGED_PROJECTS_DIRECTORY = "corvus-agent-projects"
_MANAGED_PROJECT_NAME_LIMIT = 64
_WEB_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "font-src 'self' data:; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairRequest(ApiModel):
    token: str = Field(min_length=1)


class PairResponse(ApiModel):
    status: str
    username: str


class ProjectCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)


class RepositoryCreateRequest(ApiModel):
    path: Path
    display_name: str = Field(min_length=1, max_length=200)


class LocalWorktreeResponse(ApiModel):
    run_id: str
    repository_id: str
    base_sha: str
    status: Literal["creating", "active", "discarded"]
    created_at: datetime


class ContributionPrepareRequest(ApiModel):
    selected_paths: tuple[str, ...] = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    draft: Literal[True] = True


class ContributionPublishRequest(ApiModel):
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SkillImportRequest(ApiModel):
    candidate_id: str = Field(min_length=64, max_length=64)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class OutcomeCreateRequest(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)


class WorkflowCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    items: tuple[WorkItemDefinition, ...] = Field(min_length=1)


class BudgetUpdateRequest(ApiModel):
    limit_units: int = Field(ge=0)


class ToggleRequest(ApiModel):
    enabled: bool


class KillSwitchResponse(ApiModel):
    scope_kind: str
    scope_id: str
    enabled: bool


class TeamCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)


class TeamMemberRequest(ApiModel):
    principal_id: str = Field(min_length=1)
    role: Literal["owner", "operator", "viewer"]


class ProviderCreateRequest(ApiModel):
    provider: str = Field(min_length=1, max_length=100)
    credential_ref: str = Field(min_length=1, max_length=500)


class AutonomyEvaluateRequest(ApiModel):
    capability: str = Field(min_length=1, max_length=200)
    requested_execution: bool


class MemoryCreateRequest(ApiModel):
    scope: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=20_000)


class SkillCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)


class RoutineCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    skill_version_id: str = Field(min_length=1)


class EnvelopeActorRequest(ApiModel):
    actor_id: str = Field(min_length=1, max_length=200)
    public_key: str = Field(min_length=1, max_length=1_000)


class ChannelIdentityRequest(ApiModel):
    provider: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=200)
    principal_id: str = Field(min_length=1, max_length=200)


class MutationStatus(ApiModel):
    status: str


class LocalChatContextMessage(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=50_000)


class LocalChatStartRequest(ApiModel):
    prompt: str = Field(min_length=1, max_length=1_000_000)
    context: list[LocalChatContextMessage] = Field(default_factory=list, max_length=20)
    provider: Literal["codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"] = "codex"
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
    )
    effort: Literal["normal", "low", "medium", "high", "xhigh", "max"] = "normal"
    mode: Literal["chat", "build"] = "chat"
    mcp_enabled: bool = False
    safety_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    repository_id: str | None = Field(default=None, min_length=1, max_length=200)


class SafetyPreviewResponse(ApiModel):
    policy_digest: str
    level: Literal["read_only", "protected", "elevated"]
    label: str
    summary: str
    execution: str
    filesystem: str
    network: str
    mcp: str
    approvals: str
    output: str
    requires_confirmation: bool


class LocalChatStartResponse(ApiModel):
    run_id: str
    handle_id: str
    state: Literal["running", "completed", "failed"]
    provider: Literal["codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"]
    model: str
    mode: Literal["chat", "build"]
    storage: Literal["this_device"]
    created_at: str
    working_directory: str
    safety: SafetyPreviewResponse


class SafetyArtifactResponse(ApiModel):
    download_name: str
    sha256_digest: str
    size_bytes: int
    secret_screening: Literal["passed", "not_scanned"]


class SafetyReceiptResponse(ApiModel):
    run_id: str
    status: Literal["completed", "failed", "cancelled"]
    safety: SafetyPreviewResponse
    activities: list[str]
    mcp_used: bool
    approval: str
    original_project_modified: bool
    artifact: SafetyArtifactResponse | None


class LocalChatCancelResponse(ApiModel):
    run_id: str
    state: Literal["running", "cancelled", "completed", "failed"]
    accepted: bool
    reason_code: str | None


class LocalPreferencesResponse(ApiModel):
    version: int = Field(ge=0)
    default_provider: Literal["codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"]
    default_model: str | None
    model_labels: dict[str, str]
    default_effort: Literal["low", "medium", "high", "xhigh", "max"]
    default_mode: Literal["chat", "build"]
    mcp_enabled: bool
    response_tone: Literal["concise", "balanced", "detailed"]
    custom_rules: str
    updated_at: str | None


class McpServerResponse(ApiModel):
    name: str
    enabled: bool
    transport: str
    endpoint: str
    auth_status: str


class McpRemoteCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    url: str = Field(min_length=9, max_length=2048)


class GitHubAuthResponse(ApiModel):
    hostname: str
    authenticated: bool


class GitHubRepositoryResponse(ApiModel):
    name: str
    slug: str
    url: str
    default_branch: str | None
    private: bool


class GitHubRepositoryConnectRequest(ApiModel):
    slug: str = Field(min_length=3, max_length=2048)


class EmptyProjectCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=120)


class LocalPreferencesUpdate(ApiModel):
    expected_version: int = Field(ge=0)
    default_provider: Literal["codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"]
    default_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
    )
    model_labels: dict[str, str] = Field(
        default_factory=dict,
        max_length=_MODEL_LABEL_MAX_ITEMS,
    )
    default_effort: Literal["low", "medium", "high", "xhigh", "max"]
    default_mode: Literal["chat", "build"]
    mcp_enabled: bool
    response_tone: Literal["concise", "balanced", "detailed"]
    custom_rules: str = Field(max_length=20_000)


class ProviderCredentialConnectRequest(ApiModel):
    credential: SecretStr


class ProviderCredentialStatusResponse(ApiModel):
    provider: Literal["openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"]
    configured: bool
    source: Literal["keyring", "environment", "none"]


class ProviderCredentialVerificationResponse(ApiModel):
    provider: Literal["openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"]
    configured: bool
    verified: bool
    models: list[str]


class SessionPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str
    username: str
    tenant_id: str
    csrf_token: str
    expires_at: datetime


class _AuthManager:
    def __init__(
        self,
        *,
        service: CorvusService,
        bootstrap_token: str,
        session_secret: bytes,
        allow_existing_user_pairing: bool,
    ) -> None:
        if len(session_secret) < 32:
            raise ValueError("session_secret_must_be_at_least_32_bytes")
        self.service = service
        self.bootstrap_digest = hashlib.sha256(bootstrap_token.encode("utf-8")).digest()
        self.session_secret = session_secret
        self.allow_existing_user_pairing = allow_existing_user_pairing
        self._bootstrap_lock = Lock()
        self._bootstrap_used = False

    def pair(self, token: str) -> tuple[SessionPrincipal, str]:
        candidate = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate, self.bootstrap_digest):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="pairing_token_invalid"
            )
        with self._bootstrap_lock:
            if self._bootstrap_used:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="pairing_token_consumed",
                )
            self._bootstrap_used = True
            now = datetime.now(UTC)
            with self.service.store.transaction() as connection:
                existing = connection.execute("SELECT * FROM mvp_local_users LIMIT 1").fetchone()
                if existing is not None and not self.allow_existing_user_pairing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="pairing_already_completed",
                    )
                if existing is None:
                    user_id = str(uuid4())
                    tenant_id = "local"
                    username = "local-user"
                    connection.execute(
                        "INSERT INTO mvp_local_users(id, tenant_id, username, paired_at) "
                        "VALUES (?, ?, ?, ?)",
                        (user_id, tenant_id, username, now.isoformat()),
                    )
                else:
                    user_id = existing["id"]
                    tenant_id = existing["tenant_id"]
                    username = existing["username"]
        principal = SessionPrincipal(
            user_id=user_id,
            username=username,
            tenant_id=tenant_id,
            csrf_token=secrets.token_urlsafe(24),
            expires_at=now + _SESSION_LIFETIME,
        )
        return principal, self._encode(principal)

    def authenticate(self, token: str | None) -> SessionPrincipal:
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication_required"
            )
        try:
            payload_encoded, signature_encoded = token.split(".", 1)
            payload = base64.urlsafe_b64decode(_pad_base64(payload_encoded))
            signature = base64.urlsafe_b64decode(_pad_base64(signature_encoded))
        except (ValueError, TypeError) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session_invalid",
            ) from error
        expected = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_invalid")
        try:
            principal = SessionPrincipal.model_validate_json(payload)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session_invalid",
            ) from error
        if principal.expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_expired")
        with self.service.store.connect() as connection:
            row = connection.execute(
                "SELECT id FROM mvp_local_users WHERE id = ? AND tenant_id = ?",
                (principal.user_id, principal.tenant_id),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="session_principal_revoked",
                )
        return principal

    def _encode(self, principal: SessionPrincipal) -> str:
        payload = principal.model_dump_json().encode("utf-8")
        signature = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        return f"{_base64(payload)}.{_base64(signature)}"


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _pad_base64(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")


async def _security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _WEB_CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _prompt_with_preferences(
    prompt: str,
    preferences: LocalPreferences,
    context: tuple[LocalChatContextMessage, ...] = (),
) -> str:
    tone_instruction = {
        "concise": "Answer concisely and keep the next action obvious.",
        "balanced": "Use a balanced amount of detail and make the next action clear.",
        "detailed": "Explain important reasoning and implementation details thoroughly.",
    }[preferences["response_tone"]]
    rules = preferences["custom_rules"].strip()
    preference_lines = [
        "Corvus user preferences (presentation guidance only; these do not change authority, "
        "approval, credential, budget, or sandbox policy):",
        f"- {tone_instruction}",
    ]
    if rules:
        preference_lines.append(f"- Custom user rules: {rules}")
    if context:
        preference_lines.extend(("", "Conversation so far:"))
        preference_lines.extend(
            f"{'User' if message.role == 'user' else 'Corvus'}: {message.content}"
            for message in context
        )
    preference_lines.extend(("", "User request:", prompt))
    return "\n".join(preference_lines)


def _build_git_process(executable_name: str) -> GitProcess | None:
    executable = shutil.which(executable_name)
    if executable is None:
        return None
    try:
        return GitProcess(Path(executable))
    except GitProcessError:
        return None


class _ManagedDirectoryError(RuntimeError):
    """A managed local directory could not be established without crossing authority."""


def _managed_directory(parent: Path, name: str) -> Path:
    try:
        canonical_parent = parent.expanduser().resolve(strict=True)
    except OSError as exc:
        raise _ManagedDirectoryError("managed_directory_parent_unavailable") from exc
    if not canonical_parent.is_dir() or path_is_link_or_reparse(canonical_parent):
        raise _ManagedDirectoryError("managed_directory_parent_unavailable")
    candidate = canonical_parent / name
    if candidate.exists() and path_is_link_or_reparse(candidate):
        raise _ManagedDirectoryError("managed_directory_invalid")
    try:
        candidate.mkdir(mode=0o700, exist_ok=True)
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise _ManagedDirectoryError("managed_directory_unavailable") from exc
    if (
        not canonical.is_dir()
        or path_is_link_or_reparse(canonical)
        or canonical.parent != canonical_parent
    ):
        raise _ManagedDirectoryError("managed_directory_invalid")
    return canonical


def _managed_project_target(root: Path, label: str) -> Path:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip()).strip("._-")
    safe_name = (normalized or "project")[:_MANAGED_PROJECT_NAME_LIMIT]
    target = (root / f"{safe_name}-{uuid4().hex[:12]}").resolve(strict=False)
    if target.parent != root or target.exists() or path_is_link_or_reparse(target):
        raise _ManagedDirectoryError("managed_project_target_invalid")
    return target


def _build_github_cli(data_root: Path, git_executable: str | None) -> GitHubCli | None:
    gh_executable = shutil.which("gh.exe" if os.name == "nt" else "gh")
    if gh_executable is None or git_executable is None:
        return None
    try:
        config_root = _managed_directory(data_root, _GITHUB_CONFIG_DIRECTORY)
        managed_runner = TrustedCli(
            Path(gh_executable),
            environment={"GH_CONFIG_DIR": os.fspath(config_root)},
            additional_path_entries=(Path(git_executable).parent,),
        )
        authorization_runner = TrustedCli(
            Path(gh_executable),
            additional_path_entries=(Path(git_executable).parent,),
        )
        # Explicit sign-in consent promotes the user's host gh identity for this process.
        return GitHubCli(
            managed_runner,
            cwd=data_root,
            authorization_runner=authorization_runner,
        )
    except (GitHubCliError, TrustedCliError, _ManagedDirectoryError):
        return None


def _build_repository_workspace(
    store: SqliteStore,
    git: GitProcess | None,
) -> RepositoryWorkspaceService | None:
    return None if git is None else RepositoryWorkspaceService(store, git)


def create_app(
    *,
    database: Path,
    bootstrap_token: str,
    session_secret: bytes,
    replay_limit: int = 500,
    static_web_dir: Path | None = None,
    allowed_origins: frozenset[str] | None = None,
    allow_existing_user_pairing: bool = False,
    instance_token: str | None = None,
    identity_dependencies: IdentityApiDependencies | None = None,
    local_chat_service: LocalChatService | None = None,
    provider_credentials: ProviderCredentialService | None = None,
    repository_workspace: RepositoryWorkspaceService | None = None,
    worktree_manager: WorktreeManager | None = None,
    contribution_service: ContributionService | None = None,
    run_coordinator: RunCoordinator | None = None,
    skill_import_service: SkillImportService | None = None,
) -> FastAPI:
    if replay_limit < 1:
        raise ValueError("replay_limit_must_be_positive")
    if instance_token is not None and not 16 <= len(instance_token) <= 512:
        raise ValueError("instance_token_length_invalid")
    static_root = _validated_static_root(static_web_dir)
    trusted_origins = (
        allowed_origins
        if allowed_origins is not None
        else frozenset(
            {
                "http://127.0.0.1:8080",
                "http://localhost:8080",
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:4173",
                "http://localhost:4173",
            }
        )
    )
    service = CorvusService.open(database)
    governance = GovernanceService(service.store)
    offline = OfflineConnectorService(
        service.store,
        signer=LocalEnvelopeSigner.generate(actor_id="local-browser-connector"),
    )
    channel = ChannelIngressService(service.store)
    auth = _AuthManager(
        service=service,
        bootstrap_token=bootstrap_token,
        session_secret=session_secret,
        allow_existing_user_pairing=allow_existing_user_pairing,
    )
    local_chat = local_chat_service or build_default_local_chat_service(
        scratch_root=database.parent / ".corvus-local-chat",
        cursor_secret=hmac.new(session_secret, b"local-chat-cursor", hashlib.sha256).digest(),
        store=service.store,
    )
    local_preferences = LocalPreferencesService(service.store)
    mcp_configuration: McpConfigService | None = None
    mcp_executable = discover_codex_executable()
    if mcp_executable is not None:
        try:
            mcp_configuration = McpConfigService(
                TrustedCli(mcp_executable),
                cwd=database.parent,
            )
        except (TrustedCliError, OSError):
            mcp_configuration = None
    credential_service = provider_credentials or ProviderCredentialService()
    git_executable_name = "git.exe" if os.name == "nt" else "git"
    git_executable = shutil.which(git_executable_name)
    git = _build_git_process(git_executable_name)
    change_review = ChangeReviewService(git) if git is not None else None
    repositories = repository_workspace or _build_repository_workspace(service.store, git)
    worktrees = worktree_manager
    if worktrees is None and git is not None:
        worktrees = WorktreeManager(
            service.store,
            git,
            root=database.parent / ".corvus-worktrees",
            ownership_secret=hmac.new(
                session_secret,
                b"worktree-ownership",
                hashlib.sha256,
            ).digest(),
        )
    contributions = contribution_service
    github = _build_github_cli(database.parent, git_executable if git is not None else None)
    skill_imports = skill_import_service or SkillImportService(
        service.store, library_root=database.parent / ".corvus-skills"
    )
    if contributions is None and git is not None and worktrees is not None:
        assert change_review is not None
        if github is not None:
            contributions = ContributionService(
                service.store,
                git,
                worktrees,
                change_review,
                SecretScanner(),
                github,
                confirmation_secret=hmac.new(
                    session_secret,
                    b"contribution-confirmation",
                    hashlib.sha256,
                ).digest(),
            )
    durable_runs = RunStore(service.store)
    schedules = ScheduleStore(service.store)
    run_workflow = run_coordinator
    if (
        run_workflow is None
        and repositories is not None
        and worktrees is not None
        and git is not None
    ):
        assert change_review is not None
        codex_executable = discover_codex_executable()
        if codex_executable is not None:
            from corvus.infrastructure.agent_runtimes.codex import CodexCliAdapter

            managed_root = database.parent / ".corvus-worktrees"
            adapter = CodexCliAdapter(
                executable=codex_executable,
                version="local",
                scratch_root=database.parent / ".corvus-durable-runs",
                approved_workspace_roots=(managed_root,),
                clock=lambda: datetime.now(UTC),
            )
            run_workflow = RunCoordinator(
                durable_runs,
                repositories,
                worktrees,
                change_review,
                CodexWorkspaceBackend(adapter),
                skill_provider=skill_imports,
            )
    scheduler = LocalScheduler(schedules, run_workflow) if run_workflow is not None else None
    scheduler_task: asyncio.Task[None] | None = None

    async def scheduler_loop() -> None:
        while True:
            await asyncio.sleep(15)
            if scheduler is not None:
                try:
                    await scheduler.tick()
                except Exception as error:
                    # Surface diagnostic state while keeping the local API available.
                    app.state.scheduler_error = str(error)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal scheduler_task
        application.state.recovered_skill_imports = skill_imports.recover_interrupted_imports()
        if worktrees is not None:
            application.state.recovered_worktrees = worktrees.recover_interrupted()
        if run_workflow is not None:
            run_workflow.recover_interrupted()
            scheduler_task = asyncio.create_task(scheduler_loop(), name="corvus-local-scheduler")
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
            if local_chat is not None:
                await local_chat.shutdown()

    app = FastAPI(
        title="Corvus Hackathon MVP API",
        version="0.2.0-hackathon",
        lifespan=lifespan,
    )
    app.middleware("http")(_security_headers)

    @app.exception_handler(DomainNotFound)
    async def not_found_handler(_request: Request, error: DomainNotFound) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "not_found", str(error))

    @app.exception_handler(DomainConflict)
    async def conflict_handler(_request: Request, error: DomainConflict) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(error))

    @app.exception_handler(ContributionConflict)
    async def contribution_conflict_handler(
        _request: Request,
        error: ContributionConflict,
    ) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(error))

    @app.exception_handler(WorktreeOwnershipError)
    async def worktree_error_handler(
        _request: Request,
        error: WorktreeOwnershipError,
    ) -> JSONResponse:
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", str(error))

    @app.exception_handler(RunStoreNotFound)
    async def run_not_found_handler(_request: Request, error: RunStoreNotFound) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "not_found", str(error))

    @app.exception_handler(SkillImportError)
    async def skill_import_error_handler(
        _request: Request, error: SkillImportError
    ) -> JSONResponse:
        code = str(error)
        response_status = (
            status.HTTP_404_NOT_FOUND
            if code in {"skill_candidate_not_found", "skill_not_found"}
            else status.HTTP_409_CONFLICT
        )
        return _error_response(response_status, "skill_import_error", code)

    @app.exception_handler(ScheduleError)
    async def schedule_error_handler(_request: Request, error: ScheduleError) -> JSONResponse:
        code = str(error)
        response_status = (
            status.HTTP_404_NOT_FOUND if code == "schedule_not_found" else status.HTTP_409_CONFLICT
        )
        return _error_response(response_status, "schedule_error", code)

    @app.exception_handler(RunStoreConflict)
    @app.exception_handler(RunCoordinatorConflict)
    async def run_conflict_handler(_request: Request, error: Exception) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(error))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path.startswith("/api/v2/"):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "detail": {
                        "code": "invalid_request",
                        "correlation_id": str(uuid4()),
                    }
                },
            )
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_request",
            "request_validation_failed",
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, error: ValueError) -> JSONResponse:
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_request", str(error))

    def authenticated(request: Request) -> SessionPrincipal:
        return auth.authenticate(request.cookies.get(_SESSION_COOKIE))

    def mutation_authorized(
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> SessionPrincipal:
        if csrf_token is None or not secrets.compare_digest(csrf_token, principal.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_invalid")
        origin = request.headers.get("origin")
        if origin is not None and origin not in trusted_origins:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin_forbidden")
        return principal

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def repository_service() -> RepositoryWorkspaceService:
        if repositories is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="git_unavailable",
            )
        return repositories

    def mcp_configuration_service() -> McpConfigService:
        if mcp_configuration is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_mcp_unavailable",
            )
        return mcp_configuration

    def github_service() -> GitHubCli:
        if github is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="github_cli_unavailable",
            )
        return github

    def worktree_service() -> WorktreeManager:
        if worktrees is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="git_unavailable",
            )
        return worktrees

    def contribution_workflow() -> ContributionService:
        if contributions is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="github_cli_unavailable",
            )
        return contributions

    def change_review_service() -> ChangeReviewService:
        if change_review is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="git_unavailable",
            )
        return change_review

    def optional_durable_run(tenant_id: str, run_id: str) -> RunRecord | None:
        try:
            return durable_runs.get(tenant_id, run_id)
        except RunStoreNotFound:
            return None

    def durable_run_workflow() -> RunCoordinator:
        if run_workflow is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_cli_unavailable",
            )
        return run_workflow

    def local_scheduler() -> LocalScheduler:
        if scheduler is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_cli_unavailable",
            )
        return scheduler

    def authorize_local_run(tenant_id: str, run_id: str) -> None:
        with service.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM mvp_worktree_leases w "
                "JOIN mvp_repositories r ON r.id = w.repository_id "
                "WHERE w.run_id = ? AND r.tenant_id = ?",
                (run_id, tenant_id),
            ).fetchone()
        if row is None:
            raise DomainNotFound("local_run_not_found")

    def local_repository_roots(tenant_id: str) -> tuple[Path, ...]:
        if repositories is None:
            return ()
        return tuple(Path(item.path) for item in repositories.list(tenant_id))

    @app.get("/ready")
    def ready(
        response: Response,
        instance_challenge: Annotated[str | None, Header(alias=_INSTANCE_CHALLENGE_HEADER)] = None,
    ) -> dict[str, str]:
        with service.store.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        if instance_token is not None and instance_challenge is not None:
            if not (
                _MINIMUM_INSTANCE_CHALLENGE_LENGTH
                <= len(instance_challenge)
                <= _MAXIMUM_INSTANCE_CHALLENGE_LENGTH
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="instance_challenge_length_invalid",
                )
            response.headers[_INSTANCE_PROOF_HEADER] = hmac.new(
                instance_token.encode("utf-8"),
                instance_challenge.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return {"status": "ready"}

    @app.post("/api/auth/pair", response_model=PairResponse)
    def pair(body: PairRequest, request: Request, response: Response) -> dict[str, str]:
        principal, token = auth.pair(body.token)
        response.set_cookie(
            _SESSION_COOKIE,
            token,
            max_age=int(_SESSION_LIFETIME.total_seconds()),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return {"status": "paired", "username": principal.username}

    @app.get("/api/auth/session", response_model=SessionPrincipal)
    def session(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        return principal.model_dump(mode="json")

    @app.get("/api/local/repositories", response_model=list[RepositoryRecord])
    def local_repositories(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in repository_service().list(principal.tenant_id, limit=limit, offset=offset)
        ]

    @app.get("/api/local/github/status", response_model=GitHubAuthResponse)
    def github_auth_status(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> object:
        return github_service().auth_status()

    @app.post("/api/local/github/authenticate", response_model=GitHubAuthResponse)
    def github_authenticate(
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> object:
        try:
            return github_service().authenticate()
        except GitHubCliError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error

    @app.get("/api/local/github/repositories", response_model=list[GitHubRepositoryResponse])
    def github_repositories(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> tuple[GitHubRepository, ...]:
        try:
            return github_service().list_repositories(limit=100)
        except GitHubCliError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error

    @app.post("/api/local/github/repositories", response_model=RepositoryRecord)
    def connect_github_repository(
        body: GitHubRepositoryConnectRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> RepositoryRecord:
        try:
            repository_slug = GitHubCli.normalize_repository_reference(body.slug)
        except GitHubCliError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error
        try:
            managed_root = _managed_directory(database.parent, _MANAGED_PROJECTS_DIRECTORY)
            target = _managed_project_target(managed_root, repository_slug.rsplit("/", 1)[-1])
        except _ManagedDirectoryError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error
        try:
            github_service().clone_repository(repository_slug, target)
            return repository_service().register_local(
                principal.tenant_id,
                target,
                repository_slug.rsplit("/", 1)[-1],
            )
        except (GitHubCliError, ValueError) as error:
            if target.parent == managed_root:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error

    @app.post("/api/local/projects", response_model=RepositoryRecord)
    def create_empty_project(
        body: EmptyProjectCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> RepositoryRecord:
        try:
            project_root = _managed_directory(database.parent, _MANAGED_PROJECTS_DIRECTORY)
            target = _managed_project_target(project_root, body.name)
            target.mkdir(mode=0o700)
        except (OSError, _ManagedDirectoryError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="project_directory_unavailable",
            ) from error
        project_git = repository_service().git
        try:
            initialized = project_git.run(target, ("init", "--initial-branch=main", "."))
            committed = project_git.run(
                target,
                (
                    "-c",
                    "user.name=Corvus",
                    "-c",
                    "user.email=corvus@localhost",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Initialize project",
                ),
            )
            if initialized.returncode != 0 or committed.returncode != 0:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="project_initialization_failed",
                )
            return repository_service().register_local(
                principal.tenant_id, target, body.name.strip()
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    @app.get("/api/local/skills", response_model=list[PortableSkillVersion])
    def local_skills(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in skill_imports.list(principal.tenant_id, limit=limit, offset=offset)
        ]

    @app.get("/api/local/skills/sources", response_model=list[SkillCandidate])
    def local_skill_sources(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        candidates = skill_imports.discover(local_repository_roots(principal.tenant_id))
        return [item.model_dump(mode="json") for item in candidates]

    @app.get(
        "/api/local/skills/sources/{candidate_id}/preview",
        response_model=SkillImportPreview,
    )
    def preview_local_skill(
        candidate_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        preview = skill_imports.preview(
            principal.tenant_id,
            candidate_id,
            local_repository_roots(principal.tenant_id),
        )
        return preview.model_dump(mode="json")

    @app.post(
        "/api/local/skills/import",
        response_model=PortableSkillVersion,
        status_code=status.HTTP_201_CREATED,
    )
    def import_local_skill(
        body: SkillImportRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = skill_imports.import_draft(
            principal.tenant_id,
            body.candidate_id,
            body.expected_digest,
            local_repository_roots(principal.tenant_id),
        )
        return record.model_dump(mode="json")

    @app.post("/api/local/skills/{skill_id}/activate", response_model=PortableSkillVersion)
    def activate_local_skill(
        skill_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return skill_imports.activate(principal.tenant_id, skill_id).model_dump(mode="json")

    @app.post("/api/local/skills/{skill_id}/archive", response_model=PortableSkillVersion)
    def archive_local_skill(
        skill_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return skill_imports.archive(principal.tenant_id, skill_id).model_dump(mode="json")

    @app.get("/api/local/schedules", response_model=list[ScheduleRecord])
    def local_schedules(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in schedules.list(principal.tenant_id, limit=limit, offset=offset)
        ]

    @app.post(
        "/api/local/schedules",
        response_model=ScheduleRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_local_schedule(
        body: ScheduleCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return schedules.create(principal.tenant_id, body).model_dump(mode="json")

    @app.post("/api/local/schedules/{schedule_id}/run-now", response_model=RunRecord)
    async def run_local_schedule_now(
        schedule_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = await local_scheduler().run_now(principal.tenant_id, schedule_id)
        return record.model_dump(mode="json")

    @app.post("/api/local/schedules/{schedule_id}/pause", response_model=ScheduleRecord)
    def pause_local_schedule(
        schedule_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return schedules.set_status(principal.tenant_id, schedule_id, "paused").model_dump(
            mode="json"
        )

    @app.post("/api/local/schedules/{schedule_id}/resume", response_model=ScheduleRecord)
    def resume_local_schedule(
        schedule_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return schedules.set_status(principal.tenant_id, schedule_id, "active").model_dump(
            mode="json"
        )

    @app.post("/api/local/schedules/{schedule_id}/archive", response_model=ScheduleRecord)
    def archive_local_schedule(
        schedule_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        return schedules.set_status(principal.tenant_id, schedule_id, "archived").model_dump(
            mode="json"
        )

    @app.post(
        "/api/local/repositories",
        response_model=RepositoryRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def register_local_repository(
        body: RepositoryCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = repository_service().register_local(
            principal.tenant_id,
            body.path,
            body.display_name,
        )
        return record.model_dump(mode="json")

    @app.post(
        "/api/local/repositories/{repository_id}/refresh",
        response_model=RepositoryRecord,
    )
    def refresh_local_repository(
        repository_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = repository_service().refresh(principal.tenant_id, repository_id)
        return record.model_dump(mode="json")

    @app.delete(
        "/api/local/repositories/{repository_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def remove_local_repository(
        repository_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> Response:
        repository_service().remove(principal.tenant_id, repository_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/local/repositories/{repository_id}/worktrees",
        response_model=LocalWorktreeResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_local_worktree(
        repository_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        repository = repository_service().refresh(principal.tenant_id, repository_id)
        lease = worktree_service().create(
            repository,
            str(uuid4()),
            repository.snapshot.head_sha,
        )
        return {
            "run_id": lease.run_id,
            "repository_id": lease.repository_id,
            "base_sha": lease.base_sha,
            "status": lease.status,
            "created_at": lease.created_at,
        }

    @app.delete(
        "/api/local/worktrees/{run_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def discard_local_worktree(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> Response:
        authorize_local_run(principal.tenant_id, run_id)
        if optional_durable_run(principal.tenant_id, run_id) is not None:
            raise DomainConflict("worktree_managed_by_durable_run")
        lease = worktree_service().get(run_id)
        worktree_service().discard(lease, run_terminal=True)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/local/runs", response_model=list[RunRecord])
    def local_runs(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in durable_runs.list(principal.tenant_id, limit=limit, offset=offset)
        ]

    @app.post(
        "/api/local/runs",
        response_model=RunRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_local_run(
        body: StartRunRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = await durable_run_workflow().start(principal.tenant_id, body)
        return record.model_dump(mode="json")

    @app.get("/api/local/runs/{run_id}", response_model=RunRecord)
    def local_run(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        return durable_runs.get(principal.tenant_id, run_id).model_dump(mode="json")

    @app.get("/api/local/runs/{run_id}/events", response_model=list[RunEvent])
    def local_run_events(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        after: int = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in durable_runs.events(principal.tenant_id, run_id, after=after, limit=limit)
        ]

    @app.get("/api/local/runs/{run_id}/evidence", response_model=list[RunEvidence])
    def local_run_evidence(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in durable_runs.evidence(
                principal.tenant_id, run_id, limit=limit, offset=offset
            )
        ]

    @app.post("/api/local/runs/{run_id}/cancel", response_model=RunRecord)
    async def cancel_local_run(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = await durable_run_workflow().cancel(principal.tenant_id, run_id)
        return record.model_dump(mode="json")

    @app.post(
        "/api/local/runs/{run_id}/retry",
        response_model=RunRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def retry_local_run(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = await durable_run_workflow().retry(principal.tenant_id, run_id)
        return record.model_dump(mode="json")

    @app.post("/api/local/runs/{run_id}/discard", response_model=RunRecord)
    def discard_local_run(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        record = durable_run_workflow().discard(principal.tenant_id, run_id)
        return record.model_dump(mode="json")

    @app.get("/api/local/runs/{run_id}/changes", response_model=ChangeSet)
    def local_run_changes(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        authorize_local_run(principal.tenant_id, run_id)
        lease = worktree_service().get(run_id)
        return change_review_service().snapshot(lease.root).model_dump(mode="json")

    @app.get(
        "/api/local/runs/{run_id}/contribution",
        response_model=ContributionRecord,
    )
    def local_run_contribution(
        run_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        authorize_local_run(principal.tenant_id, run_id)
        return contribution_workflow().get(run_id).model_dump(mode="json")

    @app.post(
        "/api/local/runs/{run_id}/contribution/prepare",
        response_model=ContributionRecord,
    )
    def prepare_local_contribution(
        run_id: str,
        body: ContributionPrepareRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        authorize_local_run(principal.tenant_id, run_id)
        run = optional_durable_run(principal.tenant_id, run_id)
        if run is not None and run.status not in {
            RunStatus.REVIEW_REQUIRED,
            RunStatus.CONTRIBUTION_READY,
        }:
            raise ContributionConflict("contribution_run_not_reviewable")
        record = contribution_workflow().prepare(
            run_id,
            selected_paths=body.selected_paths,
            message=body.message,
            title=body.title,
            body=body.body,
            draft=body.draft,
        )
        if run is not None and run.status == RunStatus.REVIEW_REQUIRED:
            durable_runs.transition(
                principal.tenant_id,
                run_id,
                RunStatus.CONTRIBUTION_READY,
            )
        return record.model_dump(mode="json")

    @app.post(
        "/api/local/runs/{run_id}/contribution/publish",
        response_model=ContributionRecord,
    )
    def publish_local_contribution(
        run_id: str,
        body: ContributionPublishRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        authorize_local_run(principal.tenant_id, run_id)
        run = optional_durable_run(principal.tenant_id, run_id)
        if run is not None and run.status not in {
            RunStatus.CONTRIBUTION_READY,
            RunStatus.PUBLISHING,
            RunStatus.PUBLISHED,
        }:
            raise ContributionConflict("contribution_run_not_publishable")
        publishable_run = run is not None and run.status in {
            RunStatus.CONTRIBUTION_READY,
            RunStatus.PUBLISHING,
        }
        if run is not None and run.status == RunStatus.CONTRIBUTION_READY:
            durable_runs.transition(principal.tenant_id, run_id, RunStatus.PUBLISHING)
        try:
            record = contribution_workflow().publish(
                run_id,
                expected_digest=body.expected_digest,
            )
        except Exception:
            if publishable_run:
                current = durable_runs.get(principal.tenant_id, run_id)
                if current.status == RunStatus.PUBLISHING:
                    durable_runs.transition(
                        principal.tenant_id,
                        run_id,
                        RunStatus.CONTRIBUTION_READY,
                    )
            raise
        if publishable_run:
            current = durable_runs.get(principal.tenant_id, run_id)
            if current.status == RunStatus.PUBLISHING:
                durable_runs.transition(principal.tenant_id, run_id, RunStatus.PUBLISHED)
        return record.model_dump(mode="json")

    @app.get(
        "/api/provider-credentials",
        response_model=list[ProviderCredentialStatusResponse],
    )
    def provider_credential_statuses(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[ProviderCredentialStatus]:
        return [
            credential_service.status(principal.user_id, provider)
            for provider in ("openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek")
        ]

    @app.put(
        "/api/provider-credentials/{provider}",
        response_model=ProviderCredentialStatusResponse,
    )
    def connect_provider_credential(
        provider: Literal["openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"],
        body: ProviderCredentialConnectRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> ProviderCredentialStatus:
        try:
            return credential_service.connect(
                principal.user_id,
                provider,
                body.credential.get_secret_value(),
            )
        except ProviderCredentialError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    @app.post(
        "/api/provider-credentials/{provider}/verify",
        response_model=ProviderCredentialVerificationResponse,
    )
    async def verify_provider_credential(
        provider: Literal["openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"],
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> ProviderVerification:
        try:
            return await credential_service.verify(principal.user_id, provider)
        except ProviderCredentialError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error

    @app.delete(
        "/api/provider-credentials/{provider}",
        response_model=ProviderCredentialStatusResponse,
    )
    def remove_provider_credential(
        provider: Literal["openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"],
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> ProviderCredentialStatus:
        try:
            return credential_service.remove(principal.user_id, provider)
        except ProviderCredentialError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    @app.post(
        "/api/local-chat/runs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=LocalChatStartResponse,
    )
    async def start_local_chat(
        body: LocalChatStartRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ] = None,
    ) -> dict[str, object]:
        if idempotency_key is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="idempotency_key_required",
            )
        if local_chat is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_unavailable",
            )
        try:
            preferences = local_preferences.get(principal.user_id)
            owner = f"{principal.tenant_id}:{principal.user_id}"
            source_directory = None
            if body.repository_id is not None:
                repository = repository_service().get(principal.tenant_id, body.repository_id)
                source_directory = Path(repository.path)
            if body.provider in {"openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"}:
                api_provider = cast(ApiProvider, body.provider)
                credential = credential_service.require(principal.user_id, api_provider)
                local_chat.register_owner_backend(
                    owner,
                    api_provider,
                    ApiChatBackend(
                        provider=api_provider,
                        credential=credential,
                        clock=lambda: datetime.now(UTC),
                    ),
                )
            return await local_chat.start(
                owner=owner,
                prompt=_prompt_with_preferences(body.prompt, preferences, tuple(body.context)),
                provider=body.provider,
                model=body.model,
                effort=body.effort,
                mode=body.mode,
                mcp_enabled=body.mcp_enabled,
                safety_digest=body.safety_digest,
                idempotency_key=idempotency_key,
                idempotency_prompt=json.dumps(
                    {
                        "prompt": body.prompt,
                        "context": [message.model_dump() for message in body.context],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                source_directory=source_directory,
            )
        except ProviderCredentialError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except LocalChatConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=error.reason_code
            ) from error
        except LocalChatError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error.reason_code,
            ) from error

    @app.get("/api/local-chat/providers")
    def local_chat_providers(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, object]]:
        if local_chat is None:
            return []
        local_entries = [
            entry
            for entry in local_chat.provider_catalog()
            if entry["id"] not in {"gemini", "grok"}
        ]
        labels = {
            "openai": "OpenAI API",
            "anthropic": "Anthropic API",
            "gemini": "Gemini API",
            "xai": "Grok by xAI",
            "openrouter": "OpenRouter",
            "together": "Together AI",
            "fireworks": "Fireworks AI",
            "groq": "Groq",
            "mistral": "Mistral AI",
            "deepseek": "DeepSeek",
        }
        for provider in ("openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"):
            provider_models = credential_service.models(principal.user_id, provider)
            configured = credential_service.status(principal.user_id, provider)["configured"]
            ready = configured and bool(provider_models)
            local_entries.append(
                {
                    "id": provider,
                    "label": labels[provider],
                    "runtime": "api",
                    "status": "ready" if ready else "unavailable",
                    "status_label": (
                        "Verified for API chat"
                        if ready
                        else "Connected; verify in Settings"
                        if configured
                        else "Not configured"
                    ),
                    "models": [
                        {"id": model, "label": model, "recommended": index == 0}
                        for index, model in enumerate(provider_models)
                    ],
                    "thinking_levels": (
                        ["low", "medium", "high", "xhigh"]
                        if provider == "openai"
                        else ["medium"]
                    ),
                    "supports_mcp": False,
                }
            )
        return local_entries

    @app.get("/api/local-chat/mcp", response_model=list[McpServerResponse])
    def list_mcp_servers(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> tuple[McpServer, ...]:
        try:
            return mcp_configuration_service().list()
        except McpConfigError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    @app.post("/api/local-chat/mcp", response_model=McpServerResponse)
    def add_mcp_server(
        body: McpRemoteCreateRequest,
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> McpServer:
        try:
            return mcp_configuration_service().add_remote(body.name, body.url)
        except McpConfigError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error

    @app.delete("/api/local-chat/mcp/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_mcp_server(
        name: str,
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> Response:
        try:
            mcp_configuration_service().remove(name)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except McpConfigError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error

    @app.post("/api/local-chat/mcp/{name}/login", status_code=status.HTTP_204_NO_CONTENT)
    def login_mcp_server(
        name: str,
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> Response:
        try:
            mcp_configuration_service().login(name)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except McpConfigError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
            ) from error

    @app.get("/api/local-chat/safety-preview", response_model=SafetyPreviewResponse)
    def local_chat_safety_preview(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
        provider: Literal["codex", "claude", "openai", "anthropic", "gemini", "xai", "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"] = "codex",
        mode: Literal["chat", "build"] = "chat",
        mcp_enabled: bool = False,
    ) -> dict[str, object]:
        try:
            return build_safety_preview(
                provider=provider,
                mode=mode,
                mcp_enabled=mcp_enabled,
            ).as_dict()
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error

    @app.get("/api/local-chat/preferences", response_model=LocalPreferencesResponse)
    def get_local_preferences(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> LocalPreferences:
        return local_preferences.get(principal.user_id)

    @app.put("/api/local-chat/preferences", response_model=LocalPreferencesResponse)
    def update_local_preferences(
        body: LocalPreferencesUpdate,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> LocalPreferences:
        if body.default_provider != "codex" and (body.default_mode != "chat" or body.mcp_enabled):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="provider_mode_unavailable",
            )
        if body.default_provider == "codex" and body.default_effort == "max":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="provider_effort_unavailable",
            )
        if any(
            _MODEL_LABEL_KEY_PATTERN.fullmatch(key) is None
            or len(label) > _MODEL_LABEL_MAX_LENGTH
            or not label.strip()
            for key, label in body.model_labels.items()
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="model_label_invalid",
            )
        try:
            return local_preferences.update(
                user_id=principal.user_id,
                expected_version=body.expected_version,
                default_provider=body.default_provider,
                default_model=body.default_model,
                model_labels={key: label.strip() for key, label in body.model_labels.items()},
                default_effort=body.default_effort,
                default_mode=body.default_mode,
                mcp_enabled=body.mcp_enabled,
                response_tone=body.response_tone,
                custom_rules=body.custom_rules.strip(),
            )
        except LocalPreferencesConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "preferences_version_conflict",
                    "current": error.current,
                },
            ) from error

    @app.get(
        "/api/local-chat/runs/{run_id}/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def local_chat_events(
        run_id: UUID,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        follow: bool = True,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        if local_chat is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_unavailable",
            )
        try:
            events = local_chat.events(
                owner=f"{principal.tenant_id}:{principal.user_id}",
                run_id=run_id,
                cursor=last_event_id,
                follow=follow,
            )
        except LocalChatNotFound as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=error.reason_code
            ) from error
        except LocalChatCursorError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=error.reason_code
            ) from error

        async def stream() -> AsyncIterator[str]:
            async for cursor, event in events:
                payload = {
                    "run_id": str(run_id),
                    "sequence": event.sequence,
                    "timestamp": event.timestamp.isoformat(),
                    "type": event.type,
                    "payload": event.payload,
                }
                yield (
                    f"id: {cursor}\n"
                    f"event: {event.type}\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/local-chat/runs/{run_id}/artifact",
        response_class=FileResponse,
        responses={200: {"content": {"application/zip": {}}}},
    )
    async def local_chat_artifact(
        run_id: UUID,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> FileResponse:
        if local_chat is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_unavailable",
            )
        try:
            artifact = local_chat.artifact(
                owner=f"{principal.tenant_id}:{principal.user_id}",
                run_id=run_id,
            )
        except LocalChatNotFound as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error.reason_code,
            ) from error
        return FileResponse(
            artifact.path,
            media_type="application/zip",
            filename=artifact.download_name,
        )

    @app.get(
        "/api/local-chat/runs/{run_id}/safety-receipt",
        response_model=SafetyReceiptResponse,
    )
    def local_chat_safety_receipt(
        run_id: UUID,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, object]:
        if local_chat is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_unavailable",
            )
        try:
            return local_chat.safety_receipt(
                owner=f"{principal.tenant_id}:{principal.user_id}",
                run_id=run_id,
            )
        except LocalChatNotFound as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error.reason_code,
            ) from error
        except LocalChatConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.reason_code,
            ) from error

    @app.post(
        "/api/local-chat/runs/{run_id}/cancel",
        response_model=LocalChatCancelResponse,
    )
    async def cancel_local_chat(
        run_id: UUID,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, object]:
        if local_chat is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="codex_unavailable",
            )
        try:
            return await local_chat.cancel(
                owner=f"{principal.tenant_id}:{principal.user_id}",
                run_id=run_id,
            )
        except LocalChatNotFound as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=error.reason_code
            ) from error

    @app.get("/api/projects", response_model=list[Project])
    def projects(
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        queries = TenantScopedQueries(service.store)
        return [item.model_dump(mode="json") for item in queries.list_projects(principal.tenant_id)]

    @app.post(
        "/api/projects",
        status_code=status.HTTP_201_CREATED,
        response_model=Project,
    )
    def create_project(
        body: ProjectCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        project = service.create_project(name=body.name, tenant_id=principal.tenant_id)
        return project.model_dump(mode="json")

    @app.post(
        "/api/projects/{project_id}/outcomes",
        status_code=status.HTTP_201_CREATED,
        response_model=OutcomeContract,
    )
    def create_outcome(
        project_id: str,
        body: OutcomeCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        outcome = service.create_outcome(
            project_id=project_id,
            title=body.title,
            acceptance_criteria=body.acceptance_criteria,
        )
        return outcome.model_dump(mode="json")

    @app.get(
        "/api/projects/{project_id}/outcomes",
        response_model=list[OutcomeContract],
    )
    def outcomes(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [item.model_dump(mode="json") for item in service.list_outcomes(project_id)]

    @app.post(
        "/api/outcomes/{outcome_id}/workflows",
        status_code=status.HTTP_201_CREATED,
        response_model=Workflow,
    )
    def create_workflow(
        outcome_id: str,
        body: WorkflowCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_outcome_tenant(service, outcome_id, principal.tenant_id)
        workflow = service.create_workflow(
            outcome_id=outcome_id,
            name=body.name,
            items=body.items,
        )
        return workflow.model_dump(mode="json")

    @app.get(
        "/api/outcomes/{outcome_id}/workflows",
        response_model=list[Workflow],
    )
    def workflows(
        outcome_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        _require_outcome_tenant(service, outcome_id, principal.tenant_id)
        return [item.model_dump(mode="json") for item in service.list_workflows(outcome_id)]

    @app.get("/api/workflows/{workflow_id}", response_model=Workflow)
    def workflow(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.get_workflow(workflow_id).model_dump(mode="json")

    @app.get(
        "/api/workflows/{workflow_id}/work-items",
        response_model=list[WorkItem],
    )
    def work_items(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return [item.model_dump(mode="json") for item in service.list_work_items(workflow_id)]

    @app.post("/api/workflows/{workflow_id}/start", response_model=Workflow)
    def start_workflow(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.start_workflow(workflow_id).model_dump(mode="json")

    @app.post("/api/workflows/{workflow_id}/pause", response_model=Workflow)
    def pause_workflow(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.pause_workflow(workflow_id).model_dump(mode="json")

    @app.post("/api/workflows/{workflow_id}/resume", response_model=Workflow)
    def resume_workflow(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.start_workflow(workflow_id).model_dump(mode="json")

    @app.post("/api/workflows/{workflow_id}/cancel", response_model=Workflow)
    def cancel_workflow(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.cancel_workflow(workflow_id).model_dump(mode="json")

    @app.post("/api/workflows/{workflow_id}/run-next", response_model=WorkItem)
    def run_next(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        item = service.run_next(workflow_id, worker_id=f"api:{principal.user_id}")
        if item is None:
            raise DomainConflict("no_ready_work_item")
        return item.model_dump(mode="json")

    @app.post(
        "/api/workflows/{workflow_id}/work-items/{item_key}/retry",
        response_model=WorkItem,
    )
    def retry_work_item(
        workflow_id: str,
        item_key: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return service.retry_work_item(workflow_id, item_key).model_dump(mode="json")

    @app.get(
        "/api/workflows/{workflow_id}/effects",
        response_model=list[EffectRecord],
    )
    def effects(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return [effect.model_dump(mode="json") for effect in service.list_effects(workflow_id)]

    @app.post("/api/effects/{effect_id}/approve", response_model=ApprovalRecord)
    def approve_effect(
        effect_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_effect_tenant(service, effect_id, principal.tenant_id)
        return service.approve_effect(effect_id, actor_id=principal.user_id).model_dump(mode="json")

    @app.post("/api/effects/{effect_id}/reject", response_model=ApprovalRecord)
    def reject_effect(
        effect_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_effect_tenant(service, effect_id, principal.tenant_id)
        return service.reject_effect(effect_id, actor_id=principal.user_id).model_dump(mode="json")

    @app.put(
        "/api/workflows/{workflow_id}/kill-switch",
        response_model=KillSwitchResponse,
    )
    def set_workflow_kill_switch(
        workflow_id: str,
        body: ToggleRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        service.set_kill_switch(scope_kind="workflow", scope_id=workflow_id, enabled=body.enabled)
        return {"scope_kind": "workflow", "scope_id": workflow_id, "enabled": body.enabled}

    @app.put("/api/projects/{project_id}/budget", response_model=BudgetAccount)
    def set_budget(
        project_id: str,
        body: BudgetUpdateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return service.set_budget(project_id, limit_units=body.limit_units).model_dump(mode="json")

    @app.get("/api/projects/{project_id}/budget", response_model=BudgetAccount)
    def get_budget(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return service.get_budget(project_id).model_dump(mode="json")

    @app.get(
        "/api/workflows/{workflow_id}/artifacts",
        response_model=list[ArtifactRecord],
    )
    def artifacts(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return [item.model_dump(mode="json") for item in service.list_artifacts(workflow_id)]

    @app.get(
        "/api/workflows/{workflow_id}/conversation",
        response_model=list[ConversationEntry],
    )
    def conversation(
        workflow_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        return [
            item.model_dump(mode="json") for item in service.list_conversation_entries(workflow_id)
        ]

    @app.get("/api/projects/{project_id}/teams", response_model=list[Team])
    def teams(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [item.model_dump(mode="json") for item in governance.list_teams(project_id)]

    @app.post(
        "/api/projects/{project_id}/teams",
        status_code=status.HTTP_201_CREATED,
        response_model=Team,
    )
    def create_team(
        project_id: str,
        body: TeamCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.create_team(
            project_id=project_id,
            name=body.name,
            owner_id=principal.user_id,
        ).model_dump(mode="json")

    @app.get("/api/teams/{team_id}/members", response_model=list[TeamMember])
    def team_members(
        team_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        members = governance.list_team_members(team_id)
        _require_team_tenant(service, governance, team_id, principal.tenant_id)
        return [item.model_dump(mode="json") for item in members]

    @app.post("/api/teams/{team_id}/members", response_model=MutationStatus)
    def add_team_member(
        team_id: str,
        body: TeamMemberRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, str]:
        _require_team_tenant(service, governance, team_id, principal.tenant_id)
        governance.add_member(
            team_id,
            actor_id=principal.user_id,
            principal_id=body.principal_id,
            role=body.role,
        )
        return {"status": "member_added"}

    @app.get(
        "/api/projects/{project_id}/providers",
        response_model=list[ProviderConnection],
    )
    def providers(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [
            item.model_dump(mode="json")
            for item in governance.list_provider_connections(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/providers",
        status_code=status.HTTP_201_CREATED,
        response_model=ProviderConnection,
    )
    def create_provider(
        project_id: str,
        body: ProviderCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.create_provider_connection(
            project_id=project_id,
            provider=body.provider,
            credential_ref=body.credential_ref,
        ).model_dump(mode="json")

    @app.post(
        "/api/projects/{project_id}/autonomy/evaluate",
        response_model=AutonomyDecision,
    )
    def evaluate_autonomy(
        project_id: str,
        body: AutonomyEvaluateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.evaluate_autonomy(
            project_id=project_id,
            principal_id=principal.user_id,
            capability=body.capability,
            requested_execution=body.requested_execution,
        ).model_dump(mode="json")

    @app.get("/api/projects/{project_id}/memories", response_model=list[MemoryEntry])
    def memories(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [item.model_dump(mode="json") for item in governance.list_memory_entries(project_id)]

    @app.post(
        "/api/projects/{project_id}/memories",
        status_code=status.HTTP_201_CREATED,
        response_model=MemoryEntry,
    )
    def store_memory(
        project_id: str,
        body: MemoryCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.store_memory(
            project_id=project_id,
            scope=body.scope,
            content=body.content,
            provenance=f"user:{principal.user_id}",
        ).model_dump(mode="json")

    @app.get(
        "/api/projects/{project_id}/memories/retrieve",
        response_model=list[RetrievedMemory],
    )
    def retrieve_memory(
        project_id: str,
        query: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [
            item.model_dump(mode="json")
            for item in governance.retrieve_memory(project_id=project_id, query=query)
        ]

    @app.get("/api/projects/{project_id}/skills", response_model=list[SkillVersion])
    def skills(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [item.model_dump(mode="json") for item in governance.list_skills(project_id)]

    @app.post(
        "/api/projects/{project_id}/skills",
        status_code=status.HTTP_201_CREATED,
        response_model=SkillVersion,
    )
    def create_skill(
        project_id: str,
        body: SkillCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.create_skill(
            project_id=project_id,
            name=body.name,
            content=body.content,
        ).model_dump(mode="json")

    @app.post("/api/skills/{skill_id}/activate", response_model=SkillVersion)
    def activate_skill(
        skill_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        skill = governance.get_skill(skill_id)
        TenantScopedQueries(service.store).get_project(principal.tenant_id, skill.project_id)
        return governance.activate_skill(skill_id).model_dump(mode="json")

    @app.get("/api/projects/{project_id}/routines", response_model=list[Routine])
    def routines(
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return [item.model_dump(mode="json") for item in governance.list_routines(project_id)]

    @app.post(
        "/api/projects/{project_id}/routines",
        status_code=status.HTTP_201_CREATED,
        response_model=Routine,
    )
    def create_routine(
        project_id: str,
        body: RoutineCreateRequest,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        TenantScopedQueries(service.store).get_project(principal.tenant_id, project_id)
        return governance.create_routine(
            project_id=project_id,
            name=body.name,
            skill_version_id=body.skill_version_id,
        ).model_dump(mode="json")

    @app.post("/api/routines/{routine_id}/run", response_model=RoutineRun)
    def run_routine(
        routine_id: str,
        principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, Any]:
        routine = governance.get_routine(routine_id)
        TenantScopedQueries(service.store).get_project(principal.tenant_id, routine.project_id)
        return governance.run_routine(routine_id, actor_id=principal.user_id).model_dump(
            mode="json"
        )

    @app.get("/api/offline-intents", response_model=list[OfflineIntentRecord])
    def offline_intents(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in offline.list_intents()]

    @app.post("/api/channel/actors", response_model=MutationStatus)
    def register_channel_actor(
        body: EnvelopeActorRequest,
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, str]:
        channel.register_actor(body.actor_id, body.public_key)
        return {"status": "actor_registered"}

    @app.post("/api/channel/identities", response_model=MutationStatus)
    def map_channel_identity(
        body: ChannelIdentityRequest,
        _principal: Annotated[SessionPrincipal, Depends(mutation_authorized)],
    ) -> dict[str, str]:
        channel.map_identity(
            provider=body.provider,
            external_id=body.external_id,
            principal_id=body.principal_id,
        )
        return {"status": "identity_mapped"}

    @app.post("/api/channel/events", response_model=ChannelEventRecord)
    def ingest_channel_event(body: ChannelEventEnvelope) -> dict[str, Any]:
        return channel.ingest(body).model_dump(mode="json")

    @app.get("/api/channel/events", response_model=list[ChannelEventRecord])
    def channel_events(
        _principal: Annotated[SessionPrincipal, Depends(authenticated)],
    ) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in channel.list_events()]

    @app.get("/api/workflows/{workflow_id}/events")
    async def events(
        workflow_id: str,
        request: Request,
        principal: Annotated[SessionPrincipal, Depends(authenticated)],
        follow: bool = True,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        _require_workflow_tenant(service, workflow_id, principal.tenant_id)
        cursor = _parse_event_cursor(last_event_id)
        return StreamingResponse(
            _event_stream(
                service,
                workflow_id=workflow_id,
                request=request,
                cursor=cursor,
                follow=follow,
                replay_limit=replay_limit,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    hosted_identity = (
        identity_dependencies
        if identity_dependencies is not None
        else build_hosted_identity_dependencies_from_env()
    )
    app.include_router(create_platform_router(hosted_identity))

    if static_root is not None:
        app.mount("/", StaticFiles(directory=static_root, html=True), name="operator-console")

    return app


def _validated_static_root(static_web_dir: Path | None) -> Path | None:
    if static_web_dir is None:
        return None
    root = static_web_dir.expanduser().resolve()
    index = root / "index.html"
    if not root.is_dir() or not index.is_file() or not index.resolve().is_relative_to(root):
        raise ValueError("static_web_index_missing")
    return root


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _parse_event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        cursor = int(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="last_event_id_invalid") from error
    if cursor < 0:
        raise HTTPException(status_code=400, detail="last_event_id_invalid")
    return cursor


async def _event_stream(
    service: CorvusService,
    *,
    workflow_id: str,
    request: Request,
    cursor: int,
    follow: bool,
    replay_limit: int,
) -> AsyncIterator[str]:
    current = cursor
    while True:
        events = service.list_events(workflow_id, after_id=current)
        if len(events) > replay_limit:
            latest = int(events[-1]["id"])
            yield _sse(
                event="resync_required",
                data={"reason": "cursor_too_old", "latest_event_id": latest},
            )
            return
        for event in events:
            event_id = int(event["id"])
            yield _sse(
                event_id=event_id,
                event=str(event["event_type"]),
                data=event,
            )
            current = event_id
        if not follow or await request.is_disconnected():
            return
        await asyncio.sleep(0.25)


def _sse(
    *,
    event: str,
    data: dict[str, Any],
    event_id: int | None = None,
) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), sort_keys=True)}")
    return "\n".join(lines) + "\n\n"


def _require_outcome_tenant(service: CorvusService, outcome_id: str, tenant_id: str) -> None:
    with service.store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM mvp_outcomes o JOIN mvp_projects p ON p.id = o.project_id "
            "WHERE o.id = ? AND p.tenant_id = ?",
            (outcome_id, tenant_id),
        ).fetchone()
        if row is None:
            raise DomainNotFound("outcome_not_found")


def _require_workflow_tenant(service: CorvusService, workflow_id: str, tenant_id: str) -> None:
    with service.store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM mvp_workflows w JOIN mvp_outcomes o ON o.id = w.outcome_id "
            "JOIN mvp_projects p ON p.id = o.project_id WHERE w.id = ? AND p.tenant_id = ?",
            (workflow_id, tenant_id),
        ).fetchone()
        if row is None:
            raise DomainNotFound("workflow_not_found")


def _require_effect_tenant(service: CorvusService, effect_id: str, tenant_id: str) -> None:
    with service.store.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM mvp_effects e JOIN mvp_workflows w ON w.id = e.workflow_id "
            "JOIN mvp_outcomes o ON o.id = w.outcome_id "
            "JOIN mvp_projects p ON p.id = o.project_id WHERE e.id = ? AND p.tenant_id = ?",
            (effect_id, tenant_id),
        ).fetchone()
        if row is None:
            raise DomainNotFound("effect_not_found")


def _require_team_tenant(
    service: CorvusService,
    governance: GovernanceService,
    team_id: str,
    tenant_id: str,
) -> None:
    team = governance.get_team(team_id)
    TenantScopedQueries(service.store).get_project(tenant_id, team.project_id)
