import hmac
from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from corvus.application.oauth import OAuthCallback, OAuthError
from corvus.domain.account import DeviceRegistration, ExperienceKind, normalize_identity_email
from corvus.domain.identity import (
    RecordStatus,
    WorkspaceKind,
)
from corvus.domain.identity import (
    Workspace as DomainWorkspace,
)
from corvus.infrastructure.repositories.accounts import (
    AccountRepositoryError,
    WebSessionAuthentication,
)
from corvus.infrastructure.repositories.platform_identity import PlatformIdentityRepositoryError
from corvus.platform.api.dependencies import IdentityApiDependencies

_SESSION_COOKIE = "__Host-corvus_v2_session"
_DEVICE_COOKIE = "__Host-corvus_v2_device"
_OAUTH_STATE_COOKIE = "__Host-corvus_v2_oauth_state"
_SESSION_TTL = timedelta(days=30)
_OAUTH_STATE_TTL = timedelta(minutes=10)
_CALLBACK_PATH = "/api/v2/auth/google/callback"
_ONBOARDING_DESTINATION = "/onboarding"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str = Field(min_length=1)
    correlation_id: UUID


class ApiErrorResponse(ApiModel):
    detail: ApiErrorDetail


V2_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ApiErrorResponse, "description": "Invalid request"},
    status.HTTP_401_UNAUTHORIZED: {
        "model": ApiErrorResponse,
        "description": "Authentication required",
    },
    status.HTTP_403_FORBIDDEN: {"model": ApiErrorResponse, "description": "Authority denied"},
    status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse, "description": "Resource not found"},
    status.HTTP_409_CONFLICT: {
        "model": ApiErrorResponse,
        "description": "Version or sync conflict",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ApiErrorResponse,
        "description": "Payload rejected",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ApiErrorResponse,
        "description": "Service unavailable",
    },
}


class OnboardingUpdate(ApiModel):
    experience_kind: ExperienceKind
    expected_version: int = Field(ge=1)


class WorkspaceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    workspace_kind: WorkspaceKind


class WorkspaceUpdate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("workspace_name_required")
        return normalized


class DeviceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    public_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeviceDelete(ApiModel):
    device_id: UUID
    expected_version: int = Field(ge=1)


class Workspace(ApiModel):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    workspace_kind: WorkspaceKind
    status: RecordStatus
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class SessionResponse(ApiModel):
    account_id: UUID
    principal_id: UUID
    email: str = Field(min_length=3, max_length=320)
    experience_kind: ExperienceKind | None
    account_version: int = Field(ge=1)
    session_version: int = Field(ge=1)
    csrf_token: str = Field(min_length=1)


class SessionRefreshResponse(ApiModel):
    csrf_token: str = Field(min_length=1)
    session_version: int = Field(ge=1)


class OnboardingResponse(ApiModel):
    experience_kind: ExperienceKind | None
    version: int = Field(ge=1)


def _error(code: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "correlation_id": str(uuid4())},
    )


def _map_error(error: Exception) -> HTTPException:
    code = str(error)
    if code.endswith("_conflict") or code == "idempotency_payload_mismatch":
        return _error(code, status.HTTP_409_CONFLICT)
    if code.endswith("not_found"):
        return _error(code, status.HTTP_404_NOT_FOUND)
    if code.endswith("forbidden"):
        return _error(code, status.HTTP_403_FORBIDDEN)
    if code.startswith("session_"):
        return _error("session_invalid", status.HTTP_401_UNAUTHORIZED)
    if code.startswith("oauth_") or code.startswith("google_"):
        return _error(code, status.HTTP_400_BAD_REQUEST)
    return _error("platform_identity_failure", status.HTTP_503_SERVICE_UNAVAILABLE)


def authenticate_session(
    dependencies: IdentityApiDependencies,
    session_token: str | None,
) -> WebSessionAuthentication:
    if session_token is None:
        raise _error("session_required", status.HTTP_401_UNAUTHORIZED)
    try:
        return dependencies.accounts.authenticate_web_session(
            session_token=session_token,
            session_secret=dependencies.session_secret,
            now=dependencies.clock(),
        )
    except AccountRepositoryError as exc:
        raise _map_error(exc) from None


def authenticate_mutation(
    dependencies: IdentityApiDependencies,
    request: Request,
    session: WebSessionAuthentication,
    csrf_token: str | None,
) -> WebSessionAuthentication:
    if request.headers.get("origin") != dependencies.public_origin:
        raise _error("origin_forbidden", status.HTTP_403_FORBIDDEN)
    if csrf_token is None or not hmac.compare_digest(csrf_token, session.csrf_token):
        raise _error("csrf_invalid", status.HTTP_403_FORBIDDEN)
    return session


def _set_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(
        key=name,
        value=value,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _set_oauth_state_cookie(response: Response, state_value: str) -> None:
    response.set_cookie(
        key=_OAUTH_STATE_COOKIE,
        value=state_value,
        max_age=int(_OAUTH_STATE_TTL.total_seconds()),
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        key=name,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _workspace_payload(workspace: DomainWorkspace) -> dict[str, object]:
    return workspace.model_dump(mode="json")


def _device_payload(device: DeviceRegistration) -> dict[str, object]:
    return device.model_dump(mode="json")


def create_identity_router(dependencies: IdentityApiDependencies | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v2",
        tags=["platform-identity"],
        responses=V2_ERROR_RESPONSES,
    )
    if dependencies is None:

        def unavailable() -> None:
            raise _error("platform_identity_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)

        @router.get("/auth/google/start", operation_id="unavailable_google_start")
        def unavailable_google_start() -> None:
            unavailable()

        @router.get("/auth/google/callback", operation_id="unavailable_google_callback")
        def unavailable_google_callback(
            _code: Annotated[str | None, Query(alias="code")] = None,
            _state: Annotated[str | None, Query(alias="state")] = None,
            _error_value: Annotated[str | None, Query(alias="error")] = None,
        ) -> None:
            unavailable()

        @router.get("/session", response_model=SessionResponse, operation_id="unavailable_session")
        def unavailable_session() -> None:
            unavailable()

        @router.post(
            "/session/refresh",
            response_model=SessionRefreshResponse,
            operation_id="unavailable_session_refresh",
        )
        def unavailable_session_refresh(
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            unavailable()

        @router.post("/logout", status_code=204, operation_id="unavailable_logout")
        def unavailable_logout(
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            unavailable()

        @router.get(
            "/onboarding",
            response_model=OnboardingResponse,
            operation_id="unavailable_onboarding_get",
        )
        def unavailable_onboarding_get() -> None:
            unavailable()

        @router.put(
            "/onboarding",
            response_model=OnboardingResponse,
            operation_id="unavailable_onboarding_put",
        )
        def unavailable_onboarding_put(
            _body: OnboardingUpdate,
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            unavailable()

        @router.get(
            "/workspaces",
            response_model=list[Workspace],
            operation_id="unavailable_workspaces_get",
        )
        def unavailable_workspaces_get() -> None:
            unavailable()

        @router.post(
            "/workspaces",
            response_model=Workspace,
            operation_id="unavailable_workspaces_post",
        )
        def unavailable_workspaces_post(
            _body: WorkspaceCreate,
            _idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            unavailable()

        @router.get(
            "/workspaces/{workspace_id}",
            response_model=Workspace,
            operation_id="unavailable_workspace_get",
        )
        def unavailable_workspace_get(workspace_id: UUID) -> None:
            del workspace_id
            unavailable()

        @router.patch(
            "/workspaces/{workspace_id}",
            response_model=Workspace,
            operation_id="unavailable_workspace_patch",
        )
        def unavailable_workspace_patch(
            workspace_id: UUID,
            _body: WorkspaceUpdate,
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            del workspace_id
            unavailable()

        @router.get(
            "/devices",
            response_model=list[DeviceRegistration],
            operation_id="unavailable_devices_get",
        )
        def unavailable_devices_get() -> None:
            unavailable()

        @router.post(
            "/devices",
            response_model=DeviceRegistration,
            operation_id="unavailable_devices_post",
        )
        def unavailable_devices_post(
            _body: DeviceCreate,
            _idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
            unavailable()

        @router.delete(
            "/devices",
            response_model=DeviceRegistration,
            operation_id="unavailable_devices_delete",
        )
        def unavailable_devices_delete(
            _body: DeviceDelete,
            _csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
        ) -> None:
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

    @router.get("/auth/google/start", status_code=302)
    def google_start() -> RedirectResponse:
        try:
            result = dependencies.oauth_client.start(
                f"{dependencies.public_origin}{_CALLBACK_PATH}"
            )
        except (OAuthError, ValueError) as exc:
            raise _map_error(exc) from None
        response = RedirectResponse(result.authorization_url, status_code=status.HTTP_302_FOUND)
        state_values = parse_qs(urlsplit(result.authorization_url).query).get("state", ())
        if len(state_values) != 1 or not state_values[0]:
            raise _error("oauth_state_invalid", status.HTTP_400_BAD_REQUEST)
        _set_oauth_state_cookie(response, state_values[0])
        return response

    @router.get("/auth/google/callback")
    def google_callback(
        code: Annotated[str | None, Query()] = None,
        state_value: Annotated[str | None, Query(alias="state")] = None,
        provider_error: Annotated[str | None, Query(alias="error")] = None,
        device_token: Annotated[str | None, Cookie(alias=_DEVICE_COOKIE)] = None,
        browser_state: Annotated[str | None, Cookie(alias=_OAUTH_STATE_COOKIE)] = None,
    ) -> RedirectResponse:
        if state_value is None or not 1 <= len(state_value) <= 4096:
            raise _error("oauth_state_invalid", status.HTTP_400_BAD_REQUEST)
        if browser_state is None or not hmac.compare_digest(browser_state, state_value):
            raise _error("oauth_state_invalid", status.HTTP_400_BAD_REQUEST)
        if provider_error is not None or code is None or not 1 <= len(code) <= 4096:
            try:
                dependencies.oauth_client.abort(state_value)
            except OAuthError as exc:
                if str(exc) == "oauth_state_invalid":
                    raise _map_error(exc) from None
            raise _error("oauth_callback_rejected", status.HTTP_400_BAD_REQUEST)
        try:
            identity = dependencies.oauth_client.exchange(
                OAuthCallback(code=code, state=state_value)
            )
        except (OAuthError, ValueError) as exc:
            if str(exc) == "oauth_state_invalid":
                raise _map_error(exc) from None
            raise _error("oauth_callback_rejected", status.HTTP_400_BAD_REQUEST) from None
        try:
            now = dependencies.clock()
            login = dependencies.accounts.complete_web_login(
                issuer=identity.issuer,
                subject=identity.subject,
                normalized_email=normalize_identity_email(identity.email),
                display_name=identity.display_name,
                existing_device_token=device_token,
                session_secret=dependencies.session_secret,
                now=now,
                expires_at=now + _SESSION_TTL,
            )
        except (AccountRepositoryError, ValueError) as exc:
            raise _map_error(exc) from None
        response = RedirectResponse(
            _ONBOARDING_DESTINATION,
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _set_cookie(response, _SESSION_COOKIE, login.session_token)
        if login.device_token is not None:
            _set_cookie(response, _DEVICE_COOKIE, login.device_token)
        _clear_cookie(response, _OAUTH_STATE_COOKIE)
        return response

    @router.get("/session", response_model=SessionResponse)
    def session(
        authenticated_session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> dict[str, object]:
        experience, account_version = dependencies.platform.get_onboarding(
            authenticated_session.account.id
        )
        return {
            "account_id": str(authenticated_session.account.id),
            "principal_id": str(authenticated_session.account.principal_id),
            "email": authenticated_session.account.normalized_email,
            "experience_kind": None if experience is None else experience.value,
            "account_version": account_version,
            "session_version": authenticated_session.session.version,
            "csrf_token": authenticated_session.csrf_token,
        }

    @router.post("/session/refresh", response_model=SessionRefreshResponse)
    def refresh_session(
        _authenticated: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
        response: Response,
        session_token: Annotated[str | None, Cookie(alias=_SESSION_COOKIE)] = None,
    ) -> dict[str, object]:
        if session_token is None:
            raise _error("session_required", status.HTTP_401_UNAUTHORIZED)
        try:
            now = dependencies.clock()
            rotated = dependencies.accounts.rotate_web_session(
                session_token=session_token,
                session_secret=dependencies.session_secret,
                now=now,
                expires_at=now + _SESSION_TTL,
            )
        except AccountRepositoryError as exc:
            raise _map_error(exc) from None
        _set_cookie(response, _SESSION_COOKIE, rotated.session_token)
        return {"csrf_token": rotated.csrf_token, "session_version": rotated.session.version}

    @router.post("/logout", status_code=204)
    def logout(
        _authenticated: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
        response: Response,
        session_token: Annotated[str | None, Cookie(alias=_SESSION_COOKIE)] = None,
    ) -> Response:
        if session_token is None:
            raise _error("session_required", status.HTTP_401_UNAUTHORIZED)
        try:
            dependencies.accounts.revoke_web_session(
                session_token=session_token,
                session_secret=dependencies.session_secret,
                now=dependencies.clock(),
            )
        except AccountRepositoryError as exc:
            raise _map_error(exc) from None
        _clear_cookie(response, _SESSION_COOKIE)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/onboarding", response_model=OnboardingResponse)
    def get_onboarding(
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> dict[str, object]:
        experience, version = dependencies.platform.get_onboarding(session.account.id)
        return {
            "experience_kind": None if experience is None else experience.value,
            "version": version,
        }

    @router.put("/onboarding", response_model=OnboardingResponse)
    def update_onboarding(
        body: OnboardingUpdate,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        try:
            experience, version = dependencies.platform.update_onboarding(
                account_id=session.account.id,
                experience_kind=body.experience_kind,
                expected_version=body.expected_version,
                now=dependencies.clock(),
            )
        except PlatformIdentityRepositoryError as exc:
            raise _map_error(exc) from None
        return {"experience_kind": experience.value, "version": version}

    @router.get("/workspaces", response_model=list[Workspace])
    def list_workspaces(
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> list[dict[str, object]]:
        return [
            _workspace_payload(workspace)
            for workspace in dependencies.platform.list_workspaces(session.account.principal_id)
        ]

    @router.post("/workspaces", response_model=Workspace)
    def create_workspace(
        body: WorkspaceCreate,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> JSONResponse:
        try:
            workspace, repeated = dependencies.platform.create_workspace(
                account_id=session.account.id,
                principal_id=session.account.principal_id,
                name=body.name,
                workspace_kind=body.workspace_kind,
                idempotency_key=idempotency_key,
                now=dependencies.clock(),
            )
        except PlatformIdentityRepositoryError as exc:
            raise _map_error(exc) from None
        return JSONResponse(
            _workspace_payload(workspace),
            status_code=status.HTTP_200_OK if repeated else status.HTTP_201_CREATED,
        )

    @router.get("/workspaces/{workspace_id}", response_model=Workspace)
    def get_workspace(
        workspace_id: UUID,
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> dict[str, object]:
        matches = {
            workspace.id: workspace
            for workspace in dependencies.platform.list_workspaces(session.account.principal_id)
        }
        workspace = matches.get(workspace_id)
        if workspace is None:
            raise _error("workspace_not_found", status.HTTP_404_NOT_FOUND)
        return _workspace_payload(workspace)

    @router.patch("/workspaces/{workspace_id}", response_model=Workspace)
    def update_workspace(
        workspace_id: UUID,
        body: WorkspaceUpdate,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        try:
            workspace = dependencies.platform.update_workspace(
                principal_id=session.account.principal_id,
                workspace_id=workspace_id,
                name=body.name,
                expected_version=body.expected_version,
                now=dependencies.clock(),
            )
        except PlatformIdentityRepositoryError as exc:
            raise _map_error(exc) from None
        return _workspace_payload(workspace)

    @router.get("/devices", response_model=list[DeviceRegistration])
    def list_devices(
        session: Annotated[WebSessionAuthentication, Depends(authenticated)],
    ) -> list[dict[str, object]]:
        return [
            _device_payload(device)
            for device in dependencies.platform.list_devices(session.account.id)
        ]

    @router.post("/devices", response_model=DeviceRegistration)
    def create_device(
        body: DeviceCreate,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> JSONResponse:
        try:
            device, repeated = dependencies.platform.register_device(
                account_id=session.account.id,
                name=body.name,
                public_key_digest=body.public_key_digest,
                idempotency_key=idempotency_key,
                now=dependencies.clock(),
            )
        except PlatformIdentityRepositoryError as exc:
            raise _map_error(exc) from None
        return JSONResponse(
            _device_payload(device),
            status_code=status.HTTP_200_OK if repeated else status.HTTP_201_CREATED,
        )

    @router.delete("/devices", response_model=DeviceRegistration)
    def delete_device(
        body: DeviceDelete,
        session: Annotated[WebSessionAuthentication, Depends(mutation_authenticated)],
    ) -> dict[str, object]:
        try:
            device = dependencies.platform.revoke_device(
                account_id=session.account.id,
                device_id=body.device_id,
                expected_version=body.expected_version,
                now=dependencies.clock(),
            )
        except PlatformIdentityRepositoryError as exc:
            raise _map_error(exc) from None
        return _device_payload(device)

    return router
