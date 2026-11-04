/**
 * هویت بصری X Agent در زبان «شیشه موسمی»: خط‌نگاره نازک با گره‌های کوچک.
 * ChipMark: نشان X از دو خط متقاطع که چهار سرش به گره ختم می‌شود (کاشی آبی عمیق).
 * PadArray: صورت فلکی عصبی کوچک (گره + خط) که الگوی نورش وضعیت ایجنت را نشان می‌دهد.
 * Thinking: نشانگر فشرده «در حال فکر».
 * نام‌های قدیمی برای سازگاری صفحات حفظ شده‌اند. همه انیمیشن‌ها CSS هستند و با prefers-reduced-motion ثابت می‌شوند.
 */

import { cn } from "../ui";

export type AgentState = "idle" | "thinking" | "executing" | "waiting" | "stopped";
/** سازگاری با نام قدیمی */
export type OrbState = AgentState;

export const STATE_COLOR: Record<AgentState, string> = {
  idle: "rgb(var(--primary))",
  thinking: "rgb(var(--accent))",
  executing: "rgb(var(--cyan))",
  waiting: "rgb(var(--warning))",
  stopped: "rgb(var(--danger))",
};

// صورت فلکی ثابت (۱۳ گره) — مختصات در کادر ۱۰۰×۱۰۰
const NODES: [number, number][] = [
  [50, 14], [24, 26], [76, 26], [12, 50], [38, 44], [62, 44], [88, 50], [26, 72], [50, 62], [74, 72], [40, 88], [60, 88], [50, 40],
];
const EDGES: [number, number][] = [
  [0, 1], [0, 2], [0, 12], [1, 3], [1, 4], [2, 5], [2, 6], [3, 7], [4, 12], [5, 12], [4, 8], [5, 8], [6, 9],
  [7, 8], [8, 9], [7, 10], [9, 11], [10, 11], [8, 10], [8, 11], [4, 7], [5, 9],
];

export function PadArray({ state = "idle", size = 180, className, label }: { state?: AgentState; n?: number; size?: number; className?: string; label?: string }) {
  const tone = STATE_COLOR[state];
  const flowing = state === "executing" || state === "thinking";
  return (
    <div role="img" aria-label={label || "وضعیت هسته ایجنت"} className={cn("relative shrink-0", className)} style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" className="h-full w-full overflow-visible" aria-hidden>
        <defs>
          <radialGradient id={`pa-glow-${state}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={tone} stopOpacity="0.28" />
            <stop offset="100%" stopColor={tone} stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="50" cy="50" r="48" fill={`url(#pa-glow-${state})`} />
        {EDGES.map(([a, b], i) => (
          <line key={i} x1={NODES[a][0]} y1={NODES[a][1]} x2={NODES[b][0]} y2={NODES[b][1]} stroke={tone} strokeWidth="0.8"
            strokeOpacity={state === "stopped" ? 0.25 : 0.42} strokeLinecap="round"
            strokeDasharray={flowing ? "3 5" : undefined}
            style={flowing ? { animation: `line-flow ${state === "executing" ? 3.2 : 6}s linear infinite`, animationDelay: `${-i * 0.37}s` } : undefined} />
        ))}
        {NODES.map(([x, y], i) => {
          const delay = state === "thinking" ? (x / 100) * 1.6 : state === "executing" ? (y / 100) * 1.1 : (i * 0.61) % 4;
          const dur = state === "thinking" ? 1.6 : state === "executing" ? 1.1 : 4.2;
          const center = i === 12;
          return (
            <g key={i}>
              <circle cx={x} cy={y} r={center ? 4.2 : 2.6} fill="rgb(var(--surface))" stroke={tone} strokeWidth={center ? 1.4 : 1.1}
                style={state !== "stopped" ? { animation: `node-twinkle ${dur}s ease-in-out infinite`, animationDelay: `${delay}s`, ["--lo" as any]: 0.45 } : { opacity: 0.5 }} />
              {center && <circle cx={x} cy={y} r={1.8} fill={tone} />}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function ChipMark({ state = "idle", size = 36, className }: { state?: AgentState; size?: number; className?: string }) {
  const tone = STATE_COLOR[state];
  return (
    <span className={cn("relative inline-grid shrink-0 place-items-center overflow-hidden", className)}
      style={{ width: size, height: size, borderRadius: size * 0.32,
        background: "linear-gradient(135deg, rgb(var(--stage-1)), rgb(var(--stage-2)) 55%, rgb(var(--stage-3)))",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / .28), 0 6px 14px -8px rgb(var(--stage-1) / .8)" }}
      role="img" aria-label="X Agent">
      <svg viewBox="0 0 40 40" className="h-[78%] w-[78%]" aria-hidden>
        <g stroke="rgb(255 255 255)" strokeWidth="1.6" strokeLinecap="round" fill="none">
          <line x1="10" y1="10" x2="30" y2="30" />
          <line x1="30" y1="10" x2="10" y2="30" strokeOpacity="0.75" />
        </g>
        {[[10, 10], [30, 10], [10, 30], [30, 30]].map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r="2.6" fill="rgb(var(--stage-2))" stroke="#fff" strokeWidth="1.3" />
        ))}
        <circle cx="20" cy="20" r="3.4" fill={tone} stroke="#fff" strokeWidth="1.3"
          style={state === "idle" || state === "stopped" ? undefined : { animation: "node-twinkle 1.4s ease-in-out infinite", ["--lo" as any]: 0.55 }} />
      </svg>
    </span>
  );
}

export function Thinking({ label = "در حال فکر…", className }: { variant?: number; label?: string; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 text-xs text-muted", className)} role="status">
      <span className="flex items-center gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className="h-1.5 w-1.5 rounded-full bg-primary" style={{ animation: "node-twinkle 1.1s ease-in-out infinite", animationDelay: `${i * 0.18}s`, ["--lo" as any]: 0.25 }} />
        ))}
      </span>
      {label}
    </span>
  );
}
