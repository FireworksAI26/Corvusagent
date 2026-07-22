from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import corvus.codex_cli as codex_cli
from corvus.codex_cli import _CHILD_ENVIRONMENT_ALLOWLIST, _child_environment
from corvus.config import ConfigManager
from corvus.models import Budget, Policy
from corvus.providers import ProviderError, _PinnedNetworkBackend, validate_provider_url
from corvus.security import SecurityError
from corvus.store import ArtifactStore


def test_optional_token_budgets_narrow_by_minimum_present_value() -> None:
    user = Policy(budgets=Budget(max_input_tokens=1_000, max_output_tokens=None))
    project = Policy(budgets=Budget(max_input_tokens=None, max_output_tokens=2_000))

    narrowed = ConfigManager._narrow(user, project)

    assert narrowed.budgets.max_input_tokens == 1_000
    assert narrowed.budgets.max_output_tokens == 2_000

    both = ConfigManager._narrow(
        Policy(budgets=Budget(max_input_tokens=5_000, max_output_tokens=800)),
        Policy(budgets=Budget(max_input_tokens=3_000, max_output_tokens=1_200)),
    )
    assert both.budgets.max_input_tokens == 3_000
    assert both.budgets.max_output_tokens == 800


def test_child_environment_is_strict_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_bin = tmp_path / "bin"
    safe_bin.mkdir()
    trusted_executable = safe_bin / "codex"
    trusted_executable.write_bytes(b"trusted")
    monkeypatch.setenv("PATH", str(safe_bin))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("UNRELATED_ATTACKER_CONTROLLED", "must-not-pass")
    monkeypatch.setenv("MY_PRIVATE_TOKEN", "must-not-pass")
    monkeypatch.setenv("LD_PRELOAD", str(tmp_path / "evil.so"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "injected"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")

    environment = _child_environment(trusted_executable)

    assert set(environment) <= _CHILD_ENVIRONMENT_ALLOWLIST
    assert str(safe_bin) in environment["PATH"].split(os.pathsep)
    assert environment.get("HOME") == str(tmp_path / "home")
    assert "UNRELATED_ATTACKER_CONTROLLED" not in environment
    assert not any("TOKEN" in key or "KEY" in key for key in environment)
    assert not any(key.startswith(("LD_", "DYLD_")) for key in environment)


def test_child_environment_does_not_promote_posix_lowercase_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_cli, "_environment_keys_case_insensitive", lambda: False)
    monkeypatch.setattr(
        codex_cli.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "path": "/tmp/attacker-bin",  # noqa: S108
            "HOME": "/home/trusted",
            "home": "/tmp/attacker-home",  # noqa: S108
        },
    )

    environment = _child_environment()

    assert "/tmp/attacker-bin" not in environment["PATH"].split(os.pathsep)  # noqa: S108
    assert environment["HOME"] == "/home/trusted"


@pytest.mark.parametrize(
    ("url", "addresses", "message"),
    (
        ("http://api.example.test", ("93.184.216.34",), "HTTPS"),
        ("https://user:pass@api.example.test", ("93.184.216.34",), "credentials"),
        ("https://127.0.0.1", ("127.0.0.1",), "globally routable"),
        ("https://169.254.169.254/latest", ("169.254.169.254",), "globally routable"),
        (
            "https://api.example.test",
            ("93.184.216.34", "127.0.0.1"),
            "globally routable",
        ),
    ),
)
def test_cloud_provider_url_rejects_unsafe_host_classes(
    url: str, addresses: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ProviderError, match=message):
        validate_provider_url(url, local=False, resolved_addresses=addresses)


def test_provider_url_accepts_only_topology_matching_addresses() -> None:
    assert (
        validate_provider_url(
            "https://api.example.test/v1",
            local=False,
            resolved_addresses=("93.184.216.34",),
        )
        == "https://api.example.test/v1"
    )
    assert (
        validate_provider_url(
            "http://localhost:11434",
            local=True,
            resolved_addresses=("127.0.0.1", "::1"),
        )
        == "http://localhost:11434"
    )
    with pytest.raises(ProviderError, match="local/private"):
        validate_provider_url(
            "http://api.example.test",
            local=True,
            resolved_addresses=("93.184.216.34",),
        )
    with pytest.raises(ProviderError, match="local/private"):
        validate_provider_url(
            "http://metadata.local",
            local=True,
            resolved_addresses=("169.254.169.254",),
        )


@pytest.mark.parametrize(
    "url",
    (
        "https://api.example.test/v1?api_key=must-not-pass",
        "https://api.example.test/v1#credential-fragment",
    ),
)
def test_provider_base_url_rejects_query_and_fragment(url: str) -> None:
    with pytest.raises(ProviderError, match="query or fragment"):
        validate_provider_url(
            url,
            local=False,
            resolved_addresses=("93.184.216.34",),
        )


def test_pinned_network_backend_connects_only_to_validated_address() -> None:
    class RecordingBackend:
        def __init__(self) -> None:
            self.hosts: list[str] = []

        async def connect_tcp(
            self,
            host: str,
            port: int,
            **_: object,
        ) -> object:
            assert port == 443
            self.hosts.append(host)
            return object()

    delegate = RecordingBackend()
    backend = _PinnedNetworkBackend(
        "api.example.test",
        ("93.184.216.34",),
        delegate=delegate,
    )

    asyncio.run(backend.connect_tcp("api.example.test", 443))

    assert delegate.hosts == ["93.184.216.34"]
    with pytest.raises(ProviderError, match="host mismatch"):
        asyncio.run(backend.connect_tcp("rebound.example.test", 443))


def test_artifact_store_rehashes_on_put_and_get(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    data = b"verified artifact"
    digest, path = store.put(data)
    path.write_bytes(b"tampered")

    with pytest.raises(SecurityError, match="artifact integrity"):
        store.get(digest)
    with pytest.raises(SecurityError, match="artifact integrity"):
        store.put(data)
    with pytest.raises(SecurityError, match="invalid artifact digest"):
        store.get("../escape")
