"""Add hosted account, external identity, device, and session continuity.

Revision ID: m2_001_identity_continuity
Revises: m1_009_audit_external_proofs
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import (
    M1_009_FAMILY_NAMES,
    family_proof_metadata,
)
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m2_001_identity_continuity"
down_revision: str | None = "m1_009_audit_external_proofs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000010"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000009"
_NEW_FAMILIES = {
    "accounts",
    "device_registrations",
    "external_identities",
    "session_records",
}
_IMMUTABLE_TABLES = tuple(sorted(_NEW_FAMILIES))
_IDENTITY_HISTORY_COUNT_SQL = sa.text(
    "SELECT (SELECT COUNT(*) FROM accounts) + "
    "(SELECT COUNT(*) FROM external_identities) + "
    "(SELECT COUNT(*) FROM device_registrations) + "
    "(SELECT COUNT(*) FROM session_records)"
)


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def _workspace_metadata_is_downgrade_compatible(bind: sa.Connection) -> bool:
    rows = bind.execute(
        sa.text("SELECT workspace_kind, payload_json FROM identity_workspaces")
    ).fetchall()
    for workspace_kind, payload_json in rows:
        if workspace_kind != "individual":
            return False
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("workspace_kind", "individual") != workspace_kind:
            return False
    return True


def upgrade() -> None:
    op.add_column(
        "identity_workspaces",
        sa.Column(
            "workspace_kind",
            sa.String(length=32),
            nullable=False,
            server_default="individual",
        ),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("experience_kind", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_account_version_positive"),
        sa.CheckConstraint(
            "experience_kind IS NULL OR experience_kind IN ('everyday','developer')",
            name="ck_account_experience_kind_known",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_accounts_principal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("principal_id", name="uq_accounts_principal"),
        sa.UniqueConstraint("normalized_email", name="uq_accounts_normalized_email"),
    )
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_external_identities_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_identities"),
        sa.UniqueConstraint("issuer", "subject", name="uq_external_identity_issuer_subject"),
    )
    op.create_index(
        "ix_external_identities_account",
        "external_identities",
        ["account_id", "created_at", "id"],
    )
    op.create_table(
        "device_registrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("public_key_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_device_version_positive"),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_device_revocation_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_device_registrations_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", "version", name="pk_device_registrations"),
        sa.UniqueConstraint(
            "id",
            "version",
            "account_id",
            name="uq_device_registrations_identity_account",
        ),
    )
    op.create_index(
        "ix_device_registrations_account",
        "device_registrations",
        ["account_id", "id", "version"],
    )
    op.create_table(
        "session_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("device_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=True),
        sa.Column("predecessor_digest", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("revoked_at", sa.String(length=40), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_session_version_positive"),
        sa.CheckConstraint("device_version >= 1", name="ck_session_device_version_positive"),
        sa.CheckConstraint(
            "(status = 'active' AND token_digest IS NOT NULL AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND token_digest IS NULL AND revoked_at IS NOT NULL "
            "AND predecessor_digest IS NOT NULL)",
            name="ck_session_state_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_session_records_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "device_version", "account_id"],
            [
                "device_registrations.id",
                "device_registrations.version",
                "device_registrations.account_id",
            ],
            name="fk_session_records_device_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", "version", name="pk_session_records"),
        sa.UniqueConstraint("token_digest", name="uq_session_records_token_digest"),
    )
    op.create_index(
        "ix_session_records_account_device",
        "session_records",
        ["account_id", "device_id", "id", "version"],
    )
    for table_name, label in (
        ("accounts", "accounts"),
        ("external_identities", "external identities"),
        ("device_registrations", "device registration versions"),
        ("session_records", "session record versions"),
    ):
        _immutable(table_name, label)

    bind = op.get_bind()
    if op.get_context().as_sql:
        prior = {name: family_proof_metadata(name) for name in M1_009_FAMILY_NAMES}
    else:
        prior_rows = bind.execute(
            sa.text(
                "SELECT family_name, coverage_kind, external_proof_kind "
                "FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = :id ORDER BY ordinal"
            ),
            {"id": _PRIOR_MANIFEST_ID},
        ).fetchall()
        prior = {
            str(row[0]): (str(row[1]), None if row[2] is None else str(row[2]))
            for row in prior_rows
        }
    if set(prior) != set(M1_009_FAMILY_NAMES):
        raise RuntimeError("prior authority manifest family set mismatch")
    family_names = sorted(set(prior) | _NEW_FAMILIES)
    families = [
        {
            "ordinal": ordinal,
            "family_name": family_name,
            "coverage_kind": prior.get(family_name, ("in_root", None))[0],
            "external_proof_kind": prior.get(family_name, ("in_root", None))[1],
            "canonicalization_version": 1,
        }
        for ordinal, family_name in enumerate(family_names, start=1)
    ]
    body = {"schema_version": 7, "canonicalization_version": 1, "families": families}
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
        "schema_version": 7,
        "canonicalization_version": 1,
        "manifest_digest": manifest_digest,
        "status": "active",
        "created_at": "2026-07-16T00:00:00Z",
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
    if op.get_context().as_sql:
        raise RuntimeError("identity_continuity_downgrade_requires_online_history_check")
    bind = op.get_bind()
    if bind.execute(_IDENTITY_HISTORY_COUNT_SQL).scalar_one():
        raise RuntimeError("identity_continuity_history_present")
    if not _workspace_metadata_is_downgrade_compatible(bind):
        raise RuntimeError("identity_continuity_workspace_metadata_present")
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
    _immutable("authority_state_root_manifests", "authority state-root manifests")
    _immutable("authority_state_root_leaf_families", "authority state-root leaf families")
    for table_name in reversed(_IMMUTABLE_TABLES):
        drop_immutable_triggers(table_name)
    op.drop_index("ix_session_records_account_device", table_name="session_records")
    op.drop_table("session_records")
    op.drop_index("ix_device_registrations_account", table_name="device_registrations")
    op.drop_table("device_registrations")
    op.drop_index("ix_external_identities_account", table_name="external_identities")
    op.drop_table("external_identities")
    op.drop_table("accounts")
    op.drop_column("identity_workspaces", "workspace_kind")
