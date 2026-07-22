from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def verify_v1_fixture_corpus(root: Path) -> dict[str, dict[str, Any]]:
    """Assert that the V1 fixture manifest exactly covers immutable source bytes."""

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {"algorithm", "files", "schema_version"}
    assert manifest["algorithm"] == "sha256"
    assert manifest["schema_version"] == 1
    expected = manifest["files"]
    assert isinstance(expected, dict)

    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    )
    assert actual_paths == sorted(expected)

    for relative_path, expected_entry in sorted(expected.items()):
        assert isinstance(relative_path, str)
        assert Path(relative_path).as_posix() == relative_path
        assert not Path(relative_path).is_absolute()
        assert ".." not in Path(relative_path).parts
        assert isinstance(expected_entry, dict)
        assert set(expected_entry) == {"sha256", "size"}
        source = root / relative_path
        assert source.is_file()
        assert source.stat().st_size == expected_entry["size"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_entry["sha256"]

    return expected
