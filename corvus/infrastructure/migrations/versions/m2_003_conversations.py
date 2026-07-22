"""Add workspace-scoped conversation persistence.

Revision ID: m2_003_conversations
Revises: m2_002_workspace_sync
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.manifest_history import (
    M2_002_FAMILY_NAMES,
    family_proof_metadata,
)
from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m2_003_conversations"
down_revision: str | None = "m2_002_workspace_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000012"
_PRIOR_MANIFEST_ID = "00000000-0000-4000-8000-000000000011"
_NEW_FAMILIES = {
    "agent_run_events",
    "agent_runs",
    "attachments",
    "message_attachments",
    "messages",
    "run_artifact_lineage",
    "run_artifacts",
    "thread_versions",
    "threads",
}
_HISTORY_COUNT_SQL = sa.text(
    "SELECT (SELECT COUNT(*) FROM agent_run_events) + "
    "(SELECT COUNT(*) FROM agent_runs) + (SELECT COUNT(*) FROM attachments) + "
    "(SELECT COUNT(*) FROM message_attachments) + (SELECT COUNT(*) FROM messages) + "
    "(SELECT COUNT(*) FROM run_artifact_lineage) + (SELECT COUNT(*) FROM run_artifacts) + "
    "(SELECT COUNT(*) FROM thread_versions) + (SELECT COUNT(*) FROM threads)"
)


def _manifest_families(bind: sa.Connection) -> list[dict[str, object]]:
    if op.get_context().as_sql:
        prior = set(M2_002_FAMILY_NAMES)
    else:
        prior = {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT family_name FROM authority_state_root_leaf_families "
                    "WHERE manifest_version_id = :id"
                ),
                {"id": _PRIOR_MANIFEST_ID},
            ).fetchall()
        }
    if prior != set(M2_002_FAMILY_NAMES):
        raise RuntimeError("prior authority manifest family set mismatch")
    return [
        {
            "ordinal": ordinal,
            "family_name": family_name,
            "coverage_kind": family_proof_metadata(family_name)[0],
            "external_proof_kind": family_proof_metadata(family_name)[1],
            "canonicalization_version": 1,
        }
        for ordinal, family_name in enumerate(sorted(prior | _NEW_FAMILIES), start=1)
    ]


def _insert_manifest(bind: sa.Connection) -> None:
    families = _manifest_families(bind)
    body = {"schema_version": 9, "canonicalization_version": 1, "families": families}
    manifest_digest = hashlib.sha256(
        json.dumps(body, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    manifest = {
        "id": _SEED_MANIFEST_ID,
        "schema_version": 9,
        "canonicalization_version": 1,
        "manifest_digest": manifest_digest,
        "status": "active",
        "created_at": "2026-07-17T00:00:00Z",
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


def _workspace_fk(local_version: str = "workspace_version") -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", local_version],
        ["identity_workspaces.id", "identity_workspaces.version"],
        name=f"fk_conversation_{local_version}_workspace",
        ondelete="RESTRICT",
    )


def _membership_fk(prefix: str, version: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", prefix, version],
        [
            "workspace_memberships.workspace_id",
            "workspace_memberships.principal_id",
            "workspace_memberships.version",
        ],
        name=f"fk_conversation_{prefix}_membership",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_index("uq_projects_workspace_id_id", "projects", ["workspace_id", "id"], unique=True)
    op.create_index(
        "uq_agent_identities_workspace_id_version",
        "agent_identities",
        ["workspace_id", "id", "version"],
        unique=True,
    )
    op.create_table(
        "threads",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("creator_principal_id", sa.String(36), nullable=False),
        sa.Column("creator_membership_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint("workspace_version >= 1", name="ck_threads_workspace_version"),
        sa.CheckConstraint("creator_membership_version >= 1", name="ck_threads_membership_version"),
        _workspace_fk(),
        _membership_fk("creator_principal_id", "creator_membership_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            name="fk_threads_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_threads"),
    )
    op.create_index("ix_threads_workspace_created", "threads", ["workspace_id", "created_at", "id"])
    op.create_table(
        "thread_versions",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_thread_versions_version"),
        sa.CheckConstraint("length(trim(title)) >= 1", name="ck_thread_versions_title"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_thread_versions_status"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["threads.workspace_id", "threads.id"],
            name="fk_thread_versions_thread",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "thread_id", "version", name="pk_thread_versions"),
    )
    op.create_index(
        "ix_thread_versions_current", "thread_versions", ["workspace_id", "thread_id", "version"]
    )
    op.create_table(
        "attachments",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("owner_principal_id", sa.String(36), nullable=False),
        sa.Column("owner_membership_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint("owner_membership_version >= 1", name="ck_attachments_membership"),
        sa.CheckConstraint("byte_size >= 0", name="ck_attachments_size"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_attachments_digest"),
        _membership_fk("owner_principal_id", "owner_membership_version"),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_attachments"),
    )
    op.create_index(
        "ix_attachments_owner", "attachments", ["workspace_id", "owner_principal_id", "created_at"]
    )
    op.create_table(
        "messages",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("producing_run_id", sa.String(36), nullable=True),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_principal_id", sa.String(36), nullable=True),
        sa.Column("author_membership_version", sa.Integer(), nullable=True),
        sa.Column("author_agent_id", sa.String(36), nullable=True),
        sa.Column("author_agent_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_messages_sequence"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_messages_content_digest"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_messages_request_digest"),
        sa.CheckConstraint(
            "(author_kind = 'principal' AND author_principal_id IS NOT NULL "
            "AND author_membership_version IS NOT NULL AND author_agent_id IS NULL "
            "AND author_agent_version IS NULL) OR "
            "(author_kind = 'agent' AND author_agent_id IS NOT NULL "
            "AND author_agent_version IS NOT NULL AND author_principal_id IS NULL "
            "AND author_membership_version IS NULL) OR "
            "(author_kind = 'system' AND author_principal_id IS NULL "
            "AND author_membership_version IS NULL AND author_agent_id IS NULL "
            "AND author_agent_version IS NULL)",
            name="ck_messages_author",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["threads.workspace_id", "threads.id"],
            name="fk_messages_thread",
            ondelete="RESTRICT",
        ),
        _membership_fk("author_principal_id", "author_membership_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "author_agent_id", "author_agent_version"],
            ["agent_identities.workspace_id", "agent_identities.id", "agent_identities.version"],
            name="fk_messages_agent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "thread_id", "sequence", name="pk_messages"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_messages_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "thread_id", "idempotency_key", name="uq_messages_idempotency"
        ),
    )
    op.create_index("ix_messages_page", "messages", ["workspace_id", "thread_id", "sequence"])
    op.create_table(
        "message_attachments",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("message_sequence", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("message_sequence >= 1", name="ck_message_attachments_sequence"),
        sa.CheckConstraint("ordinal >= 1", name="ck_message_attachments_ordinal"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "message_sequence"],
            ["messages.workspace_id", "messages.thread_id", "messages.sequence"],
            name="fk_message_attachments_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attachment_id"],
            ["attachments.workspace_id", "attachments.id"],
            name="fk_message_attachments_attachment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "thread_id",
            "message_sequence",
            "ordinal",
            name="pk_message_attachments",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "message_sequence",
            "attachment_id",
            name="uq_message_attachment",
        ),
    )
    op.create_table(
        "agent_runs",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("message_sequence", sa.Integer(), nullable=False),
        sa.Column("requester_principal_id", sa.String(36), nullable=False),
        sa.Column("requester_membership_version", sa.Integer(), nullable=False),
        sa.Column("authorization_snapshot_id", sa.String(36), nullable=False),
        sa.Column("authorization_snapshot_digest", sa.String(64), nullable=False),
        sa.Column("provider_binding_id", sa.String(36), nullable=False),
        sa.Column("provider_binding_version", sa.Integer(), nullable=False),
        sa.Column("provider_binding_digest", sa.String(64), nullable=False),
        sa.Column("canonical_request_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=True),
        sa.Column("root_run_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint("message_sequence >= 1", name="ck_agent_runs_message_sequence"),
        sa.CheckConstraint("requester_membership_version >= 1", name="ck_agent_runs_membership"),
        sa.CheckConstraint("provider_binding_version >= 1", name="ck_agent_runs_provider_version"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_agent_runs_request_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "message_sequence"],
            ["messages.workspace_id", "messages.thread_id", "messages.sequence"],
            name="fk_agent_runs_message",
            ondelete="RESTRICT",
        ),
        _membership_fk("requester_principal_id", "requester_membership_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_agent_runs_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "root_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_agent_runs_root",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_agent_runs"),
        sa.UniqueConstraint("workspace_id", "thread_id", "id", name="uq_agent_runs_thread"),
        sa.UniqueConstraint(
            "workspace_id", "thread_id", "idempotency_key", name="uq_agent_runs_idempotency"
        ),
    )
    op.create_index(
        "ix_agent_runs_thread", "agent_runs", ["workspace_id", "thread_id", "created_at"]
    )
    op.create_table(
        "agent_run_events",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("handle_id", sa.String(36), nullable=False),
        sa.Column("timestamp", sa.String(40), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.String(512), nullable=True),
        sa.Column("tool_call_id", sa.String(512), nullable=True),
        sa.Column("effect_authorization_decision_id", sa.String(36), nullable=True),
        sa.Column("effect_authorization_decision_digest", sa.String(64), nullable=True),
        sa.Column("previous_event_digest", sa.String(64), nullable=False),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_run_events_sequence"),
        sa.CheckConstraint("length(previous_event_digest) = 64", name="ck_run_event_previous"),
        sa.CheckConstraint("length(event_digest) = 64", name="ck_run_event_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.thread_id", "agent_runs.id"],
            name="fk_agent_run_events_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "run_id", "sequence", name="pk_agent_run_events"),
        sa.UniqueConstraint(
            "workspace_id", "run_id", "sequence", "event_digest", name="uq_agent_run_event_digest"
        ),
    )
    op.create_index(
        "ix_agent_run_events_page", "agent_run_events", ["workspace_id", "run_id", "sequence"]
    )
    op.create_index(
        "uq_agent_run_provider_event",
        "agent_run_events",
        ["workspace_id", "run_id", "provider_event_id"],
        unique=True,
        postgresql_where=sa.text("provider_event_id IS NOT NULL"),
        sqlite_where=sa.text("provider_event_id IS NOT NULL"),
    )
    op.create_table(
        "run_artifacts",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("producing_event_sequence", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("lineage_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.CheckConstraint("producing_event_sequence >= 1", name="ck_run_artifact_event"),
        sa.CheckConstraint("byte_size >= 0", name="ck_run_artifact_size"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_run_artifact_content_digest"),
        sa.CheckConstraint("length(lineage_digest) = 64", name="ck_run_artifact_lineage_digest"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "producing_event_sequence"],
            [
                "agent_run_events.workspace_id",
                "agent_run_events.run_id",
                "agent_run_events.sequence",
            ],
            name="fk_run_artifacts_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_run_artifacts"),
    )
    op.create_index(
        "ix_run_artifacts_run", "run_artifacts", ["workspace_id", "run_id", "created_at"]
    )
    op.create_table(
        "run_artifact_lineage",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("parent_artifact_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("lineage_digest", sa.String(64), nullable=False),
        sa.CheckConstraint("artifact_id <> parent_artifact_id", name="ck_artifact_lineage_no_self"),
        sa.CheckConstraint("ordinal >= 1", name="ck_artifact_lineage_ordinal"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "artifact_id"],
            ["run_artifacts.workspace_id", "run_artifacts.id"],
            name="fk_artifact_lineage_child",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_artifact_id"],
            ["run_artifacts.workspace_id", "run_artifacts.id"],
            name="fk_artifact_lineage_parent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "artifact_id", "parent_artifact_id", name="pk_run_artifact_lineage"
        ),
        sa.UniqueConstraint(
            "workspace_id", "artifact_id", "ordinal", name="uq_artifact_lineage_ordinal"
        ),
    )
    op.create_index(
        "ix_artifact_lineage_parent",
        "run_artifact_lineage",
        ["workspace_id", "parent_artifact_id", "artifact_id"],
    )
    for table_name in sorted(_NEW_FAMILIES):
        create_immutable_triggers(table_name, table_name.replace("_", " "))
    _insert_manifest(op.get_bind())


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("conversation_downgrade_requires_online_history_check")
    bind = op.get_bind()
    if bind.execute(_HISTORY_COUNT_SQL).scalar_one():
        raise RuntimeError("conversation_history_present")
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
    for table_name in sorted(_NEW_FAMILIES, reverse=True):
        drop_immutable_triggers(table_name)
    op.drop_index("ix_artifact_lineage_parent", table_name="run_artifact_lineage")
    op.drop_table("run_artifact_lineage")
    op.drop_index("ix_run_artifacts_run", table_name="run_artifacts")
    op.drop_table("run_artifacts")
    op.drop_index("uq_agent_run_provider_event", table_name="agent_run_events")
    op.drop_index("ix_agent_run_events_page", table_name="agent_run_events")
    op.drop_table("agent_run_events")
    op.drop_index("ix_agent_runs_thread", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_table("message_attachments")
    op.drop_index("ix_messages_page", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_attachments_owner", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_thread_versions_current", table_name="thread_versions")
    op.drop_table("thread_versions")
    op.drop_index("ix_threads_workspace_created", table_name="threads")
    op.drop_table("threads")
    op.drop_index("uq_agent_identities_workspace_id_version", table_name="agent_identities")
    op.drop_index("uq_projects_workspace_id_id", table_name="projects")
