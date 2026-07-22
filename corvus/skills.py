from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from corvus.models import SkillVersion
from corvus.store import SkillRow, TraceStore


class SkillError(RuntimeError):
    pass


class SkillRegistry:
    FORBIDDEN_PERMISSIONS: ClassVar[set[str]] = {
        "modify_core_policy",
        "unrestricted_host_write",
    }

    def __init__(self, store: TraceStore) -> None:
        self.store = store

    def create_draft(self, name: str, content: str, permissions: list[str]) -> SkillVersion:
        if self.FORBIDDEN_PERMISSIONS & set(permissions):
            raise SkillError("skill requests an immutable safety capability")
        with DbSession(self.store.engine) as session:
            latest = session.scalar(
                select(SkillRow)
                .where(SkillRow.skill_name == name)
                .order_by(SkillRow.version.desc())
                .limit(1)
            )
            version = 1 if latest is None else latest.version + 1
            row = SkillRow(
                skill_name=name,
                version=version,
                content=self.store.redactor.redact(content),
                permissions_json=json.dumps(sorted(set(permissions))),
                evaluation_json="{}",
                status="draft",
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.commit()
        return SkillVersion(version=version, content=content, permissions=permissions)

    def promote(self, name: str, version: int, evaluation: dict[str, object]) -> None:
        if not evaluation.get("passed"):
            raise SkillError("only evaluated drafts may be promoted")
        with DbSession(self.store.engine) as session:
            target = session.scalar(
                select(SkillRow).where(
                    SkillRow.skill_name == name,
                    SkillRow.version == version,
                    SkillRow.status == "draft",
                )
            )
            if target is None:
                raise SkillError("draft skill version not found")
            session.execute(
                update(SkillRow)
                .where(SkillRow.skill_name == name, SkillRow.status == "active")
                .values(status="retired")
            )
            target.status = "active"
            target.evaluation_json = json.dumps(evaluation, sort_keys=True)
            session.commit()

    def versions(self, name: str | None = None) -> list[tuple[str, SkillVersion]]:
        with DbSession(self.store.engine) as session:
            query = select(SkillRow).order_by(SkillRow.skill_name, SkillRow.version)
            if name:
                query = query.where(SkillRow.skill_name == name)
            rows = session.scalars(query).all()
        return [
            (
                row.skill_name,
                SkillVersion(
                    version=row.version,
                    content=row.content,
                    permissions=json.loads(row.permissions_json),
                    evaluation=json.loads(row.evaluation_json),
                    status=row.status,  # type: ignore[arg-type]
                    created_at=row.created_at,
                ),
            )
            for row in rows
        ]

    def rollback(self, name: str, version: int) -> None:
        with DbSession(self.store.engine) as session:
            target = session.scalar(
                select(SkillRow).where(
                    SkillRow.skill_name == name,
                    SkillRow.version == version,
                    SkillRow.status == "retired",
                )
            )
            if target is None:
                raise SkillError("retired skill version not found")
            session.execute(
                update(SkillRow)
                .where(SkillRow.skill_name == name, SkillRow.status == "active")
                .values(status="retired")
            )
            target.status = "active"
            session.commit()
