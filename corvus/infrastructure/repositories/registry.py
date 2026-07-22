from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from corvus.database import (
    M1_AUTHORITY_FAMILY_NAMES as _M1_AUTHORITY_FAMILY_NAMES,
)
from corvus.database import DatabaseState, classify_database
from corvus.domain.deployment import (
    AuthorityContractError,
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
    validate_authority_family_commitments,
    validate_authority_root_manifest,
    validate_registry_freshness_proof,
    validate_registry_trust_transition,
    validate_registry_verifier_time,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision

M1_AUTHORITY_FAMILY_NAMES = _M1_AUTHORITY_FAMILY_NAMES


class RegistryManifestRepositoryError(RuntimeError):
    pass


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


class RegistryManifestRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise RegistryManifestRepositoryError(
                f"database_revision_mismatch:{revision or 'unstamped'}"
            )
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise RegistryManifestRepositoryError(f"database_state_mismatch:{status.state.value}")
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        return None

    def add_registry(self, registry: AuthorityRegistry) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO authority_registries "
                    "(id, endpoint_digest, offline_root_public_key_digest, policy_digest, "
                    "status, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(registry.id),
                        registry.endpoint_digest,
                        registry.offline_root_public_key_digest,
                        registry.policy_digest,
                        registry.status.value,
                        registry.created_at.isoformat(),
                        registry.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError("authority_registry_identity_conflict") from exc

    def get_registry(self, registry_id: UUID) -> AuthorityRegistry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_registries WHERE id = ?",
                (str(registry_id),),
            ).fetchone()
        return None if row is None else AuthorityRegistry.model_validate_json(row[0])

    def append_verifier_key(self, verifier: AuthorityRegistryVerifierKeyVersion) -> None:
        try:
            with self._transaction() as connection:
                registry = self._registry(connection, verifier.registry_id)
                if registry is None or registry.status is not AuthorityRegistryStatus.ACTIVE:
                    raise RegistryManifestRepositoryError("authority_registry_not_active")
                row = connection.execute(
                    "SELECT payload_json, canonical_digest "
                    "FROM authority_registry_verifier_keys WHERE registry_id = ? "
                    "ORDER BY key_version DESC LIMIT 1",
                    (str(verifier.registry_id),),
                ).fetchone()
                if row is None:
                    if verifier.key_version != 1:
                        raise RegistryManifestRepositoryError("registry_verifier_version_skipped")
                    if (
                        verifier.predecessor_digest is not None
                        or verifier.predecessor_signature is not None
                        or verifier.offline_root_recovery_signature is not None
                    ):
                        raise RegistryManifestRepositoryError("registry_verifier_prefix_mismatch")
                else:
                    previous = AuthorityRegistryVerifierKeyVersion.model_validate_json(row[0])
                    if verifier.key_version != previous.key_version + 1:
                        raise RegistryManifestRepositoryError("registry_verifier_version_skipped")
                    if verifier.predecessor_digest != row[1]:
                        raise RegistryManifestRepositoryError("registry_verifier_prefix_mismatch")
                    if verifier.predecessor_signature is None:
                        raise RegistryManifestRepositoryError(
                            "registry_verifier_predecessor_signature_missing"
                        )
                    recovery_required = previous.status is RegistryVerifierKeyStatus.COMPROMISED
                    if recovery_required != (verifier.offline_root_recovery_signature is not None):
                        raise RegistryManifestRepositoryError(
                            "registry_verifier_recovery_authorization_mismatch"
                        )
                connection.execute(
                    "INSERT INTO authority_registry_verifier_keys "
                    "(id, registry_id, key_version, algorithm, status, valid_from, "
                    "valid_until, predecessor_digest, predecessor_signature, "
                    "offline_root_recovery_signature, revoked_at, compromise_effective_at, "
                    "canonical_digest, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(verifier.id),
                        str(verifier.registry_id),
                        verifier.key_version,
                        verifier.algorithm,
                        verifier.status.value,
                        verifier.valid_from.isoformat(),
                        None if verifier.valid_until is None else verifier.valid_until.isoformat(),
                        verifier.predecessor_digest,
                        verifier.predecessor_signature,
                        verifier.offline_root_recovery_signature,
                        None if verifier.revoked_at is None else verifier.revoked_at.isoformat(),
                        None
                        if verifier.compromise_effective_at is None
                        else verifier.compromise_effective_at.isoformat(),
                        _canonical_digest(verifier),
                        verifier.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError("registry_verifier_identity_conflict") from exc

    def list_verifier_keys(
        self,
        registry_id: UUID,
    ) -> list[AuthorityRegistryVerifierKeyVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM authority_registry_verifier_keys "
                "WHERE registry_id = ? ORDER BY key_version",
                (str(registry_id),),
            ).fetchall()
        return [AuthorityRegistryVerifierKeyVersion.model_validate_json(row[0]) for row in rows]

    def append_trust_state(
        self,
        trust_state: AuthorityRegistryTrustState,
        *,
        now: datetime,
    ) -> None:
        try:
            with self._transaction() as connection:
                registry = self._registry(connection, trust_state.registry_id)
                if registry is None or registry.status is not AuthorityRegistryStatus.ACTIVE:
                    raise RegistryManifestRepositoryError("authority_registry_not_active")
                verifier_row = connection.execute(
                    "SELECT payload_json FROM authority_registry_verifier_keys "
                    "WHERE registry_id = ? AND key_version = ?",
                    (
                        str(trust_state.registry_id),
                        trust_state.latest_verifier_key_version,
                    ),
                ).fetchone()
                if verifier_row is None:
                    raise RegistryManifestRepositoryError(
                        "registry_trust_verifier_history_incomplete"
                    )
                verifier = AuthorityRegistryVerifierKeyVersion.model_validate_json(verifier_row[0])
                if verifier.status is not RegistryVerifierKeyStatus.ACTIVE:
                    raise RegistryManifestRepositoryError(
                        "registry_trust_latest_verifier_not_active"
                    )
                validate_registry_verifier_time(verifier, now=trust_state.issued_at)
                previous_row = connection.execute(
                    "SELECT payload_json FROM authority_registry_trust_states "
                    "WHERE registry_id = ? ORDER BY metadata_version DESC LIMIT 1",
                    (str(trust_state.registry_id),),
                ).fetchone()
                if previous_row is None:
                    if trust_state.metadata_version != 1:
                        raise RegistryManifestRepositoryError("registry_metadata_version_skipped")
                    if trust_state.previous_metadata_digest is not None:
                        raise RegistryManifestRepositoryError("registry_metadata_prefix_mismatch")
                    if trust_state.expires_at <= now:
                        raise RegistryManifestRepositoryError("registry_trust_state_expired")
                else:
                    previous = AuthorityRegistryTrustState.model_validate_json(previous_row[0])
                    validate_registry_trust_transition(previous, trust_state, now=now)
                connection.execute(
                    "INSERT INTO authority_registry_trust_states "
                    "(registry_id, metadata_version, latest_verifier_key_version, "
                    "complete_history_head_digest, issued_at, expires_at, canonical_digest, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(trust_state.registry_id),
                        trust_state.metadata_version,
                        trust_state.latest_verifier_key_version,
                        trust_state.complete_history_head_digest,
                        trust_state.issued_at.isoformat(),
                        trust_state.expires_at.isoformat(),
                        trust_state.canonical_digest,
                        trust_state.model_dump_json(),
                    ),
                )
        except AuthorityContractError as exc:
            raise RegistryManifestRepositoryError(exc.reason_code) from exc
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError("registry_trust_state_identity_conflict") from exc

    def list_trust_states(self, registry_id: UUID) -> list[AuthorityRegistryTrustState]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM authority_registry_trust_states "
                "WHERE registry_id = ? ORDER BY metadata_version",
                (str(registry_id),),
            ).fetchall()
        return [AuthorityRegistryTrustState.model_validate_json(row[0]) for row in rows]

    def append_freshness_proof(
        self,
        proof: AuthorityRegistryFreshnessProof,
        *,
        now: datetime,
        minimum_sequence: int,
        expected_nonce_digest: str,
    ) -> None:
        try:
            with self._transaction() as connection:
                trust_row = connection.execute(
                    "SELECT payload_json FROM authority_registry_trust_states "
                    "WHERE registry_id = ? AND metadata_version = ?",
                    (str(proof.registry_id), proof.trust_state_metadata_version),
                ).fetchone()
                if trust_row is None:
                    raise RegistryManifestRepositoryError("registry_freshness_trust_state_missing")
                trust_state = AuthorityRegistryTrustState.model_validate_json(trust_row[0])
                if proof.complete_history_head_digest != trust_state.complete_history_head_digest:
                    raise RegistryManifestRepositoryError(
                        "registry_freshness_history_head_mismatch"
                    )
                verifier_row = connection.execute(
                    "SELECT payload_json FROM authority_registry_verifier_keys "
                    "WHERE registry_id = ? AND id = ?",
                    (str(proof.registry_id), str(proof.verifier_key_version_id)),
                ).fetchone()
                if verifier_row is None:
                    raise RegistryManifestRepositoryError("registry_freshness_verifier_missing")
                verifier = AuthorityRegistryVerifierKeyVersion.model_validate_json(verifier_row[0])
                if (
                    verifier.key_version != trust_state.latest_verifier_key_version
                    or verifier.status is not RegistryVerifierKeyStatus.ACTIVE
                ):
                    raise RegistryManifestRepositoryError("registry_freshness_verifier_mismatch")
                validate_registry_verifier_time(verifier, now=proof.issued_at)
                if (
                    proof.issued_at < trust_state.issued_at
                    or proof.expires_at <= now
                    or proof.expires_at > trust_state.expires_at
                ):
                    raise RegistryManifestRepositoryError("registry_freshness_time_invalid")
                sequence_row = connection.execute(
                    "SELECT MAX(registry_sequence) "
                    "FROM authority_registry_freshness_proofs WHERE registry_id = ?",
                    (str(proof.registry_id),),
                ).fetchone()
                persisted_minimum = (
                    0 if sequence_row is None or sequence_row[0] is None else int(sequence_row[0])
                )
                effective_minimum = max(minimum_sequence, persisted_minimum)
                validate_registry_freshness_proof(
                    proof,
                    trust_state,
                    now=now,
                    minimum_sequence=effective_minimum,
                    expected_nonce_digest=expected_nonce_digest,
                )
                if proof.registry_sequence != persisted_minimum + 1:
                    raise RegistryManifestRepositoryError("registry_freshness_sequence_skipped")
                nonce = connection.execute(
                    "SELECT 1 FROM authority_registry_freshness_proofs "
                    "WHERE registry_id = ? AND challenge_nonce_digest = ?",
                    (str(proof.registry_id), proof.challenge_nonce_digest),
                ).fetchone()
                if nonce is not None:
                    raise RegistryManifestRepositoryError("registry_nonce_replay")
                connection.execute(
                    "INSERT INTO authority_registry_freshness_proofs "
                    "(id, registry_id, trust_state_metadata_version, registry_sequence, "
                    "challenge_nonce_digest, verifier_key_version_id, issued_at, expires_at, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(proof.id),
                        str(proof.registry_id),
                        proof.trust_state_metadata_version,
                        proof.registry_sequence,
                        proof.challenge_nonce_digest,
                        str(proof.verifier_key_version_id),
                        proof.issued_at.isoformat(),
                        proof.expires_at.isoformat(),
                        proof.model_dump_json(),
                    ),
                )
        except AuthorityContractError as exc:
            raise RegistryManifestRepositoryError(exc.reason_code) from exc
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError("registry_freshness_identity_conflict") from exc

    def get_freshness_proof(
        self,
        proof_id: UUID,
    ) -> AuthorityRegistryFreshnessProof | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_registry_freshness_proofs WHERE id = ?",
                (str(proof_id),),
            ).fetchone()
        return None if row is None else AuthorityRegistryFreshnessProof.model_validate_json(row[0])

    def add_manifest(
        self,
        manifest: AuthorityStateRootManifestVersion,
        families: list[AuthorityStateRootLeafFamily],
    ) -> None:
        self._validate_manifest(manifest, families)
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO authority_state_root_manifests "
                    "(id, schema_version, canonicalization_version, manifest_digest, status, "
                    "created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(manifest.id),
                        manifest.schema_version,
                        manifest.canonicalization_version,
                        manifest.manifest_digest,
                        manifest.status.value,
                        manifest.created_at.isoformat(),
                        manifest.model_dump_json(),
                    ),
                )
                for family in families:
                    connection.execute(
                        "INSERT INTO authority_state_root_leaf_families "
                        "(manifest_version_id, ordinal, family_name, coverage_kind, "
                        "external_proof_kind, canonicalization_version, payload_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(family.manifest_version_id),
                            family.ordinal,
                            family.family_name,
                            family.coverage_kind.value,
                            family.external_proof_kind,
                            family.canonicalization_version,
                            family.model_dump_json(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError("authority_manifest_identity_conflict") from exc

    def get_manifest(
        self,
        manifest_id: UUID,
    ) -> AuthorityStateRootManifestVersion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM authority_state_root_manifests WHERE id = ?",
                (str(manifest_id),),
            ).fetchone()
        return (
            None if row is None else AuthorityStateRootManifestVersion.model_validate_json(row[0])
        )

    def list_manifest_families(
        self,
        manifest_id: UUID,
    ) -> list[AuthorityStateRootLeafFamily]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = ? ORDER BY ordinal",
                (str(manifest_id),),
            ).fetchall()
        return [AuthorityStateRootLeafFamily.model_validate_json(row[0]) for row in rows]

    def append_leaf_commitments(
        self,
        *,
        workspace_id: UUID,
        manifest: AuthorityStateRootManifestVersion,
        commitments: list[AuthorityStateRootLeafCommitment],
        observed_leaf_digests: dict[str, str],
    ) -> None:
        persisted = self.get_manifest(manifest.id)
        families = self.list_manifest_families(manifest.id)
        if persisted != manifest:
            raise RegistryManifestRepositoryError("authority_manifest_binding_mismatch")
        try:
            validate_authority_family_commitments(
                manifest,
                families,
                commitments,
                observed_leaf_digests=observed_leaf_digests,
            )
        except AuthorityContractError as exc:
            raise RegistryManifestRepositoryError(exc.reason_code) from exc
        if not commitments:
            raise RegistryManifestRepositoryError("authority_family_commitment_set_mismatch")
        generations = {commitment.authority_generation for commitment in commitments}
        if len(generations) != 1:
            raise RegistryManifestRepositoryError("authority_family_commitment_generation_mismatch")
        family_by_name = {family.family_name: family for family in families}
        for commitment in commitments:
            family = family_by_name[commitment.family_name]
            has_external_proof = commitment.external_proof_digest is not None
            if has_external_proof != (family.coverage_kind is CoverageKind.EXTERNAL_PROOF):
                raise RegistryManifestRepositoryError("authority_family_external_proof_mismatch")
        try:
            with self._transaction() as connection:
                for commitment in commitments:
                    connection.execute(
                        "INSERT INTO authority_state_root_leaf_commitments "
                        "(workspace_id, manifest_version_id, authority_generation, ordinal, "
                        "family_name, record_version, leaf_digest, external_proof_digest, "
                        "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(workspace_id),
                            str(commitment.manifest_version_id),
                            commitment.authority_generation,
                            commitment.ordinal,
                            commitment.family_name,
                            commitment.record_version,
                            commitment.leaf_digest,
                            commitment.external_proof_digest,
                            commitment.model_dump_json(),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RegistryManifestRepositoryError(
                "authority_family_commitment_identity_conflict"
            ) from exc

    def list_leaf_commitments(
        self,
        *,
        workspace_id: UUID,
        authority_generation: int,
    ) -> list[AuthorityStateRootLeafCommitment]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM authority_state_root_leaf_commitments "
                "WHERE workspace_id = ? AND authority_generation = ? ORDER BY ordinal",
                (str(workspace_id), authority_generation),
            ).fetchall()
        return [AuthorityStateRootLeafCommitment.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _registry(
        connection: sqlite3.Connection,
        registry_id: UUID,
    ) -> AuthorityRegistry | None:
        row = connection.execute(
            "SELECT payload_json FROM authority_registries WHERE id = ?",
            (str(registry_id),),
        ).fetchone()
        return None if row is None else AuthorityRegistry.model_validate_json(row[0])

    @staticmethod
    def _validate_manifest(
        manifest: AuthorityStateRootManifestVersion,
        families: list[AuthorityStateRootLeafFamily],
    ) -> None:
        try:
            validate_authority_root_manifest(
                manifest,
                families,
                mutable_authority_families=set(M1_AUTHORITY_FAMILY_NAMES),
            )
        except AuthorityContractError as exc:
            raise RegistryManifestRepositoryError(exc.reason_code) from exc
        names = {family.family_name for family in families}
        ordinals = [family.ordinal for family in sorted(families, key=lambda item: item.ordinal)]
        if names != M1_AUTHORITY_FAMILY_NAMES:
            raise RegistryManifestRepositoryError("authority_manifest_family_set_mismatch")
        if len(names) != len(families) or ordinals != list(range(1, len(families) + 1)):
            raise RegistryManifestRepositoryError("authority_manifest_order_invalid")
        for family in families:
            if (
                family.manifest_version_id != manifest.id
                or family.canonicalization_version != manifest.canonicalization_version
                or (family.coverage_kind is CoverageKind.EXTERNAL_PROOF)
                != (family.external_proof_kind is not None)
            ):
                raise RegistryManifestRepositoryError("authority_manifest_family_binding_mismatch")
        expected_digest = canonical_authority_manifest_digest(
            schema_version=manifest.schema_version,
            canonicalization_version=manifest.canonicalization_version,
            families=families,
        )
        if manifest.manifest_digest != expected_digest:
            raise RegistryManifestRepositoryError("authority_manifest_digest_mismatch")
