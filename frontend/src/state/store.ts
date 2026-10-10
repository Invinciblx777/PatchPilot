// App state. Everything shown comes from the backend; the browser only keeps
// which runs belong to the current journey and two local preferences.

import { useEffect, useRef, useState } from "preact/hooks";
import { type ApiResult, api, startSession, streamRun } from "../api/client.ts";
import type { ApiError, ConfigView, RepoView, Run, RunKind, RunSummary, RuntimeReport, Session } from "../api/types.ts";

export type Slot = "investigation" | "patchCheck" | "patchApply" | "probe";
export type Motion = "system" | "reduce" | "full";

export type AppState = {
  session: Session | null;
  sessionError: ApiError | null;
  config: ConfigView | null;
  configError: ApiError | null;
  runtime: { checking: boolean; report: RuntimeReport | null; error: ApiError | null };
  runs: Record<Slot, Run | null>;
  repos: RepoView[] | null;
  repoId: string | null;
  motion: Motion;
  announcement: string;
  finishedRuns: number; // bumps when any run ends, so lists can refetch
};

const REPO_KEY = "patchpilot.repo";
const MOTION_KEY = "patchpilot.motion";
const JOURNEY_KEY = "patchpilot.journey";

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string | null, storage: Storage | null = safeLocal()): void {
  try {
    if (value === null) storage?.removeItem(key);
    else storage?.setItem(key, value);
  } catch {
    /* storage unavailable: preference lasts for this page only */
  }
}

function safeLocal(): Storage | null {
  try {
    return localStorage;
  } catch {
    return null;
  }
}

function safeSession(): Storage | null {
  try {
    return sessionStorage;
  } catch {
    return null;
  }
}

const initialMotion = stored(MOTION_KEY);

let state: AppState = {
  session: null,
  sessionError: null,
  config: null,
  configError: null,
  runtime: { checking: false, report: null, error: null },
  runs: { investigation: null, patchCheck: null, patchApply: null, probe: null },
  repos: null,
  repoId: stored(REPO_KEY),
  motion: initialMotion === "reduce" || initialMotion === "full" ? initialMotion : "system",
  announcement: "",
  finishedRuns: 0,
};

const listeners = new Set<() => void>();

export function getState(): AppState {
  return state;
}

export function setState(patch: Partial<AppState> | ((current: AppState) => Partial<AppState>)): void {
  const next = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

export function useAppState<T>(selector: (s: AppState) => T): T {
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  const [value, setValue] = useState(() => selector(state));
  useEffect(() => {
    const check = () => {
      const next = selectorRef.current(state);
      setValue((previous) => (Object.is(previous, next) ? previous : next));
    };
    check();
    listeners.add(check);
    return () => {
      listeners.delete(check);
    };
  }, []);
  return value;
}

// --------------------------------------------------------------------------
// Preferences
// --------------------------------------------------------------------------

export function applyMotion(motion: Motion): void {
  const root = document.documentElement;
  if (motion === "system") delete root.dataset.motion;
  else root.dataset.motion = motion;
}

export function setMotion(motion: Motion): void {
  store(MOTION_KEY, motion === "system" ? null : motion);
  applyMotion(motion);
  setState({ motion });
}

export function selectRepo(repoId: string): void {
  store(REPO_KEY, repoId);
  setState({ repoId });
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

export async function loadConfig(): Promise<void> {
  const result = await api.get<ConfigView>("/api/config");
  if (!result.ok) {
    setState({ configError: result.error });
    return;
  }
  const config = result.data;
  const known = config.repos.some((r) => r.id === state.repoId);
  setState({ config, configError: null, repoId: known ? state.repoId : (config.repos[0]?.id ?? null) });
}

export async function loadRepos(): Promise<void> {
  const result = await api.get<{ repos: RepoView[] }>("/api/repos");
  if (result.ok) setState({ repos: result.data.repos });
}

export async function checkRuntime(): Promise<void> {
  if (state.runtime.checking) return;
  setState((s) => ({ runtime: { ...s.runtime, checking: true } }));
  const result = await api.get<RuntimeReport>("/api/runtime");
  setState({
    runtime: result.ok
      ? { checking: false, report: result.data, error: null }
      : { checking: false, report: state.runtime.report, error: result.error },
    finishedRuns: state.finishedRuns + 1,
  });
}

export async function boot(): Promise<void> {
  applyMotion(state.motion);
  const session = await startSession();
  if (!session.ok) {
    setState({ sessionError: session.error });
    return;
  }
  setState({ session: session.data, sessionError: null });
  await loadConfig();
  void loadRepos();
  void checkRuntime();
  void restoreJourney();
}

// --------------------------------------------------------------------------
// Runs
// --------------------------------------------------------------------------

function saveJourney(): void {
  const ids = Object.fromEntries(Object.entries(state.runs).map(([slot, run]) => [slot, run?.id ?? null]));
  store(JOURNEY_KEY, JSON.stringify(ids), safeSession());
}

function setRun(slot: Slot, runId: string, update: (run: Run) => Run): void {
  setState((s) => {
    const current = s.runs[slot];
    if (!current || current.id !== runId) return {}; // a newer run replaced this one
    return { runs: { ...s.runs, [slot]: update(current) } };
  });
}

async function follow(slot: Slot, runId: string): Promise<void> {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const after = state.runs[slot]?.id === runId ? (state.runs[slot]?.events.length ?? 0) : 0;
    const ended = await streamRun(runId, after, (message) => {
      if (message.type === "event") {
        const event = message.event;
        setRun(slot, runId, (run) => ({ ...run, events: [...run.events, event], eventCount: run.events.length + 1 }));
        setState({ announcement: event.title });
      } else if (message.type === "end") {
        setRun(slot, runId, () => message.run);
        setState((s) => ({ finishedRuns: s.finishedRuns + 1 }));
      }
    });
    if (ended) return;
    await new Promise((resolve) => setTimeout(resolve, 800 * (attempt + 1)));
  }
  // The stream kept breaking: read the run once so the screen shows its real state.
  const result = await api.get<Run>(`/api/runs/${encodeURIComponent(runId)}`);
  if (result.ok) setRun(slot, runId, () => result.data);
}

export async function startRun(slot: Slot, kind: RunKind, body: Record<string, unknown>): Promise<ApiResult<RunSummary>> {
  const result = await api.post<{ run: RunSummary }>("/api/runs", { kind, ...body });
  if (!result.ok) return result;
  const summary = result.data.run;
  setState((s) => ({ runs: { ...s.runs, [slot]: { ...summary, request: body, events: [], result: null } } }));
  saveJourney();
  void follow(slot, summary.id);
  return { ok: true, data: summary };
}

export function clearRuns(...slots: Slot[]): void {
  setState((s) => {
    const runs = { ...s.runs };
    for (const slot of slots) runs[slot] = null;
    return { runs };
  });
  saveJourney();
}

async function restoreJourney(): Promise<void> {
  let ids: Partial<Record<Slot, string | null>> = {};
  try {
    ids = JSON.parse(safeSession()?.getItem(JOURNEY_KEY) ?? "{}");
  } catch {
    ids = {};
  }
  for (const slot of ["investigation", "patchCheck", "patchApply", "probe"] as Slot[]) {
    const id = ids[slot];
    if (typeof id !== "string") continue;
    const result = await api.get<Run>(`/api/runs/${encodeURIComponent(id)}`);
    if (!result.ok) continue; // the server restarted; that run is gone
    setState((s) => ({ runs: { ...s.runs, [slot]: result.data } }));
    if (result.data.state === "running") void follow(slot, id);
  }
}
