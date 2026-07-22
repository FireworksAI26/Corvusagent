from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from keyring.errors import KeyringError
from platformdirs import user_runtime_path
from pydantic import BaseModel, ConfigDict, Field

from corvus.application.authorization import (
    AuthorityRuntimePossessionProof,
    AuthorizationRequest,
    authority_runtime_possession_digest,
)
from corvus.domain.deployment import (
    AuthorityCommitIntent,
    AuthorityCommitState,
    AuthorityEpochCredential,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorKind,
    DeploymentInstance,
    WorkspaceAuthority,
    fixed_workspace_lock_name,
)
from corvus.infrastructure.audit_history import AuditHistoryHeads
from corvus.infrastructure.project_recovery import AuthorityCommitReceiptEvidence

_RECEIPT_NAMESPACE = UUID("fd60e877-6aaa-4988-94aa-3cf63e12d047")


class LocalAuthorityError(RuntimeError):
    pass


class AuthoritySecretStore(Protocol):
    def get(self, reference: str) -> str | None: ...

    def set(self, reference: str, value: str) -> None: ...


class LiveAuthorityRootVerifier(Protocol):
    def verify_live_root(
        self,
        *,
        workspace_id: UUID,
        authority_generation: int,
        expected_root: str,
    ) -> object: ...


class KeyringAuthoritySecretStore:
    @staticmethod
    def _location(reference: str) -> tuple[str, str]:
        prefix = "keyring://"
        if not reference.startswith(prefix):
            raise LocalAuthorityError("authority_secret_reference_invalid")
        location = reference[len(prefix) :]
        service, separator, account = location.partition("/")
        if not separator or not service or not account:
            raise LocalAuthorityError("authority_secret_reference_invalid")
        return service, account

    def get(self, reference: str) -> str | None:
        service, account = self._location(reference)
        try:
            return keyring.get_password(service, account)
        except KeyringError as exc:
            raise LocalAuthorityError("authority_keyring_unavailable") from exc

    def set(self, reference: str, value: str) -> None:
        service, account = self._location(reference)
        try:
            keyring.set_password(service, account, value)
        except KeyringError as exc:
            raise LocalAuthorityError("authority_keyring_unavailable") from exc


class SealedAuthorityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    deployment_instance_id: UUID
    authority_epoch_credential_id: UUID
    epoch: int = Field(ge=1)
    generation: int = Field(ge=0)
    state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    device_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pending_intent_id: UUID | None = None
    pending_mutation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending_generation: int | None = Field(default=None, ge=1)
    pending_state_root: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    checkpoint_history_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_binding_history_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending_checkpoint_history_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending_result_binding_history_head: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    last_finalized_intent_id: UUID | None = None
    last_receipt_id: UUID | None = None
    last_receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FixedWorkspaceFileLock:
    def __init__(self, *, root: Path, lock_name: str) -> None:
        digest = hashlib.sha256(lock_name.encode("utf-8")).hexdigest()
        self.path = root / f"{digest}.lock"
        self._ensure_lock_file()

    def _ensure_lock_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            for _ in range(100):
                try:
                    if self.path.stat().st_size >= 1:
                        return
                except FileNotFoundError:
                    pass
                time.sleep(0.01)
            raise LocalAuthorityError(
                "workspace_authority_lock_initialization_incomplete"
            ) from None
        try:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def held(self) -> Iterator[None]:
        handle = self.path.open("r+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _authority_lock_root() -> Path:
    return user_runtime_path("corvus", ensure_exists=True) / "authority-locks"


class SealedLocalAuthorityAnchor:
    def __init__(
        self,
        *,
        authority: WorkspaceAuthority,
        trust_anchor: AuthorityTrustAnchor,
        deployment_instance: DeploymentInstance,
        epoch_credential: AuthorityEpochCredential,
        secret_store: AuthoritySecretStore,
        live_root_verifier: LiveAuthorityRootVerifier,
    ) -> None:
        expected_lock = fixed_workspace_lock_name(authority.workspace_id, authority.epoch)
        if (
            trust_anchor.kind is not AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION
            or trust_anchor.workspace_id != authority.workspace_id
            or trust_anchor.id != authority.trust_anchor_id
            or trust_anchor.local_lock_name != expected_lock
            or trust_anchor.sealed_generation_ref is None
            or deployment_instance.id != authority.deployment_instance_id
            or epoch_credential.id != authority.authority_epoch_credential_id
            or epoch_credential.workspace_id != authority.workspace_id
            or epoch_credential.authority_epoch != authority.epoch
            or epoch_credential.deployment_instance_id != deployment_instance.id
            or trust_anchor.device_binding_digest != deployment_instance.device_binding_digest
            or epoch_credential.device_binding_digest != deployment_instance.device_binding_digest
        ):
            raise LocalAuthorityError("sealed_authority_binding_mismatch")
        self.authority = authority
        self.trust_anchor = trust_anchor
        self.deployment_instance = deployment_instance
        self.epoch_credential = epoch_credential
        self.secret_store = secret_store
        self.live_root_verifier = live_root_verifier
        self.lock = FixedWorkspaceFileLock(root=_authority_lock_root(), lock_name=expected_lock)

    def bootstrap(self, *, audit_history_heads: AuditHistoryHeads | None = None) -> None:
        with self.lock.held():
            if self.secret_store.get(self._state_reference) is not None:
                raise LocalAuthorityError("sealed_authority_already_initialized")
            if audit_history_heads is None:
                audit_history_heads = AuditHistoryHeads(
                    checkpoint_history_head="0" * 64,
                    result_binding_history_head="0" * 64,
                )
            self._save(
                SealedAuthorityState(
                    workspace_id=self.authority.workspace_id,
                    deployment_instance_id=self.deployment_instance.id,
                    authority_epoch_credential_id=self.epoch_credential.id,
                    epoch=self.authority.epoch,
                    generation=self.authority.authority_generation,
                    state_root=self.authority.authority_state_root,
                    device_binding_digest=self.deployment_instance.device_binding_digest,
                    checkpoint_history_head=audit_history_heads.checkpoint_history_head,
                    result_binding_history_head=audit_history_heads.result_binding_history_head,
                )
            )

    @property
    def _state_reference(self) -> str:
        reference = self.trust_anchor.sealed_generation_ref
        if reference is None:  # pragma: no cover - constructor invariant
            raise LocalAuthorityError("sealed_generation_reference_missing")
        return reference

    def _load(self) -> SealedAuthorityState:
        payload = self.secret_store.get(self._state_reference)
        if payload is None:
            raise LocalAuthorityError("sealed_authority_state_missing")
        try:
            state = SealedAuthorityState.model_validate_json(payload)
        except ValueError as exc:
            raise LocalAuthorityError("sealed_authority_state_invalid") from exc
        if (
            state.workspace_id != self.authority.workspace_id
            or state.deployment_instance_id != self.deployment_instance.id
            or state.authority_epoch_credential_id != self.epoch_credential.id
            or state.epoch != self.authority.epoch
            or state.device_binding_digest != self.deployment_instance.device_binding_digest
        ):
            raise LocalAuthorityError("sealed_authority_state_substituted")
        return state

    def _save(self, state: SealedAuthorityState) -> None:
        self.secret_store.set(self._state_reference, state.model_dump_json())

    @staticmethod
    def _receipt(intent: AuthorityCommitIntent) -> AuthorityCommitReceiptEvidence:
        receipt_id = uuid5(_RECEIPT_NAMESPACE, str(intent.id))
        encoded = json.dumps(
            {
                "intent_id": str(intent.id),
                "workspace_id": str(intent.workspace_id),
                "epoch": intent.epoch,
                "generation": intent.next_generation,
                "state_root": intent.proposed_state_root,
                "mutation_digest": intent.mutation_digest,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return AuthorityCommitReceiptEvidence(
            id=receipt_id,
            digest=hashlib.sha256(encoded).hexdigest(),
        )

    def reserve(
        self,
        intent: AuthorityCommitIntent,
        *,
        prior_audit_history_heads: AuditHistoryHeads | None = None,
        next_audit_history_heads: AuditHistoryHeads | None = None,
    ) -> None:
        with self.lock.held():
            state = self._load()
            if prior_audit_history_heads is None:
                if (
                    state.checkpoint_history_head is None
                    or state.result_binding_history_head is None
                ):
                    raise LocalAuthorityError("sealed_audit_histories_uninitialized")
                prior_audit_history_heads = AuditHistoryHeads(
                    checkpoint_history_head=state.checkpoint_history_head,
                    result_binding_history_head=state.result_binding_history_head,
                )
            if next_audit_history_heads is None:
                if (
                    state.pending_checkpoint_history_head is not None
                    and state.pending_result_binding_history_head is not None
                ):
                    next_audit_history_heads = AuditHistoryHeads(
                        checkpoint_history_head=state.pending_checkpoint_history_head,
                        result_binding_history_head=state.pending_result_binding_history_head,
                    )
                else:
                    next_audit_history_heads = prior_audit_history_heads
            if state.pending_intent_id == intent.id:
                if (
                    state.pending_mutation_digest != intent.mutation_digest
                    or state.pending_generation != intent.next_generation
                    or state.pending_state_root != intent.proposed_state_root
                    or state.pending_checkpoint_history_head
                    != next_audit_history_heads.checkpoint_history_head
                    or state.pending_result_binding_history_head
                    != next_audit_history_heads.result_binding_history_head
                ):
                    raise LocalAuthorityError("sealed_authority_reservation_replay_mismatch")
                if (
                    state.checkpoint_history_head
                    != prior_audit_history_heads.checkpoint_history_head
                    or state.result_binding_history_head
                    != prior_audit_history_heads.result_binding_history_head
                ):
                    raise LocalAuthorityError("sealed_audit_history_mismatch")
                return
            if state.pending_intent_id is not None:
                raise LocalAuthorityError("sealed_authority_reservation_busy")
            if (
                intent.workspace_id != state.workspace_id
                or intent.deployment_instance_id != state.deployment_instance_id
                or intent.epoch != state.epoch
                or intent.prior_generation != state.generation
                or intent.prior_state_root != state.state_root
                or intent.next_generation != state.generation + 1
                or state.checkpoint_history_head
                != prior_audit_history_heads.checkpoint_history_head
                or state.result_binding_history_head
                != prior_audit_history_heads.result_binding_history_head
            ):
                raise LocalAuthorityError("sealed_authority_prior_state_mismatch")
            self._save(
                state.model_copy(
                    update={
                        "pending_intent_id": intent.id,
                        "pending_mutation_digest": intent.mutation_digest,
                        "pending_generation": intent.next_generation,
                        "pending_state_root": intent.proposed_state_root,
                        "pending_checkpoint_history_head": next_audit_history_heads.checkpoint_history_head,
                        "pending_result_binding_history_head": next_audit_history_heads.result_binding_history_head,
                    }
                )
            )

    def finalize(self, intent: AuthorityCommitIntent) -> AuthorityCommitReceiptEvidence:
        if intent.state not in {
            AuthorityCommitState.DB_COMMITTED,
            AuthorityCommitState.ANCHOR_FINALIZED,
        }:
            raise LocalAuthorityError("sealed_authority_finalize_state_invalid")
        evidence = self._receipt(intent)
        with self.lock.held():
            state = self._load()
            if state.last_finalized_intent_id == intent.id:
                if (
                    state.last_receipt_id != evidence.id
                    or state.last_receipt_digest != evidence.digest
                ):
                    raise LocalAuthorityError("sealed_authority_receipt_mismatch")
                return evidence
            if (
                state.pending_intent_id != intent.id
                or state.pending_mutation_digest != intent.mutation_digest
                or state.pending_generation != intent.next_generation
                or state.pending_state_root != intent.proposed_state_root
                or state.generation != intent.prior_generation
                or state.state_root != intent.prior_state_root
            ):
                raise LocalAuthorityError("sealed_authority_finalization_mismatch")
            self._save(
                state.model_copy(
                    update={
                        "generation": intent.next_generation,
                        "state_root": intent.proposed_state_root,
                        "pending_intent_id": None,
                        "pending_mutation_digest": None,
                        "pending_generation": None,
                        "pending_state_root": None,
                        "pending_checkpoint_history_head": state.pending_checkpoint_history_head,
                        "pending_result_binding_history_head": state.pending_result_binding_history_head,
                        "last_finalized_intent_id": intent.id,
                        "last_receipt_id": evidence.id,
                        "last_receipt_digest": evidence.digest,
                    }
                )
            )
        return evidence

    def finalize_audit_histories(
        self,
        intent: AuthorityCommitIntent,
        next_audit_history_heads: AuditHistoryHeads,
    ) -> None:
        if intent.state not in {
            AuthorityCommitState.DB_COMMITTED,
            AuthorityCommitState.ANCHOR_FINALIZED,
        }:
            raise LocalAuthorityError("sealed_audit_histories_state_invalid")
        with self.lock.held():
            state = self._load()
            if state.last_finalized_intent_id != intent.id:
                raise LocalAuthorityError("sealed_audit_history_not_ready")
            if (
                state.pending_checkpoint_history_head is None
                or state.pending_result_binding_history_head is None
            ):
                if (
                    state.checkpoint_history_head
                    == next_audit_history_heads.checkpoint_history_head
                    and state.result_binding_history_head
                    == next_audit_history_heads.result_binding_history_head
                ):
                    return
                raise LocalAuthorityError("sealed_audit_history_mismatch")
            pending_matches_next = (
                state.pending_checkpoint_history_head
                == next_audit_history_heads.checkpoint_history_head
                and state.pending_result_binding_history_head
                == next_audit_history_heads.result_binding_history_head
            )
            pending_matches_prior = (
                state.pending_checkpoint_history_head == state.checkpoint_history_head
                and state.pending_result_binding_history_head == state.result_binding_history_head
            )
            if not pending_matches_next and not pending_matches_prior:
                raise LocalAuthorityError("sealed_audit_history_mismatch")
            self._save(
                state.model_copy(
                    update={
                        "checkpoint_history_head": next_audit_history_heads.checkpoint_history_head,
                        "result_binding_history_head": next_audit_history_heads.result_binding_history_head,
                        "pending_checkpoint_history_head": None,
                        "pending_result_binding_history_head": None,
                    }
                )
            )

    def current_audit_history_heads(self) -> AuditHistoryHeads:
        with self.lock.held():
            state = self._load()
            if state.checkpoint_history_head is None or state.result_binding_history_head is None:
                raise LocalAuthorityError("sealed_audit_histories_uninitialized")
            return AuditHistoryHeads(
                checkpoint_history_head=state.checkpoint_history_head,
                result_binding_history_head=state.result_binding_history_head,
            )

    def issue_runtime_proof(
        self,
        request: AuthorizationRequest,
        *,
        nonce_digest: str,
        expires_at: datetime,
    ) -> AuthorityRuntimePossessionProof:
        with self.lock.held():
            state = self._load()
            if state.pending_intent_id is not None:
                raise LocalAuthorityError("sealed_authority_transition_in_progress")
            if (
                state.pending_checkpoint_history_head is not None
                or state.pending_result_binding_history_head is not None
            ):
                raise LocalAuthorityError("sealed_audit_history_transition_in_progress")
            if (
                request.workspace_id != state.workspace_id
                or request.deployment_instance_id != state.deployment_instance_id
                or request.authority_epoch_credential_id != state.authority_epoch_credential_id
                or request.workspace_authority_epoch != state.epoch
                or request.workspace_authority_generation != state.generation
                or request.authority_state_root != state.state_root
            ):
                raise LocalAuthorityError("sealed_authority_runtime_request_mismatch")
            try:
                self.live_root_verifier.verify_live_root(
                    workspace_id=state.workspace_id,
                    authority_generation=state.generation,
                    expected_root=state.state_root,
                )
            except Exception as exc:
                raise LocalAuthorityError("authority_live_root_verification_failed") from exc
            proof = AuthorityRuntimePossessionProof(
                request_context_id=request.request_context_id,
                workspace_id=request.workspace_id,
                deployment_instance_id=request.deployment_instance_id,
                authority_epoch_credential_id=request.authority_epoch_credential_id,
                authority_epoch=request.workspace_authority_epoch,
                authority_generation=request.workspace_authority_generation,
                authority_state_root=request.authority_state_root,
                device_binding_digest=state.device_binding_digest,
                lock_name=fixed_workspace_lock_name(state.workspace_id, state.epoch),
                nonce_digest=nonce_digest,
                issued_at=request.evaluated_at,
                expires_at=expires_at,
                deployment_instance_signature="pending",
                epoch_credential_signature="pending",
            )
            message = bytes.fromhex(authority_runtime_possession_digest(proof))
            instance_key = self._private_key(
                self.deployment_instance.non_exportable_activation_key_ref
            )
            epoch_key = self._private_key(self.epoch_credential.non_exportable_private_key_ref)
            return proof.model_copy(
                update={
                    "deployment_instance_signature": base64.b64encode(
                        instance_key.sign(message)
                    ).decode("ascii"),
                    "epoch_credential_signature": base64.b64encode(epoch_key.sign(message)).decode(
                        "ascii"
                    ),
                }
            )

    def _private_key(self, reference: str) -> Ed25519PrivateKey:
        encoded = self.secret_store.get(reference)
        if encoded is None:
            raise LocalAuthorityError("authority_private_key_missing")
        try:
            raw = base64.b64decode(encoded, validate=True)
            return Ed25519PrivateKey.from_private_bytes(raw)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise LocalAuthorityError("authority_private_key_invalid") from exc
