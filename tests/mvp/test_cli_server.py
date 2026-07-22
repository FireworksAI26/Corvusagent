from typer.testing import CliRunner

from corvus.mvp.cli import mvp_app


def test_server_rejects_plaintext_non_loopback_binding() -> None:
    result = CliRunner().invoke(mvp_app, ["server", "--host", "0.0.0.0"])  # noqa: S104

    assert result.exit_code != 0
    assert "local_server_loopback_required" in result.output
