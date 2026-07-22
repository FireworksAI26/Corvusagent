# Corvus feature behavior contract

## Product rule

Every feature must answer four questions without requiring the user to interpret backend state:

1. What can I do here?
2. What prerequisite is missing?
3. What happened after my action?
4. What is the safest next step if it failed or only partly completed?

Corvus never converts an unavailable capability, partial result, or unverified provider into a success state.

## Shared states

- Loading keeps the existing task context and disables only actions that depend on unfinished data.
- Initial-load failure exposes a retry for that data source.
- Action failure preserves entered values and keeps the action available for correction or retry.
- Partial success is a warning, not an error or full success.
- Success names the completed effect and the remaining human boundary.
- Disabled actions explain the missing prerequisite in adjacent text or a title.
- Machine codes are translated into concise user language. Typed correlation identifiers remain available in the error object for diagnostics.

## Repositories

- Local, empty-project, and GitHub paths communicate their runtime requirements before interaction.
- GitHub status is separate from local repository readiness.
- Refresh reports the real Git health returned by the backend.
- Removing a repository requires inline confirmation and removes only the Corvus registration. It never deletes files on disk.
- Healthy repositories can be handed directly to Runs.

## Skills

- Discovery reports how many candidates were found across supported tools.
- Preview remains mandatory before individual import.
- Bulk import continues safely when one candidate fails.
- Imported, review-required, failed, blocked, and duplicate outcomes are reported separately.
- Imports arrive as digest-pinned drafts. Activation remains a distinct user action and grants no authority.

## Runs

- Repository, run history, and skill prerequisites can be reloaded without repeating provider discovery.
- Provider discovery failure remains separate and retryable.
- Start failures preserve the complete run draft.
- Run-detail polling failure is shown as stale detail, not as a failed run.
- Retry creates a new isolated run. Stop requests provider cancellation. Discard removes only the managed worktree.
- Test and safety evidence is labelled unavailable when the backend did not capture it.

## Contributions

- The review reloads the actual worktree change set and existing prepared contribution.
- Preparation scans only checked paths, creates the local branch and commit, and returns an immutable confirmation digest.
- A prepared selection cannot be edited in place. A new run is required to change committed files.
- Publishing requires explicit confirmation and a completed passing secret scan.
- Publishing can create the requested pull request but never merges or force-pushes.

## Schedules

- Only healthy repositories, active skills, and verified provider settings are selectable.
- Creation failures preserve the schedule draft.
- Lifecycle changes announce pause, resume, or archive results.
- Run now creates a real run and moves to Runs.
- Scheduled output is limited to report-only or prepare-changes-for-review. It never pushes or opens a pull request.

## Settings and conversation

- Settings errors and success messages are cleared when the user changes category.
- Provider and safety failures remain separate and independently retryable.
- Chat and Build share one conversation history; changing mode does not discard context.
- Safety interruption preserves the response already received and offers an explicit Build or stop decision.

## Explicit non-capabilities

- No automatic merge or force-push.
- No scheduled publishing or scheduled pull-request creation.
- No mutation when provider or safety verification is unavailable.
- No claim that tests, scans, GitHub actions, or provider models succeeded without matching backend evidence.
