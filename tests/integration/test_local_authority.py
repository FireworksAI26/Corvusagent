from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corvus.application.authorization import (
    AuthorizationRequest,
    authority_runtime_possession_digest,
)
from corvus.domain.client import ClientSurface
from corvus.domain.deployment import (
    AuthorityCommitIntent,
    AuthorityCommitState,
    AuthorityEpochCredential,
    AuthorityTrustAnchor,
    AuthorityTrustAnchorKind,
    DeploymentInstance,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
    fixed_workspace_lock_name,
)
from corvus.infrastructure.audit_history import AuditHistoryHeads
from corvus.infrastructure.local_authority import (
    LocalAuthorityError,
    SealedLocalAuthorityAnchor,
)

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _history_heads(value: str) -> AuditHistoryHeads:
    return AuditHistoryHeads(
        checkpoint_history_head=value * 64,
        result_binding_history_head=value * 64,
    )


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value


class AcceptingLiveRootVerifier:
    def verify_live_root(
        self,
        *,
        workspace_id,
        authority_generation: int,
        expected_root: str,
    ) -> object:
        del workspace_id, authority_generation, expected_root
        return object()


def _private_key_value(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def _public_key_value(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _anchor(tmp_path: Path):
    workspace_id = uuid4()
    profile_id = uuid4()
    instance_key = Ed25519PrivateKey.generate()
    epoch_key = Ed25519PrivateKey.generate()
    instance_ref = "keyring://corvus-authority/instance"
    epoch_ref = "keyring://corvus-authority/epoch"
    state_ref = "keyring://corvus-authority/state"
    instance = DeploymentInstance(
        deployment_profile_id=profile_id,
        instance_public_key=_public_key_value(instance_key),
        non_exportable_activation_key_ref=instance_ref,
        device_binding_digest="a" * 64,
        activated_at=_NOW - timedelta(minutes=5),
    )
    credential = AuthorityEpochCredential(
        workspace_id=workspace_id,
        authority_epoch=1,
        deployment_instance_id=instance.id,
        public_key=_public_key_value(epoch_key),
        non_exportable_private_key_ref=epoch_ref,
        device_binding_digest=instance.device_binding_digest,
        issued_at=_NOW - timedelta(minutes=4),
    )
    trust_anchor = AuthorityTrustAnchor(
        workspace_id=workspace_id,
        kind=AuthorityTrustAnchorKind.SEALED_LOCAL_GENERATION,
        local_lock_name=fixed_workspace_lock_name(workspace_id, 1),
        sealed_generation_ref=state_ref,
        device_binding_digest=instance.device_binding_digest,
        policy_digest="b" * 64,
        created_at=_NOW - timedelta(minutes=3),
    )
    authority = WorkspaceAuthority(
        workspace_id=workspace_id,
        deployment_profile_id=profile_id,
        deployment_instance_id=instance.id,
        epoch=1,
        authority_generation=4,
        authority_state_root="c" * 64,
        authority_epoch_credential_id=credential.id,
        trust_anchor_id=trust_anchor.id,
        active_lease_id=uuid4(),
        state=WorkspaceAuthorityState.ACTIVE,
        activated_at=_NOW - timedelta(minutes=2),
    )
    secrets = MemorySecretStore()
    secrets.set(instance_ref, _private_key_value(instance_key))
    secrets.set(epoch_ref, _private_key_value(epoch_key))
    anchor = SealedLocalAuthorityAnchor(
        authority=authority,
        trust_anchor=trust_anchor,
        deployment_instance=instance,
        epoch_credential=credential,
        secret_store=secrets,
        live_root_verifier=AcceptingLiveRootVerifier(),
    )
    return anchor, authority, instance, credential, instance_key, epoch_key, secrets


def _intent(authority: WorkspaceAuthority, *, root: str = "d" * 64) -> AuthorityCommitIntent:
    return AuthorityCommitIntent(
        workspace_id=authority.workspace_id,
        epoch=authority.epoch,
        deployment_instance_id=authority.deployment_instance_id,
        prior_generation=authority.authority_generation,
        next_generation=authority.authority_generation + 1,
        prior_state_root=authority.authority_state_root,
        mutation_digest="e" * 64,
        proposed_state_root=root,
        state=AuthorityCommitState.PREPARED,
        created_at=_NOW,
    )


def _request(authority: WorkspaceAuthority) -> AuthorizationRequest:
    workspace_id = authority.workspace_id
    project_id = uuid4()
    agent_id = uuid4()
    return AuthorizationRequest(
        workspace_id=workspace_id,
        request_context_id=uuid4(),
        deployment_instance_id=authority.deployment_instance_id,
        workspace_authority_epoch=authority.epoch,
        workspace_authority_generation=authority.authority_generation,
        authority_state_root=authority.authority_state_root,
        authority_epoch_credential_id=authority.authority_epoch_credential_id,
        authority_commit_receipt_id=uuid4(),
        authority_proof_digest="f" * 64,
        trust_anchor_id=authority.trust_anchor_id,
        authority_manifest_version_id=uuid4(),
        authority_manifest_digest="1" * 64,
        kill_switch_snapshot_ids=(uuid4(), uuid4()),
        kill_switch_snapshot_digest="2" * 64,
        kill_switch_scope_bindings=(),
        audience_policy_snapshot_id=uuid4(),
        audience_policy_digest="3" * 64,
        scope_digest="4" * 64,
        client_context_id=uuid4(),
        client_surface=ClientSurface.CLI,
        transport_principal_id=uuid4(),
        requester_id=uuid4(),
        acting_agent_id=agent_id,
        scope_kind="project",
        scope_id=project_id,
        resource_kind="project",
        resource_id=project_id,
        action="project.read",
        evaluated_at=_NOW,
    )


def test_sealed_anchor_bootstrap_finalize_and_runtime_possession(tmp_path: Path) -> None:
    anchor, authority, instance, credential, instance_key, epoch_key, _ = _anchor(tmp_path)
    prior_heads = _history_heads("6")
    next_heads = AuditHistoryHeads(
        checkpoint_history_head="7" * 64,
        result_binding_history_head="8" * 64,
    )
    anchor.bootstrap(audit_history_heads=prior_heads)
    intent = _intent(authority)

    anchor.reserve(
        intent,
        prior_audit_history_heads=prior_heads,
        next_audit_history_heads=next_heads,
    )
    anchor.reserve(
        intent,
        prior_audit_history_heads=prior_heads,
        next_audit_history_heads=next_heads,
    )
    try:
        anchor.issue_runtime_proof(
            _request(authority),
            nonce_digest="5" * 64,
            expires_at=_NOW + timedelta(seconds=15),
        )
    except LocalAuthorityError as exc:
        assert str(exc) == "sealed_authority_transition_in_progress"
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("runtime proof issued during authority transition")

    committed = intent.model_copy(update={"state": AuthorityCommitState.DB_COMMITTED})
    receipt = anchor.finalize(committed)
    assert anchor.finalize(committed) == receipt

    advanced = authority.model_copy(
        update={
            "authority_generation": intent.next_generation,
            "authority_state_root": intent.proposed_state_root,
        }
    )
    request = _request(advanced)
    try:
        anchor.issue_runtime_proof(
            request,
            nonce_digest="5" * 64,
            expires_at=_NOW + timedelta(seconds=15),
        )
    except LocalAuthorityError as exc:
        assert str(exc) == "sealed_audit_history_transition_in_progress"
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("runtime proof issued before audit histories were sealed")

    for altered_result, altered_checkpoint in [
        (next_heads.result_binding_history_head, "9" * 64),
        ("9" * 64, next_heads.checkpoint_history_head),
    ]:
        try:
            anchor.finalize_audit_histories(
                committed,
                AuditHistoryHeads(
                    checkpoint_history_head=altered_checkpoint,
                    result_binding_history_head=altered_result,
                ),
            )
        except LocalAuthorityError as exc:
            assert str(exc) == "sealed_audit_history_mismatch"
        else:  # pragma: no cover - fail-closed assertion
            raise AssertionError("substituted audit history was sealed")

    anchor.finalize_audit_histories(committed, next_heads)
    assert anchor.current_audit_history_heads() == next_heads
    proof = anchor.issue_runtime_proof(
        request,
        nonce_digest="5" * 64,
        expires_at=_NOW + timedelta(seconds=15),
    )
    message = bytes.fromhex(authority_runtime_possession_digest(proof))
    instance_key.public_key().verify(base64.b64decode(proof.deployment_instance_signature), message)
    epoch_key.public_key().verify(base64.b64decode(proof.epoch_credential_signature), message)
    assert proof.deployment_instance_id == instance.id
    assert proof.authority_epoch_credential_id == credential.id


def test_sealed_anchor_rejects_rollback_and_concurrent_same_generation(tmp_path: Path) -> None:
    anchor, authority, *_ = _anchor(tmp_path)
    anchor.bootstrap()
    intents = [_intent(authority, root="d" * 64), _intent(authority, root="e" * 64)]
    barrier = Barrier(2)
    outcomes: list[str] = []

    def reserve(intent: AuthorityCommitIntent) -> None:
        barrier.wait()
        try:
            anchor.reserve(intent)
            outcomes.append("reserved")
        except LocalAuthorityError as exc:
            outcomes.append(str(exc))

    threads = [Thread(target=reserve, args=(intent,)) for intent in intents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("reserved") == 1
    assert len(outcomes) == 2
    assert any(value == "sealed_authority_reservation_busy" for value in outcomes)
