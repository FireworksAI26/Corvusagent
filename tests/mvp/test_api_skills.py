from __future__ import annotations

import secrets
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from corvus.mvp.api import create_app
from corvus.mvp.skill_imports import SkillImportService
from corvus.mvp.store import SqliteStore


def test_local_skill_api_discovers_previews_imports_and_activates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    package = home / ".codex" / "skills" / "demo-review"
    package.mkdir(parents=True)
    package.joinpath("SKILL.md").write_text(
        "---\nname: demo-review\ndescription: Review a contribution for the demo\n---\n\n"
        "Inspect the diff and report focused risks.\n",
        encoding="utf-8",
    )
    database = tmp_path / "corvus.sqlite3"
    imports = SkillImportService(
        SqliteStore(database), library_root=tmp_path / "library", home=home
    )
    token = secrets.token_urlsafe(32)
    client = TestClient(
        create_app(
            database=database,
            bootstrap_token=token,
            session_secret=secrets.token_bytes(32),
            skill_import_service=imports,
        )
    )
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    csrf = cast(str, client.get("/api/auth/session").json()["csrf_token"])

    sources = client.get("/api/local/skills/sources")
    assert sources.status_code == 200
    candidate = sources.json()[0]
    assert candidate["source"] == "codex"
    preview = client.get(f"/api/local/skills/sources/{candidate['id']}/preview")
    assert preview.status_code == 200
    assert preview.json()["compatibility"] == "ready"

    imported = client.post(
        "/api/local/skills/import",
        json={"candidate_id": candidate["id"], "expected_digest": preview.json()["digest"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["status"] == "draft"
    activated = client.post(
        f"/api/local/skills/{imported.json()['id']}/activate",
        headers={"X-CSRF-Token": csrf},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    assert client.get("/api/local/skills").json()[0]["name"] == "demo-review"
