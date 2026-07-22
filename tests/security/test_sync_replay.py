from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from corvus.application.oauth import OAuthCallback, OAuthStart, VerifiedIdentity
from corvus.infrastructure.db import M1_CURRENT_REVISION, upgrade_database
from corvus.infrastructure.repositories.sync import SyncRepository
from corvus.mvp.api import create_app
from corvus.platform.api.dependencies import build_identity_dependencies
from corvus.store import TraceStore

_ORIGIN = "https://corvus.example"
_SESSION_SECRET = "session-secret-value-that-is-at-least-32-characters"  # noqa: S105


class _OAuthClient:
    def start(self, redirect_uri: str) -> OAuthStart:
        return OAuthStart(authorization_url="https://accounts.google.com/auth?state=opaque-state")

    def exchange(self, callback: OAuthCallback) -> VerifiedIdentity:
        return VerifiedIdentity(
            issuer="https://accounts.google.com",
            subject="sync-replay-user",
            email="sync-replay@example.com",
            email_verified=True,
            display_name="Sync Replay",
        )

    def abort(self, state: str) -> None:
        return None


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    assert upgrade_database(database) == M1_CURRENT_REVISION
    dependencies = build_identity_dependencies(
        database=database,
        oauth_client=_OAuthClient(),
        public_origin=_ORIGIN,
        session_secret=_SESSION_SECRET,
        clock=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
    )
    app = create_app(
        database=database,
        bootstrap_token=secrets.token_urlsafe(32),
        session_secret=secrets.token_bytes(48),
        identity_dependencies=dependencies,
    )
    return TestClient(app, base_url=_ORIGIN), database


def _login(client: TestClient) -> tuple[dict[str, object], dict[str, str]]:
    start = client.get("/api/v2/auth/google/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    callback = client.get(
        "/api/v2/auth/google/callback",
        params={"code": "provider-code", "state": "opaque-state"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    session = client.get("/api/v2/session")
    assert session.status_code == 200, session.text
    body = session.json()
    return body, {"Origin": _ORIGIN, "X-CSRF-Token": body["csrf_token"]}


def _workspace(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        "/api/v2/workspaces",
        headers={**headers, "Idempotency-Key": "sync-workspace"},
        json={"name": "Sync workspace", "workspace_kind": "individual"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mutation(account_id: str, *, key: str, version: int = 1) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "kind": "account_profile",
        "operation": "set_experience",
        "entity_id": account_id,
        "expected_version": version,
        "payload": {"experience_kind": "developer"},
    }


def test_sync_api_reuses_session_csrf_device_and_membership_boundaries(tmp_path: Path) -> None:
    client, _database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"

    assert client.post(path, json={"acknowledged_cursor": 0, "mutations": []}).status_code == 403
    assert (
        client.post(
            path,
            headers={**headers, "Origin": "https://attacker.example"},
            json={"acknowledged_cursor": 0, "mutations": []},
        ).status_code
        == 403
    )
    accepted = client.post(
        path,
        headers=headers,
        json={
            "acknowledged_cursor": 0,
            "mutations": [_mutation(str(session["account_id"]), key="api-replay")],
        },
    )
    repeated = client.post(
        path,
        headers=headers,
        json={
            "acknowledged_cursor": 0,
            "mutations": [_mutation(str(session["account_id"]), key="api-replay")],
        },
    )

    assert accepted.status_code == repeated.status_code == 200
    assert repeated.json() == accepted.json()
    page = client.get(f"/api/v2/workspaces/{workspace['id']}/sync", params={"cursor": 0})
    assert page.status_code == 200
    assert [change["sequence"] for change in page.json()["changes"]] == [1]


def test_sync_api_returns_stable_redacted_conflict_and_idempotency_mismatch(
    tmp_path: Path,
) -> None:
    client, _database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"
    mutation = _mutation(str(session["account_id"]), key="conflict-key")
    assert (
        client.post(
            path,
            headers=headers,
            json={"acknowledged_cursor": 0, "mutations": [mutation]},
        ).status_code
        == 200
    )

    mismatch = client.post(
        path,
        headers=headers,
        json={
            "acknowledged_cursor": 0,
            "mutations": [{**mutation, "expected_version": 2}],
        },
    )
    stale = client.post(
        path,
        headers=headers,
        json={
            "acknowledged_cursor": 0,
            "mutations": [_mutation(str(session["account_id"]), key="stale", version=1)],
        },
    )

    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "idempotency_payload_mismatch"
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "sync_version_conflict"
    assert detail["mutation_index"] == 0
    assert detail["submitted_expected_version"] == 1
    assert detail["current_version"] == 2
    rendered = stale.text.casefold()
    assert "sync-replay@example.com" not in rendered
    assert "csrf" not in rendered and "cookie" not in rendered and "token" not in rendered


def test_sync_api_rejects_unknown_unbounded_or_sensitive_commands_before_persistence(
    tmp_path: Path,
) -> None:
    client, database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"
    valid = _mutation(str(session["account_id"]), key="validation")

    bodies = (
        {"acknowledged_cursor": 0, "mutations": [{**valid, "kind": "thread"}]},
        {
            "acknowledged_cursor": 0,
            "mutations": [
                {**valid, "payload": {"experience_kind": "developer", "refresh_token": "x"}}
            ],
        },
        {"acknowledged_cursor": 0, "mutations": [valid] * 101},
    )
    for body in bodies:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 422, response.text

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_changes").fetchone() == (0,)


def test_request_validation_errors_never_echo_submitted_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"
    canaries = (
        "unknown-refresh-canary",
        "malformed-json-canary",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWX",
    )
    responses = (
        client.post(
            path,
            headers=headers,
            json={
                "acknowledged_cursor": 0,
                "mutations": [
                    {
                        **_mutation(str(session["account_id"]), key="unknown-field"),
                        "payload": {
                            "experience_kind": "developer",
                            "refresh_token": canaries[0],
                        },
                    }
                ],
            },
        ),
        client.post(
            path,
            headers={**headers, "Content-Type": "application/json"},
            content=('{"acknowledged_cursor":0,"mutations":["' + canaries[1] + '"'),
        ),
        client.post(
            path,
            headers=headers,
            json={
                "acknowledged_cursor": 0,
                "mutations": [_mutation(str(session["account_id"]), key=canaries[2])],
            },
        ),
    )

    assert [response.status_code for response in responses] == [422, 422, 422]
    assert responses[0].json()["detail"]["code"] == "invalid_request"
    assert responses[1].json()["detail"]["code"] == "invalid_request"
    assert responses[2].json()["detail"]["code"] == "sync_payload_rejected"
    rendered = "\n".join(response.text for response in responses) + caplog.text
    for canary in canaries:
        assert canary not in rendered


def test_legacy_request_validation_uses_value_free_envelope(tmp_path: Path) -> None:
    client, _database = _client(tmp_path)
    canary = "legacy-refresh-canary"

    response = client.post(
        "/api/auth/pair",
        json={"token": "valid-shape", "refresh_token": canary},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "request_validation_failed"}
    }
    assert canary not in response.text


def test_sync_api_maps_secret_bearing_tail_corruption_without_echo(
    tmp_path: Path,
) -> None:
    client, database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"
    assert (
        client.post(
            path,
            headers=headers,
            json={
                "acknowledged_cursor": 0,
                "mutations": [_mutation(str(session["account_id"]), key="corrupt-tail")],
            },
        ).status_code
        == 200
    )
    canary = "sync-integrity-canary"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP TRIGGER workspace_changes_no_update")
        row = connection.execute("SELECT * FROM workspace_changes").fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["refresh_token"] = canary
        body = {
            "workspace_id": UUID(row["workspace_id"]),
            "workspace_version": row["workspace_version"],
            "sequence": row["sequence"],
            "previous_digest": row["previous_digest"],
            "kind": row["kind"],
            "operation": row["operation"],
            "entity_id": UUID(row["entity_id"]),
            "entity_version": row["entity_version"],
            "payload": payload,
            "account_id": UUID(row["account_id"]),
            "principal_id": UUID(row["principal_id"]),
            "device_id": UUID(row["device_id"]),
            "device_version": row["device_version"],
            "created_at": datetime.fromisoformat(row["created_at"]),
        }
        if "membership_version" in row.keys():
            body["membership_version"] = row["membership_version"]
        digest = SyncRepository._change_digest(body)
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        connection.execute(
            "UPDATE workspace_changes SET payload_json = ?, change_digest = ?",
            (payload_json, digest),
        )
        connection.execute(
            "UPDATE workspace_sync_heads SET chain_digest = ? WHERE workspace_id = ?",
            (digest, workspace["id"]),
        )

    response = client.get(f"/api/v2/workspaces/{workspace['id']}/sync", params={"cursor": 0})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "sync_change_integrity_invalid"
    assert canary not in response.text


def test_sync_api_exposes_explicit_resync_and_cursor_ahead_errors(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    session, headers = _login(client)
    workspace = _workspace(client, headers)
    mutation_path = f"/api/v2/workspaces/{workspace['id']}/sync/mutations"
    assert (
        client.post(
            mutation_path,
            headers=headers,
            json={
                "acknowledged_cursor": 0,
                "mutations": [_mutation(str(session["account_id"]), key="resync")],
            },
        ).status_code
        == 200
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workspace_sync_heads SET retention_floor = 1 WHERE workspace_id = ?",
            (workspace["id"],),
        )

    resync = client.get(f"/api/v2/workspaces/{workspace['id']}/sync", params={"cursor": 0})
    ahead = client.get(f"/api/v2/workspaces/{workspace['id']}/sync", params={"cursor": 2})

    assert resync.status_code == 409
    assert resync.json()["detail"] == {
        "code": "sync_resync_required",
        "earliest_available": 2,
        "latest_sequence": 1,
        "resume_cursor": 1,
        "resources": [
            "/api/v2/session",
            f"/api/v2/workspaces/{workspace['id']}",
        ],
        "correlation_id": resync.json()["detail"]["correlation_id"],
    }
    assert ahead.status_code == 400
    assert ahead.json()["detail"]["code"] == "sync_cursor_ahead"


def test_sync_router_is_composed_once_and_unconfigured_routes_are_discoverable(
    tmp_path: Path,
) -> None:
    static = tmp_path / "web"
    static.mkdir()
    (static / "index.html").write_text("<main>Corvus</main>", encoding="utf-8")
    app = create_app(
        database=tmp_path / "legacy.db",
        bootstrap_token=secrets.token_urlsafe(32),
        session_secret=secrets.token_bytes(48),
        static_web_dir=static,
    )
    client = TestClient(app, base_url=_ORIGIN)
    path = "/api/v2/workspaces/00000000-0000-4000-8000-000000000001/sync"

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "platform_identity_unavailable"
    assert list(app.openapi()["paths"]).count("/api/v2/workspaces/{workspace_id}/sync") == 1
