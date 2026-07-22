"""Utilities for sealing and transporting audit history heads."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field


class AuditHistoryHeads(BaseModel):
    """Sealed history roots used to bind rollback-sensitive audit ledgers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_history_head: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_binding_history_head: str = Field(pattern=r"^[0-9a-f]{64}$")


def advance_audit_history_head(previous_head: str, entry_digest: str) -> str:
    """Commit one canonical audit entry digest to a deterministic history head."""

    if len(previous_head) != 64 or len(entry_digest) != 64:
        raise ValueError("audit_history_digest_invalid")
    try:
        payload = bytes.fromhex(previous_head) + bytes.fromhex(entry_digest)
    except ValueError as exc:
        raise ValueError("audit_history_digest_invalid") from exc
    return hashlib.sha256(payload).hexdigest()
