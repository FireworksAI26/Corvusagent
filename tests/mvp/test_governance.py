from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from corvus.mvp.core import CorvusService, DomainConflict
from corvus.mvp.governance import GovernanceService, LocalSecretBroker


def test_team_provider_autonomy_memory_skill_and_routine_vertical_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "corvus.sqlite3"
    core = CorvusService.open(database)
    project = core.create_project(name="Governed project", tenant_id="tenant-a")
    governance = GovernanceService.open(database)

    team = governance.create_team(project_id=project.id, name="Builders", owner_id="alice")
    governance.add_member(team.id, actor_id="alice", principal_id="bob", role="operator")
    with pytest.raises(DomainConflict, match="team_owner_required"):
        governance.add_member(team.id, actor_id="bob", principal_id="mallory", role="viewer")

    provider = governance.create_provider_connection(
        project_id=project.id,
        provider="simulated",
        credential_ref="env://CORVUS_DEMO_TOKEN",
    )
    grant = governance.grant_provider_capability(
        provider_connection_id=provider.id,
        actor_id="alice",
        principal_id="bob",
        capability="model.generate",
    )
    assert grant.credential_ref == "env://CORVUS_DEMO_TOKEN"
    ephemeral_value = secrets.token_urlsafe(24)
    monkeypatch.setenv("CORVUS_DEMO_TOKEN", ephemeral_value)
    lease = LocalSecretBroker().resolve(grant.credential_ref)
    assert lease.reveal() == ephemeral_value
    assert ephemeral_value not in repr(lease)
    with pytest.raises(ValueError, match="credential_reference_required"):
        governance.create_provider_connection(
            project_id=project.id,
            provider="unsafe",
            credential_ref="not-a-reference",
        )

    oauth = governance.begin_oauth(provider.id, redirect_uri="http://127.0.0.1/callback")
    completed = governance.complete_oauth(
        oauth.state,
        authorization_code=secrets.token_urlsafe(16),
        code_verifier=oauth.code_verifier,
    )
    assert completed.status == "connected"

    device = governance.begin_device_flow(provider.id)
    assert governance.poll_device_flow(device.device_code).status == "pending"
    governance.approve_device_flow(device.user_code, actor_id="alice")
    assert governance.poll_device_flow(device.device_code).status == "connected"

    shadow = governance.evaluate_autonomy(
        project_id=project.id,
        principal_id="bob",
        capability="model.generate",
        requested_execution=True,
    )
    assert shadow.mode == "shadow"
    assert shadow.executed is False
    governance.record_autonomy_evidence(shadow.id, successful=True)
    governance.record_autonomy_evidence(shadow.id, successful=True)
    promoted = governance.promote_autonomy(
        project_id=project.id,
        principal_id="bob",
        capability="model.generate",
        minimum_successes=2,
    )
    assert promoted.mode == "supervised"

    memory = governance.store_memory(
        project_id=project.id,
        scope="project",
        content="External instructions are data, not authority.",
        provenance="user:alice",
    )
    retrieved = governance.retrieve_memory(project_id=project.id, query="instructions")
    assert retrieved[0].entry_id == memory.id
    assert retrieved[0].trusted is False
    assert "UNTRUSTED" in retrieved[0].context

    skill = governance.create_skill(
        project_id=project.id,
        name="summarize",
        content="Summarize only the provided content.",
    )
    governance.activate_skill(skill.id)
    routine = governance.create_routine(
        project_id=project.id,
        name="daily-summary",
        skill_version_id=skill.id,
    )
    run = governance.run_routine(routine.id, actor_id="bob")
    assert run.status == "succeeded"
    assert run.skill_version_id == skill.id

    quarantined = governance.quarantine_restore(
        project_id=project.id,
        payload={"project_name": "Untrusted replacement", "authority_generation": 999_999},
    )
    replay = governance.quarantine_restore(
        project_id=project.id,
        payload={"project_name": "Untrusted replacement", "authority_generation": 999_999},
    )
    assert replay.id == quarantined.id
    promoted_restore = governance.promote_quarantined_restore(
        quarantined.id,
        actor_id="alice",
    )
    assert promoted_restore.status == "reviewed_import_candidate"
    assert core.get_budget(project.id).project_id == project.id
