from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from corvus.application.oauth import OAuthError
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision

_FERNET_INFO = b"corvus/oauth-transaction-pkce/v1"


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    id: UUID
    state_digest: str
    nonce_digest: str
    nonce: str
    redirect_uri: str
    pkce_verifier: str
    expires_at: datetime
    consumed_at: datetime | None
    version: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fernet(secret: str) -> Fernet:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_FERNET_INFO,
    ).derive(secret.encode("utf-8"))
    import base64

    return Fernet(base64.urlsafe_b64encode(key))


class OAuthTransactionRepository:
    def __init__(self, database: Path | Engine, *, encryption_secret: str) -> None:
        if len(encryption_secret) < 32:
            raise ValueError("oauth_transaction_secret_too_short")
        self.database_path = database if isinstance(database, Path) else Path()
        self._owns_engine = isinstance(database, Path)
        if isinstance(database, Path):
            revision = current_revision(database)
            if revision != M1_CURRENT_REVISION:
                raise OAuthError("oauth_repository_revision_mismatch")
            self.engine = create_engine(f"sqlite:///{database}")
        else:
            self.engine = database
        self._cipher = _fernet(encryption_secret)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            yield connection

    def create(
        self,
        *,
        state: str,
        nonce: str,
        redirect_uri: str,
        pkce_verifier: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        protected_values = json.dumps(
            {"nonce": nonce, "pkce_verifier": pkce_verifier},
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self._cipher.encrypt(protected_values).decode("ascii")
        try:
            with self._transaction() as connection:
                connection.execute(
                    text(
                        "INSERT INTO oauth_transactions "
                        "(id, state_digest, nonce_digest, redirect_uri, "
                        "encrypted_pkce_verifier, created_at, expires_at, consumed_at, version) "
                        "VALUES (:id, :state_digest, :nonce_digest, :redirect_uri, :verifier, "
                        ":created_at, :expires_at, NULL, 1)"
                    ),
                    {
                        "id": str(uuid4()),
                        "state_digest": _digest(state),
                        "nonce_digest": _digest(nonce),
                        "redirect_uri": redirect_uri,
                        "verifier": encrypted,
                        "created_at": created_at.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    },
                )
        except IntegrityError as exc:
            raise OAuthError("oauth_transaction_conflict") from exc

    def consume(self, *, state: str, now: datetime) -> OAuthTransaction:
        state_digest = _digest(state)
        result: OAuthTransaction | None = None
        terminal_error: str | None = None
        with self._transaction() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, state_digest, nonce_digest, redirect_uri, "
                        "encrypted_pkce_verifier, expires_at, consumed_at, version "
                        "FROM oauth_transactions WHERE state_digest = :state_digest"
                    ),
                    {"state_digest": state_digest},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise OAuthError("oauth_transaction_invalid")
            if row["consumed_at"] is not None:
                raise OAuthError("oauth_transaction_consumed")
            expires_at = datetime.fromisoformat(cast(str, row["expires_at"]))
            if expires_at <= now:
                connection.execute(
                    text(
                        "UPDATE oauth_transactions SET consumed_at = :consumed_at, "
                        "version = version + 1 WHERE id = :id AND consumed_at IS NULL"
                    ),
                    {"consumed_at": now.isoformat(), "id": row["id"]},
                )
                terminal_error = "oauth_transaction_expired"
            else:
                updated = connection.execute(
                    text(
                        "UPDATE oauth_transactions SET consumed_at = :consumed_at, "
                        "version = version + 1 WHERE id = :id AND consumed_at IS NULL "
                        "AND version = :version"
                    ),
                    {
                        "consumed_at": now.isoformat(),
                        "id": row["id"],
                        "version": row["version"],
                    },
                )
                if updated.rowcount != 1:
                    raise OAuthError("oauth_transaction_consumed")
                try:
                    protected = self._cipher.decrypt(
                        cast(str, row["encrypted_pkce_verifier"]).encode()
                    )
                    values = json.loads(protected)
                    nonce = values["nonce"]
                    verifier = values["pkce_verifier"]
                except (InvalidToken, json.JSONDecodeError, KeyError, TypeError):
                    terminal_error = "oauth_transaction_decryption_failed"
                else:
                    if not isinstance(nonce, str) or not isinstance(verifier, str):
                        terminal_error = "oauth_transaction_decryption_failed"
                    elif _digest(nonce) != row["nonce_digest"]:
                        terminal_error = "oauth_transaction_nonce_corrupt"
                    else:
                        result = OAuthTransaction(
                            id=UUID(cast(str, row["id"])),
                            state_digest=cast(str, row["state_digest"]),
                            nonce_digest=cast(str, row["nonce_digest"]),
                            nonce=nonce,
                            redirect_uri=cast(str, row["redirect_uri"]),
                            pkce_verifier=verifier,
                            expires_at=expires_at,
                            consumed_at=now,
                            version=cast(int, row["version"]) + 1,
                        )
        if terminal_error is not None:
            raise OAuthError(terminal_error)
        if result is None:
            raise OAuthError("oauth_transaction_invalid")
        return result

    def is_consumed(self, state: str) -> bool:
        with self.engine.connect() as connection:
            value = connection.scalar(
                text("SELECT consumed_at FROM oauth_transactions WHERE state_digest = :digest"),
                {"digest": _digest(state)},
            )
        return value is not None

    def count(self) -> int:
        with self.engine.connect() as connection:
            return int(connection.scalar(text("SELECT COUNT(*) FROM oauth_transactions")) or 0)

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()
