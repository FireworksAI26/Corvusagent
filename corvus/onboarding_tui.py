from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Select,
    Static,
    Switch,
)

from corvus.codex_cli import (
    CODEX_MODEL_OPTIONS,
    CodexCliService,
    CodexLoginStatus,
    CodexModelOption,
)
from corvus.codex_install import CodexCliInstaller, CodexInstallError
from corvus.models import ModelProvider
from corvus.onboarding import SandboxBackendChoice

ProviderSaver = Callable[[ModelProvider, str | None], None]


@dataclass(frozen=True)
class ProviderPreset:
    kind: str
    name: str
    base_url: str
    model: str
    requires_secret: bool
    local: bool = False


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        kind="openai",
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1",
        requires_secret=True,
    ),
    "anthropic": ProviderPreset(
        kind="anthropic",
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-5",
        requires_secret=True,
    ),
    "gemini": ProviderPreset(
        kind="gemini",
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.5-pro",
        requires_secret=True,
    ),
    "openrouter": ProviderPreset(
        kind="openrouter",
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4.1-mini",
        requires_secret=True,
    ),
    "openai_compatible": ProviderPreset(
        kind="openai_compatible",
        name="compatible",
        base_url="http://localhost:8000/v1",
        model="model",
        requires_secret=False,
    ),
    "ollama": ProviderPreset(
        kind="ollama",
        name="ollama",
        base_url="http://localhost:11434",
        model="llama3.2",
        requires_secret=False,
        local=True,
    ),
}


@dataclass(frozen=True)
class OnboardingSelection:
    project: Path
    enable_subagents_for_window: bool
    max_subagents: int
    privacy_acknowledged: bool
    provider_name: str | None = None
    provider_configured: bool = False
    sandbox_backend: SandboxBackendChoice = "auto"


class FirstRunApp(App[OnboardingSelection | None]):
    """A finite first-run setup that never returns or persists provider secrets."""

    TITLE = "Welcome to Corvus"
    SUB_TITLE = "first-run setup"
    CSS = """
    Screen { align: center middle; }
    #card { width: 100; height: 95%; border: round $primary; padding: 0 2; }
    #form-scroll { height: 1fr; padding-right: 1; }
    .heading { text-style: bold; color: $accent; margin-top: 1; }
    .detail { color: $text-muted; margin-bottom: 1; }
    #project, #provider-choice, #sandbox-choice { margin-bottom: 1; }
    #codex-panel, #api-panel { height: auto; border: round $surface; padding: 0 1; }
    #codex-actions, #subagent-row { height: auto; margin-bottom: 1; }
    #codex-install, #codex-login, #codex-recheck { margin-right: 1; }
    #codex-install-detail { margin-bottom: 1; }
    #codex-install-progress { display: none; height: 1; margin-bottom: 1; }
    #codex-model { margin-bottom: 1; }
    .field-label { color: $text-muted; }
    .provider-field { margin-bottom: 1; }
    #subagent-switch { width: 10; }
    #max-subagents { width: 8; margin-left: 2; }
    #error { color: $error; height: auto; min-height: 1; }
    #actions { height: auto; margin: 1 0; align-horizontal: right; }
    #start { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("ctrl+q", "quit_setup", "Exit"),
    ]

    def __init__(
        self,
        *,
        project: Path,
        provider_configured: bool,
        provider_label: str | None,
        docker_available: bool,
        docker_detail: str,
        seed_subagents: bool = False,
        seed_max_subagents: int = 2,
        existing_provider: ModelProvider | None = None,
        codex_service: CodexCliService | None = None,
        codex_installer: CodexCliInstaller | None = None,
        provider_saver: ProviderSaver | None = None,
        podman_available: bool = False,
        podman_detail: str = "Podman was not checked.",
        seed_sandbox_backend: SandboxBackendChoice = "auto",
    ) -> None:
        super().__init__()
        self.initial_project = project.resolve()
        self.provider_configured = provider_configured
        self.provider_label = provider_label
        self.docker_available = docker_available
        self.docker_detail = docker_detail
        self.podman_available = podman_available
        self.podman_detail = podman_detail
        self.seed_sandbox_backend = seed_sandbox_backend
        self.seed_subagents = seed_subagents and provider_configured
        self.seed_max_subagents = min(max(seed_max_subagents, 1), 4)
        self.existing_provider = existing_provider
        self.codex_service = codex_service
        self.codex_installer = codex_installer
        self.provider_saver = provider_saver
        self.codex_status: CodexLoginStatus | None = None
        self._codex_busy = False
        self._codex_installing = False
        self._initial_provider_choice = (
            "existing"
            if provider_configured
            else "codex"
            if codex_service is not None or codex_installer is not None
            else "offline"
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="card"):
            with VerticalScroll(id="form-scroll"):
                yield Label("Welcome", classes="heading")
                yield Static(
                    "Chat normally in Corvus. Use /build for isolated coding work. Nothing is "
                    "applied to a project until you review and explicitly approve its bundle.",
                    classes="detail",
                )
                yield Label("1. Project context", classes="heading")
                yield Input(value=str(self.initial_project), id="project")
                yield Static(
                    "New chats in this window use this directory. Corvus does not write to it "
                    "during ordinary chat or sandbox builds.",
                    classes="detail",
                )
                yield Label("2. Model access", classes="heading")
                yield Select(
                    self._provider_options(),
                    value=self._initial_provider_choice,
                    allow_blank=False,
                    id="provider-choice",
                )
                yield Static(self._provider_status(), id="provider-status", classes="detail")
                with Vertical(id="codex-panel"):
                    yield Label("Codex / ChatGPT", classes="heading")
                    yield Static(
                        "Sign-in is handled by the selected Codex CLI shown below. Corvus never "
                        "receives or stores your ChatGPT token.",
                        classes="detail",
                    )
                    yield Static(self._initial_codex_status(), id="codex-status", classes="detail")
                    yield Static(
                        self._codex_installer_detail(),
                        id="codex-install-detail",
                        classes="detail",
                        markup=False,
                    )
                    yield LoadingIndicator(id="codex-install-progress")
                    with Horizontal(id="codex-actions"):
                        yield Button(
                            "Install official Codex CLI",
                            id="codex-install",
                            variant="primary",
                            disabled=self.codex_installer is None or self.codex_service is not None,
                        )
                        yield Button(
                            "Sign in with ChatGPT",
                            id="codex-login",
                            variant="primary",
                            disabled=self.codex_service is None,
                        )
                        yield Button(
                            "Recheck",
                            id="codex-recheck",
                            disabled=self.codex_service is None,
                        )
                    yield Label("Codex model", classes="field-label")
                    yield Select(
                        [(option.label, option.model) for option in CODEX_MODEL_OPTIONS],
                        value=CODEX_MODEL_OPTIONS[0].model,
                        allow_blank=False,
                        id="codex-model",
                        disabled=True,
                    )
                    yield Static(
                        CODEX_MODEL_OPTIONS[0].description,
                        id="codex-model-description",
                        classes="detail",
                    )
                with Vertical(id="api-panel"):
                    yield Label("API or local provider", classes="heading")
                    yield Label("Configuration name", classes="field-label")
                    yield Input(id="provider-name", classes="provider-field")
                    yield Label("Base URL", classes="field-label")
                    yield Input(id="provider-base-url", classes="provider-field")
                    yield Label("Model ID", classes="field-label")
                    yield Input(id="provider-model", classes="provider-field")
                    yield Label(
                        "API key (stored only through the configured saver)", classes="field-label"
                    )
                    yield Input(
                        id="provider-secret",
                        password=True,
                        placeholder="Not required for local providers",
                        classes="provider-field",
                    )
                yield Label("3. Sandbox for isolated builds", classes="heading")
                yield Select(
                    [
                        ("Auto (Docker, then Podman)", "auto"),
                        ("Docker only", "docker"),
                        ("Podman only", "podman"),
                        ("Chat only (/build disabled)", "none"),
                    ],
                    value=self.seed_sandbox_backend,
                    allow_blank=False,
                    id="sandbox-choice",
                )
                yield Static(self._sandbox_status(), id="sandbox-status", classes="detail")
                yield Static(self._sandbox_readiness(), id="sandbox-readiness", classes="detail")
                yield Label("4. Delegate analysis to subagents?", classes="heading")
                yield Static(
                    "Corvus can ask a bounded number of non-recursive analysis workers to help "
                    "with a message (default 2; hard maximum 4). They share this chat's context "
                    "with your configured model provider and may increase usage. They cannot use "
                    "tools, build or change files, access credentials, approve delivery, relax "
                    "policy, or create more agents. Permission lasts only for this Corvus window.",
                    classes="detail",
                )
                with Horizontal(id="subagent-row"):
                    yield Switch(
                        value=self.seed_subagents,
                        id="subagent-switch",
                        disabled=not self.provider_configured,
                    )
                    yield Label("Enable for this window (off is recommended)")
                    yield Input(
                        value=str(self.seed_max_subagents),
                        id="max-subagents",
                        type="integer",
                        disabled=not self.provider_configured,
                    )
                yield Checkbox(
                    "I understand that model calls can send this chat's context to my configured "
                    "provider, and project changes still require explicit delivery approval.",
                    id="privacy",
                )
                yield Static("", id="error")
            with Horizontal(id="actions"):
                yield Button("Exit", id="exit")
                yield Button(self._start_label(), id="start", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_provider_choice(self._initial_provider_choice)
        self._refresh_codex_controls()
        self.query_one("#project", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "exit":
            self.exit(None)
            return
        if event.button.id == "codex-install":
            self._start_codex_install()
            return
        if event.button.id == "codex-login":
            self._start_codex_check(login=True)
            return
        if event.button.id == "codex-recheck":
            self._start_codex_check(login=False)
            return
        if event.button.id != "start":
            return
        selection = self._selection()
        if selection is not None:
            self.exit(selection)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider-choice" and isinstance(event.value, str):
            self._apply_provider_choice(event.value)
        elif event.select.id == "codex-model" and isinstance(event.value, str):
            option = self._codex_option(event.value)
            self.query_one("#codex-model-description", Static).update(option.description)
        elif event.select.id == "sandbox-choice" and isinstance(event.value, str):
            self.query_one("#sandbox-status", Static).update(self._sandbox_status(event.value))

    def action_quit_setup(self) -> None:
        self.exit(None)

    def _selection(self) -> OnboardingSelection | None:
        error = self.query_one("#error", Static)
        error.update("")
        raw_project = self.query_one("#project", Input).value.strip()
        project = Path(raw_project).expanduser()
        if not project.exists() or not project.is_dir():
            error.update("Choose an existing project directory.")
            return None
        privacy = self.query_one("#privacy", Checkbox).value
        if not privacy:
            error.update("Confirm the privacy and approval boundary before continuing.")
            return None
        try:
            maximum = int(self.query_one("#max-subagents", Input).value)
        except ValueError:
            error.update("Subagent maximum must be a number from 1 to 4.")
            return None
        if maximum < 1 or maximum > 4:
            error.update("Subagent maximum must be from 1 to 4.")
            return None
        provider_result = self._configure_selected_provider(error)
        if provider_result is None:
            return None
        provider_name, configured = provider_result
        enabled = configured and self.query_one("#subagent-switch", Switch).value
        return OnboardingSelection(
            project=project.resolve(),
            enable_subagents_for_window=enabled,
            max_subagents=maximum,
            privacy_acknowledged=True,
            provider_name=provider_name,
            provider_configured=configured,
            sandbox_backend=self._sandbox_choice(),
        )

    def _configure_selected_provider(self, error: Static) -> tuple[str | None, bool] | None:
        choice = self._provider_choice()
        if choice == "offline":
            return None, False
        if choice == "existing":
            if not self.provider_configured:
                error.update("The existing provider is not ready. Choose another provider.")
                return None
            if self.existing_provider is not None:
                return self.existing_provider.name, True
            return self.provider_label or "configured-provider", True
        if self.provider_saver is None:
            error.update("Provider saving is unavailable in this setup session.")
            return None
        if choice == "codex":
            if not self._codex_chatgpt_ready() or self.codex_service is None:
                error.update("Sign in with ChatGPT and wait for a ready status before continuing.")
                return None
            model_value = self.query_one("#codex-model", Select).value
            if not isinstance(model_value, str):
                error.update("Choose a Codex model.")
                return None
            option = self._codex_option(model_value)
            provider = ModelProvider(
                name="codex-chatgpt",
                kind="codex_cli",
                base_url="",
                model=option.model,
                executable=self.codex_service.executable,
                executable_sha256=self.codex_service.executable_sha256,
                reasoning_effort=option.reasoning_effort,
            )
            if not self._save_provider(provider, None, error):
                return None
            return provider.name, True
        preset = PROVIDER_PRESETS.get(choice)
        if preset is None:
            error.update("Choose a supported provider or explicit offline mode.")
            return None
        name = self.query_one("#provider-name", Input).value.strip()
        base_url = self.query_one("#provider-base-url", Input).value.strip()
        model = self.query_one("#provider-model", Input).value.strip()
        secret_input = self.query_one("#provider-secret", Input)
        secret = secret_input.value.strip()
        if not name or not model:
            error.update("Provider name and model ID are required.")
            return None
        if not self._valid_base_url(base_url):
            error.update("Base URL must be an HTTP(S) URL without embedded credentials.")
            return None
        if preset.requires_secret and not secret:
            error.update("This provider requires an API key.")
            return None
        provider = ModelProvider(
            name=name,
            kind=preset.kind,  # type: ignore[arg-type]
            base_url=base_url,
            model=model,
            keyring_service="corvus-model-provider" if secret else None,
            local=preset.local,
        )
        secret_input.value = ""
        if not self._save_provider(provider, secret or None, error):
            return None
        return provider.name, True

    def _save_provider(
        self,
        provider: ModelProvider,
        secret: str | None,
        error: Static,
    ) -> bool:
        provider_saver = self.provider_saver
        if provider_saver is None:
            error.update("Provider storage is unavailable.")
            return False
        try:
            provider_saver(provider, secret)
        except Exception as exc:  # saver boundaries include keyring and filesystem backends
            error.update(f"Provider could not be saved ({type(exc).__name__}).")
            return False
        self.provider_configured = True
        self.provider_label = f"{provider.name} / {provider.model or 'Codex default'}"
        return True

    def _apply_provider_choice(self, choice: str) -> None:
        codex = choice == "codex"
        api = choice in PROVIDER_PRESETS
        self.query_one("#codex-panel", Vertical).display = codex
        self.query_one("#api-panel", Vertical).display = api
        if api:
            self._load_preset(choice)
        else:
            self.query_one("#provider-secret", Input).value = ""
        self._refresh_provider_status()
        self._refresh_subagent_controls()

    def _load_preset(self, choice: str) -> None:
        preset = PROVIDER_PRESETS[choice]
        self.query_one("#provider-name", Input).value = preset.name
        self.query_one("#provider-base-url", Input).value = preset.base_url
        self.query_one("#provider-model", Input).value = preset.model
        secret = self.query_one("#provider-secret", Input)
        secret.value = ""
        secret.disabled = not preset.requires_secret and preset.local
        secret.placeholder = (
            "Required; stored in the OS keyring"
            if preset.requires_secret
            else "Optional for this provider"
        )

    def _refresh_provider_status(self) -> None:
        choice = self._provider_choice()
        status = self.query_one("#provider-status", Static)
        if choice == "existing":
            status.update(self._provider_status())
        elif choice == "codex":
            status.update(
                "Use the official Codex CLI sign-in and choose an entitled model. "
                "No ChatGPT token is exposed to Corvus."
            )
        elif choice == "offline":
            status.update(
                "Offline mode is selected explicitly. You can enter chat, but agent responses "
                "and subagents remain unavailable until a provider is configured."
            )
        else:
            preset = PROVIDER_PRESETS[choice]
            billing = "local runtime" if preset.local else "provider API usage"
            status.update(f"Configure {preset.name}; requests use {billing}.")
        self.query_one("#start", Button).label = self._start_label()

    def _refresh_subagent_controls(self) -> None:
        ready = self._selected_provider_ready()
        switch = self.query_one("#subagent-switch", Switch)
        maximum = self.query_one("#max-subagents", Input)
        if not ready:
            switch.value = False
        switch.disabled = not ready
        maximum.disabled = not ready

    def _selected_provider_ready(self) -> bool:
        choice = self._provider_choice()
        if choice == "existing":
            return self.provider_configured
        if choice == "codex":
            return self._codex_chatgpt_ready()
        return choice in PROVIDER_PRESETS

    def _start_codex_check(self, *, login: bool) -> None:
        if self.codex_service is None or self._codex_busy:
            return
        self._codex_busy = True
        self._codex_installing = False
        self._refresh_codex_controls()
        action = "Opening the official ChatGPT sign-in..." if login else "Checking Codex sign-in..."
        self.query_one("#codex-status", Static).update(action)
        self.run_worker(
            self._run_codex_check(login=login),
            name="codex-login" if login else "codex-status",
            group="codex-auth",
            exclusive=True,
            exit_on_error=False,
        )

    def _start_codex_install(self) -> None:
        if self.codex_installer is None or self.codex_service is not None or self._codex_busy:
            return
        self._codex_busy = True
        self._codex_installing = True
        self.codex_status = None
        self._refresh_codex_controls()
        self.query_one("#codex-status", Static).update(
            "Preparing the official Codex CLI installation..."
        )
        self.run_worker(
            self._run_codex_install(),
            name="codex-install",
            group="codex-auth",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_codex_install(self) -> None:
        installer = self.codex_installer
        if installer is None:
            self._finish_codex_install_error(RuntimeError("Codex installer is unavailable"))
            return
        try:
            service = await installer.install(self._update_codex_install_progress)
            service.validate_executable()
        except Exception as exc:
            self._finish_codex_install_error(exc)
            return
        self.codex_service = service
        self.query_one("#codex-status", Static).update(
            "Codex CLI installed and verified. Checking ChatGPT sign-in..."
        )
        try:
            status = await service.login_status()
        except Exception as exc:
            self._finish_codex_error(type(exc).__name__)
            return
        self._apply_codex_status(status, installed=True)

    def _update_codex_install_progress(self, phase: str) -> None:
        # Installer progress is deliberately bounded before rendering. The installer owns any
        # detailed logs, so terminal output and environment values never reach this screen.
        rendered = " ".join(phase.split())[:160]
        if rendered:
            self.query_one("#codex-status", Static).update(rendered)

    async def _run_codex_check(self, *, login: bool) -> None:
        service = self.codex_service
        if service is None:
            self._finish_codex_error("CodexUnavailable")
            return
        try:
            status = await service.login() if login else await service.login_status()
        except Exception as exc:
            self._finish_codex_error(type(exc).__name__)
            return
        self._apply_codex_status(status)

    def _apply_codex_status(self, status: CodexLoginStatus, *, installed: bool = False) -> None:
        self.codex_status = status
        self._codex_busy = False
        self._codex_installing = False
        self._refresh_codex_controls()
        chatgpt_ready = self._codex_chatgpt_ready()
        prefix = "ChatGPT ready" if chatgpt_ready else "ChatGPT sign-in required"
        installed_prefix = "Codex CLI installed and verified. " if installed else ""
        self.query_one("#codex-status", Static).update(
            f"{installed_prefix}{prefix}. {status.detail}"
        )
        self.query_one("#codex-model", Select).disabled = not chatgpt_ready
        self._refresh_subagent_controls()

    def _finish_codex_error(self, error_type: str) -> None:
        self._codex_busy = False
        self._codex_installing = False
        self.codex_status = None
        self._refresh_codex_controls()
        self.query_one("#codex-model", Select).disabled = True
        self.query_one("#codex-status", Static).update(
            f"Codex sign-in could not be checked ({error_type}). Retry or choose another provider."
        )
        self._refresh_subagent_controls()

    def _finish_codex_install_error(self, exc: Exception) -> None:
        self._codex_busy = False
        self._codex_installing = False
        self.codex_status = None
        self.codex_service = None
        self._refresh_codex_controls()
        self.query_one("#codex-model", Select).disabled = True
        detail = (
            str(exc)
            if isinstance(exc, CodexInstallError)
            else "The installer stopped unexpectedly. No sign-in was started."
        )
        self.query_one("#codex-status", Static).update(
            "Codex CLI installation could not be completed. "
            f"{detail} Retry or choose another provider."
        )
        self._refresh_subagent_controls()

    def _refresh_codex_controls(self) -> None:
        missing = self.codex_service is None
        install = self.query_one("#codex-install", Button)
        install.display = missing
        install.disabled = not missing or self.codex_installer is None or self._codex_busy
        detail = self.query_one("#codex-install-detail", Static)
        detail.display = missing
        progress = self.query_one("#codex-install-progress", LoadingIndicator)
        progress.display = self._codex_installing
        self.query_one("#codex-login", Button).disabled = missing or self._codex_busy
        self.query_one("#codex-recheck", Button).disabled = missing or self._codex_busy

    def _codex_chatgpt_ready(self) -> bool:
        return bool(
            self.codex_status is not None
            and self.codex_status.ready
            and self.codex_status.method == "chatgpt"
        )

    def _provider_choice(self) -> str:
        try:
            select = self.query_one("#provider-choice", Select)
        except Exception:
            return self._initial_provider_choice
        return select.value if isinstance(select.value, str) else self._initial_provider_choice

    def _provider_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        if self.provider_configured:
            label = self.provider_label or "configured provider"
            options.append((f"Use existing: {label}", "existing"))
        options.extend(
            [
                ("Sign in with ChatGPT (Codex CLI)", "codex"),
                ("OpenAI API", "openai"),
                ("Anthropic API", "anthropic"),
                ("Google Gemini API", "gemini"),
                ("OpenRouter", "openrouter"),
                ("OpenAI-compatible endpoint", "openai_compatible"),
                ("Local Ollama", "ollama"),
                ("Continue explicitly offline", "offline"),
            ]
        )
        return options

    @staticmethod
    def _codex_option(model: str) -> CodexModelOption:
        return next(
            (option for option in CODEX_MODEL_OPTIONS if option.model == model),
            CODEX_MODEL_OPTIONS[0],
        )

    @staticmethod
    def _valid_base_url(value: str) -> bool:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and parsed.username is None
            and parsed.password is None
        )

    def _start_label(self) -> str:
        return (
            "Continue without a model" if self._provider_choice() == "offline" else "Start chatting"
        )

    def _provider_status(self) -> str:
        if self.provider_configured:
            label = self.provider_label or "model provider"
            return f"Configured: {label}. Secrets stay in OS keyring or the official Codex store."
        if self.provider_label:
            return (
                f"Provider entry {self.provider_label} exists, but its credential is missing or "
                "the OS keyring is unavailable. Choose a provider below or continue offline."
            )
        return "Choose a provider below or continue explicitly in offline setup mode."

    def _initial_codex_status(self) -> str:
        if self.codex_service is None:
            if self.codex_installer is not None:
                return (
                    "Codex CLI is not installed or discoverable. Review the official source, "
                    "version, and managed path below before installing."
                )
            return "Codex CLI is unavailable. Choose an API provider, Ollama, or offline."
        return (
            f"Codex CLI candidate: {self.codex_service.executable}. "
            "Choose Recheck or Sign in with ChatGPT to run it."
        )

    def _codex_installer_detail(self) -> str:
        if self.codex_installer is None:
            return "Automatic Codex CLI installation is unavailable in this setup session."
        return (
            f"Official source: {self.codex_installer.source}\n"
            f"Pinned version: {self.codex_installer.version}\n"
            f"Managed install path: {self.codex_installer.install_path}"
        )

    def _sandbox_choice(self) -> SandboxBackendChoice:
        try:
            value = self.query_one("#sandbox-choice", Select).value
        except Exception:
            return self.seed_sandbox_backend
        if value in {"auto", "docker", "podman", "none"}:
            return value  # type: ignore[return-value]
        return self.seed_sandbox_backend

    def _sandbox_status(self, choice: str | None = None) -> str:
        selected = choice or self._sandbox_choice()
        if selected == "none":
            return (
                "Chat only is explicit: /build is disabled. Corvus will not run build commands "
                "directly on your host as a fallback."
            )
        if selected == "docker":
            if self.docker_available:
                return "Docker only is selected and ready for constrained, isolated builds."
            return (
                "Docker only is selected but unavailable. /build will fail closed and will not "
                "silently switch to Podman or host execution."
            )
        if selected == "podman":
            if self.podman_available:
                return "Podman only is selected and ready for constrained, isolated builds."
            return (
                "Podman only is selected but unavailable. /build will fail closed and will not "
                "silently switch to Docker or host execution."
            )
        if self.docker_available:
            return "Auto will use Docker, the first ready isolated engine."
        if self.podman_available:
            return "Auto will use Podman because Docker is unavailable."
        return (
            "Auto found no ready isolated engine. Chat remains available, but /build will fail "
            "closed and will never run build commands directly on the host."
        )

    def _sandbox_readiness(self) -> str:
        docker_state = "READY" if self.docker_available else "NOT READY"
        podman_state = "READY" if self.podman_available else "NOT READY"
        return (
            f"Docker: {docker_state} — {self.docker_detail}\n"
            f"Podman: {podman_state} — {self.podman_detail}\n"
            "Use corvus doctor for installation and daemon guidance."
        )
