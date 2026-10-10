// Test support only: hand-written run and config objects shaped like the
// backend's JSON. They are inputs for pure functions, never shown in the UI.

import type { ConfigView, Run, RunEvent, RunKind } from "../api/types.ts";

let seq = 0;

export function ev(
  step: string,
  source: RunEvent["source"],
  status: RunEvent["status"],
  title: string,
  detail: Record<string, unknown> = {},
): RunEvent {
  seq += 1;
  return { seq, runId: "run-test", at: `2026-10-10T10:00:${String(seq % 60).padStart(2, "0")}.000Z`, source, step, status, title, detail };
}

export function run(
  kind: RunKind,
  events: RunEvent[],
  {
    state = "finished",
    outcome = null,
    result = null,
  }: { state?: Run["state"]; outcome?: string | null; result?: Record<string, unknown> | null } = {},
): Run {
  return {
    id: `run-${kind}`,
    kind,
    title: kind,
    state,
    outcome,
    startedAt: "2026-10-10T10:00:00.000Z",
    finishedAt: state === "finished" ? "2026-10-10T10:00:59.000Z" : null,
    eventCount: events.length,
    request: {},
    events,
    result,
  };
}

export function config({ issueMapped = true, fileMapped = true, tests = 1 } = {}): ConfigView {
  const capabilities: Record<string, unknown> = {};
  if (issueMapped) capabilities.get_issue = { tool: "gh.issue_read", schemaSha256: "a".repeat(64), arguments: {}, fixedArguments: {} };
  if (fileMapped) capabilities.read_file = { tool: "gh.file_read", schemaSha256: "b".repeat(64), arguments: {}, fixedArguments: {} };
  return {
    patchpilotVersion: "0.1.0",
    sdkVersion: "0.2.0",
    envFile: ".env",
    envFileFound: false,
    sources: [],
    agntid: { ok: true, endpoint: "http://localhost:8082/mcp", accessTokenSupplied: false, timeoutSeconds: 20 },
    identity: { ok: true, agentId: "patchpilot:v1", userId: "user-1" },
    capabilityMap: { configured: true, path: "map.json", ok: true, capabilities },
    capabilityParameters: { get_issue: [], get_repository: [], read_file: [] },
    repos: [{ id: "r1", name: "calc", path: "/src/calc" }],
    tests: Array.from({ length: tests }, (_, i) => ({
      name: `t${i}`,
      argv: ["pytest"],
      display: "pytest",
      timeoutSeconds: 600,
      passEnv: [],
    })),
    patchPolicy: { sensitivePatterns: [], allowedSensitivePatterns: [] },
  } as ConfigView;
}

export const runtimeOk = () =>
  ev("runtime", "agntid", "ok", "Runtime listed 3 tools (1 business)", {
    toolCount: 3,
    businessToolCount: 1,
    toolNames: ["agntid_task_open", "agntid_task_close", "gh.issue_read"],
    endpoint: "http://localhost:8082/mcp",
  });

export const mappingOk = () =>
  ev("mapping", "patchpilot", "ok", "get_issue: mapping verified", {
    capability: "get_issue",
    tool: "gh.issue_read",
    listedByRuntime: true,
    schemaCheck: { ok: true, problems: [], warnings: [] },
  });

export const taskOpened = (shortlist: string[] = ["gh.issue_read"]) =>
  ev("task", "agntid", "ok", "Task t-1 opened", { taskId: "t-1", intentEngine: "llm", shortlist });

export const taskClosed = () => ev("task", "patchpilot", "info", "Task close sent for t-1", { taskId: "t-1" });

export function readEvent(step: "issue" | "file" | "call", decision: string, execution: string, extra: Record<string, unknown> = {}) {
  const status =
    decision === "allowed"
      ? "ok"
      : decision === "denied" || decision === "review"
        ? "denied"
        : decision === "unknown"
          ? "warning"
          : "error";
  const source = decision === "allowed" || decision === "denied" || decision === "review" ? "agntid" : "patchpilot";
  return ev(step, source, status as RunEvent["status"], `${step}: ${decision}`, {
    toolName: step === "call" ? "demo_get_ticket" : "gh.issue_read",
    tool: "demo_get_ticket",
    callSent: execution !== "not_sent",
    decision,
    execution,
    ...extra,
  });
}
