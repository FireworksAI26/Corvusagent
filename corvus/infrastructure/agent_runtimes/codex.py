from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from corvus.application.ports import AgentRuntimePort
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    AgentRunState,
    CancellationResult,
    CapabilitySupport,
    ExecutableIdentity,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderFamily,
    ProviderHealth,
    ProviderStatus,
    ProviderTransport,
    compute_agent_run_event_digest,
    compute_provider_binding_digest,
)
from corvus.infrastructure.agent_runtimes.process_session import (
    ProcessInvocation,
    ProcessSession,
    ProcessSessionError,
    ProcessSessionEvent,
    ProcessSessionEventKind,
    ProcessSessionLimits,
)
from corvus.safe_process import (
    TrustedProcessError,
    grant_windows_sid_modify,
    grant_windows_sid_read,
    grant_windows_sid_traverse,
    path_is_link_or_reparse,
    windows_current_logon_sid,
    windows_current_user_sid,
    windows_directory_acl_sids,
)
from corvus.security import SecretRedactor, is_sensitive_field_name

_FIRST_EVENT_DIGEST = "0" * 64
_DEFAULT_MODEL_LABEL = "Codex default"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_WINDOWS_ACL_POLL_ATTEMPTS = 1_200
_WINDOWS_ACL_POLL_INTERVAL_SECONDS = 0.05
_WINDOWS_CSIDL_PROFILE = 0x0028
_WINDOWS_PROFILE_BUFFER_LENGTH = 32_768
_WINDOWS_SHGFP_TYPE_CURRENT = 0
_WINDOWS_APP_DATA_DIRECTORY = "AppData"
_WINDOWS_LOCAL_DATA_DIRECTORY = "Local"
_WINDOWS_DESKTOP_DATA_DIRECTORY = "app.corvus.desktop"
_LOCAL_CHAT_SANDBOX_DIRECTORY = ".corvus-local-chat"
_LOCAL_CHAT_CODEX_DIRECTORY = "codex"
_MAX_GIT_POINTER_BYTES = 4_096
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_BUILD_TIMEOUT_SECONDS = 600.0
_WINDOWS_BUILD_EDITING_GUIDANCE = (
    "Native Windows sandbox compatibility: Do not use `apply_patch`, internal file-change "
    "tools, or patch wrappers. Use sandboxed PowerShell commands or project-native tools to "
    "create and edit files. Keep every write inside the current isolated workspace. If an "
    "internal file-change tool is attempted and fails, do not retry it; continue with the "
    "sandboxed command path. "
)
_MAX_STDERR_BYTES = 64_000
_MAX_FRAME_BYTES = 1_000_000
_MAX_FRAMES = 10_000
_MAX_STDIN_BYTES = 8 * 1024 * 1024
_MAX_BUILD_FILES = 128
_MAX_BUILD_FILE_BYTES = 1_000_000
_MAX_BUILD_TOTAL_BYTES = 10_000_000
_MAX_REASONING_SUMMARY_CHARACTERS = 512
_BUILD_EXCLUDED_PARTS = frozenset({".git", "__pycache__", "node_modules"})
_BUILD_SECRET_NAMES = frozenset(
    {
        ".env",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "secrets.json",
    }
)
_BUILD_SECRET_PATHS = frozenset(
    {
        (".aws", "credentials"),
        (".azure", "accesstokens.json"),
        (".config", "gcloud", "application_default_credentials.json"),
        (".config", "gcloud", "credentials.db"),
        (".docker", "config.json"),
        (".kube", "config"),
        (".terraform.d", "credentials.tfrc.json"),
    }
)
_BUILD_SECRET_DIRECTORIES = frozenset({".ssh"})
_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_KNOWN_TOKEN_PATTERN = re.compile(
    rb"(?:"
    rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
    rb"|github_pat_[A-Za-z0-9_]{20,}"
    rb"|gh[pousr]_[A-Za-z0-9]{20,}"
    rb"|xox[baprs]-[A-Za-z0-9-]{20,}"
    rb"|AKIA[0-9A-Z]{16}"
    rb"|AIza[0-9A-Za-z_-]{30,}"
    rb")"
)
_BEARER_TOKEN_PATTERN = re.compile(
    rb"\bBearer\s+[A-Za-z0-9._~+/-]{24,}={0,2}",
    re.IGNORECASE,
)
_ASSIGNED_VALUE_PATTERN = re.compile(
    r"""["']?([A-Za-z][A-Za-z0-9_-]{1,63})["']?\s*([:=])\s*"""
    r"""(?:"([^"\r\n]{4,})"|'([^'\r\n]{4,})'|([A-Za-z0-9_~+/=-]{8,}))""",
    re.IGNORECASE,
)
_TYPE_REFERENCE_PATTERN = re.compile(
    r"^[A-Z][A-Za-z0-9]*(?:Bytes|Model|Ref|Reference|Str|String|Type)$"
)
_PLACEHOLDER_SECRET_PATTERN = re.compile(
    rb"^(?:Bearer\s+|sk-(?:proj-)?|github_pat_|gh[pousr]_|xox[baprs]-)?"
    rb"(?:your|replace|example|placeholder|changeme|dummy|not[-_]a[-_]real)(?:[-_]|$)",
    re.IGNORECASE,
)
_SUMMARY_REDACTOR = SecretRedactor()
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "tool_call",
        "web_search",
    }
)


class CodexAdapterError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class LocalCodexTextRequest:
    run_id: UUID
    prompt: str
    idempotency_key: str
    deadline: datetime
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    mode: Literal["chat", "inspect", "build"] = "chat"
    mcp_enabled: bool = False
    max_output_bytes: int = 100_000
    workspace: Path | None = None
    package_artifact: bool = True


@dataclass(frozen=True, slots=True)
class LocalBuildArtifact:
    path: Path
    download_name: str
    sha256_digest: str
    size_bytes: int
    secret_screening: Literal["passed", "not_scanned"] = "not_scanned"  # noqa: S105


class _ProcessSessionLike(Protocol):
    def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]: ...

    async def cancel(self) -> bool: ...


SessionStarter = Callable[[ProcessInvocation], Awaitable[_ProcessSessionLike]]


async def _start_process_session(invocation: ProcessInvocation) -> _ProcessSessionLike:
    return await ProcessSession.start(invocation)


def _windows_profile_directory() -> Path:
    if sys.platform != "win32":
        return Path.home().resolve(strict=True)
    import ctypes

    profile_buffer = ctypes.create_unicode_buffer(_WINDOWS_PROFILE_BUFFER_LENGTH)
    result = ctypes.windll.shell32.SHGetFolderPathW(
        None,
        _WINDOWS_CSIDL_PROFILE,
        None,
        _WINDOWS_SHGFP_TYPE_CURRENT,
        profile_buffer,
    )
    if result != 0 or not profile_buffer.value:
        raise TrustedProcessError("Windows profile directory is unavailable")
    try:
        return Path(profile_buffer.value).resolve(strict=True)
    except OSError as exc:
        raise TrustedProcessError("Windows profile directory is unavailable") from exc


def _workspace_traverse_boundaries(
    workspace: Path,
    *,
    approved_root: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    resolved_workspace = workspace.resolve(strict=True)
    repository_worktrees = resolved_workspace.parent
    worktrees_root = repository_worktrees.parent
    if worktrees_root.name == ".corvus-worktrees":
        managed_root = worktrees_root.parent
        boundaries = (repository_worktrees, worktrees_root, managed_root)
    else:
        if approved_root is None:
            raise TrustedProcessError("Codex workspace is outside the managed worktree root")
        resolved_approved_root = approved_root.resolve(strict=True)
        local_chat_root = resolved_approved_root.parent
        if (
            resolved_workspace.parent != resolved_approved_root
            or resolved_approved_root.name != _LOCAL_CHAT_CODEX_DIRECTORY
            or local_chat_root.name != _LOCAL_CHAT_SANDBOX_DIRECTORY
        ):
            raise TrustedProcessError("Codex workspace is outside the managed sandbox root")
        managed_root = local_chat_root.parent
        boundaries = (resolved_approved_root, local_chat_root, managed_root)
    resolved_home = (home or _windows_profile_directory()).resolve(strict=True)
    if home is None:
        local_data = managed_root.parent
        app_data = local_data.parent
        if (
            managed_root.name.casefold() == _WINDOWS_DESKTOP_DATA_DIRECTORY.casefold()
            and local_data.name.casefold() == _WINDOWS_LOCAL_DATA_DIRECTORY.casefold()
            and app_data.name.casefold() == _WINDOWS_APP_DATA_DIRECTORY.casefold()
        ):
            resolved_home = app_data.parent.resolve(strict=True)
    if not resolved_workspace.is_relative_to(resolved_home):
        raise TrustedProcessError("Codex workspace is outside the user profile")
    return boundaries


def _linked_git_access_paths(workspace: Path) -> tuple[Path, Path] | None:
    git_pointer = workspace / ".git"
    if git_pointer.is_dir() and not path_is_link_or_reparse(git_pointer):
        return None
    if not git_pointer.exists():
        return None
    if not git_pointer.is_file() or path_is_link_or_reparse(git_pointer):
        raise TrustedProcessError("Codex linked Git pointer is invalid")
    try:
        if git_pointer.stat().st_size > _MAX_GIT_POINTER_BYTES:
            raise TrustedProcessError("Codex linked Git pointer is oversized")
        pointer_text = git_pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise TrustedProcessError("Codex linked Git pointer is unavailable") from exc
    prefix = "gitdir: "
    if not pointer_text.startswith(prefix) or "\n" in pointer_text or "\r" in pointer_text:
        raise TrustedProcessError("Codex linked Git pointer is malformed")
    raw_git_directory = Path(pointer_text.removeprefix(prefix))
    candidate = (
        raw_git_directory if raw_git_directory.is_absolute() else workspace / raw_git_directory
    )
    try:
        git_directory = candidate.resolve(strict=True)
    except OSError as exc:
        raise TrustedProcessError("Codex linked Git directory is unavailable") from exc
    worktrees_directory = git_directory.parent
    common_git_directory = worktrees_directory.parent
    source_repository = common_git_directory.parent
    if (
        not git_directory.is_dir()
        or path_is_link_or_reparse(git_directory)
        or worktrees_directory.name != "worktrees"
        or common_git_directory.name != ".git"
        or not common_git_directory.is_dir()
        or path_is_link_or_reparse(common_git_directory)
        or not source_repository.is_dir()
        or path_is_link_or_reparse(source_repository)
    ):
        raise TrustedProcessError("Codex linked Git directory is untrusted")
    reverse_pointer = git_directory / "gitdir"
    common_pointer = git_directory / "commondir"
    try:
        reverse_text = reverse_pointer.read_text(encoding="utf-8").strip()
        common_text = common_pointer.read_text(encoding="utf-8").strip()
        reverse_candidate = Path(reverse_text)
        if not reverse_candidate.is_absolute():
            reverse_candidate = git_directory / reverse_candidate
        common_candidate = Path(common_text)
        if not common_candidate.is_absolute():
            common_candidate = git_directory / common_candidate
        if (
            reverse_candidate.resolve(strict=True) != git_pointer.resolve(strict=True)
            or common_candidate.resolve(strict=True) != common_git_directory
        ):
            raise TrustedProcessError("Codex linked Git metadata does not match the workspace")
    except (OSError, UnicodeError) as exc:
        raise TrustedProcessError("Codex linked Git metadata is unavailable") from exc
    return source_repository, common_git_directory


async def _grant_windows_workspace_access(
    workspace: Path,
    baseline_sids: frozenset[str],
    linked_git_access_paths: tuple[Path, Path] | None = None,
    *,
    approved_root: Path | None = None,
) -> None:
    sandbox_sids: frozenset[str] = frozenset()
    for _attempt in range(_WINDOWS_ACL_POLL_ATTEMPTS):
        observed_sids = await asyncio.to_thread(windows_directory_acl_sids, workspace)
        sandbox_sids = observed_sids - baseline_sids
        if sandbox_sids:
            break
        await asyncio.sleep(_WINDOWS_ACL_POLL_INTERVAL_SECONDS)
    if not sandbox_sids:
        raise TrustedProcessError("Codex sandbox SID was not applied to the workspace")
    access_tasks = [
        asyncio.to_thread(
            _grant_windows_sids_sequentially,
            ancestor,
            sandbox_sids,
            grant_windows_sid_traverse,
        )
        for ancestor in _workspace_traverse_boundaries(
            workspace,
            approved_root=approved_root,
        )
    ]
    if linked_git_access_paths is not None:
        source_repository, common_git_directory = linked_git_access_paths
        access_tasks.append(
            asyncio.to_thread(
                _grant_windows_sids_sequentially,
                source_repository,
                sandbox_sids,
                grant_windows_sid_traverse,
            )
        )
        access_tasks.append(
            asyncio.to_thread(
                _grant_windows_sids_sequentially,
                common_git_directory,
                sandbox_sids,
                grant_windows_sid_read,
            )
        )
    await asyncio.gather(*access_tasks)


def _grant_windows_sids_sequentially(
    directory: Path,
    sids: frozenset[str],
    grant: Callable[[Path, str], None],
) -> None:
    for sid in sorted(sids):
        grant(directory, sid)


async def _grant_windows_sandbox_preflight(
    workspace: Path,
    *,
    approved_root: Path | None = None,
) -> None:
    logon_sid, user_sid = await asyncio.gather(
        asyncio.to_thread(windows_current_logon_sid),
        asyncio.to_thread(windows_current_user_sid),
    )
    if logon_sid is None or user_sid is None:
        raise TrustedProcessError("Windows sandbox identity is unavailable")
    await asyncio.to_thread(grant_windows_sid_modify, workspace, user_sid)
    for directory in (
        workspace,
        *_workspace_traverse_boundaries(workspace, approved_root=approved_root),
    ):
        await asyncio.to_thread(grant_windows_sid_traverse, directory, logon_sid)


class _RunSession:
    def __init__(
        self,
        process: _ProcessSessionLike,
        *,
        scratch: Path,
        mode: Literal["chat", "inspect", "build"],
        package_artifact: bool,
        baseline_digests: Mapping[str, str] | None = None,
        workspace_access_task: asyncio.Task[None] | None = None,
    ) -> None:
        self.process = process
        self.events: list[AgentRunEvent] = []
        self.lock = asyncio.Lock()
        self.terminal_state: AgentRunState | None = None
        self.process_sequence = 0
        self.stream_complete = False
        self.scratch = scratch
        self.mode = mode
        self.package_artifact = package_artifact
        self.baseline_digests = dict(baseline_digests or {})
        self.workspace_access_task = workspace_access_task
        self.artifact: LocalBuildArtifact | None = None
        self.provider_completed = False


class CodexCliAdapter(AgentRuntimePort):
    """Bounded, text-only adapter for the locally authenticated Codex CLI."""

    def __init__(
        self,
        *,
        executable: Path,
        version: str,
        scratch_root: Path,
        approved_workspace_roots: tuple[Path, ...] = (),
        clock: Callable[[], datetime],
        session_starter: SessionStarter = _start_process_session,
    ) -> None:
        self._executable = executable.resolve(strict=False)
        self._version = version
        self._scratch_root = scratch_root.resolve(strict=False)
        self._approved_workspace_roots = tuple(
            root.resolve(strict=False) for root in approved_workspace_roots
        )
        self._clock = clock
        self._session_starter = session_starter
        self._bindings: dict[UUID, ProviderBinding] = {}
        self._sessions: dict[UUID, _RunSession] = {}
        self._idempotency_handles: dict[str, AgentRunHandle] = {}

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        if not self._executable.is_file():
            return ()
        executable_digest = _sha256_file(self._executable)
        identity = ExecutableIdentity(
            executable_path=self._executable,
            version=self._version,
            sha256_digest=executable_digest,
        )
        binding_id = uuid5(
            NAMESPACE_URL,
            "corvus:codex:"
            f"{query.workspace_id}:{query.project_id or 'workspace'}:{self._executable}:{executable_digest}",
        )
        binding = ProviderBinding(
            id=binding_id,
            workspace_id=query.workspace_id,
            project_id=query.project_id,
            family=ProviderFamily.CODEX,
            transport=ProviderTransport.LOCAL_CLI,
            status=ProviderStatus.AVAILABLE,
            executable_identity=identity,
            model=_DEFAULT_MODEL_LABEL,
            capabilities=_text_only_capabilities(),
            health_checked_at=self._clock(),
            version=1,
            data_egress_disclosure="Prompts are sent through the user's local Codex login.",
            server_storage_disclosure="OpenAI retention follows the user's Codex account policy.",
        )
        self._bindings[binding.id] = binding
        digest = compute_provider_binding_digest(binding)
        return (ProviderCandidate(binding=binding, binding_version=1, binding_digest=digest),)

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        self._require_binding(binding)
        return _text_only_capabilities()

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        self._require_binding(binding)
        status = ProviderStatus.AVAILABLE
        identity = binding.executable_identity
        if identity is None or not self._executable.is_file():
            status = ProviderStatus.UNAVAILABLE
        elif _sha256_file(self._executable) != identity.sha256_digest:
            status = ProviderStatus.UNHEALTHY
        return ProviderHealth(
            binding_id=binding.id,
            binding_version=binding.version,
            binding_digest=compute_provider_binding_digest(binding),
            status=status,
            observed_at=self._clock(),
        )

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        binding = self._binding_for_request(request)
        prompt = request.prompt or "\n".join(request.messages or ())
        return await self._start_text(
            binding=binding,
            run_id=request.run_id,
            prompt=prompt,
            model=request.model,
            effort=_normalize_effort(request.effort),
            mode="chat",
            mcp_enabled=False,
            idempotency_key=request.idempotency_key,
            deadline=request.deadline,
            max_output_bytes=request.max_output_bytes,
            workspace=None,
            package_artifact=True,
        )

    async def start_local_text(
        self,
        binding: ProviderBinding,
        request: LocalCodexTextRequest,
    ) -> AgentRunStartResult:
        self._require_binding(binding)
        return await self._start_text(
            binding=binding,
            run_id=request.run_id,
            prompt=request.prompt,
            model=request.model,
            effort=request.effort,
            mode=request.mode,
            mcp_enabled=request.mcp_enabled,
            idempotency_key=request.idempotency_key,
            deadline=request.deadline,
            max_output_bytes=request.max_output_bytes,
            workspace=request.workspace,
            package_artifact=request.package_artifact,
        )

    async def _start_text(
        self,
        *,
        binding: ProviderBinding,
        run_id: UUID,
        prompt: str,
        model: str | None,
        effort: Literal["low", "medium", "high", "xhigh"],
        mode: Literal["chat", "inspect", "build"],
        mcp_enabled: bool,
        idempotency_key: str,
        deadline: datetime,
        max_output_bytes: int,
        workspace: Path | None,
        package_artifact: bool,
    ) -> AgentRunStartResult:
        existing = self._idempotency_handles.get(idempotency_key)
        if existing is not None:
            return AgentRunStartResult(handle=existing, replayed=True)
        if not prompt:
            raise CodexAdapterError("codex_prompt_required")
        if "\0" in prompt:
            raise CodexAdapterError("codex_prompt_invalid")
        if model is not None and _MODEL_PATTERN.fullmatch(model) is None:
            raise CodexAdapterError("codex_model_invalid")
        approved_workspace_root: Path | None = None
        if workspace is None:
            scratch = self._scratch_root / str(run_id)
            scratch.mkdir(parents=True, exist_ok=False)
        else:
            try:
                scratch = workspace.resolve(strict=True)
                resolved_approved_roots = tuple(
                    root.resolve(strict=True) for root in self._approved_workspace_roots
                )
            except OSError as exc:
                raise CodexAdapterError("codex_workspace_unavailable") from exc
            if not scratch.is_dir() or path_is_link_or_reparse(scratch):
                raise CodexAdapterError("codex_workspace_unavailable")
            approved_workspace_root = next(
                (root for root in resolved_approved_roots if scratch.is_relative_to(root)),
                None,
            )
            if approved_workspace_root is None:
                raise CodexAdapterError("codex_workspace_unapproved")
        sandbox = "workspace-write" if mode == "build" else "read-only"
        arguments = [
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd",
            os.fspath(scratch),
            "--ignore-rules",
        ]
        if not mcp_enabled:
            arguments.append("--ignore-user-config")
            if os.name == "nt":
                # Ignoring config also drops Codex's Windows sandbox backend choice.
                # Restore that runtime selection without loading any user MCP/plugin config.
                arguments.extend(("--config", 'windows.sandbox="unelevated"'))
        # MCP opt-in permits configured MCP servers only. Other user extensions stay disabled.
        arguments.extend(
            (
                "--disable",
                "plugins",
                "--disable",
                "apps",
                "--disable",
                "hooks",
            )
        )
        if model is not None:
            arguments.extend(("--model", model))
        arguments.extend(("--config", f'model_reasoning_effort="{effort}"'))
        effective_prompt = (
            _build_prompt(prompt, windows_shell_edits=os.name == "nt")
            if mode == "build"
            else prompt
        )
        try:
            prompt_bytes = effective_prompt.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CodexAdapterError("codex_prompt_invalid") from exc
        if len(prompt_bytes) > _MAX_STDIN_BYTES:
            raise CodexAdapterError("codex_prompt_too_large")
        arguments.append("-")
        timeout_ceiling = _MAX_BUILD_TIMEOUT_SECONDS if mode == "build" else _MAX_TIMEOUT_SECONDS
        limits = ProcessSessionLimits(
            max_stdin_bytes=_MAX_STDIN_BYTES,
            max_stdout_bytes=max_output_bytes,
            max_stderr_bytes=min(max_output_bytes, _MAX_STDERR_BYTES),
            max_frame_bytes=min(max_output_bytes, _MAX_FRAME_BYTES),
            max_frames=_MAX_FRAMES,
            max_events=_MAX_FRAMES + 1,
            timeout_seconds=min(
                timeout_ceiling,
                max(1.0, (deadline - self._clock()).total_seconds()),
            ),
        )
        invocation = ProcessInvocation(
            executable=self._executable,
            executable_sha256=binding.executable_identity.sha256_digest,  # type: ignore[union-attr]
            arguments=tuple(arguments),
            cwd=scratch,
            approved_roots=(scratch,),
            environment=MappingProxyType({"NO_COLOR": "1"}),
            stdin=prompt_bytes,
            limits=limits,
        )
        prepare_windows_access = (
            os.name == "nt"
            and workspace is not None
            and self._session_starter is _start_process_session
        )
        linked_git_access_paths = (
            _linked_git_access_paths(scratch) if prepare_windows_access else None
        )
        if prepare_windows_access:
            try:
                await _grant_windows_sandbox_preflight(
                    scratch,
                    approved_root=approved_workspace_root,
                )
            except TrustedProcessError as error:
                raise CodexAdapterError("codex_sandbox_preflight_failed") from error
        baseline_sids = (
            windows_directory_acl_sids(scratch) if prepare_windows_access else frozenset()
        )
        baseline_digests = (
            await asyncio.to_thread(_snapshot_workspace, scratch)
            if mode == "build" and package_artifact and workspace is not None
            else {}
        )
        try:
            process = await self._session_starter(invocation)
        except ProcessSessionError as error:
            raise CodexAdapterError("codex_process_unavailable") from error
        workspace_access_task = (
            asyncio.create_task(
                _grant_windows_workspace_access(
                    scratch,
                    baseline_sids,
                    linked_git_access_paths,
                    approved_root=approved_workspace_root,
                )
            )
            if prepare_windows_access
            else None
        )
        handle = AgentRunHandle(
            run_id=run_id,
            provider_binding_id=binding.id,
            created_at=self._clock(),
            state=AgentRunState.RUNNING,
        )
        run_session = _RunSession(
            process,
            scratch=scratch,
            mode=mode,
            package_artifact=package_artifact,
            baseline_digests=baseline_digests,
            workspace_access_task=workspace_access_task,
        )
        self._sessions[handle.id] = run_session
        self._idempotency_handles[idempotency_key] = handle
        return AgentRunStartResult(handle=handle, replayed=False)

    async def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        session = self._require_session(handle)
        async with session.lock:
            for event in session.events:
                if event.sequence > after_sequence:
                    yield event
            if session.stream_complete:
                return
            async for event in self._stream_events(handle, session):
                session.events.append(event)
                if event.sequence > after_sequence:
                    yield event
            session.stream_complete = True

    def artifact(self, handle: AgentRunHandle) -> LocalBuildArtifact | None:
        return self._require_session(handle).artifact

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        del current_kill_switch_proof_id, current_kill_switch_proof_digest
        session = self._require_session(handle)
        if session.terminal_state is not None:
            return CancellationResult(
                handle_id=handle.id,
                handle=handle.model_copy(update={"state": session.terminal_state}),
                accepted=False,
                terminal=True,
                reason_code="agent_run_already_terminal",
                timestamp=self._clock(),
            )
        await session.process.cancel()
        session.terminal_state = AgentRunState.CANCELLED
        return CancellationResult(
            handle_id=handle.id,
            handle=handle.model_copy(update={"state": AgentRunState.CANCELLED}),
            accepted=True,
            terminal=True,
            reason_code="agent_run_cancelled",
            timestamp=self._clock(),
        )

    async def cancel_local(self, handle: AgentRunHandle) -> CancellationResult:
        session = self._require_session(handle)
        if session.terminal_state is not None:
            return CancellationResult(
                handle_id=handle.id,
                handle=handle.model_copy(update={"state": session.terminal_state}),
                accepted=False,
                terminal=True,
                reason_code="agent_run_already_terminal",
                timestamp=self._clock(),
            )
        await session.process.cancel()
        session.terminal_state = AgentRunState.CANCELLED
        return CancellationResult(
            handle_id=handle.id,
            handle=handle.model_copy(update={"state": AgentRunState.CANCELLED}),
            accepted=True,
            terminal=True,
            reason_code="agent_run_cancelled",
            timestamp=self._clock(),
        )

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        del handle, request_with_fresh_proofs
        raise CodexAdapterError("codex_resume_unsupported")

    def _require_binding(self, binding: ProviderBinding) -> ProviderBinding:
        registered = self._bindings.get(binding.id)
        if registered != binding:
            raise CodexAdapterError("codex_binding_unknown")
        return registered

    def _binding_for_request(self, request: AgentRunRequest) -> ProviderBinding:
        binding = self._bindings.get(request.provider_binding_id)
        if (
            binding is None
            or binding.version != request.provider_binding_version
            or compute_provider_binding_digest(binding) != request.provider_binding_digest
        ):
            raise CodexAdapterError("codex_binding_mismatch")
        return binding

    def _require_session(self, handle: AgentRunHandle) -> _RunSession:
        session = self._sessions.get(handle.id)
        if session is None:
            raise CodexAdapterError("codex_handle_unknown")
        return session

    async def _stream_events(
        self,
        handle: AgentRunHandle,
        session: _RunSession,
    ) -> AsyncIterator[AgentRunEvent]:
        terminal = False

        def event(event_type: AgentRunEventType, payload: dict[str, JsonValue]) -> AgentRunEvent:
            previous = session.events[-1].event_digest if session.events else _FIRST_EVENT_DIGEST
            sequence = len(session.events) + 1
            timestamp = self._clock()
            digest = compute_agent_run_event_digest(
                run_id=handle.run_id,
                handle_id=handle.id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=event_type,
                redacted_payload=payload,
                provider_event_id=None,
                previous_event_digest=previous,
                tool_call_id=None,
                effect_authorization_decision_id=None,
                effect_authorization_decision_digest=None,
            )
            return AgentRunEvent(
                run_id=handle.run_id,
                handle_id=handle.id,
                sequence=sequence,
                timestamp=timestamp,
                event_type=event_type,
                redacted_payload=payload,
                previous_event_digest=previous,
                event_digest=digest,
            )

        async for process_event in session.process.events(session.process_sequence):
            session.process_sequence = max(session.process_sequence, process_event.sequence)
            if session.workspace_access_task is not None and session.workspace_access_task.done():
                try:
                    await session.workspace_access_task
                except Exception:  # noqa: BLE001
                    await session.process.cancel()
                    yield event(
                        AgentRunEventType.FAILED,
                        {"reason_code": "codex_workspace_access_unavailable"},
                    )
                    session.terminal_state = AgentRunState.FAILED
                    terminal = True
                    session.workspace_access_task = None
                    continue
                session.workspace_access_task = None
            if terminal:
                continue
            if (
                process_event.kind == ProcessSessionEventKind.FRAME
                and process_event.frame is not None
            ):
                frame_type = process_event.frame.get("type")
                item = process_event.frame.get("item")
                item_type = item.get("type") if isinstance(item, Mapping) else None
                if item_type in _TOOL_ITEM_TYPES and isinstance(item, Mapping):
                    if session.mode == "chat":
                        await session.process.cancel()
                        yield event(
                            AgentRunEventType.FAILED,
                            {"reason_code": "codex_tool_event_blocked"},
                        )
                        session.terminal_state = AgentRunState.FAILED
                        terminal = True
                    else:
                        yield event(
                            AgentRunEventType.CHECKPOINT,
                            {
                                "activity": _safe_activity(item_type),
                                "label": _safe_tool_label(item),
                                "status": "started"
                                if frame_type == "item.started"
                                else "completed",
                                **_safe_tool_identity(item),
                            },
                        )
                elif frame_type == "thread.started":
                    yield event(AgentRunEventType.STARTED, {"status": "started"})
                elif item_type == "reasoning" and isinstance(item, Mapping):
                    summary = item.get("summary")
                    if isinstance(summary, str) and summary:
                        bounded = _SUMMARY_REDACTOR.bound_text(
                            summary,
                            max_characters=_MAX_REASONING_SUMMARY_CHARACTERS,
                        )
                        if bounded.text:
                            yield event(
                                AgentRunEventType.REASONING_DELTA,
                                {"text": bounded.text},
                            )
                    else:
                        yield event(
                            AgentRunEventType.CHECKPOINT,
                            {"activity": "provider", "status": "thinking"},
                        )
                elif item_type == "agent_message" and isinstance(item, Mapping):
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        yield event(AgentRunEventType.MESSAGE_DELTA, {"text": text})
                elif frame_type == "turn.completed":
                    session.provider_completed = True
                    usage = process_event.frame.get("usage")
                    if isinstance(usage, Mapping):
                        safe_usage: dict[str, JsonValue] = {
                            key: value
                            for key, value in usage.items()
                            if key in {"input_tokens", "cached_input_tokens", "output_tokens"}
                            and isinstance(value, int)
                            and not isinstance(value, bool)
                            and value >= 0
                        }
                        yield event(AgentRunEventType.USAGE, safe_usage)
            elif process_event.kind == ProcessSessionEventKind.CANCELLED:
                yield event(AgentRunEventType.CANCELLED, {"reason_code": "agent_run_cancelled"})
                session.terminal_state = AgentRunState.CANCELLED
                terminal = True
            elif process_event.kind == ProcessSessionEventKind.FAILED:
                yield event(
                    AgentRunEventType.FAILED,
                    {"reason_code": process_event.reason_code or "codex_process_failed"},
                )
                session.terminal_state = AgentRunState.FAILED
                terminal = True
            elif process_event.kind == ProcessSessionEventKind.EXITED and not terminal:
                if process_event.return_code != 0:
                    yield event(
                        AgentRunEventType.FAILED,
                        {"reason_code": "codex_process_failed"},
                    )
                    session.terminal_state = AgentRunState.FAILED
                    terminal = True
                    continue
                if not session.provider_completed:
                    yield event(
                        AgentRunEventType.FAILED,
                        {"reason_code": "codex_stream_incomplete"},
                    )
                    session.terminal_state = AgentRunState.FAILED
                    terminal = True
                    continue
                if session.mode == "build" and session.package_artifact:
                    try:
                        session.artifact = _package_workspace(
                            session.scratch,
                            handle.run_id,
                            baseline_digests=session.baseline_digests,
                        )
                    except CodexAdapterError as error:
                        if error.reason_code == "codex_build_empty":
                            yield event(
                                AgentRunEventType.COMPLETED,
                                {
                                    "reason_code": error.reason_code,
                                    "status": "needs_input",
                                },
                            )
                            session.terminal_state = AgentRunState.COMPLETED
                            terminal = True
                            continue
                        yield event(
                            AgentRunEventType.FAILED,
                            {"reason_code": error.reason_code},
                        )
                        session.terminal_state = AgentRunState.FAILED
                        terminal = True
                        continue
                    yield event(
                        AgentRunEventType.ARTIFACT,
                        {
                            "download_name": session.artifact.download_name,
                            "sha256_digest": session.artifact.sha256_digest,
                            "size_bytes": session.artifact.size_bytes,
                        },
                    )
                yield event(AgentRunEventType.COMPLETED, {"status": "completed"})
                session.terminal_state = AgentRunState.COMPLETED
                terminal = True
        if not terminal:
            yield event(AgentRunEventType.FAILED, {"reason_code": "codex_stream_incomplete"})
            session.terminal_state = AgentRunState.FAILED
        if session.workspace_access_task is not None:
            session.workspace_access_task.cancel()
            session.workspace_access_task = None


def _normalize_effort(value: str) -> Literal["low", "medium", "high", "xhigh"]:
    normalized = value.lower()
    if normalized == "normal":
        return "medium"
    if normalized in {"low", "medium", "high", "xhigh"}:
        return normalized  # type: ignore[return-value]
    raise CodexAdapterError("codex_effort_invalid")


def _build_prompt(prompt: str, *, windows_shell_edits: bool = False) -> str:
    editing_guidance = _WINDOWS_BUILD_EDITING_GUIDANCE if windows_shell_edits else ""
    return (
        "Build the complete requested project inside the current isolated workspace. "
        "Work autonomously, create all required files, run appropriate checks, and do not stop at "
        "a plan or partial scaffold. Do not read or modify files outside the current workspace. "
        f"{editing_guidance}"
        "When the project is complete, summarize what was built and the checks you ran.\n\n"
        f"User request:\n{prompt}"
    )


def _safe_activity(item_type: object) -> str:
    mapping = {
        "command_execution": "command",
        "file_change": "files",
        "mcp_tool_call": "mcp",
        "tool_call": "tool",
        "web_search": "search",
    }
    return mapping.get(str(item_type), "tool")


def _safe_tool_label(item: Mapping[str, object]) -> str:
    item_type = item.get("type")
    command = item.get("command")
    if item_type == "command_execution" and isinstance(command, str):
        return _safe_command_label(command)
    mapping = {
        "command_execution": "Run a sandboxed command",
        "file_change": "Update files",
        "mcp_tool_call": "Use MCP tool",
        "tool_call": "Use tool",
        "web_search": "Search the web",
    }
    return mapping.get(str(item_type), "Use tool")


def _safe_command_label(command: str) -> str:
    normalized = " ".join(command.casefold().split())
    if "python" in normalized or "pytest" in normalized:
        if "unittest" in normalized:
            return "Run Python unit tests"
        if "pytest" in normalized or " test" in normalized:
            return "Run Python tests"
        return "Run a Python workspace command"
    if "cargo test" in normalized:
        return "Run Rust tests"
    if any(token in normalized for token in ("pnpm test", "npm test", "yarn test", "bun test")):
        return "Run web tests"
    if any(
        token in normalized
        for token in ("pnpm build", "npm run build", "yarn build", "bun run build")
    ):
        return "Build the web project"
    if "git status" in normalized or "git diff" in normalized:
        return "Inspect project changes"
    if "set-content" in normalized or "out-file" in normalized:
        return "Write project files with PowerShell"
    if "rg " in normalized or "ripgrep" in normalized:
        return "Search project files"
    if "powershell" in normalized or "pwsh" in normalized:
        return "Run a PowerShell workspace command"
    return "Run a sandboxed command"


def _safe_tool_identity(item: Mapping[str, object]) -> dict[str, JsonValue]:
    tool_id = item.get("id")
    if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 200:
        return {}
    return {"tool_id": tool_id}


def _is_sensitive_build_path(relative: Path) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    if relative.name.lower() in _BUILD_SECRET_NAMES:
        return True
    if any(part in _BUILD_SECRET_DIRECTORIES for part in parts):
        return True
    return any(
        len(parts) >= len(secret_path) and parts[-len(secret_path) :] == secret_path
        for secret_path in _BUILD_SECRET_PATHS
    )


def _contains_sensitive_build_content(content: bytes) -> bool:
    if _PRIVATE_KEY_PATTERN.search(content) is not None:
        return True
    if any(
        not _is_placeholder_secret(match.group(0))
        for match in _KNOWN_TOKEN_PATTERN.finditer(content)
    ):
        return True
    if any(
        not _is_placeholder_secret(match.group(0))
        for match in _BEARER_TOKEN_PATTERN.finditer(content)
    ):
        return True
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    for assigned in _ASSIGNED_VALUE_PATTERN.finditer(text):
        key = assigned.group(1)
        separator = assigned.group(2)
        value = next(
            (candidate for candidate in assigned.groups()[2:] if candidate is not None),
            "",
        ).strip()
        unquoted_value = assigned.group(5)
        if (
            is_sensitive_field_name(key)
            and len(value) >= 8
            and not _is_placeholder_secret(value)
            and not value.casefold().startswith(("env://", "keyring://"))
            and not (
                separator == ":"
                and unquoted_value is not None
                and _TYPE_REFERENCE_PATTERN.fullmatch(unquoted_value) is not None
            )
        ):
            return True
    return False


def _is_placeholder_secret(value: bytes | str) -> bool:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return _PLACEHOLDER_SECRET_PATTERN.match(encoded) is not None


def _snapshot_workspace(scratch: Path) -> dict[str, str]:
    root = scratch.resolve(strict=True)
    digests: dict[str, str] = {}
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path_is_link_or_reparse(candidate):
            raise CodexAdapterError("codex_build_link_rejected")
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(root)
        if any(part in _BUILD_EXCLUDED_PARTS for part in relative.parts):
            continue
        canonical = candidate.resolve(strict=True)
        if not canonical.is_relative_to(root):
            raise CodexAdapterError("codex_build_path_escape")
        digests[relative.as_posix()] = _sha256_file(canonical)
    return digests


def _package_workspace(
    scratch: Path,
    run_id: UUID,
    *,
    baseline_digests: Mapping[str, str] | None = None,
) -> LocalBuildArtifact:
    root = scratch.resolve(strict=True)
    baseline = baseline_digests or {}
    files: list[tuple[Path, str, bytes]] = []
    total_bytes = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path_is_link_or_reparse(candidate):
            raise CodexAdapterError("codex_build_link_rejected")
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(root)
        if any(part in _BUILD_EXCLUDED_PARTS for part in relative.parts):
            continue
        name = candidate.name.lower()
        if _is_sensitive_build_path(relative) or (
            name.startswith(".env.") or candidate.suffix.lower() in {".key", ".pem"}
        ):
            raise CodexAdapterError("codex_build_secret_file_rejected")
        canonical = candidate.resolve(strict=True)
        if not canonical.is_relative_to(root):
            raise CodexAdapterError("codex_build_path_escape")
        content = canonical.read_bytes()
        if baseline.get(relative.as_posix()) == hashlib.sha256(content).hexdigest():
            continue
        if len(content) > _MAX_BUILD_FILE_BYTES:
            raise CodexAdapterError("codex_build_file_too_large")
        if _contains_sensitive_build_content(content):
            raise CodexAdapterError("codex_build_secret_file_rejected")
        total_bytes += len(content)
        if total_bytes > _MAX_BUILD_TOTAL_BYTES:
            raise CodexAdapterError("codex_build_too_large")
        files.append((canonical, relative.as_posix(), content))
        if len(files) > _MAX_BUILD_FILES:
            raise CodexAdapterError("codex_build_too_many_files")
    if not files:
        raise CodexAdapterError("codex_build_empty")

    download_name = f"corvus-build-{run_id}.zip"
    archive = root.parent / download_name
    if archive.exists():
        raise CodexAdapterError("codex_build_artifact_conflict")
    manifest_files = [
        {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        for _canonical, relative, content in files
    ]
    manifest = json.dumps(
        {"schema_version": 1, "run_id": str(run_id), "files": manifest_files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with zipfile.ZipFile(archive, mode="x", compression=zipfile.ZIP_DEFLATED) as package:
            for _canonical, archive_path, content in files:
                package.writestr(archive_path, content)
            package.writestr("corvus-manifest.json", manifest)
    except (OSError, zipfile.BadZipFile) as error:
        raise CodexAdapterError("codex_build_package_failed") from error
    return LocalBuildArtifact(
        path=archive,
        download_name=download_name,
        sha256_digest=_sha256_file(archive),
        size_bytes=archive.stat().st_size,
        secret_screening="passed",  # noqa: S106 - typed scan status, not a credential
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_only_capabilities() -> AgentCapabilities:
    unsupported = CapabilitySupport.UNSUPPORTED
    # AgentCapabilities.shell is metadata; this is not a process-spawning call.
    return AgentCapabilities(  # nosec B604
        text=CapabilitySupport.SUPPORTED,
        streaming=CapabilitySupport.SUPPORTED,
        tools=unsupported,
        repository_read=unsupported,
        repository_write=unsupported,
        shell=unsupported,
        mcp=unsupported,
        session_resume=unsupported,
        usage_cost_reporting=CapabilitySupport.SUPPORTED,
        provider_side_budget=unsupported,
        provider_side_cancellation=CapabilitySupport.SUPPORTED,
    )
