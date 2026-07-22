# Corvus Full Product Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use <code>superpowers:subagent-driven-development</code> or <code>superpowers:executing-plans</code> task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver the complete Corvus identity, synchronization, agent execution, scheduling, settings, collaboration, local/E2B runtime, and web/desktop product specified in <code>docs/superpowers/specs/2026-07-16-corvus-full-product-platform-design.md</code>.

**Architecture:** Extend the authoritative Python/FastAPI core as a modular Railway control plane backed by PostgreSQL. Web and Tauri clients consume typed HTTP/SSE contracts; paired desktop nodes and E2B implement one constrained execution-placement boundary.

**Tech Stack:** Python 3.12/3.13, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL/psycopg, Authlib, HTTPX, E2B Python SDK, React 19, TypeScript 5.9, Vite 7, openapi-fetch, TanStack Query, React Router, Zod, date-fns/rrule, Tauri 2/Rust, Vitest, Playwright, axe, pytest, Ruff, mypy, and Bandit.

## Global Constraints

- Preserve all completed M0.5-M11 behavior and existing security tests.
- Reuse <code>corvus.domain.identity.Workspace</code>/<code>WorkspaceMembership</code>, <code>corvus.domain.access.CredentialRef</code>, <code>AgentRuntimePort</code>, and <code>AgentRuntimeCoordinator</code>; do not create parallel authority, credential, approval, budget, audit, lease, or runtime systems.
- Never allow a client, profile selection, provider, local node, or E2B sandbox to manufacture authority.
- Store only opaque credential references; never put raw secrets in prompts, synchronized state, events, URLs, logs, artifacts, or audit rows.
- Authenticate Google with Authorization Code, PKCE, state, nonce, exact redirect allowlists, JWKS validation, refresh rotation, and revocation.
- Scope every repository operation and subscription by workspace/tenant.
- Require idempotency keys for mutations and explicit optimistic-version conflicts; do not silently use last-write-wins for security-relevant data.
- Preserve approval, budget, autonomy, sandbox, kill-switch, audit-chain, redaction, replay, and fencing invariants.
- Local execution may access only explicit canonical roots and local vault references.
- E2B uses secure access, pinned templates, bounded resources, scoped network/tool policy, heartbeat, and cleanup reconciliation.
- Cloud remains visibly Preview until its full lifecycle gate passes; billing remains a non-purchasing preview page.
- Preserve the Corvus visual identity and profile adaptation; do not copy T3Code chrome.
- Every visible control needs a real state transition, keyboard support, focus behavior, reduced-motion behavior, and a test.
- Commit once after each verified milestone. Push only after all seven milestones pass together.
- Open one pull request after final verification and leave it unmerged for reviewers.

## Approved Dependency Additions

Install only when the consuming milestone begins:

~~~powershell
uv add "authlib>=1.6,<2" "psycopg[binary]>=3.2,<4" "e2b>=2.3,<3" "python-dateutil>=2.9,<3" "croniter>=6,<7"
pnpm --dir apps/web add react-router-dom @tanstack/react-query zod date-fns rrule lucide-react react-markdown remark-gfm
pnpm --dir apps/web add -D @playwright/test @axe-core/playwright
~~~

Do not add Redis, a second web framework, a second ORM, or a distributed queue. PostgreSQL leases/outbox and the existing Corvus worker model are sufficient.

## Planned File Boundaries

| Area | Files and responsibility |
| --- | --- |
| Platform configuration | <code>corvus/platform/config.py</code> owns database, OAuth, public URL, E2B, and vault settings. |
| Identity | <code>corvus/domain/account.py</code> adds account/session/device types while <code>corvus/domain/identity.py</code> remains authoritative for existing workspace/membership types; services and repositories live in <code>corvus/application/identity.py</code>, <code>corvus/infrastructure/repositories/accounts.py</code>, and <code>corvus/platform/api/identity.py</code>. |
| Synchronization | <code>corvus/domain/sync.py</code>, <code>corvus/application/sync.py</code>, <code>corvus/infrastructure/repositories/sync.py</code>, <code>corvus/platform/api/sync.py</code>. |
| Conversations | <code>corvus/domain/conversations.py</code>, <code>corvus/application/conversations.py</code>, <code>corvus/infrastructure/repositories/conversations.py</code>, <code>corvus/platform/api/conversations.py</code>. |
| Providers | <code>corvus/infrastructure/agent_runtimes/{registry,process_session,codex,claude,gemini,cursor,openai_compatible}.py</code>. |
| Scheduling | <code>corvus/domain/scheduling.py</code>, <code>corvus/application/scheduling.py</code>, <code>corvus/infrastructure/repositories/scheduling.py</code>, <code>corvus/platform/worker/scheduler.py</code>, <code>corvus/platform/api/schedules.py</code>. |
| Settings/vaults | <code>corvus/domain/settings.py</code>, <code>corvus/application/settings.py</code>, <code>corvus/infrastructure/{vaults,mcp,integrations}/</code>, <code>corvus/platform/api/settings.py</code>. |
| Collaboration | <code>corvus/domain/collaboration.py</code>, <code>corvus/application/collaboration.py</code>, <code>corvus/infrastructure/repositories/collaboration.py</code>, <code>corvus/platform/api/collaboration.py</code>. |
| Runtime placement | <code>corvus/domain/runtime_placement.py</code>, <code>corvus/application/runtime_placement.py</code>, <code>corvus/infrastructure/runtime_nodes/bridge.py</code>, <code>corvus/infrastructure/e2b/runtime.py</code>, <code>corvus/platform/api/runtimes.py</code>. |
| Platform API composition | <code>corvus/platform/api/app.py</code> composes modular v2 routers; <code>corvus/mvp/api.py</code> includes that router without absorbing feature logic. |
| Web product | Focused modules under <code>apps/web/src/auth/</code>, <code>sync/</code>, <code>threads/</code>, <code>schedules/</code>, <code>settings/</code>, <code>teams/</code>, <code>runtimes/</code>, and <code>components/</code>; <code>App.tsx</code> remains composition only. |
| Desktop | Focused Rust modules under <code>apps/desktop/src-tauri/src/</code>; <code>lib.rs</code> remains composition only. |

## Execution Protocol

For every task: write the stated failing test, run the focused command and observe failure, implement the smallest complete behavior, rerun the focused test, then run the milestone suite. Mark checkboxes only after fresh evidence. Subagents may implement bounded non-overlapping tasks, but the root agent owns integration, security review, plan tracking, and milestone commits.

## Hackathon fast-track delivery (2026-07-17)

- [x] Replace permanent secondary rails with one fixed sidebar, one main canvas, on-demand history, icon-first actions, responsive navigation, and an overlay-only inspector.
- [x] Connect installed local Codex and Claude CLIs through bounded fixed-argv adapters and publish a truthful provider/model catalog; keep Gemini/xAI Preview and Cursor unavailable.
- [x] Stream messages, provider-supplied safe reasoning summaries, generic tool/work status, usage, terminal state, and cancellation through the paired owner-scoped API.
- [x] Add Chat/Build, model, thinking, and explicit MCP controls to the composer; fail closed when provider discovery cannot verify a ready runtime.
- [x] Run Codex Build inside a fresh workspace-write scratch sandbox and return a bounded ZIP plus SHA-256 manifest only after completion.
- [x] Preserve the four Everyday/Developer and Individual/Team identities in the hosted and local desktop shells, with profile changes routed through Settings/onboarding authority.
- [ ] Complete the final consolidated Python, web, Rust, security, packaging, and GUI acceptance gates; then push one branch and open one unmerged PR.

This fast track is the functional hackathon surface, not a claim that every later full-platform item below is complete. Durable v2 conversation APIs, production multi-user collaboration, real E2B lifecycle, API-key providers, timed scheduler workers, and full cross-device conversation persistence remain on their original milestone boundaries.

---

## Milestone 1 — Google Identity and Cross-Device Continuity

### Task 1.1: Produce and approve the frontend design packet

**Files:**
- Create the required packet under <code>.antigravity/website-blueprint/</code>: <code>PROJECT_CONTEXT.md</code>, <code>REFERENCE_RESEARCH.md</code>, <code>SOURCE_MANIFEST.json</code>, <code>COMPONENT_ADOPTION_MAP.json</code>, <code>FRONTEND_CRAFT_BRIEF.md</code>, <code>EXPERIENCE_STORYBOARD.json</code>, <code>INTERACTION_SPEC.json</code>, <code>VISUAL_TARGET.md</code>, <code>SECTION_PLAN.json</code>, <code>VISUAL_COMPOSITION_DRAFT.md</code>, <code>DESIGN_SCORECARD_CRITERIA.json</code>, and <code>CHANGE_PLAN.md</code>.

**Interfaces:**
- Consumes: approved design and current Vercel app.
- Produces: approved selectors, interactions, source hooks, responsive targets, and change boundary for every frontend task.

- [x] Run intake and source-backed research:

~~~powershell
ag design intake --project-root . --goal "Complete Corvus identity-backed agent platform" --audience "Everyday and developer individuals and teams" --conversion "Sign in and start a governed agent thread"
ag design research --project-root . --business-source "https://github.com/aGamingGod1234/corvus-platform" --visual-ref "https://github.com/pingdotgg/t3code" --conversion-ref "https://chatgpt.com/" --component "shadcn:button" --icon "lucide:send"
ag design target --project-root .
ag design plan --project-root .
ag design compose --project-root .
~~~

- [x] Lock the allowed frontend change surface and baseline:

~~~powershell
ag design change-plan --project-root . --allow "apps/web/src/**" --allow "apps/desktop/src-tauri/src/**" --forbid "corvus/domain/**"
ag design snapshot-before --project-root . --url "http://127.0.0.1:5173"
ag design verify-packet --project-root .
ag design approve-packet --project-root . --approved-by "Lucas" --approval-note "Approved seven-section Corvus product design in the 2026-07-16 Codex thread"
~~~

- [x] Verify the packet reports no missing artifact, weak source, stale approval hash, uncited section, or uncited interaction.

### Task 1.2: Add hosted configuration and PostgreSQL support

**Files:**
- Create: <code>corvus/platform/__init__.py</code>, <code>corvus/platform/config.py</code>, <code>compose.platform-test.yaml</code>
- Modify: <code>corvus/infrastructure/db.py</code>, <code>corvus/infrastructure/migrations/env.py</code>
- Test: <code>tests/unit/platform/test_config.py</code>, <code>tests/integration/test_postgres_database.py</code>

**Interfaces:**
- Produces: <code>PlatformSettings.from_env() -&gt; PlatformSettings</code>, <code>create_platform_engine(database_url: str) -&gt; Engine</code>, and URL-based Alembic upgrades.

- [x] Write a failing test that rejects reused signing secrets and verifies PostgreSQL URL handling:

~~~python
def test_platform_settings_require_distinct_secrets(monkeypatch):
    monkeypatch.setenv("CORVUS_DATABASE_URL", "postgresql+psycopg://corvus:test@localhost/corvus")
    monkeypatch.setenv("CORVUS_SESSION_SECRET", "x" * 48)
    monkeypatch.setenv("CORVUS_OAUTH_STATE_SECRET", "x" * 48)
    with pytest.raises(ValueError, match="secrets_must_be_distinct"):
        PlatformSettings.from_env()
~~~

- [x] Run <code>uv run --python 3.13 pytest tests/unit/platform/test_config.py tests/integration/test_postgres_database.py -q</code>; confirm missing imports fail.
- [x] Implement immutable settings, URL-based engine creation, redacted configuration errors, PostgreSQL-aware Alembic configuration, and the test compose service.
- [x] Rerun focused tests and <code>uv run --python 3.13 mypy corvus/platform corvus/infrastructure/db.py</code>; expect success.

### Task 1.3: Persist accounts, workspaces, memberships, sessions, and devices

**Files:**
- Create: <code>corvus/domain/account.py</code>, <code>corvus/application/identity.py</code>, <code>corvus/infrastructure/repositories/accounts.py</code>, <code>corvus/infrastructure/migrations/versions/m2_001_identity_continuity.py</code>
- Modify: <code>corvus/domain/identity.py</code>, <code>corvus/infrastructure/repositories/identity_scope.py</code>
- Test: <code>tests/unit/domain/test_account.py</code>, <code>tests/integration/test_account_repository.py</code>

**Interfaces:**
- Produces: <code>Account</code>, <code>ExternalIdentity</code>, <code>DeviceRegistration</code>, <code>SessionRecord</code>, and <code>IdentityService.complete_google_identity()</code>; reuses existing <code>Workspace</code> and <code>WorkspaceMembership</code> as the only workspace authority types.

- [x] Write failing tests for verified-email linking, unique issuer/subject, independent experience/workspace kind, membership capabilities, session rotation, device revocation, and cross-tenant denial.
- [x] Run both focused files and confirm missing models/migration failures.
- [x] Implement frozen account/session/device models, extend the existing workspace/membership repository contract, add constraints/indexes, and create the reversible migration without parallel authority types.
- [x] Upgrade/downgrade/upgrade fresh SQLite and PostgreSQL databases, then rerun the tests.

### Task 1.4: Implement Google OAuth and durable sessions

**Files:**
- Create: <code>corvus/infrastructure/oauth/__init__.py</code>, <code>corvus/infrastructure/oauth/google.py</code>, <code>corvus/application/oauth.py</code>, <code>corvus/platform/api/__init__.py</code>, <code>corvus/platform/api/app.py</code>, <code>corvus/platform/api/dependencies.py</code>, <code>corvus/platform/api/identity.py</code>
- Modify: <code>corvus/mvp/api.py</code>
- Test: <code>tests/security/test_google_oauth.py</code>, <code>tests/integration/test_identity_api.py</code>

**Interfaces:**
- Produces: <code>GoogleOAuthClient.start(redirect_uri) -&gt; OAuthStart</code>, <code>exchange(callback) -&gt; VerifiedIdentity</code>, plus auth/session, onboarding, workspace, and device routes: <code>GET /api/v2/auth/google/start</code>, <code>GET /api/v2/auth/google/callback</code>, <code>GET /api/v2/session</code>, <code>POST /api/v2/session/refresh</code>, <code>POST /api/v2/logout</code>, <code>GET|PUT /api/v2/onboarding</code>, <code>GET|POST /api/v2/workspaces</code>, <code>GET|PATCH /api/v2/workspaces/{workspace_id}</code>, and <code>GET|POST|DELETE /api/v2/devices</code>.

- [x] Write failing tests for PKCE/state/nonce, redirect allowlists, issuer/audience, one-time callback use, account-linking rejection, cookie flags, rotation, revocation, and redaction.
- [x] Run the two focused files; expect route/import failures.
- [x] Implement Authlib exchange behind an HTTP transport port, transaction digests, durable sessions, CSRF binding, compose the modular v2 router in <code>corvus/platform/api/app.py</code>, and include it once from the legacy app factory.
- [x] Run focused tests, Bandit on new modules, and mypy; expect success.

### Task 1.5: Implement ordered workspace synchronization

**Files:**
- Create: <code>corvus/domain/sync.py</code>, <code>corvus/application/sync.py</code>, <code>corvus/infrastructure/repositories/sync.py</code>, <code>corvus/infrastructure/migrations/versions/m2_002_workspace_sync.py</code>, <code>corvus/platform/api/sync.py</code>
- Test: <code>tests/unit/domain/test_sync.py</code>, <code>tests/integration/test_sync_repository.py</code>, <code>tests/security/test_sync_replay.py</code>

**Interfaces:**
- Produces: <code>SyncMutation</code>, <code>SyncPage</code>, <code>SyncService.apply()</code>, <code>GET /api/v2/workspaces/{workspace_id}/sync</code>, and <code>POST /api/v2/workspaces/{workspace_id}/sync/mutations</code>.

- [x] Write failing tests for monotonic cursors, idempotent replay, explicit version conflict, resync boundary, tenant isolation, and recursive secret rejection.
- [x] Run the focused files and confirm missing contracts.
- [x] Implement append-only changes/outbox, acknowledgements, canonical payload hashing, 409 conflict bodies, and tenant-scoped transactions.
- [x] Rerun focused tests and both database repository contracts.

### Task 1.6: Put Google first in onboarding and synchronize the profile

**Files:**
- Create: <code>apps/web/src/auth/AuthProvider.tsx</code>, <code>apps/web/src/auth/authApi.ts</code>, <code>apps/web/src/sync/SyncProvider.tsx</code>
- Modify: <code>apps/web/src/app/OnboardingFlow.tsx</code>, <code>apps/web/src/app/preferences.ts</code>, <code>apps/web/src/app/workspaceProfiles.ts</code>, <code>apps/web/src/components/WorkspaceSwitcher.tsx</code>
- Test: <code>apps/web/src/auth/AuthProvider.test.tsx</code>, <code>apps/web/src/app/OnboardingFlow.test.tsx</code>, <code>apps/web/src/sync/SyncProvider.test.tsx</code>

**Interfaces:**
- Produces: <code>useAuth() -&gt; AuthState</code>, <code>useWorkspaceSync() -&gt; WorkspaceSyncState</code>, server-backed profile choices, and read-only <code>WorkspaceIdentityBlock</code>.

- [ ] Write failing Vitest journeys proving Google precedes profile selection, legacy personal storage migrates to individual, API persistence works, and top-left controls cannot switch identity.
- [ ] Run <code>pnpm --dir apps/web test -- --run src/auth src/app/OnboardingFlow.test.tsx src/sync</code>; expect failures.
- [ ] Implement authenticated boot, resumable onboarding, offline/retry views, and server sync without OAuth material in localStorage.
- [ ] Run focused tests and <code>pnpm --dir apps/web build</code>; expect success.

### Milestone 1 Gate and Commit

- [ ] Run identity/sync/security tests on SQLite and PostgreSQL.
- [ ] Run all web tests and build.
- [ ] Run Ruff check/format, mypy, Bandit, and <code>git diff --check</code>.
- [ ] Update <code>PROJECT_LOG.md</code>, <code>HACKATHON_STATUS.md</code>, OpenAPI output, generated TypeScript, and these checkboxes.
- [ ] Commit: <code>feat(identity): add Google-backed workspace continuity</code>.

---

## Milestone 2 — Threads and Live Agent Execution

### Task 2.1: Persist threads, messages, attachments, runs, events, and artifacts

**Files:**
- Create: <code>corvus/domain/conversations.py</code>, <code>corvus/application/conversations.py</code>, <code>corvus/infrastructure/repositories/conversations.py</code>, <code>corvus/infrastructure/migrations/versions/m2_003_conversations.py</code>
- Test: <code>tests/unit/domain/test_conversations.py</code>, <code>tests/integration/test_conversation_repository.py</code>

**Interfaces:**
- Produces: <code>Thread</code>, <code>Message</code>, <code>AttachmentRef</code>, <code>AgentRunRecord</code>, <code>RunEventRecord</code>, <code>RunArtifact</code>, and <code>ConversationService</code>.

- [ ] Write failing tests for append-only message ordering, immutable author/workspace binding, attachment digest validation, run idempotency, ordered event cursor, artifact lineage, and cross-tenant denial.
- [ ] Run the focused files and confirm missing models/migration failures.
- [ ] Implement domain validation, repository transactions, unique/idempotency constraints, indexes, retention-safe cursors, and reversible migration.
- [ ] Run focused tests against SQLite and PostgreSQL.

### Task 2.2: Add provider-neutral registry and process sessions

**Files:**
- Create: <code>corvus/infrastructure/agent_runtimes/registry.py</code>, <code>corvus/infrastructure/agent_runtimes/process_session.py</code>
- Modify: <code>corvus/application/ports.py</code>, <code>corvus/application/agent_runtime.py</code>
- Test: <code>tests/unit/infrastructure/test_provider_registry.py</code>, <code>tests/unit/infrastructure/test_process_session.py</code>

**Interfaces:**
- Produces: <code>ProviderAdapterFactory</code>, <code>ProviderRegistry.discover()</code>, <code>ProcessSession.start()</code>, <code>ProcessSession.events()</code>, <code>ProcessSession.cancel()</code>, and <code>ProcessSession.resume()</code>.

- [ ] Write failing tests for deterministic discovery order, duplicate provider rejection, capability fail-closed behavior, bounded stdout/stderr, process-tree cancellation, invalid UTF-8, malformed JSON frames, and secret redaction.
- [ ] Run the focused tests and confirm missing adapters.
- [ ] Implement immutable registry entries and one bounded, interruptible process-session abstraction using the existing safe-process utilities.
- [ ] Run focused tests on Windows and the existing Linux/macOS CI matrix.

### Task 2.3: Implement local CLI adapters

**Files:**
- Create: <code>corvus/infrastructure/agent_runtimes/codex.py</code>, <code>claude.py</code>, <code>gemini.py</code>, <code>cursor.py</code>
- Test: <code>tests/contract/providers/test_local_cli_adapters.py</code>
- Test fixtures: <code>tests/fixtures/providers/codex/</code>, <code>tests/fixtures/providers/claude/</code>, <code>tests/fixtures/providers/gemini/</code>, <code>tests/fixtures/providers/cursor/</code>

**Interfaces:**
- Each adapter implements the existing <code>AgentRuntimePort</code> and returns normalized <code>AgentRunEvent</code> values.

- [ ] Capture sanitized fixture streams from installed/help-documented CLIs and write failing parameterized conformance tests for discovery, health, start, streaming, cancellation, and unsupported resume.
- [ ] Run <code>uv run --python 3.13 pytest tests/contract/providers/test_local_cli_adapters.py -q</code>; expect adapter import failures.
- [ ] Implement typed command builders, environment allowlists, cwd/root checks, event parsers, and normalized reason codes. Never pass shell-composed command strings.
- [ ] Rerun fixture tests; run live smoke probes only for installed CLIs and assert skipped capability instead of false success when absent.

### Task 2.4: Implement API provider adapters

**Files:**
- Create: <code>corvus/infrastructure/agent_runtimes/openai_compatible.py</code>, <code>anthropic.py</code>, <code>google.py</code>, <code>xai.py</code>
- Test: <code>tests/contract/providers/test_api_adapters.py</code>
- Test: <code>tests/security/test_provider_stream_limits.py</code>

**Interfaces:**
- Produces streaming adapters for OpenAI/Codex-compatible, OpenRouter-compatible, Anthropic, Gemini, and xAI/Grok endpoints through injected <code>httpx.AsyncClient</code> transports.

- [ ] Write failing MockTransport tests for request mapping, SSE framing, usage accounting, tool calls, retry classification, provider request IDs, cancellation, oversized frames, and redaction.
- [ ] Run the focused files and confirm missing adapters.
- [ ] Implement explicit endpoint allowlists, timeouts, response-size limits, bounded retries, schema validation, and provider-neutral events. Resolve credential references at send time only.
- [ ] Rerun focused tests, Bandit, and mypy.

### Task 2.5: Expose conversation and run APIs with resumable SSE

**Files:**
- Create: <code>corvus/platform/api/conversations.py</code>, <code>corvus/platform/api/agent_runs.py</code>
- Modify: <code>corvus/mvp/api.py</code>, <code>corvus/mvp/openapi.py</code>
- Test: <code>tests/integration/test_conversation_api.py</code>, <code>tests/security/test_agent_run_api.py</code>

**Interfaces:**
- Produces routes for thread CRUD, message append, run start/cancel/retry/fork/continue, approvals, artifacts, provider catalog, and <code>GET /api/v2/agent-runs/{run_id}/events</code> with opaque replay cursor.

- [ ] Write failing API tests for CSRF, idempotency, workspace scope, stale authority, cursor replay, disconnect/resume, event authorization, cancellation races, and stable error envelopes.
- [ ] Run the focused files and confirm 404 failures.
- [ ] Implement thin routers over <code>ConversationService</code> and <code>AgentRuntimeCoordinator</code>; authorize every SSE event before emission.
- [ ] Regenerate <code>openapi/corvus-mvp.json</code> and <code>apps/web/src/generated/api.ts</code>; rerun contract tests.

### Task 2.6: Build the real thread list, conversation, composer, and run inspector

**Files:**
- Create: <code>apps/web/src/threads/ThreadLayout.tsx</code>, <code>ThreadList.tsx</code>, <code>Conversation.tsx</code>, <code>MessageView.tsx</code>, <code>Composer.tsx</code>, <code>RunFlightpath.tsx</code>, <code>RunInspector.tsx</code>, <code>ApprovalsPanel.tsx</code>, <code>threadApi.ts</code>, <code>useRunEvents.ts</code>
- Modify: <code>apps/web/src/App.tsx</code>, <code>apps/web/src/app/AppShell.tsx</code>, <code>apps/web/src/app/WorkspaceRouter.tsx</code>
- Test: <code>apps/web/src/threads/ThreadLayout.test.tsx</code>, <code>Composer.test.tsx</code>, <code>useRunEvents.test.ts</code>

**Interfaces:**
- Produces routes <code>/new</code>, <code>/threads/:threadId</code>, persistent composer controls, ordered flightpath, and profile-adaptive inspector.

- [ ] Write failing tests for new thread, send/stop, attachment validation, model/provider/runtime selection, slash commands, approval focus restoration, SSE reconnect, and Everyday versus Developer detail.
- [ ] Run the focused Vitest files and confirm missing component failures.
- [ ] Implement React Router/TanStack Query composition, optimistic message append with idempotency, accessible streaming announcements, responsive inspector, and real empty/error/retry states.
- [ ] Run focused tests, all web tests, and production build.

### Milestone 2 Gate and Commit

- [ ] Run provider conformance, conversation repository/API, agent runtime, security, and all web tests.
- [ ] Verify a local simulator thread end-to-end in web and Tauri with start, stream, approval, cancel, retry, and artifact.
- [ ] Run Ruff, mypy, Bandit, Rust tests, web build, and diff check.
- [ ] Update logs/status/OpenAPI/generated types/plan checkboxes.
- [ ] Commit: <code>feat(agents): add threads and live provider execution</code>.

---

## Milestone 3 — Scheduling and Unattended Execution

### Task 3.1: Add schedule domain, recurrence, and persistence

**Files:**
- Create: <code>corvus/domain/scheduling.py</code>, <code>corvus/application/scheduling.py</code>, <code>corvus/infrastructure/repositories/scheduling.py</code>, <code>corvus/infrastructure/migrations/versions/m2_004_scheduling.py</code>
- Test: <code>tests/unit/domain/test_scheduling.py</code>, <code>tests/integration/test_scheduling_repository.py</code>

**Interfaces:**
- Produces: <code>Schedule</code>, <code>RecurrenceRule</code>, <code>MissedRunPolicy</code>, <code>ScheduleExecution</code>, <code>ExecutionClaim</code>, and <code>ScheduleService</code>.

- [ ] Write failing clock-controlled tests for once/interval/cron/event schedules, timezone/DST transitions, immutable authorization snapshot, bounded catch-up, version conflicts, pause/resume, and tenant scope.
- [ ] Run focused tests and confirm missing contracts.
- [ ] Implement recurrence calculation, repository, indexes, reversible migration, and default grace-window-then-skip policy.
- [ ] Rerun tests against SQLite and PostgreSQL.

### Task 3.2: Implement the fenced scheduler worker

**Files:**
- Create: <code>corvus/platform/worker/__init__.py</code>, <code>corvus/platform/worker/scheduler.py</code>
- Modify: <code>corvus/cli.py</code>
- Test: <code>tests/integration/test_scheduler_worker.py</code>, <code>tests/security/test_scheduler_fencing.py</code>

**Interfaces:**
- Produces: <code>SchedulerWorker.tick(now)</code>, lease renewal, fenced completion, retry calculation, and CLI command <code>corvus platform-worker</code>.

- [ ] Write failing tests for two-worker claims, stale fencing rejection, restart recovery, bounded retry, successful-effect deduplication, offline local node, expired authority, and kill switch.
- [ ] Run focused tests and confirm missing worker.
- [ ] Implement transactional claim/heartbeat/complete/fail methods and dispatch through the existing authoritative coordinator.
- [ ] Rerun focused tests including forced process interruption.

### Task 3.3: Add schedule APIs and idempotent notifications

**Files:**
- Create: <code>corvus/platform/api/schedules.py</code>, <code>corvus/domain/notifications.py</code>, <code>corvus/application/notifications.py</code>, <code>corvus/infrastructure/repositories/notifications.py</code>
- Test: <code>tests/integration/test_schedule_api.py</code>, <code>tests/integration/test_notifications.py</code>

**Interfaces:**
- Produces schedule list/create/update/pause/resume/run-now/history routes and <code>NotificationService.deliver_once(delivery)</code>.

- [ ] Write failing API tests for recurrence preview, expected versions, runtime readiness, authorization, run-now idempotency, history privacy, and notification deduplication.
- [ ] Run focused tests and confirm missing routes.
- [ ] Implement routes, notification outbox consumption, in-app delivery, and signed webhook/email adapter ports.
- [ ] Rerun tests and regenerate OpenAPI/client types.

### Task 3.4: Build schedules UI and desktop background status

**Files:**
- Create: <code>apps/web/src/schedules/SchedulesPage.tsx</code>, <code>ScheduleList.tsx</code>, <code>ScheduleCalendar.tsx</code>, <code>ScheduleEditor.tsx</code>, <code>ScheduleHistory.tsx</code>, <code>scheduleApi.ts</code>
- Create: <code>apps/desktop/src-tauri/src/background.rs</code>
- Modify: <code>apps/desktop/src-tauri/src/lib.rs</code>
- Test: <code>apps/web/src/schedules/SchedulesPage.test.tsx</code>, <code>ScheduleEditor.test.tsx</code>
- Test: Rust unit tests in <code>background.rs</code>

**Interfaces:**
- Produces <code>/schedules</code>, list/calendar modes, recurrence preview, runtime warning, recovery actions, and tray/background status model.

- [ ] Write failing tests for create/edit/pause/run-now, timezone preview, validation, offline node warnings, failure recovery, keyboard operation, and profile language.
- [ ] Write failing Rust tests for sanitized tray state, worker lifecycle, and shutdown.
- [ ] Implement data-backed UI and Tauri background-status commands without exposing credentials or opening a listener.
- [ ] Run web/Rust focused tests and builds.

### Milestone 3 Gate and Commit

- [ ] Run scheduler/domain/repository/API/security tests with frozen clocks and PostgreSQL.
- [ ] Run web/Rust tests and simulate restart, DST, offline-node, retry, cancellation, and notification cases.
- [ ] Run repository quality gates and diff check.
- [ ] Update logs/status/OpenAPI/generated types/plan checkboxes.
- [ ] Commit: <code>feat(scheduling): add unattended agent execution</code>.

---

## Milestone 4 — Settings, Customization, MCP, and Integrations

### Task 4.1: Add typed settings with explicit scope and precedence

**Files:**
- Create: <code>corvus/domain/settings.py</code>, <code>corvus/application/settings.py</code>, <code>corvus/infrastructure/repositories/settings.py</code>, <code>corvus/infrastructure/migrations/versions/m2_005_settings.py</code>
- Test: <code>tests/unit/domain/test_settings.py</code>, <code>tests/integration/test_settings_repository.py</code>

**Interfaces:**
- Produces: <code>SettingScope</code>, <code>SettingDocument</code>, <code>EffectiveSettings</code>, <code>SettingsService.resolve()</code>, and versioned typed schemas for account, user, device, workspace, thread, and team policy.

- [ ] Write failing tests for schema rejection, account/user/device/workspace/thread precedence, enforced team-policy ceilings, version conflict, reset, migration, and tenant isolation.
- [ ] Run focused tests and confirm missing types.
- [ ] Implement discriminated Pydantic setting documents, repository/version history, deterministic merge rules, policy explanations, and reversible migration.
- [ ] Rerun focused tests against SQLite/PostgreSQL and mypy.

### Task 4.2: Implement local and hosted secret vault boundaries

**Files:**
- Create: <code>corvus/application/secret_vault.py</code>, <code>corvus/infrastructure/vaults/__init__.py</code>, <code>keyring.py</code>, <code>encrypted_database.py</code>
- Modify: <code>corvus/security.py</code>
- Test: <code>tests/unit/infrastructure/test_secret_vaults.py</code>, <code>tests/security/test_vault_boundary.py</code>

**Interfaces:**
- Produces: <code>SecretVaultPort.put(scope, value) -&gt; CredentialRef</code>, <code>resolve(ref, effect_context) -&gt; SecretValue</code>, <code>revoke(ref)</code>, and implementations for OS keyring and encrypted hosted storage.

- [ ] Write failing tests proving ciphertext differs across writes, vault keys are configuration-only, references are scope-bound, plaintext is absent from database/events/logs/exceptions, and revocation fails closed.
- [ ] Run focused tests and confirm missing vault interfaces.
- [ ] Implement keyring storage and authenticated encryption using existing cryptography primitives with rotation metadata and zero secret serialization.
- [ ] Run focused tests, Bandit, repository secret-flow tests, and a recursive artifact/log redaction scan.

### Task 4.3: Implement MCP registration, health, and tool authorization

**Files:**
- Create: <code>corvus/domain/mcp.py</code>, <code>corvus/application/mcp.py</code>, <code>corvus/infrastructure/mcp/__init__.py</code>, <code>stdio.py</code>, <code>http.py</code>
- Test: <code>tests/contract/test_mcp_transports.py</code>, <code>tests/security/test_mcp_permissions.py</code>

**Interfaces:**
- Produces: <code>McpServerDefinition</code>, <code>McpToolCapability</code>, <code>McpTransportPort</code>, <code>McpService.health()</code>, and <code>McpService.invoke_authorized()</code>.

- [ ] Write failing transport fixtures for initialization, capability discovery, health, cancellation, malformed messages, oversized frames, remote redirects, and disconnected servers.
- [ ] Write failing security tests for environment reference resolution, tool allow/deny, elevated approval, argument redaction, workspace scope, and server identity changes.
- [ ] Implement bounded JSON-RPC stdio and allowlisted HTTPS streaming transports; keep invalid servers disabled and route every invocation through current authorization/audit.
- [ ] Run contract/security tests on Windows and CI platforms.

### Task 4.4: Implement integration connections and signed deliveries

**Files:**
- Create: <code>corvus/domain/integrations.py</code>, <code>corvus/application/integrations.py</code>, <code>corvus/infrastructure/integrations/__init__.py</code>, <code>github.py</code>, <code>webhook.py</code>, <code>email.py</code>, <code>google.py</code>
- Test: <code>tests/contract/test_integrations.py</code>, <code>tests/security/test_integration_signatures.py</code>

**Interfaces:**
- Produces: <code>IntegrationConnection</code>, <code>IntegrationHealth</code>, <code>IntegrationPort.test()</code>, <code>sync()</code>, <code>deliver()</code>, <code>revoke()</code>, and adapters for GitHub, signed generic/Slack/Discord-compatible webhooks, SMTP/API email, and Google Calendar/Drive contracts.

- [ ] Write failing MockTransport tests for connect/status/test/reconnect/revoke, pagination, rate limits, retry classification, signature generation/verification, replay windows, and redaction.
- [ ] Run focused tests and confirm missing contracts.
- [ ] Implement injected transports, vault-backed credentials, outbound idempotency, inbound nonce/timestamp replay checks, health timestamps, and stable errors.
- [ ] Rerun contract/security tests and mypy.

### Task 4.5: Expose settings, vault, MCP, and integration APIs

**Files:**
- Create: <code>corvus/platform/api/settings.py</code>, <code>vaults.py</code>, <code>mcp.py</code>, <code>integrations.py</code>
- Test: <code>tests/integration/test_settings_api.py</code>

**Interfaces:**
- Produces typed CRUD/test/revoke routes with expected versions, scope metadata, policy explanations, and credential write-only inputs.

- [ ] Write failing API tests for every scope, write-only secret responses, MCP health/invoke authorization, integration test/revoke, CSRF, idempotency, and cross-tenant denial.
- [ ] Run the focused file and confirm route failures.
- [ ] Implement thin routers over services; never echo submitted credential material.
- [ ] Regenerate OpenAPI/client types and rerun API/security tests.

### Task 4.6: Build the complete Settings product

**Files:**
- Create: <code>apps/web/src/settings/SettingsLayout.tsx</code>, <code>AccountSettings.tsx</code>, <code>AppearanceSettings.tsx</code>, <code>BehaviorSettings.tsx</code>, <code>RulesSettings.tsx</code>, <code>ProviderSettings.tsx</code>, <code>RuntimeSettings.tsx</code>, <code>McpSettings.tsx</code>, <code>IntegrationSettings.tsx</code>, <code>NotificationSettings.tsx</code>, <code>PrivacySettings.tsx</code>, <code>TeamPolicySettings.tsx</code>, <code>BillingPreview.tsx</code>, <code>settingsApi.ts</code>
- Modify: <code>apps/web/src/app/AppShell.tsx</code>, <code>apps/web/src/styles.css</code>
- Test: <code>apps/web/src/settings/SettingsLayout.test.tsx</code>, <code>ProviderSettings.test.tsx</code>, <code>McpSettings.test.tsx</code>

**Interfaces:**
- Produces searchable <code>/settings/*</code> routes, live previews, scope labels, validation, unsaved-change guard, reset, policy explanation, and synchronized status.

- [ ] Write failing tests for navigation/search, theme/reduced-motion preview, behavior sample, custom rules, write-only credential form, provider test, MCP health/permissions, integration revoke, export/delete confirmation, and policy-disabled controls.
- [ ] Run focused Vitest files and confirm missing UI.
- [ ] Implement data-backed forms with Zod validation, accessible error summaries, focus restoration, query invalidation, and no fake controls.
- [ ] Run all web tests, axe component checks, and production build.

### Milestone 4 Gate and Commit

- [ ] Run settings/vault/MCP/integration unit, contract, API, and security suites.
- [ ] Prove recursive secret absence across SQLite/PostgreSQL rows, events, logs, API responses, localStorage, and artifacts.
- [ ] Run all web tests/build and repository quality gates.
- [ ] Update logs/status/OpenAPI/generated types/plan checkboxes.
- [ ] Commit: <code>feat(settings): add customization tools and integrations</code>.

---

## Milestone 5 — Teams and Collaboration

### Task 5.1: Persist organizations, memberships, invitations, and roles

**Files:**
- Create: <code>corvus/domain/collaboration.py</code>, <code>corvus/infrastructure/repositories/collaboration.py</code>, <code>corvus/infrastructure/migrations/versions/m2_006_collaboration.py</code>
- Test: <code>tests/unit/domain/test_collaboration.py</code>, <code>tests/integration/test_collaboration_repository.py</code>

**Interfaces:**
- Produces: <code>Organization</code>, <code>WorkspaceMembership</code>, <code>Invitation</code>, <code>WorkspaceRole</code>, <code>PolicyBinding</code>, and tenant-scoped repository methods.

- [ ] Write failing tests for Owner/Admin/Manager/Member/Viewer capabilities, single-use invitation digest, expiry, resend/revoke, last-owner protection, membership versioning, and tenant isolation.
- [ ] Run focused tests and confirm missing models/migration failures.
- [ ] Implement frozen models, explicit capability sets, repository constraints/indexes, invitation hashing, and reversible migration.
- [ ] Rerun tests on SQLite/PostgreSQL.

### Task 5.2: Add authoritative collaboration authorization

**Files:**
- Create: <code>corvus/application/collaboration_authorization.py</code>
- Modify: <code>corvus/application/authorization.py</code>, <code>corvus/domain/access.py</code>
- Test: <code>tests/security/test_collaboration_authorization.py</code>

**Interfaces:**
- Produces <code>CollaborationAuthorizationService.authorize(principal, workspace, capability, resource_version)</code> and revocation propagation.

- [ ] Write failing tests for profile-selection non-authority, role downgrade during active run, removed-member session/subscription/approval revocation, stale cached capability, policy ceiling, and cross-tenant resource IDs.
- [ ] Run the focused file and confirm missing service.
- [ ] Implement capability derivation only from current memberships/policies and bind decisions to authority snapshots consumed by existing run authorization.
- [ ] Rerun security and existing authorization suites.

### Task 5.3: Implement assignments, comments, approvals, presence, and activity

**Files:**
- Create: <code>corvus/application/collaboration.py</code>, <code>corvus/platform/api/collaboration.py</code>
- Extend: <code>corvus/domain/collaboration.py</code>, <code>corvus/infrastructure/repositories/collaboration.py</code>
- Test: <code>tests/integration/test_collaboration_api.py</code>, <code>tests/security/test_collaboration_privacy.py</code>

**Interfaces:**
- Produces organization/workspace/member/invitation routes plus <code>Assignment</code>, <code>Comment</code>, <code>Mention</code>, <code>Watcher</code>, <code>PresenceSession</code>, <code>ActivityEvent</code>, and approval inbox queries.

- [ ] Write failing tests for assignment ownership, comment/mention visibility, private drafts, watcher notifications, approval capability, presence expiry, activity audit linkage, cursor ordering, and removed-member disconnect.
- [ ] Run focused files and confirm missing routes.
- [ ] Implement services, outbox-backed notifications, short-lived presence, tenant-filtered SSE, and immutable activity/audit linkage.
- [ ] Regenerate OpenAPI/types and rerun integration/security tests.

### Task 5.4: Build team administration and collaboration views

**Files:**
- Create: <code>apps/web/src/teams/TeamLayout.tsx</code>, <code>MembersPage.tsx</code>, <code>InviteDialog.tsx</code>, <code>AssignmentsPage.tsx</code>, <code>ApprovalInbox.tsx</code>, <code>ActivityFeed.tsx</code>, <code>Presence.tsx</code>, <code>teamApi.ts</code>
- Create: <code>apps/web/src/components/Inbox.tsx</code>
- Modify: <code>apps/web/src/app/workspaceProfiles.ts</code>, <code>apps/web/src/app/WorkspaceRouter.tsx</code>
- Test: <code>apps/web/src/teams/TeamLayout.test.tsx</code>, <code>ApprovalInbox.test.tsx</code>, <code>apps/web/src/components/Inbox.test.tsx</code>

**Interfaces:**
- Produces team routes, member/invite/role controls, assignments, comments, approvals, activity, presence, and universal Inbox.

- [ ] Write failing tests for role-based visibility, invite lifecycle, disabled unauthorized actions, assignment/comment/mention flows, approval decisions, removed-member handling, and Everyday/Developer wording.
- [ ] Run focused Vitest files and confirm missing UI.
- [ ] Implement responsive data-backed views; always revalidate mutations server-side and show stable denial/recovery states.
- [ ] Run all web tests, axe checks, and build.

### Milestone 5 Gate and Commit

- [ ] Run collaboration repository/API/security tests with a cross-tenant attack matrix.
- [ ] Run all web tests and four-profile team journeys.
- [ ] Verify membership removal terminates sessions, SSE, pending approvals, and runtime authority.
- [ ] Run repository quality gates and update logs/status/OpenAPI/types/plan.
- [ ] Commit: <code>feat(teams): add governed collaboration workspaces</code>.

---

## Milestone 6 — Paired Local Nodes and E2B Cloud Runtime

### Task 6.1: Persist runtime nodes, placements, leases, and cloud sandboxes

**Files:**
- Create: <code>corvus/domain/runtime_placement.py</code>, <code>corvus/infrastructure/repositories/runtime_placement.py</code>, <code>corvus/infrastructure/migrations/versions/m2_007_runtime_placement.py</code>
- Test: <code>tests/unit/domain/test_runtime_placement.py</code>, <code>tests/integration/test_runtime_placement_repository.py</code>

**Interfaces:**
- Produces: <code>RuntimeNode</code>, <code>NodeCapabilityReport</code>, <code>PlacementPolicy</code>, <code>RuntimePlacement</code>, <code>SandboxLease</code>, and tenant-scoped repository methods.

- [ ] Write failing tests for node identity binding, capability report expiry, allowed roots, placement-policy validation, lease fencing, sandbox state transitions, idempotent completion, and tenant isolation.
- [ ] Run focused files and confirm missing models/migration failures.
- [ ] Implement frozen models, repository constraints/indexes, monotonic state machine, canonical digests, and reversible migration.
- [ ] Rerun tests against SQLite/PostgreSQL.

### Task 6.2: Implement the authenticated outbound local-node bridge

**Files:**
- Create: <code>corvus/infrastructure/runtime_nodes/__init__.py</code>, <code>bridge.py</code>, <code>protocol.py</code>
- Create: <code>corvus/platform/api/runtime_nodes.py</code>
- Test: <code>tests/contract/test_runtime_node_protocol.py</code>, <code>tests/security/test_runtime_node_spoofing.py</code>

**Interfaces:**
- Produces single-use pairing challenge/proof, authenticated WebSocket protocol, <code>NodeHello</code>, <code>RunAssignment</code>, <code>NodeRunEvent</code>, <code>NodeAck</code>, heartbeat, reconnect cursor, and revocation.

- [ ] Write failing tests for challenge replay, forged node ID, origin/token confusion, stale capability report, sequence replay/gap, disconnect/reconnect, assignment acknowledgement, oversized frame, and secret redaction.
- [ ] Run focused tests and confirm missing protocol.
- [ ] Implement binary-safe schema-validated frames, channel binding, monotonic sequence/ack, bounded buffers, heartbeats, and server-side authority recheck before assignment.
- [ ] Rerun contract/security tests and existing loopback spoof tests.

### Task 6.3: Turn the Tauri sidecar into a paired execution node

**Files:**
- Create: <code>apps/desktop/src-tauri/src/node_identity.rs</code>, <code>keychain.rs</code>, <code>provider_discovery.rs</code>, <code>node_connection.rs</code>, <code>commands.rs</code>
- Modify: <code>apps/desktop/src-tauri/src/lib.rs</code>
- Test: Rust unit tests colocated in each module

**Interfaces:**
- Produces Tauri commands <code>node_status</code>, <code>pair_node</code>, <code>revoke_node</code>, <code>discover_providers</code>, <code>select_roots</code>, and an outbound assignment/event loop.

- [ ] Write failing Rust tests for key persistence, pairing replay, HMAC/proof spoofing, canonical-root traversal/symlink rejection, provider capability sanitation, reconnect cursor, buffered event deduplication, process cancellation, and diagnostic redaction.
- [ ] Run <code>cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml</code>; expect missing modules.
- [ ] Split existing sidecar responsibilities without changing startup safety; implement keychain-backed node identity, outbound TLS connection, root allowlist, provider discovery, and bounded offline buffering.
- [ ] Run Rust tests, Clippy with warnings denied, and Tauri build.

### Task 6.4: Implement authoritative runtime placement

**Files:**
- Create: <code>corvus/application/runtime_placement.py</code>
- Modify: <code>corvus/application/agent_runtime.py</code>
- Test: <code>tests/unit/application/test_runtime_placement.py</code>, <code>tests/security/test_runtime_fallback.py</code>

**Interfaces:**
- Produces <code>RuntimePlacementService.select(request, policy, nodes, cloud) -&gt; PlacementDecision</code> for this-device, ask, any-local, prefer-local, and cloud-only modes.

- [ ] Write failing tests for provider/tool/root/data-residency/credential mismatch, stale node, explicit fallback, no silent local/cloud switch, budget ceiling, and deterministic denial reasons.
- [ ] Run focused tests and confirm missing service.
- [ ] Implement fail-closed placement and bind the decision digest into the existing run authorization request/audit event.
- [ ] Rerun focused tests and the complete agent-runtime security suite.

### Task 6.5: Implement secure E2B lifecycle and cleanup reconciliation

**Files:**
- Create: <code>corvus/infrastructure/e2b/__init__.py</code>, <code>client.py</code>, <code>runtime.py</code>, <code>cleanup.py</code>
- Test: <code>tests/contract/test_e2b_runtime.py</code>, <code>tests/security/test_e2b_sandbox.py</code>

**Interfaces:**
- Produces: <code>E2bClientPort</code>, <code>E2bRuntime.start()</code>, <code>events()</code>, <code>cancel()</code>, <code>resume()</code>, and <code>E2bCleanupReconciler.run_once()</code>.

- [ ] Write failing fake-SDK tests for pinned template, secure access, resource/time limits, scoped upload/download, network/tool envelope, short-lived secret injection, heartbeat, timeout, cancellation, artifact digest, destroy, and orphan cleanup.
- [ ] Run focused files and confirm missing adapter.
- [ ] Implement an injected wrapper around the official <code>e2b</code> v2 SDK; never disable secure access, expose access tokens, or trust sandbox-reported authority.
- [ ] Rerun contract/security tests, including create/connect/destroy failures and repeated cleanup.

### Task 6.6: Expose runtime APIs and build Runtime Center

**Files:**
- Create: <code>corvus/platform/api/runtimes.py</code>
- Create: <code>apps/web/src/runtimes/RuntimeCenter.tsx</code>, <code>PlacementPicker.tsx</code>, <code>NodePairing.tsx</code>, <code>NodeStatus.tsx</code>, <code>ProviderDiscovery.tsx</code>, <code>E2bStatus.tsx</code>, <code>runtimeApi.ts</code>
- Create: <code>apps/web/src/desktop/bridge.ts</code>
- Test: <code>tests/integration/test_runtime_api.py</code>, <code>apps/web/src/runtimes/RuntimeCenter.test.tsx</code>

**Interfaces:**
- Produces node list/pair/revoke, provider capability, placement preview, cloud availability, sandbox status, and cleanup-status routes plus <code>/runtimes</code>.

- [ ] Write failing API/UI tests for pairing lifecycle, read-only capability display, root selection, readiness, fallback explanation, cloud-preview truth, sandbox progress, revoke, and hosted-web refusal to probe loopback.
- [ ] Run focused Python/Vitest files and confirm missing routes/components.
- [ ] Implement thin API routes, Tauri bridge detection, responsive Runtime Center, composer placement picker, and schedule readiness integration.
- [ ] Regenerate OpenAPI/types; run focused tests and web/Tauri builds.

### Milestone 6 Gate and Commit

- [ ] Run node protocol/spoofing, placement, E2B lifecycle/cleanup, agent-runtime, API, web, and Rust suites.
- [ ] Prove local-node disconnect/reconnect and fake-E2B create/run/cancel/destroy without duplicate effects or secret leakage.
- [ ] Run all repository quality gates and diff check.
- [ ] Update logs/status/OpenAPI/types/plan checkboxes.
- [ ] Commit: <code>feat(runtime): add paired local and E2B execution</code>.

---

## Milestone 7 — Complete Product UX and Integrated Verification

### Task 7.1: Replace placeholder navigation with the complete product shell

**Files:**
- Create: <code>apps/web/src/components/ProductNavigation.tsx</code>, <code>WorkspaceIdentityBlock.tsx</code>, <code>SearchCommand.tsx</code>, <code>NotificationCenter.tsx</code>, <code>MobileNavigation.tsx</code>
- Create: <code>apps/web/src/styles/tokens.css</code>, <code>shell.css</code>, <code>conversation.css</code>, <code>responsive.css</code>
- Modify: <code>apps/web/src/App.tsx</code>, <code>apps/web/src/app/AppShell.tsx</code>, <code>apps/web/src/components/NavigationRail.tsx</code>, <code>apps/web/src/components/ResponsiveNavigation.tsx</code>, <code>apps/web/src/styles.css</code>
- Test: <code>apps/web/src/components/ProductNavigation.test.tsx</code>, <code>SearchCommand.test.tsx</code>

**Interfaces:**
- Produces universal New thread, Search, Inbox, Schedules, workspace navigation, read-only identity, Settings footer, mobile bottom navigation, and three-pane responsive shell.

- [ ] Write failing tests for every route/control, keyboard shortcuts, focus return, active state, mobile sheets, unread state, profile labels, and unavailable capability explanations.
- [ ] Run focused Vitest files and confirm missing UI.
- [ ] Implement the shell with Corvus tokens, real routes, 44px targets, logical focus order, reduced motion, and design-packet source/component hooks.
- [ ] Run focused tests, axe, and build at desktop/tablet/mobile widths.

### Task 7.2: Complete profile-specific routes and product states

**Files:**
- Modify: <code>apps/web/src/app/WorkspaceRouter.tsx</code>, <code>apps/web/src/app/workspaceProfiles.ts</code>
- Create: focused views under <code>apps/web/src/views/</code> for Everyday outcomes/automations/knowledge and Developer repositories/changes/environments/policies.
- Test: <code>apps/web/src/app/WorkspaceRouter.test.tsx</code>, <code>apps/web/src/views/ProfileJourneys.test.tsx</code>

**Interfaces:**
- Produces real data-backed destinations for all profile routes and consistent loading, empty, error, offline, conflict, approval, and recovery states.

- [ ] Write failing tests that enumerate every profile route and assert a functional primary action, accessible heading, non-placeholder data state, and correct capability gate.
- [ ] Run focused tests and observe current placeholder-route failures.
- [ ] Implement outcome-oriented Everyday views and detailed Developer views over existing thread/schedule/team/settings/runtime APIs.
- [ ] Rerun focused tests and all web tests.

### Task 7.3: Add Playwright, axe, responsive screenshots, and cross-device fixtures

**Files:**
- Create: <code>apps/web/playwright.config.ts</code>, <code>apps/web/e2e/fixtures.ts</code>, <code>oauth-onboarding.spec.ts</code>, <code>chat-run.spec.ts</code>, <code>schedules.spec.ts</code>, <code>settings.spec.ts</code>, <code>teams.spec.ts</code>, <code>runtime-placement.spec.ts</code>, <code>sync-two-device.spec.ts</code>, <code>accessibility.spec.ts</code>
- Create: <code>apps/web/src/test/server.ts</code>

**Interfaces:**
- Produces deterministic fake Google/provider/E2B services, two-browser-device sessions, and projects for Chromium acceptance.

- [ ] Configure webServer startup, isolated databases, deterministic clocks/IDs, trace/video on failure, and projects at 1440x1000, 1024x900, and 390x844.
- [ ] Write journeys covering all four profiles, Google onboarding, thread/run/approval/stop/recovery, schedule, settings/MCP, team invite/approval/removal, node pairing, E2B placement, and sync conflict.
- [ ] Add axe scans, keyboard-only paths, aria-live assertions, reduced-motion project, screenshot baselines, and secret-pattern checks on traces.
- [ ] Run <code>pnpm --dir apps/web exec playwright test</code>; require zero unexpected failures and inspect every first-run baseline.

### Task 7.4: Run the visual blueprint build and audit gate

**Files:**
- Create/update: <code>.antigravity/website-blueprint/DESIGN_AUDIT_REPORT.md</code>, <code>DESIGN_SCORECARD_RESULT.json</code>

- [ ] Start the local production preview and run:

~~~powershell
ag design build --project-root .
ag design audit --project-root . --url "http://127.0.0.1:4173"
~~~

- [ ] Confirm every visible control, section source, component source, motion timeline, reduced-motion path, and responsive screenshot passes.
- [ ] Fix all packet/audit failures and repeat until the score gate passes; do not waive generic-layout, missing-interaction, or stale-hash failures.

### Task 7.5: Perform Windows desktop computer-use acceptance

**Files:**
- Update test evidence in <code>HACKATHON_STATUS.md</code> and <code>PROJECT_LOG.md</code>.

- [ ] Build and launch the unsigned Windows alpha installer on a clean local profile.
- [ ] Using the bundled Computer-use capability, verify Google-first onboarding with the deterministic OAuth service, profile/workspace display, New thread, streaming run, approval, Stop, schedule creation/run-now, settings persistence, MCP health, team invite/approval, local node pairing, runtime switching, reconnect, and tray/background status.
- [ ] Exercise keyboard-only navigation, 125% scaling, narrow window, offline/reconnect, crash/relaunch recovery, and secret-free diagnostics.
- [ ] Record screenshots and observed state transitions; repair and rerun every failed journey.

### Task 7.6: Run complete release-candidate verification

**Files:**
- Modify: <code>README.md</code>, <code>ROADMAP.md</code>, <code>PROJECT_LOG.md</code>, <code>HACKATHON_STATUS.md</code>, <code>.env.example</code>
- Update: OpenAPI, generated TypeScript, supply-chain outputs, installer/release documentation.

- [ ] Run the locked Python suite on 3.12 and 3.13, Ruff, format check, mypy, Bandit, pip-audit, Gitleaks, Semgrep, migration upgrade/downgrade, and PostgreSQL contracts.
- [ ] Run all web unit/E2E/axe/screenshot tests, TypeScript build, Rust tests, Clippy, Tauri build, sidecar packaging, and unsigned installer smoke tests.
- [ ] Run a clean-install cross-device scenario with local execution and fake E2B, then verify audit chains, no orphan sandboxes, no leaked secrets, and no unresolved plan checkbox.
- [ ] Verify Linux/macOS GitHub CI definitions cover Python, web, Rust, and installer compilation without PR-triggered privileged release publishing.
- [ ] Update documentation with truthful implemented capability, required environment variables, local/Railway/Vercel/E2B setup, limitations, and recovery.

### Task 7.7: Commit, push, and open the single review PR

- [ ] Commit: <code>feat(product): complete Corvus web and desktop experience</code>.
- [ ] Confirm the design and plan commits plus seven verified milestone commits are on <code>codex/full-product-platform</code> and the worktree is clean.
- [ ] Push <code>codex/full-product-platform</code> without force.
- [ ] Open one non-draft PR to <code>main</code> summarizing all seven milestones, verification evidence, screenshots, migration notes, security invariants, and deployment requirements.
- [ ] Request the configured human and bot reviewers once. Do not resolve new findings, merge, deploy production migrations, enable billing, or tag a release in this plan run.

### Milestone 7 and Final Stop Gate

- [ ] All local integrated gates pass with fresh evidence.
- [ ] Required GitHub checks have started on the one PR.
- [ ] The PR is open, unmerged, and ready for independent review.
- [ ] Stop execution and report the PR URL, commit list, verification matrix, known credential-gated live checks, and reviewer handoff.

---

# Preserved Foundation Plan (unchanged)

# Plan: Corvus CLI V2 and Shared Web/Desktop Platform
_Maintained by Codex/Hermes and gated by recorded independent review._

## Goal
Evolve the supplied Corvus CLI V1 into one tested, installable, configuration-driven Corvus V2 platform with a single authoritative Python core. Users may interact through CLI, browser web, Tauri desktop, or approved third-party channels; run Corvus for an individual or a team; and place authorized work locally, through a secure connector, or on Corvus Cloud. The platform must add team-safe identity, scope, authorization, outcome contracts, durable workflow graphs, artifact lineage, governed memory and skills, context isolation, secret brokering, earned autonomy, shadow mode, budgets, kill switches, offline operation, and proof-carrying completion without weakening V1's sandbox, verification, delivery, or rollback guarantees.

Milestones 0 and the security-characterization portion of 0.5 have started. The next implementation work completes the release-blocking V1 trust fixes, then proves one vertical project create/read path through the corrected deployment/workspace/client/execution contracts, authorization, migration-backed persistence, and audit. CLI V2, FastAPI expansion, React UI, channels, and desktop remain later consumers of that boundary.

## Product Configuration Matrix

Corvus is one product with one authoritative core. Configuration is separated by owner, lifetime, and cardinality rather than collapsed into product editions or one overloaded runtime profile.

### Configuration ownership

| Contract | Owner/cardinality | Lifetime | Values/responsibility |
|---|---|---|---|
| `DeploymentProfile` | one per installed/server deployment | deploy/restart | authority mode, auth, network, storage, enabled adapters |
| `DeploymentInstance` | one active instance per deployment activation | process/device activation | non-exportable instance identity, exclusive lease/OS lock, authority-generation anchor |
| `WorkspaceConfig` | one per workspace | mutable through authorized migration | `individual` or `team`, memberships, reviewer rules, shared scopes, budgets |
| `ClientContext` | one per request/session | request/session | `cli`, `desktop`, `web`, or `channel`; transport identity and origin only |
| `ExecutionPlacement` | one per task/run | task/run | `local_runner`, `cloud_worker`, or `connector`; sandbox and data locality |
| `ModelRouteSet` | principal/workspace scoped | independently mutable | local/API/OAuth routes, health, cost, capabilities, failover |
| `CredentialRef` | principal/workspace scoped | independently rotatable | OS keyring, cloud vault, provider OAuth, or connector reference |

Client surface never grants authority. Individual/team behavior belongs to the workspace. Different tasks may execute in different approved locations. Credentials rotate independently from deployment, workspace, client, and execution state.

### Behaviorally distinct combinations

| Deployment authority | Workspace mode | Clients | Execution | Status and controls |
|---|---|---|---|---|
| `embedded_local` | individual | CLI after Milestone 3 retained-surface cutover | local runner | Supported only after `chat`/`run`/TUI and review/apply/undo use the in-process client/application ports, plus SQLite, OS keyring, fixed workspace-wide OS lock, device-bound sealed monotonic generation/root and fail-closed sandbox |
| `local_daemon` | individual | CLI after Milestone 4; web after Milestone 5; desktop after Milestone 11 | local runner | CLI support requires every retained project/chat/run/TUI/review/apply/undo path to use HTTP/SSE and pass the Milestone 4 daemon/auth/lifecycle/parity gate; web remains unsupported until the real Milestone 5 client passes pairing, rotation, restart and reconnect Playwright tests |
| `self_hosted` | individual/team | CLI/web/channel after Milestone 10; desktop after Milestone 11 | server/local runners | Unsupported until the Milestone 10 real-client bootstrap/OIDC/session/CSRF/rotation/reconnect/restore gate and operational PostgreSQL proof pass; desktop must rerun that authenticated flow in Milestone 11 |
| `vendor_cloud` | individual/team | CLI/web/channel after Milestone 10; desktop after Milestone 11 | cloud workers | Unsupported until the Milestone 10 real-client OIDC/session/CSRF/rotation/reconnect/restore gate and tenant-isolated cloud proof pass; desktop must rerun that authenticated flow in Milestone 11 |
| `vendor_cloud` | individual/team | CLI/web/channel after Milestone 10; desktop after Milestone 11 | connector | Unsupported until Milestone 8 connector semantics and Milestone 10 authenticated real-client/cloud gates both pass; desktop remains disabled until Milestone 11 |
| any | any | any | host process without sandbox | Invalid for build/apply work; fail closed |

One workspace has exactly one authoritative control plane represented by a persisted `WorkspaceAuthority` epoch plus a non-exportable epoch-key binding or short-lived registry lease. Database state alone never proves authority. Every authority-bearing commit (policy/grant/revocation, budget/kill state, approval, workflow/effect transition, audit head, or handoff) advances a workspace-wide monotonic generation and state-root commitment outside the restorable database. Registry-backed deployments use an exclusive deployment-instance lease plus registry compare-and-swap; genuinely offline local deployments hold one fixed workspace-wide OS lock and advance a device-bound sealed monotonic generation/root. A prepared/finalized anchor protocol binds the exact mutation digest; interrupted or mismatched recovery quarantines rather than guessing. Registry trust is pinned outside the database to an offline root and versioned verifier-key history with rotation, revocation, compromise and recovery rules. Local and cloud copies are never implicit dual-primary replicas. Authority handoff requires an externally anchored close/revocation certificate and attested destruction/revocation of the old epoch key before target activation. A normal restart may resume only when the same deployment-instance activation key, exclusive lease/OS lock, database generation and externally/sealed state root match exactly. Any restored/cloned database or generation/root mismatch enters `restore_quarantine` and remains read/queue-only; resuming mutation requires exclusive takeover into a new epoch after the anchor revokes the former instance; same-epoch restoration is forbidden. Offline-capable workspaces without a reachable anchor or non-exportable-key destruction proof cannot hand off authority at all. Connectors are execution placements and never acquire control-plane authority. A remote-authority client that is offline may only queue signed intents; reconnect performs fresh authentication, authorization, revocation, budget, and kill-switch checks before any intent becomes executable work.

### Effective capabilities

The authoritative backend resolves deployment, workspace, client, execution, model, credential, policy, budget, and kill-switch state into `EffectiveCapabilities`. Clients render returned capabilities and reason codes; feature flags may hide features but cannot create authority or disable authorization, audit, verification, evidence, limits, or rollback controls.

### Bring-your-own models and credentials

- Corvus does not silently bundle model entitlement. Users select local models, API providers, or provider-owned OAuth such as Codex/ChatGPT.
- `ProviderConnection` binds a route, credential reference, execution placement, ownership, and lifecycle without exposing the secret.
- Local credentials use the OS credential store or provider-owned local session. Cloud credentials use an encrypted workspace vault plus the scoped secret broker.
- Provider OAuth uses persisted authorization transactions with state/nonce, PKCE or device-flow binding, callback ownership, expiry, token-version references, refresh rotation, revocation, and recovery; OAuth tokens remain opaque to prompts, workers, traces, and clients.
- Codex CLI OAuth remains local unless the provider offers a supported server OAuth flow.
- Corvus Cloud cannot reach a laptop-only model without an explicit outbound connector. The connector uses short-lived mutual authentication, model-only RPC, consent, health registration, revocation, egress limits, and complete audit attribution.
- Offline execution is permitted only while the local deployment owns the current workspace authority epoch. Remote-authority offline clients can inspect an authorized cache and queue signed intents only; they cannot execute work, use stale grants, or cause external effects. Cloud-only actions remain unavailable with explicit reason codes.

## Context

### Source and baseline
- Repository: `C:/Users/lucas/Projects/corvus-platform`
- Imported V1 baseline commit: `1410d7f`
- Original source manifest: `V1_SOURCE_MANIFEST.json`
- Prior architecture memo: `C:/Users/lucas/AppData/Local/hermes/.ai/HERMES_HANDOFFS/2026-07-13-corvus-web-team-agent-integration.md`
- V1 contains 27 Python files and approximately 9,322 lines.
- V1 version is `0.1.0`; it has no package metadata, lockfile, tests, migrations, README, API, web client, or desktop client.

### Current implementation status
- Certification branch: `repair/m05-certification`, based on pre-Milestone-1 commit `407c981`.
- Corvus V2 package baseline is `0.2.0a1` with locked Python 3.12 dependencies and retained CLI behavior.
- The Milestone 0.5 repair now includes one byte-exact exhaustive V1 fixture corpus, populated legacy domains, distinct project policy evidence, sealed quarantine capture, canonical `legacy_run` context ownership, and aggregate-bounded/redacted provider streaming.
- Latest local repair gate before candidate freeze: 114 tests passed; Ruff lint/format, strict mypy, compile, and Git diff checks passed. Docker/Podman integration remains environment-dependent and must be proven in CI.
- Milestone 0.5 remains under certification until all CI gates and two independent exact-commit reviews pass. No Task 1.1 schema/domain, FastAPI, React, channel, connector, or desktop code is permitted on this branch.

### Verified V1 evidence
- Intended runtime: Python 3.12.
- Clean `python -m compileall` passes under Python 3.12.10 with contaminated `PYTHONHOME/PYTHONPATH` removed.
- `python -m corvus --help` starts successfully under an isolated Python 3.12 dependency environment.
- `corvus doctor --json` reports:
  - SQLite integrity: pass.
  - Codex CLI discovery: pass.
  - Docker: unavailable.
  - Podman: unavailable.
  - Build sandbox route: fail closed to chat-only.
  - Providers configured in isolated audit home: zero.
- Ruff: all checks passed.
- Secret-pattern scan: no private-key or common live-token signatures found.
- Bandit: zero high-severity findings; 21 low and two medium findings. Its runtime could not parse two Python 3.12 `type` alias modules, so the scan is incomplete until Bandit runs under a 3.12-capable parser.
- Current Mini PC capacity during audit: about 24 GB RAM total, 4.65 GB available, 80.3% used, 24% CPU. Avoid simultaneous heavy Node/Rust/container builds.

### Existing V1 primitives worth retaining
- Typed run phases, acceptance criteria, budgets, policy, approvals, artifacts, checkpoints, memory, skills, and provider models in `corvus/models.py`.
- Docker and Podman sandbox backends with no bind mounts, disabled networking, dropped capabilities, resource limits, non-root users, and validated tar staging in `corvus/sandbox.py`.
- Hash-chained run events and content-addressed artifacts in `corvus/store.py`.
- Manifest-bound packaging, encrypted rollback backups, destination conflict checks, and optional git refs in `corvus/delivery.py`.
- Repair-loop workflow in `corvus/workflow.py`.
- Bounded one-level subagent runtime in `corvus/conversations.py`.
- Provider abstraction, onboarding, Typer CLI, Textual TUI, memory and versioned skill prototypes.

### Architectural gaps that block safe team/web/desktop use
1. `ConversationRuntime` is in-memory while `TraceStore` is durable; refresh/restart loses chats, queues, and events.
2. Run events lack mandatory workspace, project, requester, agent, policy, and access-bundle attribution.
3. Memory is keyed only by project and identity; it lacks personal/thread/channel/workspace scope and reviewed promotion.
4. Policy is path/domain/autonomy oriented, not tenant/resource/action oriented.
5. No durable work item, lease, heartbeat, dependency, retry, routine, or trigger model exists.
6. No API authentication/authorization boundary exists.
7. No migration framework exists; `create_all()` cannot safely evolve existing databases.
8. CLI/TUI modules are large adapters coupled directly to state/config construction.
9. There are no deterministic tests proving sandbox options, event integrity, approval binding, multi-user isolation, or cancellation cleanup.
10. V1 dependency versions are implicit and unreproducible.
11. `PolicyEngine` is not enforced by builds, providers, delivery, memory, or skills; advertised policy is largely disconnected.
12. Build snapshots can include `.env`, credentials, large dependency trees, and other sensitive files; command output is stored and can be sent back to the model as repair context, while plaintext staging is not reliably cleaned up.
13. The generating model chooses its own test commands, `smoke_command` is unused, and repair attempts reuse one staging tree, so trivial or stale-file tests can forge acceptance.
14. Delivery apply validates the manifest body but not each staged file against the approved artifact digest immediately before writing; journal ordering and missing cross-process locks leave crash/TOCTOU gaps.
15. Secret redaction is regex/string-oriented rather than structured-data-aware; provider URLs are not safe for a server threat model; Codex children inherit a broad host environment; artifact digest reads are not strictly validated.

## Refined Specification

### Platform invariants
1. Corvus core is authoritative for identity, scope, policy, work state, events, budgets, approvals, verification, artifacts, and audit.
2. CLI, browser, desktop, Discord, Slack, webhooks, and routines are untrusted clients of the same application services.
3. Every command/run carries an immutable request context: deployment, workspace authority epoch/trust-anchor proof, discriminated scope, immutable audience-policy snapshot, requester, client/transport identity, acting agent and agent grant, requester access bundle, execution placement when applicable, policy digest, immutable authorization-decision snapshot, correlation ID, and idempotency key.
4. Missing or mismatched identity/scope/capability information fails closed.
5. Cross-scope memory reads and promotions require explicit policy and provenance; private memory never silently becomes team memory.
6. Workers acquire monotonically fenced durable leases with expiry and heartbeat. All external effects flow through one effect gateway backed by semantic idempotency keys and a transactional outbox, so stale workers cannot duplicate effects after recovery.
7. Implementers cannot approve their own unsupported completion claims.
8. Automated routines use the same authorization, budget, sandbox, verification, and audit path as interactive runs.
9. Progress comes only from persisted backend events; clients never synthesize progress timers.
10. Sandboxes receive approved snapshots and scoped broker responses only; they never receive host credentials or direct host filesystem access.
11. Web previews use a distinct hostile-content origin and never receive Corvus auth tokens.
12. Completion is proof-carrying: every required acceptance criterion must pass or the run uses an honest partial/failed/blocked status.
13. Corvus earns autonomy through verified performance and provides proof for every completed action.

### Locked capability brief

| Capability | Authoritative owner | Required proof before enforcement/promotion |
|---|---|---|
| Outcome contracts | domain + verification service | acceptance criteria, evidence schema, permissions, budget and runtime-limit tests |
| Versioned skills | workspace skill registry | evaluation suite, independent approval, canary metrics, regression detection and rollback |
| Portable agent versions | agent release registry | immutable composition manifest, user save approval, evaluation evidence, signature verification, secret/private-data exclusion, target-chat authorization and rollback |
| Context firewall | ingestion/context service | untrusted-source labels, instruction/data separation, provenance and prompt-injection fixtures |
| Secret broker | credential service | short-lived scoped grant, host/method/path limits, revocation and zero-secret trace tests |
| Autonomy levels | workspace policy | `advise`, `observe`, `sandbox`, `propose`, `apply`, `bounded_delegation` transition tests |
| Shadow mode | evaluation service | proposed-versus-approved comparison, reliability threshold, holdout canaries and rollback |
| Durable workflow graphs | work service | dependencies, specialists, checkpoints, stuck detection and retry -> replan -> decompose recovery |
| Artifact lineage | artifact/audit service | request, source, model, tool, test, approval and receipt linkage with digest verification |
| Memory governance | memory service | scope, source, confidence, expiry, encryption, export, deletion and promotion authorization |
| Kill switches and limits | policy/budget service | atomic workspace/agent/workflow/run stop plus cost/runtime reservation tests |
| Offline mode | deployment + execution services | local model/cache/memory/queue behavior with explicit unavailable cloud capability reasons |

Autonomy starts in shadow/proposal mode. Promotion to a more powerful level requires versioned evaluation evidence, independent approval where risk requires it, canary operation, reliability thresholds, and reversible rollback. A client or feature flag cannot self-promote an agent, skill, workflow, or credential grant.

### Capability implementation ledger

Migration identifiers are plan-stable labels; implementation may map them to Alembic revision IDs while preserving this ownership and order.

| Capability | Milestone | Persistent records / migration | Legacy cutover or import | Downgrade / rollback boundary | Executable acceptance test |
|---|---:|---|---|---|---|
| Outcome contracts | 2 | `OutcomeContractVersion`, workflow pins / `M2-001` | V1 acceptance/budget fields imported after destination schema exists | retain prior immutable version; workflows never repoint silently | `test_outcome_contract_version_pin.py` |
| Versioned skills | 7 | `Skill`, `SkillVersion`, evaluations/promotions/regressions / `M7-002` | V1 skills imported idempotently from quarantine after `M7-002` | restore prior active version plus rollback receipt | `test_skill_canary_rollback.py` |
| Portable agent versions | 7 | `AgentVersion`, `PortableAgentPackage`, `AgentInstallation` / `M7-003` | no legacy agent state is treated as an approved portable release | deactivate the imported installation and restore its prior pinned version | `test_portable_agent_version.py` |
| Context firewall | 0.5, extended 7 | `ExternalContent`, `ContextEnvelope` / `M005-001` | legacy external/model content remains untrusted; no trust elevation on import | disable new ingestion while retaining provenance/readability | `test_context_firewall.py` |
| Secret broker | 6 | provider/OAuth/credential references and grants / `M6-001` | import references only; never import plaintext; reauthorize unsupported OAuth | revoke grants and return to direct local reference where supported | `test_secret_broker_lifecycle.py` |
| Autonomy levels | 6 | `AutonomyPolicy`, action/effect matrix / `M6-002` | V1 policy/autonomy YAML imported after schema and capped at `propose` until reviewed | lower ceiling immediately; no grandfathered authority | `test_autonomy_effect_matrix.py` |
| Shadow mode | 6 | `ShadowEvaluation`, canary policy / `M6-003` | no legacy promotion evidence is assumed | return subject to shadow and revoke canary grants | `test_shadow_no_real_effects.py` |
| Durable workflow graphs | 2 | graph/work/dependency/lease/attempt/recovery / `M2-002` | V1 runs imported as immutable history; only new work becomes schedulable | pause new claims; preserve graph/event readability | `test_workflow_recovery_fencing.py` |
| Artifact lineage | 2 | typed immutable lineage edges and effect receipts / `M2-003` | V1 artifacts receive imported-source digest edges, never fabricated evidence | reject completion if closure cannot verify | `test_lineage_digest_closure.py` |
| Memory governance | 7 | scoped memory/promotion/export/deletion / `M7-001` | V1 memories imported after schema into explicit owner/workspace scopes | stop writes; retain encrypted export and deletion receipts | `test_memory_scope_lifecycle.py` |
| Kill switches and limits | 2 | kill switches, budget reservations, effect intents / `M2-004` | V1 budgets imported as conservative ceilings after schema | fail closed and preserve stop/usage receipts | `test_effect_gateway_atomic_limits.py` |
| Offline mode | 1 authority, 8 queue/cache | authority/handoff plus offline intent/cache metadata / `M1-001`, `M8-001` | no legacy copy gains authority; cache import is non-authoritative | discard/requeue unaccepted intents; restore from last signed authority handoff | `test_offline_authority_reconciliation.py` |

### Capability application and client ownership ledger

No capability is complete when only its repository/service fixture passes. Each row names the transport-neutral port and retained-CLI/API/web cutover that must exercise the authoritative implementation.

| Capability | Application ports | Retained CLI cutover | API and web ownership | End-to-end client proof |
|---|---|---|---|---|
| Outcome contracts | `OutcomeCommandPort`, `OutcomeQueryPort` | additive outcome/evidence commands in Milestone 3 | outcome/evidence routes and workspace views in Milestones 4–5 | create/version/pin/verify through CLI and web |
| Versioned skills | `SkillCommandPort`, `SkillQueryPort` | route retained skill commands through ports in Milestone 7 | skill evaluate/promote/rollback routes and governance UI in Milestone 7 | import -> evaluate -> shadow -> canary -> rollback without direct registry calls |
| Portable agent versions | `AgentReleaseCommandPort`, `AgentReleaseQueryPort` | additive inspect/save/export/import/install/rollback commands in Milestone 7 | agent release routes and explicit save/import/install UI in Milestone 7 | improve -> evaluate -> user-save -> signed export -> target-chat import -> capability reauthorization -> rollback |
| Context firewall | `ContextIngestionPort`, `ContextProvenanceQueryPort` | route retained build/memory ingestion through the firewall in 0.5/7 | sanitized provenance/explain query; no raw untrusted authority path | hostile content remains data across CLI/API/web workflows |
| Secret broker | `ProviderConnectionCommandPort`, `OAuthFlowCommandPort`, `CredentialGrantPort` | route retained provider connect/revoke/status commands through ports in Milestone 6 | provider/OAuth start/callback/device/status/revoke routes and settings UI in Milestone 6 | client-driven PKCE and device flows, reconnect, revoke and recovery |
| Autonomy levels | `AutonomyPolicyCommandPort`, `AutonomyQueryPort` | additive inspect/lower/request-promotion commands in Milestone 6 | autonomy policy routes and guarded settings UI in Milestone 6 | denied self-promotion and independently approved bounded promotion |
| Shadow mode | `ShadowEvaluationCommandPort`, `ShadowQueryPort` | additive shadow evidence/status commands in Milestone 6 | shadow comparison/canary/rollback routes and UI in Milestone 6 | proposal -> comparison -> canary -> rollback through clients |
| Durable workflow graphs | `WorkCommandPort`, `WorkQueryPort`, `WorkEventPort` | work/run commands use ports in Milestone 3 | work routes, SSE and workspace views in Milestones 4–5 | submit/pause/resume/recover through CLI and web |
| Artifact lineage | `ArtifactQueryPort`, `LineageVerificationPort` | evidence/lineage commands in Milestone 3 | artifact/lineage routes and proof UI in Milestones 4–5 | verify the same digest closure through CLI and web |
| Memory governance | `MemoryCommandPort`, `MemoryQueryPort` | replace direct `MemoryManager` CLI use with ports in Milestone 7 | scoped read/promote/export/delete routes and governance UI in Milestone 7 | import/read/promote/export/delete with cross-scope denial |
| Kill switches and limits | `LimitCommandPort`, `KillSwitchCommandPort`, status queries | kill/limit commands in Milestone 3 | limit/kill routes and controls in Milestones 4–5 | stop/reserve/settle/reject races through CLI and web |
| Offline mode | `OfflineIntentCommandPort`, `OfflineStatusQueryPort`, `ConnectorCommandPort` | queue/status/reconcile commands in Milestone 8 | offline/connector status, conflict and reconnect UI in Milestone 8 | disconnect -> queue -> restore/reconnect -> reauthorize/reject without dual authority |

### Retained platform-surface cutover ledger

These pre-existing surfaces are not optional compatibility wrappers: each can create work or mutate user state, so topology support is forbidden until the real adapter uses the authoritative client path.

| Retained surface | Application ports | Embedded cutover | Daemon/network cutover | No-substitute proof |
|---|---|---|---|---|
| `corvus chat`, `corvus run`, Textual TUI | `ConversationCommandPort`, `ConversationQueryPort`, `ConversationEventPort` | Milestone 3 routes all three through the in-process client; adapters cannot construct config/provider/workflow/runtime objects | Milestone 4 routes the unchanged commands/TUI through HTTP plus SSE; Milestone 10 reruns them against packaged self-host/cloud deployments | command/exit/JSON compatibility, restart/reconnect, cancellation, event-order/snapshot parity, unsupported-topology rejection |
| `corvus review`, approval/apply, `corvus undo` | `DeliveryQueryPort`, `ApprovalCommandPort`, `ApplyCommandPort`, `UndoCommandPort` | Milestone 3 routes review/apply/undo through request context, discriminated filesystem bindings, atomic signed-approval consumption, effect fencing, audit and rollback services | Milestone 4 routes retained commands over HTTP; Milestone 5 adds generated-web controls; Milestone 10 reruns both clients and Milestone 11 reruns Tauri | no synthetic provider credentials; exact bundle/manifest/destination/rollback/original-apply binding; approval mismatch/replay/expiry/duplicate-consumption, apply conflict/crash, undo/compensation and receipt parity |

### V2 client contract
- Define transport-neutral command/query/event ports, one application composition root, and matching in-process and HTTP Python clients before any UI expansion.
- Every retained and V2 CLI/TUI command uses a topology-aware client: in-process for `embedded_local`, HTTP/SSE for daemon/self-hosted/cloud. CLI/TUI adapters may parse/render only; they cannot instantiate `ConversationRuntime`, providers, workflows, delivery managers, authority, policy, or persistence.
- Review, approval, filesystem apply and undo are authority-bearing effects. They use a filesystem—not provider—effect subtype carrying exact bundle/manifest/destination/rollback/original-apply digests. Permit claim reauthorizes and atomically consumes the exact current signed approval once through delivery/approval/undo ports and the centralized effect/audit path; no adapter calls `DeliveryManager` directly or fabricates provider credentials.
- Web uses authenticated FastAPI endpoints plus replayable SSE with opaque workspace-scoped cursors; the same build targets Corvus Cloud or a self-hosted endpoint.
- Desktop uses Tauri and the same React client package only after CLI/web team and provider flows stabilize. It may connect to cloud or supervise a least-privilege loopback service; it never gains authority because it is native.
- Third-party channels authenticate as channel/service principals and preserve the human requester, immutable scope/audience, workspace, project, guild/channel/thread, acting agent grant, and access-bundle attribution.
- Shared OpenAPI-generated TypeScript contracts and transport-parity tests prevent client-defined authority or duplicated state machines.

## Explicit Non-Goals for the Immediate Slice
- No production cloud deployment.
- No live Slack/Discord bot or Claude Tag embedding.
- No billing, purchases, or automatic external communications.
- No Docker installation or sandbox weakening on this host.
- No arbitrary MCP server execution.
- No public sharing or preview proxy yet.
- No provider credential migration.
- No automatic migration of an unknown production V1 database; only a tested local schema bootstrap and an explicit migration path.
- No complete UI workspace in the first slice.
- No recursive agents, autonomous skill self-promotion, or self-modifying safety policy.

## Locked implementation discipline
1. Inspect repository, architecture, tests, and live build status before each milestone; current evidence is recorded above.
2. Reuse the V1 map below and avoid parallel implementations of policy, sandbox, delivery, provider, memory, or runtime behavior.
3. Preserve backward compatibility where practical; expose V2 paths additively until compatibility tests pass.
4. Build the smallest secure vertical slice per capability, with TDD red -> green -> refactor and one reviewable commit per task.
5. Add explicit schema migrations, authorization checks, negative isolation tests, and rollback fixtures before changing durable state.
6. Never expose secrets in prompts, context, events, traces, snapshots, artifacts, errors, exports, or test fixtures.
7. Do not claim completion until the outcome contract's required tests and evidence pass; document partial, blocked, skipped, and unsupported states honestly.
8. Prioritize the remaining security fixes and project create/read authority path before CLI V2, FastAPI expansion, React, channels, connector, or desktop work.

## Recommended Repository Layout

```text
corvus-platform/
  corvus/                     # Authoritative Python core and local CLI adapter
    application/              # Use cases; no Typer/FastAPI/Textual imports
      ports.py
      authorization.py
      audit.py
      projects.py
      outcomes.py
      work_items.py
      context_firewall.py
      credentials.py
      memory.py
      skills.py
    domain/                   # Pure typed contracts and invariants
      deployment.py
      workspace.py
      client.py
      execution.py
      identity.py
      scope.py
      access.py
      outcomes.py
      work.py
      events.py
      memory.py
      skills.py
      providers.py
    infrastructure/
      db.py
      repositories/
      secret_broker/
      connector/
      sandbox/                # Gradually absorbs existing sandbox.py
      providers/              # Gradually absorbs existing providers modules
    api/                      # Thin FastAPI adapter, auth dependencies, SSE
    cli/                      # Thin Typer adapter after decomposition
    ...existing V1 modules during incremental migration
  apps/
    web/                      # Vite + React + TypeScript browser client
    desktop/                  # Tauri wrapper reusing web UI/assets
  packages/
    client-ui/                # Shared React workspace UI for web/desktop
    contracts/                # Generated OpenAPI TS types/client
  migrations/                # Alembic migrations
  tests/
    unit/
    integration/
    contract/
    security/
    e2e/
  examples/
  docs/
  pyproject.toml
  uv.lock
  package.json
  pnpm-workspace.yaml
  PLAN.md
  PLAN-REVIEW-LOG.md
```

### Incremental-layout rule
Do not move the 9k-line V1 package wholesale. Add clean seams and migrate one adapter/use case at a time. Existing `corvus.cli`, `corvus.tui`, `corvus.workflow`, `corvus.delivery`, and provider behavior remain callable while tests are introduced.

## V1 Reuse / Refactor / Replace Map

| V1 path | Decision | V2 treatment |
|---|---|---|
| `corvus/models.py` | Refactor gradually | Keep existing public models; move new team contracts into focused domain modules to avoid one larger model file. Export compatibility aliases where useful. |
| `corvus/security.py` | Reuse and harden | Retain path/link protections, atomic writes, hashing, and redaction; add structured zero-secret redaction plus context-firewall provenance/instruction separation. |
| `corvus/store.py` | Refactor | Keep event hashing and artifact addressing; split DB bootstrap/repositories; add migrations and mandatory scoped audit records. |
| `corvus/conversations.py` | Replace runtime state, reuse bounds behind ports | Preserve limit/delegation semantics, back chats/messages/queues/events with durable repositories, and expose only `ConversationCommandPort`/`ConversationQueryPort`/`ConversationEventPort`; retained adapters never construct the runtime. |
| `corvus/policy.py` | Extend | Keep path/domain/autonomy checks; add resource/action/scope access evaluation and deny precedence. |
| `corvus/sandbox.py` | Reuse behind protocol | Keep fail-closed Docker/Podman implementations; test options and lifecycle through fakes. No host-process fallback for builds. |
| `corvus/workflow.py` | Refactor into use case | Preserve snapshot/generate/verify/package loop; require context firewall, outcome contract, authenticated authority, workflow graph, independent evidence, lineage, limits, and receipts. |
| `corvus/delivery.py` | Reuse and harden behind authoritative ports | Keep manifest binding/conflict detection/rollback, add scanner inputs/replay-resistant approvals/archive ownership, and expose delivery query/approval/apply/undo services through the centralized effect/audit path; retained commands never call the manager directly. |
| `corvus/verification.py` | Extend | Generalize sandbox protocol, persist evidence metadata, and enforce required/optional criteria honestly. |
| `corvus/memory.py` | Replace schema/API | Add encrypted scoped records, source/confidence/expiry, promotion review, authorization, export, deletion, retention, and receipts. |
| `corvus/skills.py` | Extend | Bind versions to workspace/capabilities and add evaluation suites, shadow/canary promotion, independent approval, regression detection, and rollback. |
| `corvus/providers.py`, `provider_control.py`, `model_catalog.py`, `codex_cli.py` | Reuse behind routing/broker ports | Preserve transports; add provider connection/credential grant ownership, placement, health, rotation/revocation, budgets, failover, and zero-secret receipts. |
| `corvus/cli.py` | Decompose into topology-aware adapter | Keep command/exit/JSON compatibility, but cut retained `chat`, `run`, review/approve/apply and undo plus V2 commands to the in-process or HTTP/SSE clients in Milestones 3–4; direct config/provider/workflow/delivery construction becomes a tested import-boundary violation. |
| `corvus/tui.py` | Retain as topology-aware CLI client | Remove `ConversationRuntime` construction; consume conversation command/query/event ports through the same in-process or HTTP/SSE client selected by deployment topology. |
| `corvus/onboarding*.py` | Retain behind client/composition root | Add explicit local/remote mode and workspace selection through bootstrap/application ports; adapters cannot create a second configuration or authority path. |

## Core Data Model

All identifiers are opaque UUIDs. Every persistent row includes `created_at`, and mutable rows include `updated_at` plus optimistic `version`.

### Configuration and provider lifecycle
- `DeploymentProfile(id, authority_mode[embedded_local|local_daemon|self_hosted|vendor_cloud], auth_profile, network_profile, storage_profile, enabled_adapters, protocol_version, version)`
- `WorkspaceConfig(workspace_id, collaboration_mode[individual|team], autonomy_ceiling, shadow_policy_id, budget_policy_id, memory_policy_id, kill_switch_state, version)`
- `DeploymentInstance(id, deployment_profile_id, instance_public_key, non_exportable_activation_key_ref, device_binding_digest, status[active|revoked|retired], activated_at, revoked_at?)`; the private activation capability is outside database/workspace backups.
- `WorkspaceAuthority(workspace_id, deployment_profile_id, deployment_instance_id, epoch, authority_generation, authority_state_root, authority_epoch_credential_id, trust_anchor_id, active_lease_id?, state[active|handoff_pending|closed|restore_quarantine], previous_epoch_digest?, activated_at, closed_at?, version)`; `(workspace_id, epoch)` is unique, but mutation requires the live deployment-instance key, exclusive lock/lease, and exact external/sealed generation/root.
- `AuthorityTrustAnchor(id, workspace_id, kind[registry_generation|sealed_local_generation], anchor_registry_id?, pinned_registry_root_digest?, local_lock_name?, sealed_generation_ref?, device_binding_digest?, policy_digest, status)`; the registry-root pin or sealed local generation is stored outside the restorable workspace database.
- `AuthorityRegistry(id, endpoint_digest, offline_root_public_key_digest, policy_digest, status)`
- `AuthorityRegistryVerifierKeyVersion(id, registry_id, key_version, algorithm, public_key, valid_from, valid_until, status[active|rotated|revoked|compromised], predecessor_digest?, predecessor_signature?, offline_root_recovery_signature?, revoked_at?, compromise_effective_at?)`; clients pin the offline root outside the database, verify the complete version chain, reject rollback/freeze/expired keys, and require recovery-root authorization after compromise.
- `AuthorityRegistryTrustState(registry_id, metadata_version, latest_verifier_key_version, complete_history_head_digest, issued_at, expires_at, offline_root_version, threshold_signature_set_digest, previous_metadata_digest?)` is threshold-signed by the pinned offline root and persisted in an OS-sealed or independent transparency/registry store outside workspace/database backups. The client stores a monotonic minimum metadata version and history head there; expiry is mandatory, and an older valid chain prefix cannot satisfy freshness.
- `AuthorityRegistryFreshnessProof(id, registry_id, trust_state_metadata_version, complete_history_head_digest, registry_sequence, challenge_nonce_digest, response_digest, issued_at, expires_at, verifier_key_version_id, registry_signature)` binds every lease, close, recovery, and authority compare-and-swap response to the current non-expired trust-state head plus a caller nonce and monotonic sequence. Prefix replay, freeze, skipped-version, rotation, revocation, compromise, expiry, and recovery-root tests fail closed.
- `AuthorityRegistryWorkspaceAnchor(registry_id, workspace_id, active_deployment_instance_id, epoch, generation, state_root, lease_sequence, prepared_intent_digest?, prepared_next_generation?, prepared_proposed_root?, status[active|prepared|instance_revoked|closed])` is registry-owned non-rollback state outside workspace backups; compare-and-swap permits one active instance and one prepared mutation.
- `AuthorityEpochCredential(id, workspace_id, epoch, deployment_profile_id, trust_anchor_id, public_key, non_exportable_private_key_ref, non_exportable_attestation_digest, status[active|destroyed|revoked|expired], activated_at, destroyed_or_revoked_at?)`; snapshots/backups exclude the private capability.
- `AuthorityInstanceLeaseCertificate(id, workspace_id, epoch, deployment_instance_id, authority_generation, authority_state_root, anchor_registry_id, registry_sequence, not_before, not_after, registry_signing_key_version_id, certificate_digest, registry_signature, revoked_at?)`; the registry maintains one active instance lease/fence per workspace.
- `LocalAuthorityGenerationAnchor(id, workspace_id, deployment_instance_id, fixed_os_lock_name, sealed_generation_ref, device_binding_digest, generation, state_root, prepared_intent_digest?, prepared_next_generation?, prepared_proposed_root?, previous_anchor_digest?, anchor_receipt_digest)`; a genuinely offline mutation must hold the fixed workspace-wide OS lock and compare-and-swap this device-bound sealed state, which is outside workspace backups.
- `AuthorityCommitIntent(id, workspace_id, epoch, deployment_instance_id, prior_generation, next_generation, prior_state_root, mutation_digest, proposed_state_root, state[prepared|anchor_reserved|db_committed|anchor_finalized|quarantined], created_at)`
- `AuthorityCommitReceipt(id, workspace_id, epoch, deployment_instance_id, generation, previous_state_root, state_root, mutation_digest, audit_receipt_hashes_digest, anchor_kind[registry|sealed_local], registry_sequence?, registry_trust_state_metadata_version?, registry_history_head_digest?, registry_freshness_proof_id?, registry_signing_key_version_id?, registry_signature?, local_anchor_receipt_digest?, finalized_at)`; every authority-bearing transaction uses prepare -> external/sealed compare-and-swap -> database commit -> anchor finalize. A crash may replay only the exact prepared digest; missing/ambiguous state quarantines.
- `AuthorityStateRootManifestVersion(id, schema_version, canonicalization_version, manifest_digest, status[active|retired])` and `AuthorityStateRootLeafFamily(manifest_version_id, ordinal, family_name, coverage_kind[in_root|external_proof], external_proof_kind?, canonicalization_version)` form an immutable exhaustive allowlist. No mutable authority-bearing table may exist without one manifest row; startup/migration rejects unlisted families.
- `authority_state_root` is a canonical Merkle root under that manifest over deployment-instance activation/revocation, workspace authority and trust-anchor state, epoch-credential status, lease-certificate revocation, locally mirrored registry-verifier/trust-head state, workspace-signing-key lifecycle, policy, membership/access/agent/delegation and credential grants/revocations, authorization decisions, budget periods/scope/account/reservation/settlement-set/settlement state, kill switches, approval requests/decisions/effect bindings/consumptions, workflow/effect intents, discriminated provider/filesystem bindings, permits, outboxes and attempts, and the new audit-chain head. Registry workspace anchors/trust-state metadata and sealed local anchors are `external_proof` families bound by exact digests/versions in the commit receipt rather than recursively included. Every authoritative mutation or revocation advances a covered leaf or its named external proof; tests independently roll back every family and startup rejects a schema table, manifest omission/duplicate/unknown family, unsupported manifest version, or selective rollback before authority use.
- `AuthorityCloseCertificate(id, workspace_id, closed_epoch, source_deployment_id, source_deployment_instance_id, target_deployment_id, epoch_credential_digest, destruction_or_revocation_attestation_digest, final_authority_generation, final_state_root, workspace_signing_key_version_id, workspace_signature, anchor_registry_id?, registry_sequence?, registry_signing_key_version_id?, registry_signature?, local_anchor_receipt_digest?, anchor_receipt_digest, externally_anchored_at)`
- `AuthorityHandoff(id, workspace_id, from_deployment_id, from_deployment_instance_id, to_deployment_id, to_deployment_instance_id, from_epoch, to_epoch, export_artifact_digest, source_checkpoint_digest, authorization_snapshot_id, authorization_snapshot_digest, source_signing_key_version_id, close_certificate_id, target_epoch_credential_id, state[prepared|source_closed_anchored|target_active|aborted], prepared_at, completed_at?)`; target activation requires the anchored close certificate and cannot reuse an exported/source private capability.
- `RestoreValidationReceipt(id, workspace_id, restored_database_digest, observed_epoch, observed_generation, observed_state_root, trust_anchor_id, former_instance_revocation_digest?, takeover_lease_or_local_anchor_receipt_digest?, decision[read_queue_only|exclusive_takeover_new_epoch], reason_code, validated_at)`; every restore/clone defaults to quarantine, and mutation resumes only under a new epoch after exclusive takeover revokes the former instance. A matching normal restart is not a restore.
- `OfflineIntent(id, workspace_id, observed_authority_epoch, observed_authority_generation, client_context_id, requester_id, agent_id, command_digest, encrypted_payload_artifact_id, ciphertext_sha256, encryption_key_version_id, intent_signature, queued_at, expires_at, status[queued|accepted|rejected|expired], accepted_request_context_id?, rejection_reason_code?)`; the signature binds every field, it conveys no authority, and execution occurs only after reconnect creates a freshly authenticated/authorized request under the current epoch/generation.
- `ClientContext(id, surface[cli|desktop|web|channel], transport_principal_id?, session_id, origin, issued_at, expires_at?)`
- `ExecutionPlacement(id, kind[local_runner|cloud_worker|connector], runner_id?, connector_id?, sandbox_profile, data_policy_digest, status)`
- `ModelRouteSet(id, workspace_id, owner_principal_id?, routes, budget_policy_id, failover_policy, version)`
- `ProviderConnection(id, workspace_id, owner_principal_id?, provider, route_id, credential_ref_id, allowed_placement_ids, status, last_health_at?, version)`
- `CredentialRef(id, workspace_id, owner_principal_id?, provider_connection_id, kind[os_keyring|cloud_vault|provider_oauth|local_connector], opaque_locator, scopes, status, expires_at?, version)`
- `CredentialVersion(id, credential_ref_id, workspace_id, version_number, opaque_version_locator, status[active|rotating|revoked|expired], valid_from, valid_until?, rotated_from_id?, revoked_at?)`
- `ProviderOAuthTransaction(id, workspace_id, provider_connection_id, requester_id, flow[authorization_code_pkce|device_code], state_digest, nonce_digest, pkce_verifier_ref?, device_code_ref?, callback_owner_principal_id, redirect_uri_digest?, expires_at, consumed_at?, status)`
- `ProviderOAuthGrant(id, workspace_id, provider_connection_id, credential_version_id, provider_subject_digest, granted_scopes, refresh_family_id?, status, issued_at, refreshed_at?, revoked_at?)`
- `CredentialGrant(id, workspace_id, provider_connection_id, credential_version_id, request_context_id, agent_grant_id, execution_placement_id, purpose, operations, host_method_path_constraints, use_limit, use_count, issued_at, expires_at, rotation_epoch, revoked_at?, nonce_digest)`
- `EffectiveCapabilities(request_context_id, workspace_authority_epoch, workspace_authority_generation, authority_state_root, authority_commit_receipt_id, actions, unavailable_reason_codes, policy_digest, budget_snapshot_digest, kill_switch_snapshot_digest)`
- Configuration cannot contain plaintext credentials or disable immutable safety, authorization, audit, verification, evidence, limits, or rollback.

### Tenancy and identity
- `Workspace(id, name, status)`
- `WorkspaceMembership(workspace_id, principal_id, role, status)`
- `Project(id, workspace_id, name, root_locator, privacy, status)`
- `Principal(id, kind[user|service|channel], external_provider, external_subject, display_name)`
- `AgentIdentity(id, workspace_id, name, role, model_route, skill_set_digest, status)`
- Scope is a discriminated union with valid parentage encoded by type rather than nullable identifiers:
  - `WorkspaceScope(workspace_id)`
  - `ProjectScope(workspace_id, project_id)`
  - `ChannelScope(workspace_id, channel_id, project_id?)`
  - `ThreadScope(workspace_id, channel_id, thread_id, project_id?)`
  - `ConversationScope(workspace_id, conversation_id, parent_scope_kind, parent_scope_id)` where the parent is a valid project, channel, or thread scope in the same workspace.
- `AudiencePolicySnapshot(id, workspace_id, visibility[personal|explicit_principals|role|project|channel|thread|workspace], owner_principal_id?, principal_ids, role_ids, scope_digest, policy_version, policy_digest, created_by, created_at)` is immutable; personal visibility requires the owner and every referenced principal/role/scope belongs to the workspace.

### Access and acting-agent authority
- `AccessBundle(id, workspace_id, principal_id, scope_kind, scope_id, issued_by, policy_digest, expires_at, revoked_at?)`
- `CapabilityGrant(bundle_id, workspace_id, resource_kind, resource_id, action, effect[allow|deny], constraints_json)`
- `AgentGrant(id, workspace_id, agent_id, capability_bundle_id, autonomy_level, issued_by, expires_at?, revoked_at?)`
- `DelegationGrant(id, parent_agent_grant_id, child_agent_id, capabilities, budget_json, depth_limit, issued_at, expires_at, revoked_at?)`
- Deny wins. No grant means deny. Scope matching must never broaden a grant.
- Effective authority is the minimum intersection of current workspace authority epoch, discriminated scope/audience, requester, acting-agent/delegation, channel/routine, workspace policy, budget, autonomy ceiling, execution placement, credential grant, and kill-switch state at claim, model/tool call, approval, and external effect time.
- Short-lived signed transport tokens may reference an access bundle but cannot replace server-side bundle/revocation checks.

### Requests, audit, and approvals
- `WorkspaceSigningKeyVersion(id, workspace_id, key_epoch, algorithm, public_key, non_exportable_private_key_ref, status[active|rotated|revoked|compromised], valid_from, valid_until?, revoked_at?, compromise_effective_at?, predecessor_digest?, attestation_digest)`; `(workspace_id, key_epoch)` is unique.
- `AuthorizationDecisionSnapshot(id, workspace_id, request_context_id, deployment_instance_id, authority_epoch_credential_id, authority_generation, authority_state_root, authority_commit_receipt_id, authority_proof_digest, membership_version_ids, membership_digest, scope_kind, scope_id, scope_digest, audience_policy_snapshot_id, audience_digest, requester_id, transport_principal_id, access_bundle_id, access_bundle_version_digest, agent_grant_id, delegation_grant_ids, agent_delegation_digest, execution_placement_id?, provider_connection_id?, credential_grant_id?, credential_version_id?, policy_digest, autonomy_policy_digest, budget_snapshot_ids, budget_snapshot_digest, kill_switch_snapshot_ids, kill_switch_snapshot_digest, decision, reason_code, canonical_inputs_json, source_record_version_map, canonical_digest, signing_key_version_id, snapshot_signature)` is immutable, signed, referentially resolvable, and self-contained: evaluated roles/capabilities/constraints/statuses are copied canonically rather than recoverable only from mutable source rows.
- `RequestContext(id, deployment_profile_id, deployment_instance_id, workspace_id, workspace_authority_epoch, workspace_authority_generation, authority_state_root, authority_epoch_credential_id, authority_commit_receipt_id, authority_proof_digest, scope_kind, scope_id, audience_policy_snapshot_id, audience_policy_digest, requester_id, client_context_id, transport_principal_id?, agent_id, agent_grant_id, access_bundle_id, execution_placement_id?, policy_digest, authorization_snapshot_id, authorization_snapshot_digest, authorization_signing_key_version_id, idempotency_key, correlation_id)`
- Request context and authorization snapshot are allocated, canonicalized, signed, and linked in one transaction using deferrable references; allow and deny decisions both preserve the evaluated inputs. Later revocation checks create new receipts/snapshots and never rewrite history.
- `IdempotencyEnvelope(id, workspace_id, requester_id, transport_principal_id, agent_id, agent_grant_id, operation, idempotency_key, request_context_digest, payload_digest, status[in_progress|succeeded|failed], result_digest?, result_ref?, created_at, completed_at?)`; the composite identity is unique, creation/result commit is atomic with the command, payload mismatch fails, and cached results are returned only after current read authorization.
- `AuditReceipt(id, workspace_id, workspace_sequence, schema_version, prior_authority_epoch, prior_authority_generation, prior_authority_state_root, prior_authority_commit_receipt_id, authority_commit_intent_id, intended_mutation_digest, request_context_id, authorization_snapshot_id, authorization_snapshot_digest, action, resource, decision, reason_code, policy_digest, sanitized_input_digest, output_digest?, effect_payload_version_ids, effect_payload_commitment_digests, effect_attempt_ids, cost_json, evidence_ids, signing_key_version_id, previous_hash, receipt_hash, receipt_signature)`; `(workspace_id, workspace_sequence)` is unique and monotonic. The canonical receipt/hash/signature names only prior authority state plus the intended mutation and never includes a not-yet-derived resulting root or commit receipt.
- `AuditAnchorBinding(id, workspace_id, audit_receipt_id, receipt_hash, authority_commit_intent_id, authority_commit_receipt_id, resulting_authority_epoch, resulting_authority_generation, resulting_authority_state_root, commit_receipt_digest, binding_digest, signing_key_version_id, binding_signature, bound_at)` is immutable derived evidence. It is excluded from the state-root leaves because it depends on the result, is never an authorization input, and is verified against the signed external/sealed commit receipt; deterministic crash recovery inserts exactly the missing binding or quarantines.
- `AuditCheckpoint(id, workspace_id, prior_authority_epoch, prior_authority_generation, prior_authority_state_root, prior_authority_commit_receipt_id, through_sequence, receipt_hash, schema_version, checkpoint_authorization_snapshot_id, checkpoint_authorization_snapshot_digest, covered_authorization_snapshot_set_digest, covered_effect_payload_set_digest, signing_key_version_id, signature, previous_checkpoint_digest?, audit_anchor_binding_id?, anchored_at?)`; a checkpoint is committed through the same prior-state receipt plus post-commit anchor-binding sequence.
- Non-circular commit order is normative: allocate sequence and `AuthorityCommitIntent` against the prior generation/root; canonicalize and sign the receipt against that prior state and intended mutation; include `receipt_hash` as the proposed new audit-head leaf; compute the proposed root; reserve it with the external/sealed compare-and-swap; commit domain rows, receipt, and new head atomically; finalize the anchor; then write the immutable `AuditAnchorBinding` verified against the finalized receipt. Crash injection at every boundary must either replay the exact prepared digest/binding or quarantine—never omit receipt coverage or mutate a signed receipt after hashing.
- Signing verification resolves the durable public-key version and signing time. Rotated keys cannot sign new records but retain historical validity inside their interval. Revoked keys cannot sign at/after `revoked_at`; compromise invalidates signatures at/after `compromise_effective_at`, and the latest uncompromised key must re-anchor the preceding chain. New signatures from revoked/expired/compromised keys fail closed.
- `ApprovalRequest(id, workspace_id, request_context_id, action_kind[filesystem_apply|filesystem_undo|provider_effect|capability_promotion], subject_kind[effect_intent|bundle|promotion], subject_id, bundle_artifact_id?, bundle_digest?, manifest_digest?, destination_root_digest?, original_apply_effect_intent_id?, required_reviewer_role, status[pending|approved|denied|expired|consumed|revoked], expires_at, nonce_digest, version)`; subject/action/digests are immutable after review begins.
- `ApprovalDecision(id, workspace_id, approval_request_id, approval_request_version, reviewer_id, decision[approved|denied], rationale, decided_at, decision_digest, signing_key_version_id, decision_signature)` is immutable with unique `approval_request_id`; it signs the exact request version, subject, action, nonce and expiry.
- `EffectApprovalBinding(effect_intent_id, workspace_id, approval_request_id, approval_decision_id, approval_request_version, approval_decision_digest, required_reviewer_role)` has unique `approval_request_id` and `approval_decision_id`; it binds one approval to one effect intent rather than a reusable action label.
- `ApprovalConsumption(id, workspace_id, approval_request_id, approval_decision_id, effect_intent_id, effect_permit_id, consumed_by_request_context_id, consumed_at)` is immutable with unique `approval_request_id`, unique `approval_decision_id`, and unique `(effect_permit_id, approval_request_id)`. Permit claim locks the complete sorted approval set, reauthorizes the acting request, verifies each exact subject/action/digest, reviewer separation/signature, current request version, `approved` state, nonce and unexpired/unrevoked status, then inserts all consumptions and changes every request `approved -> consumed` in the same transaction.
- Implementer/reviewer separation is validated server-side; filesystem apply and undo each require their own exact current approval, and an undo approval is bound to the original successful apply effect/attempt and rollback snapshot.

### Outcomes, durable workflow graphs, limits, and lineage
- `OutcomeContract(id, workspace_id, name, status, latest_version_number)`
- `OutcomeContractVersion(id, outcome_contract_id, workspace_id, version_number, canonical_digest, acceptance_criteria, evidence_schema, required_permissions, budget_json, runtime_limits_json, verifier_policy_digest, created_by, created_at)` is immutable.
- `WorkflowGraph(id, workspace_id, project_id?, outcome_contract_version_id, outcome_contract_digest, authority_epoch, state, recovery_policy, budget_json, version)`
- `WorkItem(id, workflow_graph_id, workspace_id, project_id?, parent_id?, kind, state, priority, payload_json, required_capabilities, budget_json, runtime_limit_json, max_attempts, attempt_count, available_at, version)`
- `WorkDependency(work_item_id, dependency_id, condition)`
- `WorkLease(work_item_id, worker_id, authority_epoch, lease_fence, lease_token_digest, acquired_at, heartbeat_at, expires_at)`; the fence increases on every acquisition and must accompany every state mutation/effect request.
- `WorkAttempt(id, work_item_id, agent_id, started_at, finished_at?, outcome?, error_code?, cost_json, evidence_ids)`
- `WorkflowCheckpoint(id, workflow_graph_id, work_item_id?, state_digest, artifact_ids, created_by, created_at)`
- `RecoveryDecision(id, work_item_id, trigger[retry_exhausted|stuck|verification_failed|dependency_failed], action[retry|replan|decompose|pause|fail], rationale, approved_by?, created_at)`
- `EffectPayloadVersion(id, workspace_id, schema_version, canonicalization_version, ciphertext_artifact_id, ciphertext_sha256, encryption_key_version_id, plaintext_commitment_key_version_id, canonical_plaintext_hmac, sanitized_projection_digest, created_by_request_context_id, created_at)` is immutable and content-addressed. The HMAC commits to the exact schema-validated canonical plaintext; sanitization is display/audit evidence only and never the dispatch identity.
- `EffectBinding(id, workspace_id, effect_intent_id, binding_kind[provider|filesystem_apply|filesystem_undo], binding_digest)` is immutable with unique `effect_intent_id`. A mandatory deferrable constraint trigger requires exactly one matching subtype row and rejects mixed/synthetic provider credentials.
- `ProviderEffectBinding(effect_binding_id, workspace_id, provider_connection_id, credential_grant_id, credential_version_id, provider_operation, provider_idempotency_key?)` has primary key `effect_binding_id` and same-workspace composite foreign keys; it is valid only for `binding_kind=provider`.
- `FilesystemDeliveryEffectBinding(effect_binding_id, workspace_id, bundle_artifact_id, bundle_digest, manifest_digest, destination_root_digest, rollback_snapshot_artifact_id, rollback_snapshot_digest, approval_set_digest, original_apply_effect_intent_id?, original_apply_attempt_id?)` has primary key `effect_binding_id`; apply requires null original-apply fields, while undo requires an original successful apply/attempt and exactly its bundle/manifest/destination/rollback digests. It is valid only for the matching filesystem binding kind and carries no provider or credential reference.
- `ExternalEffectIntent(id, workspace_id, workflow_graph_id, work_item_id, work_attempt_id, request_context_id, authorization_snapshot_id, authorization_snapshot_digest, authorization_signing_key_version_id, requester_id, access_bundle_id, agent_grant_id, delegation_grant_ids, execution_placement_id, authority_epoch, authority_generation, authority_state_root, authority_commit_receipt_id, lease_fence, semantic_idempotency_key, effect_kind[provider|filesystem_apply|filesystem_undo], effect_binding_digest, target_digest, effect_payload_version_id, payload_schema_version, payload_canonicalization_version, ciphertext_sha256, canonical_plaintext_hmac, required_capabilities_digest, kill_switch_snapshot_ids, state[pending|dispatching|succeeded|failed|outcome_unknown|cancelled|compensating|compensated], created_at)` has unique `(workspace_id, semantic_idempotency_key)`; the semantic key canonicalizes workspace, effect kind/binding, target, payload commitment, scope and operation. A deferrable one-to-one constraint requires the matching immutable `EffectBinding`.
- `BudgetPeriod(id, workspace_id, unit, period_kind, period_sequence, starts_at, ends_at, status[scheduled|active|closed], previous_period_id?, version)` with unique `(workspace_id, unit, period_kind, period_sequence)`, `starts_at < ends_at`, and a PostgreSQL exclusion constraint preventing overlapping `scheduled|active` ranges for one workspace/unit/kind; SQLite performs the same check while holding the fixed single-process authority lock.
- `BudgetScopeNode(id, workspace_id, scope_kind[workspace|project|routine|channel|run|run_agent], scope_owner_id, scope_rank)` has unique `(workspace_id, scope_kind, scope_owner_id)` and a rank check. A PostgreSQL deferrable constraint trigger maps each discriminant to its authoritative owner table, verifies the owner and all referenced ancestors belong to the workspace, and rejects a node without exactly one valid typed owner; SQLite performs the identical validator while holding the fixed authority lock.
- `BudgetScopeParent(workspace_id, child_scope_node_id, parent_scope_node_id)` and `BudgetScopeClosure(workspace_id, ancestor_scope_node_id, descendant_scope_node_id, depth)` are generated only by that serialized validator from real ownership: workspace -> project -> routine/channel -> run -> run-agent. They are not application-writable; same-workspace composite foreign keys, one immediate parent, cycle rejection, and canonical nearest-parent rules make unrelated project/routine/run/agent links impossible.
- `BudgetAccount(id, workspace_id, budget_scope_node_id, unit, budget_period_id, limit_amount, reserved_amount, settled_amount, status[active|frozen|closed], version)` stores signed 64-bit integer canonical base units, has unique `(workspace_id, budget_scope_node_id, unit, budget_period_id)`, and checks `0 <= limit_amount`, `0 <= reserved_amount`, `0 <= settled_amount`, and `reserved_amount + settled_amount <= limit_amount`.
- `BudgetAccountParent(workspace_id, child_account_id, parent_account_id)` has unique `child_account_id` and composite same-workspace foreign keys. A mandatory deferrable PostgreSQL constraint trigger (or the SQLite fixed-lock validator) requires both accounts to share `unit`, requires their scope nodes to match one canonical `BudgetScopeParent`/ancestor relation, and requires the parent period window to contain the child window even when period kinds differ; cross-unit, unrelated-scope, inactive-window, reversed-rank, and ambiguous-parent rows fail closed.
- `BudgetAccountClosure(workspace_id, ancestor_account_id, descendant_account_id, depth)` has unique `(ancestor_account_id, descendant_account_id)`, same-workspace foreign keys and check `(depth = 0) = (ancestor_account_id = descendant_account_id)`. Parent/account/closure insertion is one serializable transaction and must equal the canonical scope closure for the configured account path.
- `BudgetReservationSet(id, workspace_id, effect_intent_id, request_context_id, authorization_snapshot_id, leaf_account_id, unit, canonical_reserved_amount, canonical_account_closure_digest, expected_reservation_count, state[held|claimed|settled|released|expired], expires_at, version)` has unique `effect_intent_id`, unique `(id, workspace_id, unit, canonical_reserved_amount)`, unique `(id, workspace_id, unit, canonical_reserved_amount, canonical_account_closure_digest, expected_reservation_count)`, and checks `canonical_reserved_amount > 0` plus `expected_reservation_count > 0`. One intent exclusively owns the set; creation serializably locks the current canonical account closure, records its digest/cardinality, and reserves exactly the same positive amount in every account or fails all.
- `BudgetReservation(id, workspace_id, reservation_set_id, account_id, unit, amount, state[held|claimed|settled|released|expired], expires_at, version)` has unique `(reservation_set_id, account_id)`, unique `id`, and `amount > 0`. Its composite foreign key `(reservation_set_id, workspace_id, unit, amount)` references the set's `(id, workspace_id, unit, canonical_reserved_amount)`, so every leaf/ancestor row carries the identical unit and amount. A deferrable closure trigger rejects missing, extra or duplicate rows and requires row count/digest to equal the set's canonical closure before `held`.
- `BudgetUsageSettlementSet(id, workspace_id, reservation_set_id, effect_attempt_id?, unit, reserved_amount, actual_amount, released_amount, canonical_account_closure_digest, expected_settlement_count, settled_at)` is immutable with unique `reservation_set_id` and unique `(id, workspace_id, reservation_set_id, unit, reserved_amount, actual_amount, released_amount)`. Composite foreign key `(reservation_set_id, workspace_id, unit, reserved_amount, canonical_account_closure_digest, expected_settlement_count)` references the reservation set's `(id, workspace_id, unit, canonical_reserved_amount, canonical_account_closure_digest, expected_reservation_count)`, and checks `reserved_amount > 0`, `actual_amount >= 0`, `released_amount >= 0`, `actual_amount + released_amount = reserved_amount`, and `expected_settlement_count > 0`.
- `BudgetUsageSettlement(id, workspace_id, settlement_set_id, reservation_set_id, reservation_id, account_id, unit, reserved_amount, actual_amount, released_amount, settled_at)` is immutable with unique `reservation_id`. Composite foreign key `(settlement_set_id, workspace_id, reservation_set_id, unit, reserved_amount, actual_amount, released_amount)` references the settlement-set unique tuple; other composite keys bind the reservation/account/set identity. Thus every row has identical unit and amounts. A deferrable trigger requires settlement row count, account IDs and closure digest to equal the complete reservation set; account counters, all rows and set transition commit atomically. Cancellation/expiry uses `actual_amount=0`; post-dispatch usage is never erased or reversed.
- `KillSwitch(id, workspace_id, scope_kind[workspace|agent|workflow|run], scope_id, state[armed|stopping|stopped|clear], reason, activated_by, activated_at?, cleared_by?, cleared_at?, version)` with unique `(workspace_id, scope_kind, scope_id)`; this materialized row is the lock/version authority for the scope.
- `EffectPermit(id, effect_intent_id, workspace_id, effect_binding_id, effect_binding_digest, approval_set_digest, reservation_set_id, authority_epoch, authority_generation, authority_state_root, authority_commit_receipt_id, lease_fence, authorization_snapshot_id, effect_payload_version_id, ciphertext_sha256, canonical_plaintext_hmac, kill_switch_versions, state[available|claimed|cancelled|consumed], claimed_by?, claimed_at?, consumed_at?)`; `effect_intent_id`, `effect_binding_id`, and `reservation_set_id` are each globally unique, so one intent/binding has at most one permit and no reservation set can authorize two permits. A deferrable trigger requires `effect_binding_digest` and sorted `approval_set_digest` to equal the immutable binding/subtype and complete `EffectApprovalBinding` set. The empty digest is allowed only when policy requires no approval; filesystem apply/undo requires a non-empty set equal to `FilesystemDeliveryEffectBinding.approval_set_digest`.
- `EffectPermitBudgetReservation(permit_id, workspace_id, reservation_set_id, reservation_id, account_id, unit, amount, account_version, reservation_version)` has unique `(permit_id, account_id)` and unique `reservation_id`. Composite foreign key `(reservation_set_id, workspace_id, unit, amount)` targets the set's `(id, workspace_id, unit, canonical_reserved_amount)`; other composite keys bind permit, reservation and account to that same workspace/set/unit. Permit creation proves these rows equal the set's complete current canonical closure/digest/cardinality before it becomes `available`.
- `ExternalEffectAttempt(id, effect_intent_id, attempt_number, gateway_id, request_context_id, authorization_snapshot_id, authorization_signing_key_version_id, effect_binding_id, effect_binding_digest, authority_epoch, authority_generation, authority_state_root, authority_commit_receipt_id, lease_fence, authorization_receipt_id, effect_payload_version_id, ciphertext_sha256, canonical_plaintext_hmac, dispatch_request_digest, state[started|succeeded|failed|outcome_unknown|cancelled|compensated], started_at, finished_at?, result_digest?, error_code?, compensation_attempt_id?)` has unique `(effect_intent_id, attempt_number)`. The selected adapter resolves the immutable provider or filesystem subtype; generic attempts never require or synthesize provider credentials.
- `EffectOutbox(id, effect_intent_id, effect_binding_id, effect_binding_digest, effect_payload_version_id, ciphertext_sha256, canonical_plaintext_hmac, dispatch_after, state[pending|claimed|delivered|outcome_unknown|cancelled], fence, claimed_by?, delivered_at?)`; `effect_intent_id` and `effect_binding_id` are unique, so one intent/binding has at most one outbox row.
- `LineageNode(id, workspace_id, kind[source|model_call|tool_call|test_evidence|approval|approval_consumption|audit_receipt|authorization_snapshot|effect_binding|effect_payload|effect_attempt|artifact], immutable_record_id, canonical_digest)`
- `ArtifactLineageEdge(id, workspace_id, artifact_id, from_node_id, to_node_id, relation, edge_digest)`; completion verifies a referentially constrained digest closure over immutable nodes and parent artifacts.
- State machine: `queued -> leased -> running -> waiting_approval|waiting_dependency|paused -> verifying -> packaging -> completed|failed|cancelled|expired`.
- Compare-and-swap version and lease fence prevent stale state mutation. Heartbeats and persisted progress trigger stuck detection. Recovery is bounded `retry -> replan -> decompose`; exhaustion pauses or fails honestly.
- Only the centralized effect gateway may dispatch provider or filesystem effects; adapters cannot invent a parallel delivery path or synthetic provider credentials.
- Permit claim locks records in canonical order: workspace authority/commit receipt, sorted kill-switch rows, sorted budget-period/scope/account/closure/reservation-set/reservation rows, sorted approval requests/decisions/effect-approval bindings, effect binding plus its one subtype, effect intent, permit plus normalized permit-reservation rows, approval-consumption uniqueness keys, then outbox.
- In one serializable transaction the gateway verifies the current external/sealed authority generation/root; request and immutable authorization snapshot; requester/access/agent/delegation grants; placement; non-overlapping budget period; same-unit canonical scope/period containment; positive set amount; identical complete closure reservations and cardinality; exclusive one-intent/one-permit ownership; kill switches; lease fence; immutable payload and effect-binding digests; and semantic key.
- For a provider binding it additionally verifies the exact provider connection/credential grant/version, provider operation and idempotency key. For a filesystem apply/undo binding it forbids provider fields, rehashes the exact bundle/manifest/destination/rollback snapshot, verifies the undo's original successful apply/attempt, locks and revalidates every bound approval request/decision, checks current revocation/reviewer separation/signature/version/nonce/expiry and inserts one-time `ApprovalConsumption` while changing `approved -> consumed`.
- The same transaction moves the reservation set and every row `held -> claimed`, inserts the attempt, claims the outbox and freezes all authority/binding/payload/approval/budget versions. Dispatch re-resolves the immutable subtype only after commit. Completion atomically consumes the permit, settles the complete reservation set through one `BudgetUsageSettlementSet` plus equal per-account rows, records result/evidence/lineage/audit, advances the authority root and marks the intent terminal; pre-dispatch cancellation/expiry releases the full set with zero actual usage. Any mismatch or partial closure/approval/settlement transition fails closed.
- Provider idempotency is mandatory for automatic retry after dispatch. If dispatch may have reached a provider but no authoritative result exists, the attempt and outbox become `outcome_unknown`; automatic retry is forbidden and reconciliation or an explicitly approved at-most-once failure path is required. Compensation is remediation, never treated as duplicate prevention.
- Outcome completion requires the pinned contract version's evidence, permissions, budget, and runtime limits plus a verified immutable lineage closure. Kill switches and budget state are enforced transactionally at effect-intent creation and rechecked at permit claim, approval, and completion.

### Routines and triggers
- `Routine(id, workspace_id, project_id?, name, trigger_type, trigger_config, command_template, access_bundle_id, budget_json, enabled)`
- `TriggerReceipt(id, routine_id, external_event_id, payload_digest, received_at)` with unique dedupe key.
- Every trigger creates a normal `RequestContext` and `WorkItem`.

### Context firewall and memory governance
- `ExternalContent(id, workspace_id, source_kind, source_locator_digest, content_digest, trust_class[untrusted|reviewed|trusted], provenance_json, sanitized_at?, expires_at?)`
- `ContextEnvelope(id, workspace_id, owner_kind[request_context|legacy_run], owner_id, system_instruction_digest, trusted_context_ids, untrusted_content_ids, firewall_policy_digest, output_digest?)`; a request-owned envelope must reference a same-workspace `RequestContext`, while legacy ownership is read-only migration provenance.
- External content is data, never instruction. The firewall preserves provenance, separates instruction/context channels, labels untrusted spans, bounds content, and prevents retrieved content from granting tools, secrets, permissions, or autonomy.
- Memory scope is a workspace-bound discriminated union: `PersonalMemoryScope(workspace_id, owner_principal_id)`, `ThreadMemoryScope(workspace_id, channel_id, thread_id)`, `ProjectMemoryScope(workspace_id, project_id)`, `ChannelMemoryScope(workspace_id, channel_id)`, or `WorkspaceMemoryScope(workspace_id)`.
- `MemoryRecord(id, workspace_id, memory_scope_kind, memory_scope_id, owner_principal_id?, visibility[owner_only|explicit_principals|role|scope_members], audience_policy_snapshot_id, kind, encrypted_content_ref, encryption_key_version, content_digest, source_record_id?, source_digest, provenance_json, confidence, status, expires_at?, deleted_at?)`; personal memory requires the same-workspace owner.
- `MemoryPromotion(id, workspace_id, source_memory_id, source_digest, target_workspace_id, target_scope_kind, target_scope_id, target_audience_policy_snapshot_id, requested_by, reviewed_by?, status, rationale)`
- `MemoryExport(id, workspace_id, requested_by, scope_filter, format, artifact_id, completed_at?)`
- `MemoryDeletion(id, workspace_id, requested_by, scope_filter, reason, encryption_key_version, primary_delete_receipt_id, index_cache_receipt_ids, retention_exception_receipt_ids, completed_at?)`
- Reads require scope membership and explicit capability. Promotion preserves source and review. Export/deletion are authorized, auditable, and tested across active stores, indexes, caches, and backups according to retention policy.

### Versioned skills, autonomy, and shadow promotion
- `Skill(id, workspace_id, name)`
- `SkillVersion(id, skill_id, version, content_digest, source, permissions, capability_manifest_id, status[draft|shadow|canary|active|rolled_back|failed], created_by, reviewed_by?)`
- `SkillEvaluation(id, skill_version_id, suite_version, fixture_digest, hidden_holdout_digest?, results_json, evaluator_id, evaluated_at)`
- `SkillPromotion(id, skill_version_id, from_stage, to_stage, evidence_ids, requested_by, approved_by?, canary_policy, rollback_version_id, status)`
- `SkillRegression(id, skill_version_id, baseline_version_id, metric, threshold, observed, detected_at, rollback_receipt_id?)`
- `AgentVersion(id, workspace_id, agent_id, version, parent_version_id?, instruction_digest, configuration_digest, skill_set_digest, governed_memory_export_id?, model_requirement_digest, capability_requirement_digest, evaluation_evidence_set_digest, created_by, created_at)` is an immutable composition record. It references prompts/instructions, configuration, approved skill versions, an optional explicitly authorized governed-memory export, model requirements, capability requirements and evaluation evidence; it is not a model-weight checkpoint.
- `PortableAgentPackage(id, workspace_id, agent_version_id, manifest_digest, artifact_id, signing_key_version_id, signature, requested_by, approved_by?, status[draft|approved|revoked], created_at)` is the signed, content-addressed export users save when they approve a self-improved version.
- `AgentInstallation(id, target_workspace_id, target_scope_kind, target_scope_id, target_conversation_id?, portable_agent_package_id, installed_agent_id, pinned_agent_version_id, prior_agent_version_id?, requested_by, approved_by, effective_capabilities_digest, status[pending|active|blocked|rolled_back|revoked], installed_at?)` records import into another chat/scope and supports explicit rollback.
- Self-improvement may create draft candidate versions only. Saving/exporting requires an explicit user action plus the normal evaluation, review and promotion evidence for the version's risk. Import verifies the package digest/signature and compatibility, re-runs destination policy/effective-capability resolution, and requires explicit destination approval before activation.
- Packages never contain model-provider credentials, secret values or references usable outside their original authorization context, access/agent/delegation grants, approvals, authority keys, session tokens, raw chat history, or private/team memory without an explicit authorized `MemoryExport`. Import never raises autonomy or capabilities and never inherits source-workspace authority; unavailable models or capabilities produce a blocked/degraded reason and require evaluation before activation.
- `AutonomyPolicy(id, workspace_id, level[advise|observe|sandbox|propose|apply|bounded_delegation], allowed_actions, evidence_thresholds, reliability_thresholds, budget_limits, runtime_limits, version)`
- `ShadowEvaluation(id, subject_kind[agent|skill|workflow], subject_id, proposal_digest, approved_action_digest?, outcome, metric_json, evaluated_at)`
- `CapabilityManifest(id, provider, name, version, operations, risk_class, input_schema_digest, network_constraints, secret_requirements)`
- Normative default-deny action/effect matrix:

  | Level | Observe/read | Simulated or sandbox effects | Production effects | Delegation |
  |---|---|---|---|---|
  | `advise` | approved context only | none; return advice | forbidden | forbidden |
  | `observe` | approved read-only tools | recorded read-only calls only | forbidden | forbidden |
  | `sandbox` | approved reads | isolated fake/sandbox effects | forbidden | forbidden |
  | `propose` | approved reads | may construct plans/diffs/effect intents without dispatch | forbidden | forbidden |
  | `apply` | approved reads | allowed | only explicitly granted effects through the gateway | forbidden |
  | `bounded_delegation` | approved reads | allowed | only explicitly granted effects through the gateway | child grants are a strict capability/budget/depth subset |

- Shadow runs never receive production credential grants and use recorded/fake/read-only effects. Only separately identified, explicitly budgeted canaries may perform narrowly scoped reversible production effects through the effect gateway with independent approval and compensation. Promotion requires versioned evaluation, independent review for privileged capability, canary limits, regression monitoring, and automatic/manual rollback.

### Budgets
- Canonical versioned `BudgetPeriod` rows define time windows once per workspace/unit/kind. PostgreSQL excludes overlapping scheduled/active ranges; SQLite enforces the same invariant under the fixed single-process authority lock before it may call that mode supported.
- Typed `BudgetScopeNode` ownership is validated against real same-workspace domain rows; generated scope parent/closure rows define the only legal workspace -> project -> routine/channel -> run -> run-agent ancestry. Account parents must follow that ancestry, share a unit, and use a parent period window that contains the child window; cross-unit, unrelated-scope, cyclic, ambiguous and inactive-window links fail closed.
- All budget amounts are signed 64-bit integer canonical base units. Effective availability is the minimum remaining allowance across the selected leaf and complete canonical account closure. One effect intent exclusively owns one `BudgetReservationSet` with one unit, one strictly positive canonical amount, closure digest and cardinality; creation locks the sorted closure and reserves exactly that same amount in every account or fails all.
- Each permit exclusively owns that set and normalized `EffectPermitBudgetReservation` rows. Composite foreign keys and deferrable closure/cardinality triggers require every reservation and permit row to equal the set's workspace/unit/positive amount and complete current account closure; two permits cannot share a row, set or capacity.
- One immutable `BudgetUsageSettlementSet` supplies the common positive reserved, nonnegative actual and nonnegative released amounts with `actual + released = reserved`; every per-account settlement row is constrained to those identical values and the full reservation closure. Set state, every reservation/account counter and all settlement rows transition atomically exactly once. Cancellation/expiry records zero actual/full release; post-dispatch usage is never reversed or erased. Database constraints, canonical locks and serializable tests cover negative/zero values, amount/unit skew, missing/extra closure rows, cardinality mismatch, cross-unit/unrelated-scope links, period containment/rollover, hierarchy mutation, shared-set races, double/partial settlement, release/kill and concurrent effects.

## Authoritative Events and State Machine

### Event envelope V2
Every durable event includes:
- `schema_version`
- `event_id`
- workspace-global monotonic `sequence`
- `workspace_id`
- `workspace_authority_epoch`
- `workspace_authority_generation`, `authority_state_root`, `authority_commit_receipt_id`, and `deployment_instance_id`
- immutable discriminated `scope_kind` and `scope_id`
- immutable `audience_policy_snapshot_id`, `audience_policy_digest`, and visibility level
- `run_id`/`workflow_graph_id` when applicable
- `work_item_id` when applicable
- `request_context_id`
- `requester_id`
- `agent_id` and acting `agent_grant_id`
- `event_type`
- `phase/state`
- timestamp
- redaction status
- structured payload
- previous hash
- event hash

### Rules
1. Event append and state transition occur in one database transaction.
2. Invalid state transitions are rejected before event creation.
3. Every live and replayed event is authorized against its persisted immutable scope and resolvable `AudiencePolicySnapshot`; workspace membership alone cannot reveal private personal/thread/conversation events.
4. Redaction and context-firewall labeling happen before hashing/persistence.
5. Clients derive display state from snapshots plus events but never write state directly.
6. `run.completed` can only follow passed required criteria, valid lineage, and a valid package/evidence record.
7. SSE uses an opaque workspace-scoped cursor and `Last-Event-ID`; the protocol defines snapshot-plus-replay, retention-gap reset, heartbeats, backpressure/slow-client limits, terminal disconnect behavior, and version compatibility.
8. Cross-thread, cross-conversation, cross-project, cross-workspace, cursor-tampering, gap, duplicate, and reconnect tests are mandatory.

## API and Client Architecture

### Authority and placement resolution
- Startup validates one `DeploymentProfile` and opens only its approved adapters. A deployment may serve several client surfaces simultaneously.
- Every request resolves the active deployment instance, epoch credential, exclusive lease/OS lock, exact external/sealed authority generation/state root/commit receipt, `WorkspaceConfig`, discriminated scope, audience snapshot, `ClientContext`, requester/access bundle, acting-agent/delegation grant, policy, budget, kill switches, model route, credential grant, and requested `ExecutionPlacement` into `EffectiveCapabilities`.
- Workspace collaboration mode changes only through an authorized migration; it does not redefine deployment identity.
- Concurrent tasks may select different permitted execution placements without changing client authority.
- `embedded_local` may bootstrap one private individual workspace with SQLite and OS keyring only when a fixed workspace-wide OS lock and device-bound sealed monotonic generation/root provider are available; otherwise authority-bearing offline mutation is unsupported and fails closed.
- Self-hosted/team/cloud operation requires explicit authentication, TLS when network-exposed, PostgreSQL before multi-worker operation, and the same isolation tests.
- One composition root provides transport-neutral command/query/event ports plus in-process and HTTP Python clients; import-boundary tests prevent CLI/TUI/API/UI adapters from owning policy or persistence.

### FastAPI
- Cloud uses OIDC Authorization Code + PKCE, secure rotating session cookies, CSRF protection, and revocable API/service tokens. Self-hosting defines first-admin bootstrap, trusted OIDC configuration, recovery, TLS trust, rotation, and revocation before network exposure.
- Authentication maps each session/token/channel/connector to a `Principal`; workspace creation is authorized explicitly rather than inferred from login.
- Authorization resolves the exact `RequestContext`, `AccessBundle`, `AgentGrant`, and `EffectiveCapabilities` server-side.
- Mutating commands require a persisted `IdempotencyEnvelope` keyed by requester, transport principal, acting agent/grant, workspace, operation, and idempotency key. The request-context and payload digests, status, and result reference commit atomically with the command; payload-digest mismatches fail and concurrent duplicates commit once. A retry never receives a cached result until current authorization confirms that requester, transport principal, agent grant, scope, audience, and access bundle may still read it.
- Initial endpoints:
  - `GET /api/v2/meta`
  - `POST /api/v2/projects`
  - `GET /api/v2/projects/{id}`
  - `POST /api/v2/work-items`
  - `GET /api/v2/work-items/{id}`
  - `GET /api/v2/events?cursor=<opaque>` (SSE)
  - `POST /api/v2/access/explain` (safe decision explanation, no secret policy dump)
- OpenAPI is the source for TypeScript contracts; Python in-process/HTTP parity tests use the same command fixtures.

### Web
- Vite + React + TypeScript, TanStack Router and Query.
- `packages/client-ui` owns accessible layout/components and consumes generated contracts.
- The first web slice authenticates, lists/creates a project, submits a work item, reconnects to SSE, and renders only persisted events.

### Desktop
- Tauri wraps the stabilized React client/UI package only after CLI/web team collaboration and provider-connection contracts pass.
- Desktop connects to one explicit authoritative workspace endpoint; switching endpoints does not synchronize or create a second authority.
- Session material stays in the OS credential store. Native commands are allowlisted; there is no generic shell/filesystem bridge.
- Local mode may supervise a signed loopback sidecar with an ephemeral token, strict origins, bounded lifecycle, visible status, protocol compatibility checks, and rollback-capable updates.

### Third-party channels
- Channel adapters are thin authenticated ingress/egress clients added after team/provider contracts stabilize.
- Signed ingress/webhook verification, durable provider-event deduplication, rate limits, bounded egress, reply correlation, and explicit human identity mapping are mandatory.
- A channel binding maps provider/guild/channel/thread to immutable Corvus scope, workspace/project, allowed agent grant, budget, and access bundle.
- Messages never carry reusable model credentials and cannot broaden scope, autonomy, credential, or access grants.
- High-risk approvals require browser/desktop step-up authentication by default; channel reactions/messages alone do not authorize apply or privileged external effects.

## Implementation Approach and Checkpoints

### Milestone 0 — Reproducible V1 baseline
1. Add `pyproject.toml` with Python `>=3.12,<3.14`, runtime dependencies inferred from imports, CLI entry point, Ruff/Pytest configuration, and dev dependencies.
2. Generate and commit `uv.lock`.
3. Add `README.md` with isolated Windows invocation and fail-closed sandbox statement.
4. Add golden smoke/JSON-schema tests for every public V1 command, plus focused event-chain, path-traversal, sandbox-option, and delivery-approval regression tests.
5. Define V2 identifier rules, command envelope, event envelope, error schema, protocol version, and database migration version before client work.
6. Freeze immutable hashed V1 exports and database fixtures for config/onboarding, provider metadata and keyring references, user/project policy and autonomy YAML, memories, skills, conversations/run events, bundles, artifacts, backups, and every public command/JSON shape. Never rewrite the source fixture.
7. Replace runtime `create_all()` with one version-aware bootstrap that detects new, unstamped, current, partially created, and incompatible databases; require backup, explicit stamp/upgrade, quarantine, restore, and failure behavior.
8. Add a sealed staging/quarantine capture importer that records canonical source records and hashes without converting them to not-yet-defined V2 domains. Run it twice to prove dedupe and source readability. Each domain-specific importer is added only after that destination migration exists.
9. Add CI commands as local scripts/config only; do not publish.

Checkpoint: existing CLI help and doctor behavior pass from `uv run corvus`; all public V1 golden fixtures are hashed; version-aware bootstrap rejects unsafe partial state; sealed quarantine capture runs twice without duplication and leaves the source fixture readable.

### Milestone 0.5 — Release-blocking V1 safety hardening
1. Enforce a snapshot/export policy before any model call: default secret/cache/dependency exclusions, approved include overrides, file/count/byte limits, link/reparse rejection, and guaranteed cleanup.
2. Add the context firewall for all external/snapshot/model-returned content: immutable provenance, untrusted labels, instruction/data separation, content bounds, and denial of content-created tools, secrets, permissions, or autonomy.
3. Redact structured mappings/lists recursively before serialization; register brokered secrets with the redactor; bound persisted/model-returned command output.
4. Separate candidate generation from verification policy. Run server/repository-selected required checks plus any model suggestions; execute smoke checks; rebuild a fresh staging tree for each repair attempt; package exactly the tree that passed.
5. Verify every staged bundle artifact digest immediately before apply; persist/flush rollback intent before mutation; use destination/bundle locks; consume durable actor-bound approvals once.
6. Make an empty/nonexistent audit chain invalid, strictly validate artifact digests, fix optional token-budget narrowing, validate provider URLs/host classes, and use a minimal child-process environment allowlist.
7. Pin sandbox images by digest for production profiles and enforce complete snapshot/candidate/command/output/workflow resource bounds.
8. Add `M005-001` for append-only `ExternalContent` and `ContextEnvelope` provenance. Legacy V1 runs use a typed legacy-run owner until Milestone 1 links new envelopes to mandatory request contexts; imported trust never exceeds `untrusted` without an audited review.

Checkpoint: adversarial secret-exfiltration, forged-verification, stale-staging, altered-bundle, crash-point, SSRF, environment-leak, context-provenance, and audit-tamper tests pass; an independent read-only security review accepts this checkpoint before Task 1.1 begins.

### Milestone 1 — Project authority vertical slice
1. Add `DeploymentProfile`, `DeploymentInstance`, `WorkspaceConfig`, non-rollback `WorkspaceAuthority` external/sealed generation anchors, exclusive leases/OS locks, threshold-signed expiring registry trust-state/freshness proofs, exhaustive authority-root manifests, epoch credentials, close/handoff/restore contracts, `ClientContext`, `ExecutionPlacement`, identity, discriminated scope, audience snapshots, requester/agent grants, immutable authorization-decision snapshots, versioned signing keys, `EffectiveCapabilities`, idempotency, and non-circular prior-state audit/anchor-binding contracts with an explicit combination table.
2. Add `M1-001` and an idempotent migration-backed local project/deployment-instance/authority-generation/registry-key/trust-state/freshness/manifest/audience/authorization-snapshot/signing-key/audit-binding repository without mutating legacy V1 rows; add only the project/config portions of the per-domain importer from sealed quarantine. V1 policy/autonomy waits for `M6-002`.
3. Implement fail-closed authorization with deny precedence, exact scope containment, expiry/revocation, acting-agent intersection, budget/kill-switch checks, and immutable allow/deny receipts.
4. Implement one transport-neutral `create_project`/`get_project` application path through identity -> authorization -> repository -> audit.
5. Test in-process command/query ports, concurrent same-epoch same-host clones, in-place database rollback after every manifest family, fixed OS-lock exclusion, sealed-generation/root mismatch, registry lease fencing, trust-state chain-prefix replay/freeze/skipped version/expiry/rotation/revocation/compromise recovery and nonce/sequence replay, unlisted authority-family startup rejection, prior-state receipt/result-binding crash points, exclusive takeover into a new epoch, externally anchored close-before-activation, restored pre-handoff source quarantine, authority handoff recovery, authorization-snapshot tampering, signing-key rotation/revocation/compromise-effective verification, revoked-key signing denial, cross-workspace/project/audience denial, receipt/binding persistence failure, idempotency revocation/mismatch/concurrency, per-domain import repetition/rollback, and client-surface parity.

Checkpoint: equivalent authenticated requester, transport, acting-agent authority, deployment-instance key and exact external/sealed authority generation/root produce the same project create/read decision across every enabled client surface. A concurrent clone, rollback of any manifest family, unlisted family, missing fixed OS lock, anchor mismatch, revoked/expired instance lease, stale/expired/prefix-replayed registry trust state or nonce proof, restored database without exclusive new-epoch takeover, destroyed/revoked epoch key, disabled adapter, mismatched transport principal, tampered client or authorization snapshot, different workspace/agent/audience, revoked retry, obsolete signing key, circular/missing audit binding, or mismatched replay is denied; every decision has a historically verifiable prior-state signed audit receipt, resulting authority-commit receipt, and immutable anchor binding/checkpoint.

### Milestone 2 — Outcome contracts and durable workflow graphs
1. Add immutable outcome-contract versions, pinned workflow graphs, nodes/dependencies, fenced leases, attempts, checkpoints, recovery decisions, typed lineage nodes/edges, immutable cryptographically committed effect-payload versions, discriminated provider/filesystem effect bindings, exact approval bindings/consumptions, canonical budget periods/typed scope ownership/account closures/exclusive positive equal-amount reservation sets/set-level conserved settlements/permit bindings, versioned unique kill-switch rows, fully authorization/payload/binding-bound effect intents/permits/outboxes/attempts, and one-per-intent effect-outbox tables through `M2-001`–`M2-004`.
2. Implement transactional claim/heartbeat/release/complete with optimistic versions, authority epochs, and monotonic lease fences.
3. Add scoped V2 events plus a state/event/effect-outbox transaction service and the workspace-sequenced audit chain.
4. Implement stuck detection and bounded `retry -> replan -> decompose` recovery with honest exhaustion.
5. Replace `ConversationRuntime` authority with durable conversation services behind `ConversationCommandPort`, `ConversationQueryPort`, and `ConversationEventPort`; define command/query/event envelopes and snapshot/replay/cancellation semantics before retained-client cutover.
6. Add the centralized effect gateway: resolvable immutable authorization snapshots plus current revocation, external/sealed authority-generation checks, canonical locks, immutable payload/binding verification, exactly-one provider-or-filesystem subtype, provider credentials only for provider effects, non-overlapping budget periods, typed scope/unit/window containment, one strictly positive set amount copied across the complete closure, identical nonnegative conserved set/per-account settlements, semantic-key mismatch rejection, provider idempotency, `outcome_unknown` reconciliation/at-most-once handling, and explicit cancellation/compensation. Add per-domain importers for V1 outcomes, run history, artifacts, and conservative budget ceilings.
7. Add `DeliveryQueryPort`, `ApprovalCommandPort`, `ApplyCommandPort`, and `UndoCommandPort` over hardened `DeliveryManager` primitives. Apply/undo create filesystem effect bindings with exact bundle/manifest/destination/rollback digests and no provider credentials; undo binds the original successful apply. Permit claim locks, reauthorizes and atomically consumes the exact signed current approval once, then dispatches through the same effect/audit path. No adapter owns delivery mutation.

Checkpoint: restart preserves conversations/queues/events/checkpoints only when database and external/sealed authority generation/root match; conversation and delivery port tests prove replay/cancellation and authorized apply/conflict/crash/undo without direct adapter mutation or synthetic provider credentials. Provider/filesystem subtype mismatch, altered bundle/manifest/destination/rollback snapshot, wrong original apply, stale/expired/replayed approval, duplicate approval consumption, ciphertext/commitment/schema changes, stale authority/lease/generation, semantic/attempt duplicates, zero/negative/skewed reservation amounts, incomplete/extra closure rows, cardinality mismatch, cross-unit/scope/window parentage, shared reservation sets, negative or unequal actual/released amounts, partial/double settlement, kill-switch and budget races all serialize and fail closed. Unknown provider outcomes never auto-retry without idempotency; completion requires the pinned outcome version and verified immutable authorization/approval/binding/payload/effect/lineage closure.

### Milestone 3 — Embedded CLI/TUI retained-surface and V2 project/run vertical slice
1. Add local identity bootstrap and additive `corvus v2 project create|get` commands over the application ports.
2. Adapt one build workflow through context firewall -> outcome contract -> authorization -> work graph -> sandbox -> independent verification -> package -> approval.
3. Use fake provider/sandbox tests; Docker/Podman remain marked integration tests with no host fallback.
4. Add pause/resume/cancel/retry, kill-switch, evidence, lineage, and event-tail commands.
5. Cut retained `corvus chat`, `corvus run`, and the Textual TUI to the topology-aware Python client. In `embedded_local` they use only the in-process client and conversation ports; CLI/TUI imports or construction of config, provider, workflow, `ConversationRuntime`, policy or repositories fail boundary tests.
6. Cut retained `corvus review`/approve/apply and `corvus undo` to delivery/approval/apply/undo ports. Test provider/filesystem subtype separation without synthetic credentials; exact bundle/manifest/destination/rollback/original-apply binding; signed approval subject/version/nonce/expiry/current-revocation checks and atomic one-time consumption; conflict, fenced apply, injected crash recovery, undo/compensation and audit/rollback/lineage receipt parity in isolated filesystem fixtures.
7. Preserve retained command names, exit codes, JSON envelopes and interactive behavior; test restart, cancellation and event ordering/snapshot parity through the actual CLI/TUI adapters.

Checkpoint: a fake-provider build executes the real state machine and produces a verifiable lineage-bound bundle; actual retained chat/run/TUI and review/apply/undo use the in-process client with no direct authority/runtime/delivery construction. Approved fixture apply/undo uses the filesystem subtype without provider credentials, atomically consumes one exact signed approval, remains conflict-safe/crash-recoverable, and emits approval-consumption/effect/audit/rollback lineage. Only now is `embedded_local` CLI support enabled.

### Milestone 4 — FastAPI, authentication, replayable events, and CLI daemon support
1. Add the concrete local-daemon, cloud, and self-hosted auth adapters, command/query API, OpenAPI, and scoped SSE protocol.
2. Enforce request, agent, scope, audience, credential, placement, budget, and kill-switch authorization on every route and replayed event.
3. Add idempotency, CSRF/session/token, 403/404 non-enumeration, cursor/gap/backpressure, and cross-tenant/thread tests.
4. Generate the versioned OpenAPI/TypeScript schema artifact and prove Python in-process/HTTP parity; the real TypeScript package/web client is not present until Milestone 5.
5. Prove only the `local_daemon` retained-CLI topology: same-user CLI bootstrap, non-exportable deployment-instance activation, loopback-only bind plus Host/origin allowlists, short-lived CLI credentials, credential rotation/revocation, PID/service lock, visible health/shutdown, crash recovery, stale-token rejection, and CLI connect/reconnect tests. The actual retained project/chat/run/TUI/review/apply/undo adapters must select HTTP/SSE, reject fallback/local construction, and pass command/exit/JSON, restart/reconnect, cancellation, event parity, approval-conflict-crash-undo and current-revocation tests. Implement and protocol-test the one-time browser-pairing/session/CSRF endpoints, but do not call web supported yet.

Checkpoint: unauthorized access leaks no existence or private events; retained chat/run/TUI and review/apply/undo prove real HTTP/SSE parity with the embedded path and cannot construct a local bypass; reconnect uses snapshot plus cursor replay without gaps or duplicates; daemon/CLI tests reject LAN binding, wrong Host/origin, stale credentials, duplicate daemon ownership, unsafe crash recovery and stale authority generation while valid retained-CLI authentication survives rotation and restart. Only CLI daemon support is enabled.

### Milestone 5 — Real web workspace and daemon-web support
1. Scaffold pnpm workspace, `apps/web`, `packages/client-ui`, and generated `packages/contracts` from Milestone 4's exact versioned schema.
2. Build the minimal real authentication/pairing/reconnect shell first: one-time same-user pairing exchange, HttpOnly/SameSite session, CSRF binding, logout/revoke, rotation/re-pair, daemon restart and SSE reconnect through the generated client. No ad hoc test page or raw HTTP fixture can satisfy this gate.
3. Implement project create/read, work submission, persisted conversation/event timeline, outcome criteria, evidence, lineage, delivery review/approve/apply/undo, limits, and kill-switch controls through generated clients only.
4. Add API-backed Playwright for pairing misuse, wrong Host/origin, CSRF, stale/rotated/revoked sessions, restart/reconnect, cancellation, approval conflict/crash/undo and duplicate tabs/processes, plus Vitest, accessibility, responsive and hostile-preview-origin tests.

Checkpoint: the actual browser client pairs with and reconnects to the local daemon, survives authorized rotation/restart, rejects every hostile pairing/session/origin case, and completes the fake-provider project/conversation/work/evidence/review/apply/undo flow using only generated contracts and backend-reported state/capabilities. Only now is local-daemon web support enabled.

### Milestone 6 — Team, provider, secret-broker, and earned-autonomy slice
1. Prove owner/member collaboration, reviewer separation, comments, approval, and shared budgets in one workspace.
2. Add `ProviderConnection -> CredentialRef -> CredentialVersion -> CredentialGrant` lifecycle, health, atomic rotation/revocation, exact request/agent/placement/purpose/use-limit binding, and the `M6-001` importer for provider/keyring references only.
3. Add persisted OAuth authorization-code + PKCE and device-flow transactions, callback ownership, expiry/consumption, refresh-family rotation, revocation, and recovery. Prove one local provider-owned OAuth fixture and one deterministic server OAuth lifecycle fixture; API-key-only coverage cannot pass this milestone.
4. Implement scoped short-lived secret grants with zero secret material in prompts/events/traces/snapshots/errors.
5. Add the normative autonomy action/effect matrix and shadow-mode comparison through `M6-002`/`M6-003`; import V1 policy/autonomy YAML capped at `propose`, and require evaluation, reliability threshold, approval, canary limits, compensation, and rollback for promotion.
6. Implement `ProviderConnectionCommandPort`, `OAuthFlowCommandPort`, `CredentialGrantPort`, `AutonomyPolicyCommandPort`/queries, and `ShadowEvaluationCommandPort`/queries. Route retained provider commands through them; add API routes and web settings for provider create/status, PKCE start/callback, device start/poll, reconnect, rotate/revoke/recover, autonomy inspection/promotion, shadow comparison, canary and rollback.
7. Add API-backed end-to-end tests that drive both OAuth flows through a real CLI or browser client, including state/PKCE mismatch, callback ownership, device expiry, refresh rotation, reconnect, revoke and recovery; direct repository/service fixtures alone cannot satisfy the checkpoint.

Checkpoint: one team run uses a brokered provider connection and shadow proposal through application ports and a real CLI/web client; both complete OAuth lifecycles pass; shadow runs have no production credential/effect path; unauthorized members/agents cannot retrieve secrets, raise autonomy, approve themselves, or exceed limits.

### Milestone 7 — Governed memory, skills, routines, and context firewall
1. Add `M7-001` scoped encrypted memory with typed owner/audience, source digest, key version, confidence, expiration, cross-workspace promotion checks, export, and deletion/retention receipts; then run the V1 memory importer twice.
2. Add `M7-002` versioned skills with evaluation suites, hidden holdouts, approval, shadow/canary stages, regression detection, and rollback; then run the V1 skill importer twice.
3. Add `M7-003` immutable agent versions, signed portable packages and target-scope installations. Self-improvement creates drafts; a user explicitly saves an evaluated version, exports it without credentials/authority/private data, and explicitly imports/installs it into another chat under that destination's current authorization and capability ceiling.
4. Route external/retrieved content through the context firewall before any model or memory use.
5. Add routines/webhooks through normal outcome, authority, budget, kill-switch, evidence, and audit paths.
6. Implement memory, skill, agent-release, context-provenance and routine command/query ports. Replace direct retained-CLI `MemoryManager`/`SkillRegistry` use, then add scoped API routes and web governance views for memory read/promote/export/delete, skill import/evaluate/promote/rollback, and agent inspect/save/export/import/install/rollback.
7. Add API-backed CLI/web end-to-end tests for imported memory and skills, cross-scope denial, deletion/export receipts, hostile retrieved content, independent promotion, canary regression/rollback, package tamper/secret scans, explicit save/import consent, target-chat capability reauthorization, unavailable-model blocking and installation rollback; direct repository/service fixtures alone cannot satisfy the checkpoint.

Checkpoint: prompt-injection fixtures cannot grant authority across retained CLI, API or web paths; memory deletion/export is auditable; a regressing skill rolls back through the same application ports; a user can explicitly save an evaluated self-improved agent version and install its signed, secret-free package in another chat without transferring source authority or exceeding destination capabilities, and can roll it back; routines and legacy adapters cannot bypass interactive controls.

### Milestone 8 — Secure connector and offline operation
1. Define outbound-only mTLS pairing, ownership, model-only RPC, health/capability registration, consent, revocation, updates, egress bounds, and audit.
2. Connectors never acquire workspace authority. Schedule connector work only while an authorized runner/model is online under the current authority epoch; preserve waiting state without exposing localhost.
3. Add `M8-001` signed offline intent, non-authoritative cache, reconnect, dedupe, conflict, revocation, replay, restore-quarantine and trust-anchor validation metadata. A local-authority workspace may execute approved offline work only while it owns the non-exportable deployment-instance key, fixed workspace OS lock, and exact device-bound sealed generation/root; a remote-authority or restored client may only queue intents for fresh authorization after reconnect.
4. Prove offline local models, cached resources, governed memory, and queued tasks in local-authority mode while cloud-only capabilities fail with reason codes.
5. Implement offline-intent/status and connector command/query ports, retained CLI queue/status/reconcile commands, and API/web offline/connector status, pairing, conflict and reconnect workflows. No client may write cache/queue/authority rows directly.
6. Add end-to-end split-brain/rollback tests: run concurrent same-epoch same-host database clones against the fixed OS lock; roll the database back before grants, budget, revocation and effect commits while sealed generation/root stays current; copy it to a device without the activation key; restore a pre-handoff snapshot; and prove CLI/web mutation/effects remain blocked. Recovery may resume an exact prepared anchor intent or perform exclusive new-epoch takeover after former-instance revocation; otherwise only read/queue intents survive for fresh authorization.

Checkpoint: cloud work can use an explicitly paired local model without receiving permanent credentials; stale grants/epochs/generations, concurrent clones, rolled-back/restored databases and devices lacking the non-exportable activation key cannot execute queued work; registry-verifier rollback, reconnect/revoke/conflict/replay fail closed through real clients; no test can produce dual-primary authority.

### Milestone 9 — Channel gateways
1. Add signed ingress, provider-event dedupe, identity mapping, immutable bindings, rate limits, bounded replies/files, and correlation.
2. Require browser/desktop step-up for privileged approvals and apply operations.

Checkpoint: replayed/forged/cross-thread messages cannot create duplicate work, broaden authority, or approve high-risk effects.

### Milestone 10 — Packaged operational self-hosted and vendor-cloud deployment proof
1. Define separate topology contracts for self-hosted and vendor cloud: TLS/auth bootstrap, PostgreSQL migrations, worker/effect-gateway topology, durable artifact storage, vault/KMS, tenant boundaries, observability, maintenance, capacity, and supported upgrade paths.
2. Produce the exact non-desktop support artifacts before testing: signed Python wheel/standalone retained CLI, OCI self-host image, and versioned static generated-React assets. Bind each digest to release provenance, dependency lockfiles and an SBOM; source-tree execution cannot satisfy this milestone.
3. Install those exact digests into self-hosted/vendor-cloud fixtures and test first install, version upgrade, database/artifact backup and quarantined restore/new-epoch takeover, key rotation, worker loss, region/service degradation, migration failure, staged rollout and rollback to a still-supported signed artifact set.
4. Prove tenant isolation across database rows, event replay, artifacts, vault grants, workers, caches, logs, metrics, traces, backups, and support/operations access.
5. Drive self-hosted first-admin bootstrap/recovery and configured OIDC, plus vendor-cloud OIDC Authorization Code + PKCE, through the packaged retained CLI **HTTP** client and packaged generated React client against deterministic identity-provider/deployment fixtures. No direct API/service/source-tree fixture can substitute for callback, session-cookie/CSRF, token/session rotation, revocation, reconnect, upgrade, backup/restore quarantine, and new-epoch recovery tests.
6. Re-run project/conversation/chat/run/TUI/work/provider/OAuth/evidence/event-replay and review/apply/undo flows through those exact clients in both topologies; run signed channel ingress/step-up tests against the same authenticated deployments. Keep each client disabled in the matrix until its own flow passes.
7. Require health gates and a documented disaster-recovery objective before either topology is called supported.

Checkpoint: self-hosted and vendor-cloud test environments install the exact provenance/SBOM-bound wheel/standalone CLI, OCI image and static web digests and complete authenticated real-CLI and real-web install/bootstrap-or-OIDC -> callback/session/CSRF -> chat/run/work/review/apply/undo -> rotate/revoke/reconnect -> upgrade -> backup -> quarantined restore/new-epoch recovery -> artifact rollback flows; adversarial tenant-isolation, failed-rollout, stale-session/token, callback and restore tests pass with verifiable receipts. CLI/web/channel support is enabled only after these no-substitute packaged-client gates; desktop remains unsupported.

### Milestone 11 — Desktop packaging and distribution
1. Package the stabilized web client in signed Windows/macOS/Linux Tauri installers with a signed sidecar lifecycle, narrow capabilities, protocol compatibility and rollback.
2. Extend the distribution matrix with desktop installer/sidecar digests while consuming the already-proven Milestone 10 wheel/CLI, OCI and static-web artifacts rather than recreating them.
3. Use a TUF-style threshold-signed update protocol: offline root keys, delegated targets/snapshot/timestamp keys, metadata expiry, monotonic version/rollback and freeze protection, key rotation/revocation recovery, reproducible provenance attestations, and SBOM-to-artifact digest binding.
4. Run OS/architecture CI for keyring, path casing, UNC/long paths, junctions/symlinks, service lifecycle, installer/update, staged rollback, expired/revoked metadata, and optional sandbox routing.
5. Through the installed Tauri artifact, rerun local-daemon pairing and the Milestone 10 self-hosted bootstrap/OIDC plus vendor-cloud PKCE flows, including conversation/chat/run, review/apply/undo, callback, OS-keystore session, CSRF-equivalent origin controls, rotation/revocation, restart/reconnect, upgrade, restore quarantine and new-epoch recovery; raw HTTP or source-tree fixtures cannot enable desktop support.

Checkpoint: installed desktop artifacts run the proven local, self-hosted and cloud team/provider/conversation/delivery flows through generated clients without duplicate authority code or generic shell/filesystem access; authenticated lifecycle tests pass for each topology; every installer/sidecar verifies provenance/SBOM, rejects rollback/freeze attacks, updates through threshold-signed metadata, survives key rotation/revocation fixtures, and rolls back through a tested path. Only then is desktop enabled for the corresponding matrix row.

## Immediate TDD Task List

The baseline-capture/bootstrap portion of Task 0.7 is the first pending action despite its later planning number: freeze pre-change fixtures and establish version-aware bootstrap before Tasks 0.3–0.6 alter behavior. Apply `M005-001` only after that bootstrap exists, then rerun the complete golden/quarantine checks to finish Task 0.7 and the Milestone 0.5 gate.

### Task 0.1 — Packaging baseline
- Create: `pyproject.toml`, `README.md`.
- Create tests: `tests/unit/test_version.py`, `tests/cli/test_cli_smoke.py`.
- First failing assertions:
  - package metadata exposes `corvus==0.2.0a1`.
  - `corvus --help` exits 0.
  - `corvus doctor --json` returns Python 3.12 and valid SQLite status in an isolated `CORVUS_HOME`.
- Implement minimal packaging/version change.
- Verify: `uv sync --all-groups --locked`; `uv run pytest tests/unit/test_version.py tests/cli/test_cli_smoke.py -q`.

### Task 0.2 — Security regression baseline
- Create tests:
  - `tests/security/test_paths.py`
  - `tests/security/test_sandbox_options.py`
  - `tests/security/test_delivery_approval.py`
  - `tests/integration/test_trace_store.py`
- Cover path traversal, symlink/reparse rejection where supported, network-disabled sandbox options, approval expiry/mismatch/conflict, event redaction and chain tampering.
- Do not alter behavior unless a test exposes a real defect.

### Task 0.3 — Secret-safe snapshots, context firewall, and repair isolation
- Create tests: `tests/security/test_snapshot_policy.py`, `tests/security/test_context_firewall.py`, `tests/security/test_secret_flow.py`, `tests/security/test_workflow_repair_isolation.py`.
- Modify: `corvus/workflow.py`, `corvus/security.py`; add a focused context/snapshot policy module only if the tests require it.
- First failing assertions: `.env`/credential/cache/dependency paths are excluded by default; limits block oversized trees; plaintext staging is removed; external/model-returned instructions remain attributed untrusted data and cannot request tools/secrets/authority; repair context is redacted/bounded; each attempt starts from a clean approved snapshot.

### Task 0.4 — Trustworthy verification
- Create tests: `tests/security/test_verification_trust.py` and extend `tests/security/test_workflow_repair_isolation.py`.
- Modify: `corvus/workflow.py`, `corvus/verification.py`.
- First failing assertions: model-declared trivial commands cannot satisfy required checks; smoke checks execute; stale files cannot influence a later attempt; the packaged tree is the exact passing tree.

### Task 0.5 — Bundle integrity and atomic delivery
- Create tests: `tests/security/test_bundle_tampering.py`, `tests/security/test_delivery_atomicity.py`, and extend `tests/security/test_delivery_approval.py`.
- Modify: `corvus/delivery.py`; add a lock/approval repository port only where the test requires it.
- First failing assertions: altered staged files, replayed/expired/mismatched approval, concurrent apply, and injected failure after each filesystem step never produce an unjournaled or unauthorized delivery.

### Task 0.6 — Server-boundary hardening
- Create tests: `tests/security/test_structured_redaction.py`, `tests/security/test_provider_url_policy.py`, `tests/security/test_codex_environment.py`, `tests/security/test_artifact_digest.py`, `tests/unit/test_config_narrowing.py`.
- Modify: `corvus/security.py`, `providers.py`, `codex_cli.py`, `store.py`, `config.py`.
- First failing assertions: nested secrets redact; internal/credential-bearing provider URLs are rejected for cloud profiles; child environment is allowlisted; invalid digests fail; optional token limits always narrow.

### Task 0.7 — V1 golden capture and version-aware migration bootstrap
- Create tests: `tests/contract/test_v1_public_golden.py`, `tests/integration/test_database_bootstrap.py`, `tests/integration/test_v1_quarantine_capture.py`.
- Freeze hashed fixtures for every public command/JSON shape plus V1 database/config/onboarding/provider-reference/policy/autonomy/memory/skill/conversation/run/bundle/artifact/backup domains.
- Replace runtime `create_all()` with one version-aware new/unstamped/current/partial/incompatible database bootstrap, explicit backup/stamp/upgrade/quarantine/restore behavior, and no silent mutation.
- Capture legacy records into a sealed canonical quarantine twice without duplication; do not convert a domain until its destination migration exists.
- Gate: Tasks 0.3–0.7 and the Milestone 0.5 checkpoint must pass, and Milestone 0.5 must receive independent read-only acceptance, before Task 1.1 starts.

### Task 1.1 — Configuration, identity, scope, and audit contracts
- Create: `corvus/domain/deployment.py`, `workspace.py`, `client.py`, `execution.py`, `identity.py`, `scope.py`, `access.py`, `audit.py`.
- Create tests: `tests/unit/domain/test_configuration_matrix.py`, `test_identity.py`, `test_scope.py`, `test_access_models.py`, `test_audit_models.py`.
- First failing assertions: overloaded profile fields are impossible; unsupported combinations fail with reason codes; client surface cannot grant authority; database restore cannot recreate authority; same-epoch clones cannot both hold the fixed OS lock/instance lease; every authority-bearing commit advances the external/sealed generation/root; in-place rollback quarantines; threshold-signed registry trust state rejects prefix replay/freeze/skipped version/expiry/rotation/revocation/compromise and freshness nonce/sequence replay; the authority-root manifest rejects every unlisted family and detects independent rollback of each listed family; prior-state audit hashing plus result anchor binding is non-circular and survives every crash boundary; anchor crash recovery replays only the exact prepared digest; takeover requires former-instance revocation and a new epoch; handoff requires anchored close and old-key destruction/revocation before activation; discriminated scope parentage and audience snapshots are workspace-bound; authorization snapshots are immutable/signed/resolvable; signing-key rotation/revocation/compromise semantics verify historically; missing workspace/requester/agent grant, cross-workspace scope, plaintext credential values, naive expiry, and unstable digests are rejected.

### Task 1.2 — Fail-closed requester and acting-agent evaluation
- Create: `corvus/application/authorization.py`.
- Create test: `tests/unit/application/test_authorization.py`.
- Matrix: exact allow, no-grant deny, explicit deny wins, wrong principal/workspace/project/thread/audience, missing deployment-instance key/OS lock/lease, stale external/sealed generation or state root, restore quarantine, stale/expired/prefix-replayed registry trust state or freshness proof, verifier rollback/revocation/compromise, unlisted authority-root family, expired/revoked requester or agent grant, authorization-snapshot tamper, delegation overreach, placement/credential mismatch, budget/runtime exhaustion, kill switch, and enabled-client-surface parity.

### Task 1.3 — Migration-backed project and audit repositories
- Create: `corvus/infrastructure/db.py`, `corvus/infrastructure/repositories/projects.py`, `audit.py`, and initial Alembic migration/fixture paths.
- Create tests: `tests/integration/test_project_repository.py`, `test_scoped_audit_repository.py`, `test_v1_migration.py`.
- Persist project ownership, deployment instances, external/sealed authority-generation metadata/intents/receipts, threshold-signed registry trust-state metadata/freshness proofs and verifier history, authority-root manifest/leaf-family rows, epoch credentials, instance leases, local anchors, close certificates/handoffs/restore receipts, discriminated scope, audience snapshots, immutable signed authorization-decision snapshots, access/agent/delegation input versions, signing-key versions, prior-state audit receipts/result anchor bindings/checkpoints, and fully bound idempotency envelopes without changing legacy rows.
- Add the project/config importer after `M1-001`; verify migration/import twice, concurrent same-host clone exclusion, in-place rollback after every manifest family, manifest omission rejection, prepared/reserved/database/finalized/audit-binding crash recovery, former-instance revoke/new-epoch takeover, pre-handoff restore quarantine, old-key destruction/revocation, lease expiry/fencing, registry trust-state prefix replay/freeze/expiry/skipped-version and verifier rotation/revocation/compromise recovery, freshness nonce/sequence replay, authorization-snapshot tampering, signing-key rotation/revoked signing/post-rotation verification/compromise re-anchoring, audit append/binding concurrency, cross-workspace/audience reads, rollback fixture readability, revoked cached-result denial, and concurrent duplicate commands. V1 policy/autonomy is not converted until `M6-002`.

### Task 1.4 — Transport-neutral project create/read service
- Create: `corvus/application/projects.py`, `corvus/application/ports.py`, and an in-process client adapter.
- Create tests: `tests/integration/test_project_authority_slice.py`.
- One command/query path resolves effective capabilities, records allow/deny, creates or reads the project transactionally, and fails closed if authorization/audit persistence fails.

### Task 1.5 — Vertical contract and compatibility tests
- Create tests: `tests/contract/test_inprocess_project_client.py`, `tests/cli/test_v1_compatibility.py`.
- Prove stable command/query/error envelopes, currently authorized idempotent response replay, payload mismatch/revocation rejection, equivalent decisions across enabled client surfaces, disabled-adapter and mismatched-transport denial, no secret fields, and unchanged retained V1 command availability.
- Do not add CLI V2, FastAPI, or UI adapters in this milestone.

### Task 1.6 — Quality gate
- `uv run pytest -q`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run bandit -r corvus -q`
- `uv run python -m compileall -q corvus`
- `uv run corvus --help`
- `uv run corvus doctor --json` with isolated `CORVUS_HOME`.
- `git diff --check`

## Worker Assignments
- Worker A — Tasks 0.3–0.7 only: first freeze Task 0.7 fixtures/bootstrap, then complete remaining Milestone 0.5 security work and `M005-001`, then rerun/finalize the golden and sealed-quarantine checks. Must not edit new authority schemas.
- Reviewer 0.5 — independently verifies the completed Milestone 0.5 checkpoint and Task 0.7 migration-preflight evidence. A revise verdict blocks Worker B.
- Worker B — configuration/authority/identity/scope/audience/access/audit domain contracts and tests only. Starts only after the exact plan review passes, Worker A completes Tasks 0.3–0.7, and Reviewer 0.5 accepts the checkpoint.
- Worker C — project/authority/audience/audit persistence, migrations, domain importer, fixtures, and repository tests. Starts only after Worker B contracts are fixed and reviewed; it does not overlap Worker B.
- Worker D — transport-neutral project application service and contract/compatibility tests. Starts only after Worker C is fixed and reviewed; it does not overlap Workers B/C.
- Reviewer — read-only spec, security, migration, diff, test, and acceptance review. Must not be the implementing worker.
- Limit concurrent local workers to two because RAM is constrained; never combine heavy Node/Rust/container builds.

## Acceptance Criteria

### Plan-level
- The plan separates deployment, workspace collaboration, client context, execution placement, model routing, and credential ownership by lifetime/cardinality.
- Every locked capability maps to an authoritative service, persistent record, enforcement point, migration path, and test strategy.
- Web, desktop, and channels reuse contracts and clients rather than duplicate authority or run state.
- V1 compatibility, migration, checkpoints, review freshness, and rollback points are identified.

### Immediate implementation slice
1. Repository installs reproducibly with `uv sync --locked` under Python 3.12.
2. Existing CLI help and doctor JSON pass from the installed project.
3. Regression tests cover retained path, sandbox, event-chain, and delivery-approval primitives.
4. Default build snapshots exclude secret/cache/dependency material, enforce size/count limits, and clean up after success, failure, or cancellation.
5. Required verification is selected by Corvus/repository policy rather than trusted solely from generating-model commands; smoke checks execute and each repair uses a clean staging tree.
6. Delivery rehashes every staged file, rejects approval mismatch/replay/expiry, locks concurrent apply, and remains recoverable at every injected crash point.
7. Structured secrets are redacted before prompts, persistence, repair context, events, traces, snapshots, or errors; child environments and provider destinations are allowlisted.
8. Deployment/workspace/client/execution/model/credential contracts cannot express an overloaded profile or plaintext secret; every authoritative commit advances an external registry or device-bound sealed generation/state root under one deployment-instance lease/OS lock, database rollback or same-epoch clones fail closed, expiring threshold-signed registry trust metadata and nonce/sequence freshness proofs prevent history-prefix replay/freeze, an exhaustive manifest covers every mutable authority family or named external proof, handoff revokes/closes the former instance before new-epoch activation, and restores default to read/queue-only quarantine.
9. A request cannot exist without deployment instance, current epoch credential, exact external/sealed authority generation/state root/commit receipt, discriminated scope, immutable audience snapshot, requester, client/transport context, acting-agent grant, requester access bundle, policy digest, signed immutable authorization-decision snapshot and signing-key version, correlation ID, and idempotency key.
10. Effective authority is the minimum current authority/requester/agent/delegation/channel/workspace/budget/placement/credential/kill-switch intersection; deny wins, scope never broadens, and historical snapshots remain immutable while current revocations are rechecked.
11. Equivalent authenticated requester/transport/agent authority creates and reads one project through every enabled client surface; disabled adapters, mismatched transports, tampered contexts, and cross-workspace/project/audience substitutions fail.
12. Allow and deny decisions create immutable signed authorization snapshots and unique-sequence prior-state audit receipts; the receipt hash becomes the proposed audit-head leaf, and an immutable result binding proves the finalized generation/root/commit receipt without circular hashing. External/sealed generation advance, append, signature, result binding, or chain-head persistence failure prevents the protected action or quarantines ambiguous recovery.
13. All public V1 command/JSON and database/domain fixtures are hashed; version-aware bootstrap detects partial/unstamped state; quarantine capture runs twice without duplication; each destination migration/import runs twice and preserves rollback/legacy readability.
14. Idempotent command retries replay only after current read authorization; payload mismatch, revocation, and concurrent duplicates do not disclose data or create a second project.
15. Empty audit chains are invalid; checkpoints use the same prior-state receipt/result-binding sequence and bind the authorizing snapshot/set and effect-payload-set digests plus deterministic workspace sequence, deployment instance, external/sealed authority generation/state root/commit receipt, epoch credential, signing-key version and compromise-effective history; artifact lookups accept only canonical SHA-256 digests.
16. Existing V1 CLI commands remain available; no CLI V2, FastAPI, web, desktop, channel, deployment, purchase, or live provider call occurs in this slice.
17. Sandbox unavailability still produces no host-execution fallback.
18. The exact final plan revision receives a fresh independent review before any remaining implementation.
19. Task 1.1 remains blocked until Tasks 0.3–0.7 pass and an independent reviewer accepts the completed Milestone 0.5 checkpoint.

### Later client milestones
- API routes authorize every resource and each live/replayed event against immutable scope/audience and a signed authorization-decision snapshot while rechecking current revocation.
- Web, desktop, CLI/TUI and channels consume transport-neutral ports/generated clients and render only persisted state plus backend-returned effective capabilities; direct runtime/provider/workflow/delivery/manager/repository construction is removed at each surface/capability cutover.
- `embedded_local` CLI support is unavailable until Milestone 3 cuts actual retained chat/run/TUI and review/apply/undo to the in-process client and proves compatibility, cancellation/event parity, discriminated filesystem binding without provider credentials, atomic exact-approval consumption, conflict/crash recovery and undo receipts.
- `local_daemon` CLI support is unavailable until Milestone 4 runs those same retained surfaces over HTTP/SSE with same-user bootstrap, deployment-instance activation, loopback/origin controls, credential rotation, lifecycle/recovery and reconnect tests; daemon web support remains unavailable until Milestone 5's actual generated-client/Playwright conversation/delivery/pairing/CSRF/rotation/restart/reconnect gate passes.
- Team, provider/OAuth, broker, autonomy and shadow flows stabilize through real CLI/web end-to-end paths before channel or desktop packaging.
- Governed memory, skills, portable agent releases, context provenance and routines expose owned ports plus retained-CLI/API/web tests before their milestones pass.
- Connector and offline behavior expose owned ports and real-client scheduling, restore-quarantine, revocation, unavailable-reason and recovery tests.
- Desktop has no generic shell/filesystem grant and contains no duplicate policy, persistence, or run-state implementation.
- Self-hosted and vendor-cloud CLI/web/channel modes are not supported until exact signed/provenance/SBOM-bound wheel-or-standalone CLI, OCI and static-web artifacts are installed and their actual retained-CLI HTTP/generated-React conversation/delivery/bootstrap/OIDC/session/CSRF/rotation/reconnect/upgrade/restore flows pass; desktop waits for installed Tauri artifacts to rerun those flows in Milestone 11.
- Desktop updates use threshold-signed expiring metadata, rollback/freeze protection, key-rotation/revocation recovery, reproducible provenance, and SBOM binding.
- One fake-provider lineage-bound vertical slice works across in-process CLI, HTTP/web, and desktop clients before release.

## Verification Matrix

| Layer | Tests |
|---|---|
| Configuration/domain | allowed/invalid combinations, deployment-instance activation, fixed OS-lock/registry-lease singleton, external/sealed generation/root CAS, same-epoch clone and in-place rollback denial, threshold-signed expiring registry trust-state head, nonce/sequence freshness, prefix replay/freeze/skipped-version/rotation/revocation/compromise recovery, exhaustive root-manifest coverage, close-before-activate handoff, restore quarantine/new-epoch takeover, capability projection, canonical digests, discriminated scope parentage, audience snapshots, state transitions |
| Authorization/audit | requester/agent/delegation intersection, exact authority generation/root/commit receipt, signed immutable decision snapshots, current revocation, deny precedence, signing-key rotation/revocation/compromise history, exact signed approval subject/version/nonce/expiry/reviewer separation, one-time approval consumption, prior-state receipt/new-head/result-binding non-circular sequence and crash points, limits, kill switches, enabled-client parity |
| Persistence/migration | V1 golden/quarantine capture, version-aware bootstrap, per-domain repeated imports, anchor prepare/reserve/database/finalize/binding crash recovery, concurrent clone, rollback of each manifest family including approval/effect-binding/settlement sets, exactly-one effect subtype, positive/nonnegative/equality/closure-cardinality constraints, unlisted-family rejection, restored pre-handoff source, transactions, sequenced signed receipt/event chains, tamper and isolation |
| Outcomes/workflows/effects | pinned criteria/evidence; immutable authorization/approval/binding/payload/effect/lineage closure; provider/filesystem subtype separation; exact delivery bundle/manifest/destination/rollback/original-apply binding; atomic approval consumption; ciphertext/commitment tamper; fenced leases/idempotency; non-overlapping periods; typed scope/unit/window; positive equal-amount full-closure reservation sets; shared-set rejection; common nonnegative conserved settlement set and equal per-account rows; missing/extra/cardinality/partial/double-settlement denial; one-per-intent permit/outbox; materialized kill locks; `outcome_unknown`, reconciliation, compensation, stuck recovery, dependencies, cancellation |
| Context firewall | provenance, instruction/data separation, bounded ingestion, prompt-injection and retrieved-content canaries |
| Secret broker/providers | zero-secret traces, workspace/connection/version/request/agent/placement/purpose/use binding, host/method/path limits, atomic rotation/revocation, local and server OAuth lifecycle fixtures |
| Skills/autonomy | normative action/effect matrix, no-effect shadow, evaluation versions, hidden holdouts, reversible canary approval/limits/compensation, regression and rollback |
| Portable agent versions | immutable composition manifests, explicit user save/import approval, signed content-addressed packages, secret/authority/private-data exclusion, model compatibility, destination capability reauthorization, cross-workspace/chat isolation and installation rollback |
| Memory governance | typed workspace scope/owner/audience isolation, immutable source digest, key version, confidence/expiry, cross-workspace promotion, export and deletion/retention receipts |
| Sandbox | option contract unit tests; marked Docker/Podman integration tests only where available |
| Delivery | delivery/approval/apply/undo port contracts, filesystem subtype with no provider credential, exact bundle/manifest/destination/rollback/original-apply commitment, current authorization/revocation, signed approval version/nonce/expiry plus atomic unique consumption, fenced one-time effect, rehash, locking, conflict/crash atomicity, backup/undo/compensation and approval/effect/audit/lineage parity through real clients, malicious paths |
| CLI/TUI | Typer/Textual tests, stable command/exit/JSON envelopes, retained chat/run/review/apply/undo compatibility, import-boundary denial of direct runtime/provider/workflow/delivery construction, in-process versus HTTP/SSE cancellation/event/effect parity, later project/work/evidence commands |
| API/local daemon/events | OIDC/session/CSRF/token protocol tests, local same-user deployment-instance bootstrap, retained project/chat/run/TUI/review/apply/undo over real HTTP/SSE, rotation/origin/Host/lifecycle/recovery/reconnect, cancellation/event/apply-conflict-crash-undo parity, protocol-level browser pairing endpoints, 403/404 behavior, idempotency, OpenAPI, scoped cursor replay; no web-support claim |
| Capability clients | transport-neutral port parity, retained-CLI cutover, API/web routes and end-to-end OAuth/broker/autonomy/shadow/memory/skill/context/work/lineage/kill workflows; no direct legacy repository/manager path |
| Web/channels | actual generated-client API-backed Playwright for one-time daemon pairing, conversation/event replay/cancellation, review/apply/undo, SameSite/CSRF, rotation/revocation, restart/reconnect and hostile origins; accessibility, hostile preview, signed channel ingress, dedupe, step-up approval |
| Connector/offline | mTLS pairing, no connector authority, deployment-instance key plus fixed OS-lock/sealed generation, same-epoch clone/in-place rollback denial, restore quarantine/new-epoch takeover, consent, health, egress, signed intent dedupe/conflict/revocation/replay, local-authority execution, remote/restored queue-only behavior, real-client reconnect |
| Deployment operations | exact signed/provenance/SBOM-bound wheel-or-standalone CLI, OCI and static-web digests; separate self-host/cloud install/upgrade/artifact rollback, PostgreSQL/workers/artifacts/vault/KMS, tenant isolation, backup/restore/DR, observability, real packaged retained-CLI HTTP/generated-React conversation/delivery/bootstrap/OIDC/session/CSRF/rotation/reconnect/restore flows |
| Desktop/distribution | installed Tauri/sidecar digest and capability audit, real-client local/self-host/cloud conversation/delivery/authenticated lifecycle reruns, TUF-style threshold metadata, expiry/rollback/freeze defense, key rotation/revocation, provenance/SBOM, OS/architecture CI |
| Security/quality | Pytest, Ruff, Bandit under Python 3.12, dependency audit, SBOM, secret scan, tenant/scope isolation |

## Key Decisions and Tradeoffs
- **One Python core, thin clients:** avoids three diverging agent/security implementations.
- **Fix V1 trust boundaries before exposing V2:** authorization models do not make an unsafe snapshot/verification/delivery pipeline safe; critical build and delivery defects are gated ahead of web/team enablement.
- **Separated configuration contracts, not editions or one profile:** deployment, workspace collaboration, client context, execution placement, model route, and credential ownership resolve independently into effective capabilities.
- **One non-rollback authoritative control plane per workspace:** every authoritative commit advances a state-root generation outside the workspace database. Networked modes require a pinned/versioned registry and exclusive deployment-instance lease; offline local mode requires a fixed OS lock plus device-bound sealed monotonic state. Restore requires former-instance revocation and new-epoch takeover; ambiguous recovery quarantines.
- **Earned autonomy:** agents and skills begin in shadow/proposal stages and advance only through versioned evidence, approval, canaries, monitoring, and rollback.
- **Portable agent versions, not silently retrained weights:** a liked self-improved agent can be saved as an immutable evaluated composition of instructions, configuration, approved skills and explicitly exported governed memory, then signed and installed in another chat. The destination reauthorizes every capability; credentials, grants, authority and private chat state never transfer.
- **Bring-your-own models:** users supply local endpoints, API credentials, or provider OAuth. Corvus stores credential references and brokers access; it does not conflate a Corvus subscription with model entitlement.
- **Local model with cloud control plane requires a connector:** Corvus Cloud never assumes it can reach localhost and never asks users to expose an unauthenticated model port.
- **Vite React rather than Next.js for the workspace client:** the product is an authenticated application, not an SEO surface; static client assets are reusable by Tauri. FastAPI remains authoritative.
- **Tauri rather than Electron:** smaller native boundary and explicit capabilities, while still reusing React UI.
- **Incremental migration rather than wholesale directory move:** preserves V1 behavior and makes regressions attributable.
- **New scoped audit repository before rewriting legacy events:** provides a safe team boundary without an all-at-once event-store migration. Legacy events remain local-only until adapted.
- **Fake provider/sandbox for deterministic tests, no host fallback:** allows verification on this machine without pretending unsandboxed builds are secure.
- **Transport-neutral ports plus generated clients:** one composition root and in-process/HTTP parity prevent CLI drift; OpenAPI-generated TypeScript prevents web/desktop drift. Governed capabilities and retained platform surfaces—including chat/run/TUI and review/apply/undo—must cut every real adapter to named ports before its topology is enabled.
- **Context firewall before model or memory use:** external/retrieved content remains attributed untrusted data and cannot grant instruction, tools, secrets, permissions, or autonomy.
- **Server-side access bundle resolution:** transport tokens are references, not authority.
- **No OpenClaw shared gateway as tenant boundary:** borrow queue/session/capability patterns only; Corvus owns authorization or isolates gateways per tenant.
- **No Claude Tag embedding:** implement first-party `@Corvus`; Claude models may be providers through supported APIs.

## Risks and Mitigations
- **Unknown V1 production data:** freeze all public command/JSON/database/domain fixtures; replace `create_all()` with version-aware bootstrap; capture immutable sealed quarantine twice before evolution; add each idempotent policy/autonomy/provider/memory/skill/run/artifact importer only after its destination migration, with backup/restore/rollback tests.
- **Plaintext snapshot and model-output exfiltration:** default-deny snapshot policy, structured redaction, output bounds, clean attempt trees, and cleanup are release blockers.
- **Model-selected verification:** repository/server-required checks and independent reviewer evidence must dominate model suggestions.
- **Bundle TOCTOU/crash windows:** filesystem apply/undo bindings commit the exact bundle/manifest/destination/rollback/original-apply digests, rehash at dispatch, atomically consume one signed current approval, lock concurrent apply, and pass every crash point through delivery ports; no synthetic credential or direct manager path exists.
- **Large CLI/TUI modules and hidden composition roots:** keep adapter registration changes minimal, extract conversation/delivery use cases, and enforce import-boundary tests. Retained chat/run/TUI/review/apply/undo select only the topology-aware in-process or HTTP/SSE client.
- **Hash-chain, authorization-snapshot, signing-key, or audit-root ambiguity:** append signed canonical decision snapshots and prior-state receipts, put the receipt hash in the proposed new head, and bind the finalized root/commit receipt afterward with immutable derived evidence. Persist public-key versions/validity/compromise times; test tampering, concurrent appends, every prepare/finalize/binding crash point, rotation/revocation, obsolete-key signing and uncompromised re-anchoring.
- **SQLite limits:** supported only for one authoritative single-process individual local workspace with file locking, integrity checks, backups, and documented recovery; PostgreSQL is required before networked or multi-worker operation.
- **Scope-comparison bugs:** centralize containment logic and use exhaustive table/property tests.
- **Approval replay or substitution:** signed decisions bind the exact request version, subject/action, bundle/manifest/destination/original apply, reviewer, nonce and expiry. Permit claim reauthorizes, locks and changes `approved -> consumed` while inserting the globally unique consumption; mismatch, revocation, expiry or replay fails closed.
- **Credential leakage or confused ownership:** typed workspace/provider/credential-version/request/agent/placement/purpose/use bindings are mandatory; OAuth state/PKCE/callback/refresh/revocation persists; the broker issues short-lived host/method/path-scoped access and zero-secret traces.
- **Acting-agent authority drift:** recompute the requester/agent/delegation/channel/workspace/budget/placement/credential/kill-switch intersection at every effect.
- **Context injection:** all external/retrieved content remains provenance-labelled untrusted data behind instruction/context separation and adversarial fixtures.
- **Premature autonomy promotion:** shadow/canary stages, hidden holdouts, independent approval, regression thresholds, kill switches, and rollback gate every increase.
- **Portable-package privilege or privacy smuggling:** canonical signed manifests, export-time secret/private-data scans, explicit memory-export authorization, destination policy/capability recomputation, model-compatibility checks, explicit user consent and reversible installations prevent an imported agent from carrying source authority or silently widening access.
- **Memory deletion gaps:** export/deletion receipts cover primary rows, indexes, caches, artifacts, and documented backup-retention exceptions.
- **Database rollback, registry-history freeze, or clone resurrects authority:** every authority-bearing commit advances a registry-backed or device-sealed generation/state root outside the workspace database. One deployment-instance lease or fixed workspace OS lock is exclusive; expiring threshold-signed trust metadata stores a monotonic verifier-history head outside backups and every registry response carries nonce/sequence freshness. The exhaustive schema-versioned root manifest covers every mutable authority/key family or a named external proof. Stale roots/history prefixes, unlisted families, concurrent clones, copied key locators and per-family rollback quarantine; restore mutation requires former-instance revocation and exclusive new-epoch takeover.
- **Stale worker, subtype/payload substitution, budget write-skew or duplicate effects:** intent/permit/outbox/attempt/audit/lineage bind one immutable payload and exactly one provider or filesystem subtype. Provider credentials exist only in the provider binding; delivery binds exact artifacts/approval/original apply. Canonical locks, typed scope ancestry, one positive canonical set amount copied by composite constraints to every complete closure row, nonnegative common conserved settlement amounts, closure cardinality/digest checks, unique approval/reservation/permit/outbox/attempt/settlement constraints and exact authority generation close local races. Unknown provider outcomes never auto-retry without provider idempotency; compensation never erases usage.
- **Parallel legacy capability or platform-surface paths:** every locked capability owns application ports, retained-CLI cutover, API/web adapters and end-to-end client proof; retained chat/run/TUI and review/apply/undo have their own mandatory cutover ledger. Direct `ConversationRuntime`, `DeliveryManager`, `MemoryManager`, `SkillRegistry`, provider repository or offline-row access cannot satisfy a milestone.
- **Local-daemon exposure or premature web-support claim:** Milestone 4 can enable only retained-CLI daemon use after every project/chat/run/TUI/review/apply/undo adapter proves HTTP/SSE parity plus instance/bootstrap, loopback/Host/origin, rotation/revocation, lifecycle and crash recovery. Browser support waits for Milestone 5's actual generated web conversation/delivery client and API-backed Playwright pairing/CSRF/rotation/restart/reconnect gate.
- **Connector compromise or offline ambiguity:** outbound-only mTLS, model-only RPC, consent, egress limits, health scheduling, revocation, deployment-instance activation plus external/sealed generation checks, same-epoch clone/rollback denial, restore quarantine, queue-only remote/restored intents, and explicit unavailable states fail closed.
- **SSE data leakage:** authorize every event against immutable scope/audience and test opaque cursor replay, retention gaps, backpressure, and cross-thread denial.
- **Desktop privilege creep:** Tauri capabilities are reviewed as code and tested against an allowlist.
- **Dependency/update supply chain:** lock Python/Node/Rust dependencies; add audits, reproducible provenance, SBOM binding, and a TUF-style threshold-signed updater with offline root, delegated rotating keys, expiry, rollback/freeze defense, revocation recovery, and staged rollback tests.
- **Declared deployment/client modes without exact distributable proof:** keep self-hosted/vendor-cloud CLI/web/channel unsupported until exact signed/provenance/SBOM-bound wheel-or-standalone CLI, OCI and static-web digests pass install/upgrade/artifact-rollback plus PostgreSQL, workers, durable artifacts, vault/KMS, tenant isolation, backup/restore/DR and no-substitute retained-CLI HTTP/generated-React conversation/delivery/authentication flows. Keep desktop disabled until installed Tauri artifacts rerun those flows.
- **Resource pressure:** serialize Node/Rust builds and avoid local container builds while RAM is constrained.
- **Review availability:** Claude returned HTTP 401, Gemini required Antigravity migration, and Codex timed out without a verdict. Earlier paired Hermes rounds are recorded in `PLAN-REVIEW-LOG.md`. Exact commit `4927aab94fd51c3583dd018b33d2f08eec9684d3` received product/topology `PASS` but security `REVISE` for generic provider-only effect fields/approval consumption and forgeable hierarchical reservation amounts. Both findings are incorporated without changing the passing topology, but both fresh scopes must return `VERDICT: PASS` on the same new exact commit; no prior pass, timeout, partial result or single pass is approval.

## Rollback and Checkpoints
1. `1410d7f` remains the immutable imported V1 baseline.
2. Commit this revised plan and review log separately; record the exact plan digest reviewed.
3. Refresh independent reviewers against that exact revision. Do not begin Task 1.1 schemas/contracts until a completed review no longer requires revision.
4. One commit per TDD task or tightly coupled security fix; never rewrite baseline or hide incomplete gates.
5. Before authority schema work, complete Tasks 0.3–0.7: hash every V1 public/database/domain fixture, validate version-aware bootstrap, run sealed quarantine capture twice, pass the Milestone 0.5 checkpoint, and obtain independent acceptance.
6. After each destination migration, run only that domain's importer twice and verify legacy readability plus backup/restore/downgrade boundaries; never use a premature all-domain conversion.
7. Backups exclude deployment-instance/epoch/signing private capabilities and external/sealed authority-generation state. Normal restart requires exact instance key, exclusive lock/lease and generation/root match. Every restore starts `restore_quarantine`; rollback may resume only an exact prepared anchor intent or revoke the former instance and activate a new epoch, never revive an old same-epoch database state.
8. CLI V2 commands are additive until replacements pass compatibility tests; each governed capability removes its direct legacy-manager path when its port cutover passes. Retained chat/run/TUI and review/apply/undo must complete their Milestone 3 in-process and Milestone 4 HTTP/SSE cutovers before the corresponding topology is enabled.
9. API, web, team/provider, connector, channel, deployment, and desktop work live on separate reversible feature commits.
10. Autonomy/skill promotions always retain the previous active version and rollback receipt.
11. No deployment, public release, live provider call, external message, credential migration, or purchase occurs in this plan.

## First Implementation Slice to Build Immediately
After this exact plan revision passes fresh review, finish Tasks 0.3–0.7 as separately reviewed TDD commits: release-blocking trust fixes, context provenance persistence, complete V1 golden capture, version-aware bootstrap, and sealed quarantine capture. Obtain independent acceptance of the completed Milestone 0.5 checkpoint. Only then may Worker B begin Milestone 1's transport-neutral project create/read authority contracts, followed sequentially by persistence and application workers. Do not add CLI V2, FastAPI, React, channel, connector, deployment, desktop, live providers, or broad workflow/autonomy features until that project slice and complete quality/security gate pass.
