# Corvus Full Product Platform Design

**Status:** Approved in conversation on 2026-07-16; written specification awaiting final user review

**Delivery model:** One implementation branch, seven milestone commits, integrated verification, one unmerged pull request

**Architecture choice:** Modular Corvus control plane on Railway with PostgreSQL, thin local-node and E2B execution adapters, and shared web/desktop clients

## 1. Purpose

Corvus will evolve from its certified local-first foundation and adaptive application shell into a complete identity-backed AI agent platform. Users must be able to sign in, choose an experience and workspace type, chat with and run real agents, schedule unattended work, synchronize safely across devices, configure providers and tools, collaborate in teams, and select a local or E2B cloud runtime.

The implementation must preserve the existing authoritative Corvus core. Clients and execution nodes never manufacture workspace authority, approvals, budgets, credential access, or audit truth.

## 2. Product Model

Corvus stores two independent classifications:

- **Experience:** `everyday` or `developer`
- **Workspace type:** `individual` or `team`

The combination selects information architecture and language, not authorization. A team workspace receives real capabilities only through server-side membership and role policy. The current selection appears read-only in the top-left identity block and is changed only in Settings.

The four product profiles are:

- Everyday Individual: personal outcomes and automations with minimal technical detail.
- Everyday Team: assignments, shared outcomes, approvals, and knowledge.
- Developer Individual: repositories, diffs, tools, terminals, environments, and run detail.
- Developer Team: work queues, reviews, policies, shared environments, and controlled deployment work.

## 3. System Architecture and State Ownership

```text
Vercel web client / Tauri desktop client
                    |
                    v
       Railway FastAPI control plane
        |           |              |
        v           v              v
   PostgreSQL   dispatcher     background worker
                    |
              +-----+-----+
              |           |
              v           v
       paired local node  E2B sandbox
```

### 3.1 Railway control plane

The Railway deployment is one modular Python application with independently testable identity, workspace, conversation, scheduling, settings, collaboration, and runtime-placement modules. A worker process uses the same domain and repository contracts; it is not a separate microservice.

Railway owns shared identities, sessions, devices, workspaces, memberships, threads, messages, schedules, settings, policies, run metadata, redacted events, synchronization cursors, runtime placement, and E2B lifecycle records.

### 3.2 PostgreSQL and local SQLite

SQLAlchemy repository ports and Alembic migrations become authoritative for Railway PostgreSQL. SQLite remains supported for local development and offline desktop state. Repository contract tests must run against both engines. SQLite-specific MVP behavior is wrapped behind ports rather than copied into the hosted path.

### 3.3 Local nodes

A desktop installation owns local files, repositories, installed CLI agents, OS-keychain credentials, local MCP processes, and device-specific configuration. It registers a cryptographic node identity and maintains an authenticated outbound connection to Railway. It exposes no public inbound server.

### 3.4 E2B

E2B owns only ephemeral execution state. Each authorized cloud run receives a pinned template, bounded resources, scoped workspace material, restricted network/tool policy, short-lived credential injection, heartbeat monitoring, and guaranteed cleanup or reconciliation.

## 4. Identity, OAuth, and Synchronization

Google OAuth is the first onboarding step. Corvus uses Authorization Code with PKCE, state, nonce, exact redirect allowlists, Google JWKS validation, and verified-email policy. OAuth transactions are single-use and expire. Sessions use secure, HttpOnly, SameSite cookies on web and protected desktop token storage with refresh rotation and revocation.

Core entities are `accounts`, `external_identities`, `sessions`, `devices`, `workspaces`, `memberships`, `workspace_changes`, `device_cursors`, and `outbox_events`.

Onboarding order is:

1. Sign in with Google.
2. Select Everyday or Developer.
3. Select Individual or Team.
4. Select Local, ask-per-run, or Corvus Cloud Preview.
5. Create or join a workspace.

Synchronization is server-authoritative and cursor-based. Mutations carry idempotency keys and expected entity versions. Append-only messages and events never overwrite one another. Conflicting mutable edits return `409` with both versions for explicit resolution; Corvus does not silently use last-write-wins for security-relevant fields. Raw credentials, provider tokens, local file contents, and unrestricted environment variables never enter synchronized state.

## 5. Threads and Live Agent Execution

Core entities are `threads`, `messages`, `attachments`, `agent_runs`, `agent_run_events`, `run_artifacts`, `provider_connections`, and `model_catalog_entries`.

Every provider implements one capability contract covering discovery, readiness, streaming, cancellation, resumability, tool use, approvals, token accounting, and normalized errors. The initial live matrix is:

- Local: Codex, Claude Code, Gemini CLI, and Cursor agent where installed.
- API: OpenAI/Codex-compatible, Anthropic, Google Gemini, xAI/Grok, and OpenRouter-compatible endpoints.
- Test: the deterministic simulator remains the conformance oracle.

Unsupported capabilities fail closed. Credentials remain opaque references resolved only at the execution boundary.

Execution flow is:

1. Create the user message transactionally.
2. Submit an idempotent run request.
3. Recheck identity, workspace authority, provider binding, budget, credential proof, autonomy, approvals, and kill-switch state.
4. Dispatch to an eligible local node or E2B.
5. Persist redacted ordered events and stream them through SSE.
6. Support stop, cancel, retry, fork, continue, approve, and schedule.
7. Reconcile interrupted streams without duplicating messages or effects.

The default product surface is a conversation with a persistent bottom composer. It includes attachment, slash-command, provider/model, runtime, autonomy, schedule, Send, and Stop controls. The Corvus flightpath becomes the live run spine: `plan -> work -> approval -> result`. A contextual inspector exposes artifacts and approvals; developer profiles additionally expose diffs, logs, tool calls, checkpoints, repository state, and budgets.

## 6. Scheduling and Unattended Execution

Core entities are `schedules`, `schedule_versions`, `schedule_executions`, `execution_claims`, `runtime_heartbeats`, and `notification_deliveries`.

Corvus supports one-time, interval, calendar/cron, event-triggered, and manual run-now schedules. A schedule stores timezone, recurrence, target agent/model, runtime policy, autonomy policy, credential references, budget, missed-run policy, and notification policy together with an immutable authorization snapshot.

Railway workers claim due work with leases and fencing tokens. Default missed-run behavior is to wait within a bounded grace window and then skip; users may choose skip, run once when available, or bounded catch-up. Retries use bounded exponential backoff and never repeat a confirmed successful effect. Local schedules require an eligible paired node; cloud schedules require an authorized E2B placement.

The product provides calendar and list views, recurrence previews, next/last run, runtime readiness, history, pause/resume, run now, cancel/kill, retry, and recovery. Desktop exposes tray/background status. Notification delivery is idempotent.

## 7. Settings, Customization, MCP, and Integrations

Settings are typed and explicitly scoped:

- Account: identity, language, data controls.
- User: theme, density, notifications, behavior defaults.
- Device: local providers, folders, keychain references, background execution.
- Workspace: profile, rules, provider defaults, integrations, shared MCP definitions.
- Thread: model, runtime, autonomy, and instructions.
- Team policy: enforced constraints that lower scopes cannot override.

Settings navigation includes Account and workspaces, Appearance and accessibility, Voice and behavior, Custom rules, Models and providers, Runtime and autonomy, MCP and tools, Integrations, Notifications, Privacy/data, Team policy, and a truthful billing placeholder.

MCP supports local stdio and remote HTTP/SSE transports, capability discovery, health checks, per-workspace and per-agent enablement, tool-level permissions, vault-backed environment references, and explicit elevation approval. Invalid servers remain disabled. Logs redact tool arguments and secrets.

Desktop secrets use the OS keychain. Railway secrets use an encrypted server-side vault boundary. Corvus stores only opaque references. Initial integrations cover GitHub, signed generic webhooks, Slack/Discord-style notification webhooks, email delivery, and Google Calendar/Drive-compatible connection contracts. Each connection exposes permissions, health, last synchronization, test, reconnect, and revoke.

## 8. Teams and Collaboration

Core entities are `organizations`, `workspaces`, `memberships`, `invitations`, `roles`, `policy_bindings`, `assignments`, `comments`, `approval_requests`, `presence_sessions`, and `activity_events`.

Initial roles are Owner, Admin, Manager, Member, and Viewer. Roles produce capabilities on the server; onboarding choices do not. Collaboration includes invitations, workspace switching, assignments, comments, mentions, watchers, shared threads, shared knowledge/rules/MCP definitions, review queues, approval inboxes, presence, and an activity feed linked to immutable audit records.

Every repository query and event is tenant-scoped. Mutations use optimistic versions and idempotency. Authorization is rechecked at execution time. Removing a member revokes sessions, subscriptions, pending authority, and runtime access immediately. Personal drafts remain private until deliberately shared.

## 9. Local and E2B Runtime Placement

The common placement contract supports:

- Always use this device.
- Ask every run.
- Use any eligible local node.
- Prefer local and fall back to cloud.
- Cloud only.

Fallback requires an explicit policy and is forbidden when authority, data residency, provider capability, credential availability, or tool policy differs.

Local pairing uses short-lived single-use challenges, cryptographic proof, node heartbeat, and an authenticated outbound channel. Provider discovery reports capabilities without returning credentials. Runs are restricted to user-selected roots. Temporary disconnection uses buffered, idempotent event reconciliation.

E2B creation happens only after all Corvus gates pass. Heartbeats detect stalled sandboxes. Completion, cancellation, timeout, or failure captures approved artifacts and destroys the sandbox. A cleanup reconciler destroys orphans and records cost/audit metadata. Cloud remains labeled Preview until the complete lifecycle and verification gates pass; afterward it may be labeled Cloud Alpha while billing remains a placeholder.

## 10. Complete Product Experience

The shared React application uses real routes and data-backed views. Universal navigation provides New thread, Search, Inbox, Schedules, workspace navigation, and Settings. The top-left identity block shows workspace name and profile but is not an inline profile switcher.

Desktop uses a three-pane layout when space allows: navigation, conversation, and contextual inspector. Tablet collapses the inspector. Mobile is thread-first with bottom navigation and sheet controls. Desktop additionally exposes local provider discovery, folders, background execution, tray controls, node health, and terminal/artifact access. Hosted web never silently probes loopback.

The visual system preserves Corvus warm paper, ink, signal blue, restrained Fraunces editorial moments, mono utility labels, and the flightpath motif. Chat and controls prioritize readable sans typography. Generic card grids, copied T3Code chrome, and developer density in Everyday profiles are prohibited.

All controls are keyboard complete, preserve focus, use accessible names and live regions, meet contrast and target-size requirements, and respect reduced motion.

## 11. Error Handling and Recovery

Errors are typed and normalized at module boundaries. API responses expose stable reason codes and correlation IDs without leaking secrets. Clients distinguish validation, authentication, authorization, conflict, provider unavailable, runtime offline, approval required, budget exceeded, retryable infrastructure failure, and terminal failure.

Mutations are idempotent. Workers use leases and fencing. Sync uses cursors and explicit resync responses. Provider streams tolerate partial frames and reconnect from persisted cursors. E2B and local-node cleanup paths are auditable. No exception is silently swallowed, and no failed scan or execution is presented as success.

## 12. Verification Strategy

Each milestone must pass its focused tests before commit. The final branch must pass:

- Python unit, integration, security, migration, and PostgreSQL repository-contract tests.
- OAuth replay/nonce/session rotation, account-linking, revocation, and cross-tenant tests.
- Sync replay, cursor, conflict, offline, and two-device continuity tests.
- Provider/runtime conformance, malformed-stream, cancellation-race, and redaction tests.
- Scheduler restart, timezone/DST, fencing, offline-node, retry, and notification tests.
- MCP/tool escalation, malicious response, credential-vault, and integration-signature tests.
- React unit, API-contract, and accessibility tests.
- Playwright journeys for all four profiles across onboarding, chat/run, schedules, settings, teams, runtime placement, and recovery.
- Fixed screenshots at 1440x1000, 1024x900, and 390x844 plus reduced-motion evidence.
- Windows Tauri computer-use testing and Linux/macOS CI and installer smoke tests.
- Clean-install, cross-device, end-to-end acceptance with no secret leakage or orphaned cloud resources.

## 13. Delivery Sequence and Stop Boundary

One branch contains seven milestone commits:

1. Identity and continuity.
2. Threads and live agent execution.
3. Scheduling and unattended execution.
4. Settings, customization, MCP, and integrations.
5. Teams and collaboration.
6. Local and E2B runtimes.
7. Complete UX and integrated verification.

The detailed `PLAN.md` will specify file-level tasks, dependencies, tests, and completion gates for every milestone. After all milestones pass together, Corvus documentation is updated, the branch is pushed, and one pull request is opened for human and bot review. The agent stops with the pull request unmerged. Production database changes, paid billing activation, and automatic merge are outside this authorization.

## 14. Source Inspiration and Copy Boundaries

- [T3Code](https://github.com/pingdotgg/t3code) informs typed IPC boundaries, provider-neutral process sessions, server-authoritative event handling, and repository/worktree workflow patterns. Its application chrome and visual layout are not copied.
- [Hermes Agent](https://github.com/nousresearch/hermes-agent) informs provider adapters, scheduler contracts, credential isolation, tool approvals, and integration health. Its command surface and visual identity are not copied.
- The existing Corvus adaptive runtime specification remains the visual, persona, authority, and truthful-capability foundation.
- The Antigravity website-design blueprint governs source provenance, interaction specification, responsive screenshots, accessibility, motion evidence, and final visual audit.

## 15. Resolved Product Decisions

- Google OAuth is mandatory at first launch; guest mode is not part of this delivery.
- Railway/PostgreSQL is authoritative for shared state.
- SQLite remains a supported local repository implementation.
- Synchronization uses ordered change logs, idempotency, and explicit conflict resolution rather than silent last-write-wins.
- Local secrets remain local; hosted secrets are vault-backed; synchronized state contains references only.
- Team authority comes only from memberships and roles.
- Local schedules require an online paired node unless an explicit cloud fallback is authorized.
- Billing remains a truthful placeholder.
- One final pull request is opened and left for review.
