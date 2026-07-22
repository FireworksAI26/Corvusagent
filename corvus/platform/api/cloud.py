from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status

from corvus.infrastructure.repositories.accounts import WebSessionAuthentication
from corvus.platform.api.dependencies import IdentityApiDependencies
from corvus.platform.api.identity import authenticate_mutation, authenticate_session
from corvus.platform.cloud_runtime import CloudRuntimeError

_SESSION_COOKIE = "__Host-corvus_v2_session"


def _error(code: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "correlation_id": str(uuid4())},
    )


def create_cloud_runtime_router(
    dependencies: IdentityApiDependencies | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["cloud-runtime"])
    if dependencies is None:
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

    def authorize(workspace_id: UUID, session: WebSessionAuthentication) -> None:
        workspaces = dependencies.platform.list_workspaces(session.account.principal_id)
        if workspace_id not in {workspace.id for workspace in workspaces}:
            raise _error("workspace_not_found", status.HTTP_404_NOT_FOUND)

    @router.get("/workspaces/{workspace_id}/runtime")
    def runtime_status(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> dict[str, object]:
        authorize(workspace_id, session)
        if dependencies.cloud_runtime is None:
            return {
                "workspace_id": str(workspace_id), "provider": "e2b",
                "configured": False, "sandbox_id": None, "template_ref": "",
                "generation": 0, "state": "unprovisioned",
                "updated_at": dependencies.clock().isoformat(),
                "error_code": "cloud_runtime_not_configured",
            }
        return dependencies.cloud_runtime.status(workspace_id).as_dict()

    def mutate(
        workspace_id: UUID,
        operation: str,
        session: WebSessionAuthentication,
    ) -> dict[str, object]:
        authorize(workspace_id, session)
        service = dependencies.cloud_runtime
        if service is None:
            raise _error("cloud_runtime_not_configured", status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            return getattr(service, operation)(workspace_id).as_dict()
        except CloudRuntimeError as error:
            raise _error(str(error), status.HTTP_503_SERVICE_UNAVAILABLE) from None

    @router.post("/workspaces/{workspace_id}/runtime/provision")
    def provision(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
        _key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> dict[str, object]:
        return mutate(workspace_id, "provision", session)

    @router.post("/workspaces/{workspace_id}/runtime/resume")
    def resume(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        return mutate(workspace_id, "resume", session)

    @router.post("/workspaces/{workspace_id}/runtime/pause")
    def pause(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        return mutate(workspace_id, "pause", session)

    @router.delete("/workspaces/{workspace_id}/runtime")
    def revoke(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        return mutate(workspace_id, "revoke", session)

    return router
