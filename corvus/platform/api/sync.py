from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from corvus.domain.sync import (
    SyncApplyResult,
    SyncConflictError,
    SyncMutation,
    SyncPage,
    SyncProtocolError,
)
from corvus.infrastructure.repositories.accounts import WebSessionAuthentication
from corvus.platform.api.dependencies import IdentityApiDependencies
from corvus.platform.api.identity import (
    V2_ERROR_RESPONSES,
    authenticate_mutation,
    authenticate_session,
)
from corvus.security import SecurityError

_SESSION_COOKIE = "__Host-corvus_v2_session"


class SyncApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SyncMutationBatch(SyncApiModel):
    acknowledged_cursor: int = Field(ge=0)
    mutations: tuple[SyncMutation, ...] = Field(default=(), max_length=100)


def _sync_error(code: str, status_code: int, **detail: object) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, **detail, "correlation_id": str(uuid4())},
    )


def _map_sync_error(error: SyncProtocolError) -> HTTPException:
    if isinstance(error, SyncConflictError):
        return _sync_error(
            error.code,
            status.HTTP_409_CONFLICT,
            **error.conflict_detail.model_dump(mode="json", exclude={"code"}),
        )
    if error.code == "sync_resync_required":
        detail = error.detail if isinstance(error.detail, dict) else {}
        return _sync_error(error.code, status.HTTP_409_CONFLICT, **detail)
    if error.code == "idempotency_payload_mismatch":
        return _sync_error(error.code, status.HTTP_409_CONFLICT)
    if error.code.endswith("not_found"):
        return _sync_error("workspace_not_found", status.HTTP_404_NOT_FOUND)
    if error.code.endswith("forbidden"):
        return _sync_error(error.code, status.HTTP_403_FORBIDDEN)
    if error.code in {
        "sync_cursor_ahead",
        "sync_cursor_invalid",
        "sync_page_limit_invalid",
        "sync_acknowledgement_ahead",
        "sync_acknowledgement_rewind",
    }:
        return _sync_error(error.code, status.HTTP_400_BAD_REQUEST)
    if error.code == "sync_change_integrity_invalid":
        return _sync_error(error.code, status.HTTP_503_SERVICE_UNAVAILABLE)
    return _sync_error("sync_failure", status.HTTP_503_SERVICE_UNAVAILABLE)


def create_sync_router(dependencies: IdentityApiDependencies | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v2",
        tags=["platform-sync"],
        responses=V2_ERROR_RESPONSES,
    )
    if dependencies is None:

        def unavailable() -> None:
            raise _sync_error("platform_identity_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)

        @router.get(
            "/workspaces/{workspace_id}/sync",
            operation_id="unavailable_workspace_sync_get",
            response_model=SyncPage,
        )
        def unavailable_workspace_sync_get(
            workspace_id: UUID,
            _cursor: Annotated[int, Query(alias="cursor", ge=0)] = 0,
            _limit: Annotated[int, Query(alias="limit", ge=1, le=100)] = 100,
        ) -> None:
            del workspace_id
            unavailable()

        @router.post(
            "/workspaces/{workspace_id}/sync/mutations",
            operation_id="unavailable_workspace_sync_post",
            response_model=SyncApplyResult,
        )
        def unavailable_workspace_sync_post(
            workspace_id: UUID,
            _body: SyncMutationBatch,
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            del workspace_id
            unavailable()

        return router

    def authenticated(
        session_token: Annotated[str | None, Cookie(alias=_SESSION_COOKIE)] = None,
    ) -> WebSessionAuthentication:
        return authenticate_session(dependencies, session_token)

    def mutation_authenticated(
        request: Request,
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> WebSessionAuthentication:
        return authenticate_mutation(dependencies, request, session, csrf_token)

    @router.get("/workspaces/{workspace_id}/sync", response_model=SyncPage)
    def page(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> dict[str, object]:
        try:
            result = dependencies.sync.page(
                workspace_id=workspace_id,
                account_id=session.account.id,
                principal_id=session.account.principal_id,
                device_id=session.session.device_id,
                device_version=session.session.device_version,
                cursor=cursor,
                limit=limit,
            )
        except SyncProtocolError as exc:
            raise _map_sync_error(exc) from None
        return result.model_dump(mode="json")

    @router.post("/workspaces/{workspace_id}/sync/mutations", response_model=SyncApplyResult)
    def apply(
        workspace_id: UUID,
        body: SyncMutationBatch,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        try:
            result = dependencies.sync.apply(
                workspace_id=workspace_id,
                account_id=session.account.id,
                principal_id=session.account.principal_id,
                device_id=session.session.device_id,
                device_version=session.session.device_version,
                acknowledged_cursor=body.acknowledged_cursor,
                mutations=body.mutations,
                now=dependencies.clock(),
            )
        except SecurityError:
            raise _sync_error(
                "sync_payload_rejected", status.HTTP_422_UNPROCESSABLE_CONTENT
            ) from None
        except SyncProtocolError as exc:
            raise _map_sync_error(exc) from None
        return result.model_dump(mode="json")

    return router
