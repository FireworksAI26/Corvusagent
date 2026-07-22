from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from typer.testing import CliRunner

from corvus.mvp.cli import build_server_app, mvp_app

runner = CliRunner()
app = mvp_app


def test_server_app_loads_credentials_only_through_secret_references(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    bootstrap_token = secrets.token_urlsafe(32)
    session_secret = secrets.token_urlsafe(48)
    monkeypatch.setenv("CORVUS_TEST_PAIR_TOKEN", bootstrap_token)
    monkeypatch.setenv("CORVUS_TEST_SESSION_SECRET", session_secret)

    server_app = build_server_app(
        database=tmp_path / "server.sqlite3",
        pairing_ref="env://CORVUS_TEST_PAIR_TOKEN",
        signing_ref="env://CORVUS_TEST_SESSION_SECRET",
    )
    with TestClient(server_app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert (
            client.post(
                "/api/auth/pair",
                json={"token": bootstrap_token},
            ).status_code
            == 200
        )

    help_result = runner.invoke(
        app,
        ["server", "--help"],
        env={"COLUMNS": "240", "NO_COLOR": "1", "TERM": "dumb"},
        terminal_width=240,
    )
    assert help_result.exit_code == 0, help_result.output
    assert "bootstrap-token-ref" in help_result.output


def test_mvp_demo_runs_complete_restart_safe_path(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"

    result = runner.invoke(
        app,
        ["demo", "--database", str(database), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["workflow_status"] == "succeeded"
    assert payload["effect_execution_count"] == 1
    assert payload["budget"] == {"available": 6, "reserved": 0, "settled": 4}
    assert payload["restart_verified"] is True
    assert database.is_file()
    inspected = runner.invoke(
        app,
        [
            "workflow",
            "inspect",
            payload["workflow_id"],
            "--database",
            str(database),
            "--json",
        ],
    )
    assert inspected.exit_code == 0, inspected.output
    inspection = json.loads(inspected.stdout)
    assert len(inspection["work_items"]) == 2
    assert len(inspection["artifacts"]) == 2
    assert inspection["events"]
    assert inspection["conversation"]


def test_mvp_project_create_uses_same_sqlite_core(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"

    result = runner.invoke(
        app,
        ["project", "create", "CLI project", "--database", str(database), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "CLI project"
    assert payload["tenant_id"] == "local"


def test_mvp_cli_manages_outcome_and_workflow_through_application_service(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corvus.sqlite3"
    common = ["--database", str(database), "--json"]
    project_result = runner.invoke(app, ["project", "create", "Managed", *common])
    project_id = json.loads(project_result.stdout)["id"]

    outcome_result = runner.invoke(
        app,
        ["outcome", "create", project_id, "Managed outcome", "--criterion", "done", *common],
    )
    assert outcome_result.exit_code == 0, outcome_result.output
    outcome_id = json.loads(outcome_result.stdout)["id"]
    definitions = json.dumps(
        [
            {"key": "first", "title": "First"},
            {"key": "second", "title": "Second", "depends_on": ["first"]},
        ]
    )
    workflow_result = runner.invoke(
        app,
        [
            "workflow",
            "create",
            outcome_id,
            "Managed workflow",
            "--items-json",
            definitions,
            *common,
        ],
    )
    assert workflow_result.exit_code == 0, workflow_result.output
    workflow_id = json.loads(workflow_result.stdout)["id"]

    assert runner.invoke(app, ["workflow", "start", workflow_id, *common]).exit_code == 0
    first = runner.invoke(app, ["workflow", "run-next", workflow_id, *common])
    second = runner.invoke(app, ["workflow", "run-next", workflow_id, *common])
    assert json.loads(first.stdout)["key"] == "first"
    assert json.loads(second.stdout)["key"] == "second"
    status = runner.invoke(app, ["workflow", "status", workflow_id, *common])
    assert json.loads(status.stdout)["status"] == "succeeded"


def test_mvp_capabilities_demo_exercises_governed_local_adapters(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"
    result = runner.invoke(
        app,
        ["capabilities-demo", "--database", str(database), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["provider_status"] == "connected"
    assert payload["autonomy_mode"] == "supervised"
    assert payload["memory_trusted"] is False
    assert payload["routine_status"] == "succeeded"
    assert payload["offline_intent_status"] == "applied"
    assert payload["offline_application_count"] == 1
    assert payload["channel_event_status"] == "step_up_required"
    assert payload["channel_processing_count"] == 1
    assert payload["restore_status"] == "reviewed_import_candidate"


def test_mvp_config_check_validates_self_host_settings(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'corvus.sqlite3'}"
    result = runner.invoke(
        app,
        [
            "config-check",
            "--mode",
            "self_hosted",
            "--database-url",
            database_url,
            "--public-url",
            "http://127.0.0.1:8080",
            "--oidc-issuer",
            "simulated://local",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "self_hosted"
    assert payload["database_kind"] == "sqlite"
