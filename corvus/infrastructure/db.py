from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, text

from corvus.database import M1_AUDIT_PROOF_MANIFEST_REVISION as _M1_AUDIT_PROOF_MANIFEST_REVISION
from corvus.database import M1_AUDIT_REVISION as _M1_AUDIT_REVISION
from corvus.database import M1_AUTHORITY_REVISION as _M1_AUTHORITY_REVISION
from corvus.database import M1_AUTHORIZATION_INPUT_REVISION as _M1_AUTHORIZATION_INPUT_REVISION
from corvus.database import M1_HANDOFF_REVISION as _M1_HANDOFF_REVISION
from corvus.database import M1_IDENTITY_SCOPE_REVISION as _M1_IDENTITY_SCOPE_REVISION
from corvus.database import M1_PROJECT_REVISION as _M1_PROJECT_REVISION
from corvus.database import M1_REGISTRY_REVISION as _M1_REGISTRY_REVISION
from corvus.database import M1_ROOT_MANIFEST_REVISION as _M1_ROOT_MANIFEST_REVISION
from corvus.database import M2_CONVERSATIONS_REVISION as _M2_CONVERSATIONS_REVISION
from corvus.database import M2_IDENTITY_CONTINUITY_REVISION as _M2_IDENTITY_CONTINUITY_REVISION
from corvus.database import M2_OAUTH_SESSIONS_REVISION as _M2_OAUTH_SESSIONS_REVISION
from corvus.database import M2_WORKSPACE_SYNC_REVISION as _M2_WORKSPACE_SYNC_REVISION
from corvus.database import DatabaseState, classify_database
from corvus.platform import create_platform_engine

M1_PROJECT_REVISION: Final = _M1_PROJECT_REVISION
M1_AUDIT_REVISION: Final = _M1_AUDIT_REVISION
M1_AUTHORITY_REVISION: Final = _M1_AUTHORITY_REVISION
M1_REGISTRY_REVISION: Final = _M1_REGISTRY_REVISION
M1_AUTHORIZATION_INPUT_REVISION: Final = _M1_AUTHORIZATION_INPUT_REVISION
M1_HANDOFF_REVISION: Final = _M1_HANDOFF_REVISION
M1_IDENTITY_SCOPE_REVISION: Final = _M1_IDENTITY_SCOPE_REVISION
M1_ROOT_MANIFEST_REVISION: Final = _M1_ROOT_MANIFEST_REVISION
M1_AUDIT_PROOF_MANIFEST_REVISION: Final = _M1_AUDIT_PROOF_MANIFEST_REVISION
M2_IDENTITY_CONTINUITY_REVISION: Final = _M2_IDENTITY_CONTINUITY_REVISION
M2_OAUTH_SESSIONS_REVISION: Final = _M2_OAUTH_SESSIONS_REVISION
M2_WORKSPACE_SYNC_REVISION: Final = _M2_WORKSPACE_SYNC_REVISION
M2_CONVERSATIONS_REVISION: Final = _M2_CONVERSATIONS_REVISION
M1_CURRENT_REVISION: Final = M2_CONVERSATIONS_REVISION


class InfrastructureDatabaseError(RuntimeError):
    pass


_CONVERSATION_HISTORY_SQL = text(
    "SELECT (SELECT COUNT(*) FROM agent_run_events) + "
    "(SELECT COUNT(*) FROM agent_runs) + (SELECT COUNT(*) FROM attachments) + "
    "(SELECT COUNT(*) FROM message_attachments) + (SELECT COUNT(*) FROM messages) + "
    "(SELECT COUNT(*) FROM run_artifact_lineage) + (SELECT COUNT(*) FROM run_artifacts) + "
    "(SELECT COUNT(*) FROM thread_versions) + (SELECT COUNT(*) FROM threads)"
)
_SYNC_HISTORY_SQL = text(
    "SELECT (SELECT COUNT(*) FROM workspace_sync_heads) + "
    "(SELECT COUNT(*) FROM workspace_changes) + "
    "(SELECT COUNT(*) FROM outbox_events) + "
    "(SELECT COUNT(*) FROM device_sync_acknowledgements) + "
    "(SELECT COUNT(*) FROM platform_idempotency WHERE scope_key <> 'account')"
)
_OAUTH_HISTORY_SQL = text(
    "SELECT (SELECT COUNT(*) FROM oauth_transactions) + "
    "(SELECT COUNT(*) FROM web_session_bindings) + "
    "(SELECT COUNT(*) FROM account_onboarding_versions) + "
    "(SELECT COUNT(*) FROM platform_idempotency WHERE scope_key = 'account')"
)
_IDENTITY_HISTORY_SQL = text(
    "SELECT (SELECT COUNT(*) FROM accounts) + "
    "(SELECT COUNT(*) FROM external_identities) + "
    "(SELECT COUNT(*) FROM device_registrations) + "
    "(SELECT COUNT(*) FROM session_records)"
)
_WORKSPACE_METADATA_SQL = text("SELECT workspace_kind, payload_json FROM identity_workspaces")


def _identity_workspace_metadata_is_compatible(engine: Engine) -> bool:
    with engine.connect() as connection:
        rows = connection.execute(_WORKSPACE_METADATA_SQL)
        for workspace_kind, payload_json in rows:
            if workspace_kind != "individual":
                return False
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError):
                return False
            if not isinstance(payload, dict):
                return False
            if payload.get("workspace_kind", "individual") != workspace_kind:
                return False
    return True


def _preflight_downgrade_history(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        if connection.scalar(_CONVERSATION_HISTORY_SQL):
            raise RuntimeError("conversation_history_present")
        if revision != M2_WORKSPACE_SYNC_REVISION and connection.scalar(_SYNC_HISTORY_SQL):
            raise RuntimeError("workspace_sync_history_present")
        if revision not in {M2_WORKSPACE_SYNC_REVISION, M2_OAUTH_SESSIONS_REVISION} and (
            connection.scalar(_OAUTH_HISTORY_SQL)
        ):
            raise RuntimeError("oauth_session_history_present")
        crosses_identity_revision = revision not in {
            M2_WORKSPACE_SYNC_REVISION,
            M2_OAUTH_SESSIONS_REVISION,
            M2_IDENTITY_CONTINUITY_REVISION,
        }
        if crosses_identity_revision and connection.scalar(_IDENTITY_HISTORY_SQL):
            raise RuntimeError("identity_continuity_history_present")
    if crosses_identity_revision and not _identity_workspace_metadata_is_compatible(engine):
        raise RuntimeError("identity_continuity_workspace_metadata_present")


def _alembic_config_url(database_url: str) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).with_name("migrations")),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _alembic_config(database: Path) -> Config:
    return _alembic_config_url(f"sqlite:///{database}")


def current_revision_url(database_url: str) -> str | None:
    engine = create_platform_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def upgrade_database_url(database_url: str) -> str:
    revision = current_revision_url(database_url)
    if revision not in {
        None,
        M1_PROJECT_REVISION,
        M1_AUDIT_REVISION,
        M1_AUTHORITY_REVISION,
        M1_REGISTRY_REVISION,
        M1_AUTHORIZATION_INPUT_REVISION,
        M1_HANDOFF_REVISION,
        M1_IDENTITY_SCOPE_REVISION,
        M1_ROOT_MANIFEST_REVISION,
        M1_AUDIT_PROOF_MANIFEST_REVISION,
        M2_IDENTITY_CONTINUITY_REVISION,
        M2_OAUTH_SESSIONS_REVISION,
        M2_WORKSPACE_SYNC_REVISION,
        M1_CURRENT_REVISION,
    }:
        raise InfrastructureDatabaseError(f"unsupported_database_revision:{revision}")
    command.upgrade(_alembic_config_url(database_url), "head")
    upgraded = current_revision_url(database_url)
    if upgraded != M1_CURRENT_REVISION:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{upgraded or 'unstamped'}")
    return upgraded


def downgrade_database_url(database_url: str, revision: str) -> str:
    current = current_revision_url(database_url)
    if current != M1_CURRENT_REVISION:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{current or 'unstamped'}")
    if revision not in {
        M1_PROJECT_REVISION,
        M1_AUDIT_REVISION,
        M1_AUTHORITY_REVISION,
        M1_REGISTRY_REVISION,
        M1_AUTHORIZATION_INPUT_REVISION,
        M1_HANDOFF_REVISION,
        M1_IDENTITY_SCOPE_REVISION,
        M1_ROOT_MANIFEST_REVISION,
        M1_AUDIT_PROOF_MANIFEST_REVISION,
        M2_IDENTITY_CONTINUITY_REVISION,
        M2_OAUTH_SESSIONS_REVISION,
        M2_WORKSPACE_SYNC_REVISION,
    }:
        raise InfrastructureDatabaseError(f"unsupported_downgrade_revision:{revision}")
    preflight_engine = create_platform_engine(database_url)
    try:
        _preflight_downgrade_history(preflight_engine, revision)
    finally:
        preflight_engine.dispose()
    command.downgrade(_alembic_config_url(database_url), revision)
    downgraded = current_revision_url(database_url)
    if downgraded != revision:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{downgraded or 'unstamped'}")
    return downgraded


def current_revision(database: Path) -> str | None:
    if not database.is_file():
        return None
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def upgrade_database(database: Path) -> str:
    status = classify_database(database)
    if status.state is not DatabaseState.CURRENT:
        raise InfrastructureDatabaseError(
            f"database_not_ready_for_milestone_migration:{status.state.value}"
        )
    revision = current_revision(database)
    if revision not in {
        None,
        M1_PROJECT_REVISION,
        M1_AUDIT_REVISION,
        M1_AUTHORITY_REVISION,
        M1_REGISTRY_REVISION,
        M1_AUTHORIZATION_INPUT_REVISION,
        M1_HANDOFF_REVISION,
        M1_IDENTITY_SCOPE_REVISION,
        M1_ROOT_MANIFEST_REVISION,
        M1_AUDIT_PROOF_MANIFEST_REVISION,
        M2_IDENTITY_CONTINUITY_REVISION,
        M2_OAUTH_SESSIONS_REVISION,
        M2_WORKSPACE_SYNC_REVISION,
        M1_CURRENT_REVISION,
    }:
        raise InfrastructureDatabaseError(f"unsupported_database_revision:{revision}")
    command.upgrade(_alembic_config(database), "head")
    upgraded = current_revision(database)
    if upgraded != M1_CURRENT_REVISION:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{upgraded or 'unstamped'}")
    return upgraded


def downgrade_database(database: Path, revision: str) -> str:
    current = current_revision(database)
    if current != M1_CURRENT_REVISION:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{current or 'unstamped'}")
    if revision not in {
        M1_PROJECT_REVISION,
        M1_AUDIT_REVISION,
        M1_AUTHORITY_REVISION,
        M1_REGISTRY_REVISION,
        M1_AUTHORIZATION_INPUT_REVISION,
        M1_HANDOFF_REVISION,
        M1_IDENTITY_SCOPE_REVISION,
        M1_ROOT_MANIFEST_REVISION,
        M1_AUDIT_PROOF_MANIFEST_REVISION,
        M2_IDENTITY_CONTINUITY_REVISION,
        M2_OAUTH_SESSIONS_REVISION,
        M2_WORKSPACE_SYNC_REVISION,
    }:
        raise InfrastructureDatabaseError(f"unsupported_downgrade_revision:{revision}")
    preflight_engine = create_engine(f"sqlite:///{database}")
    try:
        _preflight_downgrade_history(preflight_engine, revision)
    finally:
        preflight_engine.dispose()
    command.downgrade(_alembic_config(database), revision)
    downgraded = current_revision(database)
    if downgraded != revision:
        raise InfrastructureDatabaseError(f"database_revision_mismatch:{downgraded or 'unstamped'}")
    return downgraded
