"""Add ordered workspace synchronization and generalized platform idempotency.

Revision ID: m2_002_workspace_sync
Revises: m2_001a_oauth_sessions
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import (
    M2_001_FAMILY_NAMES,
    family_proof_metadata,
)
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    create_reject_trigger,
    drop_immutable_triggers,
    drop_reject_trigger,
)

revision: str = "m2_002_workspace_sync"
down_revision: str | None = "m2_001a_oauth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000011"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000010"
_NEW_FAMILIES = {
    "device_sync_acknowledgements",
    "outbox_events",
    "platform_idempotency",
    "workspace_changes",
    "workspace_sync_heads",
}
_SYNC_HISTORY_COUNT_SQL = sa.text(
    "SELECT (SELECT COUNT(*) FROM workspace_sync_heads) + "
    "(SELECT COUNT(*) FROM workspace_changes) + "
    "(SELECT COUNT(*) FROM outbox_events) + "
    "(SELECT COUNT(*) FROM device_sync_acknowledgements) + "
    "(SELECT COUNT(*) FROM platform_idempotency WHERE scope_key <> 'account')"
)


def _identity_idempotency_table() -> None:
    op.create_table(
        "identity_idempotency",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_identity_idempotency_account"
        ),
        sa.PrimaryKeyConstraint(
            "account_id", "operation", "idempotency_key", name="pk_identity_idempotency"
        ),
    )


def _manifest_families(bind: sa.Connection) -> list[dict[str, object]]:
    if op.get_context().as_sql:
        prior = {name: family_proof_metadata(name) for name in M2_001_FAMILY_NAMES}
    else:
        rows = bind.execute(
            sa.text(
                "SELECT family_name, coverage_kind, external_proof_kind "
                "FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = :id ORDER BY ordinal"
            ),
            {"id": _PRIOR_MANIFEST_ID},
        ).fetchall()
        prior = {
            str(row[0]): (str(row[1]), None if row[2] is None else str(row[2])) for row in rows
        }
    if set(prior) != set(M2_001_FAMILY_NAMES):
        raise RuntimeError("prior authority manifest family set mismatch")
    return [
        {
            "ordinal": ordinal,
            "family_name": family_name,
            "coverage_kind": prior.get(family_name, ("in_root", None))[0],
            "external_proof_kind": prior.get(family_name, ("in_root", None))[1],
            "canonicalization_version": 1,
        }
        for ordinal, family_name in enumerate(sorted(set(prior) | _NEW_FAMILIES), start=1)
    ]


def _insert_manifest(bind: sa.Connection) -> None:
    families = _manifest_families(bind)
    body = {"schema_version": 8, "canonicalization_version": 1, "families": families}
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
        "schema_version": 8,
        "canonicalization_version": 1,
        "manifest_digest": manifest_digest,
        "status": "active",
        "created_at": "2026-07-16T00:00:01Z",
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


def upgrade() -> None:
    op.create_index(
        "uq_accounts_id_principal",
        "accounts",
        ["id", "principal_id"],
        unique=True,
    )
    op.create_table(
        "platform_idempotency",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=True),
        sa.Column("scope_key", sa.String(length=200), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_version", sa.Integer(), nullable=True),
        sa.Column("membership_version", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(length=36), nullable=True),
        sa.Column("device_version", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint(
            "(scope_key = 'account' AND principal_id IS NULL AND workspace_id IS NULL "
            "AND workspace_version IS NULL AND membership_version IS NULL "
            "AND device_id IS NULL AND device_version IS NULL) OR "
            "(scope_key = workspace_id || ':' || device_id AND workspace_id IS NOT NULL "
            "AND workspace_version IS NOT NULL AND principal_id IS NOT NULL "
            "AND membership_version IS NOT NULL AND device_id IS NOT NULL "
            "AND device_version IS NOT NULL)",
            name="ck_platform_idempotency_scope",
        ),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_platform_request_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_version"],
            ["identity_workspaces.id", "identity_workspaces.version"],
            name="fk_platform_idempotency_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_platform_idempotency_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "principal_id"],
            ["accounts.id", "accounts.principal_id"],
            name="fk_platform_idempotency_account_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "principal_id", "membership_version"],
            [
                "workspace_memberships.workspace_id",
                "workspace_memberships.principal_id",
                "workspace_memberships.version",
            ],
            name="fk_platform_idempotency_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "device_version", "account_id"],
            [
                "device_registrations.id",
                "device_registrations.version",
                "device_registrations.account_id",
            ],
            name="fk_platform_idempotency_device_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "account_id",
            "scope_key",
            "operation",
            "idempotency_key",
            name="pk_platform_idempotency",
        ),
    )
    op.execute(
        "INSERT INTO platform_idempotency "
        "(account_id, scope_key, workspace_id, workspace_version, device_id, device_version, operation, "
        "idempotency_key, request_digest, result_json, created_at) "
        "SELECT account_id, 'account', NULL, NULL, NULL, NULL, operation, idempotency_key, "
        "request_digest, result_json, created_at FROM identity_idempotency"
    )
    drop_immutable_triggers("identity_idempotency")
    op.drop_table("identity_idempotency")

    op.create_table(
        "workspace_sync_heads",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("current_sequence", sa.Integer(), nullable=False),
        sa.Column("retention_floor", sa.Integer(), nullable=False),
        sa.Column("chain_digest", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("current_sequence >= 0", name="ck_sync_head_sequence"),
        sa.CheckConstraint(
            "retention_floor >= 0 AND retention_floor <= current_sequence",
            name="ck_sync_head_retention",
        ),
        sa.CheckConstraint("version >= 1", name="ck_sync_head_version"),
        sa.CheckConstraint("length(chain_digest) = 64", name="ck_sync_head_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_version"],
            ["identity_workspaces.id", "identity_workspaces.version"],
            name="fk_sync_head_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_sync_heads"),
    )
    op.create_table(
        "workspace_changes",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_digest", sa.String(length=64), nullable=False),
        sa.Column("change_digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("device_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_workspace_change_sequence"),
        sa.CheckConstraint("entity_version >= 1", name="ck_workspace_change_entity_version"),
        sa.CheckConstraint("length(previous_digest) = 64", name="ck_change_previous_digest"),
        sa.CheckConstraint("length(change_digest) = 64", name="ck_change_digest"),
        sa.CheckConstraint(
            "kind IN ('account_profile','workspace_profile')",
            name="ck_workspace_change_kind",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_version"],
            ["identity_workspaces.id", "identity_workspaces.version"],
            name="fk_workspace_change_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "principal_id", "membership_version"],
            [
                "workspace_memberships.workspace_id",
                "workspace_memberships.principal_id",
                "workspace_memberships.version",
            ],
            name="fk_workspace_change_membership",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "principal_id"],
            ["accounts.id", "accounts.principal_id"],
            name="fk_workspace_change_account_principal",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "device_version", "account_id"],
            [
                "device_registrations.id",
                "device_registrations.version",
                "device_registrations.account_id",
            ],
            name="fk_workspace_change_device_account",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "sequence", name="pk_workspace_changes"),
        sa.UniqueConstraint(
            "workspace_id", "sequence", "change_digest", name="uq_workspace_change_digest"
        ),
    )
    op.create_index(
        "ix_workspace_changes_entity",
        "workspace_changes",
        ["workspace_id", "kind", "entity_id", "entity_version"],
    )
    op.create_table(
        "outbox_events",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("change_digest", sa.String(length=64), nullable=False),
        sa.Column("event_kind", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("length(change_digest) = 64", name="ck_outbox_change_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "sequence", "change_digest"],
            [
                "workspace_changes.workspace_id",
                "workspace_changes.sequence",
                "workspace_changes.change_digest",
            ],
            name="fk_outbox_workspace_change",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "sequence", name="pk_outbox_events"),
    )
    op.create_table(
        "device_sync_acknowledgements",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("device_version", sa.Integer(), nullable=False),
        sa.Column("acknowledged_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_device_sync_ack_version"),
        sa.CheckConstraint("acknowledged_sequence >= 1", name="ck_device_sync_ack_sequence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workspace_version"],
            ["identity_workspaces.id", "identity_workspaces.version"],
            name="fk_device_sync_ack_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "principal_id", "membership_version"],
            [
                "workspace_memberships.workspace_id",
                "workspace_memberships.principal_id",
                "workspace_memberships.version",
            ],
            name="fk_device_sync_ack_membership",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "principal_id"],
            ["accounts.id", "accounts.principal_id"],
            name="fk_device_sync_ack_account_principal",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "device_version", "account_id"],
            [
                "device_registrations.id",
                "device_registrations.version",
                "device_registrations.account_id",
            ],
            name="fk_device_sync_ack_device_account",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "device_id", "version", name="pk_device_sync_acknowledgements"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "device_id",
            "acknowledged_sequence",
            name="uq_device_sync_ack_sequence",
        ),
    )
    op.create_index(
        "ix_device_sync_ack_current",
        "device_sync_acknowledgements",
        ["workspace_id", "device_id", "version"],
    )
    create_reject_trigger(
        "workspace_sync_heads", "DELETE", "workspace sync heads cannot be deleted"
    )
    for table_name, label in (
        ("platform_idempotency", "platform idempotency records"),
        ("workspace_changes", "workspace changes"),
        ("outbox_events", "outbox events"),
        ("device_sync_acknowledgements", "device sync acknowledgements"),
    ):
        create_immutable_triggers(table_name, label)
    _insert_manifest(op.get_bind())


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("workspace_sync_downgrade_requires_online_history_check")
    bind = op.get_bind()
    if bind.execute(_SYNC_HISTORY_COUNT_SQL).scalar_one():
        raise RuntimeError("workspace_sync_history_present")

    drop_immutable_triggers("authority_state_root_leaf_families")
    drop_immutable_triggers("authority_state_root_manifests")
    bind.execute(
        sa.text("DELETE FROM authority_state_root_leaf_families WHERE manifest_version_id = :id"),
        {"id": _SEED_MANIFEST_ID},
    )
    bind.execute(
        sa.text("DELETE FROM authority_state_root_manifests WHERE id = :id"),
        {"id": _SEED_MANIFEST_ID},
    )
    create_immutable_triggers("authority_state_root_manifests", "authority state-root manifests")
    create_immutable_triggers(
        "authority_state_root_leaf_families", "authority state-root leaf families"
    )

    drop_immutable_triggers("device_sync_acknowledgements")
    op.drop_index("ix_device_sync_ack_current", table_name="device_sync_acknowledgements")
    op.drop_table("device_sync_acknowledgements")
    drop_immutable_triggers("outbox_events")
    op.drop_table("outbox_events")
    drop_immutable_triggers("workspace_changes")
    op.drop_index("ix_workspace_changes_entity", table_name="workspace_changes")
    op.drop_table("workspace_changes")
    drop_reject_trigger("workspace_sync_heads", "DELETE")
    op.drop_table("workspace_sync_heads")

    _identity_idempotency_table()
    op.execute(
        "INSERT INTO identity_idempotency "
        "(account_id, operation, idempotency_key, request_digest, result_json, created_at) "
        "SELECT account_id, operation, idempotency_key, request_digest, result_json, created_at "
        "FROM platform_idempotency WHERE scope_key = 'account'"
    )
    create_immutable_triggers("identity_idempotency", "identity idempotency records")
    drop_immutable_triggers("platform_idempotency")
    op.drop_table("platform_idempotency")
    op.drop_index("uq_accounts_id_principal", table_name="accounts")
