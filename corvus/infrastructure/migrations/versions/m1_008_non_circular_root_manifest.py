"""Add a non-circular authority-root manifest.

Revision ID: m1_008_non_circular_root_manifest
Revises: m1_007_identity_scope
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import (
    M1_007_FAMILY_NAMES,
    family_proof_metadata,
)
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m1_008_non_circular_root_manifest"
down_revision: str | None = "m1_007_identity_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000008"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000007"
_DERIVED_POST_COMMIT_FAMILIES = {
    "audit_anchor_recovery_checkpoints",
    "audit_result_bindings",
}
_ALEMBIC_REVISION_LENGTH = 64


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=_ALEMBIC_REVISION_LENGTH),
            existing_nullable=False,
        )
    if op.get_context().as_sql:
        prior_rows = [(name, *family_proof_metadata(name)) for name in M1_007_FAMILY_NAMES]
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
    prior_names = {str(row[0]) for row in prior_rows}
    if not _DERIVED_POST_COMMIT_FAMILIES <= prior_names:
        raise RuntimeError("prior authority manifest is missing derived post-commit families")

    prior_by_name = {
        str(row[0]): (str(row[1]), None if row[2] is None else str(row[2])) for row in prior_rows
    }
    family_names = sorted(prior_names - _DERIVED_POST_COMMIT_FAMILIES)
    families = [
        {
            "ordinal": ordinal,
            "family_name": family_name,
            "coverage_kind": prior_by_name[family_name][0],
            "external_proof_kind": prior_by_name[family_name][1],
            "canonicalization_version": 1,
        }
        for ordinal, family_name in enumerate(family_names, start=1)
    ]
    body = {
        "schema_version": 5,
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
        "schema_version": 5,
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
