from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI

from corvus.mvp.hosted import create_hosted_app


def test_hosted_factory_preserves_local_server_boundary(monkeypatch) -> None:
    monkeypatch.setenv("CORVUS_PUBLIC_ORIGIN", "https://corvus.example")
    expected = Mock(spec=FastAPI)

    with patch("corvus.mvp.hosted.build_server_app", return_value=expected) as build:
        actual = create_hosted_app()

    assert actual is expected
    build.assert_called_once_with(
        database=Path("corvus.sqlite3"),
        pairing_ref="env://CORVUS_BOOTSTRAP_TOKEN",
        signing_ref="env://CORVUS_SESSION_SECRET",
        allowed_origins=frozenset({"https://corvus.example"}),
    )
