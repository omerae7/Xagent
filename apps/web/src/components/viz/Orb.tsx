/** گوی هوشمند X Agent — هسته متحرک با حالت‌های بیکار، در حال فکر، اجرا، منتظر اجازه و توقف. */

import { motion } from "framer-motion";
import { useId } from "react";
import { cn } from "../ui";

export type OrbState = "idle" | "thinking" | "executing" | "waiting" | "stopped";

const PALETTE: Record<OrbState, [string, string, string]> = {
  idle: ["var(--cyan)", "var(--primary)", "var(--accent)"],
  thinking: ["var(--cyan)", "var(--primary)", "var(--accent)"],
  executing: ["var(--cyan)", "var(--primary)", "var(--cyan)"],
  waiting: ["var(--warning)", "var(--primary)", "var(--accent)"],
  stopped: ["var(--danger)", "var(--accent)", "var(--danger)"],
};

export function Orb({ state = "idle", size = 220, className }: { state?: OrbState; size?: number; className?: string }) {
  const id = useId().replace(/:/g, "");
  const [c1, c2, c3] = PALETTE[state];
  const speed = state === "thinking" ? 0.45 : state === "executing" ? 0.6 : state === "stopped" ? 3 : 1;
  const active = state === "thinking" || state === "executing";
  return (
    <div className={cn("relative grid place-items-center", className)} style={{ width: size, height: size }} aria-hidden>
      {/* درخشش بیرونی */}
      <motion.div
        className="absolute inset-[6%] rounded-full blur-3xl"
        style={{ background: `radial-gradient(circle, rgb(${c2} / .55), rgb(${c3} / .25) 45%, transparent 70%)` }}
        animate={{ scale: active ? [1, 1.12, 1] : [1, 1.04, 1], opacity: state === "stopped" ? 0.4 : [0.75, 1, 0.75] }}
        transition={{ duration: 3.2 * speed + 1, repeat: Infinity, ease: "easeInOut" }}
      />
      {/* حلقه‌های پالس */}
      {active &&
        [0, 1].map((i) => (
          <span
            key={i}
            className="absolute inset-[18%] animate-pulse-ring rounded-full border"
            style={{ borderColor: `rgb(${c1} / .45)`, animationDelay: `${i * 1.2}s` }}
          />
        ))}
      <svg viewBox="0 0 200 200" className="relative h-full w-full overflow-visible">
        <defs>
          <radialGradient id={`core-${id}`} cx="38%" cy="32%" r="70%">
            <stop offset="0%" stopColor="#fff" stopOpacity=".95" />
            <stop offset="18%" stopColor={`rgb(${c1})`} stopOpacity=".95" />
            <stop offset="55%" stopColor={`rgb(${c2})`} stopOpacity=".9" />
            <stop offset="100%" stopColor={`rgb(${c3})`} stopOpacity=".95" />
          </radialGradient>
          <linearGradient id={`ring-${id}`} x1="0" x2="1" y1="0" y2="1">
            <stop offset="0%" stopColor={`rgb(${c1})`} stopOpacity="0" />
            <stop offset="50%" stopColor={`rgb(${c1})`} stopOpacity="1" />
            <stop offset="100%" stopColor={`rgb(${c3})`} stopOpacity=".2" />
          </linearGradient>
          <filter id={`blur-${id}`}>
            <feGaussianBlur stdDeviation="1.4" />
          </filter>
          <clipPath id={`clip-${id}`}>
            <circle cx="100" cy="100" r="54" />
          </clipPath>
        </defs>

        {/* حلقه بیرونی چرخان */}
        <g style={{ transformOrigin: "100px 100px", animation: `spin-slow ${16 * speed}s linear infinite` }}>
          <circle cx="100" cy="100" r="88" fill="none" stroke={`url(#ring-${id})`} strokeWidth="1.6" strokeDasharray="120 430" strokeLinecap="round" />
          <circle cx="100" cy="12" r="2.6" fill={`rgb(${c1})`} filter={`url(#blur-${id})`} />
        </g>
        <g style={{ transformOrigin: "100px 100px", animation: `spin-slow ${24 * speed}s linear infinite reverse` }}>
          <circle cx="100" cy="100" r="76" fill="none" stroke={`rgb(${c2} / .35)`} strokeWidth="1" strokeDasharray="2 7" />
        </g>
        <g style={{ transformOrigin: "100px 100px", animation: `spin-slow ${10 * speed}s linear infinite` }}>
          <circle cx="100" cy="100" r="66" fill="none" stroke={`rgb(${c3} / .5)`} strokeWidth="1.2" strokeDasharray="40 180 12 180" strokeLinecap="round" />
        </g>

        {/* هسته کروی */}
        <circle cx="100" cy="100" r="54" fill={`url(#core-${id})`} />
        <g clipPath={`url(#clip-${id})`} opacity=".7">
          {[0, 30, 60, 90, 120, 150].map((deg, i) => (
            <g key={deg} style={{ transformOrigin: "100px 100px", animation: `spin-slow ${(8 + i * 1.5) * speed}s linear infinite${i % 2 ? " reverse" : ""}` }}>
              <ellipse cx="100" cy="100" rx="54" ry={14 + i * 6} fill="none" stroke="#fff" strokeOpacity={0.28 - i * 0.025} strokeWidth="0.9" transform={`rotate(${deg} 100 100)`} />
            </g>
          ))}
        </g>
        <ellipse cx="84" cy="78" rx="18" ry="10" fill="#fff" opacity=".35" filter={`url(#blur-${id})`} />
        <circle cx="100" cy="100" r="54" fill="none" stroke="#fff" strokeOpacity=".25" strokeWidth="1" />

        {/* ذرات در حال گردش */}
        {active &&
          [0, 1, 2].map((i) => (
            <g key={i} style={{ transformOrigin: "100px 100px", animation: `spin-slow ${(3 + i) * speed * 1.4}s linear infinite`, animationDelay: `${-i}s` }}>
              <circle cx="100" cy={22 + i * 8} r={2 - i * 0.4} fill={`rgb(${i % 2 ? c3 : c1})`} />
            </g>
          ))}
      </svg>
    </div>
  );
}

/** نشانگر «در حال فکر» با ۴ حالت (الهام از طراحی Iris) */
export function Thinking({ variant = 0, label = "در حال فکر…", className }: { variant?: 0 | 1 | 2 | 3; label?: string; className?: string }) {
  return (
    <div className={cn("inline-flex items-center gap-2.5 text-sm text-muted", className)}>
      <span className="relative grid h-7 w-7 place-items-center">
        {variant === 0 && <Orb state="thinking" size={28} />}
        {variant === 1 && (
          <span className="flex items-end gap-[3px]">
            {[0, 1, 2, 3, 4].map((i) => (
              <motion.span key={i} className="h-1.5 w-1.5 rounded-full bg-primary" animate={{ y: [0, -5, 0], opacity: [0.4, 1, 0.4] }} transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.12 }} />
            ))}
          </span>
        )}
        {variant === 2 && (
          <span className="flex flex-col gap-[3px]">
            {[0, 1, 2].map((i) => (
              <motion.span key={i} className="h-[3px] rounded-full bg-gradient-to-l from-primary to-accent" animate={{ width: [8, 20, 12, 18, 8] }} transition={{ duration: 1.4, repeat: Infinity, delay: i * 0.2 }} />
            ))}
          </span>
        )}
        {variant === 3 && (
          <span className="flex items-center gap-[2px]">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <motion.span key={i} className="w-[3px] rounded-full bg-cyan" animate={{ height: [4, 18, 6, 14, 4] }} transition={{ duration: 1, repeat: Infinity, delay: i * 0.09 }} />
            ))}
          </span>
        )}
      </span>
      <span className="bg-gradient-to-l from-muted via-ink to-muted bg-[length:200%_100%] bg-clip-text text-transparent animate-shimmer">{label}</span>
    </div>
  );
}
