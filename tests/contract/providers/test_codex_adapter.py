from __future__ import annotations

import asyncio
import os
import threading
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from corvus.domain.agent_runtime import (
    AgentRunEventType,
    AgentRunRequest,
    AgentRunState,
    ProviderDiscoveryQuery,
    ProviderStatus,
    compute_provider_binding_digest,
)
from corvus.infrastructure.agent_runtimes.codex import (
    CodexAdapterError,
    CodexCliAdapter,
    LocalCodexTextRequest,
    _build_prompt,
    _grant_windows_sandbox_preflight,
    _grant_windows_workspace_access,
    _linked_git_access_paths,
    _package_workspace,
    _snapshot_workspace,
    _windows_profile_directory,
    _workspace_traverse_boundaries,
)
from corvus.infrastructure.agent_runtimes.process_session import (
    ProcessInvocation,
    ProcessSessionError,
    ProcessSessionEvent,
    ProcessSessionEventKind,
)
from corvus.safe_process import TrustedProcessError

NOW = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000001")


def test_windows_build_prompt_uses_sandboxed_shell_edits() -> None:
    prompt = _build_prompt("Create the requested files.", windows_shell_edits=True)

    assert "Do not use `apply_patch`" in prompt
    assert "sandboxed PowerShell" in prompt
    assert prompt.endswith("User request:\nCreate the requested files.")


def test_build_artifact_packages_only_files_changed_after_the_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "existing.py").write_text(
        'EXAMPLE = "ghp_12345678901234567890"\n',
        encoding="utf-8",
    )
    baseline = _snapshot_workspace(workspace)
    (workspace / "result.txt").write_text("sandbox ok", encoding="utf-8")

    artifact = _package_workspace(workspace, uuid4(), baseline_digests=baseline)

    with zipfile.ZipFile(artifact.path) as package:
        assert "result.txt" in package.namelist()
        assert "existing.py" not in package.namelist()


@pytest.mark.skipif(os.name != "nt", reason="Windows known-folder semantics")
def test_windows_profile_directory_ignores_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_profile = _windows_profile_directory()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _windows_profile_directory() == expected_profile


def test_workspace_traverse_boundaries_cover_the_complete_managed_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)

    assert _workspace_traverse_boundaries(workspace, home=tmp_path) == (
        workspace.parent,
        workspace.parent.parent,
        tmp_path,
    )


def test_workspace_traverse_boundaries_stop_at_the_managed_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    managed_root = home / "AppData" / "Local" / "app.corvus.desktop"
    workspace = managed_root / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)

    assert _workspace_traverse_boundaries(workspace, home=home) == (
        workspace.parent,
        workspace.parent.parent,
        managed_root,
    )


def test_workspace_traverse_boundaries_accept_local_chat_codex_sandbox(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    managed_root = home / "AppData" / "Local" / "app.corvus.desktop"
    approved_root = managed_root / ".corvus-local-chat" / "codex"
    workspace = approved_root / "run"
    workspace.mkdir(parents=True)

    assert _workspace_traverse_boundaries(
        workspace,
        approved_root=approved_root,
        home=home,
    ) == (
        approved_root,
        approved_root.parent,
        managed_root,
    )


def test_workspace_traverse_boundaries_ignore_desktop_profile_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    managed_root = home / "AppData" / "Local" / "app.corvus.desktop"
    workspace = managed_root / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex._windows_profile_directory",
        lambda: managed_root,
    )

    assert _workspace_traverse_boundaries(workspace) == (
        workspace.parent,
        workspace.parent.parent,
        managed_root,
    )


@pytest.mark.asyncio
async def test_sandbox_preflight_grants_only_directory_entry_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex._windows_profile_directory",
        lambda: tmp_path,
    )
    logon_sid = "S-1-5-5-100-500"
    user_sid = "S-1-5-21-100-200-300-501"
    granted: list[tuple[Path, str]] = []
    modified: list[tuple[Path, str]] = []
    operations: list[tuple[str, Path, str]] = []
    active_grants = 0
    max_active_grants = 0
    grant_lock = threading.Lock()
    grant_delay = threading.Event()

    def grant_traverse(directory: Path, sid: str) -> None:
        nonlocal active_grants, max_active_grants
        with grant_lock:
            active_grants += 1
            max_active_grants = max(max_active_grants, active_grants)
        grant_delay.wait(0.01)
        with grant_lock:
            granted.append((directory, sid))
            operations.append(("traverse", directory, sid))
            active_grants -= 1

    def grant_modify(directory: Path, sid: str) -> None:
        modified.append((directory, sid))
        operations.append(("modify", directory, sid))

    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.windows_current_logon_sid",
        lambda: logon_sid,
    )
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.windows_current_user_sid",
        lambda: user_sid,
    )
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.grant_windows_sid_traverse",
        grant_traverse,
    )
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.grant_windows_sid_modify",
        grant_modify,
    )

    await _grant_windows_sandbox_preflight(workspace)

    assert set(granted) == {
        (workspace, logon_sid),
        *((directory, logon_sid) for directory in _workspace_traverse_boundaries(workspace)),
    }
    assert modified == [(workspace, user_sid)]
    assert operations[0] == ("modify", workspace, user_sid)
    assert max_active_grants == 1


@pytest.mark.asyncio
async def test_workspace_access_grants_managed_boundaries_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex._windows_profile_directory",
        lambda: tmp_path,
    )
    sandbox_sid = "S-1-5-21-100-200-300-400"
    second_sandbox_sid = "S-1-5-21-100-200-300-401"
    sandbox_sids = frozenset({sandbox_sid, second_sandbox_sid})
    source_repository = tmp_path / "source"
    common_git_directory = source_repository / ".git"
    common_git_directory.mkdir(parents=True)
    expected_boundaries = _workspace_traverse_boundaries(workspace)
    concurrent_boundaries = frozenset(expected_boundaries[:2])
    barrier = threading.Barrier(len(concurrent_boundaries))
    granted: list[tuple[Path, str]] = []
    readable: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.windows_directory_acl_sids",
        lambda _workspace: sandbox_sids,
    )

    def grant(directory: Path, sid: str) -> None:
        granted.append((directory, sid))
        if directory in concurrent_boundaries and sid == min(sandbox_sids):
            barrier.wait(timeout=5)

    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.grant_windows_sid_traverse",
        grant,
    )

    def grant_read(directory: Path, sid: str) -> None:
        readable.append((directory, sid))

    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex.grant_windows_sid_read",
        grant_read,
    )

    await _grant_windows_workspace_access(
        workspace,
        frozenset(),
        (source_repository, common_git_directory),
    )

    assert set(granted) == {
        *((boundary, sid) for boundary in expected_boundaries for sid in sandbox_sids),
        *((source_repository, sid) for sid in sandbox_sids),
    }
    assert set(readable) == {(common_git_directory, sid) for sid in sandbox_sids}


def test_linked_git_access_paths_validate_bidirectional_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)
    source_repository = tmp_path / "source"
    git_directory = source_repository / ".git" / "worktrees" / workspace.name
    git_directory.mkdir(parents=True)
    git_pointer = workspace / ".git"
    git_pointer.write_text(f"gitdir: {git_directory}\n", encoding="utf-8")
    (git_directory / "gitdir").write_text(f"{git_pointer}\n", encoding="utf-8")
    (git_directory / "commondir").write_text("../..\n", encoding="utf-8")

    assert _linked_git_access_paths(workspace) == (
        source_repository.resolve(),
        (source_repository / ".git").resolve(),
    )


def test_linked_git_access_paths_reject_mismatched_reverse_pointer(tmp_path: Path) -> None:
    workspace = tmp_path / ".corvus-worktrees" / "repository" / "run"
    workspace.mkdir(parents=True)
    source_repository = tmp_path / "source"
    git_directory = source_repository / ".git" / "worktrees" / workspace.name
    git_directory.mkdir(parents=True)
    (workspace / ".git").write_text(f"gitdir: {git_directory}\n", encoding="utf-8")
    (git_directory / "gitdir").write_text(f"{tmp_path / 'other'}\n", encoding="utf-8")
    (git_directory / "commondir").write_text("../..\n", encoding="utf-8")

    with pytest.raises(TrustedProcessError, match="metadata is unavailable"):
        _linked_git_access_paths(workspace)


class _Session:
    def __init__(self, events: tuple[ProcessSessionEvent, ...]) -> None:
        self._events = events
        self.cancel_calls = 0

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        for event in self._events:
            if event.sequence > after_sequence:
                yield event

    async def cancel(self) -> ProcessSessionEvent:
        self.cancel_calls += 1
        return ProcessSessionEvent(
            sequence=max((event.sequence for event in self._events), default=0) + 1,
            kind=ProcessSessionEventKind.CANCELLED,
            reason_code="process_cancelled",
        )


class _Starter:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.invocation: ProcessInvocation | None = None

    async def __call__(self, invocation: ProcessInvocation) -> _Session:
        self.invocation = invocation
        return self.session


class _FailingStarter:
    async def __call__(self, _invocation: ProcessInvocation) -> _Session:
        raise ProcessSessionError("process_spawn_failed")


class _GatedSession:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        events = (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "streaming-thread"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {
                        "id": "reason-1",
                        "type": "reasoning",
                        "summary": "Checking inputs",
                        "text": "do-not-expose-hidden-reasoning",
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {"id": "message-1", "type": "agent_message", "text": "done"},
                },
            ),
            ProcessSessionEvent(
                sequence=4,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=5, kind=ProcessSessionEventKind.EXITED, return_code=0),
        )
        for event in events:
            if event.sequence <= after_sequence:
                continue
            if event.sequence == 2:
                await self.release.wait()
            yield event

    async def cancel(self) -> bool:
        self.release.set()
        return True


class _GatedStarter:
    def __init__(self) -> None:
        self.session = _GatedSession()

    async def __call__(self, _invocation: ProcessInvocation) -> _GatedSession:
        return self.session


class _BuildExitGatedSession:
    def __init__(self) -> None:
        self.release_exit = asyncio.Event()

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        events = (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "build-exit-gate"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=3, kind=ProcessSessionEventKind.EXITED, return_code=0),
        )
        for process_event in events:
            if process_event.sequence <= after_sequence:
                continue
            if process_event.sequence == 3:
                await self.release_exit.wait()
            yield process_event

    async def cancel(self) -> bool:
        self.release_exit.set()
        return True


class _BuildExitGatedStarter:
    def __init__(self) -> None:
        self.session = _BuildExitGatedSession()
        self.invocation: ProcessInvocation | None = None

    async def __call__(self, invocation: ProcessInvocation) -> _BuildExitGatedSession:
        self.invocation = invocation
        return self.session


def _request(binding_id: UUID, binding_digest: str, **updates: object) -> AgentRunRequest:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": WORKSPACE_ID,
        "provider_binding_id": binding_id,
        "provider_binding_version": 1,
        "provider_binding_digest": binding_digest,
        "model": "gpt-5.4",
        "effort": "normal",
        "prompt": "Reply with hello.",
        "authorization_proof_id": uuid4(),
        "authorization_proof_digest": "1" * 64,
        "autonomy_grant_id": uuid4(),
        "autonomy_grant_digest": "2" * 64,
        "credential_grant_ids": (),
        "kill_switch_proof_id": uuid4(),
        "kill_switch_proof_digest": "3" * 64,
        "sandbox_profile": "read-only",
        "filesystem_envelope": (),
        "network_envelope": (),
        "tool_envelope": (),
        "requested_effect_classes": frozenset(),
        "provider_spend_limit": Decimal(0),
        "corvus_budget_limit": Decimal(0),
        "budget_unit": "usd_micros",
        "budget_requested_amount": 1,
        "approval_limit": 0,
        "max_retries": 0,
        "max_turns": 1,
        "deadline": datetime(2026, 7, 18, tzinfo=UTC),
        "max_output_tokens": 2_000,
        "max_output_bytes": 100_000,
        "idempotency_key": "codex-test-run",
    }
    values.update(updates)
    return AgentRunRequest(**values)


def _adapter(
    tmp_path: Path, events: tuple[ProcessSessionEvent, ...]
) -> tuple[CodexCliAdapter, _Starter]:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    starter = _Starter(_Session(events))
    return (
        CodexCliAdapter(
            executable=executable,
            version="0.144.0",
            scratch_root=tmp_path / "runs",
            clock=lambda: NOW,
            session_starter=starter,
        ),
        starter,
    )


@pytest.mark.asyncio
async def test_codex_adapter_discovers_pinned_text_only_binding(tmp_path: Path) -> None:
    adapter, _starter = _adapter(tmp_path, ())

    candidates = await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID))

    assert len(candidates) == 1
    binding = candidates[0].binding
    assert binding.status is ProviderStatus.AVAILABLE
    assert binding.executable_identity is not None
    assert binding.executable_identity.version == "0.144.0"
    assert candidates[0].binding_digest == compute_provider_binding_digest(binding)
    assert binding.capabilities.text.value == "supported"
    assert binding.capabilities.tools.value == "unsupported"
    assert binding.capabilities.shell.value == "unsupported"
    assert binding.capabilities.mcp.value == "unsupported"


@pytest.mark.asyncio
async def test_codex_adapter_uses_only_an_explicit_approved_managed_workspace(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    managed_root = tmp_path / "managed"
    workspace = managed_root / "run-1"
    workspace.mkdir(parents=True)
    starter = _Starter(_Session(()))
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "scratch",
        approved_workspace_roots=(managed_root,),
        clock=lambda: NOW,
        session_starter=starter,
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Change the repository.",
            idempotency_key="managed-run",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
            workspace=workspace,
            package_artifact=False,
        ),
    )

    assert starter.invocation is not None
    assert starter.invocation.cwd == workspace.resolve()
    assert starter.invocation.approved_roots == (workspace.resolve(),)
    assert starter.invocation.limits.timeout_seconds == 600.0


@pytest.mark.asyncio
async def test_codex_adapter_snapshots_build_workspace_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    managed_root = tmp_path / "managed"
    workspace = managed_root / "run-1"
    workspace.mkdir(parents=True)
    snapshot_threads: list[int] = []

    def snapshot_workspace(_: Path) -> dict[str, str]:
        snapshot_threads.append(threading.get_ident())
        return {}

    monkeypatch.setattr(
        "corvus.infrastructure.agent_runtimes.codex._snapshot_workspace",
        snapshot_workspace,
    )
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "scratch",
        approved_workspace_roots=(managed_root,),
        clock=lambda: NOW,
        session_starter=_Starter(_Session(())),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    event_loop_thread = threading.get_ident()

    await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Change the repository.",
            idempotency_key="threaded-snapshot",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
            workspace=workspace,
            package_artifact=True,
        ),
    )

    assert snapshot_threads
    assert snapshot_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_codex_adapter_refuses_explicit_workspace_outside_approved_roots(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "scratch",
        approved_workspace_roots=(managed_root,),
        clock=lambda: NOW,
        session_starter=_Starter(_Session(())),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    with pytest.raises(CodexAdapterError, match="codex_workspace_unapproved"):
        await adapter.start_local_text(
            binding.binding,
            LocalCodexTextRequest(
                run_id=uuid4(),
                prompt="Escape.",
                idempotency_key="outside-run",
                deadline=datetime(2026, 7, 18, tzinfo=UTC),
                mode="build",
                workspace=outside,
                package_artifact=False,
            ),
        )


@pytest.mark.asyncio
async def test_codex_idempotency_replay_returns_the_original_bound_handle(tmp_path: Path) -> None:
    adapter, _starter = _adapter(tmp_path, ())
    candidate = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    request = _request(candidate.binding.id, candidate.binding_digest)

    started = await adapter.start(request)
    replayed = await adapter.start(request.model_copy(update={"run_id": uuid4()}))

    assert replayed.replayed is True
    assert replayed.handle == started.handle


@pytest.mark.asyncio
async def test_codex_adapter_builds_bounded_non_shell_invocation_and_normalizes_text(
    tmp_path: Path,
) -> None:
    process_events = (
        ProcessSessionEvent(
            sequence=1,
            kind=ProcessSessionEventKind.FRAME,
            frame={"type": "thread.started", "thread_id": "thread-safe"},
        ),
        ProcessSessionEvent(
            sequence=2,
            kind=ProcessSessionEventKind.FRAME,
            frame={
                "type": "item.completed",
                "item": {"id": "item-1", "type": "agent_message", "text": "hello"},
            },
        ),
        ProcessSessionEvent(
            sequence=3,
            kind=ProcessSessionEventKind.FRAME,
            frame={"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 1}},
        ),
        ProcessSessionEvent(sequence=4, kind=ProcessSessionEventKind.EXITED, return_code=0),
    )
    adapter, starter = _adapter(tmp_path, process_events)
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    request = _request(binding.binding.id, binding.binding_digest)

    start = await adapter.start(request)
    events = [event async for event in adapter.events(start.handle)]

    assert starter.invocation is not None
    assert starter.invocation.executable == binding.binding.executable_identity.executable_path
    assert starter.invocation.arguments[:7] == (
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
    )
    assert starter.invocation.arguments[-1] == "-"
    cd_index = starter.invocation.arguments.index("--cd")
    assert Path(starter.invocation.arguments[cd_index + 1]) == starter.invocation.cwd
    assert request.prompt not in starter.invocation.arguments
    assert starter.invocation.stdin == request.prompt.encode("utf-8")
    assert starter.invocation.cwd.parent == tmp_path / "runs"
    assert starter.invocation.limits.timeout_seconds <= 120
    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.MESSAGE_DELTA,
        AgentRunEventType.USAGE,
        AgentRunEventType.COMPLETED,
    ]
    assert events[1].redacted_payload == {"text": "hello"}
    assert start.handle.state is AgentRunState.RUNNING


@pytest.mark.asyncio
async def test_codex_build_waiting_for_input_is_not_reported_as_failed(
    tmp_path: Path,
) -> None:
    process_events = (
        ProcessSessionEvent(
            sequence=1,
            kind=ProcessSessionEventKind.FRAME,
            frame={"type": "thread.started", "thread_id": "thread-needs-input"},
        ),
        ProcessSessionEvent(
            sequence=2,
            kind=ProcessSessionEventKind.FRAME,
            frame={
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": "Please confirm before I make changes.",
                },
            },
        ),
        ProcessSessionEvent(
            sequence=3,
            kind=ProcessSessionEventKind.FRAME,
            frame={"type": "turn.completed", "usage": {"output_tokens": 8}},
        ),
        ProcessSessionEvent(sequence=4, kind=ProcessSessionEventKind.EXITED, return_code=0),
    )
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    managed_root = tmp_path / "managed"
    workspace = managed_root / "run"
    workspace.mkdir(parents=True)
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "scratch",
        approved_workspace_roots=(managed_root,),
        clock=lambda: NOW,
        session_starter=_Starter(_Session(process_events)),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Make a change after confirmation.",
            idempotency_key="needs-input-run",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
            workspace=workspace,
            package_artifact=True,
        ),
    )

    events = [event async for event in adapter.events(start.handle)]

    assert events[-1].event_type is AgentRunEventType.COMPLETED
    assert AgentRunEventType.FAILED not in {event.event_type for event in events}
    assert events[-1].redacted_payload == {
        "reason_code": "codex_build_empty",
        "status": "needs_input",
    }


@pytest.mark.asyncio
async def test_codex_adapter_keeps_large_prompts_out_of_windows_process_arguments(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(tmp_path, ())
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    prompt = "x" * 40_000
    request = _request(binding.binding.id, binding.binding_digest).model_copy(
        update={"prompt": prompt}
    )

    await adapter.start(request)

    assert starter.invocation is not None
    assert starter.invocation.arguments[-1] == "-"
    assert prompt not in starter.invocation.arguments
    assert starter.invocation.stdin == prompt.encode("utf-8")


@pytest.mark.asyncio
async def test_codex_adapter_streams_safe_reasoning_before_terminal(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    starter = _GatedStarter()
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "runs",
        clock=lambda: NOW,
        session_starter=starter,
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start(_request(binding.binding.id, binding.binding_digest))
    stream = adapter.events(start.handle)

    first = await asyncio.wait_for(anext(stream), timeout=0.2)

    assert first.event_type is AgentRunEventType.STARTED
    starter.session.release.set()
    remaining = [event async for event in stream]
    assert [event.event_type for event in remaining] == [
        AgentRunEventType.REASONING_DELTA,
        AgentRunEventType.MESSAGE_DELTA,
        AgentRunEventType.USAGE,
        AgentRunEventType.COMPLETED,
    ]
    assert remaining[0].redacted_payload == {"text": "Checking inputs"}
    assert "do-not-expose-hidden-reasoning" not in repr(remaining)


@pytest.mark.asyncio
async def test_codex_adapter_replaces_raw_reasoning_with_generic_status(tmp_path: Path) -> None:
    adapter, _starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "raw-reasoning"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {
                        "id": "reason-1",
                        "type": "reasoning",
                        "text": "do-not-expose-hidden-reasoning",
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=4, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start(_request(binding.binding.id, binding.binding_digest))

    events = [event async for event in adapter.events(start.handle)]

    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.USAGE,
        AgentRunEventType.COMPLETED,
    ]
    assert events[1].redacted_payload == {"activity": "provider", "status": "thinking"}
    assert "do-not-expose-hidden-reasoning" not in repr(events)


@pytest.mark.asyncio
async def test_codex_adapter_bounds_and_redacts_provider_reasoning_summary(
    tmp_path: Path,
) -> None:
    adapter, _starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "bounded-summary"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {
                        "id": "reason-1",
                        "type": "reasoning",
                        "summary": ("S" * 600) + ' api_key="alllettersecretvalue"',
                        "text": "do-not-expose-hidden-reasoning",
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=4, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start(_request(binding.binding.id, binding.binding_digest))

    events = [event async for event in adapter.events(start.handle)]

    summary = events[1].redacted_payload["text"]
    assert isinstance(summary, str)
    assert len(summary) <= 512
    assert "alllettersecretvalue" not in repr(events)
    assert "do-not-expose-hidden-reasoning" not in repr(events)


@pytest.mark.asyncio
async def test_codex_build_packages_only_after_natural_process_exit(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    starter = _BuildExitGatedStarter()
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "runs",
        clock=lambda: NOW,
        session_starter=starter,
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build a complete small project.",
            idempotency_key="build-exit-gated",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    (starter.invocation.cwd / "index.html").write_text("<h1>Built</h1>", encoding="utf-8")
    stream = adapter.events(start.handle)

    assert (await anext(stream)).event_type is AgentRunEventType.STARTED
    assert (await anext(stream)).event_type is AgentRunEventType.USAGE
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    assert adapter.artifact(start.handle) is None

    starter.session.release_exit.set()
    artifact_event = await asyncio.wait_for(pending, timeout=0.2)
    remaining = [event async for event in stream]

    assert artifact_event.event_type is AgentRunEventType.ARTIFACT
    assert [event.event_type for event in remaining] == [AgentRunEventType.COMPLETED]
    assert adapter.artifact(start.handle) is not None


@pytest.mark.asyncio
async def test_codex_build_mode_is_scratch_scoped_and_emits_safe_tool_progress(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "build-thread"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.started",
                    "item": {
                        "id": "tool-1",
                        "type": "command_execution",
                        "command": "python -m unittest -v --token do-not-expose-this-command",
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {"id": "tool-1", "type": "command_execution", "status": "completed"},
                },
            ),
            ProcessSessionEvent(
                sequence=4,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=5, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build a complete small project.",
            idempotency_key="local-build",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
            mcp_enabled=False,
        ),
    )
    assert starter.invocation is not None
    (starter.invocation.cwd / "index.html").write_text("<h1>Built</h1>", encoding="utf-8")
    events = [event async for event in adapter.events(start.handle)]

    assert "workspace-write" in starter.invocation.arguments
    assert "--ephemeral" in starter.invocation.arguments
    assert "--ignore-user-config" in starter.invocation.arguments
    assert "--ignore-rules" in starter.invocation.arguments
    for feature in ("plugins", "apps", "hooks"):
        feature_index = starter.invocation.arguments.index(feature)
        assert starter.invocation.arguments[feature_index - 1] == "--disable"
    if os.name == "nt":
        assert 'windows.sandbox="unelevated"' in starter.invocation.arguments
    assert starter.invocation.arguments[-1] == "-"
    assert starter.invocation.stdin is not None
    assert starter.invocation.stdin.decode("utf-8").endswith(
        "User request:\nBuild a complete small project."
    )
    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.USAGE,
        AgentRunEventType.ARTIFACT,
        AgentRunEventType.COMPLETED,
    ]
    assert events[1].redacted_payload == {
        "activity": "command",
        "label": "Run Python unit tests",
        "status": "started",
        "tool_id": "tool-1",
    }
    assert "do-not-expose-this-command" not in str(events[1].redacted_payload)
    assert "do-not-expose-this-command" not in repr(events)
    artifact = adapter.artifact(start.handle)
    assert artifact is not None
    assert artifact.path.is_file()
    assert artifact.download_name.endswith(".zip")
    assert artifact.secret_screening == "passed"  # noqa: S105


@pytest.mark.asyncio
async def test_codex_inspect_mode_allows_redacted_read_only_tool_progress(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "inspect-thread"},
            ),
            ProcessSessionEvent(
                sequence=2,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.started",
                    "item": {
                        "id": "tool-1",
                        "type": "command_execution",
                        "command": "do-not-expose-this-command",
                    },
                },
            ),
            ProcessSessionEvent(
                sequence=3,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.completed",
                    "item": {"id": "tool-1", "type": "command_execution"},
                },
            ),
            ProcessSessionEvent(
                sequence=4,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=5, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Inspect the repository without changing files.",
            idempotency_key="local-inspect",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="inspect",
        ),
    )
    events = [event async for event in adapter.events(start.handle)]

    assert starter.invocation is not None
    assert "read-only" in starter.invocation.arguments
    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.CHECKPOINT,
        AgentRunEventType.USAGE,
        AgentRunEventType.COMPLETED,
    ]
    assert events[1].redacted_payload == {
        "activity": "command",
        "label": "Run a sandboxed command",
        "status": "started",
        "tool_id": "tool-1",
    }
    assert starter.session.cancel_calls == 0
    assert "do-not-expose-this-command" not in repr(events)


@pytest.mark.asyncio
async def test_codex_mcp_opt_in_still_disables_non_mcp_extensions(tmp_path: Path) -> None:
    adapter, starter = _adapter(tmp_path, ())
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Use the configured MCP server.",
            idempotency_key="local-mcp",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mcp_enabled=True,
        ),
    )

    assert starter.invocation is not None
    assert "--ignore-user-config" not in starter.invocation.arguments
    for feature in ("plugins", "apps", "hooks"):
        feature_index = starter.invocation.arguments.index(feature)
        assert starter.invocation.arguments[feature_index - 1] == "--disable"


@pytest.mark.asyncio
async def test_codex_build_fails_when_process_exits_before_protocol_completion(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "thread.started", "thread_id": "truncated-build"},
            ),
            ProcessSessionEvent(sequence=2, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build the project.",
            idempotency_key="truncated-build",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    (starter.invocation.cwd / "index.html").write_text("<h1>Partial</h1>", encoding="utf-8")

    events = [event async for event in adapter.events(start.handle)]

    assert [event.event_type for event in events] == [
        AgentRunEventType.STARTED,
        AgentRunEventType.FAILED,
    ]
    assert events[-1].redacted_payload == {"reason_code": "codex_stream_incomplete"}
    assert adapter.artifact(start.handle) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_path",
    (
        Path(".npmrc"),
        Path(".aws") / "credentials",
        Path(".config") / "gcloud" / "application_default_credentials.json",
        Path(".ssh") / "id_ed25519.pub",
    ),
)
async def test_codex_build_rejects_common_credential_paths(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=2, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build the project.",
            idempotency_key=f"credential-path-{relative_path.as_posix()}",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    credential = starter.invocation.cwd / relative_path
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("credential material", encoding="utf-8")

    events = [event async for event in adapter.events(start.handle)]

    assert events[-1].event_type is AgentRunEventType.FAILED
    assert events[-1].redacted_payload == {"reason_code": "codex_build_secret_file_rejected"}
    assert adapter.artifact(start.handle) is None


@pytest.mark.asyncio
async def test_codex_build_rejects_directory_symlinks(tmp_path: Path) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=2, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    candidate = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        candidate.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build the project.",
            idempotency_key="directory-symlink",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    try:
        os.symlink(outside, starter.invocation.cwd / "linked", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    events = [event async for event in adapter.events(start.handle)]

    assert events[-1].event_type is AgentRunEventType.FAILED
    assert events[-1].redacted_payload == {"reason_code": "codex_build_link_rejected"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-material",
        'api_key = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"',
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcd1234.efgh5678",
        'password = "correcthorsebatterystaple"',
        'cookie = "sessioncookievalue"',
        'credential = "credentialmaterialonlyletters"',
        'private_key = "privatekeymaterialonlyletters"',
    ),
)
async def test_codex_build_rejects_secret_content_in_ordinary_files(
    tmp_path: Path,
    content: str,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=2, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build the project.",
            idempotency_key=f"secret-content-{hash(content)}",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    (starter.invocation.cwd / "notes.txt").write_text(content, encoding="utf-8")

    events = [event async for event in adapter.events(start.handle)]

    assert events[-1].event_type is AgentRunEventType.FAILED
    assert events[-1].redacted_payload == {"reason_code": "codex_build_secret_file_rejected"}
    assert content not in repr(events)
    assert adapter.artifact(start.handle) is None


@pytest.mark.asyncio
async def test_codex_build_allows_non_secret_examples_in_ordinary_files(tmp_path: Path) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={"type": "turn.completed", "usage": {"output_tokens": 1}},
            ),
            ProcessSessionEvent(sequence=2, kind=ProcessSessionEventKind.EXITED, return_code=0),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start_local_text(
        binding.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="Build the project.",
            idempotency_key="safe-example-content",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
            mode="build",
        ),
    )
    assert starter.invocation is not None
    (starter.invocation.cwd / "README.md").write_text(
        "\n".join(
            (
                'Set api_key = "your-api-key-here" before running.',
                "Example: sk-proj-replace-with-your-api-key-here-123456",
                "Authorization: Bearer your-placeholder-token-goes-here-123456",
                "password: SecretStr",
                "credential: CredentialReference",
                "database_password = env://DATABASE_PASSWORD",
                "api_key = keyring://corvus/provider/api-key",
            )
        ),
        encoding="utf-8",
    )

    events = [event async for event in adapter.events(start.handle)]

    assert events[-2].event_type is AgentRunEventType.ARTIFACT
    assert events[-1].event_type is AgentRunEventType.COMPLETED


@pytest.mark.asyncio
async def test_local_codex_default_omits_model_flag_and_rejects_flag_injection(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(tmp_path, ())
    candidate = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    await adapter.start_local_text(
        candidate.binding,
        LocalCodexTextRequest(
            run_id=uuid4(),
            prompt="hello",
            idempotency_key="local-default",
            deadline=datetime(2026, 7, 18, tzinfo=UTC),
        ),
    )

    assert starter.invocation is not None
    assert "--model" not in starter.invocation.arguments
    with pytest.raises(CodexAdapterError, match="codex_model_invalid"):
        await adapter.start_local_text(
            candidate.binding,
            LocalCodexTextRequest(
                run_id=uuid4(),
                prompt="hello",
                model="--dangerous-flag",
                idempotency_key="local-invalid",
                deadline=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )


@pytest.mark.asyncio
async def test_codex_adapter_translates_process_spawn_failure(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned-codex")
    adapter = CodexCliAdapter(
        executable=executable,
        version="0.144.0",
        scratch_root=tmp_path / "runs",
        clock=lambda: NOW,
        session_starter=_FailingStarter(),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]

    with pytest.raises(CodexAdapterError, match="^codex_process_unavailable$"):
        await adapter.start_local_text(
            binding.binding,
            LocalCodexTextRequest(
                run_id=uuid4(),
                prompt="hello",
                idempotency_key="local-spawn-failure",
                deadline=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )


@pytest.mark.asyncio
async def test_codex_adapter_blocks_tool_events_and_cancels_process(tmp_path: Path) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FRAME,
                frame={
                    "type": "item.started",
                    "item": {"id": "tool-1", "type": "command_execution", "command": "secret"},
                },
            ),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start(_request(binding.binding.id, binding.binding_digest))

    events = [event async for event in adapter.events(start.handle)]

    assert [event.event_type for event in events] == [AgentRunEventType.FAILED]
    assert events[0].redacted_payload == {"reason_code": "codex_tool_event_blocked"}
    assert starter.session.cancel_calls == 1
    assert "secret" not in repr(events)


@pytest.mark.asyncio
async def test_codex_adapter_redacts_terminal_diagnostics_and_cancels_idempotently(
    tmp_path: Path,
) -> None:
    adapter, starter = _adapter(
        tmp_path,
        (
            ProcessSessionEvent(
                sequence=1,
                kind=ProcessSessionEventKind.FAILED,
                stderr="Authorization: Bearer sk-secret-token",
                reason_code="process_failed",
            ),
        ),
    )
    binding = (await adapter.discover(ProviderDiscoveryQuery(workspace_id=WORKSPACE_ID)))[0]
    start = await adapter.start(_request(binding.binding.id, binding.binding_digest))

    events = [event async for event in adapter.events(start.handle)]
    first_cancel = await adapter.cancel(start.handle, uuid4(), "4" * 64)
    second_cancel = await adapter.cancel(start.handle, uuid4(), "5" * 64)

    assert events[-1].event_type is AgentRunEventType.FAILED
    assert "sk-secret-token" not in repr(events)
    assert first_cancel.reason_code == second_cancel.reason_code == "agent_run_already_terminal"
    assert starter.session.cancel_calls == 0
