/**
 * خط زمان عمودی مراحل اجرا (الهام از مسیر شماره‌دار عمودی تصاویر مرجع).
 * هر مرحله یک گره شماره‌دار روی یک خط عمودی صاف و نازک است؛ خط هر بخش هنگام ظهور از بالا به پایین کشیده می‌شود،
 * مرحله در حال اجرا هاله تپنده دارد و خطا/انسداد با رنگ وضعیت مشخص می‌شود.
 */

import { motion } from "framer-motion";
import { AlertTriangle, Check, Loader2, X } from "lucide-react";
import type { ReactNode } from "react";
import { faInt } from "../../lib/format";
import { cn } from "../ui";

export type TimelineStatus = "done" | "running" | "error" | "blocked" | "pending";
export type TimelineItem = {
  id: string;
  title: ReactNode;
  meta?: ReactNode;
  status: TimelineStatus;
  icon?: ReactNode;
  children?: ReactNode;
};

const TONE: Record<TimelineStatus, string> = {
  done: "text-primary",
  running: "text-accent",
  error: "text-danger",
  blocked: "text-warning",
  pending: "text-muted",
};

function two(n: number) {
  return n < 10 ? `${faInt(0)}${faInt(n)}` : faInt(n);
}

export function Timeline({ items, live, size = "md", className }: { items: TimelineItem[]; live?: boolean; size?: "sm" | "md"; className?: string }) {
  const node = size === "sm" ? 26 : 32;
  const gutter = size === "sm" ? 40 : 52;
  const all = live ? [...items, { id: "__live", title: "در حال ادامه…", status: "pending" as TimelineStatus }] : items;
  return (
    <ol className={cn("relative", className)}>
      {all.map((it, i) => {
        const last = i === all.length - 1;
        const cx = gutter / 2;
        return (
          <motion.li key={it.id} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
            className="relative grid" style={{ gridTemplateColumns: `${gutter}px minmax(0,1fr)`, columnGap: size === "sm" ? 8 : 12 }}>
            <div className="relative">
              {!last && (
                <svg className="absolute overflow-visible" style={{ top: node / 2, insetInlineStart: 0, width: gutter, height: "100%" }}
                  viewBox={`0 0 ${gutter} 100`} preserveAspectRatio="none" aria-hidden>
                  <motion.path d={`M ${cx} 0 L ${cx} 100`} fill="none"
                    stroke={it.status === "error" ? "rgb(var(--danger))" : "rgb(var(--primary))"} strokeOpacity={all[i + 1]?.status === "pending" ? 0.28 : 0.45}
                    strokeWidth="1.25" vectorEffect="non-scaling-stroke" strokeDasharray={all[i + 1]?.status === "pending" ? "3 5" : undefined}
                    initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1], delay: 0.1 }} />
                </svg>
              )}
              <span className={cn("relative z-[1] mx-auto grid place-items-center rounded-full bg-surface font-semibold", TONE[it.status])}
                style={{ width: node, height: node, fontSize: size === "sm" ? 10.5 : 11.5,
                  boxShadow: `inset 0 0 0 1.5px currentColor, 0 0 0 4px rgb(var(--surface) / .7)` }}>
                {it.status === "running" ? (
                  <>
                    <span className="absolute inset-0 rounded-full border border-current" style={{ animation: "node-pulse 1.6s ease-out infinite" }} />
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  </>
                ) : it.status === "error" ? <X className="h-3.5 w-3.5" />
                  : it.status === "blocked" ? <AlertTriangle className="h-3.5 w-3.5" />
                    : it.status === "pending" ? <span className="h-1.5 w-1.5 rounded-full bg-current" style={{ animation: "node-twinkle 1.2s ease-in-out infinite" }} />
                      : <span className="num text-ink">{two(i + 1)}</span>}
              </span>
            </div>
            <div className={cn("min-w-0", last ? "pb-1" : size === "sm" ? "pb-3.5" : "pb-5")} style={{ paddingTop: (node - 22) / 2 }}>
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                {it.icon && <span className={cn("shrink-0", TONE[it.status])}>{it.icon}</span>}
                <span className={cn("min-w-0 font-semibold leading-[22px]", size === "sm" ? "text-[13px]" : "text-[14px]",
                  it.status === "error" && "text-danger", it.status === "blocked" && "text-warning", it.status === "pending" && "font-normal text-muted")}>
                  {it.title}
                </span>
                {it.status === "done" && size === "md" && <Check className="h-3.5 w-3.5 text-success" aria-label="انجام شد" />}
              </div>
              {it.meta && <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] leading-5 text-muted">{it.meta}</div>}
              {it.children && <div className="mt-1.5">{it.children}</div>}
            </div>
          </motion.li>
        );
      })}
    </ol>
  );
}
