from __future__ import annotations

import json
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from corvus.cli import app

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "v1" / "public_contract.json"


def _command_paths(command, path: tuple[str, ...]) -> set[tuple[str, ...]]:
    paths = {path}
    children = getattr(command, "commands", {})
    for name, child in children.items():
        paths.update(_command_paths(child, (*path, name)))
    return paths


def test_retained_v1_command_tree_is_unchanged() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    expected = {tuple(item["path"]) for item in fixture["commands"]}
    observed = _command_paths(get_command(app), ("corvus",))

    assert observed == expected
    assert ("corvus", "project") not in observed


def test_every_retained_v1_top_level_command_still_has_help() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    top_level = sorted(
        path[1] for item in fixture["commands"] if len(path := tuple(item["path"])) == 2
    )
    runner = CliRunner()

    for command in top_level:
        result = runner.invoke(app, [command, "--help"], prog_name="corvus")
        assert result.exit_code == 0, f"{command}: {result.output}"
        assert "Usage:" in result.output
