import assert from "node:assert/strict";
import { test } from "node:test";
import { isReservedArgument } from "./reservedArgs.ts";

test("AgntID namespace and SDK correlation names are reserved", () => {
  for (const name of [
    "agntid_task_id",
    "agntid_delegation_id",
    "agntid_approval",
    "AGNTID_TASK_ID",
    "_agntid_task_id",
    "__agntid_result",
    "_task_id",
    "_delegation_id",
  ]) {
    assert.equal(isReservedArgument(name), true, name);
  }
});

test("ordinary business argument names are not reserved", () => {
  for (const name of ["ticket_id", "path", "task_id", "approval", "agntid", "my_agntid_task_id"]) {
    assert.equal(isReservedArgument(name), false, name);
  }
});
