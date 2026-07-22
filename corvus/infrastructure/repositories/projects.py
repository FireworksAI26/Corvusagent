from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import Integer, String, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from corvus.database import DatabaseState, classify_database
from corvus.domain.identity import Project, RecordStatus
from corvus.infrastructure.db import M1_CURRENT_REVISION, current_revision


class _RepositoryBase(DeclarativeBase):
    pass


class ProjectRow(_RepositoryBase):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))
    root_locator: Mapped[str] = mapped_column(String(2048))
    privacy: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)


class ProjectRepositoryError(RuntimeError):
    pass


class ProjectRepository:
    def __init__(self, database: Path) -> None:
        revision = current_revision(database)
        if revision != M1_CURRENT_REVISION:
            raise ProjectRepositoryError(f"database_revision_mismatch:{revision or 'unstamped'}")
        status = classify_database(database)
        if status.state is not DatabaseState.CURRENT:
            raise ProjectRepositoryError(f"database_state_mismatch:{status.state.value}")
        self.engine = create_engine(f"sqlite:///{database}")

    @staticmethod
    def _to_project(row: ProjectRow) -> Project:
        return Project(
            id=UUID(row.id),
            workspace_id=UUID(row.workspace_id),
            name=row.name,
            root_locator=row.root_locator,
            privacy=row.privacy,
            status=RecordStatus(row.status),
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
            version=row.version,
        )

    def add(self, project: Project) -> None:
        row = ProjectRow(
            id=str(project.id),
            workspace_id=str(project.workspace_id),
            name=project.name,
            root_locator=project.root_locator,
            privacy=project.privacy,
            status=project.status.value,
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
            version=project.version,
        )
        try:
            with Session(self.engine) as session:
                session.add(row)
                session.commit()
        except IntegrityError as exc:
            raise ProjectRepositoryError("project_identity_conflict") from exc

    def add_idempotent(self, project: Project) -> None:
        existing = self.get_staged(workspace_id=project.workspace_id, project_id=project.id)
        if existing is not None:
            if existing != project:
                raise ProjectRepositoryError("project_replay_mismatch")
            return
        try:
            self.add(project)
        except ProjectRepositoryError:
            existing = self.get_staged(workspace_id=project.workspace_id, project_id=project.id)
            if existing != project:
                raise

    def get_staged(self, *, workspace_id: UUID, project_id: UUID) -> Project | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == str(project_id),
                    ProjectRow.workspace_id == str(workspace_id),
                )
            )
            return None if row is None else self._to_project(row)

    @staticmethod
    def _mutation_digest(project: Project) -> str:
        encoded = json.dumps(
            project.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _is_finalized(cls, session: Session, project: Project) -> bool:
        result = session.execute(
            text(
                "SELECT 1 FROM audit_anchor_recovery_checkpoints AS checkpoint "
                "JOIN audit_result_bindings AS binding "
                "ON binding.id = checkpoint.result_binding_id "
                "JOIN audit_receipts AS receipt ON receipt.id = binding.audit_receipt_id "
                "WHERE checkpoint.workspace_id = :workspace_id "
                "AND checkpoint.prepared_result_digest = :digest "
                "AND checkpoint.state = 'complete' "
                "AND binding.workspace_id = :workspace_id "
                "AND json_extract(binding.payload_json, '$.prepared_result_digest') = :digest "
                "AND receipt.workspace_id = :workspace_id "
                "AND json_extract(receipt.payload_json, '$.action') = 'project.create' "
                "AND json_extract(receipt.payload_json, '$.resource') = :resource LIMIT 1"
            ),
            {
                "workspace_id": str(project.workspace_id),
                "digest": cls._mutation_digest(project),
                "resource": f"project:{project.id}",
            },
        ).first()
        return result is not None

    def get(self, *, workspace_id: UUID, project_id: UUID) -> Project | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == str(project_id),
                    ProjectRow.workspace_id == str(workspace_id),
                )
            )
            if row is None:
                return None
            project = self._to_project(row)
            return project if self._is_finalized(session, project) else None

    def list_for_workspace(self, workspace_id: UUID) -> list[Project]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ProjectRow)
                .where(ProjectRow.workspace_id == str(workspace_id))
                .order_by(ProjectRow.created_at, ProjectRow.id)
            ).all()
            projects = [self._to_project(row) for row in rows]
            return [project for project in projects if self._is_finalized(session, project)]

    def close(self) -> None:
        self.engine.dispose()
