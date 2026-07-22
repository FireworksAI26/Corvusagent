from corvus.mvp.safety import build_safety_preview


def test_safety_preview_is_deterministic_and_describes_real_local_policy() -> None:
    first = build_safety_preview(provider="codex", mode="chat", mcp_enabled=False)
    second = build_safety_preview(provider="codex", mode="chat", mcp_enabled=False)

    assert first == second
    assert first.level == "read_only"
    assert first.requires_confirmation is False
    assert "read-only" in first.filesystem.lower()
    assert "no separate network permission" in first.network.lower()


def test_build_preview_requires_confirmation_and_discloses_mcp_risk() -> None:
    protected = build_safety_preview(provider="codex", mode="build", mcp_enabled=False)
    elevated = build_safety_preview(provider="codex", mode="build", mcp_enabled=True)

    assert protected.level == "protected"
    assert protected.requires_confirmation is True
    assert "original project" in protected.filesystem.lower()
    assert elevated.level == "elevated"
    assert elevated.requires_confirmation is True
    assert "external systems" in elevated.mcp.lower()
    assert protected.policy_digest != elevated.policy_digest


def test_api_chat_never_claims_sandboxed_build_or_tool_access() -> None:
    preview = build_safety_preview(provider="openai", mode="chat", mcp_enabled=False)

    assert preview.label == "API chat"
    assert "sent directly" in preview.network
    assert "No project filesystem" in preview.filesystem
    assert preview.requires_confirmation is False
