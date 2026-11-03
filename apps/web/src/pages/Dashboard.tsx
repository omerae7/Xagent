import { motion } from "framer-motion";
import {
  ArrowLeft,
  CalendarCheck,
  CheckCircle2,
  Clock,
  Cpu,
  HardDrive,
  MemoryStick,
  Repeat,
  Send,
  ShieldAlert,
  Sparkles,
  Timer,
  Zap,
} from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { agentState, useDashboard } from "../components/layout/Shell";
import { Badge, Button, Card, CardHeader, cn, ErrorBox, Skeleton } from "../components/ui";
import { AreaChart, CHART, Gauge, Meter, Sparkline } from "../components/viz/charts";
import { Orb } from "../components/viz/Orb";
import { bytes, compact, faInt, faNum, fmtDate, fmtTime, relTime, uptime, usd } from "../lib/format";
import { useLiveMetrics } from "../lib/live";

const STATE_TEXT: Record<string, [string, string]> = {
  idle: ["آماده به کار", "منتظر دستور شما هستم."],
  thinking: ["در حال فکر…", "در حال تحلیل و برنامه‌ریزی کار."],
  executing: ["در حال اجرا", "مشغول انجام کارها در پس‌زمینه."],
  waiting: ["منتظر اجازه", "یک عملیات حساس منتظر تایید شماست."],
  stopped: ["توقف اضطراری", "همه کارها متوقف شده‌اند."],
};

const RUN_STATUS: Record<string, { label: string; tone: any }> = {
  queued: { label: "در صف", tone: "neutral" },
  running: { label: "در حال اجرا", tone: "primary" },
  waiting_approval: { label: "منتظر اجازه", tone: "warning" },
  paused: { label: "متوقف", tone: "warning" },
};

function Connector({ d, delay = 0 }: { d: string; delay?: number }) {
  return (
    <motion.path d={d} fill="none" stroke="url(#conn)" strokeWidth="1.4" strokeDasharray="5 7"
      initial={{ pathLength: 0, opacity: 0 }} animate={{ pathLength: 1, opacity: 1, strokeDashoffset: [0, -48] }}
      transition={{ pathLength: { duration: 1.2, delay }, opacity: { duration: 0.6, delay }, strokeDashoffset: { duration: 3, repeat: Infinity, ease: "linear" } }} />
  );
}

export default function Dashboard() {
  const { data: d, isLoading, error, refetch } = useDashboard();
  const nav = useNavigate();
  const [ask, setAsk] = useState("");
  const metrics = useLiveMetrics([], 40);
  const state = agentState(d);
  const [title, sub] = STATE_TEXT[state];

  if (error) return <ErrorBox error={error} retry={refetch} />;

  const today = d?.usage?.today || {};
  const month = d?.usage?.month || {};
  const sys = metrics.length ? { ...d?.system, ...metrics[metrics.length - 1] } : d?.system || {};
  // ۲۴ بازه ساعتی کامل (ساعت‌های بدون مصرف = صفر) تا نمودار پیوسته باشد
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

  const submit = () => {
    if (!ask.trim()) return;
    nav(`/chat?q=${encodeURIComponent(ask.trim())}`);
  };

  return (
    <div className="space-y-5">
      {/* ── بخش قهرمان: هسته ایجنت و کارت‌های متصل ── */}
      <section className="relative grid gap-4 xl:grid-cols-[1fr_minmax(340px,420px)_1fr] xl:items-center">
        <svg className="pointer-events-none absolute inset-0 hidden h-full w-full xl:block" preserveAspectRatio="none" viewBox="0 0 1000 400">
          <defs>
            <linearGradient id="conn" x1="0" x2="1">
              <stop offset="0" stopColor="rgb(var(--cyan))" stopOpacity=".1" />
              <stop offset=".5" stopColor="rgb(var(--primary))" stopOpacity=".8" />
              <stop offset="1" stopColor="rgb(var(--accent))" stopOpacity=".1" />
            </linearGradient>
          </defs>
          <Connector d="M500,200 C420,200 400,95 300,95" />
          <Connector d="M500,200 C420,200 400,305 300,305" delay={0.15} />
          <Connector d="M500,200 C580,200 600,95 700,95" delay={0.3} />
          <Connector d="M500,200 C580,200 600,305 700,305" delay={0.45} />
        </svg>

        {/* ستون راست (در RTL اول): مصرف توکن و بار سیستم */}
        <div className="order-2 grid gap-4 sm:grid-cols-2 xl:order-1 xl:grid-cols-1">
          <Card className="p-4" glow>
            <div className="mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm font-semibold"><Zap className="h-4 w-4 text-primary" /> مصرف توکن</span>
              <Badge tone="primary">امروز</Badge>
            </div>
            {isLoading ? <Skeleton className="h-16" /> : (
              <>
                <div className="flex items-end justify-between gap-2">
                  <div>
                    <div className="num text-3xl font-extrabold text-gradient">{compact((today.tokens_in || 0) + (today.tokens_out || 0))}</div>
                    <div className="num mt-1 text-xs text-muted">ورودی {compact(today.tokens_in)} · خروجی {compact(today.tokens_out)} · کش {compact(today.cache_read)}</div>
                  </div>
                  <div className="w-24"><Sparkline values={hourly.map((h: any) => h.total)} /></div>
                </div>
                <div className="mt-3 space-y-2 text-xs">
                  <div className="flex justify-between"><span className="text-muted">هزینه امروز</span><span className="num">{usd(today.cost, 4)} / {usd(today.limit, 2)}</span></div>
                  <Meter value={today.cost || 0} max={today.limit || 1} />
                  <div className="flex justify-between"><span className="text-muted">هزینه ماه</span><span className="num">{usd(month.cost, 3)} / {usd(month.limit, 2)}</span></div>
                  <Meter value={month.cost || 0} max={month.limit || 1} tone="accent" />
                </div>
              </>
            )}
          </Card>
          <Card className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm font-semibold"><Cpu className="h-4 w-4 text-cyan" /> درگیری سیستم</span>
              <span className="num text-[11px] text-muted">Load {faNum(sys.load1, 2)}</span>
            </div>
            {isLoading ? <Skeleton className="h-24" /> : (
              <div className="grid grid-cols-3 gap-1">
                <Gauge value={sys.cpu} label="پردازنده" sub={`${faInt(sys.cpu_count)} هسته`} size={84} />
                <Gauge value={sys.mem} label="حافظه" sub={bytes(sys.mem_used)} size={84} color="rgb(var(--accent))" />
                <Gauge value={sys.disk} label="دیسک" sub={bytes(sys.disk_used)} size={84} color="rgb(var(--primary))" />
              </div>
            )}
          </Card>
        </div>

        {/* مرکز: گوی ایجنت */}
        <div className="order-1 flex flex-col items-center gap-4 py-2 text-center xl:order-2">
          <Orb state={state} size={typeof window !== "undefined" && window.innerWidth < 640 ? 190 : 250} className="animate-float" />
          <div>
            <h2 className="text-2xl font-extrabold">{title}</h2>
            <p className="mt-1 text-sm text-muted">{sub}</p>
          </div>
          <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="glass flex w-full max-w-md items-center gap-2 rounded-2xl p-1.5 ps-4 shadow-glow">
            <input value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="از ایجنت بپرس یا کاری بسپار…" className="h-10 flex-1 bg-transparent text-sm outline-none placeholder:text-muted" />
            <Button type="submit" variant="primary" size="icon" aria-label="ارسال"><Send className="h-4 w-4 -scale-x-100" /></Button>
          </form>
        </div>

        {/* ستون چپ: وظایف و روتین‌ها */}
        <div className="order-3 grid gap-4 sm:grid-cols-2 xl:grid-cols-1">
          <Card className="cursor-pointer p-4 transition hover:shadow-glow" onClick={() => nav("/tasks")}>
            <div className="mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm font-semibold"><CalendarCheck className="h-4 w-4 text-success" /> وظایف</span>
              <ArrowLeft className="h-4 w-4 text-muted" />
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              {[["باز", openTasks, "text-primary"], ["در حال انجام", tasks.in_progress || 0, "text-cyan"], ["انجام‌شده", tasks.done || 0, "text-success"]].map(([l, v, c]) => (
                <div key={l as string} className="rounded-xl bg-line/6 py-2">
                  <div className={cn("num text-xl font-bold", c as string)}>{faInt(v as number)}</div>
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
            </div>
          </Card>
          <Card className="cursor-pointer p-4 transition hover:shadow-glow" onClick={() => nav("/routines")}>
            <div className="mb-3 flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm font-semibold"><Repeat className="h-4 w-4 text-accent" /> روتین‌های بعدی</span>
              <Badge tone="accent">{faInt(d?.routines?.total || 0)} روتین</Badge>
            </div>
            <div className="space-y-2">
              {(d?.routines?.next || []).length === 0 && <p className="text-xs text-muted">روتین فعالی زمان‌بندی نشده است.</p>}
              {(d?.routines?.next || []).slice(0, 4).map((r: any) => (
                <div key={r.id} className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-2"><Clock className="h-3.5 w-3.5 shrink-0 text-muted" /><span className="truncate">{r.name}</span></span>
                  <span className="num shrink-0 text-muted">{fmtDate(r.next_run_at)}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </section>

      {/* ── ردیف آمار ── */}
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          { l: "نرخ موفقیت ۷ روز", v: d?.success_rate_7d != null ? `${faNum(d.success_rate_7d * 100)}٪` : "—", s: `${faInt(d?.episodes_7d || 0)} کار ثبت‌شده`, i: <CheckCircle2 className="h-4 w-4" />, t: "success" },
          { l: "فراخوانی مدل امروز", v: faInt(today.calls || 0), s: `کش: ${compact(today.cache_read)} توکن`, i: <Sparkles className="h-4 w-4" />, t: "primary" },
          { l: "درخواست اجازه", v: faInt(d?.approvals_pending || 0), s: "منتظر تصمیم شما", i: <ShieldAlert className="h-4 w-4" />, t: "warning" },
          { l: "زمان روشن بودن", v: uptime(sys.uptime), s: `Swap ${faNum(sys.swap)}٪`, i: <Timer className="h-4 w-4" />, t: "cyan" },
        ].map((x, i) => (
          <motion.div key={x.l} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 + i * 0.06 }}>
            <div className="glass relative overflow-hidden rounded-card p-4">
              <div className={cn("absolute -end-8 -top-8 h-24 w-24 rounded-full opacity-20 blur-2xl", `bg-${x.t}`)} />
              <div className="flex items-center justify-between text-xs text-muted">{x.l}<span className={`text-${x.t}`}>{x.i}</span></div>
              <div className="num mt-2 truncate text-xl font-bold sm:text-2xl">{x.v}</div>
              <div className="mt-0.5 truncate text-[11px] text-muted">{x.s}</div>
            </div>
          </motion.div>
        ))}
      </section>

      {/* ── نمودارها و فعالیت‌ها ── */}
      <section className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="توکن مصرفی ۲۴ ساعت اخیر" icon={<Zap className="h-4 w-4" />} sub="ورودی و خروجی به تفکیک ساعت" />
          <div className="px-3 pb-4 pt-2">
            <AreaChart data={hourly} height={210} series={[{ key: "tokens_in", label: "ورودی", color: CHART.in }, { key: "tokens_out", label: "خروجی", color: CHART.out }]}
              xLabel={(h) => fmtTime(h.ts)} format={(v) => compact(v)} />
          </div>
        </Card>
        <Card>
          <CardHeader title="بار لحظه‌ای سیستم" icon={<MemoryStick className="h-4 w-4" />} sub="به‌روزرسانی زنده هر ۳۰ ثانیه" />
          <div className="space-y-3 px-4 pb-4 pt-3">
            {[["پردازنده", "cpu", CHART.cpu], ["حافظه", "mem", CHART.mem], ["دیسک", "disk", CHART.disk]].map(([l, k, c]) => (
              <div key={k}>
                <div className="mb-1 flex justify-between text-xs"><span className="text-muted">{l}</span><span className="num">{faNum(sys[k as string])}٪</span></div>
                <Sparkline values={metrics.length ? metrics.map((m) => m[k as string]) : [sys[k as string] || 0, sys[k as string] || 0]} color={c as string} height={30} />
              </div>
            ))}
            <div className="flex justify-between border-t hairline pt-2 text-[11px] text-muted">
              <span className="flex items-center gap-1"><HardDrive className="h-3 w-3" /> {bytes(sys.disk_used)} از {bytes(sys.disk_total)}</span>
              <span>کارهای فعال: {faInt(sys.active_runs ?? d?.runs?.counts?.running ?? 0)}</span>
            </div>
          </div>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="کارهای در جریان" icon={<Sparkles className="h-4 w-4" />} actions={<Button size="sm" variant="ghost" onClick={() => nav("/chat")}>همه</Button>} />
          <div className="space-y-2 p-4">
            {(d?.runs?.active || []).length === 0 && <p className="py-6 text-center text-sm text-muted">کار فعالی در جریان نیست.</p>}
            {(d?.runs?.active || []).map((r: any) => (
              <button key={r.id} onClick={() => r.conversation_id && nav(`/chat/${r.conversation_id}`)} className="flex w-full items-center justify-between gap-3 rounded-xl bg-line/5 px-3 py-2.5 text-start transition hover:bg-line/10">
                <div className="min-w-0">
                  <div className="truncate text-sm">{r.trigger === "routine" ? "روتین" : r.trigger === "telegram" ? "تلگرام" : "گفتگو"} — {fmtTime(r.created_at)}</div>
                  <div className="num text-[11px] text-muted">{compact(r.tokens_in + r.tokens_out)} توکن · {usd(r.cost_usd)}</div>
                </div>
                <Badge tone={RUN_STATUS[r.status]?.tone || "neutral"} dot>{RUN_STATUS[r.status]?.label || r.status}</Badge>
              </button>
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="اعلان‌های اخیر" icon={<ShieldAlert className="h-4 w-4" />} />
          <div className="space-y-1 p-3">
            {(d?.notifications?.items || []).length === 0 && <p className="py-6 text-center text-sm text-muted">اعلانی نیست.</p>}
            {(d?.notifications?.items || []).slice(0, 6).map((n: any) => (
              <div key={n.id} className="flex items-start gap-2.5 rounded-xl px-2 py-2 hover:bg-line/6">
                <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", { success: "bg-success", warning: "bg-warning", error: "bg-danger", approval: "bg-warning" }[n.level as string] || "bg-primary")} />
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
