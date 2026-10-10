// Task timeline derived only from real runs. A stage that has not run is
// "not started" or "unavailable"; only a returned result is "allowed"; a
// PatchPilot check is "checked", never an AgntID decision.

import type { ConfigView, Run } from "../api/types.ts";
import { decisionOf, denialReason, eventsFor, lastError, lastEvent, record, str } from "./events.ts";

export type StageId = "issue" | "investigation" | "patch" | "tests" | "security" | "pr";

export type StageState =
  | "not_started"
  | "running"
  | "unavailable"
  | "allowed"
  | "denied"
  | "review"
  | "unknown"
  | "not_sent"
  | "not_observed"
  | "failed"
  | "checked"
  | "warning"
  | "passed"
  | "skipped"
  | "incomplete";

export type Stage = { id: StageId; label: string; state: StageState; summary: string; at: string | null };

export type JourneyInput = {
  config: ConfigView | null;
  investigation: Run | null;
  patchCheck: Run | null;
  patchApply: Run | null;
};

type StagePart = Omit<Stage, "id" | "label"> | { state: StageState; summary: string; at?: string | null };

const LABELS: Record<StageId, string> = {
  issue: "Issue retrieval",
  investigation: "Code investigation",
  patch: "Patch preparation",
  tests: "Test execution",
  security: "Security evaluation",
  pr: "Pull request",
};

function mapped(config: ConfigView | null, capability: "get_issue" | "read_file"): boolean | null {
  if (!config) return null;
  const map = config.capabilityMap;
  return map.configured && map.ok ? capability in map.capabilities : false;
}

const READ_STATES = {
  allowed: "allowed",
  denied: "denied",
  review: "review",
  unknown: "unknown",
  not_determined: "failed",
  not_observed: "not_sent",
} as const;

function issueStage({ config, investigation: run }: JourneyInput): StagePart {
  if (!run) {
    if (mapped(config, "get_issue") === false) {
      return { state: "unavailable", summary: "No reviewed get_issue mapping is registered, so there is no tool to read issues with." };
    }
    return { state: "not_started", summary: "Waiting for an issue reference." };
  }
  const read = lastEvent(run, "issue");
  if (read) {
    if (read.detail.attempted === false) return { state: "unavailable", summary: read.title, at: read.at };
    const decision = decisionOf(read);
    if (decision) {
      const state = READ_STATES[decision];
      const task = str(record(read.detail.provenance)?.taskId);
      const summary =
        state === "allowed"
          ? `Read through AgntID task ${task ?? ""}`.trim()
          : state === "denied"
            ? `Denied: ${denialReason(read) ?? "no reason returned"}`
            : state === "review"
              ? "Held for AgntID review"
              : read.title;
      return { state, summary, at: read.at };
    }
  }
  if (run.state === "running") return { state: "running", summary: run.events.at(-1)?.title ?? "Starting", at: null };
  const failure = lastError(run);
  return { state: "failed", summary: failure?.title ?? `Ended: ${run.outcome ?? "unknown"}`, at: failure?.at ?? null };
}

const PRIORITY: StageState[] = ["denied", "review", "unknown", "failed", "not_sent", "allowed"];

function investigationStage({ config, investigation: run }: JourneyInput): StagePart {
  const events = eventsFor(run, "file");
  const requested = Array.isArray(run?.request.files) ? run.request.files.length : 0;
  if (!events.length) {
    if (run?.state === "running" && requested > 0) return { state: "running", summary: "Waiting for the issue read." };
    if (mapped(config, "read_file") === false && !run) {
      return { state: "not_started", summary: "No read_file mapping is registered. Automated investigation is not part of this build." };
    }
    return {
      state: "not_started",
      summary: run ? "No files were requested for this task." : "Reads files you list, through AgntID, after the issue.",
    };
  }
  const reads = events.filter((e) => decisionOf(e) !== null);
  if (!reads.length) {
    const skipped = events[0];
    return { state: skipped?.status === "warning" ? "unavailable" : "not_sent", summary: skipped?.title ?? "", at: skipped?.at ?? null };
  }
  const states = reads.map((e) => READ_STATES[decisionOf(e) ?? "not_observed"] as StageState);
  const worst = PRIORITY.find((s) => states.includes(s)) ?? "allowed";
  const last = reads.at(-1);
  if (worst === "allowed") {
    return {
      state: "allowed",
      summary: `${reads.length} ${reads.length === 1 ? "file" : "files"} read through AgntID`,
      at: last?.at ?? null,
    };
  }
  const culprit = reads.find((e) => READ_STATES[decisionOf(e) ?? "not_observed"] === worst);
  const summary = worst === "denied" ? `Denied: ${denialReason(culprit) ?? "no reason returned"}` : (culprit?.title ?? "");
  return { state: worst, summary, at: culprit?.at ?? null };
}

function result(run: Run | null): Record<string, unknown> {
  return record(run?.result) ?? {};
}

function firstProblem(run: Run | null): string | null {
  const problems = result(run).problems;
  return Array.isArray(problems) && typeof problems[0] === "string" ? problems[0] : null;
}

function patchStage({ patchCheck, patchApply }: JourneyInput): StagePart {
  if (patchApply) {
    if (patchApply.state === "running") return { state: "running", summary: "Applying in a disposable worktree." };
    const applied = result(patchApply);
    if (applied.patchApplied === true) {
      const preserved = applied.originalPreserved === false ? " The original tree changed unexpectedly." : "";
      return { state: "checked", summary: `Applied in a disposable worktree.${preserved}`, at: patchApply.finishedAt };
    }
    return {
      state: "failed",
      summary: firstProblem(patchApply) ?? `Not applied: ${patchApply.outcome ?? "unknown"}`,
      at: patchApply.finishedAt,
    };
  }
  if (patchCheck) {
    if (patchCheck.state === "running") return { state: "running", summary: "Checking the diff against the base." };
    const outcome = patchCheck.outcome;
    if (outcome === "check_passed") {
      const base = str(result(patchCheck).baseRevision);
      return { state: "checked", summary: `Applies cleanly${base ? ` to ${base}` : ""}. Nothing applied yet.`, at: patchCheck.finishedAt };
    }
    const state = outcome === "uncommitted_changes" ? "warning" : "failed";
    return { state, summary: firstProblem(patchCheck) ?? `Check ended: ${outcome ?? "unknown"}`, at: patchCheck.finishedAt };
  }
  return { state: "not_started", summary: "Waiting for a diff. PatchPilot does not write patches." };
}

function testsStage({ config, patchApply }: JourneyInput): StagePart {
  if (config && config.tests.length === 0) {
    return { state: "unavailable", summary: "No test commands are configured. Start patchpilot-ui with --test-command." };
  }
  if (!patchApply) return { state: "not_started", summary: "Runs after you approve applying a checked patch." };
  if (patchApply.state === "running") {
    const approved = eventsFor(patchApply, "tests").some((e) => e.detail.approved === true);
    return { state: "running", summary: approved ? "Running the configured test commands." : "Waiting for the patch to apply." };
  }
  const tests = Array.isArray(result(patchApply).tests) ? (result(patchApply).tests as { status?: unknown }[]) : [];
  const statuses = tests.map((t) => String(t.status));
  const at = patchApply.finishedAt;
  if (!statuses.length) return { state: "unavailable", summary: "No test commands were configured for this run.", at };
  const count = (s: string) => statuses.filter((x) => x === s).length;
  if (count("failed")) return { state: "failed", summary: `${count("failed")} of ${statuses.length} failed`, at };
  if (count("timed_out") || count("not_started")) {
    return { state: "incomplete", summary: "Incomplete: a command timed out or could not start", at };
  }
  if (count("skipped") === statuses.length) return { state: "skipped", summary: "Declined before running", at };
  if (count("not_run") === statuses.length) return { state: "not_started", summary: "Not run: the patch did not apply", at };
  return { state: "passed", summary: `${count("passed")} of ${statuses.length} passed`, at };
}

function securityStage({ investigation }: JourneyInput): StagePart {
  const sent = [...eventsFor(investigation, "issue"), ...eventsFor(investigation, "file")].filter(
    (e) => decisionOf(e) !== null && e.detail.callSent === true,
  );
  if (!sent.length) {
    return { state: "not_observed", summary: "No tool call was sent in this journey, so there is no AgntID decision to show." };
  }
  const decisions = sent.map((e) => decisionOf(e));
  const n = (d: string) => decisions.filter((x) => x === d).length;
  const calls = (k: number) => `${k} ${k === 1 ? "call" : "calls"}`;
  const at = sent.at(-1)?.at ?? null;
  if (n("denied")) return { state: "denied", summary: `${calls(n("denied"))} denied by AgntID`, at };
  if (n("review")) return { state: "review", summary: `${calls(n("review"))} held for AgntID review`, at };
  if (n("unknown") || n("not_determined")) {
    return { state: "unknown", summary: `No decision reported for ${calls(n("unknown") + n("not_determined"))}`, at };
  }
  return { state: "allowed", summary: `${calls(n("allowed"))} allowed by AgntID`, at };
}

export function deriveStages(input: JourneyInput): Stage[] {
  const parts: Record<StageId, StagePart> = {
    issue: issueStage(input),
    investigation: investigationStage(input),
    patch: patchStage(input),
    tests: testsStage(input),
    security: securityStage(input),
    pr: { state: "unavailable", summary: "PatchPilot has no write capability in this build, so it cannot open pull requests." },
  };
  return (Object.keys(LABELS) as StageId[]).map((id) => ({
    id,
    label: LABELS[id],
    state: parts[id].state,
    summary: parts[id].summary,
    at: parts[id].at ?? null,
  }));
}
