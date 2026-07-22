from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from corvus.security import atomic_write, sha256_file

if TYPE_CHECKING:
    from sqlalchemy import MetaData

LEGACY_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 2
CLASSIFICATION_SNAPSHOT_ATTEMPTS = 3
M005_001_MIGRATION = "M005-001"
M1_PROJECT_REVISION = "m1_001_projects"
M1_AUDIT_REVISION = "m1_002_scoped_audit"
M1_AUTHORITY_REVISION = "m1_003_authority_core"
M1_REGISTRY_REVISION = "m1_004_registry_manifest"
M1_AUTHORIZATION_INPUT_REVISION = "m1_005_authorization_inputs"
M1_HANDOFF_REVISION = "m1_006_handoff_restore"
M1_IDENTITY_SCOPE_REVISION = "m1_007_identity_scope"
M1_ROOT_MANIFEST_REVISION = "m1_008_non_circular_root_manifest"
M1_AUDIT_PROOF_MANIFEST_REVISION = "m1_009_audit_external_proofs"
M2_IDENTITY_CONTINUITY_REVISION = "m2_001_identity_continuity"
M2_OAUTH_SESSIONS_REVISION = "m2_001a_oauth_sessions"
M2_WORKSPACE_SYNC_REVISION = "m2_002_workspace_sync"
M2_CONVERSATIONS_REVISION = "m2_003_conversations"
SCHEMA_METADATA_TABLE = "corvus_schema"
V1_REQUIRED_TABLES = frozenset(
    {
        "deliveries",
        "memories",
        "run_events",
        "skill_versions",
    }
)
M005_001_REQUIRED_TABLES = frozenset({"external_contents", "context_envelopes"})
M1_ADDITIVE_REQUIRED_TABLES = frozenset({"alembic_version", "projects"})
M1_AUDIT_REQUIRED_TABLES = frozenset(
    {
        "authorization_decision_snapshots",
        "audit_receipts",
        "audit_result_bindings",
        "audit_anchor_recovery_checkpoints",
    }
)
M1_AUTHORITY_REQUIRED_TABLES = frozenset(
    {
        "deployment_profiles",
        "deployment_instances",
        "authority_epoch_credentials",
        "authority_trust_anchors",
        "deployment_instance_leases",
        "workspace_authorities",
        "authority_commit_intents",
    }
)
M1_REGISTRY_REQUIRED_TABLES = frozenset(
    {
        "authority_registries",
        "authority_registry_verifier_keys",
        "authority_registry_trust_states",
        "authority_registry_freshness_proofs",
        "authority_state_root_manifests",
        "authority_state_root_leaf_families",
        "authority_state_root_leaf_commitments",
    }
)
M1_AUTHORIZATION_INPUT_REQUIRED_TABLES = frozenset(
    {
        "audience_policy_snapshots",
        "access_bundles",
        "capability_grants",
        "agent_grants",
        "delegation_grants",
        "workspace_signing_key_versions",
        "idempotency_envelopes",
    }
)
M1_HANDOFF_REQUIRED_TABLES = frozenset(
    {
        "authority_close_certificates",
        "authority_handoffs",
        "authority_handoff_activations",
        "restore_validation_receipts",
    }
)
M1_IDENTITY_SCOPE_REQUIRED_TABLES = frozenset(
    {"identity_workspaces", "principals", "workspace_memberships", "agent_identities", "scopes"}
)
M2_IDENTITY_CONTINUITY_REQUIRED_TABLES = frozenset(
    {"accounts", "external_identities", "device_registrations", "session_records"}
)
M2_OAUTH_SESSIONS_REQUIRED_TABLES = frozenset(
    {
        "oauth_transactions",
        "web_session_bindings",
        "account_onboarding_versions",
        "identity_idempotency",
    }
)
M2_WORKSPACE_SYNC_REQUIRED_TABLES = frozenset(
    {
        "device_sync_acknowledgements",
        "outbox_events",
        "platform_idempotency",
        "workspace_changes",
        "workspace_sync_heads",
    }
)
M2_CONVERSATIONS_REQUIRED_TABLES = frozenset(
    {
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
)
M1_REGISTRY_V1_AUTHORITY_FAMILY_NAMES = frozenset(
    {
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
    }
)
M1_AUTHORIZATION_INPUT_V2_FAMILY_NAMES = frozenset(
    {
        *M1_REGISTRY_V1_AUTHORITY_FAMILY_NAMES,
        "access_bundles",
        "agent_grants",
        "audience_policy_snapshots",
        "capability_grants",
        "delegation_grants",
        "idempotency_envelopes",
        "workspace_signing_key_versions",
    }
)
M1_HANDOFF_V3_FAMILY_NAMES = frozenset(
    {
        *M1_AUTHORIZATION_INPUT_V2_FAMILY_NAMES,
        "authority_close_certificates",
        "authority_handoff_activations",
        "authority_handoffs",
        "restore_validation_receipts",
    }
)
M1_IDENTITY_SCOPE_V4_AUTHORITY_FAMILY_NAMES = frozenset(
    {
        *M1_HANDOFF_V3_FAMILY_NAMES,
        "agent_identities",
        "identity_workspaces",
        "principals",
        "scopes",
        "workspace_memberships",
    }
)
M1_NON_CIRCULAR_AUTHORITY_FAMILY_NAMES = frozenset(
    M1_IDENTITY_SCOPE_V4_AUTHORITY_FAMILY_NAMES
    - {
        "audit_anchor_recovery_checkpoints",
        "audit_result_bindings",
    }
)
M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES = M1_IDENTITY_SCOPE_V4_AUTHORITY_FAMILY_NAMES
M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES = frozenset(
    {
        *M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES,
        "accounts",
        "device_registrations",
        "external_identities",
        "session_records",
    }
)
M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES = frozenset(
    {
        *M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES,
        *M2_WORKSPACE_SYNC_REQUIRED_TABLES,
    }
)
M2_CONVERSATIONS_V9_AUTHORITY_FAMILY_NAMES = frozenset(
    {
        *M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES,
        *M2_CONVERSATIONS_REQUIRED_TABLES,
    }
)
# Compatibility name retained for existing callers; this is always the active manifest family set.
M1_AUTHORITY_FAMILY_NAMES = M2_CONVERSATIONS_V9_AUTHORITY_FAMILY_NAMES
M005_001_APPEND_ONLY_TRIGGERS = frozenset(
    {
        "external_contents_no_delete",
        "external_contents_no_update",
        "context_envelopes_no_delete",
        "context_envelopes_no_update",
    }
)
M1_AUDIT_TRIGGERS = frozenset(
    {
        "authorization_decision_snapshots_no_delete",
        "authorization_decision_snapshots_no_update",
        "audit_receipts_no_delete",
        "audit_receipts_no_update",
        "audit_result_bindings_no_delete",
        "audit_result_bindings_no_update",
        "audit_anchor_recovery_checkpoints_no_delete",
    }
)
M1_AUTHORITY_TRIGGERS = frozenset(
    {
        "deployment_profiles_no_delete",
        "deployment_profiles_no_update",
        "deployment_instances_no_delete",
        "authority_epoch_credentials_no_delete",
        "authority_trust_anchors_no_delete",
        "deployment_instance_leases_no_delete",
        "workspace_authorities_no_delete",
        "authority_commit_intents_no_delete",
    }
)
M1_AUTHORITY_REQUIRED_INDEXES = frozenset(
    {
        "uq_deployment_instance_leases_active_workspace_epoch",
        "uq_authority_commit_intents_inflight_workspace",
    }
)
M1_REGISTRY_TRIGGERS = frozenset(
    {
        f"{table_name}_{operation}"
        for table_name in M1_REGISTRY_REQUIRED_TABLES
        for operation in ("no_delete", "no_update")
    }
)
M1_AUTHORIZATION_INPUT_TRIGGERS = frozenset(
    {
        "audience_policy_snapshots_no_delete",
        "audience_policy_snapshots_no_update",
        "access_bundles_no_delete",
        "access_bundles_no_update",
        "capability_grants_no_delete",
        "capability_grants_no_update",
        "agent_grants_no_delete",
        "agent_grants_no_update",
        "delegation_grants_no_delete",
        "delegation_grants_no_update",
        "workspace_signing_key_versions_no_delete",
        "workspace_signing_key_versions_no_update",
        "idempotency_envelopes_no_delete",
    }
)
M1_HANDOFF_TRIGGERS = frozenset(
    {
        "authority_close_certificates_no_delete",
        "authority_close_certificates_no_update",
        "authority_handoffs_no_delete",
        "authority_handoff_activations_no_delete",
        "authority_handoff_activations_no_update",
        "restore_validation_receipts_no_delete",
        "restore_validation_receipts_no_update",
    }
)
M1_IDENTITY_SCOPE_TRIGGERS = frozenset(
    {
        f"{table_name}_{operation}"
        for table_name in M1_IDENTITY_SCOPE_REQUIRED_TABLES
        for operation in ("no_delete", "no_update")
    }
)
M2_IDENTITY_CONTINUITY_TRIGGERS = frozenset(
    {
        f"{table_name}_{operation}"
        for table_name in M2_IDENTITY_CONTINUITY_REQUIRED_TABLES
        for operation in ("no_delete", "no_update")
    }
)
M2_OAUTH_SESSIONS_TRIGGERS = frozenset(
    {
        f"{table_name}_{operation}"
        for table_name in (
            "web_session_bindings",
            "account_onboarding_versions",
            "identity_idempotency",
        )
        for operation in ("no_delete", "no_update")
    }
)
M2_WORKSPACE_SYNC_TRIGGERS = frozenset(
    {
        "workspace_sync_heads_no_delete",
        *(
            f"{table_name}_{operation}"
            for table_name in (
                "device_sync_acknowledgements",
                "outbox_events",
                "platform_idempotency",
                "workspace_changes",
            )
            for operation in ("no_delete", "no_update")
        ),
    }
)
M2_CONVERSATIONS_TRIGGERS = frozenset(
    {
        f"{table_name}_{operation}"
        for table_name in M2_CONVERSATIONS_REQUIRED_TABLES
        for operation in ("no_delete", "no_update")
    }
)
M2_CONVERSATIONS_REQUIRED_INDEXES = frozenset(
    {
        "ix_agent_run_events_page",
        "ix_agent_runs_thread",
        "ix_artifact_lineage_parent",
        "ix_attachments_owner",
        "ix_messages_page",
        "ix_run_artifacts_run",
        "ix_thread_versions_current",
        "ix_threads_workspace_created",
        "uq_agent_identities_workspace_id_version",
        "uq_agent_run_provider_event",
        "uq_projects_workspace_id_id",
    }
)
V1_REQUIRED_COLUMNS = {
    "deliveries": frozenset(
        {
            "id",
            "run_id",
            "bundle_json",
            "approval_json",
            "checkpoint_json",
            "status",
            "created_at",
        }
    ),
    "memories": frozenset(
        {
            "id",
            "project_id",
            "identity_id",
            "kind",
            "content",
            "source",
            "confidence",
            "pinned",
            "expires_at",
            "created_at",
        }
    ),
    "run_events": frozenset(
        {
            "id",
            "run_id",
            "sequence",
            "event_type",
            "phase",
            "payload_json",
            "previous_hash",
            "event_hash",
            "created_at",
        }
    ),
    "skill_versions": frozenset(
        {
            "id",
            "skill_name",
            "version",
            "content",
            "permissions_json",
            "evaluation_json",
            "status",
            "created_at",
        }
    ),
}
M005_001_REQUIRED_COLUMNS = {
    "external_contents": frozenset(
        {
            "id",
            "owner_kind",
            "owner_id",
            "origin",
            "source_locator_digest",
            "content_digest",
            "trust_class",
            "content_json",
            "provenance_json",
            "created_at",
        }
    ),
    "context_envelopes": frozenset(
        {
            "id",
            "owner_kind",
            "owner_id",
            "system_instruction_digest",
            "trusted_content_ids_json",
            "untrusted_content_ids_json",
            "firewall_policy_digest",
            "output_digest",
            "created_at",
        }
    ),
}
M1_ADDITIVE_REQUIRED_COLUMNS = {
    "alembic_version": frozenset({"version_num"}),
    "projects": frozenset(
        {
            "id",
            "workspace_id",
            "name",
            "root_locator",
            "privacy",
            "status",
            "created_at",
            "updated_at",
            "version",
        }
    ),
}
M1_AUDIT_REQUIRED_COLUMNS = {
    "authorization_decision_snapshots": frozenset(
        {
            "id",
            "workspace_id",
            "request_context_id",
            "signing_key_version_id",
            "canonical_digest",
            "created_at",
            "payload_json",
        }
    ),
    "audit_receipts": frozenset(
        {
            "id",
            "workspace_id",
            "workspace_sequence",
            "authorization_snapshot_id",
            "authority_commit_intent_id",
            "previous_hash",
            "receipt_hash",
            "created_at",
            "payload_json",
        }
    ),
    "audit_result_bindings": frozenset(
        {
            "id",
            "workspace_id",
            "audit_receipt_id",
            "audit_receipt_hash",
            "authority_commit_intent_id",
            "binding_hash",
            "created_at",
            "payload_json",
        }
    ),
    "audit_anchor_recovery_checkpoints": frozenset(
        {
            "id",
            "workspace_id",
            "audit_receipt_id",
            "authority_commit_intent_id",
            "prepared_result_digest",
            "state",
            "result_binding_id",
            "updated_at",
            "payload_json",
        }
    ),
}
M1_AUTHORITY_REQUIRED_COLUMNS = {
    "deployment_profiles": frozenset({"id", "version", "created_at", "payload_json"}),
    "deployment_instances": frozenset(
        {
            "id",
            "deployment_profile_id",
            "status",
            "device_binding_digest",
            "activated_at",
            "payload_json",
        }
    ),
    "authority_epoch_credentials": frozenset(
        {
            "id",
            "workspace_id",
            "authority_epoch",
            "deployment_instance_id",
            "status",
            "device_binding_digest",
            "issued_at",
            "payload_json",
        }
    ),
    "authority_trust_anchors": frozenset(
        {"id", "workspace_id", "kind", "status", "created_at", "payload_json"}
    ),
    "deployment_instance_leases": frozenset(
        {
            "id",
            "workspace_id",
            "authority_epoch",
            "deployment_instance_id",
            "lock_name",
            "fencing_token",
            "acquired_at",
            "released_at",
            "payload_json",
        }
    ),
    "workspace_authorities": frozenset(
        {
            "id",
            "workspace_id",
            "deployment_profile_id",
            "deployment_instance_id",
            "epoch",
            "authority_generation",
            "authority_state_root",
            "authority_epoch_credential_id",
            "trust_anchor_id",
            "active_lease_id",
            "state",
            "version",
            "payload_json",
        }
    ),
    "authority_commit_intents": frozenset(
        {
            "id",
            "workspace_id",
            "epoch",
            "deployment_instance_id",
            "prior_generation",
            "next_generation",
            "prior_state_root",
            "mutation_digest",
            "proposed_state_root",
            "state",
            "created_at",
            "payload_json",
        }
    ),
}
M1_REGISTRY_REQUIRED_COLUMNS = {
    "authority_registries": frozenset(
        {
            "id",
            "endpoint_digest",
            "offline_root_public_key_digest",
            "policy_digest",
            "status",
            "created_at",
            "payload_json",
        }
    ),
    "authority_registry_verifier_keys": frozenset(
        {
            "id",
            "registry_id",
            "key_version",
            "algorithm",
            "status",
            "valid_from",
            "valid_until",
            "predecessor_digest",
            "predecessor_signature",
            "offline_root_recovery_signature",
            "revoked_at",
            "compromise_effective_at",
            "canonical_digest",
            "payload_json",
        }
    ),
    "authority_registry_trust_states": frozenset(
        {
            "registry_id",
            "metadata_version",
            "latest_verifier_key_version",
            "complete_history_head_digest",
            "issued_at",
            "expires_at",
            "canonical_digest",
            "payload_json",
        }
    ),
    "authority_registry_freshness_proofs": frozenset(
        {
            "id",
            "registry_id",
            "trust_state_metadata_version",
            "registry_sequence",
            "challenge_nonce_digest",
            "verifier_key_version_id",
            "issued_at",
            "expires_at",
            "payload_json",
        }
    ),
    "authority_state_root_manifests": frozenset(
        {
            "id",
            "schema_version",
            "canonicalization_version",
            "manifest_digest",
            "status",
            "created_at",
            "payload_json",
        }
    ),
    "authority_state_root_leaf_families": frozenset(
        {
            "manifest_version_id",
            "ordinal",
            "family_name",
            "coverage_kind",
            "external_proof_kind",
            "canonicalization_version",
            "payload_json",
        }
    ),
    "authority_state_root_leaf_commitments": frozenset(
        {
            "workspace_id",
            "manifest_version_id",
            "authority_generation",
            "ordinal",
            "family_name",
            "record_version",
            "leaf_digest",
            "external_proof_digest",
            "payload_json",
        }
    ),
}
M1_AUTHORIZATION_INPUT_REQUIRED_COLUMNS = {
    "audience_policy_snapshots": frozenset(
        {
            "id",
            "workspace_id",
            "visibility",
            "policy_version",
            "policy_digest",
            "created_at",
            "payload_json",
        }
    ),
    "access_bundles": frozenset(
        {
            "id",
            "workspace_id",
            "principal_id",
            "scope_kind",
            "scope_id",
            "version",
            "policy_digest",
            "created_at",
            "payload_json",
        }
    ),
    "capability_grants": frozenset(
        {
            "grant_digest",
            "bundle_id",
            "workspace_id",
            "resource_kind",
            "resource_id",
            "action",
            "effect",
            "created_at",
            "payload_json",
        }
    ),
    "agent_grants": frozenset(
        {
            "id",
            "workspace_id",
            "agent_id",
            "capability_bundle_id",
            "autonomy_level",
            "created_at",
            "payload_json",
        }
    ),
    "delegation_grants": frozenset(
        {
            "id",
            "workspace_id",
            "parent_agent_grant_id",
            "child_agent_id",
            "expires_at",
            "payload_json",
        }
    ),
    "workspace_signing_key_versions": frozenset(
        {
            "id",
            "workspace_id",
            "key_epoch",
            "status",
            "valid_from",
            "valid_until",
            "predecessor_digest",
            "canonical_digest",
            "created_at",
            "payload_json",
        }
    ),
    "idempotency_envelopes": frozenset(
        {
            "id",
            "workspace_id",
            "requester_id",
            "transport_principal_id",
            "agent_id",
            "agent_grant_id",
            "operation",
            "idempotency_key",
            "request_context_digest",
            "payload_digest",
            "status",
            "result_digest",
            "result_ref",
            "created_at",
            "completed_at",
            "payload_json",
        }
    ),
}
M1_HANDOFF_REQUIRED_COLUMNS = {
    "authority_close_certificates": frozenset(
        {
            "id",
            "workspace_id",
            "closed_epoch",
            "source_deployment_instance_id",
            "target_deployment_id",
            "final_authority_generation",
            "final_state_root",
            "epoch_key_disposition",
            "anchor_receipt_digest",
            "externally_anchored_at",
            "canonical_digest",
            "payload_json",
        }
    ),
    "authority_handoffs": frozenset(
        {
            "id",
            "workspace_id",
            "from_epoch",
            "to_epoch",
            "close_certificate_id",
            "state",
            "prepared_at",
            "completed_at",
            "payload_json",
        }
    ),
    "authority_handoff_activations": frozenset(
        {
            "id",
            "workspace_id",
            "target_deployment_instance_id",
            "authority_epoch",
            "source_close_certificate_id",
            "source_close_certificate_digest",
            "authority_epoch_credential_id",
            "activated_at",
            "payload_json",
        }
    ),
    "restore_validation_receipts": frozenset(
        {
            "id",
            "workspace_id",
            "restored_database_digest",
            "observed_epoch",
            "takeover_epoch",
            "decision",
            "validated_at",
            "payload_json",
        }
    ),
}
M1_IDENTITY_SCOPE_REQUIRED_COLUMNS = {
    "identity_workspaces": frozenset(
        {"id", "version", "name", "status", "created_at", "updated_at", "payload_json"}
    ),
    "principals": frozenset(
        {"id", "kind", "external_provider", "external_subject", "created_at", "payload_json"}
    ),
    "workspace_memberships": frozenset(
        {
            "workspace_id",
            "principal_id",
            "version",
            "role",
            "status",
            "created_at",
            "updated_at",
            "payload_json",
        }
    ),
    "agent_identities": frozenset(
        {
            "id",
            "workspace_id",
            "version",
            "name",
            "role",
            "model_route",
            "status",
            "created_at",
            "updated_at",
            "payload_json",
        }
    ),
    "scopes": frozenset(
        {
            "workspace_id",
            "kind",
            "scope_id",
            "parent_scope_kind",
            "parent_scope_id",
            "scope_digest",
            "payload_json",
        }
    ),
}
M2_IDENTITY_CONTINUITY_REQUIRED_COLUMNS = {
    "accounts": frozenset(
        {
            "id",
            "principal_id",
            "normalized_email",
            "experience_kind",
            "status",
            "created_at",
            "updated_at",
            "version",
            "payload_json",
        }
    ),
    "external_identities": frozenset(
        {
            "id",
            "account_id",
            "issuer",
            "subject",
            "normalized_email",
            "email_verified",
            "created_at",
            "payload_json",
        }
    ),
    "device_registrations": frozenset(
        {
            "id",
            "account_id",
            "version",
            "name",
            "public_key_digest",
            "status",
            "revoked_at",
            "created_at",
            "updated_at",
            "payload_json",
        }
    ),
    "session_records": frozenset(
        {
            "id",
            "account_id",
            "device_id",
            "device_version",
            "version",
            "token_digest",
            "predecessor_digest",
            "status",
            "issued_at",
            "expires_at",
            "revoked_at",
            "payload_json",
        }
    ),
}
M2_OAUTH_SESSIONS_REQUIRED_COLUMNS = {
    "oauth_transactions": frozenset(
        {
            "id",
            "state_digest",
            "nonce_digest",
            "redirect_uri",
            "encrypted_pkce_verifier",
            "created_at",
            "expires_at",
            "consumed_at",
            "version",
        }
    ),
    "web_session_bindings": frozenset(
        {"session_id", "session_version", "csrf_digest", "created_at"}
    ),
    "account_onboarding_versions": frozenset(
        {"account_id", "version", "experience_kind", "updated_at", "payload_json"}
    ),
    "identity_idempotency": frozenset(
        {
            "account_id",
            "operation",
            "idempotency_key",
            "request_digest",
            "result_json",
            "created_at",
        }
    ),
}
M2_WORKSPACE_SYNC_REQUIRED_COLUMNS = {
    "workspace_sync_heads": frozenset(
        {
            "workspace_id",
            "workspace_version",
            "current_sequence",
            "retention_floor",
            "chain_digest",
            "version",
            "updated_at",
        }
    ),
    "workspace_changes": frozenset(
        {
            "workspace_id",
            "workspace_version",
            "sequence",
            "previous_digest",
            "change_digest",
            "kind",
            "operation",
            "entity_id",
            "entity_version",
            "payload_json",
            "account_id",
            "principal_id",
            "membership_version",
            "device_id",
            "device_version",
            "created_at",
        }
    ),
    "outbox_events": frozenset(
        {
            "workspace_id",
            "sequence",
            "change_digest",
            "event_kind",
            "payload_json",
            "created_at",
        }
    ),
    "device_sync_acknowledgements": frozenset(
        {
            "workspace_id",
            "workspace_version",
            "device_id",
            "version",
            "account_id",
            "principal_id",
            "membership_version",
            "device_version",
            "acknowledged_sequence",
            "created_at",
        }
    ),
    "platform_idempotency": frozenset(
        {
            "account_id",
            "principal_id",
            "scope_key",
            "workspace_id",
            "workspace_version",
            "membership_version",
            "device_id",
            "device_version",
            "operation",
            "idempotency_key",
            "request_digest",
            "result_json",
            "created_at",
        }
    ),
}
M2_CONVERSATIONS_REQUIRED_COLUMNS = {
    "threads": frozenset(
        {
            "workspace_id",
            "id",
            "workspace_version",
            "project_id",
            "creator_principal_id",
            "creator_membership_version",
            "created_at",
        }
    ),
    "thread_versions": frozenset(
        {"workspace_id", "thread_id", "version", "title", "status", "updated_at"}
    ),
    "attachments": frozenset(
        {
            "workspace_id",
            "id",
            "owner_principal_id",
            "owner_membership_version",
            "display_name",
            "media_type",
            "byte_size",
            "content_digest",
            "metadata_json",
            "created_at",
        }
    ),
    "messages": frozenset(
        {
            "workspace_id",
            "thread_id",
            "sequence",
            "id",
            "content",
            "content_digest",
            "idempotency_key",
            "request_digest",
            "producing_run_id",
            "author_kind",
            "author_principal_id",
            "author_membership_version",
            "author_agent_id",
            "author_agent_version",
            "created_at",
        }
    ),
    "message_attachments": frozenset(
        {"workspace_id", "thread_id", "message_sequence", "attachment_id", "ordinal"}
    ),
    "agent_runs": frozenset(
        {
            "workspace_id",
            "id",
            "thread_id",
            "message_sequence",
            "requester_principal_id",
            "requester_membership_version",
            "authorization_snapshot_id",
            "authorization_snapshot_digest",
            "provider_binding_id",
            "provider_binding_version",
            "provider_binding_digest",
            "canonical_request_digest",
            "idempotency_key",
            "request_digest",
            "parent_run_id",
            "root_run_id",
            "created_at",
        }
    ),
    "agent_run_events": frozenset(
        {
            "workspace_id",
            "thread_id",
            "run_id",
            "sequence",
            "handle_id",
            "timestamp",
            "event_type",
            "payload_json",
            "provider_event_id",
            "tool_call_id",
            "effect_authorization_decision_id",
            "effect_authorization_decision_digest",
            "previous_event_digest",
            "event_digest",
        }
    ),
    "run_artifacts": frozenset(
        {
            "workspace_id",
            "id",
            "run_id",
            "producing_event_sequence",
            "display_name",
            "media_type",
            "byte_size",
            "content_digest",
            "lineage_digest",
            "created_at",
        }
    ),
    "run_artifact_lineage": frozenset(
        {"workspace_id", "artifact_id", "parent_artifact_id", "ordinal", "lineage_digest"}
    ),
}
CURRENT_REQUIRED_COLUMNS = {**V1_REQUIRED_COLUMNS, **M005_001_REQUIRED_COLUMNS}
M1_CURRENT_REQUIRED_COLUMNS = {**CURRENT_REQUIRED_COLUMNS, **M1_ADDITIVE_REQUIRED_COLUMNS}
M1_AUDIT_CURRENT_REQUIRED_COLUMNS = {
    **M1_CURRENT_REQUIRED_COLUMNS,
    **M1_AUDIT_REQUIRED_COLUMNS,
}
M1_AUTHORITY_CURRENT_REQUIRED_COLUMNS = {
    **M1_AUDIT_CURRENT_REQUIRED_COLUMNS,
    **M1_AUTHORITY_REQUIRED_COLUMNS,
}
M1_REGISTRY_CURRENT_REQUIRED_COLUMNS = {
    **M1_AUTHORITY_CURRENT_REQUIRED_COLUMNS,
    **M1_REGISTRY_REQUIRED_COLUMNS,
}
M1_AUTHORIZATION_INPUT_CURRENT_REQUIRED_COLUMNS = {
    **M1_REGISTRY_CURRENT_REQUIRED_COLUMNS,
    **M1_AUTHORIZATION_INPUT_REQUIRED_COLUMNS,
}
M1_HANDOFF_CURRENT_REQUIRED_COLUMNS = {
    **M1_AUTHORIZATION_INPUT_CURRENT_REQUIRED_COLUMNS,
    **M1_HANDOFF_REQUIRED_COLUMNS,
}
M1_IDENTITY_SCOPE_CURRENT_REQUIRED_COLUMNS = {
    **M1_HANDOFF_CURRENT_REQUIRED_COLUMNS,
    **M1_IDENTITY_SCOPE_REQUIRED_COLUMNS,
}
M2_IDENTITY_CONTINUITY_CURRENT_REQUIRED_COLUMNS = {
    **M1_IDENTITY_SCOPE_CURRENT_REQUIRED_COLUMNS,
    **M2_IDENTITY_CONTINUITY_REQUIRED_COLUMNS,
    "identity_workspaces": frozenset(
        {*M1_IDENTITY_SCOPE_REQUIRED_COLUMNS["identity_workspaces"], "workspace_kind"}
    ),
}
M2_OAUTH_SESSIONS_CURRENT_REQUIRED_COLUMNS = {
    **M2_IDENTITY_CONTINUITY_CURRENT_REQUIRED_COLUMNS,
    **M2_OAUTH_SESSIONS_REQUIRED_COLUMNS,
}
M2_WORKSPACE_SYNC_CURRENT_REQUIRED_COLUMNS = {
    **M2_IDENTITY_CONTINUITY_CURRENT_REQUIRED_COLUMNS,
    **{
        key: value
        for key, value in M2_OAUTH_SESSIONS_REQUIRED_COLUMNS.items()
        if key != "identity_idempotency"
    },
    **M2_WORKSPACE_SYNC_REQUIRED_COLUMNS,
}
M2_CONVERSATIONS_CURRENT_REQUIRED_COLUMNS = {
    **M2_WORKSPACE_SYNC_CURRENT_REQUIRED_COLUMNS,
    **M2_CONVERSATIONS_REQUIRED_COLUMNS,
}


class DatabaseState(StrEnum):
    NEW = "new"
    UNSTAMPED_V1 = "complete_unstamped_v1"
    LEGACY_UNSTAMPED = "complete_unstamped_v1"
    MIGRATION_REQUIRED = "m005_001_migration_required"
    CURRENT = "current"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class DatabaseStatus:
    state: DatabaseState
    tables: frozenset[str]
    schema_version: int | None = None
    detail: str = ""
    recovery: str | None = None


@dataclass(frozen=True)
class DatabaseBackupReceipt:
    source_path: Path
    backup_path: Path
    sha256: str
    source_state: DatabaseState


class DatabaseBootstrapError(RuntimeError):
    def __init__(self, status: DatabaseStatus) -> None:
        self.status = status
        recovery = status.recovery or "manual recovery is required"
        super().__init__(
            f"database state {status.state.value} ({status.detail}) requires explicit recovery: "
            f"{recovery}; source was not modified"
        )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _file_identity(path: Path) -> tuple[int, int, bytes]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, path.read_bytes()


def _columns_match(
    connection: sqlite3.Connection,
    expected_columns: dict[str, frozenset[str]],
) -> bool:
    for table, expected in expected_columns.items():
        columns = frozenset(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
        if columns != expected:
            return False
    return True


def _v1_columns_match(connection: sqlite3.Connection) -> bool:
    return _columns_match(connection, V1_REQUIRED_COLUMNS)


def _m005_001_triggers_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M005_001_APPEND_ONLY_TRIGGERS.issubset(triggers)


def _m1_audit_triggers_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M1_AUDIT_TRIGGERS.issubset(triggers)


def _m1_authority_schema_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    indexes = {
        row[0]: " ".join((row[1] or "").lower().split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name"
        )
    }
    lease_sql = indexes.get("uq_deployment_instance_leases_active_workspace_epoch", "")
    commit_sql = indexes.get("uq_authority_commit_intents_inflight_workspace", "")
    return (
        M1_AUTHORITY_TRIGGERS.issubset(triggers)
        and M1_AUTHORITY_REQUIRED_INDEXES.issubset(indexes)
        and all(
            fragment in lease_sql
            for fragment in (
                "create unique index",
                "on deployment_instance_leases",
                "workspace_id, authority_epoch",
                "where released_at is null",
            )
        )
        and all(
            fragment in commit_sql
            for fragment in (
                "create unique index",
                "on authority_commit_intents",
                "workspace_id",
                "where state not in ('anchor_finalized', 'quarantined')",
            )
        )
    )


def _m1_registry_schema_controls_match(
    connection: sqlite3.Connection,
    *,
    latest_schema_version: int,
) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    if not M1_REGISTRY_TRIGGERS.issubset(triggers):
        return False
    manifests = connection.execute(
        "SELECT id, schema_version, canonicalization_version, manifest_digest "
        "FROM authority_state_root_manifests ORDER BY schema_version, id"
    ).fetchall()
    if not manifests or max(int(row[1]) for row in manifests) != latest_schema_version:
        return False
    family_sets = {
        1: M1_REGISTRY_V1_AUTHORITY_FAMILY_NAMES,
        2: M1_AUTHORIZATION_INPUT_V2_FAMILY_NAMES,
        3: M1_HANDOFF_V3_FAMILY_NAMES,
        4: M1_IDENTITY_SCOPE_V4_AUTHORITY_FAMILY_NAMES,
        5: M1_NON_CIRCULAR_AUTHORITY_FAMILY_NAMES,
        6: M1_AUDIT_PROOF_V6_AUTHORITY_FAMILY_NAMES,
        7: M2_IDENTITY_CONTINUITY_V7_AUTHORITY_FAMILY_NAMES,
        8: M2_WORKSPACE_SYNC_V8_AUTHORITY_FAMILY_NAMES,
        9: M2_CONVERSATIONS_V9_AUTHORITY_FAMILY_NAMES,
    }
    for manifest_id, schema_version, canonicalization_version, manifest_digest in manifests:
        expected_families = family_sets.get(int(schema_version))
        if expected_families is None:
            return False
        rows = connection.execute(
            "SELECT ordinal, family_name, coverage_kind, external_proof_kind, "
            "canonicalization_version FROM authority_state_root_leaf_families "
            "WHERE manifest_version_id = ? ORDER BY ordinal",
            (manifest_id,),
        ).fetchall()
        if (
            {row[1] for row in rows} != expected_families
            or [row[0] for row in rows] != list(range(1, len(rows) + 1))
            or any(row[4] != canonicalization_version for row in rows)
            or any((row[2] == "external_proof") != (row[3] is not None) for row in rows)
        ):
            return False
        payload = {
            "schema_version": schema_version,
            "canonicalization_version": canonicalization_version,
            "families": [
                {
                    "ordinal": row[0],
                    "family_name": row[1],
                    "coverage_kind": row[2],
                    "external_proof_kind": row[3],
                    "canonicalization_version": row[4],
                }
                for row in rows
            ],
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != manifest_digest:
            return False
    return True


def _m1_authorization_input_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M1_AUTHORIZATION_INPUT_TRIGGERS.issubset(triggers)


def _m1_handoff_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M1_HANDOFF_TRIGGERS.issubset(triggers)


def _m1_identity_scope_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M1_IDENTITY_SCOPE_TRIGGERS.issubset(triggers)


def _m2_identity_continuity_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    return M2_IDENTITY_CONTINUITY_TRIGGERS.issubset(triggers)


def _m2_oauth_sessions_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    tables = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    )
    required = (
        M2_OAUTH_SESSIONS_TRIGGERS
        if "identity_idempotency" in tables
        else frozenset(
            trigger
            for trigger in M2_OAUTH_SESSIONS_TRIGGERS
            if not trigger.startswith("identity_idempotency_")
        )
    )
    return required.issubset(triggers)


def _m2_workspace_sync_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    account_binding_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'uq_accounts_id_principal'"
    ).fetchone()
    normalized_index = (
        ""
        if account_binding_index is None
        else " ".join(str(account_binding_index[0] or "").casefold().split())
    )
    return M2_WORKSPACE_SYNC_TRIGGERS.issubset(triggers) and all(
        fragment in normalized_index
        for fragment in (
            "create unique index",
            "on accounts",
            "id, principal_id",
        )
    )


def _m2_conversation_controls_match(connection: sqlite3.Connection) -> bool:
    triggers = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    )
    indexes = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' ORDER BY name"
        )
    )
    provider_event_index = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'uq_agent_run_provider_event'"
    ).fetchone()
    normalized_provider_index = (
        ""
        if provider_event_index is None
        else " ".join(str(provider_event_index[0] or "").casefold().split())
    )
    return (
        M2_CONVERSATIONS_TRIGGERS.issubset(triggers)
        and M2_CONVERSATIONS_REQUIRED_INDEXES.issubset(indexes)
        and all(
            fragment in normalized_provider_index
            for fragment in (
                "create unique index uq_agent_run_provider_event",
                "on agent_run_events (workspace_id, run_id, provider_event_id)",
                "where provider_event_id is not null",
            )
        )
    )


@contextmanager
def _classification_path(path: Path) -> Iterator[Path]:
    """Avoid touching live WAL shared-memory bytes while classifying a database."""

    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    for _ in range(CLASSIFICATION_SNAPSHOT_ATTEMPTS):
        if not wal.exists() and not shm.exists():
            yield path
            return
        tracked = tuple(candidate for candidate in (path, wal) if candidate.exists())
        try:
            before = {candidate.name: _file_identity(candidate) for candidate in tracked}
            with tempfile.TemporaryDirectory(prefix="corvus-db-classify-") as temporary_root:
                snapshot = Path(temporary_root) / path.name
                shutil.copyfile(path, snapshot)
                if wal.exists():
                    shutil.copyfile(wal, Path(f"{snapshot}-wal"))
                after = {candidate.name: _file_identity(candidate) for candidate in tracked}
                if after != before:
                    continue
                yield snapshot
                return
        except FileNotFoundError:
            continue
    raise sqlite3.OperationalError("database changed while its read-only snapshot was copied")


def classify_database(path: Path) -> DatabaseStatus:
    """Classify SQLite state without creating the file or executing DDL."""

    if not path.exists() or path.stat().st_size == 0:
        return DatabaseStatus(
            DatabaseState.NEW,
            frozenset(),
            detail="database is missing or empty",
        )
    try:
        with (
            _classification_path(path) as inspected_path,
            closing(_connect_read_only(inspected_path)) as connection,
        ):
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            if tables == V1_REQUIRED_TABLES and _v1_columns_match(connection):
                return DatabaseStatus(
                    DatabaseState.UNSTAMPED_V1,
                    tables,
                    detail="complete V1 schema has no version stamp",
                    recovery=(
                        "create an integrity-checked SHA-256 backup, then explicitly "
                        "stamp/upgrade this V1 database"
                    ),
                )
            stamped_v1_tables = frozenset({*V1_REQUIRED_TABLES, SCHEMA_METADATA_TABLE})
            current_tables = frozenset(
                {*V1_REQUIRED_TABLES, *M005_001_REQUIRED_TABLES, SCHEMA_METADATA_TABLE}
            )
            m1_current_tables = frozenset({*current_tables, *M1_ADDITIVE_REQUIRED_TABLES})
            m1_audit_current_tables = frozenset({*m1_current_tables, *M1_AUDIT_REQUIRED_TABLES})
            m1_authority_current_tables = frozenset(
                {*m1_audit_current_tables, *M1_AUTHORITY_REQUIRED_TABLES}
            )
            m1_registry_current_tables = frozenset(
                {*m1_authority_current_tables, *M1_REGISTRY_REQUIRED_TABLES}
            )
            m1_authorization_input_current_tables = frozenset(
                {*m1_registry_current_tables, *M1_AUTHORIZATION_INPUT_REQUIRED_TABLES}
            )
            m1_handoff_current_tables = frozenset(
                {*m1_authorization_input_current_tables, *M1_HANDOFF_REQUIRED_TABLES}
            )
            m1_identity_scope_current_tables = frozenset(
                {*m1_handoff_current_tables, *M1_IDENTITY_SCOPE_REQUIRED_TABLES}
            )
            m2_identity_continuity_current_tables = frozenset(
                {
                    *m1_identity_scope_current_tables,
                    *M2_IDENTITY_CONTINUITY_REQUIRED_TABLES,
                }
            )
            m2_oauth_sessions_current_tables = frozenset(
                {
                    *m2_identity_continuity_current_tables,
                    *M2_OAUTH_SESSIONS_REQUIRED_TABLES,
                }
            )
            m2_workspace_sync_current_tables = frozenset(
                {
                    *(m2_oauth_sessions_current_tables - {"identity_idempotency"}),
                    *M2_WORKSPACE_SYNC_REQUIRED_TABLES,
                }
            )
            m2_conversations_current_tables = frozenset(
                {
                    *m2_workspace_sync_current_tables,
                    *M2_CONVERSATIONS_REQUIRED_TABLES,
                }
            )
            supported_table_sets = {
                stamped_v1_tables,
                current_tables,
                m1_current_tables,
                m1_audit_current_tables,
                m1_authority_current_tables,
                m1_registry_current_tables,
                m1_authorization_input_current_tables,
                m1_handoff_current_tables,
                m1_identity_scope_current_tables,
                m2_identity_continuity_current_tables,
                m2_oauth_sessions_current_tables,
                m2_workspace_sync_current_tables,
                m2_conversations_current_tables,
            }
            if tables in supported_table_sets:
                if tables == stamped_v1_tables:
                    expected_columns = V1_REQUIRED_COLUMNS
                elif tables == current_tables:
                    expected_columns = CURRENT_REQUIRED_COLUMNS
                elif tables == m1_current_tables:
                    expected_columns = M1_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_audit_current_tables:
                    expected_columns = M1_AUDIT_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_authority_current_tables:
                    expected_columns = M1_AUTHORITY_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_registry_current_tables:
                    expected_columns = M1_REGISTRY_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_authorization_input_current_tables:
                    expected_columns = M1_AUTHORIZATION_INPUT_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_handoff_current_tables:
                    expected_columns = M1_HANDOFF_CURRENT_REQUIRED_COLUMNS
                elif tables == m1_identity_scope_current_tables:
                    expected_columns = M1_IDENTITY_SCOPE_CURRENT_REQUIRED_COLUMNS
                elif tables == m2_identity_continuity_current_tables:
                    expected_columns = M2_IDENTITY_CONTINUITY_CURRENT_REQUIRED_COLUMNS
                elif tables == m2_oauth_sessions_current_tables:
                    expected_columns = M2_OAUTH_SESSIONS_CURRENT_REQUIRED_COLUMNS
                elif tables == m2_workspace_sync_current_tables:
                    expected_columns = M2_WORKSPACE_SYNC_CURRENT_REQUIRED_COLUMNS
                else:
                    expected_columns = M2_CONVERSATIONS_CURRENT_REQUIRED_COLUMNS
                if not _columns_match(connection, expected_columns):
                    return DatabaseStatus(
                        DatabaseState.PARTIAL,
                        tables,
                        detail="database schema is missing required columns",
                        recovery="restore from a digest-verified backup; source was not modified",
                    )
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({SCHEMA_METADATA_TABLE})")
                }
                rows = connection.execute("SELECT schema_version FROM corvus_schema").fetchall()
                if columns == {"schema_version"} and len(rows) == 1:
                    schema_version = rows[0][0]
                    if tables == stamped_v1_tables and schema_version == LEGACY_SCHEMA_VERSION:
                        return DatabaseStatus(
                            DatabaseState.MIGRATION_REQUIRED,
                            tables,
                            schema_version=LEGACY_SCHEMA_VERSION,
                            detail=f"database requires {M005_001_MIGRATION}",
                            recovery=(
                                "create and verify the automatic pre-M005-001 backup, then "
                                "apply the transactional provenance migration"
                            ),
                        )
                    revision_rows = (
                        connection.execute("SELECT version_num FROM alembic_version").fetchall()
                        if "alembic_version" in tables
                        else []
                    )
                    actual_revision = revision_rows[0][0] if len(revision_rows) == 1 else None
                    if tables == m2_conversations_current_tables:
                        expected_revision = M2_CONVERSATIONS_REVISION
                        latest_manifest_schema_version = 9
                    elif tables == m2_workspace_sync_current_tables:
                        expected_revision = M2_WORKSPACE_SYNC_REVISION
                        latest_manifest_schema_version = 8
                    elif tables == m2_oauth_sessions_current_tables:
                        expected_revision = M2_OAUTH_SESSIONS_REVISION
                        latest_manifest_schema_version = 7
                    elif tables == m2_identity_continuity_current_tables:
                        expected_revision = M2_IDENTITY_CONTINUITY_REVISION
                        latest_manifest_schema_version = 7
                    elif tables == m1_current_tables:
                        expected_revision = M1_PROJECT_REVISION
                        latest_manifest_schema_version = 0
                    elif tables == m1_audit_current_tables:
                        expected_revision = M1_AUDIT_REVISION
                        latest_manifest_schema_version = 0
                    elif tables == m1_authority_current_tables:
                        expected_revision = M1_AUTHORITY_REVISION
                        latest_manifest_schema_version = 0
                    elif tables == m1_registry_current_tables:
                        expected_revision = M1_REGISTRY_REVISION
                        latest_manifest_schema_version = 1
                    elif tables == m1_authorization_input_current_tables:
                        expected_revision = M1_AUTHORIZATION_INPUT_REVISION
                        latest_manifest_schema_version = 2
                    elif tables == m1_handoff_current_tables:
                        expected_revision = M1_HANDOFF_REVISION
                        latest_manifest_schema_version = 3
                    elif tables == m1_identity_scope_current_tables:
                        if actual_revision == M1_AUDIT_PROOF_MANIFEST_REVISION:
                            expected_revision = M1_AUDIT_PROOF_MANIFEST_REVISION
                            latest_manifest_schema_version = 6
                        elif actual_revision == M1_ROOT_MANIFEST_REVISION:
                            expected_revision = M1_ROOT_MANIFEST_REVISION
                            latest_manifest_schema_version = 5
                        else:
                            expected_revision = M1_IDENTITY_SCOPE_REVISION
                            latest_manifest_schema_version = 4
                    else:
                        expected_revision = M1_IDENTITY_SCOPE_REVISION
                        latest_manifest_schema_version = 0
                    m1_revision_matches = tables in {
                        stamped_v1_tables,
                        current_tables,
                    } or revision_rows == [(expected_revision,)]
                    audit_triggers_match = tables not in {
                        m1_audit_current_tables,
                        m1_authority_current_tables,
                        m1_registry_current_tables,
                        m1_authorization_input_current_tables,
                        m1_handoff_current_tables,
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_audit_triggers_match(connection)
                    authority_controls_match = tables not in {
                        m1_authority_current_tables,
                        m1_registry_current_tables,
                        m1_authorization_input_current_tables,
                        m1_handoff_current_tables,
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_authority_schema_controls_match(connection)
                    registry_controls_match = tables not in {
                        m1_registry_current_tables,
                        m1_authorization_input_current_tables,
                        m1_handoff_current_tables,
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_registry_schema_controls_match(
                        connection,
                        latest_schema_version=latest_manifest_schema_version,
                    )
                    authorization_input_controls_match = tables not in {
                        m1_authorization_input_current_tables,
                        m1_handoff_current_tables,
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_authorization_input_controls_match(connection)
                    handoff_controls_match = tables not in {
                        m1_handoff_current_tables,
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_handoff_controls_match(connection)
                    identity_scope_controls_match = tables not in {
                        m1_identity_scope_current_tables,
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m1_identity_scope_controls_match(connection)
                    identity_continuity_controls_match = tables not in {
                        m2_identity_continuity_current_tables,
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m2_identity_continuity_controls_match(connection)
                    oauth_sessions_controls_match = tables not in {
                        m2_oauth_sessions_current_tables,
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m2_oauth_sessions_controls_match(connection)
                    workspace_sync_controls_match = tables not in {
                        m2_workspace_sync_current_tables,
                        m2_conversations_current_tables,
                    } or _m2_workspace_sync_controls_match(connection)
                    conversation_controls_match = (
                        tables != m2_conversations_current_tables
                        or _m2_conversation_controls_match(connection)
                    )
                    if (
                        tables
                        in {
                            current_tables,
                            m1_current_tables,
                            m1_audit_current_tables,
                            m1_authority_current_tables,
                            m1_registry_current_tables,
                            m1_authorization_input_current_tables,
                            m1_handoff_current_tables,
                            m1_identity_scope_current_tables,
                            m2_identity_continuity_current_tables,
                            m2_oauth_sessions_current_tables,
                            m2_workspace_sync_current_tables,
                            m2_conversations_current_tables,
                        }
                        and schema_version == CURRENT_SCHEMA_VERSION
                        and _m005_001_triggers_match(connection)
                        and m1_revision_matches
                        and audit_triggers_match
                        and authority_controls_match
                        and registry_controls_match
                        and authorization_input_controls_match
                        and handoff_controls_match
                        and identity_scope_controls_match
                        and identity_continuity_controls_match
                        and oauth_sessions_controls_match
                        and workspace_sync_controls_match
                        and conversation_controls_match
                    ):
                        if tables == m2_conversations_current_tables:
                            detail = "database schema is current with M2 conversations"
                        elif tables == m2_workspace_sync_current_tables:
                            detail = "database schema is current with M2 workspace sync"
                        elif tables == m2_oauth_sessions_current_tables:
                            detail = "database schema is current with M2 OAuth sessions"
                        elif tables == m2_identity_continuity_current_tables:
                            detail = "database schema is current with M2 identity continuity"
                        elif tables == m1_identity_scope_current_tables:
                            detail = (
                                "database schema is current with M1 identity and scope persistence"
                            )
                        elif tables == m1_handoff_current_tables:
                            detail = "database schema is current with M1 handoff persistence"
                        elif tables == m1_authorization_input_current_tables:
                            detail = "database schema is current with M1 authorization inputs"
                        elif tables == m1_registry_current_tables:
                            detail = "database schema is current with M1 registry persistence"
                        elif tables == m1_authority_current_tables:
                            detail = "database schema is current with M1 authority persistence"
                        elif tables == m1_audit_current_tables:
                            detail = "database schema is current with M1 scoped audit persistence"
                        elif tables == m1_current_tables:
                            detail = "database schema is current with M1 project persistence"
                        else:
                            detail = "database schema is current"
                        return DatabaseStatus(
                            DatabaseState.CURRENT,
                            tables,
                            schema_version=CURRENT_SCHEMA_VERSION,
                            detail=detail,
                        )
                    if isinstance(schema_version, int) and schema_version > CURRENT_SCHEMA_VERSION:
                        return DatabaseStatus(
                            DatabaseState.INCOMPATIBLE,
                            tables,
                            schema_version=schema_version,
                            detail=f"schema version {schema_version} is not supported",
                            recovery=(
                                "use a compatible Corvus release or restore from a "
                                "digest-verified backup"
                            ),
                        )
                return DatabaseStatus(
                    DatabaseState.PARTIAL,
                    tables,
                    detail="schema metadata, M005-001 tables, or append-only triggers are incomplete",
                    recovery="restore from a digest-verified backup; source was not modified",
                )
            if not tables:
                return DatabaseStatus(
                    DatabaseState.NEW,
                    tables,
                    detail="database contains no application tables",
                )
            return DatabaseStatus(
                DatabaseState.PARTIAL,
                tables,
                detail="database does not contain the complete V1 schema",
                recovery="restore from a digest-verified backup; source was not modified",
            )
    except (OSError, sqlite3.DatabaseError) as exc:
        return DatabaseStatus(
            DatabaseState.INCOMPATIBLE,
            frozenset(),
            detail=f"database is not readable as supported SQLite: {exc}",
            recovery="use a supported database or restore from a digest-verified backup",
        )


def _require_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise DatabaseBootstrapError(
            DatabaseStatus(
                DatabaseState.INCOMPATIBLE,
                frozenset(),
                detail=f"{label} failed SQLite integrity check",
                recovery="restore from a digest-verified backup",
            )
        )


def m005_001_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.pre-m005-001.bak")


def _create_verified_backup(source: Path, backup: Path) -> str:
    sidecar = backup.with_suffix(f"{backup.suffix}.sha256")
    if backup.exists() or sidecar.exists():
        raise FileExistsError("database backup or digest sidecar already exists")
    backup.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup.with_name(f".{backup.name}.backup-{uuid4().hex}.tmp")
    try:
        with closing(_connect_read_only(source)) as source_connection:
            _require_integrity(source_connection, label="source database")
            if not Path(f"{source}-wal").exists() and not Path(f"{source}-shm").exists():
                shutil.copyfile(source, temporary)
            else:
                with closing(sqlite3.connect(temporary)) as destination_connection:
                    source_connection.backup(destination_connection)
                    _require_integrity(destination_connection, label="database backup")
                    destination_connection.commit()
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        digest = sha256_file(temporary)
        os.replace(temporary, backup)
        atomic_write(sidecar, digest.encode("ascii"))
    finally:
        temporary.unlink(missing_ok=True)
    if sha256_file(backup) != digest:
        raise RuntimeError("database backup digest changed after publication")
    return digest


def _create_m005_001_triggers(connection: sqlite3.Connection) -> None:
    for table_name in sorted(M005_001_REQUIRED_TABLES):
        connection.execute(
            f"CREATE TRIGGER {table_name}_no_update BEFORE UPDATE ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'M005-001 provenance is append-only'); END"
        )
        connection.execute(
            f"CREATE TRIGGER {table_name}_no_delete BEFORE DELETE ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'M005-001 provenance is append-only'); END"
        )


def _create_m005_001_tables(connection: sqlite3.Connection, metadata: MetaData) -> None:
    dialect = sqlite_dialect.dialect()
    for table_name in sorted(M005_001_REQUIRED_TABLES):
        table = metadata.tables[table_name]
        connection.execute(str(CreateTable(table).compile(dialect=dialect)))
    _create_m005_001_triggers(connection)


def _migrate_m005_001(path: Path, metadata: MetaData) -> DatabaseStatus:
    status = classify_database(path)
    if status.state is not DatabaseState.MIGRATION_REQUIRED:
        raise DatabaseBootstrapError(status)
    _create_verified_backup(path, m005_001_backup_path(path))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            expected = frozenset({*V1_REQUIRED_TABLES, SCHEMA_METADATA_TABLE})
            version_rows = connection.execute("SELECT schema_version FROM corvus_schema").fetchall()
            if (
                tables != expected
                or not _v1_columns_match(connection)
                or version_rows != [(LEGACY_SCHEMA_VERSION,)]
            ):
                raise DatabaseBootstrapError(
                    DatabaseStatus(
                        DatabaseState.PARTIAL,
                        tables,
                        detail="schema changed before M005-001 migration",
                        recovery="restore from the verified pre-M005-001 backup",
                    )
                )
            _create_m005_001_tables(connection, metadata)
            connection.execute(
                "UPDATE corvus_schema SET schema_version = ?",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())
    migrated = classify_database(path)
    if migrated.state is not DatabaseState.CURRENT:
        raise RuntimeError(f"M005-001 migration failed classification: {migrated.detail}")
    return migrated


def backup_and_stamp_v1(source: Path, backup: Path) -> DatabaseBackupReceipt:
    """Back up and explicitly stamp a complete legacy V1 database.

    The verified backup and its digest are durable before the source schema is modified.
    """

    status = classify_database(source)
    if status.state is not DatabaseState.UNSTAMPED_V1:
        raise DatabaseBootstrapError(status)
    digest = _create_verified_backup(source, backup)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            if tables != V1_REQUIRED_TABLES or not _v1_columns_match(connection):
                raise DatabaseBootstrapError(
                    DatabaseStatus(
                        DatabaseState.PARTIAL,
                        tables,
                        detail="legacy schema changed before stamp",
                        recovery="restore from the verified backup; source was not stamped",
                    )
                )
            connection.execute("CREATE TABLE corvus_schema (schema_version INTEGER NOT NULL)")
            connection.execute(
                "INSERT INTO corvus_schema (schema_version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION,),
            )
            _create_m005_001_tables(connection, _resolved_metadata(None))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with source.open("rb+") as handle:
        os.fsync(handle.fileno())

    stamped = classify_database(source)
    if stamped.state is not DatabaseState.CURRENT:
        raise RuntimeError(f"stamped database failed classification: {stamped.detail}")
    return DatabaseBackupReceipt(
        source_path=source,
        backup_path=backup,
        sha256=digest,
        source_state=status.state,
    )


def restore_database_backup(backup: Path, destination: Path) -> DatabaseStatus:
    """Verify and atomically publish a database backup without stamping or upgrading it."""

    sidecar = backup.with_suffix(f"{backup.suffix}.sha256")
    if destination.exists():
        raise FileExistsError("restore destination already exists")
    try:
        expected = sidecar.read_text(encoding="ascii").strip().casefold()
    except OSError as exc:
        raise DatabaseBootstrapError(
            DatabaseStatus(
                DatabaseState.INCOMPATIBLE,
                frozenset(),
                detail="database backup digest sidecar is unavailable",
                recovery="supply the original SHA-256 sidecar",
            )
        ) from exc
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise DatabaseBootstrapError(
            DatabaseStatus(
                DatabaseState.INCOMPATIBLE,
                frozenset(),
                detail="database backup digest sidecar is malformed",
                recovery="supply the original SHA-256 sidecar",
            )
        )
    if not backup.is_file() or sha256_file(backup) != expected:
        raise DatabaseBootstrapError(
            DatabaseStatus(
                DatabaseState.INCOMPATIBLE,
                frozenset(),
                detail="database backup SHA-256 digest mismatch",
                recovery="use the untampered backup matching the sidecar",
            )
        )
    backup_status = classify_database(backup)
    if backup_status.state not in {
        DatabaseState.UNSTAMPED_V1,
        DatabaseState.MIGRATION_REQUIRED,
        DatabaseState.CURRENT,
    }:
        raise DatabaseBootstrapError(backup_status)
    with closing(_connect_read_only(backup)) as connection:
        _require_integrity(connection, label="database backup")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore-{uuid4().hex}.tmp")
    try:
        shutil.copyfile(backup, temporary)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        if sha256_file(temporary) != expected:
            raise RuntimeError("restored temporary database digest changed during copy")
        restored_status = classify_database(temporary)
        if restored_status != backup_status:
            raise RuntimeError("restored temporary database classification changed during copy")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return classify_database(destination)


def _resolved_metadata(metadata: MetaData | None) -> MetaData:
    if metadata is not None:
        return metadata
    from corvus.store import Base

    return Base.metadata


def _create_schema(path: Path, metadata: MetaData) -> None:
    dialect = sqlite_dialect.dialect()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in sorted(metadata.sorted_tables, key=lambda item: item.name):
                connection.execute(str(CreateTable(table).compile(dialect=dialect)))
            indexes = sorted(
                (index for table in metadata.tables.values() for index in table.indexes),
                key=lambda item: item.name or "",
            )
            for index in indexes:
                connection.execute(str(CreateIndex(index).compile(dialect=dialect)))
            _create_m005_001_triggers(connection)
            connection.execute(
                f"CREATE TABLE {SCHEMA_METADATA_TABLE} (schema_version INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO corvus_schema (schema_version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def initialize_database(path: Path, metadata: MetaData | None = None) -> DatabaseStatus:
    """Atomically create and stamp a database only when classification is ``new``."""

    initial = classify_database(path)
    if initial.state is not DatabaseState.NEW:
        raise RuntimeError(
            f"database initialization requires state new, found {initial.state.value}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.initialize-{uuid4().hex}.tmp")
    try:
        _create_schema(temporary, _resolved_metadata(metadata))
        created = classify_database(temporary)
        if created.state is not DatabaseState.CURRENT:
            raise RuntimeError(f"initialized database failed classification: {created.detail}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return classify_database(path)


def bootstrap_database(path: Path, metadata: MetaData | None = None) -> DatabaseStatus:
    """Open the current schema or initialize a new database; all other states fail closed."""

    status = classify_database(path)
    if status.state is DatabaseState.NEW:
        return initialize_database(path, metadata)
    if status.state is DatabaseState.MIGRATION_REQUIRED:
        return _migrate_m005_001(path, _resolved_metadata(metadata))
    if status.state is DatabaseState.CURRENT:
        return status
    raise DatabaseBootstrapError(status)
