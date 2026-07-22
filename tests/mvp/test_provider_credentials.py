from __future__ import annotations

import httpx
import pytest

from corvus.mvp.provider_credentials import (
    ProviderCredentialError,
    ProviderCredentialService,
)


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def test_provider_credentials_are_write_only_and_never_exposed() -> None:
    keyring = _MemoryKeyring()
    service = ProviderCredentialService(keyring=keyring)

    status = service.connect("owner-1", "openai", "sk-test-secret-value")

    assert status == {"provider": "openai", "configured": True, "source": "keyring"}
    assert service.status("owner-1", "openai") == status
    assert "secret" not in repr(status).lower()
    assert service.require("owner-1", "openai") == "sk-test-secret-value"

    service.remove("owner-1", "openai")
    assert service.status("owner-1", "openai")["configured"] is False


def test_provider_credentials_fail_closed_when_secure_storage_is_unavailable() -> None:
    class _UnavailableKeyring(_MemoryKeyring):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError("backend unavailable")

    with pytest.raises(ProviderCredentialError, match="secure_storage_unavailable"):
        ProviderCredentialService(keyring=_UnavailableKeyring()).connect(
            "owner-1", "anthropic", "secret-value"
        )


def test_environment_credential_remains_available_when_keyring_reads_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnavailableReadKeyring(_MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise RuntimeError("backend unavailable")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret")
    service = ProviderCredentialService(keyring=_UnavailableReadKeyring())

    assert service.status("owner-1", "openai") == {
        "provider": "openai",
        "configured": True,
        "source": "environment",
    }
    assert service.require("owner-1", "openai") == "sk-environment-secret"


def test_keyring_read_failure_remains_explicit_without_an_environment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnavailableReadKeyring(_MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise RuntimeError("backend unavailable")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ProviderCredentialError, match="secure_storage_unavailable"):
        ProviderCredentialService(keyring=_UnavailableReadKeyring()).require("owner-1", "openai")


@pytest.mark.asyncio
async def test_verify_returns_authenticated_model_ids_without_exposing_the_key() -> None:
    keyring = _MemoryKeyring()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.6-terra"}]},
            request=request,
        )
    )
    service = ProviderCredentialService(
        keyring=keyring,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    service.connect("owner-1", "openai", "sk-test-secret-value")

    result = await service.verify("owner-1", "openai")

    assert result == {
        "provider": "openai",
        "configured": True,
        "verified": True,
        "models": ["gpt-5.6-sol", "gpt-5.6-terra"],
    }
    assert service.models("owner-1", "openai") == ("gpt-5.6-sol", "gpt-5.6-terra")
    assert "sk-test-secret-value" not in repr(result)


@pytest.mark.asyncio
async def test_verify_filters_openai_models_to_chat_capable_ids() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"id": "text-embedding-3-small"},
                    {"id": "omni-moderation-latest"},
                    {"id": "gpt-image-1"},
                    {"id": "gpt-5.6-sol"},
                    {"id": "o3"},
                ]
            },
            request=request,
        )
    )
    service = ProviderCredentialService(
        keyring=_MemoryKeyring(),
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    service.connect("owner-1", "openai", "sk-test-secret-value")

    result = await service.verify("owner-1", "openai")

    assert result["models"] == ["gpt-5.6-sol", "o3"]
