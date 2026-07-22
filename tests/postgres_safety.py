from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

POSTGRES_RESET_OPT_IN_ENV: Final = "CORVUS_TEST_POSTGRES_RESET_ALLOWED"
POSTGRES_RESET_OPT_IN_VALUE: Final = "reset-disposable-database"
_POSTGRES_DRIVER: Final = "postgresql+psycopg"
_DISPOSABLE_DATABASE_SUFFIX: Final = "_test"
_ALLOWED_TEST_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost", "postgres"})
_ALLOWED_QUERY_KEYS: Final = frozenset({"connect_timeout"})
_MAXIMUM_CONNECT_TIMEOUT_SECONDS: Final = 30


class PostgresTestSafetyError(RuntimeError):
    pass


def validate_disposable_postgres_url(database_url: str, *, environ: Mapping[str, str]) -> None:
    if environ.get(POSTGRES_RESET_OPT_IN_ENV) != POSTGRES_RESET_OPT_IN_VALUE:
        raise PostgresTestSafetyError("postgres_reset_opt_in_required")
    try:
        parsed = make_url(database_url)
    except ArgumentError:
        raise PostgresTestSafetyError("postgres_test_url_required") from None
    if parsed.drivername != _POSTGRES_DRIVER:
        raise PostgresTestSafetyError("postgres_test_url_required")
    if not set(parsed.query).issubset(_ALLOWED_QUERY_KEYS):
        raise PostgresTestSafetyError("postgres_test_query_not_allowed")
    connect_timeout = parsed.query.get("connect_timeout")
    if connect_timeout is not None:
        if not isinstance(connect_timeout, str):
            raise PostgresTestSafetyError("postgres_test_connect_timeout_invalid")
        try:
            timeout_seconds = int(connect_timeout)
        except (TypeError, ValueError):
            raise PostgresTestSafetyError("postgres_test_connect_timeout_invalid") from None
        if not 1 <= timeout_seconds <= _MAXIMUM_CONNECT_TIMEOUT_SECONDS:
            raise PostgresTestSafetyError("postgres_test_connect_timeout_invalid")
    database_name = parsed.database or ""
    if not database_name.casefold().endswith(_DISPOSABLE_DATABASE_SUFFIX):
        raise PostgresTestSafetyError("postgres_database_not_disposable")
    host = (parsed.host or "").casefold()
    if host not in _ALLOWED_TEST_HOSTS:
        raise PostgresTestSafetyError("postgres_test_host_not_allowed")
