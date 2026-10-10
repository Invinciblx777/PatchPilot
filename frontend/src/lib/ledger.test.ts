import assert from "node:assert/strict";
import { test } from "node:test";
import { ev, mappingOk, readEvent, run, runtimeOk, taskClosed, taskOpened } from "./fixtures.test-support.ts";
import { deriveLedger } from "./ledger.ts";

test("an allowed issue read fills all five facts from real events", () => {
  const ledger = deriveLedger(
    run("investigation", [runtimeOk(), mappingOk(), taskOpened(), readEvent("issue", "allowed", "success"), taskClosed()]),
  );
  assert.equal(ledger.tool, "gh.issue_read");
  assert.equal(ledger.taskId, "t-1");
  assert.deepEqual(
    [ledger.connection.state, ledger.discovered.state, ledger.inTask.state, ledger.decision.state, ledger.execution.state],
    ["yes", "yes", "yes", "allowed", "success"],
  );
  assert.deepEqual(ledger.checks, [{ label: "get_issue mapping matches the live schema", ok: true, problems: [] }]);
});

test("a tool outside the shortlist is not in the task and no decision is observed", () => {
  const ledger = deriveLedger(
    run("investigation", [runtimeOk(), mappingOk(), taskOpened(["other_tool"]), readEvent("issue", "not_observed", "not_sent")]),
  );
  assert.equal(ledger.inTask.state, "no");
  assert.equal(ledger.decision.state, "not_observed");
  assert.equal(ledger.execution.state, "not_sent");
});

test("a task that was not opened reports its stage; nothing after it is claimed", () => {
  const ledger = deriveLedger(
    run("investigation", [
      runtimeOk(),
      mappingOk(),
      ev("task", "agntid", "error", "Task not opened (task_open_rejected)", { stage: "task_open_rejected", reason: "nope" }),
    ]),
  );
  assert.equal(ledger.inTask.state, "no_task");
  assert.match(ledger.inTask.text, /task_open_rejected/);
  assert.equal(ledger.decision.state, "not_observed");
  assert.equal(ledger.execution.state, "not_sent");
});

test("probe denials keep the runtime's reason, and review is not a plain denial", () => {
  const denial = { reason: "path not allowed", policy_name: "path-scope", denial_step: "policy_evaluation" };
  const denied = deriveLedger(
    run("probe", [
      runtimeOk(),
      ev("probe_check", "patchpilot", "warning", "Call checks passed", {
        tool: "demo_get_ticket",
        readOnly: "not_declared",
        confirmed: true,
      }),
      taskOpened(["demo_get_ticket"]),
      readEvent("call", "denied", "blocked", { denial }),
    ]),
  );
  assert.equal(denied.tool, "demo_get_ticket");
  assert.equal(denied.decision.state, "denied");
  assert.deepEqual(denied.decision.denial, denial);
  const review = deriveLedger(run("probe", [runtimeOk(), taskOpened(["demo_get_ticket"]), readEvent("call", "review", "blocked")]));
  assert.equal(review.decision.state, "review");
});

test("an unreachable runtime is a failed connection; nothing else was checked", () => {
  const ledger = deriveLedger(
    run("investigation", [
      ev("runtime", "patchpilot", "error", "Runtime check failed: runtime_unreachable", { kind: "runtime_unreachable" }),
    ]),
  );
  assert.equal(ledger.connection.state, "failed");
  assert.match(ledger.connection.text, /runtime_unreachable/);
  assert.deepEqual([ledger.discovered.state, ledger.inTask.state], ["not_checked", "not_checked"]);
});

test("no run means nothing is claimed", () => {
  const ledger = deriveLedger(null);
  assert.deepEqual(
    [ledger.connection.state, ledger.discovered.state, ledger.inTask.state, ledger.decision.state, ledger.execution.state],
    ["not_checked", "not_checked", "not_checked", "not_observed", "not_sent"],
  );
});
