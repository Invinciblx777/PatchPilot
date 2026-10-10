import { EventList } from "../../components/EventList.tsx";
import { Ledger } from "../../components/Ledger.tsx";
import { RuntimeStrip } from "../../components/RuntimeStrip.tsx";
import { Timeline } from "../../components/Timeline.tsx";
import { CropMarks } from "../../ink/Ink.tsx";
import layout from "../../layout/layout.module.css";
import { deriveStages } from "../../lib/stages.ts";
import { Link } from "../../router.tsx";
import { useAppState } from "../../state/store.ts";
import { IssueIntake } from "./IssueIntake.tsx";
import styles from "./overview.module.css";
import { PatchReview } from "./PatchReview.tsx";

export function Overview() {
  const config = useAppState((s) => s.config);
  const runs = useAppState((s) => s.runs);
  const stages = deriveStages({
    config,
    investigation: runs.investigation,
    patchCheck: runs.patchCheck,
    patchApply: runs.patchApply,
  });
  const journey = [runs.investigation, runs.patchCheck, runs.patchApply]
    .flatMap((run) => run?.events ?? [])
    .sort((a, b) => b.seq - a.seq)
    .slice(0, 8);

  return (
    <div class={layout.page}>
      <RuntimeStrip />

      <div class={styles.split}>
        <section class={styles.framed} aria-labelledby="intake-title">
          <CropMarks />
          <IssueIntake />
        </section>
        <section class={styles.timelineSection} aria-labelledby="timeline-title">
          <h2 id="timeline-title">Task timeline</h2>
          <p class={`note ${styles.timelineNote}`}>each stage shows only what actually ran</p>
          <Timeline stages={stages} />
        </section>
      </div>

      <section class={`${styles.framed} ${styles.patch}`} aria-labelledby="patch-title">
        <CropMarks />
        <PatchReview />
      </section>

      <div class={styles.evidence}>
        <Ledger run={runs.investigation} heading="AgntID decisions" empty="No AgntID task has run in this journey yet." />
        <section class={styles.operations} aria-labelledby="operations-title">
          <div class={styles.sectionHead}>
            <h2 id="operations-title">Operations</h2>
            <p class="lede">What this PatchPilot server recorded for the current journey, newest first.</p>
          </div>
          {journey.length ? (
            <EventList events={journey} showDetail={false} />
          ) : (
            <p class="muted">Nothing recorded yet. Events appear here as soon as something runs.</p>
          )}
          <Link href="/activity" class={styles.more}>
            Open the full activity log
          </Link>
        </section>
      </div>
    </div>
  );
}
