from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field

from corvus.mvp.models import MvpModel


class SidecarState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


class DesktopSidecarController:
    def __init__(self) -> None:
        self._state = SidecarState.STOPPED
        self._history = [self._state]

    @property
    def state(self) -> SidecarState:
        return self._state

    @property
    def history(self) -> tuple[SidecarState, ...]:
        return tuple(self._history)

    def start(self) -> None:
        self._transition(SidecarState.STARTING, allowed={SidecarState.STOPPED, SidecarState.FAILED})

    def mark_ready(self) -> None:
        self._transition(
            SidecarState.READY,
            allowed={SidecarState.STARTING, SidecarState.RECONNECTING},
        )

    def mark_failed(self) -> None:
        self._transition(
            SidecarState.FAILED,
            allowed={SidecarState.STARTING, SidecarState.RECONNECTING, SidecarState.READY},
        )

    def connection_lost(self) -> None:
        self._transition(SidecarState.RECONNECTING, allowed={SidecarState.READY})

    def stop(self) -> None:
        self._transition(
            SidecarState.STOPPED,
            allowed={
                SidecarState.STARTING,
                SidecarState.READY,
                SidecarState.FAILED,
                SidecarState.RECONNECTING,
            },
        )

    def _transition(self, target: SidecarState, *, allowed: set[SidecarState]) -> None:
        if self._state not in allowed:
            raise ValueError(f"invalid_sidecar_transition:{self._state.value}->{target.value}")
        self._state = target
        self._history.append(target)


class UpdateSignature(MvpModel):
    key_id: str
    signature: str


class UpdateManifest(MvpModel):
    version: str = Field(min_length=1)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime
    expires_at: datetime
    threshold: int = Field(ge=1)
    signatures: tuple[UpdateSignature, ...]


def _manifest_bytes(manifest: UpdateManifest) -> bytes:
    payload = manifest.model_dump(mode="json", exclude={"signatures"})
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class LocalUpdateKey:
    """Ephemeral local test key. It is never suitable for production signing."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def generate(cls, key_id: str) -> LocalUpdateKey:
        return cls(key_id, Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def sign_manifest(self, manifest: UpdateManifest) -> UpdateSignature:
        signature = self._private_key.sign(_manifest_bytes(manifest))
        return UpdateSignature(
            key_id=self.key_id,
            signature=base64.urlsafe_b64encode(signature).decode("ascii"),
        )


def verify_update_manifest(
    manifest: UpdateManifest,
    *,
    trusted_public_keys: dict[str, str],
    minimum_version: str,
) -> None:
    now = datetime.now(UTC)
    if manifest.expires_at <= now:
        raise ValueError("update_manifest_expired")
    if manifest.published_at > now:
        raise ValueError("update_manifest_not_yet_valid")
    if _version_tuple(manifest.version) < _version_tuple(minimum_version):
        raise ValueError("update_rollback_detected")
    verified: set[str] = set()
    for signature in manifest.signatures:
        if signature.key_id in verified:
            continue
        encoded_key = trusted_public_keys.get(signature.key_id)
        if encoded_key is None:
            continue
        try:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(encoded_key.encode("ascii"))
            )
            public_key.verify(
                base64.urlsafe_b64decode(signature.signature.encode("ascii")),
                _manifest_bytes(manifest),
            )
        except (InvalidSignature, ValueError, TypeError):
            continue
        verified.add(signature.key_id)
    if len(verified) < manifest.threshold:
        raise ValueError("update_signature_threshold_not_met")


def _version_tuple(value: str) -> tuple[int, int, int]:
    core = value.split("-", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError("semantic_version_required")
    try:
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    except ValueError as error:
        raise ValueError("semantic_version_required") from error
