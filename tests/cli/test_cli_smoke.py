import json
import sys

from typer.testing import CliRunner

from corvus.cli import app

runner = CliRunner()


def test_cli_help_preserves_v1_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Corvus trusted coding agent" in result.output
    for command in ("chat", "run", "doctor", "trace", "review", "undo", "eval"):
        assert command in result.output


def test_doctor_json_uses_isolated_corvus_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORVUS_HOME", str(tmp_path / "corvus-home"))

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["version"] == "0.2.0a1"
    expected_python_prefix = f"{sys.version_info.major}.{sys.version_info.minor}."
    assert payload["python"].startswith(expected_python_prefix)
    assert payload["database"]["ok"] is True
    assert payload["sandbox"]["selected"] in {"docker", "podman", "none"}
