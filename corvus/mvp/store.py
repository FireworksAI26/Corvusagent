from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 17

_MIGRATION_001 = """
CREATE TABLE IF NOT EXISTS mvp_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE mvp_projects (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_outcomes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, title, version)
);
CREATE TABLE mvp_workflows (
    id TEXT PRIMARY KEY,
    outcome_id TEXT NOT NULL REFERENCES mvp_outcomes(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mvp_work_items (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    item_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    cost_units INTEGER NOT NULL CHECK (cost_units >= 0),
    requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    effect_json TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_id, item_key)
);
CREATE TABLE mvp_attempts (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES mvp_work_items(id),
    worker_id TEXT NOT NULL,
    lease_fence INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE mvp_checkpoints (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    work_item_id TEXT NOT NULL REFERENCES mvp_work_items(id),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_artifacts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    work_item_id TEXT NOT NULL REFERENCES mvp_work_items(id),
    digest TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_lineage (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_conversation_entries (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    work_item_id TEXT REFERENCES mvp_work_items(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_effects (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    work_item_id TEXT NOT NULL UNIQUE REFERENCES mvp_work_items(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    binding_json TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT,
    execution_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mvp_approvals (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE REFERENCES mvp_effects(id),
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE mvp_budgets (
    project_id TEXT PRIMARY KEY REFERENCES mvp_projects(id),
    limit_units INTEGER NOT NULL CHECK (limit_units >= 0),
    reserved_units INTEGER NOT NULL CHECK (reserved_units >= 0),
    settled_units INTEGER NOT NULL CHECK (settled_units >= 0)
);
CREATE TABLE mvp_budget_reservations (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE REFERENCES mvp_effects(id),
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    amount_units INTEGER NOT NULL CHECK (amount_units >= 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE TABLE mvp_kill_switches (
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_kind, scope_id)
);
CREATE TABLE mvp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES mvp_workflows(id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX mvp_work_items_ready_idx ON mvp_work_items(workflow_id, status, item_key);
CREATE INDEX mvp_events_workflow_idx ON mvp_events(workflow_id, id);
"""

_MIGRATION_002 = """
CREATE TABLE mvp_teams (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_team_members (
    team_id TEXT NOT NULL REFERENCES mvp_teams(id),
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(team_id, principal_id)
);
CREATE TABLE mvp_provider_connections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    provider TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_credential_grants (
    id TEXT PRIMARY KEY,
    provider_connection_id TEXT NOT NULL REFERENCES mvp_provider_connections(id),
    principal_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider_connection_id, principal_id, capability)
);
CREATE TABLE mvp_oauth_flows (
    state TEXT PRIMARY KEY,
    provider_connection_id TEXT NOT NULL REFERENCES mvp_provider_connections(id),
    redirect_uri TEXT NOT NULL,
    verifier_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE mvp_autonomy_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    principal_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    mode TEXT NOT NULL,
    requested_execution INTEGER NOT NULL,
    executed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_autonomy_evidence (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES mvp_autonomy_decisions(id),
    successful INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_autonomy_policies (
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    principal_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    mode TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, principal_id, capability)
);
CREATE TABLE mvp_memory_entries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    scope TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    provenance TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, id, version)
);
CREATE TABLE mvp_skill_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name, version)
);
CREATE TABLE mvp_routines (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    name TEXT NOT NULL,
    skill_version_id TEXT NOT NULL REFERENCES mvp_skill_versions(id),
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_routine_runs (
    id TEXT PRIMARY KEY,
    routine_id TEXT NOT NULL REFERENCES mvp_routines(id),
    skill_version_id TEXT NOT NULL REFERENCES mvp_skill_versions(id),
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE mvp_envelope_actor_keys (
    actor_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE mvp_offline_intents (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    application_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mvp_channel_identities (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(provider, external_id)
);
CREATE TABLE mvp_channel_events (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    principal_id TEXT,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    processing_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, external_event_id)
);
"""

_MIGRATION_003 = """
CREATE TABLE mvp_device_flows (
    device_code TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    provider_connection_id TEXT NOT NULL REFERENCES mvp_provider_connections(id),
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    polling_interval_seconds INTEGER NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE mvp_restore_quarantine (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES mvp_projects(id),
    source_digest TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
"""

_MIGRATION_004 = """
CREATE TABLE mvp_local_users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    paired_at TEXT NOT NULL
);
"""

_MIGRATION_005 = """
CREATE TABLE mvp_local_preferences (
    user_id TEXT PRIMARY KEY REFERENCES mvp_local_users(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    default_provider TEXT NOT NULL CHECK (default_provider IN ('codex', 'claude')),
    default_model TEXT,
    default_effort TEXT NOT NULL CHECK (default_effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
    default_mode TEXT NOT NULL CHECK (default_mode IN ('chat', 'build')),
    mcp_enabled INTEGER NOT NULL CHECK (mcp_enabled IN (0, 1)),
    response_tone TEXT NOT NULL CHECK (response_tone IN ('concise', 'balanced', 'detailed')),
    custom_rules TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATION_006 = """
CREATE TABLE mvp_repositories (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    remote_slug TEXT,
    default_branch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX mvp_repositories_tenant_idx
    ON mvp_repositories(tenant_id, updated_at, id);
CREATE TABLE mvp_repository_snapshots (
    repository_id TEXT PRIMARY KEY REFERENCES mvp_repositories(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    clean INTEGER NOT NULL CHECK (clean IN (0, 1)),
    ahead INTEGER NOT NULL CHECK (ahead >= 0),
    behind INTEGER NOT NULL CHECK (behind >= 0),
    health TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);
"""

_MIGRATION_007 = """
CREATE TABLE mvp_worktree_leases (
    run_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES mvp_repositories(id) ON DELETE RESTRICT,
    root_path TEXT NOT NULL UNIQUE,
    base_sha TEXT NOT NULL,
    ownership_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    discarded_at TEXT
);
CREATE INDEX mvp_worktree_leases_repository_idx
    ON mvp_worktree_leases(repository_id, status, created_at);
"""

_MIGRATION_008 = """
CREATE TABLE mvp_contributions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES mvp_worktree_leases(run_id) ON DELETE RESTRICT,
    repository_id TEXT NOT NULL REFERENCES mvp_repositories(id) ON DELETE RESTRICT,
    branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    selected_paths_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    confirmation_digest TEXT NOT NULL,
    message TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    draft INTEGER NOT NULL CHECK (draft IN (0, 1)),
    change_digest TEXT NOT NULL,
    secret_scan_json TEXT NOT NULL,
    commit_sha TEXT,
    remote_ref TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    state TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX mvp_contributions_repository_idx
    ON mvp_contributions(repository_id, updated_at, id);
"""

_MIGRATION_009 = """
CREATE TABLE mvp_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    repository_id TEXT NOT NULL REFERENCES mvp_repositories(id) ON DELETE RESTRICT,
    base_sha TEXT NOT NULL,
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    effort TEXT NOT NULL,
    mode TEXT NOT NULL,
    safety_digest TEXT NOT NULL,
    skill_version_id TEXT,
    schedule_id TEXT,
    occurrence_key TEXT,
    output_policy TEXT NOT NULL,
    retry_of_run_id TEXT REFERENCES mvp_runs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(schedule_id, occurrence_key)
);
CREATE INDEX mvp_runs_tenant_idx ON mvp_runs(tenant_id, updated_at DESC, id);
CREATE INDEX mvp_runs_repository_idx ON mvp_runs(repository_id, updated_at DESC, id);
CREATE TABLE mvp_run_events (
    run_id TEXT NOT NULL REFERENCES mvp_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);
CREATE TABLE mvp_run_evidence (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES mvp_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX mvp_run_evidence_run_idx ON mvp_run_evidence(run_id, created_at, id);
"""

_MIGRATION_010 = """
CREATE TABLE mvp_portable_skill_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,
    package_path TEXT NOT NULL,
    status TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, name, version),
    UNIQUE(tenant_id, digest)
);
CREATE INDEX mvp_portable_skills_tenant_idx
    ON mvp_portable_skill_versions(tenant_id, name, version DESC);
"""

_MIGRATION_011 = """
CREATE TABLE mvp_schedules (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mvp_schedule_revisions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES mvp_schedules(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    repository_id TEXT NOT NULL REFERENCES mvp_repositories(id) ON DELETE RESTRICT,
    task TEXT NOT NULL,
    recurrence_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    effort TEXT NOT NULL,
    mode TEXT NOT NULL,
    safety_digest TEXT NOT NULL,
    skill_version_id TEXT,
    output_policy TEXT NOT NULL,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(schedule_id, version)
);
CREATE TABLE mvp_schedule_occurrences (
    schedule_revision_id TEXT NOT NULL REFERENCES mvp_schedule_revisions(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    run_id TEXT REFERENCES mvp_runs(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(schedule_revision_id, scheduled_for)
);
CREATE INDEX mvp_schedules_tenant_idx ON mvp_schedules(tenant_id, updated_at DESC, id);
CREATE INDEX mvp_schedule_due_idx ON mvp_schedule_revisions(next_run_at, schedule_id);
"""

_MIGRATION_012 = """
CREATE UNIQUE INDEX mvp_runs_schedule_active_idx
    ON mvp_runs(schedule_id)
    WHERE schedule_id IS NOT NULL AND status IN (
        'preparing', 'running', 'review_required', 'contribution_ready', 'publishing'
    );
"""

_MIGRATION_013 = """
ALTER TABLE mvp_schedule_occurrences ADD COLUMN reason_code TEXT;
"""

_MIGRATION_014 = """
CREATE TABLE mvp_local_chat_runs (
    run_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    provider TEXT NOT NULL,
    handle_id TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json TEXT NOT NULL,
    safety_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner, idempotency_key)
);
CREATE TABLE mvp_local_chat_events (
    run_id TEXT NOT NULL REFERENCES mvp_local_chat_runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);
CREATE INDEX mvp_local_chat_owner_idx
    ON mvp_local_chat_runs(owner, created_at DESC, run_id);
"""

_MIGRATION_015 = """
ALTER TABLE mvp_local_chat_runs ADD COLUMN artifact_json TEXT;
"""

_MIGRATION_016 = """
ALTER TABLE mvp_local_preferences
    ADD COLUMN model_labels_json TEXT NOT NULL DEFAULT '{}';
"""

_MIGRATION_017 = """
CREATE TABLE mvp_local_preferences_v17 (
    user_id TEXT PRIMARY KEY REFERENCES mvp_local_users(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    default_provider TEXT NOT NULL CHECK (default_provider IN (
        'codex', 'claude', 'openai', 'anthropic', 'gemini', 'xai', 'openrouter',
        'together', 'fireworks', 'groq', 'mistral', 'deepseek'
    )),
    default_model TEXT,
    default_effort TEXT NOT NULL CHECK (default_effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
    default_mode TEXT NOT NULL CHECK (default_mode IN ('chat', 'build')),
    mcp_enabled INTEGER NOT NULL CHECK (mcp_enabled IN (0, 1)),
    response_tone TEXT NOT NULL CHECK (response_tone IN ('concise', 'balanced', 'detailed')),
    custom_rules TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    model_labels_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO mvp_local_preferences_v17
SELECT user_id, version, default_provider, default_model, default_effort, default_mode,
       mcp_enabled, response_tone, custom_rules, updated_at, model_labels_json
FROM mvp_local_preferences;
DROP TABLE mvp_local_preferences;
ALTER TABLE mvp_local_preferences_v17 RENAME TO mvp_local_preferences;
"""

_MIGRATIONS = (
    _MIGRATION_001,
    _MIGRATION_002,
    _MIGRATION_003,
    _MIGRATION_004,
    _MIGRATION_005,
    _MIGRATION_006,
    _MIGRATION_007,
    _MIGRATION_008,
    _MIGRATION_009,
    _MIGRATION_010,
    _MIGRATION_011,
    _MIGRATION_012,
    _MIGRATION_013,
    _MIGRATION_014,
    _MIGRATION_015,
    _MIGRATION_016,
    _MIGRATION_017,
)


class StoreError(RuntimeError):
    pass


class SqliteStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        deadline = time.monotonic() + 30
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    connection.close()
                    raise
                time.sleep(0.05)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS mvp_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT version FROM mvp_schema_migrations ORDER BY version"
                ).fetchall()
                versions = {int(row["version"]) for row in rows}
                if any(version > SCHEMA_VERSION for version in versions):
                    raise StoreError("unsupported_mvp_schema_version")
                for version, migration in enumerate(_MIGRATIONS, start=1):
                    if version in versions:
                        continue
                    for statement in self._migration_statements(migration):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO mvp_schema_migrations(version, applied_at) "
                        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                        (version,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _migration_statements(script: str) -> tuple[str, ...]:
        statements: list[str] = []
        buffer = ""
        for line in script.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                if statement:
                    statements.append(statement)
                buffer = ""
        if buffer.strip():
            raise StoreError("invalid_mvp_migration")
        return tuple(statements)
