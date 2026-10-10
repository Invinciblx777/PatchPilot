// The task timeline: a real sequence, so it is numbered. Each node is an ink
// ring whose mark follows only what actually happened.

import { Connector } from "../ink/Ink.tsx";
import { roughCircle, seedFrom } from "../ink/rough.ts";
import { clock } from "../lib/format.ts";
import type { Stage, StageState } from "../lib/stages.ts";
import { defaultLabel, type MarkState, StatusMark } from "./StatusMark.tsx";
import styles from "./Timeline.module.css";

function markFor(stage: Stage): MarkState {
  const map: Record<StageState, MarkState> = {
    not_started: "not_started",
    running: "running",
    unavailable: "unavailable",
    allowed: "allowed",
    denied: "denied",
    review: "review",
    unknown: "unknown",
    not_sent: "not_sent",
    not_observed: "not_observed",
    failed: stage.id === "tests" ? "failed" : "error",
    checked: "checked",
    warning: "warning",
    passed: "passed",
    skipped: "skipped",
    incomplete: "incomplete",
  };
  return map[stage.state];
}

const DONE: StageState[] = ["allowed", "checked", "passed"];

export function Timeline({ stages }: { stages: Stage[] }) {
  return (
    <ol class={styles.timeline}>
      {stages.map((stage, index) => {
        const mark = markFor(stage);
        const seed = seedFrom(stage.id);
        return (
          <li key={stage.id} class={styles.stage} data-state={stage.state}>
            <div class={styles.rail} aria-hidden="true">
              <span class={`${styles.node} ${stage.state === "unavailable" ? "hatched" : ""}`}>
                <svg viewBox="0 0 34 34" class={styles.ring} aria-hidden="true" focusable="false">
                  <path d={roughCircle(17, 17, 14, seed)} />
                </svg>
                <span class={styles.number}>{index + 1}</span>
              </span>
              {index < stages.length - 1 && (
                <span class={styles.link}>
                  <Connector seed={`${stage.id}-link`} solid={DONE.includes(stage.state)} />
                </span>
              )}
            </div>
            <div class={styles.body}>
              <div class={styles.head}>
                <h3 class={styles.label}>{stage.label}</h3>
                {stage.at && (
                  <time class={styles.time} dateTime={stage.at}>
                    {clock(stage.at)}
                  </time>
                )}
              </div>
              <StatusMark state={mark} label={stage.state === "failed" && stage.id !== "tests" ? "Failed" : defaultLabel(mark)} />
              <p class={styles.summary}>{stage.summary}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
