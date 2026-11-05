/**
 * داشبورد «مغز دیجیتال»: صحنه آبی موسمی با مغز زنده X Agent؛ ورودی‌ها (کارفرما، تلگرام، مأموریت‌ها، اجازه‌ها)
 * از یک سو و خروجی‌ها (اقدام، میز کار، حافظه، کارمندان) از سوی دیگر با خطوط نازک به آن وصل‌اند و فقط
 * خطوطی که واقعاً فعال‌اند پالس دارند. زیر صحنه: خط جریان وضعیت اجراها (تعریف واحد با API)، میز کار،
 * مأموریت‌های فعال، مصرف و بار سیستم. همه اعداد از API خود سیستم می‌آیند.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { ArrowLeft, Clock, Inbox, MemoryStick, Repeat, Send, Zap } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { agentState, useDashboard } from "../components/layout/Shell";
import { Badge, Button, Card, CardHeader, cn, ErrorBox, Skeleton } from "../components/ui";
import { type BrainLink } from "../components/viz/Brain";
import { Orbit } from "../components/viz/Orbit";
import { AreaChart, CHART, Gauge, Meter, Sparkline } from "../components/viz/charts";
import type { AgentState } from "../components/viz/Silicon";
import { get, post } from "../lib/api";
import { bytes, compact, faInt, faNum, fmtDate, fmtTime, relTime, uptime, usd } from "../lib/format";
import { useLiveMetrics } from "../lib/live";
import { toast, toastError } from "../lib/store";

const STATE_TEXT: Record<AgentState, [string, string]> = {
  idle: ["آماده به کار", "مدیر منتظر مأموریت بعدی شماست."],
  thinking: ["در حال فکر", "مدیر در حال تحلیل و برنامه‌ریزی است."],
  executing: ["در حال اجرا", "مدیر و کارمندان مشغول انجام کارها هستند."],
  waiting: ["منتظر اجازه شما", "یک عملیات حساس منتظر تأیید شماست."],
  stopped: ["توقف اضطراری", "هیچ کار یا روتین جدیدی اجرا نمی‌شود."],
};

const EXAMPLES = [
  "تا ۱۰ دقیقه آینده هر دقیقه تلگرام را بررسی کن و موارد مهم را گزارش بده",
  "چه کارهایی می‌توانی انجام بدهی؟",
];

const SEV_TONE: Record<string, string> = { critical: "text-danger", important: "text-warning", info: "text-primary" };
const KIND_LABEL: Record<string, string> = {
  report: "گزارش", error: "خطا", approval: "اجازه", mission: "مأموریت", message: "پیام", long_task: "کار طولانی", employee: "کارمند",
};

/** خط جریان وضعیت اجراها: هر گره یک وضعیت با تعریف یکسان API/UI */
function RunFlow({ s, onCleanup }: { s: any; onCleanup: () => void }) {
  const nodes: { k: string; label: string; v: number; tone: string; hint: string }[] = [
    { k: "queued", label: "در صف", v: s?.queued || 0, tone: "rgb(var(--muted))", hint: "منتظر Worker" },
    { k: "running", label: "در حال اجرا", v: s?.running || 0, tone: "rgb(var(--cyan))", hint: "کار فعال" },
    { k: "waiting_approval", label: "منتظر اجازه", v: s?.waiting_approval || 0, tone: "rgb(var(--warning))", hint: "تصمیم شما" },
    { k: "paused", label: "متوقف موقت", v: s?.paused || 0, tone: "rgb(var(--accent))", hint: "قابل ادامه" },
    { k: "succeeded_today", label: "انجام‌شده امروز", v: s?.succeeded_today || 0, tone: "rgb(var(--success))", hint: "پایان موفق" },
    { k: "failed_today", label: "ناموفق امروز", v: s?.failed_today || 0, tone: "rgb(var(--danger))", hint: "نیازمند بررسی" },
  ];
  return (
    <Card className="px-5 pb-5 pt-4 sm:px-7">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[16px] font-semibold">جریان کارها</h2>
        <span className="text-[12.5px] text-muted">«فعال» = در صف + در حال اجرا؛ منتظر اجازه و متوقف موقت جدا شمرده می‌شوند</span>
      </div>
      <div className="relative mt-5">
        <div className="absolute inset-x-[8%] top-[11px] hidden h-px bg-gradient-to-l from-line/10 via-line/35 to-line/10 md:block" />
        <ol className="relative grid grid-cols-3 gap-x-3 gap-y-5 md:grid-cols-6">
          {nodes.map((n, i) => (
            <motion.li key={n.k} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 * i, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
              className="flex flex-col items-center text-center">
              <span className="relative grid h-[22px] w-[22px] place-items-center rounded-full bg-surface" style={{ boxShadow: `inset 0 0 0 1.5px ${n.tone}, 0 0 0 5px rgb(var(--surface) / .7)` }}>
                {n.v > 0 && <span className="h-2 w-2 rounded-full" style={{ background: n.tone }} />}
                {n.v > 0 && (n.k === "running" || n.k === "waiting_approval") && (
                  <span className="absolute inset-0 rounded-full" style={{ border: `1px solid ${n.tone}`, animation: "node-pulse 1.8s ease-out infinite" }} />
                )}
              </span>
              <span className="num mt-2 text-[30px] font-light leading-9">{faInt(n.v)}</span>
              <span className="text-[13px] font-medium">{n.label}</span>
              <span className="text-[11.5px] text-muted">{n.hint}</span>
            </motion.li>
          ))}
        </ol>
      </div>
      {(s?.paused || 0) > 0 && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-accent/[.06] px-4 py-2.5 text-[13px] ring-1 ring-inset ring-accent/15">
          <span className="text-muted">اجراهای متوقف موقت با «ادامه» در گفتگو قابل ازسرگیری‌اند؛ قدیمی‌ترها را می‌توانید ببندید (سابقه حذف نمی‌شود).</span>
          <Button size="sm" variant="ghost" onClick={onCleanup}>بستن متوقف‌های قدیمی‌تر از ۷ روز</Button>
        </div>
      )}
    </Card>
  );
}

function MissionDots({ r }: { r: any }) {
  const max = r.max_runs || 0;
  if (!max || max > 24) {
    return r.end_at ? <span className="text-[12px] text-muted">پایان {fmtDate(r.end_at)}</span> : <span className="text-[12px] text-muted">بدون مهلت</span>;
  }
  return (
    <span className="flex flex-wrap items-center gap-1" dir="ltr" aria-label={`${r.run_count} از ${max} نوبت`}>
      {Array.from({ length: max }, (_, i) => (
        <span key={i} className={cn("h-2 w-2 rounded-full", i < (r.run_count || 0) ? "bg-primary" : "ring-1 ring-inset ring-line/35")} />
      ))}
    </span>
  );
}

export default function Dashboard() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const { data: d, isLoading, error, refetch } = useDashboard();
  const caps = useQuery({ queryKey: ["capabilities"], queryFn: () => get("/api/agent/capabilities"), refetchInterval: 60000 });
  const metrics = useLiveMetrics();
  const [ask, setAsk] = useState("");
  const state = agentState(d);
  const [title, sub] = STATE_TEXT[state];
  const s = d?.runs?.summary || {};
  const c = d?.control || {};
  const today = d?.usage?.today || {};
  const month = d?.usage?.month || {};
  const sys = metrics.length ? { ...d?.system, ...metrics[metrics.length - 1] } : d?.system || {};
  const tg = [...(caps.data?.ready || []), ...(caps.data?.needs_setup || [])].find((x: any) => x.key === "telegram");
  const tgReady = !!caps.data?.ready?.some((x: any) => x.key === "telegram");
  const routinesActive = d?.routines?.by_status?.active || 0;
  const nextRoutine = (d?.routines?.next || [])[0];
  const mem = c.memory || {};
  const memTotal = (mem.behavioral || 0) + (mem.semantic || 0) + (mem.episodic || 0) + (mem.experience || 0);
  const hourly = (() => {
    const map = new Map<number, any>();
    for (const h of d?.usage?.hourly || []) map.set(new Date(h.ts).setMinutes(0, 0, 0), h);
    const now = new Date();
    now.setMinutes(0, 0, 0);
    return Array.from({ length: 24 }, (_, i) => {
      const t = now.getTime() - (23 - i) * 3600_000;
      const h = map.get(t) || { tokens_in: 0, tokens_out: 0, cost: 0 };
      return { ...h, ts: new Date(t).toISOString() };
    });
  })();

  const inputs: BrainLink[] = [
    { key: "owner", title: "کارفرما و گفتگو", value: `${faInt(s.active || 0)} کار در جریان`, active: (s.running || 0) > 0, onClick: () => nav("/chat") },
    { key: "telegram", title: "تلگرام", value: caps.isLoading ? "در حال بررسی…" : tgReady ? "متصل و آماده" : tg ? "نیازمند اتصال" : "—",
      active: tgReady && routinesActive > 0, tone: !caps.isLoading && !tgReady ? "warn" : "normal", onClick: () => nav("/settings?tab=telegram") },
    { key: "routines", title: "روتین‌ها و مأموریت‌ها", value: routinesActive ? `${faInt(routinesActive)} فعال${nextRoutine ? ` · بعدی ${relTime(nextRoutine.next_run_at)}` : ""}` : "مأموریت فعالی نیست",
      active: routinesActive > 0, onClick: () => nav("/routines") },
    { key: "approvals", title: "اجازه‌های منتظر", value: d?.approvals_pending ? `${faInt(d.approvals_pending)} منتظر تصمیم شما` : "چیزی منتظر نیست",
      active: (d?.approvals_pending || 0) > 0, tone: (d?.approvals_pending || 0) > 0 ? "warn" : "normal", onClick: () => nav("/desk") },
  ];
  const outputs: BrainLink[] = [
    { key: "act", title: "اقدام با ابزار", value: `${faInt(s.running || 0)} در حال اجرا · ${faInt(s.succeeded_today || 0)} موفق امروز`, active: (s.running || 0) > 0, onClick: () => nav("/chat") },
    { key: "desk", title: "میز کار و گزارش‌ها", value: d?.inbox?.unread ? `${faInt(d.inbox.unread)} گزارش تازه` : "گزارش تازه‌ای نیست", active: (d?.inbox?.unread || 0) > 0, onClick: () => nav("/desk") },
    { key: "memory", title: "حافظه و یادگیری", value: `${compact(memTotal)} مورد در حافظه`, active: c.dream?.status === "running", onClick: () => nav("/memory") },
    { key: "team", title: "کارمندان", value: c.employees?.total ? `${faInt(c.employees?.busy || 0)} مشغول از ${faInt(c.employees.total)}` : "کارمندی تعریف نشده", active: (c.employees?.busy || 0) > 0, onClick: () => nav("/control?tab=employees") },
  ];

  const submit = (text = ask) => {
    if (!text.trim()) return;
    nav(`/chat?q=${encodeURIComponent(text.trim())}`);
  };
  const cleanup = async () => {
    try {
      const r = await post("/api/runs/close_stale?days=7");
      toast(r.closed ? `${faInt(r.closed)} اجرای قدیمی بسته شد` : "اجرای متوقف قدیمی‌تر از ۷ روز وجود ندارد", "success");
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    } catch (e) {
      toastError(e);
    }
  };

  if (error) return <ErrorBox error={error} retry={() => refetch()} />;

  return (
    <div className="space-y-5">
      {/* ── صحنه مغز دیجیتال ── */}
      <section className="stage command-center px-4 pb-5 pt-5 sm:px-8 sm:pb-7 sm:pt-7" aria-label="مرکز فرمان زنده">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="ltr mb-2 text-[10px] tracking-[.24em] text-white/70">XAGENT / COMMAND CENTER</p>
            <h2 className="text-[30px] font-light leading-10 tracking-[-0.01em] sm:text-[40px] sm:leading-[52px]">{title}</h2>
            <p className="mt-1 text-[14px] text-white/85">{sub}</p>
          </div>
          <div className="text-end text-[13px] leading-6 text-white/85">
            <div className="ltr font-semibold text-white">{d?.agent?.name || "X Agent"}</div>
            <div className="num">{fmtDate(new Date().toISOString())}</div>
          </div>
        </div>
        <div className="my-5 grid items-center gap-6 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          {isLoading ? <div className="grid h-72 place-items-center text-white/80">در حال خواندن وضعیت زنده…</div>
            : <>
              <div className="relative mx-auto grid h-[240px] w-full max-w-[360px] place-items-center">
                <Orbit size={240} className="orbit-mark text-cyan-100" />
                <span className="absolute bottom-0 text-[11px] text-white/75">درخواست ← اجرا ← شاهد ← نتیجه</span>
              </div>
              <div className="grid gap-2">
                {[...inputs.slice(0, 2), outputs[3]].map((item) => <button key={item.key} onClick={item.onClick} className="flex items-center justify-between gap-3 rounded-2xl bg-white/[.06] px-4 py-3 text-start transition-colors hover:bg-white/[.12]">
                  <span className="text-[13px] text-white/80">{item.title}</span><span className="text-[12px] font-medium">{item.value}</span>
                </button>)}
                <p className="px-1 text-[11px] leading-5 text-white/65">وضعیت از سرویس‌ها خوانده می‌شود. تأیید هر هدف به شاهد اجرای همان کار وابسته است.</p>
              </div>
            </>}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="stage-glass mx-auto mt-4 flex w-full max-w-2xl items-center gap-2 rounded-full p-1.5 ps-5">
          <input value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="مأموریتی به مدیر بسپار… (مثلاً هر دقیقه تلگرام را بررسی کن)" aria-label="مأموریت برای مدیر"
            className="h-11 min-w-0 flex-1 bg-transparent text-[14.5px] text-white outline-none placeholder:text-white/70" />
          <button type="submit" aria-label="ارسال به مدیر" className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-white text-[rgb(var(--stage-2))] transition-transform active:scale-95">
            <Send className="h-4 w-4 -scale-x-100" />
          </button>
        </form>
        <div className="mx-auto mt-2.5 flex max-w-2xl flex-wrap justify-center gap-2">
          {EXAMPLES.map((e) => (
            <button key={e} type="button" onClick={() => submit(e)} className="rounded-full px-3 py-1 text-[12.5px] text-white/90 ring-1 ring-inset ring-white/30 transition-colors hover:bg-white/10">
              {e}
            </button>
          ))}
        </div>
      </section>

      <section className="die grid grid-cols-2 overflow-hidden sm:grid-cols-4" aria-label="نمای عملیاتی">
        {[
          { label: "اجرای در جریان", value: s.running, tone: "text-primary" },
          { label: "منتظر اجازه", value: d?.approvals_pending, tone: "text-warning" },
          { label: "روتین فعال", value: routinesActive, tone: "text-cyan" },
          { label: "موفق امروز", value: s.succeeded_today, tone: "text-success" },
        ].map((metric) => <div className="operation-metric" key={metric.label}><p className="text-xs text-muted">{metric.label}</p><p className={cn("num mt-2 text-3xl font-light", metric.tone)}>{isLoading ? "…" : metric.value == null ? "—" : faInt(metric.value)}</p></div>)}
      </section>

      <RunFlow s={s} onCleanup={cleanup} />

      {/* ── میز کار و مأموریت‌ها ── */}
      <section className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader title="میز کار و گزارش‌ها" sub="نتایج مهم روتین‌ها، خطاها، اجازه‌ها و پایان مأموریت‌ها" icon={<Inbox className="h-4 w-4" />}
            actions={<Button size="sm" variant="soft" onClick={() => nav("/desk")} icon={<ArrowLeft className="h-3.5 w-3.5" />}>همه</Button>} />
          <div className="px-2 pb-3 pt-2 sm:px-3">
            {isLoading && <div className="space-y-2 p-3"><Skeleton className="h-12" /><Skeleton className="h-12" /></div>}
            {!isLoading && (d?.inbox?.items || []).length === 0 && (
              <p className="px-4 py-8 text-center text-[13.5px] leading-7 text-muted">هنوز گزارشی نیست. وقتی روتینی نتیجه مهمی بگیرد، خطایی رخ دهد یا مأموریتی تمام شود، بدون پرسیدن اینجا گزارش می‌شود.</p>
            )}
            {(d?.inbox?.items || []).slice(0, 5).map((it: any) => (
              <button key={it.id} onClick={() => nav(it.link || "/desk")}
                className={cn("flex w-full items-start gap-3 rounded-2xl px-3 py-3 text-start transition-colors hover:bg-line/[.05]", !it.read && "bg-primary/[.035]")}>
                <span className={cn("pad mt-2", SEV_TONE[it.severity] || "text-primary")} />
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 truncate text-[14px] font-medium">{it.title}</span>
                    <Badge tone="neutral" className="hidden sm:inline-flex">{KIND_LABEL[it.kind] || it.kind}</Badge>
                  </span>
                  {it.body && <span className="mt-0.5 line-clamp-2 block text-[12.5px] leading-5 text-muted">{it.body}</span>}
                </span>
                <span className="shrink-0 pt-0.5 text-[11.5px] text-muted">{relTime(it.ts)}</span>
              </button>
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader title="مأموریت‌های فعال" sub="روتین‌هایی که بدون حضور شما اجرا می‌شوند" icon={<Repeat className="h-4 w-4" />}
            actions={<Button size="sm" variant="ghost" onClick={() => nav("/routines")}>روتین‌ها</Button>} />
          <div className="space-y-1 px-2 pb-3 pt-2 sm:px-3">
            {(d?.routines?.next || []).length === 0 && <p className="px-4 py-8 text-center text-[13.5px] text-muted">مأموریت زمان‌دار فعالی نیست.</p>}
            {(d?.routines?.next || []).map((r: any) => (
              <button key={r.id} onClick={() => nav(`/routines?id=${r.id}`)} className="block w-full rounded-2xl px-3 py-3 text-start transition-colors hover:bg-line/[.05]">
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate text-[14px] font-medium">{r.name}</span>
                  <span className="num flex shrink-0 items-center gap-1 text-[12px] text-muted"><Clock className="h-3.5 w-3.5" />{fmtTime(r.next_run_at)}</span>
                </span>
                <span className="mt-2 flex items-center justify-between gap-3">
                  <MissionDots r={r} />
                  <span className="num shrink-0 text-[12px] text-muted">{faInt(r.run_count || 0)}{r.max_runs ? ` از ${faInt(r.max_runs)}` : ""} نوبت</span>
                </span>
              </button>
            ))}
          </div>
        </Card>
      </section>

      {/* ── مصرف و بار ── */}
      <section className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="توکن مصرفی ۲۴ ساعت اخیر" icon={<Zap className="h-4 w-4" />} sub={`امروز ${compact((today.tokens_in || 0) + (today.tokens_out || 0))} توکن · ${faInt(today.calls || 0)} فراخوانی · کش ${compact(today.cache_read)}`}
            actions={<span className="num text-[13px] text-muted">{usd(today.cost, 4)}</span>} />
          <div className="px-3 pb-3 pt-2">
            <AreaChart data={hourly} height={200} series={[{ key: "tokens_in", label: "ورودی", color: CHART.in }, { key: "tokens_out", label: "خروجی", color: CHART.out }]}
              xLabel={(h) => fmtTime(h.ts)} format={(v) => compact(v)} />
          </div>
          <div className="grid gap-4 border-t hairline px-5 py-4 text-[13px] sm:grid-cols-2">
            <div className="space-y-2">
              <div className="flex justify-between"><span className="text-muted">هزینه امروز از سقف</span><span className="num">{usd(today.cost, 4)} / {usd(today.limit, 2)}</span></div>
              <Meter value={today.cost || 0} max={today.limit || 1} />
            </div>
            <div className="space-y-2">
              <div className="flex justify-between"><span className="text-muted">هزینه ماه از سقف</span><span className="num">{usd(month.cost, 3)} / {usd(month.limit, 2)}</span></div>
              <Meter value={month.cost || 0} max={month.limit || 1} tone="accent" />
            </div>
          </div>
        </Card>
        <Card>
          <CardHeader title="درگیری سیستم" icon={<MemoryStick className="h-4 w-4" />} sub={`بار ${faNum(sys.load1, 2)} · روشن ${uptime(sys.uptime)}`} />
          {isLoading ? <div className="p-5"><Skeleton className="h-32" /></div> : (
            <div className="space-y-5 px-5 pb-5 pt-4">
              <Gauge value={sys.cpu} label="پردازنده" sub={`${faInt(sys.cpu_count)} هسته`} color="rgb(var(--cyan))" />
              <Gauge value={sys.mem} label="حافظه" sub={bytes(sys.mem_used)} color="rgb(var(--accent))" />
              <Gauge value={sys.disk} label="دیسک" sub={`${bytes(sys.disk_used)} از ${bytes(sys.disk_total)}`} color="rgb(var(--primary))" />
              <div className="border-t hairline pt-3">
                <div className="mb-1 text-[12px] text-muted">پردازنده در دقایق اخیر</div>
                <Sparkline values={metrics.length ? metrics.map((m) => m.cpu) : [sys.cpu || 0, sys.cpu || 0]} color={CHART.cpu} height={32} />
              </div>
            </div>
          )}
        </Card>
      </section>
    </div>
  );
}
