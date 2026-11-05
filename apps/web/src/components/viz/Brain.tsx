/**
 * هسته عصبی X Agent — صحنه اصلی داشبورد.
 *
 * یک مغز «ماتریس نقطه‌ای» از بالا: دو نیمکره از شبکه شش‌ضلعی نقاط که اندازه هر نقطه از عمق حجم و یک
 * میدان موج ملایم (شکنج‌های انتزاعی) می‌آید؛ بین دو نیمکره «گذرگاه داده» (جسم پینه‌ای) با خطوط افقی کوتاه.
 * ورودی‌ها (کارفرما، تلگرام، روتین‌ها، اجازه‌ها) با مسیرهای مدار چاپی صاف (افقی + ۴۵°) به نیمکره شروع وصل
 * می‌شوند و خروجی‌ها (ابزار، میز کار، حافظه، کارمندان) از نیمکره پایان خارج می‌شوند. هر اتصال فعال ناحیه
 * خودش را روشن می‌کند و بسته‌های داده روی آن حرکت می‌کنند؛ نوار اسکن از ورودی به خروجی می‌گذرد.
 *
 * کارایی: همه نقاط هر لایه یک <path> واحدند؛ پویانمایی فقط روی ماسک (یک نوار + چند هاله) است.
 * همه اعداد برچسب‌ها از API خود سیستم می‌آیند.
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
const CX = 500;
const CY = 300;
const GAP = 11; // نیم‌پهنای شیار بین دو نیمکره
const A = 122; // نیم‌پهنای هر نیمکره
const B = 212; // نیم‌ارتفاع (مغز از بالا کشیده‌تر از پهنا)
const STEP = 10.5;

type Dot = { x: number; y: number; r: number };

// شکل نیمکره: ابربیضی با لبه بیرونی گرد (p=2.2) و لبه کنار شیار تخت (p=4.5)؛ لوب پیشانی (بالا) کمی باریک‌تر و پس‌سری پهن‌تر
function hemi(x: number, y: number, side: 1 | -1): number {
  const cx = CX + side * (GAP + A);
  const v = (y - CY) / B;
  const a = A * (1 + 0.12 * v);
  const u = (x - cx) / a;
  const p = side * u > 0 ? 2.2 : 4.5;
  return Math.abs(u) ** p + Math.abs(v) ** 2.1;
}

// میدان شکنج: خطوط هم‌تراز یک موج درهم (شبیه چین‌های قشر مغز، ولی کاملاً رقمی)
function gyrus(x: number, y: number): number {
  return Math.sin(0.083 * x + 2.3 * Math.sin(0.027 * y + 0.4)) + Math.sin(0.067 * y + 1.8 * Math.sin(0.051 * x))
    + 0.6 * Math.sin(0.04 * (x + y) + 1.1);
}

function useDots() {
  return useMemo(() => {
    const dots: Dot[] = [];
    const rowH = STEP * 0.866;
    let row = 0;
    for (let y = CY - B; y <= CY + B; y += rowH, row++) {
      const off = row % 2 ? STEP / 2 : 0;
      for (let x = CX - GAP - 2 * A - STEP; x <= CX + GAP + 2 * A + STEP; x += STEP) {
        const px = x + off;
        const side: 1 | -1 = px >= CX ? 1 : -1;
        if (Math.abs(px - CX) < GAP * 0.55) continue;
        const f = hemi(px, y, side);
        if (f > 1) continue;
        const depth = Math.sqrt(1 - f); // ۰ لبه … ۱ مرکز حجم
        const ridge = Math.abs(gyrus(px, y)) < 0.4; // روی خط شکنج
        const r = ridge ? 1.35 + depth * 1.05 : 0.62 + depth * 0.38;
        dots.push({ x: px, y, r });
      }
    }
    const path = (k: number, pred: (d: Dot) => boolean = () => true) =>
      dots.filter(pred).map(({ x, y, r }) => {
        const rr = (r * k).toFixed(2);
        return `M${(x - r * k).toFixed(1)} ${y.toFixed(1)}a${rr} ${rr} 0 1 0 ${(2 * r * k).toFixed(2)} 0a${rr} ${rr} 0 1 0 ${(-2 * r * k).toFixed(2)} 0`;
      }).join("");
    const ridge = (d: Dot) => d.r > 1.3;
    return {
      d: { all: path(1), ridge: path(1, ridge) },
      m: { all: path(1.45), ridge: path(1.45, ridge) },
    };
  }, []);
}

const mobileQuery = {
  subscribe(cb: () => void) {
    const m = window.matchMedia("(max-width: 767px)");
    m.addEventListener("change", cb);
    return () => m.removeEventListener("change", cb);
  },
  get: () => window.matchMedia("(max-width: 767px)").matches,
};

const reducedMotion = {
  subscribe(cb: () => void) {
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    m.addEventListener("change", cb);
    return () => m.removeEventListener("change", cb);
  },
  get: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
};

// لبه بیرونی هر نیمکره در ارتفاع y (برای نقطه اتصال دقیق مسیرها)
function edgeX(y: number, side: 1 | -1): number {
  let lo = 0;
  let hi = 2 * A + 40;
  for (let k = 0; k < 24; k++) {
    const mid = (lo + hi) / 2;
    if (hemi(CX + side * (GAP + mid), y, side) <= 1) lo = mid;
    else hi = mid;
  }
  return CX + side * (GAP + lo);
}

const LABEL_Y = [140, 247, 353, 460];
const ANCHOR_Y = [178, 258, 342, 422];
const IN_DOT = 842; // گره برچسب‌های ورودی (سمت شروع/راست)
const OUT_DOT = 158; // گره برچسب‌های خروجی (سمت پایان/چپ)

// مسیر مدار چاپی: افقی → ۴۵° → افقی، با گوشه‌های نرم کوچک
function trace(x0: number, y0: number, x1: number, y1: number): string {
  const dy = y1 - y0;
  const dir = Math.sign(x1 - x0);
  const run = Math.abs(dy);
  const straight = (Math.abs(x1 - x0) - run) / 2;
  const xa = x0 + dir * Math.max(12, straight);
  const xb = xa + dir * run;
  return `M ${x0} ${y0} H ${xa} L ${xb} ${y1} H ${x1}`;
}

const TONE = { normal: "rgb(var(--line-art))", warn: "rgb(255 205 120)", bad: "rgb(255 150 170)" };

export function DigitalBrain({ state, inputs, outputs }: { state: AgentState; inputs: BrainLink[]; outputs: BrainLink[] }) {
  const dots = useDots();
  const still = useSyncExternalStore(reducedMotion.subscribe, reducedMotion.get, () => true);
  const mobile = useSyncExternalStore(mobileQuery.subscribe, mobileQuery.get, () => false);
  const busy = state === "executing" || state === "thinking";
  const accent = state === "waiting" ? "rgb(255 205 120)" : state === "stopped" ? "rgb(255 150 170)" : "rgb(var(--glow))";
  const ins = inputs.slice(0, 4).map((l, i) => {
    const ax = edgeX(ANCHOR_Y[i], 1);
    return { ...l, ax, ay: ANCHOR_Y[i], d: trace(IN_DOT, LABEL_Y[i], ax + 7, ANCHOR_Y[i]) };
  });
  const outs = outputs.slice(0, 4).map((l, i) => {
    const ax = edgeX(ANCHOR_Y[i], -1);
    return { ...l, ax, ay: ANCHOR_Y[i], d: trace(ax - 7, ANCHOR_Y[i], OUT_DOT, LABEL_Y[i]) };
  });
  const links = [...ins, ...outs];
  const sweep = busy ? 2.6 : 6.5;

  const scene = (id: "d" | "m", motion: boolean) => (
    <>
      <defs>
        <radialGradient id={`bloom-${id}`} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgb(var(--glow))" stopOpacity={busy ? 0.34 : 0.22} />
          <stop offset="100%" stopColor="rgb(var(--glow))" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`band-${id}`} x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset="0.5" stopColor="#fff" stopOpacity="1" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <radialGradient id={`spot-${id}`} cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#fff" stopOpacity="1" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
        {/* ماسک روشن‌شدن: نوار اسکن (شروع→پایان) + هاله ناحیه هر اتصال فعال */}
        <mask id={`lit-${id}`} maskUnits="userSpaceOnUse" x="0" y="0" width={W} height={H}>
          {motion && state !== "stopped" && (
            <rect y={CY - B - 10} width="150" height={2 * B + 20} fill={`url(#band-${id})`}>
              <animate attributeName="x" values={`${CX + GAP + 2 * A};${CX - GAP - 2 * A - 150}`} dur={`${sweep}s`} repeatCount="indefinite" />
            </rect>
          )}
          {links.filter((l) => l.active).map((l, i) => (
            <circle key={l.key} cx={l.ax + (l.ax > CX ? -52 : 52)} cy={l.ay} r="84" fill={`url(#spot-${id})`} opacity="0.75">
              {motion && <animate attributeName="opacity" values="0.35;0.9;0.35" dur={`${busy ? 1.8 : 3.6}s`} begin={`${i * 0.45}s`} repeatCount="indefinite" />}
            </circle>
          ))}
        </mask>
      </defs>
      <ellipse cx={CX} cy={CY} rx={2 * A + GAP + 20} ry={B + 16} fill={`url(#bloom-${id})`} />
      {/* لایه پایه: میدان نقاط ریز + خطوط شکنج درشت‌تر */}
      <path d={dots[id].all} fill="rgb(var(--line-art))" fillOpacity={state === "stopped" ? 0.2 : 0.3} />
      <path d={dots[id].ridge} fill="rgb(var(--line-art))" fillOpacity={state === "stopped" ? 0.3 : 0.62} />
      {/* لایه روشن (فقط زیر ماسک) */}
      <path d={dots[id].all} fill={accent} mask={`url(#lit-${id})`} />
      {/* گذرگاه داده بین نیمکره‌ها */}
      <g stroke="rgb(var(--line-art))" strokeLinecap="round">
        <line x1={CX} y1={CY - B + 40} x2={CX} y2={CY + B - 40} strokeOpacity="0.22" strokeWidth="1" />
        {[-2, -1, 0, 1, 2].map((k) => (
          <line key={k} x1={CX - GAP + 3} x2={CX + GAP - 3} y1={CY + k * 34} y2={CY + k * 34} strokeOpacity="0.5" strokeWidth="1.4" />
        ))}
      </g>
      {motion && busy && [-2, -1, 0, 1, 2].map((k) => (
        <rect key={`bus-${k}`} x={CX + GAP - 4} y={CY + k * 34 - 1.2} width="6" height="2.4" rx="1.2" fill={accent}>
          <animate attributeName="x" values={`${CX + GAP - 4};${CX - GAP - 2}`} dur="0.9s" begin={`${(k + 2) * 0.17}s`} repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;1;0" dur="0.9s" begin={`${(k + 2) * 0.17}s`} repeatCount="indefinite" />
        </rect>
      ))}
    </>
  );

  return (
    <div className="relative w-full">
      {/* دسکتاپ: هسته + مسیرها؛ برچسب‌ها HTML تا متن فارسی خوانا و قابل‌کلیک باشد */}
      {!mobile && <div className="relative mx-auto aspect-[1000/600] w-full max-w-[1100px]">
        <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 h-full w-full" aria-hidden>
          {scene("d", !still)}
          {links.map((l) => (
            <path key={`t-${l.key}`} id={`t-${l.key}`} d={l.d} fill="none" stroke={TONE[l.tone || "normal"]}
              strokeWidth="1.25" strokeOpacity={l.active ? 0.9 : 0.36} strokeDasharray={l.active ? undefined : "2 5"}
              strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {links.map((l) => (
            <rect key={`p-${l.key}`} x={l.ax - 4} y={l.ay - 4} width="8" height="8" rx="1.6"
              transform={`rotate(45 ${l.ax} ${l.ay})`} fill="rgb(var(--stage-2))" stroke={TONE[l.tone || "normal"]} strokeWidth="1.3" />
          ))}
          {!still && state !== "stopped" && links.filter((l) => l.active).map((l, i) => [0, 1].map((k) => (
            <rect key={`pk-${l.key}-${k}`} x="-5" y="-1.4" width="10" height="2.8" rx="1.4" fill="#fff">
              <animateMotion dur={`${busy ? 1.6 : 2.8}s`} begin={`${k * (busy ? 0.8 : 1.4) + i * 0.2}s`} repeatCount="indefinite" rotate="auto">
                <mpath href={`#t-${l.key}`} />
              </animateMotion>
            </rect>
          )))}
        </svg>
        {ins.map((l, i) => (
          <Callout key={l.key} link={l} side="start" style={{ right: "1.2%", top: `${(LABEL_Y[i] / H) * 100}%` }} />
        ))}
        {outs.map((l, i) => (
          <Callout key={l.key} link={l} side="end" style={{ left: "1.2%", top: `${(LABEL_Y[i] / H) * 100}%` }} />
        ))}
      </div>}

      {/* موبایل: هسته فشرده و گره‌ها در دو ستون زیر آن */}
      {mobile && <div>
        <svg viewBox={`${CX - GAP - 2 * A - 30} ${CY - B - 18} ${4 * A + 2 * GAP + 60} ${2 * B + 36}`}
          className="mx-auto h-auto w-full max-w-[250px]" aria-hidden>
          {scene("m", !still)}
        </svg>
        <div className="mt-3 grid grid-cols-2 gap-2">
          {[...inputs.slice(0, 4), ...outputs.slice(0, 4)].map((l) => (
            <button key={l.key} type="button" onClick={l.onClick} className="stage-glass flex min-w-0 items-start gap-2 rounded-2xl px-3 py-2.5 text-start">
              <Node link={l} className="mt-1" />
              <span className="min-w-0">
                <span className="block truncate text-[13px] font-semibold leading-5">{l.title}</span>
                <span className="num block truncate text-[12px] leading-5 text-white/85">{l.value}</span>
              </span>
            </button>
          ))}
        </div>
      </div>}
    </div>
  );
}

function Node({ link, className = "" }: { link: BrainLink; className?: string }) {
  const c = link.tone === "warn" ? "rgb(255 205 120)" : link.tone === "bad" ? "rgb(255 150 170)" : "#fff";
  // گره چهارگوش چرخیده (پد مدار) — هم‌خانواده نقاط اتصال روی هسته
  return (
    <span className={`relative grid h-3 w-3 shrink-0 rotate-45 place-items-center rounded-[3px] border-[1.5px] ${className}`} style={{ borderColor: c }}>
      <span className="h-1 w-1 rounded-[1px]" style={{ background: link.active ? c : "transparent" }} />
      {link.active && <span className="absolute -inset-px rounded-[3px]" style={{ border: `1px solid ${c}`, animation: "node-pulse 1.8s ease-out infinite" }} />}
    </span>
  );
}

function Callout({ link, side, style }: { link: BrainLink; side: "start" | "end"; style: React.CSSProperties }) {
  // گره روی لبه داخلی برچسب (رو به هسته) تا مسیر دقیقاً به آن برسد
  return (
    <button type="button" onClick={link.onClick} style={style}
      className="group absolute flex w-[14.6%] -translate-y-1/2 items-center gap-2 rounded-xl text-start outline-none focus-visible:ring-2 focus-visible:ring-white/70">
      {side === "end" && <Node link={link} />}
      <span className={side === "end" ? "min-w-0 flex-1 text-end" : "min-w-0 flex-1"}>
        <span className="block text-[13px] font-semibold leading-5 lg:text-[14px] lg:leading-6 group-hover:underline group-hover:underline-offset-4">{link.title}</span>
        <span className="num block text-[12px] leading-5 text-white/85 lg:text-[12.5px]">{link.value}</span>
      </span>
      {side === "start" && <Node link={link} />}
    </button>
  );
}
