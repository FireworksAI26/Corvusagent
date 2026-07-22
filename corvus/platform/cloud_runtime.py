from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import Engine, text

CloudRuntimeState = Literal[
    "unprovisioned", "provisioning", "ready", "paused", "resuming", "failed", "lost"
]


class CloudRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CloudRuntimeBinding:
    workspace_id: UUID
    sandbox_id: str | None
    template_ref: str
    generation: int
    state: CloudRuntimeState
    updated_at: datetime
    error_code: str | None = None

    def as_dict(self, *, configured: bool = True) -> dict[str, object]:
        payload = asdict(self)
        payload.update(
            workspace_id=str(self.workspace_id),
            updated_at=self.updated_at.isoformat(),
            configured=configured,
            provider="e2b",
        )
        return payload


class CloudSandbox(Protocol):
    sandbox_id: str

    def pause(self) -> None: ...
    def kill(self) -> None: ...


class CloudRuntimeProvider(Protocol):
    def create(self, *, workspace_id: UUID, generation: int) -> CloudSandbox: ...
    def connect(self, sandbox_id: str) -> CloudSandbox: ...
    def verify_ready(self, sandbox: CloudSandbox) -> None: ...


class CloudRuntimeRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS cloud_runtime_bindings (
                    workspace_id VARCHAR(36) PRIMARY KEY,
                    sandbox_id VARCHAR(255),
                    template_ref VARCHAR(255) NOT NULL,
                    generation INTEGER NOT NULL,
                    state VARCHAR(32) NOT NULL,
                    error_code VARCHAR(255),
                    updated_at VARCHAR(64) NOT NULL
                )
            """))

    def get(self, workspace_id: UUID) -> CloudRuntimeBinding | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM cloud_runtime_bindings WHERE workspace_id=:workspace_id"),
                {"workspace_id": str(workspace_id)},
            ).mappings().first()
        if row is None:
            return None
        return CloudRuntimeBinding(
            workspace_id=UUID(str(row["workspace_id"])),
            sandbox_id=None if row["sandbox_id"] is None else str(row["sandbox_id"]),
            template_ref=str(row["template_ref"]),
            generation=int(row["generation"]),
            state=cast(CloudRuntimeState, str(row["state"])),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def put(self, binding: CloudRuntimeBinding) -> CloudRuntimeBinding:
        with self.engine.begin() as connection:
            current = connection.execute(
                text("SELECT generation FROM cloud_runtime_bindings WHERE workspace_id=:workspace_id"),
                {"workspace_id": str(binding.workspace_id)},
            ).scalar_one_or_none()
            if current is not None and int(current) > binding.generation:
                raise CloudRuntimeError("cloud_runtime_generation_conflict")
            connection.execute(text("""
                INSERT INTO cloud_runtime_bindings
                  (workspace_id,sandbox_id,template_ref,generation,state,error_code,updated_at)
                VALUES
                  (:workspace_id,:sandbox_id,:template_ref,:generation,:state,:error_code,:updated_at)
                ON CONFLICT(workspace_id) DO UPDATE SET
                  sandbox_id=excluded.sandbox_id, template_ref=excluded.template_ref,
                  generation=excluded.generation, state=excluded.state,
                  error_code=excluded.error_code, updated_at=excluded.updated_at
            """), {
                "workspace_id": str(binding.workspace_id),
                "sandbox_id": binding.sandbox_id,
                "template_ref": binding.template_ref,
                "generation": binding.generation,
                "state": binding.state,
                "error_code": binding.error_code,
                "updated_at": binding.updated_at.isoformat(),
            })
        return binding


class E2BCloudRuntimeProvider:
    def __init__(self, *, api_key: str, template_ref: str, timeout_seconds: int = 600) -> None:
        try:
            from e2b import Sandbox
        except ImportError as error:
            raise CloudRuntimeError("e2b_sdk_unavailable") from error
        self._sandbox_type: Any = Sandbox
        self._api_key = api_key
        self.template_ref = template_ref
        self._timeout = timeout_seconds
        self._ready_command = os.environ.get(
            "CORVUS_E2B_READY_COMMAND",
            "curl -fsS http://127.0.0.1:8080/api/health >/dev/null",
        )

    def create(self, *, workspace_id: UUID, generation: int) -> CloudSandbox:
        return self._sandbox_type.create(
            template=self.template_ref,
            api_key=self._api_key,
            timeout=self._timeout,
            lifecycle={"on_timeout": "pause", "auto_resume": False},
            metadata={"corvus_workspace_id": str(workspace_id), "generation": str(generation)},
        )

    def connect(self, sandbox_id: str) -> CloudSandbox:
        return self._sandbox_type.connect(
            sandbox_id, api_key=self._api_key, timeout=self._timeout
        )

    def verify_ready(self, sandbox: CloudSandbox) -> None:
        result = getattr(sandbox, "commands").run(self._ready_command, timeout=30)
        if int(getattr(result, "exit_code", 1)) != 0:
            raise CloudRuntimeError("cloud_runtime_readiness_failed")


class CloudRuntimeService:
    def __init__(self, *, repository: CloudRuntimeRepository,
                 provider: CloudRuntimeProvider, template_ref: str) -> None:
        self.repository = repository
        self.provider = provider
        self.template_ref = template_ref

    def status(self, workspace_id: UUID) -> CloudRuntimeBinding:
        return self.repository.get(workspace_id) or CloudRuntimeBinding(
            workspace_id, None, self.template_ref, 0, "unprovisioned", datetime.now(UTC)
        )

    def provision(self, workspace_id: UUID) -> CloudRuntimeBinding:
        current = self.status(workspace_id)
        if current.state == "ready":
            return current
        generation = current.generation + 1
        self.repository.put(CloudRuntimeBinding(
            workspace_id, None, self.template_ref, generation, "provisioning", datetime.now(UTC)
        ))
        try:
            sandbox = self.provider.create(workspace_id=workspace_id, generation=generation)
            self.provider.verify_ready(sandbox)
            return self.repository.put(CloudRuntimeBinding(
                workspace_id, sandbox.sandbox_id, self.template_ref, generation,
                "ready", datetime.now(UTC)
            ))
        except Exception as error:
            self.repository.put(CloudRuntimeBinding(
                workspace_id, None, self.template_ref, generation, "failed", datetime.now(UTC),
                "cloud_runtime_provision_failed",
            ))
            raise CloudRuntimeError("cloud_runtime_provision_failed") from error

    def resume(self, workspace_id: UUID) -> CloudRuntimeBinding:
        current = self.status(workspace_id)
        if current.sandbox_id is None or current.state not in {"paused", "failed"}:
            raise CloudRuntimeError("cloud_runtime_not_resumable")
        sandbox = self.provider.connect(current.sandbox_id)
        self.provider.verify_ready(sandbox)
        return self.repository.put(CloudRuntimeBinding(
            workspace_id, current.sandbox_id, current.template_ref, current.generation,
            "ready", datetime.now(UTC)
        ))

    def pause(self, workspace_id: UUID) -> CloudRuntimeBinding:
        current = self.status(workspace_id)
        if current.sandbox_id is None or current.state != "ready":
            raise CloudRuntimeError("cloud_runtime_not_pauseable")
        self.provider.connect(current.sandbox_id).pause()
        return self.repository.put(CloudRuntimeBinding(
            workspace_id, current.sandbox_id, current.template_ref, current.generation,
            "paused", datetime.now(UTC)
        ))

    def revoke(self, workspace_id: UUID) -> CloudRuntimeBinding:
        current = self.status(workspace_id)
        if current.sandbox_id is not None:
            self.provider.connect(current.sandbox_id).kill()
        return self.repository.put(CloudRuntimeBinding(
            workspace_id, None, current.template_ref, current.generation + 1,
            "lost", datetime.now(UTC)
        ))
