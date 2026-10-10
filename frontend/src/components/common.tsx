import type { ComponentChildren, JSX } from "preact";
import type { ApiError } from "../api/types.ts";
import styles from "./common.module.css";

/** A notice for something that is unavailable, needs attention, or failed. Says what and how to fix it. */
export function Notice({
  tone,
  title,
  children,
  action,
}: {
  tone: "unavailable" | "warning" | "denied" | "info";
  title: string;
  children?: ComponentChildren;
  action?: ComponentChildren;
}) {
  return (
    <div class={`${styles.notice} ${styles[tone]}`} role={tone === "warning" || tone === "denied" ? "status" : undefined}>
      <span class={`${styles.edge} ${tone === "unavailable" ? "hatched" : ""}`} aria-hidden="true" />
      <div class={styles.noticeBody}>
        <p class={styles.noticeTitle}>{title}</p>
        {children && <div class={styles.noticeText}>{children}</div>}
        {action && <div class={styles.noticeAction}>{action}</div>}
      </div>
    </div>
  );
}

export function ErrorNotice({ error, title }: { error: ApiError; title: string }) {
  return (
    <Notice tone="warning" title={title}>
      <p>{error.message}</p>
      {error.problems.length > 0 && (
        <ul class={styles.problems}>
          {error.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
    </Notice>
  );
}

export function EmptyState({ art, title, children }: { art: JSX.Element; title: string; children?: ComponentChildren }) {
  return (
    <div class={styles.empty}>
      {art}
      <div>
        <p class={styles.emptyTitle}>{title}</p>
        {children && <div class={styles.emptyText}>{children}</div>}
      </div>
    </div>
  );
}

/** A labelled value in a definition grid. */
export function Fact({ label, children }: { label: string; children: ComponentChildren }) {
  return (
    <div class={styles.fact}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function FactGrid({ children }: { children: ComponentChildren }) {
  return <dl class={styles.factGrid}>{children}</dl>;
}
