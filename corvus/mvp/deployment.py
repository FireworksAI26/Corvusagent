from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from corvus.mvp.core import DomainNotFound
from corvus.mvp.models import MvpModel, Project
from corvus.mvp.store import SqliteStore


class DeploymentSettings(MvpModel):
    mode: Literal["local", "self_hosted", "vendor_cloud"]
    database_url: str
    database_kind: Literal["sqlite", "postgresql"]
    public_url: str
    oidc_issuer: str | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DeploymentSettings:
        mode = values.get("CORVUS_MODE", "local")
        if mode not in {"local", "self_hosted", "vendor_cloud"}:
            raise ValueError("invalid_corvus_mode")
        database_url = values.get("CORVUS_DATABASE_URL", "sqlite:///corvus-mvp.sqlite3")
        if database_url.startswith("sqlite:///"):
            database_kind = "sqlite"
        elif database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            database_kind = "postgresql"
        else:
            raise ValueError("unsupported_database_url")
        public_url = values.get("CORVUS_PUBLIC_URL", "http://127.0.0.1:8080")
        parsed = urlparse(public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid_public_url")
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not is_loopback:
            raise ValueError("https_required_for_non_loopback_public_url")
        return cls(
            mode=cast(Literal["local", "self_hosted", "vendor_cloud"], mode),
            database_url=database_url,
            database_kind=cast(Literal["sqlite", "postgresql"], database_kind),
            public_url=public_url,
            oidc_issuer=values.get("CORVUS_OIDC_ISSUER"),
        )


class OidcPrincipal(MvpModel):
    principal_id: str
    tenant_id: str
    roles: tuple[str, ...]
    issuer: str


class SimulatedOidcProvider:
    def __init__(self, *, issuer: str) -> None:
        self.issuer = issuer

    def map_claims(self, claims: Mapping[str, object]) -> OidcPrincipal:
        if claims.get("iss") != self.issuer:
            raise ValueError("oidc_issuer_mismatch")
        subject = claims.get("sub")
        tenant_id = claims.get("tenant_id")
        roles = claims.get("roles", [])
        if not isinstance(subject, str) or not subject:
            raise ValueError("oidc_subject_required")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("oidc_tenant_required")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ValueError("oidc_roles_invalid")
        return OidcPrincipal(
            principal_id=subject,
            tenant_id=tenant_id,
            roles=tuple(roles),
            issuer=self.issuer,
        )


class TenantScopedQueries:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def get_project(self, tenant_id: str, project_id: str) -> Project:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mvp_projects WHERE tenant_id = ? AND id = ?",
                (tenant_id, project_id),
            ).fetchone()
            if row is None:
                raise DomainNotFound("project_not_found")
            return self._project(row)

    def list_projects(self, tenant_id: str) -> tuple[Project, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mvp_projects WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
            return tuple(self._project(row) for row in rows)

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        from datetime import datetime

        return Project(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def sqlite_path(settings: DeploymentSettings) -> Path:
    if settings.database_kind != "sqlite":
        raise ValueError("sqlite_configuration_required")
    return Path(settings.database_url.removeprefix("sqlite:///"))
