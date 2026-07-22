# Corvus Precision UI Design

## Intent

Corvus should feel like a focused native desktop tool: direct, compact, calm, and trustworthy. The interface serves active work. It does not advertise its own complexity.

## Information architecture

- Expose only routes backed by a dedicated truthful surface.
- Developer Individual: Repositories, Runs, Schedule, Skills, Threads, Settings.
- Everyday Individual: Conversations, Schedule, Settings.
- Team profiles remain a clearly labelled preview and expose only Conversations, Schedule, and Settings until dedicated shared functionality exists.
- Settings replaces the application rail and always begins with Back to app.
- The shell never scrolls. Only the active content pane, transcript, list, detail pane, or dialog body may scroll.

## Visual system

- Use flat solid surfaces only. No gradients, blur, glow, decorative shadow, or glass treatment.
- Central task surface is the darkest layer; navigation and control surfaces are one neutral step lighter.
- Cyan is reserved for the primary action, current selection, focus, and verified information.
- Use Inter Tight for all product UI and IBM Plex Mono only for paths, hashes, identifiers, and machine state.
- Use fixed compact type sizes. Do not use editorial display typography in product surfaces.
- Use 2px, 4px, and 6px radii. Avoid pills except for conventional icon buttons or binary status when text is required.
- Prefer rules, rows, and split panes over cards.
- Route changes are instant. Menus, dialogs, and state feedback may transition for 80 to 140 milliseconds. Reduced motion removes all transitions.

## Shared interaction vocabulary

- Buttons: primary, secondary, quiet, danger, and icon-only.
- Fields: one label, optional concise description, control, and inline validation.
- Selection: native select where appropriate or a fully keyboard-operable listbox.
- Status: square marker plus label; never color alone.
- Notices: inline and contextual, with one recovery action.
- Empty states: one sentence and one available next action.
- Lists: selectable rows with stable columns and a detail pane where useful.
- Dialogs: reserved for confirmation, credential entry, and irreversible or authority-changing actions.

## OpenAI desktop reference adaptation

Official ChatGPT and Codex desktop material establishes a useful hierarchy: a narrow stable sidebar, project-scoped recent work, one dominant conversation or task surface, a persistent composer, compact activity milestones, and review inside the task. Corvus adopts that hierarchy without copying OpenAI branding, iconography, rounded consumer styling, or unsupported cloud and multi-agent claims.

- Sidebar: destination first, project context second, recent conversations inside Conversations rather than a second global taxonomy.
- Task: conversation, runtime activity, evidence, diff review, and contribution form one continuous journey.
- Composer: Project, Chat or Build, safety state, and Send remain primary. Provider, model, thinking, skill, and MCP remain progressive controls.
- Activity: show one current status and one expandable activity trail. Keep the assistant response visually dominant.
- Feature surfaces: use list to detail to action flows rather than dashboard card grids.
- Corvus distinction: authority changes, verification failures, secret screening, contribution boundaries, and human review remain explicit even when normal-state UI is quiet.

## Surface behavior

### Conversations

Keep Project, Chat or Build, and Send visible. Move provider, model, thinking, and MCP into Run options. Show safety in the toolbar only when unavailable, elevated, or confirmation is required. Tool activity and receipts stay in the transcript without displacing model output.

### Repositories

Use one Add repository action with GitHub, local folder, and empty project choices. Render connected repositories as compact rows containing name, path or remote, branch, sync state, health, and contextual actions.

### Runs and contribution

Remove demo-instruction scaffolding from the product UI. Show blockers only when they prevent starting a run. A new run begins with repository and task, inherits defaults, and reveals model, thinking, skill, mode, and output policy under Run options. History and detail use a split view. Contribution review stays inline in the run detail.

### Skills

Replace source cards with search and compact source filters. Use a scalable discovered-skills list with bulk selection and a persistent preview or focused review surface. Keep technical package details collapsed by default.

### Schedule

Creation begins with name, task, repository, cadence, and time. Model, thinking, skill, and mode inherit defaults and live under Advanced options. Schedules render as rows, not cards.

### Settings

Keep the dedicated settings sidebar. Split Models into Defaults and Providers. Provider verification and credential management are contextual to the selected provider. Use unambiguous dirty-state actions: Save, Keep editing, and Discard.

### Recovery and onboarding

Keep onboarding concise and linear. Pairing, provider, safety, sync, and runtime failures state what failed, what remains safe, and the single next action.

## Accessibility and resilience

- WCAG 2.1 AA contrast and visible focus.
- Complete keyboard navigation and focus return for menus and dialogs.
- Disabled controls include a visible or accessible reason.
- Long paths, branches, model names, and tasks never create horizontal page overflow.
- Validate at 1440x900, 1280x720, 900x700, and 390x844.
- Large lists remain usable with 200 skills, 50 repositories, and 100 runs.
- Truthful provider, safety, scan, contribution, and schedule gates must not regress.
