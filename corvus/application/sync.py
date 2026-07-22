from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from corvus.domain.sync import SyncApplyResult, SyncMutation, SyncPage, SyncProtocolError


class SyncRepositoryPort(Protocol):
    def apply(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        acknowledged_cursor: int,
        mutations: tuple[SyncMutation, ...],
        now: datetime,
    ) -> SyncApplyResult: ...

    def page(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        cursor: int,
        limit: int,
    ) -> SyncPage: ...


class SyncService:
    def __init__(self, repository: SyncRepositoryPort) -> None:
        self.repository = repository

    def apply(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        acknowledged_cursor: int,
        mutations: Sequence[SyncMutation],
        now: datetime,
    ) -> SyncApplyResult:
        if acknowledged_cursor < 0:
            raise SyncProtocolError("sync_acknowledgement_invalid")
        selected = tuple(mutations)
        if len(selected) > 100:
            raise SyncProtocolError("sync_batch_too_large")
        return self.repository.apply(
            workspace_id=workspace_id,
            account_id=account_id,
            principal_id=principal_id,
            device_id=device_id,
            device_version=device_version,
            acknowledged_cursor=acknowledged_cursor,
            mutations=selected,
            now=now,
        )

    def page(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        principal_id: UUID,
        device_id: UUID,
        device_version: int,
        cursor: int,
        limit: int,
    ) -> SyncPage:
        if cursor < 0:
            raise SyncProtocolError("sync_cursor_invalid")
        if not 1 <= limit <= 100:
            raise SyncProtocolError("sync_page_limit_invalid")
        return self.repository.page(
            workspace_id=workspace_id,
            account_id=account_id,
            principal_id=principal_id,
            device_id=device_id,
            device_version=device_version,
            cursor=cursor,
            limit=limit,
        )
