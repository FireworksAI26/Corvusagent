from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corvus.domain.client import ClientContext, ClientSurface
from corvus.domain.deployment import (
    AuthorityContractError,
    AuthorityMode,
    AuthProfile,
    ConfigurationContractError,
    DeploymentInstance,
    DeploymentInstanceLease,
    DeploymentInstanceStatus,
    DeploymentProfile,
    NetworkProfile,
    StorageProfile,
    WorkspaceAuthority,
    WorkspaceAuthorityState,
    fixed_workspace_lock_name,
    validate_configuration_combination,
    validate_exclusive_instance_lease,
)
from corvus.domain.execution import ExecutionKind, ExecutionPlacement
from corvus.domain.workspace import CollaborationMode, WorkspaceConfig


def test_deployment_profile_rejects_workspace_and_execution_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DeploymentProfile.model_validate(
            {
                "authority_mode": AuthorityMode.EMBEDDED_LOCAL,
                "auth_profile": AuthProfile.LOCAL_OS,
                "network_profile": NetworkProfile.IN_PROCESS,
                "storage_profile": StorageProfile.SQLITE,
                "enabled_adapters": {"cli"},
                "protocol_version": "v2alpha1",
                "collaboration_mode": "individual",
                "execution_placement_id": str(uuid4()),
            }
        )

    forbidden = {
        tuple(error["loc"])
        for error in exc_info.value.errors()
        if error["type"] == "extra_forbidden"
    }
    assert forbidden == {("collaboration_mode",), ("execution_placement_id",)}


def test_embedded_local_rejects_team_workspace_with_reason_code() -> None:
    profile = DeploymentProfile(
        authority_mode=AuthorityMode.EMBEDDED_LOCAL,
        auth_profile=AuthProfile.LOCAL_OS,
        network_profile=NetworkProfile.IN_PROCESS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters={"cli"},
        protocol_version="v2alpha1",
    )
    workspace = WorkspaceConfig(collaboration_mode=CollaborationMode.TEAM)
    placement = ExecutionPlacement(
        kind=ExecutionKind.LOCAL_RUNNER,
        runner_id=uuid4(),
        sandbox_profile="default",
        data_policy_digest="a" * 64,
    )

    with pytest.raises(ConfigurationContractError) as exc_info:
        validate_configuration_combination(profile, workspace, placement)

    assert exc_info.value.reason_code == "embedded_local_requires_individual_workspace"


def test_client_context_cannot_grant_authority() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ClientContext.model_validate(
            {
                "surface": ClientSurface.CLI,
                "session_id": str(uuid4()),
                "origin": "local-terminal",
                "capabilities": ["workspace.admin"],
                "autonomy_ceiling": 5,
            }
        )

    forbidden = {
        tuple(error["loc"])
        for error in exc_info.value.errors()
        if error["type"] == "extra_forbidden"
    }
    assert forbidden == {("autonomy_ceiling",), ("capabilities",)}


def test_vendor_cloud_rejects_sqlite_with_reason_code() -> None:
    profile = DeploymentProfile(
        authority_mode=AuthorityMode.VENDOR_CLOUD,
        auth_profile=AuthProfile.OIDC,
        network_profile=NetworkProfile.NETWORK_TLS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters={"web"},
        protocol_version="v2alpha1",
    )
    workspace = WorkspaceConfig(collaboration_mode=CollaborationMode.TEAM)
    placement = ExecutionPlacement(
        kind=ExecutionKind.CLOUD_WORKER,
        runner_id=uuid4(),
        sandbox_profile="default",
        data_policy_digest="b" * 64,
    )

    with pytest.raises(ConfigurationContractError) as exc_info:
        validate_configuration_combination(profile, workspace, placement)

    assert exc_info.value.reason_code == "vendor_cloud_requires_postgresql"


def test_embedded_local_rejects_cloud_worker_with_reason_code() -> None:
    profile = DeploymentProfile(
        authority_mode=AuthorityMode.EMBEDDED_LOCAL,
        auth_profile=AuthProfile.LOCAL_OS,
        network_profile=NetworkProfile.IN_PROCESS,
        storage_profile=StorageProfile.SQLITE,
        enabled_adapters={"cli"},
        protocol_version="v2alpha1",
    )
    workspace = WorkspaceConfig(collaboration_mode=CollaborationMode.INDIVIDUAL)
    placement = ExecutionPlacement(
        kind=ExecutionKind.CLOUD_WORKER,
        runner_id=uuid4(),
        sandbox_profile="default",
        data_policy_digest="c" * 64,
    )

    with pytest.raises(ConfigurationContractError) as exc_info:
        validate_configuration_combination(profile, workspace, placement)

    assert exc_info.value.reason_code == "embedded_local_requires_local_runner"


def test_same_epoch_clone_cannot_hold_fixed_workspace_lock() -> None:
    workspace_id = uuid4()
    epoch = 7
    active_instance_id = uuid4()
    clone_instance_id = uuid4()
    lock_name = fixed_workspace_lock_name(workspace_id, epoch)
    active_lease = DeploymentInstanceLease(
        workspace_id=workspace_id,
        authority_epoch=epoch,
        deployment_instance_id=active_instance_id,
        lock_name=lock_name,
        fencing_token=11,
        acquired_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(AuthorityContractError) as exc_info:
        validate_exclusive_instance_lease(
            active_lease,
            workspace_id=workspace_id,
            authority_epoch=epoch,
            claimant_instance_id=clone_instance_id,
            lock_name=lock_name,
        )

    assert exc_info.value.reason_code == "same_epoch_instance_lease_conflict"


def test_workspace_lock_name_is_stable_across_authority_epochs() -> None:
    workspace_id = uuid4()

    assert fixed_workspace_lock_name(workspace_id, 1) == fixed_workspace_lock_name(
        workspace_id, 999
    )


def test_deployment_instance_cannot_persist_private_activation_capability() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DeploymentInstance.model_validate(
            {
                "deployment_profile_id": str(uuid4()),
                "instance_public_key": "public-key-material",
                "non_exportable_activation_key_ref": "keyring://corvus/instance/current",
                "device_binding_digest": "a" * 64,
                "status": DeploymentInstanceStatus.ACTIVE,
                "activation_private_key": "must-never-be-persisted",
            }
        )

    assert tuple(exc_info.value.errors()[0]["loc"]) == ("activation_private_key",)
    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_active_workspace_authority_requires_exclusive_instance_lease() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WorkspaceAuthority(
            workspace_id=uuid4(),
            deployment_profile_id=uuid4(),
            deployment_instance_id=uuid4(),
            epoch=2,
            authority_generation=9,
            authority_state_root="b" * 64,
            authority_epoch_credential_id=uuid4(),
            trust_anchor_id=uuid4(),
            state=WorkspaceAuthorityState.ACTIVE,
        )

    assert exc_info.value.errors()[0]["ctx"]["reason_code"] == (
        "active_authority_requires_exclusive_lease"
    )
