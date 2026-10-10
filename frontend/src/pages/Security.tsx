import { useState } from "preact/hooks";
import type { ApiError, CapabilityName, ReadOnlyState, RuntimeReport, ToolView } from "../api/types.ts";
import { ErrorNotice, Fact, FactGrid, Notice } from "../components/common.tsx";
import { Button, CheckField, SelectField, TextArea } from "../components/controls.tsx";
import { JsonView } from "../components/JsonView.tsx";
import { Ledger } from "../components/Ledger.tsx";
import { runtimeMark } from "../components/RuntimeStrip.tsx";
import { StatusMark } from "../components/StatusMark.tsx";
import { IconRefresh } from "../ink/Icons.tsx";
import { Shield } from "../ink/Shield.tsx";
import layout from "../layout/layout.module.css";
import { firstEvent, lastEvent, record, str, strings } from "../lib/events.ts";
import { ago, clock, shortSha } from "../lib/format.ts";
import { isReservedArgument } from "../lib/reservedArgs.ts";
import { checkRuntime, clearRuns, startRun, useAppState } from "../state/store.ts";
import styles from "./pages.module.css";

const READ_ONLY_LABEL: Record<ReadOnlyState, string> = {
  declared: "Declared read-only",
  declares_write: "Declares writes",
  not_declared: "Not declared",
};

const CAPABILITY_LABEL: Record<CapabilityName, string> = {
  get_issue: "Read an issue",
  get_repository: "Read repository metadata",
  read_file: "Read a file",
};

export function Security() {
  const runtime = useAppState((s) => s.runtime);
  const config = useAppState((s) => s.config);
  const report = runtime.report;
  const [probeTool, setProbeTool] = useState("");

  return (
    <div class={layout.page}>
      <header class={styles.hero}>
        <Shield size={132} />
        <div class={styles.heroText}>
          <h1>Runtime protection</h1>
          <p class="lede">
            AgntID decides what PatchPilot may do. PatchPilot opens a task for every request, calls tools only through that task, and
            records what comes back. Nothing on this page is inferred.
          </p>
        </div>
      </header>

      <Connection
        report={report}
        checking={runtime.checking}
        tokenSupplied={config?.agntid.ok ? config.agntid.accessTokenSupplied : null}
      />

      {report?.ok && (
        <>
          <ToolInventory
            tools={report.tools}
            onProbe={(name) => {
              setProbeTool(name);
              document.getElementById("probe")?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}
          />
          <Capabilities report={report} />
        </>
      )}

      <Probe report={report?.ok ? report : null} tool={probeTool} onTool={setProbeTool} />
    </div>
  );
}

function Connection({
  report,
  checking,
  tokenSupplied,
}: {
  report: RuntimeReport | null;
  checking: boolean;
  tokenSupplied: boolean | null;
}) {
  const mark = runtimeMark(report, checking);
  return (
    <section class={styles.section} aria-labelledby="connection-title">
      <div class={styles.sectionHead}>
        <h2 id="connection-title">Connection</h2>
        <Button variant="quiet" icon={<IconRefresh size={16} />} busy={checking} onClick={() => void checkRuntime()}>
          Check again
        </Button>
      </div>
      <FactGrid>
        <Fact label="State">
          <StatusMark state={mark.state} label={mark.label} />
        </Fact>
        <Fact label="Endpoint">{report?.endpoint ? <code>{report.endpoint}</code> : <span class="muted">Not configured</span>}</Fact>
        <Fact label="Access token">
          {tokenSupplied === null ? <span class="muted">Unknown</span> : tokenSupplied ? "Supplied (never shown)" : "Not supplied"}
        </Fact>
        <Fact label="AgntID SDK">{report ? report.sdkVersion : <span class="muted">Unknown</span>}</Fact>
        <Fact label="Checked">
          {report ? (
            <>
              {clock(report.checkedAt)}{" "}
              <span class="muted">
                ({ago(report.checkedAt)}, took {report.durationMs} ms)
              </span>
            </>
          ) : (
            <span class="muted">Not yet</span>
          )}
        </Fact>
      </FactGrid>
      {report && !report.ok && (
        <Notice tone="warning" title={`The runtime check failed: ${report.error.kind}`}>
          <p>{report.error.message}</p>
        </Notice>
      )}
      {report?.ok && report.toolCount === 0 && (
        <Notice tone="warning" title="The runtime returned an empty tool list">
          <p>Check the protection profile review, tool classification, role grants, and that runtime sync shows Applied.</p>
        </Notice>
      )}
    </section>
  );
}

function ToolInventory({ tools, onProbe }: { tools: ToolView[]; onProbe: (name: string) => void }) {
  return (
    <section class={styles.section} id="tools" aria-labelledby="tools-title">
      <div class={styles.sectionHead}>
        <h2 id="tools-title">Tools the runtime lists</h2>
      </div>
      <p class="lede">Listed is not allowed: the runtime filters this list by profile and grants, and still evaluates every call.</p>
      <div class={styles.tableWrap}>
        <table class={styles.table}>
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Kind</th>
              <th scope="col">Read-only</th>
              <th scope="col">Schema SHA-256</th>
              <th scope="col">
                <span class="visually-hidden">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <tr key={tool.name}>
                <td>
                  <code class={styles.toolName}>{tool.name}</code>
                  {tool.description && <p class={styles.toolDesc}>{tool.description}</p>}
                </td>
                <td>{tool.isRuntimeControlTool ? <span class="muted">AgntID control</span> : "Business"}</td>
                <td>
                  {tool.isRuntimeControlTool ? (
                    <span class="faint">Never called</span>
                  ) : (
                    <span class={tool.readOnly === "declares_write" ? styles.warnText : undefined}>{READ_ONLY_LABEL[tool.readOnly]}</span>
                  )}
                </td>
                <td>
                  <code title={tool.schemaSha256}>{shortSha(tool.schemaSha256, 16)}</code>
                </td>
                <td>
                  {!tool.isRuntimeControlTool && tool.readOnly !== "declares_write" && (
                    <Button variant="quiet" onClick={() => onProbe(tool.name)}>
                      Probe
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Capabilities({ report }: { report: Extract<RuntimeReport, { ok: true }> }) {
  return (
    <section class={styles.section} id="capabilities" aria-labelledby="capabilities-title">
      <div class={styles.sectionHead}>
        <h2 id="capabilities-title">GitHub capability mappings</h2>
      </div>
      <p class="lede">
        A mapping ties a PatchPilot capability to one exact tool and pins its schema. These checks are PatchPilot's own, not AgntID
        decisions.
      </p>
      {report.capabilityMap.configured === false && (
        <Notice tone="unavailable" title="No capability map is registered">
          <p>
            GitHub reads stay off until a person reviews the tool list and writes a map. Start <code>patchpilot-ui</code> with{" "}
            <code>--mapping FILE</code> or set <code>PATCHPILOT_CAPABILITY_MAP</code>.
          </p>
        </Notice>
      )}
      {report.capabilityMap.ok === false && (
        <Notice tone="warning" title="The capability map could not be loaded">
          <p>{report.capabilityMap.error}</p>
        </Notice>
      )}
      <ul class={styles.capabilities}>
        {(Object.keys(report.capabilities) as CapabilityName[]).map((cap) => {
          const entry = report.capabilities[cap];
          return (
            <li key={cap} class={styles.capability}>
              <div class={styles.capHead}>
                <h3>{CAPABILITY_LABEL[cap]}</h3>
                <code class="muted">{cap}</code>
              </div>
              {entry.mapping ? (
                <>
                  <p>
                    Mapped to <code>{entry.mapping.tool}</code>
                  </p>
                  <StatusMark
                    state={entry.listedByRuntime ? "checked" : "not_sent"}
                    label={entry.listedByRuntime ? "Listed by the runtime" : "Not listed by the runtime"}
                    quiet={entry.listedByRuntime === true}
                  />
                  <StatusMark
                    state={entry.schemaCheck?.ok ? "checked" : "warning"}
                    label={entry.schemaCheck?.ok ? "Schema matches the reviewed hash" : "Schema check failed"}
                    quiet={entry.schemaCheck?.ok === true}
                  />
                  {entry.schemaCheck?.problems.map((p) => (
                    <p key={p} class={styles.warnText}>
                      {p}
                    </p>
                  ))}
                  {entry.schemaCheck?.warnings.map((w) => (
                    <p key={w} class="muted">
                      {w}
                    </p>
                  ))}
                </>
              ) : (
                <StatusMark state="unavailable" label="Not mapped" />
              )}
              <p class={styles.candidates}>
                {entry.candidates.length ? (
                  <>
                    Possible tools (a guess from names, never used without a mapping):{" "}
                    {entry.candidates.map((c, i) => (
                      <span key={c}>
                        {i > 0 && ", "}
                        <code>{c}</code>
                      </span>
                    ))}
                  </>
                ) : (
                  "No listed tool looks like this capability."
                )}
              </p>
            </li>
          );
        })}
      </ul>
      {report.nextSteps.length > 0 && (
        <div class={styles.nextSteps}>
          <h3>Next steps</h3>
          <ul>
            {report.nextSteps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function schemaProperties(tool: ToolView | undefined): { name: string; type: string; required: boolean; managed: boolean }[] {
  const properties = record(tool?.inputSchema.properties) ?? {};
  const required = new Set(strings(tool?.inputSchema.required));
  return Object.entries(properties).map(([name, schema]) => {
    const type = record(schema)?.type;
    return {
      name,
      type: Array.isArray(type) ? type.join(" or ") : typeof type === "string" ? type : "any",
      required: required.has(name),
      managed: isReservedArgument(name),
    };
  });
}

function ArgumentHint({ tool }: { tool: ToolView }) {
  const properties = schemaProperties(tool);
  const yours = properties.filter((p) => !p.managed);
  const managed = properties.filter((p) => p.managed);
  return (
    <span class={styles.props}>
      {yours.length === 0 && <span>This tool's schema lists no arguments for you to fill.</span>}
      {yours.map((p) => (
        <span key={p.name}>
          <code>{p.name}</code> {p.type}
          {p.required ? ", required" : ""}
        </span>
      ))}
      {managed.length > 0 && <span class="faint">Filled by the runtime, refused from you: {managed.map((p) => p.name).join(", ")}</span>}
    </span>
  );
}

function Probe({
  report,
  tool,
  onTool,
}: {
  report: Extract<RuntimeReport, { ok: true }> | null;
  tool: string;
  onTool: (name: string) => void;
}) {
  const probe = useAppState((s) => s.runs.probe);
  const investigation = useAppState((s) => s.runs.investigation);
  const [prompt, setPrompt] = useState("");
  const [args, setArgs] = useState("{}");
  const [confirm, setConfirm] = useState(false);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [sending, setSending] = useState(false);

  const callable = (report?.tools ?? []).filter((t) => !t.isRuntimeControlTool && t.readOnly !== "declares_write");
  const selected = callable.find((t) => t.name === tool);
  const needsConfirm = selected !== undefined && selected.readOnly !== "declared";
  const busy = probe?.state === "running" || investigation?.state === "running";

  const submit = async (event: Event) => {
    event.preventDefault();
    const next: Record<string, string | null> = {};
    let parsedArgs: unknown = {};
    if (!prompt.trim()) next.prompt = "Describe the task; AgntID decides the shortlist from it.";
    if (selected) {
      try {
        parsedArgs = JSON.parse(args || "{}");
        if (parsedArgs === null || typeof parsedArgs !== "object" || Array.isArray(parsedArgs))
          next.args = "Arguments must be a JSON object.";
        else {
          const managed = Object.keys(parsedArgs).filter(isReservedArgument);
          if (managed.length) next.args = `${managed.join(", ")} is reserved for AgntID context, never filled by you. Remove it.`;
        }
      } catch {
        next.args = "Arguments are not valid JSON.";
      }
      if (needsConfirm && !confirm) next.confirm = "Confirm the call, or choose a tool that declares itself read-only.";
    }
    setErrors(next);
    if (Object.values(next).some(Boolean)) return;
    setFailure(null);
    setSending(true);
    const body: Record<string, unknown> = { prompt: prompt.trim() };
    if (selected) body.call = { tool: selected.name, arguments: parsedArgs, confirmUnannotated: needsConfirm && confirm };
    const result = await startRun("probe", "probe", body);
    setSending(false);
    if (!result.ok) setFailure(result.error);
  };

  const task = firstEvent(probe, "task");
  const call = lastEvent(probe, "call");
  const shortlist = task?.status === "ok" ? strings(task.detail.shortlist) : [];
  const probedTool = str(record(probe?.request.call)?.tool);

  return (
    <section class={styles.section} id="probe" aria-labelledby="probe-title">
      <div class={styles.sectionHead}>
        <h2 id="probe-title">Task probe</h2>
      </div>
      <p class="lede">
        Open one AgntID task for a prompt and see what the runtime shortlists. Optionally call one shortlisted tool, exactly once, through
        the same dispatcher PatchPilot uses for everything.
      </p>
      <div class={styles.probeGrid}>
        <form class={styles.probeForm} onSubmit={submit} noValidate>
          <TextArea
            id="probe-prompt"
            label="Task description sent to AgntID"
            value={prompt}
            onValue={setPrompt}
            rows={2}
            placeholder="Look up ticket 42"
            error={errors.prompt}
          />
          <SelectField
            id="probe-tool"
            label="Call a tool (optional)"
            value={selected ? selected.name : ""}
            onValue={(value) => {
              onTool(value);
              setConfirm(false);
            }}
            options={[{ value: "", label: "No call: show the shortlist only" }, ...callable.map((t) => ({ value: t.name, label: t.name }))]}
            hint={
              report
                ? "Tools that declare writes are not offered. Control tools are never called."
                : "Connect to the runtime to choose a tool."
            }
            disabled={!report}
          />
          {selected && (
            <>
              <TextArea
                id="probe-args"
                label="Arguments (JSON object)"
                value={args}
                onValue={setArgs}
                rows={4}
                mono
                error={errors.args}
                hint={<ArgumentHint tool={selected} />}
              />
              {needsConfirm && (
                <CheckField
                  id="probe-confirm"
                  checked={confirm}
                  onChecked={setConfirm}
                  hint={errors.confirm ?? "PatchPilot cannot tell what this tool changes. The runtime still evaluates the call."}
                >
                  <code>{selected.name}</code> does not declare itself read-only. Call it once anyway.
                </CheckField>
              )}
            </>
          )}
          <div class={styles.actionsRow}>
            <Button type="submit" variant="primary" busy={sending} disabled={busy}>
              {selected ? "Open task and call once" : "Open task"}
            </Button>
            {busy && <span class="muted">An AgntID task is already running.</span>}
            {probe && !busy && (
              <Button variant="quiet" onClick={() => clearRuns("probe")}>
                Clear probe
              </Button>
            )}
          </div>
          {failure && <ErrorNotice error={failure} title="The probe did not start" />}
        </form>

        <div class={styles.probeResult}>
          <Ledger run={probe} heading="Probe decisions" empty="No probe has run yet." />
          {task?.status === "ok" && (
            <div class={styles.shortlist}>
              <h3>This task's shortlist</h3>
              {shortlist.length ? (
                <ul>
                  {shortlist.map((name) => (
                    <li key={name} class={name === probedTool ? styles.chosen : undefined}>
                      <code>{name}</code>
                      {name === probedTool && <span class="note">the tool you asked for</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p class="muted">Empty: the runtime shortlisted no tool for this prompt, so every call would be refused.</p>
              )}
            </div>
          )}
          {call?.detail.decision === "allowed" && (
            <div class={styles.shortlist}>
              <h3>Result exactly as returned</h3>
              <JsonView value={call.detail.value} label="Tool result exactly as returned" />
            </div>
          )}
          {call && call.detail.decision !== "allowed" && str(call.detail.message) && (
            <p class={styles.callMessage}>{str(call.detail.message)}</p>
          )}
        </div>
      </div>
    </section>
  );
}
