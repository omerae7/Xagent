import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Github, History, Package, Plug, PlugZap, Power, RefreshCw, RotateCcw, Trash2, Wrench } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge, Button, Card, CardHeader, cn, Empty, ErrorBox, Input, Modal, PageHeader, Skeleton, Tabs } from "../components/ui";
import { del, get, post } from "../lib/api";
import { faInt, fmtDate, relTime } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const CAT: Record<string, string> = {
  file_modify: "ویرایش فایل", external_message: "پیام بیرونی", install_tool: "نصب ابزار", routine_change: "تسک/روتین",
  code_exec: "اجرای کد", desktop_control: "کنترل دسکتاپ", login: "لاگین", external_tool: "ابزار خارجی",
};
const STATUS: Record<string, { l: string; tone: any }> = {
  ready: { l: "آماده", tone: "success" }, installing: { l: "در حال نصب", tone: "primary" }, error: { l: "خطا", tone: "danger" }, disabled: { l: "غیرفعال", tone: "neutral" },
};

function Versions({ tool, onClose }: { tool: any; onClose: () => void }) {
  const { data } = useQuery({ queryKey: ["tool-versions", tool.id], queryFn: () => get(`/api/tools/${tool.id}/versions`) });
  const rollback = async (vid: string) => {
    await post(`/api/tools/${tool.id}/rollback`, { version_id: vid }).catch(toastError);
    toast("درخواست بازگشت نسخه ثبت شد", "success");
    onClose();
  };
  return (
    <Modal open onClose={onClose} title={`نسخه‌های ${tool.name}`}>
      <div className="space-y-2">
        {(data?.items || []).map((v: any) => (
          <div key={v.id} className={cn("flex items-center justify-between gap-3 rounded-xl border hairline px-3 py-2.5", v.id === tool.active_version_id && "border-primary/40 bg-primary/8")}>
            <div className="min-w-0">
              <div className="ltr text-end font-mono text-sm">{v.version}</div>
              <div className="text-[11px] text-muted">{fmtDate(v.created_at)} · تست: {v.test_status === "passed" ? "✅ موفق" : v.test_status === "failed" ? "❌ ناموفق" : "—"}</div>
            </div>
            {v.id === tool.active_version_id ? <Badge tone="primary">فعال</Badge> : <Button size="sm" variant="soft" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => rollback(v.id)}>بازگشت</Button>}
          </div>
        ))}
      </div>
    </Modal>
  );
}

export default function Tools() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"installed" | "builtin">("installed");
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<any | null>(null);
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["tools"], queryFn: () => get("/api/tools") });
  useLive((_d, t) => t === "tool.changed" && qc.invalidateQueries({ queryKey: ["tools"] }));
  const groups = useMemo(() => {
    const g: Record<string, any[]> = {};
    for (const t of data?.builtin || []) (g[t.group_label] ||= []).push(t);
    return g;
  }, [data]);

  const install = async () => {
    if (!source.trim()) return;
    setBusy(true);
    try {
      const r = await post("/api/tools/install", { source: source.trim(), ref: ref.trim() || null });
      toast(r.message, "success");
      setSource("");
      setRef("");
    } catch (e) { toastError(e); } finally { setBusy(false); }
  };
  const action = async (t: any, a: string) => {
    try {
      await post(`/api/tools/${t.id}/${a}`, {});
      toast(a === "update" ? "به‌روزرسانی در محیط ایزوله آغاز شد" : a === "rollback" ? "بازگشت به نسخه قبلی آغاز شد" : "انجام شد", "success");
      qc.invalidateQueries({ queryKey: ["tools"] });
    } catch (e) { toastError(e); }
  };

  return (
    <div>
      <PageHeader title="ابزارها و مهارت‌ها" sub="انتخاب کم‌هزینه‌ترین ابزار: API/MCP ← مرورگر ساختاریافته ← کنترل تصویری دسکتاپ" icon={<Wrench className="h-5 w-5" />} />
      <Card className="mb-5 overflow-hidden" glow>
        <CardHeader title="نصب ابزار جدید" icon={<Github className="h-4 w-4" />} sub="از مخزن GitHub، بسته npm/PyPI یا نشانی سرور MCP — دریافت، تست و نصب در محیط ایزوله" />
        <div className="grid gap-3 p-4 sm:grid-cols-[1fr_180px_auto] sm:p-5">
          <Input value={source} onChange={(e) => setSource(e.target.value)} placeholder="https://github.com/owner/repo  یا  npm:@scope/server  یا  pypi:package" className="ltr" />
          <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="شاخه/تگ/نسخه (اختیاری)" className="ltr" />
          <Button variant="primary" loading={busy} onClick={install} icon={<PlugZap className="h-4 w-4" />}>نصب و تست</Button>
        </div>
      </Card>
      <Tabs value={tab} onChange={setTab} tabs={[{ value: "installed", label: `نصب‌شده (${faInt(data?.installed?.length || 0)})`, icon: <Plug className="h-4 w-4" /> }, { value: "builtin", label: `داخلی (${faInt(data?.builtin?.length || 0)})`, icon: <Package className="h-4 w-4" /> }]} />
      <div className="mt-4">
        {error && <ErrorBox error={error} retry={refetch} />}
        {isLoading && <Skeleton className="h-40" />}
        {tab === "installed" && !isLoading && (
          (data?.installed || []).length === 0 ? <Card><Empty icon={<Plug className="h-6 w-6" />} title="ابزار خارجی نصب نشده" text="سرورهای MCP یا ابزارهای GitHub را با یک لینک نصب کنید. هر ابزار نسخه‌بندی و قابل Rollback است." /></Card> : (
            <div className="grid gap-4 md:grid-cols-2">
              {data.installed.map((t: any, i: number) => (
                <motion.div key={t.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.04 }}>
                  <Card className="p-4">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2"><span className="font-semibold">{t.name}</span><Badge tone={STATUS[t.status]?.tone}>{STATUS[t.status]?.l || t.status}</Badge><Badge tone="cyan">{t.kind}</Badge></div>
                        <p className="ltr mt-1 truncate text-end text-[11px] text-muted">{t.source_url}</p>
                      </div>
                    </div>
                    {t.description && <p className="mt-2 text-sm text-muted">{t.description}</p>}
                    {t.last_error && <p className="mt-2 rounded-lg bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{t.last_error}</p>}
                    <div className="mt-3 flex flex-wrap gap-1">{(t.tool_names || []).map((n: string) => <code key={n} className="rounded-md bg-line/10 px-1.5 py-0.5 text-[10px]">{n}</code>)}</div>
                    <div className="mt-3 text-[11px] text-muted">مجوز Harness: {CAT[t.harness_category] || t.harness_category} · به‌روزرسانی {relTime(t.updated_at)}</div>
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      <Button size="sm" variant="soft" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => action(t, "update")}>به‌روزرسانی</Button>
                      <Button size="sm" variant="outline" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => action(t, "rollback")}>نسخه قبلی</Button>
                      <Button size="sm" variant="ghost" icon={<History className="h-3.5 w-3.5" />} onClick={() => setVersions(t)}>نسخه‌ها</Button>
                      <Button size="sm" variant="ghost" icon={<Power className="h-3.5 w-3.5" />} onClick={() => action(t, t.enabled ? "disable" : "enable")}>{t.enabled ? "غیرفعال" : "فعال"}</Button>
                      <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={async () => { if (!confirm("حذف شود؟")) return; await del(`/api/tools/${t.id}`); qc.invalidateQueries({ queryKey: ["tools"] }); }} />
                    </div>
                  </Card>
                </motion.div>
              ))}
            </div>
          )
        )}
        {tab === "builtin" && !isLoading && (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Object.entries(groups).map(([g, list]) => (
              <Card key={g} className="p-4">
                <h3 className="mb-3 flex items-center justify-between font-semibold">{g}<Badge>{faInt(list.length)}</Badge></h3>
                <div className="space-y-2.5">
                  {list.map((t) => (
                    <div key={t.name}>
                      <div className="flex items-center justify-between gap-2"><span className="text-sm">{t.label}</span>{t.category && <Badge tone="warning">{CAT[t.category] || t.category}</Badge>}</div>
                      <p className="mt-0.5 text-[11px] leading-5 text-muted">{t.description}</p>
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
      {versions && <Versions tool={versions} onClose={() => setVersions(null)} />}
    </div>
  );
}
