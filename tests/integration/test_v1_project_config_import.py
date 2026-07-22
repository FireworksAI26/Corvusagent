from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.domain.identity import Project
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.importers.project_config import (
    ProjectConfigImportError,
    import_project_config,
)
from corvus.infrastructure.repositories.projects import ProjectRepository
from corvus.quarantine import capture_v1_quarantine
from corvus.store import TraceStore

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "v1" / "legacy"


def _capture(tmp_path: Path) -> Path:
    source = tmp_path / "legacy"
    shutil.copytree(_FIXTURE, source)
    receipt = capture_v1_quarantine(
        database=source / "corvus.db",
        config_root=source / "config",
        project_root=source / "project",
        artifact_root=source / "artifacts",
        bundle_root=source / "bundles",
        backup_root=source / "backups",
        quarantine_root=tmp_path / "quarantine",
    )
    return receipt.path


def _repository(tmp_path: Path) -> ProjectRepository:
    database = tmp_path / "destination.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return ProjectRepository(database)


def test_verified_project_config_import_is_idempotent_and_non_authoritative(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)
    repository = _repository(tmp_path)
    workspace_id = uuid4()
    project = Project(
        workspace_id=workspace_id,
        name="Imported Corvus",
        root_locator="workspace://imported-corvus",
        privacy="private",
    )

    first = import_project_config(
        capture=capture,
        repository=repository,
        workspace_id=workspace_id,
        project=project,
    )
    second = import_project_config(
        capture=capture,
        repository=repository,
        workspace_id=workspace_id,
        project=project,
    )

    assert first == second
    assert repository.list_for_workspace(workspace_id) == []
    assert repository.get_staged(workspace_id=workspace_id, project_id=project.id) == project
    assert first.policy_hints.autonomy_level == 2
    assert first.policy_hints.max_runtime_seconds == 60
    assert first.provider_hints[0].name == "fixture"
    assert first.credentials_imported is False
    assert first.authority_imported is False
    encoded = first.model_dump_json()
    assert "api_key" not in encoded
    assert "[REDACTED]" not in encoded
    assert "fixture-sensitive-value" not in encoded


def test_tampered_capture_fails_before_destination_mutation(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    repository = _repository(tmp_path)
    workspace_id = uuid4()
    project = Project(
        workspace_id=workspace_id,
        name="Rejected Import",
        root_locator="workspace://rejected-import",
        privacy="private",
    )
    records = json.loads((capture / "records.json").read_text(encoding="utf-8"))
    records["project_policy"]["policy.yaml"]["autonomy"] = 99
    (capture / "records.json").write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(ProjectConfigImportError, match="quarantine_capture_verification_failed"):
        import_project_config(
            capture=capture,
            repository=repository,
            workspace_id=workspace_id,
            project=project,
        )

    assert repository.list_for_workspace(workspace_id) == []
