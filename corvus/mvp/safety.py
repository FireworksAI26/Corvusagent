from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

SafetyLevel = Literal["read_only", "protected", "elevated"]

_POLICY_VERSION = "local-alpha-v1"
_NETWORK_DISCLOSURE = (
    "Network access follows the selected CLI sandbox policy; "
    "Corvus grants no separate network permission."
)
_NO_BLANKET_APPROVAL = (
    "Corvus grants no blanket host approval; actions remain inside the selected runtime policy."
)
_API_PROVIDERS = frozenset({
    "openai", "anthropic", "gemini", "xai", "openrouter", "together",
    "fireworks", "groq", "mistral", "deepseek"
})


@dataclass(frozen=True, slots=True)
class SafetyPreview:
    policy_digest: str
    level: SafetyLevel
    label: str
    summary: str
    execution: str
    filesystem: str
    network: str
    mcp: str
    approvals: str
    output: str
    requires_confirmation: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_safety_preview(*, provider: str, mode: str, mcp_enabled: bool) -> SafetyPreview:
    if provider not in {"codex", "claude", *_API_PROVIDERS}:
        raise ValueError("provider_unavailable")
    if mode not in {"chat", "build"}:
        raise ValueError("provider_mode_unavailable")
    if provider != "codex" and (mode != "chat" or mcp_enabled):
        raise ValueError("provider_mode_unavailable")

    if provider in _API_PROVIDERS:
        policy = {
            "policy_version": _POLICY_VERSION,
            "provider": provider,
            "mode": mode,
            "mcp_enabled": False,
            "level": "read_only",
            "label": "API chat",
            "summary": "The prompt is sent to the selected provider for chat only; no project files or tools are available.",
            "execution": "The provider API streams a response without filesystem, shell, or MCP authority.",
            "filesystem": "No project filesystem is attached to this API chat.",
            "network": "The prompt is sent directly to the selected model provider over HTTPS.",
            "mcp": "Disabled: API chat cannot load MCP servers.",
            "approvals": _NO_BLANKET_APPROVAL,
            "output": "The response is streamed to this conversation; no project artifact is exported.",
            "requires_confirmation": False,
        }
        digest = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return SafetyPreview(
            policy_digest=digest,
            level="read_only",
            label="API chat",
            summary="The prompt is sent to the selected provider for chat only; no project files or tools are available.",
            execution="The provider API streams a response without filesystem, shell, or MCP authority.",
            filesystem="No project filesystem is attached to this API chat.",
            network="The prompt is sent directly to the selected model provider over HTTPS.",
            mcp="Disabled: API chat cannot load MCP servers.",
            approvals=_NO_BLANKET_APPROVAL,
            output="The response is streamed to this conversation; no project artifact is exported.",
            requires_confirmation=False,
        )

    is_build = mode == "build"
    level: SafetyLevel = "elevated" if mcp_enabled else "protected" if is_build else "read_only"
    label = "External tools on" if mcp_enabled else "Protected build" if is_build else "Read-only"
    summary = (
        "Configured MCP tools can reach external systems from a fresh build sandbox."
        if mcp_enabled
        else "Work happens in a fresh writable sandbox; your original project stays unchanged."
        if is_build
        else "The agent can inspect context and answer without writing project files."
    )
    execution = (
        "Codex CLI runs ephemerally with plugins, apps, and hooks disabled."
        if provider == "codex"
        else "Claude CLI runs without tools in plan mode and does not persist a session."
    )
    filesystem = (
        "A fresh scratch workspace is writable; the original project is not modified."
        if is_build
        else "The selected CLI sandbox is read-only; no project artifact is produced."
    )
    mcp = (
        "Enabled: configured MCP tools may act on external systems under their own credentials and policies."
        if mcp_enabled
        else "Disabled: configured MCP servers are not loaded for this run."
    )
    output = (
        "Only a Corvus-screened ZIP artifact can be downloaded after completion."
        if is_build
        else "The response is streamed to this conversation; no project artifact is exported."
    )
    requires_confirmation = is_build or mcp_enabled
    policy = {
        "policy_version": _POLICY_VERSION,
        "provider": provider,
        "mode": mode,
        "mcp_enabled": mcp_enabled,
        "level": level,
        "label": label,
        "summary": summary,
        "execution": execution,
        "filesystem": filesystem,
        "network": _NETWORK_DISCLOSURE,
        "mcp": mcp,
        "approvals": _NO_BLANKET_APPROVAL,
        "output": output,
        "requires_confirmation": requires_confirmation,
    }
    digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SafetyPreview(
        policy_digest=digest,
        level=level,
        label=label,
        summary=summary,
        execution=execution,
        filesystem=filesystem,
        network=_NETWORK_DISCLOSURE,
        mcp=mcp,
        approvals=_NO_BLANKET_APPROVAL,
        output=output,
        requires_confirmation=requires_confirmation,
    )
