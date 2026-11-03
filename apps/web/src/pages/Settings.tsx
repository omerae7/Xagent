import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  Brain, CheckCircle2, CircleSlash, Cloud, Cpu, Download, Globe2, KeyRound, Lock, MessageCircle, Moon, OctagonX, Plus, Power, RefreshCw,
  Shield, ShieldCheck, SlidersHorizontal, Sparkles, Trash2, User, Wand2, XCircle,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Badge, Button, Card, CardHeader, cn, ErrorBox, Field, Input, Modal, PageHeader, Segmented, Select, Skeleton, Switch, Tabs, Textarea } from "../components/ui";
import { del, get, patch, post, put } from "../lib/api";
import { faInt, fmtDate, setLocale } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

type TabKey = "general" | "ai" | "harness" | "limits" | "dream" | "telegram" | "security" | "system";

function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => get("/api/settings") });
}

function SaveBar({ onSave, saving }: { onSave: () => void; saving?: boolean }) {
  return <div className="flex justify-end border-t hairline px-5 py-3"><Button variant="primary" loading={saving} onClick={onSave}>ذخیره تغییرات</Button></div>;
}

function useSection<T>(key: string) {
  const { data } = useSettings();
  const qc = useQueryClient();
  const [v, setV] = useState<T | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (data && !v) setV(data.settings[key]); }, [data, key, v]);
  const save = async (value?: T) => {
    setSaving(true);
    try {
      const r = await put(`/api/settings/${key}`, value ?? v);
      setV(r.value);
      qc.invalidateQueries({ queryKey: ["settings"] });
      toast("ذخیره شد", "success");
      return r.value;
    } catch (e) { toastError(e); } finally { setSaving(false); }
  };
  return { v, setV, save, saving, data };
}

// ───────────── عمومی ─────────────
function General() {
  const { v, setV, save, saving, data } = useSection<any>("general");
  if (!v) return <Skeleton className="h-60" />;
  const set = (k: string, x: any) => setV({ ...v, [k]: x });
  return (
    <Card>
      <CardHeader title="تنظیمات پایه و موقعیت زمانی" icon={<Globe2 className="h-4 w-4" />} />
      <div className="grid gap-4 p-5 sm:grid-cols-2">
        <Field label="نام ایجنت"><Input value={v.agent_name} onChange={(e) => set("agent_name", e.target.value)} /></Field>
        <Field label="نام کارفرما"><Input value={v.owner_name} onChange={(e) => set("owner_name", e.target.value)} /></Field>
        <Field label="منطقه زمانی" hint="پیش‌فرض Asia/Tehran؛ زمان‌بندی روتین‌ها و رویا بر این اساس است.">
          <Select value={v.timezone} onChange={(e) => set("timezone", e.target.value)} className="ltr">
            {(data?.timezones || []).map((z: string) => <option key={z} value={z}>{z}</option>)}
          </Select>
        </Field>
        <Field label="تقویم نمایش">
          <Segmented value={v.calendar} onChange={(x) => set("calendar", x)} options={[{ value: "jalali", label: "شمسی" }, { value: "gregorian", label: "میلادی" }]} />
        </Field>
        <Field label="شخصیت و لحن ایجنت" className="sm:col-span-2"><Textarea value={v.persona} onChange={(e) => set("persona", e.target.value)} /></Field>
      </div>
      <SaveBar saving={saving} onSave={async () => { const r = await save(); if (r) setLocale(r.timezone, r.calendar); }} />
    </Card>
  );
}

// ───────────── هوش مصنوعی ─────────────
const KIND_ICON: Record<string, string> = { anthropic: "A", openai: "◎", google: "G", openrouter: "⇄", deepseek: "D", groq: "⚡", xai: "X", mistral: "M", ollama: "🦙", custom: "⚙" };
const CAPS: [string, string][] = [["tools", "ابزار"], ["vision", "بینایی"], ["stt", "گفتار→متن"], ["tts", "متن→گفتار"], ["embedding", "Embedding"], ["audio_in", "صوت"]];

function AddProvider({ catalog, onClose }: { catalog: any; onClose: () => void }) {
  const qc = useQueryClient();
  const [step, setStep] = useState(1);
  const [kind, setKind] = useState("");
  const [key, setKey] = useState("");
  const [base, setBase] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<any>(null);
  const preset = catalog?.providers?.[kind];
  const create = async () => {
    setBusy(true);
    try {
      const p = await post("/api/settings/ai/providers", { kind, api_key: key || null, base_url: base || null });
      const m = await post("/api/settings/ai/models", { provider_id: p.id, model_name: model });
      const t = await post(`/api/settings/ai/models/${m.id}/test`);
      setResult(t);
      qc.invalidateQueries({ queryKey: ["providers"] });
      qc.invalidateQueries({ queryKey: ["models-available"] });
      setStep(4);
    } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  return (
    <Modal open onClose={onClose} wide title="افزودن مدل هوش مصنوعی" footer={
      step === 1 ? null : step === 4 ? <Button variant="primary" onClick={onClose}>پایان</Button> : (
        <>
          <Button variant="ghost" onClick={() => setStep(step - 1)}>قبلی</Button>
          {step === 2 && <Button variant="primary" onClick={() => setStep(3)} disabled={kind !== "ollama" && !key}>بعدی</Button>}
          {step === 3 && <Button variant="primary" loading={busy} onClick={create} disabled={!model}>ذخیره و تست اتصال</Button>}
        </>
      )}>
      <div className="mb-5 flex items-center gap-2 text-xs">
        {["شرکت", "کلید API", "مدل", "تست"].map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span className={cn("grid h-6 w-6 place-items-center rounded-full", step > i ? "bg-primary text-white" : "bg-line/15 text-muted")}>{faInt(i + 1)}</span>
            <span className={step > i ? "" : "text-muted"}>{s}</span>
            {i < 3 && <span className="h-px w-6 bg-line/30" />}
          </div>
        ))}
      </div>
      {step === 1 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {Object.entries(catalog?.providers || {}).map(([k, p]: any) => (
            <button key={k} onClick={() => { setKind(k); setBase(p.base_url || ""); setModel(p.models?.[0]?.name || ""); setStep(2); }}
              className="glass flex flex-col items-center gap-2 rounded-2xl p-4 transition hover:shadow-glow">
              <span className="grid h-10 w-10 place-items-center rounded-xl bg-primary/12 text-lg font-bold text-primary">{KIND_ICON[k] || "•"}</span>
              <span className="text-sm">{p.label}</span>
            </button>
          ))}
        </div>
      )}
      {step === 2 && (
        <div className="space-y-4">
          <Field label={`کلید API ${preset?.label || ""}`} hint="کلید به‌صورت رمزنگاری‌شده (Fernet) در دیتابیس شما ذخیره می‌شود و هرگز نمایش داده نمی‌شود.">
            <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} className="ltr" placeholder="sk-..." autoFocus />
          </Field>
          {(kind === "custom" || kind === "ollama" || kind === "openrouter") && (
            <Field label="نشانی پایه (Base URL)"><Input value={base} onChange={(e) => setBase(e.target.value)} className="ltr" placeholder="https://.../v1" /></Field>
          )}
        </div>
      )}
      {step === 3 && (
        <div className="space-y-3">
          {(preset?.models || []).length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2">
              {preset.models.map((m: any) => (
                <button key={m.name} onClick={() => setModel(m.name)} className={cn("rounded-xl border p-3 text-start transition", model === m.name ? "border-primary/60 bg-primary/10" : "hairline hover:bg-line/5")}>
                  <div className="text-sm font-medium">{m.label}</div>
                  <div className="ltr text-end font-mono text-[11px] text-muted">{m.name}</div>
                  <div className="mt-1 flex flex-wrap gap-1">{m.caps.map((c: string) => <Badge key={c}>{c}</Badge>)}</div>
                </button>
              ))}
            </div>
          )}
          <Field label="یا نام مدل دلخواه"><Input value={model} onChange={(e) => setModel(e.target.value)} className="ltr" /></Field>
        </div>
      )}
      {step === 4 && result && (
        <div className={cn("flex items-start gap-3 rounded-2xl p-4", result.ok ? "bg-success/10" : "bg-danger/10")}>
          {result.ok ? <CheckCircle2 className="h-6 w-6 text-success" /> : <XCircle className="h-6 w-6 text-danger" />}
          <div><p className="font-medium">{result.ok ? "اتصال موفق بود" : "اتصال ناموفق بود"}</p><p className="mt-1 text-sm text-muted">{result.message} · {faInt(result.latency_ms)} میلی‌ثانیه</p></div>
        </div>
      )}
    </Modal>
  );
}

function ModelRow({ m }: { m: any }) {
  const qc = useQueryClient();
  const [testing, setTesting] = useState(false);
  const [edit, setEdit] = useState(false);
  const [f, setF] = useState<any>(m);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["providers"] }); qc.invalidateQueries({ queryKey: ["models-available"] }); };
  const test = async () => {
    setTesting(true);
    try { const r = await post(`/api/settings/ai/models/${m.id}/test`); toast(r.message, r.ok ? "success" : "error", 7000); refresh(); } catch (e) { toastError(e); } finally { setTesting(false); }
  };
  return (
    <div className="rounded-xl border hairline bg-bg2/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{m.display_name || m.model_name}</div>
          <div className="ltr text-end font-mono text-[11px] text-muted">{m.model_name}</div>
        </div>
        <div className="flex flex-wrap gap-1">{CAPS.filter(([c]) => m.capabilities?.[c]).map(([c, l]) => <Badge key={c} tone="cyan">{l}</Badge>)}</div>
        <Button size="sm" variant="soft" loading={testing} onClick={test}>تست اتصال</Button>
        <Button size="sm" variant="ghost" onClick={() => setEdit(true)}>ویرایش</Button>
        <Switch checked={m.enabled} onChange={async (v) => { await patch(`/api/settings/ai/models/${m.id}`, { enabled: v }); refresh(); }} label="فعال" />
      </div>
      <div className="num mt-1.5 text-[10px] text-muted">قیمت هر میلیون توکن: ورودی ${m.price_in} · خروجی ${m.price_out} · کش ${m.price_cache_read}</div>
      {edit && (
        <Modal open onClose={() => setEdit(false)} title="ویرایش مدل" footer={<>
          <Button variant="danger" onClick={async () => { if (!confirm("مدل حذف شود؟")) return; await del(`/api/settings/ai/models/${m.id}`); refresh(); setEdit(false); }}>حذف</Button>
          <Button variant="primary" onClick={async () => { try { await patch(`/api/settings/ai/models/${m.id}`, { display_name: f.display_name, capabilities: f.capabilities, context_window: Number(f.context_window), price_in: Number(f.price_in), price_out: Number(f.price_out), price_cache_read: Number(f.price_cache_read), price_cache_write: Number(f.price_cache_write) }); refresh(); setEdit(false); } catch (e) { toastError(e); } }}>ذخیره</Button>
        </>}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="نام نمایشی" className="sm:col-span-2"><Input value={f.display_name || ""} onChange={(e) => setF({ ...f, display_name: e.target.value })} /></Field>
            <div className="sm:col-span-2">
              <p className="mb-2 text-xs text-muted">قابلیت‌ها (برای اعتبارسنجی حالت حرفه‌ای)</p>
              <div className="flex flex-wrap gap-3">{CAPS.map(([c, l]) => <label key={c} className="flex items-center gap-2 text-sm"><Switch checked={!!f.capabilities?.[c]} onChange={(v) => setF({ ...f, capabilities: { ...f.capabilities, [c]: v } })} />{l}</label>)}</div>
            </div>
            <Field label="پنجره کانتکست"><Input type="number" value={f.context_window} onChange={(e) => setF({ ...f, context_window: e.target.value })} /></Field>
            <Field label="قیمت ورودی ($/M)"><Input type="number" step="0.01" value={f.price_in} onChange={(e) => setF({ ...f, price_in: e.target.value })} /></Field>
            <Field label="قیمت خروجی ($/M)"><Input type="number" step="0.01" value={f.price_out} onChange={(e) => setF({ ...f, price_out: e.target.value })} /></Field>
            <Field label="قیمت خواندن کش ($/M)"><Input type="number" step="0.001" value={f.price_cache_read} onChange={(e) => setF({ ...f, price_cache_read: e.target.value })} /></Field>
            <Field label="قیمت نوشتن کش ($/M)"><Input type="number" step="0.001" value={f.price_cache_write} onChange={(e) => setF({ ...f, price_cache_write: e.target.value })} /></Field>
          </div>
        </Modal>
      )}
    </div>
  );
}

function ModelSlots({ title, icon, ids, flags, models, onChange, hint }: { title: string; icon: ReactNode; ids: string[]; flags: boolean[]; models: any[]; onChange: (ids: string[], flags: boolean[]) => void; hint: string }) {
  return (
    <Card className="p-4">
      <div className="mb-1 flex items-center gap-2 font-semibold">{icon}{title}</div>
      <p className="mb-3 text-xs leading-5 text-muted">{hint}</p>
      <div className="space-y-2">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex items-center gap-2">
            <span className="num grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-line/10 text-xs">{faInt(i + 1)}</span>
            <Select value={ids[i] || ""} onChange={(e) => { const n = [...ids]; n[i] = e.target.value; const f = [...flags]; if (f[i] === undefined) f[i] = true; onChange(n.filter((x, j) => x || j < n.length), f); }}>
              <option value="">{i === 0 ? "انتخاب مدل اصلی…" : "Fallback (اختیاری)"}</option>
              {models.map((m) => <option key={m.id} value={m.id}>{m.label} — {m.provider}</option>)}
            </Select>
            <Switch checked={ids[i] ? flags[i] !== false : false} disabled={!ids[i]} onChange={(v) => { const f = [...flags]; f[i] = v; onChange(ids, f); }} label="روشن/خاموش" />
          </div>
        ))}
      </div>
    </Card>
  );
}

function AI() {
  const qc = useQueryClient();
  const { v, setV, save, saving } = useSection<any>("ai");
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: () => get("/api/settings/ai/catalog") });
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => get("/api/settings/ai/providers") });
  const available = useQuery({ queryKey: ["models-available"], queryFn: () => get("/api/settings/ai/models/available") });
  const [adding, setAdding] = useState(false);
  const models: any[] = available.data?.items || [];
  if (!v) return <Skeleton className="h-60" />;
  const roles = catalog.data?.roles || {};
  const roleCaps = catalog.data?.role_caps || {};
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="font-semibold">حالت مسیریابی مدل‌ها</div>
            <p className="text-xs text-muted">اسمارت: مدیر برای کارهای پیچیده و کارمند برای کارهای ساده با مسیریاب سبک. حرفه‌ای: تعیین مدل هر بخش به‌صورت دستی.</p>
          </div>
          <Segmented value={v.mode} onChange={(m) => setV({ ...v, mode: m })} options={[{ value: "smart", label: <span className="flex items-center gap-1"><Sparkles className="h-3.5 w-3.5" />اسمارت</span> }, { value: "pro", label: <span className="flex items-center gap-1"><SlidersHorizontal className="h-3.5 w-3.5" />حرفه‌ای</span> }]} />
        </div>
      </Card>

      <Card>
        <CardHeader title="ارائه‌دهندگان و مدل‌ها" icon={<Cloud className="h-4 w-4" />} sub="انتخاب شرکت ← کلید API ← نام مدل ← تست اتصال ← روشن/خاموش"
          actions={<Button variant="primary" size="sm" icon={<Plus className="h-4 w-4" />} onClick={() => setAdding(true)}>افزودن مدل</Button>} />
        <div className="space-y-4 p-4 sm:p-5">
          {providers.isLoading && <Skeleton className="h-24" />}
          {(providers.data?.providers || []).length === 0 && !providers.isLoading && <p className="py-6 text-center text-sm text-muted">هنوز مدلی تعریف نشده است. با دکمه «افزودن مدل» شروع کنید.</p>}
          {(providers.data?.providers || []).map((p: any) => (
            <div key={p.id} className="rounded-2xl border hairline p-3.5">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-primary/12 font-bold text-primary">{KIND_ICON[p.kind] || "•"}</span>
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{p.name}</div>
                  <div className="ltr text-end text-[11px] text-muted">{p.api_key_masked || "بدون کلید"} {p.base_url ? `· ${p.base_url}` : ""}</div>
                </div>
                {p.last_test_ok != null && <Badge tone={p.last_test_ok ? "success" : "danger"}>{p.last_test_ok ? "آخرین تست موفق" : "آخرین تست ناموفق"}</Badge>}
                <Switch checked={p.enabled} onChange={async (x) => { await patch(`/api/settings/ai/providers/${p.id}`, { enabled: x }); qc.invalidateQueries({ queryKey: ["providers"] }); qc.invalidateQueries({ queryKey: ["models-available"] }); }} label="فعال" />
                <Button size="sm" variant="ghost" onClick={async () => { const k = prompt("کلید API جدید:"); if (k) { await patch(`/api/settings/ai/providers/${p.id}`, { api_key: k }); qc.invalidateQueries({ queryKey: ["providers"] }); toast("کلید به‌روزرسانی شد", "success"); } }}><KeyRound className="h-4 w-4" /></Button>
                <Button size="sm" variant="ghost" onClick={async () => { if (!confirm("ارائه‌دهنده و مدل‌هایش حذف شوند؟")) return; await del(`/api/settings/ai/providers/${p.id}`); qc.invalidateQueries({ queryKey: ["providers"] }); }}><Trash2 className="h-4 w-4" /></Button>
              </div>
              <div className="space-y-2">
                {(providers.data?.models || []).filter((m: any) => m.provider_id === p.id).map((m: any) => <ModelRow key={m.id} m={m} />)}
                <Button size="sm" variant="ghost" icon={<Plus className="h-3.5 w-3.5" />} onClick={async () => { const name = prompt("نام مدل:"); if (name) { try { await post("/api/settings/ai/models", { provider_id: p.id, model_name: name }); qc.invalidateQueries({ queryKey: ["providers"] }); qc.invalidateQueries({ queryKey: ["models-available"] }); } catch (e) { toastError(e); } } }}>مدل دیگر از همین ارائه‌دهنده</Button>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {v.mode === "smart" ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <ModelSlots title="هوش مصنوعی مدیر" icon={<Brain className="h-4 w-4 text-accent" />} models={models} ids={v.smart.manager_model_ids} flags={v.smart.manager_enabled}
            hint="تصمیم‌گیری پیچیده، هدف‌سنجی، برنامه‌ریزی، ارزیابی و رفع خطا. تا ۳ مدل به ترتیب اولویت Fallback."
            onChange={(ids, flags) => setV({ ...v, smart: { ...v.smart, manager_model_ids: ids, manager_enabled: flags } })} />
          <ModelSlots title="هوش مصنوعی کارمند" icon={<Cpu className="h-4 w-4 text-cyan" />} models={models} ids={v.smart.worker_model_ids} flags={v.smart.worker_enabled}
            hint="کارهای روتین، استخراج داده و فراخوانی ابزارهای مشخص با کمترین هزینه. مسیریاب سبک کارهای ساده را مستقیم به اینجا می‌فرستد."
            onChange={(ids, flags) => setV({ ...v, smart: { ...v.smart, worker_model_ids: ids, worker_enabled: flags } })} />
        </div>
      ) : (
        <Card>
          <CardHeader title="تخصیص مدل به هر بخش" icon={<SlidersHorizontal className="h-4 w-4" />} sub="فقط مدل‌های دارای قابلیت لازم پذیرفته می‌شوند (بینایی، صوت، فراخوانی ابزار)" />
          <div className="grid gap-3 p-5 sm:grid-cols-2">
            {Object.entries(roles).map(([role, label]: any) => {
              const cap = roleCaps[role];
              const opts = models.filter((m) => !cap || cap === "tools" ? (cap !== "tools" || m.capabilities?.tools !== false) : m.capabilities?.[cap]);
              return (
                <Field key={role} label={<span className="flex items-center gap-1.5">{label}{cap && <Badge tone="cyan">{cap}</Badge>}</span>}>
                  <Select value={v.pro[role] || ""} onChange={(e) => setV({ ...v, pro: { ...v.pro, [role]: e.target.value || null } })}>
                    <option value="">پیش‌فرض (مدل مدیریت)</option>
                    {opts.map((m) => <option key={m.id} value={m.id}>{m.label} — {m.provider}</option>)}
                  </Select>
                </Field>
              );
            })}
          </div>
        </Card>
      )}

      <Card>
        <CardHeader title="حافظه برداری و صدا" icon={<Wand2 className="h-4 w-4" />} />
        <div className="grid gap-4 p-5 sm:grid-cols-2">
          <Field label="مدل Embedding" hint="جاسازی محلی بدون هزینه و بدون اینترنت روی هر سخت‌افزاری کار می‌کند.">
            <Select value={v.embedding_model_id || ""} onChange={(e) => setV({ ...v, embedding_model_id: e.target.value || null })}>
              <option value="">محلی (Hash) — رایگان</option>
              {models.filter((m) => m.capabilities?.embedding).map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </Select>
          </Field>
          <Field label="صدای TTS"><Input value={v.tts_voice} onChange={(e) => setV({ ...v, tts_voice: e.target.value })} className="ltr" /></Field>
        </div>
        <SaveBar saving={saving} onSave={() => save()} />
      </Card>
      {adding && <AddProvider catalog={catalog.data} onClose={() => setAdding(false)} />}
    </div>
  );
}

// ───────────── Harness ─────────────
const MODES = [
  { value: "auto", label: "خودکار", tone: "bg-success" },
  { value: "ask", label: "با اجازه", tone: "bg-primary" },
  { value: "conditional", label: "مشروط", tone: "bg-accent" },
  { value: "forbidden", label: "ممنوع", tone: "bg-danger" },
];
function HarnessRow({ r }: { r: any }) {
  const qc = useQueryClient();
  const [f, setF] = useState({ mode: r.mode, rule_text: r.rule_text || "", allow: (r.conditions?.allow || []).join("، "), deny: (r.conditions?.deny || []).join("، "), ask_actions: (r.conditions?.ask_actions || []).join("، ") });
  const [saving, setSaving] = useState(false);
  const dirty = f.mode !== r.mode || f.rule_text !== (r.rule_text || "") || f.allow !== (r.conditions?.allow || []).join("، ") || f.deny !== (r.conditions?.deny || []).join("، ") || f.ask_actions !== (r.conditions?.ask_actions || []).join("، ");
  const split = (s: string) => s.split(/[،,\n]/).map((x) => x.trim()).filter(Boolean);
  const save = async () => {
    setSaving(true);
    try {
      await put(`/api/settings/harness/${r.category}`, { mode: f.mode, rule_text: f.rule_text, conditions: { allow: split(f.allow), deny: split(f.deny), ask_actions: split(f.ask_actions) } });
      toast("قانون Harness ذخیره شد", "success");
      qc.invalidateQueries({ queryKey: ["harness"] });
    } catch (e) { toastError(e); } finally { setSaving(false); }
  };
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{r.label}</span>
          {r.never_auto && <Badge tone="danger"><Lock className="h-3 w-3" />هرگز خودکار</Badge>}
        </div>
        <Segmented size="sm" value={f.mode} onChange={(m) => setF({ ...f, mode: m })} options={MODES.filter((m) => !(r.never_auto && m.value === "auto")) as any} />
      </div>
      <Field label="باکس توضیحات و قوانین اختصاصی" className="mt-3" hint="این متن هم به مدل گفته می‌شود و هم روی کارت درخواست اجازه نمایش داده می‌شود.">
        <Textarea value={f.rule_text} onChange={(e) => setF({ ...f, rule_text: e.target.value })} className="min-h-[64px]" placeholder={r.category === "login" ? "مثال: در سرویس‌های کاری مشخص‌شده خودکار لاگین کن، اما برای حساب‌های بانکی یا جیمیل اصلی اجازه بگیر تا خودم از دسکتاپ زنده وارد شوم." : "شرط متنی دقیق برای این مجوز (اختیاری)…"} />
      </Field>
      {f.mode === "conditional" && (
        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} className="mt-3 grid gap-3 sm:grid-cols-3">
          <Field label="مجاز خودکار (دامنه/گیرنده/مسیر)" hint="مثال: work.ir، @ali"><Input value={f.allow} onChange={(e) => setF({ ...f, allow: e.target.value })} className="ltr" /></Field>
          <Field label="همیشه اجازه بگیر" hint="مثال: gmail.com، bank*"><Input value={f.deny} onChange={(e) => setF({ ...f, deny: e.target.value })} className="ltr" /></Field>
          <Field label="عملیات نیازمند اجازه" hint="مثال: delete"><Input value={f.ask_actions} onChange={(e) => setF({ ...f, ask_actions: e.target.value })} className="ltr" /></Field>
        </motion.div>
      )}
      {dirty && <div className="mt-3 flex justify-end"><Button size="sm" variant="primary" loading={saving} onClick={save}>ذخیره</Button></div>}
    </Card>
  );
}
function Harness() {
  const { data, isLoading, error } = useQuery({ queryKey: ["harness"], queryFn: () => get("/api/settings/harness") });
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-2 rounded-2xl border border-primary/25 bg-primary/8 px-4 py-3 text-sm"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />این قوانین به‌صورت قطعی در Backend اجرا می‌شوند و مستقل از تصمیم مدل هوش مصنوعی هستند. تغییر هسته سیستم و پرداخت همیشه نیازمند تایید صریح شماست.</div>
      {error && <ErrorBox error={error} />}
      {isLoading && <Skeleton className="h-40" />}
      {(data?.items || []).map((r: any) => <HarnessRow key={r.id} r={r} />)}
    </div>
  );
}

// ───────────── محدودیت‌ها ─────────────
function NumField({ label, value, onChange, step = 1, hint }: { label: string; value: number; onChange: (n: number) => void; step?: number; hint?: string }) {
  return <Field label={label} hint={hint}><Input type="number" step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="num" /></Field>;
}
function Limits() {
  const sl = useSection<any>("stoploss");
  const rs = useSection<any>("routine_safety");
  if (!sl.v || !rs.v) return <Skeleton className="h-60" />;
  const s = (k: string) => (n: number) => sl.setV({ ...sl.v, [k]: n });
  const r = (k: string) => (n: number) => rs.setV({ ...rs.v, [k]: n });
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="حد ضرر قطعی (Stop-Loss)" icon={<OctagonX className="h-4 w-4" />} sub="اعمال سمت سرور؛ در صورت رسیدن به سقف، وضعیت ذخیره و کار متوقف می‌شود تا با اجازه شما ادامه یابد." />
        <div className="grid gap-4 p-5 sm:grid-cols-3">
          <NumField label="سقف هزینه هر تسک ($)" value={sl.v.max_cost_per_task_usd} step={0.05} onChange={s("max_cost_per_task_usd")} />
          <NumField label="سقف توکن هر تسک" value={sl.v.max_tokens_per_task} step={1000} onChange={s("max_tokens_per_task")} />
          <NumField label="سقف زمان اجرا (ثانیه)" value={sl.v.max_runtime_seconds} step={30} onChange={s("max_runtime_seconds")} />
          <NumField label="سقف هزینه روزانه ($)" value={sl.v.daily_cost_usd} step={0.5} onChange={s("daily_cost_usd")} />
          <NumField label="سقف هزینه ماهانه ($)" value={sl.v.monthly_cost_usd} step={1} onChange={s("monthly_cost_usd")} />
          <div />
          <NumField label="سقف گام کار ساده" value={sl.v.max_steps_simple} onChange={s("max_steps_simple")} hint="تعداد اقدام (فراخوانی ابزار)" />
          <NumField label="سقف گام کار پیچیده" value={sl.v.max_steps_complex} onChange={s("max_steps_complex")} />
          <div />
          <NumField label="حداکثر تکرار یک اقدام" value={sl.v.max_repeat_same_action} onChange={s("max_repeat_same_action")} hint="کنترلر حلقه پس از این تعداد، استراتژی را عوض می‌کند" />
          <NumField label="حداکثر خطای پیاپی" value={sl.v.max_consecutive_errors} onChange={s("max_consecutive_errors")} />
        </div>
        <SaveBar saving={sl.saving} onSave={() => sl.save()} />
      </Card>
      <Card>
        <CardHeader title="مدارقطع‌کن روتین‌ها و قفل اتمیک" icon={<Shield className="h-4 w-4" />} />
        <div className="grid gap-4 p-5 sm:grid-cols-3">
          <NumField label="آستانه خطای پیاپی" value={rs.v.circuit_failure_threshold} onChange={r("circuit_failure_threshold")} />
          <NumField label="دوره خنک‌شدن (دقیقه)" value={rs.v.circuit_cooldown_minutes} onChange={r("circuit_cooldown_minutes")} />
          <NumField label="TTL قفل اتمیک (ثانیه)" value={rs.v.lease_ttl_seconds} onChange={r("lease_ttl_seconds")} />
        </div>
        <SaveBar saving={rs.saving} onSave={() => rs.save()} />
      </Card>
    </div>
  );
}

function DreamSettings() {
  const { v, setV, save, saving } = useSection<any>("dream");
  if (!v) return <Skeleton className="h-40" />;
  const set = (k: string) => (n: any) => setV({ ...v, [k]: n });
  return (
    <Card>
      <CardHeader title="موتور رویا (شبانه)" icon={<Moon className="h-4 w-4" />} actions={<Switch checked={v.enabled} onChange={set("enabled")} label="فعال" />} />
      <div className="grid gap-4 p-5 sm:grid-cols-3">
        <NumField label="ساعت اجرا (۰-۲۳)" value={v.hour} onChange={set("hour")} />
        <NumField label="دقیقه" value={v.minute} onChange={set("minute")} />
        <NumField label="بودجه مستقل ($)" value={v.max_cost_usd} step={0.05} onChange={set("max_cost_usd")} />
        <NumField label="حداقل رویداد برای مصرف LLM" value={v.min_events_for_llm} onChange={set("min_events_for_llm")} />
        <NumField label="تعداد اجرای دوره آزمایشی" value={v.probation_runs} onChange={set("probation_runs")} />
        <NumField label="آستانه پسرفت توکن (ضریب)" value={v.regression_token_ratio} step={0.05} onChange={set("regression_token_ratio")} />
      </div>
      <SaveBar saving={saving} onSave={() => save()} />
    </Card>
  );
}

// ───────────── تلگرام ─────────────
function Telegram() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["telegram"], queryFn: () => get("/api/telegram/status"), refetchInterval: 20000 });
  const sec = useSection<any>("telegram");
  useLive((_d, t) => t === "telegram.status" && qc.invalidateQueries({ queryKey: ["telegram"] }));
  const [f, setF] = useState({ api_id: "", api_hash: "", phone: "", code: "", password: "" });
  const [stage, setStage] = useState<"form" | "code" | "password">("form");
  const [busy, setBusy] = useState(false);
  const live = data?.live || {};
  const start = async () => {
    setBusy(true);
    try { const r = await post("/api/telegram/login/start", { api_id: Number(f.api_id), api_hash: f.api_hash, phone: f.phone }); toast(r.message, "success"); setStage("code"); } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  const verify = async () => {
    setBusy(true);
    try {
      const r = await post("/api/telegram/login/verify", stage === "password" ? { password: f.password } : { code: f.code, password: f.password || null });
      if (r.need_password) { setStage("password"); toast(r.message, "warning"); }
      else { toast(r.message, "success"); setStage("form"); qc.invalidateQueries({ queryKey: ["telegram"] }); qc.invalidateQueries({ queryKey: ["settings"] }); }
    } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  if (isLoading || !sec.v) return <Skeleton className="h-40" />;
  return (
    <div className="space-y-4">
      <Card className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="grid h-11 w-11 place-items-center rounded-2xl bg-[#229ED9]/15 text-[#229ED9]"><MessageCircle className="h-5 w-5" /></span>
            <div>
              <div className="font-semibold">تلگرام شخصی (MTProto / Telethon)</div>
              <div className="text-xs text-muted">{live.connected ? `متصل به ${live.me?.name || ""} ${live.me?.username ? "@" + live.me.username : ""}` : live.error || "متصل نیست"}</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={live.connected ? "success" : "neutral"} dot>{live.connected ? "آنلاین" : "آفلاین"}</Badge>
            {data?.settings?.api_hash_set && <Button size="sm" variant="danger" icon={<Power className="h-3.5 w-3.5" />} onClick={async () => { if (!confirm("نشست تلگرام حذف شود؟")) return; await post("/api/telegram/logout"); qc.invalidateQueries({ queryKey: ["telegram"] }); }}>قطع اتصال</Button>}
          </div>
        </div>
        {!data?.settings?.api_hash_set && (
          <div className="mt-5 grid gap-3 sm:grid-cols-3">
            {stage === "form" && (<>
              <Field label="API ID" hint="از my.telegram.org"><Input value={f.api_id} onChange={(e) => setF({ ...f, api_id: e.target.value })} className="ltr" /></Field>
              <Field label="API Hash"><Input type="password" value={f.api_hash} onChange={(e) => setF({ ...f, api_hash: e.target.value })} className="ltr" /></Field>
              <Field label="شماره تلفن"><Input value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} className="ltr" placeholder="+98912..." /></Field>
              <div className="sm:col-span-3"><Button variant="primary" loading={busy} onClick={start} disabled={!f.api_id || !f.api_hash || !f.phone}>ارسال کد تایید</Button></div>
            </>)}
            {stage === "code" && (<>
              <Field label="کد ارسال‌شده در تلگرام"><Input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} className="ltr" autoFocus /></Field>
              <Field label="رمز دومرحله‌ای (در صورت وجود)"><Input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} className="ltr" /></Field>
              <div className="flex items-end"><Button variant="primary" loading={busy} onClick={verify} disabled={!f.code}>ورود</Button></div>
            </>)}
            {stage === "password" && (<>
              <Field label="رمز دومرحله‌ای"><Input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} className="ltr" autoFocus /></Field>
              <div className="flex items-end"><Button variant="primary" loading={busy} onClick={verify}>تایید</Button></div>
            </>)}
          </div>
        )}
        <p className="mt-4 text-xs leading-6 text-muted">نشست به‌صورت رمزنگاری‌شده ذخیره می‌شود. برای فرمان دادن به ایجنت از «Saved Messages» خودتان در تلگرام پیام بدهید؛ پاسخ‌ها با 🤖 مشخص می‌شوند. کارت‌های اجازه را با «تایید xxxx» یا «رد xxxx» پاسخ دهید.</p>
      </Card>
      <Card>
        <CardHeader title="قوانین تلگرام" icon={<Shield className="h-4 w-4" />} />
        <div className="grid gap-4 p-5 sm:grid-cols-2">
          <Field label="گفتگوهای مجاز برای ارسال (Allowlist)" hint="خالی = همه؛ یوزرنیم یا شناسه، با ویرگول جدا کنید.">
            <Input value={(sec.v.allowlist || []).join("، ")} onChange={(e) => sec.setV({ ...sec.v, allowlist: e.target.value.split(/[،,]/).map((x) => x.trim()).filter(Boolean) })} className="ltr" />
          </Field>
          <Field label="گفتگوی فرمان کارفرما"><Input value={sec.v.owner_chat || "me"} onChange={(e) => sec.setV({ ...sec.v, owner_chat: e.target.value })} className="ltr" /></Field>
          <label className="flex items-center justify-between rounded-xl bg-line/5 px-3 py-2.5 text-sm">اتصال خودکار پیام‌ها به CRM<Switch checked={sec.v.auto_link_crm} onChange={(x) => sec.setV({ ...sec.v, auto_link_crm: x })} /></label>
        </div>
        <SaveBar saving={sec.saving} onSave={() => sec.save()} />
      </Card>
    </div>
  );
}

// ───────────── امنیت ─────────────
function Security() {
  const [f, setF] = useState({ current_password: "", new_password: "", repeat: "" });
  const [busy, setBusy] = useState(false);
  const change = async () => {
    if (f.new_password !== f.repeat) return toast("تکرار رمز یکسان نیست", "error");
    setBusy(true);
    try { await post("/api/auth/password", { current_password: f.current_password, new_password: f.new_password }); toast("رمز عبور تغییر کرد؛ سایر نشست‌ها خارج شدند", "success"); setF({ current_password: "", new_password: "", repeat: "" }); } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader title="تغییر رمز عبور" icon={<Lock className="h-4 w-4" />} />
        <div className="space-y-3 p-5">
          <Field label="رمز فعلی"><Input type="password" value={f.current_password} onChange={(e) => setF({ ...f, current_password: e.target.value })} /></Field>
          <Field label="رمز جدید (حداقل ۱۰ کاراکتر)"><Input type="password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} /></Field>
          <Field label="تکرار رمز جدید"><Input type="password" value={f.repeat} onChange={(e) => setF({ ...f, repeat: e.target.value })} /></Field>
          <Button variant="primary" loading={busy} onClick={change} disabled={f.new_password.length < 10}>تغییر رمز</Button>
        </div>
      </Card>
      <Card>
        <CardHeader title="نشست‌ها و امنیت" icon={<ShieldCheck className="h-4 w-4" />} />
        <div className="space-y-3 p-5 text-sm leading-7 text-muted">
          <p>• رمزها با Argon2id هش و کلیدهای API با Fernet رمزنگاری می‌شوند.</p>
          <p>• دسکتاپ ایجنت فقط از پشت احراز هویت پنل در دسترس است.</p>
          <p>• لاگ حسابرسی و حافظه رویدادی در سطح دیتابیس غیرقابل حذف است.</p>
          <Button variant="danger" onClick={async () => { if (!confirm("از همه دستگاه‌ها خارج شوید؟")) return; await post("/api/auth/logout-all"); location.reload(); }}>خروج از همه دستگاه‌ها</Button>
        </div>
      </Card>
    </div>
  );
}

// ───────────── سیستم ─────────────
function System() {
  const { data, refetch } = useQuery({ queryKey: ["update"], queryFn: () => get("/api/settings/system/update"), refetchInterval: 5000 });
  const [busy, setBusy] = useState(false);
  const st = data?.status || {};
  const run = async () => {
    if (!confirm("هسته سیستم از GitHub به‌روزرسانی و سرویس‌ها بازسازی شوند؟ (چند دقیقه طول می‌کشد)")) return;
    setBusy(true);
    try { const r = await post("/api/settings/system/update"); toast(r.message, "success"); refetch(); } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  const LABEL: Record<string, string> = { idle: "آماده", requested: "در صف", running: "در حال به‌روزرسانی", done: "انجام شد", failed: "ناموفق" };
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="به‌روزرسانی هسته سیستم از GitHub" icon={<Download className="h-4 w-4" />} sub={`نسخه فعلی: ${data?.version?.version || "—"} ${data?.version?.commit ? `(${String(data.version.commit).slice(0, 8)})` : ""}`}
          actions={<Button variant="primary" loading={busy || st.state === "running" || st.state === "requested"} onClick={run} icon={<RefreshCw className="h-4 w-4" />}>به‌روزرسانی</Button>} />
        <div className="space-y-3 p-5">
          <div className="flex flex-wrap items-center gap-2 text-sm">وضعیت: <Badge tone={st.state === "failed" ? "danger" : st.state === "done" ? "success" : st.state === "running" ? "primary" : "neutral"} dot>{LABEL[st.state] || st.state}</Badge>{st.at && <span className="text-xs text-muted">{fmtDate(st.at)}</span>}</div>
          {data?.log && <pre className="ltr max-h-72 overflow-auto rounded-xl bg-bg/70 p-3 text-[11px] leading-5 text-muted">{data.log}</pre>}
          <p className="text-xs text-muted">به‌روزرسانی توسط سرویس امن میزبان (systemd) اجرا می‌شود؛ پیش از آن از دیتابیس نسخه پشتیبان گرفته می‌شود و در صورت شکست، نسخه قبلی حفظ می‌شود.</p>
        </div>
      </Card>
    </div>
  );
}

export default function Settings() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey) || "general";
  const { error } = useSettings();
  const tabs = useMemo(() => [
    { value: "general" as const, label: "پایه و زمان", icon: <User className="h-4 w-4" /> },
    { value: "ai" as const, label: "هوش مصنوعی", icon: <Sparkles className="h-4 w-4" /> },
    { value: "harness" as const, label: "مجوزها (Harness)", icon: <ShieldCheck className="h-4 w-4" /> },
    { value: "limits" as const, label: "حلقه و Stop-Loss", icon: <CircleSlash className="h-4 w-4" /> },
    { value: "dream" as const, label: "رویا", icon: <Moon className="h-4 w-4" /> },
    { value: "telegram" as const, label: "تلگرام", icon: <MessageCircle className="h-4 w-4" /> },
    { value: "security" as const, label: "امنیت", icon: <Lock className="h-4 w-4" /> },
    { value: "system" as const, label: "سیستم", icon: <Cpu className="h-4 w-4" /> },
  ], []);
  return (
    <div>
      <PageHeader title="تنظیمات" sub="همه تنظیمات بلافاصله در سرور اعمال و در لاگ حسابرسی ثبت می‌شوند" icon={<SlidersHorizontal className="h-5 w-5" />} />
      {error && <ErrorBox error={error} />}
      <Tabs value={tab} onChange={(t) => setParams({ tab: t })} tabs={tabs} />
      <div className="mt-4">
        {tab === "general" && <General />}
        {tab === "ai" && <AI />}
        {tab === "harness" && <Harness />}
        {tab === "limits" && <Limits />}
        {tab === "dream" && <DreamSettings />}
        {tab === "telegram" && <Telegram />}
        {tab === "security" && <Security />}
        {tab === "system" && <System />}
      </div>
    </div>
  );
}
