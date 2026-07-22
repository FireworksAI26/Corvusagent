from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from corvus.models import CriterionStatus, Evidence, VerificationResult
from corvus.sandbox import CommandResult
from corvus.security import SecretRedactor
from corvus.store import ArtifactStore


class VerificationSandbox(Protocol):
    async def run(self, command: list[str], timeout_seconds: float = 300) -> CommandResult: ...


@dataclass(frozen=True)
class VerificationCommand:
    criterion_id: str
    command: tuple[str, ...]
    source: Literal["trusted_required", "trusted_smoke", "model_suggested"]
    required: bool


@dataclass(frozen=True)
class VerificationPolicy:
    required_commands: tuple[tuple[str, ...], ...] = (("python", "-m", "compileall", "-q", "."),)
    smoke_commands: tuple[tuple[str, ...], ...] = (
        ("python", "-c", "from pathlib import Path; assert Path('.').is_dir()"),
    )
    timeout_seconds: float = 300
    max_commands: int = 64
    max_arguments_per_command: int = 128

    def __post_init__(self) -> None:
        required = tuple(tuple(command) for command in self.required_commands)
        smoke = tuple(tuple(command) for command in self.smoke_commands)
        object.__setattr__(self, "required_commands", required)
        object.__setattr__(self, "smoke_commands", smoke)
        if not required:
            raise ValueError("at least one trusted required command is required")
        if self.timeout_seconds <= 0:
            raise ValueError("verification timeout_seconds must be positive")
        if self.max_commands <= 0:
            raise ValueError("verification max_commands must be positive")
        if self.max_arguments_per_command <= 0:
            raise ValueError("verification max_arguments_per_command must be positive")
        for command in required + smoke:
            self._validate_command(command)
        if len(required) + len(smoke) > self.max_commands:
            raise ValueError("trusted verification policy exceeds command limit")

    def _validate_command(self, command: tuple[str, ...]) -> None:
        if not command:
            raise ValueError("verification command must not be empty")
        if len(command) > self.max_arguments_per_command:
            raise ValueError("verification command exceeds argument limit")
        if any(not isinstance(argument, str) or not argument for argument in command):
            raise ValueError("verification command arguments must be non-empty strings")

    def plan(
        self,
        *,
        model_commands: list[list[str]] | None = None,
        model_smoke: list[str] | None = None,
    ) -> tuple[VerificationCommand, ...]:
        planned: list[VerificationCommand] = []
        seen: set[tuple[str, ...]] = set()

        def add(
            command: tuple[str, ...],
            source: Literal["trusted_required", "trusted_smoke", "model_suggested"],
            required: bool,
        ) -> None:
            self._validate_command(command)
            if command in seen:
                return
            if len(planned) >= self.max_commands:
                raise ValueError("verification plan exceeds command limit")
            seen.add(command)
            planned.append(
                VerificationCommand(
                    criterion_id=f"{source}-{len(planned) + 1}",
                    command=command,
                    source=source,
                    required=required,
                )
            )

        for trusted_command in self.required_commands:
            add(trusted_command, "trusted_required", True)
        for smoke_command in self.smoke_commands:
            add(smoke_command, "trusted_smoke", True)
        for suggested_command in model_commands or []:
            add(tuple(suggested_command), "model_suggested", False)
        if model_smoke is not None:
            add(tuple(model_smoke), "model_suggested", False)
        return tuple(planned)


class VerificationEngine:
    def __init__(
        self,
        artifacts: ArtifactStore,
        redactor: SecretRedactor | None = None,
        *,
        max_output_characters: int = 32_000,
    ) -> None:
        if max_output_characters <= 0:
            raise ValueError("max_output_characters must be positive")
        self.artifacts = artifacts
        self.redactor = redactor or SecretRedactor()
        self.max_output_characters = max_output_characters

    async def command(
        self,
        sandbox: VerificationSandbox,
        trace_id: UUID,
        criterion_id: str,
        command: list[str],
        timeout_seconds: float = 300,
    ) -> VerificationResult:
        started = time.monotonic()
        result = await sandbox.run(command, timeout_seconds)
        bounded = self.redactor.bound_text(
            result.stdout + result.stderr,
            max_characters=self.max_output_characters,
        )
        digest, _ = self.artifacts.put(bounded.text.encode())
        command_description = self.redactor.bound_text(
            f"Command {' '.join(command)} exited {result.exit_code}",
            max_characters=1024,
        ).text
        evidence = Evidence(
            artifact_digest=digest,
            media_type="text/plain",
            description=(
                f"{command_description}; original_sha256={bounded.original_sha256}; "
                f"captured_sha256={bounded.captured_sha256}; "
                f"original_bytes={bounded.original_bytes}; "
                f"original_chars={bounded.original_chars}; "
                f"captured_bytes={bounded.captured_bytes}; "
                f"captured_chars={bounded.captured_chars}"
            ),
            trace_id=trace_id,
        )
        return VerificationResult(
            criterion_id=criterion_id,
            status=CriterionStatus.PASSED if result.exit_code == 0 else CriterionStatus.FAILED,
            method="sandbox command",
            evidence=[evidence],
            output=bounded.text,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def completion_allowed(results: list[VerificationResult], required: set[str]) -> bool:
        passed = {item.criterion_id for item in results if item.status == CriterionStatus.PASSED}
        return required <= passed
