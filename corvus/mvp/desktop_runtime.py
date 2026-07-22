from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import TextIO

import uvicorn

from corvus.mvp.cli import (
    DEFAULT_INSTANCE_REFERENCE,
    DEFAULT_PAIRING_REFERENCE,
    DEFAULT_SIGNING_REFERENCE,
    build_server_app,
)

DESKTOP_SHUTDOWN_COMMAND = "shutdown"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def run_desktop_sidecar(
    *,
    database: Path,
    host: str,
    port: int,
    static_web_dir: Path,
    pairing_ref: str = DEFAULT_PAIRING_REFERENCE,
    signing_ref: str = DEFAULT_SIGNING_REFERENCE,
    shutdown_stream: TextIO = sys.stdin,
) -> None:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("desktop_sidecar_loopback_required")
    app = build_server_app(
        database=database,
        pairing_ref=pairing_ref,
        signing_ref=signing_ref,
        static_web_dir=static_web_dir,
        allow_existing_user_pairing=True,
        instance_ref=DEFAULT_INSTANCE_REFERENCE,
        allowed_origins=frozenset(
            {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            }
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            access_log=False,
            log_level="warning",
        )
    )
    monitor = threading.Thread(
        target=_monitor_shutdown,
        args=(server, shutdown_stream),
        name="corvus-desktop-shutdown",
        daemon=True,
    )
    monitor.start()
    server.run()


def _monitor_shutdown(server: uvicorn.Server, shutdown_stream: TextIO) -> None:
    for line in shutdown_stream:
        if line.strip() == DESKTOP_SHUTDOWN_COMMAND:
            server.should_exit = True
            return
    server.should_exit = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the supervised Corvus desktop sidecar")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--static-web-dir", required=True, type=Path)
    parser.add_argument("--bootstrap-token-ref", default=DEFAULT_PAIRING_REFERENCE)
    parser.add_argument("--session-secret-ref", default=DEFAULT_SIGNING_REFERENCE)
    arguments = parser.parse_args()
    run_desktop_sidecar(
        database=arguments.database.expanduser().resolve(),
        host=arguments.host,
        port=arguments.port,
        static_web_dir=arguments.static_web_dir.expanduser().resolve(),
        pairing_ref=arguments.bootstrap_token_ref,
        signing_ref=arguments.session_secret_ref,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
