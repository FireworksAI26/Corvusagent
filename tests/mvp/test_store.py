from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import corvus.mvp.store as store_module
from corvus.mvp.store import SCHEMA_VERSION, SqliteStore


def test_concurrent_initialization_applies_each_migration_once(tmp_path: Path) -> None:
    database = tmp_path / "corvus.sqlite3"

    with ThreadPoolExecutor(max_workers=4) as executor:
        stores = list(executor.map(lambda _: SqliteStore(database), range(4)))

    assert len(stores) == 4
    with stores[0].connect() as connection:
        versions = [
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM mvp_schema_migrations ORDER BY version"
            ).fetchall()
        ]
    assert versions == list(range(1, SCHEMA_VERSION + 1))


def test_failed_migration_does_not_record_partial_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "corvus.sqlite3"
    migrations = store_module._MIGRATIONS
    monkeypatch.setattr(
        store_module,
        "_MIGRATIONS",
        (*migrations[:-1], migrations[-1] + "\nTHIS IS NOT SQL;\n"),
    )

    with pytest.raises(sqlite3.OperationalError):
        SqliteStore(database)

    connection = sqlite3.connect(database)
    try:
        versions = connection.execute(
            "SELECT version FROM mvp_schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    assert versions == []
