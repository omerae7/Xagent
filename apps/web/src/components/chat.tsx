/** اجزای مرکز گفتگو: پیام، مراحل زنده اجرا، کارت مجوز، ورودی چندرسانه‌ای و حالت مکالمه صوتی زنده. */

import { AnimatePresence, motion } from "framer-motion";
import {
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleDot,
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
import { Orb } from "./viz/Orb";

// ───────────── Markdown سبک و امن ─────────────
function esc(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
export function renderMarkdown(src: string): string {
  const blocks: string[] = [];
  let s = esc(src || "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, _l, code) => {
    blocks.push(`<pre><code>${code}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  s = s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  const lines = s.split("\n");
  const out: string[] = [];
  let list: "ul" | "ol" | null = null;
  for (const line of lines) {
    const ul = line.match(/^\s*[-•*]\s+(.*)/);
    const ol = line.match(/^\s*[\d۰-۹]+[.)]\s+(.*)/);
    if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (list !== kind) {
        if (list) out.push(`</${list}>`);
        out.push(`<${kind}>`);
        list = kind;
      }
      out.push(`<li>${(ul || ol)![1]}</li>`);
      continue;
    }
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
    const h = line.match(/^#{1,4}\s+(.*)/);
    out.push(h ? `<p><strong>${h[1]}</strong></p>` : line.trim() ? `<p>${line}</p>` : "");
  }
  if (list) out.push(`</${list}>`);
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_m, i) => blocks[+i]);
}

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
  return (
    <ol className="relative space-y-1.5 border-s hairline ps-4">
      <AnimatePresence initial={false}>
        {steps.map((s) => (
          <motion.li key={s.id || s.idx} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} className="relative">
            <span className={cn("absolute -start-[25px] top-0.5 grid h-[18px] w-[18px] place-items-center rounded-full ring-2 ring-bg",
              s.status === "error" ? "bg-danger/20 text-danger" : s.status === "blocked" ? "bg-warning/20 text-warning" : s.status === "running" ? "bg-primary/20 text-primary" : "bg-success/15 text-success")}>
              {s.status === "running" ? <Loader2 className="h-3 w-3 animate-spin" /> : STEP_ICON[s.kind] || <CircleDot className="h-3 w-3" />}
            </span>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
              <span className={cn(s.status === "error" && "text-danger", s.status === "blocked" && "text-warning")}>{s.title}</span>
              {s.model_label && <span className="text-[10px] text-muted">· {s.model_label}</span>}
              {(s.tokens_in > 0 || s.tokens_out > 0) && <span className="num text-[10px] text-muted">· {compact(s.tokens_in)}↑ {compact(s.tokens_out)}↓</span>}
              {s.duration_ms > 0 && <span className="num text-[10px] text-muted">· {duration(s.duration_ms)}</span>}
            </div>
            {s.kind === "tool" && s.detail?.output && (
              <details className="mt-0.5">
                <summary className="cursor-pointer text-[10px] text-muted hover:text-ink">خروجی ابزار</summary>
                <pre className="ltr mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-bg/60 p-2 text-[10px] leading-4 text-muted">{s.detail.output}</pre>
              </details>
            )}
          </motion.li>
        ))}
      </AnimatePresence>
      {live && (
        <li className="relative text-xs text-muted">
          <span className="absolute -start-[25px] top-0.5 grid h-[18px] w-[18px] place-items-center rounded-full bg-primary/15 ring-2 ring-bg"><span className="h-1.5 w-1.5 animate-ping rounded-full bg-primary" /></span>
          در حال ادامه…
        </li>
      )}
    </ol>
  );
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
      {open && <div className="mt-2">{data ? <StepList steps={data.steps} /> : <Loader2 className="h-4 w-4 animate-spin text-muted" />}</div>}
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
      className="relative overflow-hidden rounded-2xl border border-warning/35 bg-warning/[.07] p-4 shadow-[0_0_30px_-12px_rgb(var(--warning))]">
      <div className="absolute -end-10 -top-10 h-28 w-28 rounded-full bg-warning/20 blur-2xl" />
      <div className="relative flex items-start gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-warning/15 text-warning ring-1 ring-warning/30"><ShieldAlert className="h-5 w-5" /></span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">درخواست اجازه: {a.tool_label || a.tool_name}</span>
            <Badge tone="warning">{a.category_label || a.category}</Badge>
          </div>
          <p className="text-xs leading-6 text-muted">{a.reason}</p>
          {a.rule_text && <p className="rounded-lg bg-bg/50 px-2.5 py-1.5 text-[11px] leading-5 text-muted">قانون شما: {a.rule_text}</p>}
          {a.args && Object.keys(a.args).length > 0 && (
            <pre className="ltr max-h-40 overflow-auto rounded-lg bg-bg/60 p-2.5 text-[11px] leading-5">{JSON.stringify(a.args, null, 2)}</pre>
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
      {!mine && <div className="mt-1 shrink-0"><Orb state={meta.status === "failed" ? "stopped" : "idle"} size={30} /></div>}
      <div className={cn("max-w-[86%] sm:max-w-[78%]", mine && "items-end")}>
        <div className={cn("rounded-2xl px-4 py-2.5 text-[14px] leading-7",
          mine ? "rounded-ee-md bg-gradient-to-l from-primary to-[rgb(var(--primary)/.8)] text-white shadow-[0_8px_30px_-12px_rgb(var(--primary))]"
            : "glass rounded-es-md")}>
          {mine ? <p className="whitespace-pre-wrap break-words">{m.content}</p>
            : <div className="prose-chat break-words" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }} />}
          {m.attachments?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {m.attachments.map((a: any) => (
                <a key={a.id} href={`/api/uploads/${a.id}`} target="_blank" rel="noreferrer"
                  className={cn("flex items-center gap-1.5 rounded-lg px-2 py-1 text-[11px]", mine ? "bg-white/15" : "bg-line/10")}>
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
    <div className="glass rounded-3xl p-2 shadow-glow">
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">
          {files.map((f) => (
            <span key={f.id} className="flex items-center gap-1.5 rounded-lg bg-primary/10 px-2 py-1 text-[11px] text-primary">
              <FileText className="h-3 w-3" /> <span className="max-w-[160px] truncate">{f.filename}</span>
              <button onClick={() => setFiles((x) => x.filter((y) => y.id !== f.id))} aria-label="حذف"><X className="h-3 w-3" /></button>
            </span>
          ))}
        </div>
      )}
      <div className="flex items-end gap-1.5">
        <label className="grid h-10 w-10 shrink-0 cursor-pointer place-items-center rounded-2xl text-muted transition hover:bg-line/10 hover:text-ink" title="پیوست فایل یا عکس">
          {uploading ? <Loader2 className="h-5 w-5 animate-spin" /> : <Paperclip className="h-5 w-5" />}
          <input type="file" multiple className="hidden" onChange={(e) => upload(e.target.files)} />
        </label>
        <textarea ref={taRef} value={text} onChange={(e) => setText(e.target.value)} rows={1}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
          placeholder="از ایجنت بپرس یا کاری بسپار…  (Enter ارسال، Shift+Enter خط جدید)"
          className="max-h-[220px] min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-[14px] leading-6 outline-none placeholder:text-muted/80" />
        <button onClick={toggleRecord} title="ضبط صدا و تبدیل به متن"
          className={cn("grid h-10 w-10 shrink-0 place-items-center rounded-2xl transition", recording ? "animate-pulse bg-danger/15 text-danger" : "text-muted hover:bg-line/10 hover:text-ink")}>
          {recording ? <MicOff className="h-5 w-5" /> : <Mic className="h-5 w-5" />}
        </button>
        <button onClick={onVoice} title="مکالمه صوتی زنده" className="hidden h-10 w-10 shrink-0 place-items-center rounded-2xl text-accent transition hover:bg-accent/10 sm:grid">
          <Sparkles className="h-5 w-5" />
        </button>
        {busy && onStop ? (
          <Button variant="danger" size="icon" onClick={onStop} aria-label="توقف" className="h-10 w-10 rounded-2xl"><Square className="h-4 w-4" /></Button>
        ) : (
          <Button variant="primary" size="icon" onClick={submit} loading={sending} aria-label="ارسال" className="h-10 w-10 rounded-2xl"><Send className="h-4 w-4 -scale-x-100" /></Button>
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
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 z-50 grid place-items-center bg-bg/80 p-6 backdrop-blur-2xl">
          <div className="app-bg" />
          <div className="flex w-full max-w-md flex-col items-center gap-6 text-center">
            <Orb state={phase === "thinking" ? "thinking" : phase === "speaking" ? "executing" : "idle"} size={260} />
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
