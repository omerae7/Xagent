/**
 * داشبورد: «نقشه زنده تراشه ایجنت». هسته مدیر در مرکز، بلوک‌های عملکردی (حافظه، ابزار، مدل‌ها، کارمندان،
 * رویا، تغییرات) پیرامون آن، و مسیرهای سیگنال بین‌شان که با وضعیت واقعی سیستم روشن می‌شوند.
 * همه اعداد از API خود سیستم می‌آیند؛ هیچ داده نمایشی ساختگی وجود ندارد.
 */

import {
  ArrowLeft,
  Brain,
  CalendarCheck,
  CircuitBoard,
  Clock,
  Cpu,
  HardDrive,
  History,
  MemoryStick,
  Moon,
  Repeat,
  Send,
  ShieldAlert,
  Sparkles,
  Users,
  Wrench,
  Zap,
} from "lucide-react";
import { forwardRef, type ReactNode, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { agentState, useDashboard } from "../components/layout/Shell";
import { Badge, Button, Card, CardHeader, cn, ErrorBox, Skeleton } from "../components/ui";
import { AreaChart, CHART, Gauge, Meter, Sparkline } from "../components/viz/charts";
import { type AgentState, PadArray } from "../components/viz/Silicon";
import { bytes, compact, faInt, faNum, fmtDate, fmtTime, relTime, uptime, usd } from "../lib/format";
import { useLiveMetrics } from "../lib/live";

const STATE_TEXT: Record<AgentState, [string, string]> = {
  idle: ["آماده به کار", "مدیر منتظر دستور شماست."],
  thinking: ["در حال فکر", "مدیر در حال تحلیل و برنامه‌ریزی است."],
  executing: ["در حال اجرا", "مدیر و کارمندان مشغول انجام کارها هستند."],
  waiting: ["منتظر اجازه شما", "یک عملیات حساس منتظر تأیید شماست."],
  stopped: ["توقف اضطراری", "همه کارها متوقف شده‌اند."],
};

const RUN_STATUS: Record<string, { label: string; tone: any }> = {
  queued: { label: "در صف", tone: "neutral" },
  running: { label: "در حال اجرا", tone: "primary" },
  waiting_approval: { label: "منتظر اجازه", tone: "warning" },
  paused: { label: "متوقف", tone: "warning" },
};

type BlockKey = "memory" | "models" | "dream" | "tools" | "employees" | "changes";

/** کدام مسیرها در هر وضعیت سیگنال حمل می‌کنند */
function activeLinks(state: AgentState, c: any): Partial<Record<BlockKey, "flow" | "warn" | "idle">> {
  if (state === "stopped") return {};
  if (state === "waiting") return { changes: "warn", models: "idle" };
  if (state === "executing") return { tools: "flow", models: "flow", employees: c?.employees?.busy ? "flow" : "idle", memory: "idle" };
  if (state === "thinking") return { models: "flow", memory: "flow" };
  return { dream: "idle", memory: "idle" };
}

/** یک بلوک عملکردی تراشه */
const DieBlock = forwardRef<HTMLButtonElement, { title: string; icon: ReactNode; onClick: () => void; children: ReactNode; alert?: boolean; className?: string }>(
  function DieBlock({ title, icon, onClick, children, alert, className }, ref) {
    return (
      <button ref={ref} onClick={onClick} className={cn("die group flex min-w-0 flex-col p-4 text-start transition-transform duration-150 active:translate-y-px", alert && "die-live", className)}>
        <span className="flex w-full items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-2 text-[13px] font-semibold">
            <span className="text-muted transition-colors group-hover:text-primary">{icon}</span>
            <span className="truncate">{title}</span>
          </span>
          <ArrowLeft className="h-3.5 w-3.5 shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100" />
        </span>
        <span className="mt-3 block w-full">{children}</span>
      </button>
    );
  },
);

/** ردیف پدها: هر پد یک واحد (ابزار، مدل، کارمند) */
function Pads({ on = 0, warn = 0, bad = 0, total, max = 24 }: { on?: number; warn?: number; bad?: number; total: number; max?: number }) {
  const n = Math.min(max, Math.max(total, on + warn + bad));
  if (!n) return <span className="block h-2.5 text-[11px] leading-3 text-muted">هنوز موردی ثبت نشده</span>;
  return (
    <span className="flex flex-wrap gap-[3px]" dir="ltr" aria-hidden>
      {Array.from({ length: n }, (_, i) => (
        <span key={i} className={cn("h-2.5 w-2.5 rounded-[1px]", i < bad ? "bg-danger" : i < bad + warn ? "bg-warning" : i < bad + warn + on ? "bg-primary" : "bg-line/20")} />
      ))}
    </span>
  );
}

function Readout({ items }: { items: [string, ReactNode][] }) {
  return (
    <span className="grid w-full grid-cols-3 gap-2">
      {items.map(([l, v]) => (
        <span key={l} className="min-w-0">
          <span className="num block truncate text-lg font-bold leading-6">{v}</span>
          <span className="block truncate text-[11px] text-muted">{l}</span>
        </span>
      ))}
    </span>
  );
}

/** مسیرهای سیگنال بین هسته و بلوک‌ها (فقط در چیدمان سه‌ستونه) */
function Traces({ host, core, blocks, links }: {
  host: React.RefObject<HTMLElement>;
  core: React.RefObject<HTMLElement>;
  blocks: Record<BlockKey, React.RefObject<HTMLElement>>;
  links: Partial<Record<BlockKey, "flow" | "warn" | "idle">>;
}) {
  const [paths, setPaths] = useState<{ k: BlockKey; d: string[] }[]>([]);
  const [box, setBox] = useState({ w: 0, h: 0 });
  // useEffect (نه LayoutEffect): ref والد پس از اثرهای فرزند متصل می‌شود
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const calc = () => {
      if (window.innerWidth < 1280 || !core.current) return setPaths([]);
      const H = el.getBoundingClientRect();
      const C = core.current.getBoundingClientRect();
      const out: { k: BlockKey; d: string[] }[] = [];
      (Object.keys(blocks) as BlockKey[]).forEach((k) => {
        const b = blocks[k].current?.getBoundingClientRect();
        if (!b) return;
        const leftSide = b.right <= C.left + 1;
        const x1 = (leftSide ? C.left : C.right) - H.left;
        const x2 = (leftSide ? b.right : b.left) - H.left;
        const yc = b.top + b.height / 2 - H.top;
        const dir = leftSide ? -1 : 1;
        // گذرگاه سه‌سیمه با شکست ۴۵ درجه در میانه فاصله
        const d = [-6, 0, 6].map((o, i) => {
          const y1 = yc + o;
          const mid = x1 + dir * (Math.abs(x2 - x1) / 2 - 4 + i * 4);
          const jog = (i - 1) * 3;
          return `M${x1},${y1} H${mid} L${mid + dir * 6},${y1 + jog} H${x2}`;
        });
        out.push({ k, d });
      });
      setBox({ w: H.width, h: H.height });
      setPaths(out);
    };
    calc();
    const ro = new ResizeObserver(calc);
    ro.observe(el);
    window.addEventListener("resize", calc);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", calc);
    };
  }, [host, core, blocks]);
  if (!paths.length) return null;
  return (
    <svg className="pointer-events-none absolute inset-0 z-0 hidden xl:block" width={box.w} height={box.h} aria-hidden>
      <defs>
        <linearGradient id="film-trace" x1="0" x2="1">
          <stop offset="0" stopColor="rgb(var(--film-1))" />
          <stop offset=".35" stopColor="rgb(var(--film-2))" />
          <stop offset=".65" stopColor="rgb(var(--film-3))" />
          <stop offset="1" stopColor="rgb(var(--film-5))" />
        </linearGradient>
      </defs>
      {paths.map(({ k, d }) => {
        const mode = links[k];
        return d.map((p, i) => (
          <g key={`${k}${i}`}>
            <path d={p} fill="none" stroke="rgb(var(--border) / .28)" strokeWidth="1" />
            {mode && (
              <path d={p} fill="none" strokeWidth={mode === "flow" ? 1.6 : 1.2} strokeLinecap="square"
                stroke={mode === "warn" ? "rgb(var(--warning))" : "url(#film-trace)"}
                strokeDasharray={mode === "idle" ? "2 62" : "10 54"}
                style={{ animation: `trace-flow ${mode === "flow" ? 1.4 + i * 0.2 : mode === "warn" ? 1.1 : 4 + i}s linear infinite`, opacity: mode === "idle" ? 0.6 : 1 }} />
            )}
          </g>
        ));
      })}
    </svg>
  );
}

export default function Dashboard() {
  const { data: d, isLoading, error, refetch } = useDashboard();
  const nav = useNavigate();
  const [ask, setAsk] = useState("");
  const metrics = useLiveMetrics([], 40);
  const state = agentState(d);
  const [title, sub] = STATE_TEXT[state];
  const host = useRef<HTMLElement>(null);
  const core = useRef<HTMLDivElement>(null);
  const refs = {
    memory: useRef<HTMLButtonElement>(null),
    models: useRef<HTMLButtonElement>(null),
    dream: useRef<HTMLButtonElement>(null),
    tools: useRef<HTMLButtonElement>(null),
    employees: useRef<HTMLButtonElement>(null),
    changes: useRef<HTMLButtonElement>(null),
  };
  const blocksRef = useRef(refs).current;

  if (error) return <ErrorBox error={error} retry={refetch} />;

  const c = d?.control || {};
  const today = d?.usage?.today || {};
  const month = d?.usage?.month || {};
  const sys = metrics.length ? { ...d?.system, ...metrics[metrics.length - 1] } : d?.system || {};
  const hourly = (() => {
    const map = new Map<number, any>();
    for (const h of d?.usage?.hourly || []) map.set(new Date(h.ts).setMinutes(0, 0, 0), h);
    const now = new Date();
    now.setMinutes(0, 0, 0);
    return Array.from({ length: 24 }, (_, i) => {
      const t = now.getTime() - (23 - i) * 3600_000;
      const h = map.get(t) || { tokens_in: 0, tokens_out: 0, cost: 0 };
      return { ...h, ts: new Date(t).toISOString(), total: (h.tokens_in || 0) + (h.tokens_out || 0) };
    });
  })();
  const tasks = d?.tasks?.counts || {};
  const openTasks = (tasks.todo || 0) + (tasks.in_progress || 0) + (tasks.waiting || 0);
  const mem = c.memory || {};
  const memMax = Math.max(1, mem.behavioral || 0, mem.semantic || 0, mem.episodic || 0, mem.experience || 0);
  const activeRuns = sys.active_runs ?? d?.runs?.counts?.running ?? 0;

  const submit = () => {
    if (!ask.trim()) return;
    nav(`/chat?q=${encodeURIComponent(ask.trim())}`);
  };

  return (
    <div className="space-y-5">
      {/* ── نقشه زنده تراشه ── */}
      <section ref={host} className="relative grid gap-3 sm:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,1fr)] xl:gap-x-10 xl:gap-y-4" aria-label="نقشه زنده اجزای ایجنت">
        <Traces host={host} core={core} blocks={blocksRef} links={activeLinks(state, c)} />

        {/* هسته مدیر */}
        <div ref={core} data-sheen className={cn("die die-lg relative z-[1] flex flex-col items-center gap-4 p-5 text-center sm:col-span-2 xl:col-span-1 xl:col-start-2 xl:row-span-3 xl:row-start-1 xl:justify-center", state === "stopped" ? "bg-danger/50" : "die-live", state === "idle" && "[animation-duration:24s]")}>
          <div className="flex w-full items-center justify-between text-xs text-muted">
            <span className="flex items-center gap-1.5"><CircuitBoard className="h-3.5 w-3.5" /> هسته مدیر</span>
            <span className="ltr mono">{d?.agent?.name || "X Agent"}</span>
          </div>
          <PadArray state={state} n={9} size={typeof window !== "undefined" && window.innerWidth < 640 ? 132 : 176} />
          <div>
            <h2 className="text-[22px] font-extrabold leading-8">{title}</h2>
            <p className="mt-0.5 text-sm text-muted">{sub}</p>
          </div>
          <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="bevel flex w-full max-w-md items-center gap-2 bg-surface2/80 p-1.5 ps-3.5 ring-1 ring-inset ring-line/20 focus-within:ring-primary/50">
            <input value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="کاری به مدیر بسپار…" aria-label="کار برای مدیر"
              className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none" />
            <Button type="submit" variant="primary" size="icon" aria-label="ارسال به مدیر"><Send className="h-4 w-4 -scale-x-100" /></Button>
          </form>
          <div className="grid w-full grid-cols-4 gap-2 border-t hairline pt-3 text-center">
            {[
              ["کار فعال", faInt(activeRuns)],
              ["منتظر اجازه", faInt(d?.approvals_pending || 0)],
              ["موفقیت ۷ روز", d?.success_rate_7d != null ? `${faNum(d.success_rate_7d * 100)}٪` : "—"],
              ["فراخوانی امروز", faInt(today.calls || 0)],
            ].map(([l, v]) => (
              <div key={l as string} className="min-w-0">
                <div className="num truncate text-base font-bold">{v}</div>
                <div className="truncate text-[10px] text-muted">{l}</div>
              </div>
            ))}
          </div>
        </div>

        {/* ستون شروع (راست در RTL): حافظه، مدل‌ها، رویا */}
        <DieBlock ref={refs.memory} title="حافظه ۵ لایه" icon={<Brain className="h-4 w-4" />} onClick={() => nav("/memory")} className="z-[1] xl:col-start-1 xl:row-start-1">
          {isLoading ? <Skeleton className="h-16" /> : (
            <span className="grid gap-1.5">
              {([["رفتاری", mem.behavioral], ["معنایی", mem.semantic], ["رویدادی", mem.episodic], ["تجربه", mem.experience]] as [string, number][]).map(([l, v]) => (
                <span key={l} className="grid grid-cols-[3.5rem_1fr_2.5rem] items-center gap-2 text-[11px]">
                  <span className="text-muted">{l}</span>
                  <span className="h-1.5 bg-line/12"><span className="block h-full bg-accent" style={{ width: `${v ? Math.max(4, (Math.log10(v + 1) / Math.log10(memMax + 1)) * 100) : 0}%` }} /></span>
                  <span className="num text-end">{compact(v || 0)}</span>
                </span>
              ))}
            </span>
          )}
        </DieBlock>
        <DieBlock ref={refs.models} title="مدل‌های هوش مصنوعی" icon={<Cpu className="h-4 w-4" />} onClick={() => nav("/settings?tab=ai")} alert={(c.models?.failing || 0) > 0} className="z-[1] xl:col-start-1 xl:row-start-2">
          {isLoading ? <Skeleton className="h-12" /> : (
            <span className="space-y-3">
              <Pads total={c.models?.enabled || 0} on={(c.models?.enabled || 0) - (c.models?.failing || 0) - (c.models?.degraded || 0)} warn={c.models?.degraded || 0} bad={c.models?.failing || 0} />
              <Readout items={[["فعال", faInt(c.models?.enabled || 0)], ["کاهش کیفیت", faInt(c.models?.degraded || 0)], ["از کار افتاده", faInt(c.models?.failing || 0)]]} />
            </span>
          )}
        </DieBlock>
        <DieBlock ref={refs.dream} title="رویا و خودبهبودی" icon={<Moon className="h-4 w-4" />} onClick={() => nav("/dream")} className="z-[1] xl:col-start-1 xl:row-start-3">
          {isLoading ? <Skeleton className="h-10" /> : c.dream ? (
            <span className="flex items-center justify-between gap-2 text-sm">
              <span className="min-w-0">
                <span className="block truncate">{c.dream.status === "running" ? "در حال رویا دیدن" : c.dream.status === "done" ? "آخرین رویا کامل شد" : c.dream.status === "failed" ? "آخرین رویا ناموفق بود" : "آخرین رویا رد شد"}</span>
                <span className="num block text-[11px] text-muted">{relTime(c.dream.started_at)}</span>
              </span>
              <Badge tone={c.dream.status === "failed" ? "danger" : c.dream.status === "running" ? "primary" : "neutral"} dot>{c.dream.status === "running" ? "فعال" : "خواب"}</Badge>
            </span>
          ) : <span className="text-[12px] leading-5 text-muted">هنوز رویایی اجرا نشده؛ پس از چند کار، مدیر شب‌ها از تجربه‌ها درس می‌گیرد.</span>}
        </DieBlock>

        {/* ستون پایان (چپ در RTL): ابزارها، کارمندان، تغییرات */}
        <DieBlock ref={refs.tools} title="ابزارها و مهارت‌ها" icon={<Wrench className="h-4 w-4" />} onClick={() => nav("/tools")} alert={(c.tools?.error || 0) > 0} className="z-[1] xl:col-start-3 xl:row-start-1">
          {isLoading ? <Skeleton className="h-12" /> : (
            <span className="space-y-3">
              <Pads total={(c.tools?.ready || 0) + (c.tools?.error || 0)} on={c.tools?.ready || 0} bad={c.tools?.error || 0} />
              <Readout items={[["ابزار آماده", faInt(c.tools?.ready || 0)], ["خطادار", faInt(c.tools?.error || 0)], ["مهارت آموخته", faInt(c.tools?.skills || 0)]]} />
            </span>
          )}
        </DieBlock>
        <DieBlock ref={refs.employees} title="کارمندان" icon={<Users className="h-4 w-4" />} onClick={() => nav("/control?tab=employees")} className="z-[1] xl:col-start-3 xl:row-start-2">
          {isLoading ? <Skeleton className="h-12" /> : (
            <span className="space-y-3">
              <Pads total={c.employees?.total || 0} on={c.employees?.busy || 0} warn={0} />
              <Readout items={[["کل", faInt(c.employees?.total || 0)], ["فعال", faInt(c.employees?.enabled || 0)], ["مشغول کار", faInt(c.employees?.busy || 0)]]} />
            </span>
          )}
        </DieBlock>
        <DieBlock ref={refs.changes} title="تغییرات و رخدادها" icon={<History className="h-4 w-4" />} onClick={() => nav("/control?tab=changes")} alert={(c.incidents_open || 0) > 0 || state === "waiting"} className="z-[1] xl:col-start-3 xl:row-start-3">
          {isLoading ? <Skeleton className="h-12" /> : (
            <span className="space-y-2">
              <Readout items={[["تغییر ۲۴ ساعت", faInt(c.changes_24h || 0)], ["رخداد باز", faInt(c.incidents_open || 0)], ["اجازه", faInt(d?.approvals_pending || 0)]]} />
              {c.last_change && <span className="block truncate text-[11px] text-muted">آخرین: {c.last_change.summary}</span>}
            </span>
          )}
        </DieBlock>
      </section>

      {/* ── مصرف و بار ── */}
      <section className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="توکن مصرفی ۲۴ ساعت اخیر" icon={<Zap className="h-4 w-4" />} sub={`امروز ${compact((today.tokens_in || 0) + (today.tokens_out || 0))} توکن · کش ${compact(today.cache_read)}`}
            actions={<span className="num text-xs text-muted">{usd(today.cost, 4)}</span>} />
          <div className="px-3 pb-3 pt-2">
            <AreaChart data={hourly} height={200} series={[{ key: "tokens_in", label: "ورودی", color: CHART.in }, { key: "tokens_out", label: "خروجی", color: CHART.out }]}
              xLabel={(h) => fmtTime(h.ts)} format={(v) => compact(v)} />
          </div>
          <div className="grid gap-3 border-t hairline px-4 py-3 text-xs sm:grid-cols-2 sm:px-5">
            <div className="space-y-1.5">
              <div className="flex justify-between"><span className="text-muted">هزینه امروز از سقف</span><span className="num">{usd(today.cost, 4)} / {usd(today.limit, 2)}</span></div>
              <Meter value={today.cost || 0} max={today.limit || 1} />
            </div>
            <div className="space-y-1.5">
              <div className="flex justify-between"><span className="text-muted">هزینه ماه از سقف</span><span className="num">{usd(month.cost, 3)} / {usd(month.limit, 2)}</span></div>
              <Meter value={month.cost || 0} max={month.limit || 1} tone="accent" />
            </div>
          </div>
        </Card>
        <Card>
          <CardHeader title="درگیری سیستم" icon={<MemoryStick className="h-4 w-4" />} sub={`Load ${faNum(sys.load1, 2)} · روشن ${uptime(sys.uptime)}`} />
          {isLoading ? <div className="p-4"><Skeleton className="h-32" /></div> : (
            <div className="space-y-4 px-4 pb-4 pt-3 sm:px-5">
              <Gauge value={sys.cpu} label="پردازنده" sub={`${faInt(sys.cpu_count)} هسته`} color="rgb(var(--cyan))" />
              <Gauge value={sys.mem} label="حافظه" sub={bytes(sys.mem_used)} color="rgb(var(--accent))" />
              <Gauge value={sys.disk} label="دیسک" sub={`${bytes(sys.disk_used)} از ${bytes(sys.disk_total)}`} color="rgb(var(--primary))" />
              <div className="border-t hairline pt-3">
                <div className="mb-1 flex justify-between text-[11px] text-muted"><span>پردازنده در دقایق اخیر</span><span className="flex items-center gap-1"><HardDrive className="h-3 w-3" /> Swap {faNum(sys.swap)}٪</span></div>
                <Sparkline values={metrics.length ? metrics.map((m) => m.cpu) : [sys.cpu || 0, sys.cpu || 0]} color={CHART.cpu} height={30} />
              </div>
            </div>
          )}
        </Card>
      </section>

      {/* ── جریان کار ── */}
      <section className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader title="کارهای در جریان" icon={<Sparkles className="h-4 w-4" />} actions={<Button size="sm" variant="ghost" onClick={() => nav("/chat")}>گفتگوها</Button>} />
          <div className="space-y-1.5 p-3">
            {(d?.runs?.active || []).length === 0 && <p className="px-2 py-6 text-center text-sm text-muted">کار فعالی در جریان نیست. از کادر هسته مدیر کاری بسپارید.</p>}
            {(d?.runs?.active || []).map((r: any) => (
              <button key={r.id} onClick={() => r.conversation_id && nav(`/chat/${r.conversation_id}`)} className="flex w-full items-center justify-between gap-3 rounded-[4px] px-2.5 py-2 text-start transition-colors hover:bg-line/8">
                <div className="min-w-0">
                  <div className="truncate text-sm">{r.trigger === "routine" ? "روتین" : r.trigger === "telegram" ? "تلگرام" : r.trigger === "delegate" ? "کارمند" : "گفتگو"} · {fmtTime(r.created_at)}</div>
                  <div className="num text-[11px] text-muted">{compact(r.tokens_in + r.tokens_out)} توکن · {usd(r.cost_usd)}</div>
                </div>
                <Badge tone={RUN_STATUS[r.status]?.tone || "neutral"} dot>{RUN_STATUS[r.status]?.label || r.status}</Badge>
              </button>
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="وظایف و روتین‌ها" icon={<CalendarCheck className="h-4 w-4" />} actions={<Button size="sm" variant="ghost" onClick={() => nav("/tasks")}>وظایف</Button>} />
          <div className="p-4 pt-3">
            <div className="grid grid-cols-3 gap-px overflow-hidden rounded-[4px] bg-line/12 text-center">
              {[["باز", openTasks, "text-primary"], ["در حال انجام", tasks.in_progress || 0, "text-cyan"], ["انجام‌شده", tasks.done || 0, "text-success"]].map(([l, v, cl]) => (
                <div key={l as string} className="bg-surface py-2">
                  <div className={cn("num text-lg font-bold", cl as string)}>{faInt(v as number)}</div>
                  <div className="text-[11px] text-muted">{l}</div>
                </div>
              ))}
            </div>
            <div className="mt-3 space-y-1.5">
              {(d?.tasks?.due_soon || []).slice(0, 3).map((t: any) => (
                <div key={t.id} className="flex items-center justify-between gap-2 text-xs">
                  <span className="truncate">{t.title}</span>
                  <span className="num shrink-0 text-warning">{fmtDate(t.due_at)}</span>
                </div>
              ))}
              {(d?.routines?.next || []).slice(0, 3).map((r: any) => (
                <button key={r.id} onClick={() => nav("/routines")} className="flex w-full items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-2"><Repeat className="h-3.5 w-3.5 shrink-0 text-accent" /><span className="truncate">{r.name}</span></span>
                  <span className="num flex shrink-0 items-center gap-1 text-muted"><Clock className="h-3 w-3" />{fmtDate(r.next_run_at)}</span>
                </button>
              ))}
              {!(d?.tasks?.due_soon || []).length && !(d?.routines?.next || []).length && <p className="py-2 text-center text-xs text-muted">کار سررسیددار یا روتین زمان‌بندی‌شده‌ای نیست.</p>}
            </div>
          </div>
        </Card>
        <Card>
          <CardHeader title="اعلان‌های اخیر" icon={<ShieldAlert className="h-4 w-4" />} />
          <div className="space-y-0.5 p-3">
            {(d?.notifications?.items || []).length === 0 && <p className="py-6 text-center text-sm text-muted">اعلانی نیست.</p>}
            {(d?.notifications?.items || []).slice(0, 6).map((n: any) => (
              <div key={n.id} className="flex items-start gap-2.5 rounded-[4px] px-2 py-2 hover:bg-line/6">
                <span className={cn("pad mt-2", { success: "text-success", warning: "text-warning", error: "text-danger", approval: "text-warning" }[n.level as string] || "text-primary")} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">{n.title}</div>
                  {n.body && <div className="line-clamp-1 text-xs text-muted">{n.body}</div>}
                </div>
                <span className="shrink-0 text-[10px] text-muted">{relTime(n.ts)}</span>
              </div>
            ))}
          </div>
        </Card>
      </section>
    </div>
  );
}
