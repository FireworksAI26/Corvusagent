from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type ProviderKind = Literal[
    "codex_cli",
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "ollama",
    "openai_compatible",
]
type ThinkingPreset = Literal["fast", "smart", "high", "super_high", "ultra"]
type ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
type ThinkingProfile = Literal[
    "effort_all",
    "effort_three",
    "gemini_three",
    "ollama_extended",
    "ollama_gpt_oss",
    "provider_default",
]

CATALOG_VERIFIED_ON = "2026-07-11"
PROVIDER_ORDER: tuple[ProviderKind, ...] = (
    "codex_cli",
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "ollama",
    "openai_compatible",
)
THINKING_PRESET_ORDER: tuple[ThinkingPreset, ...] = (
    "fast",
    "smart",
    "high",
    "super_high",
    "ultra",
)


@dataclass(frozen=True, slots=True)
class ThinkingSetting:
    preset: ThinkingPreset
    label: str
    effective_id: ThinkingPreset | None
    reasoning_effort: ReasoningEffort | None
    thinking_enabled: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class CatalogModel:
    id: str
    label: str
    thinking_profile: ThinkingProfile


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    kind: ProviderKind
    label: str
    source_url: str
    verified_on: str
    models: tuple[CatalogModel, ...]


_PRESET_LABELS: dict[ThinkingPreset, str] = {
    "fast": "Fast",
    "smart": "Smart",
    "high": "High",
    "super_high": "Super High",
    "ultra": "Ultra",
}
_PRESET_EFFORTS: dict[ThinkingPreset, ReasoningEffort] = {
    "fast": "low",
    "smart": "medium",
    "high": "high",
    "super_high": "xhigh",
    "ultra": "max",
}


def _models(
    profile: ThinkingProfile,
    *items: tuple[str, str],
) -> tuple[CatalogModel, ...]:
    return tuple(CatalogModel(model_id, label, profile) for model_id, label in items)


PROVIDER_CATALOGS: tuple[ProviderCatalog, ...] = (
    ProviderCatalog(
        "codex_cli",
        "Codex / ChatGPT",
        "https://developers.openai.com/codex/models",
        CATALOG_VERIFIED_ON,
        _models(
            "effort_all",
            ("gpt-5.6-sol", "GPT-5.6 Sol - strongest (Recommended)"),
            ("gpt-5.6-terra", "GPT-5.6 Terra - balanced"),
            ("gpt-5.6-luna", "GPT-5.6 Luna - fastest"),
            ("gpt-5.5", "GPT-5.5 - frontier"),
            ("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark - instant Pro preview"),
            ("gpt-5.4", "GPT-5.4 - reliable"),
            ("gpt-5.4-mini", "GPT-5.4 Mini - efficient"),
        ),
    ),
    ProviderCatalog(
        "openai",
        "OpenAI API",
        "https://developers.openai.com/api/docs/models",
        CATALOG_VERIFIED_ON,
        _models(
            "effort_all",
            ("gpt-5.6-sol", "GPT-5.6 Sol - strongest (Recommended)"),
            ("gpt-5.6-terra", "GPT-5.6 Terra - balanced"),
            ("gpt-5.6-luna", "GPT-5.6 Luna - fastest"),
            ("gpt-5.5", "GPT-5.5 - frontier"),
            ("gpt-5.4-mini", "GPT-5.4 Mini - efficient"),
        ),
    ),
    ProviderCatalog(
        "anthropic",
        "Anthropic",
        "https://platform.claude.com/docs/en/about-claude/models/overview",
        CATALOG_VERIFIED_ON,
        (
            CatalogModel("claude-fable-5", "Claude Fable 5 - top pick (Recommended)", "effort_all"),
            CatalogModel("claude-opus-4-8", "Claude Opus 4.8 - deep reasoning", "effort_all"),
            CatalogModel("claude-sonnet-5", "Claude Sonnet 5 - balanced", "effort_all"),
            CatalogModel(
                "claude-haiku-4-5",
                "Claude Haiku 4.5 - fast",
                "provider_default",
            ),
        ),
    ),
    ProviderCatalog(
        "gemini",
        "Google Gemini",
        "https://ai.google.dev/gemini-api/docs/models",
        CATALOG_VERIFIED_ON,
        _models(
            "gemini_three",
            ("gemini-3.5-flash", "Gemini 3.5 Flash - top pick (Recommended)"),
            ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview - advanced"),
            ("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite - efficient"),
            ("gemini-3-flash-preview", "Gemini 3 Flash Preview - fast"),
        ),
    ),
    ProviderCatalog(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1/models",
        CATALOG_VERIFIED_ON,
        (
            CatalogModel(
                "openai/gpt-5.6-sol", "GPT-5.6 Sol - strongest (Recommended)", "effort_all"
            ),
            CatalogModel("openai/gpt-5.6-terra", "GPT-5.6 Terra - balanced", "effort_all"),
            CatalogModel("openai/gpt-5.6-luna", "GPT-5.6 Luna - fastest", "effort_all"),
            CatalogModel("anthropic/claude-fable-5", "Claude Fable 5 - top pick", "effort_all"),
            CatalogModel(
                "anthropic/claude-opus-4.8", "Claude Opus 4.8 - deep reasoning", "effort_all"
            ),
            CatalogModel("anthropic/claude-sonnet-5", "Claude Sonnet 5 - balanced", "effort_all"),
            CatalogModel("google/gemini-3.5-flash", "Gemini 3.5 Flash - fast", "gemini_three"),
            CatalogModel(
                "google/gemini-3.1-pro-preview",
                "Gemini 3.1 Pro Preview - advanced",
                "gemini_three",
            ),
        ),
    ),
    ProviderCatalog(
        "ollama",
        "Ollama",
        "https://docs.ollama.com/capabilities/thinking",
        CATALOG_VERIFIED_ON,
        (
            CatalogModel("qwen3.6", "Qwen 3.6 - top pick (Recommended)", "ollama_extended"),
            CatalogModel("qwen3-coder-next", "Qwen 3 Coder Next - coding", "ollama_extended"),
            CatalogModel("gpt-oss:20b", "GPT-OSS 20B - efficient", "ollama_gpt_oss"),
            CatalogModel("gpt-oss:120b", "GPT-OSS 120B - large", "ollama_gpt_oss"),
            CatalogModel("deepseek-r1", "DeepSeek R1 - reasoning", "ollama_extended"),
        ),
    ),
    ProviderCatalog(
        "openai_compatible",
        "OpenAI-compatible",
        "Endpoint-defined configured models only",
        CATALOG_VERIFIED_ON,
        (),
    ),
)

CATALOG_BY_KIND: dict[ProviderKind, ProviderCatalog] = {
    catalog.kind: catalog for catalog in PROVIDER_CATALOGS
}


def thinking_settings(profile: ThinkingProfile) -> tuple[ThinkingSetting, ...]:
    supported: set[ThinkingPreset]
    if profile == "effort_all":
        supported = set(THINKING_PRESET_ORDER)
    elif profile in {"effort_three", "gemini_three", "ollama_gpt_oss"}:
        supported = {"fast", "smart", "high"}
    elif profile == "ollama_extended":
        supported = {"fast", "smart", "high", "ultra"}
    else:
        supported = {"smart"}

    settings: list[ThinkingSetting] = []
    for preset in THINKING_PRESET_ORDER:
        label = _PRESET_LABELS[preset]
        if preset not in supported:
            settings.append(
                ThinkingSetting(
                    preset,
                    label,
                    None,
                    None,
                    None,
                    "Not supported by this model; choose another preset.",
                )
            )
            continue
        if profile == "provider_default":
            settings.append(
                ThinkingSetting(
                    preset,
                    label,
                    preset,
                    None,
                    None,
                    "Uses the provider default; no explicit effort setting is sent.",
                )
            )
            continue
        effort = _PRESET_EFFORTS[preset]
        detail = (
            "Maximum single-agent effort within the configured token and cost caps; "
            "Agents ON remains a separate setting."
            if preset == "ultra"
            else f"Requests {effort} reasoning effort within configured token and cost caps."
        )
        settings.append(
            ThinkingSetting(
                preset,
                label,
                preset,
                effort,
                None,
                detail,
            )
        )
    return tuple(settings)


def catalog_model(kind: ProviderKind, model_id: str) -> CatalogModel | None:
    return next(
        (model for model in CATALOG_BY_KIND[kind].models if model.id == model_id),
        None,
    )


def custom_model_profile(kind: ProviderKind) -> ThinkingProfile:
    del kind
    return "provider_default"


__all__ = [
    "CATALOG_BY_KIND",
    "CATALOG_VERIFIED_ON",
    "PROVIDER_CATALOGS",
    "PROVIDER_ORDER",
    "THINKING_PRESET_ORDER",
    "CatalogModel",
    "ProviderCatalog",
    "ProviderKind",
    "ReasoningEffort",
    "ThinkingPreset",
    "ThinkingSetting",
    "catalog_model",
    "custom_model_profile",
    "thinking_settings",
]
