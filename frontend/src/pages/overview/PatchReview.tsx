// The local patch flow (Module 4): check a supplied diff, then apply and test
// it in a disposable worktree only after explicit approval. PatchPilot does
// not write patches, commit, push, or open pull requests.

import { useEffect, useRef, useState } from "preact/hooks";
import type { ApiError, ConfigView, FileChange, PatchResult, Run } from "../../api/types.ts";
import { ErrorNotice, Notice } from "../../components/common.tsx";
import { Button, CheckField, SelectField, TextArea, TextField } from "../../components/controls.tsx";
import { DiffView } from "../../components/DiffView.tsx";
import { type MarkState, StatusMark } from "../../components/StatusMark.tsx";
import { TestResults } from "../../components/TestResults.tsx";
import { IconUpload } from "../../ink/Icons.tsx";
import { plural, shortSha } from "../../lib/format.ts";
import { clearRuns, selectRepo, startRun, useAppState } from "../../state/store.ts";
import styles from "./overview.module.css";

const MAX_PATCH_BYTES = 2 * 1024 * 1024;

const OUTCOMES: Record<string, { state: MarkState; label: string }> = {
  check_passed: { state: "checked", label: "Applies cleanly to the base. Nothing was applied." },
  tests_passed: { state: "passed", label: "Applied in a disposable worktree; every test command passed" },
  patch_applied: { state: "checked", label: "Applied in a disposable worktree; no tests were run" },
  tests_failed: { state: "failed", label: "Applied; tests failed, so this is not a working fix" },
  tests_incomplete: { state: "incomplete", label: "Applied; tests are incomplete (timed out or could not start)" },
  not_applied: { state: "skipped", label: "Not applied" },
  uncommitted_changes: { state: "warning", label: "Refused: the repository has uncommitted changes" },
  invalid_repository: { state: "error", label: "The repository could not be used" },
  invalid_patch: { state: "error", label: "The diff could not be parsed; nothing was applied" },
  unsafe_patch: { state: "error", label: "Rejected by PatchPilot's path rules; nothing was applied" },
  patch_check_failed: { state: "error", label: "The diff does not apply to the base; nothing was applied" },
  workspace_error: { state: "error", label: "The disposable worktree failed" },
  internal_error: { state: "error", label: "PatchPilot hit an unexpected error" },
  interrupted: { state: "error", label: "Stopped: the PatchPilot server shut down" },
};

function outcome(run: Run): { state: MarkState; label: string } {
  if (run.state === "running") return { state: "running", label: run.kind === "patch_apply" ? "Applying and testing" : "Checking" };
  return OUTCOMES[run.outcome ?? ""] ?? { state: "unknown", label: `Ended: ${run.outcome ?? "unknown"}` };
}

function patchResult(run: Run | null): PatchResult | null {
  return run?.state === "finished" && run.result ? (run.result as unknown as PatchResult) : null;
}

function FileChanges({ changes }: { changes: FileChange[] }) {
  return (
    <ul class={styles.changes} aria-label="Files the patch changes">
      {changes.map((change) => (
        <li key={`${change.oldPath}-${change.newPath}`}>
          <code>
            {change.kind === "rename" || change.kind === "copy"
              ? `${change.oldPath} → ${change.newPath}`
              : (change.newPath ?? change.oldPath)}
          </code>
          <span class="muted">
            {change.kind === "modify"
              ? "modified"
              : change.kind === "create"
                ? "new file"
                : change.kind === "delete"
                  ? "deleted"
                  : `${change.kind}d`}
          </span>
          <span class={styles.counts}>
            <span class={styles.add}>+{change.added}</span>
            <span class={styles.del}>−{change.removed}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

export function PatchReview() {
  const config = useAppState((s) => s.config);
  const repos = useAppState((s) => s.repos);
  const repoId = useAppState((s) => s.repoId);
  const check = useAppState((s) => s.runs.patchCheck);
  const apply = useAppState((s) => s.runs.patchApply);

  const live = repos?.find((r) => r.id === repoId);
  const [base, setBase] = useState("");
  const [baseTouched, setBaseTouched] = useState(false);
  const [patch, setPatch] = useState("");
  const [allowUncommitted, setAllowUncommitted] = useState(false);
  const [approveApply, setApproveApply] = useState(false);
  const [approveTests, setApproveTests] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  // What was sent for the current check. The server's run record does not
  // carry the patch text, so the comparison happens here; after a reload this
  // is empty and the patch has to be checked again before it can be applied.
  const [checkedInput, setCheckedInput] = useState<{ patch: string; repoId: string | null; base: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  // The base defaults to the repository's current branch, read live from git.
  useEffect(() => {
    if (!baseTouched) setBase(live?.ok && live.branch ? live.branch : "HEAD");
  }, [live, baseTouched]);

  if (!config) return null;
  if (!config.repos.length) {
    return (
      <>
        <PatchHeading />
        <Notice tone="unavailable" title="No repository is configured">
          <p>
            Restart <code>patchpilot-ui</code> with <code>--repo PATH</code> to choose which local repositories PatchPilot may patch. The
            browser cannot add one.
          </p>
        </Notice>
      </>
    );
  }

  const checked = patchResult(check);
  const stale =
    checked !== null &&
    (checkedInput === null || checkedInput.patch !== patch || checkedInput.repoId !== repoId || checkedInput.base !== base);
  const canApprove = check?.outcome === "check_passed" && !stale && !apply;
  const repoName = config.repos.find((r) => r.id === repoId)?.name ?? "";
  const tests = config.tests;

  const runCheck = async () => {
    setFailure(null);
    setApproveApply(false);
    setApproveTests(false);
    clearRuns("patchApply");
    if (!patch.trim()) {
      setFailure({ code: "invalid_request", message: "Paste or load a unified diff first.", problems: [] });
      return;
    }
    setSending(true);
    setCheckedInput({ patch, repoId, base });
    const result = await startRun("patchCheck", "patch_check", { repoId, base, patch, allowUncommitted });
    setSending(false);
    if (!result.ok) setFailure(result.error);
  };

  const runApply = async () => {
    if (!checked) return;
    setFailure(null);
    setSending(true);
    const result = await startRun("patchApply", "patch_apply", {
      repoId,
      base,
      patch,
      allowUncommitted,
      patchSha256: checked.patchSha256,
      approveApply,
      approveTests: tests.length > 0 && approveTests,
    });
    setSending(false);
    if (!result.ok) setFailure(result.error);
  };

  const loadFile = async (file: File | undefined) => {
    setFileError(null);
    if (!file) return;
    if (file.size > MAX_PATCH_BYTES) {
      setFileError(`${file.name} is larger than 2 MiB.`);
      return;
    }
    setPatch(await file.text());
  };

  const busy = check?.state === "running" || apply?.state === "running";

  return (
    <>
      <PatchHeading />
      <div class={styles.patchControls}>
        <SelectField
          id="repo"
          label="Repository"
          value={repoId ?? ""}
          onValue={(value) => {
            selectRepo(value);
            setBaseTouched(false);
          }}
          options={config.repos.map((r) => ({ value: r.id, label: r.name }))}
          hint={config.repos.find((r) => r.id === repoId)?.path}
        />
        <TextField
          id="base"
          label="Base revision"
          value={base}
          onValue={(value) => {
            setBase(value);
            setBaseTouched(true);
          }}
          mono
          hint={
            live?.ok && live.head
              ? `HEAD is ${shortSha(live.head)}${live.dirty ? `, with ${plural(live.changedPaths, "local change")}` : ""}`
              : undefined
          }
        />
      </div>
      <TextArea
        id="patch"
        label="Unified diff"
        value={patch}
        onValue={setPatch}
        rows={10}
        mono
        placeholder={"diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@"}
        hint="git diff format with a/ and b/ prefixes, at most 2 MiB. It is parsed strictly and never repaired."
        error={fileError}
        aside={
          <>
            <input
              ref={fileInput}
              type="file"
              accept=".patch,.diff,text/plain,text/x-diff"
              class="visually-hidden"
              tabIndex={-1}
              aria-hidden="true"
              onChange={(e) => void loadFile((e.currentTarget as HTMLInputElement).files?.[0])}
            />
            <Button variant="quiet" icon={<IconUpload size={16} />} onClick={() => fileInput.current?.click()}>
              Load a .patch file
            </Button>
          </>
        }
      />
      <CheckField
        id="allow-uncommitted"
        checked={allowUncommitted}
        onChecked={setAllowUncommitted}
        hint="Patch the committed base even though your working tree has local changes. Your files are still never touched."
      >
        Allow uncommitted changes in the repository
      </CheckField>
      <div class={styles.actions}>
        <Button
          variant={canApprove ? "secondary" : "primary"}
          busy={check?.state === "running"}
          disabled={busy}
          onClick={() => void runCheck()}
        >
          Check patch
        </Button>
        {(check || apply) && !busy && (
          <Button
            variant="quiet"
            onClick={() => {
              clearRuns("patchCheck", "patchApply");
              setApproveApply(false);
              setApproveTests(false);
            }}
          >
            Clear results
          </Button>
        )}
      </div>

      {failure && <ErrorNotice error={failure} title="PatchPilot did not run this" />}

      {check && <CheckOutcome run={check} result={checked} patch={patch} stale={stale} showDiff={!apply} />}

      {canApprove && checked && (
        <section class={styles.approval} aria-labelledby="approve-title">
          <h3 id="approve-title">Approve before anything runs</h3>
          <CheckField
            id="approve-apply"
            checked={approveApply}
            onChecked={setApproveApply}
            hint="A new git worktree on a throwaway branch, outside your repository. It and the branch are removed afterwards."
          >
            Apply this patch in a disposable worktree of {repoName} at <code>{base}</code>
          </CheckField>
          {tests.length > 0 ? (
            <CheckField
              id="approve-tests"
              checked={approveTests}
              onChecked={setApproveTests}
              hint={
                <>
                  They run repository code, including code this patch adds, with your user permissions. This is not a sandbox.
                  <span class={styles.commandList}>
                    {tests.map((t) => (
                      <code key={t.name}>{t.display}</code>
                    ))}
                  </span>
                </>
              }
            >
              Run the {plural(tests.length, "configured test command")}
            </CheckField>
          ) : (
            <p class="muted">No test commands are configured, so no tests will run.</p>
          )}
          <div class={styles.actions}>
            <Button variant="primary" disabled={!approveApply || busy} busy={sending} onClick={() => void runApply()}>
              {approveTests && tests.length > 0 ? "Apply patch and run tests" : "Apply patch"}
            </Button>
            {!approveApply && <span class="muted">Tick the first box to allow applying.</span>}
          </div>
        </section>
      )}

      {apply && <ApplyOutcome run={apply} config={config} />}
    </>
  );
}

function PatchHeading() {
  return (
    <div class={styles.sectionHead}>
      <h2 id="patch-title">Proposed change</h2>
      <p class="lede">
        PatchPilot does not write fixes. Supply a unified diff: it is checked, applied in a throwaway worktree, and tested there. Your
        repository is never modified.
      </p>
    </div>
  );
}

function CheckOutcome({
  run,
  result,
  patch,
  stale,
  showDiff,
}: {
  run: Run;
  result: PatchResult | null;
  patch: string;
  stale: boolean;
  showDiff: boolean;
}) {
  const mark = outcome(run);
  return (
    <div class={styles.outcome}>
      <StatusMark state={mark.state} label={mark.label} />
      {result && result.problems.length > 0 && (
        <ul class={styles.problems}>
          {result.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {stale && (
        <Notice tone="warning" title="The diff, repository, or base changed after this check">
          Check it again before applying.
        </Notice>
      )}
      {result?.fileChanges && result.fileChanges.length > 0 && <FileChanges changes={result.fileChanges} />}
      {result && run.outcome === "check_passed" && !stale && showDiff && <DiffView text={patch} label="Submitted diff, not applied yet" />}
    </div>
  );
}

function ApplyOutcome({ run, config }: { run: Run; config: ConfigView }) {
  const mark = outcome(run);
  const result = patchResult(run);
  return (
    <section class={styles.outcome} aria-labelledby="apply-title">
      <h3 id="apply-title" class="visually-hidden">
        Apply result
      </h3>
      <StatusMark state={mark.state} label={mark.label} />
      {result && (
        <>
          {result.problems.length > 0 && (
            <ul class={styles.problems}>
              {result.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
          {result.diff && <DiffView text={result.diff} label="Applied diff: git diff --cached in the disposable worktree" />}
          {result.patchApplied && (
            <div class={styles.testsBlock}>
              <h3>Test results</h3>
              <TestResults tests={result.tests} />
              {result.changedByTests.length > 0 && (
                <p class="muted">
                  Files the test commands changed: <code>{result.changedByTests.join(", ")}</code>
                </p>
              )}
            </div>
          )}
          <dl class={styles.aftermath}>
            <div>
              <dt>Worktree</dt>
              <dd>
                {result.cleanup?.status === "removed" && <StatusMark state="checked" label="Removed, and its branch deleted" quiet />}
                {result.cleanup?.status === "kept" && <StatusMark state="warning" label="Kept on disk" />}
                {result.cleanup?.status === "failed" && (
                  <>
                    <StatusMark state="error" label="Cleanup failed" />
                    {result.cleanup.errors.map((e) => (
                      <p key={e} class={styles.problemText}>
                        {e}
                      </p>
                    ))}
                  </>
                )}
              </dd>
            </div>
            <div>
              <dt>Your repository</dt>
              <dd>
                {result.originalPreserved === true && (
                  <StatusMark state="checked" label="Unchanged (compared with the snapshot taken before the run)" quiet />
                )}
                {result.originalPreserved === false && <StatusMark state="error" label="Changed unexpectedly. Inspect it now." />}
                {result.originalPreserved === null && <span class="muted">Not compared</span>}
              </dd>
            </div>
          </dl>
          {config.tests.length > 0 && result.tests.every((t) => t.status === "passed") && (
            <p class={styles.fine}>
              Passing tests show only that the configured commands exited 0 against this patch; they do not show that the issue is resolved.
            </p>
          )}
        </>
      )}
    </section>
  );
}
