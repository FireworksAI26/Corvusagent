from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from corvus.safe_process import (
    TrustedProcessError,
    TrustedProcessResult,
    build_clean_process_environment,
    path_is_link_or_reparse,
    run_trusted_argv,
)


class GitProcessError(RuntimeError):
    """A safe, user-displayable failure from a trusted Git-family executable."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


type ProcessExecutor = Callable[
    ...,
    TrustedProcessResult,
]

_PASSTHROUGH_ENVIRONMENT = (
    "APPDATA",
    "HOME",
    "LOCALAPPDATA",
    "SSH_AUTH_SOCK",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
)


class GitProcess:
    """Execute a trusted Git-family binary without involving a command shell."""

    def __init__(
        self,
        executable: Path,
        *,
        executor: ProcessExecutor = run_trusted_argv,
        max_output_bytes: int = 2 * 1024 * 1024,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        try:
            canonical = executable.expanduser().resolve(strict=True)
        except OSError as exc:
            raise GitProcessError("trusted executable is unavailable") from exc
        if not canonical.is_file() or path_is_link_or_reparse(canonical):
            raise GitProcessError("trusted executable is unavailable")
        if max_output_bytes <= 0:
            raise GitProcessError("output limit must be positive")
        self._executable = canonical
        self._executor = executor
        self._max_output_bytes = max_output_bytes
        explicit = {
            key: value
            for key in _PASSTHROUGH_ENVIRONMENT
            if (value := os.environ.get(key)) is not None
        }
        explicit.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        if environment is not None:
            explicit.update(environment)
        try:
            self._environment = build_clean_process_environment(canonical, explicit)
        except (OSError, TrustedProcessError) as exc:
            raise GitProcessError("trusted process environment is unavailable") from exc

    def run(
        self,
        cwd: Path,
        args: tuple[str, ...],
        timeout: float = 30,
    ) -> ProcessResult:
        if not args or any(not argument or "\0" in argument for argument in args):
            raise GitProcessError("process arguments are invalid")
        try:
            working_directory = cwd.expanduser().resolve(strict=True)
        except OSError as exc:
            raise GitProcessError("working directory is unavailable") from exc
        if not working_directory.is_dir() or path_is_link_or_reparse(working_directory):
            raise GitProcessError("working directory is unavailable")
        if timeout <= 0:
            raise GitProcessError("process timeout must be positive")
        # Repository and global Git config are untrusted input. In particular,
        # core.fsmonitor can execute a command during otherwise read-only status
        # inspection, while core.hooksPath can run repository-controlled hooks
        # during supervised commits. Command-line config has the highest
        # precedence and applies consistently to every Git invocation.
        argv = (
            os.fspath(self._executable),
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
        )
        try:
            result = self._executor(
                argv,
                cwd=working_directory,
                timeout_seconds=timeout,
                env=dict(self._environment),
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise GitProcessError("trusted process execution failed") from exc
        if len(result.stdout) + len(result.stderr) > self._max_output_bytes:
            raise GitProcessError("trusted process exceeded its output limit")
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
