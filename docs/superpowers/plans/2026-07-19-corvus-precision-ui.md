# Corvus Precision UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Corvus's mixed UI generations with one minimal, sharp, truthful desktop product system across every shipped route.

**Architecture:** Keep existing backend contracts and feature ownership. Consolidate presentation through shared CSS tokens and primitives, simplify each workspace through progressive disclosure, and remove navigation paths that do not map to dedicated functionality. Preserve all safety and authority gates.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, CSS, Vite, Tauri desktop shell.

## Global Constraints

- No gradients, backdrop blur, glow, glass treatment, decorative shadow, or layout animation.
- Use flat solid surfaces and 3px, 5px, or 7px radii.
- The shell does not scroll; only intentional content regions scroll.
- Keep provider, safety, secret-scan, contribution, GitHub, and schedule claims truthful.
- Preserve keyboard access, focus visibility, focus containment, reduced motion, and responsive layouts.
- Do not modify backend authority or execution behavior for presentation convenience.
- Preserve unrelated user-owned working-tree changes.

---

### Task 1: Truthful navigation and shell

**Files:**
- Modify: `apps/web/src/app/workspaceProfiles.ts`
- Modify: `apps/web/src/app/WorkspaceRouter.tsx`
- Modify: `apps/web/src/App.tsx`
- Test: `apps/web/src/app/workspaceProfiles.test.ts`
- Test: `apps/web/src/app/WorkspaceRouter.test.tsx`

**Interfaces:** Existing `WorkspaceProfile.routes`, `getWorkspaceDefaultRoute`, and local route rendering remain the public interface.

- [ ] Add failing tests proving unfinished routes are absent and every exposed route maps to a dedicated surface.
- [ ] Run the focused tests and verify failures are caused by currently exposed placeholder routes.
- [ ] Remove misleading route entries and eliminate unrelated fallback routing for exposed routes.
- [ ] Run the focused tests and verify they pass.

### Task 2: Shared precision visual system

**Files:**
- Modify: `apps/web/src/styles.css`
- Modify: `apps/web/src/styles/adaptive-shell.css`
- Modify: `apps/web/src/styles/onboarding.css`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `apps/web/src/App.workspace.test.tsx`
- Test: `apps/web/src/app/AppShell.test.tsx`

**Interfaces:** Existing semantic class names remain stable; global tokens define surfaces, borders, radii, typography, focus, and status.

- [ ] Add structural assertions for fixed-shell scroll ownership, settings replacement navigation, and direct surface classes.
- [ ] Verify the assertions fail where the legacy system still leaks through.
- [ ] Replace gradients, blur, decorative shadows, oversized radii, pills, editorial product headings, and card-grid defaults with flat tokens, rules, rows, and split panes.
- [ ] Normalize buttons, fields, focus, disabled states, notices, status markers, scrollbars, and reduced-motion behavior.
- [ ] Run shell and workspace tests.

### Task 3: Core conversation and run flow

**Files:**
- Modify: `apps/web/src/app/ConversationWorkspace.tsx`
- Modify: `apps/web/src/app/RunsWorkspace.tsx`
- Modify: `apps/web/src/app/ContributionPanel.tsx`
- Test: `apps/web/src/app/ConversationWorkspace.test.tsx`
- Test: `apps/web/src/app/RunsWorkspace.test.tsx`
- Test: `apps/web/src/app/ContributionPanel.test.tsx`

**Interfaces:** Existing APIs and request payloads stay unchanged. UI state may introduce `optionsOpen` and progressive disclosure only.

- [ ] Add failing tests proving the composer exposes Project, Mode, Send, and one Run options control while advanced controls remain reachable by keyboard.
- [ ] Add failing tests proving Runs removes demo scaffolding, shows contextual blockers, and hides advanced creation controls initially.
- [ ] Implement compact conversation options, exceptional safety visibility, simplified run creation, and split history/detail presentation.
- [ ] Keep contribution inline and reduce its visible steps without weakening scan and explicit-confirmation gates.
- [ ] Run all three focused suites.

### Task 4: Repository, skill, and schedule workspaces

**Files:**
- Modify: `apps/web/src/app/RepositoriesWorkspace.tsx`
- Modify: `apps/web/src/app/PortableSkillsWorkspace.tsx`
- Modify: `apps/web/src/app/SchedulesWorkspace.tsx`
- Test: `apps/web/src/app/RepositoriesWorkspace.test.tsx`
- Test: `apps/web/src/app/PortableSkillsWorkspace.test.tsx`
- Test: `apps/web/src/app/SchedulesWorkspace.test.tsx`

**Interfaces:** Existing API calls and safety gates stay unchanged.

- [ ] Add failing tests for one repository add flow, skill search and filters, and collapsed schedule advanced options.
- [ ] Verify the focused failures.
- [ ] Convert repositories to dense rows with an inline add chooser.
- [ ] Convert skills to searchable filtered list/detail presentation while preserving bulk import and review.
- [ ] Convert schedules to rows and move inherited provider/model/skill settings under Advanced options.
- [ ] Run the focused suites.

### Task 5: Settings simplification

**Files:**
- Modify: `apps/web/src/app/SettingsPanel.tsx`
- Test: `apps/web/src/app/SettingsPanel.test.tsx`

**Interfaces:** Existing preferences, provider, credential, MCP, and desktop bridge APIs remain unchanged.

- [ ] Add failing tests for Models Defaults and Providers sections plus unambiguous unsaved-change actions.
- [ ] Verify the focused failures.
- [ ] Split the Models information hierarchy, make provider details contextual, and rename ambiguous dirty-state controls.
- [ ] Verify save, conflict, exit-confirmation, provider verification, and MCP tests remain green.

### Task 6: Whole-app hardening and verification

**Files:**
- Modify only files needed to resolve discovered regressions.
- Test: all `apps/web/src/**/*.test.tsx` and `apps/web/src/**/*.test.ts`.

**Interfaces:** No new product capability is introduced in this task.

- [ ] Add long-content and compact-viewport fixtures where current tests lack them.
- [ ] Verify no body or horizontal overflow and that intended panes own scrolling.
- [ ] Verify keyboard navigation, Escape, focus return, disabled explanations, light/dark themes, and reduced motion.
- [ ] Run `pnpm test` and `pnpm build` in `apps/web`.
- [ ] Run relevant Python contract tests if generated API or backend contracts changed.
- [ ] Run `git diff --check` and report remaining environment limitations exactly.
