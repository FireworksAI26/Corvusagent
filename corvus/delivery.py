from __future__ import annotations

import importlib
import json
import os
import secrets
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from difflib import unified_diff
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken

from corvus.models import ApprovalGrant, Artifact, DeliveryBundle
from corvus.safe_process import TrustedProcessError, run_trusted_argv
from corvus.security import SecurityError, atomic_write, resolve_under, sha256_bytes, sha256_file


class DeliveryError(RuntimeError):
    pass


class _AdvisoryFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise DeliveryError(f"delivery lock is busy: {self.path.name}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


class DeliveryManager:
    def __init__(
        self,
        bundle_root: Path,
        backup_root: Path,
        backup_key: bytes | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.bundle_root = bundle_root
        self.backup_root = backup_root
        bundle_root.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(backup_key) if backup_key is not None else None
        self._fault_injector = fault_injector

    def _cipher(self) -> Fernet:
        if self._fernet is None:
            raise DeliveryError("an OS-keyring-backed backup key is required for delivery")
        return self._fernet

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @staticmethod
    def _durable_write(path: Path, value: object) -> None:
        atomic_write(path, _canonical_json(value))
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    @contextmanager
    def _delivery_locks(self, bundle: DeliveryBundle) -> Iterator[None]:
        destination = str(bundle.destination.resolve(strict=True))
        if os.name == "nt":
            destination = destination.casefold()
        destination_key = sha256_bytes(destination.encode("utf-8"))
        lock_root = self.backup_root / ".locks"
        paths = sorted(
            (
                lock_root / f"bundle-{bundle.id}.lock",
                lock_root / f"destination-{destination_key}.lock",
            ),
            key=str,
        )
        locks = [_AdvisoryFileLock(path) for path in paths]
        acquired: list[_AdvisoryFileLock] = []
        try:
            for lock in locks:
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def _approval_path(self, bundle: DeliveryBundle, approval: ApprovalGrant) -> Path:
        return self.bundle_root / str(bundle.id) / "approvals" / f"{approval.id}.json"

    @staticmethod
    def _sealed_record(body: dict[str, object]) -> dict[str, object]:
        sealed = dict(body)
        sealed["record_digest"] = sha256_bytes(_canonical_json(body))
        return sealed

    @staticmethod
    def _read_sealed_record(path: Path) -> dict[str, object]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeliveryError("durable approval record is missing or invalid") from exc
        if not isinstance(record, dict):
            raise DeliveryError("durable approval record is invalid")
        digest = record.pop("record_digest", None)
        if not isinstance(digest, str) or sha256_bytes(_canonical_json(record)) != digest:
            raise DeliveryError("durable approval record integrity check failed")
        return record

    def _available_approval_record(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
        operation: str,
    ) -> tuple[Path, dict[str, object]]:
        path = self._approval_path(bundle, approval)
        record = self._read_sealed_record(path)
        if record.get("grant") != approval.model_dump(mode="json"):
            raise DeliveryError("approval is not bound to this exact durable grant")
        if record.get("actor_id") != actor_id:
            raise DeliveryError("approval actor mismatch")
        if record.get("operation") != operation:
            raise DeliveryError("approval operation mismatch")
        if record.get("state") != "approved":
            raise DeliveryError("approval has already been consumed")
        return path, record

    def _consume_approval(
        self,
        bundle: DeliveryBundle,
        approval_id: str,
        *,
        operation: str,
    ) -> bool:
        path = self.bundle_root / str(bundle.id) / "approvals" / f"{approval_id}.json"
        try:
            record = self._read_sealed_record(path)
        except DeliveryError:
            return False
        if record.get("operation") != operation or record.get("state") != "approved":
            return False
        record["state"] = "consumed"
        record["consumed_at"] = datetime.now(UTC).isoformat()
        self._durable_write(path, self._sealed_record(record))
        return True

    def _claim_approval(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
        operation: str,
    ) -> None:
        path, record = self._available_approval_record(
            bundle,
            approval,
            actor_id=actor_id,
            operation=operation,
        )
        record["state"] = "consumed"
        record["consumed_at"] = datetime.now(UTC).isoformat()
        self._durable_write(path, self._sealed_record(record))

    def _verified_bundle_payloads(self, bundle: DeliveryBundle) -> dict[str, bytes]:
        bundle_prefix = str(bundle.id)
        try:
            manifest_path = resolve_under(
                self.bundle_root,
                f"{bundle_prefix}/bundle.json",
                allow_missing_leaf=False,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, SecurityError, ValueError) as exc:
            raise DeliveryError("bundle manifest is missing or invalid") from exc
        if not isinstance(manifest, dict):
            raise DeliveryError("bundle manifest is invalid")
        manifest_digest = manifest.pop("manifest_digest", None)
        expected_manifest = {
            "schema_version": 1,
            "id": str(bundle.id),
            "run_id": str(bundle.run_id),
            "destination": str(bundle.destination),
            "changed_files": bundle.changed_files,
            "baseline_hashes": bundle.baseline_hashes,
            "artifacts": [item.model_dump(mode="json") for item in bundle.artifacts],
        }
        if (
            manifest_digest != bundle.manifest_digest
            or manifest != expected_manifest
            or sha256_bytes(_canonical_json(manifest)) != manifest_digest
        ):
            raise DeliveryError("bundle manifest integrity check failed")

        artifact_payloads: dict[str, bytes] = {}
        expected_sums: list[str] = []
        for artifact in bundle.artifacts:
            try:
                artifact_path = resolve_under(
                    self.bundle_root,
                    f"{bundle_prefix}/{artifact.relative_path}",
                    allow_missing_leaf=False,
                )
                if not artifact_path.is_file():
                    raise DeliveryError(
                        f"bundle artifact is not a regular file: {artifact.relative_path}"
                    )
                data = artifact_path.read_bytes()
            except (OSError, SecurityError) as exc:
                raise DeliveryError(
                    f"bundle artifact is missing or unsafe: {artifact.relative_path}"
                ) from exc
            if len(data) != artifact.size or sha256_bytes(data) != artifact.digest:
                raise DeliveryError(
                    f"bundle artifact integrity check failed: {artifact.relative_path}"
                )
            artifact_payloads[artifact.relative_path] = data
            expected_sums.append(f"{artifact.digest}  {artifact.relative_path}")

        try:
            sums_path = resolve_under(
                self.bundle_root,
                f"{bundle_prefix}/SHA256SUMS",
                allow_missing_leaf=False,
            )
            actual_sums = sums_path.read_text(encoding="utf-8")
        except (OSError, SecurityError) as exc:
            raise DeliveryError("bundle checksum manifest is missing or unsafe") from exc
        if actual_sums != "\n".join(expected_sums) + "\n":
            raise DeliveryError("bundle checksum manifest integrity check failed")

        changed_payloads: dict[str, bytes] = {}
        for relative in bundle.changed_files:
            artifact_name = f"files/{Path(relative).as_posix()}"
            if artifact_name not in artifact_payloads:
                raise DeliveryError(f"bundle changed file has no artifact: {relative}")
            changed_payloads[relative] = artifact_payloads[artifact_name]
        return changed_payloads

    def package(
        self,
        run_id: UUID,
        destination: Path,
        candidate_files: dict[str, bytes],
        acceptance_report: dict[str, object],
        test_report: dict[str, object],
    ) -> DeliveryBundle:
        if acceptance_report.get("passed") is not True or test_report.get("passed") is not True:
            raise DeliveryError("failed or untested acceptance criteria block packaging")
        destination = destination.resolve(strict=True)
        bundle_id = uuid4()
        root = self.bundle_root / str(bundle_id)
        files_root = root / "files"
        files_root.mkdir(parents=True)
        baseline: dict[str, str | None] = {}
        artifacts: list[Artifact] = []
        patch_lines: list[str] = []
        for relative, data in sorted(candidate_files.items()):
            try:
                target = resolve_under(destination, relative)
            except SecurityError as exc:
                raise DeliveryError(str(exc)) from exc
            baseline[relative] = sha256_file(target) if target.exists() else None
            old_data = target.read_bytes() if target.exists() else b""
            try:
                patch_lines.extend(
                    unified_diff(
                        old_data.decode().splitlines(keepends=True),
                        data.decode().splitlines(keepends=True),
                        fromfile=f"a/{relative}",
                        tofile=f"b/{relative}",
                    )
                )
            except UnicodeDecodeError:
                patch_lines.append(f"Binary file changed: {relative}\n")
            staged = files_root / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(staged, data)
            artifacts.append(
                Artifact(
                    digest=sha256_bytes(data),
                    relative_path=f"files/{Path(relative).as_posix()}",
                    media_type="application/octet-stream",
                    size=len(data),
                )
            )
        patch_data = "".join(patch_lines).encode()
        atomic_write(root / "patch.diff", patch_data)
        artifacts.append(
            Artifact(
                digest=sha256_bytes(patch_data),
                relative_path="patch.diff",
                media_type="text/x-diff",
                size=len(patch_data),
            )
        )
        dependency_files = [
            item.relative_path
            for item in artifacts
            if Path(item.relative_path).name
            in {"pyproject.toml", "uv.lock", "requirements.txt", "package.json", "pnpm-lock.yaml"}
        ]
        reports: dict[str, object] = {
            "acceptance.json": acceptance_report,
            "tests.json": test_report,
            "dependency-manifest.json": {"files": dependency_files},
            "sbom.json": {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "components": [
                    {
                        "type": "file",
                        "name": item.relative_path,
                        "hashes": [{"alg": "SHA-256", "content": item.digest}],
                    }
                    for item in artifacts
                    if item.relative_path.startswith("files/")
                ],
            },
            "security.json": {
                "dependency_audit": "not_run",
                "container_scan": "not_run",
                "reason": "scanner results were not supplied to the delivery manager",
            },
            "rollback.json": {"strategy": "restore authenticated encrypted application backup"},
            "README.json": {
                "review": (
                    "Verify bundle.json, patch.diff, acceptance.json, tests.json, and SHA256SUMS."
                ),
                "undo": f"corvus undo {bundle_id}",
            },
        }
        for name, report in reports.items():
            data = _canonical_json(report)
            atomic_write(root / name, data)
            artifacts.append(
                Artifact(
                    digest=sha256_bytes(data),
                    relative_path=name,
                    media_type="application/json",
                    size=len(data),
                )
            )
        manifest_body = {
            "schema_version": 1,
            "id": str(bundle_id),
            "run_id": str(run_id),
            "destination": str(destination),
            "changed_files": sorted(candidate_files),
            "baseline_hashes": baseline,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
        manifest_digest = sha256_bytes(_canonical_json(manifest_body))
        manifest_body["manifest_digest"] = manifest_digest
        atomic_write(root / "bundle.json", _canonical_json(manifest_body))
        sums = [f"{item.digest}  {item.relative_path}" for item in artifacts]
        atomic_write(root / "SHA256SUMS", ("\n".join(sums) + "\n").encode())
        return DeliveryBundle(
            id=bundle_id,
            run_id=run_id,
            destination=destination,
            artifacts=artifacts,
            changed_files=sorted(candidate_files),
            baseline_hashes=baseline,
            manifest_digest=manifest_digest,
        )

    def approve(
        self,
        bundle: DeliveryBundle,
        *,
        actor_id: str,
        ttl_minutes: int = 15,
    ) -> ApprovalGrant:
        if not actor_id.strip():
            raise DeliveryError("approval actor_id is required")
        if ttl_minutes <= 0:
            raise DeliveryError("approval ttl_minutes must be positive")
        approval = ApprovalGrant(
            run_id=bundle.run_id,
            bundle_id=bundle.id,
            destination=bundle.destination,
            manifest_digest=bundle.manifest_digest,
            approved_files=bundle.changed_files,
            expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
            nonce=secrets.token_urlsafe(24),
        )
        record: dict[str, object] = {
            "actor_id": actor_id,
            "grant": approval.model_dump(mode="json"),
            "operation": "apply",
            "state": "approved",
        }
        path = self._approval_path(bundle, approval)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._durable_write(path, self._sealed_record(record))
        return approval

    def approve_undo(
        self,
        bundle: DeliveryBundle,
        *,
        actor_id: str,
        ttl_minutes: int = 15,
    ) -> ApprovalGrant:
        if not actor_id.strip():
            raise DeliveryError("approval actor_id is required")
        if ttl_minutes <= 0:
            raise DeliveryError("approval ttl_minutes must be positive")
        with self._delivery_locks(bundle):
            journal_path = self.backup_root / str(bundle.id) / "journal.json"
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DeliveryError("delivery apply receipt is missing or invalid") from exc
            receipt_digest = journal.get("apply_receipt_digest")
            if journal.get("status") != "applied" or not isinstance(receipt_digest, str):
                raise DeliveryError("delivery has no applied receipt eligible for undo")
            approval = ApprovalGrant(
                run_id=bundle.run_id,
                bundle_id=bundle.id,
                destination=bundle.destination,
                manifest_digest=bundle.manifest_digest,
                approved_files=bundle.changed_files,
                expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
                nonce=secrets.token_urlsafe(24),
            )
            record: dict[str, object] = {
                "actor_id": actor_id,
                "apply_receipt_digest": receipt_digest,
                "grant": approval.model_dump(mode="json"),
                "operation": "undo",
                "state": "approved",
            }
            path = self._approval_path(bundle, approval)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._durable_write(path, self._sealed_record(record))
            return approval

    @staticmethod
    def _validate_approval(bundle: DeliveryBundle, approval: ApprovalGrant) -> None:
        if approval.expires_at <= datetime.now(UTC):
            raise DeliveryError("approval expired")
        if (
            approval.run_id != bundle.run_id
            or approval.bundle_id != bundle.id
            or approval.destination.resolve() != bundle.destination.resolve()
            or approval.manifest_digest != bundle.manifest_digest
            or sorted(approval.approved_files) != sorted(bundle.changed_files)
        ):
            raise DeliveryError("approval is not bound to this exact bundle")

    def apply(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
    ) -> Path:
        with self._delivery_locks(bundle):
            return self._apply_locked(bundle, approval, actor_id=actor_id)

    def _apply_locked(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
    ) -> Path:
        self._validate_approval(bundle, approval)
        self._available_approval_record(
            bundle,
            approval,
            actor_id=actor_id,
            operation="apply",
        )
        verified_payloads = self._verified_bundle_payloads(bundle)
        cipher = self._cipher()

        destination_state: dict[str, tuple[bool, bytes | None]] = {}
        for relative in bundle.changed_files:
            target = resolve_under(bundle.destination, relative)
            existed = target.exists()
            original = target.read_bytes() if existed else None
            actual = sha256_bytes(original) if original is not None else None
            if actual != bundle.baseline_hashes[relative]:
                raise DeliveryError(f"destination conflict: {relative}")
            destination_state[relative] = (existed, original)

        backup = self.backup_root / str(bundle.id)
        backup.mkdir(parents=True, exist_ok=False)
        files: dict[str, dict[str, object]] = {}
        for relative in bundle.changed_files:
            existed, original = destination_state[relative]
            backup_digest: str | None = None
            if original is not None:
                backup_file = backup / "files" / f"{relative}.enc"
                backup_file.parent.mkdir(parents=True, exist_ok=True)
                ciphertext = cipher.encrypt(original)
                atomic_write(backup_file, ciphertext)
                backup_digest = sha256_bytes(ciphertext)
            files[relative] = {
                "backup_digest": backup_digest,
                "baseline_hash": bundle.baseline_hashes[relative],
                "delivered_hash": sha256_bytes(verified_payloads[relative]),
                "existed": existed,
                "state": "pending",
            }

        journal: dict[str, Any] = {
            "approval_id": str(approval.id),
            "files": files,
            "git_ref": self._create_git_checkpoint(bundle),
            "manifest_digest": bundle.manifest_digest,
            "operation": "apply",
            "status": "prepared",
        }
        journal_path = backup / "journal.json"
        self._durable_write(journal_path, journal)
        self._fault("after_intent_persisted")
        self._claim_approval(
            bundle,
            approval,
            actor_id=actor_id,
            operation="apply",
        )
        self._fault("after_intent")
        self._fault("after_approval_consumed")
        journal["status"] = "applying"
        self._durable_write(journal_path, journal)

        try:
            for relative in bundle.changed_files:
                target = resolve_under(bundle.destination, relative)
                actual = sha256_file(target) if target.exists() else None
                if actual != bundle.baseline_hashes[relative]:
                    raise DeliveryError(f"destination conflict: {relative}")
                journal["files"][relative]["state"] = "writing"
                self._durable_write(journal_path, journal)
                self._fault(f"before_write:{relative}")
                atomic_write(target, verified_payloads[relative])
                self._fault(f"after_write:{relative}")
                delivered_hash = sha256_file(target)
                if delivered_hash != journal["files"][relative]["delivered_hash"]:
                    raise DeliveryError(f"delivered artifact integrity check failed: {relative}")
                journal["files"][relative]["state"] = "delivered"
                self._durable_write(journal_path, journal)
            journal["applied_at"] = datetime.now(UTC).isoformat()
            receipt_body = {
                "approval_id": journal["approval_id"],
                "applied_at": journal["applied_at"],
                "bundle_id": str(bundle.id),
                "files": journal["files"],
                "manifest_digest": bundle.manifest_digest,
            }
            journal["apply_receipt_digest"] = sha256_bytes(_canonical_json(receipt_body))
            journal["status"] = "applied"
            self._durable_write(journal_path, journal)
            self._fault("after_applied")
            return backup
        except Exception:
            self._restore(bundle.destination, backup, applied_only=bundle.changed_files)
            raise

    def undo(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
    ) -> None:
        with self._delivery_locks(bundle):
            self._undo_locked(bundle, approval, actor_id=actor_id)

    def _undo_locked(
        self,
        bundle: DeliveryBundle,
        approval: ApprovalGrant,
        *,
        actor_id: str,
    ) -> None:
        self._validate_approval(bundle, approval)
        _, approval_record = self._available_approval_record(
            bundle,
            approval,
            actor_id=actor_id,
            operation="undo",
        )
        backup = self.backup_root / str(bundle.id)
        journal_path = backup / "journal.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery apply receipt is missing or invalid") from exc
        if journal.get("status") != "applied":
            raise DeliveryError("delivery has no applied checkpoint eligible for undo")
        if approval_record.get("apply_receipt_digest") != journal.get("apply_receipt_digest"):
            raise DeliveryError("undo approval is not bound to the original apply receipt")
        for relative, item in journal["files"].items():
            target = resolve_under(bundle.destination, relative)
            if not target.exists() or sha256_file(target) != item["delivered_hash"]:
                raise DeliveryError(f"undo conflict: {relative}")
        journal["operation"] = "undo"
        journal["status"] = "undoing"
        journal["undo_approval_id"] = str(approval.id)
        self._durable_write(journal_path, journal)
        self._fault("after_undo_intent_persisted")
        self._claim_approval(
            bundle,
            approval,
            actor_id=actor_id,
            operation="undo",
        )
        self._fault("after_undo_intent")
        self._restore(bundle.destination, backup, applied_only=bundle.changed_files)

    def recover(self, bundle: DeliveryBundle) -> Path:
        with self._delivery_locks(bundle):
            return self._recover_locked(bundle)

    def _recover_locked(self, bundle: DeliveryBundle) -> Path:
        backup = self.backup_root / str(bundle.id)
        journal_path = backup / "journal.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery recovery journal is missing or invalid") from exc
        status = journal.get("status")
        if status in {"applied", "rolled_back"}:
            return backup
        if status not in {"prepared", "applying", "rolling_back", "undoing"}:
            raise DeliveryError(f"unsupported delivery recovery state: {status}")

        operation = "undo" if journal.get("operation") == "undo" else "apply"
        approval_key = "undo_approval_id" if operation == "undo" else "approval_id"
        approval_id = journal.get(approval_key)
        if isinstance(approval_id, str):
            self._consume_approval(bundle, approval_id, operation=operation)
        self._restore(bundle.destination, backup, applied_only=bundle.changed_files)
        return backup

    def _restore(self, destination: Path, backup: Path, applied_only: list[str]) -> None:
        journal_path = backup / "journal.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery recovery journal is missing or invalid") from exc
        journal["status"] = "rolling_back"
        self._durable_write(journal_path, journal)
        for relative in reversed(applied_only):
            item = journal.get("files", {}).get(relative)
            if item is None or item.get("state") == "pending":
                continue
            target = resolve_under(destination, relative)
            actual = sha256_file(target) if target.exists() else None
            allowed = {item.get("baseline_hash"), item.get("delivered_hash")}
            if actual not in allowed:
                raise DeliveryError(f"recovery conflict: {relative}")
            source = backup / "files" / f"{relative}.enc"
            if item["existed"]:
                try:
                    ciphertext = source.read_bytes()
                    if sha256_bytes(ciphertext) != item.get("backup_digest"):
                        raise DeliveryError("checkpoint ciphertext integrity check failed")
                    plaintext = self._cipher().decrypt(ciphertext)
                except InvalidToken as exc:
                    raise DeliveryError("checkpoint decryption or integrity check failed") from exc
                atomic_write(target, plaintext)
            elif target.exists():
                target.unlink()
            item["state"] = "restored"
            self._durable_write(journal_path, journal)
        journal["status"] = "rolled_back"
        journal["rolled_back_at"] = datetime.now(UTC).isoformat()
        self._durable_write(journal_path, journal)

    @staticmethod
    def _create_git_checkpoint(bundle: DeliveryBundle) -> str | None:
        git = shutil.which("git")
        if git is None or not (bundle.destination / ".git").exists():
            return None
        try:
            status = run_trusted_argv(
                [git, "status", "--porcelain"],
                cwd=bundle.destination,
            )
            if status.returncode != 0 or status.stdout.strip():
                return None
            head = run_trusted_argv(
                [git, "rev-parse", "HEAD"],
                cwd=bundle.destination,
            )
        except TrustedProcessError:
            return None
        if head.returncode != 0:
            return None
        reference = f"refs/corvus/checkpoints/{bundle.id}"
        try:
            updated = run_trusted_argv(
                [git, "update-ref", reference, head.stdout.decode().strip()],
                cwd=bundle.destination,
            )
        except (TrustedProcessError, UnicodeDecodeError):
            return None
        return reference if updated.returncode == 0 else None

    def load(self, bundle_id: UUID) -> DeliveryBundle:
        try:
            manifest_path = resolve_under(
                self.bundle_root,
                f"{bundle_id}/bundle.json",
                allow_missing_leaf=False,
            )
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, SecurityError, ValueError) as exc:
            raise DeliveryError("bundle manifest is missing, unsafe, or invalid") from exc
        data.pop("schema_version", None)
        bundle = DeliveryBundle.model_validate(data)
        self._verified_bundle_payloads(bundle)
        return bundle
