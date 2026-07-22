from __future__ import annotations

from pathlib import Path

_SERVER_ONLY_SECRET_NAMES = (
    "CORVUS_GOOGLE_CLIENT_SECRET",
    "CORVUS_GOOGLE_CLIENT_SECRET_REF",
)


def test_google_oauth_secrets_are_absent_from_web_browser_sources() -> None:
    web_source = Path(__file__).parents[2] / "apps" / "web" / "src"
    browser_sources = tuple(web_source.rglob("*.ts")) + tuple(web_source.rglob("*.tsx"))

    for source in browser_sources:
        content = source.read_text(encoding="utf-8")
        for secret_name in _SERVER_ONLY_SECRET_NAMES:
            assert secret_name not in content, f"{secret_name} crossed into {source}"
