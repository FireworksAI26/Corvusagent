# Corvus V2 Roadmap — Milestones 1–11

This document is the readable delivery outline for Corvus V2. [`PLAN.md`](PLAN.md) remains the authoritative specification for records, migrations, sequencing, adversarial tests, and exact acceptance criteria. If this outline and `PLAN.md` differ, `PLAN.md` wins.

## Foundations already required before Milestone 1

- **Milestone 0 — Reproducible V1 baseline:** package Corvus with Python 3.12 and `uv`, freeze public V1 contracts and imports, introduce version-aware database bootstrap, and prove sealed idempotent quarantine capture.
- **Milestone 0.5 — Release-blocking V1 safety hardening:** enforce secret-safe snapshots, context provenance, trustworthy verification, atomic delivery, hardened provider boundaries, bounded execution, and independently reviewed security gates.

## Delivery rules

1. Milestones execute serially. A later milestone does not begin until the previous checkpoint passes its deterministic gates and required independent review.
2. Every client is a thin adapter over one transport-neutral authority, workflow, audit, and effect core. No CLI, API, web, desktop, connector, or channel-specific bypass is permitted.
3. Authority-bearing state is migration-backed, scoped, signed, replay-safe, crash-recoverable, and fenced by the current non-rollback workspace authority generation/root.
4. Secrets stay behind credential references and short-lived grants; they never enter prompts, events, traces, snapshots, errors, or portable packages.
5. Unsupported topology/client combinations remain disabled until their real packaged-client acceptance flow passes. Test doubles cannot enable support.

---

## Milestone 1 — Project authority vertical slice

**Objective:** Establish the authoritative project create/read path that every later client and workflow must use.

**Core delivery:**
- Deployment, workspace, client, execution, identity, scope, audience, capability, signing-key, idempotency, registry-trust, audit, handoff, restore, and non-rollback authority contracts.
- Additive migration-backed repositories and idempotent project/config import from sealed V1 quarantine.
- Fail-closed authorization with acting-agent intersection, scope containment, expiry/revocation, budgets, kill switches, signed decisions, and immutable receipts.
- One transport-neutral `create_project`/`get_project` service through identity → authorization → persistence → audit.

**Exit gate:** Equivalent authenticated contexts make identical decisions across enabled surfaces, while clone/rollback, stale registry, missing lock, anchor mismatch, revoked credentials, replay, tampering, cross-scope access, incomplete finalization, and disabled-adapter cases fail closed with historically verifiable receipts and bindings.

## Milestone 2 — Outcome contracts and durable workflow graphs

**Objective:** Replace ephemeral execution with restart-safe, proof-carrying workflows and one centralized effect path.

**Core delivery:**
- Immutable outcome contracts, pinned workflow graphs, fenced leases, attempts, checkpoints, recovery decisions, lineage, payload commitments, approvals, budgets, kill switches, and effect outboxes.
- Transactional claim/heartbeat/release/complete and workspace-sequenced events/audit.
- Durable conversation command/query/event ports with snapshot, replay, and cancellation semantics.
- Central effect gateway for provider and filesystem effects, current reauthorization, exact payload/binding checks, one-time approval consumption, conserved budgets, idempotency, reconciliation, compensation, and undo.

**Exit gate:** Restart, cancellation, replay, recovery, delivery apply/undo, budget conservation, approval consumption, effect deduplication, payload integrity, authority fencing, and unknown-provider-outcome handling pass without direct adapter mutation or synthetic credentials.

## Milestone 3 — Embedded CLI/TUI retained-surface and V2 project/run vertical slice

**Objective:** Prove the real retained CLI/TUI can operate entirely through the new in-process platform client.

**Core delivery:**
- Local identity bootstrap and additive `corvus v2 project create|get` commands.
- One complete build flow through context firewall, outcome contract, authorization, workflow graph, sandbox, independent verification, packaging, and approval.
- Pause/resume/cancel/retry, kill-switch, evidence, lineage, and event-tail commands.
- Cut retained chat/run/TUI and review/apply/undo paths to application ports; preserve V1 names, exit codes, JSON envelopes, and interactive behavior.

**Exit gate:** A fake-provider build produces a verifiable lineage-bound bundle; real retained surfaces use only the in-process client, and authorized filesystem apply/undo remains one-time, conflict-safe, crash-recoverable, and fully audited. Only then is embedded CLI support enabled.

## Milestone 4 — FastAPI, authentication, replayable events, and CLI daemon support

**Objective:** Expose the same authority core over authenticated HTTP/SSE without introducing a local bypass.

**Core delivery:**
- Local-daemon, self-hosted, and cloud auth adapters; command/query API; versioned OpenAPI; scoped SSE.
- Per-route and per-event authorization, non-enumerating errors, idempotency, CSRF/session/token controls, cursor replay, gap detection, and backpressure.
- Generated schema artifacts and in-process/HTTP parity.
- Loopback-only daemon ownership, pairing protocol, rotation/revocation, health/shutdown, crash recovery, and retained CLI HTTP/SSE cutover.

**Exit gate:** Unauthorized access leaks nothing; retained CLI/TUI and delivery flows match embedded behavior over HTTP/SSE; reconnect is gap/duplicate-free; hostile bind/origin/token/ownership/recovery cases fail closed. Only daemon CLI support is enabled.

## Milestone 5 — Real web workspace and daemon-web support

**Objective:** Deliver the first real browser client using generated contracts only.

**Core delivery:**
- pnpm workspace with React web app, shared UI package, and generated TypeScript contracts.
- One-time same-user pairing, secure session/CSRF lifecycle, logout/revoke, rotation/re-pair, daemon restart, and SSE reconnect.
- Project, work, conversation, outcome, evidence, lineage, delivery review/approve/apply/undo, limits, and kill-switch surfaces.
- API-backed Playwright, Vitest, accessibility, responsive, hostile-origin, duplicate-tab, restart, cancellation, and delivery recovery coverage.

**Exit gate:** The actual browser client securely pairs and reconnects, rejects hostile session/origin cases, and completes the full fake-provider project-to-undo flow using generated clients and backend-reported capabilities. Only then is daemon-web support enabled.

## Milestone 6 — Team, provider, secret-broker, and earned-autonomy slice

**Objective:** Add safe collaboration and real provider lifecycles without exposing credentials or allowing self-approval.

**Core delivery:**
- Team membership, reviewer separation, comments, approvals, and shared budgets.
- Provider connections, credential references/versions/grants, atomic rotation/revocation, and purpose/use-limit binding.
- OAuth Authorization Code + PKCE and device-flow lifecycles with ownership, expiry, refresh rotation, revocation, and recovery.
- Short-lived scoped secret grants, autonomy action/effect matrix, shadow comparisons, canaries, promotion, compensation, and rollback.
- Application ports plus real CLI/web settings and end-to-end OAuth/autonomy tests.

**Exit gate:** A real client completes a team run with a brokered provider and shadow proposal; both OAuth flows pass; shadow work has no production effect path; unauthorized members or agents cannot access secrets, expand autonomy, self-approve, or exceed limits.

## Milestone 7 — Governed memory, skills, routines, and context firewall

**Objective:** Make learning and automation explicit, evaluated, scoped, reversible, and safe to move.

**Core delivery:**
- Encrypted scoped memory with provenance, confidence, expiry, promotion, export, deletion, and retention receipts.
- Versioned skills with evaluations, hidden holdouts, approval, shadow/canary promotion, regression detection, and rollback.
- Immutable agent versions and signed secret-free portable packages that reauthorize against the destination workspace/chat.
- Context firewall enforcement for retrieved content and routine/webhook execution through normal authority, budget, kill-switch, evidence, and audit paths.
- CLI/API/web governance surfaces and real end-to-end import/export/promotion/rollback tests.

**Exit gate:** Prompt injection cannot grant authority; memory lifecycle is auditable; regressing skills roll back; portable agents transfer no source authority or secrets, obey destination capabilities, and can be reverted.

## Milestone 8 — Secure connector and offline operation

**Objective:** Let cloud work use explicitly paired local models and let local-authority work continue safely offline without creating dual authority.

**Core delivery:**
- Outbound-only mTLS connector pairing, ownership, model-only RPC, capability health, consent, revocation, updates, egress bounds, and audit.
- Signed offline intents, non-authoritative cache, reconnect/dedupe/conflict/replay handling, restore quarantine, and trust-anchor metadata.
- Offline local models, cached resources, governed memory, and queued tasks under the exact local authority generation/root and fixed OS lock.
- Connector/offline command/query ports plus CLI/web pairing, status, conflict, reconcile, and reconnect workflows.

**Exit gate:** Cloud never receives permanent local credentials; stale grants/epochs/generations, clones, rollbacks, restored databases, missing device activation keys, verifier rollback, replay, and reconnect conflicts fail closed; tests cannot create dual-primary authority.

## Milestone 9 — Channel gateways

**Objective:** Add bounded third-party messaging surfaces without granting them privileged authority.

**Core delivery:**
- Signed ingress, provider-event deduplication, identity mapping, immutable channel/thread bindings, rate limits, bounded replies/files, and correlation.
- Browser/desktop step-up authentication for privileged approval and apply operations.

**Exit gate:** Replayed, forged, or cross-thread messages cannot duplicate work, broaden authority, or approve high-risk effects.

## Milestone 10 — Packaged operational self-hosted and vendor-cloud deployment proof

**Objective:** Prove supported network topologies using the exact distributable artifacts, not source-tree substitutes.

**Core delivery:**
- Separate self-hosted/vendor-cloud topology contracts for TLS/auth, PostgreSQL, workers, effect gateway, artifacts, vault/KMS, tenancy, observability, maintenance, capacity, upgrades, backup/restore, and disaster recovery.
- Signed wheel/standalone retained CLI, OCI image, and versioned generated-React assets bound to lockfiles, provenance, and SBOMs.
- Install/upgrade/rollback, key rotation, failure recovery, staged rollout, quarantined restore/new-epoch takeover, and complete tenant-isolation tests.
- Packaged retained CLI HTTP and generated web flows for bootstrap/OIDC, sessions/CSRF, project/work/conversation/provider/delivery, reconnect, channel step-up, and recovery.

**Exit gate:** Exact signed artifacts complete authenticated end-to-end flows in both topologies; adversarial tenancy, rollout, token/session, callback, backup/restore, and rollback tests pass with verifiable receipts. CLI/web/channel support is enabled only for proven matrix rows; desktop remains disabled.

## Milestone 11 — Desktop packaging and distribution

**Objective:** Ship the stabilized generated web client as signed, update-safe desktop applications.

**Core delivery:**
- Signed Windows, macOS, and Linux Tauri installers with narrow capabilities, signed sidecar lifecycle, protocol compatibility, and rollback.
- Distribution matrix entries that consume—rather than recreate—the proven Milestone 10 CLI, OCI, and web artifacts.
- TUF-style threshold-signed updates with offline roots, delegated keys, expiry, rollback/freeze protection, rotation/revocation recovery, reproducible provenance, and SBOM binding.
- OS/architecture CI for keyrings, paths, links, service lifecycle, installer/update/rollback, metadata expiry/revocation, and optional sandbox routing.
- Installed-artifact tests for local pairing plus self-hosted and cloud authentication, conversation/work/delivery, session/origin controls, reconnect, upgrade, restore quarantine, and new-epoch recovery.

**Exit gate:** Installed desktop artifacts run proven local, self-hosted, and cloud flows through generated clients with no duplicate authority code or generic shell/filesystem bridge; provenance, SBOM, update security, key lifecycle, and rollback gates pass before desktop is enabled.

---

## Completion definition

Corvus V2 is complete only when Milestone 11 passes and the supported-combination matrix enables each claimed CLI, web, channel, and desktop topology based on real packaged-client evidence. Passing an isolated service test, mocked adapter, raw API fixture, or source-tree execution never substitutes for a milestone checkpoint.
