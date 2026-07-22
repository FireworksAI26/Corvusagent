"""Hosted ASGI entry point that preserves the local CLI loopback boundary."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from corvus.mvp.cli import build_server_app

BOOTSTRAP_TOKEN_REFERENCE = "env://CORVUS_BOOTSTRAP_TOKEN"  # noqa: S105
DEFAULT_HOSTED_DATABASE = "corvus.sqlite3"
HOSTED_DATABASE_ENV = "CORVUS_HOSTED_DATABASE_PATH"
PUBLIC_ORIGIN_ENV = "CORVUS_PUBLIC_ORIGIN"
SESSION_SECRET_REFERENCE = "env://CORVUS_SESSION_SECRET"  # noqa: S105


def create_hosted_app() -> FastAPI:
    """Build the externally bound cloud app without relaxing local server policy."""
    public_origin = _required_environment_value(PUBLIC_ORIGIN_ENV)
    database = Path(os.environ.get(HOSTED_DATABASE_ENV, DEFAULT_HOSTED_DATABASE))
    return build_server_app(
        database=database,
        pairing_ref=BOOTSTRAP_TOKEN_REFERENCE,
        signing_ref=SESSION_SECRET_REFERENCE,
        allowed_origins=frozenset({public_origin}),
    )


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing_required_environment_variable:{name}")
    return value
