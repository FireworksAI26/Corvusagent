from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from corvus.application.ports import AgentRuntimePort
from corvus.domain.agent_runtime import (
    AgentCapabilities,
    AgentRunEvent,
    AgentRunHandle,
    AgentRunRequest,
    AgentRunStartResult,
    CancellationResult,
    CapabilitySupport,
    ProviderBinding,
    ProviderCandidate,
    ProviderDiscoveryQuery,
    ProviderFamily,
    ProviderHealth,
    ProviderTransport,
    compute_provider_binding_digest,
)

_DUPLICATE_FACTORY = "provider_registry_duplicate_factory"
_FACTORY_INVALID = "provider_registry_factory_invalid"
_FACTORY_CREATION_FAILED = "provider_registry_factory_creation_failed"
_DISCOVERY_FAILED = "provider_registry_discovery_failed"
_CANDIDATE_INVALID = "provider_registry_candidate_invalid"
_CANDIDATE_SCOPE_INVALID = "provider_registry_candidate_scope_invalid"
_CANDIDATE_KEY_INVALID = "provider_registry_candidate_key_invalid"
_DUPLICATE_BINDING = "provider_registry_duplicate_binding"
_BINDING_UNKNOWN = "provider_registry_binding_unknown"
_BINDING_KEY_INVALID = "provider_registry_binding_key_invalid"
_OPERATION_FAILED = "provider_registry_operation_failed"


class ProviderRegistryError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, order=True)
class ProviderAdapterKey:
    family: ProviderFamily
    transport: ProviderTransport


@dataclass(frozen=True)
class ProviderAdapterFactory:
    key: ProviderAdapterKey
    builder: Callable[[], AgentRuntimePort]


@dataclass(frozen=True)
class _BindingRoute:
    key: ProviderAdapterKey
    binding: ProviderBinding


class ProviderRegistry:
    def __init__(self, factories: Iterable[ProviderAdapterFactory]) -> None:
        snapshot = tuple(factories)
        if any(
            not isinstance(factory, ProviderAdapterFactory)
            or not isinstance(factory.key, ProviderAdapterKey)
            or not callable(factory.builder)
            for factory in snapshot
        ):
            raise ProviderRegistryError(_FACTORY_INVALID)
        keys = tuple(factory.key for factory in snapshot)
        if len(set(keys)) != len(keys):
            raise ProviderRegistryError(_DUPLICATE_FACTORY)
        ordered = tuple(sorted(snapshot, key=lambda item: _key_order(item.key)))
        adapters: dict[ProviderAdapterKey, AgentRuntimePort] = {}
        try:
            for factory in ordered:
                adapter = factory.builder()
                if not isinstance(adapter, AgentRuntimePort):
                    raise TypeError("adapter_protocol_invalid")
                adapters[factory.key] = adapter
        except Exception:
            raise ProviderRegistryError(_FACTORY_CREATION_FAILED) from None
        self._adapters: Mapping[ProviderAdapterKey, AgentRuntimePort] = MappingProxyType(adapters)
        self._routes: Mapping[UUID, _BindingRoute] = MappingProxyType({})
        self._discovery_lock = asyncio.Lock()

    @property
    def adapters(self) -> Mapping[ProviderAdapterKey, AgentRuntimePort]:
        return self._adapters

    async def discover(self, query: ProviderDiscoveryQuery) -> tuple[ProviderCandidate, ...]:
        async with self._discovery_lock:
            candidates: list[tuple[ProviderAdapterKey, ProviderCandidate]] = []
            seen: set[UUID] = set()
            next_routes = dict(self._routes)
            for key, adapter in self._adapters.items():
                try:
                    discovered = await adapter.discover(query)
                except Exception:
                    raise ProviderRegistryError(_DISCOVERY_FAILED) from None
                if not isinstance(discovered, tuple):
                    raise ProviderRegistryError(_CANDIDATE_INVALID)
                for raw_candidate in discovered:
                    candidate = _revalidate_candidate(raw_candidate)
                    binding = candidate.binding
                    if (
                        binding.workspace_id != query.workspace_id
                        or binding.project_id != query.project_id
                    ):
                        raise ProviderRegistryError(_CANDIDATE_SCOPE_INVALID)
                    if _binding_key(binding) != key:
                        raise ProviderRegistryError(_CANDIDATE_KEY_INVALID)
                    if binding.id in seen:
                        raise ProviderRegistryError(_DUPLICATE_BINDING)
                    existing = next_routes.get(binding.id)
                    if existing is not None and (
                        existing.key != key or existing.binding != binding
                    ):
                        raise ProviderRegistryError(_DUPLICATE_BINDING)
                    seen.add(binding.id)
                    next_routes[binding.id] = _BindingRoute(key=key, binding=binding)
                    candidates.append((key, candidate))
            candidates.sort(key=lambda item: (*_key_order(item[0]), item[1].binding.id.int))
            self._routes = MappingProxyType(next_routes)
            return tuple(candidate for _, candidate in candidates)

    def capabilities(self, binding: ProviderBinding) -> AgentCapabilities:
        adapter = self._adapter_for_binding(binding)
        try:
            runtime_capabilities = adapter.capabilities(binding)
            runtime_capabilities = AgentCapabilities.model_validate(
                runtime_capabilities.model_dump(mode="python")
            )
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None
        values: dict[str, CapabilitySupport] = {}
        for field_name in AgentCapabilities.model_fields:
            values[field_name] = _intersect_capability(
                cast(CapabilitySupport, getattr(binding.capabilities, field_name)),
                cast(CapabilitySupport, getattr(runtime_capabilities, field_name)),
            )
        return AgentCapabilities(**values)

    async def health(self, binding: ProviderBinding) -> ProviderHealth:
        adapter = self._adapter_for_binding(binding)
        try:
            raw_health = await adapter.health(binding)
            health = ProviderHealth.model_validate(raw_health.model_dump(mode="python"))
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None
        if (
            health.binding_id != binding.id
            or health.binding_version != binding.version
            or health.binding_digest != compute_provider_binding_digest(binding)
        ):
            raise ProviderRegistryError(_OPERATION_FAILED)
        return health

    async def start(self, request: AgentRunRequest) -> AgentRunStartResult:
        adapter = self._adapter_for_id(request.provider_binding_id)
        try:
            return await adapter.start(request)
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None

    async def events(
        self,
        handle: AgentRunHandle,
        after_sequence: int = 0,
    ) -> AsyncIterator[AgentRunEvent]:
        adapter = self._adapter_for_id(handle.provider_binding_id)
        try:
            async for event in adapter.events(handle, after_sequence):
                yield event
        except ProviderRegistryError:
            raise
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None

    async def cancel(
        self,
        handle: AgentRunHandle,
        current_kill_switch_proof_id: UUID,
        current_kill_switch_proof_digest: str,
    ) -> CancellationResult:
        adapter = self._adapter_for_id(handle.provider_binding_id)
        try:
            return await adapter.cancel(
                handle,
                current_kill_switch_proof_id,
                current_kill_switch_proof_digest,
            )
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None

    async def resume(
        self,
        handle: AgentRunHandle,
        request_with_fresh_proofs: AgentRunRequest,
    ) -> AgentRunHandle:
        adapter = self._adapter_for_id(handle.provider_binding_id)
        if request_with_fresh_proofs.provider_binding_id != handle.provider_binding_id:
            raise ProviderRegistryError(_BINDING_KEY_INVALID)
        try:
            return await adapter.resume(handle, request_with_fresh_proofs)
        except Exception:
            raise ProviderRegistryError(_OPERATION_FAILED) from None

    def _adapter_for_binding(self, binding: ProviderBinding) -> AgentRuntimePort:
        route = self._route_for_id(binding.id)
        if route.key != _binding_key(binding) or route.binding != binding:
            raise ProviderRegistryError(_BINDING_KEY_INVALID)
        return self._adapters[route.key]

    def _adapter_for_id(self, binding_id: UUID) -> AgentRuntimePort:
        return self._adapters[self._route_for_id(binding_id).key]

    def _route_for_id(self, binding_id: UUID) -> _BindingRoute:
        route = self._routes.get(binding_id)
        if route is None:
            raise ProviderRegistryError(_BINDING_UNKNOWN)
        return route


def _key_order(key: ProviderAdapterKey) -> tuple[str, str]:
    return key.family.value, key.transport.value


def _binding_key(binding: ProviderBinding) -> ProviderAdapterKey:
    return ProviderAdapterKey(binding.family, binding.transport)


def _revalidate_candidate(raw_candidate: object) -> ProviderCandidate:
    if not isinstance(raw_candidate, ProviderCandidate):
        raise ProviderRegistryError(_CANDIDATE_INVALID)
    try:
        return ProviderCandidate.model_validate(raw_candidate.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ProviderRegistryError(_CANDIDATE_INVALID) from None


def _intersect_capability(
    binding_support: CapabilitySupport,
    runtime_support: CapabilitySupport,
) -> CapabilitySupport:
    if (
        binding_support is CapabilitySupport.UNSUPPORTED
        or runtime_support is CapabilitySupport.UNSUPPORTED
    ):
        return CapabilitySupport.UNSUPPORTED
    if (
        binding_support is CapabilitySupport.SUPPORTED
        and runtime_support is CapabilitySupport.SUPPORTED
    ):
        return CapabilitySupport.SUPPORTED
    return CapabilitySupport.UNVERIFIED
