from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from corvus.database import DatabaseBootstrapError, DatabaseState, classify_database
from corvus.security import SecretRedactor, atomic_write, sha256_bytes, sha256_file

_CAPTURE_SCHEMA_VERSION = 3
_USER_CONFIG_FILES = ("onboarding.json", "onboarding.yaml", "policy.yaml", "providers.yaml")
_PROJECT_POLICY_DIRECTORY = ".corvus"
_PROJECT_POLICY_FILE = "policy.yaml"
_SECRET_KEYS = {"api_key", "apikey", "password", "secret", "token"}
_MAX_CONFIG_BYTES = 1_048_576
_MAX_FILES = 10_000
_MAX_FILE_BYTES = 100 * 1_048_576
_MAX_TOTAL_BYTES = 1_024 * 1_048_576
_TABLE_QUERIES: dict[str, tuple[tuple[str, ...], str]] = {
    "deliveries": (
        (
            "id",
            "run_id",
            "bundle_json",
            "approval_json",
            "checkpoint_json",
            "status",
            "created_at",
        ),
        "SELECT id, run_id, bundle_json, approval_json, checkpoint_json, status, created_at "
        "FROM deliveries ORDER BY id",
    ),
    "memories": (
        (
            "id",
            "project_id",
            "identity_id",
            "kind",
            "content",
            "source",
            "confidence",
            "pinned",
            "expires_at",
            "created_at",
        ),
        "SELECT id, project_id, identity_id, kind, content, source, confidence, pinned, "
        "expires_at, created_at FROM memories ORDER BY id",
    ),
    "run_events": (
        (
            "id",
            "run_id",
            "sequence",
            "event_type",
            "phase",
            "payload_json",
            "previous_hash",
            "event_hash",
            "created_at",
        ),
        "SELECT id, run_id, sequence, event_type, phase, payload_json, previous_hash, "
        "event_hash, created_at FROM run_events ORDER BY run_id, sequence, id",
    ),
    "skill_versions": (
        (
            "id",
            "skill_name",
            "version",
            "content",
            "permissions_json",
            "evaluation_json",
            "status",
            "created_at",
        ),
        "SELECT id, skill_name, version, content, permissions_json, evaluation_json, status, "
        "created_at FROM skill_versions ORDER BY skill_name, version, id",
    ),
}


@dataclass(frozen=True)
class QuarantineReceipt:
    capture_id: str
    path: Path
    records_sha256: str
    source_snapshot_sha256: str
    seal: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _sanitize(value: Any, redactor: SecretRedactor, *, key: str | None = None) -> Any:
    normalized_key = (key or "").casefold().replace("-", "_")
    if normalized_key in _SECRET_KEYS or normalized_key.endswith(
        ("_token", "_secret", "_password")
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, redactor, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_sanitize(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, redactor) for item in value]
    if isinstance(value, str):
        return redactor.redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redactor.redact(str(value))


def _snapshot_database(database: Path, destination: Path) -> None:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        result = source.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise ValueError("source database failed SQLite integrity check")
        source.backup(target)
        target.commit()
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("quarantine database snapshot failed SQLite integrity check")


def _database_records(snapshot: Path, redactor: SecretRedactor) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    with closing(sqlite3.connect(snapshot)) as connection:
        for table, (columns, query) in sorted(_TABLE_QUERIES.items()):
            rows = connection.execute(query).fetchall()
            records[table] = [
                _sanitize(dict(zip(columns, row, strict=True)), redactor) for row in rows
            ]
    return records


def _read_config_record(path: Path, relative_path: str, redactor: SecretRedactor) -> Any:
    if not path.is_file() or _is_link_or_reparse(path):
        raise ValueError(f"unsupported quarantine config entry: {relative_path}")
    size = path.stat().st_size
    if size > _MAX_CONFIG_BYTES:
        raise ValueError(f"quarantine config entry exceeds size limit: {relative_path}")
    text = path.read_text(encoding="utf-8")
    loaded = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return _sanitize(loaded, redactor)


def _user_config_records(root: Path, redactor: SecretRedactor) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for name in _USER_CONFIG_FILES:
        path = root / name
        if path.exists():
            records[name] = _read_config_record(path, name, redactor)
    return records


def _project_policy_records(project_root: Path | None, redactor: SecretRedactor) -> dict[str, Any]:
    if project_root is None:
        return {}
    relative_path = f"{_PROJECT_POLICY_DIRECTORY}/{_PROJECT_POLICY_FILE}"
    path = project_root / _PROJECT_POLICY_DIRECTORY / _PROJECT_POLICY_FILE
    if not path.exists():
        return {}
    return {_PROJECT_POLICY_FILE: _read_config_record(path, relative_path, redactor)}


def _project_policy_inventory(project_root: Path | None) -> list[dict[str, object]]:
    if project_root is None:
        return []
    return _file_inventory(project_root / _PROJECT_POLICY_DIRECTORY)


def _file_inventory(root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return []
    if not root.is_dir() or _is_link_or_reparse(root):
        raise ValueError(f"unsupported quarantine inventory root: {root.name}")
    inventory: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if _is_link_or_reparse(path):
            raise ValueError(
                f"link or reparse point rejected from quarantine inventory: {relative}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsupported quarantine inventory entry: {relative}")
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError(f"quarantine inventory file exceeds size limit: {relative}")
        total_bytes += size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("quarantine inventory exceeds total byte limit")
        inventory.append({"relative_path": relative, "sha256": sha256_file(path), "size": size})
        if len(inventory) > _MAX_FILES:
            raise ValueError("quarantine inventory exceeds file-count limit")
    return inventory


def _source_components(
    *,
    source_database_sha256: str,
    user_config: list[dict[str, object]],
    project_policy: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    bundles: list[dict[str, object]],
    backups: list[dict[str, object]],
) -> dict[str, str]:
    return {
        "artifacts": sha256_bytes(_canonical_json(artifacts)),
        "backups": sha256_bytes(_canonical_json(backups)),
        "bundles": sha256_bytes(_canonical_json(bundles)),
        "database": source_database_sha256,
        "project_policy": sha256_bytes(_canonical_json(project_policy)),
        "user_config": sha256_bytes(_canonical_json(user_config)),
    }


def _source_snapshot_sha256(components: dict[str, str]) -> str:
    return sha256_bytes(_canonical_json(components))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _capture_id(records_sha256: str, source_snapshot_sha256: str) -> str:
    return sha256_bytes(
        _canonical_json(
            {
                "records_sha256": records_sha256,
                "source_snapshot_sha256": source_snapshot_sha256,
            }
        )
    )


def _manifest(
    capture_id: str,
    records_sha256: str,
    source_database_sha256: str,
    source_components: dict[str, str],
    source_snapshot_sha256: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "capture_id": capture_id,
        "records_sha256": records_sha256,
        "schema_version": _CAPTURE_SCHEMA_VERSION,
        "seal_algorithm": "sha256",
        "source_components": source_components,
        "source_database_sha256": source_database_sha256,
        "source_snapshot_sha256": source_snapshot_sha256,
    }
    body["seal"] = sha256_bytes(_canonical_json(body))
    return body


def verify_v1_quarantine(path: Path) -> bool:
    try:
        records_bytes = (path / "records.json").read_bytes()
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        seal = manifest.pop("seal")
        expected_keys = {
            "capture_id",
            "records_sha256",
            "schema_version",
            "seal_algorithm",
            "source_components",
            "source_database_sha256",
            "source_snapshot_sha256",
        }
        if set(manifest) != expected_keys:
            return False
        records_sha256 = sha256_bytes(records_bytes)
        source_database_sha256 = manifest.get("source_database_sha256")
        source_snapshot_sha256 = manifest.get("source_snapshot_sha256")
        source_components = manifest.get("source_components")
        capture_id = manifest.get("capture_id")
        schema_version = manifest.get("schema_version")
        component_keys = (
            {"artifacts", "backups", "bundles", "config", "database"}
            if schema_version == 2
            else {
                "artifacts",
                "backups",
                "bundles",
                "database",
                "project_policy",
                "user_config",
            }
        )
        if schema_version not in {2, _CAPTURE_SCHEMA_VERSION}:
            return False
        if not isinstance(source_components, dict) or set(source_components) != component_keys:
            return False
        if not all(_is_sha256(value) for value in source_components.values()):
            return False
        return bool(
            _is_sha256(source_database_sha256)
            and _is_sha256(source_snapshot_sha256)
            and source_components["database"] == source_database_sha256
            and source_snapshot_sha256 == _source_snapshot_sha256(source_components)
            and _is_sha256(capture_id)
            and path.name == capture_id
            and capture_id == _capture_id(records_sha256, source_snapshot_sha256)
            and manifest.get("records_sha256") == records_sha256
            and manifest.get("seal_algorithm") == "sha256"
            and seal == sha256_bytes(_canonical_json(manifest))
            and isinstance(json.loads(records_bytes), dict)
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def capture_v1_quarantine(
    *,
    database: Path,
    config_root: Path,
    project_root: Path | None = None,
    artifact_root: Path,
    bundle_root: Path,
    backup_root: Path,
    quarantine_root: Path,
    redactor: SecretRedactor | None = None,
) -> QuarantineReceipt:
    status = classify_database(database)
    if status.state not in {DatabaseState.UNSTAMPED_V1, DatabaseState.CURRENT}:
        raise DatabaseBootstrapError(status)
    redactor = redactor or SecretRedactor()
    quarantine_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="corvus-v1-quarantine-db-") as temporary:
        snapshot = Path(temporary) / "corvus.db"
        _snapshot_database(database, snapshot)
        artifact_inventory = _file_inventory(artifact_root)
        backup_inventory = _file_inventory(backup_root)
        bundle_inventory = _file_inventory(bundle_root)
        user_config_inventory = _file_inventory(config_root)
        project_policy_inventory = _project_policy_inventory(project_root)
        user_config_records = _user_config_records(config_root, redactor)
        project_policy_records = _project_policy_records(project_root, redactor)
        if _file_inventory(config_root) != user_config_inventory:
            raise ValueError("quarantine user config source changed during capture")
        if _project_policy_inventory(project_root) != project_policy_inventory:
            raise ValueError("quarantine project policy source changed during capture")
        records = {
            "artifacts": artifact_inventory,
            "backups": backup_inventory,
            "bundles": bundle_inventory,
            "database": _database_records(snapshot, redactor),
            "project_policy": project_policy_records,
            "schema_version": _CAPTURE_SCHEMA_VERSION,
            "source_database": {
                "schema_version": status.schema_version,
                "state": status.state.value,
            },
            "user_config": user_config_records,
        }
        records_bytes = _canonical_json(records)
        records_sha256 = sha256_bytes(records_bytes)
        source_database_sha256 = sha256_file(snapshot)
        components = _source_components(
            source_database_sha256=source_database_sha256,
            user_config=user_config_inventory,
            project_policy=project_policy_inventory,
            artifacts=artifact_inventory,
            bundles=bundle_inventory,
            backups=backup_inventory,
        )
        source_snapshot_sha256 = _source_snapshot_sha256(components)
        capture_id = _capture_id(records_sha256, source_snapshot_sha256)
        manifest = _manifest(
            capture_id,
            records_sha256,
            source_database_sha256,
            components,
            source_snapshot_sha256,
        )

    destination = quarantine_root / capture_id
    if destination.exists():
        if not verify_v1_quarantine(destination):
            raise ValueError("existing quarantine capture failed verification")
    else:
        temporary_capture = quarantine_root / f".{capture_id}-{uuid4().hex}.tmp"
        try:
            temporary_capture.mkdir()
            atomic_write(temporary_capture / "records.json", records_bytes)
            atomic_write(temporary_capture / "manifest.json", _canonical_json(manifest))
            try:
                os.replace(temporary_capture, destination)
            except FileExistsError:
                if not verify_v1_quarantine(destination):
                    raise ValueError("concurrent quarantine capture failed verification") from None
        finally:
            shutil.rmtree(temporary_capture, ignore_errors=True)
    return QuarantineReceipt(
        capture_id=capture_id,
        path=destination,
        records_sha256=records_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
        seal=str(manifest["seal"]),
    )
