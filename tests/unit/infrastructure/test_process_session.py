from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

import corvus.infrastructure.agent_runtimes.process_session as process_session_module
from corvus.infrastructure.agent_runtimes.process_session import (
    ProcessInvocation,
    ProcessSession,
    ProcessSessionError,
    ProcessSessionEvent,
    ProcessSessionEventKind,
    ProcessSessionLimits,
)
from corvus.security import SecretRedactor

_SHORT_TIMEOUT_SECONDS = 5.0
_SHORT_GRACE_SECONDS = 0.5


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _limits(**updates: object) -> ProcessSessionLimits:
    values: dict[str, object] = {
        "max_stdin_bytes": 1_024,
        "max_stdout_bytes": 65_536,
        "max_stderr_bytes": 16_384,
        "max_frame_bytes": 4_096,
        "max_frames": 20,
        "max_events": 21,
        "max_arguments": 32,
        "max_argument_bytes": 16_384,
        "timeout_seconds": _SHORT_TIMEOUT_SECONDS,
        "cancellation_grace_seconds": _SHORT_GRACE_SECONDS,
    }
    values.update(updates)
    return ProcessSessionLimits(**values)


def _invocation(
    tmp_path: Path,
    code: str,
    *,
    limits: ProcessSessionLimits | None = None,
    stdin: bytes | None = None,
    environment: dict[str, str] | None = None,
    executable: Path | None = None,
    executable_sha256: str | None = None,
    cwd: Path | None = None,
    approved_roots: tuple[Path, ...] | None = None,
    arguments: tuple[str, ...] | None = None,
) -> ProcessInvocation:
    executable_path = (executable or Path(sys.executable)).resolve()
    working_directory = (cwd or tmp_path).resolve()
    return ProcessInvocation(
        executable=executable_path,
        executable_sha256=executable_sha256 or _digest(executable_path),
        arguments=arguments or ("-c", code),
        cwd=working_directory,
        approved_roots=approved_roots or (tmp_path.resolve(),),
        environment=environment or {},
        stdin=stdin,
        limits=limits or _limits(),
    )


async def _collect(session: ProcessSession, after_sequence: int = 0) -> list[ProcessSessionEvent]:
    return [event async for event in session.events(after_sequence=after_sequence)]


def _process_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


async def _wait_gone(pid: int, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _process_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _process_exists(pid), f"process {pid} remained alive"


def _emergency_kill(pid: int) -> None:
    if not _process_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - canonical OS tool used only for test cleanup
            [
                str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                "/PID",
                str(pid),
                "/T",
                "/F",
            ],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def test_process_contracts_are_frozen_and_limits_validate(tmp_path: Path) -> None:
    limits = _limits()
    invocation = _invocation(tmp_path, "print('{}')", limits=limits)

    with pytest.raises(FrozenInstanceError):
        limits.max_events = 100  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        invocation.cwd = tmp_path.parent  # type: ignore[misc]
    with pytest.raises(ValueError, match="process_session_event_capacity_invalid"):
        _limits(max_events=0)
    with pytest.raises(ValueError, match="process_session_frame_count_invalid"):
        _limits(max_frames=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    (
        ("digest", "process_executable_digest_mismatch"),
        ("relative_executable", "process_executable_invalid"),
        ("cwd_escape", "process_cwd_outside_approved_roots"),
        ("blank_argument", "process_arguments_invalid"),
        ("nul_argument", "process_arguments_invalid"),
        ("argument_count", "process_argument_limit_exceeded"),
        ("argument_bytes", "process_argument_bytes_exceeded"),
        ("stdin", "process_stdin_limit_exceeded"),
        ("sensitive_environment", "process_environment_key_forbidden"),
        ("python_injection", "process_environment_key_forbidden"),
        ("shell_executable", "process_shell_executable_forbidden"),
    ),
)
async def test_process_session_rejects_untrusted_invocations_before_spawn(
    tmp_path: Path,
    mutation: str,
    reason_code: str,
) -> None:
    kwargs: dict[str, object] = {}
    if mutation == "digest":
        kwargs["executable_sha256"] = "0" * 64
    elif mutation == "relative_executable":
        kwargs["executable"] = Path("python")
        kwargs["executable_sha256"] = "a" * 64
    elif mutation == "cwd_escape":
        kwargs["cwd"] = tmp_path.parent
    elif mutation == "blank_argument":
        kwargs["arguments"] = ("",)
    elif mutation == "nul_argument":
        kwargs["arguments"] = ("bad\0argument",)
    elif mutation == "argument_count":
        kwargs["arguments"] = ("one", "two", "three")
        kwargs["limits"] = _limits(max_arguments=2)
    elif mutation == "argument_bytes":
        kwargs["arguments"] = ("x" * 20,)
        kwargs["limits"] = _limits(max_argument_bytes=10)
    elif mutation == "stdin":
        kwargs["stdin"] = b"too-long"
        kwargs["limits"] = _limits(max_stdin_bytes=3)
    elif mutation == "sensitive_environment":
        kwargs["environment"] = {"OPENAI_API_KEY": "secret"}
    elif mutation == "python_injection":
        kwargs["environment"] = {"PYTHONPATH": "untrusted"}
    elif mutation == "shell_executable":
        shell_name = "cmd.exe" if os.name == "nt" else "sh"
        shell = next(
            (
                candidate
                for candidate in (
                    Path(os.environ.get("SystemRoot", "")) / "System32" / shell_name,
                    Path("/bin") / shell_name,
                    Path("/usr/bin") / shell_name,
                )
                if candidate.is_file()
            ),
            Path(sys.executable),
        ).resolve()
        if shell == Path(sys.executable).resolve():
            pytest.skip("a shell executable is unavailable on this host")
        kwargs["executable"] = shell
        kwargs["executable_sha256"] = _digest(shell)

    with pytest.raises(ProcessSessionError) as error:
        await ProcessSession.start(_invocation(tmp_path, "print('{}')", **kwargs))
    assert error.value.reason_code == reason_code
    assert str(error.value) == reason_code


@pytest.mark.asyncio
async def test_process_session_rejects_symlink_or_reparse_roots(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(ProcessSessionError) as error:
        await ProcessSession.start(
            _invocation(
                tmp_path,
                "print('{}')",
                cwd=target,
                approved_roots=(link,),
            )
        )
    assert error.value.reason_code == "process_approved_root_link_forbidden"


@pytest.mark.asyncio
async def test_session_parses_split_crlf_and_final_frames_with_clean_environment_and_redaction(
    tmp_path: Path,
) -> None:
    code = """
import os, sys, time
assert os.environ['CORVUS_TEST_VALUE'] == 'allowed'
assert os.environ.get('OPENAI_API_KEY') is None
assert os.environ.get('PYTHONPATH') is None
assert os.path.isabs(os.environ['PATH'].split(os.pathsep)[0])
os.write(1, b'{\"kind\":\"first\",\"api_key\":\"needle-')
time.sleep(0.02)
os.write(1, b'secret\"}\\r')
os.write(1, b'\\n{\"kind\":\"second\",\"nested\":{\"token\":\"needle-secret\"}}')
os.write(2, b'Bearer needle-secret')
"""
    session = await ProcessSession.start(
        _invocation(tmp_path, code, environment={"CORVUS_TEST_VALUE": "allowed"}),
        redactor=SecretRedactor(["needle-secret"]),
    )
    events = await _collect(session)

    assert [event.kind for event in events] == [
        ProcessSessionEventKind.FRAME,
        ProcessSessionEventKind.FRAME,
        ProcessSessionEventKind.EXITED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[0].frame is not None and events[0].frame["kind"] == "first"
    assert events[1].frame is not None and events[1].frame["kind"] == "second"
    assert events[-1].return_code == 0
    serialized = repr(events)
    assert "needle-secret" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_expression", "reason_code"),
    (
        ("b'\\xff\\n'", "process_stdout_invalid_utf8"),
        ("b'{bad json}\\n'", "process_frame_json_invalid"),
        ('b\'{"a":1,"a":2}\\n\'', "process_frame_duplicate_key"),
        ("b'[1,2]\\n'", "process_frame_object_required"),
        ("b'1\\n'", "process_frame_object_required"),
        ("b'{\"value\":NaN}\\n'", "process_frame_nonfinite_number"),
    ),
)
async def test_session_fails_closed_on_invalid_stream_frames(
    tmp_path: Path,
    payload_expression: str,
    reason_code: str,
) -> None:
    code = f"import os, time; os.write(1, {payload_expression}); time.sleep(60)"
    session = await ProcessSession.start(_invocation(tmp_path, code))
    events = await _collect(session)

    assert events[-1].kind is ProcessSessionEventKind.FAILED
    assert events[-1].reason_code == reason_code
    assert sum(event.kind in ProcessSessionEventKind.terminal_kinds() for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "limits", "reason_code"),
    (
        (
            'import os, time; os.write(1, b\'{"x":"123456789"}\\n\'); time.sleep(60)',
            _limits(max_stdout_bytes=8),
            "process_stdout_limit_exceeded",
        ),
        (
            'import os, time; os.write(1, b\'{"x":"123456789"}\\n\'); time.sleep(60)',
            _limits(max_frame_bytes=8),
            "process_frame_limit_exceeded",
        ),
        (
            "import os, time; os.write(2, b'stderr-overflow'); time.sleep(60)",
            _limits(max_stderr_bytes=4),
            "process_stderr_limit_exceeded",
        ),
    ),
)
async def test_session_enforces_every_stream_byte_limit(
    tmp_path: Path,
    code: str,
    limits: ProcessSessionLimits,
    reason_code: str,
) -> None:
    session = await ProcessSession.start(_invocation(tmp_path, code, limits=limits))
    events = await _collect(session)
    assert events == [
        ProcessSessionEvent(
            sequence=1,
            kind=ProcessSessionEventKind.FAILED,
            reason_code=reason_code,
        )
    ]


@pytest.mark.asyncio
async def test_event_limit_reserves_exactly_one_terminal_slot(tmp_path: Path) -> None:
    code = 'import os, time; os.write(1, b\'{"n":1}\\n{"n":2}\\n\'); time.sleep(60)'
    session = await ProcessSession.start(
        _invocation(tmp_path, code, limits=_limits(max_frames=2, max_events=2))
    )
    events = await _collect(session)

    assert [event.kind for event in events] == [
        ProcessSessionEventKind.FRAME,
        ProcessSessionEventKind.FAILED,
    ]
    assert events[-1].reason_code == "process_event_limit_exceeded"


@pytest.mark.asyncio
async def test_frame_count_limit_reserves_a_terminal_slot(tmp_path: Path) -> None:
    code = 'import os, time; os.write(1, b\'{"n":1}\\n{"n":2}\\n\'); time.sleep(60)'
    session = await ProcessSession.start(
        _invocation(tmp_path, code, limits=_limits(max_frames=1, max_events=3))
    )
    events = await _collect(session)

    assert [event.kind for event in events] == [
        ProcessSessionEventKind.FRAME,
        ProcessSessionEventKind.FAILED,
    ]
    assert events[-1].reason_code == "process_frame_count_exceeded"


@pytest.mark.asyncio
async def test_resume_replays_same_live_session_and_validates_cursor(tmp_path: Path) -> None:
    code = 'import os; os.write(1, b\'{"n":1}\\n{"n":2}\\n\')'
    session = await ProcessSession.start(_invocation(tmp_path, code))
    events = await _collect(session)

    replayed = [event async for event in session.resume(after_sequence=1)]
    assert replayed == events[1:]
    for cursor in (-1, len(events) + 1):
        with pytest.raises(ProcessSessionError) as error:
            [event async for event in session.resume(after_sequence=cursor)]
        assert error.value.reason_code == "process_event_cursor_invalid"


@pytest.mark.asyncio
async def test_timeout_terminates_before_emitting_one_terminal_event(tmp_path: Path) -> None:
    session = await ProcessSession.start(
        _invocation(
            tmp_path,
            "import time; time.sleep(60)",
            limits=_limits(timeout_seconds=0.1, cancellation_grace_seconds=0.05),
        )
    )
    events = await _collect(session)
    assert events == [
        ProcessSessionEvent(
            sequence=1,
            kind=ProcessSessionEventKind.FAILED,
            reason_code="process_timeout",
        )
    ]


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_terminates_parent_and_child_tree(tmp_path: Path) -> None:
    code = """
import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(json.dumps({'parent_pid': os.getpid(), 'child_pid': child.pid}), flush=True)
time.sleep(60)
"""
    session = await ProcessSession.start(
        _invocation(tmp_path, code, limits=_limits(timeout_seconds=30))
    )
    stream = session.events()
    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert first.frame is not None
    parent_pid = cast(int, first.frame["parent_pid"])
    child_pid = cast(int, first.frame["child_pid"])
    try:
        results = await asyncio.gather(session.cancel(), session.cancel())
        remaining = [event async for event in stream]
        assert results == [True, True]
        assert [event.kind for event in [first, *remaining]].count(
            ProcessSessionEventKind.CANCELLED
        ) == 1
        await _wait_gone(parent_pid)
        await _wait_gone(child_pid)
    finally:
        _emergency_kill(parent_pid)
        _emergency_kill(child_pid)


@pytest.mark.asyncio
async def test_consumer_task_cancellation_waits_for_tree_cleanup_before_propagating(
    tmp_path: Path,
) -> None:
    code = """
import json, os, time
print(json.dumps({'pid': os.getpid()}), flush=True)
time.sleep(60)
"""
    session = await ProcessSession.start(
        _invocation(tmp_path, code, limits=_limits(timeout_seconds=30))
    )
    observed_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def consume() -> None:
        async for event in session.events():
            if event.frame is not None and not observed_pid.done():
                observed_pid.set_result(cast(int, event.frame["pid"]))

    consumer = asyncio.create_task(consume())
    pid = await asyncio.wait_for(observed_pid, timeout=5)
    try:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert not _process_exists(pid)
        events = [event async for event in session.resume(after_sequence=0)]
        assert events[-1].kind is ProcessSessionEventKind.CANCELLED
        assert events[-1].reason_code == "process_consumer_cancelled"
    finally:
        _emergency_kill(pid)


@pytest.mark.asyncio
async def test_start_reads_stdout_while_feeding_large_stdin_without_pipe_deadlock(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "pipe-parent.pid"
    stdin_payload = b"i" * 4_194_304
    code = f"""
import json, os, pathlib, sys
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')
frame = json.dumps({{'blob': 'o' * 3_145_728}}, separators=(',', ':')).encode() + b'\\n'
os.write(1, frame)
received = sys.stdin.buffer.read()
print(json.dumps({{'stdin_bytes': len(received)}}), flush=True)
"""
    parent_pid: int | None = None
    try:
        session = await asyncio.wait_for(
            ProcessSession.start(
                _invocation(
                    tmp_path,
                    code,
                    stdin=stdin_payload,
                    limits=_limits(
                        max_stdin_bytes=5_000_000,
                        max_stdout_bytes=5_000_000,
                        max_frame_bytes=4_000_000,
                        timeout_seconds=10,
                    ),
                )
            ),
            timeout=2,
        )
        events = await asyncio.wait_for(_collect(session), timeout=12)
        frames = [event.frame for event in events if event.frame is not None]
        assert frames[-1] is not None
        assert frames[-1]["stdin_bytes"] == len(stdin_payload)
        assert events[-1].kind is ProcessSessionEventKind.EXITED
    finally:
        if pid_path.exists():
            parent_pid = int(pid_path.read_text(encoding="utf-8"))
        if parent_pid is not None:
            _emergency_kill(parent_pid)


@pytest.mark.asyncio
async def test_start_cancellation_recovers_spawn_handle_and_confirms_tree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "spawn-child.pid"
    code = f"""
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')
time.sleep(60)
"""
    real_create = process_session_module.create_grouped_process
    created: asyncio.Future[asyncio.subprocess.Process] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def delayed_create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real_create(*args, **kwargs)  # type: ignore[arg-type]
        created.set_result(process)
        await release.wait()
        return process

    monkeypatch.setattr(process_session_module, "create_grouped_process", delayed_create)
    start_task = asyncio.create_task(
        ProcessSession.start(_invocation(tmp_path, code, limits=_limits(timeout_seconds=10)))
    )
    process = await asyncio.wait_for(created, timeout=5)
    deadline = time.monotonic() + 5
    while not child_pid_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    try:
        start_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert not _process_exists(process.pid)
        assert not _process_exists(child_pid)
    finally:
        release.set()
        _emergency_kill(process.pid)
        _emergency_kill(child_pid)
