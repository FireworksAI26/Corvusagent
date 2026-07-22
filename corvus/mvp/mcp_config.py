from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from corvus.mvp.trusted_cli import TrustedCli

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class McpConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpServer:
    name: str
    enabled: bool
    transport: str
    endpoint: str
    auth_status: str


class McpConfigService:
    def __init__(self, cli: TrustedCli, *, cwd: Path) -> None:
        self._cli = cli
        self._cwd = cwd.resolve(strict=True)

    def list(self) -> tuple[McpServer, ...]:
        result = self._cli.run(self._cwd, ("mcp", "list", "--json"), 30)
        if result.returncode != 0:
            raise McpConfigError("mcp_list_failed")
        try:
            rows = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpConfigError("mcp_list_invalid") from exc
        if not isinstance(rows, list):
            raise McpConfigError("mcp_list_invalid")
        servers: list[McpServer] = []
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("transport"), dict):
                raise McpConfigError("mcp_list_invalid")
            row = cast(dict[str, object], raw)
            transport = cast(dict[str, object], row["transport"])
            name = row.get("name")
            kind = transport.get("type")
            if not isinstance(name, str) or not isinstance(kind, str):
                raise McpConfigError("mcp_list_invalid")
            endpoint_value = (
                transport.get("url") if kind == "streamable_http" else transport.get("command")
            )
            endpoint = endpoint_value if isinstance(endpoint_value, str) else "Configured locally"
            auth = row.get("auth_status")
            servers.append(
                McpServer(
                    name=name,
                    enabled=row.get("enabled") is True,
                    transport=kind,
                    endpoint=endpoint,
                    auth_status=auth if isinstance(auth, str) else "unknown",
                )
            )
        return tuple(servers)

    def add_remote(self, name: str, url: str) -> McpServer:
        if _NAME.fullmatch(name) is None:
            raise McpConfigError("mcp_name_invalid")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise McpConfigError("mcp_url_invalid")
        result = self._cli.run(self._cwd, ("mcp", "add", name, "--url", url), 30)
        if result.returncode != 0:
            raise McpConfigError("mcp_add_failed")
        try:
            servers = self.list()
        except McpConfigError:
            servers = ()
        return next(
            (server for server in servers if server.name == name),
            McpServer(name, True, "streamable_http", url, "unknown"),
        )

    def remove(self, name: str) -> None:
        if _NAME.fullmatch(name) is None:
            raise McpConfigError("mcp_name_invalid")
        if self._cli.run(self._cwd, ("mcp", "remove", name), 30).returncode != 0:
            raise McpConfigError("mcp_remove_failed")

    def login(self, name: str) -> None:
        if _NAME.fullmatch(name) is None:
            raise McpConfigError("mcp_name_invalid")
        if self._cli.run(self._cwd, ("mcp", "login", name), 120).returncode != 0:
            raise McpConfigError("mcp_login_failed")
