# Corvus Persona and Team Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing Corvus operator console into one web-and-desktop product with genuinely tailored Everyday/Developer and Personal/Team workspaces, a same-machine Local runtime, an E2B-backed Corvus Cloud runtime, shared Google identity, and real collaboration capabilities rather than cosmetic mode changes.

**Architecture:** Keep one authoritative Python domain/application core and one React client loaded by the existing Tauri shell or served by FastAPI. Model the product on two independent axes—experience (`everyday` or `developer`) and workspace (`personal` or `team`)—then compose shared primitives into four purpose-built information architectures. Add an independent runtime profile (`local` or `corvus_cloud`): Local supervises the loopback sidecar and SQLite on one machine, while Corvus Cloud provisions and reconnects to a secure, persistent E2B sandbox through server-side control-plane ports. Introduce identity, synchronization, and team capabilities as tenant-scoped domain services; never duplicate authority rules in clients.

**Tech Stack:** Python 3.12+, FastAPI, SQLite local mode, PostgreSQL-compatible deployment path, E2B Python SDK 2.x, Authlib 1.x, generated OpenAPI TypeScript types, React 19, TypeScript, Vite, Vitest/Testing Library, Tauri v2/Rust, SSE, Playwright/browser verification, Antigravity Website Design Blueprint.

## Global Constraints

- Preserve all M0.5-M11 security, compatibility, authority, redaction, streaming, desktop lifecycle, and update-verification behavior.
- Do not create four applications or four copies of business logic; build four workspace compositions over shared domain capabilities.
- Call the non-technical experience **Everyday** in product copy; do not label people “normal users.”
- Treat Team as shared multi-user workspaces with membership lifecycle, ownership, assignments, discussion, notifications, policy, and audit—not multiple local profiles.
- Treat Local as same-machine only. Do not automatically expose a user's machine to the LAN or public internet.
- Keep `E2B_API_KEY`, Google client secrets, refresh tokens, sandbox control credentials, and traffic-access tokens server-side or in approved secret references; never ship them in React/Tauri bundles.
- Corvus Cloud must use pinned E2B templates, secure access, pause-on-idle, auto-resume, reconnectable sandbox IDs, health checks, and explicit lifecycle/error states.
- Keep secrets as broker references; never expose credential material in UI state, logs, events, fixtures, or API responses.
- Use source repositories only for interaction-pattern research. Do not copy their branding, layouts, source code, or developer density into Everyday experiences.
- No new dependency may be installed until it is listed at the milestone preflight. The user has granted approval, but the dependency and reason must still be recorded before installation.
- Every visible feature requires an executable API/domain path or a clearly labeled unavailable state; no static mock controls.
- Respect keyboard navigation, visible focus, reduced motion, contrast, empty/loading/error/offline states, and Windows scaling.
- Verify desktop and 390x844 responsive screenshots before each UI milestone is declared complete.
- Update `PROJECT_LOG.md` after every milestone.
- After each milestone passes its gate, create one logical commit with `Co-Authored-By: Claude <noreply@anthropic.com>` and push that commit to GitHub `main`.

---

## Confirmed Product Model

| Workspace | Primary job | Default navigation | Technical detail |
|---|---|---|---|
| Everyday Personal | Turn an intent into a safe, understandable result | Home, My Work, Automations, Files | Hidden behind “Details” |
| Developer Personal | Build, inspect, and control technical work | Repositories, Threads, Changes, Runs, Skills | First-class |
| Everyday Team | Coordinate owned work, handoffs, approvals, and shared knowledge | Team Home, Assigned Work, Approvals, Knowledge, People | Translated into impact/status language |
| Developer Team | Coordinate repository work, reviews, environments, policy, and cost | Repositories, Work Queue, Reviews, Environments, Policies | First-class and auditable |

Shared data must survive switching workspace presentation. A project, run, approval, artifact, or conversation is one record with different views—not duplicated persona data.

## Confirmed Runtime Model

| Runtime | Host and reachability | Persistence | Identity and synchronization | Availability |
|---|---|---|---|---|
| Local | Tauri-supervised loopback sidecar; browser may open the same local Corvus URL on that machine | SQLite and local filesystem | Local pairing/session; no automatic cross-device synchronization | Fully usable offline on one machine |
| Corvus Cloud | Corvus control plane provisions a secure E2B sandbox and returns an authenticated workspace endpoint | E2B pause/resume state plus tenant-scoped durable records; sandbox ID is reconnectable | Google OIDC maps the stable provider subject to one Corvus principal used by web and desktop | Cloud Preview entitlement until billing is implemented |

Device-local repository paths never become cloud paths implicitly. Cloud workspaces use explicit cloud repository registrations, uploaded artifacts, or provider references.

## Confirmed Product Decisions

The user approved these defaults for implementation:

1. **Product packaging:** one Corvus desktop binary with onboarding and a persistent workspace switcher, not separate editions.
2. **Runtime choice:** onboarding and settings offer same-machine Local or Corvus Cloud on E2B. Local is never auto-exposed to other devices.
3. **Team deployment:** E2B is the first Corvus Cloud runtime provider behind a provider-neutral application port; SQLite remains the local path.
4. **Identity:** Google OIDC is the first shared-account provider. The stable issuer/subject pair maps to the Corvus principal; email is display/contact data, not identity.
5. **Collaboration transport:** durable API records plus SSE invalidation/notifications. Presence indicators are deferred until multi-user correctness is proven.
6. **Notifications:** in-app inbox in scope; email, Slack, and Teams delivery adapters deferred.
7. **Mobile:** responsive companion/browser verification only; no separate mobile application in this plan.
8. **Billing:** Cloud Preview plan/paywall pages and entitlement contracts are in scope; payment collection and subscription billing are not.
9. **Entitlement truthfulness:** preview pages never claim a payment succeeded. Development/test environments may use an explicit preview entitlement bypass.
10. **Account continuity:** cloud projects, threads/conversations, workflows, artifacts, preferences, memberships, and notifications are available to the same account on web and desktop; local filesystem paths remain device-local.

## Milestone 0 — Approved UX Architecture and Blueprint Packet

**Purpose:** Lock the information architecture, interaction contracts, and visual system before changing application code.

### Tasks

- [x] Run the required blueprint sequence from the repository root through approved build-gate validation: intake, research, target, section plan, compose, change plan, snapshot, packet verification, approval, and build. Run the visual audit after the Milestone 1 UI exists.
- [x] Produce `.antigravity/website-blueprint/SOURCE_MANIFEST.json`, `SECTION_PLAN.json`, `FRONTEND_CRAFT_BRIEF.md`, `EXPERIENCE_STORYBOARD.json`, `INTERACTION_SPEC.json`, `COMPONENT_ADOPTION_MAP.json`, and baseline screenshots.
- [x] Document journeys for all four workspaces: first run, resume work, create work, observe progress, approve/reject, recover from failure, switch workspace, invite/join team, and lose/recover connectivity.
- [x] Document Local and Corvus Cloud selection, Google sign-in, E2B provisioning/resume/failure, cloud-preview entitlement, and switching-runtime journeys without implying automatic data migration.
- [x] Define shared design tokens and four density/copy/navigation profiles without changing Corvus’s core identity.
- [x] Specify truthful progressive disclosure: Everyday uses goals, impact, owner, next step, and deliverable; Developer exposes IDs, branches, diffs, logs, policies, autonomy, budget, and environment.
- [x] Record T3Code and Hermes patterns adopted, adapted, and rejected with source provenance.
- [x] Review the packet for placeholders, contradictory navigation, fake controls, and inaccessible interactions.

### Gate

- The user approves the four workspace storyboards and visual direction.
- No product code changes occur before this gate.

### Commit

- `docs(ui): approve four-workspace desktop architecture`

## Milestone 1 — Shared Application Shell and Preference Foundation

**Purpose:** Create one stable shell that can compose four workspaces without branching the product into four codebases.

### Expected files

- `apps/web/src/App.tsx` — reduce to authentication/bootstrap and top-level routing.
- `apps/web/src/app/AppShell.tsx` — title bar, workspace switcher, command/search entry, connection status, account menu.
- `apps/web/src/app/WorkspaceRouter.tsx` — selects the approved workspace composition.
- `apps/web/src/app/preferences.ts` — typed experience/workspace preference model and migration-safe persistence.
- `apps/web/src/app/runtime.ts` — typed Local/Corvus Cloud selection, connection state, and safe persisted non-secret profile.
- `apps/web/src/components/` — shared navigation, status, composer, approval, empty-state, and error-boundary primitives.
- `apps/web/src/styles/` — tokens, shared shell, density, motion, and responsive rules.
- `apps/web/src/App.test.tsx` plus focused component tests.

### Tasks

- [x] Write tests first for first-run selection, persistence, switching, keyboard navigation, invalid preference recovery, pairing, reconnect, and capability-gated Team presentation.
- [x] Add a short onboarding chooser that asks how the person works and whether the workspace is personal or shared; always allow later switching.
- [x] Add a runtime step before any service connection: Local explains same-machine storage; Corvus Cloud explains synchronization and Cloud Preview status.
- [x] Add a truthful Cloud Preview plan page with disabled payment collection, no fake sign-in, and an immediate Local fallback.
- [x] Preserve fragment-only desktop pairing and existing session repair.
- [x] Implement shared connection/error/loading states and a route-level render error boundary.
- [x] Add semantic navigation landmarks, focus restoration, skip links, reduced-motion behavior, responsive navigation, and a full-screen mobile inspector.
- [x] Keep all data access behind the typed `CorvusApi` adapter.

### Gate

- [x] Existing pairing/project/workflow tests pass.
- [x] New shell tests pass with all four route compositions.
- [x] Production web and Tauri builds pass; the post-pair console is clean at desktop and 390x844.

### Commit

- `feat(ui): add adaptive Corvus workspace shell`

## Milestone 2 — Personal Workspaces

**Purpose:** Deliver complete, differentiated Everyday Personal and Developer Personal workflows on the existing execution core.

### Everyday Personal

- [ ] Build Home around “What do you want to get done?”, recent results, upcoming approvals, and resumable work.
- [ ] Replace workflow jargon with guided goal creation: desired result, useful context/files, constraints, review level, and confirmation.
- [ ] Present progress as a readable timeline with current step, why Corvus paused, what needs attention, and expected deliverables.
- [ ] Provide My Work, Automations, and Files views with plain-language actions and technical detail drawers.
- [ ] Make approvals explain the proposed action, affected data/system, reversibility, cost, and safe alternatives.

### Developer Personal

- [ ] Build repository registration and project association contracts before exposing repository navigation.
- [ ] Add repositories, threads, changes, runs, and skills views using existing workflows, artifacts, conversations, routines, and skills where possible.
- [ ] Add typed Git/repository ports for status, branches/worktrees, diffs, and commit metadata; keep native operations in trusted backend/desktop adapters.
- [ ] Expose branch, environment, model/provider reference, autonomy, budget, logs, checkpoints, artifacts, and test results.
- [ ] Add review actions with explicit diffs and governed apply/commit boundaries; never let the React client manufacture authority.

### Likely backend files

- `corvus/domain/` — repository/work-context records only where missing.
- `corvus/application/` — repository inspection and developer-workspace use cases.
- `corvus/ports/` and `corvus/adapters/` — typed Git/filesystem inspection boundaries.
- `corvus/api.py` and `openapi/corvus-mvp.json` — tenant-scoped endpoints and generated contracts.
- `tests/mvp/` — authorization, traversal, redaction, and adapter behavior.

### Gate

- A non-technical user can create, monitor, approve, and retrieve a result without seeing IDs or execution jargon.
- A developer can inspect the same run’s repository context, changes, logs, artifacts, and controls.
- Cross-view tests prove both presentations reference the same durable records.
- Desktop and responsive visual audits pass.

### Commit

- `feat(ui): deliver tailored personal workspaces`

## Milestone 3 — Shared Identity and E2B Cloud Runtime

**Purpose:** Make web and desktop use one account and one cloud workspace while preserving a complete same-machine Local path.

### Identity and session tasks

- [ ] Add `Authlib >=1.3,<2` and implement provider-neutral OIDC authorization-code ports with state, nonce, PKCE, discovery/JWKS caching, issuer/audience/expiry validation, and one-time callback consumption.
- [ ] Add Google configuration through secret references and environment validation; never accept client secrets or refresh tokens from the React/Tauri clients.
- [ ] Persist external identity links by `(issuer, subject)` and map them to existing principals, tenants, and workspace memberships. Do not key identity by email.
- [ ] Issue Corvus sessions after server-side OIDC validation and support account/session revocation across web and desktop.
- [ ] Keep simulated OIDC and local pairing as fully functional test/development paths.

### E2B control-plane tasks

- [ ] Add `e2b >=2.3,<3` behind a `CloudRuntimePort`; the domain layer must not import the SDK.
- [ ] Define cloud workspace and runtime records: provider, template/build pin, sandbox ID, endpoint reference, lifecycle, health, owner tenant, last activity, and version.
- [ ] Build a pinned Corvus E2B template with the real FastAPI/static client, start/readiness commands, non-root execution, secure access, and no embedded customer secrets.
- [ ] Provision with server-side `E2B_API_KEY`, restricted network/public traffic policy, pause-on-timeout, auto-resume, metadata, and tenant-scoped environment references.
- [ ] Implement create, inspect, connect, resume, pause, replace-after-terminal-failure, and revoke operations with idempotency and audit events.
- [ ] Treat the E2B endpoint as untrusted until authenticated Corvus readiness succeeds; never expose sandbox controller credentials to clients.

### Synchronization and client tasks

- [ ] Make the hosted FastAPI service authoritative for cloud records; web and desktop use the same typed API and SSE cursor.
- [ ] Persist only non-secret runtime selection and workspace identifiers in clients. Sessions use secure cookies or an OS credential reference.
- [ ] Reconcile offline cloud intents with idempotency keys and conflict/version responses; show conflicts instead of silently overwriting.
- [ ] Keep Local projects local. Runtime switching does not migrate data unless a later explicit export/import flow is invoked.

### Gate

- A simulated Google identity opens the same tenant/project from web and desktop clients.
- An E2B adapter contract test provisions/reconnects/pauses/resumes idempotently; a live E2B smoke runs when `E2B_API_KEY` is available.
- Local mode continues to launch, operate offline, restart, and preserve SQLite state without cloud credentials.
- Cross-tenant, callback replay, state/nonce/PKCE, token-validation, credential-redaction, and sandbox-endpoint substitution tests pass.

### Commit

- `feat(cloud): add shared identity and E2B runtime`

## Milestone 4 — Real Team Collaboration Core

**Purpose:** Make Team a correct multi-user product surface before building team dashboards.

### Domain and persistence

- [ ] Add tenant/workspace-scoped invitations with expiry, acceptance, cancellation, and replay protection.
- [ ] Extend membership lifecycle with owner/admin/member/viewer responsibilities, role changes, suspension, removal, and last-owner protection.
- [ ] Add work assignment, owner, due date, status, watcher, and handoff records linked to existing outcomes/workflows/work items.
- [ ] Add threaded comments, mentions, reactions only if required by the approved spec, edits with audit history, and soft deletion policy.
- [ ] Add durable in-app notifications with read state, deduplication, cursor pagination, and preference controls.
- [ ] Add team policy records for approval thresholds, autonomy ceilings, budget limits, provider grants, retention, and member capabilities.
- [ ] Add workspace-scoped shared knowledge visibility and provenance rules.
- [ ] Ensure every mutation records the acting principal and audit event.

### API and synchronization

- [ ] Expose generated, tenant-scoped endpoints for invitation, membership, assignment, comment, notification, policy, and knowledge operations.
- [ ] Add optimistic concurrency/version checks for editable shared records.
- [ ] Extend SSE with workspace-scoped invalidation events and reconnect cursors; avoid premature presence/chat protocols.
- [ ] Verify revocation takes effect on the next request and active streams terminate or lose access safely.
- [ ] Add PostgreSQL-backed integration coverage where available while retaining a functional SQLite local adapter.

### Security tests

- [ ] Cross-tenant access denial for every new repository and endpoint.
- [ ] Role escalation, invitation replay, removed-member access, mention leakage, notification leakage, stale update, and audit tamper tests.
- [ ] Secret-reference and sensitive-payload redaction tests.

### Gate

- Two distinct principals can join one workspace, assign work, discuss it, receive an in-app notification, and observe the same durable state.
- A removed member immediately loses access.
- Cross-tenant and stale-write tests pass.

### Commit

- `feat(teams): add governed multi-user collaboration core`

## Milestone 5 — Team Workspaces

**Purpose:** Build two team experiences over Milestone 3’s real collaboration services.

### Everyday Team

- [ ] Team Home: shared goals, health, blocked work, recent outcomes, and a clear “needs attention” queue.
- [ ] Assigned Work: owner/date/status-oriented list and board views without execution internals.
- [ ] Approvals: impact-first review queue with delegation/handoff and policy explanation.
- [ ] Shared Knowledge: provenance, visibility, freshness, and source-aware retrieval.
- [ ] People: invitations, roles, availability/status only when truthful, and responsibility summaries.

### Developer Team

- [ ] Repositories: registered repos, branch/worktree state, ownership, and current activity.
- [ ] Work Queue: assignments, dependencies, runs, failures, and developer-level filters.
- [ ] Reviews: diffs, checks, artifacts, comments, approvals, and governed commit/apply controls.
- [ ] Environments: provider references, readiness, deployment state, and audit trail—never secret values.
- [ ] Policies: autonomy ceilings, approval requirements, budget/cost, role capabilities, and retention.

### Cross-workspace behavior

- [ ] Provide permalinks and consistent object identity across Everyday and Developer views.
- [ ] Translate technical statuses into Everyday copy without hiding risk or inventing certainty.
- [ ] Support comments/mentions/notifications from contextual side panels, not a disconnected admin page.
- [ ] Add empty states for new teams and migration guidance for an existing personal project joining a team.

### Gate

- End-to-end browser tests cover invite → join → assign → execute → comment → approve → deliver → notify.
- Role-specific screenshots prove each user sees appropriate navigation and actions.
- Keyboard, reduced-motion, offline/reconnect, desktop, and responsive audits pass.

### Commit

- `feat(ui): deliver everyday and developer team workspaces`

## Milestone 6 — Desktop Integration and Operational Hardening

**Purpose:** Make the adaptive product feel native and remain safe under real desktop lifecycle conditions.

### Tasks

- [ ] Preserve the existing fixed-argument sidecar launch, authenticated readiness, one-time fragment pairing, navigation restrictions, CSP, graceful shutdown, and redacted diagnostics.
- [ ] Add native project/repository/file selection only through narrow Tauri commands with validated paths and least privilege.
- [ ] Restore the last valid workspace and route after sidecar restart without persisting pairing secrets.
- [ ] Surface starting, ready, reconnecting, offline, failed, and stopped states in all four experiences using persona-appropriate copy.
- [ ] Validate Windows scaling, window resizing, keyboard accelerators, minimum window size, high contrast, and screen-reader labels.
- [ ] Run Rust lifecycle/security tests, web tests/build, Python focused/full tests, type checks, lint, security scan, desktop no-bundle build, and package smoke test.
- [ ] Update `README.md`, `HACKATHON_STATUS.md`, and `PROJECT_LOG.md` with truthful local/remote/team setup and limitations.

### Gate

- The packaged Windows app launches, pairs, switches among allowed workspaces, reconnects after sidecar restart, and shuts down without orphan processes.
- All affected CI jobs are green on the pushed main commit.
- The worktree is clean.

### Commit

- `release: certify adaptive Corvus desktop MVP`

## Verification Matrix

| Concern | Automated evidence | Runtime evidence |
|---|---|---|
| Shared authority | Python service/repository tests | Attempt forbidden UI/API action |
| Persona composition | Vitest route/copy/action tests | Four desktop screenshots |
| Team isolation | Cross-tenant API and persistence tests | Two-browser/principal scenario |
| Collaboration recovery | SSE cursor, stale write, notification tests | Disconnect/reconnect scenario |
| Runtime selection | preference, entitlement, and connection-state tests | Switch Local/Cloud without leaking or migrating local state |
| Shared account | OIDC callback/replay/revocation and tenant tests | Same cloud workspace in browser and desktop |
| E2B lifecycle | fake adapter contract and optional live smoke | Provision, pause, auto-resume, reconnect, revoke |
| Accessibility | semantic queries and automated audit | keyboard, focus, reduced motion, high contrast |
| Desktop lifecycle | Rust unit/integration tests | launch, sidecar restart, close/process check |
| Responsive behavior | layout tests where useful | 390x844 screenshots for all core routes |
| Supply chain | existing build/SBOM/provenance checks | packaged artifact smoke test |

## Git and Release Procedure for Every Milestone

- [ ] Confirm `git status --short --branch` and preserve unrelated user changes.
- [ ] Pull/fetch and verify the milestone is based on current `origin/main`; never rewrite history.
- [ ] Run the milestone’s focused gate, then the proportional regression suite.
- [ ] Update `PROJECT_LOG.md` using the required format and record assumptions/deferred work.
- [ ] Review `git diff --check`, staged diff, and secret scan.
- [ ] Commit with the milestone message and required co-author trailer.
- [ ] Push the tested commit directly to GitHub `main` as requested.
- [ ] Confirm the remote main SHA and CI run; fix failures in a follow-up milestone commit before proceeding.

## Explicit Stop Boundaries

1. **Current execution state:** Milestone 0 is approved; Milestone 1 implementation is authorized and in progress.
2. **After Milestone 1:** stop for hands-on review of the adaptive shell, runtime chooser, and four workspace compositions before identity or database migrations.
3. **After Milestone 2:** stop for hands-on review of both Personal experiences before cloud/identity implementation.
4. **After Milestone 3:** stop for a security/correctness review of Google identity and E2B lifecycle before collaboration migrations.
5. **After Milestone 4:** stop for a security/correctness review of multi-user behavior before Team UI implementation.
6. **After Milestone 6:** stop after release evidence; production payment collection, email/chat notification delivery, notarization, and multi-OS certification require separate authorization.

## Planning Assumptions to Review

- One binary and one account can access multiple workspace experiences.
- Experience selection changes information architecture and defaults, never permissions or stored truth.
- Existing projects remain valid and default to Everyday Personal until a user chooses otherwise.
- Local is same-machine only; Corvus Cloud on E2B is the synchronized remote path.
- Google credentials and an E2B API key may be absent during development, so simulated adapters are required and live external smoke tests are conditional on those secrets.
- In-app notifications and SSE are sufficient for the first complete Team solution.
- Repository/Git capabilities are required for Developer workspaces but remain behind typed trusted adapters.
- The current React/Vite/Tauri/Python stack remains; no framework rewrite is planned.

## Known Scope Deferred by This Plan

- Production cloud provisioning and managed service operations.
- Production Google OAuth application registration, consent-screen verification, and enterprise directory provisioning.
- Email, Slack, Microsoft Teams, or push-notification delivery adapters.
- Payment collection, subscription billing, invoices, and marketplace flows; only truthful Cloud Preview entitlement pages/contracts are included.
- Live cursors/presence, voice/video, and full chat replacement.
- macOS signing/notarization and unsupported multi-platform installer certification.
- A separate mobile application.
