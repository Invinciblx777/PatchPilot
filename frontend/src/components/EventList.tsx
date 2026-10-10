// Recorded events. The source tag says who supplied each fact: data returned
// by the AgntID runtime, or PatchPilot's own action or check.

import type { RunEvent } from "../api/types.ts";
import { IconPen, IconSecurity } from "../ink/Icons.tsx";
import { clock } from "../lib/format.ts";
import styles from "./EventList.module.css";
import { JsonView } from "./JsonView.tsx";
import { type MarkState, StateGlyph } from "./StatusMark.tsx";

export function eventMark(event: RunEvent): MarkState {
  const decision = event.detail.decision;
  if (decision === "allowed") return "allowed";
  if (decision === "denied") return "denied";
  if (decision === "review") return "review";
  if (decision === "unknown" || decision === "not_determined") return "unknown";
  if (decision === "not_observed") return event.status === "error" ? "error" : "not_sent";
  if (event.step === "tests") {
    if (event.status === "ok") return "passed";
    if (event.status === "error") return "failed";
  }
  switch (event.status) {
    case "ok":
      return "checked";
    case "warning":
      return "warning";
    case "error":
      return "error";
    case "denied":
      return "denied";
    default:
      return "info";
  }
}

export function SourceTag({ source }: { source: RunEvent["source"] }) {
  return source === "agntid" ? (
    <span class={`${styles.source} ${styles.agntid}`}>
      <IconSecurity size={14} />
      AgntID data
    </span>
  ) : (
    <span class={styles.source}>
      <IconPen size={14} />
      PatchPilot
    </span>
  );
}

export function EventList({ events, showDetail = true }: { events: RunEvent[]; showDetail?: boolean }) {
  return (
    <ol class={styles.list}>
      {events.map((event) => (
        <li key={event.seq} class={styles.event}>
          <time class={styles.time} dateTime={event.at}>
            {clock(event.at)}
          </time>
          <StateGlyph state={eventMark(event)} />
          <div class={styles.main}>
            <p class={styles.title}>{event.title}</p>
            <div class={styles.meta}>
              <SourceTag source={event.source} />
              <span class={styles.step}>{event.step.replace("_", " ")}</span>
            </div>
            {showDetail && Object.keys(event.detail).length > 0 && (
              <details class={styles.detail}>
                <summary>Recorded detail</summary>
                <JsonView value={event.detail} label={`Detail for: ${event.title}`} />
              </details>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
