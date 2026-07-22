from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.domain.identity import Project
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision, upgrade_database
from corvus.infrastructure.repositories.projects import ProjectRepository, ProjectRepositoryError
from corvus.store import TraceStore


def _schema_rows(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()


def test_project_migration_is_repeatable_and_preserves_legacy_schema(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    legacy_store = TraceStore(database)
    legacy_store.engine.dispose()
    before = {
        name: sql
        for kind, name, sql in _schema_rows(database)
        if kind == "table" and name != "corvus_schema"
    }

    first_revision = upgrade_database(database)
    first_schema = _schema_rows(database)
    second_revision = upgrade_database(database)

    assert first_revision == M1_CURRENT_REVISION
    assert second_revision == M1_CURRENT_REVISION
    assert current_revision(database) == M1_CURRENT_REVISION
    assert _schema_rows(database) == first_schema
    after = {name: sql for kind, name, sql in first_schema if kind == "table" and name in before}
    assert after == before
    reopened = TraceStore(database)
    reopened.engine.dispose()


def test_project_repository_stages_rows_but_hides_unfinalized_authority(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    repository = ProjectRepository(database)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    project = Project(
        workspace_id=workspace_id,
        name="Corvus V2",
        root_locator="workspace://corvus-v2",
        privacy="private",
    )

    repository.add(project)

    assert repository.get_staged(workspace_id=workspace_id, project_id=project.id) == project
    assert repository.get(workspace_id=workspace_id, project_id=project.id) is None
    assert repository.get(workspace_id=other_workspace_id, project_id=project.id) is None
    assert repository.list_for_workspace(workspace_id) == []
    assert repository.list_for_workspace(other_workspace_id) == []


def test_project_repository_replay_requires_exact_record(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    repository = ProjectRepository(database)
    project = Project(
        workspace_id=uuid4(),
        name="Replay-safe Corvus",
        root_locator="workspace://replay-safe-corvus",
        privacy="private",
    )

    repository.add_idempotent(project)
    repository.add_idempotent(project)

    with pytest.raises(ProjectRepositoryError, match="project_replay_mismatch"):
        repository.add_idempotent(project.model_copy(update={"name": "Substituted"}))
    assert (
        repository.get_staged(workspace_id=project.workspace_id, project_id=project.id) == project
    )
    assert repository.list_for_workspace(project.workspace_id) == []


def test_project_repository_refuses_unmigrated_database(tmp_path: Path) -> None:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()

    with pytest.raises(ProjectRepositoryError, match="database_revision_mismatch"):
        ProjectRepository(database)
