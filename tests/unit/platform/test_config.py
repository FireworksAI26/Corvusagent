from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

import corvus.infrastructure.db as database_module
from corvus.infrastructure.db import (
    M1_CURRENT_REVISION,
    M1_PROJECT_REVISION,
    _alembic_config_url,
    current_revision_url,
    downgrade_database_url,
    upgrade_database_url,
)
from corvus.platform import PlatformSettings, create_platform_engine

POSTGRES_URL = "postgresql+psycopg://corvus:database-password@localhost/corvus"
POSTGRES_URL_WITH_QUERY_SECRETS = (
    "postgresql+psycopg://database-user:database-password@localhost/corvus"
    "?sslpassword=query-ssl-secret&access_token=query-access-secret"
    "&application_name=query-application-value"
)
SESSION_SECRET = "session-secret-value-that-is-at-least-32-characters"  # noqa: S105
OAUTH_STATE_SECRET = "oauth-state-secret-value-that-is-at-least-32-characters"  # noqa: S105


def _set_hosted_environment(monkeypatch: pytest.MonkeyPatch, *, database_url: str) -> None:
    monkeypatch.setenv("CORVUS_DATABASE_URL", database_url)
    monkeypatch.setenv("CORVUS_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("CORVUS_OAUTH_STATE_SECRET", OAUTH_STATE_SECRET)


def test_platform_settings_require_distinct_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORVUS_DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("CORVUS_SESSION_SECRET", "x" * 48)
    monkeypatch.setenv("CORVUS_OAUTH_STATE_SECRET", "x" * 48)

    with pytest.raises(ValueError, match="secrets_must_be_distinct"):
        PlatformSettings.from_env()


def test_platform_settings_constructor_cannot_bypass_validation() -> None:
    reused_secret = "x" * 48

    with pytest.raises(ValueError, match="secrets_must_be_distinct"):
        PlatformSettings(
            database_url=POSTGRES_URL,
            session_secret=reused_secret,
            oauth_state_secret=reused_secret,
        )


def test_hosted_oauth_rejects_password_only_public_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_hosted_environment(monkeypatch, database_url=POSTGRES_URL)
    monkeypatch.setenv("CORVUS_PUBLIC_ORIGIN", "https://:password@example.com")
    monkeypatch.setenv("CORVUS_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("CORVUS_GOOGLE_CLIENT_SECRET_REF", "env://CORVUS_GOOGLE_CLIENT_SECRET")
    monkeypatch.setenv(
        "CORVUS_GOOGLE_REDIRECT_URIS",
        "https://example.com/api/v2/auth/google/callback",
    )
    monkeypatch.setenv("CORVUS_OAUTH_TRANSACTION_SECRET", "t" * 48)

    with pytest.raises(ValueError, match="public_origin_invalid"):
        PlatformSettings.from_env()


@pytest.mark.parametrize("secret_name", ["session_secret", "oauth_state_secret"])
def test_platform_settings_constructor_rejects_blank_secrets(secret_name: str) -> None:
    values = {
        "database_url": POSTGRES_URL,
        "session_secret": SESSION_SECRET,
        "oauth_state_secret": OAUTH_STATE_SECRET,
    }
    values[secret_name] = " " * 48

    with pytest.raises(ValueError, match=f"secret_blank:CORVUS_{secret_name.upper()}"):
        PlatformSettings(**values)


@pytest.mark.parametrize(
    "missing_name",
    ["CORVUS_DATABASE_URL", "CORVUS_SESSION_SECRET", "CORVUS_OAUTH_STATE_SECRET"],
)
def test_platform_settings_fail_closed_when_required_value_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    _set_hosted_environment(monkeypatch, database_url=POSTGRES_URL)
    monkeypatch.delenv(missing_name)

    with pytest.raises(ValueError, match=f"missing_required_setting:{missing_name}"):
        PlatformSettings.from_env()


@pytest.mark.parametrize("secret_name", ["CORVUS_SESSION_SECRET", "CORVUS_OAUTH_STATE_SECRET"])
def test_platform_settings_reject_short_secrets(
    monkeypatch: pytest.MonkeyPatch,
    secret_name: str,
) -> None:
    _set_hosted_environment(monkeypatch, database_url=POSTGRES_URL)
    short_secret = "secret-value"  # noqa: S105
    monkeypatch.setenv(secret_name, short_secret)

    with pytest.raises(ValueError, match=f"secret_too_short:{secret_name}") as error:
        PlatformSettings.from_env()

    assert short_secret not in str(error.value)


@pytest.mark.parametrize(
    "database_url,dialect_name,driver_name",
    [
        ("sqlite+pysqlite:///:memory:", "sqlite", "pysqlite"),
        (POSTGRES_URL, "postgresql", "psycopg"),
    ],
)
def test_create_platform_engine_supports_approved_database_urls(
    database_url: str,
    dialect_name: str,
    driver_name: str,
) -> None:
    engine = create_platform_engine(database_url)
    try:
        assert isinstance(engine, Engine)
        assert engine.dialect.name == dialect_name
        assert engine.dialect.driver == driver_name
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://corvus:visible-password@localhost/corvus?access_token=visible-token",
        "mysql+pymysql://corvus:visible-password@localhost/corvus?sslpassword=visible-ssl",
        "not-a-database-url-visible-password?application_name=visible-query",
    ],
)
def test_database_url_errors_are_redacted(database_url: str) -> None:
    with pytest.raises(ValueError, match="unsupported_database_url") as error:
        create_platform_engine(database_url)

    assert "visible-password" not in str(error.value)
    assert "visible-token" not in str(error.value)
    assert "visible-ssl" not in str(error.value)
    assert "visible-query" not in str(error.value)


def test_platform_settings_are_immutable_and_redact_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_hosted_environment(monkeypatch, database_url=POSTGRES_URL_WITH_QUERY_SECRETS)

    settings = PlatformSettings.from_env()
    rendered = repr(settings)

    for secret_value in (
        "database-user",
        "database-password",
        "query-ssl-secret",
        "query-access-secret",
        "query-application-value",
        SESSION_SECRET,
        OAUTH_STATE_SECRET,
    ):
        assert secret_value not in rendered
    assert rendered == (
        "PlatformSettings(database_driver='postgresql+psycopg', "
        "session_secret='***', oauth_state_secret='***')"
    )
    with pytest.raises(FrozenInstanceError):
        settings.database_url = "sqlite:///:memory:"


def test_url_based_alembic_upgrade_supports_fresh_sqlite_database(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'platform.db'}"

    assert current_revision_url(database_url) is None
    assert upgrade_database_url(database_url) == M1_CURRENT_REVISION
    assert current_revision_url(database_url) == M1_CURRENT_REVISION


def test_url_based_alembic_downgrade_cleans_triggers_and_reupgrades(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'platform-cycle.db'}"
    upgrade_database_url(database_url)
    engine = create_platform_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'")
                ).scalar_one()
                > 0
            )

        assert downgrade_database_url(database_url, M1_PROJECT_REVISION) == M1_PROJECT_REVISION
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'")
                ).scalar_one()
                == 0
            )

        assert upgrade_database_url(database_url) == M1_CURRENT_REVISION
    finally:
        engine.dispose()


def test_postgres_alembic_upgrade_renders_offline_without_connecting() -> None:
    output = io.StringIO()
    config = _alembic_config_url(POSTGRES_URL_WITH_QUERY_SECRETS)
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    rendered = output.getvalue()
    assert "CREATE TABLE projects" in rendered
    assert "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)" in rendered
    assert "CREATE FUNCTION authorization_decision_snapshots_no_update_fn" in rendered
    assert "m1_009_audit_external_proofs" in rendered
    assert "CREATE TABLE accounts" in rendered
    assert "CREATE FUNCTION session_records_no_delete_fn" in rendered
    assert "device_version INTEGER NOT NULL" in rendered
    assert "fk_session_records_device_account" in rendered
    assert "uq_device_registrations_identity_account" in rendered
    assert "m2_001_identity_continuity" in rendered
    for secret_value in (
        "database-user",
        "database-password",
        "query-ssl-secret",
        "query-access-secret",
        "query-application-value",
    ):
        assert secret_value not in rendered


def test_m2_offline_downgrade_fails_closed_without_history_inspection() -> None:
    config = _alembic_config_url(POSTGRES_URL_WITH_QUERY_SECRETS)

    with pytest.raises(
        RuntimeError,
        match="identity_continuity_downgrade_requires_online_history_check",
    ):
        command.downgrade(
            config,
            "m2_001_identity_continuity:m1_009_audit_external_proofs",
            sql=True,
        )


def test_current_revision_url_disposes_engine_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEngine:
        disposed = False

        def connect(self) -> None:
            raise RuntimeError("connection_failed")

        def dispose(self) -> None:
            self.disposed = True

    engine = FailingEngine()
    monkeypatch.setattr(database_module, "create_platform_engine", lambda _: engine)

    with pytest.raises(RuntimeError, match="connection_failed"):
        current_revision_url(POSTGRES_URL)

    assert engine.disposed is True


def test_alembic_online_engine_disposes_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingConnectable:
        disposed = False

        def connect(self) -> None:
            raise RuntimeError("migration_connection_failed")

        def dispose(self) -> None:
            self.disposed = True

    connectable = FailingConnectable()
    monkeypatch.setattr(sqlalchemy, "engine_from_config", lambda *args, **kwargs: connectable)
    config = _alembic_config_url("sqlite+pysqlite:///:memory:")

    with pytest.raises(RuntimeError, match="migration_connection_failed"):
        command.upgrade(config, "head")

    assert connectable.disposed is True
