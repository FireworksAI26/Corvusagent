"""Add authority close, handoff, activation, and restore persistence.

Revision ID: m1_006_handoff_restore
Revises: m1_005_authorization_inputs
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

revision: str = "m1_006_handoff_restore"
down_revision: str | None = "m1_005_authorization_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000006"
_NEW_FAMILIES = {
    "authority_close_certificates",
    "authority_handoff_activations",
    "authority_handoffs",
    "restore_validation_receipts",
}


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def upgrade() -> None:
    op.create_table(
        "authority_close_certificates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("closed_epoch", sa.Integer(), nullable=False),
        sa.Column("source_deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("target_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("final_authority_generation", sa.Integer(), nullable=False),
        sa.Column("final_state_root", sa.String(length=64), nullable=False),
        sa.Column("epoch_key_disposition", sa.String(length=32), nullable=False),
        sa.Column("anchor_receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("externally_anchored_at", sa.String(length=40), nullable=False),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("closed_epoch >= 1", name="ck_close_certificate_epoch_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_authority_close_certificates"),
        sa.UniqueConstraint(
            "workspace_id", "closed_epoch", name="uq_close_certificate_workspace_epoch"
        ),
    )

    op.create_table(
        "authority_handoffs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("from_epoch", sa.Integer(), nullable=False),
        sa.Column("to_epoch", sa.Integer(), nullable=False),
        sa.Column("close_certificate_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("prepared_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("to_epoch = from_epoch + 1", name="ck_handoff_epoch_advance"),
        sa.ForeignKeyConstraint(
            ["close_certificate_id"],
            ["authority_close_certificates.id"],
            name="fk_handoff_close_certificate_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_handoffs"),
        sa.UniqueConstraint("workspace_id", "from_epoch", name="uq_handoff_workspace_epoch"),
    )

    op.create_table(
        "authority_handoff_activations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("target_deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
        sa.Column("source_close_certificate_id", sa.String(length=36), nullable=False),
        sa.Column("source_close_certificate_digest", sa.String(length=64), nullable=False),
        sa.Column("authority_epoch_credential_id", sa.String(length=36), nullable=False),
        sa.Column("activated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_close_certificate_id"],
            ["authority_close_certificates.id"],
            name="fk_handoff_activation_close_certificate_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_handoff_activations"),
        sa.UniqueConstraint(
            "workspace_id", "authority_epoch", name="uq_handoff_activation_workspace_epoch"
        ),
    )

    op.create_table(
        "restore_validation_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("restored_database_digest", sa.String(length=64), nullable=False),
        sa.Column("observed_epoch", sa.Integer(), nullable=False),
        sa.Column("takeover_epoch", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("validated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_restore_validation_receipts"),
    )
    op.create_index(
        "ix_restore_receipts_workspace_time",
        "restore_validation_receipts",
        ["workspace_id", "validated_at", "id"],
    )

    _immutable("authority_close_certificates", "authority close certificates")
    _immutable("authority_handoff_activations", "authority handoff activations")
    _immutable("restore_validation_receipts", "restore validation receipts")
    create_reject_trigger(
        "authority_handoffs",
        "DELETE",
        "authority handoffs cannot be deleted",
    )

    bind = op.get_bind()
    if op.get_context().as_sql:
        prior_rows = [(name,) for name in M1_005_FAMILY_NAMES]
    else:
        prior_rows = [
            (row[0],)
            for row in bind.execute(
                sa.text(
                    "SELECT family_name, coverage_kind, external_proof_kind, "
                    "canonicalization_version FROM authority_state_root_leaf_families "
                    "WHERE manifest_version_id = "
                    "'00000000-0000-4000-8000-000000000005' ORDER BY ordinal"
                )
            ).fetchall()
        ]
    names = {str(row[0]) for row in prior_rows} | _NEW_FAMILIES
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
    digest_payload = {
        "schema_version": 3,
        "canonicalization_version": 1,
        "families": families,
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "id": _SEED_MANIFEST_ID,
        "schema_version": 3,
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

    drop_reject_trigger("authority_handoffs", "DELETE")
    for table_name in (
        "restore_validation_receipts",
        "authority_handoff_activations",
        "authority_close_certificates",
    ):
        drop_immutable_triggers(table_name)
    op.drop_index("ix_restore_receipts_workspace_time", table_name="restore_validation_receipts")
    op.drop_table("restore_validation_receipts")
    op.drop_table("authority_handoff_activations")
    op.drop_table("authority_handoffs")
    op.drop_table("authority_close_certificates")
