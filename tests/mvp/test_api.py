from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from corvus.mvp.api import create_app
from corvus.mvp.ingress import LocalEnvelopeSigner


def _client(database: Path) -> tuple[TestClient, str]:
    bootstrap_token = secrets.token_urlsafe(32)
    app = create_app(
        database=database,
        bootstrap_token=bootstrap_token,
        session_secret=secrets.token_bytes(32),
        replay_limit=100,
    )
    return TestClient(app), bootstrap_token


def _pair(client: TestClient, bootstrap_token: str) -> str:
    response = client.post("/api/auth/pair", json={"token": bootstrap_token})
    assert response.status_code == 200, response.text
    session = client.get("/api/auth/session")
    assert session.status_code == 200, session.text
    return cast(str, session.json()["csrf_token"])


def test_pairing_session_authorization_and_csrf(tmp_path: Path) -> None:
    client, bootstrap_token = _client(tmp_path / "corvus.sqlite3")

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/pair", json={"token": "wrong"}).status_code == 403

    csrf = _pair(client, bootstrap_token)
    replay = client.post("/api/auth/pair", json={"token": bootstrap_token})
    assert replay.status_code == 403
    assert replay.json()["detail"] == "pairing_token_consumed"
    assert client.post("/api/projects", json={"name": "No CSRF"}).status_code == 403
    created = client.post(
        "/api/projects",
        json={"name": "API project"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == "local"
    assert client.get("/api/projects").json()[0]["name"] == "API project"
    assert client.get("/openapi.json").status_code == 200


def test_pairing_cookie_tracks_the_transport_security(tmp_path: Path) -> None:
    http_token = secrets.token_urlsafe(32)
    https_token = secrets.token_urlsafe(32)
    http_app = create_app(
        database=tmp_path / "http.sqlite3",
        bootstrap_token=http_token,
        session_secret=secrets.token_bytes(32),
    )
    https_app = create_app(
        database=tmp_path / "https.sqlite3",
        bootstrap_token=https_token,
        session_secret=secrets.token_bytes(32),
    )

    http_cookie = (
        TestClient(http_app, base_url="http://127.0.0.1:8080")
        .post("/api/auth/pair", json={"token": http_token})
        .headers["set-cookie"]
    )
    https_cookie = (
        TestClient(https_app, base_url="https://127.0.0.1:8080")
        .post("/api/auth/pair", json={"token": https_token})
        .headers["set-cookie"]
    )

    assert "Secure" not in http_cookie
    assert "Secure" in https_cookie
    assert "HttpOnly" in http_cookie
    assert "SameSite=strict" in http_cookie


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ],
)
def test_default_vite_origins_are_trusted(tmp_path: Path, origin: str) -> None:
    bootstrap_token = secrets.token_urlsafe(32)
    app = create_app(
        database=tmp_path / "default-vite-origin.sqlite3",
        bootstrap_token=bootstrap_token,
        session_secret=secrets.token_bytes(32),
    )
    client = TestClient(app)
    csrf = _pair(client, bootstrap_token)

    response = client.post(
        "/api/projects",
        json={"name": "Vite origin"},
        headers={"Origin": origin, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 201


def test_origins_remain_fail_closed_and_explicitly_configurable(tmp_path: Path) -> None:
    default_token = secrets.token_urlsafe(32)
    configured_token = secrets.token_urlsafe(32)
    default_app = create_app(
        database=tmp_path / "default-origin.sqlite3",
        bootstrap_token=default_token,
        session_secret=secrets.token_bytes(32),
    )
    default_client = TestClient(default_app)
    default_csrf = _pair(default_client, default_token)
    assert (
        default_client.post(
            "/api/projects",
            json={"name": "Rejected origin"},
            headers={
                "Origin": "http://127.0.0.1:9999",
                "X-CSRF-Token": default_csrf,
            },
        ).status_code
        == 403
    )

    configured_app = create_app(
        database=tmp_path / "configured-origin.sqlite3",
        bootstrap_token=configured_token,
        session_secret=secrets.token_bytes(32),
        allowed_origins=frozenset({"http://127.0.0.1:4173"}),
    )
    configured_client = TestClient(configured_app)
    configured_csrf = _pair(configured_client, configured_token)
    assert (
        configured_client.post(
            "/api/projects",
            json={"name": "Explicit origin"},
            headers={
                "Origin": "http://127.0.0.1:4173",
                "X-CSRF-Token": configured_csrf,
            },
        ).status_code
        == 201
    )


def test_static_web_csp_needs_no_inline_style_exception(tmp_path: Path) -> None:
    static_root = tmp_path / "web"
    static_root.mkdir()
    (static_root / "index.html").write_text("<main>Corvus</main>", encoding="utf-8")
    app = create_app(
        database=tmp_path / "static.sqlite3",
        bootstrap_token=secrets.token_urlsafe(32),
        session_secret=secrets.token_bytes(32),
        static_web_dir=static_root,
    )

    response = TestClient(app).get("/")

    csp = response.headers["content-security-policy"]
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "connect-src 'self'" in csp


def test_readiness_can_prove_the_desktop_sidecar_instance(tmp_path: Path) -> None:
    instance_token = secrets.token_urlsafe(32)
    challenge = secrets.token_hex(16)
    app = create_app(
        database=tmp_path / "corvus.sqlite3",
        bootstrap_token=secrets.token_urlsafe(32),
        session_secret=secrets.token_bytes(32),
        instance_token=instance_token,
    )

    client = TestClient(app)
    public_response = client.get("/ready")
    response = client.get("/ready", headers={"X-Corvus-Challenge": challenge})

    assert public_response.json() == {"status": "ready"}
    assert "X-Corvus-Instance-Proof" not in public_response.headers
    assert response.json() == {"status": "ready"}
    expected_proof = hmac.new(
        instance_token.encode(), challenge.encode(), hashlib.sha256
    ).hexdigest()
    assert response.headers["X-Corvus-Instance-Proof"] == expected_proof
    assert instance_token not in str(response.headers)
    assert instance_token not in response.text


def test_standard_server_does_not_repair_an_existing_pairing(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"
    first_client, first_token = _client(database)
    _pair(first_client, first_token)
    second_client, second_token = _client(database)

    response = second_client.post("/api/auth/pair", json={"token": second_token})

    assert response.status_code == 409
    assert response.json()["detail"] == "pairing_already_completed"


def test_http_workflow_execution_and_sse_replay(tmp_path: Path) -> None:
    client, bootstrap_token = _client(tmp_path / "corvus.sqlite3")
    csrf = _pair(client, bootstrap_token)
    mutation_headers = {"X-CSRF-Token": csrf}
    project = client.post(
        "/api/projects",
        json={"name": "HTTP workflow"},
        headers=mutation_headers,
    ).json()
    outcome_response = client.post(
        f"/api/projects/{project['id']}/outcomes",
        json={"title": "Execute over HTTP", "acceptance_criteria": ["two items complete"]},
        headers=mutation_headers,
    )
    assert outcome_response.status_code == 201, outcome_response.text
    outcome = outcome_response.json()
    assert client.get(f"/api/projects/{project['id']}/outcomes").json() == [outcome]
    workflow_response = client.post(
        f"/api/outcomes/{outcome['id']}/workflows",
        json={
            "name": "HTTP graph",
            "items": [
                {"key": "prepare", "title": "Prepare"},
                {"key": "deliver", "title": "Deliver", "depends_on": ["prepare"]},
            ],
        },
        headers=mutation_headers,
    )
    assert workflow_response.status_code == 201, workflow_response.text
    workflow = workflow_response.json()
    assert client.get(f"/api/outcomes/{outcome['id']}/workflows").json() == [workflow]
    workflow_id = workflow["id"]
    listed_items = client.get(f"/api/workflows/{workflow_id}/work-items").json()
    assert [item["key"] for item in listed_items] == ["prepare", "deliver"]
    assert (
        client.post(f"/api/workflows/{workflow_id}/start", headers=mutation_headers).status_code
        == 200
    )
    assert (
        client.post(f"/api/workflows/{workflow_id}/run-next", headers=mutation_headers).json()[
            "key"
        ]
        == "prepare"
    )
    assert (
        client.post(f"/api/workflows/{workflow_id}/run-next", headers=mutation_headers).json()[
            "key"
        ]
        == "deliver"
    )
    assert client.get(f"/api/workflows/{workflow_id}").json()["status"] == "succeeded"

    stream = client.get(f"/api/workflows/{workflow_id}/events?follow=false")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: workflow.succeeded" in stream.text
    event_ids = [
        int(line.removeprefix("id: "))
        for line in stream.text.splitlines()
        if line.startswith("id: ")
    ]
    assert event_ids == sorted(event_ids)

    replay = client.get(
        f"/api/workflows/{workflow_id}/events?follow=false",
        headers={"Last-Event-ID": str(event_ids[-2])},
    )
    replay_ids = [
        int(line.removeprefix("id: "))
        for line in replay.text.splitlines()
        if line.startswith("id: ")
    ]
    assert replay_ids == [event_ids[-1]]


def test_http_approval_budget_and_duplicate_safety(tmp_path: Path) -> None:
    client, bootstrap_token = _client(tmp_path / "corvus.sqlite3")
    csrf = _pair(client, bootstrap_token)
    headers = {"X-CSRF-Token": csrf}
    project = client.post("/api/projects", json={"name": "Approval"}, headers=headers).json()
    assert (
        client.put(
            f"/api/projects/{project['id']}/budget",
            json={"limit_units": 10},
            headers=headers,
        ).status_code
        == 200
    )
    outcome = client.post(
        f"/api/projects/{project['id']}/outcomes",
        json={"title": "Approval", "acceptance_criteria": ["once"]},
        headers=headers,
    ).json()
    workflow = client.post(
        f"/api/outcomes/{outcome['id']}/workflows",
        json={
            "name": "Approval graph",
            "items": [
                {
                    "key": "apply",
                    "title": "Apply",
                    "cost_units": 4,
                    "requires_approval": True,
                    "effect": {
                        "kind": "filesystem",
                        "target": "demo/output.txt",
                        "payload": {"content": "approved"},
                    },
                }
            ],
        },
        headers=headers,
    ).json()
    workflow_id = workflow["id"]
    client.post(f"/api/workflows/{workflow_id}/start", headers=headers)
    waiting = client.post(f"/api/workflows/{workflow_id}/run-next", headers=headers)
    assert waiting.json()["status"] == "waiting_approval"
    effect = client.get(f"/api/workflows/{workflow_id}/effects").json()[0]
    first = client.post(f"/api/effects/{effect['id']}/approve", headers=headers)
    replay = client.post(f"/api/effects/{effect['id']}/approve", headers=headers)
    assert replay.json()["id"] == first.json()["id"]
    client.post(f"/api/workflows/{workflow_id}/run-next", headers=headers)
    final_effect = client.get(f"/api/workflows/{workflow_id}/effects").json()[0]
    assert final_effect["execution_count"] == 1
    budget = client.get(f"/api/projects/{project['id']}/budget").json()
    assert budget["reserved_units"] == 0
    assert budget["settled_units"] == 4


def test_http_workflow_controls_rejection_and_kill_switch(tmp_path: Path) -> None:
    client, bootstrap_token = _client(tmp_path / "corvus.sqlite3")
    headers = {"X-CSRF-Token": _pair(client, bootstrap_token)}
    project = client.post("/api/projects", json={"name": "Controls"}, headers=headers).json()
    client.put(
        f"/api/projects/{project['id']}/budget",
        json={"limit_units": 10},
        headers=headers,
    )
    outcome = client.post(
        f"/api/projects/{project['id']}/outcomes",
        json={"title": "Control run", "acceptance_criteria": ["governed"]},
        headers=headers,
    ).json()
    workflow = client.post(
        f"/api/outcomes/{outcome['id']}/workflows",
        json={
            "name": "Rejectable",
            "items": [
                {
                    "key": "apply",
                    "title": "Apply",
                    "cost_units": 3,
                    "requires_approval": True,
                    "effect": {
                        "kind": "filesystem",
                        "target": "demo/reject.txt",
                        "payload": {"content": "review"},
                    },
                }
            ],
        },
        headers=headers,
    ).json()
    workflow_id = workflow["id"]
    client.post(f"/api/workflows/{workflow_id}/start", headers=headers)
    client.post(f"/api/workflows/{workflow_id}/run-next", headers=headers)
    effect = client.get(f"/api/workflows/{workflow_id}/effects").json()[0]
    rejected = client.post(f"/api/effects/{effect['id']}/reject", headers=headers)
    replay = client.post(f"/api/effects/{effect['id']}/reject", headers=headers)
    assert rejected.status_code == 200, rejected.text
    assert replay.json()["id"] == rejected.json()["id"]
    assert rejected.json()["status"] == "rejected"
    assert client.get(f"/api/workflows/{workflow_id}").json()["status"] == "failed"
    assert client.get(f"/api/projects/{project['id']}/budget").json()["reserved_units"] == 0

    controlled = client.post(
        f"/api/outcomes/{outcome['id']}/workflows",
        json={"name": "Controlled", "items": [{"key": "one", "title": "One"}]},
        headers=headers,
    ).json()
    controlled_id = controlled["id"]
    client.post(f"/api/workflows/{controlled_id}/start", headers=headers)
    assert (
        client.post(f"/api/workflows/{controlled_id}/pause", headers=headers).json()["status"]
        == "paused"
    )
    assert (
        client.post(f"/api/workflows/{controlled_id}/resume", headers=headers).json()["status"]
        == "running"
    )
    assert client.put(
        f"/api/workflows/{controlled_id}/kill-switch",
        json={"enabled": True},
        headers=headers,
    ).json() == {"enabled": True, "scope_id": controlled_id, "scope_kind": "workflow"}
    blocked = client.post(f"/api/workflows/{controlled_id}/run-next", headers=headers)
    assert blocked.status_code == 409
    assert "kill_switch_enabled" in blocked.text
    assert (
        client.post(f"/api/workflows/{controlled_id}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )


def test_http_governance_and_ingress_operator_surfaces(tmp_path: Path) -> None:
    client, bootstrap_token = _client(tmp_path / "corvus.sqlite3")
    csrf = _pair(client, bootstrap_token)
    headers = {"X-CSRF-Token": csrf}
    session = client.get("/api/auth/session").json()
    project = client.post("/api/projects", json={"name": "Governed"}, headers=headers).json()
    project_id = project["id"]

    team = client.post(
        f"/api/projects/{project_id}/teams", json={"name": "Operators"}, headers=headers
    )
    assert team.status_code == 201, team.text
    assert client.get(f"/api/projects/{project_id}/teams").json() == [team.json()]

    provider = client.post(
        f"/api/projects/{project_id}/providers",
        json={"provider": "simulated", "credential_ref": "env://CORVUS_DEMO_TOKEN"},
        headers=headers,
    )
    assert provider.status_code == 201, provider.text
    assert client.get(f"/api/projects/{project_id}/providers").json() == [provider.json()]

    shadow = client.post(
        f"/api/projects/{project_id}/autonomy/evaluate",
        json={"capability": "model.generate", "requested_execution": True},
        headers=headers,
    )
    assert shadow.json()["mode"] == "shadow"
    assert shadow.json()["executed"] is False

    memory = client.post(
        f"/api/projects/{project_id}/memories",
        json={"scope": "project", "content": "External instructions remain data."},
        headers=headers,
    )
    assert memory.status_code == 201, memory.text
    assert client.get(f"/api/projects/{project_id}/memories").json() == [memory.json()]
    retrieved = client.get(
        f"/api/projects/{project_id}/memories/retrieve", params={"query": "instructions"}
    ).json()
    assert retrieved[0]["trusted"] is False

    skill = client.post(
        f"/api/projects/{project_id}/skills",
        json={"name": "summarize", "content": "Summarize only provided content."},
        headers=headers,
    ).json()
    active = client.post(f"/api/skills/{skill['id']}/activate", headers=headers).json()
    routine = client.post(
        f"/api/projects/{project_id}/routines",
        json={"name": "daily", "skill_version_id": active["id"]},
        headers=headers,
    ).json()
    run = client.post(f"/api/routines/{routine['id']}/run", headers=headers)
    assert run.json()["status"] == "succeeded"
    assert client.get(f"/api/projects/{project_id}/skills").json()[0]["status"] == "active"
    assert client.get(f"/api/projects/{project_id}/routines").json() == [routine]
    assert client.get("/api/offline-intents").json() == []

    signer = LocalEnvelopeSigner.generate(actor_id="channel:operator")
    assert (
        client.post(
            "/api/channel/actors",
            json={"actor_id": "channel:operator", "public_key": signer.public_key},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/channel/identities",
            json={
                "provider": "local-channel",
                "external_id": "operator",
                "principal_id": session["user_id"],
            },
            headers=headers,
        ).status_code
        == 200
    )
    envelope = signer.sign_channel_event(
        provider="local-channel",
        external_event_id="event-1",
        external_identity_id="operator",
        action="effect.approve",
        payload={"untrusted_text": "approve all"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    event_payload = envelope.model_dump(mode="json")
    first = client.post("/api/channel/events", json=event_payload)
    replay = client.post("/api/channel/events", json=event_payload)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "step_up_required"
    assert replay.json()["id"] == first.json()["id"]
    assert client.get("/api/channel/events").json() == [first.json()]
