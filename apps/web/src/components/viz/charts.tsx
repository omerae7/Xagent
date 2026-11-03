/** نمودارهای سبک SVG (بدون کتابخانه سنگین): ناحیه‌ای، خطی کوچک، گیج دایره‌ای، دونات و نوار پیشرفت. */

import { motion } from "framer-motion";
import { useId, useMemo, useState } from "react";
import { cn } from "../ui";
import { faNum } from "../../lib/format";

type Series = { key: string; label: string; color: string };

/** منحنی یکنوا (Fritsch–Carlson): بدون بیرون‌زدگی زیر صفر یا بالای قله */
function smoothPath(pts: [number, number][]): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M${pts[0][0]},${pts[0][1]}`;
  const dx: number[] = [];
  const m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0] || 1e-6;
    m[i] = (pts[i + 1][1] - pts[i][1]) / dx[i];
  }
  const t: number[] = [m[0]];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  t[n - 1] = m[n - 2];
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) {
      t[i] = 0;
      t[i + 1] = 0;
      continue;
    }
    const a = t[i] / m[i];
    const b = t[i + 1] / m[i];
    const sq = a * a + b * b;
    if (sq > 9) {
      const tau = 3 / Math.sqrt(sq);
      t[i] = tau * a * m[i];
      t[i + 1] = tau * b * m[i];
    }
  }
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += ` C${pts[i][0] + h},${pts[i][1] + t[i] * h} ${pts[i + 1][0] - h},${pts[i + 1][1] - t[i + 1] * h} ${pts[i + 1][0]},${pts[i + 1][1]}`;
  }
  return d;
}

export function AreaChart({
  data,
  series,
  height = 200,
  xLabel,
  format = (v: number) => faNum(v),
  max: forcedMax,
}: {
  data: Record<string, any>[];
  series: Series[];
  height?: number;
  xLabel?: (d: Record<string, any>, i: number) => string;
  format?: (v: number) => string;
  max?: number;
}) {
  const id = useId().replace(/:/g, "");
  const [hover, setHover] = useState<number | null>(null);
  const W = 600;
  const H = height;
  const pad = { t: 12, b: 24, l: 6, r: 6 };
  const max = useMemo(() => forcedMax ?? Math.max(1, ...data.flatMap((d) => series.map((s) => Number(d[s.key]) || 0))) * 1.12, [data, series, forcedMax]);
  const x = (i: number) => pad.l + (data.length <= 1 ? (W - pad.l - pad.r) / 2 : (i / (data.length - 1)) * (W - pad.l - pad.r));
  const y = (v: number) => pad.t + (1 - v / max) * (H - pad.t - pad.b);
  if (!data.length) return <div className="grid h-full place-items-center py-10 text-sm text-muted">داده‌ای برای نمایش نیست</div>;
  return (
    <div className="relative w-full">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full overflow-visible" style={{ height }} preserveAspectRatio="none" onMouseLeave={() => setHover(null)}>
        <defs>
          {series.map((s) => (
            <linearGradient key={s.key} id={`g-${id}-${s.key}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity=".38" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={pad.l} x2={W - pad.r} y1={pad.t + f * (H - pad.t - pad.b)} y2={pad.t + f * (H - pad.t - pad.b)} stroke="rgb(var(--border) / .12)" strokeDasharray="3 5" />
        ))}
        {series.map((s, si) => {
          const pts = data.map((d, i) => [x(i), y(Number(d[s.key]) || 0)] as [number, number]);
          const line = smoothPath(pts);
          const area = `${line} L${x(data.length - 1)},${H - pad.b} L${x(0)},${H - pad.b} Z`;
          return (
            <g key={s.key}>
              <motion.path d={area} fill={`url(#g-${id}-${s.key})`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.8, delay: si * 0.1 }} />
              <motion.path d={line} fill="none" stroke={s.color} strokeWidth="2.2" strokeLinecap="round" vectorEffect="non-scaling-stroke"
                initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 1.1, ease: "easeOut", delay: si * 0.1 }}
                style={{ filter: `drop-shadow(0 0 6px ${s.color})` }} />
            </g>
          );
        })}
        {data.map((_, i) => (
          <rect key={i} x={x(i) - (W / Math.max(data.length, 1)) / 2} y={0} width={W / Math.max(data.length, 1)} height={H} fill="transparent" onMouseEnter={() => setHover(i)} onTouchStart={() => setHover(i)} />
        ))}
        {hover != null && (
          <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={H - pad.b} stroke="rgb(var(--primary) / .45)" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
      {xLabel && (
        <div className="mt-1 flex justify-between px-1 text-[10px] text-muted" dir="ltr">
          {data.map((d, i) =>
            i % Math.max(1, Math.ceil(data.length / 7)) === 0 || i === data.length - 1 ? (
              <span key={i} dir="rtl">{xLabel(d, i)}</span>
            ) : null,
          )}
        </div>
      )}
      {hover != null && (
        <div className="glass-strong pointer-events-none absolute top-1 z-10 min-w-[140px] rounded-xl px-3 py-2 text-xs shadow-xl"
          style={{ left: `${Math.min(80, Math.max(0, (x(hover) / W) * 100 - 10))}%` }}>
          {xLabel && <div className="mb-1 text-muted">{xLabel(data[hover], hover)}</div>}
          {series.map((s) => (
            <div key={s.key} className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: s.color }} />{s.label}</span>
              <span className="num font-semibold">{format(Number(data[hover][s.key]) || 0)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Sparkline({ values, color = "rgb(var(--primary))", height = 36, className }: { values: number[]; color?: string; height?: number; className?: string }) {
  const id = useId().replace(/:/g, "");
  const W = 120;
  const max = Math.max(1, ...values);
  const pts = values.map((v, i) => [values.length <= 1 ? W / 2 : (i / (values.length - 1)) * W, height - 2 - (v / max) * (height - 4)] as [number, number]);
  const line = smoothPath(pts);
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className={cn("w-full", className)} style={{ height }} preserveAspectRatio="none">
      <defs>
        <linearGradient id={`s-${id}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity=".35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {pts.length > 1 && <path d={`${line} L${W},${height} L0,${height} Z`} fill={`url(#s-${id})`} />}
      <path d={line} fill="none" stroke={color} strokeWidth="1.8" vectorEffect="non-scaling-stroke" strokeLinecap="round" />
    </svg>
  );
}

export function Gauge({ value, label, sub, size = 108, color }: { value: number; label: string; sub?: string; size?: number; color?: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  const c = color || (v > 85 ? "rgb(var(--danger))" : v > 65 ? "rgb(var(--warning))" : "rgb(var(--cyan))");
  const r = 42;
  const circ = 2 * Math.PI * r;
  const arc = circ * 0.75;
  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: size, height: size }}>
        <svg viewBox="0 0 100 100" className="h-full w-full -rotate-[225deg]">
          <circle cx="50" cy="50" r={r} fill="none" stroke="rgb(var(--border) / .15)" strokeWidth="7" strokeDasharray={`${arc} ${circ}`} strokeLinecap="round" />
          <motion.circle cx="50" cy="50" r={r} fill="none" stroke={c} strokeWidth="7" strokeLinecap="round"
            strokeDasharray={`${arc} ${circ}`} initial={{ strokeDashoffset: arc }} animate={{ strokeDashoffset: arc * (1 - v / 100) }}
            transition={{ type: "spring", stiffness: 60, damping: 16 }} style={{ filter: `drop-shadow(0 0 5px ${c})` }} />
        </svg>
        <div className="absolute inset-0 grid place-items-center">
          <div className="text-center">
            <div className="num text-xl font-bold">{faNum(v)}<span className="text-xs text-muted">٪</span></div>
          </div>
        </div>
      </div>
      <div className="text-center">
        <div className="text-xs font-medium">{label}</div>
        {sub && <div className="num text-[10px] text-muted">{sub}</div>}
      </div>
    </div>
  );
}

export function Donut({ items, size = 140, center }: { items: { label: string; value: number; color: string }[]; size?: number; center?: React.ReactNode }) {
  const total = items.reduce((a, b) => a + (b.value || 0), 0) || 1;
  const r = 40;
  const circ = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
        <circle cx="50" cy="50" r={r} fill="none" stroke="rgb(var(--border) / .12)" strokeWidth="12" />
        {items.map((it, i) => {
          const len = (it.value / total) * circ;
          const el = (
            <motion.circle key={i} cx="50" cy="50" r={r} fill="none" stroke={it.color} strokeWidth="12" strokeDasharray={`${Math.max(0, len - 1.5)} ${circ}`}
              strokeDashoffset={-acc} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.08 }} />
          );
          acc += len;
          return el;
        })}
      </svg>
      {center && <div className="absolute inset-0 grid place-items-center text-center">{center}</div>}
    </div>
  );
}

export function Meter({ value, max, tone = "primary", className, thresholds = true }: { value: number; max: number; tone?: string; className?: string; thresholds?: boolean }) {
  const p = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const color = thresholds && p > 90 ? "bg-danger" : thresholds && p > 70 ? "bg-warning" : `bg-${tone}`;
  return (
    <div className={cn("h-2 w-full overflow-hidden rounded-full bg-line/15", className)}>
      <motion.div className={cn("h-full rounded-full", color)} initial={{ width: 0 }} animate={{ width: `${p}%` }} transition={{ type: "spring", stiffness: 70, damping: 18 }}
        style={{ boxShadow: "0 0 12px currentColor" }} />
    </div>
  );
}

export const CHART = {
  in: "rgb(var(--primary))",
  out: "rgb(var(--accent))",
  cache: "rgb(var(--cyan))",
  cost: "rgb(var(--warning))",
  cpu: "rgb(var(--cyan))",
  mem: "rgb(var(--accent))",
  disk: "rgb(var(--primary))",
};
