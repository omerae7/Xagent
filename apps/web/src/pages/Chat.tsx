import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Archive, Bot, Cpu, MessageSquarePlus, PanelRightOpen, Pin, Play, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApprovalCard, Composer, MessageBubble, StepList, VoiceMode } from "../components/chat";
import { Badge, Button, cn, Drawer, Empty, ErrorBox, Input, Select, Skeleton } from "../components/ui";
import { Orb, Thinking } from "../components/viz/Orb";
import { del, get, patch, post } from "../lib/api";
import { compact, relTime, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const ACTIVE = ["queued", "running", "waiting_approval"];

function ConversationList({ activeId, onPick }: { activeId?: string; onPick: () => void }) {
  const [q, setQ] = useState("");
  const nav = useNavigate();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["conversations", q], queryFn: () => get(`/api/conversations?q=${encodeURIComponent(q)}`) });
  useLive((_d, t) => t === "message.created" && qc.invalidateQueries({ queryKey: ["conversations"] }));
  const items: any[] = data?.items || [];
  const CH: Record<string, string> = { telegram: "تلگرام", routine: "روتین", voice: "صوتی" };
  return (
    <div className="flex h-full flex-col">
      <div className="space-y-2 p-3">
        <Button variant="primary" className="w-full" icon={<MessageSquarePlus className="h-4 w-4" />} onClick={() => { nav("/chat"); onPick(); }}>گفتگوی جدید</Button>
        <div className="relative">
          <Search className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="جست‌وجو در گفتگوها…" className="ps-9" />
        </div>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-3">
        {isLoading && [1, 2, 3, 4].map((i) => <Skeleton key={i} className="mx-1 h-12" />)}
        {!isLoading && items.length === 0 && <p className="px-3 py-8 text-center text-xs text-muted">گفتگویی یافت نشد</p>}
        {items.map((c) => (
          <button key={c.id} onClick={() => { nav(`/chat/${c.id}`); onPick(); }}
            className={cn("group relative w-full rounded-xl px-3 py-2.5 text-start transition", activeId === c.id ? "bg-primary/12 ring-1 ring-primary/25" : "hover:bg-line/8")}>
            <div className="flex items-center gap-2">
              {c.pinned && <Pin className="h-3 w-3 shrink-0 text-primary" />}
              <span className="truncate text-sm">{c.title}</span>
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
              <span>{relTime(c.updated_at)}</span>
              {CH[c.channel] && <Badge tone="accent" className="!py-0">{CH[c.channel]}</Badge>}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function Chat() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [listOpen, setListOpen] = useState(false);
  const [voice, setVoice] = useState(params.get("voice") === "1");
  const [model, setModel] = useState("auto");
  const [liveSteps, setLiveSteps] = useState<Record<string, any[]>>({});
  const [liveApprovals, setLiveApprovals] = useState<any[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const waiters = useRef<Map<string, (t: string) => void>>(new Map());
  const autoSent = useRef(false);

  const models = useQuery({ queryKey: ["models-available"], queryFn: () => get("/api/settings/ai/models/available") });
  const thread = useQuery({ queryKey: ["messages", id], queryFn: () => get(`/api/conversations/${id}/messages`), enabled: !!id });
  const data = thread.data;
  const activeRun = data?.active_run;
  const runs = data?.runs || {};

  useEffect(() => {
    if (data?.conversation?.model_selection) setModel(data.conversation.model_selection);
  }, [data?.conversation?.id, data?.conversation?.model_selection]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [data?.items?.length, liveSteps, activeRun?.status]);

  useEffect(() => setLiveApprovals([]), [id]);

  // پس از رفرش صفحه، مراحل اجرای فعال از سرور بارگذاری می‌شود
  useEffect(() => {
    const rid = activeRun?.id;
    if (!rid || liveSteps[rid]) return;
    get(`/api/runs/${rid}`).then((r) => setLiveSteps((s) => (s[rid] ? s : { ...s, [rid]: r.steps }))).catch(() => undefined);
  }, [activeRun?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useLive((d, t) => {
    if (d.conversation_id && d.conversation_id !== id) {
      if (t === "message.created" && d.role === "assistant") waiters.current.get(d.run_id)?.(d.content);
      return;
    }
    if (t === "run.step") {
      setLiveSteps((s) => {
        const list = [...(s[d.run_id] || [])];
        const i = list.findIndex((x) => x.id === d.step.id);
        if (i >= 0) list[i] = d.step;
        else list.push(d.step);
        return { ...s, [d.run_id]: list };
      });
    } else if (t === "approval.created") {
      setLiveApprovals((a) => [...a.filter((x) => x.id !== d.id), d]);
    } else if (["message.created", "run.status", "run.finished", "approval.decided"].includes(t)) {
      if (t === "message.created" && d.role === "assistant") waiters.current.get(d.run_id)?.(d.content);
      if (t === "approval.decided") setLiveApprovals((a) => a.filter((x) => x.id !== d.id));
      qc.invalidateQueries({ queryKey: ["messages", id] });
    }
  }, [id]);

  const send = useCallback(async (text: string, attachments: any[] = [], channel = "web") => {
    const r = await post("/api/chat/send", { conversation_id: id || null, text, attachments: attachments.map((a) => a.id), model, channel });
    if (!id) nav(`/chat/${r.conversation.id}`, { replace: true });
    qc.invalidateQueries({ queryKey: ["messages", r.conversation.id] });
    qc.invalidateQueries({ queryKey: ["conversations"] });
    return r;
  }, [id, model, nav, qc]);

  useEffect(() => {
    const q = params.get("q");
    if (q && !autoSent.current) {
      autoSent.current = true;
      params.delete("q");
      setParams(params, { replace: true });
      send(q).catch(toastError);
    }
  }, [params, send, setParams]);

  const voiceSend = async (text: string) => {
    const r = await send(text, [], "voice");
    if (r.reply) return r.reply.content as string;
    return new Promise<string>((resolve) => {
      waiters.current.set(r.run.id, (t) => { waiters.current.delete(r.run.id); resolve(t); });
      setTimeout(() => resolve("پاسخ طول کشید؛ نتیجه در گفتگو نمایش داده می‌شود."), 180000);
    });
  };

  const cancel = async () => {
    if (!activeRun) return;
    try { await post(`/api/runs/${activeRun.id}/cancel`); toast("درخواست توقف ارسال شد", "warning"); } catch (e) { toastError(e); }
  };
  const resume = async (extra?: number) => {
    if (!activeRun) return;
    try {
      await post(`/api/runs/${activeRun.id}/resume`, extra ? { extra_budget_usd: extra, extra_steps: 10 } : { extra_steps: 10 });
      toast("کار ادامه می‌یابد", "success");
      qc.invalidateQueries({ queryKey: ["messages", id] });
    } catch (e) { toastError(e); }
  };

  const changeModel = async (v: string) => {
    setModel(v);
    if (id) await patch(`/api/conversations/${id}`, { model_selection: v }).catch(() => undefined);
  };

  const approvals = useMemo(() => {
    const map = new Map<string, any>();
    for (const a of data?.pending_approvals || []) map.set(a.id, a);
    for (const a of liveApprovals) map.set(a.id, { ...map.get(a.id), ...a });
    return [...map.values()];
  }, [data?.pending_approvals, liveApprovals]);

  const busy = !!activeRun && ACTIVE.includes(activeRun.status);
  const steps = activeRun ? liveSteps[activeRun.id] || [] : [];
  const thinkingVariant = (steps.length % 4) as 0 | 1 | 2 | 3;

  return (
    <div className="grid h-[calc(100dvh-11.75rem)] gap-4 lg:h-[calc(100dvh-7rem)] lg:grid-cols-[290px_1fr]">
      <aside className="glass hidden overflow-hidden rounded-card lg:block"><ConversationList activeId={id} onPick={() => undefined} /></aside>
      <Drawer open={listOpen} onClose={() => setListOpen(false)} title="گفتگوها"><ConversationList activeId={id} onPick={() => setListOpen(false)} /></Drawer>

      <section className="glass relative flex min-h-0 flex-col overflow-hidden rounded-card">
        {/* سربرگ: انتخاب مدل */}
        <div className="flex items-center gap-2 border-b hairline px-3 py-2.5 sm:px-4">
          <Button variant="ghost" size="icon" className="lg:hidden" onClick={() => setListOpen(true)} aria-label="فهرست گفتگوها"><PanelRightOpen className="h-5 w-5" /></Button>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold">{data?.conversation?.title || "گفتگوی جدید"}</div>
            {busy && <div className="truncate text-[11px] text-primary">در حال کار…</div>}
          </div>
          <div className="flex items-center gap-1.5">
            <Cpu className="hidden h-4 w-4 text-muted sm:block" />
            <Select value={model} onChange={(e) => changeModel(e.target.value)} className="h-9 w-32 text-xs sm:w-52" aria-label="انتخاب مدل">
              <option value="auto">✨ هوشمند (Smart Auto)</option>
              {(models.data?.items || []).filter((m: any) => m.capabilities?.tools !== false).map((m: any) => (
                <option key={m.id} value={m.id}>{m.label} — {m.provider}</option>
              ))}
            </Select>
            {id && (
              <>
                <Button variant="ghost" size="icon" className="hidden sm:inline-flex" title="سنجاق" onClick={async () => { await patch(`/api/conversations/${id}`, { pinned: !data?.conversation?.pinned }); qc.invalidateQueries({ queryKey: ["conversations"] }); qc.invalidateQueries({ queryKey: ["messages", id] }); }}>
                  <Pin className={cn("h-4 w-4", data?.conversation?.pinned && "fill-primary text-primary")} />
                </Button>
                <Button variant="ghost" size="icon" className="hidden sm:inline-flex" title="بایگانی" onClick={async () => { await patch(`/api/conversations/${id}`, { archived: true }); qc.invalidateQueries({ queryKey: ["conversations"] }); nav("/chat"); }}>
                  <Archive className="h-4 w-4" />
                </Button>
                <Button variant="ghost" size="icon" title="حذف" onClick={async () => { if (!confirm("این گفتگو حذف شود؟")) return; await del(`/api/conversations/${id}`); qc.invalidateQueries({ queryKey: ["conversations"] }); nav("/chat"); }}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
        </div>

        {/* پیام‌ها */}
        <div className="flex-1 space-y-4 overflow-y-auto px-3 py-5 sm:px-6">
          {!id && (
            <div className="flex h-full flex-col items-center justify-center gap-5 text-center">
              <Orb state="idle" size={180} className="animate-float" />
              <div>
                <h2 className="text-xl font-bold">چه کاری برایتان انجام دهم؟</h2>
                <p className="mt-1 text-sm text-muted">پیام، فایل، عکس یا صدا بفرستید. کارهای حساس قبل از اجرا از شما اجازه می‌گیرند.</p>
              </div>
              <div className="grid w-full max-w-xl gap-2 sm:grid-cols-2">
                {["امروز چه تسک‌هایی دارم؟", "هر روز ساعت ۹ صبح گزارش تسک‌ها را برایم بفرست", "قیمت امروز دلار را از وب پیدا کن", "یک تسک برای پیگیری قرارداد با علی بساز"].map((s) => (
                  <button key={s} onClick={() => send(s).catch(toastError)} className="glass rounded-2xl px-4 py-3 text-start text-sm transition hover:shadow-glow">{s}</button>
                ))}
              </div>
            </div>
          )}
          {id && thread.isLoading && <div className="space-y-3">{[1, 2, 3].map((i) => <Skeleton key={i} className="h-16 w-3/4" />)}</div>}
          {id && thread.error && <ErrorBox error={thread.error} retry={() => thread.refetch()} />}
          {data?.items?.map((m: any) => <MessageBubble key={m.id} m={m} run={runs[m.run_id]} />)}

          {/* اجرای زنده */}
          <AnimatePresence>
            {busy && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="flex gap-2.5">
                <div className="mt-1 shrink-0"><Orb state={activeRun.status === "waiting_approval" ? "waiting" : "thinking"} size={30} /></div>
                <div className="glass min-w-0 max-w-[86%] flex-1 rounded-2xl rounded-es-md px-4 py-3 sm:max-w-[78%]">
                  <Thinking variant={thinkingVariant} label={activeRun.status === "waiting_approval" ? "منتظر اجازه شما…" : activeRun.status === "queued" ? "در صف اجرا…" : "در حال فکر و اجرا…"} />
                  {steps.length > 0 && <div className="mt-3"><StepList steps={steps} live={activeRun.status === "running"} /></div>}
                  <div className="num mt-2 text-[10px] text-muted">{compact(activeRun.tokens_in + activeRun.tokens_out)} توکن · {usd(activeRun.cost_usd)}</div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
          {approvals.map((a) => <ApprovalCard key={a.id} a={a} onDone={() => qc.invalidateQueries({ queryKey: ["messages", id] })} />)}
          {activeRun?.status === "paused" && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-warning/30 bg-warning/8 px-4 py-3 text-sm">
              <span>⏸ اجرا متوقف شده است ({activeRun.stop_reason}). وضعیت ذخیره شده و قابل ادامه است.</span>
              <div className="flex gap-2">
                <Button size="sm" variant="soft" icon={<Play className="h-3.5 w-3.5" />} onClick={() => resume()}>ادامه</Button>
                <Button size="sm" variant="outline" onClick={() => resume(0.5)}>ادامه با ۰٫۵$ بودجه بیشتر</Button>
              </div>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="border-t hairline p-3 sm:p-4">
          <Composer onSend={async (t, a) => { try { await send(t, a); } catch (e) { toastError(e); throw e; } }} busy={busy} onStop={cancel} conversationId={id} onVoice={() => setVoice(true)} />
          <p className="mt-2 hidden items-center justify-center gap-1.5 text-center text-[10px] text-muted sm:flex">
            <Bot className="h-3 w-3" /> Enter ارسال · Shift+Enter خط جدید · فرمان‌های سریع بدون توکن: «وضعیت»، «توقف همه»، «ادامه همه»، «آندو»
          </p>
        </div>
      </section>
      <VoiceMode open={voice} onClose={() => { setVoice(false); if (params.get("voice")) { params.delete("voice"); setParams(params, { replace: true }); } }} send={voiceSend} />
      {!id && !models.isLoading && (models.data?.items || []).length === 0 && (
        <div className="fixed bottom-24 start-1/2 z-20 -translate-x-1/2 lg:bottom-8">
          <Empty title="" action={<Button variant="primary" onClick={() => nav("/settings?tab=ai")}>ابتدا یک مدل هوش مصنوعی اضافه کنید</Button>} />
        </div>
      )}
    </div>
  );
}
