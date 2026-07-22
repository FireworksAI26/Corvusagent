from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corvus.domain.identity import Project
from corvus.infrastructure.repositories.projects import ProjectRepository
from corvus.quarantine import verify_v1_quarantine


class ProjectConfigImportError(RuntimeError):
    pass


class ImportedPolicyHints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    autonomy_level: int | None = Field(default=None, ge=0)
    max_runtime_seconds: int | None = Field(default=None, ge=1)


class ProviderImportHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    keyring_service: str | None = Field(default=None, max_length=200)


class ProjectConfigImportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: Project
    policy_hints: ImportedPolicyHints
    provider_hints: tuple[ProviderImportHint, ...]
    authority_imported: bool = False
    credentials_imported: bool = False


def _load_capture(capture: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not verify_v1_quarantine(capture):
        raise ProjectConfigImportError("quarantine_capture_verification_failed")
    try:
        manifest = json.loads((capture / "manifest.json").read_text(encoding="utf-8"))
        records = json.loads((capture / "records.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectConfigImportError("quarantine_capture_read_failed") from exc
    schema_version = records.get("schema_version")
    if schema_version == 2:
        if not isinstance(records.get("config"), dict):
            raise ProjectConfigImportError("quarantine_capture_schema_unsupported")
    elif schema_version == 3:
        if not isinstance(records.get("user_config"), dict) or not isinstance(
            records.get("project_policy"), dict
        ):
            raise ProjectConfigImportError("quarantine_capture_schema_unsupported")
    else:
        raise ProjectConfigImportError("quarantine_capture_schema_unsupported")
    return manifest, records


def _policy_hints(config: dict[str, Any]) -> ImportedPolicyHints:
    policy = config.get("policy.yaml", {})
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ProjectConfigImportError("legacy_policy_shape_invalid")
    autonomy = policy.get("autonomy")
    budgets = policy.get("budgets", {})
    if autonomy is not None and (not isinstance(autonomy, int) or isinstance(autonomy, bool)):
        raise ProjectConfigImportError("legacy_autonomy_level_invalid")
    if budgets is None:
        budgets = {}
    if not isinstance(budgets, dict):
        raise ProjectConfigImportError("legacy_budget_shape_invalid")
    runtime = budgets.get("max_runtime_seconds")
    if runtime is not None and (not isinstance(runtime, int) or isinstance(runtime, bool)):
        raise ProjectConfigImportError("legacy_runtime_budget_invalid")
    return ImportedPolicyHints(
        autonomy_level=autonomy,
        max_runtime_seconds=runtime,
    )


def _provider_hints(config: dict[str, Any]) -> tuple[ProviderImportHint, ...]:
    provider_config = config.get("providers.yaml", {})
    if provider_config is None:
        return ()
    if not isinstance(provider_config, dict):
        raise ProjectConfigImportError("legacy_provider_shape_invalid")
    providers = provider_config.get("providers", [])
    if not isinstance(providers, list):
        raise ProjectConfigImportError("legacy_provider_list_invalid")
    hints: list[ProviderImportHint] = []
    for provider in providers:
        if not isinstance(provider, dict):
            raise ProjectConfigImportError("legacy_provider_entry_invalid")
        try:
            hints.append(
                ProviderImportHint(
                    name=provider["name"],
                    kind=provider["kind"],
                    model=provider["model"],
                    base_url=provider.get("base_url"),
                    keyring_service=provider.get("keyring_service"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectConfigImportError("legacy_provider_entry_invalid") from exc
    return tuple(hints)


def import_project_config(
    *,
    capture: Path,
    repository: ProjectRepository,
    workspace_id: UUID,
    project: Project,
) -> ProjectConfigImportReceipt:
    if project.workspace_id != workspace_id:
        raise ProjectConfigImportError("project_workspace_mismatch")
    manifest, records = _load_capture(capture)
    if records["schema_version"] == 2:
        user_config = records["config"]
        project_policy: dict[str, Any] = {}
    else:
        user_config = records["user_config"]
        project_policy = records["project_policy"]
    policy_hints = _policy_hints(project_policy or user_config)
    provider_hints = _provider_hints(user_config)

    existing = repository.get_staged(workspace_id=workspace_id, project_id=project.id)
    if existing is None:
        repository.add(project)
    elif existing != project:
        raise ProjectConfigImportError("project_import_identity_conflict")

    return ProjectConfigImportReceipt(
        capture_id=manifest["capture_id"],
        source_snapshot_sha256=manifest["source_snapshot_sha256"],
        project=project,
        policy_hints=policy_hints,
        provider_hints=provider_hints,
    )
