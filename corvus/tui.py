from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar
from uuid import UUID

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    Tree,
)

from corvus.conversations import (
    ConversationError,
    ConversationEvent,
    ConversationRunner,
    ConversationRuntime,
    ConversationSnapshot,
    SubagentPolicy,
)


@dataclass(frozen=True)
class LiveThinkingOption:
    """A catalog preset plus its provider-specific native, fallback, or unavailable route."""

    id: str
    label: str
    effective_id: str | None
    detail: str


@dataclass(frozen=True)
class LiveModelOption:
    """Credential-free model metadata that is safe to render in the live TUI."""

    id: str
    label: str
    thinking: tuple[LiveThinkingOption, ...] = ()


@dataclass(frozen=True)
class LiveProviderOption:
    """Credential-free provider metadata that is safe to render in the live TUI."""

    name: str
    label: str
    models: tuple[LiveModelOption, ...]
    selected_model: str | None
    configured: bool = True
    selected_thinking: str | None = None


@dataclass(frozen=True)
class LiveModelState:
    """Sanitized provider/model selection state supplied by the application controller."""

    providers: tuple[LiveProviderOption, ...]
    active_provider: str | None
    active_model: str | None
    active_thinking: str | None = None


class LiveModelSelectionError(RuntimeError):
    """A controller failure whose fixed, sanitized message may be rendered to the user."""


class LiveModelController(Protocol):
    """Atomically replace the live provider without exposing configuration or credentials."""

    def state(self) -> LiveModelState: ...

    async def activate(
        self,
        provider_name: str,
        model: str,
        thinking: str,
    ) -> LiveModelState: ...


_NO_PROVIDER = "__corvus_no_provider__"
_NO_MODEL = "__corvus_no_model__"
_NO_THINKING = "__corvus_no_thinking__"
_WidgetT = TypeVar("_WidgetT", bound=Widget)


class CorvusApp(App[None]):
    """Live multi-chat terminal for the bounded conversation runtime."""

    TITLE = "Corvus"
    SUB_TITLE = "chat immediately | work in background | approve before delivery"

    def query_one_optional(self, selector: str, expect_type: type[_WidgetT]) -> _WidgetT | None:
        try:
            return self.query_one(selector, expect_type)
        except NoMatches:
            return None

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 30; border-right: solid $primary; padding: 0 1; }
    #sidebar-title { height: 1; text-style: bold; }
    #new-chat, #subagents { width: 1fr; margin: 0 0 1 0; }
    #chat-list { height: 1fr; border: round $surface; }
    #agent-tree { height: 12; border: round $surface; }
    #main { width: 1fr; }
    #status { height: 3; padding: 0 1; background: $surface; }
    #route-bar {
        height: 11;
        margin: 0 1;
        padding: 0 1;
        border: round $accent;
        background: $panel;
    }
    #route-title { height: 1; text-style: bold; color: $accent; }
    #model-controls { height: 3; background: transparent; align-vertical: middle; }
    #provider-select { width: 2fr; margin-right: 1; }
    #model-select { width: 2fr; margin-right: 1; }
    #model-apply { width: 18; }
    #thinking-controls { height: 3; background: transparent; align-vertical: middle; }
    #thinking-label { width: 17; }
    #thinking-select { width: 28; margin-right: 1; }
    #thinking-status { width: 1fr; height: 3; color: $warning; }
    .model-label { width: auto; margin-right: 1; text-style: bold; color: $text; }
    #route-bar SelectCurrent { border: tall $accent; background: $surface; }
    #route-bar SelectCurrent .arrow { color: $accent; text-style: bold; }
    #thinking-select SelectCurrent { border: tall $warning; }
    #thinking-select SelectCurrent .arrow { color: $warning; }
    #model-status { height: 2; color: $text-muted; background: transparent; }
    #activity-shell {
        height: 8;
        margin: 0 1;
        border: round $secondary;
        background: $surface;
    }
    #activity-title { height: 1; padding: 0 1; text-style: bold; color: $secondary; }
    #activity { height: 1fr; padding: 0 1; color: $text-muted; }
    #log { height: 1fr; padding: 1 2; }
    #stream { height: auto; max-height: 8; padding: 0 2; color: $accent; }
    #prompt { dock: bottom; margin: 0 1 1 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("ctrl+c", "cancel_active", "Cancel chat"),
        ("ctrl+n", "new_chat", "New chat"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        project: Path | None = None,
        allow_subagents: bool = False,
        max_subagents: int = 2,
        model_controller: LiveModelController | None = None,
        sandbox_backend: str = "none",
    ) -> None:
        super().__init__()
        self.project = (project or Path.cwd()).resolve()
        self.runner = runner
        self.model_controller = model_controller
        self.sandbox_backend = (
            sandbox_backend if sandbox_backend in {"docker", "podman"} else "none"
        )
        self._model_state_error = False
        self.model_state = self._load_initial_model_state()
        self.max_subagents = max_subagents
        self.runtime = ConversationRuntime(
            runner,
            subagents=SubagentPolicy(
                enabled=allow_subagents,
                max_concurrency=max_subagents,
                max_per_run=max_subagents,
                max_cost_usd=Decimal(str(max_subagents)),
            ),
        )
        self.subagents_enabled = allow_subagents
        self.active_chat_id: UUID | None = None
        self.chat_titles: dict[UUID, str] = {}
        self.chat_logs: dict[UUID, list[str]] = {}
        self.activity_logs: dict[UUID, list[str]] = {}
        self.stream_buffers: dict[UUID, str] = {}
        self.item_chats: dict[str, UUID] = {}
        self.agent_nodes: dict[UUID, Any] = {}
        self._event_worker_started = False
        self._subagent_consent_pending = False
        self._model_switching = False
        self._model_feedback: str | None = (
            "Provider selection is unavailable in this session."
            if self._model_state_error
            else None
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("Chats", id="sidebar-title")
                with Horizontal():
                    yield Button("New", id="new-chat", variant="primary")
                    yield Button(
                        "Agents: ON" if self.subagents_enabled else "Agents: OFF",
                        id="subagents",
                    )
                yield ListView(id="chat-list")
                yield Tree("Agent activity", id="agent-tree")
            with Vertical(id="main"):
                yield Static("Starting Corvus...", id="status")
                with Vertical(id="route-bar"):
                    yield Static(
                        "MODEL ROUTE - click a bordered dropdown or focus it and press Enter",
                        id="route-title",
                        markup=False,
                    )
                    with Horizontal(id="model-controls"):
                        yield Label(
                            "PROVIDER [v]",
                            id="provider-label",
                            classes="model-label",
                            markup=False,
                        )
                        yield Select(
                            self._provider_select_options(),
                            value=self._initial_provider_value(),
                            allow_blank=False,
                            id="provider-select",
                            disabled=(
                                self.model_controller is None or not self.model_state.providers
                            ),
                        )
                        yield Label(
                            "MODEL [v]",
                            id="model-label",
                            classes="model-label",
                            markup=False,
                        )
                        yield Select(
                            self._model_select_options(self._initial_provider_value()),
                            value=self._initial_model_value(),
                            allow_blank=False,
                            id="model-select",
                            disabled=(
                                self.model_controller is None or not self.model_state.providers
                            ),
                        )
                        yield Button(
                            "Apply route",
                            id="model-apply",
                            variant="primary",
                            disabled=True,
                        )
                    with Horizontal(id="thinking-controls"):
                        yield Label(
                            "THINKING [v]",
                            id="thinking-label",
                            classes="model-label",
                            markup=False,
                        )
                        yield Select(
                            self._thinking_select_options(
                                self._initial_provider_value(),
                                self._initial_model_value(),
                            ),
                            value=self._initial_thinking_value(),
                            allow_blank=False,
                            id="thinking-select",
                            disabled=(
                                self.model_controller is None or not self.model_state.providers
                            ),
                        )
                        yield Static(
                            self._thinking_summary(),
                            id="thinking-status",
                            markup=False,
                        )
                    yield Static(self._model_summary(), id="model-status", markup=False)
                with Vertical(id="activity-shell"):
                    yield Static(
                        "THINKING / ACTIVITY - status summaries only; no hidden reasoning",
                        id="activity-title",
                        markup=False,
                    )
                    yield RichLog(
                        id="activity",
                        wrap=True,
                        markup=False,
                        max_lines=200,
                    )
                yield RichLog(id="log", wrap=True, markup=True, max_lines=2_000)
                yield Static("", id="stream")
                yield Input(
                    placeholder="Message Corvus; /new, /build, /subagents on, /cancel, /help",
                    id="prompt",
                )
        yield Footer()

    async def on_mount(self) -> None:
        await self._create_chat("Chat 1")
        self.run_worker(
            self._consume_events(),
            name="conversation-events",
            group="runtime",
            exclusive=False,
            exit_on_error=False,
        )
        self._event_worker_started = True
        self._append_log(
            self._require_active(),
            "[bold cyan]Corvus[/bold cyan] Ready. Type a message now. "
            "Use [bold]/build REQUEST[/bold] for sandbox work or "
            "[bold]/new[/bold] for another concurrent chat.",
        )
        self.query_one(Input).focus()

    async def on_unmount(self) -> None:
        await self.runtime.close()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if value.startswith("/") and await self._handle_command(value):
            return
        chat_id = self._require_active()
        self._append_log(chat_id, f"[bold green]You[/bold green] {escape(value)}")
        try:
            await self.runtime.send_message(chat_id, value)
        except ConversationError as exc:
            self._append_log(chat_id, f"[red]Message rejected: {escape(str(exc))}[/red]")
        await self._refresh_status()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-chat":
            await self.action_new_chat()
        elif event.button.id == "subagents":
            if self._subagent_consent_pending:
                await self._set_subagents(True, confirmed=True)
            else:
                await self._set_subagents(not self.subagents_enabled)
        elif event.button.id == "model-apply":
            await self._apply_model_selection()

    async def on_select_changed(self, event: Select.Changed) -> None:
        provider_select = self.query_one_optional("#provider-select", Select)
        model_select = self.query_one_optional("#model-select", Select)
        thinking_select = self.query_one_optional("#thinking-select", Select)
        if provider_select is None or model_select is None or thinking_select is None:
            return
        if event.select.id == "provider-select" and isinstance(event.value, str):
            self._model_feedback = None
            self._populate_models(event.value)
            await self._sync_model_controls()
        elif event.select.id == "model-select" and isinstance(event.value, str):
            self._model_feedback = None
            provider_value = provider_select.value
            if isinstance(provider_value, str):
                self._populate_thinking(provider_value, event.value)
            await self._sync_model_controls()
        elif event.select.id == "thinking-select":
            self._model_feedback = None
            await self._sync_model_controls()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id and event.item.id in self.item_chats:
            self.active_chat_id = self.item_chats[event.item.id]
            self._render_active_chat()
            await self._refresh_status()

    async def action_new_chat(self) -> None:
        await self._create_chat(f"Chat {len(self.chat_titles) + 1}")

    async def action_cancel_active(self) -> None:
        if self.active_chat_id is None:
            return
        await self.runtime.cancel_chat(self.active_chat_id)
        self._append_log(self.active_chat_id, "[yellow]Chat and active work cancelled.[/yellow]")
        await self._refresh_status()

    async def _handle_command(self, value: str) -> bool:
        command, _, argument = value.partition(" ")
        command = command.lower()
        if command == "/new":
            await self._create_chat(argument.strip() or f"Chat {len(self.chat_titles) + 1}")
            return True
        if command == "/cancel":
            await self.action_cancel_active()
            return True
        if command == "/subagents":
            normalized = argument.strip().lower()
            if normalized not in {"on", "off", "confirm"}:
                self._append_log(
                    self._require_active(),
                    "[yellow]Usage: /subagents on|off|confirm[/yellow]",
                )
            else:
                await self._set_subagents(
                    normalized != "off",
                    confirmed=normalized == "confirm",
                )
            return True
        if command == "/chats":
            names = ", ".join(self.chat_titles.values())
            self._append_log(self._require_active(), f"[cyan]Chats:[/cyan] {escape(names)}")
            return True
        if command == "/help":
            self._append_log(
                self._require_active(),
                "[cyan]/new [name][/cyan], [cyan]/build REQUEST[/cyan], "
                "[cyan]/subagents on|off[/cyan], [cyan]/cancel[/cyan], "
                "[cyan]/chats[/cyan]. Messages sent while busy are queued automatically.",
            )
            return True
        return False

    async def _set_subagents(self, enabled: bool, *, confirmed: bool = False) -> None:
        if enabled and not self.subagents_enabled:
            if getattr(self.runner, "provider", object()) is None:
                self._append_log(
                    self._require_active(),
                    "[yellow]Configure a model provider before enabling subagents.[/yellow]",
                )
                return
            if not confirmed:
                self._subagent_consent_pending = True
                self.query_one("#subagents", Button).label = "Confirm agents"
                self._append_log(
                    self._require_active(),
                    "[yellow]Subagents share this chat's context with your model provider and may "
                    f"increase API usage. This window allows at most {self.max_subagents} "
                    "non-recursive analysis children per root turn with a shared bounded "
                    "reservation. They cannot use tools, "
                    "build or change files, access credentials, approve delivery, relax policy, "
                    "or spawn children. Type /subagents confirm or press Confirm agents.[/yellow]",
                )
                return
        await self.runtime.set_subagents_enabled(enabled)
        self.subagents_enabled = enabled
        self._subagent_consent_pending = False
        setter = getattr(self.runner, "set_subagents_enabled", None)
        if callable(setter):
            setter(enabled)
        self.query_one("#subagents", Button).label = "Agents: ON" if enabled else "Agents: OFF"
        if self.active_chat_id is not None:
            if enabled:
                message = (
                    f"[yellow]Subagents ON for this window: max {self.max_subagents} per turn, "
                    "analysis only. "
                    "Current chat context may be shared with your configured provider.[/yellow]"
                )
            else:
                message = (
                    "[yellow]Subagents OFF. Active delegated analysis was cancelled; root tasks "
                    "continue. Completed provider calls cannot be undone.[/yellow]"
                )
            self._append_log(self.active_chat_id, message)
        await self._refresh_status()

    async def _create_chat(self, title: str) -> None:
        snapshot = await self.runtime.create_chat(title)
        self.chat_titles[snapshot.id] = snapshot.title
        self.chat_logs[snapshot.id] = []
        self.activity_logs[snapshot.id] = ["Ready for a message."]
        self.stream_buffers[snapshot.id] = ""
        item_id = f"chat-{snapshot.id.hex}"
        self.item_chats[item_id] = snapshot.id
        item = ListItem(Label(snapshot.title), id=item_id)
        chat_list = self.query_one("#chat-list", ListView)
        await chat_list.append(item)
        self.active_chat_id = snapshot.id
        chat_list.index = len(self.chat_titles) - 1
        self._render_active_chat()
        await self._refresh_status(snapshot)

    async def _consume_events(self) -> None:
        async for event in self.runtime.events():
            self._record_event(event)
            if event.chat_id == self.active_chat_id or event.chat_id is None:
                self._render_event(event)
            await self._refresh_status()

    def _record_event(self, event: ConversationEvent) -> None:
        activity_chat = event.chat_id or self.active_chat_id
        activity = self._activity_summary(event)
        if activity_chat is not None and activity is not None:
            self._append_activity(activity_chat, activity)
        if event.chat_id is None:
            return
        if event.event_type == "assistant.message":
            content = str(event.payload.get("content", ""))
            self.chat_logs.setdefault(event.chat_id, []).append(
                f"[bold cyan]Corvus[/bold cyan] {escape(content)}"
            )
            self.stream_buffers[event.chat_id] = ""
        elif event.event_type == "run.queued":
            self.chat_logs.setdefault(event.chat_id, []).append(
                "[dim]Task queued; input remains available.[/dim]"
            )
        elif event.event_type in {"run.failed", "run.timed_out"}:
            detail = event.payload.get("error", event.event_type)
            self.chat_logs.setdefault(event.chat_id, []).append(f"[red]{escape(str(detail))}[/red]")

    def _render_event(self, event: ConversationEvent) -> None:
        if event.chat_id is not None and event.chat_id != self.active_chat_id:
            return
        if event.event_type == "assistant.message":
            if event.chat_id is None:
                return
            log = self.query_one("#log", RichLog)
            log.write(self.chat_logs[event.chat_id][-1])
            self.query_one("#stream", Static).update("")
        elif event.event_type == "agent.delta" and event.chat_id is not None:
            text = str(event.payload.get("text", ""))
            self.stream_buffers[event.chat_id] = self.stream_buffers.get(event.chat_id, "") + text
            self.query_one("#stream", Static).update(
                "Corvus: " + self.stream_buffers[event.chat_id]
            )
        elif event.event_type == "subagent.started" and event.run_id is not None:
            tree = self.query_one("#agent-tree", Tree)
            node = tree.root.add(f"{str(event.run_id)[:8]} running")
            self.agent_nodes[event.run_id] = node
            tree.root.expand()
        elif event.event_type.startswith("subagent.") and event.run_id is not None:
            existing_node = self.agent_nodes.get(event.run_id)
            if existing_node is not None:
                existing_node.label = f"{str(event.run_id)[:8]} {event.event_type.split('.')[-1]}"
                tree = self.query_one("#agent-tree", Tree)
                tree.refresh()

    def _append_log(self, chat_id: UUID, rendered: str) -> None:
        self.chat_logs.setdefault(chat_id, []).append(rendered)
        if chat_id == self.active_chat_id:
            self.query_one("#log", RichLog).write(rendered)

    def _append_activity(self, chat_id: UUID, summary: str) -> None:
        rendered = self._safe_ui_text(summary, "Activity updated.", limit=240)
        entries = self.activity_logs.setdefault(chat_id, [])
        if entries and entries[-1] == rendered:
            return
        entries.append(rendered)
        if len(entries) > 200:
            del entries[:-200]
        if chat_id == self.active_chat_id:
            self.query_one("#activity", RichLog).write(rendered)

    def _render_active_chat(self) -> None:
        if self.active_chat_id is None:
            return
        log = self.query_one("#log", RichLog)
        log.clear()
        for line in self.chat_logs.get(self.active_chat_id, []):
            log.write(line)
        activity = self.query_one("#activity", RichLog)
        activity.clear()
        for line in self.activity_logs.get(self.active_chat_id, []):
            activity.write(line)
        stream = self.stream_buffers.get(self.active_chat_id, "")
        self.query_one("#stream", Static).update("Corvus: " + stream if stream else "")

    @staticmethod
    def _activity_summary(event: ConversationEvent) -> str | None:
        event_type = event.event_type
        run = f" [{str(event.run_id)[:8]}]" if event.run_id is not None else ""
        if event_type == "agent.status":
            return CorvusApp._safe_agent_status(event.payload)
        if event_type == "agent.build.progress":
            return CorvusApp._safe_build_progress(event.payload)
        fixed: dict[str, str] = {
            "message.received": "Message accepted and queued.",
            "run.queued": f"Task{run} queued.",
            "run.started": f"Task{run} started. Thinking...",
            "agent.started": "Preparing a response...",
            "agent.delta": "Drafting the response...",
            "agent.completed": "Response draft complete.",
            "assistant.message": "Response delivered.",
            "run.completed": f"Task{run} completed.",
            "run.cancelled": f"Task{run} cancelled.",
            "run.failed": f"Task{run} failed safely.",
            "run.timed_out": f"Task{run} timed out.",
            "conversation.cancelled": "Chat work cancelled.",
            "agent.error": "The model provider reported an error.",
            "agent.configuration.required": "Model provider configuration is required.",
            "agent.build.started": "Isolated build started.",
            "agent.build.completed": "Isolated build completed; a review bundle is ready.",
            "agent.build.blocked": "Isolated build blocked; no project changes were applied.",
            "subagent.started": f"Subagent{run} started bounded analysis.",
            "subagent.completed": f"Subagent{run} completed bounded analysis.",
            "subagent.failed": f"Subagent{run} failed safely.",
            "subagent.timed_out": f"Subagent{run} timed out.",
            "subagent.cancelled": f"Subagent{run} cancelled.",
            "agent.delegation.started": "Delegated analysis started.",
            "agent.delegation.delta": "Delegated analysis is responding...",
            "agent.delegation.completed": "Delegated analysis completed.",
            "delegation.granted": "Subagent delegation enabled for this window.",
            "delegation.revoked": "Subagent delegation disabled for this window.",
            "stream.gap": "Some earlier activity events are no longer retained.",
        }
        if event_type in fixed:
            return fixed[event_type]
        if event_type.startswith("agent.reasoning"):
            return "Reasoning summary updated; hidden reasoning is not displayed."
        if event_type.startswith(("agent.tool.", "tool.")):
            return f"Tool activity {CorvusApp._safe_activity_state(event_type)}."
        if event_type.startswith(("agent.task.", "task.")):
            return f"Task activity {CorvusApp._safe_activity_state(event_type)}."
        return None

    @staticmethod
    def _safe_activity_state(event_type: str) -> str:
        state = event_type.rsplit(".", 1)[-1]
        return {
            "started": "started",
            "completed": "completed",
            "failed": "failed safely",
            "cancelled": "cancelled",
            "timed_out": "timed out",
            "blocked": "was blocked",
        }.get(state, "updated")

    @staticmethod
    def _safe_agent_status(payload: dict[str, object]) -> str:
        value = payload.get("text")
        fixed = {
            "Reviewing conversation context.",
            "Checking whether bounded subagents would help.",
            "Waiting for the selected model response.",
        }
        if isinstance(value, str) and value in fixed:
            return value
        if isinstance(value, str):
            for count in range(1, 9):
                expected = f"Running {count} bounded analysis subagent(s)."
                if value == expected:
                    return value
        return "Agent status updated; unrecognized details are hidden."

    @staticmethod
    def _safe_build_progress(payload: dict[str, object]) -> str:
        parts = ["Isolated build progress"]
        phase = payload.get("phase")
        allowed_phases = {
            "understand",
            "plan",
            "build",
            "verify",
            "package",
            "approve",
            "deliver",
            "complete",
            "blocked",
            "failed",
            "cancelled",
            "paused",
        }
        if isinstance(phase, str) and phase in allowed_phases:
            parts.append(f"phase={phase}")
        counts = (
            ("tests", "test_count"),
            ("tests passed", "tests_passed"),
            ("tests failed", "tests_failed"),
            ("repairs", "repair_count"),
            ("repairs", "repairs"),
        )
        seen_labels: set[str] = set()
        for label, key in counts:
            value = payload.get(key)
            if label in seen_labels or type(value) is not int or not 0 <= value <= 1_000_000:
                continue
            parts.append(f"{label}={value}")
            seen_labels.add(label)
        return "; ".join(parts) + "."

    async def _refresh_status(self, snapshot: ConversationSnapshot | None = None) -> None:
        if self.active_chat_id is None:
            return
        snapshot = snapshot or await self.runtime.get_chat(self.active_chat_id)
        agents = "ON" if self.subagents_enabled else "OFF"
        model = self._active_model_summary()
        self.query_one("#status", Static).update(
            f"{snapshot.title} | {snapshot.status.value} | queued {snapshot.queued_messages} | "
            f"subagents {agents} | sandbox {self.sandbox_backend.upper()} | model {model}\n"
            f"Project: {self.project}"
        )
        await self._sync_model_controls()

    def _load_initial_model_state(self) -> LiveModelState:
        if self.model_controller is None:
            return LiveModelState(providers=(), active_provider=None, active_model=None)
        try:
            return self.model_controller.state()
        except Exception:
            # Controller state is an internal trust boundary. Unexpected details are never shown.
            self._model_state_error = True
            return LiveModelState(providers=(), active_provider=None, active_model=None)

    def _provider_select_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for provider in self.model_state.providers:
            if not provider.name or provider.name == _NO_PROVIDER:
                continue
            label = self._safe_ui_text(provider.label, provider.name)
            if not provider.configured and "(NOT ADDED)" not in label.upper():
                label = f"{label} (NOT ADDED)"
            options.append((label, provider.name))
        return options or [("No configured providers", _NO_PROVIDER)]

    def _model_select_options(self, provider_name: str) -> list[tuple[str, str]]:
        provider = self._provider_option(provider_name)
        if provider is None:
            return [("No models available", _NO_MODEL)]
        options = [
            (self._safe_ui_text(model.label, model.id or "Provider default"), model.id)
            for model in provider.models
            if model.id != _NO_MODEL
        ]
        return options or [("No models available", _NO_MODEL)]

    def _thinking_select_options(
        self,
        provider_name: str,
        model_id: str,
    ) -> list[tuple[str, str]]:
        model = self._model_option(provider_name, model_id)
        if model is None:
            return [("No thinking presets available", _NO_THINKING)]
        options = [
            (
                self._safe_ui_text(
                    (
                        option.label
                        if option.effective_id is not None
                        else f"{option.label} (unsupported)"
                    ),
                    option.id,
                ),
                option.id,
            )
            for option in model.thinking
            if option.id != _NO_THINKING
        ]
        return options or [("No thinking presets available", _NO_THINKING)]

    def _initial_provider_value(self) -> str:
        active_provider = self.model_state.active_provider
        if active_provider is not None and self._provider_option(active_provider) is not None:
            return active_provider
        return self.model_state.providers[0].name if self.model_state.providers else _NO_PROVIDER

    def _initial_model_value(self) -> str:
        return self._model_value_for(self._initial_provider_value())

    def _initial_thinking_value(self) -> str:
        provider = self._initial_provider_value()
        model = self._model_value_for(provider)
        return self._thinking_value_for(provider, model)

    def _model_value_for(self, provider_name: str) -> str:
        provider = self._provider_option(provider_name)
        if provider is None or not provider.models:
            return _NO_MODEL
        model_ids = {model.id for model in provider.models}
        active_model = self.model_state.active_model
        if (
            provider_name == self.model_state.active_provider
            and active_model is not None
            and active_model in model_ids
        ):
            return active_model
        selected_model = provider.selected_model
        if selected_model is not None and selected_model in model_ids:
            return selected_model
        return provider.models[0].id

    def _thinking_value_for(self, provider_name: str, model_id: str) -> str:
        provider = self._provider_option(provider_name)
        model = self._model_option(provider_name, model_id)
        if provider is None or model is None or not model.thinking:
            return _NO_THINKING
        thinking_ids = {option.id for option in model.thinking}
        active_thinking = self.model_state.active_thinking
        if (
            provider_name == self.model_state.active_provider
            and model_id == self.model_state.active_model
            and active_thinking is not None
            and active_thinking in thinking_ids
        ):
            return active_thinking
        selected_thinking = provider.selected_thinking
        if selected_thinking is not None and selected_thinking in thinking_ids:
            return selected_thinking
        return model.thinking[0].id

    def _provider_option(self, name: str | None) -> LiveProviderOption | None:
        if name is None:
            return None
        return next(
            (provider for provider in self.model_state.providers if provider.name == name),
            None,
        )

    def _model_option(self, provider_name: str, model_id: str) -> LiveModelOption | None:
        provider = self._provider_option(provider_name)
        if provider is None:
            return None
        return next((model for model in provider.models if model.id == model_id), None)

    def _thinking_option(
        self,
        provider_name: str,
        model_id: str,
        thinking_id: str,
    ) -> LiveThinkingOption | None:
        model = self._model_option(provider_name, model_id)
        if model is None:
            return None
        return next((option for option in model.thinking if option.id == thinking_id), None)

    def _populate_models(self, provider_name: str) -> None:
        model_select = self.query_one_optional("#model-select", Select)
        if model_select is None:
            return
        model_select.set_options(self._model_select_options(provider_name))
        model_value = self._model_value_for(provider_name)
        model_select.value = model_value
        self._populate_thinking(provider_name, model_value)

    def _populate_thinking(self, provider_name: str, model_id: str) -> None:
        thinking_select = self.query_one_optional("#thinking-select", Select)
        if thinking_select is None:
            return
        thinking_select.set_options(self._thinking_select_options(provider_name, model_id))
        thinking_select.value = self._thinking_value_for(provider_name, model_id)

    def _render_model_state(self) -> None:
        provider_select = self.query_one_optional("#provider-select", Select)
        if provider_select is None:
            return
        provider_select.set_options(self._provider_select_options())
        provider_select.value = self._initial_provider_value()
        self._populate_models(self._initial_provider_value())

    def _selected_route(self) -> tuple[str, str, str] | None:
        provider_select = self.query_one_optional("#provider-select", Select)
        model_select = self.query_one_optional("#model-select", Select)
        thinking_select = self.query_one_optional("#thinking-select", Select)
        if provider_select is None or model_select is None or thinking_select is None:
            return None
        provider_value = provider_select.value
        model_value = model_select.value
        thinking_value = thinking_select.value
        if (
            not isinstance(provider_value, str)
            or not isinstance(model_value, str)
            or not isinstance(thinking_value, str)
        ):
            return None
        provider = self._provider_option(provider_value)
        if provider is None or model_value == _NO_MODEL or thinking_value == _NO_THINKING:
            return None
        model = self._model_option(provider_value, model_value)
        if model is None or thinking_value not in {option.id for option in model.thinking}:
            return None
        return provider_value, model_value, thinking_value

    async def _has_active_work(self) -> bool:
        snapshots = await self.runtime.list_chats()
        return any(
            snapshot.active_run_id is not None or snapshot.queued_messages > 0
            for snapshot in snapshots
        )

    async def _sync_model_controls(self) -> None:
        busy = await self._has_active_work()
        provider_select = self.query_one_optional("#provider-select", Select)
        model_select = self.query_one_optional("#model-select", Select)
        thinking_select = self.query_one_optional("#thinking-select", Select)
        apply_button = self.query_one_optional("#model-apply", Button)
        prompt = self.query_one_optional("#prompt", Input)
        thinking_status = self.query_one_optional("#thinking-status", Static)
        status = self.query_one_optional("#model-status", Static)
        if (
            provider_select is None
            or model_select is None
            or thinking_select is None
            or apply_button is None
            or prompt is None
            or thinking_status is None
            or status is None
        ):
            return
        unavailable = self.model_controller is None or not self.model_state.providers
        locked = unavailable or busy or self._model_switching
        provider_select.disabled = locked
        model_select.disabled = locked
        thinking_select.disabled = locked
        selection = self._selected_route()
        provider = self._provider_option(selection[0]) if selection is not None else None
        thinking = self._thinking_option(*selection) if selection is not None else None
        changed = selection is not None and selection != (
            self.model_state.active_provider,
            self.model_state.active_model,
            self.model_state.active_thinking,
        )
        activatable = bool(
            provider is not None
            and provider.configured
            and thinking is not None
            and thinking.effective_id is not None
        )
        apply_button.disabled = locked or not changed or not activatable
        prompt.disabled = self._model_switching
        thinking_status.update(self._thinking_summary())
        if self._model_switching:
            status.update("Switching provider, model, and thinking preset for future messages...")
        elif busy:
            status.update(f"{self._model_summary()} Selection is locked while chat work is active.")
        elif self._model_feedback is not None:
            status.update(self._model_feedback)
        else:
            status.update(self._selected_route_summary())

    async def _apply_model_selection(self) -> None:
        if self.model_controller is None or self._model_switching:
            return
        selection = self._selected_route()
        if selection is None:
            self._model_feedback = "Choose an available provider, model, and thinking preset."
            await self._sync_model_controls()
            return
        provider = self._provider_option(selection[0])
        if provider is None or not provider.configured:
            self._model_feedback = (
                "That provider is not added. Add it with `corvus run --setup`; "
                "no route was activated."
            )
            await self._sync_model_controls()
            return
        thinking_option = self._thinking_option(*selection)
        if thinking_option is None or thinking_option.effective_id is None:
            self._model_feedback = (
                "That thinking preset is unsupported for this model; no route was activated."
            )
            await self._sync_model_controls()
            return
        if await self._has_active_work():
            self._model_feedback = "Wait for active and queued chat work before switching models."
            await self._sync_model_controls()
            return
        provider_name, model, thinking = selection
        self._model_switching = True
        self._model_feedback = None
        await self._sync_model_controls()
        try:
            state = await self.model_controller.activate(provider_name, model, thinking)
        except LiveModelSelectionError as exc:
            detail = self._safe_ui_text(str(exc), "The selection was rejected.", limit=180)
            self._model_feedback = f"Selection unchanged. {detail}"
        except Exception as exc:
            self._model_feedback = (
                f"Selection unchanged. Provider switch failed safely ({type(exc).__name__})."
            )
        else:
            self.model_state = state
            self._render_model_state()
            self._model_feedback = (
                f"Now using {self._active_model_summary()}. New messages use this selection."
            )
            if self.active_chat_id is not None:
                self._append_log(
                    self.active_chat_id,
                    f"[cyan]{escape(self._model_feedback)}[/cyan]",
                )
                self._append_activity(
                    self.active_chat_id,
                    f"Model route changed to {self._active_model_summary()}.",
                )
        finally:
            self._model_switching = False
        await self._refresh_status()

    def _model_summary(self) -> str:
        if self.model_controller is None:
            return "Provider and model selection is unavailable in this session."
        if not self.model_state.providers:
            return "No configured providers are available. Run setup to add one."
        provider = self._provider_option(self.model_state.active_provider)
        if provider is None:
            return "No configured model selection is active."
        if (
            self.model_state.active_model is None
            or self._model_option(provider.name, self.model_state.active_model) is None
        ):
            return "No valid configured model is active. Review the route and choose Apply route."
        return f"Using {self._active_model_summary()}."

    def _selected_route_summary(self) -> str:
        selection = self._selected_route()
        if selection is None:
            return self._model_summary()
        provider = self._provider_option(selection[0])
        if provider is None:
            return self._model_summary()
        provider_label = self._safe_ui_text(provider.label, provider.name)
        if not provider.configured:
            if "(NOT ADDED)" not in provider_label.upper():
                provider_label = f"{provider_label} (NOT ADDED)"
            return f"{provider_label}. Add it with `corvus run --setup`; Apply route is disabled."
        thinking = self._thinking_option(*selection)
        if thinking is None or thinking.effective_id is None:
            return "This thinking preset is unsupported for the selected model; Apply is disabled."
        if selection == (
            self.model_state.active_provider,
            self.model_state.active_model,
            self.model_state.active_thinking,
        ):
            return self._model_summary()
        return "Route selection is ready. Choose Apply route to use it for future messages."

    def _thinking_summary(self) -> str:
        try:
            selection = self._selected_route()
        except Exception:
            selection = None
        if selection is None:
            provider_name = self._initial_provider_value()
            model_id = self._model_value_for(provider_name)
            thinking_id = self._thinking_value_for(provider_name, model_id)
            selection = (provider_name, model_id, thinking_id)
        thinking = self._thinking_option(*selection)
        if thinking is None:
            return "No thinking preset is available for this model."
        cap_note = " Effort preference only; Corvus token and cost caps still apply."
        detail = self._safe_ui_text(
            thinking.detail,
            "Thinking support information is unavailable.",
            limit=180,
        )
        if thinking.effective_id is None:
            return f"UNSUPPORTED - {detail}{cap_note}"
        if thinking.effective_id != thinking.id:
            model = self._model_option(selection[0], selection[1])
            effective = (
                next(
                    (option for option in model.thinking if option.id == thinking.effective_id),
                    None,
                )
                if model is not None
                else None
            )
            effective_label = self._safe_ui_text(
                effective.label if effective is not None else thinking.effective_id,
                thinking.effective_id,
            )
            return f"FALLBACK -> {effective_label} - {detail}{cap_note}"
        return f"SUPPORTED - {detail}{cap_note}"

    def _active_model_summary(self) -> str:
        provider = self._provider_option(self.model_state.active_provider)
        if provider is None:
            return "none"
        provider_label = self._safe_ui_text(provider.label, provider.name)
        if self.model_state.active_model is None:
            return f"{provider_label} / no valid configured model"
        model = next(
            (item for item in provider.models if item.id == self.model_state.active_model),
            None,
        )
        if model is None:
            return f"{provider_label} / no valid configured model"
        model_label = self._safe_ui_text(
            model.label,
            "Provider default",
        )
        thinking = self._thinking_option(
            provider.name,
            self.model_state.active_model or "",
            self.model_state.active_thinking or "",
        )
        if thinking is None:
            return f"{provider_label} / {model_label}"
        thinking_label = self._safe_ui_text(thinking.label, thinking.id)
        return f"{provider_label} / {model_label} / {thinking_label}"

    @staticmethod
    def _safe_ui_text(value: str, fallback: str, *, limit: int = 80) -> str:
        printable = "".join(character if character.isprintable() else " " for character in value)
        normalized = " ".join(printable.split())[:limit]
        return normalized or fallback

    def _require_active(self) -> UUID:
        if self.active_chat_id is None:
            raise RuntimeError("no active chat")
        return self.active_chat_id
