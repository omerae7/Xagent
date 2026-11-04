import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Activity, CheckCircle2, Coins, Cpu, Gauge as GaugeIcon, Layers, Timer, Zap } from "lucide-react";
import { useState } from "react";
import { Badge, Card, CardHeader, ErrorBox, PageHeader, Segmented, Skeleton } from "../components/ui";
import { AreaChart, CHART, Donut, Gauge, Meter } from "../components/viz/charts";
import { get } from "../lib/api";
import { compact, faInt, faNum, fmtDate, fmtTime, toFaDigits, usd } from "../lib/format";
import { useLiveMetrics } from "../lib/live";

const PURPOSE: Record<string, string> = {
  execution: "اجرا", management: "مدیریت", goal: "هدف‌سنجی", planning: "برنامه‌ریزی", evaluation: "ارزیابی", vision: "بینایی", memory: "حافظه", dream: "رویا",
};
const COLORS = ["rgb(var(--primary))", "rgb(var(--accent))", "rgb(var(--cyan))", "rgb(var(--warning))", "rgb(var(--success))", "rgb(var(--danger))", "#94a3b8", "#f472b6"];
const RUN_ST: Record<string, [string, any]> = { succeeded: ["موفق", "success"], failed: ["ناموفق", "danger"], paused: ["متوقف", "warning"], stopped: ["لغو", "neutral"], cancelled: ["لغو", "neutral"], running: ["در حال اجرا", "primary"], queued: ["صف", "neutral"], waiting_approval: ["منتظر اجازه", "warning"] };

export default function Usage() {
  const [days, setDays] = useState<"7" | "30" | "90">("30");
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["usage", days], queryFn: () => get(`/api/usage?days=${days}`) });
  const sys = useQuery({ queryKey: ["metrics"], queryFn: () => get("/api/system/metrics?hours=24"), refetchInterval: 60000 });
  const live = useLiveMetrics(sys.data?.series || [], 300);
  const totals = (data?.daily || []).reduce((a: any, d: any) => ({ in: a.in + d.tokens_in, out: a.out + d.tokens_out, cache: a.cache + d.cache_read, cost: a.cost + d.cost, calls: a.calls + d.calls }), { in: 0, out: 0, cache: 0, cost: 0, calls: 0 });
  const runsTotal = (data?.runs || []).reduce((a: number, r: any) => a + r.count, 0);
  const avgSec = (data?.runs || []).find((r: any) => r.status === "succeeded")?.avg_seconds;
  const cur = sys.data?.current || {};
  const cacheRate = totals.in ? (totals.cache / totals.in) * 100 : 0;
  // روزهای بدون مصرف هم نمایش داده می‌شوند
  const daily = (() => {
    const map = new Map<string, any>((data?.daily || []).map((d: any) => [d.day, d]));
    const n = Number(days);
    const out = [];
    for (let i = n - 1; i >= 0; i--) {
      const dt = new Date(Date.now() - i * 86400_000);
      const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      out.push(map.get(key) || { day: key, tokens_in: 0, tokens_out: 0, cache_read: 0, cost: 0, calls: 0 });
    }
    return out;
  })();

  return (
    <div>
      <PageHeader title="ارزیابی و مصرف" sub="توکن‌ها، هزینه، نرخ موفقیت، زمان اجرا و درگیری سیستم" icon={<GaugeIcon className="h-5 w-5" />}
        actions={<Segmented value={days} onChange={setDays} options={[{ value: "7", label: "۷ روز" }, { value: "30", label: "۳۰ روز" }, { value: "90", label: "۹۰ روز" }]} />} />
      {error && <ErrorBox error={error} retry={refetch} />}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        {[
          { l: "کل توکن", v: compact(totals.in + totals.out), s: `ورودی ${compact(totals.in)} · خروجی ${compact(totals.out)}`, i: <Zap className="h-4 w-4" />, c: "text-primary" },
          { l: "هزینه", v: usd(totals.cost, 3), s: `${faInt(totals.calls)} فراخوانی مدل`, i: <Coins className="h-4 w-4" />, c: "text-warning" },
          { l: "نرخ موفقیت", v: data?.success_rate != null ? `${faNum(data.success_rate * 100)}٪` : "—", s: `${faInt(runsTotal)} اجرا`, i: <CheckCircle2 className="h-4 w-4" />, c: "text-success" },
          { l: "میانگین زمان اجرا", v: avgSec ? `${faNum(avgSec, 1)} ث` : "—", s: "کارهای موفق", i: <Timer className="h-4 w-4" />, c: "text-cyan" },
          { l: "صرفه‌جویی کش", v: `${faNum(cacheRate)}٪`, s: `${compact(totals.cache)} توکن از کش`, i: <Layers className="h-4 w-4" />, c: "text-accent" },
        ].map((x, i) => (
          <motion.div key={x.l} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.05 }} className="die p-4">
            <div className="flex items-center justify-between text-xs text-muted">{x.l}<span className={x.c}>{x.i}</span></div>
            {isLoading ? <Skeleton className="mt-2 h-7" /> : <div className="num mt-1.5 text-2xl font-bold">{x.v}</div>}
            <div className="mt-0.5 truncate text-[11px] text-muted">{x.s}</div>
          </motion.div>
        ))}
      </div>

      <div className="mt-5 grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title="مصرف روزانه توکن" icon={<Zap className="h-4 w-4" />} sub="ورودی، خروجی و خوانده‌شده از کش" />
          <div className="px-3 pb-4 pt-2">
            {isLoading ? <Skeleton className="h-52" /> : <AreaChart data={daily} height={230} format={compact}
              series={[{ key: "tokens_in", label: "ورودی", color: CHART.in }, { key: "tokens_out", label: "خروجی", color: CHART.out }, { key: "cache_read", label: "کش", color: CHART.cache }]}
              xLabel={(d) => fmtDate(d.day, false)} />}
          </div>
        </Card>
        <Card>
          <CardHeader title="هزینه به تفکیک کاربرد" icon={<Activity className="h-4 w-4" />} />
          <div className="flex flex-col items-center gap-4 p-4">
            <Donut size={150} items={(data?.by_purpose || []).map((p: any, i: number) => ({ label: p.purpose, value: p.tokens, color: COLORS[i % COLORS.length] }))}
              center={<div><div className="num text-lg font-bold">{compact((data?.by_purpose || []).reduce((a: number, p: any) => a + p.tokens, 0))}</div><div className="text-[10px] text-muted">توکن</div></div>} />
            <div className="w-full space-y-1.5">
              {(data?.by_purpose || []).map((p: any, i: number) => (
                <div key={p.purpose} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-[2px]" style={{ background: COLORS[i % COLORS.length] }} />{PURPOSE[p.purpose] || p.purpose}</span>
                  <span className="num text-muted">{compact(p.tokens)} · {usd(p.cost)}</span>
                </div>
              ))}
            </div>
          </div>
        </Card>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2 overflow-hidden">
          <CardHeader title="مصرف به تفکیک مدل" icon={<Layers className="h-4 w-4" />} />
          <div className="overflow-x-auto p-2">
            <table className="w-full min-w-[640px] text-sm">
              <thead><tr className="text-xs text-muted">{["مدل", "ورودی", "خروجی", "کش", "هزینه", "فراخوانی", "تأخیر", "خطا"].map((h) => <th key={h} className="px-3 py-2 text-start font-medium">{h}</th>)}</tr></thead>
              <tbody>
                {(data?.by_model || []).map((m: any) => (
                  <tr key={m.model + m.provider} className="border-t hairline">
                    <td className="px-3 py-2"><div className="font-medium">{m.model}</div><div className="text-[10px] text-muted">{m.provider}</div></td>
                    <td className="num px-3 py-2">{compact(m.tokens_in)}</td>
                    <td className="num px-3 py-2">{compact(m.tokens_out)}</td>
                    <td className="num px-3 py-2">{compact(m.cache_read)}</td>
                    <td className="num px-3 py-2">{usd(m.cost)}</td>
                    <td className="num px-3 py-2">{faInt(m.calls)}</td>
                    <td className="num px-3 py-2">{faNum(m.avg_latency_ms / 1000, 1)}ث</td>
                    <td className="px-3 py-2">{m.errors > 0 ? <Badge tone="danger">{faInt(m.errors)}</Badge> : <span className="text-muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(data?.by_model || []).length === 0 && !isLoading && <p className="py-8 text-center text-sm text-muted">هنوز مصرفی ثبت نشده است.</p>}
          </div>
        </Card>
        <Card>
          <CardHeader title="وضعیت اجراها" icon={<CheckCircle2 className="h-4 w-4" />} />
          <div className="space-y-3 p-4">
            {(data?.runs || []).map((r: any) => (
              <div key={r.status}>
                <div className="mb-1 flex justify-between text-xs"><Badge tone={RUN_ST[r.status]?.[1]}>{RUN_ST[r.status]?.[0] || r.status}</Badge><span className="num text-muted">{faInt(r.count)} · میانگین {compact(r.avg_tokens)} توکن</span></div>
                <Meter thresholds={false} value={r.count} max={runsTotal || 1} tone={r.status === "succeeded" ? "success" : r.status === "failed" ? "danger" : "primary"} />
              </div>
            ))}
            {(data?.by_trigger || []).length > 0 && (
              <div className="border-t hairline pt-3">
                <p className="mb-2 text-xs text-muted">منبع درخواست‌ها</p>
                <div className="flex flex-wrap gap-1.5">{data.by_trigger.map((t: any) => <Badge key={t.trigger} tone="primary">{t.trigger}: {faInt(t.count)}</Badge>)}</div>
              </div>
            )}
          </div>
        </Card>
      </div>

      <Card className="mt-4">
        <CardHeader title="درگیری سیستم (۲۴ ساعت)" icon={<Cpu className="h-4 w-4" />} sub={`${toFaDigits(cur.cpu_count || 0)} هسته · بار ${faNum(cur.load1, 2)}`} />
        <div className="grid gap-4 p-4 lg:grid-cols-[1fr_auto]">
          <AreaChart data={live} height={200} max={100} format={(v) => `${faNum(v)}٪`}
            series={[{ key: "cpu", label: "CPU", color: CHART.cpu }, { key: "mem", label: "RAM", color: CHART.mem }, { key: "disk", label: "دیسک", color: CHART.disk }]}
            xLabel={(d) => fmtTime(d.ts)} />
          <div className="grid grid-cols-3 gap-2 lg:grid-cols-1">
            <Gauge value={cur.cpu} label="CPU" size={90} />
            <Gauge value={cur.mem} label="RAM" size={90} color="rgb(var(--accent))" />
            <Gauge value={cur.swap} label="Swap" size={90} color="rgb(var(--warning))" />
          </div>
        </div>
      </Card>

      {(data?.top_runs || []).length > 0 && (
        <Card className="mt-4">
          <CardHeader title="پرهزینه‌ترین اجراها" icon={<Coins className="h-4 w-4" />} />
          <div className="divide-y hairline p-2">
            {data.top_runs.map((r: any) => (
              <div key={r.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <span className="num text-xs text-muted">{fmtDate(r.created_at)}</span>
                <Badge tone={RUN_ST[r.status]?.[1]}>{RUN_ST[r.status]?.[0] || r.status}</Badge>
                <span className="num ms-auto text-xs">{compact(r.tokens_in + r.tokens_out)} توکن · {faInt(r.steps_count)} گام · {usd(r.cost_usd)}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
