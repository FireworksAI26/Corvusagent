"""Add registry history and authority-root manifest persistence.

Revision ID: m1_004_registry_manifest
Revises: m1_003_authority_core
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corvus.infrastructure.migrations.trigger_ddl import (
    create_immutable_triggers,
    drop_immutable_triggers,
)

revision: str = "m1_004_registry_manifest"
down_revision: str | None = "m1_003_authority_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_MANIFEST_ID = "00000000-0000-4000-8000-000000000004"
_FAMILY_NAMES = (
    "audit_anchor_recovery_checkpoints",
    "audit_receipts",
    "audit_result_bindings",
    "authority_commit_intents",
    "authority_epoch_credentials",
    "authority_registries",
    "authority_registry_freshness_proofs",
    "authority_registry_trust_states",
    "authority_registry_verifier_keys",
    "authority_state_root_manifests",
    "authority_trust_anchors",
    "authorization_decision_snapshots",
    "deployment_instance_leases",
    "deployment_instances",
    "projects",
    "workspace_authorities",
)


def _immutable(table_name: str, label: str) -> None:
    create_immutable_triggers(table_name, label)


def _seed_families() -> list[dict[str, object]]:
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


def _seed_manifest_digest(families: list[dict[str, object]]) -> str:
    payload = {
        "schema_version": 1,
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
        "authority_registries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_digest", sa.String(length=64), nullable=False),
        sa.Column("offline_root_public_key_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_authority_registries"),
        sa.UniqueConstraint("endpoint_digest", name="uq_authority_registries_endpoint_digest"),
    )

    op.create_table(
        "authority_registry_verifier_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("registry_id", sa.String(length=36), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("algorithm", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.String(length=40), nullable=False),
        sa.Column("valid_until", sa.String(length=40), nullable=True),
        sa.Column("predecessor_digest", sa.String(length=64), nullable=True),
        sa.Column("predecessor_signature", sa.Text(), nullable=True),
        sa.Column("offline_root_recovery_signature", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.String(length=40), nullable=True),
        sa.Column("compromise_effective_at", sa.String(length=40), nullable=True),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("key_version >= 1", name="ck_registry_verifier_key_version_positive"),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["authority_registries.id"],
            name="fk_registry_verifier_keys_registry_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_registry_verifier_keys"),
        sa.UniqueConstraint(
            "registry_id",
            "key_version",
            name="uq_registry_verifier_keys_registry_version",
        ),
    )

    op.create_table(
        "authority_registry_trust_states",
        sa.Column("registry_id", sa.String(length=36), nullable=False),
        sa.Column("metadata_version", sa.Integer(), nullable=False),
        sa.Column("latest_verifier_key_version", sa.Integer(), nullable=False),
        sa.Column("complete_history_head_digest", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("metadata_version >= 1", name="ck_registry_trust_metadata_positive"),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["authority_registries.id"],
            name="fk_registry_trust_states_registry_id",
        ),
        sa.ForeignKeyConstraint(
            ["registry_id", "latest_verifier_key_version"],
            [
                "authority_registry_verifier_keys.registry_id",
                "authority_registry_verifier_keys.key_version",
            ],
            name="fk_registry_trust_states_latest_verifier",
        ),
        sa.PrimaryKeyConstraint(
            "registry_id",
            "metadata_version",
            name="pk_authority_registry_trust_states",
        ),
    )

    op.create_table(
        "authority_registry_freshness_proofs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("registry_id", sa.String(length=36), nullable=False),
        sa.Column("trust_state_metadata_version", sa.Integer(), nullable=False),
        sa.Column("registry_sequence", sa.Integer(), nullable=False),
        sa.Column("challenge_nonce_digest", sa.String(length=64), nullable=False),
        sa.Column("verifier_key_version_id", sa.String(length=36), nullable=False),
        sa.Column("issued_at", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "registry_sequence >= 1", name="ck_registry_freshness_sequence_positive"
        ),
        sa.ForeignKeyConstraint(
            ["registry_id", "trust_state_metadata_version"],
            [
                "authority_registry_trust_states.registry_id",
                "authority_registry_trust_states.metadata_version",
            ],
            name="fk_registry_freshness_trust_state",
        ),
        sa.ForeignKeyConstraint(
            ["verifier_key_version_id"],
            ["authority_registry_verifier_keys.id"],
            name="fk_registry_freshness_verifier_key_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_authority_registry_freshness_proofs"),
        sa.UniqueConstraint(
            "registry_id",
            "registry_sequence",
            name="uq_registry_freshness_registry_sequence",
        ),
        sa.UniqueConstraint(
            "registry_id",
            "challenge_nonce_digest",
            name="uq_registry_freshness_registry_nonce",
        ),
    )

    op.create_table(
        "authority_state_root_manifests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("canonicalization_version", sa.Integer(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("schema_version >= 1", name="ck_authority_manifest_schema_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_authority_state_root_manifests"),
        sa.UniqueConstraint("manifest_digest", name="uq_authority_manifest_digest"),
    )

    op.create_table(
        "authority_state_root_leaf_families",
        sa.Column("manifest_version_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("family_name", sa.String(length=200), nullable=False),
        sa.Column("coverage_kind", sa.String(length=32), nullable=False),
        sa.Column("external_proof_kind", sa.String(length=200), nullable=True),
        sa.Column("canonicalization_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_authority_leaf_family_ordinal_positive"),
        sa.ForeignKeyConstraint(
            ["manifest_version_id"],
            ["authority_state_root_manifests.id"],
            name="fk_authority_leaf_families_manifest_id",
        ),
        sa.PrimaryKeyConstraint(
            "manifest_version_id",
            "ordinal",
            name="pk_authority_state_root_leaf_families",
        ),
        sa.UniqueConstraint(
            "manifest_version_id",
            "family_name",
            name="uq_authority_leaf_families_manifest_name",
        ),
    )

    op.create_table(
        "authority_state_root_leaf_commitments",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("manifest_version_id", sa.String(length=36), nullable=False),
        sa.Column("authority_generation", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("family_name", sa.String(length=200), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("leaf_digest", sa.String(length=64), nullable=False),
        sa.Column("external_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "authority_generation >= 0",
            name="ck_authority_leaf_commitment_generation_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_version_id", "ordinal"],
            [
                "authority_state_root_leaf_families.manifest_version_id",
                "authority_state_root_leaf_families.ordinal",
            ],
            name="fk_authority_leaf_commitments_family",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "authority_generation",
            "ordinal",
            name="pk_authority_state_root_leaf_commitments",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "authority_generation",
            "family_name",
            name="uq_authority_leaf_commitments_workspace_generation_family",
        ),
    )

    families = _seed_families()
    manifest_digest = _seed_manifest_digest(families)
    manifest_payload = {
        "id": _SEED_MANIFEST_ID,
        "schema_version": 1,
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

    for table_name, label in (
        ("authority_registries", "authority registries"),
        ("authority_registry_verifier_keys", "authority registry verifier keys"),
        ("authority_registry_trust_states", "authority registry trust states"),
        ("authority_registry_freshness_proofs", "authority registry freshness proofs"),
        ("authority_state_root_manifests", "authority state-root manifests"),
        ("authority_state_root_leaf_families", "authority state-root leaf families"),
        ("authority_state_root_leaf_commitments", "authority state-root leaf commitments"),
    ):
        _immutable(table_name, label)


def downgrade() -> None:
    for table_name in (
        "authority_state_root_leaf_commitments",
        "authority_state_root_leaf_families",
        "authority_state_root_manifests",
        "authority_registry_freshness_proofs",
        "authority_registry_trust_states",
        "authority_registry_verifier_keys",
        "authority_registries",
    ):
        drop_immutable_triggers(table_name)
        op.drop_table(table_name)
