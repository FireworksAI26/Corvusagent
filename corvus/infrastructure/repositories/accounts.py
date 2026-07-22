from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from corvus.database import DatabaseState, classify_database
from corvus.domain.account import (
    Account,
    DeviceRegistration,
    DeviceStatus,
    ExternalIdentity,
    SessionRecord,
    SessionStatus,
    normalize_identity_email,
)
from corvus.domain.identity import Principal, PrincipalKind, RecordStatus
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class AccountRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WebSessionAuthentication:
    account: Account
    session: SessionRecord
    csrf_token: str


@dataclass(frozen=True, slots=True)
class WebLoginResult(WebSessionAuthentication):
    session_token: str
    device_token: str | None


_WEB_SESSION_CSRF_LABEL = b"corvus/web-session-csrf/v1\0"


def _credential_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _csrf_token(session_token: str, session_secret: str) -> str:
    return hmac.new(
        session_secret.encode("utf-8"),
        _WEB_SESSION_CSRF_LABEL + session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _opaque_credential(identifier: UUID | None = None) -> tuple[UUID, str]:
    selected = identifier or uuid4()
    return selected, f"{selected}.{secrets.token_urlsafe(32)}"


def _credential_id(value: str) -> UUID | None:
    try:
        identifier, raw = value.split(".", maxsplit=1)
        if len(raw) < 43:
            return None
        return UUID(identifier)
    except (ValueError, AttributeError):
        return None


class AccountRepository:
    def __init__(self, database: Path | Engine) -> None:
        self._owns_engine = isinstance(database, Path)
        if isinstance(database, Path):
            revision = current_revision(database)
            if revision != M1_CURRENT_REVISION:
                raise AccountRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
            status = classify_database(database)
            if status.state is not DatabaseState.CURRENT:
                raise AccountRepositoryError(f"database_state_mismatch:{status.state.value}")
            self.engine = create_engine(f"sqlite:///{database}")
        else:
            self.engine = database
            with self.engine.connect() as connection:
                revision = MigrationContext.configure(connection).get_current_revision()
            if revision != M1_CURRENT_REVISION:
                raise AccountRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
        if self.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise AccountRepositoryError("unsupported_repository_dialect")

    @staticmethod
    def _enable_sqlite_foreign_keys(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            self._enable_sqlite_foreign_keys(connection)
            yield connection

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            self._enable_sqlite_foreign_keys(connection)
            yield connection

    @staticmethod
    def _account_from_payload(payload: str) -> Account:
        return Account.model_validate_json(payload)

    @staticmethod
    def _identity_from_payload(payload: str) -> ExternalIdentity:
        return ExternalIdentity.model_validate_json(payload)

    @staticmethod
    def _device_from_payload(payload: str) -> DeviceRegistration:
        return DeviceRegistration.model_validate_json(payload)

    @staticmethod
    def _session_from_payload(payload: str) -> SessionRecord:
        return SessionRecord.model_validate_json(payload)

    @staticmethod
    def _insert_principal(connection: Connection, principal: Principal) -> None:
        connection.execute(
            text(
                "INSERT INTO principals "
                "(id, kind, external_provider, external_subject, created_at, payload_json) "
                "VALUES (:id, :kind, :provider, :subject, :created_at, :payload_json)"
            ),
            {
                "id": str(principal.id),
                "kind": principal.kind.value,
                "provider": principal.external_provider,
                "subject": principal.external_subject,
                "created_at": principal.created_at.isoformat(),
                "payload_json": principal.model_dump_json(),
            },
        )

    @staticmethod
    def _insert_account(connection: Connection, account: Account) -> None:
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, principal_id, normalized_email, experience_kind, status, created_at, "
                "updated_at, version, payload_json) VALUES (:id, :principal_id, "
                ":normalized_email, :experience_kind, :status, :created_at, :updated_at, "
                ":version, :payload_json)"
            ),
            {
                "id": str(account.id),
                "principal_id": str(account.principal_id),
                "normalized_email": account.normalized_email,
                "experience_kind": (
                    None if account.experience_kind is None else account.experience_kind.value
                ),
                "status": account.status.value,
                "created_at": account.created_at.isoformat(),
                "updated_at": account.updated_at.isoformat(),
                "version": account.version,
                "payload_json": account.model_dump_json(),
            },
        )

    @staticmethod
    def _insert_external_identity(connection: Connection, identity: ExternalIdentity) -> None:
        connection.execute(
            text(
                "INSERT INTO external_identities "
                "(id, account_id, issuer, subject, normalized_email, email_verified, "
                "created_at, payload_json) VALUES (:id, :account_id, :issuer, :subject, "
                ":normalized_email, :email_verified, :created_at, :payload_json)"
            ),
            {
                "id": str(identity.id),
                "account_id": str(identity.account_id),
                "issuer": identity.issuer,
                "subject": identity.subject,
                "normalized_email": identity.normalized_email,
                "email_verified": identity.email_verified,
                "created_at": identity.created_at.isoformat(),
                "payload_json": identity.model_dump_json(),
            },
        )

    @staticmethod
    def _account_payload_by_id(connection: Connection, account_id: UUID) -> str | None:
        return cast(
            str | None,
            connection.scalar(
                text("SELECT payload_json FROM accounts WHERE id = :id"),
                {"id": str(account_id)},
            ),
        )

    def create_preprovisioned_account(self, *, principal: Principal, account: Account) -> None:
        if principal.kind is not PrincipalKind.USER:
            raise AccountRepositoryError("account_principal_must_be_user")
        if account.principal_id != principal.id:
            raise AccountRepositoryError("account_principal_mismatch")
        try:
            with self._transaction() as connection:
                self._insert_principal(connection, principal)
                self._insert_account(connection, account)
        except IntegrityError as exc:
            raise AccountRepositoryError("account_identity_conflict") from exc

    def get_account(self, account_id: UUID) -> Account | None:
        with self._connection() as connection:
            payload = self._account_payload_by_id(connection, account_id)
        return None if payload is None else self._account_from_payload(payload)

    def get_account_by_email(self, email: str) -> Account | None:
        try:
            normalized_email = normalize_identity_email(email)
        except ValueError as exc:
            raise AccountRepositoryError("identity_email_invalid") from exc
        with self._connection() as connection:
            payload = connection.scalar(
                text("SELECT payload_json FROM accounts WHERE normalized_email = :email"),
                {"email": normalized_email},
            )
        return None if payload is None else self._account_from_payload(payload)

    def get_principal(self, principal_id: UUID) -> Principal | None:
        with self._connection() as connection:
            payload = connection.scalar(
                text("SELECT payload_json FROM principals WHERE id = :id"),
                {"id": str(principal_id)},
            )
        return None if payload is None else Principal.model_validate_json(payload)

    def get_external_identity(self, issuer: str, subject: str) -> ExternalIdentity | None:
        with self._connection() as connection:
            payload = connection.scalar(
                text(
                    "SELECT payload_json FROM external_identities "
                    "WHERE issuer = :issuer AND subject = :subject"
                ),
                {"issuer": issuer, "subject": subject},
            )
        return None if payload is None else self._identity_from_payload(payload)

    def list_external_identities(self, account_id: UUID) -> list[ExternalIdentity]:
        with self._connection() as connection:
            payloads = connection.scalars(
                text(
                    "SELECT payload_json FROM external_identities WHERE account_id = :account_id "
                    "ORDER BY created_at, id"
                ),
                {"account_id": str(account_id)},
            ).all()
        return [self._identity_from_payload(payload) for payload in payloads]

    def append_external_identity(self, identity: ExternalIdentity) -> None:
        try:
            with self._transaction() as connection:
                account_payload = self._account_payload_by_id(connection, identity.account_id)
                if account_payload is None:
                    raise AccountRepositoryError("external_identity_account_missing")
                account = self._account_from_payload(account_payload)
                if account.normalized_email != identity.normalized_email:
                    raise AccountRepositoryError("external_identity_email_mismatch")
                self._insert_external_identity(connection, identity)
        except IntegrityError as exc:
            raise AccountRepositoryError("external_identity_conflict") from exc

    def complete_google_identity(
        self,
        *,
        issuer: str,
        subject: str,
        normalized_email: str,
        display_name: str,
        now: datetime,
    ) -> Account:
        try:
            with self._transaction() as connection:
                return self._complete_google_identity(
                    connection,
                    issuer=issuer,
                    subject=subject,
                    normalized_email=normalized_email,
                    display_name=display_name,
                    now=now,
                )
        except IntegrityError as exc:
            existing = self.get_external_identity(issuer, subject)
            if existing is not None and existing.normalized_email == normalized_email:
                resolved_account = self.get_account(existing.account_id)
                if resolved_account is not None:
                    return resolved_account
            raise AccountRepositoryError("external_identity_conflict") from exc

    def _complete_google_identity(
        self,
        connection: Connection,
        *,
        issuer: str,
        subject: str,
        normalized_email: str,
        display_name: str,
        now: datetime,
    ) -> Account:
        existing_identity_payload = connection.scalar(
            text(
                "SELECT payload_json FROM external_identities "
                "WHERE issuer = :issuer AND subject = :subject"
            ),
            {"issuer": issuer, "subject": subject},
        )
        if existing_identity_payload is not None:
            existing_identity = self._identity_from_payload(existing_identity_payload)
            if (
                not existing_identity.email_verified
                or existing_identity.normalized_email != normalized_email
            ):
                raise AccountRepositoryError("external_identity_claim_conflict")
            account_payload = self._account_payload_by_id(connection, existing_identity.account_id)
            if account_payload is None:
                raise AccountRepositoryError("external_identity_account_missing")
            return self._account_from_payload(account_payload)
        account_payload = connection.scalar(
            text("SELECT payload_json FROM accounts WHERE normalized_email = :email"),
            {"email": normalized_email},
        )
        if account_payload is not None:
            account = self._account_from_payload(account_payload)
            identity_count = connection.scalar(
                text("SELECT COUNT(*) FROM external_identities WHERE account_id = :account_id"),
                {"account_id": str(account.id)},
            )
            if identity_count != 0:
                raise AccountRepositoryError("identity_email_link_conflict")
            principal_payload = connection.scalar(
                text("SELECT payload_json FROM principals WHERE id = :id"),
                {"id": str(account.principal_id)},
            )
            if principal_payload is None or (
                Principal.model_validate_json(principal_payload).kind is not PrincipalKind.USER
            ):
                raise AccountRepositoryError("account_principal_must_be_user")
        else:
            account_id = uuid4()
            principal_id = uuid4()
            principal = Principal(
                id=principal_id,
                kind=PrincipalKind.USER,
                external_provider="corvus-account",
                external_subject=f"account:{account_id}",
                display_name=display_name.strip(),
                created_at=now,
            )
            account = Account(
                id=account_id,
                principal_id=principal_id,
                normalized_email=normalized_email,
                experience_kind=None,
                status=RecordStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            self._insert_principal(connection, principal)
            self._insert_account(connection, account)
        self._insert_external_identity(
            connection,
            ExternalIdentity(
                account_id=account.id,
                issuer=issuer,
                subject=subject,
                normalized_email=normalized_email,
                email_verified=True,
                created_at=now,
            ),
        )
        return account

    @staticmethod
    def _insert_device(connection: Connection, device: DeviceRegistration) -> None:
        connection.execute(
            text(
                "INSERT INTO device_registrations "
                "(id, account_id, version, name, public_key_digest, status, revoked_at, "
                "created_at, updated_at, payload_json) VALUES (:id, :account_id, :version, "
                ":name, :public_key_digest, :status, :revoked_at, :created_at, :updated_at, "
                ":payload_json)"
            ),
            {
                "id": str(device.id),
                "account_id": str(device.account_id),
                "version": device.version,
                "name": device.name,
                "public_key_digest": device.public_key_digest,
                "status": device.status.value,
                "revoked_at": None if device.revoked_at is None else device.revoked_at.isoformat(),
                "created_at": device.created_at.isoformat(),
                "updated_at": device.updated_at.isoformat(),
                "payload_json": device.model_dump_json(),
            },
        )

    @staticmethod
    def _latest_device(
        connection: Connection,
        *,
        account_id: UUID,
        device_id: UUID,
    ) -> DeviceRegistration | None:
        payload = connection.scalar(
            text(
                "SELECT payload_json FROM device_registrations "
                "WHERE account_id = :account_id AND id = :device_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"account_id": str(account_id), "device_id": str(device_id)},
        )
        return None if payload is None else DeviceRegistration.model_validate_json(payload)

    def append_device(self, device: DeviceRegistration) -> None:
        if device.version != 1:
            raise AccountRepositoryError("initial_device_version_required")
        try:
            with self._transaction() as connection:
                if self._account_payload_by_id(connection, device.account_id) is None:
                    raise AccountRepositoryError("device_account_missing")
                self._insert_device(connection, device)
        except IntegrityError as exc:
            raise AccountRepositoryError("device_identity_conflict") from exc

    def revoke_device(
        self,
        *,
        account_id: UUID,
        device_id: UUID,
        revoked_at: datetime,
    ) -> DeviceRegistration:
        try:
            with self._transaction() as connection:
                current = self._latest_device(
                    connection,
                    account_id=account_id,
                    device_id=device_id,
                )
                if current is None:
                    raise AccountRepositoryError("device_not_found")
                if current.status == DeviceStatus.REVOKED:
                    return current
                revoked = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "status": DeviceStatus.REVOKED,
                        "revoked_at": revoked_at,
                        "updated_at": revoked_at,
                    }
                )
                self._insert_device(connection, revoked)
                return revoked
        except IntegrityError as exc:
            raise AccountRepositoryError("device_revocation_conflict") from exc

    @staticmethod
    def _insert_session(connection: Connection, session: SessionRecord) -> None:
        connection.execute(
            text(
                "INSERT INTO session_records "
                "(id, account_id, device_id, device_version, version, token_digest, "
                "predecessor_digest, status, issued_at, expires_at, revoked_at, payload_json) "
                "VALUES (:id, :account_id, :device_id, :device_version, :version, "
                ":token_digest, :predecessor_digest, :status, :issued_at, :expires_at, "
                ":revoked_at, :payload_json)"
            ),
            {
                "id": str(session.id),
                "account_id": str(session.account_id),
                "device_id": str(session.device_id),
                "device_version": session.device_version,
                "version": session.version,
                "token_digest": session.token_digest,
                "predecessor_digest": session.predecessor_digest,
                "status": session.status.value,
                "issued_at": session.issued_at.isoformat(),
                "expires_at": session.expires_at.isoformat(),
                "revoked_at": None
                if session.revoked_at is None
                else session.revoked_at.isoformat(),
                "payload_json": session.model_dump_json(),
            },
        )

    @staticmethod
    def _latest_session(
        connection: Connection,
        *,
        account_id: UUID,
        session_id: UUID,
    ) -> SessionRecord | None:
        payload = connection.scalar(
            text(
                "SELECT payload_json FROM session_records "
                "WHERE account_id = :account_id AND id = :session_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"account_id": str(account_id), "session_id": str(session_id)},
        )
        return None if payload is None else SessionRecord.model_validate_json(payload)

    def create_session(self, session: SessionRecord) -> None:
        if session.version != 1 or session.status is not SessionStatus.ACTIVE:
            raise AccountRepositoryError("initial_active_session_required")
        try:
            with self._transaction() as connection:
                device = self._latest_device(
                    connection,
                    account_id=session.account_id,
                    device_id=session.device_id,
                )
                if device is None:
                    raise AccountRepositoryError("session_device_missing")
                if device.status is not DeviceStatus.ACTIVE:
                    raise AccountRepositoryError("session_device_revoked")
                if session.device_version != device.version:
                    raise AccountRepositoryError("session_device_version_stale")
                self._insert_session(connection, session)
        except IntegrityError as exc:
            raise AccountRepositoryError("session_identity_conflict") from exc

    @staticmethod
    def _digest_was_seen(
        connection: Connection,
        *,
        account_id: UUID,
        session_id: UUID,
        digest: str,
    ) -> bool:
        count = connection.scalar(
            text(
                "SELECT COUNT(*) FROM session_records "
                "WHERE account_id = :account_id AND id = :session_id "
                "AND (token_digest = :digest OR predecessor_digest = :digest)"
            ),
            {
                "account_id": str(account_id),
                "session_id": str(session_id),
                "digest": digest,
            },
        )
        return bool(count)

    @staticmethod
    def _replacement_digest_conflicts(connection: Connection, digest: str) -> bool:
        count = connection.scalar(
            text(
                "SELECT COUNT(*) FROM session_records "
                "WHERE token_digest = :digest OR predecessor_digest = :digest"
            ),
            {"digest": digest},
        )
        return bool(count)

    def get_active_session(
        self,
        *,
        account_id: UUID,
        token_digest: str,
        now: datetime,
    ) -> SessionRecord | None:
        with self._connection() as connection:
            payload = connection.scalar(
                text(
                    "SELECT payload_json FROM session_records "
                    "WHERE account_id = :account_id AND token_digest = :digest"
                ),
                {"account_id": str(account_id), "digest": token_digest},
            )
            if payload is None:
                return None
            matched = self._session_from_payload(payload)
            current = self._latest_session(
                connection,
                account_id=account_id,
                session_id=matched.id,
            )
            if (
                current is None
                or current.version != matched.version
                or current.status is not SessionStatus.ACTIVE
                or current.expires_at <= now
            ):
                return None
            device = self._latest_device(
                connection,
                account_id=account_id,
                device_id=current.device_id,
            )
            if (
                device is None
                or device.status is not DeviceStatus.ACTIVE
                or current.device_version != device.version
            ):
                return None
            return current

    def rotate_session(
        self,
        *,
        account_id: UUID,
        session_id: UUID,
        presented_digest: str,
        replacement_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        if replacement_digest == presented_digest:
            raise AccountRepositoryError("session_replacement_conflict")
        try:
            with self._transaction() as connection:
                current = self._latest_session(
                    connection,
                    account_id=account_id,
                    session_id=session_id,
                )
                if current is None:
                    raise AccountRepositoryError("session_not_found")
                if current.token_digest != presented_digest:
                    reason = (
                        "session_replay_detected"
                        if self._digest_was_seen(
                            connection,
                            account_id=account_id,
                            session_id=session_id,
                            digest=presented_digest,
                        )
                        else "session_authentication_failed"
                    )
                    raise AccountRepositoryError(reason)
                if current.status is not SessionStatus.ACTIVE or current.expires_at <= now:
                    raise AccountRepositoryError("session_inactive")
                device = self._latest_device(
                    connection,
                    account_id=account_id,
                    device_id=current.device_id,
                )
                if device is None or device.status == DeviceStatus.REVOKED:
                    raise AccountRepositoryError("session_device_revoked")
                if current.device_version != device.version:
                    raise AccountRepositoryError("session_device_version_stale")
                if self._replacement_digest_conflicts(connection, replacement_digest):
                    raise AccountRepositoryError("session_replacement_conflict")
                rotated = SessionRecord(
                    id=current.id,
                    account_id=current.account_id,
                    device_id=current.device_id,
                    device_version=device.version,
                    version=current.version + 1,
                    token_digest=replacement_digest,
                    predecessor_digest=presented_digest,
                    status=SessionStatus.ACTIVE,
                    issued_at=now,
                    expires_at=expires_at,
                )
                self._insert_session(connection, rotated)
                return rotated
        except IntegrityError as exc:
            raise AccountRepositoryError("session_replacement_conflict") from exc

    def revoke_session(
        self,
        *,
        account_id: UUID,
        session_id: UUID,
        presented_digest: str,
        revoked_at: datetime,
    ) -> SessionRecord:
        try:
            with self._transaction() as connection:
                current = self._latest_session(
                    connection,
                    account_id=account_id,
                    session_id=session_id,
                )
                if current is None:
                    raise AccountRepositoryError("session_not_found")
                if current.token_digest != presented_digest:
                    reason = (
                        "session_replay_detected"
                        if self._digest_was_seen(
                            connection,
                            account_id=account_id,
                            session_id=session_id,
                            digest=presented_digest,
                        )
                        else "session_authentication_failed"
                    )
                    raise AccountRepositoryError(reason)
                device = self._latest_device(
                    connection,
                    account_id=account_id,
                    device_id=current.device_id,
                )
                if device is None:
                    raise AccountRepositoryError("session_device_missing")
                if current.device_version != device.version:
                    raise AccountRepositoryError("session_device_version_stale")
                revoked = SessionRecord(
                    id=current.id,
                    account_id=current.account_id,
                    device_id=current.device_id,
                    device_version=device.version,
                    version=current.version + 1,
                    token_digest=None,
                    predecessor_digest=presented_digest,
                    status=SessionStatus.REVOKED,
                    issued_at=current.issued_at,
                    expires_at=current.expires_at,
                    revoked_at=revoked_at,
                )
                self._insert_session(connection, revoked)
                return revoked
        except IntegrityError as exc:
            raise AccountRepositoryError("session_revocation_conflict") from exc

    @staticmethod
    def _latest_session_by_id(
        connection: Connection,
        session_id: UUID,
    ) -> SessionRecord | None:
        payload = connection.scalar(
            text(
                "SELECT payload_json FROM session_records WHERE id = :session_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"session_id": str(session_id)},
        )
        return None if payload is None else SessionRecord.model_validate_json(payload)

    def _authenticate_web_session(
        self,
        connection: Connection,
        *,
        session_token: str,
        session_secret: str,
        now: datetime,
    ) -> WebSessionAuthentication:
        session_id = _credential_id(session_token)
        if session_id is None:
            raise AccountRepositoryError("session_authentication_failed")
        session = self._latest_session_by_id(connection, session_id)
        presented_digest = _credential_digest(session_token)
        if session is None:
            raise AccountRepositoryError("session_authentication_failed")
        if session.token_digest is None or not hmac.compare_digest(
            session.token_digest, presented_digest
        ):
            if self._digest_was_seen(
                connection,
                account_id=session.account_id,
                session_id=session.id,
                digest=presented_digest,
            ):
                raise AccountRepositoryError("session_replay_detected")
            raise AccountRepositoryError("session_authentication_failed")
        if session.status is not SessionStatus.ACTIVE or session.expires_at <= now:
            raise AccountRepositoryError("session_inactive")
        device = self._latest_device(
            connection,
            account_id=session.account_id,
            device_id=session.device_id,
        )
        if device is None or device.status is not DeviceStatus.ACTIVE:
            raise AccountRepositoryError("session_device_revoked")
        if session.device_version != device.version:
            raise AccountRepositoryError("session_device_version_stale")
        csrf = _csrf_token(session_token, session_secret)
        persisted_csrf = connection.scalar(
            text(
                "SELECT csrf_digest FROM web_session_bindings "
                "WHERE session_id = :session_id AND session_version = :session_version"
            ),
            {"session_id": str(session.id), "session_version": session.version},
        )
        if not isinstance(persisted_csrf, str) or not hmac.compare_digest(
            persisted_csrf, _credential_digest(csrf)
        ):
            raise AccountRepositoryError("session_binding_invalid")
        account_payload = self._account_payload_by_id(connection, session.account_id)
        if account_payload is None:
            raise AccountRepositoryError("session_account_missing")
        return WebSessionAuthentication(
            account=self._account_from_payload(account_payload),
            session=session,
            csrf_token=csrf,
        )

    def complete_web_login(
        self,
        *,
        issuer: str,
        subject: str,
        normalized_email: str,
        display_name: str,
        existing_device_token: str | None,
        session_secret: str,
        now: datetime,
        expires_at: datetime,
    ) -> WebLoginResult:
        try:
            with self._transaction() as connection:
                account = self._complete_google_identity(
                    connection,
                    issuer=issuer,
                    subject=subject,
                    normalized_email=normalized_email,
                    display_name=display_name,
                    now=now,
                )
                device: DeviceRegistration | None = None
                if existing_device_token is not None:
                    device_id = _credential_id(existing_device_token)
                    if device_id is not None:
                        candidate = self._latest_device(
                            connection,
                            account_id=account.id,
                            device_id=device_id,
                        )
                        if (
                            candidate is not None
                            and candidate.status == DeviceStatus.ACTIVE
                            and hmac.compare_digest(
                                candidate.public_key_digest,
                                _credential_digest(existing_device_token),
                            )
                        ):
                            device = candidate
                new_device_token: str | None = None
                if device is None:
                    device_id, new_device_token = _opaque_credential()
                    device = DeviceRegistration(
                        id=device_id,
                        account_id=account.id,
                        name="Web browser",
                        public_key_digest=_credential_digest(new_device_token),
                        created_at=now,
                        updated_at=now,
                    )
                    self._insert_device(connection, device)
                session_id, session_token = _opaque_credential()
                session = SessionRecord(
                    id=session_id,
                    account_id=account.id,
                    device_id=device.id,
                    device_version=device.version,
                    token_digest=_credential_digest(session_token),
                    issued_at=now,
                    expires_at=expires_at,
                )
                self._insert_session(connection, session)
                csrf = _csrf_token(session_token, session_secret)
                connection.execute(
                    text(
                        "INSERT INTO web_session_bindings "
                        "(session_id, session_version, csrf_digest, created_at) "
                        "VALUES (:session_id, :session_version, :csrf_digest, :created_at)"
                    ),
                    {
                        "session_id": str(session.id),
                        "session_version": session.version,
                        "csrf_digest": _credential_digest(csrf),
                        "created_at": now.isoformat(),
                    },
                )
                return WebLoginResult(
                    account=account,
                    session=session,
                    csrf_token=csrf,
                    session_token=session_token,
                    device_token=new_device_token,
                )
        except IntegrityError as exc:
            raise AccountRepositoryError("web_login_conflict") from exc

    def authenticate_web_session(
        self,
        *,
        session_token: str,
        session_secret: str,
        now: datetime,
    ) -> WebSessionAuthentication:
        with self._connection() as connection:
            return self._authenticate_web_session(
                connection,
                session_token=session_token,
                session_secret=session_secret,
                now=now,
            )

    def rotate_web_session(
        self,
        *,
        session_token: str,
        session_secret: str,
        now: datetime,
        expires_at: datetime,
    ) -> WebLoginResult:
        try:
            with self._transaction() as connection:
                authenticated = self._authenticate_web_session(
                    connection,
                    session_token=session_token,
                    session_secret=session_secret,
                    now=now,
                )
                _identifier, replacement_token = _opaque_credential(authenticated.session.id)
                replacement_digest = _credential_digest(replacement_token)
                if self._replacement_digest_conflicts(connection, replacement_digest):
                    raise AccountRepositoryError("session_replacement_conflict")
                rotated = SessionRecord(
                    id=authenticated.session.id,
                    account_id=authenticated.account.id,
                    device_id=authenticated.session.device_id,
                    device_version=authenticated.session.device_version,
                    version=authenticated.session.version + 1,
                    token_digest=replacement_digest,
                    predecessor_digest=_credential_digest(session_token),
                    issued_at=now,
                    expires_at=expires_at,
                )
                self._insert_session(connection, rotated)
                csrf = _csrf_token(replacement_token, session_secret)
                connection.execute(
                    text(
                        "INSERT INTO web_session_bindings "
                        "(session_id, session_version, csrf_digest, created_at) "
                        "VALUES (:session_id, :session_version, :csrf_digest, :created_at)"
                    ),
                    {
                        "session_id": str(rotated.id),
                        "session_version": rotated.version,
                        "csrf_digest": _credential_digest(csrf),
                        "created_at": now.isoformat(),
                    },
                )
                return WebLoginResult(
                    account=authenticated.account,
                    session=rotated,
                    csrf_token=csrf,
                    session_token=replacement_token,
                    device_token=None,
                )
        except IntegrityError as exc:
            raise AccountRepositoryError("session_replacement_conflict") from exc

    def revoke_web_session(
        self,
        *,
        session_token: str,
        session_secret: str,
        now: datetime,
    ) -> SessionRecord:
        try:
            with self._transaction() as connection:
                authenticated = self._authenticate_web_session(
                    connection,
                    session_token=session_token,
                    session_secret=session_secret,
                    now=now,
                )
                revoked = SessionRecord(
                    id=authenticated.session.id,
                    account_id=authenticated.account.id,
                    device_id=authenticated.session.device_id,
                    device_version=authenticated.session.device_version,
                    version=authenticated.session.version + 1,
                    token_digest=None,
                    predecessor_digest=_credential_digest(session_token),
                    status=SessionStatus.REVOKED,
                    issued_at=authenticated.session.issued_at,
                    expires_at=authenticated.session.expires_at,
                    revoked_at=now,
                )
                self._insert_session(connection, revoked)
                return revoked
        except IntegrityError as exc:
            raise AccountRepositoryError("session_revocation_conflict") from exc

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()
