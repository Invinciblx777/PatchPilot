import assert from "node:assert/strict";
import { test } from "node:test";
import { defaultPrompt, parseIssueRef } from "./issueRef.ts";

test("parses the GitHub issue URL form", () => {
  assert.deepEqual(parseIssueRef("https://github.com/fixture-org/fixture-repo/issues/7"), {
    owner: "fixture-org",
    repo: "fixture-repo",
    number: 7,
    reference: "fixture-org/fixture-repo#7",
  });
});

test("parses the short form and tolerates surrounding space, a trailing slash, a query, or a fragment", () => {
  assert.equal(parseIssueRef("  octo/hello.world#42 ")?.reference, "octo/hello.world#42");
  assert.equal(parseIssueRef("https://github.com/octo/repo/issues/9/")?.number, 9);
  assert.equal(parseIssueRef("https://github.com/octo/repo/issues/9#issuecomment-1")?.number, 9);
  assert.equal(parseIssueRef("https://github.com/octo/repo/issues/9?x=1")?.number, 9);
});

test("rejects what the backend rejects", () => {
  for (const bad of [
    "",
    "octo/repo",
    "octo/repo#0",
    "octo/repo#7 and more",
    "../repo#7",
    "-octo/repo#7",
    "octo/..#7",
    "https://github.com/octo/repo/pull/7",
    "https://gitlab.com/octo/repo/issues/7",
    "http://github.com/octo/repo/issues/7",
    "octo/repo#99999999999",
  ]) {
    assert.equal(parseIssueRef(bad), null, bad);
  }
});

test("the default task prompt names the issue and every requested file", () => {
  const ref = parseIssueRef("octo/repo#12");
  assert.ok(ref);
  const plain = defaultPrompt(ref, []);
  assert.match(plain, /octo\/repo#12/);
  const withFiles = defaultPrompt(ref, ["src/app.py", "README.md"]);
  assert.match(withFiles, /octo\/repo#12/);
  assert.match(withFiles, /src\/app\.py/);
  assert.match(withFiles, /README\.md/);
});
