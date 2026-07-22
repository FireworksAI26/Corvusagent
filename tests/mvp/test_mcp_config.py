from __future__ import annotations

import json
from pathlib import Path

import pytest

from corvus.mvp.git_process import ProcessResult
from corvus.mvp.mcp_config import McpConfigError, McpConfigService, McpServer


class _Cli:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def run(self, cwd: Path, args: tuple[str, ...], timeout: float = 30) -> ProcessResult:
        del cwd, timeout
        self.calls.append(args)
        if args[:3] == ("mcp", "list", "--json"):
            return ProcessResult(0, json.dumps(self.payload).encode(), b"")
        return ProcessResult(0, b"", b"")


def test_mcp_list_exposes_configuration_without_environment_values(tmp_path: Path) -> None:
    sensitive_value = "do-not-return-this-value"
    cli = _Cli(
        [
            {
                "name": "example",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": "example-mcp",
                    "env": {"TOKEN": sensitive_value},
                    "env_vars": ["TOKEN"],
                },
                "auth_status": "unsupported",
            }
        ]
    )

    servers = McpConfigService(cli, cwd=tmp_path).list()  # type: ignore[arg-type]

    assert servers[0].endpoint == "example-mcp"
    assert sensitive_value not in repr(servers)


def test_mcp_add_requires_https_and_uses_argv_not_a_shell(tmp_path: Path) -> None:
    cli = _Cli([])
    service = McpConfigService(cli, cwd=tmp_path)  # type: ignore[arg-type]

    with pytest.raises(McpConfigError, match="mcp_url_invalid"):
        service.add_remote("example", "http://example.com/mcp")

    server = service.add_remote("example", "https://example.com/mcp")

    assert cli.calls[0] == ("mcp", "add", "example", "--url", "https://example.com/mcp")
    assert server.endpoint == "https://example.com/mcp"


def test_mcp_add_returns_safe_fallback_when_refresh_is_invalid(tmp_path: Path) -> None:
    cli = _Cli({"unexpected": "shape"})
    service = McpConfigService(cli, cwd=tmp_path)  # type: ignore[arg-type]

    server = service.add_remote("example", "https://example.com/mcp")

    assert server == McpServer(
        "example", True, "streamable_http", "https://example.com/mcp", "unknown"
    )
