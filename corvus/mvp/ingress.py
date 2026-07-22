from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from corvus.mvp.core import DomainConflict, DomainNotFound
from corvus.mvp.models import MvpModel
from corvus.mvp.store import SqliteStore

_SENSITIVE_ACTIONS: Final = {"effect.approve", "filesystem.apply", "credential.grant"}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _canonical(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude={"signature"})
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class OfflineIntentEnvelope(MvpModel):
    intent_id: str
    actor_id: str
    audience: str
    scope: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    idempotency_key: str
    payload_digest: str
    payload: dict[str, Any]
    signature: str


class OfflineIntentRecord(MvpModel):
    id: str
    envelope: OfflineIntentEnvelope
    status: str
    application_count: int
    result: dict[str, Any] | None = None


class ChannelEventEnvelope(MvpModel):
    actor_id: str
    provider: str
    external_event_id: str
    external_identity_id: str
    action: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    payload_digest: str
    payload: dict[str, Any]
    signature: str


class ChannelEventRecord(MvpModel):
    id: str
    provider: str
    external_event_id: str
    principal_id: str | None
    status: str
    processing_count: int
    result: dict[str, Any] | None = None


class LocalEnvelopeSigner:
    def __init__(self, *, actor_id: str, private_key: Ed25519PrivateKey) -> None:
        self.actor_id = actor_id
        self._private_key = private_key

    @classmethod
    def generate(cls, *, actor_id: str) -> LocalEnvelopeSigner:
        return cls(actor_id=actor_id, private_key=Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def sign_intent(
        self,
        *,
        audience: str,
        scope: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> OfflineIntentEnvelope:
        issued_at = _now_utc()
        intent_id = str(uuid4())
        nonce = secrets.token_urlsafe(18)
        payload_digest = hashlib.sha256(_canonical(payload)).hexdigest()
        idempotency_key = hashlib.sha256(
            _canonical(
                {
                    "intent_id": intent_id,
                    "actor_id": self.actor_id,
                    "audience": audience,
                    "scope": scope,
                    "payload_digest": payload_digest,
                    "nonce": nonce,
                }
            )
        ).hexdigest()
        unsigned = OfflineIntentEnvelope(
            intent_id=intent_id,
            actor_id=self.actor_id,
            audience=audience,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            payload=payload,
            signature="pending",
        )
        signature = base64.urlsafe_b64encode(self._private_key.sign(_canonical(unsigned))).decode(
            "ascii"
        )
        return unsigned.model_copy(update={"signature": signature})

    def sign_channel_event(
        self,
        *,
        provider: str,
        external_event_id: str,
        external_identity_id: str,
        action: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> ChannelEventEnvelope:
        issued_at = _now_utc()
        unsigned = ChannelEventEnvelope(
            actor_id=self.actor_id,
            provider=provider,
            external_event_id=external_event_id,
            external_identity_id=external_identity_id,
            action=action,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=secrets.token_urlsafe(18),
            payload_digest=hashlib.sha256(_canonical(payload)).hexdigest(),
            payload=payload,
            signature="pending",
        )
        signature = base64.urlsafe_b64encode(self._private_key.sign(_canonical(unsigned))).decode(
            "ascii"
        )
        return unsigned.model_copy(update={"signature": signature})


class _SignedEnvelopeService:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def register_actor(self, actor_id: str, public_key: str) -> None:
        try:
            decoded = base64.urlsafe_b64decode(public_key.encode("ascii"))
            Ed25519PublicKey.from_public_bytes(decoded)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid_actor_public_key") from error
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_envelope_actor_keys(actor_id, public_key, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(actor_id) DO UPDATE SET "
                "public_key = excluded.public_key",
                (actor_id, public_key, _now_utc().isoformat()),
            )

    @staticmethod
    def _verify(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        envelope: MvpModel,
        signature: str,
        expires_at: datetime,
        payload: dict[str, Any],
        payload_digest: str,
    ) -> None:
        if expires_at <= _now_utc():
            raise DomainConflict("envelope_expired")
        actual_payload_digest = hashlib.sha256(_canonical(payload)).hexdigest()
        if not secrets.compare_digest(actual_payload_digest, payload_digest):
            raise DomainConflict("envelope_payload_digest_mismatch")
        row = connection.execute(
            "SELECT public_key FROM mvp_envelope_actor_keys WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        if row is None:
            raise DomainConflict("envelope_actor_unknown")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(row["public_key"].encode("ascii"))
            )
            public_key.verify(
                base64.urlsafe_b64decode(signature.encode("ascii")), _canonical(envelope)
            )
        except (InvalidSignature, ValueError, TypeError) as error:
            raise DomainConflict("envelope_signature_invalid") from error


class OfflineConnectorService(_SignedEnvelopeService):
    def __init__(self, store: SqliteStore, *, signer: LocalEnvelopeSigner) -> None:
        super().__init__(store)
        self.signer = signer
        self.connected = True

    @classmethod
    def open(cls, database: Path, *, signer: LocalEnvelopeSigner) -> OfflineConnectorService:
        return cls(SqliteStore(database), signer=signer)

    def disconnect(self) -> None:
        self.connected = False

    def queue_intent(
        self,
        *,
        actor_id: str,
        audience: str,
        scope: str,
        payload: dict[str, Any],
        expires_at: datetime,
    ) -> OfflineIntentRecord:
        if self.connected:
            raise DomainConflict("connector_must_be_disconnected_to_queue")
        if actor_id != self.signer.actor_id:
            raise DomainConflict("connector_signer_actor_mismatch")
        envelope = self.signer.sign_intent(
            audience=audience,
            scope=scope,
            payload=payload,
            expires_at=expires_at,
        )
        now = _now_utc()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_offline_intents(id, actor_id, idempotency_key, envelope_json, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (
                    envelope.intent_id,
                    actor_id,
                    envelope.idempotency_key,
                    envelope.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return self.get_intent(envelope.intent_id)

    def reconnect_and_reconcile(self) -> tuple[OfflineIntentRecord, ...]:
        self.connected = True
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM mvp_offline_intents WHERE status = 'queued' "
                "ORDER BY created_at"
            ).fetchall()
        return tuple(
            self.reconcile(OfflineIntentEnvelope.model_validate_json(row["envelope_json"]))
            for row in rows
        )

    def reconcile(self, envelope: OfflineIntentEnvelope) -> OfflineIntentRecord:
        now = _now_utc()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM mvp_offline_intents WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO mvp_offline_intents(id, actor_id, idempotency_key, envelope_json, "
                    "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                    (
                        envelope.intent_id,
                        envelope.actor_id,
                        envelope.idempotency_key,
                        envelope.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            elif existing["status"] == "applied":
                return self._intent(existing)
            self._verify(
                connection,
                actor_id=envelope.actor_id,
                envelope=envelope,
                signature=envelope.signature,
                expires_at=envelope.expires_at,
                payload=envelope.payload,
                payload_digest=envelope.payload_digest,
            )
            if envelope.audience != "local-corvus" or not envelope.scope.startswith("project:"):
                raise DomainConflict("offline_intent_scope_or_audience_invalid")
            result = {
                "accepted_command": envelope.payload.get("command"),
                "payload_digest": envelope.payload_digest,
            }
            connection.execute(
                "UPDATE mvp_offline_intents SET status = 'applied', application_count = 1, "
                "result_json = ?, updated_at = ? WHERE idempotency_key = ? "
                "AND application_count = 0",
                (_canonical(result).decode("utf-8"), now.isoformat(), envelope.idempotency_key),
            )
            row = connection.execute(
                "SELECT * FROM mvp_offline_intents WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise DomainNotFound("offline_intent_not_found")
            return self._intent(row)

    def get_intent(self, intent_id: str) -> OfflineIntentRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_offline_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise DomainNotFound("offline_intent_not_found")
            return self._intent(row)

    def list_intents(self) -> tuple[OfflineIntentRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_offline_intents ORDER BY created_at"
            ).fetchall()
        return tuple(self._intent(row) for row in rows)

    @staticmethod
    def _intent(row: sqlite3.Row) -> OfflineIntentRecord:
        return OfflineIntentRecord(
            id=row["id"],
            envelope=OfflineIntentEnvelope.model_validate_json(row["envelope_json"]),
            status=row["status"],
            application_count=int(row["application_count"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )


class ChannelIngressService(_SignedEnvelopeService):
    @classmethod
    def open(cls, database: Path) -> ChannelIngressService:
        return cls(SqliteStore(database))

    def map_identity(self, *, provider: str, external_id: str, principal_id: str) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO mvp_channel_identities(provider, external_id, principal_id, "
                "created_at) VALUES (?, ?, ?, ?) ON CONFLICT(provider, external_id) DO UPDATE SET "
                "principal_id = excluded.principal_id",
                (provider, external_id, principal_id, _now_utc().isoformat()),
            )

    def ingest(self, envelope: ChannelEventEnvelope) -> ChannelEventRecord:
        now = _now_utc()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM mvp_channel_events WHERE provider = ? AND external_event_id = ?",
                (envelope.provider, envelope.external_event_id),
            ).fetchone()
            if existing is not None:
                return self._event(existing)
            self._verify(
                connection,
                actor_id=envelope.actor_id,
                envelope=envelope,
                signature=envelope.signature,
                expires_at=envelope.expires_at,
                payload=envelope.payload,
                payload_digest=envelope.payload_digest,
            )
            identity = connection.execute(
                "SELECT principal_id FROM mvp_channel_identities WHERE provider = ? "
                "AND external_id = ?",
                (envelope.provider, envelope.external_identity_id),
            ).fetchone()
            if identity is None:
                status = "identity_unmapped"
                principal_id = None
            else:
                principal_id = identity["principal_id"]
                status = "step_up_required" if envelope.action in _SENSITIVE_ACTIONS else "accepted"
            result = {
                "trusted_action": envelope.action,
                "untrusted_payload_digest": envelope.payload_digest,
            }
            event_id = str(uuid4())
            connection.execute(
                "INSERT INTO mvp_channel_events(id, provider, external_event_id, actor_id, "
                "principal_id, envelope_json, status, processing_count, result_json, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    event_id,
                    envelope.provider,
                    envelope.external_event_id,
                    envelope.actor_id,
                    principal_id,
                    envelope.model_dump_json(),
                    status,
                    _canonical(result).decode("utf-8"),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM mvp_channel_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise DomainNotFound("channel_event_not_found")
            return self._event(row)

    def get_event(self, event_id: str) -> ChannelEventRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_channel_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise DomainNotFound("channel_event_not_found")
            return self._event(row)

    def list_events(self) -> tuple[ChannelEventRecord, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_channel_events ORDER BY created_at"
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    @staticmethod
    def _event(row: sqlite3.Row) -> ChannelEventRecord:
        return ChannelEventRecord(
            id=row["id"],
            provider=row["provider"],
            external_event_id=row["external_event_id"],
            principal_id=row["principal_id"],
            status=row["status"],
            processing_count=int(row["processing_count"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
        )
