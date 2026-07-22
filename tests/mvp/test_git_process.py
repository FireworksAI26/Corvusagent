from __future__ import annotations

import os
from pathlib import Path

import pytest

from corvus.mvp.git_process import GitProcess, GitProcessError, ProcessResult
from corvus.safe_process import TrustedProcessResult


class RecordingExecutor:
    def __init__(self, result: TrustedProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], Path, float, dict[str, str]]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> TrustedProcessResult:
        self.calls.append((argv, cwd, timeout_seconds, env))
        return self.result


def test_git_process_preserves_each_argument_and_uses_explicit_environment(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"git")
    executor = RecordingExecutor(TrustedProcessResult(0, b"ok\n", b""))

    result = GitProcess(executable, executor=executor).run(
        tmp_path,
        ("commit", "-m", "title with spaces; $(ignored)"),
        timeout=7,
    )

    assert result == ProcessResult(returncode=0, stdout=b"ok\n", stderr=b"")
    assert executor.calls[0][0] == (
        str(executable.resolve()),
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "commit",
        "-m",
        "title with spaces; $(ignored)",
    )
    assert executor.calls[0][1:3] == (tmp_path.resolve(), 7)
    assert executor.calls[0][3]["GIT_TERMINAL_PROMPT"] == "0"


def test_git_process_rejects_invalid_arguments(tmp_path: Path) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"git")

    with pytest.raises(GitProcessError, match="arguments are invalid"):
        GitProcess(executable).run(tmp_path, ("status", "bad\0value"))


def test_git_process_rejects_output_over_limit(tmp_path: Path) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"git")
    executor = RecordingExecutor(TrustedProcessResult(0, b"x" * 17, b""))

    with pytest.raises(GitProcessError, match="output limit"):
        GitProcess(executable, executor=executor, max_output_bytes=16).run(tmp_path, ("status",))


def test_git_process_maps_executor_failure_without_leaking_details(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "git.exe"
    executable.write_bytes(b"git")

    def fail(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> TrustedProcessResult:
        del argv, cwd, timeout_seconds, env
        raise RuntimeError("ghp_super_secret")

    with pytest.raises(GitProcessError) as raised:
        GitProcess(executable, executor=fail).run(tmp_path, ("status",))

    assert "ghp_super_secret" not in str(raised.value)
