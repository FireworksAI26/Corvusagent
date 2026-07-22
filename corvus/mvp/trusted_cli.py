from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from corvus.mvp.git_process import ProcessResult
from corvus.safe_process import (
    TrustedProcessError,
    build_clean_process_environment,
    path_is_link_or_reparse,
    run_trusted_argv,
)


class TrustedCliError(RuntimeError):
    """A sanitized failure from an explicitly discovered local CLI."""


class TrustedCli:
    def __init__(
        self,
        executable: Path,
        *,
        environment: Mapping[str, str] | None = None,
        additional_path_entries: tuple[Path, ...] = (),
    ) -> None:
        try:
            self._executable = executable.expanduser().resolve(strict=True)
        except OSError as exc:
            raise TrustedCliError("trusted_cli_unavailable") from exc
        if not self._executable.is_file() or path_is_link_or_reparse(self._executable):
            raise TrustedCliError("trusted_cli_unavailable")
        inherited = {
            key: value
            for key in (
                "APPDATA",
                "HOME",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "XDG_CONFIG_HOME",
            )
            if (value := os.environ.get(key)) is not None
        }
        if environment is not None and any(key.casefold() == "path" for key in environment):
            raise TrustedCliError("trusted_cli_environment_invalid")
        if environment is not None:
            inherited.update(environment)
        try:
            self._environment = build_clean_process_environment(self._executable, inherited)
            trusted_path_entries = self._trusted_path_entries(additional_path_entries)
        except (OSError, TrustedProcessError) as exc:
            raise TrustedCliError("trusted_cli_environment_unavailable") from exc
        if trusted_path_entries:
            current_path = self._environment.get("PATH", "")
            current_entries = tuple(entry for entry in current_path.split(os.pathsep) if entry)
            combined_entries = dict.fromkeys((*trusted_path_entries, *current_entries))
            self._environment["PATH"] = os.pathsep.join(combined_entries)

    @staticmethod
    def _trusted_path_entries(entries: tuple[Path, ...]) -> tuple[str, ...]:
        trusted: list[str] = []
        for entry in entries:
            try:
                canonical = entry.expanduser().resolve(strict=True)
            except OSError as exc:
                raise TrustedCliError("trusted_cli_path_entry_unavailable") from exc
            if not canonical.is_dir() or path_is_link_or_reparse(canonical):
                raise TrustedCliError("trusted_cli_path_entry_unavailable")
            trusted.append(os.fspath(canonical))
        return tuple(trusted)

    def run(self, cwd: Path, args: tuple[str, ...], timeout: float = 30) -> ProcessResult:
        if not args or any(not argument or "\0" in argument for argument in args):
            raise TrustedCliError("trusted_cli_arguments_invalid")
        try:
            working_directory = cwd.expanduser().resolve(strict=True)
        except OSError as exc:
            raise TrustedCliError("trusted_cli_working_directory_unavailable") from exc
        if not working_directory.is_dir() or path_is_link_or_reparse(working_directory):
            raise TrustedCliError("trusted_cli_working_directory_unavailable")
        try:
            result = run_trusted_argv(
                (os.fspath(self._executable), *args),
                cwd=working_directory,
                timeout_seconds=timeout,
                env=dict(self._environment),
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise TrustedCliError("trusted_cli_execution_failed") from exc
        if len(result.stdout) + len(result.stderr) > 2 * 1024 * 1024:
            raise TrustedCliError("trusted_cli_output_limit_exceeded")
        return ProcessResult(result.returncode, result.stdout, result.stderr)
