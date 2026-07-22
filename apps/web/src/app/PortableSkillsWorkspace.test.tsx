import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PortableSkill, SkillImportCandidate, SkillImportPreview } from "../api";
import { PortableSkillsWorkspace, type PortableSkillsApi } from "./PortableSkillsWorkspace";

const candidate = { id: "a".repeat(64), source: "claude", name: "review-pr", path: "C:\\Users\\me\\.claude\\skills\\review-pr", kind: "package" } as SkillImportCandidate;
const preview = { candidate, name: "review-pr", description: "Review a pull request", digest: "b".repeat(64), compatibility: "needs_review", findings: [{ code: "unapproved_tools", severity: "review", location: "SKILL.md", message: "Requested tools require approval." }], files: ["SKILL.md", "scripts/check.py"], duplicate: "none" } as SkillImportPreview;
const readyPreview = { ...preview, compatibility: "ready", findings: [] } as SkillImportPreview;
const imported = { id: "skill-1", tenant_id: "local", name: "review-pr", description: "Review a pull request", version: 1, digest: preview.digest, source: "claude", source_path: candidate.path, package_path: "C:\\library\\skill-1", status: "draft", findings: preview.findings, created_at: "2026-07-18T00:00:00Z" } as PortableSkill;

function api(): PortableSkillsApi {
  return {
    listPortableSkills: vi.fn().mockResolvedValue([]),
    listSkillImportSources: vi.fn().mockResolvedValue([candidate]),
    previewSkillImport: vi.fn().mockResolvedValue(preview),
    importPortableSkill: vi.fn().mockResolvedValue(imported),
    activatePortableSkill: vi.fn(),
    archivePortableSkill: vi.fn()
  };
}

describe("PortableSkillsWorkspace", () => {
  it("searches and filters discovered skills without changing the library", async () => {
    const second: SkillImportCandidate = { ...candidate, id: "c".repeat(64), source: "codex", name: "ship-release", path: "C:\\Users\\me\\.codex\\skills\\ship-release" };
    const client = api();
    vi.mocked(client.listSkillImportSources).mockResolvedValue([candidate, second]);
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await screen.findByRole("button", { name: /review-pr/i });
    await user.type(screen.getByRole("searchbox", { name: "Search discovered skills" }), "ship");
    expect(screen.queryByRole("button", { name: /review-pr/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ship-release/i })).toBeVisible();

    await user.clear(screen.getByRole("searchbox", { name: "Search discovered skills" }));
    await user.click(screen.getByRole("button", { name: "Claude Code 1" }));
    expect(screen.getByRole("button", { name: /review-pr/i })).toBeVisible();
    expect(screen.queryByRole("button", { name: /ship-release/i })).not.toBeInTheDocument();
  });

  it("supports selecting and importing multiple discovered skills", async () => {
    const second = { ...candidate, id: "c".repeat(64), name: "ship-release", path: "C:\\Users\\me\\.codex\\skills\\ship-release" };
    const client = api();
    vi.mocked(client.listSkillImportSources).mockResolvedValue([candidate, second]);
    vi.mocked(client.previewSkillImport).mockImplementation(async (candidateId) => ({
      ...readyPreview,
      candidate: candidateId === candidate.id ? candidate : second,
      name: candidateId === candidate.id ? candidate.name : second.name,
      digest: candidateId === candidate.id ? preview.digest : "d".repeat(64)
    }));
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await screen.findByRole("checkbox", { name: "Select review-pr" });
    await user.click(screen.getByRole("button", { name: "Select all" }));
    await user.click(screen.getByRole("button", { name: "Import selected (2)" }));

    await waitFor(() => expect(client.importPortableSkill).toHaveBeenCalledTimes(2));
    expect(client.importPortableSkill).toHaveBeenCalledWith(candidate.id, preview.digest);
    expect(client.importPortableSkill).toHaveBeenCalledWith(second.id, "d".repeat(64));
  });

  it("continues bulk imports when one selected skill fails", async () => {
    const second = { ...candidate, id: "c".repeat(64), name: "ship-release", path: "C:\\Users\\me\\.codex\\skills\\ship-release" };
    const client = api();
    vi.mocked(client.listSkillImportSources).mockResolvedValue([candidate, second]);
    vi.mocked(client.previewSkillImport).mockImplementation(async (candidateId) => ({
      ...readyPreview,
      candidate: candidateId === candidate.id ? candidate : second,
      name: candidateId === candidate.id ? candidate.name : second.name,
      digest: candidateId === candidate.id ? preview.digest : "d".repeat(64)
    }));
    vi.mocked(client.importPortableSkill).mockImplementation(async (candidateId) => {
      if (candidateId === candidate.id) throw new Error("first import failed");
      return { ...imported, id: "skill-2", name: second.name, source_path: second.path };
    });
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await screen.findByRole("checkbox", { name: "Select review-pr" });
    await user.click(screen.getByRole("button", { name: "Select all" }));
    await user.click(screen.getByRole("button", { name: "Import selected (2)" }));

    expect(await screen.findByText(/1 selected skill could not be imported/i)).toBeVisible();
    expect(screen.getAllByText("ship-release").length).toBeGreaterThan(0);
    expect(screen.getByRole("checkbox", { name: "Select review-pr" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select ship-release" })).not.toBeChecked();
  });

  it("keeps review-required skills selected for individual review", async () => {
    const client = api();
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await user.click(await screen.findByRole("checkbox", { name: "Select review-pr" }));
    await user.click(screen.getByRole("button", { name: "Import selected (1)" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/requires individual review/i);
    expect(client.importPortableSkill).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "Select review-pr" })).toBeChecked();
  });

  it("keeps safety-blocked skills selected and explains how to review them", async () => {
    const client = api();
    vi.mocked(client.previewSkillImport).mockResolvedValue({
      ...readyPreview,
      compatibility: "blocked",
      findings: [{
        code: "destructive_command",
        severity: "blocked",
        location: "SKILL.md",
        message: "Destructive recursive command detected."
      }]
    });
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await user.click(await screen.findByRole("checkbox", { name: "Select review-pr" }));
    await user.click(screen.getByRole("button", { name: "Import selected (1)" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      /blocked by the safety review.*open it to see what needs attention/i
    );
    expect(client.importPortableSkill).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "Select review-pr" })).toBeChecked();
  });

  it("hydrates an exact duplicate into the Library through the idempotent import", async () => {
    const client = api();
    vi.mocked(client.previewSkillImport).mockResolvedValue({
      ...readyPreview,
      duplicate: "exact"
    });
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} />);

    await user.click(await screen.findByRole("checkbox", { name: "Select review-pr" }));
    await user.click(screen.getByRole("button", { name: "Import selected (1)" }));

    await waitFor(() => expect(client.importPortableSkill).toHaveBeenCalledWith(candidate.id, preview.digest));
    expect(await screen.findByText("Review a pull request")).toBeVisible();
    expect(screen.queryByText(/already imported or blocked/i)).not.toBeInTheDocument();
  });

  it("discovers, reviews, and imports a cross-agent skill", async () => {
    const user = userEvent.setup();
    const client = api();
    render(<PortableSkillsWorkspace api={client} />);
    await user.click(await screen.findByRole("button", { name: /review-pr/i }));
    expect(await screen.findByRole("dialog", { name: "review-pr" })).toBeVisible();
    expect(screen.getByText("Requested tools require approval.")).toBeVisible();
    expect(screen.getByRole("button", { name: /technical package details/i })).toBeVisible();
    expect(screen.queryByText("scripts/check.py")).not.toBeInTheDocument();
    expect(screen.getByText("Imported permissions are never granted automatically.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Close skill review" })).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "Import as draft" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "review-pr" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /review-pr/i }));
    await user.click(screen.getByRole("button", { name: "Import as draft" }));
    await waitFor(() => expect(client.importPortableSkill).toHaveBeenCalledWith(candidate.id, preview.digest));
    expect(await screen.findByText("Review a pull request")).toBeVisible();
  });

  it("hands an active reviewed skill to Runs", async () => {
    const client = api();
    vi.mocked(client.listPortableSkills).mockResolvedValue([{ ...imported, status: "active" }]);
    const onOpenRuns = vi.fn();
    const user = userEvent.setup();
    render(<PortableSkillsWorkspace api={client} onOpenRuns={onOpenRuns} />);

    await user.click(await screen.findByRole("button", { name: "Use review-pr in Runs" }));
    expect(onOpenRuns).toHaveBeenCalledWith(imported.id);
  });
});
