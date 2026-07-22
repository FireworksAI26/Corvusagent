from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from alembic.runtime.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from corvus.database import DatabaseState, classify_database
from corvus.domain.agent_runtime import (
    AgentRunEvent,
    AgentRunEventChainError,
    AgentRunEventType,
    compute_agent_run_event_digest,
    validate_agent_run_event_chain,
)
from corvus.domain.conversations import (
    GENESIS_EVENT_DIGEST,
    AgentRunRecord,
    AttachmentRef,
    Message,
    MessageAuthorKind,
    RunArtifact,
    RunEventPage,
    RunEventRecord,
    Thread,
    ThreadStatus,
)
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision
from corvus.security import canonical_json_bytes

_TERMINAL_EVENTS = {
    AgentRunEventType.COMPLETED,
    AgentRunEventType.FAILED,
    AgentRunEventType.CANCELLED,
}
_THREAD_ROW_SQL = (
    "SELECT t.id, t.workspace_id, t.workspace_version, t.project_id, "
    "t.creator_principal_id, t.creator_membership_version, t.created_at, "
    "v.title, v.status, v.updated_at, v.version FROM threads AS t "
    "JOIN thread_versions AS v ON v.workspace_id = t.workspace_id "
    "AND v.thread_id = t.id WHERE t.workspace_id = :workspace_id "
    "AND t.id = :thread_id AND v.version = (SELECT MAX(v2.version) "
    "FROM thread_versions AS v2 WHERE v2.workspace_id = t.workspace_id "
    "AND v2.thread_id = t.id)"
)
_THREAD_ROW_LOCKED_SQL = (
    "SELECT t.id, t.workspace_id, t.workspace_version, t.project_id, "
    "t.creator_principal_id, t.creator_membership_version, t.created_at, "
    "v.title, v.status, v.updated_at, v.version FROM threads AS t "
    "JOIN thread_versions AS v ON v.workspace_id = t.workspace_id "
    "AND v.thread_id = t.id WHERE t.workspace_id = :workspace_id "
    "AND t.id = :thread_id AND v.version = (SELECT MAX(v2.version) "
    "FROM thread_versions AS v2 WHERE v2.workspace_id = t.workspace_id "
    "AND v2.thread_id = t.id) FOR UPDATE"
)
_RUN_ROW_SQL = "SELECT * FROM agent_runs WHERE workspace_id = :workspace_id AND id = :id"
_RUN_ROW_LOCKED_SQL = (
    "SELECT * FROM agent_runs WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
)
_ARTIFACT_PARENT_SQL = "SELECT 1 FROM run_artifacts WHERE workspace_id = :workspace_id AND id = :id"
_ARTIFACT_PARENT_LOCKED_SQL = (
    "SELECT 1 FROM run_artifacts WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
)


class ConversationRepositoryError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _digest(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _message_request_digest(message: Message) -> str:
    return _digest(message.model_dump(mode="python", exclude={"sequence"}))


def _run_request_digest(run: AgentRunRecord) -> str:
    return _digest(run)


def _event_replay_body(event: AgentRunEvent) -> dict[str, object]:
    return event.model_dump(
        mode="python",
        exclude={"sequence", "previous_event_digest", "event_digest"},
    )


class ConversationRepository:
    def __init__(self, database: Path | Engine) -> None:
        self._owns_engine = isinstance(database, Path)
        if isinstance(database, Path):
            revision = current_revision(database)
            if revision != M1_CURRENT_REVISION:
                raise ConversationRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
            status = classify_database(database)
            if status.state is not DatabaseState.CURRENT:
                raise ConversationRepositoryError(f"database_state_mismatch:{status.state.value}")
            self.engine = create_engine(
                f"sqlite:///{database}",
                connect_args={"timeout": 30},
            )
        else:
            self.engine = database
            with self.engine.connect() as connection:
                revision = MigrationContext.configure(connection).get_current_revision()
            if revision != M1_CURRENT_REVISION:
                raise ConversationRepositoryError(
                    f"database_revision_mismatch:{revision or 'unstamped'}"
                )
        if self.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise ConversationRepositoryError("unsupported_repository_dialect")

    @staticmethod
    def _enable_sqlite_foreign_keys(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            self._enable_sqlite_foreign_keys(connection)
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield connection
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                return
            with connection.begin():
                yield connection

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            self._enable_sqlite_foreign_keys(connection)
            yield connection

    @staticmethod
    def _current_membership(
        connection: Connection,
        workspace_id: UUID,
        principal_id: UUID,
        membership_version: int | None = None,
    ) -> int | None:
        row = connection.execute(
            text(
                "SELECT version, status FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND principal_id = :principal_id "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"workspace_id": str(workspace_id), "principal_id": str(principal_id)},
        ).first()
        if row is None or str(row.status) != "active":
            return None
        current = int(row.version)
        if membership_version is not None and current != membership_version:
            raise ConversationRepositoryError("conversation_membership_stale")
        return current

    @classmethod
    def _require_membership(
        cls,
        connection: Connection,
        workspace_id: UUID,
        principal_id: UUID,
        membership_version: int,
    ) -> None:
        if cls._current_membership(connection, workspace_id, principal_id) is None:
            raise ConversationRepositoryError("conversation_membership_inactive")
        current = cls._current_membership(connection, workspace_id, principal_id)
        if current != membership_version:
            raise ConversationRepositoryError("conversation_membership_stale")

    @staticmethod
    def _thread_from_row(row: RowMapping) -> Thread:
        return Thread(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            workspace_version=int(row["workspace_version"]),
            project_id=None if row["project_id"] is None else UUID(row["project_id"]),
            creator_principal_id=UUID(row["creator_principal_id"]),
            creator_membership_version=int(row["creator_membership_version"]),
            title=row["title"],
            status=ThreadStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
        )

    @staticmethod
    def _thread_row(
        connection: Connection,
        workspace_id: UUID,
        thread_id: UUID,
        *,
        lock: bool = False,
    ) -> RowMapping | None:
        statement = (
            _THREAD_ROW_LOCKED_SQL
            if lock and connection.dialect.name == "postgresql"
            else _THREAD_ROW_SQL
        )
        row = (
            connection.execute(
                text(statement),
                {"workspace_id": str(workspace_id), "thread_id": str(thread_id)},
            )
            .mappings()
            .first()
        )
        return row

    def create_thread(self, thread: Thread) -> Thread:
        with self._transaction() as connection:
            self._require_membership(
                connection,
                thread.workspace_id,
                thread.creator_principal_id,
                thread.creator_membership_version,
            )
            existing_row = self._thread_row(connection, thread.workspace_id, thread.id, lock=True)
            if existing_row is not None:
                existing = self._thread_from_row(existing_row)
                if existing == thread:
                    return existing
                raise ConversationRepositoryError("thread_identity_conflict")
            try:
                connection.execute(
                    text(
                        "INSERT INTO threads (workspace_id, id, workspace_version, project_id, "
                        "creator_principal_id, creator_membership_version, created_at) VALUES "
                        "(:workspace_id, :id, :workspace_version, :project_id, "
                        ":creator_principal_id, :creator_membership_version, :created_at)"
                    ),
                    {
                        "workspace_id": str(thread.workspace_id),
                        "id": str(thread.id),
                        "workspace_version": thread.workspace_version,
                        "project_id": None if thread.project_id is None else str(thread.project_id),
                        "creator_principal_id": str(thread.creator_principal_id),
                        "creator_membership_version": thread.creator_membership_version,
                        "created_at": thread.created_at.isoformat(),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO thread_versions (workspace_id, thread_id, version, title, "
                        "status, updated_at) VALUES (:workspace_id, :thread_id, :version, :title, "
                        ":status, :updated_at)"
                    ),
                    {
                        "workspace_id": str(thread.workspace_id),
                        "thread_id": str(thread.id),
                        "version": thread.version,
                        "title": thread.title,
                        "status": thread.status.value,
                        "updated_at": thread.updated_at.isoformat(),
                    },
                )
            except IntegrityError as exc:
                raise ConversationRepositoryError("thread_binding_invalid") from exc
        return thread

    def get_thread(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        requester_principal_id: UUID,
    ) -> Thread | None:
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                return None
            row = self._thread_row(connection, workspace_id, thread_id)
            return None if row is None else self._thread_from_row(row)

    def list_threads(
        self,
        workspace_id: UUID,
        requester_principal_id: UUID,
    ) -> tuple[Thread, ...]:
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                return ()
            ids = connection.scalars(
                text(
                    "SELECT id FROM threads WHERE workspace_id = :workspace_id "
                    "ORDER BY created_at, id"
                ),
                {"workspace_id": str(workspace_id)},
            ).all()
            rows = [self._thread_row(connection, workspace_id, UUID(value)) for value in ids]
            return tuple(self._thread_from_row(row) for row in rows if row is not None)

    def archive_thread(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        expected_version: int,
        requester_principal_id: UUID,
        requester_membership_version: int,
        updated_at: datetime,
    ) -> Thread:
        with self._transaction() as connection:
            self._require_membership(
                connection, workspace_id, requester_principal_id, requester_membership_version
            )
            row = self._thread_row(connection, workspace_id, thread_id, lock=True)
            if row is None:
                raise ConversationRepositoryError("thread_not_found")
            current = self._thread_from_row(row)
            if current.version != expected_version:
                raise ConversationRepositoryError("thread_version_conflict")
            archived = current.model_copy(
                update={
                    "status": ThreadStatus.ARCHIVED,
                    "version": current.version + 1,
                    "updated_at": updated_at,
                }
            )
            connection.execute(
                text(
                    "INSERT INTO thread_versions (workspace_id, thread_id, version, title, status, "
                    "updated_at) VALUES (:workspace_id, :thread_id, :version, :title, :status, "
                    ":updated_at)"
                ),
                {
                    "workspace_id": str(workspace_id),
                    "thread_id": str(thread_id),
                    "version": archived.version,
                    "title": archived.title,
                    "status": archived.status.value,
                    "updated_at": archived.updated_at.isoformat(),
                },
            )
            return archived

    @staticmethod
    def _attachment_from_row(row: RowMapping) -> AttachmentRef:
        return AttachmentRef(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            owner_principal_id=UUID(row["owner_principal_id"]),
            owner_membership_version=int(row["owner_membership_version"]),
            display_name=row["display_name"],
            media_type=row["media_type"],
            byte_size=int(row["byte_size"]),
            content_digest=row["content_digest"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )

    def register_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        with self._transaction() as connection:
            self._require_membership(
                connection,
                attachment.workspace_id,
                attachment.owner_principal_id,
                attachment.owner_membership_version,
            )
            existing = (
                connection.execute(
                    text(
                        "SELECT * FROM attachments WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    {"workspace_id": str(attachment.workspace_id), "id": str(attachment.id)},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                value = self._attachment_from_row(existing)
                if value == attachment:
                    return value
                raise ConversationRepositoryError("attachment_identity_conflict")
            try:
                connection.execute(
                    text(
                        "INSERT INTO attachments (workspace_id, id, owner_principal_id, "
                        "owner_membership_version, display_name, media_type, byte_size, "
                        "content_digest, metadata_json, created_at) VALUES (:workspace_id, :id, "
                        ":owner_principal_id, :owner_membership_version, :display_name, "
                        ":media_type, :byte_size, :content_digest, :metadata_json, :created_at)"
                    ),
                    {
                        "workspace_id": str(attachment.workspace_id),
                        "id": str(attachment.id),
                        "owner_principal_id": str(attachment.owner_principal_id),
                        "owner_membership_version": attachment.owner_membership_version,
                        "display_name": attachment.display_name,
                        "media_type": attachment.media_type,
                        "byte_size": attachment.byte_size,
                        "content_digest": attachment.content_digest,
                        "metadata_json": canonical_json_bytes(attachment.metadata).decode(),
                        "created_at": attachment.created_at.isoformat(),
                    },
                )
            except IntegrityError as exc:
                raise ConversationRepositoryError("attachment_binding_invalid") from exc
        return attachment

    @staticmethod
    def _message_from_row(connection: Connection, row: RowMapping) -> Message:
        attachment_ids = tuple(
            UUID(value)
            for value in connection.scalars(
                text(
                    "SELECT attachment_id FROM message_attachments WHERE workspace_id = :workspace_id "
                    "AND thread_id = :thread_id AND message_sequence = :sequence ORDER BY ordinal"
                ),
                {
                    "workspace_id": row["workspace_id"],
                    "thread_id": row["thread_id"],
                    "sequence": row["sequence"],
                },
            ).all()
        )
        return Message(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            thread_id=UUID(row["thread_id"]),
            sequence=int(row["sequence"]),
            content=row["content"],
            content_digest=row["content_digest"],
            idempotency_key=row["idempotency_key"],
            producing_run_id=(
                None if row["producing_run_id"] is None else UUID(row["producing_run_id"])
            ),
            attachment_ids=attachment_ids,
            author_kind=MessageAuthorKind(row["author_kind"]),
            author_principal_id=(
                None if row["author_principal_id"] is None else UUID(row["author_principal_id"])
            ),
            author_membership_version=row["author_membership_version"],
            author_agent_id=(
                None if row["author_agent_id"] is None else UUID(row["author_agent_id"])
            ),
            author_agent_version=row["author_agent_version"],
            created_at=row["created_at"],
        )

    def append_message(
        self,
        message: Message,
        *,
        requester_principal_id: UUID,
        requester_membership_version: int,
    ) -> Message:
        request_digest = _message_request_digest(message)
        with self._transaction() as connection:
            self._require_membership(
                connection,
                message.workspace_id,
                requester_principal_id,
                requester_membership_version,
            )
            if (
                self._thread_row(connection, message.workspace_id, message.thread_id, lock=True)
                is None
            ):
                raise ConversationRepositoryError("thread_not_found")
            existing = (
                connection.execute(
                    text(
                        "SELECT * FROM messages WHERE workspace_id = :workspace_id "
                        "AND thread_id = :thread_id AND idempotency_key = :idempotency_key"
                    ),
                    {
                        "workspace_id": str(message.workspace_id),
                        "thread_id": str(message.thread_id),
                        "idempotency_key": message.idempotency_key,
                    },
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConversationRepositoryError("conversation_idempotency_payload_mismatch")
                return self._message_from_row(connection, existing)
            for attachment_id in message.attachment_ids:
                found = connection.execute(
                    text(
                        "SELECT 1 FROM attachments WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    {"workspace_id": str(message.workspace_id), "id": str(attachment_id)},
                ).first()
                if found is None:
                    raise ConversationRepositoryError("attachment_not_found")
            sequence = int(
                connection.scalar(
                    text(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages "
                        "WHERE workspace_id = :workspace_id AND thread_id = :thread_id"
                    ),
                    {
                        "workspace_id": str(message.workspace_id),
                        "thread_id": str(message.thread_id),
                    },
                )
            )
            stored = Message(**{**message.model_dump(), "sequence": sequence})
            try:
                connection.execute(
                    text(
                        "INSERT INTO messages (workspace_id, thread_id, sequence, id, content, "
                        "content_digest, idempotency_key, request_digest, producing_run_id, "
                        "author_kind, author_principal_id, author_membership_version, "
                        "author_agent_id, author_agent_version, created_at) VALUES "
                        "(:workspace_id, :thread_id, :sequence, :id, :content, :content_digest, "
                        ":idempotency_key, :request_digest, :producing_run_id, :author_kind, "
                        ":author_principal_id, :author_membership_version, :author_agent_id, "
                        ":author_agent_version, :created_at)"
                    ),
                    {
                        "workspace_id": str(stored.workspace_id),
                        "thread_id": str(stored.thread_id),
                        "sequence": stored.sequence,
                        "id": str(stored.id),
                        "content": stored.content,
                        "content_digest": stored.content_digest,
                        "idempotency_key": stored.idempotency_key,
                        "request_digest": request_digest,
                        "producing_run_id": (
                            None
                            if stored.producing_run_id is None
                            else str(stored.producing_run_id)
                        ),
                        "author_kind": stored.author_kind.value,
                        "author_principal_id": (
                            None
                            if stored.author_principal_id is None
                            else str(stored.author_principal_id)
                        ),
                        "author_membership_version": stored.author_membership_version,
                        "author_agent_id": (
                            None if stored.author_agent_id is None else str(stored.author_agent_id)
                        ),
                        "author_agent_version": stored.author_agent_version,
                        "created_at": stored.created_at.isoformat(),
                    },
                )
                for ordinal, attachment_id in enumerate(stored.attachment_ids, start=1):
                    connection.execute(
                        text(
                            "INSERT INTO message_attachments (workspace_id, thread_id, "
                            "message_sequence, attachment_id, ordinal) VALUES (:workspace_id, "
                            ":thread_id, :message_sequence, :attachment_id, :ordinal)"
                        ),
                        {
                            "workspace_id": str(stored.workspace_id),
                            "thread_id": str(stored.thread_id),
                            "message_sequence": stored.sequence,
                            "attachment_id": str(attachment_id),
                            "ordinal": ordinal,
                        },
                    )
            except IntegrityError as exc:
                raise ConversationRepositoryError("message_binding_invalid") from exc
            return stored

    def list_messages(
        self,
        workspace_id: UUID,
        thread_id: UUID,
        requester_principal_id: UUID,
    ) -> tuple[Message, ...]:
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                return ()
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM messages WHERE workspace_id = :workspace_id "
                        "AND thread_id = :thread_id ORDER BY sequence"
                    ),
                    {"workspace_id": str(workspace_id), "thread_id": str(thread_id)},
                )
                .mappings()
                .all()
            )
            return tuple(self._message_from_row(connection, row) for row in rows)

    @staticmethod
    def _run_from_row(row: RowMapping) -> AgentRunRecord:
        return AgentRunRecord(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            thread_id=UUID(row["thread_id"]),
            message_sequence=int(row["message_sequence"]),
            requester_principal_id=UUID(row["requester_principal_id"]),
            requester_membership_version=int(row["requester_membership_version"]),
            authorization_snapshot_id=UUID(row["authorization_snapshot_id"]),
            authorization_snapshot_digest=row["authorization_snapshot_digest"],
            provider_binding_id=UUID(row["provider_binding_id"]),
            provider_binding_version=int(row["provider_binding_version"]),
            provider_binding_digest=row["provider_binding_digest"],
            canonical_request_digest=row["canonical_request_digest"],
            idempotency_key=row["idempotency_key"],
            parent_run_id=None if row["parent_run_id"] is None else UUID(row["parent_run_id"]),
            root_run_id=None if row["root_run_id"] is None else UUID(row["root_run_id"]),
            created_at=row["created_at"],
        )

    def create_run(self, run: AgentRunRecord) -> AgentRunRecord:
        request_digest = _run_request_digest(run)
        with self._transaction() as connection:
            self._require_membership(
                connection,
                run.workspace_id,
                run.requester_principal_id,
                run.requester_membership_version,
            )
            self._thread_row(connection, run.workspace_id, run.thread_id, lock=True)
            existing = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_runs WHERE workspace_id = :workspace_id "
                        "AND thread_id = :thread_id AND idempotency_key = :idempotency_key"
                    ),
                    {
                        "workspace_id": str(run.workspace_id),
                        "thread_id": str(run.thread_id),
                        "idempotency_key": run.idempotency_key,
                    },
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConversationRepositoryError("conversation_idempotency_payload_mismatch")
                return self._run_from_row(existing)
            values = run.model_dump(mode="python")
            values = {
                key: str(value) if isinstance(value, UUID) else value
                for key, value in values.items()
            }
            values["created_at"] = run.created_at.isoformat()
            values["request_digest"] = request_digest
            try:
                connection.execute(
                    text(
                        "INSERT INTO agent_runs (workspace_id, id, thread_id, message_sequence, "
                        "requester_principal_id, requester_membership_version, "
                        "authorization_snapshot_id, authorization_snapshot_digest, "
                        "provider_binding_id, provider_binding_version, provider_binding_digest, "
                        "canonical_request_digest, idempotency_key, request_digest, parent_run_id, "
                        "root_run_id, created_at) VALUES (:workspace_id, :id, :thread_id, "
                        ":message_sequence, :requester_principal_id, :requester_membership_version, "
                        ":authorization_snapshot_id, :authorization_snapshot_digest, "
                        ":provider_binding_id, :provider_binding_version, :provider_binding_digest, "
                        ":canonical_request_digest, :idempotency_key, :request_digest, "
                        ":parent_run_id, :root_run_id, :created_at)"
                    ),
                    values,
                )
            except IntegrityError as exc:
                raise ConversationRepositoryError("run_binding_invalid") from exc
            return run

    def get_run(
        self,
        workspace_id: UUID,
        run_id: UUID,
        requester_principal_id: UUID,
    ) -> AgentRunRecord | None:
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                return None
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_runs WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    {"workspace_id": str(workspace_id), "id": str(run_id)},
                )
                .mappings()
                .first()
            )
            return None if row is None else self._run_from_row(row)

    @staticmethod
    def _event_from_row(row: RowMapping) -> RunEventRecord:
        try:
            return ConversationRepository._validated_event_from_row(row)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise ConversationRepositoryError("run_event_integrity_invalid") from exc

    @staticmethod
    def _validated_event_from_row(row: RowMapping) -> RunEventRecord:
        event = AgentRunEvent(
            run_id=UUID(row["run_id"]),
            handle_id=UUID(row["handle_id"]),
            sequence=int(row["sequence"]),
            timestamp=row["timestamp"],
            event_type=AgentRunEventType(row["event_type"]),
            redacted_payload=json.loads(row["payload_json"]),
            provider_event_id=row["provider_event_id"],
            tool_call_id=row["tool_call_id"],
            effect_authorization_decision_id=(
                None
                if row["effect_authorization_decision_id"] is None
                else UUID(row["effect_authorization_decision_id"])
            ),
            effect_authorization_decision_digest=row["effect_authorization_decision_digest"],
            previous_event_digest=row["previous_event_digest"],
            event_digest=row["event_digest"],
        )
        return RunEventRecord(
            workspace_id=UUID(row["workspace_id"]),
            thread_id=UUID(row["thread_id"]),
            run_id=UUID(row["run_id"]),
            event=event,
        )

    @staticmethod
    def _lock_run(connection: Connection, workspace_id: UUID, run_id: UUID) -> RowMapping | None:
        statement = _RUN_ROW_LOCKED_SQL if connection.dialect.name == "postgresql" else _RUN_ROW_SQL
        return (
            connection.execute(
                text(statement),
                {"workspace_id": str(workspace_id), "id": str(run_id)},
            )
            .mappings()
            .first()
        )

    def append_event(
        self,
        record: RunEventRecord,
        *,
        requester_principal_id: UUID,
        requester_membership_version: int,
    ) -> RunEventRecord:
        with self._transaction() as connection:
            self._require_membership(
                connection,
                record.workspace_id,
                requester_principal_id,
                requester_membership_version,
            )
            run_row = self._lock_run(connection, record.workspace_id, record.run_id)
            if run_row is None or run_row["thread_id"] != str(record.thread_id):
                raise ConversationRepositoryError("run_not_found")
            if record.event.provider_event_id is not None:
                replay_row = (
                    connection.execute(
                        text(
                            "SELECT * FROM agent_run_events WHERE workspace_id = :workspace_id "
                            "AND run_id = :run_id AND provider_event_id = :provider_event_id"
                        ),
                        {
                            "workspace_id": str(record.workspace_id),
                            "run_id": str(record.run_id),
                            "provider_event_id": record.event.provider_event_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                if replay_row is not None:
                    replay = self._event_from_row(replay_row)
                    if _event_replay_body(replay.event) != _event_replay_body(record.event):
                        raise ConversationRepositoryError(
                            "conversation_idempotency_payload_mismatch"
                        )
                    return replay
            last = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_run_events WHERE workspace_id = :workspace_id "
                        "AND run_id = :run_id ORDER BY sequence DESC LIMIT 1"
                    ),
                    {"workspace_id": str(record.workspace_id), "run_id": str(record.run_id)},
                )
                .mappings()
                .first()
            )
            if last is not None and AgentRunEventType(last["event_type"]) in _TERMINAL_EVENTS:
                raise ConversationRepositoryError("run_event_after_terminal")
            sequence = 1 if last is None else int(last["sequence"]) + 1
            previous_digest = GENESIS_EVENT_DIGEST if last is None else last["event_digest"]
            if sequence == 1 and record.event.event_type is not AgentRunEventType.STARTED:
                raise ConversationRepositoryError("run_event_stream_must_start")
            digest = compute_agent_run_event_digest(
                run_id=record.run_id,
                handle_id=record.event.handle_id,
                sequence=sequence,
                timestamp=record.event.timestamp,
                event_type=record.event.event_type,
                redacted_payload=record.event.redacted_payload,
                provider_event_id=record.event.provider_event_id,
                previous_event_digest=previous_digest,
                tool_call_id=record.event.tool_call_id,
                effect_authorization_decision_id=record.event.effect_authorization_decision_id,
                effect_authorization_decision_digest=record.event.effect_authorization_decision_digest,
            )
            event = AgentRunEvent(
                **{
                    **record.event.model_dump(),
                    "sequence": sequence,
                    "previous_event_digest": previous_digest,
                    "event_digest": digest,
                }
            )
            stored = RunEventRecord(
                workspace_id=record.workspace_id,
                thread_id=record.thread_id,
                run_id=record.run_id,
                event=event,
            )
            prior_rows = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_run_events WHERE workspace_id = :workspace_id "
                        "AND run_id = :run_id ORDER BY sequence"
                    ),
                    {"workspace_id": str(record.workspace_id), "run_id": str(record.run_id)},
                )
                .mappings()
                .all()
            )
            try:
                validate_agent_run_event_chain(
                    tuple(self._event_from_row(row).event for row in prior_rows) + (event,)
                )
            except AgentRunEventChainError as exc:
                raise ConversationRepositoryError(exc.reason_code) from exc
            try:
                connection.execute(
                    text(
                        "INSERT INTO agent_run_events (workspace_id, thread_id, run_id, sequence, "
                        "handle_id, timestamp, event_type, payload_json, provider_event_id, "
                        "tool_call_id, effect_authorization_decision_id, "
                        "effect_authorization_decision_digest, previous_event_digest, event_digest) "
                        "VALUES (:workspace_id, :thread_id, :run_id, :sequence, :handle_id, "
                        ":timestamp, :event_type, :payload_json, :provider_event_id, :tool_call_id, "
                        ":effect_authorization_decision_id, :effect_authorization_decision_digest, "
                        ":previous_event_digest, :event_digest)"
                    ),
                    {
                        "workspace_id": str(stored.workspace_id),
                        "thread_id": str(stored.thread_id),
                        "run_id": str(stored.run_id),
                        "sequence": event.sequence,
                        "handle_id": str(event.handle_id),
                        "timestamp": event.timestamp.isoformat(),
                        "event_type": event.event_type.value,
                        "payload_json": canonical_json_bytes(event.redacted_payload).decode(),
                        "provider_event_id": event.provider_event_id,
                        "tool_call_id": event.tool_call_id,
                        "effect_authorization_decision_id": (
                            None
                            if event.effect_authorization_decision_id is None
                            else str(event.effect_authorization_decision_id)
                        ),
                        "effect_authorization_decision_digest": event.effect_authorization_decision_digest,
                        "previous_event_digest": event.previous_event_digest,
                        "event_digest": event.event_digest,
                    },
                )
            except IntegrityError as exc:
                raise ConversationRepositoryError("run_event_binding_invalid") from exc
            return stored

    def page_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        requester_principal_id: UUID,
        *,
        after_sequence: int,
        limit: int,
    ) -> RunEventPage:
        if after_sequence < 0 or not 1 <= limit <= 1_000:
            raise ConversationRepositoryError("conversation_cursor_invalid")
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                raise ConversationRepositoryError("run_not_found")
            run_row = self._lock_run(connection, workspace_id, run_id)
            if run_row is None:
                raise ConversationRepositoryError("run_not_found")
            bounds = connection.execute(
                text(
                    "SELECT COALESCE(MIN(sequence), 1) AS earliest, COALESCE(MAX(sequence), 0) "
                    "AS high FROM agent_run_events WHERE workspace_id = :workspace_id "
                    "AND run_id = :run_id"
                ),
                {"workspace_id": str(workspace_id), "run_id": str(run_id)},
            ).first()
            if bounds is None:  # pragma: no cover - aggregate SELECT always returns one row
                raise ConversationRepositoryError("run_event_integrity_invalid")
            earliest = int(bounds.earliest)
            high = int(bounds.high)
            if after_sequence > high:
                raise ConversationRepositoryError("conversation_cursor_invalid")
            if after_sequence < earliest - 1:
                raise ConversationRepositoryError("conversation_cursor_resync_required")
            chain_rows = (
                connection.execute(
                    text(
                        "SELECT * FROM agent_run_events WHERE workspace_id = :workspace_id "
                        "AND run_id = :run_id AND sequence <= :high ORDER BY sequence"
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "run_id": str(run_id),
                        "high": high,
                    },
                )
                .mappings()
                .all()
            )
            chain = tuple(self._event_from_row(row) for row in chain_rows)
            if chain:
                expected_thread_id = UUID(run_row["thread_id"])
                if (
                    len(chain) != high
                    or earliest != 1
                    or any(
                        record.workspace_id != workspace_id
                        or record.thread_id != expected_thread_id
                        or record.run_id != run_id
                        for record in chain
                    )
                ):
                    raise ConversationRepositoryError("run_event_integrity_invalid")
                try:
                    validate_agent_run_event_chain(tuple(record.event for record in chain))
                except AgentRunEventChainError as exc:
                    raise ConversationRepositoryError("run_event_integrity_invalid") from exc
            elif high != 0:
                raise ConversationRepositoryError("run_event_integrity_invalid")
            events = tuple(record for record in chain if record.event.sequence > after_sequence)[
                :limit
            ]
            next_after = after_sequence if not events else events[-1].event.sequence
            return RunEventPage(
                workspace_id=workspace_id,
                run_id=run_id,
                requested_after=after_sequence,
                next_after=next_after,
                high_watermark=high,
                earliest_sequence=earliest,
                events=events,
                has_more=next_after < high,
            )

    @staticmethod
    def _artifact_from_row(connection: Connection, row: RowMapping) -> RunArtifact:
        parents = tuple(
            UUID(value)
            for value in connection.scalars(
                text(
                    "SELECT parent_artifact_id FROM run_artifact_lineage WHERE "
                    "workspace_id = :workspace_id AND artifact_id = :artifact_id ORDER BY ordinal"
                ),
                {"workspace_id": row["workspace_id"], "artifact_id": row["id"]},
            ).all()
        )
        return RunArtifact(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            run_id=UUID(row["run_id"]),
            producing_event_sequence=int(row["producing_event_sequence"]),
            display_name=row["display_name"],
            media_type=row["media_type"],
            byte_size=int(row["byte_size"]),
            content_digest=row["content_digest"],
            parent_artifact_ids=parents,
            lineage_digest=row["lineage_digest"],
            created_at=row["created_at"],
        )

    def record_artifact(
        self,
        artifact: RunArtifact,
        *,
        requester_principal_id: UUID,
        requester_membership_version: int,
    ) -> RunArtifact:
        with self._transaction() as connection:
            self._require_membership(
                connection,
                artifact.workspace_id,
                requester_principal_id,
                requester_membership_version,
            )
            if self._lock_run(connection, artifact.workspace_id, artifact.run_id) is None:
                raise ConversationRepositoryError("run_not_found")
            existing = (
                connection.execute(
                    text(
                        "SELECT * FROM run_artifacts WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    {"workspace_id": str(artifact.workspace_id), "id": str(artifact.id)},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                value = self._artifact_from_row(connection, existing)
                if value == artifact:
                    return value
                raise ConversationRepositoryError("artifact_identity_conflict")
            event_exists = connection.execute(
                text(
                    "SELECT 1 FROM agent_run_events WHERE workspace_id = :workspace_id "
                    "AND run_id = :run_id AND sequence = :sequence"
                ),
                {
                    "workspace_id": str(artifact.workspace_id),
                    "run_id": str(artifact.run_id),
                    "sequence": artifact.producing_event_sequence,
                },
            ).first()
            if event_exists is None:
                raise ConversationRepositoryError("artifact_producing_event_not_found")
            parent_ids = sorted(artifact.parent_artifact_ids, key=str)
            for parent_id in parent_ids:
                statement = (
                    _ARTIFACT_PARENT_LOCKED_SQL
                    if connection.dialect.name == "postgresql"
                    else _ARTIFACT_PARENT_SQL
                )
                parent = connection.execute(
                    text(statement),
                    {"workspace_id": str(artifact.workspace_id), "id": str(parent_id)},
                ).first()
                if parent is None:
                    raise ConversationRepositoryError("artifact_parent_not_found")
            try:
                connection.execute(
                    text(
                        "INSERT INTO run_artifacts (workspace_id, id, run_id, "
                        "producing_event_sequence, display_name, media_type, byte_size, "
                        "content_digest, lineage_digest, created_at) VALUES (:workspace_id, :id, "
                        ":run_id, :producing_event_sequence, :display_name, :media_type, "
                        ":byte_size, :content_digest, :lineage_digest, :created_at)"
                    ),
                    {
                        "workspace_id": str(artifact.workspace_id),
                        "id": str(artifact.id),
                        "run_id": str(artifact.run_id),
                        "producing_event_sequence": artifact.producing_event_sequence,
                        "display_name": artifact.display_name,
                        "media_type": artifact.media_type,
                        "byte_size": artifact.byte_size,
                        "content_digest": artifact.content_digest,
                        "lineage_digest": artifact.lineage_digest,
                        "created_at": artifact.created_at.isoformat(),
                    },
                )
                for ordinal, parent_id in enumerate(artifact.parent_artifact_ids, start=1):
                    connection.execute(
                        text(
                            "INSERT INTO run_artifact_lineage (workspace_id, artifact_id, "
                            "parent_artifact_id, ordinal, lineage_digest) VALUES (:workspace_id, "
                            ":artifact_id, :parent_artifact_id, :ordinal, :lineage_digest)"
                        ),
                        {
                            "workspace_id": str(artifact.workspace_id),
                            "artifact_id": str(artifact.id),
                            "parent_artifact_id": str(parent_id),
                            "ordinal": ordinal,
                            "lineage_digest": artifact.lineage_digest,
                        },
                    )
            except IntegrityError as exc:
                raise ConversationRepositoryError("artifact_binding_invalid") from exc
            return artifact

    def list_artifacts(
        self,
        workspace_id: UUID,
        run_id: UUID,
        requester_principal_id: UUID,
    ) -> tuple[RunArtifact, ...]:
        with self._connection() as connection:
            if self._current_membership(connection, workspace_id, requester_principal_id) is None:
                return ()
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM run_artifacts WHERE workspace_id = :workspace_id "
                        "AND run_id = :run_id ORDER BY created_at, id"
                    ),
                    {"workspace_id": str(workspace_id), "run_id": str(run_id)},
                )
                .mappings()
                .all()
            )
            return tuple(self._artifact_from_row(connection, row) for row in rows)

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()
