"""Add deployment and workspace authority persistence.

Revision ID: m1_003_authority_core
Revises: m1_002_scoped_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    create_reject_trigger,
    drop_immutable_triggers,
    drop_reject_trigger,
)

revision: str = "m1_003_authority_core"
down_revision: str | None = "m1_002_scoped_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _no_delete(table_name: str, label: str) -> None:
    create_reject_trigger(table_name, "DELETE", f"{label} cannot be deleted")


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def upgrade() -> None:
    op.create_table(
        "deployment_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_deployment_profiles_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_profiles"),
    )

    op.create_table(
        "deployment_instances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("deployment_profile_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("device_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("activated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_profile_id"],
            ["deployment_profiles.id"],
            name="fk_deployment_instances_profile_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_instances"),
    )
    op.create_index(
        "ix_deployment_instances_profile_status",
        "deployment_instances",
        ["deployment_profile_id", "status", "id"],
        unique=False,
    )

    op.create_table(
        "authority_epoch_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
        sa.Column("deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("device_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "authority_epoch >= 1",
            name="ck_authority_epoch_credentials_epoch_positive",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_instance_id"],
            ["deployment_instances.id"],
            name="fk_authority_epoch_credentials_instance_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_epoch_credentials"),
        sa.UniqueConstraint(
            "workspace_id",
            "authority_epoch",
            name="uq_authority_epoch_credentials_workspace_epoch",
        ),
    )

    op.create_table(
        "authority_trust_anchors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_authority_trust_anchors"),
    )
    op.create_index(
        "ix_authority_trust_anchors_workspace_status",
        "authority_trust_anchors",
        ["workspace_id", "status", "id"],
        unique=False,
    )

    op.create_table(
        "deployment_instance_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
        sa.Column("deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("lock_name", sa.String(length=200), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.String(length=40), nullable=False),
        sa.Column("released_at", sa.String(length=40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "authority_epoch >= 1",
            name="ck_deployment_instance_leases_epoch_positive",
        ),
        sa.CheckConstraint(
            "fencing_token >= 1",
            name="ck_deployment_instance_leases_fencing_positive",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_instance_id"],
            ["deployment_instances.id"],
            name="fk_deployment_instance_leases_instance_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_instance_leases"),
        sa.UniqueConstraint(
            "workspace_id",
            "authority_epoch",
            "fencing_token",
            name="uq_deployment_instance_leases_workspace_epoch_fencing",
        ),
    )
    op.create_index(
        "uq_deployment_instance_leases_active_workspace_epoch",
        "deployment_instance_leases",
        ["workspace_id", "authority_epoch"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "workspace_authorities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("deployment_profile_id", sa.String(length=36), nullable=False),
        sa.Column("deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("authority_generation", sa.Integer(), nullable=False),
        sa.Column("authority_state_root", sa.String(length=64), nullable=False),
        sa.Column("authority_epoch_credential_id", sa.String(length=36), nullable=False),
        sa.Column("trust_anchor_id", sa.String(length=36), nullable=False),
        sa.Column("active_lease_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="ck_workspace_authorities_epoch_positive"),
        sa.CheckConstraint(
            "authority_generation >= 0",
            name="ck_workspace_authorities_generation_nonnegative",
        ),
        sa.CheckConstraint("version >= 1", name="ck_workspace_authorities_version_positive"),
        sa.ForeignKeyConstraint(
            ["deployment_profile_id"],
            ["deployment_profiles.id"],
            name="fk_workspace_authorities_profile_id",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_instance_id"],
            ["deployment_instances.id"],
            name="fk_workspace_authorities_instance_id",
        ),
        sa.ForeignKeyConstraint(
            ["authority_epoch_credential_id"],
            ["authority_epoch_credentials.id"],
            name="fk_workspace_authorities_credential_id",
        ),
        sa.ForeignKeyConstraint(
            ["trust_anchor_id"],
            ["authority_trust_anchors.id"],
            name="fk_workspace_authorities_trust_anchor_id",
        ),
        sa.ForeignKeyConstraint(
            ["active_lease_id"],
            ["deployment_instance_leases.id"],
            name="fk_workspace_authorities_active_lease_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_authorities"),
        sa.UniqueConstraint("workspace_id", name="uq_workspace_authorities_workspace_id"),
    )

    op.create_table(
        "authority_commit_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("deployment_instance_id", sa.String(length=36), nullable=False),
        sa.Column("prior_generation", sa.Integer(), nullable=False),
        sa.Column("next_generation", sa.Integer(), nullable=False),
        sa.Column("prior_state_root", sa.String(length=64), nullable=False),
        sa.Column("mutation_digest", sa.String(length=64), nullable=False),
        sa.Column("proposed_state_root", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="ck_authority_commit_intents_epoch_positive"),
        sa.CheckConstraint(
            "next_generation = prior_generation + 1",
            name="ck_authority_commit_intents_generation_advance",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_instance_id"],
            ["deployment_instances.id"],
            name="fk_authority_commit_intents_instance_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_commit_intents"),
    )
    op.create_index(
        "uq_authority_commit_intents_inflight_workspace",
        "authority_commit_intents",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text("state NOT IN ('anchor_finalized', 'quarantined')"),
        postgresql_where=sa.text("state NOT IN ('anchor_finalized', 'quarantined')"),
    )

    _immutable("deployment_profiles", "deployment profiles")
    for table_name, label in (
        ("deployment_instances", "deployment instances"),
        ("authority_epoch_credentials", "authority epoch credentials"),
        ("authority_trust_anchors", "authority trust anchors"),
        ("deployment_instance_leases", "deployment instance leases"),
        ("workspace_authorities", "workspace authorities"),
        ("authority_commit_intents", "authority commit intents"),
    ):
        _no_delete(table_name, label)


def downgrade() -> None:
    for table_name in (
        "authority_commit_intents",
        "workspace_authorities",
        "deployment_instance_leases",
        "authority_trust_anchors",
        "authority_epoch_credentials",
        "deployment_instances",
    ):
        drop_reject_trigger(table_name, "DELETE")
    drop_immutable_triggers("deployment_profiles")
    op.drop_index(
        "uq_authority_commit_intents_inflight_workspace",
        table_name="authority_commit_intents",
    )
    op.drop_table("authority_commit_intents")
    op.drop_table("workspace_authorities")
    op.drop_index(
        "uq_deployment_instance_leases_active_workspace_epoch",
        table_name="deployment_instance_leases",
    )
    op.drop_table("deployment_instance_leases")
    op.drop_index(
        "ix_authority_trust_anchors_workspace_status",
        table_name="authority_trust_anchors",
    )
    op.drop_table("authority_trust_anchors")
    op.drop_table("authority_epoch_credentials")
    op.drop_index(
        "ix_deployment_instances_profile_status",
        table_name="deployment_instances",
    )
    op.drop_table("deployment_instances")
    op.drop_table("deployment_profiles")
