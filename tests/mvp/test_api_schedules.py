from __future__ import annotations

import secrets
from typing import cast

from fastapi.testclient import TestClient

from corvus.mvp.api import create_app
from corvus.mvp.safety import build_safety_preview
from tests.mvp.test_run_coordinator import FakeBackend, _coordinator


def test_schedule_api_creates_runs_now_and_controls_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    coordinator, repository, _ = _coordinator(tmp_path, FakeBackend(change_file=False))
    token = secrets.token_urlsafe(32)
    client = TestClient(
        create_app(
            database=tmp_path / "corvus.sqlite3",
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            run_coordinator=coordinator,
        )
    )
    with client:
        assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
        csrf = cast(str, client.get("/api/auth/session").json()["csrf_token"])
        preview = build_safety_preview(provider="codex", mode="chat", mcp_enabled=False)
        created = client.post(
            "/api/local/schedules",
            json={
                "name": "Daily repository review",
                "repository_id": repository.id,  # type: ignore[attr-defined]
                "task": "Review the repository and report risks",
                "recurrence": {"kind": "daily", "local_time": "09:00:00"},
                "timezone": "UTC",
                "mode": "chat",
                "effort": "high",
                "safety_digest": preview.policy_digest,
                "output_policy": "report_only",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201, created.text
        schedule_id = created.json()["id"]
        assert client.get("/api/local/schedules").json()[0]["id"] == schedule_id

        run = client.post(
            f"/api/local/schedules/{schedule_id}/run-now",
            headers={"X-CSRF-Token": csrf},
        )
        assert run.status_code == 200, run.text
        assert run.json()["repository_id"] == repository.id  # type: ignore[attr-defined]
        assert (
            client.post(
                f"/api/local/schedules/{schedule_id}/pause",
                headers={"X-CSRF-Token": csrf},
            ).json()["status"]
            == "paused"
        )
        assert (
            client.post(
                f"/api/local/schedules/{schedule_id}/resume",
                headers={"X-CSRF-Token": csrf},
            ).json()["status"]
            == "active"
        )
        assert (
            client.post(
                f"/api/local/schedules/{schedule_id}/archive",
                headers={"X-CSRF-Token": csrf},
            ).json()["status"]
            == "archived"
        )
