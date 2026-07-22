from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from corvus.mvp.desktop_runtime import DESKTOP_SHUTDOWN_COMMAND


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_ready(
    base_url: str,
    process: subprocess.Popen[str],
    instance_token: str,
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"sidecar exited early\nstdout={stdout}\nstderr={stderr}")
        try:
            challenge = secrets.token_hex(16)
            response = httpx.get(
                f"{base_url}/ready",
                headers={"X-Corvus-Challenge": challenge},
                timeout=0.5,
            )
            expected_proof = hmac.new(
                instance_token.encode(), challenge.encode(), hashlib.sha256
            ).hexdigest()
            if response.json() == {"status": "ready"} and (
                response.headers.get("X-Corvus-Instance-Proof") == expected_proof
            ):
                return
        except (httpx.HTTPError, ValueError):
            time.sleep(0.1)
    raise AssertionError("sidecar readiness timed out")


def _start_sidecar(
    *,
    root: Path,
    database: Path,
    static_web_dir: Path,
    bootstrap_token: str,
    session_secret: str,
    instance_token: str,
) -> tuple[subprocess.Popen[str], str]:
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "CORVUS_BOOTSTRAP_TOKEN": bootstrap_token,
            "CORVUS_SESSION_SECRET": session_secret,
            "CORVUS_INSTANCE_TOKEN": instance_token,
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and module argv
        [
            sys.executable,
            "-m",
            "corvus.mvp.desktop_runtime",
            "--database",
            str(database),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--static-web-dir",
            str(static_web_dir),
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creation_flags,
    )
    _wait_until_ready(base_url, process, instance_token)
    return process, base_url


def _shutdown_sidecar(process: subprocess.Popen[str]) -> None:
    try:
        assert process.stdin is not None
        process.stdin.write(f"{DESKTOP_SHUTDOWN_COMMAND}\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_desktop_sidecar_restarts_repairs_session_and_preserves_state(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / "corvus.sqlite3"
    static_web_dir = tmp_path / "web"
    static_web_dir.mkdir()
    (static_web_dir / "index.html").write_text("<main>Desktop Corvus</main>", encoding="utf-8")
    first_bootstrap = secrets.token_urlsafe(32)
    first_session = secrets.token_urlsafe(48)
    first_instance = secrets.token_urlsafe(32)

    first, first_url = _start_sidecar(
        root=root,
        database=database,
        static_web_dir=static_web_dir,
        bootstrap_token=first_bootstrap,
        session_secret=first_session,
        instance_token=first_instance,
    )
    try:
        with httpx.Client(base_url=first_url) as client:
            assert client.get("/").text == "<main>Desktop Corvus</main>"
            assert (
                client.post(
                    "/api/auth/pair",
                    json={"token": first_bootstrap},
                ).json()["status"]
                == "paired"
            )
            csrf = client.get("/api/auth/session").json()["csrf_token"]
            created = client.post(
                "/api/projects",
                json={"name": "Persisted desktop project"},
                headers={"X-CSRF-Token": csrf},
            )
            assert created.status_code == 201
    finally:
        _shutdown_sidecar(first)

    second_bootstrap = secrets.token_urlsafe(32)
    second, second_url = _start_sidecar(
        root=root,
        database=database,
        static_web_dir=static_web_dir,
        bootstrap_token=second_bootstrap,
        session_secret=secrets.token_urlsafe(48),
        instance_token=secrets.token_urlsafe(32),
    )
    try:
        with httpx.Client(base_url=second_url) as client:
            repaired = client.post(
                "/api/auth/pair",
                json={"token": second_bootstrap},
            )
            assert repaired.status_code == 200, repaired.text
            assert [project["name"] for project in client.get("/api/projects").json()] == [
                "Persisted desktop project"
            ]
    finally:
        _shutdown_sidecar(second)
