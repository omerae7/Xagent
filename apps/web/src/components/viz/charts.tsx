/** نمودارهای سبک SVG (بدون کتابخانه سنگین) در زبان «شیشه موسمی»: ناحیه‌ای، خطی کوچک، سنج خطی با گره، نوار سهم و نوار پیشرفت. */

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
  const pick = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const rel = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < data.length; i++) if (Math.abs(x(i) - rel) < Math.abs(x(best) - rel)) best = i;
    setHover(best);
  };
  if (!data.length) return <div className="grid h-full place-items-center py-10 text-sm text-muted">داده‌ای برای نمایش نیست</div>;
  return (
    <div className="relative w-full touch-pan-y" onPointerLeave={(e) => e.pointerType === "mouse" && setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full overflow-visible" style={{ height }} preserveAspectRatio="none"
        onPointerMove={pick} onPointerDown={pick}>
        <defs>
          {series.map((s) => (
            <linearGradient key={s.key} id={`g-${id}-${s.key}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity=".24" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={pad.l} x2={W - pad.r} y1={pad.t + f * (H - pad.t - pad.b)} y2={pad.t + f * (H - pad.t - pad.b)} stroke="rgb(var(--border) / .14)" strokeDasharray="2 6" vectorEffect="non-scaling-stroke" />
        ))}
        {series.map((s, si) => {
          const pts = data.map((d, i) => [x(i), y(Number(d[s.key]) || 0)] as [number, number]);
          const line = smoothPath(pts);
          const area = `${line} L${x(data.length - 1)},${H - pad.b} L${x(0)},${H - pad.b} Z`;
          return (
            <g key={s.key}>
              <motion.path d={area} fill={`url(#g-${id}-${s.key})`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.8, delay: si * 0.1 }} />
              <motion.path d={line} fill="none" stroke={s.color} strokeWidth="1.8" strokeLinecap="square" vectorEffect="non-scaling-stroke"
                initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1], delay: si * 0.08 }} />
            </g>
          );
        })}
        {hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={H - pad.b} stroke="rgb(var(--primary) / .5)" vectorEffect="non-scaling-stroke" />
            {series.map((s) => (
              <rect key={s.key} x={x(hover) - 3.5} y={y(Number(data[hover][s.key]) || 0) - 3.5} width="7" height="7" fill="rgb(var(--surface))" stroke={s.color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
            ))}
          </g>
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
        // عرض ثابت و clamp: Tooltip هیچ‌وقت از لبه نمودار (به‌ویژه در موبایل) بیرون نمی‌زند
        <div className="die die-sm die-2 pointer-events-none absolute top-1 z-10 w-[148px] px-3 py-2 text-xs shadow-lift"
          style={{ left: `clamp(0px, calc(${(x(hover) / W) * 100}% - 74px), calc(100% - 148px))` }}>
          {xLabel && <div className="mb-1 text-muted">{xLabel(data[hover], hover)}</div>}
          {series.map((s) => (
            <div key={s.key} className="flex items-center justify-between gap-3">
              <span className="flex min-w-0 items-center gap-1.5"><span className="pad" style={{ color: s.color }} /><span className="truncate">{s.label}</span></span>
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

/** سنج خطی: خط نازک با گره روی مقدار و عدد سبک بزرگ (زبان خط‌نگاره تصاویر مرجع) */
export function Gauge({ value, label, sub, color }: { value: number; label: string; sub?: string; size?: number; color?: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  const c = v > 85 ? "rgb(var(--danger))" : v > 65 ? "rgb(var(--warning))" : color || "rgb(var(--cyan))";
  return (
    <div className="min-w-0" role="meter" aria-valuenow={Math.round(v)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className="flex items-baseline justify-between gap-1">
        <span className="truncate text-[13px] font-medium">{label}</span>
        <span className="num text-[22px] font-light leading-7">{faNum(v)}<span className="text-[12px] text-muted">٪</span></span>
      </div>
      <div className="relative mt-2 h-3.5" dir="ltr">
        <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-line/25" />
        {[25, 50, 75].map((t) => <span key={t} className="absolute top-1/2 h-1.5 w-px -translate-y-1/2 bg-line/30" style={{ left: `${t}%` }} />)}
        <motion.span className="absolute left-0 top-1/2 h-[2px] -translate-y-1/2 rounded-full" style={{ background: c }}
          initial={false} animate={{ width: `${v}%` }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }} />
        <motion.span className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-surface"
          style={{ boxShadow: `inset 0 0 0 2px ${c}, 0 0 0 4px rgb(var(--surface) / .6)` }}
          initial={false} animate={{ left: `${v}%` }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }} />
      </div>
      {sub && <div className="num mt-1.5 truncate text-[12px] text-muted">{sub}</div>}
    </div>
  );
}

/** نوار سهم‌ها: بخش‌های گرد با فاصله */
export function Donut({ items, center }: { items: { label: string; value: number; color: string }[]; size?: number; center?: React.ReactNode }) {
  const total = items.reduce((a, b) => a + (b.value || 0), 0);
  return (
    <div className="w-full">
      {center && <div className="mb-3 text-center">{center}</div>}
      <div className="flex h-2.5 w-full gap-1 overflow-hidden rounded-full" dir="ltr">
        {total > 0 ? items.filter((it) => it.value > 0).map((it, i) => (
          <motion.span key={it.label + i} title={it.label} style={{ background: it.color }} initial={{ flexGrow: 0 }} animate={{ flexGrow: it.value / total }}
            transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1], delay: i * 0.04 }} className="h-full min-w-[6px] basis-0 rounded-full" />
        )) : <span className="h-full w-full rounded-full bg-line/10" />}
      </div>
    </div>
  );
}

export function Meter({ value, max, tone = "primary", className, thresholds = true }: { value: number; max: number; tone?: string; className?: string; thresholds?: boolean }) {
  const p = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const color = thresholds && p > 90 ? "bg-danger" : thresholds && p > 70 ? "bg-warning" : `bg-${tone}`;
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-line/[.12]", className)} role="progressbar" aria-valuenow={Math.round(p)} aria-valuemin={0} aria-valuemax={100}>
      <motion.div className={cn("h-full rounded-full", color)} initial={{ width: 0 }} animate={{ width: `${p}%` }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }} />
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
