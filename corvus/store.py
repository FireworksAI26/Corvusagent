from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as DbSession

from corvus.context import ContextEnvelope, ContextOwner, ContextOwnerKind, ExternalContent
from corvus.database import bootstrap_database
from corvus.models import RunEvent, RunPhase
from corvus.security import (
    SecretRedactor,
    SecurityError,
    atomic_write,
    resolve_under,
    sha256_bytes,
)


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120))
    phase: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemoryRow(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    identity_id: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16))
    pinned: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillRow(Base):
    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    permissions_json: Mapped[str] = mapped_column(Text)
    evaluation_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeliveryRow(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    bundle_json: Mapped[str] = mapped_column(Text)
    approval_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalContentRow(Base):
    __tablename__ = "external_contents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    origin: Mapped[str] = mapped_column(String(32))
    source_locator_digest: Mapped[str] = mapped_column(String(64))
    content_digest: Mapped[str] = mapped_column(String(64))
    trust_class: Mapped[str] = mapped_column(String(16))
    content_json: Mapped[str] = mapped_column(Text)
    provenance_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextEnvelopeRow(Base):
    __tablename__ = "context_envelopes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    system_instruction_digest: Mapped[str] = mapped_column(String(64))
    trusted_content_ids_json: Mapped[str] = mapped_column(Text)
    untrusted_content_ids_json: Mapped[str] = mapped_column(Text)
    firewall_policy_digest: Mapped[str] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TraceStore:
    def __init__(self, db_path: Path, redactor: SecretRedactor | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.redactor = redactor or SecretRedactor()
        self._write_lock = threading.Lock()
        bootstrap_database(db_path, Base.metadata)
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": 30},
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def append(
        self, run_id: UUID, event_type: str, phase: RunPhase, payload: dict[str, Any]
    ) -> RunEvent:
        with self._write_lock:
            return self._append_unlocked(run_id, event_type, phase, payload)

    def _append_unlocked(
        self, run_id: UUID, event_type: str, phase: RunPhase, payload: dict[str, Any]
    ) -> RunEvent:
        with DbSession(self.engine) as session:
            previous = session.scalar(
                select(EventRow)
                .where(EventRow.run_id == str(run_id))
                .order_by(EventRow.sequence.desc())
                .limit(1)
            )
            sequence = 1 if previous is None else previous.sequence + 1
            previous_hash = "0" * 64 if previous is None else previous.event_hash
            clean_payload = self.redactor.redact_value(payload)
            created_at = datetime.now(UTC)
            canonical = json.dumps(
                {
                    "run_id": str(run_id),
                    "sequence": sequence,
                    "event_type": event_type,
                    "phase": phase.value,
                    "payload": clean_payload,
                    "previous_hash": previous_hash,
                    "created_at": created_at.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            event_hash = sha256_bytes(canonical)
            row = EventRow(
                run_id=str(run_id),
                sequence=sequence,
                event_type=event_type,
                phase=phase.value,
                payload_json=json.dumps(clean_payload, sort_keys=True),
                previous_hash=previous_hash,
                event_hash=event_hash,
                created_at=created_at,
            )
            session.add(row)
            session.commit()
            return RunEvent(
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                phase=phase,
                payload=clean_payload,
                previous_hash=previous_hash,
                event_hash=event_hash,
                created_at=created_at,
            )

    @staticmethod
    def _require_new_context_owner(owner: ContextOwner) -> None:
        """Keep new V1 provenance writes under the canonical legacy run owner.

        Query methods intentionally do not call this guard: rows written by older
        versions under the retired owner kinds remain readable for migration and
        forensic review.
        """

        if owner.kind is not ContextOwnerKind.LEGACY_RUN:
            raise SecurityError("new context writes require a legacy_run owner")

    def _external_row(self, owner: ContextOwner, content: ExternalContent) -> ExternalContentRow:
        self._require_new_context_owner(owner)
        content_json = self.redactor.redact_json(content.data)
        content_digest = sha256_bytes(content_json.encode("utf-8"))
        source = self.redactor.redact(content.source)
        source_locator_digest = sha256_bytes(source.encode("utf-8"))
        provenance = {
            "content_digest": content_digest,
            "origin": content.origin.value,
            "source": source,
            "source_locator_digest": source_locator_digest,
            "trust_class": content.trust_class.value,
        }
        return ExternalContentRow(
            id=str(content.id),
            owner_kind=owner.kind.value,
            owner_id=str(owner.id),
            origin=content.origin.value,
            source_locator_digest=source_locator_digest,
            content_digest=content_digest,
            trust_class=content.trust_class.value,
            content_json=content_json,
            provenance_json=json.dumps(provenance, ensure_ascii=False, sort_keys=True),
            created_at=datetime.now(UTC),
        )

    def append_context_envelope(self, envelope: ContextEnvelope) -> UUID:
        self._require_new_context_owner(envelope.owner)
        envelope_id = uuid4()
        with self._write_lock, DbSession(self.engine) as session:
            for content in (*envelope.trusted, *envelope.external):
                if session.get(ExternalContentRow, str(content.id)) is None:
                    session.add(self._external_row(envelope.owner, content))
            session.add(
                ContextEnvelopeRow(
                    id=str(envelope_id),
                    owner_kind=envelope.owner.kind.value,
                    owner_id=str(envelope.owner.id),
                    system_instruction_digest=envelope.system_instruction_digest,
                    trusted_content_ids_json=json.dumps(
                        [str(item.id) for item in envelope.trusted], separators=(",", ":")
                    ),
                    untrusted_content_ids_json=json.dumps(
                        [str(item.id) for item in envelope.external], separators=(",", ":")
                    ),
                    firewall_policy_digest=envelope.firewall_policy_digest,
                    output_digest=None,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        return envelope_id

    def append_external_content(self, owner: ContextOwner, content: ExternalContent) -> None:
        self._require_new_context_owner(owner)
        with self._write_lock, DbSession(self.engine) as session:
            if session.get(ExternalContentRow, str(content.id)) is None:
                session.add(self._external_row(owner, content))
                session.commit()

    def context_envelope_count(self) -> int:
        with DbSession(self.engine) as session:
            return len(session.scalars(select(ContextEnvelopeRow.id)).all())

    def context_envelopes(self, owner: ContextOwner) -> list[dict[str, Any]]:
        with DbSession(self.engine) as session:
            rows = session.scalars(
                select(ContextEnvelopeRow)
                .where(
                    ContextEnvelopeRow.owner_kind == owner.kind.value,
                    ContextEnvelopeRow.owner_id == str(owner.id),
                )
                .order_by(ContextEnvelopeRow.created_at, ContextEnvelopeRow.id)
            ).all()
            return [
                {
                    "id": row.id,
                    "owner_kind": row.owner_kind,
                    "owner_id": row.owner_id,
                    "system_instruction_digest": row.system_instruction_digest,
                    "trusted_content_ids": json.loads(row.trusted_content_ids_json),
                    "untrusted_content_ids": json.loads(row.untrusted_content_ids_json),
                    "firewall_policy_digest": row.firewall_policy_digest,
                    "output_digest": row.output_digest,
                }
                for row in rows
            ]

    def external_contents(self, owner: ContextOwner) -> list[dict[str, Any]]:
        with DbSession(self.engine) as session:
            rows = session.scalars(
                select(ExternalContentRow)
                .where(
                    ExternalContentRow.owner_kind == owner.kind.value,
                    ExternalContentRow.owner_id == str(owner.id),
                )
                .order_by(ExternalContentRow.created_at, ExternalContentRow.id)
            ).all()
            return [
                {
                    "id": row.id,
                    "owner_kind": row.owner_kind,
                    "owner_id": row.owner_id,
                    "origin": row.origin,
                    "source_locator_digest": row.source_locator_digest,
                    "content_digest": row.content_digest,
                    "trust_class": row.trust_class,
                    "content": json.loads(row.content_json),
                    "provenance": json.loads(row.provenance_json),
                }
                for row in rows
            ]

    def events(self, run_id: UUID) -> Iterator[RunEvent]:
        with DbSession(self.engine) as session:
            rows = session.scalars(
                select(EventRow).where(EventRow.run_id == str(run_id)).order_by(EventRow.sequence)
            ).all()
            for row in rows:
                yield RunEvent(
                    run_id=UUID(row.run_id),
                    sequence=row.sequence,
                    event_type=row.event_type,
                    phase=RunPhase(row.phase),
                    payload=json.loads(row.payload_json),
                    previous_hash=row.previous_hash,
                    event_hash=row.event_hash,
                    created_at=row.created_at.replace(tzinfo=UTC)
                    if row.created_at.tzinfo is None
                    else row.created_at,
                )

    def verify(self, run_id: UUID) -> bool:
        previous_hash = "0" * 64
        seen_event = False
        for event in self.events(run_id):
            seen_event = True
            if event.previous_hash != previous_hash:
                return False
            canonical = json.dumps(
                {
                    "run_id": str(event.run_id),
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "phase": event.phase.value,
                    "payload": event.payload,
                    "previous_hash": event.previous_hash,
                    "created_at": event.created_at.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if sha256_bytes(canonical) != event.event_hash:
                return False
            previous_hash = event.event_hash
        return seen_event

    def backup(self, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as connection:
            raw = connection.connection.driver_connection
            import sqlite3

            destination = sqlite3.connect(target)
            try:
                if raw is None:
                    raise RuntimeError("SQLite driver connection is unavailable")
                raw.backup(destination)
            finally:
                destination.close()
        digest = sha256_bytes(target.read_bytes())
        atomic_write(target.with_suffix(target.suffix + ".sha256"), digest.encode())
        return digest

    def integrity_check(self) -> tuple[bool, str]:
        with self.engine.connect() as connection:
            value = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        return value == "ok", str(value)


class ArtifactStore:
    _DIGEST = re.compile(r"^[0-9a-f]{64}$")

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str, *, for_write: bool) -> Path:
        if self._DIGEST.fullmatch(digest) is None:
            raise SecurityError("invalid artifact digest")
        if for_write:
            directory = resolve_under(self.root, digest[:2])
            directory.mkdir(exist_ok=True)
        return resolve_under(
            self.root,
            f"{digest[:2]}/{digest}",
            allow_missing_leaf=for_write,
        )

    def put(self, data: bytes) -> tuple[str, Path]:
        digest = sha256_bytes(data)
        path = self._path(digest, for_write=True)
        if path.exists():
            existing = path.read_bytes()
            if sha256_bytes(existing) != digest:
                raise SecurityError("artifact integrity check failed during put")
        else:
            atomic_write(path, data)
        if sha256_bytes(path.read_bytes()) != digest:
            raise SecurityError("artifact integrity check failed after put")
        return digest, path

    def get(self, digest: str) -> bytes:
        path = self._path(digest, for_write=False)
        data = path.read_bytes()
        if sha256_bytes(data) != digest:
            raise SecurityError("artifact integrity check failed during get")
        return data
