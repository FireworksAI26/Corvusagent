from __future__ import annotations

import json

from corvus.mvp.provider_catalog import build_provider_catalog


def test_provider_catalog_is_truthful_and_contains_recommended_models() -> None:
    catalog = build_provider_catalog(
        codex_available=True,
        claude_available=True,
        codex_models=("gpt-5.6-sol", "gpt-5.6-terra"),
        codex_effective_model="gpt-5.6-sol",
    )

    by_id = {provider.id: provider for provider in catalog}

    assert tuple(by_id) == ("codex", "claude", "gemini", "cursor", "xai")
    assert by_id["codex"].status == "ready"
    assert by_id["claude"].status == "ready"
    assert by_id["gemini"].status == "preview"
    assert by_id["cursor"].status == "unavailable"
    assert by_id["xai"].status == "preview"
    assert by_id["codex"].transport == "local"
    assert by_id["xai"].transport == "api"
    assert [model.id for model in by_id["codex"].models] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert all(model.id != "default" for model in by_id["codex"].models)
    assert by_id["codex"].models[0].recommended is True
    assert any(model.recommended for model in by_id["claude"].models)
    assert by_id["claude"].thinking_levels[-1] == "max"
    assert by_id["codex"].thinking_levels == ("low", "medium", "high", "xhigh")
    assert by_id["codex"].status_label == "CLI and login verified"
    assert by_id["claude"].status_label == "CLI and login verified"


def test_provider_catalog_never_serializes_executable_paths_or_secrets() -> None:
    rendered = json.dumps(
        [provider.as_dict() for provider in build_provider_catalog(False, False)],
        sort_keys=True,
    ).lower()

    assert "c:\\" not in rendered
    assert "/users/" not in rendered
    assert "api_key" not in rendered
    assert "token" not in rendered
    assert "secret" not in rendered
    assert "executable" not in rendered


def test_ready_codex_always_has_curated_models_when_cli_config_is_empty() -> None:
    catalog = build_provider_catalog(codex_available=True, claude_available=False)

    codex = next(provider for provider in catalog if provider.id == "codex")

    assert [model.id for model in codex.models] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert codex.models[0].recommended is True


def test_unverified_codex_never_exposes_models_or_efforts() -> None:
    catalog = build_provider_catalog(
        codex_available=False,
        codex_detected=True,
        claude_available=False,
    )

    codex = next(provider for provider in catalog if provider.id == "codex")

    assert codex.status == "unavailable"
    assert codex.status_label == "Detected, but CLI or login verification failed"
    assert codex.models == ()
    assert codex.thinking_levels == ()


def test_unverified_claude_never_exposes_models_or_efforts() -> None:
    catalog = build_provider_catalog(
        codex_available=False,
        claude_available=False,
        claude_detected=True,
    )

    claude = next(provider for provider in catalog if provider.id == "claude")

    assert claude.status == "unavailable"
    assert claude.status_label == "Detected, but CLI or login verification failed"
    assert claude.models == ()
    assert claude.thinking_levels == ()


def test_single_token_codex_model_keeps_a_visible_label() -> None:
    catalog = build_provider_catalog(
        codex_available=True,
        claude_available=False,
        codex_models=("o3",),
        codex_effective_model="o3",
    )

    codex = next(provider for provider in catalog if provider.id == "codex")

    assert codex.models[0].id == "o3"
    assert codex.models[0].label == "o3"
