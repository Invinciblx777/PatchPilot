// Original drawings for empty states, plus the PatchPilot mark and the
// decision stamp. Decorative only: every one is aria-hidden and sits next to
// text that says the same thing.

import styles from "./art.module.css";

/** The bug waiting to be investigated: issue intake has nothing loaded yet. */
export function Bug({ size = 150 }: { size?: number }) {
  return (
    <svg
      class={`${styles.art} ${styles.draw}`}
      width={size}
      height={(size * 132) / 160}
      viewBox="0 0 160 132"
      aria-hidden="true"
      focusable="false"
    >
      <g class={styles.ink}>
        <path pathLength={1} d="M80 40C103 40 117 58 117 80C117 104 100 120 80 120C60 120 43 104 43 80C43 58 57 40 80 40Z" />
        <path pathLength={1} d="M80.2 52C79.4 70 79.8 92 80.2 118.4" />
        <path pathLength={1} d="M58 50.4C66 45.8 94 45.8 102 50.4" />
        <path pathLength={1} d="M64 42C63.4 31.4 70.4 24.6 80 24.4C89.6 24.2 96.6 31 96 42" />
        <circle pathLength={1} cx="72.2" cy="33.4" r="6.2" />
        <circle pathLength={1} cx="88.4" cy="32.6" r="6.6" />
        <path pathLength={1} d="M73 25.4C69.2 15.6 62.6 10.6 55.4 11.2C52.2 11.6 51.2 14.6 53.4 16.2" />
        <path pathLength={1} d="M87.4 24.6C91.8 14.8 98.8 10.4 105.6 11.6C108.6 12.2 109 15.2 106.6 16.6" />
        <path pathLength={1} d="M46.2 66L33.4 59.4L26.6 63.8M43.4 82.4L29.4 82L22.8 88.4M47.8 99.6L36.6 106.4L32.6 115" />
        <path pathLength={1} d="M113.8 66L126.6 59.4L133.4 63.8M116.6 82.4L130.6 82L137.2 88.4M112.2 99.6L123.4 106.4L127.4 115" />
        <path pathLength={1} d="M66.6 24.8C69.2 22.8 72.6 22.2 75.6 23M84.6 22C87.8 21.2 91.2 21.8 94 23.8" />
      </g>
      <circle cx="74.4" cy="31.6" r="2.4" class={styles.fill} />
      <circle cx="90.8" cy="30.6" r="2.6" class={styles.fill} />
      <g class={styles.pencil}>
        <path pathLength={1} d="M54.6 70.4C56.4 64.2 60.6 59.4 66.4 57" />
        <path
          pathLength={1}
          d="M101 66.6L106.4 61.6M103.2 74.8L109.4 69.2M104 83.4L110.6 77.4M103.2 92.2L109.4 86.6M100.6 100.6L106 95.6M96.4 107.8L101 103.6"
        />
        <path
          pathLength={1}
          d="M110.4 30.4C112.6 33.6 113.6 36 112.8 38.2C112 40.2 109 40.4 108.2 38.4C107.6 36.4 108.6 33.6 110.4 30.4Z"
        />
      </g>
    </svg>
  );
}

/** A branch running off to a dashed, throwaway worktree. */
export function BranchSketch({ size = 150 }: { size?: number }) {
  return (
    <svg
      class={`${styles.art} ${styles.draw}`}
      width={size}
      height={(size * 110) / 160}
      viewBox="0 0 160 110"
      aria-hidden="true"
      focusable="false"
    >
      <g class={styles.ink}>
        <path pathLength={1} d="M30 8C30.6 40 29.6 70 30.4 102" />
        <circle pathLength={1} cx="30" cy="24" r="5" />
        <circle pathLength={1} cx="30.2" cy="52" r="5" />
        <circle pathLength={1} cx="30" cy="84" r="5" />
        <path pathLength={1} d="M35 54C52 58 62 68 71.6 75" />
      </g>
      <g class={styles.pencil}>
        <path
          class={styles.dashedLine}
          d="M74 62.6C94 62.2 116 62.8 136.4 62.4C136.8 72 136.2 82 136.6 91.6C116 92 94.2 91.4 73.8 91.8C74.2 82 73.6 72.2 74 62.6Z"
        />
      </g>
    </svg>
  );
}

/** A ruled ledger page with a pen: the activity log is empty. */
export function LedgerSketch({ size = 140 }: { size?: number }) {
  return (
    <svg
      class={`${styles.art} ${styles.draw}`}
      width={size}
      height={(size * 110) / 150}
      viewBox="0 0 150 110"
      aria-hidden="true"
      focusable="false"
    >
      <g class={styles.ink}>
        <path
          pathLength={1}
          d="M28 10C62 9.6 96 10.4 122.4 10C122.8 42 122.2 74 122.6 100C96 100.4 62 99.6 28.4 100C28.8 74 28.2 42 28 10Z"
        />
        <path pathLength={1} d="M118 82.6L134.6 99.2L130.6 103.2L114 86.6Z M114 86.6L118 82.6" />
      </g>
      <g class={styles.pencil}>
        <path pathLength={1} d="M44 12C44.4 40 43.8 70 44.2 98" />
        <path
          pathLength={1}
          d="M50 28C70 27.6 92 28.4 112 28M50 40C70 39.6 92 40.4 112 40M50 52C70 51.6 92 52.4 112 52M50 64C70 63.6 92 64.4 104 64"
        />
      </g>
    </svg>
  );
}

/** PatchPilot mark: a stitched patch with a loose orange thread. */
export function Mark({ size = 26 }: { size?: number }) {
  return (
    <svg class={styles.mark} width={size} height={size} viewBox="0 0 26 26" aria-hidden="true" focusable="false">
      <g transform="rotate(-8 12 13)">
        <path
          class={styles.markEdge}
          d="M4.6 5.4C9.6 5.2 14.6 5.6 19.4 5.4C19.6 10.4 19.4 15.4 19.6 20.4C14.6 20.6 9.6 20.4 4.4 20.6C4.6 15.6 4.4 10.6 4.6 5.4Z"
        />
        <path class={styles.markStitch} d="M7.6 8.4H16.4V17.4H7.6Z" />
      </g>
      <path class={styles.markThread} d="M19.2 4.6C20.8 3.2 22.4 2.6 23.6 3.4C24.6 4.1 24 5.6 22.6 6" />
    </svg>
  );
}

export type StampKind = "allowed" | "denied" | "review";
const STAMP_TEXT: Record<StampKind, string> = { allowed: "Allowed", denied: "Denied", review: "In review" };

/** A rubber stamp for a decision the AgntID runtime actually returned. */
export function Stamp({ kind }: { kind: StampKind }) {
  return (
    <svg class={`${styles.stamp} ${styles[kind]}`} viewBox="0 0 136 46" aria-hidden="true" focusable="false">
      <g filter="url(#pp-stamp-ink)">
        <rect x="2" y="2" width="132" height="42" rx="6" class={styles.stampOuter} />
        <rect x="6.5" y="6.5" width="123" height="33" rx="3.5" class={styles.stampInner} />
        <text x="68" y="29.5" text-anchor="middle" class={styles.stampText}>
          {STAMP_TEXT[kind]}
        </text>
      </g>
    </svg>
  );
}

/** Shared SVG filter definitions; rendered once at the app root. */
export function InkDefs() {
  return (
    <svg class={styles.defs} width="0" height="0" aria-hidden="true" focusable="false">
      <defs>
        <filter id="pp-stamp-ink" x="-4%" y="-8%" width="108%" height="116%">
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="11" result="noise" />
          <feColorMatrix in="noise" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -2.2 1.6" result="speckle" />
          <feComposite in="SourceGraphic" in2="speckle" operator="in" result="speckled" />
          <feDisplacementMap in="speckled" in2="noise" scale="1.2" xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </defs>
    </svg>
  );
}
