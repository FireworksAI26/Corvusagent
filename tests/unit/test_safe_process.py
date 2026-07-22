from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest

import corvus.safe_process as safe_process_module
from corvus.safe_process import (
    TrustedProcessError,
    build_clean_process_environment,
    create_grouped_process,
    run_trusted_argv,
    terminate_process_tree,
)


def test_trusted_process_runs_absolute_executable_without_a_shell(tmp_path: Path) -> None:
    result = run_trusted_argv(
        [sys.executable, "-c", "print('safe-process-ok')"],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert result.returncode == 0
    assert result.stdout.decode().strip() == "safe-process-ok"
    assert result.stderr == b""


def test_trusted_process_rejects_path_lookup_and_missing_working_directory(tmp_path: Path) -> None:
    with pytest.raises(TrustedProcessError, match="absolute regular file"):
        run_trusted_argv(["python", "-V"], cwd=tmp_path)
    with pytest.raises(TrustedProcessError, match="working directory"):
        run_trusted_argv([sys.executable, "-V"], cwd=tmp_path / "missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows trusted tool path semantics")
def test_clean_windows_environment_includes_only_existing_admin_git_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_directory = tmp_path / "Windows"
    system_directory = windows_directory / "System32"
    git_directory = tmp_path / "Program Files" / "Git" / "cmd"
    executable = tmp_path / "provider" / "provider.exe"
    system_directory.mkdir(parents=True)
    git_directory.mkdir(parents=True)
    executable.parent.mkdir()
    executable.write_bytes(b"provider")
    monkeypatch.setattr(safe_process_module, "windows_system_directory", lambda: system_directory)

    environment = build_clean_process_environment(executable, {})

    path_entries = environment["PATH"].split(os.pathsep)
    assert str(git_directory.resolve()) in path_entries
    assert str(tmp_path / "untrusted") not in path_entries


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL command semantics")
def test_codex_sandbox_traverse_targets_only_validated_sid(
    tmp_path: Path,
) -> None:
    sandbox_sid = "S-1-5-21-100-200-300-400"
    safe_process_module.grant_windows_sid_traverse(tmp_path, sandbox_sid)

    assert sandbox_sid in safe_process_module.windows_directory_acl_sids(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows token semantics")
def test_current_logon_sid_is_session_scoped() -> None:
    logon_sid = safe_process_module.windows_current_logon_sid()

    assert logon_sid is not None
    assert logon_sid.startswith("S-1-5-5-")


@pytest.mark.skipif(os.name != "nt", reason="Windows token semantics")
def test_current_user_sid_matches_process_identity() -> None:
    user_sid = safe_process_module.windows_current_user_sid()

    assert user_sid is not None
    assert user_sid.startswith("S-1-5-")


def test_trusted_process_stops_reading_when_combined_output_exceeds_limit(
    tmp_path: Path,
) -> None:
    with pytest.raises(TrustedProcessError, match="output limit"):
        run_trusted_argv(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()",
            ],
            cwd=tmp_path,
            timeout_seconds=10,
            max_output_bytes=128,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
async def test_tree_termination_confirms_descendants_after_leader_exits_first(
    tmp_path: Path,
) -> None:
    executable = Path(sys.executable).resolve()
    code = """
import json, signal, subprocess, sys
child = subprocess.Popen([
    sys.executable,
    '-c',
    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({'child_pid': child.pid}), flush=True)
"""
    process = await create_grouped_process(
        (str(executable), "-c", code),
        cwd=tmp_path,
        env=build_clean_process_environment(executable, {}),
        stdin=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    payload = await asyncio.wait_for(process.stdout.readline(), timeout=5)
    child_pid = int(__import__("json").loads(payload)["child_pid"])
    await asyncio.wait_for(process.wait(), timeout=5)
    try:
        assert await terminate_process_tree(process, grace_seconds=0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_posix_group_confirmation_escalates_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_group_id = 42_424
    signal_kill = 9
    group_present = True
    sent_signals: list[int] = []

    def fake_kill_process_group(group_id: int, sent_signal: int) -> None:
        nonlocal group_present
        assert group_id == process_group_id
        if sent_signal == 0:
            if not group_present:
                raise ProcessLookupError
            return
        sent_signals.append(sent_signal)
        if sent_signal == signal_kill:
            group_present = False

    class _ReapedLeader:
        pid = process_group_id
        returncode = 0

        async def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        safe_process_module.os,
        "killpg",
        fake_kill_process_group,
        raising=False,
    )
    monkeypatch.setattr(safe_process_module.signal, "SIGKILL", signal_kill, raising=False)

    confirmed = await safe_process_module._terminate_posix_process_tree(
        cast(asyncio.subprocess.Process, _ReapedLeader()),
        grace_seconds=0.01,
    )

    assert confirmed
    assert sent_signals == [int(signal.SIGTERM), signal_kill]
    assert not group_present


@pytest.mark.asyncio
async def test_windows_taskkill_parent_race_confirms_observed_tree_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taskkill = tmp_path / "taskkill.exe"
    taskkill.write_bytes(b"trusted-test-binary")
    snapshots = iter(({42_424: 1, 42_425: 42_424}, {}))

    class _ExitedLeader:
        pid = 42_424
        returncode = 1

        async def wait(self) -> int:
            return 1

    class _RacingTaskkill:
        returncode = 255

        async def wait(self) -> int:
            return self.returncode

    async def fake_subprocess(*args: object, **kwargs: object) -> _RacingTaskkill:
        del args, kwargs
        return _RacingTaskkill()

    monkeypatch.setattr(safe_process_module, "windows_system_directory", lambda: tmp_path)
    monkeypatch.setattr(
        safe_process_module,
        "build_clean_process_environment",
        lambda executable, explicit: {},
    )
    monkeypatch.setattr(
        safe_process_module,
        "_windows_process_snapshot",
        lambda: next(snapshots),
        raising=False,
    )
    monkeypatch.setattr(safe_process_module.asyncio, "create_subprocess_exec", fake_subprocess)

    confirmed = await safe_process_module._terminate_windows_process_tree(
        cast(asyncio.subprocess.Process, _ExitedLeader())
    )

    assert confirmed
