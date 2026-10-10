// Unified diff parser for display. Counts come from the hunk lines themselves,
// and hunk headers bound each hunk, so a removed line that starts with "---"
// is never mistaken for a file header.

export type DiffLine = {
  kind: "context" | "added" | "removed" | "meta";
  oldNumber: number | null;
  newNumber: number | null;
  text: string;
};

export type DiffHunk = { header: string; lines: DiffLine[] };

export type DiffFile = {
  path: string;
  oldPath: string | null;
  newPath: string | null;
  kind: "modify" | "create" | "delete" | "rename" | "copy";
  added: number;
  removed: number;
  binary: boolean;
  hunks: DiffHunk[];
};

type Draft = {
  headerOld?: string;
  headerNew?: string;
  minus?: string | null;
  plus?: string | null;
  created: boolean;
  deleted: boolean;
  renameFrom?: string;
  renameTo?: string;
  copyFrom?: string;
  copyTo?: string;
  binary: boolean;
  hunks: DiffHunk[];
};

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

function stripPath(raw: string, prefix: string): string | null {
  const value = raw.split("\t")[0] ?? "";
  if (value === "/dev/null") return null;
  return value.startsWith(prefix) ? value.slice(prefix.length) : value;
}

function finish(draft: Draft): DiffFile | null {
  const created = draft.created || draft.minus === null;
  const deleted = draft.deleted || draft.plus === null;
  const oldPath = draft.renameFrom ?? draft.copyFrom ?? draft.minus ?? draft.headerOld ?? null;
  const newPath = draft.renameTo ?? draft.copyTo ?? draft.plus ?? draft.headerNew ?? null;
  const kind = draft.renameFrom ? "rename" : draft.copyFrom ? "copy" : created ? "create" : deleted ? "delete" : "modify";
  const path = (deleted ? oldPath : newPath) ?? oldPath;
  if (!path || (!draft.hunks.length && kind === "modify" && !draft.binary)) return null;
  let added = 0;
  let removed = 0;
  for (const hunk of draft.hunks) {
    for (const line of hunk.lines) {
      if (line.kind === "added") added += 1;
      if (line.kind === "removed") removed += 1;
    }
  }
  return {
    path,
    oldPath: kind === "create" ? null : oldPath,
    newPath: kind === "delete" ? null : newPath,
    kind,
    added,
    removed,
    binary: draft.binary,
    hunks: draft.hunks,
  };
}

export function parseUnifiedDiff(text: string): DiffFile[] {
  const drafts: Draft[] = [];
  let draft: Draft | null = null;
  let hunk: DiffHunk | null = null;
  let oldLeft = 0;
  let newLeft = 0;
  let oldNumber = 0;
  let newNumber = 0;
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();

  const start = (): Draft => {
    const fresh: Draft = { created: false, deleted: false, binary: false, hunks: [] };
    drafts.push(fresh);
    return fresh;
  };

  for (const raw of lines) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (hunk && (oldLeft > 0 || newLeft > 0)) {
      const tag = line[0];
      if (line === "" || tag === " ") {
        hunk.lines.push({ kind: "context", oldNumber: oldNumber++, newNumber: newNumber++, text: line.slice(1) });
        oldLeft -= 1;
        newLeft -= 1;
        continue;
      }
      if (tag === "-") {
        hunk.lines.push({ kind: "removed", oldNumber: oldNumber++, newNumber: null, text: line.slice(1) });
        oldLeft -= 1;
        continue;
      }
      if (tag === "+") {
        hunk.lines.push({ kind: "added", oldNumber: null, newNumber: newNumber++, text: line.slice(1) });
        newLeft -= 1;
        continue;
      }
      if (tag === "\\") {
        hunk.lines.push({ kind: "meta", oldNumber: null, newNumber: null, text: line });
        continue;
      }
      oldLeft = 0; // the hunk ended early; read this line as a header
      newLeft = 0;
    }
    if (line.startsWith("\\") && hunk) {
      hunk.lines.push({ kind: "meta", oldNumber: null, newNumber: null, text: line });
      continue;
    }
    if (line.startsWith("diff --git ")) {
      draft = start();
      hunk = null;
      const rest = line.slice("diff --git ".length);
      const split = rest.indexOf(" b/");
      if (rest.startsWith("a/") && split > 0) {
        draft.headerOld = rest.slice(2, split);
        draft.headerNew = rest.slice(split + 3);
      }
      continue;
    }
    if (line.startsWith("--- ")) {
      if (!draft || draft.minus !== undefined || draft.hunks.length) draft = start();
      draft.minus = stripPath(line.slice(4), "a/");
      hunk = null;
      continue;
    }
    if (line.startsWith("+++ ") && draft) {
      draft.plus = stripPath(line.slice(4), "b/");
      continue;
    }
    const match = HUNK.exec(line);
    if (match && draft) {
      hunk = { header: line, lines: [] };
      draft.hunks.push(hunk);
      oldNumber = Number(match[1]);
      oldLeft = match[2] === undefined ? 1 : Number(match[2]);
      newNumber = Number(match[3]);
      newLeft = match[4] === undefined ? 1 : Number(match[4]);
      continue;
    }
    if (!draft) continue;
    if (line.startsWith("new file mode")) draft.created = true;
    else if (line.startsWith("deleted file mode")) draft.deleted = true;
    else if (line.startsWith("rename from ")) draft.renameFrom = line.slice("rename from ".length);
    else if (line.startsWith("rename to ")) draft.renameTo = line.slice("rename to ".length);
    else if (line.startsWith("copy from ")) draft.copyFrom = line.slice("copy from ".length);
    else if (line.startsWith("copy to ")) draft.copyTo = line.slice("copy to ".length);
    else if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) draft.binary = true;
  }
  return drafts.map(finish).filter((file): file is DiffFile => file !== null);
}
