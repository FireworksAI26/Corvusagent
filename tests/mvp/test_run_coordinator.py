from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from corvus.mvp.change_review import ChangeReviewService
from corvus.mvp.git_process import GitProcess
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.run_coordinator import (
    ProviderRunEvent,
    RunCoordinator,
    RunCoordinatorConflict,
    RunSkillProvider,
)
from corvus.mvp.run_models import RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore
from corvus.mvp.safety import build_safety_preview
from corvus.mvp.store import SqliteStore
from corvus.mvp.worktrees import WorktreeManager


class FakeBackend:
    def __init__(self, *, change_file: bool = True, gated: bool = False) -> None:
        self.change_file = change_file
        self.gated = gated
        self.cwd: Path | None = None
        self.prompt = ""
        self.cancelled = False
        self.release = asyncio.Event()

    async def start(self, *, run_id: str, cwd: Path, request: StartRunRequest, prompt: str) -> str:
        del request
        self.cwd = cwd
        self.prompt = prompt
        return f"handle-{run_id}"

    async def events(self, handle: str):  # type: ignore[no-untyped-def]
        del handle
        yield ProviderRunEvent(event_type="provider.started", payload={"status": "started"})
        if self.gated:
            await self.release.wait()
        if self.cancelled:
            yield ProviderRunEvent(event_type="provider.cancelled", payload={})
            return
        if self.change_file:
            assert self.cwd is not None
            (self.cwd / "feature.txt").write_text("built\n", encoding="utf-8")
        yield ProviderRunEvent(event_type="provider.completed", payload={"status": "completed"})

    async def cancel(self, handle: str) -> bool:
        del handle
        self.cancelled = True
        self.release.set()
        return True


class FakeSkills:
    def instructions(self, tenant_id: str, skill_id: str) -> str:
        assert tenant_id == "local"
        assert skill_id == "skill-1"
        return "Always run the focused verification command."


def _git() -> GitProcess:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    return GitProcess(Path(executable))


def _run(git: GitProcess, cwd: Path, *args: str) -> str:
    result = git.run(cwd, tuple(args))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def _coordinator(
    tmp_path: Path,
    backend: FakeBackend,
    skill_provider: RunSkillProvider | None = None,
) -> tuple[RunCoordinator, object, RunStore]:
    git = _git()
    source = tmp_path / "source"
    source.mkdir()
    _run(git, source, "init", "--initial-branch=main")
    _run(git, source, "config", "user.email", "corvus@example.test")
    _run(git, source, "config", "user.name", "Corvus Tests")
    (source / "README.md").write_text("initial\n", encoding="utf-8")
    _run(git, source, "add", "--", "README.md")
    _run(git, source, "commit", "-m", "initial")
    store = SqliteStore(tmp_path / "corvus.sqlite3")
    repositories = RepositoryWorkspaceService(store, git)
    repository = repositories.register_local("local", source, "Source")
    runs = RunStore(store)
    coordinator = RunCoordinator(
        runs,
        repositories,
        WorktreeManager(
            store,
            git,
            root=tmp_path / "worktrees",
            ownership_secret=b"worktree-test-secret",
        ),
        ChangeReviewService(git),
        backend,
        skill_provider=skill_provider,
    )
    return coordinator, repository, runs


def _request(repository_id: str, *, mode: str = "build") -> StartRunRequest:
    preview = build_safety_preview(provider="codex", mode=mode, mcp_enabled=False)
    return StartRunRequest(
        repository_id=repository_id,
        task="Implement a focused feature",
        model="gpt-5.6-codex",
        effort="high",
        mode=mode,  # type: ignore[arg-type]
        safety_digest=preview.policy_digest,
        output_policy="prepare_contribution" if mode == "build" else "report_only",
    )


@pytest.mark.asyncio
async def test_run_uses_exact_worktree_and_persists_events_before_notification(
    tmp_path: Path,
) -> None:
    backend = FakeBackend(change_file=True)
    coordinator, repository, runs = _coordinator(tmp_path, backend)
    observed_sequences: list[int] = []

    async def notified(event):  # type: ignore[no-untyped-def]
        persisted = runs.events("local", event.run_id)
        assert persisted[-1].sequence == event.sequence
        observed_sequences.append(event.sequence)

    coordinator.event_notifier = notified
    started = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    completed = await coordinator.wait("local", started.id)

    assert backend.cwd is not None
    assert backend.cwd.name == started.id
    assert "Do not commit, push, merge, or open a pull request" in backend.prompt
    assert "Implement a focused feature" in backend.prompt
    assert completed.status == RunStatus.REVIEW_REQUIRED
    assert observed_sequences == [1, 2]
    assert [event.event_type for event in runs.events("local", started.id)] == [
        "provider.started",
        "provider.completed",
    ]
    evidence = runs.evidence("local", started.id)
    assert len(evidence) == 4
    assert {item.kind for item in evidence} == {
        "safety_policy",
        "repository_base",
        "provider_completion",
        "change_set",
    }
    change_set = next(item for item in evidence if item.kind == "change_set")
    assert change_set.summary == "Observed 1 changed file in the isolated worktree"


@pytest.mark.asyncio
async def test_read_only_completion_has_no_review_state(tmp_path: Path) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, _ = _coordinator(tmp_path, backend)

    started = await coordinator.start("local", _request(repository.id, mode="chat"))  # type: ignore[attr-defined]
    completed = await coordinator.wait("local", started.id)

    assert completed.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_build_completion_without_changes_is_failed(tmp_path: Path) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, runs = _coordinator(tmp_path, backend)

    started = await coordinator.start("local", _request(repository.id))
    completed = await coordinator.wait("local", started.id)

    assert completed.status == RunStatus.FAILED
    evidence = runs.evidence("local", started.id)
    evidence_by_kind = {item.kind: item for item in evidence}
    assert {"change_set", "runtime_validation"} <= evidence_by_kind.keys()
    assert evidence_by_kind["runtime_validation"].summary == (
        "Build run completed without producing any reviewable file changes"
    )


@pytest.mark.asyncio
async def test_preflight_rejects_wrong_safety_digest_before_creating_run(tmp_path: Path) -> None:
    backend = FakeBackend()
    coordinator, repository, runs = _coordinator(tmp_path, backend)
    request = _request(repository.id).model_copy(update={"safety_digest": "0" * 64})  # type: ignore[attr-defined]

    with pytest.raises(RunCoordinatorConflict, match="run_safety_digest_mismatch"):
        await coordinator.start("local", request)
    assert runs.list("local") == ()


@pytest.mark.asyncio
async def test_cancel_terminates_only_owned_backend_handle(tmp_path: Path) -> None:
    backend = FakeBackend(gated=True)
    coordinator, repository, _ = _coordinator(tmp_path, backend)
    started = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    cancelled = await coordinator.cancel("local", started.id)
    terminal = await coordinator.wait("local", started.id)

    assert backend.cancelled is True
    assert cancelled.status == RunStatus.CANCELLED
    assert terminal.status == RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_run_pump_failure_cancels_provider_before_marking_failed(tmp_path: Path) -> None:
    backend = FakeBackend(gated=True)
    coordinator, repository, runs = _coordinator(tmp_path, backend)

    async def fail_notification(_event):  # type: ignore[no-untyped-def]
        raise RuntimeError("notification failed")

    coordinator.event_notifier = fail_notification
    started = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    terminal = await coordinator.wait("local", started.id)

    assert backend.cancelled is True
    assert terminal.status == RunStatus.FAILED
    failure = runs.events("local", started.id)[-1]
    assert failure.event_type == "runtime.failed"
    assert failure.payload == {
        "reason_code": "run_event_pump_failed",
        "provider_stop_accepted": True,
    }


@pytest.mark.asyncio
async def test_run_pump_task_cancellation_propagates_after_stopping_provider(
    tmp_path: Path,
) -> None:
    backend = FakeBackend(gated=True)
    coordinator, repository, runs = _coordinator(tmp_path, backend)
    started = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    task = coordinator._tasks[started.id]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.cancelled is True
    assert runs.get("local", started.id).status == RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_retry_creates_new_worktree_with_lineage(tmp_path: Path) -> None:
    first_backend = FakeBackend(change_file=False)
    coordinator, repository, runs = _coordinator(tmp_path, first_backend)
    original = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    original = await coordinator.wait("local", original.id)

    retried = await coordinator.retry("local", original.id)
    retried = await coordinator.wait("local", retried.id)

    assert retried.id != original.id
    assert retried.retry_of_run_id == original.id
    assert len(runs.list("local")) == 2


@pytest.mark.asyncio
async def test_discard_removes_terminal_managed_worktree(tmp_path: Path) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, _ = _coordinator(tmp_path, backend)
    started = await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    terminal = await coordinator.wait("local", started.id)
    assert backend.cwd is not None and backend.cwd.exists()

    discarded = coordinator.discard("local", terminal.id)

    assert discarded.status == RunStatus.DISCARDED
    assert not backend.cwd.exists()


@pytest.mark.asyncio
async def test_wait_rejects_cross_tenant_cache_access(tmp_path: Path) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, _ = _coordinator(tmp_path, backend)
    started = await coordinator.start(
        "local",
        _request(repository.id, mode="chat"),  # type: ignore[attr-defined]
    )

    with pytest.raises(RunCoordinatorConflict, match="run_not_owned_by_coordinator"):
        await coordinator.wait("another-tenant", started.id)
    assert (await coordinator.wait("local", started.id)).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_discard_allows_failed_start_without_a_worktree_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, runs = _coordinator(tmp_path, backend)

    def fail_create(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated startup failure")

    monkeypatch.setattr(coordinator.worktrees, "create", fail_create)
    with pytest.raises(RunCoordinatorConflict, match="run_provider_start_failed"):
        await coordinator.start("local", _request(repository.id))  # type: ignore[attr-defined]
    failed = runs.list("local")[0]

    assert coordinator.discard("local", failed.id).status == RunStatus.DISCARDED


@pytest.mark.asyncio
async def test_selected_skill_instructions_are_tenant_scoped_and_added_to_prompt(
    tmp_path: Path,
) -> None:
    backend = FakeBackend(change_file=False)
    coordinator, repository, runs = _coordinator(tmp_path, backend, FakeSkills())
    request = _request(repository.id).model_copy(update={"skill_version_id": "skill-1"})  # type: ignore[attr-defined]

    started = await coordinator.start("local", request)
    await coordinator.wait("local", started.id)

    assert "Authorized skill instructions:" in backend.prompt
    assert "Always run the focused verification command." in backend.prompt
    skill_evidence = next(
        item for item in runs.evidence("local", started.id) if item.kind == "skill"
    )
    assert skill_evidence.summary == "Active skill skill-1 verified for this run"
