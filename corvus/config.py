from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_cache_path, user_config_path, user_data_path

from corvus.models import (
    Budget,
    FilesystemPolicy,
    ModelProvider,
    NetworkPolicy,
    Policy,
    SandboxPolicy,
)
from corvus.security import atomic_write


def _minimum_present(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


class CorvusPaths:
    def __init__(self, home: Path | None = None) -> None:
        environment_home = os.environ.get("CORVUS_HOME")
        home = home or (Path(environment_home) if environment_home else None)
        base = home or user_data_path("corvus", "corvus")
        self.data = Path(base)
        self.config = Path(home) / "config" if home else user_config_path("corvus", "corvus")
        self.cache = Path(home) / "cache" if home else user_cache_path("corvus", "corvus")
        self.db = self.data / "corvus.db"
        self.artifacts = self.data / "artifacts"
        self.bundles = self.data / "bundles"
        self.backups = self.data / "backups"

    def ensure(self) -> None:
        for path in (
            self.data,
            self.config,
            self.cache,
            self.artifacts,
            self.bundles,
            self.backups,
        ):
            path.mkdir(parents=True, exist_ok=True)


class ConfigManager:
    def __init__(self, paths: CorvusPaths) -> None:
        self.paths = paths
        paths.ensure()

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def load_policy(self, project_root: Path | None = None) -> Policy:
        data = self._read_yaml(self.paths.config / "policy.yaml")
        policy = Policy.model_validate(data) if data else Policy()
        if project_root:
            project_data = self._read_yaml(project_root / ".corvus" / "policy.yaml")
            if project_data:
                project = Policy.model_validate(project_data)
                policy = self._narrow(policy, project)
        return policy

    @staticmethod
    def _narrow(user: Policy, project: Policy) -> Policy:
        return Policy(
            autonomy=min(user.autonomy, project.autonomy),
            filesystem=FilesystemPolicy(
                read=[p for p in project.filesystem.read if p in user.filesystem.read],
                write=[p for p in project.filesystem.write if p in user.filesystem.write],
                deny=sorted(set(user.filesystem.deny + project.filesystem.deny)),
            ),
            network=NetworkPolicy(
                allow_domains=[
                    d for d in project.network.allow_domains if d in user.network.allow_domains
                ]
            ),
            confirm=sorted(set(user.confirm + project.confirm)),
            budgets=Budget(
                max_cost_usd=min(user.budgets.max_cost_usd, project.budgets.max_cost_usd),
                max_runtime_seconds=min(
                    user.budgets.max_runtime_seconds, project.budgets.max_runtime_seconds
                ),
                max_repair_attempts=min(
                    user.budgets.max_repair_attempts, project.budgets.max_repair_attempts
                ),
                max_input_tokens=_minimum_present(
                    user.budgets.max_input_tokens,
                    project.budgets.max_input_tokens,
                ),
                max_output_tokens=_minimum_present(
                    user.budgets.max_output_tokens,
                    project.budgets.max_output_tokens,
                ),
            ),
            sandbox=SandboxPolicy(
                network_default=user.sandbox.network_default and project.sandbox.network_default,
                cpu_limit=min(user.sandbox.cpu_limit, project.sandbox.cpu_limit),
                memory_mb=min(user.sandbox.memory_mb, project.sandbox.memory_mb),
                pids_limit=min(user.sandbox.pids_limit, project.sandbox.pids_limit),
            ),
        )

    def providers(self) -> list[ModelProvider]:
        data = self._read_yaml(self.paths.config / "providers.yaml")
        return [ModelProvider.model_validate(item) for item in data.get("providers", [])]

    def active_provider_name(self) -> str | None:
        data = self._read_yaml(self.paths.config / "providers.yaml")
        active = data.get("active_provider")
        return active if isinstance(active, str) and active else None

    def selected_provider(self) -> ModelProvider | None:
        providers = self.providers()
        if not providers:
            return None
        active = self.active_provider_name()
        if active is not None:
            selected = next((provider for provider in providers if provider.name == active), None)
            if selected is not None:
                return selected
        return providers[0]

    def save_providers(
        self,
        providers: list[ModelProvider],
        *,
        active_provider: str | None = None,
    ) -> None:
        path = self.paths.config / "providers.yaml"
        names = {provider.name for provider in providers}
        selected = active_provider
        if selected is None:
            previous = self.active_provider_name()
            selected = previous if previous in names else None
        if selected is not None and selected not in names:
            raise ValueError("active provider must name a configured provider")
        if selected is None and providers:
            selected = providers[0].name
        payload: dict[str, Any] = {
            "active_provider": selected,
            "providers": [provider.model_dump(mode="json") for provider in providers],
        }
        encoded = yaml.safe_dump(payload, allow_unicode=True, sort_keys=True).encode()
        atomic_write(path, encoded)

    def set_active_provider(self, name: str) -> None:
        providers = self.providers()
        self.save_providers(providers, active_provider=name)
