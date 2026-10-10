// Small hand-drawn marks. Each one carries meaning: an ink outline marks the
// panel that holds the evidence, crop marks frame primary work areas, the
// marker underline shows where you are, connectors join timeline stages.

import type { ComponentChildren, HTMLAttributes, RefObject } from "preact";
import { useLayoutEffect, useRef, useState } from "preact/hooks";
import styles from "./ink.module.css";
import { mulberry32, roughLine, roughRect, roughRectStrokes, seedFrom } from "./rough.ts";

function useSize<T extends HTMLElement>(): [RefObject<T | null>, { width: number; height: number }] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const width = Math.round(element.offsetWidth);
      const height = Math.round(element.offsetHeight);
      setSize((s) => (s.width === width && s.height === height ? s : { width, height }));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, size];
}

type InkBoxProps = HTMLAttributes<HTMLDivElement> & { seed: string; children: ComponentChildren };

/** A panel with an irregular, retraced ink outline. Reserved for the evidence panel. */
export function InkBox({ seed, children, class: className, ...rest }: InkBoxProps) {
  const [ref, { width, height }] = useSize<HTMLDivElement>();
  const pad = 3;
  const w = width + pad * 2;
  const h = height + pad * 2;
  const value = seedFrom(seed);
  return (
    <div {...rest} ref={ref} class={`${styles.inkBox} ${className ?? ""}`}>
      {width > 0 && (
        <svg class={styles.inkBoxLines} width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" focusable="false">
          <path d={roughRect(w, h, value, 3, 2.2, 5)} class={styles.inkStroke} />
          <path
            d={roughRectStrokes(w, h, value + 7, 4.5, 3, 6)
              .slice(0, 2)
              .join("")}
            class={styles.inkRetrace}
          />
        </svg>
      )}
      {children}
    </div>
  );
}

const CORNER_PATHS = {
  tl: "M1 13.2C1.3 9 1.1 5.1 1.2 1.1M1.4 1.3C5.2 1.1 9.3 1.4 13.4 1.2",
  tr: "M16.8 13.4C16.6 9.2 16.9 5.3 16.7 1.2M16.5 1.1C12.6 1.4 8.7 1.1 4.6 1.3",
  bl: "M1.2 4.6C1 8.8 1.3 12.7 1.1 16.8M1.3 16.7C5.3 16.9 9.1 16.6 13.3 16.8",
  br: "M16.9 4.7C16.6 8.9 16.8 12.6 16.7 16.9M16.6 16.8C12.7 16.6 8.8 16.9 4.7 16.7",
} as const;

/** Printer's crop marks around a primary work area. */
export function CropMarks() {
  return (
    <>
      {(Object.keys(CORNER_PATHS) as (keyof typeof CORNER_PATHS)[]).map((corner) => (
        <svg key={corner} class={`${styles.crop} ${styles[corner]}`} viewBox="0 0 18 18" aria-hidden="true" focusable="false">
          <path d={CORNER_PATHS[corner]} />
        </svg>
      ))}
    </>
  );
}

/** The orange marker stroke under the current navigation item. */
export function MarkerUnderline({ seed }: { seed: string }) {
  const rand = mulberry32(seedFrom(seed));
  const lift = 1.2 + rand() * 1.2;
  const d = `M2 ${(5 + rand()).toFixed(1)}C22 ${(4.2 - lift).toFixed(1)} 58 ${(3.4 + rand()).toFixed(1)} 98 ${(3 - lift / 2).toFixed(1)}`;
  return (
    <svg class={styles.underline} viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <path d={d} pathLength={1} />
    </svg>
  );
}

/** A vertical hand-drawn connector that fills its container's height. */
export function Connector({ seed, solid }: { seed: string; solid: boolean }) {
  const rand = mulberry32(seedFrom(seed));
  const d = roughLine(5, 0, 5 + (rand() - 0.5) * 2, 100, rand, 1.4, 0);
  return (
    <svg
      class={`${styles.connector} ${solid ? styles.connectorSolid : ""}`}
      viewBox="0 0 10 100"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <path d={d} vector-effect="non-scaling-stroke" />
    </svg>
  );
}

/** A small curved arrow that points from a margin note to what it explains. */
export function HandArrow({ flip = false }: { flip?: boolean }) {
  return (
    <svg class={`${styles.arrow} ${flip ? styles.flip : ""}`} viewBox="0 0 44 26" aria-hidden="true" focusable="false">
      <path d="M41.5 21.6C31.4 23.9 18.6 21.4 9.4 9.2" pathLength={1} />
      <path d="M15.6 9.9C12.4 9.1 9.9 8.6 7.6 6.2C8 9.4 8.3 12.1 7.4 15.4" pathLength={1} />
    </svg>
  );
}
