"""Add workspace-scoped immutable audit persistence.

Revision ID: m1_002_scoped_audit
Revises: m1_001_projects
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.trigger_ddl import (
    create_reject_trigger,
    drop_immutable_triggers,
    drop_reject_trigger,
)

revision: str = "m1_002_scoped_audit"
down_revision: str | None = "m1_001_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TABLES = (
    "authorization_decision_snapshots",
    "audit_receipts",
    "audit_result_bindings",
)


def _create_immutable_triggers(table_name: str, label: str) -> None:
    create_reject_trigger(table_name, "UPDATE", f"{label} are immutable")
    create_reject_trigger(table_name, "DELETE", f"{label} are immutable")


def upgrade() -> None:
    op.create_table(
        "authorization_decision_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("request_context_id", sa.String(length=36), nullable=False),
        sa.Column("signing_key_version_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_authorization_decision_snapshots"),
        sa.UniqueConstraint(
            "workspace_id",
            "canonical_digest",
            name="uq_authorization_decision_snapshots_workspace_digest",
        ),
    )
    op.create_index(
        "ix_authorization_decision_snapshots_workspace_created",
        "authorization_decision_snapshots",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "audit_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_sequence", sa.Integer(), nullable=False),
        sa.Column("authorization_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("authority_commit_intent_id", sa.String(length=36), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "workspace_sequence >= 1",
            name="ck_audit_receipts_workspace_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_snapshot_id"],
            ["authorization_decision_snapshots.id"],
            name="fk_audit_receipts_authorization_snapshot_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_receipts"),
        sa.UniqueConstraint(
            "workspace_id",
            "workspace_sequence",
            name="uq_audit_receipts_workspace_sequence",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "receipt_hash",
            name="uq_audit_receipts_workspace_hash",
        ),
    )
    op.create_index(
        "ix_audit_receipts_workspace_created",
        "audit_receipts",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "audit_result_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("audit_receipt_id", sa.String(length=36), nullable=False),
        sa.Column("audit_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("authority_commit_intent_id", sa.String(length=36), nullable=False),
        sa.Column("binding_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_receipt_id"],
            ["audit_receipts.id"],
            name="fk_audit_result_bindings_audit_receipt_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_result_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "audit_receipt_id",
            name="uq_audit_result_bindings_workspace_receipt",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "binding_hash",
            name="uq_audit_result_bindings_workspace_hash",
        ),
    )
    op.create_index(
        "ix_audit_result_bindings_workspace_created",
        "audit_result_bindings",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "audit_anchor_recovery_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("audit_receipt_id", sa.String(length=36), nullable=False),
        sa.Column("authority_commit_intent_id", sa.String(length=36), nullable=False),
        sa.Column("prepared_result_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_binding_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_receipt_id"],
            ["audit_receipts.id"],
            name="fk_audit_anchor_recovery_receipt_id",
        ),
        sa.ForeignKeyConstraint(
            ["result_binding_id"],
            ["audit_result_bindings.id"],
            name="fk_audit_anchor_recovery_result_binding_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_anchor_recovery_checkpoints"),
        sa.UniqueConstraint(
            "workspace_id",
            "audit_receipt_id",
            name="uq_audit_anchor_recovery_workspace_receipt",
        ),
    )
    op.create_index(
        "ix_audit_anchor_recovery_workspace_state",
        "audit_anchor_recovery_checkpoints",
        ["workspace_id", "state", "updated_at", "id"],
        unique=False,
    )
    create_reject_trigger(
        "audit_anchor_recovery_checkpoints",
        "DELETE",
        "audit recovery checkpoints cannot be deleted",
    )

    _create_immutable_triggers(
        "authorization_decision_snapshots",
        "authorization snapshots",
    )
    _create_immutable_triggers("audit_receipts", "audit receipts")
    _create_immutable_triggers("audit_result_bindings", "audit result bindings")


def downgrade() -> None:
    drop_reject_trigger("audit_anchor_recovery_checkpoints", "DELETE")
    for table_name in reversed(_IMMUTABLE_TABLES):
        drop_immutable_triggers(table_name)
    op.drop_index(
        "ix_audit_anchor_recovery_workspace_state",
        table_name="audit_anchor_recovery_checkpoints",
    )
    op.drop_table("audit_anchor_recovery_checkpoints")
    op.drop_index(
        "ix_audit_result_bindings_workspace_created",
        table_name="audit_result_bindings",
    )
    op.drop_table("audit_result_bindings")
    op.drop_index("ix_audit_receipts_workspace_created", table_name="audit_receipts")
    op.drop_table("audit_receipts")
    op.drop_index(
        "ix_authorization_decision_snapshots_workspace_created",
        table_name="authorization_decision_snapshots",
    )
    op.drop_table("authorization_decision_snapshots")
