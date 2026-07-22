from __future__ import annotations

M1_005_FAMILY_NAMES = (
    "access_bundles",
    "agent_grants",
    "audit_anchor_recovery_checkpoints",
    "audit_receipts",
    "audit_result_bindings",
    "audience_policy_snapshots",
    "authority_commit_intents",
    "authority_epoch_credentials",
    "authority_registries",
    "authority_registry_freshness_proofs",
    "authority_registry_trust_states",
    "authority_registry_verifier_keys",
    "authority_state_root_manifests",
    "authority_trust_anchors",
    "authorization_decision_snapshots",
    "capability_grants",
    "delegation_grants",
    "deployment_instance_leases",
    "deployment_instances",
    "idempotency_envelopes",
    "projects",
    "workspace_authorities",
    "workspace_signing_key_versions",
)
M1_006_FAMILY_NAMES = tuple(
    sorted(
        {
            *M1_005_FAMILY_NAMES,
            "authority_close_certificates",
            "authority_handoff_activations",
            "authority_handoffs",
            "restore_validation_receipts",
        }
    )
)
M1_007_FAMILY_NAMES = tuple(
    sorted(
        {
            *M1_006_FAMILY_NAMES,
            "agent_identities",
            "identity_workspaces",
            "principals",
            "scopes",
            "workspace_memberships",
        }
    )
)
M1_008_FAMILY_NAMES = tuple(
    name
    for name in M1_007_FAMILY_NAMES
    if name not in {"audit_anchor_recovery_checkpoints", "audit_result_bindings"}
)
M1_009_FAMILY_NAMES = M1_007_FAMILY_NAMES
M2_001_FAMILY_NAMES = tuple(
    sorted(
        {
            *M1_009_FAMILY_NAMES,
            "accounts",
            "device_registrations",
            "external_identities",
            "session_records",
        }
    )
)
M2_002_FAMILY_NAMES = tuple(
    sorted(
        {
            *M2_001_FAMILY_NAMES,
            "device_sync_acknowledgements",
            "outbox_events",
            "platform_idempotency",
            "workspace_changes",
            "workspace_sync_heads",
        }
    )
)
M2_003_FAMILY_NAMES = tuple(
    sorted(
        {
            *M2_002_FAMILY_NAMES,
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
)


def family_proof_metadata(family_name: str) -> tuple[str, str | None]:
    if family_name == "authority_registry_freshness_proofs":
        return "external_proof", "registry_freshness_proof"
    if family_name == "audit_anchor_recovery_checkpoints":
        return "external_proof", "sealed_audit_checkpoint_history"
    if family_name == "audit_result_bindings":
        return "external_proof", "sealed_audit_result_binding_history"
    return "in_root", None
