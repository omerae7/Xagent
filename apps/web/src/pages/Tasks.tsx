import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Bot, CalendarCheck, CalendarDays, ChevronLeft, ChevronRight, Columns3, GitBranch, List, Play, Plus, Trash2, User } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge, Button, Card, cn, Empty, ErrorBox, Field, Input, Modal, PageHeader, Segmented, Select, Skeleton, Textarea } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { faInt, fmtDate, isoToLocalInput, localInputToIso, toFaDigits } from "../lib/format";
import { currentJMonth, J_MONTHS, J_WEEKDAYS, monthGrid, sameDay, shiftMonth } from "../lib/jalali";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const STATUS = [
  { v: "todo", l: "انجام‌نشده", tone: "neutral" },
  { v: "in_progress", l: "در حال انجام", tone: "primary" },
  { v: "waiting", l: "منتظر", tone: "warning" },
  { v: "done", l: "انجام‌شده", tone: "success" },
  { v: "failed", l: "ناموفق", tone: "danger" },
] as const;
// نوع اجرای تسک (Migration 0006): human فقط یادآوری؛ agent در موعد خودکار اجرا می‌شود؛ dependent پس از پیش‌نیاز
const KIND: Record<string, { l: string; short: string; tone: any; hint: string }> = {
  human: { l: "یادآوری برای انسان", short: "یادآوری", tone: "neutral", hint: "در موعد فقط یادآوری در میز کار ثبت می‌شود؛ ایجنت کاری انجام نمی‌دهد." },
  agent: { l: "تسک زمان‌بندی‌شده ایجنت", short: "ایجنت · زمان‌بندی", tone: "accent", hint: "در زمان شروع (یا مهلت) ایجنت خودکار اجرا می‌کند و نتیجه را گزارش می‌دهد." },
  dependent: { l: "وابسته به تسک دیگر", short: "وابسته", tone: "cyan", hint: "پس از انجام‌شدن پیش‌نیاز، ایجنت خودکار اجرا می‌کند." },
};
function runLabel(t: any): { l: string; tone: any } | null {
  if (t.kind === "human" || !t.kind) return null;
  if (t.status === "in_progress") return { l: "در حال اجرا", tone: "primary" };
  if (t.status === "done") return { l: "اجرا شد", tone: "success" };
  if (t.status === "failed") return { l: "اجرای ناموفق", tone: "danger" };
  if (t.status === "waiting") return { l: "منتظر اجازه/ورودی", tone: "warning" };
  return { l: t.kind === "dependent" ? "منتظر پیش‌نیاز" : "زمان‌بندی‌شده", tone: "neutral" };
}
function KindBadge({ t }: { t: any }) {
  const k = KIND[t.kind || "human"];
  const r = runLabel(t);
  return (
    <>
      <Badge tone={k.tone} className="!py-0">
        {t.kind === "agent" ? <Bot className="h-3 w-3" /> : t.kind === "dependent" ? <GitBranch className="h-3 w-3" /> : <User className="h-3 w-3" />}
        {k.short}
      </Badge>
      {r && t.status !== "done" && t.status !== "todo" && <Badge tone={r.tone} className="!py-0">{r.l}</Badge>}
    </>
  );
}
const PRIORITY: Record<string, { l: string; tone: any }> = {
  low: { l: "کم", tone: "neutral" },
  normal: { l: "عادی", tone: "cyan" },
  high: { l: "بالا", tone: "warning" },
  urgent: { l: "فوری", tone: "danger" },
};
const ASSIGNEE: Record<string, string> = { agent: "ایجنت", owner: "کارفرما", contact: "همکار" };

function TaskForm({ open, onClose, task }: { open: boolean; onClose: () => void; task?: any }) {
  const qc = useQueryClient();
  const contacts = useQuery({ queryKey: ["contacts"], queryFn: () => get("/api/crm/contacts"), enabled: open });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => get("/api/crm/projects"), enabled: open });
  const [f, setF] = useState<any>(() => task || { title: "", description: "", priority: "normal", status: "todo", assignee_type: "agent", kind: "human", missed_policy: "run_late" });
  const others = useQuery({ queryKey: ["tasks", "all"], queryFn: () => get("/api/tasks?status=todo,in_progress,waiting,done,failed"), enabled: open && f.kind === "dependent" });
  const save = useMutation({
    mutationFn: (runNow: boolean) => {
      const body = { ...f, run_now: runNow, due_at: f.due_at || null, start_at: f.start_at || null,
        depends_on_id: f.kind === "dependent" ? f.depends_on_id || "" : "",
        assignee_contact_id: f.assignee_type === "contact" ? f.assignee_contact_id || "" : "" };
      return task ? patch(`/api/tasks/${task.id}`, body) : post("/api/tasks", body);
    },
    onSuccess: (_r, runNow) => {
      toast(runNow ? "تسک به ایجنت سپرده شد" : "تسک ذخیره شد", "success");
      qc.invalidateQueries({ queryKey: ["tasks"] });
      onClose();
    },
    onError: toastError,
  });
  const set = (k: string, v: any) => setF((x: any) => ({ ...x, [k]: v }));
  return (
    <Modal open={open} onClose={onClose} title={task ? "ویرایش تسک" : "تسک جدید"} footer={
      <>
        <Button variant="ghost" onClick={onClose}>انصراف</Button>
        {f.assignee_type === "agent" && <Button variant="soft" loading={save.isPending} onClick={() => save.mutate(true)} icon={<Play className="h-4 w-4" />}>ذخیره و اجرا توسط ایجنت</Button>}
        <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(false)} disabled={!f.title || (f.kind === "dependent" && !f.depends_on_id)}>ذخیره</Button>
      </>
    }>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="عنوان" className="sm:col-span-2"><Input value={f.title} onChange={(e) => set("title", e.target.value)} autoFocus /></Field>
        <Field label="شرح" className="sm:col-span-2"><Textarea value={f.description || ""} onChange={(e) => set("description", e.target.value)} /></Field>
        <Field label="نوع اجرا" hint={KIND[f.kind || "human"].hint} className="sm:col-span-2">
          <Segmented value={f.kind || "human"} onChange={(v) => set("kind", v)} options={Object.entries(KIND).map(([k, v]) => ({ value: k, label: v.l }))} />
        </Field>
        {f.kind === "agent" && (
          <>
            <Field label="زمان اجرا" hint="اگر خالی بماند، مهلت ملاک اجراست."><Input type="datetime-local" value={isoToLocalInput(f.start_at)} onChange={(e) => set("start_at", localInputToIso(e.target.value))} /></Field>
            <Field label="اگر زمان اجرا از دست رفت" hint="مثلاً سرور خاموش بوده است.">
              <Select value={f.missed_policy || "run_late"} onChange={(e) => set("missed_policy", e.target.value)}>
                <option value="run_late">با تأخیر اجرا شود</option><option value="skip">اجرا نشود و گزارش شود</option>
              </Select>
            </Field>
          </>
        )}
        {f.kind === "dependent" && (
          <Field label="پیش‌نیاز" className="sm:col-span-2">
            <Select value={f.depends_on_id || ""} onChange={(e) => set("depends_on_id", e.target.value)}>
              <option value="">انتخاب تسک پیش‌نیاز…</option>
              {(others.data?.items || []).filter((x: any) => x.id !== task?.id).map((x: any) => <option key={x.id} value={x.id}>{x.title}</option>)}
            </Select>
          </Field>
        )}
        <Field label="وضعیت"><Select value={f.status} onChange={(e) => set("status", e.target.value)}>{STATUS.map((s) => <option key={s.v} value={s.v}>{s.l}</option>)}<option value="cancelled">لغوشده</option></Select></Field>
        <Field label="اولویت"><Select value={f.priority} onChange={(e) => set("priority", e.target.value)}>{Object.entries(PRIORITY).map(([k, v]) => <option key={k} value={k}>{v.l}</option>)}</Select></Field>
        <Field label="مسئول"><Select value={f.assignee_type} onChange={(e) => set("assignee_type", e.target.value)}>{Object.entries(ASSIGNEE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></Field>
        {f.assignee_type === "contact" ? (
          <Field label="همکار"><Select value={f.assignee_contact_id || ""} onChange={(e) => set("assignee_contact_id", e.target.value)}><option value="">انتخاب…</option>{(contacts.data?.items || []).map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}</Select></Field>
        ) : <div className="hidden sm:block" />}
        <Field label="مهلت" hint="تاریخ بر اساس تقویم مرورگر وارد می‌شود و در پنل به شمسی نمایش داده می‌شود."><Input type="datetime-local" value={isoToLocalInput(f.due_at)} onChange={(e) => set("due_at", localInputToIso(e.target.value))} /></Field>
        <Field label="پروژه"><Select value={f.project_id || ""} onChange={(e) => set("project_id", e.target.value)}><option value="">بدون پروژه</option>{(projects.data?.items || []).map((p: any) => <option key={p.id} value={p.id}>{p.name}</option>)}</Select></Field>
      </div>
    </Modal>
  );
}

function TaskRow({ t, onEdit }: { t: any; onEdit: () => void }) {
  const qc = useQueryClient();
  const toggle = async () => {
    await patch(`/api/tasks/${t.id}`, { status: t.status === "done" ? "todo" : "done" }).catch(toastError);
    qc.invalidateQueries({ queryKey: ["tasks"] });
  };
  return (
    <motion.div layout initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-3 rounded-xl px-3 py-2.5 transition hover:bg-line/6">
      <button onClick={toggle} aria-label="تغییر وضعیت" className={cn("grid h-5 w-5 shrink-0 place-items-center rounded-md border-2 transition", t.status === "done" ? "border-success bg-success text-white" : "border-line/40 hover:border-primary")}>
        {t.status === "done" && <svg viewBox="0 0 12 12" className="h-3 w-3"><path d="M2 6l3 3 5-6" stroke="currentColor" strokeWidth="2" fill="none" /></svg>}
      </button>
      <button onClick={onEdit} className="min-w-0 flex-1 text-start">
        <div className={cn("truncate text-sm", t.status === "done" && "text-muted line-through")}>{t.title}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-muted">
          <span className="flex items-center gap-1"><User className="h-3 w-3" />{t.assignee_name || ASSIGNEE[t.assignee_type]}</span>
          {t.due_at && <span className={cn("num", new Date(t.due_at) < new Date() && t.status !== "done" && "text-danger")}>⏰ {fmtDate(t.due_at)}</span>}
          <KindBadge t={t} />
          {t.created_by === "agent" && <Badge tone="neutral" className="!py-0">ساخته ایجنت</Badge>}
        </div>
        {t.result_summary && <div className="mt-1 line-clamp-2 text-[12px] leading-5 text-muted">{t.result_summary}</div>}
      </button>
      <Badge tone={PRIORITY[t.priority]?.tone}>{PRIORITY[t.priority]?.l}</Badge>
    </motion.div>
  );
}

function Kanban({ items, onEdit }: { items: any[]; onEdit: (t: any) => void }) {
  const qc = useQueryClient();
  const [over, setOver] = useState<string | null>(null);
  const move = async (id: string, status: string) => {
    qc.setQueryData(["tasks", "all"], (old: any) => old && { items: old.items.map((x: any) => (x.id === id ? { ...x, status } : x)) });
    await patch(`/api/tasks/${id}`, { status }).catch(toastError);
    qc.invalidateQueries({ queryKey: ["tasks"] });
  };
  return (
    <div className={cn("no-scrollbar -mx-3 flex snap-x gap-4 overflow-x-auto px-3 pb-2 sm:-mx-4 sm:px-4 lg:mx-0 lg:grid lg:px-0",
      items.some((t) => t.status === "failed") ? "lg:grid-cols-5" : "lg:grid-cols-4")}>
      {STATUS.filter((c) => c.v !== "failed" || items.some((t) => t.status === "failed")).map((col) => {
        const list = items.filter((t) => t.status === col.v);
        return (
          <div key={col.v} onDragOver={(e) => { e.preventDefault(); setOver(col.v); }} onDragLeave={() => setOver(null)}
            onDrop={(e) => { setOver(null); move(e.dataTransfer.getData("id"), col.v); }}
            className={cn("die min-h-[300px] w-[80vw] shrink-0 snap-start p-3 transition sm:w-[320px] lg:w-auto", over === col.v && " ring-1 ring-primary/40")}>
            <div className="mb-3 flex items-center justify-between px-1">
              <Badge tone={col.tone as any} dot>{col.l}</Badge>
              <span className="num text-xs text-muted">{faInt(list.length)}</span>
            </div>
            <div className="space-y-2">
              {list.map((t) => (
                <motion.div layout key={t.id} draggable onDragStart={(e: any) => e.dataTransfer.setData("id", t.id)} onClick={() => onEdit(t)}
                  className="cursor-grab rounded-xl border hairline bg-bg2/60 p-3 transition hover:border-primary/40 active:cursor-grabbing">
                  <div className="text-sm">{t.title}</div>
                  <div className="mt-1.5 flex flex-wrap gap-1 text-[11px]"><KindBadge t={t} /></div>
                  {t.result_summary && <div className="mt-1.5 line-clamp-2 text-[11.5px] leading-5 text-muted">{t.result_summary}</div>}
                  <div className="mt-2 flex items-center justify-between text-[11px] text-muted">
                    <span>{t.assignee_name || ASSIGNEE[t.assignee_type]}</span>
                    <Badge tone={PRIORITY[t.priority]?.tone}>{PRIORITY[t.priority]?.l}</Badge>
                  </div>
                  {t.due_at && <div className="num mt-1.5 text-[11px] text-muted">⏰ {fmtDate(t.due_at)}</div>}
                </motion.div>
              ))}
              {list.length === 0 && <p className="py-8 text-center text-xs text-muted">کارت را اینجا رها کنید</p>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Calendar({ items, onEdit }: { items: any[]; onEdit: (t: any) => void }) {
  const [m, setM] = useState(currentJMonth());
  const cells = useMemo(() => monthGrid(m), [m]);
  const today = new Date();
  return (
    <Card className="p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between">
        <Button variant="ghost" size="icon" onClick={() => setM(shiftMonth(m, -1))} aria-label="ماه قبل"><ChevronRight className="h-5 w-5" /></Button>
        <div className="text-base font-semibold">{J_MONTHS[m.jm - 1]} {toFaDigits(m.jy)}</div>
        <Button variant="ghost" size="icon" onClick={() => setM(shiftMonth(m, 1))} aria-label="ماه بعد"><ChevronLeft className="h-5 w-5" /></Button>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center text-[11px] text-muted">{J_WEEKDAYS.map((w) => <div key={w} className="py-1">{w}</div>)}</div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((c, i) => {
          const dayTasks = c.date ? items.filter((t) => t.due_at && sameDay(new Date(t.due_at), c.date!)) : [];
          const isToday = c.date && sameDay(c.date, today);
          return (
            <div key={i} className={cn("min-h-[64px] rounded-xl p-1 sm:min-h-[92px] sm:p-1.5", c.date ? "bg-line/5" : "", isToday && "ring-1 ring-primary/60 bg-primary/8")}>
              {c.jd && <div className={cn("num mb-1 text-[11px]", isToday ? "font-bold text-primary" : "text-muted")}>{toFaDigits(c.jd)}</div>}
              <div className="space-y-1">
                {dayTasks.slice(0, 3).map((t) => (
                  <button key={t.id} onClick={() => onEdit(t)} className={cn("block w-full truncate rounded-md px-1 py-0.5 text-start text-[10px]", t.status === "done" ? "bg-success/15 text-success line-through" : "bg-primary/15 text-primary")}>
                    {t.title}
                  </button>
                ))}
                {dayTasks.length > 3 && <div className="text-[10px] text-muted">+{toFaDigits(dayTasks.length - 3)}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export default function Tasks() {
  const [view, setView] = useState<"list" | "kanban" | "calendar">("kanban");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<"all" | "human" | "agent">("all");
  const [edit, setEdit] = useState<any | null>(null);
  const [creating, setCreating] = useState(false);
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["tasks", "all"], queryFn: () => get("/api/tasks?status=todo,in_progress,waiting,done,failed") });
  useLive((_d, t) => t === "task.changed" && qc.invalidateQueries({ queryKey: ["tasks"] }));
  const items: any[] = (data?.items || []).filter((t: any) => (!q || t.title.includes(q))
    && (kind === "all" || (kind === "human" ? (t.kind || "human") === "human" : t.kind === "agent" || t.kind === "dependent")));

  return (
    <div>
      <PageHeader title="وظایف" sub="کارهای ایجنت، شما و همکاران" icon={<CalendarCheck className="h-5 w-5" />} actions={
        <>
          <Segmented value={view} onChange={setView} options={[
            { value: "kanban", label: <span className="flex items-center gap-1"><Columns3 className="h-3.5 w-3.5" />کانبان</span> },
            { value: "list", label: <span className="flex items-center gap-1"><List className="h-3.5 w-3.5" />لیست</span> },
            { value: "calendar", label: <span className="flex items-center gap-1"><CalendarDays className="h-3.5 w-3.5" />تقویم</span> },
          ]} />
          <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>تسک جدید</Button>
        </>
      } />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="w-full max-w-sm"><Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="جست‌وجوی تسک…" /></div>
        <Segmented value={kind} onChange={setKind} options={[
          { value: "all", label: "همه" }, { value: "agent", label: "کارهای ایجنت" }, { value: "human", label: "یادآوری‌های انسانی" },
        ]} />
      </div>
      {error && <ErrorBox error={error} retry={refetch} />}
      {isLoading ? <div className="grid gap-3 lg:grid-cols-4">{[1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-64" />)}</div> : (
        <>
          {view === "kanban" && <Kanban items={items} onEdit={setEdit} />}
          {view === "calendar" && <Calendar items={items} onEdit={setEdit} />}
          {view === "list" && (
            <Card className="p-2">
              {items.length === 0 ? <Empty icon={<CalendarCheck className="h-6 w-6" />} title="تسکی وجود ندارد" text="تسک جدید بسازید یا در گفتگو از ایجنت بخواهید." /> :
                items.map((t) => (
                  <div key={t.id} className="group flex items-center">
                    <div className="flex-1"><TaskRow t={t} onEdit={() => setEdit(t)} /></div>
                    <Button variant="ghost" size="icon" className="opacity-0 group-hover:opacity-100" onClick={async () => { if (!confirm("حذف شود؟")) return; await del(`/api/tasks/${t.id}`); qc.invalidateQueries({ queryKey: ["tasks"] }); }} aria-label="حذف"><Trash2 className="h-4 w-4" /></Button>
                  </div>
                ))}
            </Card>
          )}
        </>
      )}
      {(creating || edit) && <TaskForm open onClose={() => { setCreating(false); setEdit(null); }} task={edit || undefined} />}
    </div>
  );
}
