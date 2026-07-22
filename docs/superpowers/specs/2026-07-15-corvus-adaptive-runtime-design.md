# Corvus Adaptive Workspace and Runtime Design

**Status:** Approved for staged implementation on 2026-07-15
**Stop boundary:** Implement and release Milestone 1, then stop for hands-on review before identity or database migrations.

## 1. Objective

Turn the current Corvus operator console into one web-and-desktop product with four genuinely tailored workspace experiences:

- Everyday Personal
- Developer Personal
- Everyday Team
- Developer Team

Work style and workspace scope are independent, reversible presentation choices over one authoritative capability and permission model. Runtime is a third independent choice: same-machine Local now, or an E2B-backed Corvus Cloud workspace after the identity/control-plane milestone.

## 2. Confirmed Decisions

1. **Local is same-machine only.** The Tauri shell supervises a loopback Python sidecar and SQLite. A browser on that same machine may use the same loopback service. A hosted web client never claims it can attach to a private local runtime.
2. **Corvus Cloud uses E2B.** One secure, persistent sandbox is bound to a Corvus workspace. Personal or authorized Team members access the same authoritative cloud workspace through a Corvus control plane/gateway.
3. **Cloud continuity uses Google OIDC.** External identity is keyed by validated `(issuer, subject)`, not email. Desktop and web sessions map to the same Corvus account and memberships.
4. **No local/cloud dual-write.** Local data remains local. Cloud data remains authoritative in its cloud workspace. Runtime switching does not silently migrate or merge data.
5. **Payments are deferred.** Cloud plans are Preview/Not yet available. No card fields, checkout, fake purchase result, or paid entitlement claim is allowed.
6. **One client and one domain.** Four workspaces are configuration-driven compositions. Client presentation cannot grant permissions.

## 3. Product Axes

```text
ExperienceMode = everyday | developer
WorkspaceScope = personal | team
RuntimeMode = local | corvus_cloud
```

The first client preference is versioned and validated:

```ts
interface WorkspacePreferenceV1 {
  version: 1;
  experience: "everyday" | "developer";
  scope: "personal" | "team";
  runtime: "local" | "corvus_cloud";
  workspaceId?: string;
  onboardingComplete: boolean;
}
```

Invalid or unknown versions return to setup with a polite recovery message. The preference stores no credential, token, role, or authorization claim.

## 4. First-run Journey

### Step 1 — Work style

Heading: **How do you want Corvus to work with you?**

- **Everyday** — Clear plans, progress, approvals, and results. Technical details stay available.
- **Developer** — Repositories, runs, diffs, logs, policy, and precise controls.

### Step 2 — Scope

Heading: **Who is this workspace for?**

- **Just me** — Private work and personal automations.
- **My team** — Assign work, review decisions, and share knowledge.

Selecting Team says “You can invite people after setup.” It does not create a team or grant membership.

### Step 3 — Runtime

Heading: **Where should Corvus run?**

- **On this computer** — Corvus and your data stay on this device. Use it in the desktop app or a browser on this computer. CTA: **Use this computer**.
- **Corvus Cloud (E2B)** — Use the same workspace from desktop and web. Google sign-in required. Cloud Preview; billing comes later. CTA: **Continue to Cloud Preview**.

All steps use semantic radio groups, arrow keys, visible focus, Back, step count, and “Change anytime.” No workspace API fetch occurs before runtime choice. Completion focuses the workspace `h1`.

## 5. Workspace Information Architecture

| Workspace | Navigation | Home emphasis | Detail language |
|---|---|---|---|
| Everyday Personal | Home, My Work, Automations, Files | Today, help, outcomes, routines | plan, progress, source, result |
| Developer Personal | Repositories, Threads, Changes, Runs, Skills | repos, agents, checks, branches | IDs, tool calls, logs, diffs, tests, budget |
| Everyday Team | Team Home, Assigned Work, Approvals, Knowledge, People | assignments, review needs, shared outcomes | owner, status, decision, comment, handoff |
| Developer Team | Repositories, Work Queue, Reviews, Environments, Policies | engineering queue, failures, reviews, activity | diff, check, approval, audit, ownership, cost |

Capability-filter routes and actions before rendering them. A direct visit to a forbidden route opens an explanatory access page. Team profile selection alone never enables Team actions.

## 6. Shared Client Boundaries

The current monolithic `App.tsx` becomes session and bootstrap coordination only.

```text
apps/web/src/
  app/
    AppShell.tsx
    OnboardingFlow.tsx
    WorkspaceRouter.tsx
    preferences.ts
    workspaceProfiles.ts
  runtime/
    CloudPreview.tsx
    RuntimeChooser.tsx
    RuntimeGate.tsx
    RuntimeStatus.tsx
  components/
    AsyncState.tsx
    ConnectionBanner.tsx
    ErrorBoundary.tsx
    NavigationRail.tsx
    ResponsiveNavigation.tsx
    SkipLink.tsx
    WorkspaceSwitcher.tsx
  workspaces/
    EverydayPersonalWorkspace.tsx
    DeveloperPersonalWorkspace.tsx
    EverydayTeamWorkspace.tsx
    DeveloperTeamWorkspace.tsx
  styles/
    tokens.css
    shell.css
    profiles.css
    responsive.css
    motion.css
```

Views consume typed capabilities/session/runtime contracts. They do not fetch directly. No router dependency is needed for the first shell; typed route keys are sufficient.

## 7. Runtime States and Copy

```text
choosing | starting | online | reconnecting | offline |
auth-required | paused | unavailable
```

- Local online: **Local · Connected**
- Cloud online: **Cloud · Synced**
- Local start: **Starting Corvus on this computer…**
- Cloud paused: **Cloud workspace paused. Waking it…**
- Reconnecting: **Connection interrupted. Reconnecting…**
- Auth required: **Your session expired. Sign in again to continue.**
- Cloud unavailable: **We couldn’t reach your cloud workspace. Try again or open a local workspace.**
- Offline without durable queue: **Editing is unavailable until connection returns.**

Use `role=status`/`aria-live`, keep focus stable, and make Retry keyboard accessible. Never say an edit will sync later until the durable queue capability is true.

## 8. Cloud Preview and Entitlements

Preview copy:

> Corvus Cloud is in preview. Cloud workspaces run in isolated E2B environments and sync across your signed-in devices. No payment will be collected.

Show **Sign in with Google** only when the auth API capability exists. Otherwise show **Cloud setup is not available in this build** and **Use local workspace**.

Plans show a Preview badge, **Cloud plans are coming later**, **Not yet available**, and a disabled **Billing not enabled** control. There are no prices, card inputs, enabled purchase controls, or success screens.

## 9. Identity and Session Architecture

The Corvus control plane is authoritative for identity, memberships, entitlements, and workspace-to-sandbox routing.

Required concepts:

- `VerifiedIdentity(issuer, subject, display_name, email_snapshot, email_verified, avatar_url)`
- `AuthTransaction(state_digest, nonce_digest, pkce_verifier_vault_ref, redirect_target, expires_at, consumed_at)`
- `AuthSession(principal_id, session_digest, issued_at, expires_at, revoked_at)`
- `AccountPreference(principal_id, experience, scope, last_workspace_id)`

Required ports:

- `OidcProviderPort`
- `AccountIdentityRepository`
- `TokenVaultPort`
- `WorkspaceGatewayPort`

OIDC uses authorization code, state, nonce, PKCE, exact redirect validation, discovery/JWKS caching, issuer/audience/expiry/issued-at validation, and one-time callback consumption. Roles and memberships come from Corvus persistence, never Google claims or client input.

Web sessions use secure, HTTP-only, SameSite cookies. Exact loopback development may use non-Secure cookies. Desktop remains a loopback BFF: React calls the sidecar, while OS keyring/server-side storage owns cloud refresh credentials. Tokens never enter localStorage.

## 10. E2B Cloud Architecture

Required concepts:

- `WorkspaceRuntimeBinding(workspace_id, provider, sandbox_id, template_ref, generation, state, endpoint_ref, last_ready_at)`
- `RuntimeOperation(idempotency_key, workspace_id, generation, operation, state, error_code, timestamps)`
- `EntitlementStatus = preview | eligible | blocked`

Required ports:

- `CloudRuntimeProvider`
- `RuntimeBindingRepository`
- `WorkspaceGatewayPort`

Lifecycle:

```text
UNPROVISIONED -> PROVISIONING -> STARTING -> READY
READY -> PAUSING -> PAUSED -> RESUMING -> READY
active state -> DEGRADED | FAILED
killed sandbox -> LOST (terminal); explicit reprovision creates a new generation
```

Provision/resume operations are idempotent and generation-fenced. E2B templates are pinned. Secure access is enabled. E2B API keys remain server-side. The gateway issues short-lived internal assertions bound to workspace, sandbox, and generation; a raw client-supplied sandbox URL is never authority.

The control plane must live outside the sandbox so a signed-in user can discover their workspace before the sandbox endpoint is known. Pause/resume and auto-resume are surfaced as lifecycle state, not hidden as generic network loading.

## 11. Team Collaboration Boundary

The four-workspace shell may present capability-gated Team navigation, but real Team actions wait for authoritative models and APIs for organizations/workspaces, memberships, invitations, groups, roles, assignment, comments, mentions, notifications, review queues, shared policies, audit retention, and multi-user conflict handling.

Existing project-level team records are insufficient as the authorization boundary. Team UI ships only after cross-tenant and permission tests pass.

## 12. Visual System

Preserve warm paper, ink, cobalt, copper, graph-line texture, flight-path activity motif, Fraunces, Inter Tight, and IBM Plex Mono. Everyday is spacious and narrative. Developer is denser and implementation-first. Personal uses cobalt emphasis. Team adds accessible teal plus owner/scope labels; color is not the only distinction.

Desktop: 240px navigation, flexible content, optional 360px inspector. Mobile 390x844: compact top bar, four primary bottom navigation items plus More, context drawer, full-screen inspector. Web omits native drag/window affordances; content components and tokens remain shared.

## 13. Verification Contract

### Milestone 1 automated

- Missing preference opens setup and prevents workspace fetch before runtime choice.
- All four combinations save/reload correct navigation and copy.
- Invalid preference safely returns to setup.
- Profile switching preserves selected entity IDs and never duplicates data.
- Team preference does not grant capabilities.
- Forbidden routes show an access explanation.
- Runtime cards contain exact Local and Preview truth.
- Cloud Preview has no payment inputs, price claim, enabled purchase button, or fake success.
- Pairing fragment is consumed/removed and never rendered or logged.
- Status, skip link, focus restoration, keyboard selection, reduced motion, mobile navigation, inspector dialog, error/loading/empty/reconnect cases pass.

### Runtime evidence

- 1440x960 and 390x844 screenshots for all four profiles plus setup, Cloud Preview, and offline state.
- Keyboard-only, 200% Windows scaling, high contrast, and reduced motion passes.
- Packaged Windows Computer Use later covers launch, Local setup, project selection, four profile switches, inspector, narrow resize, persisted restart, sidecar recovery, pairing-secret absence, Cloud Preview truth, and child-process exit.
- Hosted web later repeats profile/responsive/Google entry states. A two-browser Team test waits for the real collaboration milestone.

## 14. Dependency and Delivery Plan

- Milestone 1 adds no dependency.
- Identity milestone plans `Authlib >=1.3,<2`.
- Cloud runtime milestone plans `e2b >=2.3,<3`.
- A Tauri system-browser opener is added only if the existing shell cannot safely launch the platform browser for OIDC.

Every milestone updates `PROJECT_LOG.md`, runs focused and proportional regression checks, reviews staged changes and secrets, commits with the required co-author trailer, pushes the tested commit to GitHub `main`, and verifies remote SHA/CI.

## 15. Deferred

Production Google credentials, E2B API key/template, production control-plane endpoint, payment collection, automatic local/cloud migration, email/chat delivery, notarization, and multi-OS certification remain deferred until separately configured or authorized.
