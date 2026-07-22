# Provider Runtime and Unattended Mode Decision Boundary

Date: 2026-07-15

## Task Understanding

- Continue end-to-end testing of the adaptive desktop/web shell.
- Streamline the UX without removing expert controls.
- Let a workspace use an installed local AI tool or an API-key-backed provider.
- Add an unattended mode that can keep working while the user is away.
- Preserve Corvus authority, credential, sandbox, idempotency, budget, approval, audit, redaction, and kill-switch invariants.

## Verified Current State

- Codex 0.144.0, Claude Code 2.1.209, and Gemini CLI 0.44.1 are installed; their existing login state was detected without reading credentials.
- Cursor/cursor-agent and Grok/xAI CLIs are not installed.
- Corvus currently has one local CLI adapter: Codex, restricted to text-only execution with tools forbidden.
- API-key providers already use opaque references and OS-keyring-backed storage, but that path is not unified with the MVP provider screen.
- The provider screen currently records a provider/reference and evaluates a shadow action; it does not execute a selected local agent.
- The live E2E pass verified project/team/provider-reference/memory/skill/routine/workflow flows, secret-reference containment, approval blocking, successful post-approval completion, reload persistence, and mobile width without horizontal overflow.
- A hard offline page reload is not supported; the browser reaches its network error page and requires connectivity before reopening Corvus.

## Assumptions That Require Confirmation

1. "Full auto" means a pre-authorized unattended envelope, not a permission bypass.
2. Existing local CLI login state should be used through each vendor's supported CLI; Corvus must never scrape or copy login tokens.
3. API keys should remain in the OS keyring and be represented in UI/API/logs only by opaque credential references.
4. Provider changes belong before the planned Google identity/E2B Cloud milestone.
5. Silent provider fallback is disabled unless the user explicitly enables an ordered fallback list because fallback can change cost, data egress, and behavior.

## Decisions Needed Before Code Is Written

1. **Provider order:** Should the default be local tools first, API providers first, or an explicit choice per workspace?
2. **Cursor meaning:** Should Cursor be a later `cursor-agent` runtime adapter, or only a handoff that opens the project in Cursor?
3. **Grok meaning:** Should Grok be implemented as the xAI HTTP API (recommended until a supported local CLI exists)?
4. **Unattended approvals:** Which effects may be pre-approved? Recommended default: repository-local reads/writes and tests only; external writes, destructive operations, credential access, policy changes, deployment, and spending above the run ceiling always block and queue.
5. **Provider milestone order:** Implement the local/API AI connection and bounded unattended runtime now, then Google/E2B; or preserve the existing Google/E2B milestone as next?

## Recommended UX Contract

- Replace the generic provider form with **AI connections**: `Detected on this computer` and `Use an API key`.
- Show Codex, Claude, and Gemini as detected but enable only adapters that pass Corvus capability checks; label the others Preview until implemented and verified.
- Offer three autonomy profiles: `Review first`, `Auto within limits`, and `Full auto while away`.
- Before an unattended run, show repositories/paths, tools, network, time, budget, approval ceiling, stop conditions, notifications, and the persistent kill switch.
- Return to a concise summary of work completed, blocked/queued actions, cost/time, changed files, tests, and audit links.
- Give Everyday users a goal/result-first flow with advanced controls collapsed; keep Developer logs, diffs, policies, and runtime detail visible.

## Stop Boundary

No provider-runtime or unattended-mode implementation should begin until the decisions above are confirmed. Testing and read-only architecture work may continue without crossing this boundary.
