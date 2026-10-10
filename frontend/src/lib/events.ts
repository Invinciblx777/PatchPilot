import type { Decision, Run, RunEvent } from "../api/types.ts";

export function eventsFor(run: Run | null, step: string): RunEvent[] {
  return run ? run.events.filter((e) => e.step === step) : [];
}

export function firstEvent(run: Run | null, step: string): RunEvent | undefined {
  return eventsFor(run, step)[0];
}

export function lastEvent(run: Run | null, step: string): RunEvent | undefined {
  return eventsFor(run, step).at(-1);
}

export function lastError(run: Run | null): RunEvent | undefined {
  return run?.events.filter((e) => e.status === "error").at(-1);
}

export function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

const DECISIONS = new Set<Decision>(["allowed", "denied", "review", "not_observed", "unknown", "not_determined"]);

/** The decision the backend computed for a real call event, if it is one. */
export function decisionOf(event: RunEvent | undefined): Decision | null {
  const value = event?.detail.decision;
  return typeof value === "string" && DECISIONS.has(value as Decision) ? (value as Decision) : null;
}

/** The runtime's own denial reason, as returned; falls back to the formatted explanation. */
export function denialReason(event: RunEvent | undefined): string | null {
  if (!event) return null;
  const denial = record(event.detail.denial);
  return str(denial?.reason) ?? str(event.detail.denialExplanation) ?? str(event.detail.message);
}
