from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from corvus.mvp.core import CorvusService, DomainNotFound
from corvus.mvp.deployment import DeploymentSettings, SimulatedOidcProvider, TenantScopedQueries
from corvus.mvp.desktop import (
    DesktopSidecarController,
    LocalUpdateKey,
    SidecarState,
    UpdateManifest,
    verify_update_manifest,
)


def test_self_host_configuration_and_tenant_isolation(tmp_path: Path) -> None:
    settings = DeploymentSettings.from_mapping(
        {
            "CORVUS_MODE": "self_hosted",
            "CORVUS_DATABASE_URL": f"sqlite:///{tmp_path / 'corvus.sqlite3'}",
            "CORVUS_PUBLIC_URL": "http://127.0.0.1:8080",
            "CORVUS_OIDC_ISSUER": "simulated://local",
        }
    )
    assert settings.database_kind == "sqlite"
    with pytest.raises(ValueError, match="https_required_for_non_loopback_public_url"):
        DeploymentSettings.from_mapping(
            {
                "CORVUS_MODE": "self_hosted",
                "CORVUS_DATABASE_URL": "sqlite:///corvus.sqlite3",
                "CORVUS_PUBLIC_URL": "http://corvus.example.test",
            }
        )

    core = CorvusService.open(tmp_path / "corvus.sqlite3")
    project_a = core.create_project(name="Tenant A", tenant_id="tenant-a")
    project_b = core.create_project(name="Tenant B", tenant_id="tenant-b")
    queries = TenantScopedQueries(core.store)
    assert queries.get_project("tenant-a", project_a.id).id == project_a.id
    with pytest.raises(DomainNotFound, match="project_not_found"):
        queries.get_project("tenant-a", project_b.id)


def test_simulated_oidc_maps_claims_without_client_authority() -> None:
    provider = SimulatedOidcProvider(issuer="simulated://local")
    principal = provider.map_claims(
        {
            "iss": "simulated://local",
            "sub": "user-1",
            "tenant_id": "tenant-a",
            "roles": ["operator"],
            "workspace_authority": "forged-client-claim",
        }
    )
    assert principal.principal_id == "user-1"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == ("operator",)
    assert not hasattr(principal, "workspace_authority")


def test_desktop_sidecar_lifecycle_and_threshold_update_verification() -> None:
    controller = DesktopSidecarController()
    assert controller.state is SidecarState.STOPPED
    controller.start()
    controller.mark_ready()
    controller.connection_lost()
    controller.mark_ready()
    controller.stop()
    assert controller.history == (
        SidecarState.STOPPED,
        SidecarState.STARTING,
        SidecarState.READY,
        SidecarState.RECONNECTING,
        SidecarState.READY,
        SidecarState.STOPPED,
    )

    keys = (LocalUpdateKey.generate("test-key-1"), LocalUpdateKey.generate("test-key-2"))
    manifest = UpdateManifest(
        version="0.2.0-hackathon",
        artifact_digest="a" * 64,
        published_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        threshold=2,
        signatures=(),
    )
    signed = manifest.model_copy(
        update={"signatures": tuple(key.sign_manifest(manifest) for key in keys)}
    )
    verify_update_manifest(
        signed,
        trusted_public_keys={key.key_id: key.public_key for key in keys},
        minimum_version="0.1.0",
    )
    with pytest.raises(ValueError, match="update_signature_threshold_not_met"):
        verify_update_manifest(
            signed.model_copy(update={"signatures": signed.signatures[:1]}),
            trusted_public_keys={key.key_id: key.public_key for key in keys},
            minimum_version="0.1.0",
        )
