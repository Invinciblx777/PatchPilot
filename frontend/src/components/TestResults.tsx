import type { TestRun, TestStatus } from "../api/types.ts";
import { seconds, shellJoin } from "../lib/format.ts";
import { type MarkState, StatusMark } from "./StatusMark.tsx";
import styles from "./TestResults.module.css";

const STATES: Record<TestStatus, { state: MarkState; label: string }> = {
  passed: { state: "passed", label: "Passed" },
  failed: { state: "failed", label: "Failed" },
  timed_out: { state: "incomplete", label: "Timed out" },
  not_started: { state: "failed", label: "Could not start" },
  skipped: { state: "skipped", label: "Skipped" },
  not_run: { state: "not_started", label: "Not run" },
};

export function TestResults({ tests }: { tests: TestRun[] }) {
  if (!tests.length) return <p class="muted">No test commands were configured for this run.</p>;
  return (
    <ul class={styles.list} aria-label="Test command results">
      {tests.map((test) => {
        const spec = STATES[test.status] ?? { state: "unknown" as MarkState, label: test.status };
        return (
          <li key={test.name} class={styles.row}>
            <div class={styles.top}>
              <StatusMark state={spec.state} label={spec.label} />
              <span class={styles.name}>{test.name}</span>
              <span class={styles.facts}>
                {test.exitCode !== null && (
                  <span>
                    exit code <strong>{test.exitCode}</strong>
                  </span>
                )}
                {test.durationSeconds !== null && <span>{seconds(test.durationSeconds)}</span>}
              </span>
            </div>
            <code class={styles.argv}>{shellJoin(test.argv)}</code>
            {test.note && <p class={styles.note}>{test.note}</p>}
            {test.output && (
              <details class={styles.output} open={test.status !== "passed"}>
                <summary>{test.outputTruncated ? "Output (last 20,000 characters)" : "Output"}</summary>
                {/* biome-ignore lint/a11y/noNoninteractiveTabindex: scrollable output must be reachable by keyboard */}
                <pre tabIndex={0}>{test.output}</pre>
              </details>
            )}
          </li>
        );
      })}
    </ul>
  );
}
