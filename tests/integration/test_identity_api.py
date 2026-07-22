from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, get_ident
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine
from sqlalchemy.exc import IntegrityError

from corvus.application.oauth import OAuthCallback, OAuthStart, VerifiedIdentity
from corvus.domain.account import ExperienceKind
from corvus.infrastructure.db import M1_CURRENT_REVISION, upgrade_database
from corvus.infrastructure.repositories.platform_identity import (
    PlatformIdentityRepository,
    PlatformIdentityRepositoryError,
)
from corvus.mvp.api import create_app
from corvus.platform.api.dependencies import build_identity_dependencies
from corvus.store import TraceStore

_ORIGIN = "https://corvus.example"
_CALLBACK = f"{_ORIGIN}/api/v2/auth/google/callback"
_SESSION_SECRET = "session-secret-value-that-is-at-least-32-characters"  # noqa: S105


class _OAuthClient:
    def __init__(self) -> None:
        self.exchange_calls: list[OAuthCallback] = []
        self.abort_calls: list[str] = []

    def start(self, redirect_uri: str) -> OAuthStart:
        assert redirect_uri == _CALLBACK
        return OAuthStart(
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth?state=opaque-state"
        )

    def exchange(self, callback: OAuthCallback) -> VerifiedIdentity:
        self.exchange_calls.append(callback)
        return VerifiedIdentity(
            issuer="https://accounts.google.com",
            subject="google-subject-1",
            email="lucas@example.com",
            email_verified=True,
            display_name="Lucas",
        )

    def abort(self, state: str) -> None:
        self.abort_calls.append(state)


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    assert upgrade_database(database) == M1_CURRENT_REVISION
    return database


def _configured_client(tmp_path: Path) -> tuple[TestClient, _OAuthClient]:
    return _configured_client_for_database(_database(tmp_path))


def _configured_client_for_database(database: Path) -> tuple[TestClient, _OAuthClient]:
    oauth = _OAuthClient()
    dependencies = build_identity_dependencies(
        database=database,
        oauth_client=oauth,
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
    return TestClient(app, base_url=_ORIGIN), oauth


def _synchronize_version_insert(
    monkeypatch: pytest.MonkeyPatch,
    *,
    table_name: str,
) -> None:
    barrier = Barrier(2, timeout=10)
    original_execute = Connection.execute
    synchronized_threads: set[int] = set()

    def execute(
        connection: Connection,
        statement: Any,
        parameters: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        sql = str(statement)
        thread_id = get_ident()
        if (
            f"INSERT INTO {table_name}" in sql
            and isinstance(parameters, dict)
            and parameters.get("version") == 2
            and thread_id not in synchronized_threads
        ):
            synchronized_threads.add(thread_id)
            barrier.wait()
        return original_execute(connection, statement, parameters, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute)


def _concurrent_results(calls: tuple[Callable[[], object], Callable[[], object]]) -> list[object]:
    def invoke(call: Callable[[], object]) -> object:
        try:
            return call()
        except Exception as exc:  # Return failures for symmetric race assertions.
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, call) for call in calls]
        return [future.result(timeout=20) for future in futures]


def _login(client: TestClient) -> tuple[str, str, str]:
    start = client.get("/api/v2/auth/google/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    response = client.get(
        "/api/v2/auth/google/callback",
        params={"code": "provider-code", "state": "opaque-state"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/onboarding"
    session_cookie = client.cookies.get("__Host-corvus_v2_session")
    device_cookie = client.cookies.get("__Host-corvus_v2_device")
    assert session_cookie is not None
    assert device_cookie is not None
    session = client.get("/api/v2/session")
    assert session.status_code == 200, session.text
    return session.json()["csrf_token"], session_cookie, device_cookie


def _mutation_headers(csrf: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Origin": _ORIGIN, "X-CSRF-Token": csrf}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_unconfigured_v2_routes_are_discoverable_and_truthfully_unavailable(tmp_path: Path) -> None:
    static = tmp_path / "web"
    static.mkdir()
    (static / "index.html").write_text("<main>Corvus</main>", encoding="utf-8")
    client = TestClient(
        create_app(
            database=tmp_path / "legacy.db",
            bootstrap_token=secrets.token_urlsafe(32),
            session_secret=secrets.token_bytes(48),
            static_web_dir=static,
        )
    )

    response = client.get("/api/v2/auth/google/start", follow_redirects=False)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "platform_identity_unavailable"
    assert client.get("/").text == "<main>Corvus</main>"
    paths = client.app.openapi()["paths"]
    assert "/api/v2/auth/google/start" in paths
    assert "/api/v2/devices" in paths
    assert list(paths).count("/api/v2/session") == 1


def test_google_callback_sets_separate_secure_host_only_cookies_and_clean_redirect(
    tmp_path: Path,
) -> None:
    client, oauth = _configured_client(tmp_path)

    start = client.get("/api/v2/auth/google/start", follow_redirects=False)
    callback = client.get(
        "/api/v2/auth/google/callback?code=provider-code&state=opaque-state",
        follow_redirects=False,
    )

    assert start.status_code == 302
    assert urlparse(start.headers["location"]).netloc == "accounts.google.com"
    assert parse_qs(urlparse(start.headers["location"]).query)["state"] == ["opaque-state"]
    assert callback.status_code == 303
    assert callback.headers["location"] == "/onboarding"
    set_cookie = callback.headers.get_list("set-cookie")
    assert any(
        value.startswith("__Host-corvus_v2_session=")
        and "Secure" in value
        and "HttpOnly" in value
        and "SameSite=lax" in value
        and "Path=/" in value
        and "Domain=" not in value
        for value in set_cookie
    )
    assert any(value.startswith("__Host-corvus_v2_device=") for value in set_cookie)
    assert all("provider-code" not in value and "opaque-state" not in value for value in set_cookie)
    assert oauth.exchange_calls == [OAuthCallback(code="provider-code", state="opaque-state")]


def test_google_callback_rejects_state_not_bound_to_the_initiating_browser(
    tmp_path: Path,
) -> None:
    client, oauth = _configured_client(tmp_path)

    callback = client.get(
        "/api/v2/auth/google/callback?code=provider-code&state=opaque-state",
        follow_redirects=False,
    )

    assert callback.status_code == 400
    assert callback.json()["detail"]["code"] == "oauth_state_invalid"
    assert oauth.exchange_calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"state": "opaque-state", "error": "provider-denial-canary"},
        {
            "state": "opaque-state",
            "error": "access_denied",
            "error_description": "provider-description-canary",
        },
        {"state": "opaque-state"},
        {"state": "opaque-state", "code": "x" * 4097},
    ],
)
def test_terminal_callback_rejection_consumes_valid_state_without_echoing_provider_values(
    tmp_path: Path,
    params: dict[str, str],
) -> None:
    client, oauth = _configured_client(tmp_path)
    start = client.get("/api/v2/auth/google/start", follow_redirects=False)
    assert start.status_code == 302

    response = client.get(
        "/api/v2/auth/google/callback",
        params=params,
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_callback_rejected"
    assert response.json()["detail"]["correlation_id"]
    assert oauth.abort_calls == ["opaque-state"]
    assert oauth.exchange_calls == []
    assert all(value not in response.text for value in params.values())


def test_session_onboarding_workspace_and_device_contracts_are_versioned_and_scoped(
    tmp_path: Path,
) -> None:
    client, _oauth = _configured_client(tmp_path)
    csrf, _session_token, _device_token = _login(client)
    session = client.get("/api/v2/session").json()

    missing_origin = client.put(
        "/api/v2/onboarding",
        json={"experience_kind": "developer", "expected_version": session["account_version"]},
        headers={"X-CSRF-Token": csrf},
    )
    cross_origin = client.put(
        "/api/v2/onboarding",
        json={"experience_kind": "developer", "expected_version": session["account_version"]},
        headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf},
    )
    assert missing_origin.status_code == 403
    assert cross_origin.status_code == 403

    onboarding = client.put(
        "/api/v2/onboarding",
        json={"experience_kind": "developer", "expected_version": session["account_version"]},
        headers=_mutation_headers(csrf),
    )
    assert onboarding.status_code == 200, onboarding.text
    assert onboarding.json()["experience_kind"] == "developer"
    assert onboarding.json()["version"] == session["account_version"] + 1

    workspace_body = {"name": "Lucas workspace", "workspace_kind": "individual"}
    workspace = client.post(
        "/api/v2/workspaces",
        json=workspace_body,
        headers=_mutation_headers(csrf, idempotency_key="workspace-create-1"),
    )
    repeated = client.post(
        "/api/v2/workspaces",
        json=workspace_body,
        headers=_mutation_headers(csrf, idempotency_key="workspace-create-1"),
    )
    assert workspace.status_code == 201, workspace.text
    assert repeated.status_code == 200
    assert repeated.json() == workspace.json()
    workspace_id = workspace.json()["id"]
    assert client.get("/api/v2/workspaces").json() == [workspace.json()]

    blank = client.patch(
        f"/api/v2/workspaces/{workspace_id}",
        json={"name": "   ", "expected_version": workspace.json()["version"]},
        headers=_mutation_headers(csrf),
    )
    assert blank.status_code == 422

    patched = client.patch(
        f"/api/v2/workspaces/{workspace_id}",
        json={"name": "Renamed", "expected_version": workspace.json()["version"]},
        headers=_mutation_headers(csrf),
    )
    stale = client.patch(
        f"/api/v2/workspaces/{workspace_id}",
        json={"name": "Stale", "expected_version": workspace.json()["version"]},
        headers=_mutation_headers(csrf),
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2
    assert stale.status_code == 409

    public_key_digest = hashlib.sha256(b"named-device-public-key").hexdigest()
    device_body = {"name": "Laptop", "public_key_digest": public_key_digest}
    device = client.post(
        "/api/v2/devices",
        json=device_body,
        headers=_mutation_headers(csrf, idempotency_key="device-create-1"),
    )
    repeated_device = client.post(
        "/api/v2/devices",
        json=device_body,
        headers=_mutation_headers(csrf, idempotency_key="device-create-1"),
    )
    assert device.status_code == 201, device.text
    assert repeated_device.status_code == 200
    assert repeated_device.json() == device.json()
    assert "token" not in device.text.casefold()
    assert len(client.get("/api/v2/devices").json()) == 2

    deleted = client.request(
        "DELETE",
        "/api/v2/devices",
        json={"device_id": device.json()["id"], "expected_version": device.json()["version"]},
        headers=_mutation_headers(csrf),
    )
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "revoked"


def test_refresh_rotates_cookie_rejects_replay_and_logout_clears_server_state(
    tmp_path: Path,
) -> None:
    client, _oauth = _configured_client(tmp_path)
    csrf, original_token, _device = _login(client)

    refresh = client.post("/api/v2/session/refresh", headers=_mutation_headers(csrf))

    assert refresh.status_code == 200, refresh.text
    rotated_token = client.cookies.get("__Host-corvus_v2_session")
    assert rotated_token is not None and rotated_token != original_token
    rotated_csrf = refresh.json()["csrf_token"]
    replay_client = TestClient(client.app, base_url=_ORIGIN)
    replay_client.cookies.set("__Host-corvus_v2_session", original_token)
    replay = replay_client.post(
        "/api/v2/session/refresh",
        headers=_mutation_headers(csrf),
    )
    assert replay.status_code == 401
    assert original_token not in replay.text

    logout = client.post("/api/v2/logout", headers=_mutation_headers(rotated_csrf))
    assert logout.status_code == 204
    assert client.get("/api/v2/session").status_code == 401
    assert any(
        value.startswith("__Host-corvus_v2_session=") and "Max-Age=0" in value
        for value in logout.headers.get_list("set-cookie")
    )


def test_legacy_pairing_auth_remains_independent_from_v2_cookie(tmp_path: Path) -> None:
    client, _oauth = _configured_client(tmp_path)
    _login(client)

    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/session",
        "/api/v2/onboarding",
        "/api/v2/workspaces",
        "/api/v2/devices",
    ],
)
def test_unauthenticated_v2_bodies_never_echo_desktop_or_provider_tokens(
    tmp_path: Path,
    path: str,
) -> None:
    client, _oauth = _configured_client(tmp_path)
    canary = "desktop-provider-secret-canary"

    response = client.get(path, params={"token": canary})

    assert response.status_code == 401
    assert canary not in response.text


def test_concurrent_onboarding_updates_return_one_stable_version_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    client, _oauth = _configured_client_for_database(database)
    _login(client)
    session = client.get("/api/v2/session").json()
    repository = PlatformIdentityRepository(create_engine(f"sqlite:///{database}"))
    _synchronize_version_insert(monkeypatch, table_name="account_onboarding_versions")
    now = datetime(2026, 7, 16, 12, 1, tzinfo=UTC)
    account_id = UUID(session["account_id"])

    results = _concurrent_results(
        (
            lambda: repository.update_onboarding(
                account_id=account_id,
                experience_kind=ExperienceKind.EVERYDAY,
                expected_version=session["account_version"],
                now=now,
            ),
            lambda: repository.update_onboarding(
                account_id=account_id,
                experience_kind=ExperienceKind.DEVELOPER,
                expected_version=session["account_version"],
                now=now,
            ),
        )
    )

    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], PlatformIdentityRepositoryError)
    assert str(errors[0]) == "account_version_conflict"
    assert repository.get_onboarding(account_id)[1] == session["account_version"] + 1


def test_concurrent_workspace_updates_return_one_stable_version_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    client, _oauth = _configured_client_for_database(database)
    csrf, _session, _device = _login(client)
    session = client.get("/api/v2/session").json()
    created = client.post(
        "/api/v2/workspaces",
        json={"name": "Race", "workspace_kind": "individual"},
        headers=_mutation_headers(csrf, idempotency_key="race-workspace"),
    ).json()
    repository = PlatformIdentityRepository(create_engine(f"sqlite:///{database}"))
    _synchronize_version_insert(monkeypatch, table_name="identity_workspaces")
    now = datetime(2026, 7, 16, 12, 1, tzinfo=UTC)
    workspace_id = UUID(created["id"])
    principal_id = UUID(session["principal_id"])

    results = _concurrent_results(
        (
            lambda: repository.update_workspace(
                principal_id=principal_id,
                workspace_id=workspace_id,
                name="First",
                expected_version=created["version"],
                now=now,
            ),
            lambda: repository.update_workspace(
                principal_id=principal_id,
                workspace_id=workspace_id,
                name="Second",
                expected_version=created["version"],
                now=now,
            ),
        )
    )

    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], PlatformIdentityRepositoryError)
    assert str(errors[0]) == "workspace_version_conflict"
    assert repository.list_workspaces(principal_id)[0].version == created["version"] + 1


def test_concurrent_device_revocations_return_one_stable_version_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    client, _oauth = _configured_client_for_database(database)
    csrf, _session, _browser_device = _login(client)
    session = client.get("/api/v2/session").json()
    created = client.post(
        "/api/v2/devices",
        json={"name": "Race", "public_key_digest": hashlib.sha256(b"race").hexdigest()},
        headers=_mutation_headers(csrf, idempotency_key="race-device"),
    ).json()
    repository = PlatformIdentityRepository(create_engine(f"sqlite:///{database}"))
    _synchronize_version_insert(monkeypatch, table_name="device_registrations")
    now = datetime(2026, 7, 16, 12, 1, tzinfo=UTC)
    account_id = UUID(session["account_id"])
    device_id = UUID(created["id"])

    def revoke() -> object:
        return repository.revoke_device(
            account_id=account_id,
            device_id=device_id,
            expected_version=created["version"],
            now=now,
        )

    results = _concurrent_results((revoke, revoke))

    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], PlatformIdentityRepositoryError)
    assert str(errors[0]) == "device_version_conflict"
    current = next(
        device for device in repository.list_devices(account_id) if device.id == device_id
    )
    assert current.version == created["version"] + 1


def test_unrelated_integrity_error_is_not_reclassified_as_version_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    client, _oauth = _configured_client_for_database(database)
    _login(client)
    session = client.get("/api/v2/session").json()
    repository = PlatformIdentityRepository(create_engine(f"sqlite:///{database}"))
    original_execute = Connection.execute

    def execute(
        connection: Connection,
        statement: Any,
        parameters: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if "INSERT INTO account_onboarding_versions" in str(statement):
            raise IntegrityError(
                str(statement),
                parameters,
                sqlite3.IntegrityError("CHECK constraint failed: unrelated_canary"),
            )
        return original_execute(connection, statement, parameters, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute)

    with pytest.raises(IntegrityError, match="unrelated_canary"):
        repository.update_onboarding(
            account_id=UUID(session["account_id"]),
            experience_kind=ExperienceKind.DEVELOPER,
            expected_version=session["account_version"],
            now=datetime(2026, 7, 16, 12, 1, tzinfo=UTC),
        )
