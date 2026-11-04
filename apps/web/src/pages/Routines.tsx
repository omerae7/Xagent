import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Activity, Clock, FlaskConical, Pause, Pencil, Play, Plus, Repeat, ShieldAlert, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, Button, Card, cn, Empty, ErrorBox, Field, Input, Modal, PageHeader, Segmented, Skeleton, Switch, Textarea } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { faInt, fmtDate, isoToLocalInput, localInputToIso, relTime } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const PRESETS = [
  { l: "هر روز ۹ صبح", c: "0 9 * * *" },
  { l: "هر ساعت", c: "0 * * * *" },
  { l: "شنبه‌ها ۸ صبح", c: "0 8 * * 6" },
  { l: "روزهای کاری ۱۸", c: "0 18 * * 6,0,1,2,3" },
  { l: "اول هر ماه", c: "0 10 1 * *" },
];

function describe(r: any) {
  if (r.trigger_type === "cron") return `زمان‌بندی: ${r.cron_expr}`;
  if (r.trigger_type === "interval") return `هر ${faInt(Math.round((r.interval_seconds || 0) / 60))} دقیقه`;
  if (r.trigger_type === "once") return `یک‌باره: ${fmtDate(r.run_at)}`;
  const f = r.event_filter || {};
  return `رویداد ${f.source || ""}${f.keywords?.length ? ` · کلیدواژه: ${f.keywords.join("، ")}` : ""}`;
}

function RoutineForm({ routine, onClose }: { routine?: any; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<any>(() => routine ? { ...routine, keywords: (routine.event_filter?.keywords || []).join("، "), chats: (routine.event_filter?.chat_ids || []).join("، ") }
    : { name: "", instruction: "", trigger_type: "cron", cron_expr: "0 9 * * *", interval_seconds: 3600, time_sensitive: false, keywords: "", chats: "" });
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
      };
      return routine ? patch(`/api/routines/${routine.id}`, body) : post("/api/routines", body);
    },
    onSuccess: () => { toast("روتین ذخیره شد", "success"); qc.invalidateQueries({ queryKey: ["routines"] }); onClose(); },
    onError: toastError,
  });
  return (
    <Modal open onClose={onClose} wide title={routine ? "ویرایش روتین" : "روتین جدید"} footer={<><Button variant="ghost" onClick={onClose}>انصراف</Button><Button variant="primary" loading={save.isPending} disabled={!f.name || !f.instruction} onClick={() => save.mutate()}>ذخیره</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="نام روتین"><Input value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="نوع راه‌انداز">
          <Segmented size="sm" value={f.trigger_type} onChange={(v) => set("trigger_type", v)} options={[{ value: "cron", label: "زمان‌بندی" }, { value: "interval", label: "بازه‌ای" }, { value: "once", label: "یک‌باره" }, { value: "event", label: "رویدادمحور" }]} />
        </Field>
        <Field label="دستور کامل برای ایجنت (هر بار اجرا می‌شود)" className="sm:col-span-2"><Textarea value={f.instruction} onChange={(e) => set("instruction", e.target.value)} placeholder="مثال: تسک‌های باز را بررسی کن و خلاصه را در تلگرام برایم بفرست." /></Field>
        {f.trigger_type === "cron" && (
          <Field label="عبارت cron (به وقت منطقه زمانی تنظیمات)" className="sm:col-span-2" hint="دقیقه ساعت روز‌ماه ماه روز‌هفته — روز هفته: ۰=یکشنبه … ۶=شنبه">
            <Input value={f.cron_expr} onChange={(e) => set("cron_expr", e.target.value)} className="ltr" />
            <div className="mt-2 flex flex-wrap gap-1.5">{PRESETS.map((p) => <button key={p.c} type="button" onClick={() => set("cron_expr", p.c)} className={cn("rounded-[4px] px-2.5 py-1 text-[11px] ring-1", f.cron_expr === p.c ? "bg-primary/15 text-primary ring-primary/30" : "text-muted ring-line/20 hover:text-ink")}>{p.l}</button>)}</div>
          </Field>
        )}
        {f.trigger_type === "interval" && <Field label="فاصله (دقیقه)"><Input type="number" min={1} value={Math.round(f.interval_seconds / 60)} onChange={(e) => set("interval_seconds", Number(e.target.value) * 60)} /></Field>}
        {f.trigger_type === "once" && <Field label="زمان اجرا"><Input type="datetime-local" value={isoToLocalInput(f.run_at)} onChange={(e) => set("run_at", localInputToIso(e.target.value))} /></Field>}
        {f.trigger_type === "event" && (
          <>
            <Field label="کلیدواژه‌ها (با ویرگول)" hint="فیلتر قطعی بدون مصرف توکن؛ فقط پیام‌های منطبق ایجنت را فعال می‌کنند."><Input value={f.keywords} onChange={(e) => set("keywords", e.target.value)} /></Field>
            <Field label="گفتگوهای تلگرام (یوزرنیم یا شناسه، اختیاری)"><Input value={f.chats} onChange={(e) => set("chats", e.target.value)} /></Field>
          </>
        )}
        <Field label="سقف گام (اختیاری)"><Input type="number" value={f.max_steps || ""} onChange={(e) => set("max_steps", e.target.value)} /></Field>
        <Field label="سقف هزینه هر اجرا (دلار، اختیاری)"><Input type="number" step="0.01" value={f.max_cost_usd || ""} onChange={(e) => set("max_cost_usd", e.target.value)} /></Field>
        <div className="flex items-center justify-between rounded-[5px] bg-line/5 px-3 py-2.5 sm:col-span-2">
          <div><div className="text-sm">زمان‌حساس</div><div className="text-[11px] text-muted">در صورت خطاهای پیاپی، هشدار فوری و دوره خنک‌شدن کوتاه‌تر</div></div>
          <Switch checked={!!f.time_sensitive} onChange={(v) => set("time_sensitive", v)} />
        </div>
      </div>
    </Modal>
  );
}

export default function Routines() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [edit, setEdit] = useState<any | null>(null);
  const [creating, setCreating] = useState(false);
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["routines"], queryFn: () => get("/api/routines") });
  useLive((_d, t) => t === "routine.changed" && qc.invalidateQueries({ queryKey: ["routines"] }));
  const act = async (r: any, a: string) => {
    try {
      const res = await post(`/api/routines/${r.id}/${a}`);
      toast(a === "test" ? "اجرای آزمایشی آغاز شد" : a === "pause" ? "روتین متوقف شد" : "روتین ادامه یافت", "success");
      qc.invalidateQueries({ queryKey: ["routines"] });
      if (a === "test" && res.run_id) nav("/chat");
    } catch (e) { toastError(e); }
  };
  const items: any[] = data?.items || [];
  return (
    <div>
      <PageHeader title="روتین‌ها" sub="کارهای زمان‌بندی‌شده و رویدادمحور با قفل اتمیک و مدارقطع‌کن" icon={<Repeat className="h-5 w-5" />}
        actions={<Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>روتین جدید</Button>} />
      {error && <ErrorBox error={error} retry={refetch} />}
      {isLoading && <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{[1, 2, 3].map((i) => <Skeleton key={i} className="h-48" />)}</div>}
      {!isLoading && items.length === 0 && <Card><Empty icon={<Repeat className="h-6 w-6" />} title="هنوز روتینی ندارید" text="مثلاً «هر روز ساعت ۹ گزارش تسک‌ها را در تلگرام بفرست». می‌توانید در گفتگو هم از ایجنت بخواهید." action={<Button variant="primary" onClick={() => setCreating(true)}>ساخت روتین</Button>} /></Card>}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((r, i) => {
          const open = r.circuit_state === "open";
          return (
            <motion.div key={r.id} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.04 }}>
              <Card className={cn("flex h-full flex-col p-4", open && "ring-1 ring-danger/40")}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="truncate font-semibold">{r.name}</h3>
                    <p className="ltr mt-0.5 truncate text-end text-[11px] text-muted">{describe(r)}</p>
                  </div>
                  <Switch checked={!r.paused && r.enabled} onChange={(v) => act(r, v ? "resume" : "pause")} label="فعال" />
                </div>
                <p className="mt-3 line-clamp-3 flex-1 text-sm leading-6 text-muted">{r.instruction}</p>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {r.paused ? <Badge tone="warning" dot>متوقف</Badge> : <Badge tone="success" dot>فعال</Badge>}
                  {r.trigger_type === "event" && <Badge tone="cyan"><Zap className="h-3 w-3" />رویدادمحور</Badge>}
                  {r.time_sensitive && <Badge tone="danger">زمان‌حساس</Badge>}
                  {open && <Badge tone="danger"><ShieldAlert className="h-3 w-3" />مدارقطع‌کن باز</Badge>}
                  {r.circuit_state === "half_open" && <Badge tone="warning">نیمه‌باز (آزمایشی)</Badge>}
                  {r.created_by === "agent" && <Badge tone="accent">ساخته ایجنت</Badge>}
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2 rounded-[5px] bg-line/5 p-2.5 text-[11px]">
                  <div><div className="text-muted">اجرای بعدی</div><div className="num flex items-center gap-1"><Clock className="h-3 w-3" />{r.next_run_at ? fmtDate(r.next_run_at) : "—"}</div></div>
                  <div><div className="text-muted">آخرین اجرا</div><div className="num flex items-center gap-1"><Activity className="h-3 w-3" />{r.last_run_at ? relTime(r.last_run_at) : "—"} {r.last_status && <span className={r.last_status === "success" ? "text-success" : "text-danger"}>({r.last_status === "success" ? "موفق" : "ناموفق"})</span>}</div></div>
                  <div><div className="text-muted">تعداد اجرا</div><div className="num">{faInt(r.run_count)}</div></div>
                  <div><div className="text-muted">خطای پیاپی</div><div className={cn("num", r.consecutive_failures > 0 && "text-danger")}>{faInt(r.consecutive_failures)}</div></div>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5">
                  <Button size="sm" variant="soft" icon={<FlaskConical className="h-3.5 w-3.5" />} onClick={() => act(r, "test")}>اجرای آزمایشی</Button>
                  {r.paused || open ? <Button size="sm" variant="outline" icon={<Play className="h-3.5 w-3.5" />} onClick={() => act(r, "resume")}>ادامه</Button>
                    : <Button size="sm" variant="outline" icon={<Pause className="h-3.5 w-3.5" />} onClick={() => act(r, "pause")}>توقف</Button>}
                  <Button size="sm" variant="ghost" icon={<Pencil className="h-3.5 w-3.5" />} onClick={() => setEdit(r)}>ویرایش</Button>
                  <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={async () => { if (!confirm(`روتین «${r.name}» حذف شود؟`)) return; await del(`/api/routines/${r.id}`).catch(toastError); qc.invalidateQueries({ queryKey: ["routines"] }); }} />
                </div>
              </Card>
            </motion.div>
          );
        })}
      </div>
      {(creating || edit) && <RoutineForm routine={edit || undefined} onClose={() => { setCreating(false); setEdit(null); }} />}
    </div>
  );
}
