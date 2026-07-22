"""Restore rollback-sensitive audit families through external anchor proofs.

Revision ID: m1_009_audit_external_proofs
Revises: m1_008_non_circular_root_manifest
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import (
    M1_008_FAMILY_NAMES,
    family_proof_metadata,
)
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m1_009_audit_external_proofs"
down_revision: str | None = "m1_008_non_circular_root_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000009"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000008"
_AUDIT_EXTERNAL_FAMILIES = {
    "audit_anchor_recovery_checkpoints": "sealed_audit_checkpoint_history",
    "audit_result_bindings": "sealed_audit_result_binding_history",
}


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def upgrade() -> None:
    bind = op.get_bind()
    if op.get_context().as_sql:
        prior_rows = [(name, *family_proof_metadata(name)) for name in M1_008_FAMILY_NAMES]
    else:
        prior_rows = [
            (row[0], row[1], row[2])
            for row in bind.execute(
                sa.text(
                    "SELECT family_name, coverage_kind, external_proof_kind "
                    "FROM authority_state_root_leaf_families "
                    "WHERE manifest_version_id = :id ORDER BY ordinal"
                ),
                {"id": _PRIOR_MANIFEST_ID},
            ).fetchall()
        ]
    prior = {
        str(row[0]): (str(row[1]), None if row[2] is None else str(row[2])) for row in prior_rows
    }
    if not prior:
        raise RuntimeError("prior non-circular authority manifest is missing")
    if set(prior) & set(_AUDIT_EXTERNAL_FAMILIES):
        raise RuntimeError("prior authority manifest unexpectedly contains audit proof families")

    family_names = sorted(set(prior) | set(_AUDIT_EXTERNAL_FAMILIES))
    families = []
    for ordinal, family_name in enumerate(family_names, start=1):
        coverage_kind: str
        external_proof_kind: str | None
        if family_name in _AUDIT_EXTERNAL_FAMILIES:
            coverage_kind = "external_proof"
            external_proof_kind = _AUDIT_EXTERNAL_FAMILIES[family_name]
        else:
            coverage_kind, external_proof_kind = prior[family_name]
        families.append(
            {
                "ordinal": ordinal,
                "family_name": family_name,
                "coverage_kind": coverage_kind,
                "external_proof_kind": external_proof_kind,
                "canonicalization_version": 1,
            }
        )

    body = {
        "schema_version": 6,
        "canonicalization_version": 1,
        "families": families,
    }
    manifest_digest = hashlib.sha256(
        json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "id": _SEED_MANIFEST_ID,
        "schema_version": 6,
        "canonicalization_version": 1,
        "manifest_digest": manifest_digest,
        "status": "active",
        "created_at": "2026-07-14T00:00:00Z",
    }
    bind.execute(
        sa.text(
            "INSERT INTO authority_state_root_manifests "
            "(id, schema_version, canonicalization_version, manifest_digest, status, "
            "created_at, payload_json) VALUES (:id, :schema_version, :canonicalization_version, "
            ":manifest_digest, :status, :created_at, :payload_json)"
        ),
        {**manifest, "payload_json": json.dumps(manifest, separators=(",", ":"))},
    )
    for family in families:
        payload = {"manifest_version_id": _SEED_MANIFEST_ID, **family}
        bind.execute(
            sa.text(
                "INSERT INTO authority_state_root_leaf_families "
                "(manifest_version_id, ordinal, family_name, coverage_kind, "
                "external_proof_kind, canonicalization_version, payload_json) "
                "VALUES (:manifest_version_id, :ordinal, :family_name, :coverage_kind, "
                ":external_proof_kind, :canonicalization_version, :payload_json)"
            ),
            {**payload, "payload_json": json.dumps(payload, separators=(",", ":"))},
        )


def downgrade() -> None:
    drop_immutable_triggers("authority_state_root_leaf_families")
    drop_immutable_triggers("authority_state_root_manifests")
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM authority_state_root_leaf_families WHERE manifest_version_id = :id"),
        {"id": _SEED_MANIFEST_ID},
    )
    bind.execute(
        sa.text("DELETE FROM authority_state_root_manifests WHERE id = :id"),
        {"id": _SEED_MANIFEST_ID},
    )
    _immutable("authority_state_root_manifests", "authority state-root manifests")
    _immutable("authority_state_root_leaf_families", "authority state-root leaf families")
