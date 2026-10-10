// AgntID decisions for one real call: five separate facts, then PatchPilot's
// own checks on a line of their own. A stamp appears only when the runtime
// actually returned a decision.

import type { ComponentChildren } from "preact";
import type { Run } from "../api/types.ts";
import { Stamp } from "../ink/Drawings.tsx";
import { HandArrow, InkBox } from "../ink/Ink.tsx";
import { Shield, type ShieldState } from "../ink/Shield.tsx";
import { str } from "../lib/events.ts";
import { clock } from "../lib/format.ts";
import { deriveLedger, type LedgerView } from "../lib/ledger.ts";
import styles from "./Ledger.module.css";
import { type MarkState, StatusMark } from "./StatusMark.tsx";

function shieldState(run: Run | null, ledger: LedgerView): ShieldState {
  switch (ledger.decision.state) {
    case "allowed":
      return "allowed";
    case "denied":
      return "denied";
    case "review":
      return "review";
    case "unknown":
    case "not_determined":
      return "unknown";
    default:
      return run?.state === "finished" ? "not_observed" : "idle";
  }
}

const CONNECTION: Record<LedgerView["connection"]["state"], MarkState> = { yes: "connected", failed: "error", not_checked: "not_started" };
const DISCOVERED: Record<LedgerView["discovered"]["state"], MarkState> = { yes: "checked", no: "not_sent", not_checked: "not_started" };
const IN_TASK: Record<LedgerView["inTask"]["state"], MarkState> = {
  yes: "checked",
  no: "not_sent",
  no_task: "error",
  not_checked: "not_started",
};
const DECISION: Record<LedgerView["decision"]["state"], MarkState> = {
  allowed: "allowed",
  denied: "denied",
  review: "review",
  unknown: "unknown",
  not_determined: "unknown",
  not_observed: "not_observed",
};
const EXECUTION: Record<LedgerView["execution"]["state"], MarkState> = {
  success: "checked",
  blocked: "skipped",
  failed: "error",
  unknown: "unknown",
  not_sent: "not_sent",
};

function Row({
  term,
  state,
  text,
  children,
  emphasis = false,
}: {
  term: string;
  state: MarkState;
  text: string;
  children?: ComponentChildren;
  emphasis?: boolean;
}) {
  return (
    <div class={`${styles.row} ${emphasis ? styles.emphasis : ""}`}>
      <dt>{term}</dt>
      <dd>
        <StatusMark state={state} label={text} />
        {children}
      </dd>
    </div>
  );
}

export function Ledger({ run, heading, empty }: { run: Run | null; heading: string; empty: string }) {
  const ledger = deriveLedger(run);
  const decision = ledger.decision;
  const stampKind = decision.state === "allowed" || decision.state === "denied" || decision.state === "review" ? decision.state : null;
  const denial = decision.denial;
  return (
    <InkBox seed={`ledger-${heading}`} class={styles.ledger}>
      <header class={styles.header}>
        <Shield size={58} state={shieldState(run, ledger)} />
        <div>
          <h2 class={styles.title}>{heading}</h2>
          <p class={styles.sub}>
            {ledger.tool ? (
              <>
                For <code>{ledger.tool}</code>
                {ledger.taskId && (
                  <>
                    {" "}
                    in task <code>{ledger.taskId}</code>
                  </>
                )}
              </>
            ) : (
              empty
            )}
          </p>
        </div>
      </header>

      <dl class={styles.facts}>
        <Row term="Runtime connection" state={CONNECTION[ledger.connection.state]} text={ledger.connection.text} />
        <Row term="Tool listed by the runtime" state={DISCOVERED[ledger.discovered.state]} text={ledger.discovered.text} />
        <Row term="Tool in this task's shortlist" state={IN_TASK[ledger.inTask.state]} text={ledger.inTask.text} />
        <Row term="Policy decision" state={DECISION[decision.state]} text={decision.text} emphasis>
          {stampKind && (
            <div class={styles.stamp}>
              <Stamp key={`${run?.id}-${stampKind}`} kind={stampKind} />
              {decision.at && <span class={styles.when}>returned {clock(decision.at)}</span>}
            </div>
          )}
          {denial && (
            <dl class={styles.denial}>
              {str(denial.policy_name) && (
                <div>
                  <dt>Policy</dt>
                  <dd>
                    <code>{str(denial.policy_name)}</code>
                  </dd>
                </div>
              )}
              {str(denial.intent_mismatch) && (
                <div>
                  <dt>Intent</dt>
                  <dd>{str(denial.intent_mismatch)}</dd>
                </div>
              )}
              {str(denial.denial_step) && (
                <div>
                  <dt>Step</dt>
                  <dd>
                    <code>{str(denial.denial_step)}</code>
                  </dd>
                </div>
              )}
            </dl>
          )}
        </Row>
        <Row term="Execution" state={EXECUTION[ledger.execution.state]} text={ledger.execution.text} />
      </dl>

      {ledger.checks.length > 0 && (
        <div class={styles.checks}>
          <div class={styles.checksHead}>
            <h3>PatchPilot checks</h3>
            <span class={`note ${styles.aside}`}>
              <HandArrow flip />
              application checks, not AgntID decisions
            </span>
          </div>
          <ul>
            {ledger.checks.map((check, index) => (
              <li key={`${check.label}-${index}`}>
                <StatusMark state={check.ok ? "checked" : "warning"} label={check.label} quiet={check.ok} />
                {check.problems.map((problem) => (
                  <p key={problem} class={styles.problem}>
                    {problem}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
    </InkBox>
  );
}
