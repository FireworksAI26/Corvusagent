from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from corvus.config import CorvusPaths
from corvus.models import StrictModel, now_utc
from corvus.security import atomic_write

OnboardingFormat = Literal["yaml", "json"]
SandboxBackendChoice = Literal["auto", "docker", "podman", "none"]
WizardStepId = Literal["privacy", "project", "provider", "subagents", "docker", "finish"]


class OnboardingError(RuntimeError):
    """Base error for persisted onboarding state."""


class OnboardingStateError(OnboardingError):
    """Raised when persisted onboarding state is unreadable or invalid."""


class OnboardingIncompleteError(OnboardingError):
    def __init__(self, missing_steps: Sequence[str]) -> None:
        self.missing_steps = tuple(missing_steps)
        super().__init__(f"onboarding is incomplete: {', '.join(missing_steps)}")


class WizardStepStatus(StrEnum):
    COMPLETE = "complete"
    CURRENT = "current"
    PENDING = "pending"
    WARNING = "warning"


class OnboardingChoices(StrictModel):
    """Wizard selections; subagent enablement is intentionally session-only."""

    subagents_enabled: bool | None = None
    max_subagents: int = Field(default=2, ge=1, le=4)
    project_path: Path | None = None
    privacy_acknowledged: bool = False
    sandbox_backend: SandboxBackendChoice = "auto"

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve(strict=False) if value is not None else None


class OnboardingState(StrictModel):
    schema_version: Literal[1] = 1
    choices: OnboardingChoices = Field(default_factory=OnboardingChoices)
    completed: bool = False
    provider_configured_at_completion: bool | None = None
    docker_available_at_completion: bool | None = None
    podman_available_at_completion: bool | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def completed_state_has_timestamp(self) -> OnboardingState:
        if self.completed and self.completed_at is None:
            raise ValueError("completed onboarding state requires completed_at")
        if not self.completed and self.completed_at is not None:
            raise ValueError("incomplete onboarding state cannot have completed_at")
        return self


class WizardStep(StrictModel):
    id: WizardStepId
    title: str
    status: WizardStepStatus
    required: bool
    detail: str


class OnboardingStatus(StrictModel):
    first_run: bool
    completed: bool
    ready_to_complete: bool
    current_step: WizardStepId | None
    provider_configured: bool
    docker_available: bool
    podman_available: bool = False
    sandbox_ready: bool = False
    selected_sandbox_backend: SandboxBackendChoice
    choices: OnboardingChoices
    steps: list[WizardStep]
    warnings: list[str] = Field(default_factory=list)


class OnboardingCompletion(StrictModel):
    state: OnboardingState
    session_subagents_enabled: bool
    max_subagents: int
    sandbox_backend: SandboxBackendChoice


class OnboardingManager:
    """Atomically persist non-secret onboarding preferences and completion state."""

    def __init__(
        self,
        paths: CorvusPaths,
        *,
        state_format: OnboardingFormat = "yaml",
    ) -> None:
        if state_format not in {"yaml", "json"}:
            raise ValueError("state_format must be 'yaml' or 'json'")
        self.paths = paths
        self.paths.ensure()
        self.state_format = state_format
        self.state_path = paths.config / f"onboarding.{state_format}"

    def load(self) -> OnboardingState:
        if not self.state_path.exists():
            return OnboardingState()
        try:
            text = self.state_path.read_text(encoding="utf-8")
            loaded: object
            if self.state_format == "json":
                loaded = json.loads(text)
            else:
                loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise OnboardingStateError("onboarding state must be an object")
            state = OnboardingState.model_validate(loaded)
        except OnboardingStateError:
            raise
        except (OSError, json.JSONDecodeError, yaml.YAMLError, ValidationError) as exc:
            raise OnboardingStateError("onboarding state is unreadable or invalid") from exc
        return self._sanitize_state(state)

    def save_choices(self, choices: OnboardingChoices) -> OnboardingState:
        existing = self.load()
        state = OnboardingState(
            choices=self._sanitize_choices(choices),
            completed=False,
            created_at=existing.created_at,
            updated_at=now_utc(),
        )
        self._write(state)
        return state

    def save(self, choices: OnboardingChoices) -> OnboardingState:
        return self.save_choices(choices)

    def status(
        self,
        *,
        provider_configured: bool,
        docker_available: bool,
        docker_detail: str | None = None,
        podman_available: bool = False,
        podman_detail: str | None = None,
        choices: OnboardingChoices | None = None,
    ) -> OnboardingStatus:
        state = self.load()
        effective_choices = choices or state.choices
        return self._status_for(
            state,
            effective_choices,
            provider_configured=provider_configured,
            docker_available=docker_available,
            docker_detail=docker_detail,
            podman_available=podman_available,
            podman_detail=podman_detail,
        )

    def complete(
        self,
        choices: OnboardingChoices,
        *,
        provider_configured: bool,
        docker_available: bool,
        podman_available: bool = False,
    ) -> OnboardingCompletion:
        existing = self.load()
        candidate = OnboardingState(
            choices=self._sanitize_choices(choices),
            completed=False,
            created_at=existing.created_at,
            updated_at=now_utc(),
        )
        status = self._status_for(
            candidate,
            choices,
            provider_configured=provider_configured,
            docker_available=docker_available,
            docker_detail=None,
            podman_available=podman_available,
            podman_detail=None,
        )
        if not status.ready_to_complete:
            missing = [
                step.id
                for step in status.steps
                if step.required and step.status is not WizardStepStatus.COMPLETE
            ]
            raise OnboardingIncompleteError(missing)
        if choices.subagents_enabled is None:
            # Defensive guard in case required steps change independently.
            raise OnboardingIncompleteError(["subagents"])
        completed_at = now_utc()
        state = OnboardingState(
            choices=self._sanitize_choices(choices),
            completed=True,
            provider_configured_at_completion=provider_configured,
            docker_available_at_completion=docker_available,
            podman_available_at_completion=podman_available,
            created_at=existing.created_at,
            updated_at=completed_at,
            completed_at=completed_at,
        )
        self._write(state)
        return OnboardingCompletion(
            state=state,
            session_subagents_enabled=choices.subagents_enabled,
            max_subagents=choices.max_subagents,
            sandbox_backend=choices.sandbox_backend,
        )

    def reset(self) -> OnboardingState:
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError as exc:
            raise OnboardingStateError("could not reset onboarding state") from exc
        return OnboardingState()

    def is_first_run(self) -> bool:
        return not self.load().completed

    def needs_onboarding(self) -> bool:
        return self.is_first_run()

    def _write(self, state: OnboardingState) -> None:
        state = self._sanitize_state(state)
        payload = state.model_dump(mode="json", exclude_none=True)
        if self.state_format == "json":
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        else:
            encoded = yaml.safe_dump(
                payload,
                allow_unicode=True,
                sort_keys=True,
            ).encode()
        try:
            atomic_write(self.state_path, encoded)
        except OSError as exc:
            raise OnboardingStateError("could not persist onboarding state") from exc

    def _status_for(
        self,
        state: OnboardingState,
        choices: OnboardingChoices,
        *,
        provider_configured: bool,
        docker_available: bool,
        docker_detail: str | None,
        podman_available: bool,
        podman_detail: str | None,
    ) -> OnboardingStatus:
        project_valid = self._project_is_valid(choices.project_path)
        required_done: dict[WizardStepId, bool] = {
            "privacy": choices.privacy_acknowledged,
            "project": project_valid,
            "subagents": choices.subagents_enabled is not None,
        }
        ready = state.completed or all(required_done.values())
        current: WizardStepId | None = None
        if not state.completed:
            required_order: tuple[WizardStepId, ...] = ("privacy", "project", "subagents")
            current = next(
                (step for step in required_order if not required_done[step]),
                "finish" if ready else None,
            )

        def required_status(step_id: WizardStepId) -> WizardStepStatus:
            if required_done[step_id]:
                return WizardStepStatus.COMPLETE
            if current == step_id:
                return WizardStepStatus.CURRENT
            return WizardStepStatus.PENDING

        project_detail = "Choose an existing project directory."
        if choices.project_path is not None:
            project_detail = (
                f"Project directory: {choices.project_path}"
                if project_valid
                else f"Project directory is unavailable: {choices.project_path}"
            )
        provider_detail = (
            "A model provider is configured."
            if provider_configured
            else "No model provider is configured; chat cannot answer until one is added."
        )
        sandbox_ready, sandbox_step_detail = self._sandbox_status(
            choices.sandbox_backend,
            docker_available=docker_available,
            docker_detail=docker_detail,
            podman_available=podman_available,
            podman_detail=podman_detail,
        )
        if choices.subagents_enabled is None:
            subagent_detail = (
                f"Choose whether to enable up to {choices.max_subagents} subagents this session."
            )
        elif choices.subagents_enabled:
            subagent_detail = (
                f"Enable up to {choices.max_subagents} subagents for this session only."
            )
        else:
            subagent_detail = "Keep subagents disabled for this session."
        steps = [
            WizardStep(
                id="privacy",
                title="Privacy and approval",
                status=required_status("privacy"),
                required=True,
                detail=(
                    "Privacy and approval boundaries acknowledged."
                    if choices.privacy_acknowledged
                    else "Acknowledge sandbox, provider, and approval boundaries."
                ),
            ),
            WizardStep(
                id="project",
                title="Project directory",
                status=required_status("project"),
                required=True,
                detail=project_detail,
            ),
            WizardStep(
                id="provider",
                title="Model provider",
                status=(
                    WizardStepStatus.COMPLETE if provider_configured else WizardStepStatus.WARNING
                ),
                required=False,
                detail=provider_detail,
            ),
            WizardStep(
                id="subagents",
                title="Subagents",
                status=(
                    WizardStepStatus.COMPLETE
                    if state.completed and choices.subagents_enabled is None
                    else required_status("subagents")
                ),
                required=True,
                detail=subagent_detail,
            ),
            WizardStep(
                id="docker",
                title="Sandbox engine",
                status=(WizardStepStatus.COMPLETE if sandbox_ready else WizardStepStatus.WARNING),
                required=False,
                detail=sandbox_step_detail,
            ),
            WizardStep(
                id="finish",
                title="Finish",
                status=(
                    WizardStepStatus.COMPLETE
                    if state.completed
                    else WizardStepStatus.CURRENT
                    if ready
                    else WizardStepStatus.PENDING
                ),
                required=False,
                detail=(
                    "Onboarding is complete."
                    if state.completed
                    else "Save these choices and start chatting."
                    if ready
                    else "Complete the required steps first."
                ),
            ),
        ]
        warnings: list[str] = []
        if not provider_configured:
            warnings.append("No model provider is configured.")
        if not sandbox_ready:
            warnings.append(sandbox_step_detail)
        return OnboardingStatus(
            first_run=not state.completed,
            completed=state.completed,
            ready_to_complete=ready,
            current_step=current,
            provider_configured=provider_configured,
            docker_available=docker_available,
            podman_available=podman_available,
            sandbox_ready=sandbox_ready,
            selected_sandbox_backend=choices.sandbox_backend,
            choices=choices,
            steps=steps,
            warnings=warnings,
        )

    @staticmethod
    def _sandbox_status(
        selected: SandboxBackendChoice,
        *,
        docker_available: bool,
        docker_detail: str | None,
        podman_available: bool,
        podman_detail: str | None,
    ) -> tuple[bool, str]:
        if selected == "none":
            return (
                False,
                "Chat-only mode is selected explicitly; /build is disabled and Corvus will "
                "not execute build commands on the host.",
            )
        if selected == "docker":
            if docker_available:
                return True, docker_detail or "Docker is ready for isolated builds."
            detail = docker_detail or "Docker is unavailable."
            return (
                False,
                f"Docker is selected but unavailable; /build will fail closed. {detail}",
            )
        if selected == "podman":
            if podman_available:
                return True, podman_detail or "Podman is ready for isolated builds."
            detail = podman_detail or "Podman is unavailable."
            return (
                False,
                f"Podman is selected but unavailable; /build will fail closed. {detail}",
            )
        if docker_available:
            return (
                True,
                "Auto selected Docker, the first available isolated engine. "
                f"{docker_detail or 'Docker is ready.'}",
            )
        if podman_available:
            return (
                True,
                "Auto selected Podman because Docker is unavailable. "
                f"{podman_detail or 'Podman is ready.'}",
            )
        docker_reason = docker_detail or "unavailable"
        podman_reason = podman_detail or "unavailable"
        return (
            False,
            "Auto found no isolated sandbox engine; /build will fail closed and never run "
            f"on the host. Docker: {docker_reason} Podman: {podman_reason}",
        )

    @staticmethod
    def _project_is_valid(project_path: Path | None) -> bool:
        if project_path is None:
            return False
        try:
            return project_path.exists() and project_path.is_dir()
        except OSError:
            return False

    @staticmethod
    def _sanitize_choices(choices: OnboardingChoices) -> OnboardingChoices:
        return choices.model_copy(update={"subagents_enabled": None})

    @classmethod
    def _sanitize_state(cls, state: OnboardingState) -> OnboardingState:
        return state.model_copy(update={"choices": cls._sanitize_choices(state.choices)})
