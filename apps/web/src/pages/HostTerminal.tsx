/**
 * ترمینال مدیریتی VPS: عملیات ساختاریافته (نه Shell آزاد) با سطح ۱ خودکار، ۲ خودکار مشروط و ۳ با تأیید کارفرما.
 * خروجی زنده، کد خروج، وضعیت، تاریخچه کامل با درخواست‌کننده، کارت‌های اجازه مدیر و سیاست کارفرما.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Check, OctagonX, Play, Power, RefreshCw, Shield, Square, Terminal, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Card, CardHeader, cn, Empty, ErrorBox, Field, Input, Modal, Select, Skeleton, Switch, type Tone } from "../components/ui";
import { get, post, put } from "../lib/api";
import { faInt, relTime } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const LEVEL: Record<number, [string, Tone]> = { 1: ["سطح ۱ · خودکار", "success"], 2: ["سطح ۲ · خودکار مشروط", "warning"], 3: ["سطح ۳ · تأیید کارفرما", "danger"] };
const STATUS: Record<string, [string, Tone]> = {
  queued: ["در صف", "neutral"], running: ["در حال اجرا", "primary"], succeeded: ["موفق", "success"], failed: ["ناموفق", "danger"],
  timeout: ["پایان زمان", "danger"], cancelled: ["لغو شد", "neutral"], rejected: ["رد شد", "danger"], lost: ["گم‌شده", "warning"],
};
const ACTOR: Record<string, string> = { owner: "کارفرما", manager: "مدیر", policy: "سیاست خودکار" };

function ArgInput({ name, spec, value, onChange }: { name: string; spec: any; value: any; onChange: (v: any) => void }) {
  if (spec?.type === "enum") {
    return (
      <Select value={value ?? spec.default ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">انتخاب {name}…</option>
        {spec.choices.map((c: string) => <option key={c} value={c}>{c}</option>)}
      </Select>
    );
  }
  if (spec?.type === "int") {
    return <Input type="number" min={spec.min} max={spec.max} value={value ?? spec.default ?? ""} onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} className="ltr" />;
  }
  if (spec?.type === "list") {
    return <Input value={Array.isArray(value) ? value.join(", ") : value ?? ""} placeholder={spec.placeholder}
      onChange={(e) => onChange(e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} className="ltr" />;
  }
  return <Input value={value ?? ""} placeholder={spec?.placeholder} onChange={(e) => onChange(e.target.value || undefined)} className="ltr" />;
}

function Console({ opId, onDone }: { opId: string | null; onDone: () => void }) {
  const [op, setOp] = useState<any>(null);
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (!opId) return;
    let alive = true;
    offset.current = 0;
    setText("");
    setOp(null);
    const tick = async () => {
      try {
        const d = await get(`/api/host/ops/${opId}?offset=${offset.current}`);
        if (!alive) return;
        setOp(d);
        if (d.chunk) { setText((t) => (t + d.chunk).slice(-200000)); offset.current = d.next_offset; }
        else if (!offset.current && d.output && d.status !== "running") setText(d.output);
        if (d.status === "running" || d.status === "queued") setTimeout(tick, 900);
        else onDone();
      } catch (e) { if (alive) setTimeout(tick, 2500); }
    };
    tick();
    return () => { alive = false; };
  }, [opId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }); }, [text]);
  if (!opId) return <Empty icon={<Terminal className="h-6 w-6" />} title="یک عملیات اجرا یا از تاریخچه انتخاب کنید" text="خروجی، کد خروج و وضعیت اجرا اینجا زنده نمایش داده می‌شود." />;
  const [sl, st] = STATUS[op?.status] || ["…", "neutral"];
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="ltr mono font-semibold">{op?.op}</span>
        <Badge tone={st} dot>{sl}</Badge>
        {op?.exit_code != null && <Badge tone={op.exit_code === 0 ? "success" : "danger"}>exit {op.exit_code}</Badge>}
        {op?.duration_ms > 0 && <span className="num text-muted">{(op.duration_ms / 1000).toFixed(1)} ثانیه</span>}
        {op?.approved_by && <span className="text-muted">مجوز: {op.approved_by}</span>}
        <span className="text-muted">درخواست: {ACTOR[op?.actor_kind] || op?.actor_kind} ({op?.requested_by})</span>
        {op?.status === "running" && (
          <Button size="sm" variant="danger" className="ms-auto" icon={<Square className="h-3.5 w-3.5" />}
            onClick={async () => { try { await post(`/api/host/ops/${opId}/cancel`); toast("درخواست توقف ارسال شد", "warning"); } catch (e) { toastError(e); } }}>توقف</Button>
        )}
      </div>
      {op?.error && <p role="alert" className="bevel border border-danger/30 bg-danger/8 px-3 py-2 text-xs text-danger">{op.error}</p>}
      <pre ref={box} dir="ltr" className="die die-sm die-sunk mono max-h-[420px] min-h-[220px] overflow-auto whitespace-pre-wrap break-words p-3 text-[12px] leading-5 text-ink/90">
        {text || (op?.status === "running" ? "در انتظار خروجی…" : "(بدون خروجی)")}
        {op?.truncated && "\n[خروجی کوتاه شده است]"}
      </pre>
    </div>
  );
}

function Policy({ policy, ops, onSaved }: { policy: any; ops: any[]; onSaved: () => void }) {
  const [p, setP] = useState<any>(policy);
  const [saving, setSaving] = useState(false);
  useEffect(() => setP(policy), [policy]);
  const save = async () => {
    setSaving(true);
    try { await put("/api/settings/host", p); toast("سیاست ترمینال میزبان ذخیره شد", "success"); onSaved(); } catch (e) { toastError(e); } finally { setSaving(false); }
  };
  return (
    <Modal open wide onClose={onSaved} title="سیاست کارفرما برای ترمینال میزبان"
      footer={<><Button variant="ghost" onClick={onSaved}>بستن</Button><Button variant="primary" loading={saving} onClick={save}>ذخیره سیاست</Button></>}>
      <div className="space-y-4">
        <label className="flex items-center gap-2 text-sm"><Switch checked={p.enabled} onChange={(v) => setP({ ...p, enabled: v })} /> کارگزار فرمان میزبان فعال باشد</label>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="سقف Restart خودکار در ساعت"><Input type="number" min={0} max={20} value={p.restart_per_hour} onChange={(e) => setP({ ...p, restart_per_hour: Number(e.target.value) })} /></Field>
          <Field label="سقف پشتیبان‌گیری خودکار در ساعت"><Input type="number" min={0} max={10} value={p.backup_per_hour} onChange={(e) => setP({ ...p, backup_per_hour: Number(e.target.value) })} /></Field>
          <Field label="اعتبار مجوز فرمان خاص (دقیقه)"><Input type="number" min={1} max={60} value={p.exec_session_minutes} onChange={(e) => setP({ ...p, exec_session_minutes: Number(e.target.value) })} /></Field>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="آپدیت هسته" hint="پیش‌فرض: همیشه با تأیید شما. «خودکار» فقط از شاخه تنظیم‌شده و در بازه زمانی مشخص اجرا می‌شود.">
            <Select value={p.update_policy} onChange={(e) => setP({ ...p, update_policy: e.target.value })}>
              <option value="manual">فقط با تأیید کارفرما</option><option value="auto">خودکار در بازه زمانی</option>
            </Select>
          </Field>
          <Field label="شروع بازه (ساعت)"><Input type="number" min={0} max={23} value={p.auto_window_start_hour} onChange={(e) => setP({ ...p, auto_window_start_hour: Number(e.target.value) })} /></Field>
          <Field label="پایان بازه (ساعت)"><Input type="number" min={0} max={23} value={p.auto_window_end_hour} onChange={(e) => setP({ ...p, auto_window_end_hour: Number(e.target.value) })} /></Field>
        </div>
        <div>
          <p className="mb-2 text-xs font-medium text-muted">حالت هر عملیات (پیش‌فرض = طبق سطح؛ سطح ۳ هرگز خودکار نمی‌شود)</p>
          <div className="grid max-h-72 gap-1.5 overflow-y-auto sm:grid-cols-2">
            {ops.map((o) => (
              <div key={o.op} className="flex items-center justify-between gap-2 rounded-[4px] px-2 py-1 hover:bg-line/6">
                <span className="min-w-0"><span className="ltr mono block truncate text-start text-xs">{o.op}</span><span className="block truncate text-[11px] text-muted">{o.description}</span></span>
                <select className="bevel h-8 shrink-0 border hairline bg-surface2 px-2 text-xs" value={p.op_modes?.[o.op] || "default"}
                  onChange={(e) => { const m = { ...(p.op_modes || {}) }; if (e.target.value === "default") delete m[o.op]; else m[o.op] = e.target.value; setP({ ...p, op_modes: m }); }}>
                  <option value="default">پیش‌فرض</option>
                  {o.level < 3 && <option value="auto">خودکار</option>}
                  <option value="ask">با تأیید</option>
                  <option value="forbidden">ممنوع</option>
                </select>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  );
}

export default function HostTerminal() {
  const qc = useQueryClient();
  const cat = useQuery({ queryKey: ["host-catalog"], queryFn: () => get("/api/host/catalog") });
  const ops = useQuery({ queryKey: ["host-ops"], queryFn: () => get("/api/host/ops"), refetchInterval: 5000 });
  const [sel, setSel] = useState<string>("system.status");
  const [args, setArgs] = useState<Record<string, any>>({});
  const [reason, setReason] = useState("");
  const [current, setCurrent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm3, setConfirm3] = useState(false);
  const [policyOpen, setPolicyOpen] = useState(false);
  useLive((_d, t) => { if (t === "host.op" || t === "approval.created" || t === "approval.decided") qc.invalidateQueries({ queryKey: ["host-ops"] }); });
  const catalog: any[] = cat.data?.ops || [];
  const meta = useMemo(() => catalog.find((o) => o.op === sel), [catalog, sel]);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["host-ops"] }); qc.invalidateQueries({ queryKey: ["host-catalog"] }); };

  const run = async (confirmed = false) => {
    if (!meta) return;
    if (meta.level >= 3 && !confirmed) { setConfirm3(true); return; }
    setBusy(true);
    try {
      const d = await post("/api/host/ops", { op: sel, args, reason: reason || "اجرا از پنل کارفرما", confirm: confirmed });
      setCurrent(d.id);
      if (d.status === "rejected" || d.status === "failed") toast(d.error || "اجرا نشد", "error", 8000);
      refresh();
    } catch (e) { toastError(e); } finally { setBusy(false); setConfirm3(false); }
  };

  if (cat.error) return <ErrorBox error={cat.error} retry={cat.refetch} />;
  const grouped = [1, 2, 3].map((lv) => ({ lv, items: catalog.filter((o) => o.level === lv) }));
  const policy = cat.data?.policy;
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="die die-sm grid h-10 w-10 shrink-0 place-items-center text-primary"><Terminal className="h-5 w-5" /></span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2 font-semibold">
                ترمینال مدیریتی VPS
                {cat.isLoading ? <Skeleton className="h-5 w-16" /> : cat.data?.available
                  ? <Badge tone={policy?.enabled ? "success" : "warning"} dot>{policy?.enabled ? "متصل و فعال" : "غیرفعال توسط شما"}</Badge>
                  : <Badge tone="danger" dot>سرویس میزبان در دسترس نیست</Badge>}
              </div>
              <p className="text-xs leading-5 text-muted">{cat.data?.available ? "فقط عملیات ساختاریافته از فهرست مجاز؛ بدون Shell آزاد، بدون پورت شبکه، با ثبت کامل." : cat.data?.error}</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" icon={<Shield className="h-4 w-4" />} onClick={() => setPolicyOpen(true)} disabled={!policy}>سیاست و مجوزها</Button>
            {policy?.enabled === false
              ? <Button size="sm" variant="soft" icon={<Power className="h-4 w-4" />} onClick={async () => { try { await put("/api/settings/host", { ...policy, enabled: true }); refresh(); } catch (e) { toastError(e); } }}>فعال‌سازی دوباره</Button>
              : <Button size="sm" variant="danger" icon={<OctagonX className="h-4 w-4" />} onClick={async () => {
                  if (!confirm("همه عملیات میزبان متوقف و کارگزار تا فعال‌سازی دوباره غیرفعال شود؟")) return;
                  try { const r = await post("/api/host/stop"); toast(`توقف اضطراری: ${faInt(r.cancelled)} عملیات لغو شد`, "warning"); refresh(); } catch (e) { toastError(e); }
                }}>توقف اضطراری</Button>}
          </div>
        </div>
      </Card>

      {(ops.data?.pending_approvals || []).length > 0 && (
        <Card className="die-live p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><Shield className="h-4 w-4 text-warning" /> درخواست‌های مدیر منتظر تأیید شما</div>
          <div className="space-y-2">
            {ops.data.pending_approvals.map((a: any) => (
              <div key={a.id} className="die die-sm die-sunk flex flex-wrap items-center gap-2 p-3">
                <span className="ltr mono text-sm font-semibold">{a.op}</span>
                <span className="ltr mono text-[11px] text-muted">{JSON.stringify(a.args || {})}</span>
                <span className="min-w-0 flex-1 text-xs text-muted">{a.reason}</span>
                <Button size="sm" variant="primary" icon={<Check className="h-3.5 w-3.5" />} onClick={async () => { try { await post(`/api/approvals/${a.id}`, { decision: "approve" }); toast("تأیید شد؛ مدیر اجرا را ادامه می‌دهد", "success"); refresh(); } catch (e) { toastError(e); } }}>تأیید و اجرا</Button>
                <Button size="sm" variant="ghost" icon={<X className="h-3.5 w-3.5" />} onClick={async () => { try { await post(`/api/approvals/${a.id}`, { decision: "deny" }); refresh(); } catch (e) { toastError(e); } }}>رد</Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <Card>
          <CardHeader title="اجرای عملیات" icon={<Play className="h-4 w-4" />} sub="مدیر هم همین عملیات را با ابزار host_run اجرا می‌کند" />
          <div className="space-y-3 p-4">
            {cat.isLoading && <Skeleton className="h-40" />}
            {grouped.map(({ lv, items }) => items.length > 0 && (
              <div key={lv}>
                <div className="mb-1.5"><Badge tone={LEVEL[lv][1]}>{LEVEL[lv][0]}</Badge></div>
                <div className="flex flex-wrap gap-1.5">
                  {items.map((o) => (
                    <button key={o.op} type="button" title={o.description} onClick={() => { setSel(o.op); setArgs({}); }}
                      aria-pressed={sel === o.op}
                      className={cn("bevel ltr mono h-8 px-2.5 text-[11px] ring-1 ring-inset transition-colors",
                        sel === o.op ? "bg-primary/14 text-primary ring-primary/40" : "text-muted ring-line/20 hover:text-ink",
                        policy?.op_modes?.[o.op] === "forbidden" && "line-through opacity-50")}>{o.op}</button>
                  ))}
                </div>
              </div>
            ))}
            {meta && (
              <div className="die die-sm die-sunk space-y-3 p-3">
                <div className="flex flex-wrap items-center gap-2"><span className="ltr mono font-semibold">{meta.op}</span><Badge tone={LEVEL[meta.level][1]}>{LEVEL[meta.level][0]}</Badge></div>
                <p className="text-xs text-muted">{meta.description}</p>
                {Object.keys(meta.form || {}).length > 0 && (
                  <div className="grid gap-2 sm:grid-cols-2">
                    {Object.entries(meta.form).map(([k, spec]: any) => (
                      <Field key={k} label={k}><ArgInput name={k} spec={spec} value={args[k]} onChange={(v) => setArgs({ ...args, [k]: v })} /></Field>
                    ))}
                  </div>
                )}
                <Field label="دلیل (در حسابرسی ثبت می‌شود)"><Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="مثلاً بررسی کندی پاسخ" /></Field>
                <Button variant={meta.level >= 3 ? "danger" : "primary"} loading={busy} disabled={!cat.data?.available || !policy?.enabled}
                  icon={meta.level >= 3 ? <Shield className="h-4 w-4" /> : <Play className="h-4 w-4" />} onClick={() => run(false)}>
                  {meta.level >= 3 ? "اجرا با تأیید صریح" : "اجرا"}
                </Button>
              </div>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="خروجی ترمینال" icon={<Terminal className="h-4 w-4" />} actions={<Button size="icon" variant="ghost" aria-label="به‌روزرسانی" onClick={refresh}><RefreshCw className="h-4 w-4" /></Button>} />
          <div className="p-4"><Console opId={current} onDone={refresh} /></div>
        </Card>
      </div>

      <Card>
        <CardHeader title="تاریخچه کامل عملیات میزبان" icon={<Ban className="h-4 w-4" />} sub="درخواست‌کننده، زمان، مجوز، کد خروج و نتیجه" />
        <div className="overflow-x-auto p-2 sm:p-4">
          {ops.isLoading && <Skeleton className="h-24" />}
          {!ops.isLoading && !(ops.data?.items || []).length && <Empty icon={<Terminal className="h-6 w-6" />} title="هنوز عملیاتی اجرا نشده است" />}
          {(ops.data?.items || []).length > 0 && (
            <table className="w-full min-w-[640px] text-sm">
              <thead><tr className="border-b hairline text-[11px] text-muted">
                <th className="py-2 text-start font-medium">عملیات</th><th className="text-start font-medium">سطح</th><th className="text-start font-medium">درخواست‌کننده</th>
                <th className="text-start font-medium">وضعیت</th><th className="text-start font-medium">exit</th><th className="text-start font-medium">زمان</th>
              </tr></thead>
              <tbody>
                {ops.data.items.map((o: any) => {
                  const [sl, st] = STATUS[o.status] || [o.status, "neutral"];
                  return (
                    <tr key={o.id} onClick={() => setCurrent(o.id)} className={cn("cursor-pointer border-b hairline hover:bg-line/6", current === o.id && "bg-primary/6")}>
                      <td className="py-2"><span className="ltr mono text-xs">{o.op}</span>{o.reason && <div className="max-w-[260px] truncate text-[11px] text-muted">{o.reason}</div>}</td>
                      <td><Badge tone={LEVEL[o.level]?.[1] || "neutral"}>{faInt(o.level)}</Badge></td>
                      <td className="text-xs">{ACTOR[o.actor_kind] || o.actor_kind}<div className="text-[11px] text-muted">{o.requested_by}{o.approved_by ? ` · مجوز: ${o.approved_by}` : ""}</div></td>
                      <td><Badge tone={st} dot>{sl}</Badge></td>
                      <td className="num text-xs">{o.exit_code ?? "—"}</td>
                      <td className="num text-[11px] text-muted">{relTime(o.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </Card>

      {confirm3 && meta && (
        <Modal open onClose={() => setConfirm3(false)} title="تأیید صریح عملیات سطح ۳"
          footer={<><Button variant="ghost" onClick={() => setConfirm3(false)}>انصراف</Button><Button variant="danger" loading={busy} onClick={() => run(true)}>تأیید و اجرا</Button></>}>
          <p className="text-sm leading-7">عملیات <span className="ltr mono font-semibold">{meta.op}</span> روی سرور اصلی اجرا می‌شود: {meta.description}.</p>
          {Object.keys(args).length > 0 && <pre dir="ltr" className="mono mt-2 rounded-[4px] bg-bg2 p-2 text-xs">{JSON.stringify(args, null, 1)}</pre>}
          <p className="mt-2 text-xs text-muted">این تأیید در تاریخچه با نام شما ثبت می‌شود.</p>
        </Modal>
      )}
      {policyOpen && policy && <Policy policy={policy} ops={catalog} onSaved={() => { setPolicyOpen(false); refresh(); }} />}
    </div>
  );
}
