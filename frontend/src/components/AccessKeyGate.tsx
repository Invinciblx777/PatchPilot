import { useState } from "preact/hooks";
import layout from "../layout/layout.module.css";
import { signIn } from "../state/store.ts";
import styles from "./AccessKeyGate.module.css";
import { Notice } from "./common.tsx";
import controls from "./controls.module.css";
import { Button } from "./controls.tsx";

/** Shown when the server runs in public mode and needs its access key. */
export function AccessKeyGate({ rejected }: { rejected: boolean }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: Event) => {
    event.preventDefault();
    const value = key.trim();
    if (!value) {
      setError("Enter the access key.");
      return;
    }
    setError(null);
    setBusy(true);
    await signIn(value);
    setBusy(false);
  };

  return (
    <div class={layout.page}>
      <h1 tabIndex={-1}>Sign in to PatchPilot</h1>
      <p class="lede">
        This PatchPilot server can apply patches and run tests on its operator's machine, so it asks for the access key the operator gave
        you.
      </p>
      {rejected && (
        <Notice tone="warning" title="That key was not accepted">
          <p>Check it with the operator and enter it again.</p>
        </Notice>
      )}
      <form class={styles.form} onSubmit={submit} noValidate>
        <div class={controls.field}>
          <div class={controls.labelRow}>
            <label class={controls.label} for="access-key">
              Access key
            </label>
          </div>
          <input
            id="access-key"
            type="password"
            class={`${controls.input} ${controls.mono}`}
            value={key}
            autocomplete="current-password"
            spellcheck={false}
            aria-invalid={error ? "true" : undefined}
            aria-describedby={error ? "access-key-hint access-key-error" : "access-key-hint"}
            onInput={(e) => setKey((e.currentTarget as HTMLInputElement).value)}
          />
          <p id="access-key-hint" class={controls.hint}>
            Kept in this browser tab only, and sent only to this site.
          </p>
          {error && (
            <p id="access-key-error" class={controls.error} role="alert">
              {error}
            </p>
          )}
        </div>
        <div class={styles.actions}>
          <Button type="submit" variant="primary" busy={busy}>
            Sign in
          </Button>
        </div>
      </form>
    </div>
  );
}
