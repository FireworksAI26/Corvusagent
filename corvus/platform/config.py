from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError

_DATABASE_URL_ENV: Final = "CORVUS_DATABASE_URL"
_SESSION_SECRET_ENV: Final = "CORVUS_SESSION_SECRET"  # noqa: S105
_OAUTH_STATE_SECRET_ENV: Final = "CORVUS_OAUTH_STATE_SECRET"  # noqa: S105
_PUBLIC_ORIGIN_ENV: Final = "CORVUS_PUBLIC_ORIGIN"
_GOOGLE_CLIENT_ID_ENV: Final = "CORVUS_GOOGLE_CLIENT_ID"
_GOOGLE_CLIENT_SECRET_REF_ENV: Final = "CORVUS_GOOGLE_CLIENT_SECRET_REF"  # noqa: S105
_GOOGLE_REDIRECT_URIS_ENV: Final = "CORVUS_GOOGLE_REDIRECT_URIS"
_OAUTH_TRANSACTION_SECRET_ENV: Final = "CORVUS_OAUTH_TRANSACTION_SECRET"  # noqa: S105
_MINIMUM_SECRET_LENGTH: Final = 32
_SQLITE_DRIVERS: Final = frozenset({"sqlite", "sqlite+pysqlite"})
_POSTGRES_DRIVER: Final = "postgresql+psycopg"


def _required_environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"missing_required_setting:{name}")
    return value


def _validated_database_url(database_url: str) -> URL:
    try:
        parsed = make_url(database_url)
    except ArgumentError:
        raise ValueError("unsupported_database_url") from None
    if parsed.drivername not in {*_SQLITE_DRIVERS, _POSTGRES_DRIVER}:
        raise ValueError("unsupported_database_url")
    return parsed


def _database_driver(database_url: str) -> str:
    return _validated_database_url(database_url).drivername


def _secure_web_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.query or parsed.fragment:
        return False
    has_credentials = parsed.username is not None or parsed.password is not None
    if parsed.scheme == "https" and parsed.netloc and not has_credentials:
        return True
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.netloc != ""
        and not has_credentials
    )


@dataclass(frozen=True, slots=True)
class HostedOAuthSettings:
    public_origin: str
    google_client_id: str
    google_client_secret_ref: str = field(repr=False)
    google_redirect_uris: frozenset[str]
    transaction_encryption_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _secure_web_url(self.public_origin) or urlparse(self.public_origin).path not in {
            "",
            "/",
        }:
            raise ValueError("public_origin_invalid")
        if not self.google_client_id.strip() or not self.google_client_secret_ref.strip():
            raise ValueError("google_oauth_configuration_invalid")
        if not self.google_redirect_uris or any(
            not _secure_web_url(uri) for uri in self.google_redirect_uris
        ):
            raise ValueError("google_redirect_uri_invalid")
        if len(self.transaction_encryption_secret) < _MINIMUM_SECRET_LENGTH:
            raise ValueError(f"secret_too_short:{_OAUTH_TRANSACTION_SECRET_ENV}")


@dataclass(frozen=True, slots=True)
class PlatformSettings:
    database_url: str = field(repr=False)
    session_secret: str = field(repr=False)
    oauth_state_secret: str = field(repr=False)
    hosted_oauth: HostedOAuthSettings | None = None

    def __post_init__(self) -> None:
        _validated_database_url(self.database_url)
        for name, secret in (
            (_SESSION_SECRET_ENV, self.session_secret),
            (_OAUTH_STATE_SECRET_ENV, self.oauth_state_secret),
        ):
            if not secret.strip():
                raise ValueError(f"secret_blank:{name}")
            if len(secret) < _MINIMUM_SECRET_LENGTH:
                raise ValueError(f"secret_too_short:{name}")
        if hmac.compare_digest(self.session_secret, self.oauth_state_secret):
            raise ValueError("secrets_must_be_distinct")
        if self.hosted_oauth is not None and any(
            hmac.compare_digest(self.hosted_oauth.transaction_encryption_secret, secret)
            for secret in (self.session_secret, self.oauth_state_secret)
        ):
            raise ValueError("secrets_must_be_distinct")

    @classmethod
    def from_env(cls) -> PlatformSettings:
        database_url = _required_environment_value(_DATABASE_URL_ENV)
        session_secret = _required_environment_value(_SESSION_SECRET_ENV)
        oauth_state_secret = _required_environment_value(_OAUTH_STATE_SECRET_ENV)
        optional = {
            name: os.environ.get(name, "").strip()
            for name in (
                _PUBLIC_ORIGIN_ENV,
                _GOOGLE_CLIENT_ID_ENV,
                _GOOGLE_CLIENT_SECRET_REF_ENV,
                _GOOGLE_REDIRECT_URIS_ENV,
                _OAUTH_TRANSACTION_SECRET_ENV,
            )
        }
        present = {name for name, value in optional.items() if value}
        if present and len(present) != len(optional):
            raise ValueError("hosted_oauth_configuration_partial")
        hosted_oauth = None
        if present:
            hosted_oauth = HostedOAuthSettings(
                public_origin=optional[_PUBLIC_ORIGIN_ENV].rstrip("/"),
                google_client_id=optional[_GOOGLE_CLIENT_ID_ENV],
                google_client_secret_ref=optional[_GOOGLE_CLIENT_SECRET_REF_ENV],
                google_redirect_uris=frozenset(
                    uri.strip()
                    for uri in optional[_GOOGLE_REDIRECT_URIS_ENV].split(",")
                    if uri.strip()
                ),
                transaction_encryption_secret=optional[_OAUTH_TRANSACTION_SECRET_ENV],
            )
        return cls(
            database_url=database_url,
            session_secret=session_secret,
            oauth_state_secret=oauth_state_secret,
            hosted_oauth=hosted_oauth,
        )

    def __repr__(self) -> str:
        oauth_status = "unconfigured" if self.hosted_oauth is None else "configured"
        suffix = "" if self.hosted_oauth is None else f", hosted_oauth='{oauth_status}'"
        return (
            f"{type(self).__name__}("
            f"database_driver={_database_driver(self.database_url)!r}, "
            f"session_secret='***', oauth_state_secret='***'{suffix})"
        )


def create_platform_engine(database_url: str) -> Engine:
    parsed = _validated_database_url(database_url)
    try:
        return create_engine(parsed)
    except (ArgumentError, NoSuchModuleError):
        raise ValueError("database_engine_creation_failed") from None
