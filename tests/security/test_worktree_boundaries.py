from __future__ import annotations

from pathlib import Path


def test_worktree_implementation_never_uses_shell_or_recursive_delete() -> None:
    source = (Path(__file__).parents[2] / "corvus" / "mvp" / "worktrees.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in source
    assert "rmtree" not in source
    assert "rm -rf" not in source
    assert '"worktree", "add"' in source
    assert '"worktree", "remove"' in source
