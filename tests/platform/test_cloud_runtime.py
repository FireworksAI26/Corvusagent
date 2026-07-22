from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import create_engine

from corvus.platform.cloud_runtime import (
    CloudRuntimeRepository,
    CloudRuntimeService,
)

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")


@dataclass
class _Sandbox:
    sandbox_id: str
    paused: bool = False
    killed: bool = False

    def pause(self) -> None:
        self.paused = True

    def kill(self) -> None:
        self.killed = True


class _Provider:
    def __init__(self) -> None:
        self.sandboxes: dict[str, _Sandbox] = {}
        self.ready_checks = 0

    def create(self, *, workspace_id: UUID, generation: int) -> _Sandbox:
        sandbox = _Sandbox(f"sandbox-{workspace_id}-{generation}")
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox

    def connect(self, sandbox_id: str) -> _Sandbox:
        return self.sandboxes[sandbox_id]

    def verify_ready(self, sandbox: _Sandbox) -> None:
        assert sandbox.killed is False
        sandbox.paused = False
        self.ready_checks += 1


def test_cloud_runtime_lifecycle_is_durable_and_generation_fenced() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    provider = _Provider()
    repository = CloudRuntimeRepository(engine)
    service = CloudRuntimeService(
        repository=repository,
        provider=provider,
        template_ref="corvus-pinned-template-v1",
    )

    provisioned = service.provision(WORKSPACE_ID)
    assert provisioned.state == "ready"
    assert provisioned.generation == 1
    assert repository.get(WORKSPACE_ID) == provisioned

    paused = service.pause(WORKSPACE_ID)
    assert paused.state == "paused"
    assert provider.sandboxes[paused.sandbox_id or ""].paused is True

    resumed = service.resume(WORKSPACE_ID)
    assert resumed.state == "ready"
    assert resumed.generation == 1
    assert provider.ready_checks == 2

    revoked = service.revoke(WORKSPACE_ID)
    assert revoked.state == "lost"
    assert revoked.generation == 2
    assert revoked.sandbox_id is None
