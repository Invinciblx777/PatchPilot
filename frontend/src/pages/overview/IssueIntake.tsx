import { useState } from "preact/hooks";
import type { ApiError, Run } from "../../api/types.ts";
import { EmptyState, ErrorNotice, Notice } from "../../components/common.tsx";
import { Button, TextArea, TextField } from "../../components/controls.tsx";
import { JsonView } from "../../components/JsonView.tsx";
import { StatusMark } from "../../components/StatusMark.tsx";
import { Bug } from "../../ink/Drawings.tsx";
import { record, str } from "../../lib/events.ts";
import { defaultPrompt, parseIssueRef } from "../../lib/issueRef.ts";
import { Link } from "../../router.tsx";
import { clearRuns, startRun, useAppState } from "../../state/store.ts";
import styles from "./overview.module.css";

const MAX_FILES = 5;

function filePaths(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function pathProblem(path: string): string | null {
  if (path.length > 300) return `${path.slice(0, 40)}… is longer than 300 characters`;
  if (path.startsWith("/") || path.includes("\\") || path.split("/").some((p) => p === "" || p === "." || p === "..")) {
    return `${path} must be a repository-relative path without "..", empty parts, or backslashes`;
  }
  return null;
}

const REF = /^[A-Za-z0-9._/-]{1,200}$/;

export function IssueIntake() {
  const config = useAppState((s) => s.config);
  const investigation = useAppState((s) => s.runs.investigation);
  const probe = useAppState((s) => s.runs.probe);
  const [issue, setIssue] = useState("");
  const [files, setFiles] = useState("");
  const [ref, setRef] = useState("");
  const [prompt, setPrompt] = useState("");
  const [promptEdited, setPromptEdited] = useState(false);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [starting, setStarting] = useState(false);

  const map = config?.capabilityMap;
  const issueMapped = map ? map.configured && map.ok && "get_issue" in map.capabilities : null;
  const parsed = parseIssueRef(issue);
  const paths = filePaths(files);
  const suggested = parsed ? defaultPrompt(parsed, paths) : "";
  const effectivePrompt = promptEdited ? prompt : suggested;
  const busy = investigation?.state === "running" || probe?.state === "running";

  const submit = async (event: Event) => {
    event.preventDefault();
    const next: Record<string, string | null> = {};
    if (!parsed) next.issue = "Use a GitHub issue link (https://github.com/OWNER/REPO/issues/N) or OWNER/REPO#N.";
    if (paths.length > MAX_FILES) next.files = `List at most ${MAX_FILES} files.`;
    else next.files = paths.map(pathProblem).find(Boolean) ?? null;
    if (ref && (!REF.test(ref) || ref.startsWith("-") || ref.includes(".."))) next.ref = "Use a branch, tag, or commit name.";
    if (!effectivePrompt.trim()) next.prompt = "Describe the task; AgntID uses it to decide which tools this task may use.";
    setErrors(next);
    if (Object.values(next).some(Boolean) || !parsed) return;
    setStarting(true);
    setFailure(null);
    const result = await startRun("investigation", "investigation", {
      issue: issue.trim(),
      prompt: effectivePrompt.trim(),
      files: paths.map((path) => (ref ? { path, ref } : { path })),
    });
    setStarting(false);
    if (!result.ok) setFailure(result.error);
  };

  return (
    <div class={styles.intake}>
      <div class={styles.intakeHead}>
        <h1 id="intake-title">Investigate an issue</h1>
        <p class="lede">
          PatchPilot opens one AgntID task, reads the issue with the tools that task is allowed, and records every decision the runtime
          returns.
        </p>
      </div>

      {issueMapped === false && (
        <Notice
          tone="unavailable"
          title="GitHub reads are off"
          action={<Link href="/security#capabilities">See what the runtime lists</Link>}
        >
          <p>
            No reviewed capability map has a <code>get_issue</code> entry, so PatchPilot has no tool it may use to read issues. Write the
            map from <code>patchpilot-github-preflight</code> output, then restart <code>patchpilot-ui</code> with{" "}
            <code>--mapping FILE</code>.
          </p>
        </Notice>
      )}

      <form class={styles.form} onSubmit={submit} noValidate>
        <TextField
          id="issue"
          label="GitHub issue"
          value={issue}
          onValue={setIssue}
          placeholder="https://github.com/owner/repo/issues/42 or owner/repo#42"
          error={errors.issue}
          mono
        />
        <div class={styles.formRow}>
          <TextArea
            id="files"
            label="Files to read (optional)"
            value={files}
            onValue={setFiles}
            rows={3}
            mono
            placeholder={"src/parser.py\ntests/test_parser.py"}
            hint={`One repository path per line, up to ${MAX_FILES}. Each is read through AgntID after the issue.`}
            error={errors.files}
          />
          <TextField
            id="ref"
            label="Branch or commit (optional)"
            value={ref}
            onValue={setRef}
            mono
            placeholder="main"
            hint="For the file reads. Empty means the repository default."
            error={errors.ref}
          />
        </div>
        <TextArea
          id="prompt"
          label="Task description sent to AgntID"
          value={effectivePrompt}
          onValue={(value) => {
            setPrompt(value);
            setPromptEdited(true);
          }}
          rows={2}
          placeholder="Filled in from the issue reference"
          hint="AgntID's intent engine reads this to decide which tools this task may use."
          error={errors.prompt}
          aside={
            promptEdited ? (
              <button type="button" class={styles.linkButton} onClick={() => setPromptEdited(false)}>
                Use the suggested text
              </button>
            ) : undefined
          }
        />
        <div class={styles.actions}>
          <Button type="submit" variant="primary" busy={starting} disabled={issueMapped === false || busy}>
            Open task and read issue
          </Button>
          {busy && <span class="muted">An AgntID task is already running.</span>}
          {investigation && !busy && (
            <Button variant="quiet" onClick={() => clearRuns("investigation")}>
              Clear this investigation
            </Button>
          )}
        </div>
      </form>

      {failure && <ErrorNotice error={failure} title="The investigation did not start" />}

      {investigation ? (
        <IssueResult run={investigation} />
      ) : (
        <EmptyState art={<Bug size={132} />} title="No issue loaded yet">
          Paste an issue link above. Nothing is sent to the runtime until you open the task.
        </EmptyState>
      )}
    </div>
  );
}

function IssueResult({ run }: { run: Run }) {
  const issueEvent = run.events.find((e) => e.step === "issue" && e.detail.decision === "allowed");
  const fileEvents = run.events.filter((e) => e.step === "file" && e.detail.decision === "allowed");
  const value = issueEvent?.detail.value;
  const data = record(value);
  const title = str(data?.title);
  const body = str(data?.body);
  const tool = str(issueEvent?.detail.toolName);
  if (!issueEvent) {
    return (
      <div class={styles.result}>
        <StatusMark
          state={run.state === "running" ? "running" : "not_observed"}
          label={run.state === "running" ? "Task in progress" : "No issue content was returned"}
        />
        <p class="muted">
          {run.state === "running" ? "Following the task as it runs." : "The timeline and the decision ledger below show why."}
        </p>
      </div>
    );
  }
  return (
    <div class={styles.result}>
      <p class={styles.resultLabel}>
        Issue as returned by <code>{tool}</code>
      </p>
      {title && <h3 class={styles.issueTitle}>{title}</h3>}
      {body && <p class={styles.issueBody}>{body}</p>}
      {!title && !body && <JsonView value={value} label="Issue result exactly as returned" />}
      {(title || body) && (
        <details>
          <summary class={styles.summaryLink}>Full result exactly as returned</summary>
          <JsonView value={value} label="Issue result exactly as returned" />
        </details>
      )}
      {fileEvents.map((event) => (
        <details key={event.seq} class={styles.fileResult}>
          <summary class={styles.summaryLink}>
            <code>{str(event.detail.path)}</code> as returned by <code>{str(event.detail.toolName)}</code>
          </summary>
          <JsonView value={event.detail.value} label={`File result for ${str(event.detail.path)}`} />
        </details>
      ))}
    </div>
  );
}
