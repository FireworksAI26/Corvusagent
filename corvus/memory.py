from __future__ import annotations

import builtins
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from corvus.models import MemoryRecord
from corvus.store import MemoryRow, TraceStore


class MemoryManager:
    def __init__(self, store: TraceStore) -> None:
        self.store = store

    def add(self, record: MemoryRecord) -> MemoryRecord:
        with DbSession(self.store.engine) as session:
            session.add(
                MemoryRow(
                    id=str(record.id),
                    project_id=str(record.project_id),
                    identity_id=record.identity_id,
                    kind=record.kind,
                    content=self.store.redactor.redact(record.content),
                    source=record.source,
                    confidence=str(record.confidence),
                    pinned=int(record.pinned),
                    expires_at=record.expires_at,
                    created_at=record.created_at,
                )
            )
            session.commit()
        return record

    def list(self, project_id: UUID, identity_id: str) -> builtins.list[MemoryRecord]:
        now = datetime.now(UTC)
        with DbSession(self.store.engine) as session:
            rows = session.scalars(
                select(MemoryRow).where(
                    MemoryRow.project_id == str(project_id), MemoryRow.identity_id == identity_id
                )
            ).all()
        result = []
        for row in rows:
            expiry = row.expires_at
            if expiry and expiry.replace(tzinfo=expiry.tzinfo or UTC) <= now and not row.pinned:
                continue
            result.append(
                MemoryRecord(
                    id=UUID(row.id),
                    project_id=UUID(row.project_id),
                    identity_id=row.identity_id,
                    kind=row.kind,  # type: ignore[arg-type]
                    content=row.content,
                    source=row.source,
                    confidence=float(row.confidence),
                    pinned=bool(row.pinned),
                    expires_at=expiry,
                    created_at=row.created_at,
                )
            )
        return result

    def search(self, project_id: UUID, identity_id: str, query: str) -> builtins.list[MemoryRecord]:
        terms = {term.lower() for term in query.split() if len(term) > 2}
        records = self.list(project_id, identity_id)
        return sorted(
            records,
            key=lambda record: sum(term in record.content.lower() for term in terms),
            reverse=True,
        )

    def delete(self, memory_id: UUID, project_id: UUID, identity_id: str) -> bool:
        with DbSession(self.store.engine) as session:
            result = session.execute(
                delete(MemoryRow).where(
                    MemoryRow.id == str(memory_id),
                    MemoryRow.project_id == str(project_id),
                    MemoryRow.identity_id == identity_id,
                )
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))

    def set_pinned(
        self, memory_id: UUID, project_id: UUID, identity_id: str, *, pinned: bool
    ) -> bool:
        with DbSession(self.store.engine) as session:
            result = session.execute(
                update(MemoryRow)
                .where(
                    MemoryRow.id == str(memory_id),
                    MemoryRow.project_id == str(project_id),
                    MemoryRow.identity_id == identity_id,
                )
                .values(pinned=int(pinned))
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))

    def edit(self, memory_id: UUID, project_id: UUID, identity_id: str, content: str) -> bool:
        with DbSession(self.store.engine) as session:
            result = session.execute(
                update(MemoryRow)
                .where(
                    MemoryRow.id == str(memory_id),
                    MemoryRow.project_id == str(project_id),
                    MemoryRow.identity_id == identity_id,
                )
                .values(content=self.store.redactor.redact(content))
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))
