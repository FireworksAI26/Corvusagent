from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

from corvus.safe_process import (
    build_clean_process_environment,
    create_grouped_process,
    path_is_link_or_reparse,
    terminate_process_tree,
)
from corvus.security import SecretRedactor, is_sensitive_field_name

DEFAULT_MAX_STDIN_BYTES = 1_048_576
DEFAULT_MAX_STDOUT_BYTES = 8_388_608
DEFAULT_MAX_STDERR_BYTES = 1_048_576
DEFAULT_MAX_FRAME_BYTES = 1_048_576
DEFAULT_MAX_FRAMES = 10_000
DEFAULT_MAX_EVENTS = 10_001
DEFAULT_MAX_ARGUMENTS = 256
DEFAULT_MAX_ARGUMENT_BYTES = 131_072
DEFAULT_TIMEOUT_SECONDS = 3_600.0
DEFAULT_CANCELLATION_GRACE_SECONDS = 2.0
_READ_CHUNK_BYTES = 65_536
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_APPROVED_EXPLICIT_ENVIRONMENT_KEYS = frozenset({"COLORTERM", "NO_COLOR", "TERM"})
_APPROVED_EXPLICIT_ENVIRONMENT_PREFIXES = ("CORVUS_",)
_RESERVED_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "SYSTEMROOT",
        "WINDIR",
    }
)
_SHELL_EXECUTABLE_NAMES = frozenset(
    {
        "bash",
        "cmd.exe",
        "csh",
        "dash",
        "fish",
        "ksh",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "tcsh",
        "zsh",
    }
)


class ProcessSessionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _ProcessProtocolFailure(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ProcessSessionLimits:
    max_stdin_bytes: int = DEFAULT_MAX_STDIN_BYTES
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    max_frames: int = DEFAULT_MAX_FRAMES
    max_events: int = DEFAULT_MAX_EVENTS
    max_arguments: int = DEFAULT_MAX_ARGUMENTS
    max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cancellation_grace_seconds: float = DEFAULT_CANCELLATION_GRACE_SECONDS

    def __post_init__(self) -> None:
        integer_limits = {
            "process_session_stdin_capacity_invalid": self.max_stdin_bytes,
            "process_session_stdout_capacity_invalid": self.max_stdout_bytes,
            "process_session_stderr_capacity_invalid": self.max_stderr_bytes,
            "process_session_frame_capacity_invalid": self.max_frame_bytes,
            "process_session_frame_count_invalid": self.max_frames,
            "process_session_event_capacity_invalid": self.max_events,
            "process_session_argument_capacity_invalid": self.max_arguments,
            "process_session_argument_bytes_invalid": self.max_argument_bytes,
        }
        for reason_code, value in integer_limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(reason_code)
        if self.max_events < 1:
            raise ValueError("process_session_event_capacity_invalid")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("process_session_timeout_invalid")
        if (
            not isinstance(self.cancellation_grace_seconds, (int, float))
            or self.cancellation_grace_seconds <= 0
        ):
            raise ValueError("process_session_cancellation_grace_invalid")


@dataclass(frozen=True)
class ProcessInvocation:
    executable: Path
    executable_sha256: str
    arguments: tuple[str, ...]
    cwd: Path
    approved_roots: tuple[Path, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    limits: ProcessSessionLimits = field(default_factory=ProcessSessionLimits)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "approved_roots", tuple(self.approved_roots))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        if self.stdin is not None:
            object.__setattr__(self, "stdin", bytes(self.stdin))


class ProcessSessionEventKind(StrEnum):
    FRAME = "frame"
    EXITED = "exited"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def terminal_kinds(cls) -> frozenset[ProcessSessionEventKind]:
        return frozenset({cls.EXITED, cls.CANCELLED, cls.FAILED})


@dataclass(frozen=True)
class ProcessSessionEvent:
    sequence: int
    kind: ProcessSessionEventKind
    frame: Mapping[str, object] | None = None
    return_code: int | None = None
    stderr: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("process_event_sequence_invalid")
        if self.frame is not None:
            object.__setattr__(self, "frame", _freeze_mapping(self.frame))


class ProcessSession:
    def __init__(
        self,
        *,
        invocation: ProcessInvocation,
        process: asyncio.subprocess.Process,
        redactor: SecretRedactor,
    ) -> None:
        self._invocation = invocation
        self._process = process
        self._redactor = redactor
        self._events: list[ProcessSessionEvent] = []
        self._stderr = b""
        self._terminal = False
        self._cancel_reason: str | None = None
        self._condition = asyncio.Condition()
        self._terminal_lock = asyncio.Lock()
        self._supervisor = asyncio.create_task(
            self._supervise(),
            name="corvus-process-session-supervisor",
        )

    @classmethod
    async def start(
        cls,
        invocation: ProcessInvocation,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ProcessSession:
        executable, cwd, environment = _validate_invocation(invocation)
        # This immediate recheck pins the intended executable identity. It is not an
        # atomic OS sandbox or a substitute for provider tool/sandbox restrictions.
        if _sha256_file(executable) != invocation.executable_sha256:
            raise ProcessSessionError("process_executable_digest_mismatch")
        stdin_mode = (
            asyncio.subprocess.PIPE if invocation.stdin is not None else asyncio.subprocess.DEVNULL
        )
        spawn_task = asyncio.create_task(
            create_grouped_process(
                (os.fspath(executable), *invocation.arguments),
                cwd=cwd,
                env=environment,
                stdin=stdin_mode,
            ),
            name="corvus-process-session-spawn",
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError as cancellation:
            try:
                process = await _await_spawn_completion(spawn_task)
            except Exception:
                raise cancellation from None
            confirmed = await _shielded_spawn_cleanup(
                process,
                grace_seconds=invocation.limits.cancellation_grace_seconds,
            )
            if not confirmed:
                raise ProcessSessionError("process_tree_termination_unconfirmed") from None
            raise cancellation
        except (OSError, RuntimeError, ValueError):
            raise ProcessSessionError("process_spawn_failed") from None
        return cls(
            invocation=invocation,
            process=process,
            redactor=redactor or SecretRedactor(),
        )

    async def events(self, after_sequence: int = 0) -> AsyncIterator[ProcessSessionEvent]:
        try:
            async for event in self._iterate(after_sequence):
                yield event
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._cancel("process_consumer_cancelled"),
                name="corvus-process-session-consumer-cleanup",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup
            raise

    def resume(self, after_sequence: int) -> AsyncIterator[ProcessSessionEvent]:
        return self.events(after_sequence=after_sequence)

    async def cancel(self) -> bool:
        return await self._cancel("process_cancelled")

    async def _iterate(self, after_sequence: int) -> AsyncIterator[ProcessSessionEvent]:
        async with self._condition:
            if (
                not isinstance(after_sequence, int)
                or isinstance(after_sequence, bool)
                or after_sequence < 0
                or after_sequence > len(self._events)
            ):
                raise ProcessSessionError("process_event_cursor_invalid")
        next_index = after_sequence
        while True:
            async with self._condition:
                while next_index >= len(self._events) and not self._terminal:
                    await self._condition.wait()
                available = tuple(self._events[next_index:])
                terminal = self._terminal
            for event in available:
                next_index += 1
                yield event
            if terminal and next_index >= len(self._events):
                return

    async def _supervise(self) -> None:
        stdout = self._process.stdout
        stderr = self._process.stderr
        if stdout is None or stderr is None:  # pragma: no cover - configured by start
            await self._fail("process_stream_unavailable")
            return
        stdout_task = asyncio.create_task(self._read_stdout(stdout))
        stderr_task = asyncio.create_task(self._read_stderr(stderr))
        stdin_task = asyncio.create_task(self._feed_stdin())
        wait_task = asyncio.create_task(self._process.wait())
        tasks = (stdout_task, stderr_task, stdin_task, wait_task)
        failure_reason: str | None = None
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self._invocation.limits.timeout_seconds,
            )
        except TimeoutError:
            failure_reason = "process_timeout"
        except _ProcessProtocolFailure as exc:
            failure_reason = exc.reason_code
        except asyncio.CancelledError:
            failure_reason = "process_supervisor_cancelled"
        except Exception:
            failure_reason = "process_internal_failure"
        if failure_reason is not None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._fail(failure_reason)
            return
        await self._finish_natural_exit()

    async def _feed_stdin(self) -> None:
        payload = self._invocation.stdin
        if payload is None:
            return
        writer = self._process.stdin
        if writer is None:  # pragma: no cover - guarded by PIPE selection
            raise _ProcessProtocolFailure("process_stdin_unavailable")
        try:
            writer.write(payload)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise _ProcessProtocolFailure("process_stdin_write_failed") from None
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def _read_stdout(self, reader: asyncio.StreamReader) -> None:
        limits = self._invocation.limits
        total = 0
        frame_count = 0
        buffer = bytearray()
        while True:
            chunk = await reader.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limits.max_stdout_bytes:
                raise _ProcessProtocolFailure("process_stdout_limit_exceeded")
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw_frame = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if raw_frame.endswith(b"\r"):
                    raw_frame = raw_frame[:-1]
                frame_count += 1
                await self._accept_frame(raw_frame, frame_count=frame_count)
            if len(buffer) > limits.max_frame_bytes:
                raise _ProcessProtocolFailure("process_frame_limit_exceeded")
        if buffer:
            frame_count += 1
            await self._accept_frame(bytes(buffer), frame_count=frame_count)

    async def _read_stderr(self, reader: asyncio.StreamReader) -> None:
        limits = self._invocation.limits
        buffer = bytearray()
        while True:
            chunk = await reader.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            if len(buffer) + len(chunk) > limits.max_stderr_bytes:
                raise _ProcessProtocolFailure("process_stderr_limit_exceeded")
            buffer.extend(chunk)
        try:
            bytes(buffer).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _ProcessProtocolFailure("process_stderr_invalid_utf8") from None
        self._stderr = bytes(buffer)

    async def _accept_frame(self, raw_frame: bytes, *, frame_count: int) -> None:
        limits = self._invocation.limits
        if len(raw_frame) > limits.max_frame_bytes:
            raise _ProcessProtocolFailure("process_frame_limit_exceeded")
        if frame_count > limits.max_frames:
            raise _ProcessProtocolFailure("process_frame_count_exceeded")
        try:
            text = raw_frame.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _ProcessProtocolFailure("process_stdout_invalid_utf8") from None
        try:
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_number,
            )
        except _ProcessProtocolFailure:
            raise
        except (json.JSONDecodeError, TypeError, ValueError):
            raise _ProcessProtocolFailure("process_frame_json_invalid") from None
        if not isinstance(value, dict):
            raise _ProcessProtocolFailure("process_frame_object_required")
        redacted = self._redactor.redact_value(value)
        if not isinstance(redacted, dict):  # pragma: no cover - dict input stays dict
            raise _ProcessProtocolFailure("process_frame_redaction_failed")
        frozen = _freeze_mapping(redacted)
        async with self._condition:
            if len(self._events) >= limits.max_events - 1:
                raise _ProcessProtocolFailure("process_event_limit_exceeded")
            self._events.append(
                ProcessSessionEvent(
                    sequence=len(self._events) + 1,
                    kind=ProcessSessionEventKind.FRAME,
                    frame=frozen,
                )
            )
            self._condition.notify_all()

    async def _cancel(self, reason_code: str) -> bool:
        async with self._terminal_lock:
            if self._terminal:
                return True
            self._cancel_reason = reason_code
            confirmed = await terminate_process_tree(
                self._process,
                grace_seconds=self._invocation.limits.cancellation_grace_seconds,
            )
            if confirmed:
                await self._append_terminal(
                    ProcessSessionEventKind.CANCELLED,
                    reason_code=reason_code,
                )
            else:
                await self._append_terminal(
                    ProcessSessionEventKind.FAILED,
                    reason_code="process_tree_termination_unconfirmed",
                )
            return confirmed

    async def _fail(self, reason_code: str) -> None:
        async with self._terminal_lock:
            if self._terminal:
                return
            confirmed = await terminate_process_tree(
                self._process,
                grace_seconds=self._invocation.limits.cancellation_grace_seconds,
            )
            await self._append_terminal(
                ProcessSessionEventKind.FAILED,
                reason_code=(reason_code if confirmed else "process_tree_termination_unconfirmed"),
            )

    async def _finish_natural_exit(self) -> None:
        async with self._terminal_lock:
            if self._terminal:
                return
            if self._cancel_reason is not None:
                await self._append_terminal(
                    ProcessSessionEventKind.CANCELLED,
                    reason_code=self._cancel_reason,
                )
                return
            try:
                stderr = self._stderr.decode("utf-8", errors="strict")
            except UnicodeDecodeError:  # pragma: no cover - validated in reader
                await self._append_terminal(
                    ProcessSessionEventKind.FAILED,
                    reason_code="process_stderr_invalid_utf8",
                )
                return
            await self._append_terminal(
                ProcessSessionEventKind.EXITED,
                return_code=int(self._process.returncode or 0),
                stderr=self._redactor.redact(stderr),
            )

    async def _append_terminal(
        self,
        kind: ProcessSessionEventKind,
        *,
        return_code: int | None = None,
        stderr: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        if self._terminal:
            return
        async with self._condition:
            self._events.append(
                ProcessSessionEvent(
                    sequence=len(self._events) + 1,
                    kind=kind,
                    return_code=return_code,
                    stderr=stderr,
                    reason_code=reason_code,
                )
            )
            self._terminal = True
            self._condition.notify_all()


def _validate_invocation(
    invocation: ProcessInvocation,
) -> tuple[Path, Path, dict[str, str]]:
    executable = invocation.executable
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or executable != executable.resolve(strict=True)
        or path_is_link_or_reparse(executable)
        or _path_contains_link(executable)
    ):
        raise ProcessSessionError("process_executable_invalid")
    if executable.name.lower() in _SHELL_EXECUTABLE_NAMES:
        raise ProcessSessionError("process_shell_executable_forbidden")
    if not _SHA256_PATTERN.fullmatch(invocation.executable_sha256):
        raise ProcessSessionError("process_executable_digest_invalid")
    if _sha256_file(executable) != invocation.executable_sha256:
        raise ProcessSessionError("process_executable_digest_mismatch")
    limits = invocation.limits
    if len(invocation.arguments) > limits.max_arguments or any(
        not isinstance(argument, str) for argument in invocation.arguments
    ):
        raise ProcessSessionError("process_argument_limit_exceeded")
    if any(not argument or "\0" in argument for argument in invocation.arguments):
        raise ProcessSessionError("process_arguments_invalid")
    argument_bytes = sum(len(argument.encode("utf-8")) for argument in invocation.arguments)
    if argument_bytes > limits.max_argument_bytes:
        raise ProcessSessionError("process_argument_bytes_exceeded")
    if invocation.stdin is not None and len(invocation.stdin) > limits.max_stdin_bytes:
        raise ProcessSessionError("process_stdin_limit_exceeded")
    cwd = _validate_working_directory(invocation.cwd, invocation.approved_roots)
    explicit_environment = _validate_explicit_environment(invocation.environment)
    try:
        environment = build_clean_process_environment(executable, explicit_environment)
    except (OSError, RuntimeError, ValueError):
        raise ProcessSessionError("process_environment_unavailable") from None
    return executable, cwd, environment


def _validate_working_directory(cwd: Path, approved_roots: tuple[Path, ...]) -> Path:
    if not cwd.is_absolute() or not cwd.is_dir() or cwd != cwd.resolve(strict=True):
        raise ProcessSessionError("process_cwd_invalid")
    if not approved_roots:
        raise ProcessSessionError("process_approved_root_required")
    canonical_roots: list[Path] = []
    for root in approved_roots:
        if path_is_link_or_reparse(root):
            raise ProcessSessionError("process_approved_root_link_forbidden")
        if not root.is_absolute() or not root.is_dir() or root != root.resolve(strict=True):
            raise ProcessSessionError("process_approved_root_invalid")
        if _path_contains_link(root):
            raise ProcessSessionError("process_approved_root_link_forbidden")
        canonical_roots.append(root)
    containing_roots = [root for root in canonical_roots if _is_relative_to(cwd, root)]
    if not containing_roots:
        raise ProcessSessionError("process_cwd_outside_approved_roots")
    if _path_contains_link(cwd):
        raise ProcessSessionError("process_cwd_link_forbidden")
    return cwd


def _validate_explicit_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in environment.items():
        normalized = key.upper() if isinstance(key, str) else ""
        approved = normalized in _APPROVED_EXPLICIT_ENVIRONMENT_KEYS or any(
            normalized.startswith(prefix) for prefix in _APPROVED_EXPLICIT_ENVIRONMENT_PREFIXES
        )
        if (
            not isinstance(key, str)
            or not _ENVIRONMENT_KEY_PATTERN.fullmatch(key)
            or not approved
            or normalized in _RESERVED_ENVIRONMENT_KEYS
            or is_sensitive_field_name(key)
        ):
            raise ProcessSessionError("process_environment_key_forbidden")
        if not isinstance(value, str) or "\0" in value:
            raise ProcessSessionError("process_environment_value_invalid")
        result[key] = value
    return result


def _path_contains_link(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if path_is_link_or_reparse(current):
            return True
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(path), os.path.normcase(root))
        ) == os.path.normcase(root)
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _ProcessProtocolFailure("process_frame_duplicate_key")
        value[key] = item
    return value


def _reject_nonfinite_number(value: str) -> object:
    raise _ProcessProtocolFailure("process_frame_nonfinite_number")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(cast(Mapping[str, object], value))
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


async def _await_spawn_completion(
    spawn_task: asyncio.Task[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    while True:
        try:
            return await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            if spawn_task.cancelled():
                raise
            continue


async def _shielded_spawn_cleanup(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> bool:
    async def cleanup() -> bool:
        writer = process.stdin
        if writer is not None:
            writer.close()
        confirmed = await terminate_process_tree(process, grace_seconds=grace_seconds)
        readers = tuple(reader for reader in (process.stdout, process.stderr) if reader is not None)
        if readers:
            await asyncio.gather(*(reader.read() for reader in readers), return_exceptions=True)
        return confirmed

    cleanup_task = asyncio.create_task(
        cleanup(),
        name="corvus-process-session-cancelled-spawn-cleanup",
    )
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue


__all__ = [
    "ProcessInvocation",
    "ProcessSession",
    "ProcessSessionError",
    "ProcessSessionEvent",
    "ProcessSessionEventKind",
    "ProcessSessionLimits",
]
