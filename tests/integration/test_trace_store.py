from pathlib import Path
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from corvus.models import RunPhase
from corvus.store import EventRow, TraceStore


def test_trace_chain_verifies_and_detects_tampering(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "corvus.db")
    run_id = uuid4()
    store.append(run_id, "run.created", RunPhase.UNDERSTAND, {"request": "safe"})
    store.append(run_id, "plan.created", RunPhase.PLAN, {"steps": ["inspect"]})

    assert store.verify(run_id) is True

    with DbSession(store.engine) as session:
        session.execute(
            update(EventRow)
            .where(EventRow.run_id == str(run_id), EventRow.sequence == 2)
            .values(payload_json='{"steps":["tampered"]}')
        )
        session.commit()

    assert store.verify(run_id) is False


def test_empty_or_unknown_trace_is_not_valid(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "corvus.db")

    assert store.verify(uuid4()) is False
