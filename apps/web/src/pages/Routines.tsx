/**
 * روتین‌ها و مأموریت‌ها: چرخه حیات کامل (فعال، در حال اجرا، منتظر موعد، منتظر تأیید، متوقف موقت، تکمیل‌شده،
 * ناموفق، لغوشده، منقضی). هر روتین زمان اجرای بعدی، پایان، نوبت‌های انجام‌شده/باقی‌مانده، آخرین نتیجه، هزینه
 * و خطای اخیر را نشان می‌دهد؛ تاریخچه نوبت‌ها روی خط زمان عمودی.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Ban, Clock, FlaskConical, ListTree, Pause, Pencil, Play, Plus, Repeat, ShieldAlert, ShieldCheck, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, Button, Card, cn, Drawer, Empty, ErrorBox, Field, Input, Modal, PageHeader, Segmented, Skeleton, Switch, Textarea, type Tone } from "../components/ui";
import { Timeline, type TimelineItem } from "../components/viz/Timeline";
import { del, get, patch, post } from "../lib/api";
import { faInt, fmtDate, fmtTime, isoToLocalInput, localInputToIso, relTime, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const PRESETS = [
  { l: "هر روز ۹ صبح", c: "0 9 * * *" },
  { l: "هر ساعت", c: "0 * * * *" },
  { l: "شنبه‌ها ۸ صبح", c: "0 8 * * 6" },
  { l: "روزهای کاری ۱۸", c: "0 18 * * 6,0,1,2,3" },
  { l: "اول هر ماه", c: "0 10 1 * *" },
];

export const ROUTINE_STATE: Record<string, { label: string; tone: Tone; hint: string }> = {
  running: { label: "در حال اجرا", tone: "cyan", hint: "یک نوبت همین حالا اجرا می‌شود" },
  queued: { label: "در صف اجرا", tone: "primary", hint: "نوبت ساخته شده و منتظر Worker است" },
  waiting_approval: { label: "منتظر تأیید", tone: "warning", hint: "یک اقدام این نوبت منتظر اجازه شماست" },
  waiting: { label: "منتظر موعد", tone: "primary", hint: "تا موعد بعدی هیچ هزینه‌ای مصرف نمی‌شود" },
  listening: { label: "گوش به رویداد", tone: "primary", hint: "با رویداد منطبق فعال می‌شود" },
  circuit_open: { label: "مدارقطع‌کن باز", tone: "danger", hint: "پس از خطاهای پیاپی موقتاً متوقف شده" },
  paused: { label: "متوقف موقت", tone: "warning", hint: "تا ادامه دادن شما اجرا نمی‌شود" },
  disabled: { label: "غیرفعال", tone: "neutral", hint: "غیرفعال‌شده توسط کارفرما" },
  completed: { label: "تکمیل‌شده", tone: "success", hint: "مأموریت به پایان طبیعی رسید" },
  failed: { label: "ناموفق", tone: "danger", hint: "مأموریت بدون نوبت موفق پایان یافت" },
  cancelled: { label: "لغوشده", tone: "neutral", hint: "لغو توسط کارفرما یا لغو مجوز" },
  expired: { label: "منقضی", tone: "neutral", hint: "مجوز مأموریت منقضی شد" },
};
const REASON: Record<string, string> = {
  end_at: "پایان مهلت", max_runs: "رسیدن به سقف نوبت‌ها", once: "اجرای یک‌باره انجام شد", grant_expired: "انقضای مجوز",
  grant_revoked: "لغو مجوز", owner_cancelled: "لغو توسط شما",
};
const OUTCOME: Record<string, { label: string; tone: Tone }> = {
  success: { label: "موفق", tone: "success" }, no_change: { label: "بدون تغییر", tone: "neutral" },
  failure: { label: "ناموفق", tone: "danger" }, partial: { label: "نیمه‌کاره", tone: "warning" },
  interrupted: { label: "قطع با توقف اضطراری", tone: "warning" }, cancelled: { label: "لغوشده", tone: "neutral" },
};
const TERMINAL = new Set(["completed", "failed", "cancelled", "expired"]);

function describe(r: any): React.ReactNode {
  if (r.trigger_type === "cron") {
    const m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(r.cron_expr || "");
    if (m) return `هر روز ساعت ${faInt(+m[2])}:${String(+m[1]).padStart(2, "0").replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d])}`;
    return <>زمان‌بندی <bdi className="ltr mono">{r.cron_expr}</bdi></>;
  }
  if (r.trigger_type === "interval") {
    const m = Math.round((r.interval_seconds || 0) / 60);
    return m % 60 === 0 && m >= 60 ? `هر ${faInt(m / 60)} ساعت` : `هر ${faInt(m)} دقیقه`;
  }
  if (r.trigger_type === "once") return `یک‌باره: ${fmtDate(r.run_at)}`;
  const f = r.event_filter || {};
  return `رویداد ${f.source || ""}${f.keywords?.length ? ` · کلیدواژه: ${f.keywords.join("، ")}` : ""}`;
}

function RoutineForm({ routine, onClose }: { routine?: any; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<any>(() => routine ? { ...routine, keywords: (routine.event_filter?.keywords || []).join("، "), chats: (routine.event_filter?.chat_ids || []).join("، "), watch_tg: routine.watch?.source === "telegram" }
    : { name: "", instruction: "", trigger_type: "interval", cron_expr: "0 9 * * *", interval_seconds: 3600, time_sensitive: false, keywords: "", chats: "", duration_minutes: "", max_runs: "", budget_usd: "", run_now: true, watch_tg: false });
  const set = (k: string, v: any) => setF((x: any) => ({ ...x, [k]: v }));
  const save = useMutation({
    mutationFn: () => {
      const split = (s: string) => s.split(/[،,]/).map((x) => x.trim()).filter(Boolean);
      const body: any = {
        name: f.name, description: f.description || "", instruction: f.instruction, trigger_type: f.trigger_type,
        cron_expr: f.trigger_type === "cron" ? f.cron_expr : null,
        interval_seconds: f.trigger_type === "interval" ? Number(f.interval_seconds) : null,
        run_at: f.trigger_type === "once" ? f.run_at : null,
        event_filter: f.trigger_type === "event" ? { source: "telegram", keywords: split(f.keywords), chat_ids: split(f.chats) } : {},
        time_sensitive: f.time_sensitive, max_steps: f.max_steps ? Number(f.max_steps) : null, max_cost_usd: f.max_cost_usd ? Number(f.max_cost_usd) : null,
        duration_minutes: f.duration_minutes ? Number(f.duration_minutes) : null, max_runs: f.max_runs ? Number(f.max_runs) : null,
        budget_usd: f.budget_usd ? Number(f.budget_usd) : null,
        watch: f.trigger_type !== "event" && f.watch_tg ? { source: "telegram" } : routine ? {} : null,
        run_now: !routine && f.run_now && f.trigger_type !== "event" && f.trigger_type !== "once",
      };
      return routine ? patch(`/api/routines/${routine.id}`, body) : post("/api/routines", body);
    },
    onSuccess: () => { toast("روتین ذخیره و از سرور بازخوانی شد", "success"); qc.invalidateQueries({ queryKey: ["routines"] }); onClose(); },
    onError: toastError,
  });
  const recurring = f.trigger_type === "interval" || f.trigger_type === "cron";
  return (
    <Modal open onClose={onClose} wide title={routine ? "ویرایش روتین" : "مأموریت / روتین جدید"} footer={<><Button variant="ghost" onClick={onClose}>انصراف</Button><Button variant="primary" loading={save.isPending} disabled={!f.name || !f.instruction} onClick={() => save.mutate()}>ذخیره</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="نام"><Input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="مثلاً پایش تلگرام" /></Field>
        <Field label="نوع راه‌انداز">
          <Segmented size="sm" value={f.trigger_type} onChange={(v) => set("trigger_type", v)} options={[{ value: "interval", label: "بازه‌ای" }, { value: "cron", label: "زمان‌بندی" }, { value: "once", label: "یک‌باره" }, { value: "event", label: "رویدادمحور" }]} />
        </Field>
        <Field label="دستور کامل برای هر نوبت" className="sm:col-span-2"><Textarea value={f.instruction} onChange={(e) => set("instruction", e.target.value)} placeholder="مثال: اگر پیام جدیدی آمد خلاصه‌اش را گزارش کن." /></Field>
        {f.trigger_type === "cron" && (
          <Field label="عبارت cron (به وقت منطقه زمانی تنظیمات)" className="sm:col-span-2" hint="دقیقه ساعت روز‌ماه ماه روز‌هفته — روز هفته: ۰=یکشنبه … ۶=شنبه">
            <Input value={f.cron_expr} onChange={(e) => set("cron_expr", e.target.value)} className="ltr" />
            <div className="mt-2 flex flex-wrap gap-1.5">{PRESETS.map((p) => <button key={p.c} type="button" onClick={() => set("cron_expr", p.c)} className={cn("rounded-full px-3 py-1 text-[12px] ring-1 ring-inset", f.cron_expr === p.c ? "bg-primary/10 text-primary ring-primary/30" : "text-muted ring-line/20 hover:text-ink")}>{p.l}</button>)}</div>
          </Field>
        )}
        {f.trigger_type === "interval" && <Field label="فاصله نوبت‌ها (دقیقه)" hint="حداقل ۱ دقیقه"><Input type="number" min={1} value={Math.round(f.interval_seconds / 60)} onChange={(e) => set("interval_seconds", Math.max(60, Number(e.target.value) * 60))} /></Field>}
        {f.trigger_type === "once" && <Field label="زمان اجرا"><Input type="datetime-local" value={isoToLocalInput(f.run_at)} onChange={(e) => set("run_at", localInputToIso(e.target.value))} /></Field>}
        {recurring && <Field label="مدت مأموریت از الان (دقیقه، اختیاری)" hint="بازه نیمه‌باز؛ در پایان خودکار متوقف و گزارش می‌شود"><Input type="number" min={1} value={f.duration_minutes || ""} onChange={(e) => set("duration_minutes", e.target.value)} /></Field>}
        {recurring && <Field label="سقف نوبت‌ها (اختیاری)" hint="شامل اجرای فوری اول"><Input type="number" min={1} value={f.max_runs || ""} onChange={(e) => set("max_runs", e.target.value)} /></Field>}
        {f.trigger_type === "event" && (
          <>
            <Field label="کلیدواژه‌ها (با ویرگول)" hint="فیلتر قطعی بدون مصرف توکن"><Input value={f.keywords} onChange={(e) => set("keywords", e.target.value)} /></Field>
            <Field label="گفتگوهای تلگرام (یوزرنیم یا شناسه، اختیاری)"><Input value={f.chats} onChange={(e) => set("chats", e.target.value)} /></Field>
          </>
        )}
        <Field label="سقف هزینه هر نوبت (دلار، اختیاری)"><Input type="number" step="0.01" value={f.max_cost_usd || ""} onChange={(e) => set("max_cost_usd", e.target.value)} /></Field>
        <Field label="سقف هزینه کل مأموریت (دلار، اختیاری)"><Input type="number" step="0.01" value={f.budget_usd || ""} onChange={(e) => set("budget_usd", e.target.value)} /></Field>
        {f.trigger_type !== "event" && (
          <div className="flex items-center justify-between gap-3 rounded-2xl bg-line/[.05] px-4 py-3 sm:col-span-2">
            <div><div className="text-[14px] font-medium">پیش‌بررسی قطعی پیام‌های تلگرام</div><div className="text-[12.5px] text-muted">نوبت‌های بدون پیام جدید هیچ فراخوانی مدل و هزینه‌ای ندارند</div></div>
            <Switch checked={!!f.watch_tg} onChange={(v) => set("watch_tg", v)} label="پیش‌بررسی تلگرام" />
          </div>
        )}
        {!routine && recurring && (
          <div className="flex items-center justify-between gap-3 rounded-2xl bg-line/[.05] px-4 py-3">
            <div><div className="text-[14px] font-medium">اجرای فوری نوبت اول</div><div className="text-[12.5px] text-muted">جزو سقف نوبت‌ها شمرده می‌شود و تکرار نمی‌شود</div></div>
            <Switch checked={!!f.run_now} onChange={(v) => set("run_now", v)} label="اجرای فوری" />
          </div>
        )}
        <div className="flex items-center justify-between gap-3 rounded-2xl bg-line/[.05] px-4 py-3">
          <div><div className="text-[14px] font-medium">زمان‌حساس</div><div className="text-[12.5px] text-muted">هشدار فوری و خنک‌شدن کوتاه‌تر پس از خطا</div></div>
          <Switch checked={!!f.time_sensitive} onChange={(v) => set("time_sensitive", v)} label="زمان‌حساس" />
        </div>
      </div>
    </Modal>
  );
}

function RunsDrawer({ r, onClose }: { r: any | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["routine-runs", r?.id], queryFn: () => get(`/api/routines/${r.id}/runs`), enabled: !!r });
  const runs: any[] = [...(q.data?.items || [])].reverse();
  const items: TimelineItem[] = runs.map((x) => {
    const st = x.status;
    const oc = x.outcome === "no_change" ? "no_change" : st;
    return {
      id: x.id,
      status: st === "running" || st === "queued" ? "running" : st === "failed" ? "error" : st === "paused" || st === "waiting_approval" ? "blocked" : "done",
      title: <span className="num">{fmtTime(x.created_at)} — {oc === "no_change" ? "بدون تغییر (بدون فراخوانی مدل)" : st === "succeeded" ? "انجام شد" : st === "failed" ? "ناموفق" : st === "paused" ? "متوقف موقت" : st === "waiting_approval" ? "منتظر اجازه" : st === "running" ? "در حال اجرا" : st === "queued" ? "در صف" : st}</span>,
      meta: <><span className="num">{faInt(x.llm_calls)} فراخوانی مدل</span><span className="num">{usd(x.cost_usd, 4)}</span>{x.stop_reason && <span>{x.stop_reason}</span>}</>,
      children: x.result && x.outcome !== "no_change" ? <p className="line-clamp-3 whitespace-pre-wrap text-[12.5px] leading-6 text-muted">{x.result}</p> : undefined,
    };
  });
  return (
    <Drawer open={!!r} onClose={onClose} title={r ? `تاریخچه نوبت‌ها — ${r.name}` : ""}>
      {q.isLoading && <Skeleton className="h-40" />}
      {!q.isLoading && items.length === 0 && <p className="py-10 text-center text-sm text-muted">هنوز نوبتی اجرا نشده است.</p>}
      <Timeline items={items} size="sm" />
    </Drawer>
  );
}

function RunDots({ r }: { r: any }) {
  const max = r.max_runs;
  if (!max || max > 40) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" dir="ltr" aria-label={`${r.run_count} از ${max} نوبت`}>
      {Array.from({ length: max }, (_, i) => (
        <span key={i} className={cn("h-2.5 w-2.5 rounded-full transition-colors", i < (r.run_count || 0) ? "bg-primary" : "ring-1 ring-inset ring-line/35")} />
      ))}
    </div>
  );
}

export default function Routines() {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const [edit, setEdit] = useState<any | null>(null);
  const [creating, setCreating] = useState(false);
  const [history, setHistory] = useState<any | null>(null);
  const [filter, setFilter] = useState<"live" | "ended" | "all">("live");
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["routines"], queryFn: () => get("/api/routines") });
  useLive((_d, t) => (t === "routine.changed" || t === "run.finished") && qc.invalidateQueries({ queryKey: ["routines"] }));
  const act = async (r: any, a: string) => {
    if (a === "cancel" && !window.confirm(`مأموریت «${r.name}» لغو شود؟ سوابق آن حفظ می‌شود.`)) return;
    try {
      await post(`/api/routines/${r.id}/${a}`);
      toast({ test: "اجرای آزمایشی آغاز شد", pause: "روتین متوقف موقت شد", resume: "روتین ادامه یافت", cancel: "مأموریت لغو شد" }[a] || "انجام شد", "success");
      qc.invalidateQueries({ queryKey: ["routines"] });
    } catch (e) { toastError(e); }
  };
  const all: any[] = data?.items || [];
  const focus = params.get("id");
  const items = all.filter((r) => filter === "all" || (filter === "ended" ? TERMINAL.has(r.status) : !TERMINAL.has(r.status)));
  return (
    <div>
      <PageHeader title="روتین‌ها و مأموریت‌ها" sub="اجرای مستقل زمان‌دار با پایان خودکار، بدون مصرف مدل در فاصله نوبت‌ها" icon={<Repeat className="h-5 w-5" />}
        actions={<Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>مأموریت جدید</Button>} />
      <div className="mb-5">
        <Segmented value={filter} onChange={setFilter} options={[{ value: "live", label: `جاری (${faInt(all.filter((r) => !TERMINAL.has(r.status)).length)})` }, { value: "ended", label: "پایان‌یافته" }, { value: "all", label: "همه" }]} />
      </div>
      {error && <ErrorBox error={error} retry={refetch} />}
      {isLoading && <div className="grid gap-5 lg:grid-cols-2">{[1, 2].map((i) => <Skeleton key={i} className="h-60" />)}</div>}
      {!isLoading && items.length === 0 && <Card><Empty icon={<Repeat className="h-6 w-6" />} title={filter === "ended" ? "مأموریت پایان‌یافته‌ای نیست" : "مأموریت جاری ندارید"} text="در گفتگو بنویسید «تا ۱۰ دقیقه آینده هر دقیقه تلگرام را بررسی کن» یا از اینجا بسازید." action={<Button variant="primary" onClick={() => setCreating(true)}>ساخت مأموریت</Button>} /></Card>}
      <div className="grid gap-5 lg:grid-cols-2">
        {items.map((r, i) => {
          const ds = ROUTINE_STATE[r.display_state] || ROUTINE_STATE[r.status] || { label: r.status, tone: "neutral" as Tone, hint: "" };
          const ended = TERMINAL.has(r.status);
          const live = ["running", "queued", "waiting_approval"].includes(r.display_state);
          const oc = r.last_status ? OUTCOME[r.last_status] || { label: r.last_status, tone: "neutral" as Tone } : null;
          return (
            <motion.div key={r.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.04, duration: 0.35, ease: [0.16, 1, 0.3, 1] }}>
              <Card className={cn("flex h-full flex-col p-5", live && "die-live", focus === r.id && "ring-2 ring-primary/40", ended && "opacity-90")}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="truncate text-[16px] font-semibold">{r.name}</h3>
                    <p className="mt-0.5 text-[12.5px] text-muted">{describe(r)}{r.watch?.source === "telegram" && " · پیش‌بررسی تلگرام"}</p>
                  </div>
                  <Badge tone={ds.tone} dot>{ds.label}</Badge>
                </div>
                <p className="mt-3 line-clamp-2 text-[13.5px] leading-6 text-muted">{r.instruction}</p>
                <div className="mt-4 space-y-2.5">
                  <RunDots r={r} />
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 text-[12.5px] sm:grid-cols-3">
                    <div><dt className="text-muted">نوبت بعدی</dt><dd className="num flex items-center gap-1 font-medium"><Clock className="h-3.5 w-3.5 text-muted" />{r.next_run_at ? fmtTime(r.next_run_at) : "—"}</dd></div>
                    <div><dt className="text-muted">پایان</dt><dd className="num font-medium">{r.end_at ? fmtDate(r.end_at) : "بدون مهلت"}</dd></div>
                    <div><dt className="text-muted">نوبت‌ها</dt><dd className="num font-medium">{faInt(r.run_count)}{r.max_runs ? ` از ${faInt(r.max_runs)}` : ""}{r.remaining_runs != null && !ended ? ` · ${faInt(r.remaining_runs)} مانده` : ""}</dd></div>
                    <div><dt className="text-muted">نتیجه‌ها</dt><dd className="num font-medium">{faInt(r.success_count)} موفق · {faInt(r.no_change_count)} بی‌تغییر · <span className={r.failure_count ? "text-danger" : ""}>{faInt(r.failure_count)} ناموفق</span></dd></div>
                    <div><dt className="text-muted">هزینه</dt><dd className="num font-medium">{usd(r.spent_usd, 4)}{r.budget_usd ? ` از ${usd(r.budget_usd, 2)}` : ""}</dd></div>
                    <div><dt className="text-muted">آخرین نتیجه</dt><dd className="font-medium">{oc ? <Badge tone={oc.tone}>{oc.label}</Badge> : "—"}{r.last_run_at && <span className="num ms-1 text-muted">{relTime(r.last_run_at)}</span>}</dd></div>
                  </dl>
                </div>
                {ended && r.completion_reason && <p className="mt-3 text-[12.5px] text-muted">پایان: {REASON[r.completion_reason] || r.completion_reason} · {fmtDate(r.completed_at)}</p>}
                {r.last_error && !ended && <p className="mt-3 flex items-start gap-1.5 rounded-xl bg-danger/[.06] px-3 py-2 text-[12.5px] leading-5 text-danger"><ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />{r.last_error}</p>}
                {r.mission && (
                  <p className="mt-3 flex items-start gap-1.5 rounded-xl bg-primary/[.05] px-3 py-2 text-[12.5px] leading-5">
                    <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                    <span>مجوز مأموریت ({r.mission.status === "active" ? "فعال" : r.mission.status}): {r.mission.scope?.telegram?.reply ? `پاسخ فقط به ${(r.mission.scope.telegram.reply_to || []).join("، ")}` : "فقط خواندن و گزارش"}
                      {r.mission.max_actions ? ` · ${faInt(r.mission.actions_count)} از ${faInt(r.mission.max_actions)} اقدام` : ""}</span>
                  </p>
                )}
                <div className="mt-4 flex flex-wrap gap-1.5 border-t hairline pt-3.5">
                  <Button size="sm" variant="soft" icon={<ListTree className="h-3.5 w-3.5" />} onClick={() => setHistory(r)}>تاریخچه نوبت‌ها</Button>
                  {!ended && (r.status === "paused" || r.status === "disabled" || r.display_state === "circuit_open"
                    ? <Button size="sm" variant="outline" icon={<Play className="h-3.5 w-3.5" />} onClick={() => act(r, "resume")}>ادامه</Button>
                    : <Button size="sm" variant="outline" icon={<Pause className="h-3.5 w-3.5" />} onClick={() => act(r, "pause")}>توقف موقت</Button>)}
                  {!ended && <Button size="sm" variant="ghost" icon={<FlaskConical className="h-3.5 w-3.5" />} onClick={() => act(r, "test")}>آزمایش</Button>}
                  <Button size="sm" variant="ghost" icon={<Pencil className="h-3.5 w-3.5" />} onClick={() => setEdit(r)}>{ended ? "تمدید/ویرایش" : "ویرایش"}</Button>
                  {!ended && <Button size="sm" variant="ghost" icon={<Ban className="h-3.5 w-3.5" />} onClick={() => act(r, "cancel")}>لغو</Button>}
                  <Button size="sm" variant="ghost" aria-label="حذف" className="ms-auto" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={async () => { if (!confirm(`روتین «${r.name}» حذف شود؟`)) return; await del(`/api/routines/${r.id}`).catch(toastError); qc.invalidateQueries({ queryKey: ["routines"] }); }} />
                  {r.trigger_type === "event" && <Badge tone="cyan"><Zap className="h-3 w-3" />رویدادمحور</Badge>}
                </div>
              </Card>
            </motion.div>
          );
        })}
      </div>
      {(creating || edit) && <RoutineForm routine={edit || undefined} onClose={() => { setCreating(false); setEdit(null); }} />}
      <RunsDrawer r={history} onClose={() => setHistory(null)} />
    </div>
  );
}
