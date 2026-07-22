from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderCatalogStatus = Literal["ready", "preview", "unavailable"]
ProviderCatalogTransport = Literal["local", "api"]
ThinkingLevel = Literal["low", "medium", "high", "xhigh", "max"]
_CURATED_CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra")


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    label: str
    recommended: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "recommended": self.recommended,
        }


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    id: str
    name: str
    transport: ProviderCatalogTransport
    status: ProviderCatalogStatus
    status_label: str
    models: tuple[ProviderModel, ...]
    thinking_levels: tuple[ThinkingLevel, ...]
    supports_mcp: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "status": self.status,
            "status_label": self.status_label,
            "models": [model.as_dict() for model in self.models],
            "thinking_levels": list(self.thinking_levels),
            "supports_mcp": self.supports_mcp,
        }


def build_provider_catalog(
    codex_available: bool,
    claude_available: bool,
    *,
    codex_detected: bool | None = None,
    claude_detected: bool | None = None,
    codex_models: tuple[str, ...] = (),
    codex_effective_model: str | None = None,
) -> tuple[ProviderCatalogEntry, ...]:
    """Return the UI-safe provider catalog without local identity details."""

    codex_was_detected = codex_available if codex_detected is None else codex_detected
    claude_was_detected = claude_available if claude_detected is None else claude_detected

    return (
        ProviderCatalogEntry(
            id="codex",
            name="OpenAI Codex",
            transport="local",
            status="ready" if codex_available else "unavailable",
            status_label=(
                "CLI and login verified"
                if codex_available
                else "Detected, but CLI or login verification failed"
                if codex_was_detected
                else "Not installed"
            ),
            models=(_codex_models(codex_models, codex_effective_model) if codex_available else ()),
            thinking_levels=("low", "medium", "high", "xhigh") if codex_available else (),
            supports_mcp=True,
        ),
        ProviderCatalogEntry(
            id="claude",
            name="Claude Code",
            transport="local",
            status="ready" if claude_available else "unavailable",
            status_label=(
                "CLI and login verified"
                if claude_available
                else "Detected, but CLI or login verification failed"
                if claude_was_detected
                else "Not installed"
            ),
            models=(
                ProviderModel("sonnet", "Claude Sonnet", recommended=True),
                ProviderModel("opus", "Claude Opus"),
            )
            if claude_available
            else (),
            thinking_levels=("low", "medium", "high", "xhigh", "max") if claude_available else (),
            supports_mcp=False,
        ),
        ProviderCatalogEntry(
            id="gemini",
            name="Gemini CLI",
            transport="local",
            status="preview",
            status_label="Preview - safe local execution is not enabled yet",
            models=(ProviderModel("gemini-default", "Gemini recommended", recommended=True),),
            thinking_levels=("low", "medium", "high"),
            supports_mcp=False,
        ),
        ProviderCatalogEntry(
            id="cursor",
            name="Cursor Agent",
            transport="local",
            status="unavailable",
            status_label="Unavailable on this device",
            models=(ProviderModel("cursor-default", "Cursor recommended", recommended=True),),
            thinking_levels=("low", "medium", "high"),
            supports_mcp=False,
        ),
        ProviderCatalogEntry(
            id="xai",
            name="Grok by xAI",
            transport="api",
            status="preview",
            status_label="Preview - API connection is coming later",
            models=(ProviderModel("grok-default", "Grok recommended", recommended=True),),
            thinking_levels=("low", "medium", "high"),
            supports_mcp=False,
        ),
    )


def _codex_models(
    discovered: tuple[str, ...],
    effective: str | None,
) -> tuple[ProviderModel, ...]:
    ordered: list[str] = []
    if effective:
        ordered.append(effective)
    ordered.extend(discovered)
    ordered.extend(_CURATED_CODEX_MODELS)
    unique = tuple(dict.fromkeys(model.strip() for model in ordered if model.strip()))
    return tuple(
        ProviderModel(model, _model_label(model), recommended=index == 0)
        for index, model in enumerate(unique)
    )


def _model_label(model: str) -> str:
    parts = model.split("-")
    if len(parts) == 1:
        return model
    if parts and parts[0].lower() == "gpt":
        parts[0] = "GPT"
    return "-".join(parts[:-1]) + f" {parts[-1].title()}"
