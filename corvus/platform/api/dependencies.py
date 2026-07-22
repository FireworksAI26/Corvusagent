from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from corvus.application.oauth import OAuthClient
from corvus.application.sync import SyncService
from corvus.infrastructure.oauth.google import (
    GoogleOAuthClient,
    GoogleOAuthConfig,
    HttpxGoogleOAuthTransport,
)
from corvus.infrastructure.oauth.repository import OAuthTransactionRepository
from corvus.infrastructure.repositories.accounts import AccountRepository
from corvus.infrastructure.repositories.platform_identity import PlatformIdentityRepository
from corvus.infrastructure.repositories.sync import SyncRepository
from corvus.platform.config import PlatformSettings, create_platform_engine
from corvus.platform.cloud_runtime import (
    CloudRuntimeRepository, CloudRuntimeService, E2BCloudRuntimeProvider,
)


class EnvironmentSecretResolver:
    def resolve(self, reference: str) -> str:
        prefix = "env://"
        if not reference.startswith(prefix):
            raise ValueError("secret_reference_unsupported")
        name = reference.removeprefix(prefix)
        value = os.environ.get(name)
        if value is None or not value:
            raise ValueError("secret_reference_unavailable")
        return value


@dataclass(frozen=True, slots=True)
class IdentityApiDependencies:
    accounts: AccountRepository
    platform: PlatformIdentityRepository
    oauth_client: OAuthClient
    sync: SyncService
    public_origin: str
    session_secret: str
    clock: Callable[[], datetime]
    cloud_runtime: CloudRuntimeService | None = None


def build_identity_dependencies(
    *,
    database: Path,
    oauth_client: OAuthClient,
    public_origin: str,
    session_secret: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IdentityApiDependencies:
    accounts = AccountRepository(database)
    return IdentityApiDependencies(
        accounts=accounts,
        platform=PlatformIdentityRepository(accounts.engine),
        oauth_client=oauth_client,
        sync=SyncService(SyncRepository(accounts.engine)),
        public_origin=public_origin.rstrip("/"),
        session_secret=session_secret,
        clock=clock,
    )


def build_hosted_identity_dependencies(
    settings: PlatformSettings,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IdentityApiDependencies | None:
    hosted = settings.hosted_oauth
    if hosted is None:
        return None
    engine = create_platform_engine(settings.database_url)
    accounts = AccountRepository(engine)
    transactions = OAuthTransactionRepository(
        engine,
        encryption_secret=hosted.transaction_encryption_secret,
    )
    oauth_client = GoogleOAuthClient(
        config=GoogleOAuthConfig(
            client_id=hosted.google_client_id,
            client_secret_ref=hosted.google_client_secret_ref,
            redirect_uris=hosted.google_redirect_uris,
            state_secret=settings.oauth_state_secret,
        ),
        transactions=transactions,
        transport=HttpxGoogleOAuthTransport(),
        secret_resolver=EnvironmentSecretResolver(),
        clock=clock,
    )
    cloud_runtime = None
    e2b_api_key = os.environ.get("E2B_API_KEY", "").strip()
    e2b_template = os.environ.get("CORVUS_E2B_TEMPLATE", "").strip()
    if e2b_api_key and e2b_template:
        cloud_runtime = CloudRuntimeService(
            repository=CloudRuntimeRepository(engine),
            provider=E2BCloudRuntimeProvider(
                api_key=e2b_api_key, template_ref=e2b_template
            ),
            template_ref=e2b_template,
        )
    return IdentityApiDependencies(
        accounts=accounts,
        platform=PlatformIdentityRepository(engine),
        oauth_client=oauth_client,
        sync=SyncService(SyncRepository(engine)),
        public_origin=hosted.public_origin,
        session_secret=settings.session_secret,
        clock=clock,
        cloud_runtime=cloud_runtime,
    )


def build_hosted_identity_dependencies_from_env() -> IdentityApiDependencies | None:
    oauth_names = (
        "CORVUS_PUBLIC_ORIGIN",
        "CORVUS_GOOGLE_CLIENT_ID",
        "CORVUS_GOOGLE_CLIENT_SECRET_REF",
        "CORVUS_GOOGLE_REDIRECT_URIS",
        "CORVUS_OAUTH_TRANSACTION_SECRET",
    )
    if not any(os.environ.get(name, "").strip() for name in oauth_names):
        return None
    return build_hosted_identity_dependencies(PlatformSettings.from_env())
