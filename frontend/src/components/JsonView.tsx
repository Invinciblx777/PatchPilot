import styles from "./JsonView.module.css";

/** A value exactly as the backend returned it, pretty-printed. */
export function JsonView({ value, label }: { value: unknown; label: string }) {
  return (
    <figure class={styles.figure}>
      <figcaption class="visually-hidden">{label}</figcaption>
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: scrollable content must be reachable by keyboard */}
      <pre class={styles.json} tabIndex={0}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </figure>
  );
}
