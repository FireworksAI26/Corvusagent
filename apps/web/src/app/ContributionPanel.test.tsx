import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ChangeSet, Contribution } from "../api";
import { ContributionPanel, type ContributionApi } from "./ContributionPanel";

const changes: ChangeSet = {
  captured_at: "2026-07-18T00:00:00Z",
  digest: "a".repeat(64),
  files: [
    {
      path: "src/feature.ts",
      status: "modified",
      binary: false,
      patch: "@@ -1 +1 @@\n-old\n+new\n",
      patch_truncated: false
    },
    {
      path: "notes.txt",
      status: "untracked",
      binary: false,
      patch: "+notes\n",
      patch_truncated: false
    }
  ]
};

const prepared: Contribution = {
  id: "contribution-1",
  run_id: "run-1",
  repository_id: "repository-1",
  branch: "corvus/run-1-add-feature",
  base_branch: "main",
  selected_paths: ["src/feature.ts", "notes.txt"],
  confirmation_digest: "b".repeat(64),
  message: "Add feature",
  title: "Add feature",
  body: "Reviewed by Corvus.",
  draft: true,
  change_digest: "a".repeat(64),
  secret_scan: {
    status: "passed",
    scanner_version: "corvus-secrets-v1",
    scanned_paths: ["src/feature.ts", "notes.txt"],
    findings: [],
    completed_at: "2026-07-18T00:00:01Z",
    digest: "c".repeat(64)
  },
  commit_sha: "d".repeat(40),
  remote_ref: null,
  pr_number: null,
  pr_url: null,
  state: "committed",
  last_error: null,
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:01Z"
};

function api(): ContributionApi {
  return {
    getRunChanges: vi.fn().mockResolvedValue(changes),
    getContribution: vi.fn().mockRejectedValue(new Error("contribution_not_found")),
    prepareContribution: vi.fn().mockResolvedValue(prepared),
    publishContribution: vi.fn().mockResolvedValue({
      ...prepared,
      state: "published",
      pr_number: 17,
      pr_url: "https://github.com/team/corvus/pull/17"
    })
  };
}

describe("ContributionPanel", () => {
  it("reviews selected files and requires explicit publish confirmation", async () => {
    const contributionApi = api();
    render(<ContributionPanel api={contributionApi} runId="run-1" />);
    const user = userEvent.setup();

    expect(await screen.findByText("src/feature.ts")).toBeVisible();
    expect(screen.getByLabelText("Include src/feature.ts")).toBeChecked();
    await user.type(screen.getByLabelText("Commit message"), "Add feature");
    await user.type(screen.getByLabelText("Pull request title"), "Add feature");
    await user.type(screen.getByLabelText("Pull request body"), "Reviewed by Corvus.");
    await user.click(screen.getByRole("button", { name: "Prepare contribution" }));

    expect(contributionApi.prepareContribution).toHaveBeenCalledWith("run-1", {
      selectedPaths: ["src/feature.ts", "notes.txt"],
      message: "Add feature",
      title: "Add feature",
      body: "Reviewed by Corvus.",
      draft: true
    });
    expect(await screen.findByText("Secret scan passed")).toBeVisible();
    expect(screen.getByText("2 paths scanned")).toBeVisible();
    expect(screen.getByText("Draft pull request preview")).toBeVisible();
    expect(screen.getByText(/corvus\/run-1-add-feature → main/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Publish draft pull request" })).toBeDisabled();
    await user.click(screen.getByLabelText(/I reviewed the selected files/));
    await user.click(screen.getByRole("button", { name: "Publish draft pull request" }));

    expect(contributionApi.publishContribution).toHaveBeenCalledWith(
      "run-1",
      prepared.confirmation_digest
    );
    expect(await screen.findByRole("link", { name: "Open pull request #17" })).toHaveAttribute(
      "href",
      "https://github.com/team/corvus/pull/17"
    );
  });

  it("never claims success when preparation fails", async () => {
    const contributionApi = api();
    vi.mocked(contributionApi.prepareContribution).mockRejectedValue(
      new Error("contribution_secret_scan_blocked")
    );
    render(<ContributionPanel api={contributionApi} runId="run-1" />);
    const user = userEvent.setup();

    await screen.findByText("src/feature.ts");
    await user.type(screen.getByLabelText("Commit message"), "Unsafe");
    await user.type(screen.getByLabelText("Pull request title"), "Unsafe");
    await user.type(screen.getByLabelText("Pull request body"), "Unsafe");
    await user.click(screen.getByRole("button", { name: "Prepare contribution" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/contribution secret scan blocked/i);
    expect(screen.queryByText("Secret scan passed")).not.toBeInTheDocument();
  });

  it("restores an existing prepared contribution after navigation", async () => {
    const contributionApi = api();
    vi.mocked(contributionApi.getContribution).mockResolvedValue(prepared);

    render(<ContributionPanel api={contributionApi} runId="run-1" />);

    expect(await screen.findByText("Secret scan passed")).toBeVisible();
    expect(screen.queryByLabelText("Commit message")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish draft pull request" })).toBeDisabled();
  });

  it("keeps publishing gated when a restored scan is not passing", async () => {
    const contributionApi = api();
    vi.mocked(contributionApi.getContribution).mockResolvedValue({
      ...prepared,
      secret_scan: { ...prepared.secret_scan, status: "warning" }
    });
    const user = userEvent.setup();
    render(<ContributionPanel api={contributionApi} runId="run-1" />);

    await user.click(await screen.findByLabelText(/I reviewed the selected files/));
    const publish = screen.getByRole("button", { name: "Publish draft pull request" });
    expect(publish).toBeDisabled();
    expect(publish).toHaveAttribute("title", expect.stringMatching(/passing secret scan/i));
  });

  it("retries a failed review load and preserves truthful empty state", async () => {
    const contributionApi = api();
    vi.mocked(contributionApi.getRunChanges)
      .mockRejectedValueOnce(new Error("request_failed_503"))
      .mockResolvedValueOnce({ ...changes, files: [] });
    const user = userEvent.setup();
    render(<ContributionPanel api={contributionApi} runId="run-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime is temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: "Retry review" }));
    expect(await screen.findByText("No changes yet")).toBeVisible();
    expect(contributionApi.getRunChanges).toHaveBeenCalledTimes(2);
  });
});
