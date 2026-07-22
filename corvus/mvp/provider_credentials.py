from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, Literal, Protocol, TypedDict, cast

import httpx
import keyring

ProviderCredentialId = Literal[
    "openai", "anthropic", "gemini", "xai", "openrouter", "together",
    "fireworks", "groq", "mistral", "deepseek"
]
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "openai", "anthropic", "gemini", "xai", "openrouter", "together",
    "fireworks", "groq", "mistral", "deepseek"
})
_ENVIRONMENT_KEYS: dict[ProviderCredentialId, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_KEYRING_SERVICE = "corvus.provider-credentials.v1"
_MODEL_ENDPOINTS: dict[ProviderCredentialId, str] = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
    "xai": "https://api.x.ai/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "together": "https://api.together.xyz/v1/models",
    "fireworks": "https://api.fireworks.ai/inference/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "deepseek": "https://api.deepseek.com/v1/models",
}
_HTTP_TIMEOUT_SECONDS = 15.0


class ProviderCredentialStatus(TypedDict):
    provider: ProviderCredentialId
    configured: bool
    source: Literal["keyring", "environment", "none"]


class ProviderVerification(TypedDict):
    provider: ProviderCredentialId
    configured: bool
    verified: bool
    models: list[str]


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class ProviderCredentialError(RuntimeError):
    pass


class ProviderCredentialService:
    """Owner-scoped, write-only API credentials stored by the operating system."""

    def __init__(
        self,
        *,
        keyring: KeyringBackend | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._keyring = keyring or keyring_module()
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False)
        )
        self._verified_models: dict[tuple[str, ProviderCredentialId], tuple[str, ...]] = {}

    def connect(
        self,
        owner: str,
        provider: str,
        credential: str,
    ) -> ProviderCredentialStatus:
        provider_id = _provider_id(provider)
        normalized = credential.strip()
        if len(normalized) < 8 or len(normalized) > 16_384:
            raise ProviderCredentialError("provider_credential_invalid")
        try:
            self._keyring.set_password(
                _KEYRING_SERVICE,
                _account(owner, provider_id),
                normalized,
            )
        except Exception as error:
            raise ProviderCredentialError("secure_storage_unavailable") from error
        self._verified_models.pop((owner, provider_id), None)
        return {"provider": provider_id, "configured": True, "source": "keyring"}

    def status(self, owner: str, provider: str) -> ProviderCredentialStatus:
        provider_id = _provider_id(provider)
        try:
            stored = self._keyring.get_password(
                _KEYRING_SERVICE,
                _account(owner, provider_id),
            )
        except Exception:
            stored = None
        if stored:
            return {"provider": provider_id, "configured": True, "source": "keyring"}
        if os.environ.get(_ENVIRONMENT_KEYS[provider_id], "").strip():
            return {"provider": provider_id, "configured": True, "source": "environment"}
        return {"provider": provider_id, "configured": False, "source": "none"}

    def require(self, owner: str, provider: str) -> str:
        provider_id = _provider_id(provider)
        try:
            stored = self._keyring.get_password(
                _KEYRING_SERVICE,
                _account(owner, provider_id),
            )
        except Exception as error:
            environment_credential = os.environ.get(_ENVIRONMENT_KEYS[provider_id], "").strip()
            if environment_credential:
                return environment_credential
            raise ProviderCredentialError("secure_storage_unavailable") from error
        credential = stored or os.environ.get(_ENVIRONMENT_KEYS[provider_id], "")
        if not credential.strip():
            raise ProviderCredentialError("provider_credential_missing")
        return credential.strip()

    def remove(self, owner: str, provider: str) -> ProviderCredentialStatus:
        provider_id = _provider_id(provider)
        try:
            if self._keyring.get_password(_KEYRING_SERVICE, _account(owner, provider_id)):
                self._keyring.delete_password(_KEYRING_SERVICE, _account(owner, provider_id))
        except Exception as error:
            raise ProviderCredentialError("secure_storage_unavailable") from error
        self._verified_models.pop((owner, provider_id), None)
        return self.status(owner, provider_id)

    async def verify(self, owner: str, provider: str) -> ProviderVerification:
        provider_id = _provider_id(provider)
        credential = self.require(owner, provider_id)
        headers = _verification_headers(provider_id, credential)
        try:
            async with self._http_client_factory() as client:
                response = await client.get(_MODEL_ENDPOINTS[provider_id], headers=headers)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderCredentialError("provider_verification_failed") from error
        models = _model_ids(provider_id, payload)
        self._verified_models[(owner, provider_id)] = tuple(models)
        return {
            "provider": provider_id,
            "configured": True,
            "verified": True,
            "models": models,
        }

    def models(self, owner: str, provider: str) -> tuple[str, ...]:
        return self._verified_models.get((owner, _provider_id(provider)), ())


def keyring_module() -> KeyringBackend:
    return cast(KeyringBackend, keyring)


def _provider_id(provider: str) -> ProviderCredentialId:
    normalized = provider.strip().lower()
    if normalized not in _SUPPORTED_PROVIDERS:
        raise ProviderCredentialError("provider_unsupported")
    return cast(ProviderCredentialId, normalized)


def _account(owner: str, provider: ProviderCredentialId) -> str:
    normalized_owner = owner.strip()
    if not normalized_owner or len(normalized_owner) > 512:
        raise ProviderCredentialError("credential_owner_invalid")
    return f"{normalized_owner}:{provider}"


def _verification_headers(provider: ProviderCredentialId, credential: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": credential, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {"x-goog-api-key": credential}
    return {"Authorization": f"Bearer {credential}"}


def _model_ids(provider: ProviderCredentialId, payload: Any) -> list[str]:
    if isinstance(payload, list) and provider in {
        "openrouter", "together", "fireworks", "groq", "mistral", "deepseek"
    }:
        raw_models = payload
    elif isinstance(payload, dict):
        raw_models = payload.get("models" if provider == "gemini" else "data")
    else:
        raise ProviderCredentialError("provider_response_invalid")
    if not isinstance(raw_models, list):
        raise ProviderCredentialError("provider_response_invalid")
    ids: list[str] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        candidate = raw_model.get("name" if provider == "gemini" else "id")
        if (
            isinstance(candidate, str)
            and candidate.strip()
            and _is_chat_capable_model(provider, raw_model, candidate)
        ):
            ids.append(candidate.removeprefix("models/").strip())
    return list(dict.fromkeys(ids))[:200]


def _is_chat_capable_model(
    provider: ProviderCredentialId,
    raw_model: dict[str, Any],
    candidate: str,
) -> bool:
    normalized = candidate.removeprefix("models/").strip().lower()
    if provider == "openai":
        if any(
            marker in normalized
            for marker in (
                "audio",
                "dall-e",
                "embedding",
                "image",
                "moderation",
                "realtime",
                "transcribe",
                "tts",
                "whisper",
            )
        ):
            return False
        return normalized.startswith(("gpt-", "chatgpt-", "codex-")) or bool(
            re.fullmatch(r"o\d+(?:[-.].+)?", normalized)
        )
    if provider == "anthropic":
        return normalized.startswith("claude-")
    if provider == "xai":
        return normalized.startswith("grok-")
    if provider in {"openrouter", "together", "fireworks", "groq", "mistral", "deepseek"}:
        return not any(marker in normalized for marker in ("embedding", "rerank", "moderation", "tts", "whisper"))
    methods = raw_model.get("supportedGenerationMethods")
    return isinstance(methods, list) and any(
        method in {"generateContent", "streamGenerateContent"} for method in methods
    )
