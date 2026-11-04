import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Lightbulb, Moon, Play, Power, Sparkles, Undo2, Wand2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Badge, Button, Card, CardHeader, Empty, ErrorBox, PageHeader, Skeleton } from "../components/ui";
import { ChipMark } from "../components/viz/Silicon";
import { get, post } from "../lib/api";
import { bytes, compact, faInt, fmtDate, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const ST: Record<string, { l: string; tone: any }> = {
  running: { l: "در حال رویا", tone: "primary" }, done: { l: "انجام شد", tone: "success" }, skipped: { l: "بدون مصرف LLM", tone: "cyan" }, failed: { l: "ناموفق", tone: "danger" },
};

export default function Dream() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const reports = useQuery({ queryKey: ["dream"], queryFn: () => get("/api/dream/reports") });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => get("/api/skills") });
  useLive((_d, t) => { if (t.startsWith("dream.")) { qc.invalidateQueries({ queryKey: ["dream"] }); qc.invalidateQueries({ queryKey: ["skills"] }); } });
  const running = (reports.data?.items || []).some((r: any) => r.status === "running");
  const run = async () => { try { const r = await post("/api/dream/run"); toast(r.message, "success"); } catch (e) { toastError(e); } };
  const skillAct = async (id: string, a: string) => { await post(`/api/skills/${id}/${a}`).catch(toastError); qc.invalidateQueries({ queryKey: ["skills"] }); };
  return (
    <div>
      <PageHeader title="رویا و خودبهبودی" sub="تقطیر افزایشی شبانه: فقط رویدادهای جدید، بدون LLM اگر چیز مهمی نبود" icon={<Moon className="h-5 w-5" />}
        actions={<Button variant="primary" loading={running} onClick={run} icon={<Play className="h-4 w-4" />}>اجرای رویا الان</Button>} />
      <div className="grid gap-5 xl:grid-cols-[1fr_380px]">
        <div className="space-y-3">
          {reports.error && <ErrorBox error={reports.error} />}
          {reports.isLoading && <Skeleton className="h-40" />}
          {!reports.isLoading && (reports.data?.items || []).length === 0 && <Card><Empty icon={<Moon className="h-6 w-6" />} title="هنوز رویایی اجرا نشده" text="اجرای خودکار رویا پیش‌فرض غیرفعال است. برای اجرای دستی یا شبانه، ابتدا آن را از تنظیمات فعال کنید." /></Card>}
          {(reports.data?.items || []).map((r: any, i: number) => (
            <motion.div key={r.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.04 }}>
              <Card className="p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">{r.status === "running" ? <ChipMark state="thinking" size={26} /> : <Moon className="h-5 w-5 text-accent" />}<span className="font-semibold">{fmtDate(r.started_at)}</span><Badge tone={ST[r.status]?.tone}>{ST[r.status]?.l}</Badge></div>
                  <div className="num flex gap-3 text-[11px] text-muted"><span>{faInt(r.events_reviewed)} رویداد</span>{r.llm_used && <span>{compact(r.tokens)} توکن · {usd(r.cost_usd)}</span>}{r.cleanup?.files_removed > 0 && <span>پاکسازی {bytes(r.cleanup.bytes_freed)}</span>}</div>
                </div>
                <p className="mt-2 whitespace-pre-line text-sm leading-7 text-muted">{r.summary}</p>
                {(r.learnings || []).length > 0 && (
                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    {r.learnings.map((l: any, j: number) => <Badge key={j} tone={l.type === "skill" ? "accent" : l.type === "obsolete" ? "neutral" : "primary"}>{l.type === "skill" ? `مهارت: ${l.name}` : l.type === "agent_fact" ? `دانسته: ${l.key}` : "تجربه منسوخ"}</Badge>)}
                    <Button size="sm" variant="ghost" icon={<Undo2 className="h-3.5 w-3.5" />} onClick={() => nav("/memory")}>Undo یادگیری‌ها</Button>
                  </div>
                )}
              </Card>
            </motion.div>
          ))}
        </div>
        <Card className="h-fit">
          <CardHeader title="مهارت‌های آموخته‌شده" icon={<Wand2 className="h-4 w-4" />} sub="ابتدا آزمایشی؛ در صورت پسرفت خودکار حذف می‌شوند" />
          <div className="space-y-2 p-4">
            {skills.isLoading && <Skeleton className="h-20" />}
            {(skills.data?.items || []).length === 0 && !skills.isLoading && <p className="py-6 text-center text-sm text-muted"><Lightbulb className="mx-auto mb-2 h-6 w-6" />هنوز مهارتی تقطیر نشده است.</p>}
            {(skills.data?.items || []).map((s: any) => (
              <div key={s.id} className="rounded-xl border hairline p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 text-sm font-medium"><Sparkles className="h-3.5 w-3.5 text-accent" />{s.name}</span>
                  <Badge tone={s.status === "active" ? "success" : s.status === "probationary" ? "warning" : "neutral"}>{s.status === "active" ? "فعال" : s.status === "probationary" ? "آزمایشی" : "غیرفعال"}</Badge>
                </div>
                <p className="mt-1 text-xs leading-5 text-muted">{s.description}</p>
                <div className="num mt-1.5 flex gap-3 text-[10px] text-muted"><span>✓ {faInt(s.success_count)}</span><span>✗ {faInt(s.failure_count)}</span><span>تست: {s.test_status === "passed" ? "موفق" : s.test_status || "—"}</span></div>
                <div className="mt-2 flex gap-1.5">
                  {s.status !== "active" && <Button size="sm" variant="soft" onClick={() => skillAct(s.id, "activate")}>فعال‌سازی</Button>}
                  {s.status !== "disabled" && <Button size="sm" variant="ghost" icon={<Power className="h-3.5 w-3.5" />} onClick={() => skillAct(s.id, "disable")}>غیرفعال</Button>}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
