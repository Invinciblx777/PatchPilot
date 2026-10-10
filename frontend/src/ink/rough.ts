// Seeded, hand-drawn path geometry. The same seed always draws the same
// wobble, so an outline does not shimmer between renders.

export type Rand = () => number;

export function mulberry32(seed: number): Rand {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function seedFrom(text: string): number {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

const f = (n: number) => (Math.round(n * 10) / 10).toString();

/** One stroke from (x1,y1) to (x2,y2) that wobbles and slightly over- or under-shoots its ends. */
export function roughLine(x1: number, y1: number, x2: number, y2: number, rand: Rand, amp = 1, overshoot = 2): string {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  const nx = -uy;
  const ny = ux;
  const jitter = () => (rand() - 0.5) * 2 * amp;
  const start = (rand() - 0.35) * overshoot;
  const end = (rand() - 0.35) * overshoot;
  const sx = x1 - ux * start + nx * jitter() * 0.5;
  const sy = y1 - uy * start + ny * jitter() * 0.5;
  const ex = x2 + ux * end + nx * jitter() * 0.5;
  const ey = y2 + uy * end + ny * jitter() * 0.5;
  const c1x = x1 + dx * 0.32 + nx * jitter();
  const c1y = y1 + dy * 0.32 + ny * jitter();
  const c2x = x1 + dx * 0.68 + nx * jitter();
  const c2y = y1 + dy * 0.68 + ny * jitter();
  return `M${f(sx)} ${f(sy)}C${f(c1x)} ${f(c1y)} ${f(c2x)} ${f(c2y)} ${f(ex)} ${f(ey)}`;
}

/** A box drawn as four separate strokes whose corners do not quite meet. */
export function roughRectStrokes(width: number, height: number, seed: number, inset = 2, amp = 0.9, overshoot = 3): string[] {
  const rand = mulberry32(seed);
  const l = inset;
  const t = inset;
  const r = width - inset;
  const b = height - inset;
  return [
    roughLine(l, t, r, t, rand, amp, overshoot),
    roughLine(r, t, r, b, rand, amp, overshoot),
    roughLine(r, b, l, b, rand, amp, overshoot),
    roughLine(l, b, l, t, rand, amp, overshoot),
  ];
}

export function roughRect(width: number, height: number, seed: number, inset = 2, amp = 0.9, overshoot = 3): string {
  return roughRectStrokes(width, height, seed, inset, amp, overshoot).join("");
}

/** A circle drawn in one loop that overlaps itself a little where the pen lifts. */
export function roughCircle(cx: number, cy: number, radius: number, seed: number, amp = 0.05): string {
  const rand = mulberry32(seed);
  const start = rand() * Math.PI * 2;
  const sweep = Math.PI * 2 + 0.45;
  const steps = 9;
  const points: [number, number][] = [];
  for (let i = 0; i <= steps; i += 1) {
    const angle = start + (sweep * i) / steps;
    const rr = radius * (1 + (rand() - 0.5) * 2 * amp) * (i === steps ? 1.04 : 1);
    points.push([cx + Math.cos(angle) * rr, cy + Math.sin(angle) * rr]);
  }
  const [first, ...rest] = points;
  if (!first) return "";
  let path = `M${f(first[0])} ${f(first[1])}`;
  for (let i = 0; i < rest.length - 1; i += 1) {
    const p = rest[i];
    const q = rest[i + 1];
    if (!p || !q) continue;
    path += `Q${f(p[0])} ${f(p[1])} ${f((p[0] + q[0]) / 2)} ${f((p[1] + q[1]) / 2)}`;
  }
  const last = rest.at(-1);
  if (last) path += `L${f(last[0])} ${f(last[1])}`;
  return path;
}
