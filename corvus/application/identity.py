from __future__ import annotations

from datetime import datetime
from typing import Final

from corvus.domain.account import Account, normalize_identity_email
from corvus.infrastructure.repositories.accounts import AccountRepository, AccountRepositoryError

GOOGLE_ISSUER: Final = "https://accounts.google.com"


class IdentityServiceError(RuntimeError):
    pass


class IdentityService:
    def __init__(self, repository: AccountRepository) -> None:
        self.repository = repository

    def complete_google_identity(
        self,
        *,
        issuer: str,
        subject: str,
        email: str,
        email_verified: bool,
        display_name: str,
        now: datetime,
    ) -> Account:
        if issuer != GOOGLE_ISSUER:
            raise IdentityServiceError("google_issuer_required")
        if not email_verified:
            raise IdentityServiceError("google_email_unverified")
        if not subject.strip():
            raise IdentityServiceError("google_subject_required")
        if not display_name.strip():
            raise IdentityServiceError("google_display_name_required")
        try:
            normalized_email = normalize_identity_email(email)
        except ValueError as exc:
            raise IdentityServiceError("identity_email_invalid") from exc
        try:
            return self.repository.complete_google_identity(
                issuer=issuer,
                subject=subject,
                normalized_email=normalized_email,
                display_name=display_name,
                now=now,
            )
        except AccountRepositoryError as exc:
            raise IdentityServiceError(str(exc)) from exc
