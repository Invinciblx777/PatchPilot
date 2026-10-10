import assert from "node:assert/strict";
import { test } from "node:test";
import { config, ev, mappingOk, readEvent, run, runtimeOk, taskClosed, taskOpened } from "./fixtures.test-support.ts";
import { deriveStages, type Stage } from "./stages.ts";

function states(stages: Stage[]): Record<string, string> {
  return Object.fromEntries(stages.map((s) => [s.id, s.state]));
}

test("nothing has run: unmapped and unconfigured stages are unavailable, never successful", () => {
  const stages = deriveStages({
    config: config({ issueMapped: false, fileMapped: false, tests: 0 }),
    investigation: null,
    patchCheck: null,
    patchApply: null,
  });
  assert.deepEqual(states(stages), {
    issue: "unavailable",
    investigation: "not_started",
    patch: "not_started",
    tests: "unavailable",
    security: "not_observed",
    pr: "unavailable",
  });
  assert.deepEqual(
    stages.map((s) => s.id),
    ["issue", "investigation", "patch", "tests", "security", "pr"],
  );
});

test("a successful issue and file read is allowed in both stages and counted once each", () => {
  const investigation = run(
    "investigation",
    [
      runtimeOk(),
      mappingOk(),
      taskOpened(),
      readEvent("issue", "allowed", "success"),
      readEvent("file", "allowed", "success", { path: "a.py" }),
      taskClosed(),
    ],
    { outcome: "success" },
  );
  const stages = deriveStages({ config: config(), investigation, patchCheck: null, patchApply: null });
  assert.equal(states(stages).issue, "allowed");
  assert.equal(states(stages).investigation, "allowed");
  const security = stages.find((s) => s.id === "security");
  assert.equal(security?.state, "allowed");
  assert.match(security?.summary ?? "", /2 calls allowed/);
});

test("a denial stops at the issue stage and marks file reads as not attempted", () => {
  const investigation = run(
    "investigation",
    [
      runtimeOk(),
      mappingOk(),
      taskOpened(),
      readEvent("issue", "denied", "blocked", { denial: { reason: "repository not allowed", policy_name: "scope" } }),
      ev("file", "patchpilot", "info", "File reads not attempted", { attempted: false }),
      taskClosed(),
    ],
    { outcome: "denied" },
  );
  const stages = deriveStages({ config: config(), investigation, patchCheck: null, patchApply: null });
  const issue = stages.find((s) => s.id === "issue");
  assert.equal(issue?.state, "denied");
  assert.match(issue?.summary ?? "", /repository not allowed/);
  assert.equal(states(stages).investigation, "not_sent");
  assert.equal(states(stages).security, "denied");
});

test("a running investigation before the read is running, not successful", () => {
  const investigation = run("investigation", [runtimeOk(), mappingOk(), taskOpened()], { state: "running" });
  const stages = deriveStages({ config: config(), investigation, patchCheck: null, patchApply: null });
  assert.equal(states(stages).issue, "running");
  assert.equal(states(stages).security, "not_observed");
});

test("a task that was not opened fails the issue stage without any decision", () => {
  const investigation = run(
    "investigation",
    [runtimeOk(), mappingOk(), ev("task", "agntid", "error", "Task not opened (intent_unavailable)", { stage: "intent_unavailable" })],
    { outcome: "task_not_opened" },
  );
  const stages = deriveStages({ config: config(), investigation, patchCheck: null, patchApply: null });
  const issue = stages.find((s) => s.id === "issue");
  assert.equal(issue?.state, "failed");
  assert.match(issue?.summary ?? "", /intent_unavailable/);
  assert.equal(states(stages).security, "not_observed");
});

test("an unknown outcome is reported as unknown everywhere", () => {
  const investigation = run("investigation", [runtimeOk(), taskOpened(), readEvent("issue", "unknown", "unknown")], {
    outcome: "outcome_unknown",
  });
  const stages = deriveStages({ config: config(), investigation, patchCheck: null, patchApply: null });
  assert.equal(states(stages).issue, "unknown");
  assert.equal(states(stages).security, "unknown");
});

test("patch and test stages follow the real outcomes; a local check is never an allowed decision", () => {
  const patchCheck = run("patch_check", [ev("patch", "patchpilot", "ok", "Patch applies cleanly")], {
    outcome: "check_passed",
    result: { outcome: "check_passed", patchApplied: false, tests: [{ status: "not_run" }], baseRevision: "main" },
  });
  const checked = deriveStages({ config: config(), investigation: null, patchCheck, patchApply: null });
  assert.equal(states(checked).patch, "checked");
  assert.equal(states(checked).tests, "not_started");
  assert.equal(states(checked).security, "not_observed");

  const failing = run("patch_apply", [ev("patch", "patchpilot", "error", "tests failed")], {
    outcome: "tests_failed",
    result: { outcome: "tests_failed", patchApplied: true, tests: [{ status: "passed" }, { status: "failed" }] },
  });
  const applied = deriveStages({ config: config(), investigation: null, patchCheck, patchApply: failing });
  assert.equal(states(applied).patch, "checked");
  assert.equal(states(applied).tests, "failed");

  const skipped = run("patch_apply", [], {
    outcome: "patch_applied",
    result: { outcome: "patch_applied", patchApplied: true, tests: [{ status: "skipped" }] },
  });
  assert.equal(states(deriveStages({ config: config(), investigation: null, patchCheck, patchApply: skipped })).tests, "skipped");

  const passing = run("patch_apply", [], {
    outcome: "tests_passed",
    result: { outcome: "tests_passed", patchApplied: true, tests: [{ status: "passed" }] },
  });
  assert.equal(states(deriveStages({ config: config(), investigation: null, patchCheck, patchApply: passing })).tests, "passed");
});

test("a rejected patch fails the patch stage with its problem", () => {
  const patchCheck = run("patch_check", [], {
    outcome: "unsafe_patch",
    result: { outcome: "unsafe_patch", patchApplied: false, problems: ["'.env' matches sensitive pattern '.env'"], tests: [] },
  });
  const patch = deriveStages({ config: config(), investigation: null, patchCheck, patchApply: null }).find((s) => s.id === "patch");
  assert.equal(patch?.state, "failed");
  assert.match(patch?.summary ?? "", /sensitive pattern/);
});
