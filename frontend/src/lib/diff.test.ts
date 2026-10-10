import assert from "node:assert/strict";
import { test } from "node:test";
import { parseUnifiedDiff } from "./diff.ts";

const TWO_FILES = [
  "diff --git a/calc.py b/calc.py",
  "index 1111111..2222222 100644",
  "--- a/calc.py",
  "+++ b/calc.py",
  "@@ -1,2 +1,2 @@",
  " def add(a, b):",
  "-    return a - b",
  "+    return a + b",
  "diff --git a/new.txt b/new.txt",
  "new file mode 100644",
  "index 0000000..3333333",
  "--- /dev/null",
  "+++ b/new.txt",
  "@@ -0,0 +1,2 @@",
  "+hello",
  "+world",
  "",
].join("\n");

test("splits files and counts added and removed lines from the hunks", () => {
  const files = parseUnifiedDiff(TWO_FILES);
  assert.equal(files.length, 2);
  const [calc, created] = files;
  assert.ok(calc && created);
  assert.deepEqual(
    { path: calc.path, kind: calc.kind, added: calc.added, removed: calc.removed },
    { path: "calc.py", kind: "modify", added: 1, removed: 1 },
  );
  assert.deepEqual(
    { path: created.path, kind: created.kind, added: created.added, removed: created.removed },
    { path: "new.txt", kind: "create", added: 2, removed: 0 },
  );
});

test("numbers context, removed, and added lines on the correct side", () => {
  const [calc] = parseUnifiedDiff(TWO_FILES);
  const lines = calc?.hunks[0]?.lines.map((l) => [l.kind, l.oldNumber, l.newNumber, l.text]);
  assert.deepEqual(lines, [
    ["context", 1, 1, "def add(a, b):"],
    ["removed", 2, null, "    return a - b"],
    ["added", null, 2, "    return a + b"],
  ]);
  assert.equal(calc?.hunks[0]?.header, "@@ -1,2 +1,2 @@");
});

test("a removed line that starts with dashes stays inside its hunk", () => {
  const sql = ["diff --git a/q.sql b/q.sql", "--- a/q.sql", "+++ b/q.sql", "@@ -1,2 +1,1 @@", "--- old comment", " select 1;", ""].join(
    "\n",
  );
  const [file] = parseUnifiedDiff(sql);
  assert.equal(file?.removed, 1);
  assert.equal(file?.added, 0);
  assert.deepEqual(file?.hunks[0]?.lines[0], { kind: "removed", oldNumber: 1, newNumber: null, text: "-- old comment" });
});

test("deletions, renames, multiple hunks, and no-newline markers", () => {
  const text = [
    "diff --git a/gone.txt b/gone.txt",
    "deleted file mode 100644",
    "--- a/gone.txt",
    "+++ /dev/null",
    "@@ -1 +0,0 @@",
    "-bye",
    "\\ No newline at end of file",
    "diff --git a/old.md b/new.md",
    "similarity index 90%",
    "rename from old.md",
    "rename to new.md",
    "--- a/old.md",
    "+++ b/new.md",
    "@@ -1,1 +1,1 @@",
    "-a",
    "+b",
    "@@ -10,2 +10,3 @@ section",
    " x",
    "+y",
    " z",
    "",
  ].join("\n");
  const [gone, renamed] = parseUnifiedDiff(text);
  assert.deepEqual([gone?.kind, gone?.path, gone?.removed], ["delete", "gone.txt", 1]);
  assert.equal(gone?.hunks[0]?.lines[1]?.kind, "meta");
  assert.deepEqual([renamed?.kind, renamed?.oldPath, renamed?.path], ["rename", "old.md", "new.md"]);
  assert.equal(renamed?.hunks.length, 2);
  assert.deepEqual(
    renamed?.hunks[1]?.lines.map((l) => [l.oldNumber, l.newNumber]),
    [
      [10, 10],
      [null, 11],
      [11, 12],
    ],
  );
  assert.deepEqual([renamed?.added, renamed?.removed], [2, 1]);
});

test("text without file changes yields no files", () => {
  assert.deepEqual(parseUnifiedDiff(""), []);
  assert.deepEqual(parseUnifiedDiff("just some words\nnot a diff\n"), []);
});
