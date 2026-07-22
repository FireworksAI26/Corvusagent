"""Add immutable authorization inputs and idempotency persistence.

Revision ID: m1_005_authorization_inputs
Revises: m1_004_registry_manifest
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import M1_005_FAMILY_NAMES
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    create_reject_trigger,
    drop_immutable_triggers,
    drop_reject_trigger,
)

revision: str = "m1_005_authorization_inputs"
down_revision: str | None = "m1_004_registry_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000005"
_FAMILY_NAMES = M1_005_FAMILY_NAMES


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def _manifest_families() -> list[dict[str, object]]:
    return [
        {
            "ordinal": ordinal,
            "family_name": family_name,
            "coverage_kind": (
                "external_proof"
                if family_name == "authority_registry_freshness_proofs"
                else "in_root"
            ),
            "external_proof_kind": (
                "registry_freshness_proof"
                if family_name == "authority_registry_freshness_proofs"
                else None
            ),
            "canonicalization_version": 1,
        }
        for ordinal, family_name in enumerate(_FAMILY_NAMES, start=1)
    ]


def _manifest_digest(families: list[dict[str, object]]) -> str:
    payload = {
        "schema_version": 2,
        "canonicalization_version": 1,
        "families": families,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.create_table(
        "audience_policy_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("visibility", sa.String(length=40), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("policy_version >= 1", name="ck_audience_policy_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_audience_policy_snapshots"),
    )
    op.create_index(
        "ix_audience_policy_workspace_visibility",
        "audience_policy_snapshots",
        ["workspace_id", "visibility", "id"],
    )

    op.create_table(
        "access_bundles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_access_bundle_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_access_bundles"),
    )
    op.create_index(
        "ix_access_bundle_workspace_principal",
        "access_bundles",
        ["workspace_id", "principal_id", "scope_kind", "scope_id"],
    )

    op.create_table(
        "capability_grants",
        sa.Column("grant_digest", sa.String(length=64), nullable=False),
        sa.Column("bundle_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("resource_kind", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("effect", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bundle_id"], ["access_bundles.id"], name="fk_capability_grants_bundle_id"
        ),
        sa.PrimaryKeyConstraint("grant_digest", name="pk_capability_grants"),
        sa.UniqueConstraint(
            "bundle_id",
            "resource_kind",
            "resource_id",
            "action",
            "effect",
            name="uq_capability_grant_semantics",
        ),
    )

    op.create_table(
        "agent_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("capability_bundle_id", sa.String(length=36), nullable=False),
        sa.Column("autonomy_level", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("autonomy_level BETWEEN 0 AND 5", name="ck_agent_grant_autonomy_range"),
        sa.ForeignKeyConstraint(
            ["capability_bundle_id"],
            ["access_bundles.id"],
            name="fk_agent_grants_capability_bundle_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_grants"),
    )
    op.create_index(
        "ix_agent_grants_workspace_agent",
        "agent_grants",
        ["workspace_id", "agent_id", "id"],
    )

    op.create_table(
        "delegation_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("parent_agent_grant_id", sa.String(length=36), nullable=False),
        sa.Column("child_agent_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_agent_grant_id"],
            ["agent_grants.id"],
            name="fk_delegation_grants_parent_agent_grant_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_delegation_grants"),
    )
    op.create_index(
        "ix_delegation_grants_workspace_parent",
        "delegation_grants",
        ["workspace_id", "parent_agent_grant_id", "id"],
    )

    op.create_table(
        "workspace_signing_key_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.String(length=40), nullable=False),
        sa.Column("valid_until", sa.String(length=40), nullable=True),
        sa.Column("predecessor_digest", sa.String(length=64), nullable=True),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("key_epoch >= 1", name="ck_workspace_signing_key_epoch_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_signing_key_versions"),
        sa.UniqueConstraint("workspace_id", "key_epoch", name="uq_workspace_signing_key_epoch"),
    )

    op.create_table(
        "idempotency_envelopes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("requester_id", sa.String(length=36), nullable=False),
        sa.Column("transport_principal_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("agent_grant_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("request_context_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("result_ref", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_envelopes"),
        sa.UniqueConstraint(
            "workspace_id",
            "requester_id",
            "transport_principal_id",
            "agent_id",
            "agent_grant_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_envelope_identity",
        ),
    )

    for table_name, label in (
        ("audience_policy_snapshots", "audience policy snapshots"),
        ("access_bundles", "access bundles"),
        ("capability_grants", "capability grants"),
        ("agent_grants", "agent grants"),
        ("delegation_grants", "delegation grants"),
        ("workspace_signing_key_versions", "workspace signing key versions"),
    ):
        _immutable(table_name, label)
    create_reject_trigger(
        "idempotency_envelopes",
        "DELETE",
        "idempotency envelopes cannot be deleted",
    )

    families = _manifest_families()
    manifest_digest = _manifest_digest(families)
    manifest_payload = {
        "id": _SEED_MANIFEST_ID,
        "schema_version": 2,
        "canonicalization_version": 1,
        "manifest_digest": manifest_digest,
        "status": "active",
        "created_at": "2026-07-14T00:00:00Z",
    }
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO authority_state_root_manifests "
            "(id, schema_version, canonicalization_version, manifest_digest, status, "
            "created_at, payload_json) VALUES (:id, :schema_version, :canonicalization_version, "
            ":manifest_digest, :status, :created_at, :payload_json)"
        ),
        {**manifest_payload, "payload_json": json.dumps(manifest_payload, separators=(",", ":"))},
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
        sa.text(
            "DELETE FROM authority_state_root_leaf_families "
            "WHERE manifest_version_id = :manifest_id"
        ),
        {"manifest_id": _SEED_MANIFEST_ID},
    )
    bind.execute(
        sa.text("DELETE FROM authority_state_root_manifests WHERE id = :manifest_id"),
        {"manifest_id": _SEED_MANIFEST_ID},
    )
    _immutable("authority_state_root_manifests", "authority state-root manifests")
    _immutable("authority_state_root_leaf_families", "authority state-root leaf families")

    drop_reject_trigger("idempotency_envelopes", "DELETE")
    op.drop_table("idempotency_envelopes")
    for table_name in (
        "workspace_signing_key_versions",
        "delegation_grants",
        "agent_grants",
        "capability_grants",
        "access_bundles",
        "audience_policy_snapshots",
    ):
        drop_immutable_triggers(table_name)
        op.drop_table(table_name)
