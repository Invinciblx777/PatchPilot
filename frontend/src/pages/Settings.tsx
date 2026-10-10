import { ErrorNotice, Fact, FactGrid, Notice } from "../components/common.tsx";
import { StatusMark } from "../components/StatusMark.tsx";
import layout from "../layout/layout.module.css";
import { shortSha } from "../lib/format.ts";
import { type Motion, setMotion, useAppState } from "../state/store.ts";
import styles from "./pages.module.css";

const MOTION: { value: Motion; label: string; hint: string }[] = [
  { value: "system", label: "Follow the system setting", hint: "Uses your operating system's reduced-motion preference." },
  { value: "reduce", label: "Reduced", hint: "No drawing, stamping, or spinning animations." },
  { value: "full", label: "Full", hint: "Ink draws in and decisions are stamped." },
];

export function Settings() {
  const config = useAppState((s) => s.config);
  const configError = useAppState((s) => s.configError);
  const motion = useAppState((s) => s.motion);

  return (
    <div class={layout.page}>
      <header class={styles.pageHead}>
        <h1>Settings</h1>
        <p class="lede">
          PatchPilot reads these from its startup flags and environment. They cannot be changed from the browser, so a web page can never
          widen what PatchPilot may do.
        </p>
      </header>

      {configError && <ErrorNotice error={configError} title="The configuration could not be read" />}

      {config && (
        <>
          <section class={styles.section} aria-labelledby="agntid-title">
            <h2 id="agntid-title">AgntID connection</h2>
            {config.agntid.ok ? (
              <FactGrid>
                <Fact label="Endpoint">
                  <code>{config.agntid.endpoint}</code>
                </Fact>
                <Fact label="Access token">{config.agntid.accessTokenSupplied ? "Supplied (never shown)" : "Not supplied"}</Fact>
                <Fact label="Timeout">{config.agntid.timeoutSeconds} s for connect, list, and task open</Fact>
                <Fact label="AgntID SDK">{config.sdkVersion}</Fact>
              </FactGrid>
            ) : (
              <Notice tone="warning" title="Not configured">
                <p>{config.agntid.error.message}</p>
              </Notice>
            )}
            <FactGrid>
              <Fact label=".env file">
                {config.envFile ? <code>{config.envFile}</code> : "None"}{" "}
                <span class="muted">{config.envFileFound ? "(found)" : "(not found)"}</span>
              </Fact>
              <Fact label="Where each value came from">
                {config.sources.length ? (
                  <span class={styles.sourceList}>
                    {config.sources.map((s) => (
                      <code key={s}>{s}</code>
                    ))}
                  </span>
                ) : (
                  <span class="muted">No AgntID or PatchPilot values are set</span>
                )}
              </Fact>
            </FactGrid>
          </section>

          <section class={styles.section} aria-labelledby="identity-title">
            <h2 id="identity-title">Task identity</h2>
            {config.identity.ok ? (
              <FactGrid>
                <Fact label="Agent ID">
                  <code>{config.identity.agentId}</code>
                </Fact>
                <Fact label="User ID">
                  <code>{config.identity.userId}</code>
                </Fact>
              </FactGrid>
            ) : (
              <Notice tone="warning" title="Task identity is not usable">
                <p>{config.identity.error.message}</p>
                <p>
                  Set <code>PATCHPILOT_AGENT_ID</code> and <code>PATCHPILOT_USER_ID</code> in the environment or the .env file.
                </p>
              </Notice>
            )}
          </section>

          <section class={styles.section} aria-labelledby="map-title">
            <h2 id="map-title">GitHub capability map</h2>
            {!config.capabilityMap.configured && <p class="muted">None registered. GitHub reads are off.</p>}
            {config.capabilityMap.configured && !config.capabilityMap.ok && (
              <Notice tone="warning" title={`Could not load ${config.capabilityMap.path}`}>
                <p>{config.capabilityMap.error}</p>
              </Notice>
            )}
            {config.capabilityMap.configured && config.capabilityMap.ok && (
              <>
                <p>
                  <code>{config.capabilityMap.path}</code>
                </p>
                <ul class={styles.plainList}>
                  {Object.entries(config.capabilityMap.capabilities).map(([cap, mapping]) => (
                    <li key={cap}>
                      <code>{cap}</code> uses <code>{mapping?.tool}</code>{" "}
                      <span class="muted">(schema {shortSha(mapping?.schemaSha256, 12)})</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>

          <section class={styles.section} aria-labelledby="repos-settings-title">
            <h2 id="repos-settings-title">Repositories PatchPilot may patch</h2>
            {config.repos.length ? (
              <ul class={styles.plainList}>
                {config.repos.map((r) => (
                  <li key={r.id}>
                    <strong>{r.name}</strong> <code class="muted">{r.path}</code>
                  </li>
                ))}
              </ul>
            ) : (
              <p class="muted">None. Add one with --repo PATH.</p>
            )}
          </section>

          <section class={styles.section} aria-labelledby="tests-title">
            <h2 id="tests-title">Test commands</h2>
            {config.tests.length ? (
              <>
                <ul class={styles.plainList}>
                  {config.tests.map((t) => (
                    <li key={t.name}>
                      <strong>{t.name}</strong> <code>{t.display}</code>{" "}
                      <span class="muted">
                        (timeout {t.timeoutSeconds} s{t.passEnv.length ? `, passes ${t.passEnv.join(", ")}` : ""})
                      </span>
                    </li>
                  ))}
                </ul>
                <p class="muted">
                  Commands run with shell=False in the worktree, with a minimal environment and a temporary HOME. They have your user
                  permissions and network access: this is not a sandbox.
                </p>
              </>
            ) : (
              <p class="muted">None. Add them with --test-command or --test-config.</p>
            )}
          </section>

          <section class={styles.section} aria-labelledby="policy-title">
            <h2 id="policy-title">Patch path policy</h2>
            <p class="muted">Patches touching these paths are refused unless an exception was given at startup.</p>
            <p class={styles.patternList}>
              {config.patchPolicy.sensitivePatterns.map((p) => (
                <code key={p}>{p}</code>
              ))}
            </p>
            {config.patchPolicy.allowedSensitivePatterns.length > 0 && (
              <StatusMark state="warning" label={`Exceptions: ${config.patchPolicy.allowedSensitivePatterns.join(", ")}`} />
            )}
          </section>
        </>
      )}

      <section class={styles.section} aria-labelledby="motion-title">
        <h2 id="motion-title">Interface</h2>
        <fieldset class={styles.radios}>
          <legend>Motion</legend>
          {MOTION.map((m) => (
            <label key={m.value} class={styles.radio}>
              <input type="radio" name="motion" value={m.value} checked={motion === m.value} onChange={() => setMotion(m.value)} />
              <span>
                <span class={styles.radioLabel}>{m.label}</span>
                <span class="muted">{m.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <p class="muted">Stored in this browser only.</p>
      </section>

      <section class={styles.section} aria-labelledby="change-title">
        <h2 id="change-title">How to change these</h2>
        <pre class={styles.command}>{`patchpilot-ui --repo ~/src/project \\
  --test-command "python -m pytest -q" \\
  --mapping capabilities.json`}</pre>
        <p class="muted">
          AgntID settings live in the environment or the .env file: <code>AGNTID_MCP_URL</code>, <code>AGNTID_ACCESS_TOKEN</code>,{" "}
          <code>PATCHPILOT_AGENT_ID</code>, <code>PATCHPILOT_USER_ID</code>. They are read again for every operation.
        </p>
      </section>
    </div>
  );
}
