from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from corvus.mvp.api import create_app

ROOT = Path(__file__).resolve().parents[2]


def _static_app(tmp_path: Path, static_web_dir: Path) -> TestClient:
    return TestClient(
        create_app(
            database=tmp_path / "corvus.sqlite3",
            bootstrap_token=secrets.token_urlsafe(32),
            session_secret=secrets.token_bytes(32),
            static_web_dir=static_web_dir,
        )
    )


def test_static_web_and_api_share_one_origin(tmp_path: Path) -> None:
    static_web_dir = tmp_path / "web"
    assets = static_web_dir / "assets"
    assets.mkdir(parents=True)
    (static_web_dir / "index.html").write_text("<main>Corvus operator</main>", encoding="utf-8")
    (assets / "app.js").write_text("window.corvus = true;", encoding="utf-8")

    client = _static_app(tmp_path, static_web_dir)

    root_response = client.get("/")
    assert root_response.text == "<main>Corvus operator</main>"
    assert "default-src 'self'" in root_response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in root_response.headers["Content-Security-Policy"]
    assert root_response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/assets/app.js").text == "window.corvus = true;"
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/api/projects").status_code == 401
    assert client.get("/../pyproject.toml").status_code == 404


def test_static_web_requires_a_built_index(tmp_path: Path) -> None:
    static_web_dir = tmp_path / "web"
    static_web_dir.mkdir()

    with pytest.raises(ValueError, match="static_web_index_missing"):
        _static_app(tmp_path, static_web_dir)


def test_container_and_compose_contracts_are_tracked() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "USER corvus" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "--static-web-dir" in dockerfile
    assert "read_only: true" in compose
    assert "corvus-data:/data" in compose
