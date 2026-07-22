import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from corvus.delivery import DeliveryError, DeliveryManager


def _manager(tmp_path: Path) -> DeliveryManager:
    return DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=Fernet.generate_key(),
    )


def _bundle(manager: DeliveryManager, destination: Path):
    return manager.package(
        uuid4(),
        destination,
        {"src/app.py": b"print('ok')\n"},
        {"passed": True},
        {"passed": True},
    )


def test_packaging_requires_passed_acceptance_and_tests(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    destination.mkdir()
    manager = _manager(tmp_path)

    with pytest.raises(DeliveryError, match="block packaging"):
        manager.package(
            uuid4(),
            destination,
            {"app.py": b"pass\n"},
            {"passed": False},
            {"passed": True},
        )


def test_approval_is_bound_to_exact_manifest_and_files(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    destination.mkdir()
    manager = _manager(tmp_path)
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")

    altered = approval.model_copy(update={"manifest_digest": "0" * 64})

    with pytest.raises(DeliveryError, match="exact bundle"):
        manager.apply(bundle, altered, actor_id="lucas")


def test_expired_approval_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    destination.mkdir()
    manager = _manager(tmp_path)
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas").model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )

    with pytest.raises(DeliveryError, match="expired"):
        manager.apply(bundle, approval, actor_id="lucas")


def test_approval_is_actor_bound_and_consumed_once_durably(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    destination.mkdir()
    manager = _manager(tmp_path)
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")

    with pytest.raises(DeliveryError, match="actor"):
        manager.apply(bundle, approval, actor_id="mallory")

    manager.apply(bundle, approval, actor_id="lucas")
    reloaded = _manager(tmp_path)
    with pytest.raises(DeliveryError, match="consumed"):
        reloaded.apply(bundle, approval, actor_id="lucas")


def test_artifact_tamper_blocks_before_mutation_or_approval_consumption(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")
    staged = tmp_path / "bundles" / str(bundle.id) / "files" / "src" / "app.py"
    original = staged.read_bytes()
    staged.write_bytes(b"print('tampered')\n")

    with pytest.raises(DeliveryError, match="artifact integrity"):
        manager.apply(bundle, approval, actor_id="lucas")

    assert target.read_text(encoding="utf-8") == "print('old')\n"
    staged.write_bytes(original)
    manager.apply(bundle, approval, actor_id="lucas")
    assert target.read_text(encoding="utf-8") == "print('ok')\n"


class _InjectedCrash(BaseException):
    pass


def test_crash_after_write_recovers_from_persisted_intent(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    backup_key = Fernet.generate_key()

    def inject(point: str) -> None:
        if point == "after_write:src/app.py":
            raise _InjectedCrash

    manager = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
        fault_injector=inject,
    )
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")

    with pytest.raises(_InjectedCrash):
        manager.apply(bundle, approval, actor_id="lucas")

    assert target.read_text(encoding="utf-8") == "print('ok')\n"
    recovered = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
    )
    recovered.recover(bundle)
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    journal = json.loads(
        (tmp_path / "backups" / str(bundle.id) / "journal.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "rolled_back"
    with pytest.raises(DeliveryError, match="consumed"):
        recovered.apply(bundle, approval, actor_id="lucas")


@pytest.mark.parametrize(
    "point", ("after_intent_persisted", "after_intent", "before_write:src/app.py")
)
def test_crash_before_file_write_recovers_without_mutation(tmp_path: Path, point: str) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    backup_key = Fernet.generate_key()

    def inject(actual: str) -> None:
        if actual == point:
            raise _InjectedCrash

    manager = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
        fault_injector=inject,
    )
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")

    with pytest.raises(_InjectedCrash):
        manager.apply(bundle, approval, actor_id="lucas")

    assert target.read_text(encoding="utf-8") == "print('old')\n"
    recovered = DeliveryManager(tmp_path / "bundles", tmp_path / "backups", backup_key=backup_key)
    recovered.recover(bundle)
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    with pytest.raises(DeliveryError, match="consumed"):
        recovered.apply(bundle, approval, actor_id="lucas")


def test_crash_after_applied_receipt_recovers_as_completed(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    backup_key = Fernet.generate_key()

    def inject(point: str) -> None:
        if point == "after_applied":
            raise _InjectedCrash

    manager = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
        fault_injector=inject,
    )
    bundle = _bundle(manager, destination)
    approval = manager.approve(bundle, actor_id="lucas")

    with pytest.raises(_InjectedCrash):
        manager.apply(bundle, approval, actor_id="lucas")

    recovered = DeliveryManager(tmp_path / "bundles", tmp_path / "backups", backup_key=backup_key)
    recovered.recover(bundle)
    assert target.read_text(encoding="utf-8") == "print('ok')\n"
    journal = json.loads(
        (tmp_path / "backups" / str(bundle.id) / "journal.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "applied"
    assert isinstance(journal["apply_receipt_digest"], str)


def test_concurrent_apply_is_rejected_by_bundle_and_destination_locks(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    backup_key = Fernet.generate_key()
    entered = threading.Event()
    release = threading.Event()
    thread_errors: list[BaseException] = []

    def pause(point: str) -> None:
        if point == "before_write:src/app.py":
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test lock wait timed out")

    first = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
        fault_injector=pause,
    )
    bundle = _bundle(first, destination)
    first_approval = first.approve(bundle, actor_id="lucas")
    second_approval = first.approve(bundle, actor_id="lucas")
    second = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
    )

    def apply_first() -> None:
        try:
            first.apply(bundle, first_approval, actor_id="lucas")
        except BaseException as exc:  # pragma: no cover - asserted after join
            thread_errors.append(exc)

    thread = threading.Thread(target=apply_first)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(DeliveryError, match="lock is busy"):
            second.apply(bundle, second_approval, actor_id="lucas")
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert thread_errors == []
    assert target.read_text(encoding="utf-8") == "print('ok')\n"


def test_undo_requires_actor_bound_receipt_bound_one_time_approval(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bundle = _bundle(manager, destination)
    apply_approval = manager.approve(bundle, actor_id="lucas")
    manager.apply(bundle, apply_approval, actor_id="lucas")
    undo_approval = manager.approve_undo(bundle, actor_id="lucas")

    with pytest.raises(DeliveryError, match="actor"):
        manager.undo(bundle, undo_approval, actor_id="mallory")

    manager.undo(bundle, undo_approval, actor_id="lucas")
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    with pytest.raises(DeliveryError, match="consumed"):
        manager.undo(bundle, undo_approval, actor_id="lucas")


@pytest.mark.parametrize("point", ("after_undo_intent_persisted", "after_undo_intent"))
def test_crash_after_undo_intent_recovers_original_files(tmp_path: Path, point: str) -> None:
    destination = tmp_path / "project"
    target = destination / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('old')\n", encoding="utf-8")
    backup_key = Fernet.generate_key()
    manager = DeliveryManager(tmp_path / "bundles", tmp_path / "backups", backup_key=backup_key)
    bundle = _bundle(manager, destination)
    apply_approval = manager.approve(bundle, actor_id="lucas")
    manager.apply(bundle, apply_approval, actor_id="lucas")
    undo_approval = manager.approve_undo(bundle, actor_id="lucas")

    def inject(current: str) -> None:
        if current == point:
            raise _InjectedCrash

    crashing = DeliveryManager(
        tmp_path / "bundles",
        tmp_path / "backups",
        backup_key=backup_key,
        fault_injector=inject,
    )
    with pytest.raises(_InjectedCrash):
        crashing.undo(bundle, undo_approval, actor_id="lucas")

    assert target.read_text(encoding="utf-8") == "print('ok')\n"
    recovered = DeliveryManager(tmp_path / "bundles", tmp_path / "backups", backup_key=backup_key)
    recovered.recover(bundle)
    assert target.read_text(encoding="utf-8") == "print('old')\n"
    with pytest.raises(DeliveryError, match="consumed"):
        recovered.undo(bundle, undo_approval, actor_id="lucas")
