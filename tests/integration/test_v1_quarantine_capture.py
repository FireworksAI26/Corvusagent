from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import corvus.quarantine as quarantine
from corvus.database import V1_REQUIRED_TABLES, DatabaseState, classify_database
from corvus.security import sha256_file
from tests.fixture_corpus import verify_v1_fixture_corpus

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "v1"
FIXTURE_SOURCE = FIXTURE_ROOT / "legacy"
CANARY = "fixture-sensitive-value-123456"
SECOND_CANARY = "fixture-sensitive-value-654321"


def _copy_verified_fixture(tmp_path: Path, name: str) -> Path:
    expected_files = verify_v1_fixture_corpus(FIXTURE_ROOT)
    expected_source_files = {
        relative_path.removeprefix("legacy/"): expected
        for relative_path, expected in expected_files.items()
        if relative_path.startswith("legacy/")
    }
    actual_source_files = sorted(
        path.relative_to(FIXTURE_SOURCE).as_posix()
        for path in FIXTURE_SOURCE.rglob("*")
        if path.is_file()
    )
    assert actual_source_files == sorted(expected_source_files)

    destination = tmp_path / name
    shutil.copytree(FIXTURE_SOURCE, destination, copy_function=shutil.copy2)
    for relative_path, expected in sorted(expected_source_files.items()):
        copied = destination / relative_path
        assert copied.stat().st_size == expected["size"]
        assert sha256_file(copied) == expected["sha256"]
    return destination


def _capture(source: Path, quarantine_root: Path) -> quarantine.QuarantineReceipt:
    return quarantine.capture_v1_quarantine(
        database=source / "corvus.db",
        config_root=source / "config",
        project_root=source / "project",
        artifact_root=source / "artifacts",
        bundle_root=source / "bundles",
        backup_root=source / "backups",
        quarantine_root=quarantine_root,
    )


def test_v1_fixture_manifest_rejects_exact_path_set_and_byte_tampering(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURE_ROOT, corpus, copy_function=shutil.copy2)

    verify_v1_fixture_corpus(corpus)
    unexpected = corpus / "legacy" / "unlisted.bin"
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(AssertionError):
        verify_v1_fixture_corpus(corpus)

    unexpected.unlink()
    policy = corpus / "legacy" / "config" / "policy.yaml"
    original_policy = policy.read_bytes()
    policy.unlink()
    with pytest.raises(AssertionError):
        verify_v1_fixture_corpus(corpus)

    policy.write_bytes(original_policy + b"# tampered\n")
    with pytest.raises(AssertionError):
        verify_v1_fixture_corpus(corpus)


def test_v1_legacy_corpus_is_complete_unstamped_and_semantically_representative() -> None:
    verify_v1_fixture_corpus(FIXTURE_ROOT)
    database = FIXTURE_SOURCE / "corvus.db"

    status = classify_database(database)
    assert status.state is DatabaseState.UNSTAMPED_V1
    assert status.tables == V1_REQUIRED_TABLES
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM skill_versions").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (1,)
        assert connection.execute(
            "SELECT kind, content, source, confidence, pinned FROM memories"
        ).fetchone() == (
            "semantic",
            "token=fixture-sensitive-value-123456",
            "explicit user input",
            "0.95",
            1,
        )
        assert connection.execute(
            "SELECT version, permissions_json, evaluation_json, status "
            "FROM skill_versions ORDER BY version"
        ).fetchall() == [
            (1, '["project_read"]', '{"passed":true,"suite":"legacy"}', "active"),
            (2, "[]", '{"passed":false,"suite":"legacy"}', "draft"),
        ]
        assert connection.execute(
            "SELECT event_type, payload_json FROM run_events ORDER BY sequence"
        ).fetchall() == [
            ("conversation.started", '{"conversation_id":"conversation-v1"}'),
            (
                "conversation.message",
                '{"content":"token=fixture-sensitive-value-123456","role":"user"}',
            ),
        ]
        assert connection.execute(
            "SELECT status, bundle_json, approval_json, checkpoint_json FROM deliveries"
        ).fetchone() == (
            "approved",
            '{"changed_files":["README.md"],"manifest_digest":"' + "3" * 64 + '"}',
            '{"approved_files":["README.md"]}',
            '{"backup_digest":"' + "4" * 64 + '"}',
        )
    assert (
        (FIXTURE_SOURCE / "project" / ".corvus" / "policy.yaml")
        .read_text(encoding="utf-8")
        .startswith("autonomy: 2\n")
    )


def test_v1_quarantine_capture_is_sealed_redacted_and_idempotent(tmp_path: Path) -> None:
    source = _copy_verified_fixture(tmp_path, "source")
    database = source / "corvus.db"
    captures = tmp_path / "quarantine"
    database_before = sha256_file(database)

    first = _capture(source, captures)
    second = _capture(source, captures)

    assert first == second
    assert first.path == captures / first.capture_id
    assert first.capture_id != first.records_sha256
    assert quarantine.verify_v1_quarantine(first.path) is True
    assert sha256_file(database) == database_before
    assert [item.name for item in captures.iterdir()] == [first.capture_id]

    records = json.loads((first.path / "records.json").read_text(encoding="utf-8"))
    encoded = json.dumps(records, sort_keys=True)
    assert CANARY not in encoded
    assert "[REDACTED]" in encoded
    assert ".env" not in records["user_config"]
    assert sorted(records["database"]) == [
        "deliveries",
        "memories",
        "run_events",
        "skill_versions",
    ]
    assert records["user_config"]["providers.yaml"]["providers"][0]["keyring_service"] == (
        "corvus-model-provider"
    )
    assert "api_key" in records["user_config"]["providers.yaml"]["providers"][0]
    assert records["project_policy"]["policy.yaml"]["autonomy"] == 2
    for domain in ("artifacts", "bundles", "backups"):
        assert records[domain]
        assert set(records[domain][0]) == {"relative_path", "sha256", "size"}

    manifest = json.loads((first.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capture_id"] == first.capture_id
    assert manifest["records_sha256"] == first.records_sha256
    assert manifest["schema_version"] == 3
    assert manifest["seal_algorithm"] == "sha256"
    assert set(manifest["source_components"]) == {
        "artifacts",
        "backups",
        "bundles",
        "database",
        "project_policy",
        "user_config",
    }
    assert len(manifest["source_database_sha256"]) == 64
    assert len(manifest["source_snapshot_sha256"]) == 64
    assert first.source_snapshot_sha256 == manifest["source_snapshot_sha256"]
    assert len(manifest["seal"]) == 64

    manifest["source_components"]["project_policy"] = "0" * 64
    (first.path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert quarantine.verify_v1_quarantine(first.path) is False

    (first.path / "records.json").write_text("{}", encoding="utf-8")
    assert quarantine.verify_v1_quarantine(first.path) is False
    with pytest.raises(ValueError, match="existing quarantine capture failed verification"):
        _capture(source, captures)


def test_source_distinct_captures_cannot_alias_after_redaction(tmp_path: Path) -> None:
    source_a = _copy_verified_fixture(tmp_path, "source-a")
    source_b = _copy_verified_fixture(tmp_path, "source-b")
    with sqlite3.connect(source_b / "corvus.db") as connection:
        connection.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            (
                f"token={SECOND_CANARY}",
                "00000000-0000-0000-0000-000000000001",
            ),
        )
        connection.commit()

    captures = tmp_path / "quarantine"
    first = _capture(source_a, captures)
    second = _capture(source_b, captures)

    assert first.records_sha256 == second.records_sha256
    assert first.capture_id != second.capture_id
    assert first.path != second.path
    assert quarantine.verify_v1_quarantine(first.path) is True
    assert quarantine.verify_v1_quarantine(second.path) is True

    first_manifest = json.loads((first.path / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second.path / "manifest.json").read_text(encoding="utf-8"))
    assert first_manifest["source_database_sha256"] != second_manifest["source_database_sha256"]
    assert sorted(item.name for item in captures.iterdir()) == sorted(
        [first.capture_id, second.capture_id]
    )

    first_records = (first.path / "records.json").read_bytes()
    second_records = (second.path / "records.json").read_bytes()
    assert first_records == second_records
    assert SECOND_CANARY.encode() not in second_records


@pytest.mark.parametrize("relative_path", ("config/.env", "config/providers.yaml"))
def test_user_config_source_distinct_captures_cannot_alias_after_redaction(
    tmp_path: Path, relative_path: str
) -> None:
    source_a = _copy_verified_fixture(tmp_path, "source-a")
    source_b = _copy_verified_fixture(tmp_path, "source-b")
    changed = source_b / relative_path
    changed.write_text(
        changed.read_text(encoding="utf-8").replace(CANARY, SECOND_CANARY),
        encoding="utf-8",
    )

    captures = tmp_path / "quarantine"
    first = _capture(source_a, captures)
    second = _capture(source_b, captures)

    assert (first.path / "records.json").read_bytes() == (second.path / "records.json").read_bytes()
    assert first.records_sha256 == second.records_sha256
    assert first.capture_id != second.capture_id
    assert first.source_snapshot_sha256 != second.source_snapshot_sha256

    first_manifest = json.loads((first.path / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second.path / "manifest.json").read_text(encoding="utf-8"))
    assert first_manifest["source_database_sha256"] == second_manifest["source_database_sha256"]
    assert first_manifest["source_snapshot_sha256"] != second_manifest["source_snapshot_sha256"]
    assert CANARY not in json.dumps(first_manifest, sort_keys=True)
    assert SECOND_CANARY not in json.dumps(second_manifest, sort_keys=True)
    assert quarantine.verify_v1_quarantine(first.path) is True
    assert quarantine.verify_v1_quarantine(second.path) is True


def test_project_policy_source_is_distinct_redacted_and_sealed(tmp_path: Path) -> None:
    source_a = _copy_verified_fixture(tmp_path, "source-a")
    source_b = _copy_verified_fixture(tmp_path, "source-b")
    policy = source_b / "project" / ".corvus" / "policy.yaml"
    policy.write_text(
        policy.read_text(encoding="utf-8") + f"# token={SECOND_CANARY}\n",
        encoding="utf-8",
    )

    captures = tmp_path / "quarantine"
    first = _capture(source_a, captures)
    second = _capture(source_b, captures)

    assert (first.path / "records.json").read_bytes() == (second.path / "records.json").read_bytes()
    assert first.capture_id != second.capture_id
    assert first.source_snapshot_sha256 != second.source_snapshot_sha256
    assert SECOND_CANARY.encode() not in (second.path / "records.json").read_bytes()
    assert quarantine.verify_v1_quarantine(first.path) is True
    assert quarantine.verify_v1_quarantine(second.path) is True


def test_v2_quarantine_manifests_remain_readable(tmp_path: Path) -> None:
    records_bytes = quarantine._canonical_json({"schema_version": 2})
    records_sha256 = quarantine.sha256_bytes(records_bytes)
    components = {
        "artifacts": "a" * 64,
        "backups": "b" * 64,
        "bundles": "c" * 64,
        "config": "d" * 64,
        "database": "e" * 64,
    }
    source_snapshot_sha256 = quarantine._source_snapshot_sha256(components)
    capture_id = quarantine._capture_id(records_sha256, source_snapshot_sha256)
    manifest = {
        "capture_id": capture_id,
        "records_sha256": records_sha256,
        "schema_version": 2,
        "seal_algorithm": "sha256",
        "source_components": components,
        "source_database_sha256": components["database"],
        "source_snapshot_sha256": source_snapshot_sha256,
    }
    manifest["seal"] = quarantine.sha256_bytes(quarantine._canonical_json(manifest))
    capture = tmp_path / capture_id
    capture.mkdir()
    (capture / "records.json").write_bytes(records_bytes)
    (capture / "manifest.json").write_bytes(quarantine._canonical_json(manifest))

    assert quarantine.verify_v1_quarantine(capture) is True
