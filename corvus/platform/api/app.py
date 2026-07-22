from __future__ import annotations

from fastapi import APIRouter

from corvus.platform.api.dependencies import IdentityApiDependencies
from corvus.platform.api.cloud import create_cloud_runtime_router
from corvus.platform.api.identity import create_identity_router
from corvus.platform.api.sync import create_sync_router


def create_platform_router(
    dependencies: IdentityApiDependencies | None,
) -> APIRouter:
    router = APIRouter()
    router.include_router(create_identity_router(dependencies))
    router.include_router(create_sync_router(dependencies))
    router.include_router(create_cloud_runtime_router(dependencies))
    return router
