// The five security facts for one real call, kept separate:
// runtime connection, tool listed, tool in the task shortlist, the policy
// decision actually observed, and the execution outcome. PatchPilot's own
// checks are listed apart and never fill in a decision.

import type { Decision, Execution, Run, RunEvent } from "../api/types.ts";
import { decisionOf, denialReason, eventsFor, firstEvent, lastEvent, num, record, str, strings } from "./events.ts";

export type Fact<S extends string> = { state: S; text: string };

export type LedgerView = {
  tool: string | null;
  taskId: string | null;
  connection: Fact<"yes" | "failed" | "not_checked">;
  discovered: Fact<"yes" | "no" | "not_checked">;
  inTask: Fact<"yes" | "no" | "no_task" | "not_checked">;
  decision: Fact<Decision> & { denial: Record<string, unknown> | null; at: string | null };
  execution: Fact<Execution>;
  checks: { label: string; ok: boolean; problems: string[] }[];
};

const EXECUTIONS = new Set<Execution>(["success", "blocked", "failed", "unknown", "not_sent"]);

function callEvent(run: Run | null): RunEvent | undefined {
  if (!run) return undefined;
  return run.kind === "probe" ? lastEvent(run, "call") : firstEvent(run, "issue");
}

function toolOf(run: Run | null, call: RunEvent | undefined): string | null {
  if (!run) return null;
  if (run.kind === "probe") {
    const requested = record(run.request.call);
    return str(firstEvent(run, "probe_check")?.detail.tool) ?? str(call?.detail.tool) ?? str(requested?.tool);
  }
  const mapping = eventsFor(run, "mapping").find((e) => e.detail.capability === "get_issue");
  return str(mapping?.detail.tool) ?? str(call?.detail.toolName);
}

const DECISION_TEXT: Record<Decision, string> = {
  allowed: "Allowed: the runtime returned a result",
  denied: "Denied",
  review: "Held for AgntID review",
  not_observed: "No call was sent, so there is no decision",
  unknown: "Unknown: no definite answer arrived",
  not_determined: "No decision reported: the tool answered with an error",
};

const EXECUTION_TEXT: Record<Execution, string> = {
  success: "Result returned",
  blocked: "Not executed: blocked by the decision",
  failed: "The tool answered with an error",
  unknown: "Unknown: the call may have run",
  not_sent: "Nothing was sent",
};

export function deriveLedger(run: Run | null): LedgerView {
  const call = callEvent(run);
  const tool = toolOf(run, call);
  const runtime = lastEvent(run, "runtime");
  const task = firstEvent(run, "task");

  let connection: LedgerView["connection"] = { state: "not_checked", text: "Not checked in this run" };
  let discovered: LedgerView["discovered"] = { state: "not_checked", text: "Not checked in this run" };
  if (runtime?.status === "ok") {
    const count = num(runtime.detail.toolCount);
    connection = { state: "yes", text: `Connected; ${count ?? "?"} tools listed` };
    if (tool) {
      const listed = strings(runtime.detail.toolNames).includes(tool);
      discovered = listed ? { state: "yes", text: "Listed by the runtime" } : { state: "no", text: "Not listed by the runtime" };
    }
  } else if (runtime) {
    connection = { state: "failed", text: runtime.title };
  }

  let inTask: LedgerView["inTask"] = { state: "not_checked", text: "No task was opened" };
  if (task?.status === "ok") {
    const shortlisted = tool !== null && strings(task.detail.shortlist).includes(tool);
    inTask = shortlisted ? { state: "yes", text: "In this task's shortlist" } : { state: "no", text: "Left out of this task's shortlist" };
  } else if (task?.status === "error") {
    inTask = { state: "no_task", text: task.title };
  }

  const decisionState = decisionOf(call) ?? "not_observed";
  const reason = decisionState === "denied" ? denialReason(call) : null;
  const decision: LedgerView["decision"] = {
    state: decisionState,
    text: reason ? `Denied: ${reason}` : DECISION_TEXT[decisionState],
    denial: record(call?.detail.denial),
    at: decisionOf(call) ? (call?.at ?? null) : null,
  };
  const executionValue = call?.detail.execution;
  const executionState =
    typeof executionValue === "string" && EXECUTIONS.has(executionValue as Execution) ? (executionValue as Execution) : "not_sent";

  const checks = [
    ...eventsFor(run, "mapping").map((e) => {
      const check = record(e.detail.schemaCheck);
      return {
        label: `${str(e.detail.capability) ?? "capability"} mapping matches the live schema`,
        ok: check?.ok === true,
        problems: strings(check?.problems),
      };
    }),
    ...eventsFor(run, "probe_check").map((e) => ({
      label: "Call checks: read-only declaration and arguments",
      ok: e.status !== "error",
      problems: strings(e.detail.problems),
    })),
  ];

  return {
    tool,
    taskId: task?.status === "ok" ? str(task.detail.taskId) : null,
    connection,
    discovered,
    inTask,
    decision,
    execution: { state: executionState, text: EXECUTION_TEXT[executionState] },
    checks,
  };
}
