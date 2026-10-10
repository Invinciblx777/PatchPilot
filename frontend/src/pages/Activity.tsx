import { useEffect, useState } from "preact/hooks";
import { api } from "../api/client.ts";
import type { RunEvent, RunSummary } from "../api/types.ts";
import { EmptyState } from "../components/common.tsx";
import { Button } from "../components/controls.tsx";
import { EventList } from "../components/EventList.tsx";
import { LedgerSketch } from "../ink/Drawings.tsx";
import { IconRefresh } from "../ink/Icons.tsx";
import layout from "../layout/layout.module.css";
import { clock } from "../lib/format.ts";
import { useAppState } from "../state/store.ts";
import styles from "./pages.module.css";

type Filter = "all" | "agntid" | "patchpilot";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "Everything" },
  { value: "agntid", label: "AgntID data" },
  { value: "patchpilot", label: "PatchPilot actions and checks" },
];

const KIND_LABEL: Record<string, string> = {
  runtime_check: "Runtime check",
  investigation: "Investigation",
  probe: "Task probe",
  patch_check: "Patch check",
  patch_apply: "Patch apply",
};

export function Activity() {
  const session = useAppState((s) => s.session);
  const finished = useAppState((s) => s.finishedRuns);
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    const [activity, list] = await Promise.all([
      api.get<{ events: RunEvent[] }>("/api/activity"),
      api.get<{ runs: RunSummary[] }>("/api/runs"),
    ]);
    if (activity.ok) setEvents(activity.data.events);
    if (list.ok) setRuns(list.data.runs);
    setLoading(false);
  };

  useEffect(() => {
    void load();
  }, [finished]);

  const shown = (events ?? []).filter((e) => filter === "all" || e.source === filter);
  const groups = runs.map((run) => ({ run, events: shown.filter((e) => e.runId === run.id) })).filter((g) => g.events.length > 0);

  return (
    <div class={layout.page}>
      <header class={styles.pageHead}>
        <h1>Activity</h1>
        <p class="lede">
          Every event this PatchPilot server recorded{session ? ` since it started at ${clock(session.startedAt)}` : ""}. "AgntID data"
          marks facts the runtime returned; "PatchPilot" marks local actions and checks. The runtime keeps its own audit trail in the AgntID
          portal; it is not shown here.
        </p>
      </header>

      <div class={styles.toolbar}>
        <fieldset class={styles.segmented}>
          <legend class="visually-hidden">Show events from</legend>
          {FILTERS.map((f) => (
            <button key={f.value} type="button" aria-pressed={filter === f.value ? "true" : "false"} onClick={() => setFilter(f.value)}>
              {f.label}
            </button>
          ))}
        </fieldset>
        <Button variant="quiet" icon={<IconRefresh size={16} />} busy={loading} onClick={() => void load()}>
          Read again
        </Button>
      </div>

      {events !== null && groups.length === 0 && (
        <EmptyState art={<LedgerSketch size={130} />} title={events.length ? "Nothing matches this filter" : "Nothing recorded yet"}>
          {events.length
            ? "Choose another filter to see the other events."
            : "Events appear here once PatchPilot checks the runtime, opens a task, or checks a patch."}
        </EmptyState>
      )}

      <div class={styles.groups}>
        {groups.map(({ run, events: list }) => (
          <section key={run.id} class={styles.group} aria-label={run.title}>
            <header class={styles.groupHead}>
              <h2 class={styles.groupTitle}>{run.title}</h2>
              <span class="muted">
                {KIND_LABEL[run.kind] ?? run.kind}, started {clock(run.startedAt)}
                {run.state === "running" ? ", still running" : run.outcome ? `, ended ${run.outcome.replaceAll("_", " ")}` : ""}
              </span>
            </header>
            <EventList events={list} />
          </section>
        ))}
      </div>
    </div>
  );
}
