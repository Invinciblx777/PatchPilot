// The dateline: what the AgntID runtime connection looks like right now,
// from the last real check, and when that check happened.

import { useEffect, useState } from "preact/hooks";
import type { ConfigView, RuntimeReport } from "../api/types.ts";
import { IconRefresh } from "../ink/Icons.tsx";
import { ago, clock } from "../lib/format.ts";
import { checkRuntime, useAppState } from "../state/store.ts";
import { Button } from "./controls.tsx";
import styles from "./RuntimeStrip.module.css";
import { type MarkState, StatusMark } from "./StatusMark.tsx";

const ERROR_LABELS: Record<string, string> = {
  configuration_error: "Not configured",
  runtime_unreachable: "Unreachable",
  authentication_failed: "Authentication failed",
  runtime_protocol_error: "Protocol error",
  discovery_failed: "Tool list failed",
  timeout: "Timed out",
  cleanup_failed: "Close failed",
};

export function runtimeMark(report: RuntimeReport | null, checking: boolean): { state: MarkState; label: string } {
  if (checking && !report) return { state: "running", label: "Checking" };
  if (!report) return { state: "not_started", label: "Not checked" };
  if (report.ok) return { state: "connected", label: "Connected" };
  return { state: "error", label: ERROR_LABELS[report.error.kind] ?? report.error.kind };
}

export function githubReads(config: ConfigView | null, report: RuntimeReport | null): string {
  if (!config) return "Loading";
  const map = config.capabilityMap;
  if (!map.configured) return "No capability map";
  if (!map.ok) return "Capability map invalid";
  if (!map.capabilities.get_issue) return "No get_issue mapping";
  if (!report?.ok) return "Mapping not checked yet";
  const check = report.capabilities.get_issue.schemaCheck;
  return check?.ok ? "get_issue mapping verified" : "get_issue mapping failed";
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function RuntimeStrip() {
  const runtime = useAppState((s) => s.runtime);
  const config = useAppState((s) => s.config);
  const now = useNow(15000);
  const report = runtime.report;
  const mark = runtimeMark(report, runtime.checking);
  const endpoint = report?.endpoint ?? (config?.agntid.ok ? config.agntid.endpoint : null);
  return (
    <section class={styles.strip} aria-label="AgntID runtime connection">
      <dl class={styles.cells}>
        <div class={styles.cell}>
          <dt>AgntID runtime</dt>
          <dd>
            <StatusMark
              state={runtime.checking && report ? "running" : mark.state}
              label={runtime.checking && report ? "Checking again" : mark.label}
            />
          </dd>
        </div>
        <div class={styles.cell}>
          <dt>Endpoint</dt>
          <dd>{endpoint ? <code>{endpoint}</code> : <span class="muted">Not configured</span>}</dd>
        </div>
        <div class={styles.cell}>
          <dt>Tools listed</dt>
          <dd>
            {report?.ok ? (
              `${report.toolCount}, of which ${report.businessToolCount} business`
            ) : (
              <span class="muted">Unknown until connected</span>
            )}
          </dd>
        </div>
        <div class={styles.cell}>
          <dt>GitHub reads</dt>
          <dd>{githubReads(config, report)}</dd>
        </div>
        <div class={styles.cell}>
          <dt>Last check</dt>
          <dd>
            {report ? (
              <time dateTime={report.checkedAt} title={clock(report.checkedAt)}>
                {ago(report.checkedAt, now)}
              </time>
            ) : (
              <span class="muted">Never</span>
            )}
          </dd>
        </div>
      </dl>
      <Button variant="quiet" icon={<IconRefresh size={16} />} busy={runtime.checking} onClick={() => void checkRuntime()}>
        Check again
      </Button>
      {report && !report.ok && <p class={styles.error}>{report.error.message}</p>}
      {runtime.error && <p class={styles.error}>{runtime.error.message}</p>}
    </section>
  );
}
