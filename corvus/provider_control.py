from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from corvus.chat_agent import ChatAgent, CodingWorkflowLike
from corvus.config import ConfigManager
from corvus.model_catalog import (
    CATALOG_BY_KIND,
    PROVIDER_ORDER,
    ProviderKind,
    ThinkingSetting,
    catalog_model,
    custom_model_profile,
    thinking_settings,
)
from corvus.models import ModelProvider
from corvus.providers import ModelProviderClient
from corvus.tui import (
    LiveModelOption,
    LiveModelSelectionError,
    LiveModelState,
    LiveProviderOption,
    LiveThinkingOption,
)

ProviderBuilder = Callable[[ModelProvider], ModelProviderClient | None]
ProviderReady = Callable[[ModelProvider], bool]
WorkflowBuilder = Callable[[ModelProviderClient], CodingWorkflowLike | None]

_CODEX_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SIMPLE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ROUTED_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,191}$")
_ROUTED_MODEL_KINDS = {"ollama", "openai_compatible", "openrouter"}
_HEALTH_TIMEOUT_SECONDS = 20.0
_CATALOG_PREFIX = "catalog:"
_EFFORT_PRESETS = {
    "low": "fast",
    "medium": "smart",
    "high": "high",
    "xhigh": "super_high",
    "max": "ultra",
}


class ConfiguredLiveModelController:
    """Validate, persist, and activate configured routes using the curated catalog."""

    def __init__(
        self,
        config: ConfigManager,
        runner: ChatAgent,
        *,
        provider_builder: ProviderBuilder,
        provider_ready: ProviderReady,
        workflow_builder: WorkflowBuilder,
    ) -> None:
        self._config = config
        self._runner = runner
        self._provider_builder = provider_builder
        self._provider_ready = provider_ready
        self._workflow_builder = workflow_builder
        self._activation_lock = asyncio.Lock()

    def state(self) -> LiveModelState:
        configured = self._config.providers()
        providers = self._provider_options(configured)
        selected = self._config.selected_provider()
        selected_option = next(
            (
                option
                for option in providers
                if selected is not None and option.configured and option.name == selected.name
            ),
            None,
        )
        return LiveModelState(
            providers=providers,
            active_provider=selected_option.name if selected_option is not None else None,
            active_model=(selected_option.selected_model if selected_option is not None else None),
            active_thinking=(
                selected_option.selected_thinking if selected_option is not None else None
            ),
        )

    async def activate(
        self,
        provider_name: str,
        model: str,
        thinking: str = "smart",
    ) -> LiveModelState:
        async with self._activation_lock:
            providers = await asyncio.to_thread(self._config.providers)
            target = next((item for item in providers if item.name == provider_name), None)
            if target is None:
                raise LiveModelSelectionError(
                    "That provider is not added. Run `corvus run --setup` first."
                )
            model_option = next(
                (item for item in self._models(target) if item.id == model),
                None,
            )
            if model_option is None:
                raise LiveModelSelectionError("That model is not available for this provider.")
            thinking_option = next(
                (item for item in model_option.thinking if item.id == thinking),
                None,
            )
            if thinking_option is None or thinking_option.effective_id is None:
                raise LiveModelSelectionError(
                    "That thinking preset is not supported by this model."
                )
            settings = self._settings(target, model, thinking)

            candidate_data = target.model_dump(mode="python")
            candidate_data.update(
                {
                    "model": model,
                    "thinking_preset": thinking,
                    "reasoning_effort": settings.reasoning_effort,
                    "thinking_enabled": settings.thinking_enabled,
                }
            )
            try:
                candidate = ModelProvider.model_validate(candidate_data)
            except ValueError as exc:
                raise LiveModelSelectionError(
                    "That model or thinking preset is not valid for this provider."
                ) from exc

            try:
                ready = await asyncio.to_thread(self._provider_ready, candidate)
            except Exception as exc:
                raise LiveModelSelectionError("Provider readiness could not be verified.") from exc
            if not ready:
                raise LiveModelSelectionError(
                    "Provider is not ready. Check its sign-in, credential, or local service."
                )
            try:
                client = await asyncio.to_thread(self._provider_builder, candidate)
            except Exception as exc:
                raise LiveModelSelectionError(
                    "Provider activation failed its integrity checks."
                ) from exc
            if client is None:
                raise LiveModelSelectionError("Provider activation failed its integrity checks.")
            try:
                healthy = await asyncio.wait_for(
                    client.health(),
                    timeout=_HEALTH_TIMEOUT_SECONDS,
                )
            except Exception:
                healthy = False
            if not healthy:
                raise LiveModelSelectionError(
                    "Provider health check failed; selection was not saved."
                )
            try:
                workflow = self._workflow_builder(client)
            except Exception as exc:
                raise LiveModelSelectionError("The live route could not be prepared.") from exc

            updated = [candidate if item.name == provider_name else item for item in providers]
            previous_active = await asyncio.to_thread(self._config.active_provider_name)
            try:
                await asyncio.to_thread(
                    self._config.save_providers,
                    updated,
                    active_provider=provider_name,
                )
            except Exception as exc:
                raise LiveModelSelectionError("The model selection could not be saved.") from exc
            try:
                self._runner.set_provider(client, workflow=workflow)
            except Exception as exc:
                await asyncio.to_thread(
                    self._config.save_providers,
                    providers,
                    active_provider=previous_active,
                )
                raise LiveModelSelectionError(
                    "The live route could not be replaced; the saved selection was restored."
                ) from exc
            return self.state()

    @classmethod
    def _provider_options(
        cls,
        providers: list[ModelProvider],
    ) -> tuple[LiveProviderOption, ...]:
        options: list[LiveProviderOption] = []
        for kind in PROVIDER_ORDER:
            matches = [provider for provider in providers if provider.kind == kind]
            if matches:
                options.extend(cls._option(provider) for provider in matches)
            else:
                catalog = CATALOG_BY_KIND[kind]
                options.append(
                    LiveProviderOption(
                        name=f"{_CATALOG_PREFIX}{kind}",
                        label=f"{catalog.label} (NOT ADDED)",
                        models=cls._catalog_models(kind),
                        selected_model=None,
                        configured=False,
                        selected_thinking=None,
                    )
                )
        return tuple(options)

    @classmethod
    def _option(cls, provider: ModelProvider) -> LiveProviderOption:
        kind = provider.kind
        models = cls._models(provider)
        selected_model = provider.model if provider.model in {item.id for item in models} else None
        selected_thinking = cls._selected_thinking(provider, models, selected_model)
        return LiveProviderOption(
            name=provider.name,
            label=f"{provider.name} ({CATALOG_BY_KIND[kind].label})",
            models=models,
            selected_model=selected_model,
            configured=True,
            selected_thinking=selected_thinking,
        )

    @classmethod
    def _catalog_models(cls, kind: ProviderKind) -> tuple[LiveModelOption, ...]:
        return tuple(
            cls._live_model(kind, model.id, model.label) for model in CATALOG_BY_KIND[kind].models
        )

    @classmethod
    def _models(cls, provider: ModelProvider) -> tuple[LiveModelOption, ...]:
        kind = provider.kind
        models = list(cls._catalog_models(kind))
        if cls._valid_model(provider, provider.model) and provider.model not in {
            item.id for item in models
        }:
            label = (provider.model or "Provider default") + " - configured custom"
            models.append(cls._live_model(kind, provider.model, label))
        return tuple(models)

    @staticmethod
    def _live_model(kind: ProviderKind, model_id: str, label: str) -> LiveModelOption:
        model = catalog_model(kind, model_id)
        profile = model.thinking_profile if model is not None else custom_model_profile(kind)
        thinking = tuple(
            LiveThinkingOption(
                id=setting.preset,
                label=setting.label,
                effective_id=setting.effective_id,
                detail=setting.detail,
            )
            for setting in thinking_settings(profile)
        )
        return LiveModelOption(id=model_id, label=label, thinking=thinking)

    @classmethod
    def _settings(
        cls,
        provider: ModelProvider,
        model_id: str,
        preset: str,
    ) -> ThinkingSetting:
        kind = provider.kind
        model = catalog_model(kind, model_id)
        profile = model.thinking_profile if model is not None else custom_model_profile(kind)
        return next(setting for setting in thinking_settings(profile) if setting.preset == preset)

    @staticmethod
    def _selected_thinking(
        provider: ModelProvider,
        models: tuple[LiveModelOption, ...],
        selected_model: str | None,
    ) -> str | None:
        model = next((item for item in models if item.id == selected_model), None)
        if model is None:
            return None
        supported = {item.id for item in model.thinking if item.effective_id is not None}
        if provider.thinking_preset in supported:
            return provider.thinking_preset
        inferred = _EFFORT_PRESETS.get(provider.reasoning_effort or "")
        if inferred in supported:
            return inferred
        return "smart" if "smart" in supported else next(iter(supported), None)

    @staticmethod
    def _valid_model(provider: ModelProvider, model: str) -> bool:
        if provider.kind == "codex_cli":
            return model == "" or _CODEX_MODEL_ID.fullmatch(model) is not None
        pattern = _ROUTED_MODEL_ID if provider.kind in _ROUTED_MODEL_KINDS else _SIMPLE_MODEL_ID
        return (
            pattern.fullmatch(model) is not None
            and ".." not in model
            and "//" not in model
            and not model.endswith(("/", ":"))
        )


__all__ = ["ConfiguredLiveModelController"]
