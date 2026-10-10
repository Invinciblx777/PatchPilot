// Readable unified diff. Counts come from the diff's own lines; signs and
// line numbers carry the meaning, the green/red wash only reinforces it.

import { useState } from "preact/hooks";
import { IconChevron } from "../ink/Icons.tsx";
import { type DiffFile, parseUnifiedDiff } from "../lib/diff.ts";
import styles from "./DiffView.module.css";

const KIND_LABEL: Record<DiffFile["kind"], string> = {
  modify: "modified",
  create: "new file",
  delete: "deleted",
  rename: "renamed",
  copy: "copied",
};

export function DiffView({ text, label }: { text: string; label: string }) {
  const files = parseUnifiedDiff(text);
  const added = files.reduce((n, f) => n + f.added, 0);
  const removed = files.reduce((n, f) => n + f.removed, 0);
  if (!files.length) {
    return <p class="muted">The text is not a unified diff with file changes.</p>;
  }
  return (
    <section class={styles.diff} aria-label={label}>
      <header class={styles.summary}>
        <span>{label}</span>
        <span class={styles.counts}>
          {files.length} {files.length === 1 ? "file" : "files"}
          <span class={styles.add}>+{added}</span>
          <span class={styles.del}>−{removed}</span>
        </span>
      </header>
      {files.map((file, index) => (
        <FileBlock key={`${file.path}-${index}`} file={file} startOpen={files.length <= 4} />
      ))}
    </section>
  );
}

function FileBlock({ file, startOpen }: { file: DiffFile; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen);
  const name = file.kind === "rename" || file.kind === "copy" ? `${file.oldPath} → ${file.path}` : file.path;
  return (
    <div class={styles.file}>
      <button type="button" class={styles.fileHead} aria-expanded={open ? "true" : "false"} onClick={() => setOpen(!open)}>
        <IconChevron size={16} class={`${styles.chev} ${open ? styles.open : ""}`} />
        <span class={styles.path}>{name}</span>
        <span class={styles.kind}>{KIND_LABEL[file.kind]}</span>
        <span class={styles.counts}>
          <span class={styles.add}>+{file.added}</span>
          <span class={styles.del}>−{file.removed}</span>
        </span>
      </button>
      <div class={`${styles.bodyWrap} ${open ? styles.bodyOpen : ""}`}>
        <div class={styles.bodyInner}>
          {file.binary ? (
            <p class={styles.note}>Binary change: no line diff is shown.</p>
          ) : (
            // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be reachable by keyboard
            // biome-ignore lint/a11y/useSemanticElements: a section would add an extra landmark per file
            <div class={styles.scroller} tabIndex={0} role="region" aria-label={`Changes in ${file.path}`}>
              <table class={styles.lines}>
                <tbody>
                  {file.hunks.map((hunk, h) => [
                    <tr key={`h${h}`} class={styles.hunk}>
                      <td colSpan={4}>{hunk.header}</td>
                    </tr>,
                    ...hunk.lines.map((line, i) => (
                      <tr key={`h${h}-${i}`} class={styles[line.kind]}>
                        <td class={styles.num}>{line.oldNumber ?? ""}</td>
                        <td class={styles.num}>{line.newNumber ?? ""}</td>
                        <td class={styles.sign} aria-hidden="true">
                          {line.kind === "added" ? "+" : line.kind === "removed" ? "−" : ""}
                        </td>
                        <td class={styles.code}>
                          {line.kind === "added" && <span class="visually-hidden">added: </span>}
                          {line.kind === "removed" && <span class="visually-hidden">removed: </span>}
                          {line.text || " "}
                        </td>
                      </tr>
                    )),
                  ])}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
