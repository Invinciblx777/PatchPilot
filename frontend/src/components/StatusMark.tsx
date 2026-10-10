// One vocabulary for every state, always shape plus words, never colour alone.
// Green and red are reserved for outcomes that really happened: an AgntID
// allow or deny, a test command's exit code. PatchPilot's own checks are ink.

import type { JSX } from "preact";
import styles from "./StatusMark.module.css";

export type MarkState =
  | "allowed"
  | "denied"
  | "review"
  | "unknown"
  | "not_observed"
  | "not_sent"
  | "not_started"
  | "unavailable"
  | "running"
  | "checked"
  | "passed"
  | "failed"
  | "skipped"
  | "warning"
  | "incomplete"
  | "connected"
  | "error"
  | "info";

type Tone = "allow" | "deny" | "warn" | "ink" | "pencil" | "marker";

const SPEC: Record<MarkState, { tone: Tone; label: string; glyph: Glyph }> = {
  allowed: { tone: "allow", label: "Allowed by AgntID", glyph: "check" },
  denied: { tone: "deny", label: "Denied by AgntID", glyph: "cross" },
  review: { tone: "warn", label: "Held for AgntID review", glyph: "pause" },
  unknown: { tone: "warn", label: "Outcome unknown", glyph: "question" },
  not_observed: { tone: "pencil", label: "Not observed", glyph: "dashed" },
  not_sent: { tone: "pencil", label: "Not sent", glyph: "dashed" },
  not_started: { tone: "pencil", label: "Not started", glyph: "empty" },
  unavailable: { tone: "pencil", label: "Unavailable", glyph: "hatched" },
  running: { tone: "marker", label: "Running", glyph: "arc" },
  checked: { tone: "ink", label: "Checked by PatchPilot", glyph: "check" },
  passed: { tone: "allow", label: "Passed", glyph: "check" },
  failed: { tone: "deny", label: "Failed", glyph: "cross" },
  skipped: { tone: "pencil", label: "Skipped", glyph: "slash" },
  warning: { tone: "warn", label: "Needs attention", glyph: "bang" },
  incomplete: { tone: "warn", label: "Incomplete", glyph: "bang" },
  connected: { tone: "ink", label: "Connected", glyph: "dot" },
  error: { tone: "warn", label: "Failed", glyph: "bang" },
  info: { tone: "pencil", label: "Recorded", glyph: "small" },
};

type Glyph = "check" | "cross" | "pause" | "question" | "dashed" | "empty" | "hatched" | "arc" | "slash" | "bang" | "dot" | "small";

const RING = <circle cx="8" cy="8" r="6.25" />;

const GLYPHS: Record<Glyph, JSX.Element> = {
  check: (
    <>
      {RING}
      <path d="M5.2 8.3l1.9 1.9 3.8-4.3" />
    </>
  ),
  cross: (
    <>
      {RING}
      <path d="M5.9 5.9l4.2 4.2M10.1 5.9l-4.2 4.2" />
    </>
  ),
  pause: (
    <>
      {RING}
      <path d="M6.7 5.7v4.6M9.3 5.7v4.6" />
    </>
  ),
  question: (
    <>
      <circle cx="8" cy="8" r="6.25" stroke-dasharray="2.4 1.8" />
      <path d="M6.4 6.5c.1-1.1.9-1.7 1.7-1.7 1 0 1.8.7 1.8 1.6 0 1.3-1.7 1.3-1.8 2.5" />
      <circle cx="8.1" cy="11.1" r="0.5" fill="currentColor" />
    </>
  ),
  dashed: <circle cx="8" cy="8" r="6.25" stroke-dasharray="2.4 1.8" />,
  empty: RING,
  hatched: (
    <>
      {RING}
      <path d="M4.5 9.8l5.3-5.3M5.6 11.5l5.9-5.9M7.6 12.3l4.6-4.6" stroke-width="1" />
    </>
  ),
  arc: <path d="M8 1.75a6.25 6.25 0 0 1 6.25 6.25" />,
  slash: (
    <>
      {RING}
      <path d="M5.4 10.6l5.2-5.2" />
    </>
  ),
  bang: (
    <>
      {RING}
      <path d="M8 4.9v3.7" />
      <circle cx="8" cy="11" r="0.5" fill="currentColor" />
    </>
  ),
  dot: (
    <>
      {RING}
      <circle cx="8" cy="8" r="2.3" fill="currentColor" stroke="none" />
    </>
  ),
  small: <circle cx="8" cy="8" r="2.2" fill="currentColor" stroke="none" />,
};

export function StateGlyph({ state, size = 16 }: { state: MarkState; size?: number }) {
  const spec = SPEC[state];
  return (
    <svg
      class={`${styles.glyph} ${styles[spec.tone]} ${spec.glyph === "arc" ? styles.spin : ""}`}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {GLYPHS[spec.glyph]}
    </svg>
  );
}

export function defaultLabel(state: MarkState): string {
  return SPEC[state].label;
}

export function StatusMark({ state, label, quiet = false }: { state: MarkState; label?: string; quiet?: boolean }) {
  const spec = SPEC[state];
  return (
    <span class={`${styles.mark} ${quiet ? styles.quiet : styles[spec.tone]}`}>
      <StateGlyph state={state} />
      <span class={styles.label}>{label ?? spec.label}</span>
    </span>
  );
}
