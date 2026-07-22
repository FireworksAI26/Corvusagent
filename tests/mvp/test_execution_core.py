from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from corvus.mvp.core import CorvusService, DomainConflict
from corvus.mvp.models import EffectBinding, WorkItemDefinition, WorkItemStatus


def _service(database: Path, *, now: datetime | None = None) -> CorvusService:
    current = now or datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    return CorvusService.open(database, clock=lambda: current)


def test_dependency_scheduler_persists_restart_recovery(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"
    service = _service(database)
    project = service.create_project(name="Launch demo")
    outcome = service.create_outcome(
        project_id=project.id,
        title="Ship the demo",
        acceptance_criteria=("artifact exists",),
    )
    workflow = service.create_workflow(
        outcome_id=outcome.id,
        name="Build and verify",
        items=(
            WorkItemDefinition(key="build", title="Build artifact"),
            WorkItemDefinition(key="verify", title="Verify artifact", depends_on=("build",)),
        ),
    )

    service.start_workflow(workflow.id)
    assert service.get_work_item(workflow.id, "build").status is WorkItemStatus.READY
    assert service.get_work_item(workflow.id, "verify").status is WorkItemStatus.PENDING

    completed = service.run_next(workflow.id, worker_id="local-worker")
    assert completed is not None
    assert completed.key == "build"
    assert completed.status is WorkItemStatus.SUCCEEDED

    restarted = _service(database)
    assert restarted.get_work_item(workflow.id, "verify").status is WorkItemStatus.READY
    recovered = restarted.recover_workflow(workflow.id)
    assert recovered.recovered_items == 0
    assert restarted.run_next(workflow.id, worker_id="local-worker").status is (
        WorkItemStatus.SUCCEEDED
    )
    assert restarted.get_workflow(workflow.id).status == "succeeded"
    assert restarted.list_artifacts(workflow.id)
    assert restarted.list_checkpoints(workflow.id)
    assert restarted.list_conversation_entries(workflow.id)


def test_stale_lease_recovery_uses_new_fence(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    service = _service(database, now=started_at)
    project = service.create_project(name="Lease demo")
    outcome = service.create_outcome(
        project_id=project.id,
        title="Recover safely",
        acceptance_criteria=("one attempt wins",),
    )
    workflow = service.create_workflow(
        outcome_id=outcome.id,
        name="Lease workflow",
        items=(WorkItemDefinition(key="only", title="Only item"),),
    )
    service.start_workflow(workflow.id)
    first = service.claim_next(
        workflow.id,
        worker_id="worker-a",
        lease_duration=timedelta(seconds=5),
    )
    assert first is not None

    recovered_service = _service(database, now=started_at + timedelta(seconds=6))
    recovery = recovered_service.recover_workflow(workflow.id)
    assert recovery.recovered_items == 1
    second = recovered_service.claim_next(workflow.id, worker_id="worker-b")
    assert second is not None
    assert second.lease_fence == first.lease_fence + 1
    with pytest.raises(DomainConflict, match="stale_lease_fence"):
        recovered_service.complete_claim(first)


def test_approval_is_consumed_once_and_budget_is_conserved(tmp_path: Path) -> None:
    service = _service(tmp_path / "corvus.sqlite3")
    project = service.create_project(name="Effect demo")
    service.set_budget(project.id, limit_units=10)
    outcome = service.create_outcome(
        project_id=project.id,
        title="Apply approved change",
        acceptance_criteria=("effect executes once",),
    )
    workflow = service.create_workflow(
        outcome_id=outcome.id,
        name="Approval workflow",
        items=(
            WorkItemDefinition(
                key="apply",
                title="Apply change",
                cost_units=4,
                requires_approval=True,
                effect=EffectBinding(
                    kind="filesystem",
                    target="demo/output.txt",
                    payload={"content": "approved"},
                ),
            ),
        ),
    )
    service.start_workflow(workflow.id)

    waiting = service.run_next(workflow.id, worker_id="local-worker")
    assert waiting is not None
    assert waiting.status is WorkItemStatus.WAITING_APPROVAL
    effect = service.list_effects(workflow.id)[0]
    budget = service.get_budget(project.id)
    assert (budget.reserved_units, budget.settled_units, budget.available_units) == (4, 0, 6)

    approval = service.approve_effect(effect.id, actor_id="local-user")
    replay = service.approve_effect(effect.id, actor_id="local-user")
    assert replay.id == approval.id

    completed = service.run_next(workflow.id, worker_id="local-worker")
    assert completed is not None
    assert completed.status is WorkItemStatus.SUCCEEDED
    assert service.get_effect(effect.id).execution_count == 1
    budget = service.get_budget(project.id)
    assert (budget.reserved_units, budget.settled_units, budget.available_units) == (0, 4, 6)

    with pytest.raises(DomainConflict, match="effect_already_executed"):
        service.execute_effect(effect.id)


def test_kill_switch_heartbeat_failure_and_retry_are_durable(tmp_path: Path) -> None:
    service = _service(tmp_path / "corvus.sqlite3")
    project = service.create_project(name="Control demo")
    outcome = service.create_outcome(
        project_id=project.id,
        title="Control execution",
        acceptance_criteria=("operators remain in control",),
    )
    workflow = service.create_workflow(
        outcome_id=outcome.id,
        name="Controlled workflow",
        items=(WorkItemDefinition(key="controlled", title="Controlled item"),),
    )
    service.start_workflow(workflow.id)
    service.set_kill_switch(scope_kind="workflow", scope_id=workflow.id, enabled=True)
    with pytest.raises(DomainConflict, match="workflow_kill_switch_enabled"):
        service.claim_next(workflow.id, worker_id="worker-a")

    service.set_kill_switch(scope_kind="workflow", scope_id=workflow.id, enabled=False)
    claim = service.claim_next(
        workflow.id,
        worker_id="worker-a",
        lease_duration=timedelta(seconds=5),
    )
    assert claim is not None
    renewed = service.heartbeat(claim, lease_duration=timedelta(seconds=20))
    assert renewed.lease_expires_at > claim.lease_expires_at

    failed = service.fail_claim(renewed, error="transient_provider_error")
    assert failed.status is WorkItemStatus.FAILED
    assert failed.error == "transient_provider_error"
    assert service.list_attempts(workflow.id)[0]["status"] == "failed"

    retried = service.retry_work_item(workflow.id, "controlled")
    assert retried.status is WorkItemStatus.READY
    assert service.run_next(workflow.id, worker_id="worker-b").status is WorkItemStatus.SUCCEEDED
    assert any(
        event["event_type"] == "work_item.retried" for event in service.list_events(workflow.id)
    )
    assert service.list_lineage(workflow.id)
