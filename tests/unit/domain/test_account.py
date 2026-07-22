from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.account import (
    Account,
    DeviceRegistration,
    DeviceStatus,
    ExperienceKind,
    ExternalIdentity,
    SessionRecord,
    SessionStatus,
    normalize_identity_email,
)
from corvus.domain.identity import Workspace, WorkspaceKind, WorkspaceMembership

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def test_account_experience_is_independent_from_workspace_presentation_kind() -> None:
    account = Account(
        principal_id=uuid4(),
        normalized_email="lucas@example.com",
        experience_kind=ExperienceKind.EVERYDAY,
        created_at=_NOW,
        updated_at=_NOW,
    )
    workspace = Workspace(
        name="Corvus",
        workspace_kind=WorkspaceKind.TEAM,
        created_at=_NOW,
        updated_at=_NOW,
    )

    assert account.experience_kind is ExperienceKind.EVERYDAY
    assert workspace.workspace_kind is WorkspaceKind.TEAM
    assert "capabilities" not in Workspace.model_fields
    assert "capabilities" not in WorkspaceMembership.model_fields


def test_account_experience_is_unselected_until_onboarding() -> None:
    account = Account(
        principal_id=uuid4(),
        normalized_email="new-user@example.com",
        created_at=_NOW,
        updated_at=_NOW,
    )

    assert account.experience_kind is None


def test_identity_models_are_frozen_and_reject_unexpected_authority_fields() -> None:
    account = Account(
        principal_id=uuid4(),
        normalized_email="lucas@example.com",
        experience_kind=ExperienceKind.DEVELOPER,
        created_at=_NOW,
        updated_at=_NOW,
    )

    with pytest.raises(ValidationError, match="frozen"):
        account.experience_kind = ExperienceKind.EVERYDAY  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Account.model_validate(
            {
                **account.model_dump(mode="python"),
                "capabilities": ["workspace.admin"],
            }
        )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (" Lucas@Example.COM ", "lucas@example.com"),
        ("LUCAS+Corvus@GMAIL.COM", "lucas+corvus@gmail.com"),
    ],
)
def test_verified_identity_email_normalization_is_bounded(
    raw: str,
    normalized: str,
) -> None:
    assert normalize_identity_email(raw) == normalized

    with pytest.raises(ValueError, match="identity_email_invalid"):
        normalize_identity_email("not-an-email")


def test_external_identity_is_explicit_and_contains_no_token_material() -> None:
    identity = ExternalIdentity(
        account_id=uuid4(),
        issuer="https://accounts.google.com",
        subject="google-subject",
        normalized_email="lucas@example.com",
        email_verified=True,
        created_at=_NOW,
    )

    assert identity.email_verified is True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExternalIdentity.model_validate(
            {
                **identity.model_dump(mode="python"),
                "access_token": "plaintext-secret",
            }
        )


def test_session_record_is_digest_only_and_has_a_versioned_lineage() -> None:
    account_id = uuid4()
    device_id = uuid4()
    session = SessionRecord(
        account_id=account_id,
        device_id=device_id,
        device_version=3,
        version=1,
        token_digest="a" * 64,
        status=SessionStatus.ACTIVE,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )

    assert session.token_digest == "a" * 64
    assert session.device_version == 3
    assert session.predecessor_digest is None
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SessionRecord.model_validate(
            {
                **session.model_dump(mode="python"),
                "token": "plaintext-session-token",
            }
        )
    with pytest.raises(ValidationError, match="active_session_digest_required"):
        SessionRecord(
            account_id=account_id,
            device_id=device_id,
            version=1,
            token_digest=None,
            status=SessionStatus.ACTIVE,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )


def test_revoked_device_requires_append_only_revocation_metadata() -> None:
    with pytest.raises(ValidationError, match="device_revoked_at_required"):
        DeviceRegistration(
            account_id=uuid4(),
            name="Desktop",
            public_key_digest="b" * 64,
            status=DeviceStatus.REVOKED,
            created_at=_NOW,
            updated_at=_NOW,
        )
