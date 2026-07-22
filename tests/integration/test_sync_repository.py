from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from corvus.application.sync import SyncService
from corvus.domain.account import Account, DeviceRegistration, ExperienceKind
from corvus.domain.identity import Principal, PrincipalKind, WorkspaceKind, WorkspaceMembership
from corvus.domain.sync import SyncConflictError, SyncMutation, SyncProtocolError
from corvus.infrastructure.db import M1_CURRENT_REVISION, upgrade_database, upgrade_database_url
from corvus.infrastructure.repositories.accounts import AccountRepository
from corvus.infrastructure.repositories.identity_scope import IdentityScopeRepository
from corvus.infrastructure.repositories.platform_identity import (
    PlatformIdentityRepository,
    PlatformIdentityRepositoryError,
)
from corvus.infrastructure.repositories.sync import SyncRepository
from corvus.platform import create_platform_engine
from corvus.store import TraceStore
from tests.postgres_safety import PostgresTestSafetyError, validate_disposable_postgres_url

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_SYNC_COUNT_STATEMENTS = {
    "account_onboarding_versions": "SELECT COUNT(*) FROM account_onboarding_versions",
    "workspace_changes": "SELECT COUNT(*) FROM workspace_changes",
    "outbox_events": "SELECT COUNT(*) FROM outbox_events",
    "device_sync_acknowledgements": "SELECT COUNT(*) FROM device_sync_acknowledgements",
    "platform_idempotency": "SELECT COUNT(*) FROM platform_idempotency",
}
_SYNC_TRANSPLANT_STATEMENTS = {
    "platform_idempotency": (
        "INSERT INTO platform_idempotency "
        "(account_id, principal_id, scope_key, workspace_id, workspace_version, "
        "membership_version, device_id, device_version, operation, idempotency_key, "
        "request_digest, result_json, created_at) VALUES (:account_id, :principal_id, "
        ":scope_key, :workspace_id, :workspace_version, :membership_version, :device_id, "
        ":device_version, 'account_profile.set_experience', 'transplant', :digest, '{}', "
        ":created_at)"
    ),
    "workspace_changes": (
        "INSERT INTO workspace_changes "
        "(workspace_id, workspace_version, sequence, previous_digest, change_digest, kind, "
        "operation, entity_id, entity_version, payload_json, account_id, principal_id, "
        "membership_version, device_id, device_version, created_at) VALUES (:workspace_id, "
        ":workspace_version, 1, :genesis, :digest, 'account_profile', 'set_experience', "
        ":account_id, 2, '{}', :account_id, :principal_id, :membership_version, "
        ":device_id, :device_version, :created_at)"
    ),
    "device_sync_acknowledgements": (
        "INSERT INTO device_sync_acknowledgements "
        "(workspace_id, workspace_version, device_id, version, account_id, principal_id, "
        "membership_version, device_version, acknowledged_sequence, created_at) VALUES "
        "(:workspace_id, :workspace_version, :device_id, 1, :account_id, :principal_id, "
        ":membership_version, :device_version, 1, :created_at)"
    ),
}


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "corvus.db"
    TraceStore(database).engine.dispose()
    assert upgrade_database(database) == M1_CURRENT_REVISION
    return database


def _sync_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(statement).fetchone()[0]
        for table, statement in _SYNC_COUNT_STATEMENTS.items()
    }


def _identity(
    database: Path | Engine,
    *,
    suffix: str,
) -> tuple[Account, Principal, DeviceRegistration, object]:
    accounts = AccountRepository(database)
    principal = Principal(
        kind=PrincipalKind.USER,
        external_provider="corvus-account",
        external_subject=f"sync-{suffix}",
        display_name="Sync user",
        created_at=_NOW,
    )
    account = Account(
        principal_id=principal.id,
        normalized_email=f"sync-{suffix}@example.com",
        experience_kind=ExperienceKind.EVERYDAY,
        created_at=_NOW,
        updated_at=_NOW,
    )
    accounts.create_preprovisioned_account(principal=principal, account=account)
    device = DeviceRegistration(
        account_id=account.id,
        name=f"Device {suffix}",
        public_key_digest="a" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    accounts.append_device(device)
    platform = PlatformIdentityRepository(accounts.engine)
    workspace, _ = platform.create_workspace(
        account_id=account.id,
        principal_id=principal.id,
        name=f"Workspace {suffix}",
        workspace_kind=WorkspaceKind.INDIVIDUAL,
        idempotency_key=f"workspace-{suffix}",
        now=_NOW,
    )
    accounts.close()
    return account, principal, device, workspace


def _guarded_postgres_engine() -> Engine:
    database_url = os.environ.get(
        "CORVUS_TEST_POSTGRES_URL",
        "postgresql+psycopg://corvus:corvus@127.0.0.1:55432/corvus_platform_test?connect_timeout=2",
    )
    try:
        validate_disposable_postgres_url(database_url, environ=os.environ)
    except PostgresTestSafetyError as exc:
        pytest.skip(f"PostgreSQL destructive test disabled: {exc}")
    engine = create_platform_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        if getattr(exc.orig, "sqlstate", None) is not None:
            raise
        pytest.skip(f"PostgreSQL test service unavailable: {exc.__class__.__name__}")
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    upgrade_database_url(database_url)
    return engine


def _account_mutation(account: Account, *, key: str, version: int = 1) -> SyncMutation:
    return SyncMutation.model_validate(
        {
            "idempotency_key": key,
            "kind": "account_profile",
            "operation": "set_experience",
            "entity_id": str(account.id),
            "expected_version": version,
            "payload": {"experience_kind": "developer"},
        }
    )


def _additional_workspace(
    database: Path | Engine,
    *,
    account: Account,
    principal: Principal,
    key: str,
) -> object:
    accounts = AccountRepository(database)
    workspace, _ = PlatformIdentityRepository(accounts.engine).create_workspace(
        account_id=account.id,
        principal_id=principal.id,
        name=f"Additional {key}",
        workspace_kind=WorkspaceKind.INDIVIDUAL,
        idempotency_key=key,
        now=_NOW,
    )
    accounts.close()
    return workspace


def _workspace_mutation(workspace: object, *, key: str, version: int = 1) -> SyncMutation:
    return SyncMutation.model_validate(
        {
            "idempotency_key": key,
            "kind": "workspace_profile",
            "operation": "update",
            "entity_id": str(workspace.id),
            "expected_version": version,
            "payload": {"name": "Renamed", "workspace_kind": "team"},
        }
    )


def _apply(
    service: SyncService,
    *,
    account: Account,
    principal: Principal,
    device: DeviceRegistration,
    workspace: object,
    mutations: tuple[SyncMutation, ...],
    acknowledged_cursor: int = 0,
    now: datetime = _NOW,
):
    return service.apply(
        workspace_id=workspace.id,
        account_id=account.id,
        principal_id=principal.id,
        device_id=device.id,
        device_version=device.version,
        acknowledged_cursor=acknowledged_cursor,
        mutations=mutations,
        now=now,
    )


def test_atomic_batch_appends_ordered_changes_outbox_and_versioned_ack(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="a")
    service = SyncService(SyncRepository(database))

    applied = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(
            _account_mutation(account, key="account-1"),
            _workspace_mutation(workspace, key="workspace-1"),
        ),
    )
    page = service.page(
        workspace_id=workspace.id,
        account_id=account.id,
        principal_id=principal.id,
        device_id=device.id,
        device_version=device.version,
        cursor=0,
        limit=1,
    )
    second = service.page(
        workspace_id=workspace.id,
        account_id=account.id,
        principal_id=principal.id,
        device_id=device.id,
        device_version=device.version,
        cursor=page.next_cursor,
        limit=100,
    )
    acknowledged = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(),
        acknowledged_cursor=second.next_cursor,
        now=_NOW + timedelta(seconds=1),
    )

    assert [result.sequence for result in applied.results] == [1, 2]
    assert page.high_watermark == second.high_watermark == 2
    assert page.next_cursor == 1 and page.has_more is True
    assert second.next_cursor == 2 and second.has_more is False
    assert [change.sequence for change in (*page.changes, *second.changes)] == [1, 2]
    assert {change.membership_version for change in (*page.changes, *second.changes)} == {1}
    assert acknowledged.acknowledged_cursor == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM device_sync_acknowledgements"
        ).fetchone() == (1,)


def test_empty_zero_ack_apply_is_side_effect_free(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="z")
    service = SyncService(SyncRepository(database))

    result = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(),
        acknowledged_cursor=0,
    )

    assert result.results == () and result.acknowledged_cursor == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_sync_heads").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM device_sync_acknowledgements"
        ).fetchone() == (0,)


def test_idempotent_replay_returns_original_results_without_new_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="b")
    service = SyncService(SyncRepository(database))
    mutation = _account_mutation(account, key="stable-replay")

    first = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(mutation,),
    )
    repeated = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(mutation,),
        now=_NOW + timedelta(minutes=1),
    )

    assert repeated.results == first.results
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_changes").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM platform_idempotency").fetchone() == (2,)


def test_replay_key_is_stably_rejected_after_membership_version_changes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="membership-replay")
    service = SyncService(SyncRepository(database))
    mutation = _account_mutation(account, key="membership-bound")
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(mutation,),
    )
    IdentityScopeRepository(database).append_membership(
        WorkspaceMembership(
            workspace_id=workspace.id,
            principal_id=principal.id,
            role="owner",
            version=2,
            created_at=_NOW,
            updated_at=_NOW + timedelta(seconds=1),
        )
    )

    with pytest.raises(SyncProtocolError, match="idempotency_payload_mismatch"):
        _apply(
            service,
            account=account,
            principal=principal,
            device=device,
            workspace=workspace,
            mutations=(mutation,),
            now=_NOW + timedelta(seconds=2),
        )


def test_same_key_different_request_and_version_conflict_abort_whole_batch(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="c")
    service = SyncService(SyncRepository(database))
    original = _account_mutation(account, key="collision")
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(original,),
    )
    changed = original.model_copy(update={"expected_version": 2})
    with pytest.raises(SyncProtocolError, match="idempotency_payload_mismatch"):
        _apply(
            service,
            account=account,
            principal=principal,
            device=device,
            workspace=workspace,
            mutations=(changed,),
        )

    with pytest.raises(SyncConflictError) as raised:
        _apply(
            service,
            account=account,
            principal=principal,
            device=device,
            workspace=workspace,
            mutations=(
                _workspace_mutation(workspace, key="would-write"),
                _account_mutation(account, key="stale-account", version=1),
            ),
        )
    assert raised.value.detail.mutation_index == 1
    assert raised.value.detail.current_version == 2
    assert "normalized_email" not in raised.value.detail.current_profile
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_changes").fetchone() == (1,)


def test_tenant_device_membership_ack_and_resync_boundaries_fail_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account_a, principal_a, device_a, workspace_a = _identity(database, suffix="d")
    account_b, principal_b, device_b, workspace_b = _identity(database, suffix="e")
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account_a,
        principal=principal_a,
        device=device_a,
        workspace=workspace_a,
        mutations=(_account_mutation(account_a, key="tenant-a"),),
    )

    for bad in (
        dict(account=account_b, principal=principal_b, device=device_b, workspace=workspace_a),
        dict(account=account_a, principal=principal_a, device=device_b, workspace=workspace_a),
    ):
        with pytest.raises(SyncProtocolError, match="workspace_not_found|device_not_found"):
            _apply(service, mutations=(), **bad)

    with pytest.raises(SyncProtocolError, match="acknowledgement_ahead"):
        _apply(
            service,
            account=account_a,
            principal=principal_a,
            device=device_a,
            workspace=workspace_a,
            mutations=(),
            acknowledged_cursor=2,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workspace_sync_heads SET retention_floor = 1 WHERE workspace_id = ?",
            (str(workspace_a.id),),
        )
    with pytest.raises(SyncProtocolError) as resync:
        service.page(
            workspace_id=workspace_a.id,
            account_id=account_a.id,
            principal_id=principal_a.id,
            device_id=device_a.id,
            device_version=device_a.version,
            cursor=0,
            limit=100,
        )
    assert resync.value.code == "sync_resync_required"
    assert resync.value.detail["resume_cursor"] == 1
    assert resync.value.detail["resources"] == [
        "/api/v2/session",
        f"/api/v2/workspaces/{workspace_a.id}",
    ]
    assert workspace_b.id != workspace_a.id


def test_database_constraints_reject_tenant_transplant_and_noncanonical_scope(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="h")
    _other_account, _other_principal, _other_device, other_workspace = _identity(
        database, suffix="i"
    )
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="tenant-fk"),),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            "SELECT change_digest, payload_json, created_at FROM outbox_events "
            "WHERE workspace_id = ? AND sequence = 1",
            (str(workspace.id),),
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO outbox_events "
                "(workspace_id, sequence, change_digest, event_kind, payload_json, created_at) "
                "VALUES (?, 1, ?, 'workspace.change', ?, ?)",
                (str(other_workspace.id), row[0], row[1], row[2]),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO platform_idempotency "
                "(account_id, scope_key, workspace_id, workspace_version, device_id, "
                "device_version, operation, idempotency_key, request_digest, result_json, "
                "created_at) VALUES (?, 'forged-scope', ?, ?, ?, ?, 'test', 'forged', ?, "
                "'{}', ?)",
                (
                    str(account.id),
                    str(workspace.id),
                    workspace.version,
                    str(device.id),
                    device.version,
                    "a" * 64,
                    _NOW.isoformat(),
                ),
            )


def test_cross_tenant_denial_precedes_workspace_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    account_a, principal_a, device_a, workspace_a = _identity(database, suffix="preauth-a")
    account_b, principal_b, device_b, _workspace_b = _identity(database, suffix="preauth-b")
    service = SyncService(SyncRepository(database))

    def forbidden_lock(_connection: object, _workspace_id: object) -> None:
        raise AssertionError("cross-tenant request reached workspace lock")

    monkeypatch.setattr(SyncRepository, "_lock_workspace_profile", forbidden_lock)
    with pytest.raises(SyncProtocolError, match="workspace_not_found"):
        _apply(
            service,
            account=account_b,
            principal=principal_b,
            device=device_b,
            workspace=workspace_a,
            mutations=(),
        )
    assert account_a.id != account_b.id
    assert principal_a.id != principal_b.id
    assert device_a.id != device_b.id


def test_concurrent_workspace_writers_allocate_gap_free_sequences(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="f")
    services = (SyncService(SyncRepository(database)), SyncService(SyncRepository(database)))

    def write(index: int) -> int:
        service = services[index - 1]
        mutation = SyncMutation.model_validate(
            {
                "idempotency_key": f"concurrent-{index}",
                "kind": "workspace_profile",
                "operation": "update",
                "entity_id": str(workspace.id),
                "expected_version": index,
                "payload": {"name": f"Workspace {index + 1}"},
            }
        )
        return (
            _apply(
                service,
                account=account,
                principal=principal,
                device=device,
                workspace=workspace,
                mutations=(mutation,),
                now=_NOW + timedelta(seconds=index),
            )
            .results[0]
            .sequence
        )

    # Expected versions intentionally serialize in order through a small retry at the caller.
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, 1)
        second = executor.submit(write, 2)
        results = []
        for future in (first, second):
            try:
                results.append(future.result(timeout=20))
            except SyncConflictError:
                pass
    if len(results) == 1:
        current_version = 2
        results.append(write(current_version))

    with sqlite3.connect(database) as connection:
        sequences = [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM workspace_changes WHERE workspace_id = ? ORDER BY sequence",
                (str(workspace.id),),
            )
        ]
    assert sequences == [1, 2]
    assert sorted(results) == [1, 2]


def test_two_workspace_account_writers_serialize_shared_profile_version(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, first_workspace = _identity(database, suffix="shared-account")
    second_workspace = _additional_workspace(
        database,
        account=account,
        principal=principal,
        key="shared-account-second",
    )
    services = (SyncService(SyncRepository(database)), SyncService(SyncRepository(database)))

    def write(index: int, workspace: object) -> object:
        try:
            return _apply(
                services[index],
                account=account,
                principal=principal,
                device=device,
                workspace=workspace,
                mutations=(_account_mutation(account, key=f"shared-account-{index}", version=1),),
            )
        except Exception as exc:  # Symmetric race result capture.
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(write, 0, first_workspace),
            executor.submit(write, 1, second_workspace),
        )
        outcomes = [future.result(timeout=20) for future in futures]

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(conflicts) == 1 and isinstance(conflicts[0], SyncConflictError)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_changes").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (1,)


def test_sync_and_existing_workspace_patch_return_one_stable_conflict(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="cross-path")
    sync = SyncService(SyncRepository(database))
    accounts = AccountRepository(database)
    platform = PlatformIdentityRepository(accounts.engine)

    def sync_write() -> object:
        try:
            return _apply(
                sync,
                account=account,
                principal=principal,
                device=device,
                workspace=workspace,
                mutations=(_workspace_mutation(workspace, key="cross-path-sync"),),
            )
        except Exception as exc:
            return exc

    def patch_write() -> object:
        try:
            return platform.update_workspace(
                principal_id=principal.id,
                workspace_id=workspace.id,
                name="Existing PATCH winner",
                expected_version=1,
                now=_NOW,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=20)
            for future in (executor.submit(sync_write), executor.submit(patch_write))
        ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]

    assert len(failures) == 1
    assert isinstance(failures[0], (SyncConflictError, PlatformIdentityRepositoryError))
    assert "workspace_version_conflict" in str(failures[0]) or isinstance(
        failures[0], SyncConflictError
    )
    accounts.close()


def test_read_detects_payload_column_or_hash_chain_tampering(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="g")
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="tamper"),),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER workspace_changes_no_update")
        connection.execute(
            "UPDATE workspace_changes SET payload_json = '{}' WHERE workspace_id = ?",
            (str(workspace.id),),
        )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=0,
            limit=100,
        )


@pytest.mark.parametrize(
    "head_update",
    [
        "current_sequence = 1, chain_digest = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "current_sequence = 2, chain_digest = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
    ],
)
def test_read_detects_head_tail_digest_or_sequence_tampering(
    tmp_path: Path,
    head_update: str,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix=f"head-{uuid4().hex}")
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="head-tamper"),),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE workspace_sync_heads SET {head_update} WHERE workspace_id = ?",  # noqa: S608
            (str(workspace.id),),
        )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=0,
            limit=100,
        )


def test_read_detects_non_genesis_digest_at_zero_sequence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="zero-head")
    service = SyncService(SyncRepository(database))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workspace_sync_heads "
            "(workspace_id, workspace_version, current_sequence, retention_floor, chain_digest, "
            "version, updated_at) VALUES (?, ?, 0, 0, ?, 1, ?)",
            (str(workspace.id), workspace.version, "a" * 64, _NOW.isoformat()),
        )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=0,
            limit=100,
        )


@pytest.mark.parametrize("apply_kind", ["mutation", "ack_only", "replay_only"])
def test_apply_validates_existing_tail_before_any_write_or_replay(
    tmp_path: Path,
    apply_kind: str,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix=f"apply-tail-{apply_kind}")
    service = SyncService(SyncRepository(database))
    original = _account_mutation(account, key="original")
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(original,),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workspace_sync_heads SET current_sequence = 2, chain_digest = ? "
            "WHERE workspace_id = ?",
            ("b" * 64, str(workspace.id)),
        )
        before = _sync_counts(connection)

    mutations = {
        "mutation": (_account_mutation(account, key="new", version=2),),
        "ack_only": (),
        "replay_only": (original,),
    }[apply_kind]
    acknowledged_cursor = 1 if apply_kind == "ack_only" else 0
    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        _apply(
            service,
            account=account,
            principal=principal,
            device=device,
            workspace=workspace,
            mutations=mutations,
            acknowledged_cursor=acknowledged_cursor,
            now=_NOW + timedelta(seconds=1),
        )

    with sqlite3.connect(database) as connection:
        after = _sync_counts(connection)
        profile_version = connection.execute(
            "SELECT MAX(version) FROM account_onboarding_versions WHERE account_id = ?",
            (str(account.id),),
        ).fetchone()[0]
    assert after == before
    assert profile_version == 2


@pytest.mark.parametrize("operation", ["page", "ack_only", "replay", "new_mutation"])
def test_missing_head_with_workspace_history_fails_closed_before_any_write(
    tmp_path: Path,
    operation: str,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix=f"missing-head-{operation}")
    service = SyncService(SyncRepository(database))
    original = _account_mutation(account, key="orphaned-original")
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(original,),
    )
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(),
        acknowledged_cursor=1,
        now=_NOW + timedelta(seconds=1),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER workspace_sync_heads_no_delete")
        connection.execute(
            "DELETE FROM workspace_sync_heads WHERE workspace_id = ?",
            (str(workspace.id),),
        )
        before = _sync_counts(connection)

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        if operation == "page":
            service.page(
                workspace_id=workspace.id,
                account_id=account.id,
                principal_id=principal.id,
                device_id=device.id,
                device_version=device.version,
                cursor=0,
                limit=100,
            )
        else:
            mutations = {
                "ack_only": (),
                "replay": (original,),
                "new_mutation": (_account_mutation(account, key="orphaned-new", version=2),),
            }[operation]
            _apply(
                service,
                account=account,
                principal=principal,
                device=device,
                workspace=workspace,
                mutations=mutations,
                acknowledged_cursor=1 if operation == "ack_only" else 0,
                now=_NOW + timedelta(seconds=2),
            )

    with sqlite3.connect(database) as connection:
        assert _sync_counts(connection) == before
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_sync_heads WHERE workspace_id = ?",
            (str(workspace.id),),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT MAX(version) FROM account_onboarding_versions WHERE account_id = ?",
            (str(account.id),),
        ).fetchone() == (2,)


def test_missing_head_without_workspace_history_allows_genesis(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="true-genesis")
    service = SyncService(SyncRepository(database))

    empty_page = service.page(
        workspace_id=workspace.id,
        account_id=account.id,
        principal_id=principal.id,
        device_id=device.id,
        device_version=device.version,
        cursor=0,
        limit=100,
    )
    applied = _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="genesis"),),
    )

    assert empty_page.high_watermark == 0 and empty_page.changes == ()
    assert [result.sequence for result in applied.results] == [1]


def _rewrite_tail_change(
    database: Path,
    *,
    workspace_id: object,
    payload_json: str | None = None,
    kind: str | None = None,
    operation: str | None = None,
    entity_id: object | None = None,
    entity_version: int | None = None,
    workspace_version: int | None = None,
    created_at: str | None = None,
    previous_digest: str | None = None,
    recompute_digest: bool = False,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DROP TRIGGER workspace_changes_no_update")
        row = connection.execute(
            "SELECT * FROM workspace_changes WHERE workspace_id = ? ORDER BY sequence DESC LIMIT 1",
            (str(workspace_id),),
        ).fetchone()
        assert row is not None
        updated = dict(row)
        updated["payload_json"] = payload_json or str(row["payload_json"])
        updated["kind"] = kind or str(row["kind"])
        updated["operation"] = operation or str(row["operation"])
        updated["entity_id"] = str(entity_id or row["entity_id"])
        updated["entity_version"] = entity_version or int(row["entity_version"])
        updated["workspace_version"] = workspace_version or int(row["workspace_version"])
        updated["created_at"] = created_at or str(row["created_at"])
        updated["previous_digest"] = previous_digest or str(row["previous_digest"])
        digest = str(row["change_digest"])
        if recompute_digest:
            body = {
                "workspace_id": UUID(str(row["workspace_id"])),
                "workspace_version": updated["workspace_version"],
                "sequence": int(row["sequence"]),
                "previous_digest": updated["previous_digest"],
                "kind": updated["kind"],
                "operation": updated["operation"],
                "entity_id": UUID(updated["entity_id"]),
                "entity_version": updated["entity_version"],
                "payload": json.loads(updated["payload_json"]),
                "account_id": UUID(str(row["account_id"])),
                "principal_id": UUID(str(row["principal_id"])),
                "device_id": UUID(str(row["device_id"])),
                "device_version": int(row["device_version"]),
                "created_at": datetime.fromisoformat(updated["created_at"]),
            }
            if "membership_version" in row.keys():
                body["membership_version"] = int(row["membership_version"])
            digest = SyncRepository._change_digest(body)
        connection.execute(
            "UPDATE workspace_changes SET workspace_version = ?, payload_json = ?, kind = ?, "
            "operation = ?, entity_id = ?, entity_version = ?, created_at = ?, "
            "previous_digest = ?, change_digest = ? "
            "WHERE workspace_id = ? AND sequence = ?",
            (
                updated["workspace_version"],
                updated["payload_json"],
                updated["kind"],
                updated["operation"],
                updated["entity_id"],
                updated["entity_version"],
                updated["created_at"],
                updated["previous_digest"],
                digest,
                str(workspace_id),
                int(row["sequence"]),
            ),
        )
        connection.execute(
            "UPDATE workspace_sync_heads SET chain_digest = ? WHERE workspace_id = ?",
            (digest, str(workspace_id)),
        )


@pytest.mark.parametrize(
    ("tamper", "cursor"),
    [
        ("noncanonical", 0),
        ("duplicate", 0),
        ("nonfinite", 1),
        ("naive_time", 1),
        ("secret", 0),
        ("extra_profile_field", 0),
        ("kind_profile_mismatch", 0),
    ],
)
def test_read_rejects_noncanonical_untyped_or_sensitive_tail_rows(
    tmp_path: Path,
    tamper: str,
    cursor: int,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix=f"read-{tamper}")
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="tail"),),
    )
    canonical = f'{{"entity_id":"{account.id}","experience_kind":"developer","version":2}}'
    if tamper == "noncanonical":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            payload_json=f'{{ "version": 2, "experience_kind": "developer", "entity_id": "{account.id}" }}',
        )
    elif tamper == "duplicate":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            payload_json=canonical[:-1] + ',"version":2}',
        )
    elif tamper == "nonfinite":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            payload_json=canonical[:-1] + ',"score":NaN}',
        )
    elif tamper == "naive_time":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            created_at="2026-07-16T12:00:00",
        )
    elif tamper == "secret":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            payload_json=canonical[:-1] + ',"refresh_token":"sync-tail-canary"}',
            recompute_digest=True,
        )
    elif tamper == "extra_profile_field":
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            payload_json=canonical[:-1] + ',"nickname":"unexpected"}',
            recompute_digest=True,
        )
    else:
        _rewrite_tail_change(
            database,
            workspace_id=workspace.id,
            kind="workspace_profile",
            operation="update",
            recompute_digest=True,
        )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=cursor,
            limit=100,
        )


def test_empty_page_validates_tail_immediate_previous_link(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="previous-link")
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(
            _account_mutation(account, key="first"),
            _workspace_mutation(workspace, key="second"),
        ),
    )
    _rewrite_tail_change(
        database,
        workspace_id=workspace.id,
        previous_digest="c" * 64,
        recompute_digest=True,
    )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=2,
            limit=100,
        )


def test_empty_page_rejects_recomputed_account_entity_substitution(tmp_path: Path) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(database, suffix="account-provenance")
    other_account, _other_principal, _other_device, _other_workspace = _identity(
        database, suffix="account-provenance-other"
    )
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_account_mutation(account, key="account-provenance"),),
    )
    substituted_payload = json.dumps(
        {
            "entity_id": str(other_account.id),
            "experience_kind": "developer",
            "version": 2,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    _rewrite_tail_change(
        database,
        workspace_id=workspace.id,
        entity_id=other_account.id,
        payload_json=substituted_payload,
        recompute_digest=True,
    )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=1,
            limit=100,
        )


@pytest.mark.parametrize("tamper", ["entity", "version"])
def test_empty_page_rejects_recomputed_workspace_entity_provenance_substitution(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = _database(tmp_path)
    account, principal, device, workspace = _identity(
        database, suffix=f"workspace-provenance-{tamper}"
    )
    other_workspace = _additional_workspace(
        database,
        account=account,
        principal=principal,
        key=f"workspace-provenance-other-{tamper}",
    )
    service = SyncService(SyncRepository(database))
    _apply(
        service,
        account=account,
        principal=principal,
        device=device,
        workspace=workspace,
        mutations=(_workspace_mutation(workspace, key="workspace-provenance"),),
    )
    substituted_entity_id = other_workspace.id if tamper == "entity" else workspace.id
    substituted_version = 3 if tamper == "version" else 2
    substituted_payload = json.dumps(
        {
            "entity_id": str(substituted_entity_id),
            "name": "Renamed",
            "status": "active",
            "version": substituted_version,
            "workspace_kind": "team",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    _rewrite_tail_change(
        database,
        workspace_id=workspace.id,
        entity_id=substituted_entity_id,
        entity_version=substituted_version,
        payload_json=substituted_payload,
        recompute_digest=True,
    )

    with pytest.raises(SyncProtocolError, match="sync_change_integrity_invalid"):
        service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=1,
            limit=100,
        )


@pytest.mark.parametrize(
    ("table_name", "principal_source"),
    [
        ("platform_idempotency", "foreign_membership"),
        ("platform_idempotency", "account_principal_mismatch"),
        ("workspace_changes", "foreign_membership"),
        ("workspace_changes", "account_principal_mismatch"),
        ("device_sync_acknowledgements", "foreign_membership"),
        ("device_sync_acknowledgements", "account_principal_mismatch"),
    ],
)
def test_sync_authority_rows_reject_workspace_and_account_principal_transplants(
    tmp_path: Path,
    table_name: str,
    principal_source: str,
) -> None:
    database = _database(tmp_path)
    account_a, principal_a, _device_a, workspace_a = _identity(database, suffix="binding-a")
    account_b, principal_b, device_b, _workspace_b = _identity(database, suffix="binding-b")
    principal_id = principal_b.id if principal_source == "foreign_membership" else principal_a.id
    common = {
        "workspace_id": str(workspace_a.id),
        "workspace_version": workspace_a.version,
        "account_id": str(account_b.id),
        "principal_id": str(principal_id),
        "membership_version": 1,
        "device_id": str(device_b.id),
        "device_version": device_b.version,
        "created_at": _NOW.isoformat(),
    }
    parameters = {
        **common,
        "scope_key": f"{workspace_a.id}:{device_b.id}",
        "digest": "d" * 64,
        "genesis": "0" * 64,
    }

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_SYNC_TRANSPLANT_STATEMENTS[table_name], parameters)


def test_guarded_postgres_sync_repository_contract() -> None:
    engine = _guarded_postgres_engine()
    try:
        account, principal, device, workspace = _identity(engine, suffix="p")
        service = SyncService(SyncRepository(engine))

        applied = _apply(
            service,
            account=account,
            principal=principal,
            device=device,
            workspace=workspace,
            mutations=(
                _account_mutation(account, key="postgres-account"),
                _workspace_mutation(workspace, key="postgres-workspace"),
            ),
        )
        page = service.page(
            workspace_id=workspace.id,
            account_id=account.id,
            principal_id=principal.id,
            device_id=device.id,
            device_version=device.version,
            cursor=0,
            limit=100,
        )

        assert [item.sequence for item in applied.results] == [1, 2]
        assert [item.sequence for item in page.changes] == [1, 2]
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM outbox_events")) == 2
    finally:
        engine.dispose()


def test_guarded_postgres_sync_authority_transplant_constraints() -> None:
    engine = _guarded_postgres_engine()
    try:
        _account_a, _principal_a, _device_a, workspace_a = _identity(
            engine, suffix="postgres-binding-a"
        )
        account_b, principal_b, device_b, _workspace_b = _identity(
            engine, suffix="postgres-binding-b"
        )
        parameters = {
            "workspace_id": str(workspace_a.id),
            "workspace_version": workspace_a.version,
            "account_id": str(account_b.id),
            "principal_id": str(principal_b.id),
            "membership_version": 1,
            "device_id": str(device_b.id),
            "device_version": device_b.version,
            "created_at": _NOW.isoformat(),
            "scope_key": f"{workspace_a.id}:{device_b.id}",
            "digest": "d" * 64,
            "genesis": "0" * 64,
        }
        with engine.begin() as connection:
            for statement in _SYNC_TRANSPLANT_STATEMENTS.values():
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(text(statement), parameters)
    finally:
        engine.dispose()


def test_guarded_postgres_cross_workspace_account_serialization() -> None:
    engine = _guarded_postgres_engine()
    try:
        account, principal, device, first_workspace = _identity(engine, suffix="q")
        second_workspace = _additional_workspace(
            engine,
            account=account,
            principal=principal,
            key="postgres-shared-account",
        )
        services = (SyncService(SyncRepository(engine)), SyncService(SyncRepository(engine)))

        def write(index: int, workspace: object) -> object:
            try:
                return _apply(
                    services[index],
                    account=account,
                    principal=principal,
                    device=device,
                    workspace=workspace,
                    mutations=(
                        _account_mutation(
                            account,
                            key=f"postgres-shared-{index}",
                            version=1,
                        ),
                    ),
                )
            except Exception as exc:  # Symmetric guarded-contract capture.
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(write, 0, first_workspace),
                executor.submit(write, 1, second_workspace),
            )
            outcomes = [future.result(timeout=20) for future in futures]

        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(failures) == 1 and isinstance(failures[0], SyncConflictError)
    finally:
        engine.dispose()
