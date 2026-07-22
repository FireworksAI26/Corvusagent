from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from corvus.infrastructure.agent_runtimes.codex import LocalBuildArtifact, LocalCodexTextRequest
from corvus.mvp import api as api_module
from corvus.mvp import local_chat as local_chat_module
from corvus.mvp.api import create_app
from corvus.mvp.git_process import ProcessResult
from corvus.mvp.local_chat import (
    LocalChatBackendEvent,
    LocalChatBackendHandle,
    LocalChatNotFound,
    LocalChatService,
)
from corvus.mvp.provider_credentials import ProviderCredentialService
from corvus.mvp.safety import build_safety_preview
from corvus.mvp.store import SqliteStore

NOW = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)


class _Backend:
    def __init__(self) -> None:
        self.starts = 0
        self.cancelled: set[UUID] = set()
        self.last_effort: str | None = None
        self.last_prompt: str | None = None

    async def start(
        self,
        *,
        run_id: UUID,
        prompt: str,
        model: str | None,
        effort: str,
        mode: str,
        mcp_enabled: bool,
        idempotency_key: str,
    ) -> LocalChatBackendHandle:
        self.last_prompt = prompt
        del model, mode, mcp_enabled, idempotency_key
        self.starts += 1
        self.last_effort = effort
        return LocalChatBackendHandle(id=uuid4(), run_id=run_id)

    async def events(
        self,
        handle: LocalChatBackendHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[LocalChatBackendEvent]:
        del handle
        events = (
            LocalChatBackendEvent(1, NOW, "started", {"status": "started"}),
            LocalChatBackendEvent(2, NOW, "message", {"text": "safe reply"}),
            LocalChatBackendEvent(3, NOW, "completed", {"status": "completed"}),
        )
        for event in events:
            if event.sequence > after_sequence:
                yield event

    async def cancel(self, handle: LocalChatBackendHandle) -> bool:
        self.cancelled.add(handle.id)
        return True

    def artifact(self, handle: LocalChatBackendHandle) -> LocalBuildArtifact | None:
        del handle
        return None


class _ProjectBackend(_Backend):
    source_directory: Path | None = None

    async def start_in_workspace(
        self,
        *,
        run_id: UUID,
        prompt: str,
        model: str | None,
        effort: str,
        mode: str,
        mcp_enabled: bool,
        idempotency_key: str,
        source_directory: Path,
    ) -> LocalChatBackendHandle:
        self.source_directory = source_directory
        return await self.start(
            run_id=run_id,
            prompt=prompt,
            model=model,
            effort=effort,
            mode=mode,
            mcp_enabled=mcp_enabled,
            idempotency_key=idempotency_key,
        )


@pytest.mark.asyncio
async def test_project_directory_is_bound_to_a_project_aware_backend(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    backend = _ProjectBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)

    await service.start(
        owner="local:user",
        prompt="Inspect this project",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="project-run",
        source_directory=project,
    )

    assert backend.source_directory == project


@pytest.mark.asyncio
async def test_provider_readiness_probe_runs_off_the_event_loop() -> None:
    backend = _Backend()
    service = LocalChatService(
        backend=backend,
        readiness_probes={
            "codex": lambda: threading.current_thread() is not threading.main_thread()
        },
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
    )

    await service.start(
        owner="local:user",
        prompt="Check provider readiness",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="readiness-thread",
    )

    assert backend.starts == 1


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_recheck_provider_readiness() -> None:
    backend = _Backend()
    readiness = {"ready": True, "calls": 0}
    readiness_clock = {"now": 0.0}

    def probe() -> bool:
        readiness["calls"] += 1
        return bool(readiness["ready"])

    service = LocalChatService(
        backend=backend,
        readiness_probes={"codex": probe},
        readiness_clock=lambda: readiness_clock["now"],
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
    )
    request = {
        "owner": "local:user",
        "prompt": "Replay this run",
        "provider": "codex",
        "model": None,
        "effort": "medium",
        "mode": "chat",
        "mcp_enabled": False,
        "idempotency_key": "readiness-replay",
    }

    first = await service.start(**request)
    readiness["ready"] = False
    readiness_clock["now"] = 10.0
    replay = await service.start(**request)

    assert replay == first
    assert readiness["calls"] == 1
    assert backend.starts == 1


@pytest.mark.asyncio
async def test_completed_local_chat_replay_returns_terminal_state() -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    request = {
        "owner": "local:user",
        "prompt": "Replay the completed run",
        "provider": "codex",
        "model": None,
        "effort": "medium",
        "mode": "chat",
        "mcp_enabled": False,
        "idempotency_key": "completed-replay",
    }

    started = await service.start(**request)
    run_id = UUID(str(started["run_id"]))
    assert [
        event.type
        async for _cursor, event in service.events(
            owner="local:user",
            run_id=run_id,
            cursor=None,
        )
    ] == ["started", "message", "completed"]

    replay = await service.start(**request)

    assert replay["state"] == "completed"
    assert backend.starts == 1


def test_project_copy_creates_an_isolated_workspace(tmp_path: Path) -> None:
    source = tmp_path / "registered-project"
    source.mkdir()
    (source / "README.md").write_text("original", encoding="utf-8")
    destination = tmp_path / "scratch" / "run-1"

    local_chat_module._copy_project(source, destination)
    (destination / "README.md").write_text("changed", encoding="utf-8")

    assert (source / "README.md").read_text(encoding="utf-8") == "original"


def test_project_copy_skips_dependency_and_vcs_directories(tmp_path: Path) -> None:
    source = tmp_path / "registered-project"
    (source / "node_modules").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / "src").mkdir()
    (source / "node_modules" / "large-package.js").write_bytes(b"dependency")
    (source / ".git" / "objects").write_bytes(b"history")
    (source / "src" / "main.ts").write_text("export {};", encoding="utf-8")
    destination = tmp_path / "scratch" / "run-ignored"

    local_chat_module._copy_project(source, destination)

    assert (destination / "src" / "main.ts").is_file()
    assert not (destination / "node_modules").exists()
    assert not (destination / ".git").exists()


def test_project_copy_omits_sensitive_files_from_the_sandbox(tmp_path: Path) -> None:
    source = tmp_path / "registered-project"
    (source / "tests" / "fixtures").mkdir(parents=True)
    (source / "tests" / "fixtures" / ".env").write_text("TOKEN=fake", encoding="utf-8")
    (source / "README.md").write_text("safe", encoding="utf-8")
    destination = tmp_path / "scratch" / "run-sensitive"

    local_chat_module._copy_project(source, destination)

    assert (destination / "README.md").is_file()
    assert not (destination / "tests" / "fixtures" / ".env").exists()


def test_project_copy_rejects_sources_over_the_size_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "registered-project"
    source.mkdir()
    (source / "oversized.bin").write_bytes(b"12345")
    destination = tmp_path / "scratch" / "run-oversized"
    monkeypatch.setattr(local_chat_module, "_PROJECT_COPY_MAX_BYTES", 4)

    with pytest.raises(local_chat_module.LocalChatError, match="local_chat_project_too_large"):
        local_chat_module._copy_project(source, destination)

    assert not destination.exists()


def test_project_copy_rejects_sources_over_the_entry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "registered-project"
    (source / "first").mkdir(parents=True)
    (source / "second").mkdir()
    destination = tmp_path / "scratch" / "run-too-many-entries"
    monkeypatch.setattr(local_chat_module, "_PROJECT_COPY_MAX_ENTRIES", 1)

    with pytest.raises(local_chat_module.LocalChatError, match="local_chat_project_too_large"):
        local_chat_module._copy_project(source, destination)

    assert not destination.exists()


@pytest.mark.asyncio
async def test_codex_backend_removes_copied_workspace_when_start_fails(tmp_path: Path) -> None:
    class _Candidate:
        binding = object()

    class _FailingAdapter:
        async def discover(self, query: object) -> tuple[_Candidate, ...]:
            del query
            return (_Candidate(),)

        async def start_local_text(self, binding: object, request: object) -> None:
            del binding, request
            raise local_chat_module.CodexAdapterError("codex_start_failed")

    source = tmp_path / "registered-project"
    source.mkdir()
    (source / "README.md").write_text("project", encoding="utf-8")
    scratch_root = tmp_path / "scratch"
    run_id = uuid4()
    backend = local_chat_module.CodexLocalChatBackend(_FailingAdapter(), lambda: NOW, scratch_root)

    with pytest.raises(local_chat_module.CodexAdapterError, match="codex_start_failed"):
        await backend.start_in_workspace(
            run_id=run_id,
            prompt="Inspect the project",
            model=None,
            effort="medium",
            mode="chat",
            mcp_enabled=False,
            idempotency_key="failed-codex-start",
            source_directory=source,
        )

    assert not (scratch_root / str(run_id)).exists()


@pytest.mark.asyncio
async def test_codex_backend_uses_extended_deadline_for_build_mode(tmp_path: Path) -> None:
    class _Candidate:
        binding = object()

    class _CapturingAdapter:
        request: LocalCodexTextRequest | None = None

        async def discover(self, query: object) -> tuple[_Candidate, ...]:
            del query
            return (_Candidate(),)

        async def start_local_text(
            self,
            binding: object,
            request: LocalCodexTextRequest,
        ) -> None:
            del binding
            self.request = request
            raise local_chat_module.CodexAdapterError("codex_start_failed")

    source = tmp_path / "registered-project"
    source.mkdir()
    (source / "README.md").write_text("project", encoding="utf-8")
    adapter = _CapturingAdapter()
    backend = local_chat_module.CodexLocalChatBackend(
        adapter,
        lambda: NOW,
        tmp_path / "scratch",
    )

    with pytest.raises(local_chat_module.CodexAdapterError, match="codex_start_failed"):
        await backend.start_in_workspace(
            run_id=uuid4(),
            prompt="Build the project",
            model=None,
            effort="medium",
            mode="build",
            mcp_enabled=False,
            idempotency_key="build-deadline",
            source_directory=source,
        )

    assert adapter.request is not None
    assert adapter.request.deadline == NOW + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_claude_backend_removes_copied_workspace_when_start_fails(tmp_path: Path) -> None:
    class _Candidate:
        binding = object()

    class _FailingAdapter:
        async def discover(self, query: object) -> tuple[_Candidate, ...]:
            del query
            return (_Candidate(),)

        async def start_local_text(self, binding: object, request: object) -> None:
            del binding, request
            raise local_chat_module.ClaudeAdapterError("claude_start_failed")

    source = tmp_path / "registered-project"
    source.mkdir()
    (source / "README.md").write_text("project", encoding="utf-8")
    scratch_root = tmp_path / "scratch"
    run_id = uuid4()
    backend = local_chat_module.ClaudeLocalChatBackend(_FailingAdapter(), lambda: NOW, scratch_root)

    with pytest.raises(local_chat_module.ClaudeAdapterError, match="claude_start_failed"):
        await backend.start_in_workspace(
            run_id=run_id,
            prompt="Inspect the project",
            model=None,
            effort="medium",
            mode="chat",
            mcp_enabled=False,
            idempotency_key="failed-claude-start",
            source_directory=source,
        )

    assert not (scratch_root / str(run_id)).exists()


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class _GatedBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def events(
        self,
        handle: LocalChatBackendHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[LocalChatBackendEvent]:
        del handle
        if after_sequence < 1:
            self.started.set()
            yield LocalChatBackendEvent(1, NOW, "started", {"status": "started"})
        await self.release.wait()
        if after_sequence < 2:
            yield LocalChatBackendEvent(2, NOW, "completed", {"status": "completed"})


class _GatedStartBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(
        self,
        *,
        run_id: UUID,
        prompt: str,
        model: str | None,
        effort: str,
        mode: str,
        mcp_enabled: bool,
        idempotency_key: str,
    ) -> LocalChatBackendHandle:
        self.starts += 1
        self.entered.set()
        await self.release.wait()
        self.last_effort = effort
        del prompt, model, mode, mcp_enabled, idempotency_key
        return LocalChatBackendHandle(id=uuid4(), run_id=run_id)


class _SingleConsumerGatedBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.event_calls = 0

    async def events(
        self,
        handle: LocalChatBackendHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[LocalChatBackendEvent]:
        del handle
        self.event_calls += 1
        if self.event_calls > 1:
            return
        if after_sequence < 1:
            yield LocalChatBackendEvent(1, NOW, "started", {"status": "started"})
        await self.release.wait()
        if after_sequence < 2:
            yield LocalChatBackendEvent(2, NOW, "completed", {"status": "completed"})


class _CancellableBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def events(
        self,
        handle: LocalChatBackendHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[LocalChatBackendEvent]:
        if after_sequence < 1:
            yield LocalChatBackendEvent(1, NOW, "started", {"status": "started"})
        await self.release.wait()
        if after_sequence < 2:
            yield LocalChatBackendEvent(2, NOW, "cancelled", {"status": "cancelled"})

    async def cancel(self, handle: LocalChatBackendHandle) -> bool:
        accepted = await super().cancel(handle)
        self.release.set()
        return accepted


class _ArtifactBackend(_Backend):
    def __init__(self, artifact: LocalBuildArtifact) -> None:
        super().__init__()
        self._artifact = artifact

    def artifact(self, handle: LocalChatBackendHandle) -> LocalBuildArtifact | None:
        del handle
        return self._artifact


def _client(
    tmp_path: Path,
    name: str,
    local_chat: LocalChatService,
) -> tuple[TestClient, dict[str, str]]:
    token = f"bootstrap-{name}"
    client = TestClient(
        create_app(
            database=tmp_path / f"{name}.sqlite3",
            bootstrap_token=token,
            session_secret=b"s" * 32,
            local_chat_service=local_chat,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    session = client.get("/api/auth/session").json()
    return client, {"X-CSRF-Token": session["csrf_token"]}


def _sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    parsed: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        parsed.append((fields["id"], json.loads(fields["data"])))
    return parsed


class _FailingProjectGit:
    def run(self, _cwd: Path, _args: tuple[str, ...]) -> ProcessResult:
        return ProcessResult(1, b"", b"project initialization failed")


class _FailingProjectWorkspace:
    def __init__(self) -> None:
        self.git = _FailingProjectGit()


def test_failed_empty_project_initialization_removes_managed_directory(tmp_path: Path) -> None:
    token = str(uuid4())
    client = TestClient(
        create_app(
            database=tmp_path / "project-cleanup.sqlite3",
            bootstrap_token=token,
            session_secret=b"s" * 32,
            local_chat_service=LocalChatService(
                backend=_Backend(), cursor_secret=b"c" * 32, clock=lambda: NOW
            ),
            repository_workspace=_FailingProjectWorkspace(),  # type: ignore[arg-type]
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    session = client.get("/api/auth/session").json()

    response = client.post(
        "/api/local/projects",
        json={"name": "Broken project"},
        headers={"X-CSRF-Token": session["csrf_token"]},
    )

    assert response.status_code == 503
    project_root = tmp_path / "corvus-agent-projects"
    assert project_root.is_dir()
    assert list(project_root.iterdir()) == []


@pytest.mark.asyncio
async def test_local_chat_service_streams_first_event_before_terminal(tmp_path: Path) -> None:
    backend = _GatedBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    started = await service.start(
        owner="local:user",
        prompt="Hello",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="stream-once",
    )
    stream = service.events(
        owner="local:user",
        run_id=UUID(str(started["run_id"])),
        cursor=None,
    )

    _cursor, first = await asyncio.wait_for(anext(stream), timeout=0.2)

    assert first.type == "started"
    backend.release.set()
    assert [event.type async for _cursor, event in stream] == ["completed"]


@pytest.mark.asyncio
async def test_local_chat_owner_backend_enables_configured_api_provider_without_cross_owner_access(
    tmp_path: Path,
) -> None:
    del tmp_path
    service = LocalChatService(backend=_Backend(), cursor_secret=b"c" * 32, clock=lambda: NOW)
    api_backend = _Backend()
    service.register_owner_backend("local:owner-1", "openai", api_backend)

    started = await service.start(
        owner="local:owner-1",
        prompt="Hello",
        provider="openai",
        model="gpt-5.6-sol",
        effort="high",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="owner-api-run",
    )

    assert started["provider"] == "openai"
    with pytest.raises(Exception, match="provider_unavailable"):
        await service.start(
            owner="local:owner-2",
            prompt="Hello",
            provider="openai",
            model="gpt-5.6-sol",
            effort="high",
            mode="chat",
            mcp_enabled=False,
            idempotency_key="other-owner-api-run",
        )


@pytest.mark.asyncio
async def test_local_chat_non_following_poll_returns_after_buffered_events(tmp_path: Path) -> None:
    backend = _GatedBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    started = await service.start(
        owner="local:user",
        prompt="Hello",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="poll-once",
    )
    stream = service.events(
        owner="local:user",
        run_id=UUID(str(started["run_id"])),
        cursor=None,
        follow=False,
    )

    await asyncio.wait_for(backend.started.wait(), timeout=1)
    _cursor, first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first.type == "started"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1)
    backend.release.set()


@pytest.mark.asyncio
async def test_local_chat_stream_disconnect_does_not_cancel_provider_pump() -> None:
    backend = _SingleConsumerGatedBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    started = await service.start(
        owner="local:user",
        prompt="Hello",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="disconnect-once",
    )
    run_id = UUID(str(started["run_id"]))
    first_stream = service.events(owner="local:user", run_id=run_id, cursor=None)
    first_cursor, first = await asyncio.wait_for(anext(first_stream), timeout=0.2)
    assert first.type == "started"

    await first_stream.aclose()
    backend.release.set()
    replay = service.events(owner="local:user", run_id=run_id, cursor=first_cursor)

    assert [event.type async for _cursor, event in replay] == ["completed"]
    assert backend.event_calls == 1


@pytest.mark.asyncio
async def test_local_chat_concurrent_subscribers_share_one_provider_pump() -> None:
    backend = _SingleConsumerGatedBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    started = await service.start(
        owner="local:user",
        prompt="Hello",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="two-subscribers",
    )
    run_id = UUID(str(started["run_id"]))
    first_stream = service.events(owner="local:user", run_id=run_id, cursor=None)
    second_stream = service.events(owner="local:user", run_id=run_id, cursor=None)

    first, second = await asyncio.gather(anext(first_stream), anext(second_stream))
    assert first[1].type == second[1].type == "started"
    assert backend.event_calls == 1

    backend.release.set()
    assert [event.type async for _cursor, event in first_stream] == ["completed"]
    assert [event.type async for _cursor, event in second_stream] == ["completed"]


@pytest.mark.asyncio
async def test_local_chat_concurrent_idempotent_starts_launch_one_backend() -> None:
    backend = _GatedStartBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    request = {
        "owner": "local:user",
        "prompt": "Hello",
        "provider": "codex",
        "model": None,
        "effort": "medium",
        "mode": "chat",
        "mcp_enabled": False,
        "idempotency_key": "concurrent-once",
    }

    first = asyncio.create_task(service.start(**request))
    await asyncio.wait_for(backend.entered.wait(), timeout=0.2)
    second = asyncio.create_task(service.start(**request))
    await asyncio.sleep(0.01)
    starts_before_release = backend.starts
    backend.release.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert starts_before_release == 1
    assert backend.starts == 1
    assert {key: value for key, value in first_response.items() if key != "state"} == {
        key: value for key, value in second_response.items() if key != "state"
    }
    assert second_response["state"] in {"running", "completed"}


@pytest.mark.asyncio
async def test_local_chat_completed_run_replays_after_service_restart(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    first_backend = _Backend()
    first_service = LocalChatService(
        backend=first_backend,
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
    )
    request = {
        "owner": "local:restart",
        "prompt": "Remember this run",
        "provider": "codex",
        "model": None,
        "effort": "medium",
        "mode": "chat",
        "mcp_enabled": False,
        "idempotency_key": "restart-once",
    }
    response = await first_service.start(**request)
    first_events = [
        event
        async for _cursor, event in first_service.events(
            owner="local:restart", run_id=UUID(response["run_id"]), cursor=None
        )
    ]
    assert first_events[-1].type == "completed"

    restarted_backend = _Backend()
    restarted = LocalChatService(
        backend=restarted_backend,
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
    )

    replay = await restarted.start(**request)
    replayed_events = [
        event
        async for _cursor, event in restarted.events(
            owner="local:restart",
            run_id=UUID(response["run_id"]),
            cursor=None,
            follow=False,
        )
    ]

    assert replay["run_id"] == response["run_id"]
    assert replay["state"] == "completed"
    assert [event.type for event in replayed_events] == ["started", "message", "completed"]
    assert restarted_backend.starts == 0


@pytest.mark.asyncio
async def test_local_build_artifact_reopens_after_restart_when_digest_matches(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifact_path = artifact_root / "project.zip"
    artifact_path.write_bytes(b"screened build")
    artifact = LocalBuildArtifact(
        path=artifact_path,
        download_name="project.zip",
        sha256_digest=hashlib.sha256(b"screened build").hexdigest(),
        size_bytes=len(b"screened build"),
        secret_screening="passed",  # noqa: S106
    )
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    service = LocalChatService(
        backend=_ArtifactBackend(artifact),
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
        artifact_roots=(artifact_root,),
    )
    response = await service.start(
        owner="local:artifact-restart",
        prompt="Build it",
        provider="codex",
        model=None,
        effort="medium",
        mode="build",
        mcp_enabled=False,
        idempotency_key="artifact-restart",
        safety_digest=build_safety_preview(
            provider="codex", mode="build", mcp_enabled=False
        ).policy_digest,
    )
    run_id = UUID(response["run_id"])
    _ = [
        event
        async for _cursor, event in service.events(
            owner="local:artifact-restart", run_id=run_id, cursor=None
        )
    ]

    restarted = LocalChatService(
        backend=_Backend(),
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
        artifact_roots=(artifact_root,),
    )

    assert restarted.artifact(owner="local:artifact-restart", run_id=run_id) == artifact

    artifact_path.write_bytes(b"tampered build")
    with pytest.raises(LocalChatNotFound, match="local_chat_artifact_invalid"):
        restarted.artifact(owner="local:artifact-restart", run_id=run_id)


@pytest.mark.asyncio
async def test_local_chat_running_run_becomes_interrupted_after_restart(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    backend = _GatedBackend()
    first_service = LocalChatService(
        backend=backend,
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
    )
    response = await first_service.start(
        owner="local:interrupted",
        prompt="Long running work",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="interrupted-once",
    )
    await asyncio.wait_for(backend.started.wait(), timeout=0.2)
    run_id = UUID(response["run_id"])
    pump = first_service._runs[run_id].pump_task
    assert pump is not None
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump

    restarted = LocalChatService(
        backend=_Backend(),
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
        store=store,
    )
    events = [
        event
        async for _cursor, event in restarted.events(
            owner="local:interrupted", run_id=run_id, cursor=None, follow=False
        )
    ]

    assert [event.type for event in events] == ["started", "failed"]
    assert events[-1].payload == {"reason_code": "local_chat_interrupted"}


@pytest.mark.asyncio
async def test_local_chat_shutdown_cancels_live_provider_handle() -> None:
    backend = _CancellableBackend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    response = await service.start(
        owner="local:shutdown",
        prompt="Keep working",
        provider="codex",
        model=None,
        effort="medium",
        mode="chat",
        mcp_enabled=False,
        idempotency_key="shutdown-once",
    )
    run_id = UUID(response["run_id"])

    await service.shutdown()

    assert service._runs[run_id].state == "cancelled"
    assert service._runs[run_id].handle.id in backend.cancelled


@pytest.mark.asyncio
async def test_local_chat_journal_prunes_old_terminal_runs_per_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_chat_module, "_MAX_PERSISTED_RUNS_PER_OWNER", 2)
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    service = LocalChatService(
        backend=_Backend(), cursor_secret=b"c" * 32, clock=lambda: NOW, store=store
    )
    for index in range(3):
        response = await service.start(
            owner="local:retention",
            prompt=f"Run {index}",
            provider="codex",
            model=None,
            effort="medium",
            mode="chat",
            mcp_enabled=False,
            idempotency_key=f"retention-{index}",
        )
        _ = [
            event
            async for _cursor, event in service.events(
                owner="local:retention", run_id=UUID(response["run_id"]), cursor=None
            )
        ]

    with store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM mvp_local_chat_runs WHERE owner = ?",
            ("local:retention",),
        ).fetchone()
    assert count is not None
    assert count["count"] == 2


def test_local_chat_requires_csrf_and_idempotently_starts_this_device_run(
    tmp_path: Path,
) -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    client, headers = _client(tmp_path, "owner", service)
    body = {"prompt": "Hello", "effort": "normal"}

    assert client.post("/api/local-chat/runs", json=body).status_code == 403
    first = client.post(
        "/api/local-chat/runs",
        json=body,
        headers={**headers, "Idempotency-Key": "run-once"},
    )
    replay = client.post(
        "/api/local-chat/runs",
        json=body,
        headers={**headers, "Idempotency-Key": "run-once"},
    )
    conflict = client.post(
        "/api/local-chat/runs",
        json={**body, "prompt": "Changed"},
        headers={**headers, "Idempotency-Key": "run-once"},
    )

    assert first.status_code == replay.status_code == 202
    first_body = first.json()
    replay_body = replay.json()
    assert {key: value for key, value in first_body.items() if key != "state"} == {
        key: value for key, value in replay_body.items() if key != "state"
    }
    assert replay_body["state"] in {"running", "completed"}
    assert first_body["model"] != "Codex default"
    assert first_body["model"]
    assert first_body["storage"] == "this_device"
    assert backend.starts == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_conflict"


def test_local_chat_includes_bounded_conversation_context_in_provider_prompt(
    tmp_path: Path,
) -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    client, headers = _client(tmp_path, "context", service)

    response = client.post(
        "/api/local-chat/runs",
        json={
            "prompt": "Continue",
            "context": [
                {"role": "user", "content": "Build me a website"},
                {"role": "assistant", "content": "I can help shape the website."},
            ],
        },
        headers={**headers, "Idempotency-Key": "context-run"},
    )

    assert response.status_code == 202
    assert backend.last_prompt is not None
    assert "Conversation so far:" in backend.last_prompt
    assert "User: Build me a website" in backend.last_prompt
    assert "Corvus: I can help shape the website." in backend.last_prompt
    assert backend.last_prompt.endswith("User request:\nContinue")


def test_local_build_requires_current_safety_digest(tmp_path: Path) -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    client, headers = _client(tmp_path, "safety-digest", service)
    preview = client.get(
        "/api/local-chat/safety-preview",
        params={"provider": "codex", "mode": "build", "mcp_enabled": False},
    )

    assert preview.status_code == 200
    assert preview.json()["level"] == "protected"
    assert preview.json()["requires_confirmation"] is True

    body = {
        "prompt": "Build a safe project",
        "provider": "codex",
        "mode": "build",
        "mcp_enabled": False,
    }
    missing = client.post(
        "/api/local-chat/runs",
        json=body,
        headers={**headers, "Idempotency-Key": "missing-safety"},
    )
    stale = client.post(
        "/api/local-chat/runs",
        json={**body, "safety_digest": "0" * 64},
        headers={**headers, "Idempotency-Key": "stale-safety"},
    )
    started = client.post(
        "/api/local-chat/runs",
        json={**body, "safety_digest": preview.json()["policy_digest"]},
        headers={**headers, "Idempotency-Key": "verified-safety"},
    )

    assert missing.status_code == 409
    assert missing.json()["detail"] == "safety_digest_mismatch"
    assert stale.status_code == 409
    assert stale.json()["detail"] == "safety_digest_mismatch"
    assert started.status_code == 202
    assert started.json()["safety"] == preview.json()
    assert backend.starts == 1


def test_local_chat_provider_catalog_is_truthful_and_path_free(tmp_path: Path) -> None:
    service = LocalChatService(backend=_Backend(), cursor_secret=b"c" * 32, clock=lambda: NOW)
    client, _headers = _client(tmp_path, "catalog", service)

    response = client.get("/api/local-chat/providers")

    assert response.status_code == 200
    catalog = {entry["id"]: entry for entry in response.json()}
    assert catalog["codex"]["status"] == "ready"
    assert catalog["claude"]["status"] == "unavailable"
    assert catalog["gemini"]["status"] == "unavailable"
    assert catalog["xai"]["status"] == "unavailable"
    assert "path" not in response.text.lower()


def test_local_chat_provider_catalog_rechecks_codex_readiness_on_retry(tmp_path: Path) -> None:
    readiness = {"ready": False}
    readiness_clock = {"now": 0.0}
    probe_calls = 0

    def probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return readiness["ready"]

    service = LocalChatService(
        backend=_Backend(),
        readiness_probes={"codex": probe},
        readiness_clock=lambda: readiness_clock["now"],
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
    )
    client, _headers = _client(tmp_path, "catalog-retry", service)

    unavailable = {entry["id"]: entry for entry in client.get("/api/local-chat/providers").json()}
    assert unavailable["codex"]["status"] == "unavailable"
    assert unavailable["codex"]["models"] == []
    assert unavailable["codex"]["thinking_levels"] == []

    cached = {entry["id"]: entry for entry in client.get("/api/local-chat/providers").json()}
    assert cached["codex"]["status"] == "unavailable"
    assert probe_calls == 1

    readiness["ready"] = True
    readiness_clock["now"] = 10.0
    verified = {entry["id"]: entry for entry in client.get("/api/local-chat/providers").json()}
    assert verified["codex"]["status"] == "ready"
    assert {model["id"] for model in verified["codex"]["models"]} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    }
    assert verified["codex"]["thinking_levels"] == ["low", "medium", "high", "xhigh"]
    assert probe_calls == 2


def test_api_chat_starts_without_any_local_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_chat_module, "_discover_codex_executable", lambda: None)
    monkeypatch.setattr(local_chat_module, "_discover_claude_executable", lambda: None)
    backend = _Backend()
    monkeypatch.setattr(api_module, "ApiChatBackend", lambda **_kwargs: backend)
    credentials = ProviderCredentialService(keyring=_MemoryKeyring())
    token = "bootstrap-api-only"  # noqa: S105
    client = TestClient(
        create_app(
            database=tmp_path / "api-only.sqlite3",
            bootstrap_token=token,
            session_secret=b"s" * 32,
            provider_credentials=credentials,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    connected = client.put(
        "/api/provider-credentials/openai",
        headers={"X-CSRF-Token": csrf},
        json={"credential": "sk-test-api-only"},
    )

    providers = client.get("/api/local-chat/providers")
    started = client.post(
        "/api/local-chat/runs",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "api-only-run"},
        json={
            "prompt": "Hello from an API-only machine",
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "effort": "medium",
            "mode": "chat",
            "mcp_enabled": False,
        },
    )

    assert connected.status_code == 200
    assert providers.status_code == 200
    assert "openai" in {entry["id"] for entry in providers.json()}
    assert started.status_code == 202
    assert backend.starts == 1


def test_provider_credentials_api_is_authenticated_csrf_protected_and_write_only(
    tmp_path: Path,
) -> None:
    backend = _Backend()
    local_chat = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    token = "bootstrap-provider-credentials"  # noqa: S105
    client = TestClient(
        create_app(
            database=tmp_path / "credentials.sqlite3",
            bootstrap_token=token,
            session_secret=b"s" * 32,
            local_chat_service=local_chat,
            provider_credentials=ProviderCredentialService(keyring=_MemoryKeyring()),
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    csrf = client.get("/api/auth/session").json()["csrf_token"]

    denied = client.put(
        "/api/provider-credentials/openai",
        json={"credential": "sk-test-never-return-this"},
    )
    assert denied.status_code == 403

    connected = client.put(
        "/api/provider-credentials/openai",
        headers={"X-CSRF-Token": csrf},
        json={"credential": "sk-test-never-return-this"},
    )
    assert connected.status_code == 200
    assert connected.json() == {
        "provider": "openai",
        "configured": True,
        "source": "keyring",
    }
    assert "sk-test-never-return-this" not in connected.text
    assert client.get("/api/provider-credentials").json()[0]["provider"] == "openai"

    removed = client.delete(
        "/api/provider-credentials/openai",
        headers={"X-CSRF-Token": csrf},
    )
    assert removed.status_code == 200
    assert removed.json()["configured"] is False


def test_local_preferences_are_owner_scoped_versioned_and_applied_to_runs(
    tmp_path: Path,
) -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    client, headers = _client(tmp_path, "preferences", service)

    defaults = client.get("/api/local-chat/preferences")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "version": 0,
        "default_provider": "codex",
        "default_model": None,
        "model_labels": {},
        "default_effort": "medium",
        "default_mode": "chat",
        "mcp_enabled": False,
        "response_tone": "balanced",
        "custom_rules": "",
        "updated_at": None,
    }
    update = {
        "expected_version": 0,
        "default_provider": "codex",
        "default_model": "gpt-5.6-sol",
        "model_labels": {"codex:gpt-5.6-sol": "My Sol model"},
        "default_effort": "high",
        "default_mode": "build",
        "mcp_enabled": True,
        "response_tone": "concise",
        "custom_rules": "Always end with a verification result.",
    }
    assert client.put("/api/local-chat/preferences", json=update).status_code == 403
    invalid_labels = {
        **update,
        "model_labels": {"openai:gpt-5.6-sol": "Unsupported provider"},
    }
    assert (
        client.put("/api/local-chat/preferences", json=invalid_labels, headers=headers).status_code
        == 422
    )
    saved = client.put("/api/local-chat/preferences", json=update, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert saved.json()["custom_rules"] == "Always end with a verification result."

    stale = client.put("/api/local-chat/preferences", json=update, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "preferences_version_conflict"
    assert stale.json()["detail"]["current"]["version"] == 1

    started = client.post(
        "/api/local-chat/runs",
        json={"prompt": "Fix the failing test", "effort": "high"},
        headers={**headers, "Idempotency-Key": "preferences-run"},
    )
    assert started.status_code == 202
    assert backend.last_prompt is not None
    assert "presentation guidance only" in backend.last_prompt
    assert "Always end with a verification result." in backend.last_prompt
    assert backend.last_prompt.endswith("User request:\nFix the failing test")

    newer = {**update, "expected_version": 1, "custom_rules": "Use the newer saved rule."}
    assert client.put("/api/local-chat/preferences", json=newer, headers=headers).status_code == 200
    replay = client.post(
        "/api/local-chat/runs",
        json={"prompt": "Fix the failing test", "effort": "high"},
        headers={**headers, "Idempotency-Key": "preferences-run"},
    )
    assert replay.status_code == 202
    assert replay.json()["run_id"] == started.json()["run_id"]
    assert backend.starts == 1
    changed_prompt = client.post(
        "/api/local-chat/runs",
        json={"prompt": "Fix a different test", "effort": "high"},
        headers={**headers, "Idempotency-Key": "preferences-run"},
    )
    assert changed_prompt.status_code == 409


def test_local_chat_dispatches_to_selected_ready_provider(tmp_path: Path) -> None:
    codex = _Backend()
    claude = _Backend()
    service = LocalChatService(
        backends={"codex": codex, "claude": claude},
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
    )
    client, headers = _client(tmp_path, "providers", service)

    response = client.post(
        "/api/local-chat/runs",
        json={
            "prompt": "Explain this code",
            "provider": "claude",
            "model": "sonnet",
            "effort": "max",
            "mode": "chat",
            "mcp_enabled": False,
        },
        headers={**headers, "Idempotency-Key": "claude-once"},
    )

    assert response.status_code == 202
    assert response.json()["provider"] == "claude"
    assert response.json()["model"] == "sonnet"
    assert codex.starts == 0
    assert claude.starts == 1
    assert claude.last_effort == "max"

    unsupported = client.post(
        "/api/local-chat/runs",
        json={
            "prompt": "Explain this code",
            "provider": "codex",
            "effort": "max",
        },
        headers={**headers, "Idempotency-Key": "codex-max"},
    )
    assert unsupported.status_code == 503
    assert unsupported.json()["detail"] == "provider_effort_unavailable"
    assert codex.starts == 0


def test_local_build_download_is_owner_scoped(tmp_path: Path) -> None:
    archive = tmp_path / "corvus-build.zip"
    archive.write_bytes(b"PK-safe-build")
    artifact = LocalBuildArtifact(
        path=archive,
        download_name="corvus-build.zip",
        sha256_digest="a" * 64,
        size_bytes=archive.stat().st_size,
    )
    service = LocalChatService(
        backend=_ArtifactBackend(artifact),
        cursor_secret=b"c" * 32,
        clock=lambda: NOW,
    )
    owner, headers = _client(tmp_path, "builder", service)
    stranger, _stranger_headers = _client(tmp_path, "not-builder", service)
    preview = owner.get(
        "/api/local-chat/safety-preview",
        params={"provider": "codex", "mode": "build", "mcp_enabled": False},
    ).json()
    started = owner.post(
        "/api/local-chat/runs",
        json={
            "prompt": "Build a small site",
            "provider": "codex",
            "effort": "high",
            "mode": "build",
            "mcp_enabled": False,
            "safety_digest": preview["policy_digest"],
        },
        headers={**headers, "Idempotency-Key": "build-once"},
    )

    assert started.status_code == 202
    assert started.json()["mode"] == "build"
    run_id = started.json()["run_id"]
    owner.get(f"/api/local-chat/runs/{run_id}/events?follow=false")
    download = owner.get(f"/api/local-chat/runs/{run_id}/artifact")
    assert download.status_code == 200
    assert download.content == b"PK-safe-build"
    assert download.headers["content-disposition"].endswith('filename="corvus-build.zip"')
    assert stranger.get(f"/api/local-chat/runs/{run_id}/artifact").status_code == 404

    receipt = owner.get(f"/api/local-chat/runs/{run_id}/safety-receipt")
    assert receipt.status_code == 200
    assert receipt.json()["run_id"] == run_id
    assert receipt.json()["safety"] == preview
    assert receipt.json()["original_project_modified"] is False
    assert receipt.json()["artifact"]["sha256_digest"] == "a" * 64
    assert receipt.json()["artifact"]["secret_screening"] == "not_scanned"  # noqa: S105
    assert stranger.get(f"/api/local-chat/runs/{run_id}/safety-receipt").status_code == 404

    openapi = owner.get("/openapi.json").json()
    event_content = openapi["paths"]["/api/local-chat/runs/{run_id}/events"]["get"]["responses"][
        "200"
    ]["content"]
    artifact_content = openapi["paths"]["/api/local-chat/runs/{run_id}/artifact"]["get"][
        "responses"
    ]["200"]["content"]
    assert "text/event-stream" in event_content
    assert "application/json" not in event_content
    assert "application/zip" in artifact_content
    assert "application/json" not in artifact_content


def test_local_chat_owner_scopes_sse_cursor_and_cancel(tmp_path: Path) -> None:
    backend = _Backend()
    service = LocalChatService(backend=backend, cursor_secret=b"c" * 32, clock=lambda: NOW)
    owner, owner_headers = _client(tmp_path, "owner", service)
    stranger, stranger_headers = _client(tmp_path, "stranger", service)
    started = owner.post(
        "/api/local-chat/runs",
        json={"prompt": "Do not expose sk-secret"},
        headers={**owner_headers, "Idempotency-Key": "owner-run"},
    ).json()
    run_id = started["run_id"]

    stream = owner.get(f"/api/local-chat/runs/{run_id}/events?follow=false")
    events = _sse_events(stream.text)
    cursor = events[0][0]

    assert stream.status_code == 200
    assert [event[1]["type"] for event in events] == ["started", "message", "completed"]
    assert "sk-secret" not in stream.text
    assert (
        owner.get(
            f"/api/local-chat/runs/{run_id}/events?follow=false",
            headers={"Last-Event-ID": cursor + "tampered"},
        ).status_code
        == 400
    )
    assert stranger.get(f"/api/local-chat/runs/{run_id}/events?follow=false").status_code == 404
    cancellation_backend = _CancellableBackend()
    cancellation_service = LocalChatService(
        backend=cancellation_backend,
        cursor_secret=b"d" * 32,
        clock=lambda: NOW,
    )
    cancellation_owner, cancellation_headers = _client(
        tmp_path,
        "cancellation-owner",
        cancellation_service,
    )
    cancellation_stranger, cancellation_stranger_headers = _client(
        tmp_path,
        "cancellation-stranger",
        cancellation_service,
    )
    cancellable = cancellation_owner.post(
        "/api/local-chat/runs",
        json={"prompt": "Wait for cancellation"},
        headers={**cancellation_headers, "Idempotency-Key": "cancel-run"},
    ).json()
    cancel_run_id = cancellable["run_id"]
    assert (
        cancellation_stranger.post(
            f"/api/local-chat/runs/{cancel_run_id}/cancel",
            headers=cancellation_stranger_headers,
        ).status_code
        == 404
    )

    cancelled = cancellation_owner.post(
        f"/api/local-chat/runs/{cancel_run_id}/cancel",
        headers=cancellation_headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "run_id": cancel_run_id,
        "state": "cancelled",
        "accepted": True,
        "reason_code": "agent_run_cancelled",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex launcher layout")
def test_default_local_chat_prefers_npm_native_codex_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_root = tmp_path / "npm"
    wrapper = npm_root / "codex.cmd"
    packaged = tmp_path / "WindowsApps" / "codex.exe"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    for candidate in (wrapper, packaged, native):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"codex")

    candidates = {
        "codex.exe": os.fspath(packaged),
        "codex.cmd": os.fspath(wrapper),
        "codex": os.fspath(wrapper),
    }
    monkeypatch.setattr(
        local_chat_module.shutil,
        "which",
        lambda name: candidates.get(name),
    )

    service = local_chat_module.build_default_local_chat_service(
        scratch_root=tmp_path / "runs",
        cursor_secret=b"c" * 32,
    )

    assert service is not None
    backend = service._backend
    assert isinstance(backend, local_chat_module.CodexLocalChatBackend)
    assert backend._adapter._executable == native.resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex launcher layout")
def test_default_local_chat_finds_user_npm_codex_without_terminal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    npm_root = appdata / "npm"
    wrapper = npm_root / "codex.cmd"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes(b"wrapper")
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"codex")
    monkeypatch.setenv("APPDATA", os.fspath(appdata))
    monkeypatch.setattr(local_chat_module.shutil, "which", lambda _name: None)

    service = local_chat_module.build_default_local_chat_service(
        scratch_root=tmp_path / "runs",
        cursor_secret=b"c" * 32,
    )

    assert service is not None
    backend = service._backend
    assert isinstance(backend, local_chat_module.CodexLocalChatBackend)
    assert backend._adapter._executable == native.resolve()
