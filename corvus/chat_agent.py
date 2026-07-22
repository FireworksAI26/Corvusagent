from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from corvus.context import ContextOwner, ContextProvenanceSink
from corvus.conversations import AgentRunContext, ConversationError
from corvus.interactive import AgentEvent, InteractiveAgent
from corvus.models import DeliveryBundle, ModelMessage, RunEvent
from corvus.providers import ModelProviderClient


class CodingWorkflowLike(Protocol):
    async def execute(self, prompt: str, project: Path) -> tuple[UUID, DeliveryBundle | None]: ...

    def events(self, run_id: UUID) -> list[RunEvent]: ...


class ChatAgent:
    """Adapt the interactive model and isolated build workflow to a chat runner."""

    CONFIGURATION_MESSAGE = (
        "No model provider is configured yet. Run `corvus run --setup` for the provider wizard, "
        "use `corvus model login` to sign in with ChatGPT through the official Codex CLI, or "
        "configure an API endpoint with `corvus model add`."
    )
    PROVIDER_FAILURE_MESSAGE = (
        "The configured model provider did not return a response. Check it with "
        "`corvus doctor`; for Codex / ChatGPT, run `corvus model status` or "
        "`corvus model login`, then retry this message."
    )

    def __init__(
        self,
        provider: ModelProviderClient | None,
        *,
        provenance: ContextProvenanceSink,
        workflow: CodingWorkflowLike | None = None,
        project: Path | None = None,
        allow_subagents: bool = False,
        max_subagents: int = 2,
        build_unavailable_reason: str | None = None,
    ) -> None:
        self.provider = provider
        self.provenance = provenance
        self.workflow = workflow
        self.project = project.resolve() if project is not None else None
        self.allow_subagents = allow_subagents
        self.max_subagents = max_subagents
        self.build_unavailable_reason = build_unavailable_reason
        self._interactive = (
            InteractiveAgent(
                provider,
                provenance=self.provenance,
                max_subagents=max_subagents,
            )
            if provider is not None
            else None
        )

    def set_provider(
        self,
        provider: ModelProviderClient,
        *,
        workflow: CodingWorkflowLike | None,
    ) -> None:
        """Replace the provider used by future turns after the runtime becomes idle."""

        interactive = InteractiveAgent(
            provider,
            provenance=self.provenance,
            max_subagents=self.max_subagents,
        )
        self.provider = provider
        self.workflow = workflow
        self._interactive = interactive

    def set_allow_subagents(self, enabled: bool) -> None:
        """Explicitly enable or disable delegation for subsequent root turns."""

        self.allow_subagents = enabled

    def set_subagents_enabled(self, enabled: bool) -> None:
        """Alias used by conversational command surfaces."""

        self.set_allow_subagents(enabled)

    async def __call__(self, context: AgentRunContext) -> str:
        workflow = self.workflow
        interactive = self._interactive
        build_prompt = self._build_prompt(context.prompt)
        if build_prompt is not None:
            return await self._build(context, build_prompt, workflow)
        if interactive is None:
            await context.emit(
                "configuration.required",
                {"reason": "no model provider is configured"},
            )
            return self.CONFIGURATION_MESSAGE
        history = self._history(context)

        async def emit(event: AgentEvent) -> None:
            await self._emit_interactive_event(context, event)

        async def spawn(task: str) -> str | None:
            try:
                result = await context.spawn_subagent(task)
            except ConversationError as exc:
                await context.emit("delegation.error", {"error": str(exc), "task": task})
                return f"Subagent unavailable: {exc}"
            if result.ok:
                return result.output
            return f"Subagent failed: {result.error or 'no result'}"

        owner = (
            ContextOwner.subagent(context.run_id)
            if context.is_subagent
            else ContextOwner.root(context.run_id)
        )
        response = await interactive.respond(
            context.prompt,
            history,
            emit,
            owner=owner,
            allow_subagents=self.allow_subagents and not context.is_subagent,
            spawn_subagent=spawn if not context.is_subagent else None,
        )
        return response or self.PROVIDER_FAILURE_MESSAGE

    async def _build(
        self,
        context: AgentRunContext,
        prompt: str,
        workflow: CodingWorkflowLike | None,
    ) -> str:
        if context.is_subagent:
            await context.emit(
                "build.blocked",
                {"reason": "build commands are restricted to root chat turns"},
            )
            return "Build commands are available only in the root chat, not delegated subagents."
        if not prompt:
            return "Usage: `/build DESCRIBE THE PROJECT CHANGE`"
        if workflow is None:
            reason = self.build_unavailable_reason
            await context.emit(
                "build.blocked",
                {"reason": reason or "the isolated coding workflow is not configured"},
            )
            if reason is not None:
                return f"Isolated /build is unavailable: {reason} No project files were changed."
            return (
                "The isolated build workflow is not configured for this session. "
                "Configure a model provider and choose Docker or Podman with "
                "`corvus run --setup`, then restart Corvus before using `/build`."
            )
        if self.project is None:
            await context.emit(
                "build.blocked",
                {"reason": "no project directory is selected"},
            )
            return "No project directory is selected for this chat, so the build was not started."
        await context.emit(
            "build.started",
            {"project": str(self.project), "prompt": prompt},
        )
        try:
            run_id, bundle = await workflow.execute(prompt, self.project)
            events = workflow.events(run_id)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            await context.emit("build.blocked", {"reason": reason})
            return f"Build could not start: {reason}. No project files were changed."
        for event in events:
            payload: dict[str, object] = {
                "workflow_event": event.event_type,
                "workflow_run_id": str(run_id),
                "phase": event.phase.value,
                "sequence": event.sequence,
            }
            event_reason = event.payload.get("reason")
            if isinstance(event_reason, str):
                payload["reason"] = event_reason
            await context.emit("build.progress", payload)
        if bundle is None:
            reason = self._blocked_reason(events)
            await context.emit(
                "build.blocked",
                {"workflow_run_id": str(run_id), "reason": reason},
            )
            return (
                f"Build blocked in isolation (run {run_id}): {reason}. "
                "No project files were changed."
            )
        await context.emit(
            "build.completed",
            {
                "workflow_run_id": str(run_id),
                "bundle_id": str(bundle.id),
                "manifest_digest": bundle.manifest_digest,
                "changed_files": list(bundle.changed_files),
            },
        )
        return (
            f"Build completed and verified in isolation. Bundle {bundle.id} is ready for review; "
            f"run `corvus review {bundle.id}` to inspect it. No project changes have been applied."
        )

    @staticmethod
    def _build_prompt(prompt: str) -> str | None:
        stripped = prompt.strip()
        command, separator, remainder = stripped.partition(" ")
        if command.casefold() != "/build":
            return None
        return remainder.strip() if separator else ""

    @staticmethod
    def _history(context: AgentRunContext) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for message in context.history:
            if message.id == context.message_id:
                continue
            role: Literal["user", "assistant"] = (
                "user" if message.role.value == "user" else "assistant"
            )
            messages.append(ModelMessage(role=role, content=message.content))
        return messages

    @staticmethod
    async def _emit_interactive_event(context: AgentRunContext, event: AgentEvent) -> None:
        if event.type.startswith("agent."):
            event_name = event.type.removeprefix("agent.")
        else:
            event_name = f"delegation.{event.type.removeprefix('subagent.')}"
        payload: dict[str, object] = {}
        if event.text:
            payload["text"] = event.text
        if event.subagent_id is not None:
            payload["subagent_id"] = str(event.subagent_id)
        if event.metadata:
            payload["metadata"] = dict(event.metadata)
        await context.emit(event_name, payload)

    @staticmethod
    def _blocked_reason(events: list[RunEvent]) -> str:
        for event in reversed(events):
            reason = event.payload.get("reason")
            if isinstance(reason, str) and reason:
                return reason
        return "verification or packaging did not complete"


ChatAgentRunner = ChatAgent
