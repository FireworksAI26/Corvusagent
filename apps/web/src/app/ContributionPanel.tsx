import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import type { ChangeSet, Contribution } from "../api";
import { featureErrorMessage } from "./featureFeedback";

export interface ContributionApi {
  getRunChanges(runId: string): Promise<ChangeSet>;
  getContribution(runId: string): Promise<Contribution>;
  prepareContribution(
    runId: string,
    input: {
      selectedPaths: string[];
      message: string;
      title: string;
      body: string;
      draft: true;
    }
  ): Promise<Contribution>;
  publishContribution(runId: string, expectedDigest: string): Promise<Contribution>;
}

export function ContributionPanel({ api, runId }: { api: ContributionApi; runId: string }) {
  const [changes, setChanges] = useState<ChangeSet | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const draft = true;
  const [prepared, setPrepared] = useState<Contribution | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadFailed(false);
    setChanges(null);
    setPrepared(null);
    setConfirmed(false);
    setError("");
    try {
      const [loaded, existing] = await Promise.all([
        api.getRunChanges(runId),
        api.getContribution(runId).catch((reason: unknown) => {
          const message = reason instanceof Error ? reason.message : "";
          if (message.includes("contribution_not_found")) return null;
          throw reason;
        })
      ]);
      setChanges(loaded);
      setSelected(new Set(loaded.files.map((file) => file.path)));
      setPrepared(existing);
    } catch (reason) {
      setLoadFailed(true);
      setError(featureErrorMessage(reason, "contribution"));
    } finally {
      setLoading(false);
    }
  }, [api, runId]);

  useEffect(() => { void load(); }, [load]);

  const selectedPaths = useMemo(
    () => changes?.files.map((file) => file.path).filter((path) => selected.has(path)) ?? [],
    [changes, selected]
  );

  function toggle(path: string): void {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  async function prepare(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (selectedPaths.length === 0) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.prepareContribution(runId, {
        selectedPaths,
        message: message.trim(),
        title: title.trim(),
        body: body.trim(),
        draft
      });
      setPrepared(result);
      setConfirmed(false);
      setNotice("Contribution prepared locally. Review the completed scan, branch, commit, and draft pull request before publishing.");
    } catch (reason) {
      setError(featureErrorMessage(reason, "contribution"));
    } finally {
      setBusy(false);
    }
  }

  async function publish(): Promise<void> {
    if (prepared === null || !confirmed) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setPrepared(await api.publishContribution(runId, prepared.confirmation_digest));
      setNotice("Draft pull request published. A human review and merge decision are still required.");
    } catch (reason) {
      setError(featureErrorMessage(reason, "contribution"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="contribution-title" className="contribution-panel">
      <header>
        <div>
          <p className="eyebrow">Isolated run {runId.slice(0, 8)}</p>
          <h2 id="contribution-title">Review contribution</h2>
          <p>Review the real worktree diff. Preparing performs a completed secret scan, creates a local branch, and commits only checked files. Publishing never force-pushes or merges.</p>
        </div>
      </header>
      <div className="contribution-toolbar"><button className="button button--quiet" disabled={busy || loading} onClick={() => void load()} type="button">Refresh review</button></div>
      {notice ? <p className="inline-success" role="status">{notice}</p> : null}
      {error ? <p className="inline-error" role="alert">{error} {loadFailed ? <button className="text-button" disabled={loading} onClick={() => void load()} type="button">Retry review</button> : null}</p> : null}
      {loading ? <p className="quiet-copy">Loading worktree changes…</p> : null}
      {changes?.files.length === 0 ? (
        <div className="resource-empty">
          <strong>No changes yet</strong>
          <span>Run a build task in this worktree, then refresh the review.</span>
        </div>
      ) : null}

      {changes && changes.files.length > 0 && prepared === null ? (
        <form onSubmit={(event) => void prepare(event)}>
          <div aria-label="Changed files" className="contribution-files">
            {changes.files.map((file) => (
              <article key={file.path}>
                <label>
                  <input
                    aria-label={`Include ${file.path}`}
                    checked={selected.has(file.path)}
                    onChange={() => toggle(file.path)}
                    type="checkbox"
                  />
                  <span><strong>{file.path}</strong><small>{file.status}{file.binary ? " · binary" : ""}</small></span>
                </label>
                {file.patch ? <details><summary>View patch</summary><pre>{file.patch}</pre></details> : null}
                {file.patch_truncated ? <small>Patch preview truncated</small> : null}
              </article>
            ))}
          </div>
          <div className="contribution-form">
            <label>Commit message<input onChange={(event) => setMessage(event.target.value)} value={message} /></label>
            <label>Pull request title<input onChange={(event) => setTitle(event.target.value)} value={title} /></label>
            <label className="contribution-form__body">Pull request body<textarea onChange={(event) => setBody(event.target.value)} rows={4} value={body} /></label>
            <p className="contribution-draft"><strong>Draft pull request</strong><span>Corvus always publishes contributions as drafts so a human review remains required.</span></p>
          </div>
          <button
            className="button button--primary"
            disabled={busy || selectedPaths.length === 0 || !message.trim() || !title.trim() || !body.trim()}
            type="submit"
          >
            {busy ? "Preparing…" : "Prepare contribution"}
          </button>
        </form>
      ) : null}

      {prepared ? (
        <div className="contribution-confirmation">
          <div className="contribution-evidence">
            <span data-status={prepared.secret_scan.status}>Secret scan {prepared.secret_scan.status}</span>
            <span>{prepared.secret_scan.scanned_paths.length} paths scanned</span>
            <span title={prepared.secret_scan.digest ?? "No completed digest"}>Scan digest {prepared.secret_scan.digest?.slice(0, 10) ?? "unavailable"}</span>
            <span>Branch {prepared.branch}</span>
            <span>Commit {prepared.commit_sha?.slice(0, 8) ?? "pending"}</span>
            <span>{prepared.selected_paths.length} selected files</span>
          </div>
          <div className="contribution-pr-preview">
            <strong>{prepared.draft ? "Draft pull request preview" : "Pull request preview"}</strong>
            <span>{prepared.title}</span>
            <small>{prepared.branch} → {prepared.base_branch}</small>
            <p>{prepared.body}</p>
          </div>
          <p className="quiet-copy">This prepared selection is locked to its confirmation digest. Start a new run if you need to change the committed files.</p>
          {prepared.secret_scan.findings.length > 0 ? <div className="contribution-findings"><strong>Secret scan findings</strong>{prepared.secret_scan.findings.map((finding, index) => <span data-severity={finding.severity} key={`${finding.path}-${finding.line ?? "file"}-${index}`}>{finding.path}{finding.line ? `:${finding.line}` : ""} · {finding.kind}</span>)}</div> : null}
          {prepared.state === "published" && prepared.pr_url && prepared.pr_number ? (
            <a href={prepared.pr_url} rel="noreferrer" target="_blank">Open pull request #{prepared.pr_number}</a>
          ) : (
            <>
              <label className="contribution-confirm-check">
                <input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" />
                I reviewed the selected files, completed secret scan, branch, commit, and pull-request preview. A human still decides whether to merge.
              </label>
              <button
                className="button button--primary"
                disabled={!confirmed || busy || prepared.secret_scan.status !== "passed"}
                onClick={() => void publish()}
                type="button"
                title={prepared.secret_scan.status !== "passed" ? "A completed passing secret scan is required before publishing." : undefined}
              >
                {busy ? "Publishing…" : `Publish ${prepared.draft ? "draft " : ""}pull request`}
              </button>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}
