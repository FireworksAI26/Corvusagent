from __future__ import annotations

import sqlite3
from pathlib import Path

from corvus.database import (
    M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES,
    M1_NON_CIRCULAR_AUTHORITY_FAMILY_NAMES,
    M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES,
    M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES,
    DatabaseState,
    classify_database,
)
from corvus.infrastructure.db import (
    M1_CURRENT_REVISION,
    M1_IDENTITY_SCOPE_REVISION,
    M1_ROOT_MANIFEST_REVISION,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from corvus.infrastructure.repositories.registry import M1_AUTHORITY_FAMILY_NAMES
from corvus.store import TraceStore

_V4_MANIFEST_ID = "00000000-0000-4000-8000-000000000007"
_V5_MANIFEST_ID = "00000000-0000-4000-8000-000000000008"
_V6_MANIFEST_ID = "00000000-0000-4000-8000-000000000009"
_V7_MANIFEST_ID = "00000000-0000-4000-8000-000000000010"
_V8_MANIFEST_ID = "00000000-0000-4000-8000-000000000011"
_V9_MANIFEST_ID = "00000000-0000-4000-8000-000000000012"
_DERIVED_POST_COMMIT_FAMILIES = {
    "audit_anchor_recovery_checkpoints",
    "audit_result_bindings",
}


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _families(database: Path, manifest_id: str) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT family_name FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = ? ORDER BY ordinal",
                (manifest_id,),
            ).fetchall()
        ]


def test_non_circular_manifest_preserves_history_and_excludes_derived_rows(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    v4_families = _families(database, _V4_MANIFEST_ID)
    v5_families = _families(database, _V5_MANIFEST_ID)
    v6_families = _families(database, _V6_MANIFEST_ID)
    v7_families = _families(database, _V7_MANIFEST_ID)
    v8_families = _families(database, _V8_MANIFEST_ID)
    v9_families = _families(database, _V9_MANIFEST_ID)

    assert _DERIVED_POST_COMMIT_FAMILIES <= set(v4_families)
    assert set(v5_families) == M1_NON_CIRCULAR_AUTHORITY_FAMILY_NAMES
    assert _DERIVED_POST_COMMIT_FAMILIES.isdisjoint(v5_families)
    assert v5_families == sorted(v5_families)
    assert set(v6_families) == M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES
    assert _DERIVED_POST_COMMIT_FAMILIES <= set(v6_families)
    assert v6_families == sorted(v6_families)
    assert set(v7_families) == M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES
    assert v7_families == sorted(v7_families)
    assert set(v8_families) == M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES
    assert v8_families == sorted(v8_families)
    assert set(v9_families) == M1_AUTHORITY_FAMILY_NAMES
    assert v9_families == sorted(v9_families)
    with sqlite3.connect(database) as connection:
        manifests = connection.execute(
            "SELECT schema_version, canonicalization_version FROM "
            "authority_state_root_manifests ORDER BY schema_version"
        ).fetchall()
        audit_external = dict(
            connection.execute(
                "SELECT family_name, external_proof_kind FROM "
                "authority_state_root_leaf_families WHERE manifest_version_id = ? "
                "AND coverage_kind = 'external_proof'",
                (_V6_MANIFEST_ID,),
            ).fetchall()
        )
    assert manifests[-6:] == [(4, 1), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1)]
    assert audit_external == {
        "audit_anchor_recovery_checkpoints": "sealed_audit_checkpoint_history",
        "audit_result_bindings": "sealed_audit_result_binding_history",
        "authority_registry_freshness_proofs": "registry_freshness_proof",
    }
    assert classify_database(database).state is DatabaseState.CURRENT
    assert current_revision(database) == M1_CURRENT_REVISION


def test_non_circular_manifest_downgrades_and_reapplies_without_history_loss(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    v4_before = _families(database, _V4_MANIFEST_ID)

    assert downgrade_database(database, M1_ROOT_MANIFEST_REVISION) == M1_ROOT_MANIFEST_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT
    assert _families(database, _V5_MANIFEST_ID)
    assert _families(database, _V6_MANIFEST_ID) == []
    assert _families(database, _V7_MANIFEST_ID) == []
    assert _families(database, _V8_MANIFEST_ID) == []
    assert _families(database, _V9_MANIFEST_ID) == []

    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert _families(database, _V6_MANIFEST_ID)

    assert downgrade_database(database, M1_IDENTITY_SCOPE_REVISION) == M1_IDENTITY_SCOPE_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT
    assert _families(database, _V4_MANIFEST_ID) == v4_before
    assert _families(database, _V5_MANIFEST_ID) == []
    assert _families(database, _V6_MANIFEST_ID) == []
    assert _families(database, _V7_MANIFEST_ID) == []
    assert _families(database, _V8_MANIFEST_ID) == []
    assert _families(database, _V9_MANIFEST_ID) == []

    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT
    assert _families(database, _V4_MANIFEST_ID) == v4_before
    assert set(_families(database, _V5_MANIFEST_ID)) == M1_NON_CIRCULAR_AUTHORITY_FAMILY_NAMES
    assert set(_families(database, _V6_MANIFEST_ID)) == M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES
    assert set(_families(database, _V7_MANIFEST_ID)) == (
        M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES
    )
    assert set(_families(database, _V8_MANIFEST_ID)) == (
        M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES
    )
    assert set(_families(database, _V9_MANIFEST_ID)) == M1_AUTHORITY_FAMILY_NAMES
