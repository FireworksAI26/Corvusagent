from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from corvus.application.ports import AgentRuntimePort
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    CancellationResult,
    CapabilitySupport,
    ExecutableIdentity,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderFamily,
    ProviderHealth,
    ProviderStatus,
    ProviderTransport,
    compute_provider_binding_digest,
)
from corvus.infrastructure.agent_runtimes.registry import (
    ProviderAdapterFactory,
    ProviderAdapterKey,
    ProviderRegistry,
    ProviderRegistryError,
)

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _binding(
    tmp_path: Path,
    *,
    workspace_id: UUID,
    project_id: UUID | None,
    family: ProviderFamily,
    transport: ProviderTransport,
    binding_id: UUID | None = None,
    capabilities: AgentCapabilities | None = None,
) -> ProviderBinding:
    executable_identity = None
    credential_ref_id = None
    if transport is ProviderTransport.LOCAL_CLI:
        executable_identity = ExecutableIdentity(
            executable_path=(tmp_path / f"{family.value}.exe").resolve(),
            version="1.0.0",
            sha256_digest="a" * 64,
        )
    else:
        credential_ref_id = uuid4()
    return ProviderBinding(
        id=binding_id or uuid4(),
        workspace_id=workspace_id,
        project_id=project_id,
        family=family,
        transport=transport,
        status=ProviderStatus.AVAILABLE,
        executable_identity=executable_identity,
        credential_ref_id=credential_ref_id,
        model="test-model",
        capabilities=capabilities or AgentCapabilities(),
        health_checked_at=_NOW,
        version=1,
        data_egress_disclosure="Test prompts may leave this process.",
        server_storage_disclosure="Test provider retention may apply.",
    )


def _candidate(binding: ProviderBinding) -> ProviderCandidate:
    return ProviderCandidate(
        binding=binding,
        binding_version=binding.version,
        binding_digest=compute_provider_binding_digest(binding),
    )


class _Adapter:
    def __init__(
        self,
        candidates: tuple[object, ...],
        *,
        runtime_capabilities: AgentCapabilities | None = None,
        discover_error: BaseException | None = None,
    ) -> None:
        self.candidates = candidates
        self.runtime_capabilities = runtime_capabilities or AgentCapabilities()
        self.discover_error = discover_error
        self.calls: list[str] = []

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        self.calls.append("discover")
        if self.discover_error is not None:
            raise self.discover_error
        return cast(tuple[ProviderCandidate, ...], self.candidates)

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        self.calls.append("capabilities")
        return self.runtime_capabilities

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        self.calls.append("health")
        return ProviderHealth(
            binding_id=binding.id,
            binding_version=binding.version,
            binding_digest=compute_provider_binding_digest(binding),
            status=binding.status,
            observed_at=_NOW,
        )

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        self.calls.append("start")
        return cast(AgentRunStartResult, "start-result")

    async def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        self.calls.append(f"events:{after_sequence}")
        if False:  # pragma: no cover - makes this an async iterator
            yield cast(AgentRunEvent, None)

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        self.calls.append("cancel")
        return cast(CancellationResult, "cancel-result")

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        self.calls.append("resume")
        return cast(AgentRunHandle, "resume-result")


def _factory(
    key: ProviderAdapterKey,
    adapter: AgentRuntimePort,
    creations: list[ProviderAdapterKey],
) -> ProviderAdapterFactory:
    def build() -> AgentRuntimePort:
        creations.append(key)
        return adapter

    return ProviderAdapterFactory(key=key, builder=build)


@pytest.mark.asyncio
async def test_registry_snapshots_factories_once_and_discovers_in_stable_order(
    tmp_path: Path,
) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    codex_key = ProviderAdapterKey(ProviderFamily.CODEX, ProviderTransport.LOCAL_CLI)
    claude_key = ProviderAdapterKey(ProviderFamily.CLAUDE, ProviderTransport.LOCAL_CLI)
    codex_bindings = (
        _binding(
            tmp_path,
            workspace_id=workspace_id,
            project_id=project_id,
            family=ProviderFamily.CODEX,
            transport=ProviderTransport.LOCAL_CLI,
            binding_id=UUID(int=9),
        ),
        _binding(
            tmp_path,
            workspace_id=workspace_id,
            project_id=project_id,
            family=ProviderFamily.CODEX,
            transport=ProviderTransport.LOCAL_CLI,
            binding_id=UUID(int=2),
        ),
    )
    claude_binding = _binding(
        tmp_path,
        workspace_id=workspace_id,
        project_id=project_id,
        family=ProviderFamily.CLAUDE,
        transport=ProviderTransport.LOCAL_CLI,
        binding_id=UUID(int=7),
    )
    creations: list[ProviderAdapterKey] = []
    factories = [
        _factory(codex_key, _Adapter(tuple(map(_candidate, codex_bindings))), creations),
        _factory(claude_key, _Adapter((_candidate(claude_binding),)), creations),
    ]

    registry = ProviderRegistry(reversed(factories))
    discovered = await registry.discover(
        ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
    )

    assert tuple(registry.adapters) == (claude_key, codex_key)
    assert isinstance(registry.adapters, MappingProxyType)
    assert creations == [claude_key, codex_key]
    assert [candidate.binding.id for candidate in discovered] == [
        claude_binding.id,
        codex_bindings[1].id,
        codex_bindings[0].id,
    ]
    await registry.discover(
        ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
    )
    assert creations == [claude_key, codex_key]


def test_registry_rejects_duplicate_factory_keys_before_creating_any_adapter() -> None:
    key = ProviderAdapterKey(ProviderFamily.CODEX, ProviderTransport.LOCAL_CLI)
    creations: list[ProviderAdapterKey] = []
    adapter = _Adapter(())

    with pytest.raises(ProviderRegistryError) as error:
        ProviderRegistry(
            (
                _factory(key, adapter, creations),
                _factory(key, adapter, creations),
            )
        )

    assert error.value.reason_code == "provider_registry_duplicate_factory"
    assert creations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    (
        ("wrong_workspace", "provider_registry_candidate_scope_invalid"),
        ("wrong_project", "provider_registry_candidate_scope_invalid"),
        ("wrong_key", "provider_registry_candidate_key_invalid"),
        ("malformed", "provider_registry_candidate_invalid"),
    ),
)
async def test_registry_rejects_malformed_or_misbound_candidates_all_or_nothing(
    tmp_path: Path,
    mutation: str,
    reason_code: str,
) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    key = ProviderAdapterKey(ProviderFamily.CODEX, ProviderTransport.LOCAL_CLI)
    binding = _binding(
        tmp_path,
        workspace_id=workspace_id,
        project_id=project_id,
        family=key.family,
        transport=key.transport,
    )
    candidate: object = _candidate(binding)
    if mutation == "wrong_workspace":
        candidate = _candidate(binding.model_copy(update={"workspace_id": uuid4()}))
    elif mutation == "wrong_project":
        candidate = _candidate(binding.model_copy(update={"project_id": uuid4()}))
    elif mutation == "wrong_key":
        candidate = _candidate(binding.model_copy(update={"family": ProviderFamily.CLAUDE}))
    elif mutation == "malformed":
        candidate = object()

    registry = ProviderRegistry((_factory(key, _Adapter((candidate,)), []),))
    with pytest.raises(ProviderRegistryError) as error:
        await registry.discover(
            ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
        )
    assert error.value.reason_code == reason_code


@pytest.mark.asyncio
async def test_registry_rejects_duplicate_binding_ids_across_adapters(tmp_path: Path) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    binding_id = uuid4()
    factories: list[ProviderAdapterFactory] = []
    for family in (ProviderFamily.CODEX, ProviderFamily.CLAUDE):
        key = ProviderAdapterKey(family, ProviderTransport.LOCAL_CLI)
        binding = _binding(
            tmp_path,
            workspace_id=workspace_id,
            project_id=project_id,
            family=family,
            transport=key.transport,
            binding_id=binding_id,
        )
        factories.append(_factory(key, _Adapter((_candidate(binding),)), []))

    registry = ProviderRegistry(factories)
    with pytest.raises(ProviderRegistryError) as error:
        await registry.discover(
            ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
        )
    assert error.value.reason_code == "provider_registry_duplicate_binding"


@pytest.mark.asyncio
async def test_registry_errors_are_stable_and_do_not_reflect_adapter_secrets() -> None:
    key = ProviderAdapterKey(ProviderFamily.CODEX, ProviderTransport.LOCAL_CLI)
    registry = ProviderRegistry(
        (
            _factory(
                key,
                _Adapter((), discover_error=RuntimeError("Bearer top-secret-adapter-token")),
                [],
            ),
        )
    )

    with pytest.raises(ProviderRegistryError) as error:
        await registry.discover(ProviderDiscoveryQuery(workspace_id=uuid4()))

    assert error.value.reason_code == "provider_registry_discovery_failed"
    assert str(error.value) == "provider_registry_discovery_failed"
    assert "secret" not in repr(error.value).lower()


@pytest.mark.asyncio
async def test_registry_intersects_capabilities_and_routes_only_to_owner(tmp_path: Path) -> None:
    workspace_id = uuid4()
    project_id = uuid4()
    key = ProviderAdapterKey(ProviderFamily.CODEX, ProviderTransport.LOCAL_CLI)
    binding = _binding(
        tmp_path,
        workspace_id=workspace_id,
        project_id=project_id,
        family=key.family,
        transport=key.transport,
        capabilities=AgentCapabilities(
            text=CapabilitySupport.SUPPORTED,
            streaming=CapabilitySupport.SUPPORTED,
            tools=CapabilitySupport.UNVERIFIED,
            repository_read=CapabilitySupport.SUPPORTED,
        ),
    )
    adapter = _Adapter(
        (_candidate(binding),),
        runtime_capabilities=AgentCapabilities(
            text=CapabilitySupport.SUPPORTED,
            streaming=CapabilitySupport.UNVERIFIED,
            tools=CapabilitySupport.SUPPORTED,
            repository_read=CapabilitySupport.UNSUPPORTED,
        ),
    )
    registry = ProviderRegistry((_factory(key, adapter, []),))
    await registry.discover(
        ProviderDiscoveryQuery(workspace_id=workspace_id, project_id=project_id)
    )

    capabilities = registry.capabilities(binding)
    assert capabilities.text is CapabilitySupport.SUPPORTED
    assert capabilities.streaming is CapabilitySupport.UNVERIFIED
    assert capabilities.tools is CapabilitySupport.UNVERIFIED
    assert capabilities.repository_read is CapabilitySupport.UNSUPPORTED
    await registry.health(binding)

    request = cast(AgentRunRequest, SimpleNamespace(provider_binding_id=binding.id))
    handle = cast(AgentRunHandle, SimpleNamespace(provider_binding_id=binding.id))
    assert await registry.start(request) == "start-result"
    assert [event async for event in registry.events(handle, after_sequence=4)] == []
    assert await registry.cancel(handle, uuid4(), "f" * 64) == "cancel-result"
    assert await registry.resume(handle, request) == "resume-result"
    assert adapter.calls == [
        "discover",
        "capabilities",
        "health",
        "start",
        "events:4",
        "cancel",
        "resume",
    ]

    unknown = cast(AgentRunHandle, SimpleNamespace(provider_binding_id=uuid4()))
    with pytest.raises(ProviderRegistryError) as error:
        [event async for event in registry.events(unknown)]
    assert error.value.reason_code == "provider_registry_binding_unknown"
