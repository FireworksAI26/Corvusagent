export type FeatureArea =
  | "repository"
  | "github"
  | "skill"
  | "run"
  | "contribution"
  | "schedule"
  | "settings";

export interface FeatureFailure {
  code: string;
  message: string;
  correlationId: string | null;
}

const MESSAGES: Record<string, string> = {
  session_required: "The local Corvus session expired. Reopen the app, then try again.",
  repository_not_found: "This repository is no longer registered. Refresh the repository list.",
  repository_unhealthy: "Git could not verify this repository. Refresh it and resolve the reported Git state before continuing.",
  repository_path_invalid: "Corvus could not use that folder. Choose an existing Git repository and try again.",
  github_not_authenticated: "GitHub is not connected. Sign in before choosing or publishing a repository.",
  github_cli_unavailable: "GitHub support is unavailable on this device. Install and sign in to the GitHub CLI, then retry.",
  github_authentication_failed: "GitHub sign-in did not complete. Finish the browser flow, then try again.",
  github_clone_failed: "Corvus could not clone that GitHub project. Check access and the repository URL, then try again.",
  google_sign_in_popup_blocked: "Your browser blocked the Google sign-in window. Allow popups for Corvus, then try again.",
  external_url_invalid: "Corvus refused an invalid sign-in address.",
  external_url_forbidden: "Corvus refused a sign-in address outside its trusted allowlist.",
  browser_launcher_unavailable: "Corvus could not find the system browser launcher on this computer.",
  browser_launch_failed: "Corvus could not open your browser. Open Corvus Web and sign in there instead.",
  provider_unavailable: "The selected provider is unavailable. Retry discovery or choose a verified provider in Settings.",
  codex_unavailable: "Codex is unavailable. Check the CLI installation and login, then retry discovery.",
  safety_policy_unavailable: "Corvus could not verify the safety policy. Retry before starting work.",
  skill_digest_mismatch: "This skill changed after review. Review the latest version before importing it.",
  contribution_not_found: "No contribution has been prepared for this run yet.",
  contribution_confirmation_mismatch: "The prepared contribution changed. Refresh the review and confirm the latest version.",
  contribution_draft_required: "Corvus only publishes draft pull requests so human review remains required.",
  contribution_run_not_reviewable: "This run is not ready for contribution review. Wait for it to reach review, then refresh the changes.",
  contribution_run_not_publishable: "This contribution is not ready to publish. Prepare and confirm the latest reviewed draft first.",
  secret_scan_required: "A completed passing secret scan is required before publishing.",
  schedule_overlap: "This schedule overlaps an existing run window. Choose a different cadence or time.",
  schedule_run_already_active: "This schedule already has work in progress. Review, finish, or discard that run before starting another.",
  request_failed_409: "The saved state changed elsewhere. Refresh this view and review the latest values.",
  request_failed_503: "The local Corvus runtime is temporarily unavailable. Check that it is running, then retry."
};

function titleCaseCode(value: string): string {
  const sentence = value.replaceAll("_", " ").replace(/\s+/g, " ").trim();
  if (sentence === "") return "Corvus could not complete that action.";
  return `${sentence[0].toUpperCase()}${sentence.slice(1)}${/[.!?]$/.test(sentence) ? "" : "."}`;
}

function detailFrom(reason: unknown): Record<string, unknown> | null {
  if (typeof reason !== "object" || reason === null || !("detail" in reason)) return null;
  const detail = (reason as { detail?: unknown }).detail;
  return typeof detail === "object" && detail !== null ? detail as Record<string, unknown> : null;
}

export function featureFailure(reason: unknown, area: FeatureArea): FeatureFailure {
    const detail = detailFrom(reason);
  const rawMessage = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : "";
  const detailCode = typeof detail?.code === "string" ? detail.code : "";
  const code = detailCode || rawMessage || `${area}_request_failed`;
  const correlationId = typeof detail?.correlation_id === "string" ? detail.correlation_id : null;
  return {
    code,
    correlationId,
    message: MESSAGES[code] ?? titleCaseCode(code)
  };
}

export function featureErrorMessage(reason: unknown, area: FeatureArea): string {
  return featureFailure(reason, area).message;
}
