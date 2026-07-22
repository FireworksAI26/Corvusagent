from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import yaml

from corvus.orchestration import AgentOrchestrator
from corvus.store import TraceStore


async def run_eval(path: Path, store: TraceStore) -> dict[str, object]:
    suite = yaml.safe_load(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    results: list[dict[str, object]] = []
    for case in suite.get("cases", []):
        before = {entry.name for entry in path.parent.iterdir()}
        events = [
            event
            async for event in AgentOrchestrator(store).begin(str(case["prompt"]), path.parent)
        ]
        after = {entry.name for entry in path.parent.iterdir()}
        expected = str(case["expect_event"])
        passed = expected in {event.event_type for event in events}
        if case.get("expect_no_host_writes"):
            passed = passed and before == after
        results.append(
            {
                "id": case.get("id", str(uuid4())),
                "passed": passed,
                "events": [event.event_type for event in events],
                "run_id": str(events[0].run_id),
            }
        )
    return {
        "suite": suite.get("name", path.stem),
        "passed": all(bool(result["passed"]) for result in results),
        "results": results,
    }
