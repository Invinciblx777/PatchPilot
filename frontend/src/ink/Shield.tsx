// Original line-art shield for runtime protection. The fingerprint inside
// stands for agent identity. The badge in the corner appears only for a
// decision the runtime actually returned.

import styles from "./art.module.css";

export type ShieldState = "idle" | "allowed" | "denied" | "review" | "unknown" | "not_observed";

const BADGE: Record<Exclude<ShieldState, "idle">, string> = {
  allowed: "M51.6 62.4L55 65.8L61.2 58.6",
  denied: "M52.4 58.4L59.8 65.8M59.8 58.4L52.4 65.8",
  review: "M53.8 58.4V65.8M58.4 58.4V65.8",
  unknown: "M53.2 59.6C53.4 57.4 54.8 56.4 56.4 56.4C58.2 56.4 59.6 57.6 59.6 59.4C59.6 61.6 56.6 61.8 56.4 64",
  not_observed: "",
};

export function Shield({ size = 72, state = "idle", draw = true }: { size?: number; state?: ShieldState; draw?: boolean }) {
  const badge = state === "idle" ? null : state;
  return (
    <svg
      class={`${styles.art} ${draw ? styles.draw : ""}`}
      width={size}
      height={(size * 80) / 72}
      viewBox="0 0 72 80"
      aria-hidden="true"
      focusable="false"
    >
      <g class={styles.ink}>
        <path
          pathLength={1}
          d="M12.4 15.6C20.2 13.8 28.1 11.4 36 7.6C43.9 11.4 51.8 13.8 59.6 15.6C60.4 37.4 54.6 59.6 36 73.4C17.4 59.6 11.6 37.4 12.4 15.6Z"
        />
        <path
          pathLength={1}
          class={styles.retrace}
          d="M13.8 17.2C21.4 15.4 28.8 13 35.6 9.6M37.2 9.8C44.2 13.2 51 15.4 58.2 17.1C58.6 37 53.2 57.6 36.4 70.6"
        />
      </g>
      <g class={styles.pencil}>
        <path
          pathLength={1}
          d="M18.4 20.6C24.6 19.2 30.4 17.4 36 14.8C41.6 17.4 47.4 19.2 53.6 20.6C53.8 38.6 49.2 55.6 36 66.2C22.8 55.6 18.2 38.6 18.4 20.6Z"
        />
        <path
          pathLength={1}
          d="M47.6 24.6L51.6 21.4M48.4 29.8L52.6 26M48.6 35.2L52.8 31.2M48 40.8L52 36.8M46.8 46.4L50.6 42.4M45 51.8L48.6 48M42.6 56.8L45.8 53.2"
        />
      </g>
      <g class={styles.ink}>
        <path pathLength={1} d="M23.4 42C24.8 32.2 29.6 26.4 36.2 26.2C42.4 26 47.2 30.8 48.6 38.4" />
        <path pathLength={1} d="M26.6 47.8C25.8 37.2 29.4 30.6 36 30.4C42.6 30.2 46.4 36.4 45.8 45.2" />
        <path pathLength={1} d="M30.2 50.6C29.4 41.6 31.6 35 36 34.8C40.4 34.6 42.4 40.4 41.8 47.8" />
        <path pathLength={1} d="M33.8 52.6C33.4 45.4 34.2 39.4 36 39.2C37.8 39 38.4 44.2 38 50.4" />
      </g>
      {badge && (
        <g class={`${styles.badge} ${styles[badge]}`}>
          <circle cx="56.4" cy="62" r="10" class={badge === "not_observed" ? styles.dashed : ""} />
          {BADGE[badge] && <path pathLength={1} d={BADGE[badge]} />}
          {badge === "unknown" && <circle cx="56.4" cy="67.2" r="0.9" class={styles.dot} />}
        </g>
      )}
    </svg>
  );
}
