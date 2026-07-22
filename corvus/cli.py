from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import keyring
import typer
from cryptography.fernet import Fernet
from keyring.errors import KeyringError
from rich.console import Console
from rich.table import Table

from corvus import __version__
from corvus.chat_agent import ChatAgent
from corvus.codex_cli import CodexCliProvider, CodexCliService
from corvus.codex_install import CodexCliInstaller, CodexInstallError
from corvus.config import ConfigManager, CorvusPaths
from corvus.delivery import DeliveryError, DeliveryManager
from corvus.evals import run_eval
from corvus.memory import MemoryManager
from corvus.models import MemoryRecord, ModelProvider, RunPhase
from corvus.onboarding import OnboardingChoices, OnboardingError, OnboardingManager
from corvus.onboarding_tui import FirstRunApp
from corvus.orchestration import AgentOrchestrator
from corvus.provider_control import ConfiguredLiveModelController
from corvus.providers import HttpProvider, ModelProviderClient
from corvus.sandbox import (
    DEVELOPMENT_SANDBOX_IMAGE,
    PRODUCTION_SANDBOX_IMAGE,
    DockerSandbox,
    PodmanSandbox,
    SandboxError,
    validate_sandbox_image,
)
from corvus.skills import SkillRegistry
from corvus.store import TraceStore
from corvus.tui import CorvusApp
from corvus.workflow import CodingWorkflow, SandboxBackend

app = typer.Typer(no_args_is_help=False, help="Corvus trusted coding agent")
model_app = typer.Typer(help="Configure model providers")
memory_app = typer.Typer(help="Inspect and control project memory")
skills_app = typer.Typer(help="Inspect and control versioned skills")
app.add_typer(model_app, name="model")
app.add_typer(memory_app, name="memory")
app.add_typer(skills_app, name="skills")
console = Console(stderr=False)


class SandboxOption(StrEnum):
    """User-selectable sandbox routes; ``none`` is deliberately chat-only."""

    AUTO = "auto"
    DOCKER = "docker"
    PODMAN = "podman"
    NONE = "none"


SandboxFactory = Callable[[], SandboxBackend]


@dataclass(frozen=True)
class SandboxRuntime:
    requested: SandboxOption
    backend: Literal["docker", "podman", "none"]
    factory: SandboxFactory | None
    detail: str

    @property
    def available(self) -> bool:
        return self.factory is not None


def resolve_sandbox_runtime(
    requested: SandboxOption | str,
    *,
    image: str | None = None,
    production: bool = True,
    docker_status: tuple[bool, str] | None = None,
    podman_status: tuple[bool, str] | None = None,
) -> SandboxRuntime:
    """Resolve one explicit or automatic route without ever falling back to host execution."""

    selection = requested if isinstance(requested, SandboxOption) else SandboxOption(requested)
    if selection is SandboxOption.NONE:
        return SandboxRuntime(
            requested=selection,
            backend="none",
            factory=None,
            detail=(
                "Chat-only mode selected; ordinary chat is available and isolated /build is "
                "disabled."
            ),
        )
    selected_image = image or (
        PRODUCTION_SANDBOX_IMAGE if production else DEVELOPMENT_SANDBOX_IMAGE
    )
    try:
        validate_sandbox_image(selected_image, production=production)
    except SandboxError as exc:
        return SandboxRuntime(
            requested=selection,
            backend="none",
            factory=None,
            detail=f"Isolated /build is disabled: {exc}.",
        )
    docker_ok, docker_detail = docker_status or DockerSandbox.available()
    podman_ok, podman_detail = podman_status or PodmanSandbox.available()
    if selection is SandboxOption.DOCKER:
        return SandboxRuntime(
            requested=selection,
            backend="docker" if docker_ok else "none",
            factory=(lambda: DockerSandbox(image=selected_image, production=production))
            if docker_ok
            else None,
            detail=(
                f"Docker {docker_detail}"
                if docker_ok
                else f"Docker was selected but is unavailable. {docker_detail}"
            ),
        )
    if selection is SandboxOption.PODMAN:
        return SandboxRuntime(
            requested=selection,
            backend="podman" if podman_ok else "none",
            factory=(lambda: PodmanSandbox(image=selected_image, production=production))
            if podman_ok
            else None,
            detail=(
                f"Podman {podman_detail}"
                if podman_ok
                else f"Podman was selected but is unavailable. {podman_detail}"
            ),
        )
    if docker_ok:
        return SandboxRuntime(
            requested=selection,
            backend="docker",
            factory=lambda: DockerSandbox(image=selected_image, production=production),
            detail=f"Auto selected Docker {docker_detail}",
        )
    if podman_ok:
        return SandboxRuntime(
            requested=selection,
            backend="podman",
            factory=lambda: PodmanSandbox(image=selected_image, production=production),
            detail=f"Auto selected Podman {podman_detail}",
        )
    return SandboxRuntime(
        requested=selection,
        backend="none",
        factory=None,
        detail=(
            "No supported sandbox engine is available. "
            f"Docker: {docker_detail} Podman: {podman_detail} "
            "Ordinary chat remains available; isolated /build is disabled."
        ),
    )


def configured_sandbox_image() -> str | None:
    image = os.environ.get("CORVUS_SANDBOX_IMAGE", "").strip()
    return image or None


def configured_sandbox_option(paths: CorvusPaths) -> SandboxOption:
    """Load the persisted onboarding choice, defaulting safely to auto on corrupt state."""

    try:
        selected = OnboardingManager(paths).load().choices.sandbox_backend
        return SandboxOption(selected)
    except (OnboardingError, ValueError):
        return SandboxOption.AUTO


def context() -> tuple[CorvusPaths, ConfigManager, TraceStore]:
    paths = CorvusPaths()
    paths.ensure()
    return paths, ConfigManager(paths), TraceStore(paths.db)


def configuration_context() -> tuple[CorvusPaths, ConfigManager]:
    """Load configuration without creating or migrating the run database."""

    paths = CorvusPaths()
    paths.ensure()
    return paths, ConfigManager(paths)


def verified_managed_codex_service(scratch: Path | None = None) -> CodexCliService | None:
    """Bind the managed CLI to the full pinned package manifest before execution."""

    try:
        installer = CodexCliInstaller()
        if not installer.install_path.is_file():
            return None
        service = asyncio.run(installer.verify_existing())
    except (CodexInstallError, OSError, RuntimeError):
        return None
    return CodexCliService(service.executable, scratch) if scratch is not None else service


def is_corvus_managed_codex_path(executable: Path) -> bool:
    """Identify managed paths even when their package manifest has been rejected."""

    try:
        managed_root = CodexCliInstaller().install_path.parents[2].resolve(strict=False)
        return executable.resolve(strict=False).is_relative_to(managed_root)
    except (CodexInstallError, OSError, RuntimeError, IndexError):
        return False


def delivery_manager(paths: CorvusPaths) -> DeliveryManager:
    try:
        encoded = keyring.get_password("corvus-delivery", "backup-key")
        if encoded is None:
            encoded = Fernet.generate_key().decode()
            keyring.set_password("corvus-delivery", "backup-key", encoded)
    except KeyringError as exc:
        raise DeliveryError(
            "OS keyring is unavailable; refusing to create plaintext backups"
        ) from exc
    return DeliveryManager(paths.bundles, paths.backups, encoded.encode())


def configured_provider(config: ConfigManager) -> ModelProviderClient | None:
    selected = config.selected_provider()
    if selected is None:
        return None
    return provider_client_for(config, selected)


def provider_client_for(
    config: ConfigManager,
    selected: ModelProvider,
) -> ModelProviderClient | None:
    """Construct one validated configured client without changing the active route."""

    if selected.kind == "codex_cli":
        if selected.executable is None:
            return None
        managed = verified_managed_codex_service(config.paths.cache / "codex")
        selected_is_managed = is_corvus_managed_codex_path(selected.executable)
        if selected_is_managed and managed is None:
            return None
        service = (
            managed
            if selected_is_managed and managed is not None
            else CodexCliService(selected.executable, config.paths.cache / "codex")
        )
        if (
            selected.executable_sha256 is None
            or service.executable_sha256 != selected.executable_sha256
        ):
            return None
        return CodexCliProvider(selected, service)
    try:
        secret = keyring.get_password(
            selected.keyring_service or "corvus-model-provider",
            selected.name,
        )
    except KeyringError:
        secret = None
    return HttpProvider(selected, secret)


def provider_is_ready(provider: ModelProvider) -> bool:
    if provider.kind == "codex_cli":
        if provider.executable is None:
            return False
        try:
            managed = verified_managed_codex_service()
            provider_is_managed = is_corvus_managed_codex_path(provider.executable)
            if provider_is_managed and managed is None:
                return False
            service = (
                managed
                if provider_is_managed and managed is not None
                else CodexCliService(provider.executable)
            )
            if (
                provider.executable_sha256 is None
                or service.executable_sha256 != provider.executable_sha256
            ):
                return False
            return asyncio.run(service.login_status()).ready
        except (OSError, RuntimeError):
            return False
    if provider.local or provider.kind == "ollama":
        return True
    try:
        return (
            keyring.get_password(
                provider.keyring_service or "corvus-model-provider",
                provider.name,
            )
            is not None
        )
    except KeyringError:
        return False


def save_provider_configuration(
    config: ConfigManager,
    provider: ModelProvider,
    secret: str | None,
) -> None:
    """Persist nonsecret metadata and make the new provider active."""

    if provider.kind == "codex_cli":
        if secret is not None or provider.keyring_service is not None:
            raise ValueError("Codex login credentials are owned by the official Codex CLI")
        if provider.executable_sha256 is None:
            raise ValueError("Codex executable identity is required")
    elif secret is not None:
        try:
            keyring.set_password(
                provider.keyring_service or "corvus-model-provider",
                provider.name,
                secret,
            )
        except KeyringError as exc:
            raise RuntimeError("OS keyring is unavailable") from exc
    providers = [item for item in config.providers() if item.name != provider.name]
    providers.append(provider)
    config.save_providers(providers, active_provider=provider.name)


def discover_codex_service(
    config: ConfigManager,
    project: Path | None = None,
) -> CodexCliService | None:
    managed = verified_managed_codex_service(config.paths.cache / "codex")
    selected = config.selected_provider()
    if selected is not None and selected.kind == "codex_cli" and selected.executable is not None:
        selected_is_managed = is_corvus_managed_codex_path(selected.executable)
        saved = (
            managed
            if selected_is_managed and managed is not None
            else CodexCliService(selected.executable, config.paths.cache / "codex")
        )
        if not (selected_is_managed and managed is None) and (
            selected.executable_sha256 is not None
            and saved.executable_sha256 == selected.executable_sha256
            and asyncio.run(saved.available())
        ):
            return saved
    if managed is not None:
        return managed
    service = CodexCliService.discover(project)
    if service is None:
        return None
    if is_corvus_managed_codex_path(service.executable):
        return None
    try:
        service.validate_executable()
    except (OSError, RuntimeError):
        return None
    if not asyncio.run(service.available()):
        return None
    return CodexCliService(service.executable, config.paths.cache / "codex")


def launch_tui(
    paths: CorvusPaths,
    config: ConfigManager,
    store: TraceStore,
    project: Path,
    *,
    allow_subagents: bool | None = None,
    max_subagents: int | None = None,
    sandbox: SandboxOption | None = None,
) -> None:
    selected_provider_config = config.selected_provider()
    provider_label = (
        f"{selected_provider_config.name} / {selected_provider_config.model or 'Codex default'}"
        if selected_provider_config is not None
        else None
    )
    onboarding = OnboardingManager(paths)
    try:
        state = onboarding.load()
    except OnboardingError:
        console.print("[yellow]Onboarding state was invalid and will be recreated.[/yellow]")
        state = onboarding.reset()
    needs_onboarding = not state.completed
    selected_sandbox = SandboxOption(state.choices.sandbox_backend)
    should_probe_sandboxes = (
        needs_onboarding or (sandbox or selected_sandbox) is not SandboxOption.NONE
    )
    if should_probe_sandboxes:
        docker_available, docker_detail = DockerSandbox.available()
        podman_available, podman_detail = PodmanSandbox.available()
    else:
        docker_available, docker_detail = False, "Not checked for this chat-only session."
        podman_available, podman_detail = False, "Not checked for this chat-only session."
    provider_configured = bool(
        selected_provider_config is not None
        and (not needs_onboarding or provider_is_ready(selected_provider_config))
    )

    selected_project = project
    selected_subagents = bool(allow_subagents)
    selected_max = max_subagents or state.choices.max_subagents
    if needs_onboarding:
        codex_service = discover_codex_service(config, project)
        try:
            codex_installer = CodexCliInstaller()
        except CodexInstallError:
            codex_installer = None
        selection = FirstRunApp(
            project=project,
            provider_configured=provider_configured,
            provider_label=provider_label,
            docker_available=docker_available,
            docker_detail=docker_detail,
            podman_available=podman_available,
            podman_detail=podman_detail,
            seed_subagents=bool(allow_subagents),
            seed_max_subagents=selected_max,
            seed_sandbox_backend=(sandbox or selected_sandbox).value,
            existing_provider=selected_provider_config,
            codex_service=codex_service,
            codex_installer=codex_installer,
            provider_saver=lambda provider, secret: save_provider_configuration(
                config, provider, secret
            ),
        ).run()
        if selection is None:
            return
        # The wizard may have created a provider, so never reuse the pre-wizard readiness sample.
        provider_configured = selection.provider_configured
        completion = onboarding.complete(
            OnboardingChoices(
                subagents_enabled=selection.enable_subagents_for_window,
                max_subagents=selection.max_subagents,
                project_path=selection.project,
                privacy_acknowledged=selection.privacy_acknowledged,
                sandbox_backend=selection.sandbox_backend,
            ),
            provider_configured=provider_configured,
            docker_available=docker_available,
            podman_available=podman_available,
        )
        selected_project = selection.project
        selected_subagents = completion.session_subagents_enabled
        selected_max = completion.max_subagents
        selected_sandbox = SandboxOption(completion.sandbox_backend)
    else:
        saved_project = state.choices.project_path
        if project == Path.cwd().resolve() and saved_project is not None and saved_project.is_dir():
            selected_project = saved_project
        # Enabling agents is session consent and is intentionally never restored from disk.
        selected_subagents = bool(allow_subagents)

    sandbox_runtime = resolve_sandbox_runtime(
        sandbox or selected_sandbox,
        image=configured_sandbox_image(),
        docker_status=(docker_available, docker_detail),
        podman_status=(podman_available, podman_detail),
    )

    def workflow_for(client: ModelProviderClient) -> CodingWorkflow | None:
        if sandbox_runtime.factory is None:
            return None
        return CodingWorkflow(
            store,
            client,
            DeliveryManager(paths.bundles, paths.backups),
            paths.cache / "runs",
            sandbox_factory=sandbox_runtime.factory,
            sandbox_name=sandbox_runtime.backend,
        )

    provider = configured_provider(config)
    workflow = workflow_for(provider) if provider is not None else None
    runner = ChatAgent(
        provider,
        provenance=store,
        workflow=workflow,
        project=selected_project,
        allow_subagents=selected_subagents,
        max_subagents=selected_max,
        build_unavailable_reason=(
            sandbox_runtime.detail if sandbox_runtime.factory is None else None
        ),
    )
    model_controller = ConfiguredLiveModelController(
        config,
        runner,
        provider_builder=lambda selected: provider_client_for(config, selected),
        provider_ready=provider_is_ready,
        workflow_builder=workflow_for,
    )
    CorvusApp(
        runner,
        project=selected_project,
        allow_subagents=selected_subagents,
        max_subagents=selected_max,
        model_controller=model_controller,
        sandbox_backend=sandbox_runtime.backend,
    ).run()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(
            "[bold]Corvus[/bold] — sandbox first, evidence always, approval before delivery"
        )
        console.print("Run [cyan]corvus run[/cyan] or [cyan]corvus doctor[/cyan].")


@app.command()
def chat(
    prompt: Annotated[str | None, typer.Argument(help="Work request")] = None,
    project: Annotated[Path | None, typer.Option("--project", exists=True, file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSONL events")] = False,
    plain: Annotated[bool, typer.Option("--plain", help="Use plain terminal output")] = False,
    resume: Annotated[UUID | None, typer.Option("--resume", help="Resume/inspect a run")] = None,
    sandbox: Annotated[
        SandboxOption | None,
        typer.Option(
            "--sandbox",
            case_sensitive=False,
            help="Session sandbox override: auto, docker, podman, or none (chat-only)",
        ),
    ] = None,
) -> None:
    """Start or resume a policy-controlled conversation."""
    paths, config, store = context()
    project = project or Path.cwd()
    orchestrator: AgentOrchestrator
    if resume:
        events = AgentOrchestrator(store).resume(resume)
        for event in events:
            typer.echo(
                event.model_dump_json()
                if json_output
                else f"{event.phase.value}: {event.event_type}"
            )
        return
    if prompt is None and not plain and not json_output:
        launch_tui(paths, config, store, project, sandbox=sandbox)
        return
    if not prompt:
        raise typer.BadParameter("a prompt is required in plain or JSON mode")
    provider = configured_provider(config)
    orchestrator = AgentOrchestrator(store, provider)
    sandbox_runtime = resolve_sandbox_runtime(
        sandbox or configured_sandbox_option(paths),
        image=configured_sandbox_image(),
    )

    async def execute() -> None:
        if provider is not None and sandbox_runtime.factory is not None:
            workflow = CodingWorkflow(
                store,
                provider,
                DeliveryManager(paths.bundles, paths.backups),
                paths.cache / "runs",
                sandbox_factory=sandbox_runtime.factory,
                sandbox_name=sandbox_runtime.backend,
            )
            run_id, _ = await workflow.execute(prompt, project.resolve())
            events = workflow.events(run_id)
        elif provider is not None:
            run_id = uuid4()
            store.append(
                run_id,
                "run.created",
                RunPhase.UNDERSTAND,
                {
                    "prompt": prompt,
                    "project": str(project.resolve()),
                    "autonomy": 3,
                    "sandbox_requested": sandbox_runtime.requested.value,
                },
            )
            store.append(
                run_id,
                "run.blocked",
                RunPhase.BLOCKED,
                {
                    "reason": sandbox_runtime.detail,
                    "sandbox_backend": "none",
                    "host_writes": False,
                },
            )
            events = list(store.events(run_id))
        else:
            events = [event async for event in orchestrator.begin(prompt, project.resolve())]
        for event in events:
            if json_output:
                typer.echo(event.model_dump_json())
            else:
                console.print(f"[cyan]{event.phase.value:>10}[/cyan]  {event.event_type}")
                if event.event_type == "run.blocked":
                    console.print(f"[yellow]{event.payload['reason']}[/yellow]")

    asyncio.run(execute())


@app.command("run")
def run_command(
    project: Annotated[
        Path | None,
        typer.Option("--project", exists=True, file_okay=False, help="Default project context"),
    ] = None,
    subagents: Annotated[
        bool | None,
        typer.Option(
            "--subagents/--no-subagents",
            help="Allow bounded analysis subagents for this Corvus window",
        ),
    ] = None,
    max_subagents: Annotated[
        int | None,
        typer.Option(
            "--max-subagents",
            min=1,
            max=4,
            help="Maximum children per root turn and concurrently",
        ),
    ] = None,
    sandbox: Annotated[
        SandboxOption | None,
        typer.Option(
            "--sandbox",
            case_sensitive=False,
            help=(
                "Sandbox for this window: auto, docker, podman, or none "
                "(chat-only; /build disabled)"
            ),
        ),
    ] = None,
    setup: Annotated[
        bool,
        typer.Option("--setup", help="Run first-time setup again before opening chat"),
    ] = False,
) -> None:
    """Launch the live multi-chat conversational terminal."""
    paths, config, store = context()
    if setup:
        OnboardingManager(paths).reset()
    launch_tui(
        paths,
        config,
        store,
        (project or Path.cwd()).resolve(),
        allow_subagents=subagents,
        max_subagents=max_subagents,
        sandbox=sandbox,
    )


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check runtime, state, keyring, and sandbox health."""
    paths, config, store = context()
    docker_ok, docker_detail = DockerSandbox.available()
    podman_ok, podman_detail = PodmanSandbox.available()
    sandbox_runtime = resolve_sandbox_runtime(
        configured_sandbox_option(paths),
        image=configured_sandbox_image(),
        docker_status=(docker_ok, docker_detail),
        podman_status=(podman_ok, podman_detail),
    )
    db_ok, db_detail = store.integrity_check()
    codex_service = discover_codex_service(config, Path.cwd())
    checks: dict[str, object] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "database": {"ok": db_ok, "detail": db_detail, "path": str(paths.db)},
        "docker": {"ok": docker_ok, "detail": docker_detail},
        "podman": {"ok": podman_ok, "detail": podman_detail},
        "sandbox": {
            "ok": sandbox_runtime.available,
            "requested": sandbox_runtime.requested.value,
            "selected": sandbox_runtime.backend,
            "detail": sandbox_runtime.detail,
        },
        "codex_cli": {
            "ok": codex_service is not None,
            "detail": (
                str(codex_service.executable)
                if codex_service is not None
                else "run `corvus model install-codex`"
            ),
        },
        "providers": len(config.providers()),
        "default_autonomy": int(config.load_policy().autonomy),
    }
    if json_output:
        typer.echo(json.dumps(checks, sort_keys=True))
        return
    table = Table(title="Corvus doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_row("Python", str(checks["python"]))
    table.add_row("SQLite", "ok" if db_ok else db_detail)
    table.add_row(
        "Docker",
        f"ok ({docker_detail})" if docker_ok else f"optional: unavailable ({docker_detail})",
    )
    table.add_row(
        "Podman",
        f"ok ({podman_detail})" if podman_ok else f"optional: unavailable ({podman_detail})",
    )
    table.add_row(
        "Sandbox route",
        (
            f"{sandbox_runtime.backend} ({sandbox_runtime.detail})"
            if sandbox_runtime.available
            else f"chat-only ({sandbox_runtime.detail})"
        ),
    )
    table.add_row(
        "Codex CLI",
        str(codex_service.executable)
        if codex_service is not None
        else "unavailable (run corvus model install-codex)",
    )
    table.add_row("Providers", str(checks["providers"]))
    table.add_row("Autonomy", str(checks["default_autonomy"]))
    console.print(table)


@app.command("trace")
def trace_command(
    run_id: UUID,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect and verify a redacted execution trace."""
    _, _, store = context()
    events = list(store.events(run_id))
    if not events:
        raise typer.BadParameter("run not found")
    if json_output:
        for event in events:
            typer.echo(event.model_dump_json())
    else:
        console.print(f"Trace {run_id} — chain {'valid' if store.verify(run_id) else 'INVALID'}")
        for event in events:
            console.print(f"{event.sequence:04d} {event.phase.value:>10} {event.event_type}")


@app.command()
def review(
    bundle_id: UUID,
    approve: Annotated[
        bool, typer.Option("--approve", help="Explicitly approve and apply")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Review a delivery bundle; JSON mode is always read-only."""
    paths, _, _ = context()
    manager = delivery_manager(paths)
    try:
        bundle = manager.load(bundle_id)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("bundle not found or invalid") from exc
    if json_output:
        typer.echo(bundle.model_dump_json())
        return
    console.print(f"Bundle: {bundle.id}\nDestination: {bundle.destination}")
    console.print(f"Manifest: {bundle.manifest_digest}")
    for changed in bundle.changed_files:
        console.print(f"  • {changed}")
    if approve:
        grant = manager.approve(bundle, actor_id="local-cli")
        try:
            backup = manager.apply(bundle, grant, actor_id="local-cli")
        except DeliveryError as exc:
            console.print(f"[red]Delivery blocked: {exc}[/red]")
            raise typer.Exit(2) from exc
        console.print(f"[green]Applied.[/green] Checkpoint: {backup}")
    else:
        console.print(
            "No changes applied. Re-run with --approve after reviewing the exact manifest."
        )


@app.command()
def undo(delivery_id: UUID) -> None:
    """Undo a delivery only when delivered files have not diverged."""
    paths, _, _ = context()
    manager = delivery_manager(paths)
    try:
        bundle = manager.load(delivery_id)
        approval = manager.approve_undo(bundle, actor_id="local-cli")
        manager.undo(bundle, approval, actor_id="local-cli")
    except (OSError, ValueError, DeliveryError) as exc:
        console.print(f"[red]Undo blocked: {exc}[/red]")
        raise typer.Exit(2) from exc
    console.print("[green]Delivery restored from checkpoint.[/green]")


@app.command("eval")
def eval_command(
    suite: Annotated[Path, typer.Argument(exists=True, dir_okay=False)] = Path(
        "examples/eval.yaml"
    ),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run deterministic, non-live capability evaluations."""
    _, _, store = context()
    result = asyncio.run(run_eval(suite, store))
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        console.print_json(data=result)
    if not result["passed"]:
        raise typer.Exit(1)


@model_app.command("list")
def model_list() -> None:
    _, config = configuration_context()
    active = config.active_provider_name()
    for provider in config.providers():
        marker = "*" if provider.name == active else " "
        location = (
            f"via {provider.executable}"
            if provider.kind == "codex_cli"
            else f"@ {provider.base_url}"
        )
        model = provider.model or "Codex default"
        console.print(f"{marker} {provider.name}: {provider.kind} {model} {location}")


@model_app.command("use")
def model_use(name: str) -> None:
    """Select which configured provider Corvus uses."""

    _, config = configuration_context()
    try:
        config.set_active_provider(name)
    except ValueError as exc:
        raise typer.BadParameter("provider is not configured") from exc
    console.print(f"Active provider: {name}")


@model_app.command("status")
def model_status() -> None:
    """Check the official Codex/ChatGPT sign-in without reading its tokens."""

    _, config = configuration_context()
    service = discover_codex_service(config, Path.cwd())
    if service is None:
        console.print(
            "[yellow]Codex CLI was not found. Run "
            "`corvus model install-codex`, then retry.[/yellow]"
        )
        raise typer.Exit(2)
    console.print(f"Using Codex CLI: {service.executable}")
    status = asyncio.run(service.login_status())
    console.print(status.detail)
    if not status.ready:
        raise typer.Exit(2)


@model_app.command("login")
def model_login() -> None:
    """Open the official Codex browser flow and select it as the active provider."""

    _, config = configuration_context()
    service = discover_codex_service(config, Path.cwd())
    if service is None:
        console.print(
            "[yellow]Codex CLI was not found. Run `corvus model install-codex`, then retry "
            "`corvus model login`.[/yellow]"
        )
        raise typer.Exit(2)
    console.print(f"Using Codex CLI: {service.executable}")
    status = asyncio.run(service.login())
    if not status.ready:
        console.print(f"[yellow]{status.detail}[/yellow]")
        raise typer.Exit(2)
    existing = next(
        (provider for provider in config.providers() if provider.kind == "codex_cli"),
        None,
    )
    provider = (
        existing.model_copy(
            update={
                "executable": service.executable,
                "executable_sha256": service.executable_sha256,
            }
        )
        if existing is not None
        else ModelProvider(
            name="codex-chatgpt",
            kind="codex_cli",
            executable=service.executable,
            executable_sha256=service.executable_sha256,
            model="",
            reasoning_effort="medium",
        )
    )
    save_provider_configuration(config, provider, None)
    console.print("[green]Signed in with ChatGPT through Codex.[/green]")


@model_app.command("install-codex")
def model_install_codex(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve the displayed pinned release noninteractively"),
    ] = False,
) -> None:
    """Install Corvus's tested official Codex CLI release for the current user."""

    try:
        installer = CodexCliInstaller()
    except CodexInstallError as exc:
        console.print(f"[red]Codex CLI installation is unavailable: {exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"Official source: {installer.source}")
    console.print(f"Pinned Codex release: {installer.version}")
    console.print(f"Per-user install path: {installer.install_path}")
    if not yes and not typer.confirm(
        "Download, verify, and safely install this official OpenAI release package?",
        default=False,
    ):
        console.print("No installation was performed.")
        raise typer.Exit(1)
    try:
        service = asyncio.run(installer.install(lambda detail: console.print(f"  {detail}")))
    except CodexInstallError as exc:
        console.print(f"[red]Codex CLI installation failed: {exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]Codex CLI ready: {service.executable}[/green]")
    console.print("Next: run `corvus model login` to sign in with ChatGPT.")


@model_app.command("add")
def model_add(
    name: str,
    kind: Annotated[str, typer.Option("--kind")],
    base_url: Annotated[str, typer.Option("--base-url")],
    model: Annotated[str, typer.Option("--model")],
    local: Annotated[bool, typer.Option("--local")] = False,
) -> None:
    _, config = configuration_context()
    if kind == "codex_cli":
        raise typer.BadParameter("use `corvus model login` for Codex / ChatGPT")
    providers = [item for item in config.providers() if item.name != name]
    providers.append(
        ModelProvider(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            base_url=base_url,
            model=model,
            keyring_service="corvus-model-provider",
            local=local,
        )
    )
    config.save_providers(providers, active_provider=name)
    console.print(f"Configured {name}; no secret was written to YAML.")


@model_app.command("secret")
def model_secret(name: str) -> None:
    _, config = configuration_context()
    provider = next((item for item in config.providers() if item.name == name), None)
    if provider is None:
        raise typer.BadParameter("provider is not configured")
    if provider.kind == "codex_cli":
        raise typer.BadParameter("Codex credentials are managed by `codex login`")
    secret = typer.prompt("Provider secret", hide_input=True, confirmation_prompt=True)
    keyring.set_password(provider.keyring_service or "corvus-model-provider", name, secret)
    console.print("Secret stored through the OS keyring backend.")


@memory_app.command("list")
def memory_list(project_id: UUID, identity: str = "local") -> None:
    _, _, store = context()
    for record in MemoryManager(store).list(project_id, identity):
        console.print(f"{record.id} {record.kind} {record.confidence:.2f} {record.content}")


@memory_app.command("add")
def memory_add(project_id: UUID, kind: str, content: str, identity: str = "local") -> None:
    _, _, store = context()
    record = MemoryRecord(
        project_id=project_id,
        identity_id=identity,
        kind=kind,  # type: ignore[arg-type]
        content=content,
        source="explicit user input",
        confidence=1.0,
    )
    MemoryManager(store).add(record)
    console.print(str(record.id))


@memory_app.command("delete")
def memory_delete(memory_id: UUID, project_id: UUID, identity: str = "local") -> None:
    _, _, store = context()
    deleted = MemoryManager(store).delete(memory_id, project_id, identity)
    raise typer.Exit(0 if deleted else 1)


@memory_app.command("pin")
def memory_pin(memory_id: UUID, project_id: UUID, identity: str = "local") -> None:
    _, _, store = context()
    changed = MemoryManager(store).set_pinned(memory_id, project_id, identity, pinned=True)
    raise typer.Exit(0 if changed else 1)


@memory_app.command("edit")
def memory_edit(memory_id: UUID, project_id: UUID, content: str, identity: str = "local") -> None:
    _, _, store = context()
    changed = MemoryManager(store).edit(memory_id, project_id, identity, content)
    raise typer.Exit(0 if changed else 1)


@memory_app.command("export")
def memory_export(project_id: UUID, identity: str = "local") -> None:
    _, _, store = context()
    records = MemoryManager(store).list(project_id, identity)
    typer.echo(json.dumps([item.model_dump(mode="json") for item in records], default=str))


@skills_app.command("list")
def skills_list() -> None:
    _, _, store = context()
    for name, version in SkillRegistry(store).versions():
        console.print(f"{name} v{version.version} {version.status}")


@skills_app.command("draft")
def skills_draft(name: str, content: str, permission: list[str] | None = None) -> None:
    _, _, store = context()
    version = SkillRegistry(store).create_draft(name, content, permission or [])
    console.print(f"{name} v{version.version} saved as inspectable draft")


@skills_app.command("promote")
def skills_promote(name: str, version: int, passed: bool = False) -> None:
    """Promote only with an explicit passing evaluation result."""
    _, _, store = context()
    SkillRegistry(store).promote(name, version, {"passed": passed, "approved": True})
    console.print(f"{name} v{version} is active")


@skills_app.command("rollback")
def skills_rollback(name: str, version: int) -> None:
    _, _, store = context()
    SkillRegistry(store).rollback(name, version)
    console.print(f"{name} rolled back to v{version}")
