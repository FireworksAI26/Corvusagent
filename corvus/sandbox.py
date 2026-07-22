from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import stat
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import docker
from docker.errors import DockerException

from corvus.models import SandboxPolicy
from corvus.safe_process import TrustedProcessError, run_trusted_argv

_CONTAINER_WORKSPACE = PurePosixPath("/").joinpath("workspace").as_posix()
_CONTAINER_TEMP = PurePosixPath("/").joinpath("tmp").as_posix()


class SandboxError(RuntimeError):
    pass


def _docker_unavailable_detail() -> str:
    runtime = "Docker Desktop" if sys.platform in {"darwin", "win32"} else "Docker Engine"
    return (
        f"{runtime} is not reachable. Install or start it, then retry. "
        "Ordinary chat remains available; isolated /build is disabled."
    )


def _podman_unavailable_detail() -> str:
    runtime = (
        "Podman Desktop or its Podman machine" if sys.platform in {"darwin", "win32"} else "Podman"
    )
    return (
        f"{runtime} is not reachable. Install or start it, then retry. "
        "Ordinary chat remains available; isolated /build is disabled."
    )


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class SandboxLimits:
    max_files: int = 20_000
    max_entries: int = 25_000
    max_directory_depth: int = 64
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_file_bytes: int = 100 * 1024 * 1024
    max_archive_bytes: int = 128 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024
    max_command_arguments: int = 128
    max_argument_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class _SourceFile:
    path: Path
    relative_path: str
    device: int
    inode: int
    size: int
    modified_ns: int


class _BoundedBytesIO(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.high_watermark = 0

    def write(self, data: Any) -> int:
        end = self.tell() + len(data)
        self.high_watermark = max(self.high_watermark, end)
        if self.high_watermark > self.limit:
            raise SandboxError("sandbox source archive byte limit exceeded")
        return super().write(data)


_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
DEVELOPMENT_SANDBOX_IMAGE = "python:3.12-slim"
PRODUCTION_SANDBOX_IMAGE = (
    "python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
)


def validate_sandbox_image(image: str, *, production: bool) -> None:
    if production and _IMAGE_DIGEST.fullmatch(image) is None:
        raise SandboxError("production sandbox image must be digest-pinned")


def _validate_command(command: list[str], limits: SandboxLimits) -> None:
    if not command:
        raise SandboxError("sandbox command must not be empty")
    if len(command) > limits.max_command_arguments:
        raise SandboxError("sandbox command argument count limit exceeded")
    if any(not isinstance(argument, str) for argument in command):
        raise SandboxError("sandbox command arguments must be strings")
    if any(len(argument.encode("utf-8")) > limits.max_argument_bytes for argument in command):
        raise SandboxError("sandbox command argument byte limit exceeded")


async def _bounded_process_communicate(
    process: asyncio.subprocess.Process,
    input_data: bytes | None,
    limit: int,
) -> tuple[bytes, bytes, int]:
    stdout_reader = process.stdout
    stderr_reader = process.stderr
    if stdout_reader is None or stderr_reader is None:
        raise SandboxError("sandbox process pipes are unavailable")
    stdout = bytearray()
    stderr = bytearray()
    total = [0]

    async def read_stream(reader: Any, destination: bytearray) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                return
            total[0] += len(chunk)
            if total[0] > limit:
                raise SandboxError("sandbox command output byte limit exceeded")
            destination.extend(chunk)

    async def write_input() -> None:
        if input_data is None:
            return
        writer = process.stdin
        if writer is None:
            raise SandboxError("sandbox process stdin is unavailable")
        try:
            for offset in range(0, len(input_data), 64 * 1024):
                writer.write(input_data[offset : offset + 64 * 1024])
                await writer.drain()
        finally:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()

    tasks = (
        asyncio.create_task(read_stream(stdout_reader, stdout)),
        asyncio.create_task(read_stream(stderr_reader, stderr)),
        asyncio.create_task(write_input()),
        asyncio.create_task(process.wait()),
    )
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await process.wait()
        except ProcessLookupError:
            pass
        raise
    returncode = process.returncode if process.returncode is not None else 1
    return bytes(stdout), bytes(stderr), returncode


def _is_link_or_reparse_info(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _iter_directory(directory: Path) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            yield from entries
    except OSError as exc:
        raise SandboxError(f"sandbox source directory is unsafe: {directory}") from exc


def _archive_source(source: Path, limits: SandboxLimits) -> bytes:
    try:
        source_info = source.lstat()
    except OSError as exc:
        raise SandboxError(f"sandbox source is unavailable: {source}") from exc
    if _is_link_or_reparse_info(source_info):
        raise SandboxError(f"source symlink or junction rejected: {source}")
    if not stat.S_ISDIR(source_info.st_mode):
        raise SandboxError(f"sandbox source is not a directory: {source}")
    source = source.resolve(strict=True)

    files: list[_SourceFile] = []
    total_bytes = 0
    entry_count = 0
    directories: list[tuple[Path, int, int, int]] = [
        (source, 0, source_info.st_dev, source_info.st_ino)
    ]
    while directories:
        directory, depth, expected_device, expected_inode = directories.pop()
        try:
            current_directory = directory.stat(follow_symlinks=False)
            if (
                _is_link_or_reparse_info(current_directory)
                or not stat.S_ISDIR(current_directory.st_mode)
                or current_directory.st_dev != expected_device
                or current_directory.st_ino != expected_inode
            ):
                raise SandboxError(f"sandbox source directory changed during capture: {directory}")
            entries = _iter_directory(directory)
        except OSError as exc:
            raise SandboxError(f"sandbox source directory is unsafe: {directory}") from exc

        for entry in entries:
            entry_count += 1
            if entry_count > limits.max_entries:
                raise SandboxError("sandbox source entry count limit exceeded")
            path = Path(entry.path)
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SandboxError(f"sandbox source entry is unavailable: {path}") from exc
            if _is_link_or_reparse_info(info):
                raise SandboxError(f"source symlink or junction rejected: {path}")
            if stat.S_ISDIR(info.st_mode):
                child_depth = depth + 1
                if child_depth > limits.max_directory_depth:
                    raise SandboxError("sandbox source directory depth limit exceeded")
                directories.append((path, child_depth, info.st_dev, info.st_ino))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SandboxError(f"unsupported source entry rejected: {path}")
            if info.st_size > limits.max_file_bytes:
                raise SandboxError(f"sandbox source per-file byte limit exceeded: {path}")
            files.append(
                _SourceFile(
                    path=path,
                    relative_path=path.relative_to(source).as_posix(),
                    device=info.st_dev,
                    inode=info.st_ino,
                    size=info.st_size,
                    modified_ns=info.st_mtime_ns,
                )
            )
            if len(files) > limits.max_files:
                raise SandboxError("sandbox source file count limit exceeded")
            total_bytes += info.st_size
            if total_bytes > limits.max_total_file_bytes:
                raise SandboxError("sandbox source total file bytes limit exceeded")

    archive = _BoundedBytesIO(limits.max_archive_bytes)
    actual_total_bytes = 0
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for source_file in sorted(files, key=lambda item: item.relative_path):
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(source_file.path, flags)
            except OSError as exc:
                raise SandboxError(
                    f"sandbox source file cannot be opened safely: {source_file.path}"
                ) from exc
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                expected = (
                    source_file.device,
                    source_file.inode,
                    source_file.size,
                    source_file.modified_ns,
                )
                if not stat.S_ISREG(opened.st_mode) or identity != expected:
                    raise SandboxError(
                        f"sandbox source file changed during capture: {source_file.path}"
                    )
                actual_total_bytes += opened.st_size
                if opened.st_size > limits.max_file_bytes:
                    raise SandboxError(
                        f"sandbox source per-file byte limit exceeded: {source_file.path}"
                    )
                if actual_total_bytes > limits.max_total_file_bytes:
                    raise SandboxError("sandbox source total file bytes limit exceeded")
                tar_entry = tarfile.TarInfo(source_file.relative_path)
                tar_entry.size = opened.st_size
                tar_entry.mtime = int(opened.st_mtime)
                tar_entry.mode = stat.S_IMODE(opened.st_mode)
                tar_entry.uid = 65534
                tar_entry.gid = 65534
                tar_entry.uname = "nobody"
                tar_entry.gname = "nogroup"
                try:
                    tar.addfile(tar_entry, handle)
                except OSError as exc:
                    raise SandboxError(
                        f"sandbox source file changed during capture: {source_file.path}"
                    ) from exc
                completed = os.fstat(handle.fileno())
                completed_identity = (
                    completed.st_dev,
                    completed.st_ino,
                    completed.st_size,
                    completed.st_mtime_ns,
                )
                if completed_identity != identity:
                    raise SandboxError(
                        f"sandbox source file changed during capture: {source_file.path}"
                    )
    return archive.getvalue()


class DockerSandbox:
    def __init__(
        self,
        image: str | None = None,
        policy: SandboxPolicy | None = None,
        client: Any | None = None,
        *,
        limits: SandboxLimits | None = None,
        production: bool = True,
    ) -> None:
        selected_image = image or (
            PRODUCTION_SANDBOX_IMAGE if production else DEVELOPMENT_SANDBOX_IMAGE
        )
        validate_sandbox_image(selected_image, production=production)
        self.image = selected_image
        self.policy = policy or SandboxPolicy()
        self.client = client
        self.limits = limits or SandboxLimits()
        self.container: Any | None = None
        self._container_name: str | None = None

    @staticmethod
    def available() -> tuple[bool, str]:
        client: Any | None = None
        try:
            client = docker.from_env()
            client.ping()
            version_info = client.version()
            version = (
                version_info.get("Version", "unknown")
                if isinstance(version_info, dict)
                else "unknown"
            )
            return True, str(version)
        except DockerException:
            # Missing daemons and inaccessible sockets are expected optional-capability states.
            # Do not expose Docker SDK transport exceptions or host-specific pipe paths.
            return False, _docker_unavailable_detail()
        finally:
            if client is not None:
                try:
                    client.close()
                except (DockerException, OSError):
                    pass

    def container_options(self) -> dict[str, Any]:
        if self.policy.network_default:
            raise SandboxError(
                "direct worker networking is forbidden; configure the domain egress proxy"
            )
        return {
            "image": self.image,
            "command": ["sleep", "infinity"],
            "detach": True,
            "network_disabled": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": f"{self.policy.memory_mb}m",
            "nano_cpus": int(self.policy.cpu_limit * 1_000_000_000),
            "pids_limit": self.policy.pids_limit,
            "tmpfs": {
                _CONTAINER_WORKSPACE: "rw,noexec,nosuid,size=1g",
                _CONTAINER_TEMP: "rw,noexec,nosuid,size=256m",
            },
            "working_dir": _CONTAINER_WORKSPACE,
            "user": "65534:65534",
            "environment": {
                "HOME": _CONTAINER_TEMP,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "labels": {"io.corvus.sandbox": "true"},
        }

    async def _discard_container(self, container: Any | None, name: str) -> None:
        candidate = container
        if candidate is None and self.client is not None:
            try:
                candidate = await asyncio.to_thread(self.client.containers.get, name)
            except (DockerException, OSError):
                candidate = None
        if candidate is not None:
            try:
                await asyncio.to_thread(candidate.remove, force=True)
            except (DockerException, OSError):
                pass
        if self.container is candidate:
            self.container = None
        if self._container_name == name:
            self._container_name = None

    async def start(self) -> None:
        if self.container is not None:
            return
        if self.client is None:
            try:
                self.client = docker.from_env(timeout=60)
            except DockerException as exc:
                raise SandboxError("Docker is unavailable") from exc
        client = self.client
        container_name = f"corvus-{uuid4().hex}"
        self._container_name = container_name

        def create_container() -> Any:
            return client.containers.run(name=container_name, **self.container_options())

        creation = asyncio.create_task(asyncio.to_thread(create_container))
        created_container: Any | None = None
        try:
            created_container = await asyncio.shield(creation)
        except asyncio.CancelledError:
            await asyncio.shield(self._discard_container(created_container, container_name))
            raise
        except (DockerException, OSError) as exc:
            await self._discard_container(None, container_name)
            raise SandboxError("failed to create constrained Docker sandbox") from exc
        self.container = created_container

    async def stage(self, source: Path) -> None:
        if self.container is None:
            raise SandboxError("sandbox has not started")
        archive_data = await asyncio.to_thread(self._archive_source, source)
        ok = await asyncio.to_thread(self.container.put_archive, "/workspace", archive_data)
        if not ok:
            raise SandboxError("Docker rejected source archive")

    def _archive_source(self, source: Path) -> bytes:
        return _archive_source(source, self.limits)

    async def run(self, command: list[str], timeout_seconds: float = 300) -> CommandResult:
        if self.container is None or self.client is None:
            raise SandboxError("sandbox has not started")
        _validate_command(command, self.limits)
        container = self.container
        container_name = self._container_name or str(container.id)
        api = self.client.api

        def execute() -> CommandResult:
            created = api.exec_create(
                container.id,
                command,
                workdir="/workspace",
                user="65534:65534",
            )
            exec_id = created.get("Id") if isinstance(created, dict) else None
            if not isinstance(exec_id, str) or not exec_id:
                raise SandboxError("Docker did not return a valid exec identifier")
            stdout_bytes = bytearray()
            stderr_bytes = bytearray()
            total = 0
            stream = api.exec_start(exec_id, stream=True, demux=True)
            for chunk in stream:
                if isinstance(chunk, tuple):
                    stdout_chunk, stderr_chunk = chunk
                else:
                    stdout_chunk, stderr_chunk = chunk, None
                for source, destination in (
                    (stdout_chunk, stdout_bytes),
                    (stderr_chunk, stderr_bytes),
                ):
                    if not source:
                        continue
                    total += len(source)
                    if total > self.limits.max_output_bytes:
                        raise SandboxError("sandbox command output byte limit exceeded")
                    destination.extend(source)
            inspected = api.exec_inspect(exec_id)
            exit_code = inspected.get("ExitCode") if isinstance(inspected, dict) else None
            return CommandResult(
                exit_code=int(exit_code) if isinstance(exit_code, int) else 1,
                stdout=bytes(stdout_bytes).decode(errors="replace"),
                stderr=bytes(stderr_bytes).decode(errors="replace"),
            )

        try:
            return await asyncio.wait_for(asyncio.to_thread(execute), timeout_seconds)
        except TimeoutError:
            await self._discard_container(container, container_name)
            return CommandResult(
                exit_code=124,
                stdout="",
                stderr="command timed out",
                timed_out=True,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._discard_container(container, container_name))
            raise
        except SandboxError:
            await self._discard_container(container, container_name)
            raise
        except (DockerException, OSError) as exc:
            await self._discard_container(container, container_name)
            raise SandboxError("Docker sandbox command failed") from exc

    async def close(self) -> None:
        if self.container is not None:
            container = self.container
            name = self._container_name or str(container.id)
            await self._discard_container(container, name)
        if self.client is not None:
            self.client.close()

    async def __aenter__(self) -> DockerSandbox:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class PodmanSandbox:
    """A rootless-friendly Podman backend with Docker security parity.

    The backend intentionally uses the Podman CLI rather than its Docker-compatible
    socket. Every invocation is an argv vector (never a shell command), and source
    content is transferred as a validated tar stream over stdin instead of a bind mount.
    """

    def __init__(
        self,
        image: str | None = None,
        policy: SandboxPolicy | None = None,
        executable: str | Path | None = None,
        *,
        limits: SandboxLimits | None = None,
        production: bool = True,
    ) -> None:
        selected_image = image or (
            PRODUCTION_SANDBOX_IMAGE if production else DEVELOPMENT_SANDBOX_IMAGE
        )
        validate_sandbox_image(selected_image, production=production)
        self.image = selected_image
        self.policy = policy or SandboxPolicy()
        self.executable = str(executable) if executable is not None else None
        self.limits = limits or SandboxLimits()
        self.container: str | None = None

    @staticmethod
    def available() -> tuple[bool, str]:
        executable = shutil.which("podman")
        if executable is None:
            return False, _podman_unavailable_detail()
        try:
            result = run_trusted_argv(
                [executable, "info", "--format", "{{.Version.Version}}"],
                cwd=Path.cwd(),
                timeout_seconds=10,
            )
        except (OSError, TrustedProcessError):
            return False, _podman_unavailable_detail()
        if result.returncode != 0:
            return False, _podman_unavailable_detail()
        return True, result.stdout.decode(errors="replace").strip() or "unknown"

    def container_options(self) -> list[str]:
        if self.policy.network_default:
            raise SandboxError(
                "direct worker networking is forbidden; configure the domain egress proxy"
            )
        return [
            "--detach",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--read-only-tmpfs=false",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={self.policy.memory_mb}m",
            f"--cpus={self.policy.cpu_limit}",
            f"--pids-limit={self.policy.pids_limit}",
            f"--tmpfs={_CONTAINER_WORKSPACE}:rw,noexec,nosuid,size=1g,uid=65534,gid=65534,mode=0700",
            f"--tmpfs={_CONTAINER_TEMP}:rw,noexec,nosuid,size=256m,uid=65534,gid=65534,mode=0700",
            f"--workdir={_CONTAINER_WORKSPACE}",
            "--user=65534:65534",
            f"--env=HOME={_CONTAINER_TEMP}",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--label=io.corvus.sandbox=true",
        ]

    def _resolve_executable(self) -> str:
        executable = self.executable or shutil.which("podman")
        if executable is None:
            raise SandboxError("Podman is unavailable")
        return executable

    async def _invoke(
        self,
        arguments: list[str],
        *,
        input_data: bytes | None = None,
        timeout_seconds: float = 60,
    ) -> CommandResult:
        executable = self._resolve_executable()
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdin=(
                    asyncio.subprocess.PIPE
                    if input_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError("Podman is unavailable") from exc
        collection = asyncio.create_task(
            _bounded_process_communicate(process, input_data, self.limits.max_output_bytes)
        )
        try:
            stdout, stderr, returncode = await asyncio.wait_for(
                asyncio.shield(collection), timeout=timeout_seconds
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(collection), timeout=5)
            except SandboxError:
                raise
            except TimeoutError as exc:
                collection.cancel()
                await asyncio.gather(collection, return_exceptions=True)
                raise SandboxError("Podman command did not terminate after timeout") from exc
            return CommandResult(
                exit_code=124,
                stdout="",
                stderr="Podman command timed out",
                timed_out=True,
            )
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.shield(collection)
            except BaseException:  # noqa: S110
                # pragma: no cover - best-effort cleanup on cancellation/error
                pass
            raise
        return CommandResult(
            exit_code=returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def _remove(self, container: str) -> None:
        try:
            await self._invoke(["rm", "--force", "--time=0", container], timeout_seconds=30)
        except SandboxError:
            # Removal is best effort when the runtime itself has disappeared. The unique
            # name is never reused, so a later invocation cannot target another container.
            pass

    async def start(self) -> None:
        if self.container is not None:
            return
        container = f"corvus-{uuid4().hex}"
        # Track the generated name before invoking Podman so cancellation and partial
        # creation can only clean up this exact container.
        self.container = container
        try:
            result = await self._invoke(
                [
                    "run",
                    *self.container_options(),
                    f"--name={container}",
                    "--",
                    self.image,
                    "sleep",
                    "infinity",
                ],
                timeout_seconds=60,
            )
            if result.timed_out or result.exit_code != 0:
                raise SandboxError("failed to create constrained Podman sandbox")
        except (Exception, asyncio.CancelledError):
            try:
                await asyncio.shield(self._remove(container))
            finally:
                self.container = None
            raise

    async def stage(self, source: Path) -> None:
        if self.container is None:
            raise SandboxError("sandbox has not started")
        archive_data = await asyncio.to_thread(self._archive_source, source)
        result = await self._invoke(
            ["cp", "-", f"{self.container}:/workspace"],
            input_data=archive_data,
            timeout_seconds=300,
        )
        if result.timed_out or result.exit_code != 0:
            container = self.container
            await asyncio.shield(self._remove(container))
            self.container = None
            raise SandboxError("Podman rejected source archive")

    def _archive_source(self, source: Path) -> bytes:
        return _archive_source(source, self.limits)

    async def run(self, command: list[str], timeout_seconds: float = 300) -> CommandResult:
        if self.container is None:
            raise SandboxError("sandbox has not started")
        _validate_command(command, self.limits)
        container = self.container
        try:
            result = await self._invoke(
                [
                    "exec",
                    "--user=65534:65534",
                    "--workdir=/workspace",
                    "--",
                    container,
                    *command,
                ],
                timeout_seconds=timeout_seconds,
            )
        except (SandboxError, asyncio.CancelledError):
            await asyncio.shield(self._remove(container))
            self.container = None
            raise
        if result.timed_out:
            await asyncio.shield(self._remove(container))
            self.container = None
            return CommandResult(
                exit_code=124,
                stdout=result.stdout,
                stderr="command timed out",
                timed_out=True,
            )
        if result.exit_code == 125:
            # Podman reserves 125 for engine/CLI errors. Its raw stderr can contain
            # host socket paths or runtime implementation details, so it is not evidence
            # produced by the sandboxed command and must not be persisted.
            return CommandResult(
                exit_code=125,
                stdout="",
                stderr="Podman failed to execute the sandbox command",
            )
        return result

    async def close(self) -> None:
        if self.container is None:
            return
        container = self.container
        try:
            await asyncio.shield(self._remove(container))
        finally:
            self.container = None

    async def __aenter__(self) -> PodmanSandbox:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
