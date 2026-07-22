from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from corvus.domain.agent_runtime import AgentRunHandle, ProviderBinding, ProviderDiscoveryQuery
from corvus.infrastructure.agent_runtimes.codex import (
    CodexAdapterError,
    CodexCliAdapter,
    LocalCodexTextRequest,
)
from corvus.mvp.change_review import ChangeReviewService
from corvus.mvp.core import DomainNotFound
from corvus.mvp.repository_workspace import RepositoryWorkspaceService
from corvus.mvp.run_models import RunEvent, RunRecord, RunStatus, StartRunRequest
from corvus.mvp.run_store import RunStore, RunStoreConflict
from corvus.mvp.safety import build_safety_preview
from corvus.mvp.worktrees import WorktreeManager


class RunCoordinatorConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderRunEvent:
    event_type: str
    payload: dict[str, Any]


class RepositoryRunBackend(Protocol):
    async def start(
        self,
        *,
        run_id: str,
        cwd: Path,
        request: StartRunRequest,
        prompt: str,
    ) -> str: ...

    def events(self, handle: str) -> AsyncIterator[ProviderRunEvent]: ...

    async def cancel(self, handle: str) -> bool: ...


class RunSkillProvider(Protocol):
    def instructions(self, tenant_id: str, skill_id: str) -> str: ...


type EventNotifier = Callable[[RunEvent], Awaitable[None]]


class RunCoordinator:
    def __init__(
        self,
        runs: RunStore,
        repositories: RepositoryWorkspaceService,
        worktrees: WorktreeManager,
        review: ChangeReviewService,
        backend: RepositoryRunBackend,
        *,
        skill_provider: RunSkillProvider | None = None,
        event_notifier: EventNotifier | None = None,
    ) -> None:
        self.runs = runs
        self.repositories = repositories
        self.worktrees = worktrees
        self.review = review
        self.backend = backend
        self.skill_provider = skill_provider
        self.event_notifier = event_notifier
        self._handles: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._owners: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        tenant_id: str,
        request: StartRunRequest,
        *,
        retry_of_run_id: str | None = None,
    ) -> RunRecord:
        preview = build_safety_preview(
            provider=request.provider,
            mode=request.mode,
            mcp_enabled=False,
        )
        if not hmac.compare_digest(preview.policy_digest, request.safety_digest):
            raise RunCoordinatorConflict("run_safety_digest_mismatch")
        skill_instructions: str | None = None
        if request.skill_version_id is not None:
            if self.skill_provider is None:
                raise RunCoordinatorConflict("run_skill_unavailable")
            try:
                skill_instructions = self.skill_provider.instructions(
                    tenant_id, request.skill_version_id
                )
            except RuntimeError as exc:
                raise RunCoordinatorConflict("run_skill_unavailable") from exc
        repository = self.repositories.refresh(tenant_id, request.repository_id)
        if repository.snapshot.health != "healthy" or not repository.snapshot.head_sha:
            raise RunCoordinatorConflict("run_repository_unavailable")
        run_id = str(uuid4())
        self.runs.create(
            tenant_id,
            request,
            base_sha=repository.snapshot.head_sha,
            run_id=run_id,
            retry_of_run_id=retry_of_run_id,
        )
        self.runs.add_evidence(
            run_id,
            "safety_policy",
            f"{preview.label} policy locked before Codex started",
            preview.policy_digest,
        )
        base_summary = f"Pinned {repository.display_name} at {repository.snapshot.head_sha[:12]}"
        self.runs.add_evidence(
            run_id,
            "repository_base",
            base_summary,
            hashlib.sha256(base_summary.encode("utf-8")).hexdigest(),
        )
        if skill_instructions is not None:
            skill_summary = f"Active skill {request.skill_version_id} verified for this run"
            self.runs.add_evidence(
                run_id,
                "skill",
                skill_summary,
                hashlib.sha256(skill_instructions.encode("utf-8")).hexdigest(),
            )
        try:
            lease = self.worktrees.create(
                repository,
                run_id,
                repository.snapshot.head_sha,
            )
            handle = await self.backend.start(
                run_id=run_id,
                cwd=lease.root,
                request=request,
                prompt=self._prompt(repository.display_name, request, skill_instructions),
            )
        except asyncio.CancelledError:
            self.runs.transition(tenant_id, run_id, RunStatus.INTERRUPTED)
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.runs.transition(tenant_id, run_id, RunStatus.FAILED)
            raise RunCoordinatorConflict("run_provider_start_failed") from exc
        self._handles[run_id] = handle
        self._owners[run_id] = tenant_id
        self._locks[run_id] = asyncio.Lock()
        running = self.runs.transition(tenant_id, run_id, RunStatus.RUNNING)
        self._tasks[run_id] = asyncio.create_task(
            self._pump(tenant_id, running),
            name=f"corvus-durable-run-{run_id}",
        )
        return running

    async def retry(self, tenant_id: str, run_id: str) -> RunRecord:
        original = self.runs.get(tenant_id, run_id)
        if original.status not in {
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.COMPLETED,
        }:
            raise RunCoordinatorConflict("run_retry_unavailable")
        request = StartRunRequest(
            repository_id=original.repository_id,
            task=original.task,
            provider=original.provider,
            model=original.model,
            effort=original.effort,
            mode=original.mode,
            safety_digest=original.safety_digest,
            skill_version_id=original.skill_version_id,
            output_policy=original.output_policy,
        )
        return await self.start(tenant_id, request, retry_of_run_id=original.id)

    def discard(self, tenant_id: str, run_id: str) -> RunRecord:
        current = self.runs.get(tenant_id, run_id)
        if current.status in {RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PUBLISHING}:
            raise RunCoordinatorConflict("run_discard_active")
        if current.status == RunStatus.PUBLISHED:
            raise RunCoordinatorConflict("run_discard_published")
        if current.status == RunStatus.DISCARDED:
            return current
        try:
            lease = self.worktrees.get(run_id)
        except DomainNotFound:
            if current.status != RunStatus.FAILED:
                raise RunCoordinatorConflict("run_worktree_missing") from None
        else:
            self.worktrees.discard(lease, run_terminal=True)
        return self.runs.transition(tenant_id, run_id, RunStatus.DISCARDED)

    async def cancel(self, tenant_id: str, run_id: str) -> RunRecord:
        current = self.runs.get(tenant_id, run_id)
        if current.status in {
            RunStatus.CANCELLED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
            RunStatus.PUBLISHED,
            RunStatus.DISCARDED,
        }:
            return current
        handle = self._handles.get(run_id)
        if handle is None:
            return self.runs.transition(tenant_id, run_id, RunStatus.INTERRUPTED)
        accepted = await self.backend.cancel(handle)
        if not accepted:
            return self.runs.get(tenant_id, run_id)
        async with self._locks[run_id]:
            current = self.runs.get(tenant_id, run_id)
            if current.status in {RunStatus.PREPARING, RunStatus.RUNNING}:
                return self.runs.transition(tenant_id, run_id, RunStatus.CANCELLED)
            return current

    async def wait(self, tenant_id: str, run_id: str) -> RunRecord:
        owner = self._owners.get(run_id)
        if owner is None or not hmac.compare_digest(owner, tenant_id):
            raise RunCoordinatorConflict("run_not_owned_by_coordinator")
        task = self._tasks.get(run_id)
        if task is not None:
            await task
        return self.runs.get(tenant_id, run_id)

    def recover_interrupted(self) -> tuple[RunRecord, ...]:
        recovered: list[RunRecord] = []
        with self.runs.store.connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, id FROM mvp_runs WHERE status IN ('preparing', 'running')"
            ).fetchall()
        for row in rows:
            recovered.append(
                self.runs.transition(
                    str(row["tenant_id"]),
                    str(row["id"]),
                    RunStatus.INTERRUPTED,
                )
            )
        return tuple(recovered)

    async def _pump(self, tenant_id: str, run: RunRecord) -> None:
        handle = self._handles[run.id]
        saw_terminal = False
        try:
            async for provider_event in self.backend.events(handle):
                event = self.runs.append_event(
                    run.id,
                    provider_event.event_type,
                    provider_event.payload,
                )
                if self.event_notifier is not None:
                    await self.event_notifier(event)
                if provider_event.event_type == "provider.completed":
                    saw_terminal = True
                    changes = self.review.snapshot(self.worktrees.get(run.id).root)
                    completion_payload = json.dumps(
                        provider_event.payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    self.runs.add_evidence(
                        run.id,
                        "provider_completion",
                        "Codex reported that the provider run completed",
                        hashlib.sha256(completion_payload.encode("utf-8")).hexdigest(),
                    )
                    self.runs.add_evidence(
                        run.id,
                        "change_set",
                        (
                            f"Observed {len(changes.files)} changed file"
                            f"{'s' if len(changes.files) != 1 else ''} in the isolated worktree"
                        ),
                        changes.digest,
                    )
                    if run.mode == "build" and not changes.files:
                        self.runs.add_evidence(
                            run.id,
                            "runtime_validation",
                            "Build run completed without producing any reviewable file changes",
                            hashlib.sha256(b"build_completed_without_changes").hexdigest(),
                        )
                        target = RunStatus.FAILED
                    elif run.mode == "build":
                        target = RunStatus.REVIEW_REQUIRED
                    else:
                        target = RunStatus.COMPLETED
                    await self._terminalize(tenant_id, run.id, target)
                elif provider_event.event_type == "provider.failed":
                    saw_terminal = True
                    await self._terminalize(tenant_id, run.id, RunStatus.FAILED)
                elif provider_event.event_type == "provider.cancelled":
                    saw_terminal = True
                    await self._terminalize(tenant_id, run.id, RunStatus.CANCELLED)
            if not saw_terminal:
                self.runs.append_event(
                    run.id,
                    "runtime.interrupted",
                    {"reason_code": "provider_stream_closed_without_terminal"},
                )
                await self._terminalize(tenant_id, run.id, RunStatus.INTERRUPTED)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._stop_and_terminalize(
                    tenant_id,
                    run.id,
                    handle,
                    RunStatus.INTERRUPTED,
                    "run_event_pump_cancelled",
                )
            )
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            await self._stop_and_terminalize(
                tenant_id,
                run.id,
                handle,
                RunStatus.FAILED,
                "run_event_pump_failed",
            )

    async def _stop_and_terminalize(
        self,
        tenant_id: str,
        run_id: str,
        handle: str,
        target: RunStatus,
        reason_code: str,
    ) -> None:
        provider_stopped = False
        try:
            provider_stopped = await self.backend.cancel(handle)
        except Exception:
            provider_stopped = False
        try:
            self.runs.append_event(
                run_id,
                "runtime.interrupted" if target == RunStatus.INTERRUPTED else "runtime.failed",
                {"reason_code": reason_code, "provider_stop_accepted": provider_stopped},
            )
            await self._terminalize(tenant_id, run_id, target)
        except (RunStoreConflict, RuntimeError):
            return

    async def _terminalize(
        self,
        tenant_id: str,
        run_id: str,
        target: RunStatus,
    ) -> None:
        async with self._locks[run_id]:
            current = self.runs.get(tenant_id, run_id)
            if current.status == target:
                return
            if current.status not in {RunStatus.PREPARING, RunStatus.RUNNING}:
                return
            self.runs.transition(tenant_id, run_id, target)

    @staticmethod
    def _prompt(
        repository_name: str,
        request: StartRunRequest,
        skill_instructions: str | None,
    ) -> str:
        prompt = (
            "You are working in a Corvus-managed isolated Git worktree for repository "
            f"{repository_name}. Implement the requested task completely in the current working "
            "directory. Inspect existing project instructions, make focused changes, and run "
            "appropriate checks. Do not commit, push, merge, or open a pull request; Corvus owns "
            "the supervised contribution workflow. Do not access files outside the current "
            "working directory.\n\n"
            f"Output policy: {request.output_policy}\n\n"
            f"Task:\n{request.task}"
        )
        if skill_instructions is not None:
            prompt += f"\n\nAuthorized skill instructions:\n{skill_instructions}"
        return prompt


class CodexWorkspaceBackend:
    def __init__(self, adapter: CodexCliAdapter) -> None:
        self._adapter = adapter
        self._binding: ProviderBinding | None = None
        self._handles: dict[str, AgentRunHandle] = {}

    async def _provider_binding(self) -> ProviderBinding:
        if self._binding is None:
            candidates = await self._adapter.discover(
                ProviderDiscoveryQuery(workspace_id=UUID("39fef4c9-baf0-40c7-bada-9c2bd9165445"))
            )
            if not candidates:
                raise CodexAdapterError("codex_unavailable")
            self._binding = candidates[0].binding
        return self._binding

    async def start(
        self,
        *,
        run_id: str,
        cwd: Path,
        request: StartRunRequest,
        prompt: str,
    ) -> str:
        binding = await self._provider_binding()
        result = await self._adapter.start_local_text(
            binding,
            LocalCodexTextRequest(
                run_id=UUID(run_id),
                prompt=prompt,
                idempotency_key=f"durable:{run_id}",
                deadline=datetime.now(UTC) + timedelta(minutes=20),
                model=request.model,
                effort=request.effort,
                mode="inspect" if request.mode == "chat" else request.mode,
                mcp_enabled=False,
                max_output_bytes=1_000_000,
                workspace=cwd,
                package_artifact=False,
            ),
        )
        identifier = str(result.handle.id)
        self._handles[identifier] = result.handle
        return identifier

    async def events(self, handle: str) -> AsyncIterator[ProviderRunEvent]:
        agent_handle = self._handles.get(handle)
        if agent_handle is None:
            raise CodexAdapterError("codex_handle_unknown")
        async for event in self._adapter.events(agent_handle):
            yield ProviderRunEvent(
                event_type=f"provider.{event.event_type.value}",
                payload=dict(event.redacted_payload),
            )

    async def cancel(self, handle: str) -> bool:
        agent_handle = self._handles.get(handle)
        if agent_handle is None:
            return False
        result = await self._adapter.cancel_local(agent_handle)
        return result.accepted
