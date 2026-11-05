/** اجزای مرکز گفتگو: پیام، مراحل زنده اجرا، کارت مجوز، ورودی چندرسانه‌ای و حالت مکالمه صوتی زنده. */

import { AnimatePresence, motion } from "framer-motion";
import { ChatMarkdown } from "./markdown";
import {
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  Compass,
  Eye,
  FileText,
  Flag,
  ListChecks,
  Loader2,
  Mic,
  MicOff,
  Paperclip,
  Route,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  Target,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api, get, post } from "../lib/api";
import { compact, duration, fmtTime, usd } from "../lib/format";
import { toast, toastError } from "../lib/store";
import { Badge, Button, cn } from "./ui";
import { ChipMark, PadArray } from "./viz/Silicon";
import { Timeline, type TimelineItem } from "./viz/Timeline";

// ───────────── Markdown سبک و امن ─────────────
// ───────────── مراحل اجرا ─────────────
const STEP_ICON: Record<string, ReactNode> = {
  route: <Route className="h-3.5 w-3.5" />,
  goal: <Target className="h-3.5 w-3.5" />,
  plan: <ListChecks className="h-3.5 w-3.5" />,
  memory: <Brain className="h-3.5 w-3.5" />,
  llm: <Sparkles className="h-3.5 w-3.5" />,
  tool: <Wrench className="h-3.5 w-3.5" />,
  validate: <ShieldCheck className="h-3.5 w-3.5" />,
  recover: <Compass className="h-3.5 w-3.5" />,
  approval: <ShieldAlert className="h-3.5 w-3.5" />,
  stoploss: <Flag className="h-3.5 w-3.5" />,
  answer: <CheckCircle2 className="h-3.5 w-3.5" />,
  info: <Eye className="h-3.5 w-3.5" />,
};

export function StepList({ steps, live }: { steps: any[]; live?: boolean }) {
  const items: TimelineItem[] = steps.map((s) => ({
    id: String(s.id || s.idx),
    status: s.status === "running" ? "running" : s.status === "error" ? "error" : s.status === "blocked" ? "blocked" : "done",
    icon: STEP_ICON[s.kind],
    title: s.title,
    meta: (s.model_label || s.tokens_in > 0 || s.tokens_out > 0 || s.duration_ms > 0) ? (
      <>
        {s.model_label && <span>{s.model_label}</span>}
        {(s.tokens_in > 0 || s.tokens_out > 0) && <span className="num">{compact(s.tokens_in)} ورودی · {compact(s.tokens_out)} خروجی</span>}
        {s.duration_ms > 0 && <span className="num">{duration(s.duration_ms)}</span>}
      </>
    ) : undefined,
    children: s.kind === "tool" && s.detail?.output ? (
      <details className="group">
        <summary className="cursor-pointer list-none text-[12px] text-primary hover:underline">نمایش خروجی ابزار</summary>
        <pre className="ltr mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-line/[.06] p-2.5 text-[11px] leading-5 text-muted ring-1 ring-inset ring-line/10">{s.detail.output}</pre>
      </details>
    ) : undefined,
  }));
  return <Timeline items={items} live={live} size="sm" />;
}

export function RunSteps({ runId, initialOpen = false }: { runId: string; initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen);
  const [data, setData] = useState<any>(null);
  useEffect(() => {
    if (open && !data) get(`/api/runs/${runId}`).then(setData).catch(() => undefined);
  }, [open, data, runId]);
  return (
    <div className="mt-2">
      <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-1 text-[11px] text-muted hover:text-ink">
        <ChevronDown className={cn("h-3 w-3 transition", open && "rotate-180")} /> مراحل اجرا
      </button>
      {open && <div className="mt-2">{data ? <><Outcome o={data.outcome} /><StepList steps={data.steps} /></> : <Loader2 className="h-4 w-4 animate-spin text-muted" />}</div>}
    </div>
  );
}

const DELIVERY_FA: Record<string, [string, string]> = {
  sent: ["تحویل شد", "text-success"], failed: ["ارسال نشد", "text-danger"], unknown: ["نتیجه ارسال نامعلوم", "text-warning"],
  uncertain: ["نتیجه تلاش قبلی نامعلوم؛ تکرار نشد", "text-warning"], not_connected: ["کانال متصل نبود؛ ارسال نشد", "text-muted"],
  pending_reconciliation: ["در انتظار تطبیق: نتیجه تلاش قبلی نامعلوم؛ تکرار نشد", "text-warning"],
};

/** واقعیت اجرایی Run از State سمت سرور: شواهد ابزار، اعتبارسنجی و تحویل اعلان — نه برداشت از متن مدل */
function Outcome({ o }: { o?: any }) {
  if (!o) return null;
  const ev: any[] = o.evidence || [];
  const ok = ev.filter((e) => e.ok && (e.verified === true || e.requires_verification === false)).length;
  const bad = ev.filter((e) => !e.ok || e.verified === false).length;
  const d = o.delivery?.status ? DELIVERY_FA[o.delivery.status] : null;
  if (!ev.length && o.validation?.passed == null && !d) return null;
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl bg-line/[.05] px-3 py-2 text-[11.5px] leading-5 ring-1 ring-inset ring-line/10">
      <span className={o.validation?.passed === true ? "text-success" : o.validation?.passed === false ? "text-danger" : "text-muted"}>
        {o.validation?.passed === true ? "تکمیل با شواهد تأیید شد" : o.validation?.passed === false ? "تکمیل تأیید نشد" : "اعتبارسنجی ثبت نشده"}
      </span>
      {ev.length > 0 && <span className="text-muted">شواهد ابزار: <span className="num">{ok}</span> موفق{bad ? <> · <span className="num text-danger">{bad}</span> ناموفق/تأییدنشده</> : null}</span>}
      {d && <span className={d[1]}>اعلان نهایی: {d[0]}</span>}
      {o.context_fit?.changed && <span className="text-muted">کانتکست برای جا شدن در مدل خلاصه شد</span>}
      {o.validation?.passed === false && (o.validation.issues || []).length > 0 && (
        <span className="basis-full text-danger/90">{o.validation.issues.slice(0, 3).join(" · ")}</span>
      )}
    </div>
  );
}

// ───────────── کارت مجوز ─────────────
export function ApprovalCard({ a, onDone }: { a: any; onDone?: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const decide = async (decision: "approve" | "deny") => {
    setBusy(decision);
    try {
      await post(`/api/approvals/${a.id}`, { decision });
      toast(decision === "approve" ? "اجازه داده شد؛ کار ادامه می‌یابد" : "درخواست رد شد", decision === "approve" ? "success" : "warning");
      onDone?.();
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <motion.div initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }}
      className="die die-live relative overflow-hidden p-4">
     
      <div className="relative flex items-start gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-warning/12 text-warning ring-1 ring-inset ring-warning/30"><ShieldAlert className="h-5 w-5" /></span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">درخواست اجازه: {a.tool_label || a.tool_name}</span>
            <Badge tone="warning">{a.category_label || a.category}</Badge>
          </div>
          <p className="text-xs leading-6 text-muted">{a.reason}</p>
          {a.rule_text && <p className="rounded-xl bg-line/[.06] px-3 py-1.5 text-[11px] leading-5 text-muted">قانون شما: {a.rule_text}</p>}
          {a.args && Object.keys(a.args).length > 0 && (
            <pre className="ltr max-h-40 overflow-auto rounded-xl bg-line/[.06] p-3 text-[11px] leading-5">{JSON.stringify(a.args, null, 2)}</pre>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button size="sm" variant="primary" loading={busy === "approve"} onClick={() => decide("approve")} icon={<Check className="h-3.5 w-3.5" />}>اجازه می‌دهم</Button>
            <Button size="sm" variant="danger" loading={busy === "deny"} onClick={() => decide("deny")} icon={<XCircle className="h-3.5 w-3.5" />}>رد</Button>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

// ───────────── پیام ─────────────
export function MessageBubble({ m, run }: { m: any; run?: any }) {
  const mine = m.role === "user";
  const meta = m.meta || {};
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}
      className={cn("flex gap-2.5", mine ? "flex-row-reverse" : "flex-row")}>
      {!mine && <div className="mt-1 shrink-0"><ChipMark state={meta.status === "failed" ? "stopped" : "idle"} size={30} /></div>}
      <div className={cn("max-w-[86%] sm:max-w-[78%]", mine && "items-end")}>
        <div className={cn("px-4 py-3 text-[14.5px] leading-7",
          mine ? "rounded-[22px] rounded-ee-[8px] bg-primary text-onprimary shadow-[0_14px_30px_-18px_rgb(var(--primary))]"
            : "die rounded-[22px] rounded-es-[8px]")}>
          {mine ? <p className="whitespace-pre-wrap break-words">{m.content}</p>
            : <ChatMarkdown text={m.content} />}
          {m.attachments?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {m.attachments.map((a: any) => (
                <a key={a.id} href={`/api/uploads/${a.id}`} target="_blank" rel="noreferrer"
                  className={cn("flex items-center gap-1.5 rounded-xl px-2 py-1 text-[11px]", mine ? "bg-white/15" : "bg-line/10")}>
                  {a.mime?.startsWith("image/") ? <img src={`/api/uploads/${a.id}`} alt="" className="h-14 w-14 rounded-md object-cover" /> : <FileText className="h-3.5 w-3.5" />}
                  {!a.mime?.startsWith("image/") && <span className="max-w-[140px] truncate">{a.filename}</span>}
                </a>
              ))}
            </div>
          )}
        </div>
        <div className={cn("mt-1 flex flex-wrap items-center gap-2 px-1 text-[10px] text-muted", mine && "justify-end")}>
          <span>{fmtTime(m.created_at)}</span>
          {!mine && meta.tokens_in > 0 && <span className="num">{compact(meta.tokens_in)}↑ {compact(meta.tokens_out)}↓ · {usd(meta.cost)}</span>}
          {!mine && meta.route === "command" && <Badge tone="cyan">فرمان سریع · بدون توکن</Badge>}
          {meta.channel === "voice" && <Badge tone="accent">صوتی</Badge>}
          {meta.channel === "telegram" && <Badge tone="primary">تلگرام</Badge>}
        </div>
        {!mine && m.run_id && run && run.route?.path !== "command" && <RunSteps runId={m.run_id} />}
      </div>
    </motion.div>
  );
}

// ───────────── ورودی ─────────────
export function Composer({
  onSend,
  busy,
  onStop,
  conversationId,
  onVoice,
}: {
  onSend: (text: string, attachments: any[]) => Promise<void>;
  busy: boolean;
  onStop?: () => void;
  conversationId?: string;
  onVoice: () => void;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<any[]>([]);
  const [uploading, setUploading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [sending, setSending] = useState(false);
  const recRef = useRef<MediaRecorder | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = taRef.current;
    if (ta) {
      ta.style.height = "0px";
      ta.style.height = Math.min(220, ta.scrollHeight) + "px";
    }
  }, [text]);

  const upload = async (list: FileList | null) => {
    if (!list?.length) return;
    setUploading(true);
    try {
      for (const f of Array.from(list)) {
        const form = new FormData();
        form.append("file", f);
        const u = await api(`/api/uploads${conversationId ? `?conversation_id=${conversationId}` : ""}`, { form });
        setFiles((x) => [...x, u]);
      }
    } catch (e) {
      toastError(e);
    } finally {
      setUploading(false);
    }
  };

  const toggleRecord = async () => {
    if (recording) {
      recRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        const form = new FormData();
        form.append("file", blob, "voice.webm");
        try {
          const r = await api<{ text: string }>("/api/voice/stt", { form });
          setText((t) => (t ? t + " " : "") + r.text);
        } catch (e) {
          toastError(e);
        }
      };
      recRef.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      toast("دسترسی به میکروفون ممکن نیست", "error");
    }
  };

  const submit = async () => {
    if ((!text.trim() && !files.length) || sending) return;
    setSending(true);
    try {
      await onSend(text.trim(), files);
      setText("");
      setFiles([]);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="die rounded-xl p-2">
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">
          {files.map((f) => (
            <span key={f.id} className="flex items-center gap-1.5 rounded-xl bg-primary/10 px-2 py-1 text-[11px] text-primary">
              <FileText className="h-3 w-3" /> <span className="max-w-[160px] truncate">{f.filename}</span>
              <button onClick={() => setFiles((x) => x.filter((y) => y.id !== f.id))} aria-label="حذف"><X className="h-3 w-3" /></button>
            </span>
          ))}
        </div>
      )}
      <div className="flex items-end gap-1.5">
        <label className="grid h-10 w-10 shrink-0 cursor-pointer place-items-center rounded-xl text-muted transition hover:bg-line/10 hover:text-ink" title="پیوست فایل یا عکس">
          {uploading ? <Loader2 className="h-5 w-5 animate-spin" /> : <Paperclip className="h-5 w-5" />}
          <input type="file" multiple className="hidden" onChange={(e) => upload(e.target.files)} />
        </label>
        <textarea ref={taRef} value={text} onChange={(e) => setText(e.target.value)} rows={1}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
          placeholder="از ایجنت بپرس یا کاری بسپار…"
          className="max-h-[220px] min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-[14px] leading-6 outline-none placeholder:text-muted/80" />
        <button onClick={toggleRecord} title="ضبط صدا و تبدیل به متن"
          className={cn("grid h-10 w-10 shrink-0 place-items-center rounded-xl transition", recording ? "animate-pulse bg-danger/15 text-danger" : "text-muted hover:bg-line/10 hover:text-ink")}>
          {recording ? <MicOff className="h-5 w-5" /> : <Mic className="h-5 w-5" />}
        </button>
        <button onClick={onVoice} title="مکالمه صوتی زنده" className="hidden h-10 w-10 shrink-0 place-items-center rounded-xl text-accent transition hover:bg-accent/10 sm:grid">
          <Sparkles className="h-5 w-5" />
        </button>
        {busy && onStop ? (
          <Button variant="danger" size="icon" onClick={onStop} aria-label="توقف" className="h-10 w-10 rounded-xl"><Square className="h-4 w-4" /></Button>
        ) : (
          <Button variant="primary" size="icon" onClick={submit} loading={sending} aria-label="ارسال" className="h-10 w-10 rounded-xl"><Send className="h-4 w-4 -scale-x-100" /></Button>
        )}
      </div>
    </div>
  );
}

// ───────────── حالت مکالمه صوتی زنده ─────────────
type VoicePhase = "listening" | "hearing" | "thinking" | "speaking" | "idle";

export function VoiceMode({ open, onClose, send }: { open: boolean; onClose: () => void; send: (text: string) => Promise<string> }) {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [level, setLevel] = useState(0);
  const [last, setLast] = useState<{ q?: string; a?: string }>({});
  const active = useRef(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (!open) {
      active.current = false;
      audioRef.current?.pause();
      return;
    }
    active.current = true;
    let stream: MediaStream | null = null;
    let ctx: AudioContext | null = null;
    let raf = 0;
    const loop = async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch {
        toast("دسترسی به میکروفون ممکن نیست", "error");
        onClose();
        return;
      }
      ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.connect(an);
      const buf = new Uint8Array(an.fftSize);
      const rms = () => {
        an.getByteTimeDomainData(buf);
        let s = 0;
        for (const v of buf) s += ((v - 128) / 128) ** 2;
        return Math.sqrt(s / buf.length);
      };
      while (active.current) {
        setPhase("listening");
        // تشخیص گفتار با آستانه انرژی و سکوت
        const blob: Blob | null = await new Promise((resolve) => {
          const rec = new MediaRecorder(stream!);
          const chunks: Blob[] = [];
          let speaking = false;
          let silenceSince = 0;
          const started = performance.now();
          rec.ondataavailable = (e) => chunks.push(e.data);
          rec.onstop = () => resolve(speaking ? new Blob(chunks, { type: rec.mimeType || "audio/webm" }) : null);
          rec.start(250);
          const tick = () => {
            if (!active.current) { rec.state !== "inactive" && rec.stop(); return; }
            const v = rms();
            setLevel(v);
            const now = performance.now();
            if (v > 0.045) { speaking = true; silenceSince = 0; setPhase("hearing"); }
            else if (speaking && !silenceSince) silenceSince = now;
            if ((speaking && silenceSince && now - silenceSince > 1300) || now - started > 30000 || (!speaking && now - started > 15000)) {
              rec.state !== "inactive" && rec.stop();
              return;
            }
            raf = requestAnimationFrame(tick);
          };
          tick();
        });
        if (!active.current) break;
        if (!blob) continue;
        setPhase("thinking");
        try {
          const form = new FormData();
          form.append("file", blob, "turn.webm");
          const { text } = await api<{ text: string }>("/api/voice/stt", { form });
          if (!text?.trim()) continue;
          setLast({ q: text });
          const answer = await send(text);
          setLast({ q: text, a: answer });
          if (!active.current) break;
          setPhase("speaking");
          const res = await api<Response>("/api/voice/tts", { method: "POST", body: { text: answer.slice(0, 1200) }, raw: true });
          const url = URL.createObjectURL(await res.blob());
          await new Promise<void>((r) => {
            const a = new Audio(url);
            audioRef.current = a;
            a.onended = () => r();
            a.onerror = () => r();
            a.play().catch(() => r());
          });
          URL.revokeObjectURL(url);
        } catch (e) {
          toastError(e);
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
    };
    loop();
    return () => {
      active.current = false;
      cancelAnimationFrame(raf);
      stream?.getTracks().forEach((t) => t.stop());
      ctx?.close().catch(() => undefined);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const LABEL: Record<VoicePhase, string> = {
    idle: "آماده",
    listening: "در حال شنیدن… صحبت کنید",
    hearing: "می‌شنوم…",
    thinking: "در حال فکر…",
    speaking: "در حال پاسخ…",
  };
  return (
    <AnimatePresence>
      {open && (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 z-50 grid place-items-center bg-bg/80 p-6">
          <div className="app-bg" />
          <div className="flex w-full max-w-md flex-col items-center gap-6 text-center">
            <PadArray state={phase === "thinking" ? "thinking" : phase === "speaking" || phase === "hearing" ? "executing" : "idle"} size={240} n={10} label="وضعیت گفتگوی صوتی" />
            <div className="flex h-12 items-center gap-1" aria-hidden>
              {Array.from({ length: 28 }).map((_, i) => (
                <motion.span key={i} className="w-1 rounded-full bg-gradient-to-t from-primary to-cyan"
                  animate={{ height: phase === "hearing" ? 6 + Math.abs(Math.sin(i * 1.7)) * level * 260 : phase === "speaking" ? [6, 10 + (i % 5) * 6, 6] : 5 }}
                  transition={{ duration: phase === "speaking" ? 0.6 : 0.12, repeat: phase === "speaking" ? Infinity : 0, delay: i * 0.02 }} />
              ))}
            </div>
            <p className="text-lg font-semibold">{LABEL[phase]}</p>
            {last.q && <p className="line-clamp-2 text-sm text-muted">شما: {last.q}</p>}
            {last.a && <p className="line-clamp-3 text-sm">{last.a}</p>}
            <Button variant="danger" size="lg" onClick={onClose} icon={<X className="h-4 w-4" />}>پایان مکالمه</Button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
