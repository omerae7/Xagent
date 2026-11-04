/**
 * مرکز کنترل مدیر: هر کاری که مدیر روی سیستم خودش انجام می‌دهد اینجا دیده، بررسی و برگردانده می‌شود.
 * زبانه‌ها: کارمندان · تاریخچه تغییرات (Rollback) · عیب‌یابی و خوددرمانی · سلامت مدل‌ها.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, AlertTriangle, CheckCircle2, CircuitBoard, History, Plus, RotateCcw, ShieldCheck, Stethoscope, TerminalSquare, Trash2, Users, Wrench } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, Button, Card, CardHeader, cn, Empty, ErrorBox, Field, Input, Modal, PageHeader, Select, Skeleton, Switch, Tabs, Textarea, type Tone } from "../components/ui";
import { ChipMark } from "../components/viz/Silicon";
import HostTerminal from "./HostTerminal";
import { del, get, post } from "../lib/api";
import { faInt, fmtDate, relTime, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

type TabKey = "employees" | "changes" | "health" | "models" | "host";

const GROUPS: [string, string][] = [
  ["web", "وب (جستجو و خواندن)"], ["files", "فایل‌ها"], ["system", "سیستم"], ["tasks", "وظایف"], ["routines", "روتین‌ها"],
  ["crm", "CRM"], ["browser", "مرورگر"], ["desktop", "دسکتاپ"], ["telegram", "تلگرام"], ["episodes", "سابقه کارها"], ["installed", "ابزارهای نصب‌شده"],
];
const ACTOR_FA: Record<string, string> = { owner: "شما", manager: "مدیر", self_heal: "خوددرمانی", system: "سیستم", rollback: "بازگردانی" };
const CH_STATUS: Record<string, [string, Tone]> = {
  applied: ["اعمال شد", "primary"], verified: ["آزمون‌شده", "success"], verification_failed: ["آزمون ناموفق", "danger"],
  rolled_back: ["برگردانده شد", "neutral"], rejected: ["رد شد", "danger"], pending_approval: ["منتظر اجازه", "warning"],
};
const TARGET_FA: Record<string, string> = { setting: "تنظیمات", model: "مدل", provider: "ارائه‌دهنده", tool: "ابزار", employee: "کارمند", rollback: "بازگردانی" };
const RISK: Record<string, Tone> = { low: "neutral", medium: "warning", high: "danger" };
const INC_STATUS: Record<string, [string, Tone]> = { open: ["باز", "danger"], fixing: ["در حال اصلاح", "warning"], fixed: ["رفع شد", "success"], escalated: ["ارجاع به شما", "warning"], ignored: ["نادیده", "neutral"] };
const SEV: Record<string, Tone> = { high: "danger", medium: "warning", low: "neutral" };

function useModels() {
  return useQuery({ queryKey: ["providers"], queryFn: () => get("/api/settings/ai/providers") });
}

// ───────────── کارمندان ─────────────
function EmployeeEditor({ initial, models, onClose }: { initial: any | null; models: any[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<any>(initial || { name: "", title: "", description: "", instructions: "", model_ids: [], tool_groups: ["web"], memory_scope: "shared", enabled: true, max_steps: 12, max_cost_usd: 0.2 });
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try {
      await post("/api/control/employees", { ...f, max_steps: Number(f.max_steps), max_cost_usd: Number(f.max_cost_usd), model_ids: (f.model_ids || []).filter(Boolean) });
      toast("کارمند ذخیره شد و در تاریخچه تغییرات ثبت شد", "success");
      qc.invalidateQueries({ queryKey: ["employees"] });
      qc.invalidateQueries({ queryKey: ["changes"] });
      onClose();
    } catch (e) { toastError(e); } finally { setSaving(false); }
  };
  const toggleGroup = (g: string) => setF({ ...f, tool_groups: f.tool_groups?.includes(g) ? f.tool_groups.filter((x: string) => x !== g) : [...(f.tool_groups || []), g] });
  return (
    <Modal open wide onClose={onClose} title={initial ? `ویرایش کارمند «${initial.name}»` : "کارمند جدید"}
      footer={<><Button variant="ghost" onClick={onClose}>انصراف</Button><Button variant="primary" loading={saving} disabled={!f.name.trim()} onClick={save}>ذخیره کارمند</Button></>}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="نام (شناسه یکتا)"><Input value={f.name} disabled={!!initial} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="مثلاً researcher" className="ltr" /></Field>
        <Field label="عنوان شغلی"><Input value={f.title || ""} onChange={(e) => setF({ ...f, title: e.target.value })} placeholder="مثلاً پژوهشگر بازار" /></Field>
        <Field label="شرح وظیفه" className="sm:col-span-2"><Input value={f.description || ""} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="مدیر بر اساس این شرح تصمیم می‌گیرد کار را به این کارمند بسپارد" /></Field>
        <Field label="دستورالعمل کاری" className="sm:col-span-2"><Textarea value={f.instructions || ""} onChange={(e) => setF({ ...f, instructions: e.target.value })} placeholder="روش کار، قالب گزارش، محدودیت‌ها" /></Field>
        <div className="sm:col-span-2">
          <p className="mb-2 text-xs font-medium text-muted">مدل‌ها به ترتیب اولویت (خالی = انتخاب خودکار مدل کارمند)</p>
          <div className="grid gap-2 sm:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <Select key={i} value={f.model_ids?.[i] || ""} onChange={(e) => { const ids = [...(f.model_ids || [])]; ids[i] = e.target.value; setF({ ...f, model_ids: ids }); }}>
                <option value="">{i === 0 ? "خودکار" : "Fallback (اختیاری)"}</option>
                {models.filter((m) => m.enabled).map((m) => <option key={m.id} value={m.id}>{m.display_name || m.model_name}</option>)}
              </Select>
            ))}
          </div>
        </div>
        <div className="sm:col-span-2">
          <p className="mb-2 text-xs font-medium text-muted">گروه ابزارهای مجاز (پایه و حافظه همیشه فعال‌اند)</p>
          <div className="flex flex-wrap gap-1.5">
            {GROUPS.map(([g, l]) => (
              <button key={g} type="button" onClick={() => toggleGroup(g)} aria-pressed={f.tool_groups?.includes(g)}
                className={cn("bevel h-8 px-3 text-xs ring-1 ring-inset transition-colors", f.tool_groups?.includes(g) ? "bg-primary/12 text-primary ring-primary/30" : "text-muted ring-line/20 hover:text-ink")}>{l}</button>
            ))}
          </div>
        </div>
        <Field label="حافظه"><Select value={f.memory_scope} onChange={(e) => setF({ ...f, memory_scope: e.target.value })}><option value="shared">مشترک با مدیر</option><option value="private">خصوصی کارمند</option></Select></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="حداکثر گام"><Input type="number" min={1} max={100} value={f.max_steps} onChange={(e) => setF({ ...f, max_steps: e.target.value })} /></Field>
          <Field label="سقف هزینه هر کار ($)"><Input type="number" min={0} max={20} step="0.05" value={f.max_cost_usd} onChange={(e) => setF({ ...f, max_cost_usd: e.target.value })} /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm sm:col-span-2"><Switch checked={f.enabled} onChange={(v) => setF({ ...f, enabled: v })} /> کارمند فعال باشد</label>
      </div>
    </Modal>
  );
}

function Employees() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["employees"], queryFn: () => get("/api/control/employees") });
  const models = useModels();
  const [edit, setEdit] = useState<any | null | undefined>(undefined);
  const items: any[] = q.data?.items || [];
  const allModels: any[] = models.data?.models || [];
  const name = (id: string) => allModels.find((m) => m.id === id)?.display_name || allModels.find((m) => m.id === id)?.model_name || "مدل حذف‌شده";
  if (q.error) return <ErrorBox error={q.error} retry={q.refetch} />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm leading-6 text-muted">مدیر کارهای تخصصی را به کارمندان می‌سپارد و گزارش‌شان را ارزیابی می‌کند. مدیر خودش هم می‌تواند با دستور زبان طبیعی کارمند بسازد یا تغییر دهد؛ همه تغییرات در «تاریخچه» قابل بازگشت‌اند.</p>
        <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setEdit(null)}>کارمند جدید</Button>
      </div>
      {q.isLoading && <Skeleton className="h-32" />}
      {!q.isLoading && !items.length && (
        <Card><Empty icon={<Users className="h-6 w-6" />} title="هنوز کارمندی تعریف نشده"
          text="مثلاً در گفتگو بنویسید: «یک کارمند پژوهشگر بساز که فقط به وب دسترسی داشته باشد» یا از دکمه کارمند جدید استفاده کنید." /></Card>
      )}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {items.map((e) => {
          const st = e.stats || {};
          return (
            <Card key={e.id} className="flex flex-col p-4">
              <div className="flex items-start gap-3">
                <ChipMark state={!e.enabled ? "stopped" : e.status === "busy" ? "executing" : e.status === "error" ? "waiting" : "idle"} size={34} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="truncate font-semibold">{e.title || e.name}</span>
                    <Badge tone={!e.enabled ? "neutral" : e.status === "busy" ? "primary" : e.status === "error" ? "warning" : "success"} dot>{!e.enabled ? "غیرفعال" : e.status === "busy" ? "مشغول" : e.status === "error" ? "آخرین کار ناموفق" : "آماده"}</Badge>
                  </div>
                  <div className="ltr mono text-end text-[11px] text-muted">{e.name}</div>
                </div>
                <Switch checked={e.enabled} label="فعال" onChange={async (v) => { try { await post("/api/control/employees", { name: e.name, enabled: v }); qc.invalidateQueries({ queryKey: ["employees"] }); qc.invalidateQueries({ queryKey: ["changes"] }); } catch (x) { toastError(x); } }} />
              </div>
              {e.description && <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted">{e.description}</p>}
              <div className="mt-3 flex flex-wrap gap-1">
                {(e.tool_groups || []).map((g: string) => <Badge key={g} tone="cyan">{GROUPS.find(([k]) => k === g)?.[1] || g}</Badge>)}
                {(e.model_ids || []).map((id: string) => <Badge key={id} tone="accent">{name(id)}</Badge>)}
                {!(e.model_ids || []).length && <Badge>مدل خودکار</Badge>}
              </div>
              <div className="mt-auto grid grid-cols-3 gap-2 border-t hairline pt-3 text-center">
                {[["کار انجام‌شده", faInt(st.runs || 0)], ["موفق", faInt(st.success || 0)], ["هزینه کل", usd(st.cost || 0, 3)]].map(([l, v]) => (
                  <div key={l}><div className="num text-sm font-bold">{v}</div><div className="text-[10px] text-muted">{l}</div></div>
                ))}
              </div>
              <div className="mt-3 flex gap-1.5">
                <Button size="sm" variant="outline" className="flex-1" onClick={() => setEdit(e)}>ویرایش</Button>
                <Button size="icon" variant="ghost" aria-label="حذف کارمند" onClick={async () => { if (!confirm(`کارمند «${e.name}» حذف شود؟`)) return; try { await del(`/api/control/employees/${encodeURIComponent(e.name)}`); qc.invalidateQueries({ queryKey: ["employees"] }); qc.invalidateQueries({ queryKey: ["changes"] }); } catch (x) { toastError(x); } }}><Trash2 className="h-4 w-4" /></Button>
              </div>
            </Card>
          );
        })}
      </div>
      {edit !== undefined && <EmployeeEditor initial={edit} models={allModels} onClose={() => setEdit(undefined)} />}
    </div>
  );
}

// ───────────── تاریخچه تغییرات ─────────────
function Diff({ before, after }: { before: any; after: any }) {
  const keys = Array.from(new Set([...Object.keys(before || {}), ...Object.keys(after || {})])).filter((k) => JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]));
  if (!keys.length) return <p className="text-xs text-muted">جزئیات قبل/بعد ثبت نشده است.</p>;
  const show = (v: any) => (v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));
  return (
    <div className="space-y-1.5">
      {keys.slice(0, 12).map((k) => (
        <div key={k} className="grid gap-1 text-[11px] sm:grid-cols-[9rem_1fr_1fr]">
          <span className="ltr mono truncate text-end text-muted">{k}</span>
          <span className="ltr mono break-all rounded-[3px] bg-danger/8 px-2 py-1 text-end text-danger">{show(before?.[k])}</span>
          <span className="ltr mono break-all rounded-[3px] bg-success/8 px-2 py-1 text-end text-success">{show(after?.[k])}</span>
        </div>
      ))}
    </div>
  );
}

function Changes() {
  const qc = useQueryClient();
  const [type, setType] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["changes", type], queryFn: () => get(`/api/control/changes${type ? `?target_type=${type}` : ""}`) });
  useLive((_d, t) => { if (t === "config.changed") qc.invalidateQueries({ queryKey: ["changes"] }); });
  const items: any[] = q.data?.items || [];
  const rollback = async (c: any) => {
    if (!confirm(`تغییر «${c.summary}» برگردانده شود؟ تغییرات جدیدتر روی همین مورد هم برگردانده می‌شوند.`)) return;
    setBusy(c.id);
    try {
      const r = await post(`/api/control/changes/${c.id}/rollback`);
      toast(r.message || "برگردانده شد", "success");
      qc.invalidateQueries({ queryKey: ["changes"] });
      qc.invalidateQueries({ queryKey: ["employees"] });
      qc.invalidateQueries({ queryKey: ["providers"] });
    } catch (e) { toastError(e); } finally { setBusy(null); }
  };
  if (q.error) return <ErrorBox error={q.error} retry={q.refetch} />;
  return (
    <Card>
      <CardHeader title="هر تغییری که در سیستم داده شد" icon={<History className="h-4 w-4" />} sub="چه کسی، چه چیزی، چرا، نتیجه آزمون — با امکان بازگشت"
        actions={<Select value={type} onChange={(e) => setType(e.target.value)} className="h-8 w-36 text-xs"><option value="">همه</option>{Object.entries(TARGET_FA).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>} />
      <div className="p-3 sm:p-4">
        {q.isLoading && <Skeleton className="h-40" />}
        {!q.isLoading && !items.length && <Empty icon={<ShieldCheck className="h-6 w-6" />} title="هنوز تغییری ثبت نشده" text="وقتی شما یا مدیر تنظیمی را تغییر دهید، با جزئیات قبل و بعد اینجا ثبت می‌شود." />}
        <ol className="relative space-y-2 border-s hairline ps-4">
          {items.map((c) => {
            const [sl, st] = CH_STATUS[c.status] || [c.status, "neutral"];
            const canRollback = !["rolled_back", "rejected", "pending_approval"].includes(c.status) && c.target_type !== "rollback";
            return (
              <li key={c.id} className="relative">
                <span className={cn("pad absolute -start-[19.5px] top-4", `text-${st === "neutral" ? "muted" : st}`)} />
                <div className="die die-sm die-sunk p-3">
                  <button className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 text-start" onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id}>
                    <span className="min-w-0 flex-1 text-sm font-medium">{c.summary}</span>
                    <Badge tone={st} dot>{sl}</Badge>
                    {c.risk && c.risk !== "low" && <Badge tone={RISK[c.risk]}>ریسک {c.risk === "high" ? "بالا" : "متوسط"}</Badge>}
                  </button>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
                    <span>{ACTOR_FA[c.actor] || c.actor}</span>
                    <span>{TARGET_FA[c.target_type] || c.target_type}{c.target_id ? ` · ${c.target_id.length > 24 ? c.target_id.slice(0, 8) : c.target_id}` : ""}</span>
                    <span className="num" title={fmtDate(c.created_at)}>{relTime(c.created_at)}</span>
                    {c.rolled_back_at && <span>برگشت: {ACTOR_FA[c.rolled_back_by] || c.rolled_back_by} · {relTime(c.rolled_back_at)}</span>}
                  </div>
                  {open === c.id && (
                    <div className="mt-3 space-y-3 border-t hairline pt-3">
                      {c.reason && <p className="text-xs leading-5"><span className="text-muted">دلیل: </span>{c.reason}</p>}
                      <Diff before={c.before} after={c.after} />
                      {c.verification && Object.keys(c.verification).length > 0 && (
                        <p className={cn("text-xs", c.verification.ok === false ? "text-danger" : "text-success")}>
                          آزمون پس از تغییر: {c.verification.ok === false ? "ناموفق" : "موفق"}{c.verification.message ? ` — ${c.verification.message}` : ""}
                        </p>
                      )}
                    </div>
                  )}
                  {canRollback && (
                    <div className="mt-2 flex justify-end">
                      <Button size="sm" variant="outline" loading={busy === c.id} icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => rollback(c)}>بازگرداندن</Button>
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </Card>
  );
}

// ───────────── عیب‌یابی و خوددرمانی ─────────────
function Health() {
  const qc = useQueryClient();
  const inc = useQuery({ queryKey: ["incidents"], queryFn: () => get("/api/control/incidents") });
  const [diag, setDiag] = useState<any>(null);
  const [busy, setBusy] = useState<"diag" | "heal" | "dry" | null>(null);
  useLive((_d, t) => { if (t === "model.health" || t === "config.changed") qc.invalidateQueries({ queryKey: ["incidents"] }); });
  const run = async (kind: "diag" | "heal" | "dry") => {
    setBusy(kind);
    try {
      if (kind === "diag") setDiag(await post("/api/control/diagnose"));
      else {
        const r = await post(`/api/control/heal${kind === "dry" ? "?dry_run=true" : ""}`);
        const a = (r.applied || []).length, s = (r.skipped || []).length, e = (r.escalated || []).length;
        toast(kind === "dry" ? `پیش‌نمایش: ${faInt(a)} اصلاح قابل اجرا · ${faInt(e)} نیازمند اجازه شما` : `${faInt(a)} اصلاح اعمال شد · ${faInt(s)} رد شد · ${faInt(e)} به شما ارجاع شد`, a ? "success" : "info", 9000);
        setDiag(await post("/api/control/diagnose"));
        qc.invalidateQueries({ queryKey: ["incidents"] });
        qc.invalidateQueries({ queryKey: ["changes"] });
      }
    } catch (e) { toastError(e); } finally { setBusy(null); }
  };
  const items: any[] = inc.data?.items || [];
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader title="عیب‌یابی کل سیستم" icon={<Stethoscope className="h-4 w-4" />} sub="مسیریابی مدل‌ها، سلامت مدل و ابزار، کلیدها و خطاهای اخیر" />
        <div className="space-y-3 p-4">
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" loading={busy === "diag"} icon={<Activity className="h-4 w-4" />} onClick={() => run("diag")}>اجرای عیب‌یابی</Button>
            <Button variant="outline" loading={busy === "dry"} onClick={() => run("dry")}>پیش‌نمایش اصلاح</Button>
            <Button variant="soft" loading={busy === "heal"} icon={<Wrench className="h-4 w-4" />} onClick={() => run("heal")}>اصلاح خودکار امن</Button>
          </div>
          <p className="text-[11px] leading-5 text-muted">اصلاح خودکار فقط تغییرات مجاز و برگشت‌پذیر انجام می‌دهد (مثل جایگزینی مدل از کار افتاده با مدل سالم). هر اصلاح آزمون می‌شود، در تاریخچه ثبت می‌شود و حداکثر ۶ اصلاح در ساعت انجام می‌شود تا از چرخه بی‌پایان جلوگیری شود. تنظیمات امنیتی هرگز خودکار تغییر نمی‌کنند.</p>
          {diag && (
            (diag.findings || []).length === 0
              ? <div className="bevel flex items-center gap-2 bg-success/10 px-3 py-2.5 text-sm text-success"><CheckCircle2 className="h-4 w-4" /> مشکلی پیدا نشد؛ همه بخش‌ها سالم‌اند.</div>
              : <div className="space-y-2">
                {(diag.findings || []).map((f: any, i: number) => (
                  <div key={i} className="die die-sm die-sunk p-3">
                    <div className="flex flex-wrap items-center gap-2"><Badge tone={SEV[f.severity] || "neutral"} dot>{f.severity === "high" ? "مهم" : f.severity === "medium" ? "متوسط" : "کم"}</Badge><span className="ltr mono text-[11px] text-muted">{f.class}</span></div>
                    <p className="mt-1.5 text-sm leading-6">{f.message}</p>
                    {f.fix && <p className="mt-1 text-xs leading-5 text-primary">پیشنهاد: {typeof f.fix === "string" ? f.fix : f.fix.summary || JSON.stringify(f.fix)}</p>}
                  </div>
                ))}
              </div>
          )}
        </div>
      </Card>
      <Card>
        <CardHeader title="رخدادها" icon={<AlertTriangle className="h-4 w-4" />} sub="خطاهای طبقه‌بندی‌شده و اقدام‌های انجام‌شده" />
        <div className="space-y-2 p-3 sm:p-4">
          {inc.isLoading && <Skeleton className="h-24" />}
          {!inc.isLoading && !items.length && <Empty icon={<CheckCircle2 className="h-6 w-6" />} title="رخداد بازی وجود ندارد" />}
          {items.map((x) => {
            const [l, t] = INC_STATUS[x.status] || [x.status, "neutral"];
            return (
              <div key={x.id} className="die die-sm die-sunk p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={t} dot>{l}</Badge>
                  <span className="ltr mono text-[11px] text-muted">{x.source}:{x.error_class}</span>
                  <span className="num ms-auto text-[11px] text-muted">{faInt(x.count)} بار · {relTime(x.last_seen_at)}</span>
                </div>
                <p className="mt-1.5 line-clamp-3 text-xs leading-5">{x.message}</p>
                {(x.actions || []).length > 0 && <p className="mt-1 text-[11px] text-muted">اقدام: {(x.actions || []).slice(-1)[0]?.summary || (x.actions || []).slice(-1)[0]?.action}</p>}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

// ───────────── سلامت مدل‌ها ─────────────
function ModelHealth() {
  const qc = useQueryClient();
  const q = useModels();
  const [busy, setBusy] = useState<string | null>(null);
  const models: any[] = q.data?.models || [];
  const providers: any[] = q.data?.providers || [];
  const H: Record<string, [string, Tone]> = { ok: ["سالم", "success"], degraded: ["کاهش کیفیت", "warning"], failing: ["از کار افتاده", "danger"], unknown: ["آزمون‌نشده", "neutral"] };
  if (q.error) return <ErrorBox error={q.error} retry={q.refetch} />;
  return (
    <Card>
      <CardHeader title="سلامت و قابلیت مدل‌ها" icon={<CircuitBoard className="h-4 w-4" />} sub="قابلیت‌ها با آزمون عملی سنجیده می‌شوند؛ مدل ناسالم موقتاً کنار گذاشته و Fallback فعال می‌شود" />
      <div className="overflow-x-auto p-2 sm:p-4">
        {q.isLoading && <Skeleton className="h-32" />}
        {!q.isLoading && !models.length && <Empty icon={<CircuitBoard className="h-6 w-6" />} title="مدلی ثبت نشده" text="از تنظیمات ← هوش مصنوعی، ارائه‌دهنده اضافه کنید و مدل‌های حساب را همگام‌سازی کنید." />}
        {models.length > 0 && (
          <table className="w-full min-w-[640px] text-sm">
            <thead><tr className="border-b hairline text-start text-[11px] text-muted">
              <th className="py-2 text-start font-medium">مدل</th><th className="text-start font-medium">ارائه‌دهنده</th><th className="text-start font-medium">سلامت</th><th className="text-start font-medium">ابزار</th><th className="text-start font-medium">بینایی</th><th className="text-start font-medium">آخرین موفقیت</th><th />
            </tr></thead>
            <tbody>
              {models.map((m) => {
                const [hl, ht] = H[m.health] || H.unknown;
                const cap = (k: string) => m.capabilities?.[k] ? <Badge tone={m.capability_meta?.[k]?.source === "probe" ? "success" : "cyan"}>{m.capability_meta?.[k]?.source === "probe" ? "آزمون‌شده" : "اعلام‌شده"}</Badge> : <span className="text-muted">—</span>;
                return (
                  <tr key={m.id} className={cn("border-b hairline align-middle", !m.enabled && "opacity-55")}>
                    <td className="py-2.5"><div className="font-medium">{m.display_name || m.model_name}</div><div className="ltr mono text-end text-[10px] text-muted">{m.model_name}</div></td>
                    <td className="text-xs text-muted">{providers.find((p) => p.id === m.provider_id)?.name}</td>
                    <td><Badge tone={ht} dot>{hl}</Badge>{m.cooldown_until && new Date(m.cooldown_until) > new Date() && <div className="mt-0.5 text-[10px] text-warning">استراحت تا {relTime(m.cooldown_until)}</div>}</td>
                    <td>{cap("tools")}</td>
                    <td>{cap("vision")}</td>
                    <td className="num text-[11px] text-muted">{m.last_ok_at ? relTime(m.last_ok_at) : "—"}</td>
                    <td className="text-end">
                      <Button size="sm" variant="outline" loading={busy === m.id} onClick={async () => {
                        setBusy(m.id);
                        try { const r = await post(`/api/control/models/${m.id}/probe`); toast(`${r.model}: ${r.ok ? "آزمون موفق" : "آزمون ناموفق"}`, r.ok ? "success" : "error"); qc.invalidateQueries({ queryKey: ["providers"] }); } catch (e) { toastError(e); } finally { setBusy(null); }
                      }}>آزمون</Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </Card>
  );
}

export default function Control() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey) || "employees";
  return (
    <div>
      <PageHeader title="مرکز کنترل مدیر" icon={<CircuitBoard className="h-5 w-5" />}
        sub="کارمندان، هر تغییری که مدیر در سیستم داده، عیب‌یابی، سلامت مدل‌ها و ترمینال مدیریتی سرور. تنظیمات امنیتی فقط با اجازه شما تغییر می‌کنند." />
      <Tabs value={tab} onChange={(t) => setParams({ tab: t })} tabs={[
        { value: "employees", label: "کارمندان", icon: <Users className="h-4 w-4" /> },
        { value: "changes", label: "تاریخچه تغییرات", icon: <History className="h-4 w-4" /> },
        { value: "health", label: "عیب‌یابی و خوددرمانی", icon: <Stethoscope className="h-4 w-4" /> },
        { value: "models", label: "سلامت مدل‌ها", icon: <CircuitBoard className="h-4 w-4" /> },
        { value: "host", label: "ترمینال VPS", icon: <TerminalSquare className="h-4 w-4" /> },
      ]} />
      <div className="mt-4">
        {tab === "employees" && <Employees />}
        {tab === "changes" && <Changes />}
        {tab === "health" && <Health />}
        {tab === "models" && <ModelHealth />}
        {tab === "host" && <HostTerminal />}
      </div>
    </div>
  );
}
