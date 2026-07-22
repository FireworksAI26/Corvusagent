from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import corvus.mvp.trusted_cli as trusted_cli_module
from corvus.mvp.trusted_cli import TrustedCli, TrustedCliError
from corvus.safe_process import TrustedProcessResult


def test_trusted_cli_adds_only_explicit_validated_executable_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "github-cli" / "gh.exe"
    executable.parent.mkdir()
    executable.touch()
    git_directory = tmp_path / "git" / "bin"
    git_directory.mkdir(parents=True)
    config_directory = tmp_path / "github-config"
    config_directory.mkdir()
    untrusted_directory = tmp_path / "untrusted"
    untrusted_directory.mkdir()
    monkeypatch.setenv("PATH", os.fspath(untrusted_directory))
    captured: dict[str, Any] = {}

    def fake_run_trusted_argv(*_args: object, **kwargs: object) -> TrustedProcessResult:
        captured.update(kwargs)
        return TrustedProcessResult(0, b"", b"")

    monkeypatch.setattr(trusted_cli_module, "run_trusted_argv", fake_run_trusted_argv)
    cli = TrustedCli(
        executable,
        environment={"GH_CONFIG_DIR": os.fspath(config_directory)},
        additional_path_entries=(git_directory,),
    )

    result = cli.run(tmp_path, ("repo", "list"))

    environment = captured["env"]
    assert isinstance(environment, dict)
    path_entries = environment["PATH"].split(os.pathsep)
    assert path_entries[0] == os.fspath(git_directory.resolve())
    assert os.fspath(executable.parent.resolve()) in path_entries
    assert os.fspath(git_directory.resolve()) in path_entries
    assert os.fspath(untrusted_directory.resolve()) not in path_entries
    assert environment["GH_CONFIG_DIR"] == os.fspath(config_directory)
    assert result.returncode == 0


def test_trusted_cli_rejects_path_overrides_and_missing_extra_directories(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "gh.exe"
    executable.touch()

    with pytest.raises(TrustedCliError, match="trusted_cli_environment_invalid"):
        TrustedCli(executable, environment={"PATH": os.fspath(tmp_path)})
    with pytest.raises(TrustedCliError, match="trusted_cli_path_entry_unavailable"):
        TrustedCli(executable, additional_path_entries=(tmp_path / "missing-git",))
