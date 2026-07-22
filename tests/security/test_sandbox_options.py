import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import corvus.sandbox as sandbox_module
from corvus.cli import SandboxOption, resolve_sandbox_runtime
from corvus.models import SandboxPolicy
from corvus.sandbox import (
    DEVELOPMENT_SANDBOX_IMAGE,
    PRODUCTION_SANDBOX_IMAGE,
    CommandResult,
    DockerSandbox,
    PodmanSandbox,
    SandboxError,
    SandboxLimits,
)


def test_docker_options_are_offline_read_only_and_non_root() -> None:
    options = DockerSandbox(production=False).container_options()

    assert options["network_disabled"] is True
    assert options["read_only"] is True
    assert options["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in options["security_opt"]
    assert options["user"] == "65534:65534"
    assert set(options["tmpfs"]) == {"/workspace", "/tmp"}  # noqa: S108


def test_podman_options_match_the_fail_closed_contract() -> None:
    options = PodmanSandbox(production=False).container_options()

    for required in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
    ):
        assert required in options


def test_direct_worker_networking_is_rejected() -> None:
    policy = SandboxPolicy(network_default=True)

    with pytest.raises(SandboxError, match="egress proxy"):
        DockerSandbox(policy=policy, production=False).container_options()
    with pytest.raises(SandboxError, match="egress proxy"):
        PodmanSandbox(policy=policy, production=False).container_options()


def test_unavailable_explicit_sandbox_never_falls_back_to_host() -> None:
    runtime = resolve_sandbox_runtime(
        SandboxOption.DOCKER,
        docker_status=(False, "not running"),
        podman_status=(True, "available"),
    )

    assert runtime.backend == "none"
    assert runtime.factory is None
    assert runtime.available is False


def test_production_sandbox_uses_digest_pinned_default_and_rejects_tags() -> None:
    assert DockerSandbox().image == PRODUCTION_SANDBOX_IMAGE
    assert PodmanSandbox().image == PRODUCTION_SANDBOX_IMAGE
    with pytest.raises(SandboxError, match="digest-pinned"):
        DockerSandbox(image=DEVELOPMENT_SANDBOX_IMAGE)
    with pytest.raises(SandboxError, match="digest-pinned"):
        PodmanSandbox(image=DEVELOPMENT_SANDBOX_IMAGE)

    image = f"python@sha256:{'a' * 64}"
    assert DockerSandbox(image=image).container_options()["image"] == image
    assert image not in PodmanSandbox(image=image).container_options()


def test_runtime_uses_pinned_production_default_and_rejects_unpinned_override() -> None:
    runtime = resolve_sandbox_runtime(
        SandboxOption.DOCKER,
        docker_status=(True, "available"),
        podman_status=(False, "unavailable"),
    )

    assert runtime.backend == "docker"
    assert runtime.factory is not None
    assert runtime.factory().image == PRODUCTION_SANDBOX_IMAGE

    rejected = resolve_sandbox_runtime(
        SandboxOption.DOCKER,
        image=DEVELOPMENT_SANDBOX_IMAGE,
        docker_status=(True, "available"),
        podman_status=(False, "unavailable"),
    )
    assert rejected.backend == "none"
    assert rejected.factory is None
    assert "digest-pinned" in rejected.detail

    image = f"python@sha256:{'b' * 64}"
    pinned = resolve_sandbox_runtime(
        SandboxOption.DOCKER,
        image=image,
        docker_status=(True, "available"),
        podman_status=(False, "unavailable"),
    )
    assert pinned.backend == "docker"
    assert pinned.factory is not None
    assert pinned.factory().image == image

    development = resolve_sandbox_runtime(
        SandboxOption.PODMAN,
        production=False,
        docker_status=(False, "unavailable"),
        podman_status=(True, "available"),
    )
    assert development.factory is not None
    assert development.factory().image == DEVELOPMENT_SANDBOX_IMAGE


def test_archive_limits_fail_before_container_transfer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_bytes(b"1234")
    (source / "two.txt").write_bytes(b"5678")

    count_limited = DockerSandbox(production=False, limits=SandboxLimits(max_files=1))
    with pytest.raises(SandboxError, match="file count"):
        count_limited._archive_source(source)

    file_limited = DockerSandbox(production=False, limits=SandboxLimits(max_file_bytes=3))
    with pytest.raises(SandboxError, match="per-file"):
        file_limited._archive_source(source)

    total_limited = DockerSandbox(production=False, limits=SandboxLimits(max_total_file_bytes=7))
    with pytest.raises(SandboxError, match="total file bytes"):
        total_limited._archive_source(source)


def test_archive_limits_bound_all_entries_and_depth(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "one" / "two" / "three").mkdir(parents=True)

    entry_limited = DockerSandbox(
        production=False,
        limits=SandboxLimits(max_entries=2),
    )
    with pytest.raises(SandboxError, match="entry count"):
        entry_limited._archive_source(source)

    depth_limited = DockerSandbox(
        production=False,
        limits=SandboxLimits(max_directory_depth=2),
    )
    with pytest.raises(SandboxError, match="directory depth"):
        depth_limited._archive_source(source)


def test_archive_revalidates_the_opened_file_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "payload.txt"
    original.write_bytes(b"safe")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"evil")
    real_open = sandbox_module.os.open

    def substituted_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        selected = replacement if Path(path) == original else path
        return real_open(selected, flags, *args, **kwargs)

    monkeypatch.setattr(sandbox_module.os, "open", substituted_open)
    sandbox = DockerSandbox(production=False)

    with pytest.raises(SandboxError, match="changed during capture"):
        sandbox._archive_source(source)


def test_archive_buffer_enforces_limit_during_tar_write(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_bytes(b"safe")
    sandbox = DockerSandbox(
        production=False,
        limits=SandboxLimits(max_archive_bytes=128),
    )

    with pytest.raises(SandboxError, match="archive byte limit"):
        sandbox._archive_source(source)


def test_docker_command_and_output_limits_fail_closed_before_evidence() -> None:
    class RecordingApi:
        def __init__(self) -> None:
            self.calls = 0
            self.chunks_yielded = 0

        def exec_create(self, *_: object, **__: object) -> dict[str, str]:
            self.calls += 1
            return {"Id": "exec-1"}

        def exec_start(self, *_: object, **__: object):
            for chunk in ((b"12345", b""), (b"", b"67890"), (b"unreachable", b"")):
                self.chunks_yielded += 1
                yield chunk

        def exec_inspect(self, *_: object, **__: object) -> dict[str, int]:
            return {"ExitCode": 0}

    class RecordingContainer:
        id = "container-1"

        def __init__(self) -> None:
            self.removed = False

        def remove(self, *, force: bool) -> None:
            assert force is True
            self.removed = True

    api = RecordingApi()
    container = RecordingContainer()
    sandbox = DockerSandbox(
        production=False,
        client=SimpleNamespace(api=api),
        limits=SandboxLimits(
            max_command_arguments=2,
            max_argument_bytes=4,
            max_output_bytes=8,
        ),
    )
    sandbox.container = container

    with pytest.raises(SandboxError, match="argument count"):
        asyncio.run(sandbox.run(["one", "two", "three"]))
    with pytest.raises(SandboxError, match="argument byte"):
        asyncio.run(sandbox.run(["12345"]))
    assert api.calls == 0

    with pytest.raises(SandboxError, match="output byte"):
        asyncio.run(sandbox.run(["ok"]))
    assert api.chunks_yielded == 2
    assert container.removed is True
    assert sandbox.container is None


def test_docker_start_cancellation_reconciles_created_container() -> None:
    started = threading.Event()
    release = threading.Event()

    class CreatedContainer:
        def __init__(self) -> None:
            self.removed = False

        def remove(self, *, force: bool) -> None:
            assert force is True
            self.removed = True

    created = CreatedContainer()

    class Containers:
        def __init__(self) -> None:
            self.name: str | None = None

        def run(self, **options: object) -> CreatedContainer:
            self.name = str(options["name"])
            started.set()
            release.wait(timeout=5)
            return created

        def get(self, name: str) -> CreatedContainer:
            assert name == self.name
            return created

    containers = Containers()
    sandbox = DockerSandbox(
        production=False,
        client=SimpleNamespace(containers=containers),
    )

    async def scenario() -> None:
        task = asyncio.create_task(sandbox.start())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert containers.name is not None and containers.name.startswith("corvus-")
    assert created.removed is True
    assert sandbox.container is None


def test_podman_timeout_discards_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def read(self, _: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    async def scenario() -> CommandResult:
        terminated = asyncio.Event()

        class Process:
            def __init__(self) -> None:
                self.stdout = Reader([b"partial"])
                self.stderr = Reader([b"diagnostic"])
                self.stdin = None
                self.returncode: int | None = None

            async def wait(self) -> int:
                await terminated.wait()
                return self.returncode or 0

            def kill(self) -> None:
                self.returncode = -9
                terminated.set()

        process = Process()

        async def create_process(*_: object, **__: object) -> Process:
            return process

        monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", create_process)
        sandbox = PodmanSandbox(production=False, executable="podman")
        return await sandbox._invoke(["version"], timeout_seconds=0.01)

    result = asyncio.run(scenario())

    assert result.timed_out is True
    assert result.stdout == ""
    assert result.stderr == "Podman command timed out"


def test_podman_output_limit_is_enforced_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def read(self, _: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    class Process:
        def __init__(self) -> None:
            self.stdout = Reader([b"12345"])
            self.stderr = Reader([b"67890"])
            self.stdin = None
            self.returncode = 0
            self.killed = False

        async def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            self.killed = True

    process = Process()

    async def create_process(*_: object, **__: object) -> Process:
        return process

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", create_process)
    sandbox = PodmanSandbox(
        production=False,
        executable="podman",
        limits=SandboxLimits(max_output_bytes=8),
    )
    sandbox.container = "container-1"
    removed: list[str] = []

    async def remove(container: str) -> None:
        removed.append(container)

    monkeypatch.setattr(sandbox, "_remove", remove)

    with pytest.raises(SandboxError, match="output byte"):
        asyncio.run(sandbox.run(["version"]))
    assert process.killed is True
    assert removed == ["container-1"]
    assert sandbox.container is None
