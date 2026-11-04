/**
 * مغز دیجیتال X Agent — صحنه اصلی داشبورد.
 *
 * سیلوئت نیم‌رخ مغز با شیارهای اصلی + شبکه عصبی رویه‌ای (گره‌های داخل سیلوئت که به نزدیک‌ترین
 * همسایه‌ها وصل‌اند). ورودی‌ها (کارفرما، تلگرام، روتین‌ها، اجازه‌ها) از سمت شروع و خروجی‌ها (اقدام با
 * ابزار، میز کار، حافظه، کارمندان) از سمت پایان با خطوط نازک به مغز وصل می‌شوند. روی خطوطی که واقعاً
 * فعال‌اند (داده زنده)، پالس نور حرکت می‌کند. همه اعداد از API خود سیستم می‌آیند.
 */

import { useMemo, useSyncExternalStore } from "react";
import type { AgentState } from "./Silicon";

export type BrainLink = {
  key: string;
  title: string;
  value: string;
  active?: boolean;
  tone?: "normal" | "warn" | "bad";
  onClick?: () => void;
};

const W = 1000;
const H = 600;

// سیلوئت نیم‌رخ نیمکره مغز (لوب پیشانی به سمت x کوچک): پیشانی، آهیانه، پس‌سری و لوب گیجگاهی؛ اسپلاین بسته Catmull-Rom
const OUTLINE: [number, number][] = [
  [336, 334], [302, 286], [300, 228], [326, 176], [374, 136], [438, 110], [512, 98], [590, 102], [660, 122],
  [716, 160], [754, 214], [770, 274], [758, 326], [716, 356], [650, 372], [580, 390], [510, 402], [446, 396],
  [404, 372], [388, 346], [362, 352],
];
// مخچه: زیر لوب پس‌سری، جدا با چین‌های افقی
const CEREBELLUM: [number, number][] = [[632, 386], [690, 368], [748, 376], [778, 404], [770, 440], [728, 464], [672, 466], [634, 446], [618, 414]];
const FOLIA: [number, number][][] = [
  [[630, 398], [690, 384], [762, 394]], [[624, 414], [692, 400], [776, 412]], [[624, 430], [694, 418], [774, 428]],
  [[632, 446], [696, 436], [762, 446]], [[652, 460], [700, 452], [744, 458]],
];
const STEM: [number, number][] = [[594, 394], [588, 430], [578, 462], [566, 488]];
const STEM_B: [number, number][] = [[632, 404], [628, 436], [618, 466], [604, 492]];
const SULCI: [number, number][][] = [
  [[392, 346], [446, 330], [512, 312], [574, 300], [636, 278], [676, 246]], // شیار جانبی (سیلویوس)
  [[566, 100], [552, 150], [560, 196], [540, 246], [548, 290]], // شیار مرکزی (رولاندو)
  [[700, 142], [672, 182], [690, 220], [668, 262]], // شیار آهیانه‌ای-پس‌سری
];
// چین‌های شکنج (gyri): خطوط سینوسی که با clipPath داخل سیلوئت بریده می‌شوند
function gyri(): string[] {
  const out: string[] = [];
  let seed = 7;
  const r = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
  for (let row = 0; row < 11; row++) {
    const y0 = 130 + row * 26;
    let d = `M 290 ${y0}`;
    for (let x = 290; x < 790; x += 34) {
      const amp = 7 + r() * 9;
      d += ` q 8 ${row % 2 ? amp : -amp}, 17 0 t 17 0`;
    }
    out.push(d);
  }
  return out;
}

function spline(points: [number, number][], closed: boolean): string {
  const p = closed ? [points[points.length - 1], ...points, points[0], points[1]] : [points[0], ...points, points[points.length - 1]];
  let d = `M ${p[1][0]} ${p[1][1]}`;
  for (let i = 1; i < p.length - 2; i++) {
    const [x0, y0] = p[i - 1];
    const [x1, y1] = p[i];
    const [x2, y2] = p[i + 1];
    const [x3, y3] = p[i + 2];
    d += ` C ${x1 + (x2 - x0) / 6} ${y1 + (y2 - y0) / 6}, ${x2 - (x3 - x1) / 6} ${y2 - (y3 - y1) / 6}, ${x2} ${y2}`;
  }
  return closed ? `${d} Z` : d;
}

function inside(x: number, y: number, poly: [number, number][]): boolean {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

function rng(seed: number) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

function useMesh() {
  return useMemo(() => {
    const rand = rng(20261104);
    const pts: [number, number][] = [];
    for (let y = 112; y < 400; y += 28) {
      for (let x = 300; x < 780; x += 30) {
        const px = x + (rand() - 0.5) * 22;
        const py = y + (rand() - 0.5) * 20;
        if (inside(px, py, OUTLINE) && inside(px + 9, py, OUTLINE) && inside(px - 9, py, OUTLINE)) pts.push([px, py]);
      }
    }
    const edges = new Set<string>();
    pts.forEach((a, i) => {
      const near = pts
        .map((b, j) => [j, (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2] as const)
        .filter(([j]) => j !== i)
        .sort((m, n) => m[1] - n[1])
        .slice(0, 3);
      near.forEach(([j, dist]) => {
        if (dist < 48 ** 2) edges.add(i < j ? `${i}-${j}` : `${j}-${i}`);
      });
    });
    const hubs = new Set(pts.map((_, i) => i).filter((i) => (i * 7) % 11 === 0));
    return { pts, edges: [...edges].map((e) => e.split("-").map(Number) as [number, number]), hubs };
  }, []);
}

const reducedMotion = {
  subscribe(cb: () => void) {
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    m.addEventListener("change", cb);
    return () => m.removeEventListener("change", cb);
  },
  get: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
};

// نقاط اتصال روی لبه مغز (RTL: ورودی‌ها از راست/شروع، خروجی‌ها به چپ/پایان)
const IN_ANCHORS: [number, number][] = [[716, 160], [770, 274], [758, 326], [770, 410]];
const OUT_ANCHORS: [number, number][] = [[374, 136], [300, 228], [302, 286], [404, 372]];
const IN_LABEL_Y = [128, 240, 352, 464];
const OUT_LABEL_Y = [128, 240, 352, 464];
// محل گره برچسب‌ها (لبه داخلی برچسب، نزدیک مغز)
const IN_DOT = 826;
const OUT_DOT = 174;

function connector(from: [number, number], to: [number, number]): string {
  const mx = (from[0] + to[0]) / 2;
  return `M ${from[0]} ${from[1]} C ${mx} ${from[1]}, ${mx} ${to[1]}, ${to[0]} ${to[1]}`;
}

const TONE_STROKE = { normal: "rgb(var(--line-art))", warn: "rgb(255 205 120)", bad: "rgb(255 150 170)" };

export function DigitalBrain({ state, inputs, outputs }: { state: AgentState; inputs: BrainLink[]; outputs: BrainLink[] }) {
  const { pts, edges, hubs } = useMesh();
  const still = useSyncExternalStore(reducedMotion.subscribe, reducedMotion.get, () => true);
  const outline = useMemo(() => spline(OUTLINE, true), []);
  const stem = useMemo(() => `${spline(STEM, false)} ${spline(STEM_B, false)} M ${STEM[STEM.length - 1][0]} ${STEM[STEM.length - 1][1]} L ${STEM_B[STEM_B.length - 1][0]} ${STEM_B[STEM_B.length - 1][1]}`, []);
  const cerebellum = useMemo(() => spline(CEREBELLUM, true), []);
  const folia = useMemo(() => FOLIA.map((f) => spline(f, false)), []);
  const folds = useMemo(() => gyri(), []);
  const sulci = useMemo(() => SULCI.map((s) => spline(s, false)), []);
  const busy = state === "executing" || state === "thinking";
  const hubColor = state === "waiting" ? "rgb(255 205 120)" : state === "stopped" ? "rgb(255 150 170)" : "rgb(var(--glow))";
  const ins = inputs.slice(0, 4).map((l, i) => ({ ...l, d: connector([IN_DOT, IN_LABEL_Y[i]], IN_ANCHORS[i]) }));
  const outs = outputs.slice(0, 4).map((l, i) => ({ ...l, d: connector(OUT_ANCHORS[i], [OUT_DOT, OUT_LABEL_Y[i]]) }));

  return (
    <div className="relative w-full">
      {/* صحنه دسکتاپ: مغز + اتصال‌ها؛ برچسب‌ها HTML تا متن فارسی خوانا و قابل‌کلیک باشد */}
      <div className="relative mx-auto hidden aspect-[1000/600] w-full max-w-[1100px] md:block">
        <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 h-full w-full" aria-hidden>
          <defs>
            <radialGradient id="brain-glow" cx="54%" cy="48%" r="46%">
              <stop offset="0%" stopColor="rgb(var(--glow))" stopOpacity={busy ? 0.42 : 0.3} />
              <stop offset="70%" stopColor="rgb(var(--glow))" stopOpacity="0.06" />
              <stop offset="100%" stopColor="rgb(var(--glow))" stopOpacity="0" />
            </radialGradient>
            <linearGradient id="brain-fill" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#fff" stopOpacity="0.16" />
              <stop offset="100%" stopColor="#fff" stopOpacity="0.04" />
            </linearGradient>
          </defs>
          <ellipse cx="540" cy="290" rx="330" ry="240" fill="url(#brain-glow)" />
          {[...ins, ...outs].map((l) => (
            <path key={`c-${l.key}`} id={`c-${l.key}`} d={l.d} fill="none" stroke={TONE_STROKE[l.tone || "normal"]}
              strokeWidth="1.2" strokeOpacity={l.active ? 0.85 : 0.38} strokeDasharray={l.active ? undefined : "2 6"} strokeLinecap="round" />
          ))}
          <BrainBody outline={outline} cerebellum={cerebellum} folia={folia} stem={stem} sulci={sulci} folds={folds} id="d" scale={1} />
          <g stroke="rgb(var(--line-art))" strokeOpacity="0.26" strokeWidth="0.8">
            {edges.map(([a, b], i) => (
              <line key={i} x1={pts[a][0]} y1={pts[a][1]} x2={pts[b][0]} y2={pts[b][1]} />
            ))}
          </g>
          {pts.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={hubs.has(i) ? 3.4 : 1.9} fill={hubs.has(i) ? hubColor : "rgb(var(--line-art))"}
              style={still ? { opacity: hubs.has(i) ? 1 : 0.7 } : {
                animation: `node-twinkle ${busy ? 1.8 + (i % 5) * 0.3 : 4 + (i % 7) * 0.6}s ease-in-out infinite`,
                animationDelay: `${(i * 0.37) % 5}s`, ["--lo" as any]: hubs.has(i) ? 0.55 : 0.3,
              }} />
          ))}
          {[...ins, ...outs].map((l) => (
            <g key={`a-${l.key}`}>
              {!still && l.active && state !== "stopped" && [0, 1].map((k) => (
                <circle key={k} r="3.6" fill="#fff" opacity="0.95">
                  <animateMotion dur={`${2.4 + k * 0.4}s`} begin={`${k * 1.2}s`} repeatCount="indefinite" calcMode="linear">
                    <mpath href={`#c-${l.key}`} />
                  </animateMotion>
                </circle>
              ))}
            </g>
          ))}
          {[...IN_ANCHORS.slice(0, ins.length), ...OUT_ANCHORS.slice(0, outs.length)].map(([x, y], i) => (
            <circle key={`n-${i}`} cx={x} cy={y} r="4.5" fill="rgb(var(--stage-2))" stroke="#fff" strokeWidth="1.4" />
          ))}
        </svg>
        {ins.map((l, i) => (
          <Callout key={l.key} link={l} side="start" style={{ right: "1.2%", top: `${(IN_LABEL_Y[i] / H) * 100}%` }} />
        ))}
        {outs.map((l, i) => (
          <Callout key={l.key} link={l} side="end" style={{ left: "1.2%", top: `${(OUT_LABEL_Y[i] / H) * 100}%` }} />
        ))}
      </div>

      {/* موبایل: مغز فشرده و گره‌ها در دو ستون زیر آن */}
      <div className="md:hidden">
        <svg viewBox="280 80 510 460" className="mx-auto h-auto w-full max-w-[340px]" aria-hidden>
          <ellipse cx="540" cy="290" rx="280" ry="210" fill="url(#brain-glow-m)" />
          <defs>
            <radialGradient id="brain-glow-m" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgb(var(--glow))" stopOpacity="0.35" />
              <stop offset="100%" stopColor="rgb(var(--glow))" stopOpacity="0" />
            </radialGradient>
          </defs>
          <BrainBody outline={outline} cerebellum={cerebellum} folia={folia} stem={stem} sulci={sulci} folds={folds} id="m" scale={1.3} />
          <g stroke="rgb(var(--line-art))" strokeOpacity="0.24" strokeWidth="1">
            {edges.map(([a, b], i) => <line key={i} x1={pts[a][0]} y1={pts[a][1]} x2={pts[b][0]} y2={pts[b][1]} />)}
          </g>
          {pts.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={hubs.has(i) ? 4.4 : 2.4} fill={hubs.has(i) ? hubColor : "rgb(var(--line-art))"}
              style={still ? undefined : { animation: `node-twinkle ${busy ? 1.8 : 4.4}s ease-in-out infinite`, animationDelay: `${(i * 0.37) % 5}s`, ["--lo" as any]: 0.35 }} />
          ))}
        </svg>
        <div className="mt-2 grid grid-cols-2 gap-2">
          {[...inputs.slice(0, 4), ...outputs.slice(0, 4)].map((l) => (
            <button key={l.key} type="button" onClick={l.onClick} className="stage-glass flex min-w-0 items-start gap-2 rounded-2xl px-3 py-2.5 text-start">
              <Dot link={l} />
              <span className="min-w-0">
                <span className="block truncate text-[13px] font-semibold leading-5">{l.title}</span>
                <span className="num block truncate text-[12px] leading-5 text-white/85">{l.value}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function BrainBody({ outline, cerebellum, folia, stem, sulci, folds, id, scale }: {
  outline: string; cerebellum: string; folia: string[]; stem: string; sulci: string[]; folds: string[]; id: string; scale: number;
}) {
  const ink = "rgb(var(--line-art))";
  return (
    <g>
      <defs>
        <linearGradient id={`brain-fill-${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#fff" stopOpacity="0.17" />
          <stop offset="100%" stopColor="#fff" stopOpacity="0.04" />
        </linearGradient>
        <clipPath id={`brain-clip-${id}`}><path d={outline} /></clipPath>
        <clipPath id={`cb-clip-${id}`}><path d={cerebellum} /></clipPath>
      </defs>
      <path d={stem} fill="none" stroke={ink} strokeOpacity="0.55" strokeWidth={1.3 * scale} strokeLinecap="round" />
      <path d={cerebellum} fill="rgb(255 255 255 / .07)" stroke={ink} strokeOpacity="0.75" strokeWidth={1.4 * scale} />
      <g clipPath={`url(#cb-clip-${id})`}>
        {folia.map((d, i) => <path key={i} d={d} fill="none" stroke={ink} strokeOpacity="0.35" strokeWidth={1 * scale} />)}
      </g>
      <path d={outline} fill={`url(#brain-fill-${id})`} stroke={ink} strokeOpacity="0.9" strokeWidth={1.7 * scale} />
      <g clipPath={`url(#brain-clip-${id})`} fill="none" stroke={ink} strokeLinecap="round">
        {folds.map((d, i) => <path key={i} d={d} strokeOpacity="0.11" strokeWidth={1.1 * scale} />)}
        {sulci.map((d, i) => <path key={`s${i}`} d={d} strokeOpacity="0.5" strokeWidth={1.5 * scale} />)}
      </g>
    </g>
  );
}

function Dot({ link }: { link: BrainLink }) {
  const c = link.tone === "warn" ? "rgb(255 205 120)" : link.tone === "bad" ? "rgb(255 150 170)" : "#fff";
  return (
    <span className="relative grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border-[1.5px]" style={{ borderColor: c }}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: link.active ? c : "transparent" }} />
      {link.active && <span className="absolute inset-0 rounded-full" style={{ border: `1px solid ${c}`, animation: "node-pulse 1.8s ease-out infinite" }} />}
    </span>
  );
}

function Callout({ link, side, style }: { link: BrainLink; side: "start" | "end"; style: React.CSSProperties }) {
  // گره روی لبه داخلی برچسب (رو به مغز) تا خط اتصال دقیقاً به آن برسد
  return (
    <button type="button" onClick={link.onClick} style={style}
      className="group absolute flex w-[16.2%] min-w-[148px] -translate-y-1/2 items-center gap-2.5 text-start">
      {side === "end" && <Dot link={link} />}
      <span className={side === "end" ? "min-w-0 flex-1 text-end" : "min-w-0 flex-1"}>
        <span className="block text-[14px] font-semibold leading-6 group-hover:underline">{link.title}</span>
        <span className="num block text-[12.5px] leading-5 text-white/85">{link.value}</span>
      </span>
      {side === "start" && <Dot link={link} />}
    </button>
  );
}
