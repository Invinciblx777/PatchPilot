import { useEffect, useState } from "preact/hooks";
import { api } from "../api/client.ts";
import type { PatchResult, Run, RunSummary } from "../api/types.ts";
import { EmptyState, Notice } from "../components/common.tsx";
import { Button } from "../components/controls.tsx";
import { DiffView } from "../components/DiffView.tsx";
import { type MarkState, StatusMark } from "../components/StatusMark.tsx";
import { TestResults } from "../components/TestResults.tsx";
import { BranchSketch } from "../ink/Drawings.tsx";
import { IconRefresh } from "../ink/Icons.tsx";
import { InkBox } from "../ink/Ink.tsx";
import layout from "../layout/layout.module.css";
import { clock, plural, shortSha } from "../lib/format.ts";
import { loadRepos, useAppState } from "../state/store.ts";
import styles from "./pages.module.css";

const OUTCOME_LABEL: Record<string, string> = {
  check_passed: "Check passed",
  tests_passed: "Tests passed",
  patch_applied: "Applied, no tests",
  tests_failed: "Tests failed",
  tests_incomplete: "Tests incomplete",
  uncommitted_changes: "Refused: local changes",
  invalid_repository: "Invalid repository",
  invalid_patch: "Invalid patch",
  unsafe_patch: "Rejected: unsafe path",
  patch_check_failed: "Does not apply",
  workspace_error: "Workspace error",
  internal_error: "PatchPilot error",
  interrupted: "Interrupted",
};

const OUTCOME_MARK: Record<string, MarkState> = {
  check_passed: "checked",
  tests_passed: "passed",
  patch_applied: "checked",
  tests_failed: "failed",
  tests_incomplete: "incomplete",
  uncommitted_changes: "warning",
};

function outcomeMark(run: RunSummary): MarkState {
  if (run.state === "running") return "running";
  return OUTCOME_MARK[run.outcome ?? ""] ?? "error";
}

export function Workspaces() {
  const repos = useAppState((s) => s.repos);
  const config = useAppState((s) => s.config);
  const finished = useAppState((s) => s.finishedRuns);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    void api.get<{ runs: RunSummary[] }>("/api/runs").then((result) => {
      if (result.ok) setRuns(result.data.runs.filter((r) => r.kind === "patch_check" || r.kind === "patch_apply"));
    });
  }, [finished]);

  const refresh = async () => {
    setRefreshing(true);
    await loadRepos();
    setRefreshing(false);
  };

  return (
    <div class={layout.page}>
      <header class={styles.pageHead}>
        <h1>Workspaces</h1>
        <p class="lede">
          The repositories you allowed when starting <code>patchpilot-ui</code>, and the disposable worktrees PatchPilot created in this
          session. Patches are applied only inside those worktrees.
        </p>
      </header>

      <section class={styles.section} aria-labelledby="repos-title">
        <div class={styles.sectionHead}>
          <h2 id="repos-title">Repositories</h2>
          <Button variant="quiet" icon={<IconRefresh size={16} />} busy={refreshing} onClick={() => void refresh()}>
            Read again
          </Button>
        </div>
        {config && config.repos.length === 0 && (
          <Notice tone="unavailable" title="No repository is configured">
            <p>
              Restart <code>patchpilot-ui</code> with <code>--repo PATH</code> for each repository PatchPilot may patch.
            </p>
          </Notice>
        )}
        {repos && repos.length > 0 && (
          <div class={styles.tableWrap}>
            <table class={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Branch</th>
                  <th scope="col">HEAD</th>
                  <th scope="col">Working tree</th>
                </tr>
              </thead>
              <tbody>
                {repos.map((repo) => (
                  <tr key={repo.id}>
                    <td>
                      <strong>{repo.name}</strong>
                      <p class={styles.toolDesc}>
                        <code>{repo.path}</code>
                      </p>
                    </td>
                    {repo.ok ? (
                      <>
                        <td>{repo.branch ? <code>{repo.branch}</code> : <span class="muted">Detached</span>}</td>
                        <td>{repo.head ? <code title={repo.head}>{shortSha(repo.head)}</code> : <span class="muted">No commits</span>}</td>
                        <td>
                          {repo.dirty ? (
                            <StatusMark state="warning" label={plural(repo.changedPaths, "local change")} />
                          ) : (
                            <StatusMark state="checked" label="Clean" quiet />
                          )}
                        </td>
                      </>
                    ) : (
                      <td colSpan={3}>
                        <StatusMark state="error" label={repo.error} />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p class="muted">Read with git rev-parse, status, and symbolic-ref only. Nothing here changes a repository.</p>
      </section>

      <section class={styles.section} aria-labelledby="runs-title">
        <div class={styles.sectionHead}>
          <h2 id="runs-title">Patch runs in this session</h2>
        </div>
        {runs && runs.length === 0 && (
          <EmptyState art={<BranchSketch size={140} />} title="No worktree yet">
            Check a patch on the Overview page. Each check or apply gets its own throwaway worktree, removed when it finishes.
          </EmptyState>
        )}
        {runs && runs.length > 0 && (
          <ul class={styles.runList}>
            {runs.map((run) => (
              <li key={run.id}>
                <button
                  type="button"
                  class={styles.runRow}
                  aria-expanded={open === run.id ? "true" : "false"}
                  onClick={() => setOpen(open === run.id ? null : run.id)}
                >
                  <time dateTime={run.startedAt}>{clock(run.startedAt)}</time>
                  <span class={styles.runTitle}>{run.title}</span>
                  <StatusMark
                    state={outcomeMark(run)}
                    label={run.state === "running" ? "Running" : (OUTCOME_LABEL[run.outcome ?? ""] ?? run.outcome ?? "Unknown")}
                  />
                </button>
                {open === run.id && <RunDetail id={run.id} />}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function RunDetail({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    void api.get<Run>(`/api/runs/${encodeURIComponent(id)}`).then((result) => {
      if (result.ok) setRun(result.data);
      else setMissing(true);
    });
  }, [id]);
  if (missing) return <p class="muted">This run is no longer held by the server.</p>;
  if (!run) return <p class="muted">Reading the run…</p>;
  const result = run.result as unknown as PatchResult | null;
  if (!result) return <p class="muted">The run has not finished yet.</p>;
  return (
    <InkBox seed={`run-${id}`} class={styles.runDetail}>
      <dl class={styles.detailGrid}>
        <div>
          <dt>Base</dt>
          <dd>
            <code>{result.baseRevision}</code> {result.baseSha && <code class="muted">{shortSha(result.baseSha)}</code>}
          </dd>
        </div>
        <div>
          <dt>Worktree branch</dt>
          <dd>{result.workspaceBranch ? <code>{result.workspaceBranch}</code> : <span class="muted">Not created</span>}</dd>
        </div>
        <div>
          <dt>Cleanup</dt>
          <dd>{result.cleanup ? result.cleanup.status : <span class="muted">Not needed</span>}</dd>
        </div>
        <div>
          <dt>Original tree</dt>
          <dd>
            {result.originalPreserved === null ? (
              <span class="muted">Not compared</span>
            ) : result.originalPreserved ? (
              "Unchanged"
            ) : (
              "Changed unexpectedly"
            )}
          </dd>
        </div>
      </dl>
      {result.problems.length > 0 && (
        <ul class={styles.problemList}>
          {result.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {result.diff && <DiffView text={result.diff} label="Applied diff: git diff --cached in the worktree" />}
      {result.patchApplied && <TestResults tests={result.tests} />}
    </InkBox>
  );
}
