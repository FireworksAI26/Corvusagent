from __future__ import annotations

import asyncio
import hashlib
import io
import os
import platform
import shutil
import stat
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from platformdirs import user_data_path

from corvus.codex_cli import (
    CodexCliService,
    _child_environment,
    _process_group_options,
    _terminate,
)
from corvus.providers import ProviderError

TESTED_CODEX_VERSION = "0.144.1"

_PACKAGE_METADATA: dict[str, tuple[str, int]] = {
    "codex-package-aarch64-pc-windows-msvc.tar.gz": (
        "a471e55e85bdada7a9d1cb081bed896688a0e7fe0f0fdcd61027187aaa59f8a1",
        130_018_336,
    ),
    "codex-package-aarch64-unknown-linux-musl.tar.gz": (
        "218ab48bdda98dde3e10df184cc0c4eb92c4372d9ca924ef1aa5fc81b4f6a38e",
        119_014_929,
    ),
    "codex-package-x86_64-pc-windows-msvc.tar.gz": (
        "ce94e1fb84693d3f7332ceeab5be73e93de38725da4b82dff863cd2c795b4730",
        139_699_079,
    ),
    "codex-package-x86_64-unknown-linux-musl.tar.gz": (
        "3fd50cf96809b1eea294bbfba0a5c3a576871b4876a1f0e91226e520c1923be1",
        127_942_625,
    ),
}

_PACKAGE_FILE_METADATA: dict[str, dict[str, tuple[str, int]]] = {
    "codex-package-aarch64-pc-windows-msvc.tar.gz": {
        "bin/codex-code-mode-host.exe": (
            "50da9a41d2766e42f07e30c82241ddef34b8473211ffa59db6e3a6adc46b227e",
            50_627_888,
        ),
        "bin/codex.exe": (
            "d3d92e9c10a6f3371a425214c3df67eb97ec5c2ff1b88876410fe0e61d4791da",
            293_038_384,
        ),
        "codex-package.json": (
            "d5d930661771f8e123488d29e060e3cb3b19cb37f29eaa6dd66b2bdb7bfb2a5a",
            216,
        ),
        "codex-path/rg.exe": (
            "f7799d737b520e00b10dfa72def23904fe66fb03315636a7b78549845ee9609c",
            3_930_112,
        ),
        "codex-resources/codex-command-runner.exe": (
            "22fdddb1455557a07143a8a471b01673e9845a0d15ff520cf3eb82c285c3a3d7",
            1_092_912,
        ),
        "codex-resources/codex-windows-sandbox-setup.exe": (
            "d5c10e5fa065b311ebf168a7acd633be725e2291438ddb79c51b6b5d45b11979",
            7_761_200,
        ),
    },
    "codex-package-aarch64-unknown-linux-musl.tar.gz": {
        "bin/codex": (
            "9513fa3f5f4ad444ac1e40d972aef0e2664834ec54da987d54aba0dc2f13ea07",
            259_006_256,
        ),
        "bin/codex-code-mode-host": (
            "450bb85e8fad3a18035af82d642f8f79fa8212f0de0f3db48f186d6865d1cf76",
            43_502_544,
        ),
        "codex-package.json": (
            "8e0969d5d7fb4412a95f328f6c8ce0a939649d220a6bb6b78ece11d6c7f75c57",
            206,
        ),
        "codex-path/rg": (
            "968cabe8efed72fd8fd482cb76b6084fcb695fc5293af7fb62296b02f487fb69",
            4_543_848,
        ),
        "codex-resources/bwrap": (
            "c547cbdc762a70ed216789ffaa4c6c0e7d2beabe32245a498f8e365a9fc8dab4",
            529_168,
        ),
        "codex-resources/zsh/bin/zsh": (
            "7feeacd883e1dc749847936948c378653c80a69ec4a9542f0f126b411882c179",
            878_056,
        ),
    },
    "codex-package-x86_64-pc-windows-msvc.tar.gz": {
        "bin/codex-code-mode-host.exe": (
            "36e6bf90f70439a03cd7f2852242fe5f952b87cd6480540aefa6b150c18b8772",
            53_594_928,
        ),
        "bin/codex.exe": (
            "cbacbb9726262ef558b4af0438a1b2a5bba9076132401d947b5b4d2bf92ab0e4",
            341_200_688,
        ),
        "codex-package.json": (
            "c9874af2c8fb0854c8a1d8603454fb301f4f72c269884665225d4c6808cc98a3",
            215,
        ),
        "codex-path/rg.exe": (
            "decdd4992f3f1b9a5ef9898f1b40ab16886d579d6516b4efd3d5eaa19364e408",
            4_266_496,
        ),
        "codex-resources/codex-command-runner.exe": (
            "712f535d0a01f28adfe22b13f6a222d2a54f6f0956b25520c7ca70c042bf2d81",
            1_271_600,
        ),
        "codex-resources/codex-windows-sandbox-setup.exe": (
            "eb4d4cc098de57a0b9c9d8ec184d5346b4735e3bd65c39ce92269969d2ed643e",
            8_820_528,
        ),
    },
    "codex-package-x86_64-unknown-linux-musl.tar.gz": {
        "bin/codex": (
            "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902",
            298_520_624,
        ),
        "bin/codex-code-mode-host": (
            "107cc233a8d90a545ee9647c527c1161068512880f1ee4eda2415eb7d33a700b",
            46_131_096,
        ),
        "codex-package.json": (
            "aca661373fdc74d51a5a60bb3d6a258943dd326b7ab76bb01fdc18275137548d",
            205,
        ),
        "codex-path/rg": (
            "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2",
            5_445_512,
        ),
        "codex-resources/bwrap": (
            "77360cb751ccedc5971391444ac86a8a33c15b04d6b4a6fe45f5d25496e62c4c",
            529_776,
        ),
        "codex-resources/zsh/bin/zsh": (
            "67faaaa89242c4a332e16e508a1977cffc24bf7fca31d4411cdfd101f3831ef3",
            898_480,
        ),
    },
}

_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4_096
_MAX_EXPANDED_PACKAGE_BYTES = 1024 * 1024 * 1024
_EXPECTED_VERSION_OUTPUT = f"codex-cli {TESTED_CODEX_VERSION}".encode()


class CodexInstallError(RuntimeError):
    """A sanitized failure raised by the bounded Codex CLI installer."""


@dataclass(frozen=True)
class _InstallerArtifact:
    url: str
    sha256: str
    size: int
    suffix: str
    files: tuple[tuple[str, str, int], ...]


def _package_artifact(platform_name: str, machine_name: str) -> _InstallerArtifact:
    machine = machine_name.casefold().replace("-", "_")
    if machine in {"amd64", "x64", "x86_64"}:
        architecture = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        architecture = "aarch64"
    else:
        raise CodexInstallError("Codex installation is unsupported on this architecture.")

    if platform_name in {"win32", "windows"}:
        target = "pc-windows-msvc"
    elif platform_name == "linux":
        target = "unknown-linux-musl"
    else:
        raise CodexInstallError("Codex installation is unsupported on this platform.")

    name = f"codex-package-{architecture}-{target}.tar.gz"
    sha256, size = _PACKAGE_METADATA[name]
    url = f"https://github.com/openai/codex/releases/download/rust-v{TESTED_CODEX_VERSION}/{name}"
    files = tuple(
        (path, digest, file_size)
        for path, (digest, file_size) in sorted(_PACKAGE_FILE_METADATA[name].items())
    )
    return _InstallerArtifact(url, sha256, size, ".tar.gz", files)


@dataclass(frozen=True)
class InstallerCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class InstallerDownloader(Protocol):
    async def download(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> bytes: ...


class InstallerCommandRunner(Protocol):
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> InstallerCommandResult: ...


def _validate_download_url(url: str, allowed_hosts: frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CodexInstallError("The Codex release returned an invalid download address.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise CodexInstallError("The Codex release download left the approved hosts.")


class HttpxInstallerDownloader:
    """Download a pinned release artifact while validating every redirect hop."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def download(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> bytes:
        if timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError("download limits must be positive")
        current = url
        timeout = httpx.Timeout(timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds):
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=timeout,
                    transport=self._transport,
                ) as client:
                    for redirects in range(_MAX_REDIRECTS + 1):
                        _validate_download_url(current, allowed_hosts)
                        async with client.stream("GET", current) as response:
                            if response.status_code in _REDIRECT_CODES:
                                if redirects == _MAX_REDIRECTS:
                                    raise CodexInstallError(
                                        "The Codex release download redirected too many times."
                                    )
                                location = response.headers.get("location")
                                if not location:
                                    raise CodexInstallError(
                                        "The Codex release returned an invalid redirect."
                                    )
                                next_url = urljoin(current, location)
                                _validate_download_url(next_url, allowed_hosts)
                                current = next_url
                                continue
                            if response.status_code != 200:
                                raise CodexInstallError(
                                    "The verified Codex release could not be downloaded."
                                )
                            content_length = response.headers.get("content-length")
                            if content_length is not None:
                                try:
                                    declared_length = int(content_length)
                                except ValueError as exc:
                                    raise CodexInstallError(
                                        "The Codex release returned an invalid response."
                                    ) from exc
                                if declared_length < 0 or declared_length > max_bytes:
                                    raise CodexInstallError(
                                        "The Codex release download exceeded its safety limit."
                                    )
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > max_bytes:
                                    raise CodexInstallError(
                                        "The Codex release download exceeded its safety limit."
                                    )
                            return bytes(body)
        except TimeoutError as exc:
            raise CodexInstallError("The Codex release download timed out.") from exc
        except httpx.HTTPError as exc:
            raise CodexInstallError("The verified Codex release could not be downloaded.") from exc
        raise CodexInstallError("The verified Codex release could not be downloaded.")


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    captured = bytearray()
    while True:
        chunk = await stream.read(min(65_536, limit + 1))
        if not chunk:
            return bytes(captured)
        captured.extend(chunk)
        if len(captured) > limit:
            raise CodexInstallError("The Codex verification command produced too much output.")


class BoundedInstallerCommandRunner:
    """Run one explicit command with closed input and bounded time and output."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> InstallerCommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            raise CodexInstallError("The Codex verification command was not explicit.")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("process limits must be positive")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=dict(env),
                limit=min(max_output_bytes + 1, 1_048_576),
                **_process_group_options(),  # type: ignore[arg-type]
            )
        except OSError as exc:
            raise CodexInstallError("The Codex verification command could not be started.") from exc
        stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output_bytes))
        stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output_bytes))
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            stdout, stderr, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            await _terminate(process)
            raise CodexInstallError("The Codex verification command timed out.") from exc
        except BaseException:
            await _terminate(process)
            raise
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return InstallerCommandResult(returncode, stdout, stderr)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _lexical_absolute(path: Path) -> Path:
    """Normalize dot segments without following a filesystem link or junction."""

    return Path(os.path.abspath(path))


def _assert_no_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_link_or_reparse(current):
            raise CodexInstallError("The Corvus Codex install directory is not safe.")


def _archive_member_parts(member: tarfile.TarInfo, *, windows: bool) -> tuple[str, ...]:
    name = member.name
    if not name or "\x00" in name or "\\" in name:
        raise CodexInstallError("The Codex package contains an unsafe path.")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise CodexInstallError("The Codex package contains an unsafe path.")
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if windows and any(":" in part for part in parts):
        raise CodexInstallError("The Codex package contains an unsafe path.")
    return parts


def _extract_verified_package(content: bytes, destination: Path, *, windows: bool) -> None:
    """Extract a hash-verified package without delegating path handling to tarfile."""

    total_size = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
                raise CodexInstallError("The Codex package has an invalid member count.")
            for member in members:
                if not (member.isdir() or member.isfile()):
                    raise CodexInstallError("The Codex package contains an unsafe member.")
                parts = _archive_member_parts(member, windows=windows)
                if not parts:
                    if member.isdir():
                        continue
                    raise CodexInstallError("The Codex package contains an unsafe path.")
                key = "/".join(parts)
                key = key.casefold() if windows else key
                if key in seen:
                    raise CodexInstallError("The Codex package contains duplicate members.")
                seen.add(key)
                if member.size < 0:
                    raise CodexInstallError("The Codex package contains an invalid member.")
                total_size += member.size
                if total_size > _MAX_EXPANDED_PACKAGE_BYTES:
                    raise CodexInstallError("The Codex package exceeds its expanded size limit.")

                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise CodexInstallError("The Codex package contains an invalid file.")
                remaining = member.size
                with source, target.open("xb") as output:
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise CodexInstallError("The Codex package ended unexpectedly.")
                        output.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise CodexInstallError("The Codex package member exceeded its size.")
                    output.flush()
                    os.fsync(output.fileno())
                if not windows:
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
    except CodexInstallError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise CodexInstallError("The verified Codex package could not be extracted.") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CodexInstallError("The managed Codex package could not be verified.") from exc
    return digest.hexdigest()


def _verify_installed_package(
    root: Path,
    artifact: _InstallerArtifact,
    *,
    windows: bool,
) -> None:
    expected_files = {
        (expected_path.casefold() if windows else expected_path): (sha256, size)
        for expected_path, sha256, size in artifact.files
    }
    expected_directories: set[str] = set()
    for expected_path, _, _ in artifact.files:
        parent = PurePosixPath(expected_path).parent
        while parent.parts:
            rendered = parent.as_posix()
            expected_directories.add(rendered.casefold() if windows else rendered)
            parent = parent.parent

    actual_files: dict[str, Path] = {}
    actual_directories: set[str] = set()
    stack = [root]
    try:
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    actual_path = Path(entry.path)
                    if _is_link_or_reparse(actual_path):
                        raise CodexInstallError(
                            "The managed Codex package contains an unsafe link."
                        )
                    relative = actual_path.relative_to(root).as_posix()
                    key = relative.casefold() if windows else relative
                    if entry.is_dir(follow_symlinks=False):
                        actual_directories.add(key)
                        stack.append(actual_path)
                    elif entry.is_file(follow_symlinks=False):
                        actual_files[key] = actual_path
                    else:
                        raise CodexInstallError(
                            "The managed Codex package contains an unsafe entry."
                        )
    except CodexInstallError:
        raise
    except OSError as exc:
        raise CodexInstallError("The managed Codex package could not be verified.") from exc

    if set(actual_files) != set(expected_files) or actual_directories != expected_directories:
        raise CodexInstallError("The managed Codex package file set did not match the release.")
    for key, (expected_sha256, expected_size) in expected_files.items():
        actual_path = actual_files[key]
        try:
            size = actual_path.stat().st_size
        except OSError as exc:
            raise CodexInstallError("The managed Codex package could not be verified.") from exc
        if size != expected_size or _file_sha256(actual_path) != expected_sha256:
            raise CodexInstallError("The managed Codex package digest did not match the release.")


def _remove_plain_managed_tree(path: Path, *, expected_parent: Path) -> None:
    if path.parent != expected_parent or not path.is_dir():
        raise CodexInstallError("The managed Codex version directory is not safe to replace.")
    _assert_no_link_components(path)
    try:
        for directory, directory_names, file_names in os.walk(path, topdown=True):
            base = Path(directory)
            for name in [*directory_names, *file_names]:
                child = base / name
                if _is_link_or_reparse(child):
                    raise CodexInstallError(
                        "The managed Codex version contains a link and was not replaced."
                    )
                info = child.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise CodexInstallError("The managed Codex version contains an unsafe entry.")
        shutil.rmtree(path)
    except CodexInstallError:
        raise
    except OSError as exc:
        raise CodexInstallError("The managed Codex version could not be replaced safely.") from exc


class CodexCliInstaller:
    """Install one tested Codex release into Corvus-owned per-user storage."""

    def __init__(
        self,
        install_root: Path | None = None,
        *,
        platform_name: str | None = None,
        machine_name: str | None = None,
        interpreter_path: Path | None = None,
        downloader: InstallerDownloader | None = None,
        command_runner: InstallerCommandRunner | None = None,
        download_timeout_seconds: float = 300.0,
        install_timeout_seconds: float = 300.0,
        verify_timeout_seconds: float = 15.0,
        max_installer_output_bytes: int = 1_048_576,
    ) -> None:
        selected_platform = (platform_name or sys.platform).casefold()
        self._platform_name = selected_platform
        self._windows = selected_platform in {"win32", "windows"}
        self._artifact = _package_artifact(
            selected_platform,
            machine_name or platform.machine(),
        )
        root = Path(
            install_root or (Path(user_data_path("corvus", "corvus")) / "managed-codex")
        ).expanduser()
        if not root.is_absolute():
            raise ValueError("Codex managed install root must be absolute")
        self._root = _lexical_absolute(root)
        self._version_root = self._root / TESTED_CODEX_VERSION
        self._bin_directory = self._version_root / "bin"
        executable_name = "codex.exe" if self._windows else "codex"
        self.install_path = self._bin_directory / executable_name
        self.version = TESTED_CODEX_VERSION
        self.source = self._artifact.url
        self._interpreter = (
            Path(interpreter_path).expanduser().resolve(strict=False)
            if interpreter_path is not None
            else self._default_interpreter()
        )
        if not self._interpreter.is_absolute():
            raise ValueError("installer interpreter path must be absolute")
        self._downloader = downloader or HttpxInstallerDownloader()
        self._runner = command_runner or BoundedInstallerCommandRunner()
        self._download_timeout = download_timeout_seconds
        self._install_timeout = install_timeout_seconds
        self._verify_timeout = verify_timeout_seconds
        self._max_output = max_installer_output_bytes
        if (
            min(
                self._download_timeout,
                self._install_timeout,
                self._verify_timeout,
                float(self._max_output),
            )
            <= 0
        ):
            raise ValueError("installer limits must be positive")

    def _default_interpreter(self) -> Path:
        if self._windows:
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            if not system_root.is_absolute():
                system_root = Path(r"C:\Windows")
            return (
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            ).resolve(strict=False)
        return Path("/bin/sh").resolve(strict=False)

    def _install_environment(self, codex_home: Path) -> dict[str, str]:
        environment = _child_environment()
        environment["CODEX_HOME"] = str(codex_home)
        environment["CODEX_INSTALL_DIR"] = str(self._bin_directory)
        environment["CODEX_NON_INTERACTIVE"] = "1"
        environment["CODEX_RELEASE"] = TESTED_CODEX_VERSION
        environment["PATH"] = os.pathsep.join(str(path) for path in self._trusted_path())
        return environment

    def _trusted_path(self) -> tuple[Path, ...]:
        if self._windows:
            try:
                system32 = self._interpreter.parents[2]
                system_root = system32.parent
            except IndexError:
                system_root = Path(r"C:\Windows")
                system32 = system_root / "System32"
            return (
                self._bin_directory,
                system32,
                system_root,
                self._interpreter.parent,
                system32 / "Wbem",
            )
        return (
            self._bin_directory,
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        )

    async def install(
        self,
        progress: Callable[[str], None] | None = None,
    ) -> CodexCliService:
        report = progress or (lambda _message: None)
        report("Preparing the managed Codex CLI installation…")
        _assert_no_link_components(self._root)
        if self._version_root.exists():
            try:
                return await self._use_existing(report)
            except CodexInstallError:
                report("The existing managed Codex files failed integrity checks; rebuilding…")
                _remove_plain_managed_tree(
                    self._version_root,
                    expected_parent=self._root,
                )
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise CodexInstallError(
                "The Corvus Codex install directory could not be created."
            ) from exc
        _assert_no_link_components(self._root)
        try:
            self._version_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError:
            return await self._use_existing(report)
        except OSError as exc:
            raise CodexInstallError(
                "The Corvus Codex version directory could not be created."
            ) from exc
        _assert_no_link_components(self._version_root)
        created_version_root = True
        try:
            report(f"Downloading the verified Codex CLI {TESTED_CODEX_VERSION} package…")
            content = await self._downloader.download(
                self._artifact.url,
                allowed_hosts=_ALLOWED_DOWNLOAD_HOSTS,
                timeout_seconds=self._download_timeout,
                max_bytes=self._artifact.size,
            )
            if (
                len(content) != self._artifact.size
                or hashlib.sha256(content).hexdigest() != self._artifact.sha256
            ):
                raise CodexInstallError("The Codex package failed integrity verification.")
            _assert_no_link_components(self._version_root)
            report(f"Extracting Codex CLI {TESTED_CODEX_VERSION} safely…")
            _extract_verified_package(
                content,
                self._version_root,
                windows=self._windows,
            )
            report("Verifying the installed Codex CLI…")
            service = await self._verified_service()
            created_version_root = False
            report(f"Codex CLI {TESTED_CODEX_VERSION} is ready.")
            return service
        finally:
            if created_version_root:
                shutil.rmtree(self._version_root, ignore_errors=True)

    async def _use_existing(self, report: Callable[[str], None]) -> CodexCliService:
        report("Verifying the installed Codex CLI…")
        service = await self.verify_existing()
        report(f"Codex CLI {TESTED_CODEX_VERSION} is ready.")
        return service

    async def verify_existing(self) -> CodexCliService:
        """Verify every pinned package file before executing an existing managed CLI."""

        _assert_no_link_components(self._version_root)
        return await self._verified_service()

    async def _verified_service(self) -> CodexCliService:
        _verify_installed_package(
            self._version_root,
            self._artifact,
            windows=self._windows,
        )
        try:
            resolved_root = self._version_root.resolve(strict=True)
            resolved_executable = self.install_path.resolve(strict=True)
            info = resolved_executable.stat()
        except OSError as exc:
            raise CodexInstallError("The managed Codex executable was not created.") from exc
        if not resolved_executable.is_relative_to(resolved_root):
            raise CodexInstallError("The managed Codex executable escaped its install directory.")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > (_MAX_EXECUTABLE_BYTES)
        ):
            raise CodexInstallError("The managed Codex executable is invalid.")
        if not self._windows and not os.access(resolved_executable, os.X_OK):
            raise CodexInstallError("The managed Codex executable is not executable.")
        try:
            with resolved_executable.open("rb") as handle:
                magic = handle.read(4)
        except OSError as exc:
            raise CodexInstallError("The managed Codex executable could not be verified.") from exc
        if not self._valid_executable_magic(magic):
            raise CodexInstallError("The managed Codex executable has an invalid format.")
        verification_home = self._root / f".verify-{uuid4().hex}"
        try:
            verification_home.mkdir(mode=0o700, parents=False, exist_ok=False)
            _assert_no_link_components(verification_home)
            result = await self._runner.run(
                (str(resolved_executable), "--version"),
                cwd=self._version_root,
                env=self._install_environment(verification_home),
                timeout_seconds=self._verify_timeout,
                max_output_bytes=65_536,
            )
        except OSError as exc:
            raise CodexInstallError("The Codex verification home could not be created.") from exc
        finally:
            shutil.rmtree(verification_home, ignore_errors=True)
        if (
            result.returncode != 0
            or result.stderr.strip()
            or result.stdout.strip() != _EXPECTED_VERSION_OUTPUT
        ):
            raise CodexInstallError("The installed Codex CLI version could not be verified.")
        _verify_installed_package(
            self._version_root,
            self._artifact,
            windows=self._windows,
        )
        service = CodexCliService(resolved_executable, self._root / "scratch")
        if service.executable_sha256 is None:
            raise CodexInstallError("The managed Codex executable could not be verified.")
        try:
            service.validate_executable()
        except ProviderError as exc:
            raise CodexInstallError(
                "The managed Codex executable changed during verification."
            ) from exc
        return service

    def _valid_executable_magic(self, magic: bytes) -> bool:
        if self._windows:
            return magic.startswith(b"MZ")
        if self._platform_name == "darwin":
            return magic in {
                b"\xca\xfe\xba\xbe",
                b"\xca\xfe\xba\xbf",
                b"\xce\xfa\xed\xfe",
                b"\xcf\xfa\xed\xfe",
                b"\xbe\xba\xfe\xca",
                b"\xbf\xba\xfe\xca",
                b"\xfe\xed\xfa\xce",
                b"\xfe\xed\xfa\xcf",
            }
        return magic == b"\x7fELF"


# Compatibility-friendly short name for CLI/TUI composition.
CodexInstaller = CodexCliInstaller

__all__ = [
    "TESTED_CODEX_VERSION",
    "BoundedInstallerCommandRunner",
    "CodexCliInstaller",
    "CodexInstallError",
    "CodexInstaller",
    "HttpxInstallerDownloader",
    "InstallerCommandResult",
    "InstallerCommandRunner",
    "InstallerDownloader",
]
