from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from corvus.database import M1_AUTHORITY_FAMILY_NAMES, DatabaseState, classify_database
from corvus.domain.identity import Principal, PrincipalKind, Workspace, WorkspaceMembership
from corvus.infrastructure.db import (
    M1_CURRENT_REVISION,
    M2_CONVERSATIONS_REVISION,
    M2_WORKSPACE_SYNC_REVISION,
    _alembic_config_url,
    downgrade_database,
    upgrade_database,
)
from corvus.infrastructure.repositories.identity_scope import IdentityScopeRepository
from corvus.store import TraceStore

_TABLES = frozenset(
    {
        "threads",
        "thread_versions",
        "attachments",
        "messages",
        "message_attachments",
        "agent_runs",
        "agent_run_events",
        "run_artifacts",
        "run_artifact_lineage",
    }
)


def _base_database(path: Path) -> None:
    TraceStore(path).engine.dispose()


def test_fresh_conversation_schema_is_current_root_covered_and_immutable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M2_CONVERSATIONS_REVISION == M1_CURRENT_REVISION
    status = classify_database(database)
    assert status.state is DatabaseState.CURRENT
    assert _TABLES.issubset(status.tables)
    assert _TABLES.issubset(M1_AUTHORITY_FAMILY_NAMES)

    with sqlite3.connect(database) as connection:
        families = {
            row[0]
            for row in connection.execute(
                "SELECT family_name FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = '00000000-0000-4000-8000-000000000012'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
    assert _TABLES.issubset(families)
    assert {
        f"{table}_{suffix}" for table in _TABLES for suffix in ("no_update", "no_delete")
    }.issubset(triggers)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TRIGGER agent_run_events_no_update",
        "DROP INDEX ix_agent_run_events_page",
    ],
)
def test_conversation_classifier_rejects_missing_trigger_or_tenant_index(
    tmp_path: Path,
    statement: str,
) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    assert classify_database(database).state is DatabaseState.PARTIAL


def test_conversation_classifier_rejects_recreated_wrong_tenant_index(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX uq_agent_run_provider_event")
        connection.execute(
            "CREATE UNIQUE INDEX uq_agent_run_provider_event ON agent_run_events(provider_event_id)"
        )
    assert classify_database(database).state is DatabaseState.PARTIAL


def test_populated_conversation_downgrade_refuses_before_mutation(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    upgrade_database(database)
    workspace = Workspace(name="Downgrade workspace")
    principal = Principal(
        kind=PrincipalKind.USER,
        external_provider="test",
        external_subject="downgrade",
        display_name="Downgrade user",
    )
    identities = IdentityScopeRepository(database)
    identities.append_workspace(workspace)
    identities.append_principal(principal)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=principal.id,
            role="owner",
        )
    )
    identities.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO threads "
            "(workspace_id, id, workspace_version, creator_principal_id, "
            "creator_membership_version, created_at) VALUES (?, ?, 1, ?, 1, ?)",
            (
                str(workspace.id),
                "20000000-0000-4000-8000-000000000001",
                str(principal.id),
                "2026-07-17T00:00:00+00:00",
            ),
        )
        before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    with pytest.raises(RuntimeError, match="conversation_history_present"):
        downgrade_database(database, M2_WORKSPACE_SYNC_REVISION)
    with sqlite3.connect(database) as connection:
        after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone() == (1,)
    assert after == before


def test_empty_conversation_downgrade_upgrade_cycle_is_deterministic(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    _base_database(database)
    assert upgrade_database(database) == M2_CONVERSATIONS_REVISION
    assert downgrade_database(database, M2_WORKSPACE_SYNC_REVISION) == M2_WORKSPACE_SYNC_REVISION
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not _TABLES.intersection(tables)
    assert upgrade_database(database) == M2_CONVERSATIONS_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT


def test_conversation_migration_renders_portable_offline_postgres_ddl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _alembic_config_url("postgresql+psycopg://corvus:test@127.0.0.1/corvus_test")
    command.upgrade(config, "head", sql=True)
    rendered = capsys.readouterr().out.casefold()
    assert "create table agent_run_events" in rendered
    assert "create table run_artifact_lineage" in rendered
    assert "foreign key(workspace_id, principal_id, membership_version)" in rendered
    assert "references workspace_memberships" in rendered
    assert "create unique index uq_agent_run_provider_event" in rendered
    assert "where provider_event_id is not null" in rendered
    assert "create trigger agent_run_events_no_update" in rendered
