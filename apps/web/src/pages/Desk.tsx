/**
 * میز کار و گزارش‌های ایجنت: کارفرما مجبور نیست بپرسد «چی شد؟».
 * نتایج مهم روتین‌ها، پیام‌های خارج از محدوده، خطاهای نیازمند رسیدگی، اجازه‌های منتظر و پایان مأموریت‌ها —
 * هر گزارش به اجرای واقعی متصل است و پس از Refresh باقی می‌ماند. نوبت‌های «بدون تغییر» گزارشی نمی‌سازند.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Archive, ArrowLeft, CheckCheck, Inbox, ListTree, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApprovalCard, StepList } from "../components/chat";
import { Badge, Button, Card, cn, Drawer, Empty, ErrorBox, PageHeader, Segmented, Skeleton } from "../components/ui";
import { get, post } from "../lib/api";
import { faInt, fmtDate, fmtTime, relTime, usd } from "../lib/format";
import { useLive } from "../lib/live";
import { toastError } from "../lib/store";

const KINDS = [
  { value: "all", label: "همه" },
  { value: "report", label: "گزارش‌ها" },
  { value: "error", label: "خطاها" },
  { value: "message", label: "پیام‌ها" },
  { value: "mission", label: "مأموریت‌ها" },
  { value: "approval", label: "اجازه‌ها" },
] as const;
const KIND_LABEL: Record<string, string> = {
  report: "گزارش روتین", error: "نیازمند رسیدگی", approval: "درخواست اجازه", mission: "پایان مأموریت", message: "پیام خارج از محدوده",
  long_task: "کار طولانی", employee: "گزارش کارمند",
};
const SEV: Record<string, { label: string; color: string }> = {
  critical: { label: "فوری", color: "rgb(var(--danger))" },
  important: { label: "مهم", color: "rgb(var(--warning))" },
  info: { label: "اطلاع", color: "rgb(var(--primary))" },
};

function dayKey(iso: string) {
  return fmtDate(iso, false);
}

function RunDrawer({ runId, onClose }: { runId: string | null; onClose: () => void }) {
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["run", runId], queryFn: () => get(`/api/runs/${runId}`), enabled: !!runId });
  const run = q.data?.run;
  return (
    <Drawer open={!!runId} onClose={onClose} title="جزئیات اجرا">
      {q.isLoading && <Skeleton className="h-40" />}
      {run && (
        <div className="space-y-5">
          <div className="grid grid-cols-3 gap-3 text-center">
            {[["وضعیت", run.status], ["هزینه", usd(run.cost_usd, 4)], ["فراخوانی مدل", faInt(run.llm_calls)]].map(([l, v]) => (
              <div key={l} className="rounded-2xl bg-line/[.05] px-2 py-3">
                <div className="num text-[16px] font-semibold">{v}</div>
                <div className="text-[12px] text-muted">{l}</div>
              </div>
            ))}
          </div>
          {run.result && <div className="whitespace-pre-wrap rounded-2xl bg-line/[.05] p-4 text-[13.5px] leading-7">{run.result}</div>}
          <StepList steps={q.data.steps || []} />
          {run.conversation_id && <Button variant="soft" className="w-full" onClick={() => { onClose(); nav(`/chat/${run.conversation_id}`); }}>باز کردن گفتگوی اجرا</Button>}
        </div>
      )}
    </Drawer>
  );
}

export default function Desk() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [kind, setKind] = useState<string>("all");
  const [archived, setArchived] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["inbox", kind, archived],
    queryFn: () => get(`/api/inbox?archived=${archived}${kind !== "all" ? `&kind=${kind}` : ""}`),
  });
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: () => get("/api/approvals?status=pending") });
  useLive((_d, t) => {
    if (t === "inbox.created" || t === "inbox.changed") qc.invalidateQueries({ queryKey: ["inbox"] });
    if (t === "approval.created" || t === "approval.decided") qc.invalidateQueries({ queryKey: ["approvals"] });
  });
  const act = async (id: string, action: string) => {
    try {
      await post(`/api/inbox/${id}/${action}`);
      qc.invalidateQueries({ queryKey: ["inbox"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    } catch (e) {
      toastError(e);
    }
  };
  const items: any[] = q.data?.items || [];
  const groups = items.reduce<Record<string, any[]>>((acc, it) => {
    (acc[dayKey(it.ts)] ||= []).push(it);
    return acc;
  }, {});
  const pending: any[] = approvals.data?.items || [];

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader title="میز کار و گزارش‌ها" icon={<Inbox className="h-5 w-5" />}
        sub="آنچه ایجنت بدون پرسیدن شما انجام داده، نتیجه گرفته یا نیاز به تصمیم شما دارد."
        actions={<>
          <Button variant="ghost" size="sm" onClick={() => setArchived((a) => !a)} icon={<Archive className="h-4 w-4" />}>{archived ? "گزارش‌های فعال" : "بایگانی"}</Button>
          <Button variant="soft" size="sm" onClick={async () => { await post("/api/inbox/read_all"); qc.invalidateQueries({ queryKey: ["inbox"] }); qc.invalidateQueries({ queryKey: ["dashboard"] }); }}
            icon={<CheckCheck className="h-4 w-4" />}>خواندن همه</Button>
        </>} />

      {pending.length > 0 && (
        <section className="mb-6 space-y-3" aria-label="اجازه‌های منتظر">
          <h2 className="flex items-center gap-2 text-[15px] font-semibold"><ShieldAlert className="h-4 w-4 text-warning" /> منتظر تصمیم شما ({faInt(pending.length)})</h2>
          {pending.map((a) => <ApprovalCard key={a.id} a={a} onDone={() => { qc.invalidateQueries({ queryKey: ["approvals"] }); qc.invalidateQueries({ queryKey: ["dashboard"] }); }} />)}
        </section>
      )}

      <div className="mb-5 overflow-x-auto no-scrollbar">
        <Segmented value={kind} onChange={setKind} options={KINDS.map((k) => ({ value: k.value, label: (
          <span className="flex items-center gap-1.5">{k.label}{k.value !== "all" && q.data?.unread_by_kind?.[k.value] ? <span className="num rounded-full bg-primary px-1.5 text-[10.5px] text-onprimary">{faInt(q.data.unread_by_kind[k.value])}</span> : null}</span>
        ) }))} />
      </div>

      {q.error && <ErrorBox error={q.error} retry={() => q.refetch()} />}
      {q.isLoading && <div className="space-y-3"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>}
      {!q.isLoading && items.length === 0 && (
        <Card><Empty icon={<Inbox className="h-6 w-6" />} title={archived ? "بایگانی خالی است" : "میز کار مرتب است"}
          text="نوبت‌هایی که تغییری پیدا نکنند گزارش نمی‌سازند؛ فقط نتیجه مهم، خطا، پیام خارج از محدوده و پایان مأموریت اینجا می‌آید." /></Card>
      )}

      <div className="space-y-7">
        {Object.entries(groups).map(([day, list]) => (
          <section key={day}>
            <h2 className="num mb-3 px-1 text-[13px] font-medium text-muted">{day}</h2>
            <ol className="relative space-y-3 ps-7">
              <span className="absolute bottom-3 start-[9px] top-3 w-px bg-gradient-to-b from-line/30 via-line/20 to-transparent" aria-hidden />
              <AnimatePresence initial={false}>
                {list.map((it) => {
                  const sev = SEV[it.severity] || SEV.info;
                  return (
                    <motion.li key={it.id} layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, x: -20 }}
                      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }} className="relative">
                      <span className="absolute -start-7 top-5 grid h-[18px] w-[18px] place-items-center rounded-full bg-surface"
                        style={{ boxShadow: `inset 0 0 0 1.5px ${sev.color}, 0 0 0 4px rgb(var(--bg) / .6)` }} aria-hidden>
                        {!it.read && <span className="h-2 w-2 rounded-full" style={{ background: sev.color }} />}
                      </span>
                      <Card className={cn("p-4 sm:p-5", !it.read && "die-2")}>
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone={it.severity === "critical" ? "danger" : it.severity === "important" ? "warning" : "primary"}>{sev.label}</Badge>
                          <span className="text-[12px] text-muted">{KIND_LABEL[it.kind] || it.kind}</span>
                          <span className="ms-auto num text-[12px] text-muted" title={fmtDate(it.ts)}><bdi>{fmtTime(it.ts)}</bdi> · {relTime(it.ts)}</span>
                        </div>
                        <h3 className={cn("mt-2 text-[15.5px] leading-7", it.read ? "font-medium" : "font-semibold")}>{it.title}</h3>
                        {it.body && <p className="mt-1 whitespace-pre-wrap text-[13.5px] leading-7 text-muted">{it.body}</p>}
                        {it.delivery?.telegram && (
                          <p className="mt-2 text-[12px] text-muted">ارسال تلگرام: {it.delivery.telegram === "sent" ? "ارسال شد" : it.delivery.telegram === "not_connected" ? "تلگرام متصل نبود (فقط پنل)" : "ناموفق"}</p>
                        )}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {it.run_id && <Button size="sm" variant="soft" onClick={() => { setRunId(it.run_id); if (!it.read) act(it.id, "read"); }} icon={<ListTree className="h-3.5 w-3.5" />}>مراحل اجرا</Button>}
                          {it.link && <Button size="sm" variant="ghost" onClick={() => { if (!it.read) act(it.id, "read"); nav(it.link); }} icon={<ArrowLeft className="h-3.5 w-3.5" />}>رفتن به جزئیات</Button>}
                          {!it.read && <Button size="sm" variant="ghost" onClick={() => act(it.id, "read")}>خوانده شد</Button>}
                          <Button size="sm" variant="ghost" onClick={() => act(it.id, archived ? "restore" : "archive")}>{archived ? "بازگردانی" : "بایگانی"}</Button>
                        </div>
                      </Card>
                    </motion.li>
                  );
                })}
              </AnimatePresence>
            </ol>
          </section>
        ))}
      </div>
      <RunDrawer runId={runId} onClose={() => setRunId(null)} />
    </div>
  );
}
