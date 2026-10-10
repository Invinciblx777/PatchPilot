import { Menu } from "../components/Menu.tsx";
import { runtimeMark } from "../components/RuntimeStrip.tsx";
import { StateGlyph } from "../components/StatusMark.tsx";
import { Mark } from "../ink/Drawings.tsx";
import { IconSettings } from "../ink/Icons.tsx";
import { Link, navigate } from "../router.tsx";
import { checkRuntime, type Motion, setMotion, useAppState } from "../state/store.ts";
import styles from "./layout.module.css";

const MOTION_LABELS: Record<Motion, string> = {
  system: "Follow the system setting",
  reduce: "Reduced",
  full: "Full",
};

export function Header() {
  const config = useAppState((s) => s.config);
  const repos = useAppState((s) => s.repos);
  const repoId = useAppState((s) => s.repoId);
  const runtime = useAppState((s) => s.runtime);
  const motion = useAppState((s) => s.motion);
  const repo = config?.repos.find((r) => r.id === repoId) ?? null;
  const live = repos?.find((r) => r.id === repoId);
  const branch = live?.ok ? live.branch : null;
  const mark = runtimeMark(runtime.report, runtime.checking);
  const endpoint = runtime.report?.endpoint ?? (config?.agntid.ok ? config.agntid.endpoint : null);

  return (
    <header class={styles.header}>
      <Link href="/" class={styles.brand} aria-label="PatchPilot overview">
        <Mark />
        <span class={styles.wordmark}>PatchPilot</span>
      </Link>
      <div class={styles.status}>
        <Link href="/workspaces" class={styles.statusItem} title="Active workspace">
          <span class={styles.statusLabel}>Workspace</span>
          {repo ? (
            <span class={styles.statusValue}>
              {repo.name}
              {branch && <code class={styles.branch}>{branch}</code>}
            </span>
          ) : (
            <span class={`${styles.statusValue} muted`}>{config ? "None configured" : "Loading"}</span>
          )}
        </Link>
        <Link href="/security" class={styles.statusItem} title="AgntID runtime connection">
          <span class={styles.statusLabel}>Runtime</span>
          <span class={styles.statusValue}>
            <StateGlyph state={runtime.checking ? "running" : mark.state} />
            {runtime.checking ? "Checking" : mark.label}
            {endpoint && <code class={styles.endpoint}>{endpoint}</code>}
          </span>
        </Link>
        <Menu
          label="Settings menu"
          icon={<IconSettings />}
          items={[
            { kind: "action", label: "Check the runtime connection again", onSelect: () => void checkRuntime() },
            { kind: "heading", label: "Motion" },
            ...(["system", "reduce", "full"] as Motion[]).map((m) => ({
              kind: "radio" as const,
              label: MOTION_LABELS[m],
              checked: motion === m,
              onSelect: () => setMotion(m),
            })),
            { kind: "action", label: "Open settings", onSelect: () => navigate("/settings") },
          ]}
        />
      </div>
    </header>
  );
}
