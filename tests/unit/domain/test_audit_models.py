from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.audit import (
    AuditAnchorBindingState,
    AuditAnchorRecoveryCheckpoint,
    AuditReceipt,
    AuditResultBinding,
    AuthorizationDecisionSnapshot,
    SigningKeyStatus,
    WorkspaceSigningKeyVersion,
    authorization_snapshot_digest,
    validate_anchor_recovery_replay,
    validate_signing_time,
)
from corvus.domain.deployment import (
    AuthorityCloseCertificate,
    AuthorityCommitIntent,
    AuthorityCommitState,
    AuthorityContractError,
    AuthorityHandoff,
    AuthorityHandoffActivation,
    AuthorityHandoffState,
    AuthorityRegistryFreshnessProof,
    AuthorityRegistryTrustState,
    AuthorityRegistryVerifierKeyVersion,
    AuthorityStateRootLeafCommitment,
    AuthorityStateRootLeafFamily,
    AuthorityStateRootManifestVersion,
    CoverageKind,
    EpochKeyDisposition,
    RegistryVerifierKeyStatus,
    RestoreDecision,
    RestoreValidationReceipt,
    validate_authority_family_commitments,
    validate_authority_root_manifest,
    validate_handoff_activation,
    validate_registry_freshness_proof,
    validate_registry_trust_transition,
    validate_registry_verifier_time,
)


def _authorization_snapshot_payload(*, canonical_digest: str) -> dict[str, object]:
    return {
        "workspace_id": str(uuid4()),
        "request_context_id": str(uuid4()),
        "deployment_instance_id": str(uuid4()),
        "authority_epoch_credential_id": str(uuid4()),
        "authority_generation": 7,
        "authority_state_root": "a" * 64,
        "authority_commit_receipt_id": str(uuid4()),
        "authority_proof_digest": "b" * 64,
        "membership_version_ids": [str(uuid4())],
        "membership_digest": "c" * 64,
        "scope_kind": "project",
        "scope_id": str(uuid4()),
        "scope_digest": "d" * 64,
        "audience_policy_snapshot_id": str(uuid4()),
        "audience_digest": "e" * 64,
        "requester_id": str(uuid4()),
        "transport_principal_id": str(uuid4()),
        "access_bundle_id": str(uuid4()),
        "access_bundle_version_digest": "f" * 64,
        "agent_grant_id": str(uuid4()),
        "agent_delegation_digest": "0" * 64,
        "policy_digest": "1" * 64,
        "autonomy_policy_digest": "2" * 64,
        "budget_snapshot_ids": [str(uuid4())],
        "budget_snapshot_digest": "3" * 64,
        "kill_switch_snapshot_ids": [str(uuid4())],
        "kill_switch_snapshot_digest": "4" * 64,
        "decision": "allow",
        "reason_code": "authorized",
        "canonical_inputs_json": {"resource": {"z": 2, "a": 1}},
        "source_record_version_map": {"access_bundle": 3, "agent_grant": 2},
        "canonical_digest": canonical_digest,
        "signing_key_version_id": str(uuid4()),
        "snapshot_signature": "signed-snapshot",
    }


def test_authorization_snapshot_digest_is_stable_and_self_validating() -> None:
    first_inputs = {"resource": {"z": 2, "a": 1}}
    reordered_inputs = {"resource": {"a": 1, "z": 2}}
    versions = {"access_bundle": 3, "agent_grant": 2}
    expected = authorization_snapshot_digest(first_inputs, versions)

    assert expected == authorization_snapshot_digest(
        reordered_inputs, dict(reversed(versions.items()))
    )
    AuthorizationDecisionSnapshot.model_validate(
        _authorization_snapshot_payload(canonical_digest=expected)
    )
    with pytest.raises(ValidationError) as exc_info:
        AuthorizationDecisionSnapshot.model_validate(
            _authorization_snapshot_payload(canonical_digest="9" * 64)
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "authorization_snapshot_digest_mismatch"
    )


def test_authority_commit_intent_requires_generation_and_root_advance() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AuthorityCommitIntent(
            workspace_id=uuid4(),
            epoch=1,
            deployment_instance_id=uuid4(),
            prior_generation=4,
            next_generation=5,
            prior_state_root="a" * 64,
            mutation_digest="b" * 64,
            proposed_state_root="a" * 64,
            state=AuthorityCommitState.PREPARED,
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == "authority_root_must_advance"


def test_restore_cannot_recreate_mutation_authority() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RestoreValidationReceipt(
            workspace_id=uuid4(),
            restored_database_digest="a" * 64,
            observed_epoch=4,
            observed_generation=12,
            observed_state_root="b" * 64,
            trust_anchor_id=uuid4(),
            decision=RestoreDecision.EXCLUSIVE_TAKEOVER_NEW_EPOCH,
            reason_code="operator_requested_takeover",
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "takeover_requires_revocation_and_exclusive_receipt"
    )


def test_restore_takeover_must_activate_exactly_the_next_epoch() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RestoreValidationReceipt(
            workspace_id=uuid4(),
            restored_database_digest="a" * 64,
            observed_epoch=4,
            observed_generation=12,
            observed_state_root="b" * 64,
            trust_anchor_id=uuid4(),
            former_instance_revocation_digest="c" * 64,
            takeover_lease_or_local_anchor_receipt_digest="d" * 64,
            takeover_epoch=4,
            decision=RestoreDecision.EXCLUSIVE_TAKEOVER_NEW_EPOCH,
            reason_code="operator_requested_takeover",
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == ("takeover_epoch_must_advance_once")


def test_registry_trust_state_rejects_skipped_metadata_version() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    previous = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=3,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    current = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=5,
        latest_verifier_key_version=3,
        complete_history_head_digest="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        offline_root_version=1,
        threshold_signature_set_digest="d" * 64,
        previous_metadata_digest=previous.canonical_digest,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_trust_transition(previous, current, now=now)

    assert exc_info.value.reason_code == "registry_metadata_version_skipped"


def test_registry_trust_state_rejects_history_prefix_substitution() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    previous = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=3,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    substituted = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=3,
        complete_history_head_digest="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        offline_root_version=1,
        threshold_signature_set_digest="d" * 64,
        previous_metadata_digest="e" * 64,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_trust_transition(previous, substituted, now=now)

    assert exc_info.value.reason_code == "registry_metadata_prefix_mismatch"


def test_registry_trust_state_rejects_expired_metadata() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    previous = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=3,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    expired = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=3,
        complete_history_head_digest="c" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now,
        offline_root_version=1,
        threshold_signature_set_digest="d" * 64,
        previous_metadata_digest=previous.canonical_digest,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_trust_transition(previous, expired, now=now)

    assert exc_info.value.reason_code == "registry_trust_state_expired"


def test_registry_freshness_proof_rejects_sequence_replay() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    trust_state = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=3,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    proof = AuthorityRegistryFreshnessProof(
        registry_id=registry_id,
        trust_state_metadata_version=4,
        complete_history_head_digest="a" * 64,
        registry_sequence=9,
        challenge_nonce_digest="c" * 64,
        response_digest="d" * 64,
        issued_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(minutes=5),
        verifier_key_version_id=uuid4(),
        registry_signature="signed-proof",
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_freshness_proof(
            proof,
            trust_state,
            now=now,
            minimum_sequence=9,
            expected_nonce_digest="c" * 64,
        )

    assert exc_info.value.reason_code == "registry_sequence_replay"


def test_registry_freshness_proof_rejects_nonce_replay() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    trust_state = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=3,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    proof = AuthorityRegistryFreshnessProof(
        registry_id=registry_id,
        trust_state_metadata_version=4,
        complete_history_head_digest="a" * 64,
        registry_sequence=10,
        challenge_nonce_digest="c" * 64,
        response_digest="d" * 64,
        issued_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(minutes=5),
        verifier_key_version_id=uuid4(),
        registry_signature="signed-proof",
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_freshness_proof(
            proof,
            trust_state,
            now=now,
            minimum_sequence=9,
            expected_nonce_digest="e" * 64,
        )

    assert exc_info.value.reason_code == "registry_nonce_mismatch"


def test_authority_root_manifest_rejects_unlisted_family() -> None:
    manifest = AuthorityStateRootManifestVersion(
        schema_version=1,
        canonicalization_version=1,
        manifest_digest="a" * 64,
    )
    listed = AuthorityStateRootLeafFamily(
        manifest_version_id=manifest.id,
        ordinal=1,
        family_name="workspace_memberships",
        coverage_kind=CoverageKind.IN_ROOT,
        canonicalization_version=1,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_authority_root_manifest(
            manifest,
            [listed],
            mutable_authority_families={"workspace_memberships", "access_bundles"},
        )

    assert exc_info.value.reason_code == "unlisted_authority_family"


def test_audit_receipt_cannot_include_resulting_authority_state() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AuditReceipt.model_validate(
            {
                "workspace_id": str(uuid4()),
                "workspace_sequence": 1,
                "schema_version": 1,
                "prior_authority_epoch": 1,
                "prior_authority_generation": 4,
                "prior_authority_state_root": "a" * 64,
                "prior_authority_commit_receipt_id": str(uuid4()),
                "authority_commit_intent_id": str(uuid4()),
                "intended_mutation_digest": "b" * 64,
                "request_context_id": str(uuid4()),
                "authorization_snapshot_id": str(uuid4()),
                "authorization_snapshot_digest": "c" * 64,
                "action": "project.create",
                "resource": "project",
                "decision": "allow",
                "reason_code": "authorized",
                "policy_digest": "d" * 64,
                "sanitized_input_digest": "e" * 64,
                "signing_key_version_id": str(uuid4()),
                "previous_hash": "f" * 64,
                "receipt_hash": "1" * 64,
                "receipt_signature": "signed-receipt",
                "resulting_authority_state_root": "2" * 64,
            }
        )

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
    assert tuple(exc_info.value.errors()[0]["loc"]) == ("resulting_authority_state_root",)


def test_authorization_snapshot_requires_requester_and_agent_authority_inputs() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AuthorizationDecisionSnapshot.model_validate({})

    missing = {
        tuple(error["loc"]) for error in exc_info.value.errors() if error["type"] == "missing"
    }
    assert {
        ("workspace_id",),
        ("requester_id",),
        ("access_bundle_id",),
        ("agent_grant_id",),
        ("authority_commit_receipt_id",),
        ("signing_key_version_id",),
        ("snapshot_signature",),
    } <= missing


def test_revoked_signing_key_rejects_post_revocation_signature() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    key = WorkspaceSigningKeyVersion(
        workspace_id=uuid4(),
        key_epoch=2,
        algorithm="ed25519",
        public_key="public-key-material",
        non_exportable_private_key_ref="keyring://workspace/signing/2",
        status=SigningKeyStatus.REVOKED,
        valid_from=now - timedelta(days=1),
        revoked_at=now,
        predecessor_digest="a" * 64,
        attestation_digest="b" * 64,
    )

    with pytest.raises(ValueError, match="signing_key_revoked_at_signing_time"):
        validate_signing_time(key, now + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("close_anchor_digest", "key_disposition", "key_evidence_digest", "reason_code"),
    [
        (None, EpochKeyDisposition.DESTROYED, "a" * 64, "handoff_close_not_anchored"),
        (
            "b" * 64,
            EpochKeyDisposition.PENDING,
            None,
            "handoff_old_epoch_key_still_active",
        ),
    ],
)
def test_handoff_activation_requires_anchored_close_and_old_key_disposition(
    close_anchor_digest: str | None,
    key_disposition: EpochKeyDisposition,
    key_evidence_digest: str | None,
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    close = AuthorityCloseCertificate(
        workspace_id=workspace_id,
        closed_epoch=4,
        source_deployment_id=uuid4(),
        source_deployment_instance_id=uuid4(),
        target_deployment_id=uuid4(),
        epoch_credential_digest="a" * 64,
        destruction_or_revocation_attestation_digest=key_evidence_digest,
        final_authority_generation=19,
        final_state_root="c" * 64,
        workspace_signing_key_version_id=uuid4(),
        workspace_signature="signed-close",
        anchor_receipt_digest=close_anchor_digest,
        epoch_key_disposition=key_disposition,
        externally_anchored_at=(
            datetime(2026, 7, 14, 12, 0, tzinfo=UTC) if close_anchor_digest is not None else None
        ),
    )
    activation = AuthorityHandoffActivation(
        workspace_id=workspace_id,
        target_deployment_instance_id=uuid4(),
        authority_epoch=5,
        source_close_certificate_id=close.id,
        source_close_certificate_digest="d" * 64,
        authority_epoch_credential_id=uuid4(),
        exclusive_lease_or_local_anchor_receipt_digest="e" * 64,
        activated_at=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_handoff_activation(close, activation)

    assert exc_info.value.reason_code == reason_code


@pytest.mark.parametrize("rolled_back_family", ["workspace_memberships", "access_bundles"])
def test_authority_manifest_detects_independent_family_rollback(
    rolled_back_family: str,
) -> None:
    manifest = AuthorityStateRootManifestVersion(
        schema_version=1,
        canonicalization_version=1,
        manifest_digest="a" * 64,
    )
    families = [
        AuthorityStateRootLeafFamily(
            manifest_version_id=manifest.id,
            ordinal=1,
            family_name="workspace_memberships",
            coverage_kind=CoverageKind.IN_ROOT,
            canonicalization_version=1,
        ),
        AuthorityStateRootLeafFamily(
            manifest_version_id=manifest.id,
            ordinal=2,
            family_name="access_bundles",
            coverage_kind=CoverageKind.IN_ROOT,
            canonicalization_version=1,
        ),
    ]
    commitments = [
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=12,
            ordinal=1,
            family_name="workspace_memberships",
            record_version=8,
            leaf_digest="b" * 64,
        ),
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=12,
            ordinal=2,
            family_name="access_bundles",
            record_version=5,
            leaf_digest="c" * 64,
        ),
    ]
    observed = {
        "workspace_memberships": "b" * 64,
        "access_bundles": "c" * 64,
    }
    observed[rolled_back_family] = "d" * 64

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_authority_family_commitments(
            manifest,
            families,
            commitments,
            observed_leaf_digests=observed,
        )

    assert exc_info.value.reason_code == "authority_family_rollback_detected"


def test_audit_result_binding_separates_finalized_state_from_prior_state_receipt() -> None:
    binding = AuditResultBinding(
        workspace_id=uuid4(),
        audit_receipt_id=uuid4(),
        audit_receipt_hash="a" * 64,
        authority_commit_intent_id=uuid4(),
        prepared_result_digest="b" * 64,
        finalized_authority_epoch=3,
        finalized_authority_generation=17,
        finalized_authority_state_root="c" * 64,
        authority_commit_receipt_id=uuid4(),
        authority_commit_receipt_digest="d" * 64,
        signing_key_version_id=uuid4(),
        binding_hash="e" * 64,
        binding_signature="signed-binding",
    )

    assert binding.finalized_authority_generation == 17
    assert "finalized_authority_state_root" in binding.model_fields_set


@pytest.mark.parametrize(
    ("intent_matches", "result_digest", "reason_code"),
    [
        (False, "f" * 64, "anchor_recovery_intent_mismatch"),
        (True, "0" * 64, "anchor_recovery_digest_mismatch"),
    ],
)
def test_anchor_recovery_replays_only_exact_prepared_digest(
    intent_matches: bool,
    result_digest: str,
    reason_code: str,
) -> None:
    intent_id = uuid4()
    checkpoint = AuditAnchorRecoveryCheckpoint(
        workspace_id=uuid4(),
        audit_receipt_id=uuid4(),
        authority_commit_intent_id=intent_id,
        prepared_result_digest="f" * 64,
        state=AuditAnchorBindingState.PREPARED,
    )

    with pytest.raises(ValueError, match=reason_code):
        validate_anchor_recovery_replay(
            checkpoint,
            authority_commit_intent_id=intent_id if intent_matches else uuid4(),
            prepared_result_digest=result_digest,
        )


def test_rotated_signing_key_verifies_only_before_rotation_cutoff() -> None:
    cutoff = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    key = WorkspaceSigningKeyVersion(
        workspace_id=uuid4(),
        key_epoch=2,
        algorithm="ed25519",
        public_key="public-key-material",
        non_exportable_private_key_ref="keyring://workspace/signing/2",
        status=SigningKeyStatus.ROTATED,
        valid_from=cutoff - timedelta(days=1),
        valid_until=cutoff,
        predecessor_digest="a" * 64,
        attestation_digest="b" * 64,
    )

    validate_signing_time(key, cutoff - timedelta(seconds=1))
    with pytest.raises(ValueError, match="signing_key_expired_at_signing_time"):
        validate_signing_time(key, cutoff)


def test_compromised_signing_key_preserves_pre_compromise_history_only() -> None:
    compromised_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    key = WorkspaceSigningKeyVersion(
        workspace_id=uuid4(),
        key_epoch=2,
        algorithm="ed25519",
        public_key="public-key-material",
        non_exportable_private_key_ref="keyring://workspace/signing/2",
        status=SigningKeyStatus.COMPROMISED,
        valid_from=compromised_at - timedelta(days=1),
        compromise_effective_at=compromised_at,
        predecessor_digest="a" * 64,
        attestation_digest="b" * 64,
    )

    validate_signing_time(key, compromised_at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="signing_key_compromised_at_signing_time"):
        validate_signing_time(key, compromised_at)


def test_signing_key_rejects_signature_before_validity_window() -> None:
    valid_from = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    key = WorkspaceSigningKeyVersion(
        workspace_id=uuid4(),
        key_epoch=1,
        algorithm="ed25519",
        public_key="public-key-material",
        non_exportable_private_key_ref="keyring://workspace/signing/1",
        status=SigningKeyStatus.ACTIVE,
        valid_from=valid_from,
        attestation_digest="b" * 64,
    )

    with pytest.raises(ValueError, match="signing_key_not_yet_valid"):
        validate_signing_time(key, valid_from - timedelta(seconds=1))


def test_registry_trust_state_rejects_frozen_history_head() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    previous = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    frozen = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=5,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        offline_root_version=1,
        threshold_signature_set_digest="c" * 64,
        previous_metadata_digest=previous.canonical_digest,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_trust_transition(previous, frozen, now=now)

    assert exc_info.value.reason_code == "registry_history_head_frozen"


def test_registry_trust_state_rejects_skipped_verifier_rotation() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    registry_id = uuid4()
    previous = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=4,
        latest_verifier_key_version=2,
        complete_history_head_digest="a" * 64,
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        offline_root_version=1,
        threshold_signature_set_digest="b" * 64,
    )
    skipped = AuthorityRegistryTrustState(
        registry_id=registry_id,
        metadata_version=5,
        latest_verifier_key_version=4,
        complete_history_head_digest="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        offline_root_version=1,
        threshold_signature_set_digest="d" * 64,
        previous_metadata_digest=previous.canonical_digest,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_trust_transition(previous, skipped, now=now)

    assert exc_info.value.reason_code == "registry_verifier_version_skipped"


@pytest.mark.parametrize(
    ("status", "revoked_at", "compromised_at", "reason_code"),
    [
        (
            RegistryVerifierKeyStatus.REVOKED,
            datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            None,
            "registry_verifier_revoked_at_verification_time",
        ),
        (
            RegistryVerifierKeyStatus.COMPROMISED,
            None,
            datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            "registry_verifier_compromised_at_verification_time",
        ),
    ],
)
def test_registry_verifier_rejects_post_revocation_or_compromise(
    status: RegistryVerifierKeyStatus,
    revoked_at: datetime | None,
    compromised_at: datetime | None,
    reason_code: str,
) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    verifier = AuthorityRegistryVerifierKeyVersion(
        registry_id=uuid4(),
        key_version=3,
        public_key="registry-public-key",
        status=status,
        valid_from=now - timedelta(days=1),
        revoked_at=revoked_at,
        compromise_effective_at=compromised_at,
        predecessor_digest="d" * 64,
        threshold_attestation_digest="e" * 64,
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_registry_verifier_time(verifier, now=now)

    assert exc_info.value.reason_code == reason_code


def test_authority_handoff_requires_next_epoch_and_forbids_private_capability() -> None:
    payload = {
        "workspace_id": str(uuid4()),
        "from_deployment_id": str(uuid4()),
        "from_deployment_instance_id": str(uuid4()),
        "to_deployment_id": str(uuid4()),
        "to_deployment_instance_id": str(uuid4()),
        "from_epoch": 5,
        "to_epoch": 5,
        "export_artifact_digest": "a" * 64,
        "source_checkpoint_digest": "b" * 64,
        "authorization_snapshot_id": str(uuid4()),
        "authorization_snapshot_digest": "c" * 64,
        "source_signing_key_version_id": str(uuid4()),
        "close_certificate_id": str(uuid4()),
        "target_epoch_credential_id": str(uuid4()),
        "state": AuthorityHandoffState.PREPARED,
    }

    with pytest.raises(ValidationError) as epoch_exc:
        AuthorityHandoff.model_validate(payload)
    assert epoch_exc.value.errors()[0]["ctx"]["reason_code"] == ("handoff_epoch_must_advance_once")

    payload["to_epoch"] = 6
    payload["exported_private_capability"] = "must-never-cross-handoff"
    with pytest.raises(ValidationError) as capability_exc:
        AuthorityHandoff.model_validate(payload)
    assert tuple(capability_exc.value.errors()[0]["loc"]) == ("exported_private_capability",)
    assert capability_exc.value.errors()[0]["type"] == "extra_forbidden"


def test_authority_close_certificate_carries_complete_anchored_handoff_evidence() -> None:
    anchored_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    certificate = AuthorityCloseCertificate(
        workspace_id=uuid4(),
        closed_epoch=5,
        source_deployment_id=uuid4(),
        source_deployment_instance_id=uuid4(),
        target_deployment_id=uuid4(),
        epoch_credential_digest="a" * 64,
        destruction_or_revocation_attestation_digest="b" * 64,
        final_authority_generation=22,
        final_state_root="c" * 64,
        workspace_signing_key_version_id=uuid4(),
        workspace_signature="signed-close",
        local_anchor_receipt_digest="d" * 64,
        anchor_receipt_digest="e" * 64,
        externally_anchored_at=anchored_at,
    )

    assert certificate.closed_epoch == 5
    assert certificate.externally_anchored_at == anchored_at
