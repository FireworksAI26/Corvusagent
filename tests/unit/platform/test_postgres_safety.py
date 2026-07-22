from __future__ import annotations

import pytest

from tests.postgres_safety import (
    POSTGRES_RESET_OPT_IN_ENV,
    POSTGRES_RESET_OPT_IN_VALUE,
    PostgresTestSafetyError,
    validate_disposable_postgres_url,
)

LOCAL_TEST_URL = "postgresql+psycopg://corvus:corvus@127.0.0.1:55432/corvus_platform_test"


def _opted_in_environment() -> dict[str, str]:
    return {POSTGRES_RESET_OPT_IN_ENV: POSTGRES_RESET_OPT_IN_VALUE}


def test_postgres_reset_requires_explicit_opt_in() -> None:
    with pytest.raises(PostgresTestSafetyError, match="postgres_reset_opt_in_required"):
        validate_disposable_postgres_url(LOCAL_TEST_URL, environ={})


def test_postgres_reset_rejects_database_without_test_suffix() -> None:
    url = "postgresql+psycopg://corvus:corvus@127.0.0.1:55432/corvus_production"

    with pytest.raises(PostgresTestSafetyError, match="postgres_database_not_disposable"):
        validate_disposable_postgres_url(url, environ=_opted_in_environment())


def test_postgres_reset_rejects_arbitrary_remote_host() -> None:
    url = "postgresql+psycopg://corvus:corvus@database.example.com/corvus_platform_test"

    with pytest.raises(PostgresTestSafetyError, match="postgres_test_host_not_allowed"):
        validate_disposable_postgres_url(url, environ=_opted_in_environment())


def test_postgres_reset_rejects_non_psycopg_url() -> None:
    url = "sqlite+pysqlite:///corvus_platform_test"

    with pytest.raises(PostgresTestSafetyError, match="postgres_test_url_required"):
        validate_disposable_postgres_url(url, environ=_opted_in_environment())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "postgres"])
def test_postgres_reset_accepts_local_and_compose_service_hosts(host: str) -> None:
    rendered_host = f"[{host}]" if ":" in host else host
    url = f"postgresql+psycopg://corvus:corvus@{rendered_host}/corvus_platform_test"

    validate_disposable_postgres_url(url, environ=_opted_in_environment())


@pytest.mark.parametrize(
    "query",
    [
        "host=database.example.com",
        "hostaddr=203.0.113.10",
        "port=5432",
        "dbname=production",
        "user=production-user",
        "password=production-password",
        "service=production-service",
        "servicefile=production-service-file",
        "database=production",
        "passfile=production-pass-file",
        "options=production-options",
        "target_session_attrs=read-write",
        "application_name=arbitrary-value",
        "ho%73t=encoded-host-override",
    ],
)
def test_postgres_reset_rejects_all_non_allowlisted_query_parameters(query: str) -> None:
    url = f"{LOCAL_TEST_URL}?{query}"

    with pytest.raises(PostgresTestSafetyError, match="postgres_test_query_not_allowed") as error:
        validate_disposable_postgres_url(url, environ=_opted_in_environment())

    assert query not in str(error.value)


def test_postgres_reset_accepts_bounded_connect_timeout() -> None:
    validate_disposable_postgres_url(
        f"{LOCAL_TEST_URL}?connect_timeout=2",
        environ=_opted_in_environment(),
    )


@pytest.mark.parametrize(
    "query",
    [
        "connect_timeout=0",
        "connect_timeout=-1",
        "connect_timeout=not-a-number",
        "connect_timeout=31",
        "connect_timeout=2&connect_timeout=3",
    ],
)
def test_postgres_reset_rejects_unbounded_or_ambiguous_connect_timeout(query: str) -> None:
    with pytest.raises(PostgresTestSafetyError, match="postgres_test_connect_timeout_invalid"):
        validate_disposable_postgres_url(
            f"{LOCAL_TEST_URL}?{query}",
            environ=_opted_in_environment(),
        )


def test_postgres_reset_safety_errors_do_not_expose_url_secrets() -> None:
    secret_url = (
        "postgresql+psycopg://visible-user:visible-password@database.example.com/"  # noqa: S105
        "corvus_platform_test?sslpassword=visible-query-secret"
    )

    with pytest.raises(PostgresTestSafetyError) as error:
        validate_disposable_postgres_url(secret_url, environ=_opted_in_environment())

    rendered = str(error.value)
    assert "visible-user" not in rendered
    assert "visible-password" not in rendered
    assert "visible-query-secret" not in rendered
