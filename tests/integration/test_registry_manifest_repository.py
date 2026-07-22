from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from corvus.domain.deployment import (
    AuthorityRegistry,
    AuthorityRegistryFreshnessProof,
    AuthorityRegistryStatus,
    AuthorityRegistryTrustState,
    AuthorityRegistryVerifierKeyVersion,
    AuthorityStateRootLeafCommitment,
    AuthorityStateRootLeafFamily,
    AuthorityStateRootManifestVersion,
    CoverageKind,
    RegistryVerifierKeyStatus,
    canonical_authority_manifest_digest,
)
from corvus.infrastructure.db import upgrade_database
from corvus.infrastructure.repositories.registry import (
    M1_AUTHORITY_FAMILY_NAMES,
    RegistryManifestRepository,
    RegistryManifestRepositoryError,
)
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    upgrade_database(database)
    return database


def _canonical_digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _registry() -> AuthorityRegistry:
    return AuthorityRegistry(
        endpoint_digest="1" * 64,
        offline_root_public_key_digest="2" * 64,
        policy_digest="3" * 64,
        status=AuthorityRegistryStatus.ACTIVE,
        created_at=_NOW,
    )


def _verifier(
    registry: AuthorityRegistry,
    *,
    version: int,
    predecessor_digest: str | None = None,
    status: RegistryVerifierKeyStatus = RegistryVerifierKeyStatus.ACTIVE,
    offline_root_recovery_signature: str | None = None,
) -> AuthorityRegistryVerifierKeyVersion:
    return AuthorityRegistryVerifierKeyVersion(
        registry_id=registry.id,
        key_version=version,
        public_key=f"registry-verifier-public-key-{version}",
        status=status,
        valid_from=_NOW + timedelta(minutes=version - 1),
        valid_until=_NOW + timedelta(days=30),
        predecessor_digest=predecessor_digest,
        predecessor_signature=(
            None if predecessor_digest is None else f"predecessor-signature-{version}"
        ),
        offline_root_recovery_signature=offline_root_recovery_signature,
        threshold_attestation_digest=f"{version + 3:x}" * 64,
    )


def _trust_state(
    registry: AuthorityRegistry,
    *,
    metadata_version: int,
    verifier_version: int,
    previous: AuthorityRegistryTrustState | None = None,
    history_marker: str = "7",
) -> AuthorityRegistryTrustState:
    return AuthorityRegistryTrustState(
        registry_id=registry.id,
        metadata_version=metadata_version,
        latest_verifier_key_version=verifier_version,
        complete_history_head_digest=history_marker * 64,
        issued_at=_NOW + timedelta(hours=metadata_version),
        expires_at=_NOW + timedelta(days=7),
        offline_root_version=1,
        threshold_signature_set_digest="8" * 64,
        previous_metadata_digest=None if previous is None else previous.canonical_digest,
    )


def _freshness(
    registry: AuthorityRegistry,
    trust_state: AuthorityRegistryTrustState,
    verifier: AuthorityRegistryVerifierKeyVersion,
    *,
    sequence: int,
    nonce_marker: str,
) -> AuthorityRegistryFreshnessProof:
    return AuthorityRegistryFreshnessProof(
        registry_id=registry.id,
        trust_state_metadata_version=trust_state.metadata_version,
        complete_history_head_digest=trust_state.complete_history_head_digest,
        registry_sequence=sequence,
        challenge_nonce_digest=nonce_marker * 64,
        response_digest=f"{sequence % 10}" * 64,
        issued_at=_NOW + timedelta(hours=2, minutes=sequence),
        expires_at=_NOW + timedelta(hours=3),
        verifier_key_version_id=verifier.id,
        registry_signature=f"registry-signature-{sequence}",
    )


def _manifest() -> tuple[
    AuthorityStateRootManifestVersion,
    list[AuthorityStateRootLeafFamily],
]:
    manifest_id = uuid4()
    families = [
        AuthorityStateRootLeafFamily(
            manifest_version_id=manifest_id,
            ordinal=ordinal,
            family_name=family_name,
            coverage_kind=(
                CoverageKind.EXTERNAL_PROOF
                if family_name == "authority_registry_freshness_proofs"
                else CoverageKind.IN_ROOT
            ),
            external_proof_kind=(
                "registry_freshness_proof"
                if family_name == "authority_registry_freshness_proofs"
                else None
            ),
            canonicalization_version=1,
        )
        for ordinal, family_name in enumerate(sorted(M1_AUTHORITY_FAMILY_NAMES), start=1)
    ]
    manifest = AuthorityStateRootManifestVersion(
        id=manifest_id,
        schema_version=6,
        canonicalization_version=1,
        manifest_digest=canonical_authority_manifest_digest(
            schema_version=6,
            canonicalization_version=1,
            families=families,
        ),
        created_at=_NOW,
    )
    return manifest, families


def test_registry_history_and_freshness_round_trip_after_restart(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = RegistryManifestRepository(database)
    registry = _registry()
    first_verifier = _verifier(registry, version=1)
    second_verifier = _verifier(
        registry,
        version=2,
        predecessor_digest=_canonical_digest(first_verifier),
    )
    first_trust = _trust_state(registry, metadata_version=1, verifier_version=1)
    second_trust = _trust_state(
        registry,
        metadata_version=2,
        verifier_version=2,
        previous=first_trust,
        history_marker="9",
    )
    freshness = _freshness(
        registry,
        second_trust,
        second_verifier,
        sequence=1,
        nonce_marker="a",
    )

    repository.add_registry(registry)
    repository.append_verifier_key(first_verifier)
    repository.append_trust_state(first_trust, now=_NOW + timedelta(hours=1))
    repository.append_verifier_key(second_verifier)
    repository.append_trust_state(second_trust, now=_NOW + timedelta(hours=2))
    repository.append_freshness_proof(
        freshness,
        now=_NOW + timedelta(hours=2, minutes=2),
        minimum_sequence=0,
        expected_nonce_digest="a" * 64,
    )
    repository.close()

    reopened = RegistryManifestRepository(database)
    assert reopened.get_registry(registry.id) == registry
    assert reopened.list_verifier_keys(registry.id) == [first_verifier, second_verifier]
    assert reopened.list_trust_states(registry.id) == [first_trust, second_trust]
    assert reopened.get_freshness_proof(freshness.id) == freshness


def test_verifier_history_rejects_skip_and_predecessor_substitution(tmp_path: Path) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    registry = _registry()
    first = _verifier(registry, version=1)
    repository.add_registry(registry)
    repository.append_verifier_key(first)

    skipped = _verifier(registry, version=3, predecessor_digest=_canonical_digest(first))
    with pytest.raises(RegistryManifestRepositoryError, match="registry_verifier_version_skipped"):
        repository.append_verifier_key(skipped)

    substituted = _verifier(registry, version=2, predecessor_digest="f" * 64)
    with pytest.raises(RegistryManifestRepositoryError, match="registry_verifier_prefix_mismatch"):
        repository.append_verifier_key(substituted)


@pytest.mark.parametrize(
    "status",
    [
        RegistryVerifierKeyStatus.ROTATED,
        RegistryVerifierKeyStatus.REVOKED,
        RegistryVerifierKeyStatus.COMPROMISED,
    ],
)
def test_trust_state_rejects_non_active_latest_verifier(
    tmp_path: Path,
    status: RegistryVerifierKeyStatus,
) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    registry = _registry()
    verifier = _verifier(registry, version=1, status=status)
    trust = _trust_state(registry, metadata_version=1, verifier_version=1)
    repository.add_registry(registry)
    repository.append_verifier_key(verifier)

    with pytest.raises(
        RegistryManifestRepositoryError,
        match="registry_trust_latest_verifier_not_active",
    ):
        repository.append_trust_state(trust, now=_NOW + timedelta(hours=1))


def test_compromised_verifier_recovery_requires_offline_root_authorization(
    tmp_path: Path,
) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    registry = _registry()
    compromised = _verifier(
        registry,
        version=1,
        status=RegistryVerifierKeyStatus.COMPROMISED,
    )
    repository.add_registry(registry)
    repository.append_verifier_key(compromised)
    predecessor_digest = _canonical_digest(compromised)
    unsigned_recovery = _verifier(
        registry,
        version=2,
        predecessor_digest=predecessor_digest,
    )

    with pytest.raises(
        RegistryManifestRepositoryError,
        match="registry_verifier_recovery_authorization_mismatch",
    ):
        repository.append_verifier_key(unsigned_recovery)

    authorized_recovery = _verifier(
        registry,
        version=2,
        predecessor_digest=predecessor_digest,
        offline_root_recovery_signature="offline-root-recovery-signature",
    )
    repository.append_verifier_key(authorized_recovery)

    assert repository.list_verifier_keys(registry.id) == [
        compromised,
        authorized_recovery,
    ]


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"metadata_version": 3}, "registry_metadata_version_skipped"),
        ({"complete_history_head_digest": "7" * 64}, "registry_history_head_frozen"),
        ({"previous_metadata_digest": "f" * 64}, "registry_metadata_prefix_mismatch"),
        ({"expires_at": _NOW + timedelta(hours=1)}, "registry_trust_state_expired"),
    ],
)
def test_trust_history_rejects_non_monotonic_updates(
    tmp_path: Path,
    update: dict[str, object],
    reason: str,
) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    registry = _registry()
    verifier = _verifier(registry, version=1)
    first = _trust_state(registry, metadata_version=1, verifier_version=1)
    repository.add_registry(registry)
    repository.append_verifier_key(verifier)
    repository.append_trust_state(first, now=_NOW + timedelta(hours=1))
    current = _trust_state(
        registry,
        metadata_version=2,
        verifier_version=1,
        previous=first,
        history_marker="9",
    ).model_copy(update=update)

    with pytest.raises(RegistryManifestRepositoryError, match=reason):
        repository.append_trust_state(current, now=_NOW + timedelta(hours=2))


def test_freshness_proof_rejects_nonce_and_sequence_replay(tmp_path: Path) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    registry = _registry()
    verifier = _verifier(registry, version=1)
    trust = _trust_state(registry, metadata_version=1, verifier_version=1)
    first = _freshness(registry, trust, verifier, sequence=1, nonce_marker="a")
    repository.add_registry(registry)
    repository.append_verifier_key(verifier)
    repository.append_trust_state(trust, now=_NOW + timedelta(hours=1))
    repository.append_freshness_proof(
        first,
        now=_NOW + timedelta(hours=2, minutes=2),
        minimum_sequence=0,
        expected_nonce_digest="a" * 64,
    )

    stale = _freshness(registry, trust, verifier, sequence=1, nonce_marker="b")
    with pytest.raises(RegistryManifestRepositoryError, match="registry_sequence_replay"):
        repository.append_freshness_proof(
            stale,
            now=_NOW + timedelta(hours=2, minutes=2),
            minimum_sequence=1,
            expected_nonce_digest="b" * 64,
        )
    replayed_nonce = _freshness(registry, trust, verifier, sequence=2, nonce_marker="a")
    with pytest.raises(RegistryManifestRepositoryError, match="registry_nonce_replay"):
        repository.append_freshness_proof(
            replayed_nonce,
            now=_NOW + timedelta(hours=2, minutes=3),
            minimum_sequence=1,
            expected_nonce_digest="a" * 64,
        )


def test_manifest_and_commitments_are_exhaustive_and_workspace_scoped(tmp_path: Path) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    manifest, families = _manifest()
    repository.add_manifest(manifest, families)
    workspace_id = uuid4()
    observed = {family.family_name: f"{family.ordinal % 10}" * 64 for family in families}
    commitments = [
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=4,
            ordinal=family.ordinal,
            family_name=family.family_name,
            record_version=1,
            leaf_digest=observed[family.family_name],
            external_proof_digest=(
                "e" * 64 if family.coverage_kind is CoverageKind.EXTERNAL_PROOF else None
            ),
        )
        for family in families
    ]

    repository.append_leaf_commitments(
        workspace_id=workspace_id,
        manifest=manifest,
        commitments=commitments,
        observed_leaf_digests=observed,
    )

    assert repository.get_manifest(manifest.id) == manifest
    assert repository.list_manifest_families(manifest.id) == families
    assert (
        repository.list_leaf_commitments(
            workspace_id=workspace_id,
            authority_generation=4,
        )
        == commitments
    )
    assert (
        repository.list_leaf_commitments(
            workspace_id=uuid4(),
            authority_generation=4,
        )
        == []
    )


def test_manifest_rejects_missing_family_and_digest_substitution(tmp_path: Path) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    manifest, families = _manifest()
    missing = families[:-1]
    with pytest.raises(RegistryManifestRepositoryError, match="unlisted_authority_family"):
        repository.add_manifest(manifest, missing)

    substituted = manifest.model_copy(update={"manifest_digest": "f" * 64})
    with pytest.raises(RegistryManifestRepositoryError, match="authority_manifest_digest_mismatch"):
        repository.add_manifest(substituted, families)


@pytest.mark.parametrize("family_name", sorted(M1_AUTHORITY_FAMILY_NAMES))
def test_every_manifest_family_detects_in_place_rollback(
    tmp_path: Path,
    family_name: str,
) -> None:
    repository = RegistryManifestRepository(_database(tmp_path))
    manifest, families = _manifest()
    repository.add_manifest(manifest, families)
    observed = {family.family_name: f"{family.ordinal % 10}" * 64 for family in families}
    commitments = [
        AuthorityStateRootLeafCommitment(
            manifest_version_id=manifest.id,
            authority_generation=4,
            ordinal=family.ordinal,
            family_name=family.family_name,
            record_version=1,
            leaf_digest=observed[family.family_name],
            external_proof_digest=(
                "e" * 64 if family.coverage_kind is CoverageKind.EXTERNAL_PROOF else None
            ),
        )
        for family in families
    ]
    rolled_back = dict(observed)
    rolled_back[family_name] = "f" * 64

    with pytest.raises(RegistryManifestRepositoryError, match="authority_family_rollback_detected"):
        repository.append_leaf_commitments(
            workspace_id=uuid4(),
            manifest=manifest,
            commitments=commitments,
            observed_leaf_digests=rolled_back,
        )


def test_registry_repository_rejects_forged_head_with_missing_manifest_trigger(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER authority_state_root_leaf_families_no_update")

    with pytest.raises(RegistryManifestRepositoryError, match="database_state_mismatch:partial"):
        RegistryManifestRepository(database)
