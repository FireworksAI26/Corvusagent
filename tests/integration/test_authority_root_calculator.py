from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from corvus.database import M1_AUTHORITY_FAMILY_NAMES
from corvus.domain.identity import Project
from corvus.infrastructure.authority_root import (
    AuthorityRootCalculationError,
    AuthorityRootCalculator,
)
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.projects import ProjectRepository
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
_EXTERNAL_PROOFS = {
    "audit_anchor_recovery_checkpoints": "d" * 64,
    "audit_result_bindings": "e" * 64,
    "authority_registry_freshness_proofs": "f" * 64,
}


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _project_row(project: Project) -> dict[str, object]:
    return {
        "id": str(project.id),
        "workspace_id": str(project.workspace_id),
        "name": project.name,
        "root_locator": project.root_locator,
        "privacy": project.privacy,
        "status": project.status.value,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "version": project.version,
    }


def _workspace_authority_row(workspace_id: UUID, *, root: str) -> dict[str, object]:
    payload = {
        "id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "authority_state_root": root,
        "authority_generation": 3,
    }
    return {
        "id": payload["id"],
        "workspace_id": str(workspace_id),
        "deployment_profile_id": str(uuid4()),
        "deployment_instance_id": str(uuid4()),
        "epoch": 1,
        "authority_generation": 3,
        "authority_state_root": root,
        "authority_epoch_credential_id": str(uuid4()),
        "trust_anchor_id": str(uuid4()),
        "active_lease_id": str(uuid4()),
        "state": "active",
        "version": 2,
        "payload_json": json.dumps(payload, separators=(",", ":")),
    }


def _intent_row(workspace_id: UUID, *, root: str, state: str) -> dict[str, object]:
    payload = {
        "id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "proposed_state_root": root,
        "state": state,
        "next_generation": 3,
    }
    return {
        "id": payload["id"],
        "workspace_id": str(workspace_id),
        "epoch": 1,
        "deployment_instance_id": str(uuid4()),
        "prior_generation": 2,
        "next_generation": 3,
        "prior_state_root": "1" * 64,
        "mutation_digest": "2" * 64,
        "proposed_state_root": root,
        "state": state,
        "created_at": _NOW.isoformat(),
        "payload_json": json.dumps(payload, separators=(",", ":")),
    }


def test_calculator_is_deterministic_exhaustive_and_workspace_bound(tmp_path: Path) -> None:
    calculator = AuthorityRootCalculator(_database(tmp_path))
    workspace_id = uuid4()

    first = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=1,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    replay = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=1,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    other = calculator.calculate(
        workspace_id=uuid4(),
        authority_generation=1,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    substituted_proof = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=1,
        external_proof_digests={
            **_EXTERNAL_PROOFS,
            "authority_registry_freshness_proofs": "e" * 64,
        },
    )

    assert first == replay
    assert first.root_digest != other.root_digest
    assert first.root_digest != substituted_proof.root_digest
    assert first.observed_leaf_digests != substituted_proof.observed_leaf_digests
    assert (
        first.observed_leaf_digests["authority_registry_freshness_proofs"]
        != substituted_proof.observed_leaf_digests["authority_registry_freshness_proofs"]
    )
    assert first.observed_leaf_digests == other.observed_leaf_digests
    assert set(first.observed_leaf_digests) == M1_AUTHORITY_FAMILY_NAMES
    assert [item.ordinal for item in first.commitments] == list(
        range(1, len(M1_AUTHORITY_FAMILY_NAMES) + 1)
    )
    freshness = next(
        item
        for item in first.commitments
        if item.family_name == "authority_registry_freshness_proofs"
    )
    assert freshness.external_proof_digest is not None


def test_calculator_requires_every_manifest_external_proof(tmp_path: Path) -> None:
    calculator = AuthorityRootCalculator(_database(tmp_path))

    with pytest.raises(AuthorityRootCalculationError, match="authority_external_proof_missing"):
        calculator.calculate(workspace_id=uuid4(), authority_generation=1)


def test_live_root_verification_rejects_uncommitted_family_changes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    calculator = AuthorityRootCalculator(database)
    workspace_id = uuid4()
    calculation = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=1,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    calculator.registry.append_leaf_commitments(
        workspace_id=workspace_id,
        manifest=calculation.manifest,
        commitments=list(calculation.commitments),
        observed_leaf_digests=calculation.observed_leaf_digests,
    )

    assert (
        calculator.verify_live_root(
            workspace_id=workspace_id,
            authority_generation=1,
            expected_root=calculation.root_digest,
        )
        == calculation
    )

    ProjectRepository(database).add(
        Project(
            workspace_id=workspace_id,
            name="Uncommitted Project",
            root_locator="workspace://uncommitted-project",
            privacy="private",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    with pytest.raises(
        AuthorityRootCalculationError,
        match="authority_live_family_rollback_detected",
    ):
        calculator.verify_live_root(
            workspace_id=workspace_id,
            authority_generation=1,
            expected_root=calculation.root_digest,
        )


def test_project_projection_is_workspace_isolated_and_matches_persistence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    calculator = AuthorityRootCalculator(database)
    repository = ProjectRepository(database)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    project = Project(
        workspace_id=workspace_id,
        name="Projected Corvus",
        root_locator="workspace://projected-corvus",
        privacy="private",
        created_at=_NOW,
        updated_at=_NOW,
    )
    before = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=2,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    other_before = calculator.calculate(
        workspace_id=other_workspace_id,
        authority_generation=2,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    prospective = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=2,
        prospective_family_rows={"projects": [_project_row(project)]},
        external_proof_digests=_EXTERNAL_PROOFS,
    )

    repository.add(project)
    persisted = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=2,
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    other_after = calculator.calculate(
        workspace_id=other_workspace_id,
        authority_generation=2,
        external_proof_digests=_EXTERNAL_PROOFS,
    )

    assert prospective == persisted
    assert prospective.root_digest != before.root_digest
    assert prospective.observed_leaf_digests["projects"] != before.observed_leaf_digests["projects"]
    assert other_after == other_before
    repository.close()


def test_self_referential_root_and_transition_fields_are_normalized(tmp_path: Path) -> None:
    calculator = AuthorityRootCalculator(_database(tmp_path))
    workspace_id = uuid4()
    authority = _workspace_authority_row(workspace_id, root="a" * 64)
    intent = _intent_row(workspace_id, root="b" * 64, state="prepared")
    first = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=3,
        prospective_family_rows={
            "workspace_authorities": [authority],
            "authority_commit_intents": [intent],
        },
        external_proof_digests=_EXTERNAL_PROOFS,
    )
    changed_authority = dict(authority)
    changed_authority["authority_state_root"] = "c" * 64
    changed_authority["payload_json"] = str(authority["payload_json"]).replace("a" * 64, "c" * 64)
    changed_intent = dict(intent)
    changed_intent["proposed_state_root"] = "d" * 64
    changed_intent["state"] = "anchor_finalized"
    changed_intent["payload_json"] = (
        str(intent["payload_json"])
        .replace("b" * 64, "d" * 64)
        .replace("prepared", "anchor_finalized")
    )
    replay = calculator.calculate(
        workspace_id=workspace_id,
        authority_generation=3,
        prospective_family_rows={
            "workspace_authorities": [changed_authority],
            "authority_commit_intents": [changed_intent],
        },
        external_proof_digests=_EXTERNAL_PROOFS,
    )

    assert first == replay


def test_calculator_rejects_partial_or_unknown_projections(tmp_path: Path) -> None:
    calculator = AuthorityRootCalculator(_database(tmp_path))
    workspace_id = uuid4()

    with pytest.raises(AuthorityRootCalculationError, match="projection_columns_mismatch"):
        calculator.calculate(
            workspace_id=workspace_id,
            authority_generation=1,
            prospective_family_rows={"projects": [{"id": str(uuid4())}]},
            external_proof_digests=_EXTERNAL_PROOFS,
        )
    with pytest.raises(AuthorityRootCalculationError, match="projection_family_unknown"):
        calculator.calculate(
            workspace_id=workspace_id,
            authority_generation=1,
            prospective_family_rows={"not_a_manifest_family": []},
            external_proof_digests=_EXTERNAL_PROOFS,
        )
    foreign_project = Project(
        workspace_id=uuid4(),
        name="Foreign",
        root_locator="workspace://foreign",
        privacy="private",
        created_at=_NOW,
        updated_at=_NOW,
    )
    with pytest.raises(AuthorityRootCalculationError, match="projection_workspace_mismatch"):
        calculator.calculate(
            workspace_id=workspace_id,
            authority_generation=1,
            prospective_family_rows={"projects": [_project_row(foreign_project)]},
            external_proof_digests=_EXTERNAL_PROOFS,
        )
