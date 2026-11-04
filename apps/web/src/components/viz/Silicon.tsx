/**
 * هویت بصری X Agent: «سیلیکون زنده» — جایگزین گوی دایره‌ای.
 * ChipMark: نشان تراشه با پایه‌ها و پدهای داخلی که با وضعیت ایجنت روشن می‌شوند.
 * PadArray: هسته عصبی ایجنت به‌صورت آرایه پدهای مربعی؛ هر وضعیت الگوی نوری خودش را دارد.
 * Thinking: نشانگر فشرده «در حال فکر».
 * همه با CSS انیمیشن (بدون حلقه JS) تا روی سخت‌افزار ضعیف هم سبک باشند؛ با prefers-reduced-motion ثابت می‌شوند.
 */

import type { CSSProperties } from "react";
import { cn } from "../ui";

export type AgentState = "idle" | "thinking" | "executing" | "waiting" | "stopped";
/** سازگاری با نام قدیمی */
export type OrbState = AgentState;

const TONE: Record<AgentState, string> = {
  idle: "var(--primary)",
  thinking: "var(--film-2)",
  executing: "var(--film-1)",
  waiting: "var(--warning)",
  stopped: "var(--danger)",
};

/** الگوی تأخیر هر پد بر اساس وضعیت (میلی‌ثانیه) */
function padTiming(state: AgentState, r: number, c: number, n: number): { delay: number; dur: number; lo: number; hi: number } {
  const hash = ((r * 73856093) ^ (c * 19349663)) >>> 0;
  switch (state) {
    case "thinking": // موج قطری
      return { delay: (r + c) * 70, dur: 1400, lo: 0.12, hi: 0.95 };
    case "executing": // اسکن ردیفی سریع
      return { delay: r * 90 + (c % 2) * 30, dur: 900, lo: 0.2, hi: 1 };
    case "waiting": // گوشه‌ها چشمک می‌زنند
      return (r === 0 || r === n - 1) && (c === 0 || c === n - 1)
        ? { delay: 0, dur: 1100, lo: 0.25, hi: 1 }
        : { delay: hash % 3000, dur: 4200, lo: 0.08, hi: 0.3 };
    case "stopped":
      return { delay: 0, dur: 0, lo: 0.1, hi: 0.1 };
    default: // تنفس پراکنده
      return { delay: hash % 5200, dur: 5200 + (hash % 1800), lo: 0.16, hi: 0.85 };
  }
}

export function PadArray({ state = "idle", n = 8, size = 180, className, label }: { state?: AgentState; n?: number; size?: number; className?: string; label?: string }) {
  const gap = Math.max(2, Math.round(size / (n * 7)));
  const cells = Array.from({ length: n * n }, (_, i) => [Math.floor(i / n), i % n] as const);
  const tone = TONE[state];
  return (
    <div
      role="img"
      aria-label={label || "وضعیت هسته ایجنت"}
      className={cn("die die-sunk relative grid p-[10%]", className)}
      style={{ width: size, height: size, ["--cut" as any]: `${Math.round(size / 9)}px` }}
    >
      <div className="grid h-full w-full" style={{ gridTemplateColumns: `repeat(${n}, 1fr)`, gap }}>
        {cells.map(([r, c]) => {
          const t = padTiming(state, r, c, n);
          const cut = state === "stopped" && (r === c || r === c + 1);
          // الگوی ثابت برای حالت بدون حرکت (prefers-reduced-motion) تا هسته خاموش به نظر نرسد
          const lit = ((r * 7 + c * 13) % 5 === 0 && state !== "stopped") || (state === "waiting" && t.dur === 1100);
          const style: CSSProperties = {
            background: cut ? "rgb(var(--danger))" : state === "executing" ? `rgb(var(--film-${((r + c) % 5) + 1}))` : `rgb(${tone})`,
            opacity: t.dur ? (lit ? t.hi * 0.85 : t.lo) : cut ? 0.85 : 0.12,
            animation: t.dur ? `pad-breathe ${t.dur}ms ${t.delay}ms cubic-bezier(.4,0,.2,1) infinite` : undefined,
            ["--lo" as any]: t.lo,
            ["--hi" as any]: t.hi,
            borderRadius: 1,
          };
          return <span key={`${r}-${c}`} style={style} />;
        })}
      </div>
    </div>
  );
}

/** نشان تراشه کوچک (برند، آواتار، وضعیت) */
export function ChipMark({ state = "idle", size = 36, className }: { state?: AgentState; size?: number; className?: string }) {
  const tone = TONE[state];
  const pins = [10, 18, 26];
  const pads = [0, 1, 2].flatMap((r) => [0, 1, 2].map((c) => [r, c] as const));
  return (
    <svg viewBox="0 0 36 36" width={size} height={size} className={cn("shrink-0", className)} aria-hidden>
      {/* پایه‌ها */}
      {pins.map((p) => (
        <g key={p} stroke="rgb(var(--muted))" strokeOpacity=".55" strokeWidth="1.4">
          <line x1={p} y1="1" x2={p} y2="5" />
          <line x1={p} y1="31" x2={p} y2="35" />
          <line x1="1" y1={p} x2="5" y2={p} />
          <line x1="31" y1={p} x2="35" y2={p} />
        </g>
      ))}
      {/* بدنه پَخ‌دار */}
      <path d="M6 6h20l4 4v20H10l-4-4z" fill="rgb(var(--surface-2))" stroke={`rgb(${tone})`} strokeOpacity=".7" strokeWidth="1.2" />
      {/* پدهای هسته */}
      {pads.map(([r, c]) => {
        const t = padTiming(state, r, c, 3);
        return (
          <rect
            key={`${r}${c}`}
            x={11 + c * 5.2}
            y={11 + r * 5.2}
            width="3.6"
            height="3.6"
            rx=".5"
            fill={state === "stopped" && r === c ? "rgb(var(--danger))" : `rgb(${tone})`}
            style={{
              opacity: t.dur ? t.lo : 0.35,
              animation: t.dur ? `pad-breathe ${Math.min(t.dur, 2400)}ms ${t.delay % 1600}ms ease-in-out infinite` : undefined,
              ["--lo" as any]: Math.max(0.25, t.lo),
              ["--hi" as any]: 1,
            }}
          />
        );
      })}
      {/* علامت پایه ۱ */}
      <path d="M26 6l4 4h-4z" fill={`rgb(${tone})`} opacity=".9" />
    </svg>
  );
}

export function Thinking({ label = "در حال فکر…", className }: { variant?: number; label?: string; className?: string }) {
  return (
    <div className={cn("inline-flex items-center gap-2.5 text-sm text-muted", className)} role="status">
      <span className="flex items-center gap-[3px]" aria-hidden>
        {[0, 1, 2, 3].map((i) => (
          <span
            key={i}
            className="h-2 w-2 rounded-[1px] bg-primary"
            style={{ animation: `pad-breathe 1100ms ${i * 140}ms ease-in-out infinite`, ["--lo" as any]: 0.2, ["--hi" as any]: 1, opacity: 0.2 }}
          />
        ))}
      </span>
      {label && <span>{label}</span>}
    </div>
  );
}
