from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from corvus.database import M1_AUTHORITY_FAMILY_NAMES, DatabaseState, classify_database
from corvus.infrastructure.db import (
    M1_CURRENT_REVISION,
    M2_OAUTH_SESSIONS_REVISION,
    _alembic_config,
    _alembic_config_url,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from corvus.store import TraceStore

_SYNC_TABLES = frozenset(
    {
        "workspace_sync_heads",
        "workspace_changes",
        "outbox_events",
        "device_sync_acknowledgements",
        "platform_idempotency",
    }
)


def _base_database(path: Path) -> None:
    TraceStore(path).engine.dispose()


def test_sync_migration_generalizes_existing_identity_idempotency_without_data_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    command.upgrade(_alembic_config(database), M2_OAUTH_SESSIONS_REVISION)
    account_id = "00000000-0000-4000-8000-000000000201"
    principal_id = "00000000-0000-4000-8000-000000000202"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principals "
            "(id, kind, external_provider, external_subject, created_at, payload_json) "
            "VALUES (?, 'user', 'corvus-account', 'migration-user', ?, '{}')",
            (principal_id, "2026-07-16T12:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO accounts "
            "(id, principal_id, normalized_email, experience_kind, status, created_at, "
            "updated_at, version, payload_json) VALUES (?, ?, 'migration@example.com', NULL, "
            "'active', ?, ?, 1, '{}')",
            (
                account_id,
                principal_id,
                "2026-07-16T12:00:00+00:00",
                "2026-07-16T12:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO identity_idempotency "
            "(account_id, operation, idempotency_key, request_digest, result_json, created_at) "
            "VALUES (?, 'workspace.create', 'legacy-key', ?, ?, ?)",
            (
                account_id,
                "a" * 64,
                json.dumps({"id": "legacy-result"}),
                "2026-07-16T12:00:00+00:00",
            ),
        )

    command.upgrade(_alembic_config(database), "head")

    assert current_revision(database) == M1_CURRENT_REVISION
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        migrated = connection.execute(
            "SELECT account_id, principal_id, membership_version, scope_key, operation, "
            "idempotency_key, request_digest, result_json FROM platform_idempotency"
        ).fetchone()
    assert "identity_idempotency" not in tables
    assert migrated == (
        account_id,
        None,
        None,
        "account",
        "workspace.create",
        "legacy-key",
        "a" * 64,
        json.dumps({"id": "legacy-result"}),
    )


def test_fresh_sync_schema_classifies_current_and_requires_root_and_append_only_controls(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M1_CURRENT_REVISION

    status = classify_database(database)
    assert status.state is DatabaseState.CURRENT
    assert _SYNC_TABLES.issubset(status.tables)
    assert _SYNC_TABLES.issubset(M1_AUTHORITY_FAMILY_NAMES)
    with sqlite3.connect(database) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        family_names = {
            row[0]
            for row in connection.execute(
                "SELECT family_name FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = '00000000-0000-4000-8000-000000000011'"
            )
        }
        connection.execute("DROP TRIGGER outbox_events_no_update")
    assert revision == (M1_CURRENT_REVISION,)
    assert _SYNC_TABLES.issubset(family_names)
    assert classify_database(database).state is DatabaseState.PARTIAL


def test_sync_classifier_requires_account_principal_binding_index(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M1_CURRENT_REVISION
    with sqlite3.connect(database) as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "uq_accounts_id_principal" in indexes
        connection.execute("DROP INDEX uq_accounts_id_principal")

    assert classify_database(database).state is DatabaseState.PARTIAL


def test_account_scoped_platform_idempotency_retains_account_foreign_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M1_CURRENT_REVISION

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO platform_idempotency "
                "(account_id, scope_key, operation, idempotency_key, request_digest, "
                "result_json, created_at) VALUES (?, 'account', 'workspace.create', "
                "'missing-account', ?, '{}', ?)",
                (
                    "00000000-0000-4000-8000-000000000404",
                    "a" * 64,
                    "2026-07-16T12:00:00+00:00",
                ),
            )


def test_populated_sync_downgrade_refuses_before_mutating_schema(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M1_CURRENT_REVISION
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO workspace_sync_heads "
            "(workspace_id, workspace_version, current_sequence, retention_floor, chain_digest, "
            "version, updated_at) VALUES ('00000000-0000-4000-8000-000000000299', "
            "1, 0, 0, ?, 1, ?)",
            ("0" * 64, "2026-07-16T12:00:00+00:00"),
        )
        before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    with pytest.raises(RuntimeError, match="workspace_sync_history_present"):
        downgrade_database(database, M2_OAUTH_SESSIONS_REVISION)

    assert current_revision(database) == M1_CURRENT_REVISION
    with sqlite3.connect(database) as connection:
        after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert connection.execute("SELECT COUNT(*) FROM workspace_sync_heads").fetchone() == (1,)
    assert after == before


def test_empty_sync_downgrade_upgrade_cycle_restores_legacy_table_name(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M1_CURRENT_REVISION

    assert downgrade_database(database, M2_OAUTH_SESSIONS_REVISION) == M2_OAUTH_SESSIONS_REVISION
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert "identity_idempotency" in tables
    assert not _SYNC_TABLES.intersection(tables)
    assert "uq_accounts_id_principal" not in indexes
    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT


def test_sync_migration_renders_deterministic_offline_postgres_ddl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _alembic_config_url("postgresql+psycopg://corvus:test@127.0.0.1/corvus_platform_test")

    command.upgrade(config, "head", sql=True)

    rendered = capsys.readouterr().out.casefold()
    assert "create table workspace_changes" in rendered
    assert "create table outbox_events" in rendered
    assert (
        "foreign key(workspace_id, sequence, change_digest) references workspace_changes"
        in rendered
    )
    assert "create table platform_idempotency" in rendered
    assert "create unique index uq_accounts_id_principal on accounts (id, principal_id)" in rendered
    assert (
        "foreign key(workspace_id, principal_id, membership_version) "
        "references workspace_memberships" in rendered
    )
    assert "foreign key(account_id, principal_id) references accounts" in rendered
    assert "drop table identity_idempotency" in rendered
