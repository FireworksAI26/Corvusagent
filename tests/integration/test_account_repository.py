from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from corvus.application.identity import IdentityService, IdentityServiceError
from corvus.database import M1_AUTHORITY_FAMILY_NAMES, DatabaseState, classify_database
from corvus.domain.access import AccessBundle, CapabilityEffect, CapabilityGrant
from corvus.domain.account import (
    Account,
    DeviceRegistration,
    DeviceStatus,
    ExperienceKind,
    ExternalIdentity,
    SessionRecord,
    SessionStatus,
)
from corvus.domain.identity import (
    MembershipStatus,
    Principal,
    PrincipalKind,
    Workspace,
    WorkspaceKind,
    WorkspaceMembership,
)
from corvus.infrastructure.db import (
    M1_AUDIT_PROOF_MANIFEST_REVISION,
    M1_CURRENT_REVISION,
    M2_IDENTITY_CONTINUITY_REVISION,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from corvus.infrastructure.repositories.accounts import AccountRepository, AccountRepositoryError
from corvus.infrastructure.repositories.authorization_inputs import AuthorizationInputRepository
from corvus.infrastructure.repositories.identity_scope import (
    IdentityScopeRepository,
    IdentityScopeRepositoryError,
)
from corvus.store import TraceStore

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_GOOGLE_ISSUER = "https://accounts.google.com"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    assert upgrade_database(database) == M1_CURRENT_REVISION
    return database


def _principal(*, subject: str) -> Principal:
    return Principal(
        kind=PrincipalKind.USER,
        external_provider="corvus-account",
        external_subject=subject,
        display_name="Lucas",
        created_at=_NOW,
    )


def _account(principal: Principal, *, email: str = "lucas@example.com") -> Account:
    return Account(
        principal_id=principal.id,
        normalized_email=email,
        experience_kind=ExperienceKind.DEVELOPER,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _preprovisioned_account(repository: AccountRepository) -> Account:
    principal = _principal(subject=f"preprovisioned:{uuid4()}")
    account = _account(principal)
    repository.create_preprovisioned_account(principal=principal, account=account)
    return account


def _stale_device_session(
    tmp_path: Path,
) -> tuple[Path, AccountRepository, Account, DeviceRegistration, SessionRecord]:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    account = _preprovisioned_account(repository)
    device = DeviceRegistration(
        account_id=account.id,
        name="Versioned device",
        public_key_digest="f" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.append_device(device)
    issued = SessionRecord(
        account_id=account.id,
        device_id=device.id,
        device_version=device.version,
        token_digest="1" * 64,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    repository.create_session(issued)
    current = device.model_copy(update={"version": 2, "updated_at": _NOW + timedelta(minutes=1)})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO device_registrations "
            "(id, account_id, version, name, public_key_digest, status, revoked_at, "
            "created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                str(current.id),
                str(current.account_id),
                current.version,
                current.name,
                current.public_key_digest,
                current.status.value,
                current.created_at.isoformat(),
                current.updated_at.isoformat(),
                current.model_dump_json(),
            ),
        )
    return database, repository, account, device, issued


def test_google_identity_creates_user_principal_account_and_identity_atomically(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    service = IdentityService(repository)

    account = service.complete_google_identity(
        issuer=_GOOGLE_ISSUER,
        subject="google-new-user",
        email=" Lucas@Example.COM ",
        email_verified=True,
        display_name="Lucas",
        now=_NOW,
    )
    repeated = service.complete_google_identity(
        issuer=_GOOGLE_ISSUER,
        subject="google-new-user",
        email="lucas@example.com",
        email_verified=True,
        display_name="Changed upstream name",
        now=_NOW + timedelta(minutes=1),
    )

    assert repeated == account
    assert account.normalized_email == "lucas@example.com"
    assert account.experience_kind is None
    principal = repository.get_principal(account.principal_id)
    assert principal is not None
    assert principal.kind is PrincipalKind.USER
    assert repository.get_external_identity(_GOOGLE_ISSUER, "google-new-user") is not None


def test_verified_google_email_attaches_only_to_zero_identity_preprovisioned_account(
    tmp_path: Path,
) -> None:
    repository = AccountRepository(_database(tmp_path))
    expected = _preprovisioned_account(repository)
    service = IdentityService(repository)

    attached = service.complete_google_identity(
        issuer=_GOOGLE_ISSUER,
        subject="google-preprovisioned",
        email="LUCAS@example.com",
        email_verified=True,
        display_name="Lucas",
        now=_NOW,
    )

    assert attached == expected
    identities = repository.list_external_identities(expected.id)
    assert [identity.subject for identity in identities] == ["google-preprovisioned"]


@pytest.mark.parametrize(
    ("issuer", "verified", "reason"),
    [
        (_GOOGLE_ISSUER, False, "google_email_unverified"),
        ("https://login.example.com", True, "google_issuer_required"),
    ],
)
def test_google_identity_rejects_unverified_or_non_google_claims_without_writes(
    tmp_path: Path,
    issuer: str,
    verified: bool,
    reason: str,
) -> None:
    repository = AccountRepository(_database(tmp_path))
    service = IdentityService(repository)

    with pytest.raises(IdentityServiceError, match=reason):
        service.complete_google_identity(
            issuer=issuer,
            subject="subject",
            email="lucas@example.com",
            email_verified=verified,
            display_name="Lucas",
            now=_NOW,
        )

    assert repository.get_account_by_email("lucas@example.com") is None


def test_google_identity_rejects_email_link_when_account_already_has_an_identity(
    tmp_path: Path,
) -> None:
    repository = AccountRepository(_database(tmp_path))
    account = _preprovisioned_account(repository)
    repository.append_external_identity(
        ExternalIdentity(
            account_id=account.id,
            issuer=_GOOGLE_ISSUER,
            subject="existing-subject",
            normalized_email=account.normalized_email,
            email_verified=True,
            created_at=_NOW,
        )
    )

    with pytest.raises(IdentityServiceError, match="identity_email_link_conflict"):
        IdentityService(repository).complete_google_identity(
            issuer=_GOOGLE_ISSUER,
            subject="different-subject",
            email=account.normalized_email,
            email_verified=True,
            display_name="Lucas",
            now=_NOW,
        )

    assert repository.get_external_identity(_GOOGLE_ISSUER, "different-subject") is None


def test_external_identity_issuer_subject_is_globally_unique(tmp_path: Path) -> None:
    repository = AccountRepository(_database(tmp_path))
    first = _preprovisioned_account(repository)
    second_principal = _principal(subject=f"preprovisioned:{uuid4()}")
    second = _account(second_principal, email="other@example.com")
    repository.create_preprovisioned_account(principal=second_principal, account=second)
    identity = ExternalIdentity(
        account_id=first.id,
        issuer=_GOOGLE_ISSUER,
        subject="unique-subject",
        normalized_email=first.normalized_email,
        email_verified=True,
        created_at=_NOW,
    )
    repository.append_external_identity(identity)

    with pytest.raises(AccountRepositoryError, match="external_identity_conflict"):
        repository.append_external_identity(
            identity.model_copy(
                update={
                    "account_id": second.id,
                    "normalized_email": second.normalized_email,
                }
            )
        )


def test_membership_access_uses_existing_bundle_and_grant_contracts_and_is_tenant_scoped(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    identities = IdentityScopeRepository(database)
    authorization = AuthorizationInputRepository(database)
    workspace = Workspace(
        name="Corvus Team",
        workspace_kind=WorkspaceKind.TEAM,
        created_at=_NOW,
        updated_at=_NOW,
    )
    foreign_workspace = Workspace(
        name="Foreign",
        workspace_kind=WorkspaceKind.INDIVIDUAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    principal = _principal(subject="membership-principal")
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        principal_id=principal.id,
        role="owner",
        status=MembershipStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    bundle = AccessBundle(
        workspace_id=workspace.id,
        principal_id=principal.id,
        scope_kind="workspace",
        scope_id=workspace.id,
        issued_by=principal.id,
        policy_digest="c" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    grant = CapabilityGrant(
        bundle_id=bundle.id,
        workspace_id=workspace.id,
        resource_kind="workspace",
        resource_id=workspace.id,
        action="workspace.manage",
        effect=CapabilityEffect.ALLOW,
        created_at=_NOW,
    )

    identities.append_workspace(workspace)
    identities.append_workspace(foreign_workspace)
    identities.append_principal(principal)
    identities.append_membership(membership)
    authorization.append_access_bundle(bundle, [grant])

    assert identities.get_workspace(workspace.id) == workspace
    assert identities.get_membership_access(workspace.id, principal.id) == ((bundle, (grant,)),)
    assert identities.get_membership(foreign_workspace.id, principal.id) is None
    assert identities.get_membership_access(foreign_workspace.id, principal.id) == ()


@pytest.mark.parametrize(
    ("role", "action"),
    [
        ("Owner", "workspace.manage"),
        ("ADMIN", "workspace.manage"),
        ("manager", "project.create"),
        ("Member", "project.read"),
        ("viewer", "project.read"),
    ],
)
def test_membership_role_ceiling_accepts_only_approved_actions_case_insensitively(
    tmp_path: Path,
    role: str,
    action: str,
) -> None:
    database = _database(tmp_path)
    identities = IdentityScopeRepository(database)
    authorization = AuthorizationInputRepository(database)
    workspace = Workspace(name="Role ceiling", created_at=_NOW, updated_at=_NOW)
    principal = _principal(subject=f"role:{role}")
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        principal_id=principal.id,
        role=role,
        created_at=_NOW,
        updated_at=_NOW,
    )
    bundle = AccessBundle(
        workspace_id=workspace.id,
        principal_id=principal.id,
        scope_kind="workspace",
        scope_id=workspace.id,
        issued_by=principal.id,
        policy_digest="a" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    grant = CapabilityGrant(
        bundle_id=bundle.id,
        workspace_id=workspace.id,
        resource_kind="workspace",
        resource_id=workspace.id,
        action=action,
        effect=CapabilityEffect.ALLOW,
        created_at=_NOW,
    )
    identities.append_workspace(workspace)
    identities.append_principal(principal)
    identities.append_membership(membership)
    authorization.append_access_bundle(bundle, [grant])

    assert identities.get_membership_access(workspace.id, principal.id) == ((bundle, (grant,)),)


def test_membership_role_ceiling_rejects_out_of_role_allows_and_unknown_roles(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    identities = IdentityScopeRepository(database)
    authorization = AuthorizationInputRepository(database)
    workspace = Workspace(name="Role rejection", created_at=_NOW, updated_at=_NOW)
    viewer = _principal(subject="role:viewer-rejected")
    identities.append_workspace(workspace)
    identities.append_principal(viewer)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=viewer.id,
            role="Viewer",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    bundle = AccessBundle(
        workspace_id=workspace.id,
        principal_id=viewer.id,
        scope_kind="workspace",
        scope_id=workspace.id,
        issued_by=viewer.id,
        policy_digest="b" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    forbidden = CapabilityGrant(
        bundle_id=bundle.id,
        workspace_id=workspace.id,
        resource_kind="workspace",
        resource_id=workspace.id,
        action="workspace.manage",
        effect=CapabilityEffect.ALLOW,
        created_at=_NOW,
    )
    authorization.append_access_bundle(bundle, [forbidden])

    with pytest.raises(IdentityScopeRepositoryError, match="membership_role_capability_mismatch"):
        identities.get_membership_access(workspace.id, viewer.id)

    unknown = _principal(subject="role:unknown")
    identities.append_principal(unknown)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=unknown.id,
            role="operator",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    with pytest.raises(IdentityScopeRepositoryError, match="membership_role_capability_mismatch"):
        identities.get_membership_access(workspace.id, unknown.id)


def test_membership_role_projection_preserves_deny_grants(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    identities = IdentityScopeRepository(database)
    authorization = AuthorizationInputRepository(database)
    workspace = Workspace(name="Deny precedence", created_at=_NOW, updated_at=_NOW)
    principal = _principal(subject="role:viewer-deny")
    identities.append_workspace(workspace)
    identities.append_principal(principal)
    identities.append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=principal.id,
            role="viewer",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    bundle = AccessBundle(
        workspace_id=workspace.id,
        principal_id=principal.id,
        scope_kind="workspace",
        scope_id=workspace.id,
        issued_by=principal.id,
        policy_digest="c" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    grants = [
        CapabilityGrant(
            bundle_id=bundle.id,
            workspace_id=workspace.id,
            resource_kind="project",
            resource_id=workspace.id,
            action="project.read",
            effect=CapabilityEffect.ALLOW,
            created_at=_NOW,
        ),
        CapabilityGrant(
            bundle_id=bundle.id,
            workspace_id=workspace.id,
            resource_kind="workspace",
            resource_id=workspace.id,
            action="workspace.manage",
            effect=CapabilityEffect.DENY,
            created_at=_NOW,
        ),
    ]
    authorization.append_access_bundle(bundle, grants)

    projected = identities.get_membership_access(workspace.id, principal.id)

    assert len(projected) == 1
    assert projected[0][0] == bundle
    assert {(grant.action, grant.effect) for grant in projected[0][1]} == {
        ("project.read", CapabilityEffect.ALLOW),
        ("workspace.manage", CapabilityEffect.DENY),
    }


def test_session_rotation_is_atomic_digest_only_and_rejects_predecessor_replay(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    account = _preprovisioned_account(repository)
    device = DeviceRegistration(
        account_id=account.id,
        name="Desktop",
        public_key_digest="d" * 64,
        status=DeviceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    original_token = "raw-original-session-token"  # noqa: S105
    replacement_token = "raw-replacement-session-token"  # noqa: S105
    session = SessionRecord(
        account_id=account.id,
        device_id=device.id,
        token_digest=_digest(original_token),
        status=SessionStatus.ACTIVE,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    repository.append_device(device)
    repository.create_session(session)

    rotated = repository.rotate_session(
        account_id=account.id,
        session_id=session.id,
        presented_digest=_digest(original_token),
        replacement_digest=_digest(replacement_token),
        now=_NOW + timedelta(minutes=5),
        expires_at=_NOW + timedelta(hours=2),
    )

    assert rotated.version == 2
    assert rotated.predecessor_digest == _digest(original_token)
    assert rotated.token_digest == _digest(replacement_token)
    with pytest.raises(AccountRepositoryError, match="session_replay_detected"):
        repository.rotate_session(
            account_id=account.id,
            session_id=session.id,
            presented_digest=_digest(original_token),
            replacement_digest="e" * 64,
            now=_NOW + timedelta(minutes=6),
            expires_at=_NOW + timedelta(hours=2),
        )

    with sqlite3.connect(database) as connection:
        payloads = "\n".join(
            row[0] for row in connection.execute("SELECT payload_json FROM session_records")
        )
    assert original_token not in payloads
    assert replacement_token not in payloads


def test_device_revocation_versions_history_and_invalidates_bound_sessions(tmp_path: Path) -> None:
    repository = AccountRepository(_database(tmp_path))
    account = _preprovisioned_account(repository)
    device = DeviceRegistration(
        account_id=account.id,
        name="Desktop",
        public_key_digest="f" * 64,
        status=DeviceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    digest = "1" * 64
    session = SessionRecord(
        account_id=account.id,
        device_id=device.id,
        token_digest=digest,
        status=SessionStatus.ACTIVE,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    repository.append_device(device)
    repository.create_session(session)
    assert repository.get_active_session(account_id=account.id, token_digest=digest, now=_NOW)

    revoked = repository.revoke_device(
        account_id=account.id,
        device_id=device.id,
        revoked_at=_NOW + timedelta(minutes=1),
    )

    assert revoked.version == 2
    assert revoked.status is DeviceStatus.REVOKED
    assert (
        repository.get_active_session(account_id=account.id, token_digest=digest, now=_NOW) is None
    )
    with pytest.raises(AccountRepositoryError, match="session_device_revoked"):
        repository.rotate_session(
            account_id=account.id,
            session_id=session.id,
            presented_digest=digest,
            replacement_digest="2" * 64,
            now=_NOW + timedelta(minutes=2),
            expires_at=_NOW + timedelta(hours=2),
        )


def test_session_revocation_appends_a_version_and_denies_the_revoked_digest(tmp_path: Path) -> None:
    repository = AccountRepository(_database(tmp_path))
    account = _preprovisioned_account(repository)
    device = DeviceRegistration(
        account_id=account.id,
        name="Desktop",
        public_key_digest="3" * 64,
        status=DeviceStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    digest = "4" * 64
    session = SessionRecord(
        account_id=account.id,
        device_id=device.id,
        token_digest=digest,
        status=SessionStatus.ACTIVE,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    repository.append_device(device)
    repository.create_session(session)

    revoked = repository.revoke_session(
        account_id=account.id,
        session_id=session.id,
        presented_digest=digest,
        revoked_at=_NOW + timedelta(minutes=1),
    )

    assert revoked.version == 2
    assert revoked.status is SessionStatus.REVOKED
    assert revoked.token_digest is None
    assert revoked.predecessor_digest == digest
    assert (
        repository.get_active_session(account_id=account.id, token_digest=digest, now=_NOW) is None
    )


def test_session_schema_rejects_missing_or_cross_account_device_versions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    first = _preprovisioned_account(repository)
    second_principal = _principal(subject=f"preprovisioned:{uuid4()}")
    second = _account(second_principal, email="second-device-owner@example.com")
    repository.create_preprovisioned_account(principal=second_principal, account=second)
    device = DeviceRegistration(
        account_id=second.id,
        name="Second account device",
        public_key_digest="5" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.append_device(device)

    statement = (
        "INSERT INTO session_records "
        "(id, account_id, device_id, device_version, version, token_digest, "
        "predecessor_digest, status, issued_at, expires_at, revoked_at, payload_json) "
        "VALUES (?, ?, ?, ?, 1, ?, NULL, 'active', ?, ?, NULL, '{}')"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for device_id, digest in ((uuid4(), "6" * 64), (device.id, "7" * 64)):
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
                connection.execute(
                    statement,
                    (
                        str(uuid4()),
                        str(first.id),
                        str(device_id),
                        device.version,
                        digest,
                        _NOW.isoformat(),
                        (_NOW + timedelta(hours=1)).isoformat(),
                    ),
                )


def test_session_repository_requires_the_exact_current_device_version(tmp_path: Path) -> None:
    database, repository, account, device, issued = _stale_device_session(tmp_path)

    assert (
        repository.get_active_session(
            account_id=account.id,
            token_digest=issued.token_digest or "",
            now=_NOW + timedelta(minutes=2),
        )
        is None
    )

    stale_session = SessionRecord(
        account_id=account.id,
        device_id=device.id,
        device_version=device.version,
        token_digest="0" * 64,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    with pytest.raises(AccountRepositoryError, match="^session_device_version_stale$"):
        repository.create_session(stale_session)


def test_stale_device_version_cannot_rotate_or_mint_a_replacement(tmp_path: Path) -> None:
    database, repository, account, _, issued = _stale_device_session(tmp_path)
    replacement_digest = "2" * 64

    with pytest.raises(AccountRepositoryError, match="^session_device_version_stale$"):
        repository.rotate_session(
            account_id=account.id,
            session_id=issued.id,
            presented_digest=issued.token_digest or "",
            replacement_digest=replacement_digest,
            now=_NOW + timedelta(minutes=2),
            expires_at=_NOW + timedelta(hours=2),
        )

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM session_records WHERE id = ?", (str(issued.id),)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM session_records WHERE token_digest = ?",
                (replacement_digest,),
            ).fetchone()[0]
            == 0
        )


def test_stale_device_version_cannot_revoke_or_append_history(tmp_path: Path) -> None:
    database, repository, account, _, issued = _stale_device_session(tmp_path)

    with pytest.raises(AccountRepositoryError, match="^session_device_version_stale$"):
        repository.revoke_session(
            account_id=account.id,
            session_id=issued.id,
            presented_digest=issued.token_digest or "",
            revoked_at=_NOW + timedelta(minutes=2),
        )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT version, status, token_digest FROM session_records WHERE id = ?",
            (str(issued.id),),
        ).fetchall()
    assert rows == [(1, "active", issued.token_digest)]


def test_session_replay_classification_is_scoped_to_account_and_session(tmp_path: Path) -> None:
    repository = AccountRepository(_database(tmp_path))
    first = _preprovisioned_account(repository)
    second_principal = _principal(subject=f"preprovisioned:{uuid4()}")
    second = _account(second_principal, email="second-session-owner@example.com")
    repository.create_preprovisioned_account(principal=second_principal, account=second)
    first_device = DeviceRegistration(
        account_id=first.id,
        name="First device",
        public_key_digest="8" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    second_device = DeviceRegistration(
        account_id=second.id,
        name="Second device",
        public_key_digest="9" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.append_device(first_device)
    repository.append_device(second_device)
    first_session = SessionRecord(
        account_id=first.id,
        device_id=first_device.id,
        device_version=first_device.version,
        token_digest="a" * 64,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    second_session = SessionRecord(
        account_id=second.id,
        device_id=second_device.id,
        device_version=second_device.version,
        token_digest="b" * 64,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    repository.create_session(first_session)
    repository.create_session(second_session)

    with pytest.raises(AccountRepositoryError, match="^session_authentication_failed$"):
        repository.rotate_session(
            account_id=first.id,
            session_id=first_session.id,
            presented_digest="b" * 64,
            replacement_digest="c" * 64,
            now=_NOW + timedelta(minutes=1),
            expires_at=_NOW + timedelta(hours=2),
        )
    with pytest.raises(AccountRepositoryError, match="^session_authentication_failed$"):
        repository.revoke_session(
            account_id=first.id,
            session_id=first_session.id,
            presented_digest="b" * 64,
            revoked_at=_NOW + timedelta(minutes=1),
        )
    with pytest.raises(AccountRepositoryError, match="^session_replacement_conflict$"):
        repository.rotate_session(
            account_id=first.id,
            session_id=first_session.id,
            presented_digest="a" * 64,
            replacement_digest="b" * 64,
            now=_NOW + timedelta(minutes=1),
            expires_at=_NOW + timedelta(hours=2),
        )


def test_m2_identity_migration_is_reversible_and_manifest_covers_revocation_history(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    required_tables = {"accounts", "external_identities", "device_registrations", "session_records"}

    assert required_tables <= classify_database(database).tables
    with sqlite3.connect(database) as connection:
        latest_manifest = connection.execute(
            "SELECT id FROM authority_state_root_manifests ORDER BY schema_version DESC LIMIT 1"
        ).fetchone()
        assert latest_manifest is not None
        families = {
            row[0]
            for row in connection.execute(
                "SELECT family_name FROM authority_state_root_leaf_families "
                "WHERE manifest_version_id = ?",
                (latest_manifest[0],),
            )
        }
    assert required_tables <= families
    assert families == M1_AUTHORITY_FAMILY_NAMES

    assert (
        downgrade_database(database, M1_AUDIT_PROOF_MANIFEST_REVISION)
        == M1_AUDIT_PROOF_MANIFEST_REVISION
    )
    assert classify_database(database).state is DatabaseState.CURRENT
    with sqlite3.connect(database) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
    assert required_tables.isdisjoint(downgraded_tables)
    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT


@pytest.mark.parametrize(
    ("mutation", "target"),
    [
        ("DROP TRIGGER web_session_bindings_no_update", "trigger"),
        ("DROP TABLE platform_idempotency", "table"),
        ("ALTER TABLE account_onboarding_versions DROP COLUMN payload_json", "column"),
    ],
)
def test_m2_oauth_classifier_requires_exact_tables_columns_and_immutable_triggers(
    tmp_path: Path,
    mutation: str,
    target: str,
) -> None:
    database = _database(tmp_path)
    assert classify_database(database).state is DatabaseState.CURRENT

    with sqlite3.connect(database) as connection:
        connection.execute(mutation)

    status = classify_database(database)
    assert status.state is DatabaseState.PARTIAL, target


def test_m2_oauth_empty_downgrade_and_reupgrade_remain_supported(tmp_path: Path) -> None:
    database = _database(tmp_path)

    assert (
        downgrade_database(database, M2_IDENTITY_CONTINUITY_REVISION)
        == M2_IDENTITY_CONTINUITY_REVISION
    )
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
    assert {
        "oauth_transactions",
        "web_session_bindings",
        "account_onboarding_versions",
        "identity_idempotency",
    }.isdisjoint(tables)
    assert upgrade_database(database) == M1_CURRENT_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT


@pytest.mark.parametrize(
    "history_family",
    [
        "oauth_transactions",
        "web_session_bindings",
        "account_onboarding_versions",
        "identity_idempotency",
    ],
)
def test_m2_oauth_populated_downgrade_refuses_before_mutating_schema(
    tmp_path: Path,
    history_family: str,
) -> None:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    account: Account | None = None
    session: SessionRecord | None = None
    if history_family != "oauth_transactions":
        account = _preprovisioned_account(repository)
    if history_family == "web_session_bindings":
        assert account is not None
        device = DeviceRegistration(
            account_id=account.id,
            name="Downgrade browser",
            public_key_digest="d" * 64,
            created_at=_NOW,
            updated_at=_NOW,
        )
        repository.append_device(device)
        session = SessionRecord(
            account_id=account.id,
            device_id=device.id,
            device_version=device.version,
            token_digest="e" * 64,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
        repository.create_session(session)
    with sqlite3.connect(database) as connection:
        if history_family == "oauth_transactions":
            connection.execute(
                "INSERT INTO oauth_transactions "
                "(id, state_digest, nonce_digest, redirect_uri, encrypted_pkce_verifier, "
                "created_at, expires_at, consumed_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)",
                (
                    str(uuid4()),
                    "a" * 64,
                    "b" * 64,
                    "https://corvus.example/api/v2/auth/google/callback",
                    "encrypted",
                    _NOW.isoformat(),
                    (_NOW + timedelta(minutes=10)).isoformat(),
                ),
            )
        elif history_family == "web_session_bindings":
            assert session is not None
            connection.execute(
                "INSERT INTO web_session_bindings "
                "(session_id, session_version, csrf_digest, created_at) VALUES (?, ?, ?, ?)",
                (str(session.id), session.version, "c" * 64, _NOW.isoformat()),
            )
        elif history_family == "account_onboarding_versions":
            assert account is not None
            connection.execute(
                "INSERT INTO account_onboarding_versions "
                "(account_id, version, experience_kind, updated_at, payload_json) "
                "VALUES (?, 2, 'developer', ?, '{}')",
                (str(account.id), _NOW.isoformat()),
            )
        else:
            assert account is not None
            connection.execute(
                "INSERT INTO platform_idempotency "
                "(account_id, scope_key, workspace_id, workspace_version, device_id, "
                "device_version, operation, idempotency_key, request_digest, result_json, "
                "created_at) VALUES (?, 'account', NULL, NULL, NULL, NULL, "
                "'test.create', 'key', ?, '{}', ?)",
                (str(account.id), "f" * 64, _NOW.isoformat()),
            )
        before_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        before_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        }
        before_family = (
            "platform_idempotency" if history_family == "identity_idempotency" else history_family
        )
        before_count = connection.execute(
            f"SELECT COUNT(*) FROM {before_family}"  # noqa: S608
        ).fetchone()[0]
    before_revision = current_revision(database)

    with pytest.raises(RuntimeError, match="oauth_session_history_present"):
        downgrade_database(database, M2_IDENTITY_CONTINUITY_REVISION)

    with sqlite3.connect(database) as connection:
        after_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        after_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        }
        after_count = connection.execute(
            f"SELECT COUNT(*) FROM {before_family}"  # noqa: S608
        ).fetchone()[0]
    assert after_tables == before_tables
    assert after_triggers == before_triggers
    assert before_count == after_count == 1
    assert current_revision(database) == before_revision == M1_CURRENT_REVISION
    assert classify_database(database).state is DatabaseState.CURRENT


def test_m2_populated_downgrade_refuses_before_removing_history_or_guards(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = AccountRepository(database)
    account = _preprovisioned_account(repository)
    repository.append_external_identity(
        ExternalIdentity(
            account_id=account.id,
            issuer=_GOOGLE_ISSUER,
            subject="downgrade-history",
            normalized_email=account.normalized_email,
            email_verified=True,
            created_at=_NOW,
        )
    )
    device = DeviceRegistration(
        account_id=account.id,
        name="Downgrade guard device",
        public_key_digest="d" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.append_device(device)
    repository.create_session(
        SessionRecord(
            account_id=account.id,
            device_id=device.id,
            device_version=device.version,
            token_digest="e" * 64,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )
    )
    required_tables = ("accounts", "external_identities", "device_registrations", "session_records")
    with sqlite3.connect(database) as connection:
        before_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in required_tables
        }
        before_manifest = connection.execute(
            "SELECT COUNT(*) FROM authority_state_root_manifests WHERE id = ?",
            ("00000000-0000-4000-8000-000000000010",),
        ).fetchone()[0]
        before_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND "
                "(name LIKE 'accounts_no_%' OR name LIKE 'external_identities_no_%' OR "
                "name LIKE 'device_registrations_no_%' OR name LIKE 'session_records_no_%')"
            )
        }

    with pytest.raises(RuntimeError, match="identity_continuity_history_present"):
        downgrade_database(database, M1_AUDIT_PROOF_MANIFEST_REVISION)

    assert M1_CURRENT_REVISION == upgrade_database(database)
    with sqlite3.connect(database) as connection:
        after_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in required_tables
        }
        after_manifest = connection.execute(
            "SELECT COUNT(*) FROM authority_state_root_manifests WHERE id = ?",
            ("00000000-0000-4000-8000-000000000010",),
        ).fetchone()[0]
        after_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND "
                "(name LIKE 'accounts_no_%' OR name LIKE 'external_identities_no_%' OR "
                "name LIKE 'device_registrations_no_%' OR name LIKE 'session_records_no_%')"
            )
        }
    assert before_counts == after_counts == {table: 1 for table in required_tables}
    assert before_manifest == after_manifest == 1
    assert before_triggers == after_triggers
    assert len(after_triggers) == 8


def test_m2_team_workspace_only_downgrade_refuses_without_changing_metadata(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    identities = IdentityScopeRepository(database)
    workspace = Workspace(
        name="Team-only M2 metadata",
        workspace_kind=WorkspaceKind.TEAM,
        created_at=_NOW,
        updated_at=_NOW,
    )
    identities.append_workspace(workspace)
    with sqlite3.connect(database) as connection:
        assert {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in (
                "accounts",
                "external_identities",
                "device_registrations",
                "session_records",
            )
        } == {
            "accounts": 0,
            "external_identities": 0,
            "device_registrations": 0,
            "session_records": 0,
        }
        before_row = connection.execute(
            "SELECT workspace_kind, payload_json FROM identity_workspaces WHERE id = ?",
            (str(workspace.id),),
        ).fetchone()
        before_manifest = connection.execute(
            "SELECT COUNT(*) FROM authority_state_root_manifests WHERE id = ?",
            ("00000000-0000-4000-8000-000000000010",),
        ).fetchone()[0]
        before_triggers = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()[0]
    assert before_row is not None
    assert before_row[0] == WorkspaceKind.TEAM.value
    assert json.loads(before_row[1])["workspace_kind"] == WorkspaceKind.TEAM.value

    with pytest.raises(RuntimeError, match="identity_continuity_workspace_metadata_present"):
        downgrade_database(database, M1_AUDIT_PROOF_MANIFEST_REVISION)

    assert upgrade_database(database) == M1_CURRENT_REVISION
    with sqlite3.connect(database) as connection:
        after_row = connection.execute(
            "SELECT workspace_kind, payload_json FROM identity_workspaces WHERE id = ?",
            (str(workspace.id),),
        ).fetchone()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM authority_state_root_manifests WHERE id = ?",
                ("00000000-0000-4000-8000-000000000010",),
            ).fetchone()[0]
            == before_manifest
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
            ).fetchone()[0]
            == before_triggers
        )
        assert "workspace_kind" in {
            row[1] for row in connection.execute("PRAGMA table_info(identity_workspaces)")
        }
    assert after_row == before_row


def test_m2_workspace_kind_column_payload_divergence_refuses_downgrade(tmp_path: Path) -> None:
    database = _database(tmp_path)
    workspace = Workspace(
        name="Divergent workspace",
        workspace_kind=WorkspaceKind.TEAM,
        created_at=_NOW,
        updated_at=_NOW,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO identity_workspaces "
            "(id, version, name, workspace_kind, status, created_at, updated_at, payload_json) "
            "VALUES (?, 1, ?, 'individual', 'active', ?, ?, ?)",
            (
                str(workspace.id),
                workspace.name,
                workspace.created_at.isoformat(),
                workspace.updated_at.isoformat(),
                workspace.model_dump_json(),
            ),
        )

    with pytest.raises(RuntimeError, match="identity_continuity_workspace_metadata_present"):
        downgrade_database(database, M1_AUDIT_PROOF_MANIFEST_REVISION)

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT workspace_kind, payload_json FROM identity_workspaces WHERE id = ?",
            (str(workspace.id),),
        ).fetchone()
    assert persisted == ("individual", workspace.model_dump_json())


def test_m2_default_individual_workspace_is_legacy_compatible_on_downgrade(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    workspace = Workspace(name="Legacy individual", created_at=_NOW, updated_at=_NOW)
    payload = workspace.model_dump(mode="json", exclude={"workspace_kind"})
    payload_json = json.dumps(payload, separators=(",", ":"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO identity_workspaces "
            "(id, version, name, workspace_kind, status, created_at, updated_at, payload_json) "
            "VALUES (?, 1, ?, 'individual', 'active', ?, ?, ?)",
            (
                str(workspace.id),
                workspace.name,
                workspace.created_at.isoformat(),
                workspace.updated_at.isoformat(),
                payload_json,
            ),
        )

    assert downgrade_database(database, M1_AUDIT_PROOF_MANIFEST_REVISION) == (
        M1_AUDIT_PROOF_MANIFEST_REVISION
    )
    with sqlite3.connect(database) as connection:
        persisted_payload = connection.execute(
            "SELECT payload_json FROM identity_workspaces WHERE id = ?",
            (str(workspace.id),),
        ).fetchone()
        assert "workspace_kind" not in {
            row[1] for row in connection.execute("PRAGMA table_info(identity_workspaces)")
        }
    assert persisted_payload == (payload_json,)


def test_repositories_accept_caller_owned_sqlalchemy_engine(tmp_path: Path) -> None:
    database = _database(tmp_path)
    engine = create_engine(f"sqlite:///{database}")
    accounts = AccountRepository(engine)
    identities = IdentityScopeRepository(engine)
    principal = _principal(subject="engine-contract")
    account = _account(principal, email="engine@example.com")

    accounts.create_preprovisioned_account(principal=principal, account=account)
    accounts.close()
    identities.close()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM accounts")) == 1
    engine.dispose()
