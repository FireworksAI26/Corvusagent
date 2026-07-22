"""Add identity ownership and discriminated scope persistence.

Revision ID: m1_007_identity_scope
Revises: m1_006_handoff_restore
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import M1_006_FAMILY_NAMES
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m1_007_identity_scope"
down_revision: str | None = "m1_006_handoff_restore"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000007"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000006"
_NEW_FAMILIES = {
    "agent_identities",
    "identity_workspaces",
    "principals",
    "scopes",
    "workspace_memberships",
}


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def upgrade() -> None:
    op.create_table(
        "identity_workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_identity_workspace_version_positive"),
        sa.PrimaryKeyConstraint("id", "version", name="pk_identity_workspaces"),
    )
    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("external_provider", sa.String(length=200), nullable=False),
        sa.Column("external_subject", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_principals"),
        sa.UniqueConstraint(
            "external_provider", "external_subject", name="uq_principal_external_identity"
        ),
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_membership_version_positive"),
        sa.PrimaryKeyConstraint(
            "workspace_id", "principal_id", "version", name="pk_workspace_memberships"
        ),
    )
    op.create_table(
        "agent_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("model_route", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_agent_identity_version_positive"),
        sa.PrimaryKeyConstraint("id", "version", name="pk_agent_identities"),
    )
    op.create_index(
        "ix_agent_identities_workspace",
        "agent_identities",
        ["workspace_id", "id", "version"],
    )
    op.create_table(
        "scopes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("parent_scope_kind", sa.String(length=32), nullable=True),
        sa.Column("parent_scope_id", sa.String(length=36), nullable=True),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('workspace','project','channel','thread','conversation')",
            name="ck_scope_kind_known",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "kind", "scope_id", name="pk_scopes"),
    )
    for table_name, label in (
        ("identity_workspaces", "identity workspace versions"),
        ("principals", "principals"),
        ("workspace_memberships", "workspace membership versions"),
        ("agent_identities", "agent identity versions"),
        ("scopes", "scopes"),
    ):
        _immutable(table_name, label)

    bind = op.get_bind()
    if op.get_context().as_sql:
        prior = [(name,) for name in M1_006_FAMILY_NAMES]
    else:
        prior = [
            (row[0],)
            for row in bind.execute(
                sa.text(
                    "SELECT family_name FROM authority_state_root_leaf_families "
                    "WHERE manifest_version_id = :id ORDER BY ordinal"
                ),
                {"id": _PRIOR_MANIFEST_ID},
            ).fetchall()
        ]
    names = {str(row[0]) for row in prior} | _NEW_FAMILIES
    families = [
        {
            "ordinal": ordinal,
            "family_name": name,
            "coverage_kind": (
                "external_proof" if name == "authority_registry_freshness_proofs" else "in_root"
            ),
            "external_proof_kind": (
                "registry_freshness_proof"
                if name == "authority_registry_freshness_proofs"
                else None
            ),
            "canonicalization_version": 1,
        }
        for ordinal, name in enumerate(sorted(names), start=1)
    ]
    body = {"schema_version": 4, "canonicalization_version": 1, "families": families}
    digest = hashlib.sha256(
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
        "schema_version": 4,
        "canonicalization_version": 1,
        "manifest_digest": digest,
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
    for table_name in (
        "scopes",
        "agent_identities",
        "workspace_memberships",
        "principals",
        "identity_workspaces",
    ):
        drop_immutable_triggers(table_name)
    op.drop_table("scopes")
    op.drop_index("ix_agent_identities_workspace", table_name="agent_identities")
    op.drop_table("agent_identities")
    op.drop_table("workspace_memberships")
    op.drop_table("principals")
    op.drop_table("identity_workspaces")
