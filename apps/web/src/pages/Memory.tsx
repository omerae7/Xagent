import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { AlertTriangle, BookOpen, Brain, Clock3, Database, FlaskConical, History, MessagesSquare, Plus, RotateCcw, ScrollText, Search, Shield, Sparkles, Trash2, Undo2 } from "lucide-react";
import { useState } from "react";
import { Badge, Button, Card, cn, Empty, ErrorBox, Field, Input, Modal, PageHeader, Select, Skeleton, Tabs, Textarea } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { compact, duration, faInt, fmtDate, relTime, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

type Tab = "undo" | "behavioral" | "semantic" | "episodic" | "experiential" | "conflicts";
const ENTITY: Record<string, string> = { behavioral_rule: "قانون رفتاری", semantic_fact: "دانسته معنایی", experience: "تجربه", skill: "مهارت", tool: "ابزار" };
const REV_STATUS: Record<string, { l: string; tone: any }> = {
  applied: { l: "اعمال‌شده", tone: "primary" }, probationary: { l: "آزمایشی", tone: "warning" }, confirmed: { l: "تاییدشده", tone: "success" }, reverted: { l: "بازگردانده‌شده", tone: "neutral" },
};
const ACTOR: Record<string, string> = { owner: "کارفرما", agent: "ایجنت", dream: "رویا", evaluator: "ارزیاب خودکار" };

function UndoCenter() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState("applied,probationary,confirmed");
  const { data, isLoading } = useQuery({ queryKey: ["revisions", filter], queryFn: () => get(`/api/memory/revisions?status=${filter}`) });
  const undo = async (id: string) => {
    try {
      const r = await post(`/api/memory/revisions/${id}/undo`, {});
      toast(r.message, "success");
      qc.invalidateQueries({ queryKey: ["revisions"] });
      qc.invalidateQueries({ queryKey: ["memory-overview"] });
    } catch (e) { toastError(e); }
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={filter} onChange={(e) => setFilter(e.target.value)} className="w-56">
          <option value="applied,probationary,confirmed">یادگیری‌های فعال</option>
          <option value="probationary">فقط آزمایشی</option>
          <option value="reverted">بازگردانده‌شده‌ها</option>
          <option value="applied,probationary,confirmed,reverted">همه</option>
        </Select>
        <p className="text-xs text-muted">هر تغییر حافظه یک Snapshot دارد؛ با Undo فوراً به حالت قبل برمی‌گردد. یادگیری‌های مضر به‌طور خودکار هم پاکسازی می‌شوند.</p>
      </div>
      {isLoading ? <Skeleton className="h-40" /> : (data?.items || []).length === 0 ? <Card><Empty icon={<History className="h-6 w-6" />} title="تغییری ثبت نشده" /></Card> : (
        <ol className="relative space-y-3 border-s hairline ps-5">
          {data.items.map((r: any, i: number) => (
            <motion.li key={r.id} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: Math.min(i, 10) * 0.03 }} className="relative">
              <span className={cn("absolute -start-[27px] top-4 h-3 w-3 rounded-full ring-4 ring-bg", r.status === "reverted" ? "bg-line" : r.status === "probationary" ? "bg-warning" : r.actor === "evaluator" ? "bg-danger" : "bg-primary")} />
              <Card className="p-3.5">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge tone="accent">{ENTITY[r.entity_type] || r.entity_type}</Badge>
                      <Badge tone={REV_STATUS[r.status]?.tone}>{REV_STATUS[r.status]?.l}</Badge>
                      <span className="text-[11px] text-muted">{ACTOR[r.actor] || r.actor} · {relTime(r.created_at)}</span>
                    </div>
                    <p className="mt-1.5 text-sm">{r.reason}</p>
                    {r.after && <p className="mt-1 line-clamp-2 text-xs text-muted">{r.after.content || r.after.title || r.after.name || r.after.approach || JSON.stringify(r.after).slice(0, 160)}</p>}
                    {r.revert_reason && <p className="mt-1 text-xs text-warning">↩ {r.revert_reason}</p>}
                    {r.status === "probationary" && r.observed?.runs > 0 && <p className="num mt-1 text-[11px] text-muted">دوره آزمایشی: {faInt(r.observed.runs)} اجرا، {faInt(r.observed.failures)} خطا</p>}
                  </div>
                  {r.status !== "reverted" && <Button size="sm" variant="danger" icon={<Undo2 className="h-3.5 w-3.5" />} onClick={() => undo(r.id)}>Undo</Button>}
                </div>
                <p className="ltr mt-1 text-end font-mono text-[10px] text-muted/70">Revision {r.id.slice(0, 8)}</p>
              </Card>
            </motion.li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Behavioral() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["rules"], queryFn: () => get("/api/memory/rules") });
  const [f, setF] = useState<any | null>(null);
  const save = async () => {
    try {
      if (f.id) await patch(`/api/memory/rules/${f.id}`, { title: f.title, content: f.content, priority: Number(f.priority), scope: f.scope });
      else await post("/api/memory/rules", { title: f.title, content: f.content, priority: Number(f.priority || 50), scope: f.scope || "global" });
      setF(null);
      qc.invalidateQueries({ queryKey: ["rules"] });
      toast("قانون ذخیره شد (با Snapshot)", "success");
    } catch (e) { toastError(e); }
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs text-muted"><Shield className="h-3.5 w-3.5 text-success" /> فقط کارفرما می‌تواند قانون ثبت کند؛ محتوای وب، فایل یا پیام اشخاص ثالث هرگز قوانین را تغییر نمی‌دهد.</p>
        <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setF({ priority: 60, scope: "global" })}>قانون جدید</Button>
      </div>
      {isLoading ? <Skeleton className="h-32" /> : (data?.items || []).length === 0 ? <Card><Empty icon={<ScrollText className="h-6 w-6" />} title="قانونی ثبت نشده" text="در گفتگو بنویسید «از این به بعد…» یا از همین‌جا اضافه کنید." /></Card> :
        data.items.map((r: any) => (
          <Card key={r.id} className="flex items-start gap-3 p-3.5">
            <div className="num grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-primary/10 text-sm font-bold text-primary">{faInt(r.priority)}</div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5"><span className="font-medium">{r.title}</span><Badge>{r.scope}</Badge>{r.status === "probationary" && <Badge tone="warning">آزمایشی</Badge>}<span className="text-[10px] text-muted">نسخه {faInt(r.version)}</span></div>
              <p className="mt-1 text-sm leading-6 text-muted">{r.content}</p>
            </div>
            <div className="flex gap-1">
              <Button size="sm" variant="ghost" onClick={() => setF(r)}>ویرایش</Button>
              <Button size="icon" variant="ghost" onClick={async () => { await del(`/api/memory/rules/${r.id}`); qc.invalidateQueries({ queryKey: ["rules"] }); }} aria-label="حذف"><Trash2 className="h-4 w-4" /></Button>
            </div>
          </Card>
        ))}
      {f && (
        <Modal open onClose={() => setF(null)} title={f.id ? "ویرایش قانون" : "قانون رفتاری جدید"} footer={<><Button variant="ghost" onClick={() => setF(null)}>انصراف</Button><Button variant="primary" onClick={save} disabled={!f.content}>ذخیره</Button></>}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="عنوان" className="sm:col-span-2"><Input value={f.title || ""} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
            <Field label="متن قانون" className="sm:col-span-2"><Textarea value={f.content || ""} onChange={(e) => setF({ ...f, content: e.target.value })} /></Field>
            <Field label="اولویت (۰ تا ۱۰۰)"><Input type="number" min={0} max={100} value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })} /></Field>
            <Field label="دامنه"><Select value={f.scope} onChange={(e) => setF({ ...f, scope: e.target.value })}><option value="global">همه جا</option><option value="chat">گفتگو</option><option value="telegram">تلگرام</option><option value="routine">روتین‌ها</option></Select></Field>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Semantic() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [section, setSection] = useState("");
  const { data, isLoading } = useQuery({ queryKey: ["facts", q, section], queryFn: () => get(`/api/memory/facts?q=${encodeURIComponent(q)}${section ? `&section=${section}` : ""}`) });
  const [f, setF] = useState<any | null>(null);
  const save = async () => {
    try { await post("/api/memory/facts", f); setF(null); qc.invalidateQueries({ queryKey: ["facts"] }); toast("ذخیره شد", "success"); } catch (e) { toastError(e); }
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <div className="relative max-w-sm flex-1"><Search className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" /><Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="جست‌وجوی ترکیبی (متن + بردار)…" className="ps-9" /></div>
        <Select value={section} onChange={(e) => setSection(e.target.value)} className="w-48"><option value="">هر دو بخش</option><option value="agent">X Agent چیست</option><option value="owner">کارفرما و کسب‌وکار</option></Select>
        <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setF({ section: "owner", key: "", content: "" })}>دانسته جدید</Button>
      </div>
      {isLoading ? <Skeleton className="h-32" /> : (
        <div className="grid gap-3 md:grid-cols-2">
          {(data?.items || []).map((x: any) => (
            <Card key={x.id} className="p-3.5">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5"><Badge tone={x.section === "agent" ? "accent" : "primary"}>{x.section === "agent" ? "ایجنت" : "کارفرما"}</Badge><code className="text-[11px] text-muted">{x.key}</code>{x.status === "probationary" && <Badge tone="warning">آزمایشی</Badge>}</div>
                <div className="flex">
                  <Button size="sm" variant="ghost" onClick={() => setF({ section: x.section, key: x.key, content: x.content })}>ویرایش</Button>
                  <Button size="icon" variant="ghost" onClick={async () => { await del(`/api/memory/facts/${x.id}`); qc.invalidateQueries({ queryKey: ["facts"] }); }} aria-label="حذف"><Trash2 className="h-4 w-4" /></Button>
                </div>
              </div>
              <p className="mt-1.5 text-sm leading-6">{x.content}</p>
              <p className="mt-1 text-[10px] text-muted">منبع: {x.source} · {relTime(x.updated_at)}</p>
            </Card>
          ))}
          {(data?.items || []).length === 0 && <p className="text-sm text-muted">موردی یافت نشد.</p>}
        </div>
      )}
      {f && (
        <Modal open onClose={() => setF(null)} title="دانسته معنایی" footer={<><Button variant="ghost" onClick={() => setF(null)}>انصراف</Button><Button variant="primary" onClick={save} disabled={!f.key || !f.content}>ذخیره</Button></>}>
          <div className="grid gap-3">
            <Field label="بخش"><Select value={f.section} onChange={(e) => setF({ ...f, section: e.target.value })}><option value="owner">کارفرما، کسب‌وکار و همکاران</option><option value="agent">X Agent و وظایفش</option></Select></Field>
            <Field label="کلید (مثلاً business یا colleague:ali)"><Input value={f.key} onChange={(e) => setF({ ...f, key: e.target.value })} className="ltr" /></Field>
            <Field label="محتوا (کوتاه و فشرده)"><Textarea value={f.content} onChange={(e) => setF({ ...f, content: e.target.value })} /></Field>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Episodic() {
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const { data, isLoading } = useQuery({ queryKey: ["episodes", kind, status, q], queryFn: () => get(`/api/memory/episodes?limit=100${kind ? `&kind=${kind}` : ""}${status ? `&status=${status}` : ""}${q ? `&q=${encodeURIComponent(q)}` : ""}`) });
  const ST: Record<string, any> = { success: ["موفق", "success"], failed: ["ناموفق", "danger"], running: ["در حال اجرا", "primary"], stopped: ["متوقف", "warning"] };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="جست‌وجو در عنوان…" className="max-w-xs" />
        <Select value={kind} onChange={(e) => setKind(e.target.value)} className="w-36"><option value="">همه انواع</option><option value="chat">گفتگو</option><option value="task">تسک</option><option value="routine">روتین</option><option value="system">سیستم</option></Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-36"><option value="">همه وضعیت‌ها</option>{Object.entries(ST).map(([k, v]) => <option key={k} value={k}>{v[0]}</option>)}</Select>
      </div>
      <p className="flex items-center gap-1.5 text-xs text-muted"><Database className="h-3.5 w-3.5" /> این حافظه در کارهای عادی خوانده نمی‌شود (بدون توکن‌سوزی) و فقط با کوئری فیلترشده بازیابی می‌شود. سوابق حسابرسی هرگز حذف نمی‌شوند.</p>
      {isLoading ? <Skeleton className="h-40" /> : (
        <Card className="overflow-x-auto p-0">
          <table className="w-full min-w-[720px] text-sm">
            <thead><tr className="border-b hairline text-xs text-muted">{["زمان", "نوع", "عنوان", "ابزارها", "توکن", "هزینه", "مدت", "وضعیت"].map((h) => <th key={h} className="px-3 py-2.5 text-start font-medium">{h}</th>)}</tr></thead>
            <tbody>
              {(data?.items || []).map((e: any) => (
                <tr key={e.id} className="border-b hairline last:border-0 hover:bg-line/5">
                  <td className="num whitespace-nowrap px-3 py-2 text-xs text-muted">{fmtDate(e.ts)}</td>
                  <td className="px-3 py-2"><Badge>{e.kind}</Badge></td>
                  <td className="max-w-[280px] truncate px-3 py-2" title={e.summary}>{e.title}</td>
                  <td className="px-3 py-2 text-[11px] text-muted">{(e.tools || []).slice(0, 3).join("، ")}</td>
                  <td className="num px-3 py-2 text-xs">{compact(e.tokens_in + e.tokens_out)}</td>
                  <td className="num px-3 py-2 text-xs">{usd(e.cost_usd)}</td>
                  <td className="num px-3 py-2 text-xs">{duration(e.duration_ms)}</td>
                  <td className="px-3 py-2"><Badge tone={ST[e.status]?.[1]}>{ST[e.status]?.[0] || e.status}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
          {(data?.items || []).length === 0 && <p className="py-8 text-center text-sm text-muted">رویدادی یافت نشد.</p>}
        </Card>
      )}
    </div>
  );
}

function Experiential() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["experiences"], queryFn: () => get("/api/memory/experiences") });
  const OUT: Record<string, any> = { verified_success: ["موفقیت تاییدشده", "success"], failed_attempt: ["تلاش ناموفق", "danger"], obsolete: ["منسوخ", "neutral"] };
  return isLoading ? <Skeleton className="h-40" /> : (data?.items || []).length === 0 ? <Card><Empty icon={<FlaskConical className="h-6 w-6" />} title="تجربه‌ای ثبت نشده" text="پس از انجام کارهای واقعی، بهترین و کم‌مصرف‌ترین روش‌ها اینجا ذخیره می‌شوند." /></Card> : (
    <div className="grid gap-3 md:grid-cols-2">
      {data.items.map((e: any) => (
        <Card key={e.id} className="p-3.5">
          <div className="flex items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5"><Badge tone={OUT[e.outcome]?.[1]}>{OUT[e.outcome]?.[0]}</Badge>{e.status === "probationary" && <Badge tone="warning">آزمایشی</Badge>}</div>
            {e.outcome !== "obsolete" && <Button size="sm" variant="ghost" onClick={async () => { await post(`/api/memory/experiences/${e.id}/obsolete`); qc.invalidateQueries({ queryKey: ["experiences"] }); }}>منسوخ</Button>}
          </div>
          <p className="mt-1.5 font-medium">{e.title}</p>
          <p className="mt-1 whitespace-pre-line text-xs leading-5 text-muted">{e.approach}</p>
          <div className="num mt-2 flex flex-wrap gap-3 text-[11px] text-muted"><span>✓ {faInt(e.success_count)}</span><span>✗ {faInt(e.failure_count)}</span><span>{compact(e.tokens_used)} توکن</span><span>{duration(e.duration_ms)}</span></div>
        </Card>
      ))}
    </div>
  );
}

function Conflicts() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["candidates"], queryFn: () => get("/api/memory/candidates?status=conflict") });
  const act = async (id: string, a: string) => { await post(`/api/memory/candidates/${id}/${a}`).catch(toastError); qc.invalidateQueries({ queryKey: ["candidates"] }); };
  return isLoading ? <Skeleton className="h-24" /> : (data?.items || []).length === 0 ? <Card><Empty icon={<AlertTriangle className="h-6 w-6" />} title="تعارضی وجود ندارد" text="وقتی دستور جدید شما با قانون قبلی در تضاد باشد، برای تصمیم شما اینجا می‌آید." /></Card> : (
    <div className="space-y-3">{data.items.map((c: any) => (
      <Card key={c.id} className="p-4">
        <p className="text-sm">{c.content}</p>
        <p className="mt-1 text-xs text-warning">{c.note}</p>
        <div className="mt-3 flex gap-2"><Button size="sm" variant="primary" onClick={() => act(c.id, "accept")}>جایگزین قانون قبلی شود</Button><Button size="sm" variant="ghost" onClick={() => act(c.id, "reject")}>نادیده بگیر</Button></div>
      </Card>
    ))}</div>
  );
}

export default function Memory() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("undo");
  const { data: ov, error } = useQuery({ queryKey: ["memory-overview"], queryFn: () => get("/api/memory/overview") });
  useLive((_d, t) => { if (t === "memory.revision" || t === "memory.undo") { qc.invalidateQueries({ queryKey: ["revisions"] }); qc.invalidateQueries({ queryKey: ["memory-overview"] }); } });
  const layers = [
    { k: "working", l: "حافظه چت", v: ov?.working?.messages, i: <MessagesSquare className="h-4 w-4" />, t: "cyan" },
    { k: "behavioral", l: "رفتاری", v: ov?.behavioral, i: <ScrollText className="h-4 w-4" />, t: "primary" },
    { k: "semantic", l: "معنایی", v: ov?.semantic, i: <BookOpen className="h-4 w-4" />, t: "accent" },
    { k: "episodic", l: "رویدادی", v: ov?.episodic, i: <Clock3 className="h-4 w-4" />, t: "warning" },
    { k: "experiential", l: "تجربی", v: ov?.experiential, i: <Sparkles className="h-4 w-4" />, t: "success" },
  ];
  return (
    <div>
      <PageHeader title="حافظه و مرکز بازگشت" sub="۵ لایه حافظه با بودجه‌بندی کانتکست و Undo آنی یادگیری‌ها" icon={<Brain className="h-5 w-5" />} />
      {error && <ErrorBox error={error} />}
      <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {layers.map((l, i) => (
          <motion.div key={l.k} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.05 }} className="glass rounded-card p-3.5">
            <div className={cn("flex items-center gap-2 text-xs", `text-${l.t}`)}>{l.i}<span className="text-muted">{l.l}</span></div>
            <div className="num mt-1.5 text-2xl font-bold">{l.v == null ? "—" : compact(l.v)}</div>
          </motion.div>
        ))}
      </div>
      {ov?.probationary > 0 && <div className="mb-4 rounded-2xl border border-warning/30 bg-warning/8 px-4 py-2.5 text-sm"><RotateCcw className="me-1.5 inline h-4 w-4 text-warning" />{faInt(ov.probationary)} یادگیری در دوره آزمایشی است؛ در صورت پسرفت (خطا یا توکن بیشتر) خودکار پاکسازی می‌شوند.</div>}
      <Tabs value={tab} onChange={setTab} tabs={[
        { value: "undo", label: "مرکز بازگشت (Undo)", icon: <Undo2 className="h-4 w-4" /> },
        { value: "behavioral", label: "رفتاری", icon: <ScrollText className="h-4 w-4" /> },
        { value: "semantic", label: "معنایی", icon: <BookOpen className="h-4 w-4" /> },
        { value: "episodic", label: "رویدادی", icon: <Clock3 className="h-4 w-4" /> },
        { value: "experiential", label: "تجربی", icon: <Sparkles className="h-4 w-4" /> },
        { value: "conflicts", label: `تعارض‌ها${ov?.candidates_conflict ? ` (${faInt(ov.candidates_conflict)})` : ""}`, icon: <AlertTriangle className="h-4 w-4" /> },
      ]} />
      <div className="mt-4">
        {tab === "undo" && <UndoCenter />}
        {tab === "behavioral" && <Behavioral />}
        {tab === "semantic" && <Semantic />}
        {tab === "episodic" && <Episodic />}
        {tab === "experiential" && <Experiential />}
        {tab === "conflicts" && <Conflicts />}
      </div>
    </div>
  );
}
