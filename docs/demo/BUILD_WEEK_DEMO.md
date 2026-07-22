# OpenAI Build Week demo

## One-line story

Corvus turns a locally authenticated Codex session into a reviewable, repeatable GitHub contribution without giving an agent merge or force-push authority.

## Prepared state

Use a small GitHub-backed repository with a clean default branch and a task that changes one source file plus one test. Before recording:

1. Sign in to Codex CLI and GitHub CLI on the demo computer.
2. Register the repository in Corvus and refresh it until Git and GitHub show healthy.
3. Put a real, harmless review skill in `~/.claude/skills/review-ready-change/SKILL.md` or `~/.codex/skills/review-ready-change/SKILL.md`.
4. Import and activate that skill in Corvus after reviewing its normalized files, requested capabilities, scan findings, and digest.
5. Complete one rehearsal run so the model and repository caches are warm, then discard its Corvus-owned worktree.
6. Leave the repository checkout clean. Do not pre-create the recording branch or pull request.

Recommended task:

> Add a `normalize_title` helper, cover whitespace and repeated-dash cases with tests, run the focused test suite, and update the README example. Do not publish anything.

Recommended pull-request title:

> Add tested title normalization helper

Recommended schedule:

- Weekdays at 09:00 in the computer's IANA timezone
- Output policy: **Prepare changes**
- Missed-run grace: 30 minutes
- Native notification: enabled

Keep the real repository URL, run ID, commit SHA, pull-request URL, and Codex feedback session ID in the recording notes, not in source control.

## Recording script: 2 minutes 50 seconds

| Time | Screen action | Judge takeaway |
| --- | --- | --- |
| 0:00-0:12 | Launch the installed Corvus app. Open **Settings → Models** and show Codex, the selected model, and Low/Medium/High/XHigh choices. | Corvus uses the developer's existing authenticated Codex CLI. |
| 0:12-0:30 | Return with **Back to app**. Open **Repositories**, select the prepared repository, and refresh Git and GitHub status. | The repository and GitHub data are real; the original checkout is clean. |
| 0:30-0:50 | Open **Skills**, discover local sources, select `review-ready-change`, and show its source, normalized files, scan result, requested capabilities, and immutable digest. | Imports are inspectable and grant no authority. |
| 0:50-1:13 | Open **Runs**, select the repository and skill, paste the task, choose Codex/model/thinking, and start **Prepare changes**. Show the pinned base SHA and managed worktree while events arrive. | Codex works in an isolated Git worktree with pinned inputs. |
| 1:13-1:42 | When the run reaches review, open **Changes** and **Evidence**. Show the file diff, additions/deletions, test command and exit state, safety receipt, and completed secret scan. | The result is evidence-backed and reviewable before GitHub mutation. |
| 1:42-2:16 | Open **Contribution**, select files, review the branch/commit/PR preview, confirm, and create a draft pull request. Open its URL and show checks without merging. | Push and PR creation require explicit human confirmation; merge remains unavailable. |
| 2:16-2:40 | Return to Corvus, open **Schedule**, create the weekday schedule from the same repository/task/skill, and show the next occurrence plus **Prepare changes** policy. | The workflow is repeatable, but scheduled code changes still stop for review. |
| 2:40-2:50 | Close the window, surface Corvus from the tray, and show the final frame: pull-request URL, schedule next run, and “human review required.” | Corvus is a practical local control plane, not an autonomous merge bot. |

If the real run takes longer than the recording window, keep recording continuously and accelerate only the waiting section in editing. Do not substitute fake events or a fabricated completed run.

## Visual acceptance checklist

Capture the same real build at 1440×1000, 1024×900, and 390×844.

- The document does not scroll or rubber-band; only the designated center pane scrolls.
- The center workspace is darker than the app and settings sidebars.
- Settings fully replaces app navigation and begins with **← Back to app**.
- Repository, run, skill, and schedule empty/error/loading states explain recovery.
- Long repository names, model labels, branches, and file paths wrap or truncate without horizontal page overflow.
- Composer, review confirmation, import review, and schedule controls remain keyboard reachable.
- Focus is visible, dialogs retain focus, Escape closes non-destructive dialogs, and reduced motion removes decorative transitions.
- Disabled actions explain the missing prerequisite; no placeholder action reports success.

## Failure-safe recording fallbacks

- **Codex unavailable:** show the specific discovery error and retry; do not switch providers silently.
- **GitHub unavailable:** finish at a local contribution preview and state that push is paused; do not claim a PR exists.
- **Secret scan not complete:** keep Publish disabled and show the evidence requirement.
- **Schedule dependency stale:** show **Needs attention** and the exact model/skill/repository dependency that must be restored.
- **Unsigned Windows warning:** disclose that the hackathon installer is an unsigned alpha build.

