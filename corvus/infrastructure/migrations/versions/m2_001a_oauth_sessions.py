"""Add durable OAuth transactions and browser-session bindings.

Revision ID: m2_001a_oauth_sessions
Revises: m2_001_identity_continuity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m2_001a_oauth_sessions"
down_revision: str | None = "m2_001_identity_continuity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("nonce_digest", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("encrypted_pkce_verifier", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("consumed_at", sa.String(length=40), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_oauth_transaction_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_transactions"),
        sa.UniqueConstraint("state_digest", name="uq_oauth_transactions_state_digest"),
    )
    op.create_index(
        "ix_oauth_transactions_expiry",
        "oauth_transactions",
        ["expires_at", "consumed_at"],
    )
    op.create_table(
        "web_session_bindings",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("csrf_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "session_version"],
            ["session_records.id", "session_records.version"],
            name="fk_web_session_binding_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", "session_version", name="pk_web_session_bindings"),
        sa.UniqueConstraint("csrf_digest", name="uq_web_session_bindings_csrf_digest"),
    )
    op.create_table(
        "account_onboarding_versions",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("experience_kind", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 2", name="ck_account_onboarding_version"),
        sa.CheckConstraint(
            "experience_kind IN ('everyday','developer')",
            name="ck_account_onboarding_experience_kind",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_account_onboarding_account"
        ),
        sa.PrimaryKeyConstraint("account_id", "version", name="pk_account_onboarding_versions"),
    )
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
            "account_id",
            "operation",
            "idempotency_key",
            name="pk_identity_idempotency",
        ),
    )
    for table_name, label in (
        ("web_session_bindings", "web session bindings"),
        ("account_onboarding_versions", "account onboarding versions"),
        ("identity_idempotency", "identity idempotency records"),
    ):
        create_immutable_triggers(table_name, label)


def downgrade() -> None:
    bind = op.get_bind()
    history = bind.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM oauth_transactions) + "
            "(SELECT COUNT(*) FROM web_session_bindings) + "
            "(SELECT COUNT(*) FROM account_onboarding_versions) + "
            "(SELECT COUNT(*) FROM identity_idempotency)"
        )
    ).scalar_one()
    if history:
        raise RuntimeError("oauth_session_history_present")
    for table_name in (
        "identity_idempotency",
        "account_onboarding_versions",
        "web_session_bindings",
    ):
        drop_immutable_triggers(table_name)
    op.drop_table("identity_idempotency")
    op.drop_table("account_onboarding_versions")
    op.drop_table("web_session_bindings")
    op.drop_index("ix_oauth_transactions_expiry", table_name="oauth_transactions")
    op.drop_table("oauth_transactions")
