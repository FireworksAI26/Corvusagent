from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.sandbox import CommandResult
from corvus.security import SecretRedactor
from corvus.store import ArtifactStore
from corvus.verification import VerificationEngine, VerificationPolicy


def test_verification_policy_keeps_trusted_checks_and_smoke_ahead_of_model_suggestions() -> None:
    policy = VerificationPolicy(
        required_commands=(("python", "-m", "compileall", "-q", "."),),
        smoke_commands=(("python", "-c", "print('smoke')"),),
    )

    planned = policy.plan(
        model_commands=[["python", "-c", "print('suggested')"]],
        model_smoke=["python", "-c", "print('model-smoke')"],
    )

    assert [(item.source, item.required, item.command) for item in planned] == [
        ("trusted_required", True, ("python", "-m", "compileall", "-q", ".")),
        ("trusted_smoke", True, ("python", "-c", "print('smoke')")),
        ("model_suggested", False, ("python", "-c", "print('suggested')")),
        ("model_suggested", False, ("python", "-c", "print('model-smoke')")),
    ]


def test_verification_policy_requires_real_trusted_checks() -> None:
    with pytest.raises(ValueError, match="required command"):
        VerificationPolicy(required_commands=())
    with pytest.raises(ValueError, match="must not be empty"):
        VerificationPolicy(required_commands=((),))


class _OutputSandbox:
    def __init__(self, output: str) -> None:
        self.output = output

    async def run(self, command: list[str], timeout_seconds: float = 300) -> CommandResult:
        del command, timeout_seconds
        return CommandResult(exit_code=0, stdout=self.output, stderr="")


def test_verification_engine_redacts_and_bounds_output_before_persistence(
    tmp_path: Path,
) -> None:
    canary = "corvus-canary-value-4402"
    redactor = SecretRedactor([canary])
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engine = VerificationEngine(artifacts, redactor=redactor, max_output_characters=48)
    sandbox = _OutputSandbox(("A" * 20) + canary + ("B" * 100))

    result = asyncio.run(
        engine.command(
            sandbox,  # type: ignore[arg-type]
            uuid4(),
            "required-1",
            ["python", "-c", canary],
        )
    )

    assert result.output.endswith("[TRUNCATED]")
    assert len(result.output) <= 48
    assert canary not in result.output
    assert canary not in result.evidence[0].description
    assert artifacts.get(result.evidence[0].artifact_digest).decode("utf-8") == result.output


def test_verification_engine_requires_positive_output_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_output_characters must be positive"):
        VerificationEngine(ArtifactStore(tmp_path / "artifacts"), max_output_characters=0)
