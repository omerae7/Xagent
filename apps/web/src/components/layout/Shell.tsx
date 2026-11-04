/** پوسته اصلی پنل «شیشه موسمی»: ریل شیشه‌ای شناور (دسکتاپ)، داک قرصی (موبایل)، نوار بالا با وضعیت زنده، مصرف توکن، بار سیستم و میز کار. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  Bell,
  Brain,
  CalendarCheck,
  CircuitBoard,
  Cpu,
  Gauge as GaugeIcon,
  Inbox,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  Mic,
  Monitor,
  Moon,
  OctagonX,
  Play,
  Repeat,
  Settings,
  Sparkles,
  Sun,
  Users,
  Wrench,
  Zap,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { get, post } from "../../lib/api";
import { compact, faNum, relTime, usd } from "../../lib/format";
import { useLive, useLiveStatus } from "../../lib/live";
import { toast, toastError, useTheme } from "../../lib/store";
import { Badge, Button, cn, Drawer } from "../ui";
import { type AgentState, ChipMark } from "../viz/Silicon";

export const NAV = [
  { to: "/", label: "داشبورد", icon: LayoutDashboard, group: "work" },
  { to: "/chat", label: "گفتگو", icon: MessageSquare, group: "work" },
  { to: "/desk", label: "میز کار و گزارش‌ها", icon: Inbox, group: "work" },
  { to: "/tasks", label: "وظایف", icon: CalendarCheck, group: "work" },
  { to: "/routines", label: "روتین‌ها", icon: Repeat, group: "work" },
  { to: "/crm", label: "همکاران و CRM", icon: Users, group: "work" },
  { to: "/control", label: "مرکز کنترل مدیر", icon: CircuitBoard, group: "system" },
  { to: "/tools", label: "ابزارها و مهارت‌ها", icon: Wrench, group: "system" },
  { to: "/memory", label: "حافظه و بازگشت", icon: Brain, group: "system" },
  { to: "/dream", label: "رویا و خودبهبودی", icon: Sparkles, group: "system" },
  { to: "/desktop", label: "دسکتاپ زنده", icon: Monitor, group: "system" },
  { to: "/usage", label: "ارزیابی و مصرف", icon: GaugeIcon, group: "system" },
  { to: "/settings", label: "تنظیمات", icon: Settings, group: "system" },
];
const GROUPS = [
  { id: "work", label: "کار" },
  { id: "system", label: "سیستم و مدیر" },
];

const STATE_LABEL: Record<string, string> = {
  idle: "آماده",
  thinking: "در حال فکر",
  executing: "در حال اجرا",
  waiting: "منتظر اجازه",
  stopped: "توقف اضطراری",
};

export function useDashboard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => fetch("/api/dashboard", { credentials: "same-origin" }).then((r) => {
    if (!r.ok) throw new Error("خطا در دریافت داشبورد");
    return r.json();
  }), refetchInterval: 30000 });
  useLive((_d, t) => {
    if (["run.status", "run.finished", "approval.created", "approval.decided", "notification", "system.stop_all", "task.changed", "routine.changed", "desktop.takeover", "inbox.created", "inbox.changed"].includes(t)) {
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    }
  });
  return q;
}

export function agentState(d: any): AgentState {
  if (!d) return "idle";
  if (d.agent?.stop_all) return "stopped";
  return (d.agent?.state as AgentState) || "idle";
}

function SideNav({ onNavigate, badges }: { onNavigate?: () => void; badges?: Record<string, number> }) {
  return (
    <nav className="space-y-5" aria-label="بخش‌ها">
      {GROUPS.map((g) => (
        <div key={g.id}>
          <div className="mb-1.5 px-3 text-[12px] font-medium text-muted">{g.label}</div>
          <div className="space-y-0.5">
            {NAV.filter((n) => n.group === g.id).map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    "group relative flex h-11 items-center gap-3 rounded-full px-3.5 text-[14px] transition-colors duration-150",
                    isActive ? "font-semibold text-ink" : "text-muted hover:text-ink",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span layoutId="nav-active" className="absolute inset-0 rounded-full bg-surface shadow-[0_6px_18px_-10px_rgb(var(--glass-shadow)/.45)] ring-1 ring-inset ring-line/10" transition={{ type: "spring", stiffness: 600, damping: 46 }} />
                    )}
                    {!isActive && <span className="absolute inset-0 rounded-full transition-colors group-hover:bg-line/[.06]" />}
                    <n.icon className={cn("relative h-[18px] w-[18px] shrink-0", isActive && "text-primary")} strokeWidth={1.7} />
                    <span className="relative min-w-0 flex-1 truncate">{n.label}</span>
                    {!!badges?.[n.to] && <span className="num relative grid h-5 min-w-5 place-items-center rounded-full bg-primary px-1.5 text-[11px] font-semibold text-onprimary">{faNum(Math.min(badges[n.to], 99))}</span>}
                  </>
                )}
              </NavLink>
            ))}
          </div>
        </div>
      ))}
    </nav>
  );
}

/** سنج خطی نازک با گره روی مقدار (زبان خط‌نگاره) */
function LineMeter({ value, tone }: { value: number; tone: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  const c = v > 85 ? "rgb(var(--danger))" : v > 65 ? "rgb(var(--warning))" : tone;
  return (
    <span className="relative block h-3 w-16" dir="ltr" aria-hidden>
      <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-line/25" />
      <span className="absolute start-0 top-1/2 h-[2px] -translate-y-1/2 rounded-full transition-[width] duration-500" style={{ width: `${v}%`, background: c }} />
      <span className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-surface transition-[left] duration-500" style={{ left: `${v}%`, boxShadow: `inset 0 0 0 1.5px ${c}` }} />
    </span>
  );
}

function TokenMeter({ d }: { d: any }) {
  const today = d?.usage?.today;
  if (!today) return null;
  const tokens = (today.tokens_in || 0) + (today.tokens_out || 0);
  const pct = today.limit ? Math.min(100, (today.cost / today.limit) * 100) : 0;
  return (
    <div className="hidden h-10 items-center gap-3 rounded-full bg-surface/70 px-4 ring-1 ring-inset ring-line/10 xl:flex" title="مصرف امروز نسبت به سقف روزانه">
      <Zap className="h-4 w-4 text-primary" strokeWidth={1.7} />
      <div className="text-[11.5px] leading-4">
        <div className="num font-semibold">{compact(tokens)} توکن</div>
        <div className="num text-muted">{usd(today.cost, 3)} از {usd(today.limit, 2)}</div>
      </div>
      <LineMeter value={pct} tone="rgb(var(--primary))" />
    </div>
  );
}

function LoadMeter({ d }: { d: any }) {
  const [m, setM] = useState<any>(null);
  useLive((x, t) => t === "system.metrics" && setM(x));
  const s = m || d?.system;
  if (!s) return null;
  return (
    <div className="hidden h-10 items-center gap-3 rounded-full bg-surface/70 px-4 ring-1 ring-inset ring-line/10 md:flex" title="بار لحظه‌ای سیستم">
      <Cpu className="h-4 w-4 text-cyan" strokeWidth={1.7} />
      <div className="grid gap-0.5 text-[10.5px] leading-[14px] text-muted">
        <span className="flex items-center gap-2"><span className="ltr w-7">CPU</span><LineMeter value={s.cpu} tone="rgb(var(--cyan))" /><span className="num w-8 text-ink">{faNum(s.cpu)}٪</span></span>
        <span className="flex items-center gap-2"><span className="ltr w-7">RAM</span><LineMeter value={s.mem} tone="rgb(var(--accent))" /><span className="num w-8 text-ink">{faNum(s.mem)}٪</span></span>
      </div>
    </div>
  );
}

const SEV_TONE: Record<string, string> = { critical: "text-danger", important: "text-warning", info: "text-primary" };

function Notifications({ d }: { d: any }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const nav = useNavigate();
  useLive((x, t) => {
    if (t === "notification") toast(x.title, x.level === "error" ? "error" : x.level === "success" ? "success" : x.level === "warning" || x.level === "approval" ? "warning" : "info");
    if (t === "inbox.created" && x?.severity !== "info") toast(x.title, x.severity === "critical" ? "error" : "info");
  });
  const unread = d?.inbox?.unread || 0;
  const items: any[] = d?.inbox?.items || [];
  return (
    <div className="relative">
      <Button variant="ghost" size="icon" onClick={() => setOpen((o) => !o)} aria-label={`میز کار و گزارش‌ها${unread ? ` — ${unread} خوانده‌نشده` : ""}`}>
        <Bell className="h-[18px] w-[18px]" />
        {unread > 0 && <span className="num absolute end-0.5 top-0.5 grid h-[18px] min-w-[18px] place-items-center rounded-full bg-danger px-1 text-[10px] font-bold text-white ring-2 ring-[rgb(var(--bg))]">{faNum(Math.min(unread, 99))}</span>}
      </Button>
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className="die die-2 glass-blur fixed end-3 top-[4.5rem] z-50 w-[min(94vw,400px)] overflow-hidden sm:absolute sm:end-0 sm:top-12">
              <div className="flex items-center justify-between border-b hairline px-5 py-3.5">
                <span className="text-[15px] font-semibold">گزارش‌های تازه</span>
                <button className="text-[12.5px] text-primary hover:underline" onClick={async () => { await post("/api/inbox/read_all"); qc.invalidateQueries({ queryKey: ["dashboard"] }); qc.invalidateQueries({ queryKey: ["inbox"] }); }}>
                  خواندن همه
                </button>
              </div>
              <div className="max-h-[60vh] overflow-y-auto">
                {items.length === 0 && <p className="px-5 py-10 text-center text-sm text-muted">گزارش تازه‌ای نیست. نتایج روتین‌ها، خطاها و پایان مأموریت‌ها اینجا می‌آیند.</p>}
                {items.map((n) => (
                  <button key={n.id} onClick={() => { setOpen(false); nav(n.link || "/desk"); }}
                    className={cn("block w-full border-b hairline px-5 py-3 text-start transition-colors hover:bg-line/[.05]", !n.read && "bg-primary/[.04]")}>
                    <div className="flex items-center gap-2.5">
                      <span className={cn("pad", SEV_TONE[n.severity] || "text-primary")} />
                      <span className="truncate text-[13.5px] font-medium">{n.title}</span>
                    </div>
                    {n.body && <p className="mt-1 line-clamp-2 ps-[18px] text-[12.5px] leading-5 text-muted">{n.body}</p>}
                    <p className="mt-1 ps-[18px] text-[11px] text-muted">{relTime(n.ts)}</p>
                  </button>
                ))}
              </div>
              <button onClick={() => { setOpen(false); nav("/desk"); }} className="block w-full px-5 py-3 text-center text-[13px] font-medium text-primary hover:bg-line/[.05]">
                باز کردن میز کار
              </button>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  );
}

export function Shell({ children, onLogout }: { children: ReactNode; onLogout: () => void }) {
  const { theme, toggle } = useTheme();
  const { data: d } = useDashboard();
  const live = useLiveStatus();
  const loc = useLocation();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [menu, setMenu] = useState(false);
  const state = agentState(d);
  const current = NAV.find((n) => (n.to === "/" ? loc.pathname === "/" : loc.pathname.startsWith(n.to)));
  const stopAll = d?.agent?.stop_all;

  const emergency = async () => {
    try {
      if (stopAll) {
        await post("/api/settings/system/resume-all");
        toast("سیستم دوباره فعال شد", "success");
      } else {
        if (!window.confirm("همه کارها و روتین‌ها فوراً متوقف شوند؟")) return;
        await post("/api/settings/system/stop-all");
        toast("توقف اضطراری فعال شد", "warning");
      }
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    } catch (e) {
      toastError(e);
    }
  };

  const badges = { "/desk": d?.inbox?.unread || 0, "/chat": d?.approvals_pending || 0 };
  // وضعیت واقعی سرویس‌ها (نه وضعیت ساختگی): API/دیتابیس/Redis و آمادگی حلقه‌های Worker
  const health = useQuery({ queryKey: ["health"], queryFn: () => get("/api/health"), refetchInterval: 30_000, retry: 0 });
  const backendDown = health.isError || (health.data && health.data.ok === false);
  const workerDegraded = !backendDown && health.data?.worker?.ready === false;

  return (
    <div className="min-h-dvh">
      <div className="app-bg" />
      {/* ریل شیشه‌ای شناور دسکتاپ (سمت راست در RTL) */}
      <aside className="die glass-blur fixed inset-y-3 start-3 z-30 hidden w-[252px] flex-col overflow-hidden rounded-[28px] lg:flex">
        <div className="flex h-[72px] items-center gap-3 px-5">
          <ChipMark state={state} size={38} />
          <div className="min-w-0">
            <div className="ltr text-[18px] font-bold leading-6 tracking-[-0.01em]">X Agent</div>
            <div className="flex items-center gap-1.5 text-[12px] text-muted">
              <span className={cn("pad", live === "open" ? "text-success" : "text-warning")} />
              {STATE_LABEL[state]}
              {live !== "open" && <span>· اتصال زنده قطع</span>}
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-3 pb-4 pt-1"><SideNav badges={badges} /></div>
        <div className="space-y-1.5 border-t hairline p-3">
          <Button variant={stopAll ? "soft" : "danger"} className="w-full" onClick={emergency} icon={stopAll ? <Play className="h-4 w-4" /> : <OctagonX className="h-4 w-4" />}>
            {stopAll ? "ادامه همه کارها" : "توقف اضطراری"}
          </Button>
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
        </div>
      </aside>

      <div className="lg:ps-[276px]">
        {/* نوار بالا */}
        <header className="sticky top-0 z-20 px-3 pt-3 sm:px-5">
          <div className="die glass-blur mx-auto flex h-16 max-w-[1480px] items-center gap-3 rounded-full ps-3 pe-2 sm:ps-5">
            <div className="flex items-center gap-2 lg:hidden">
              <ChipMark state={state} size={32} />
            </div>
            <div className="flex min-w-0 items-center gap-2.5">
              <h1 className="truncate text-[16px] font-semibold">{current?.label || "X Agent"}</h1>
              <Badge tone={state === "stopped" ? "danger" : state === "waiting" ? "warning" : state === "idle" ? "success" : "primary"} dot className="hidden sm:inline-flex">
                {STATE_LABEL[state]}
              </Badge>
              {d?.approvals_pending > 0 && (
                <button onClick={() => nav("/desk")} className="shrink-0"><Badge tone="warning" dot>{faNum(d.approvals_pending)} منتظر اجازه</Badge></button>
              )}
            </div>
            <div className="ms-auto flex items-center gap-1 sm:gap-2">
              <TokenMeter d={d} />
              <LoadMeter d={d} />
              <Button variant="ghost" size="icon" onClick={toggle} aria-label={theme === "dark" ? "حالت روشن" : "حالت تاریک"}>
                <AnimatePresence mode="wait" initial={false}>
                  <motion.span key={theme} initial={{ opacity: 0, rotate: -30 }} animate={{ opacity: 1, rotate: 0 }} exit={{ opacity: 0, rotate: 30 }} transition={{ duration: 0.18 }}>
                    {theme === "dark" ? <Sun className="h-[18px] w-[18px]" /> : <Moon className="h-[18px] w-[18px]" />}
                  </motion.span>
                </AnimatePresence>
              </Button>
              <Notifications d={d} />
              <Button variant={stopAll ? "soft" : "danger"} size="icon" className="lg:hidden" onClick={emergency} aria-label={stopAll ? "ادامه همه کارها" : "توقف اضطراری"}>
                {stopAll ? <Play className="h-4 w-4" /> : <OctagonX className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          {stopAll && (
            <div className="mx-auto mt-2 flex max-w-[1480px] items-center justify-center gap-2 rounded-full bg-danger/[.1] px-4 py-2 text-[13px] text-danger ring-1 ring-inset ring-danger/25" role="status">
              <OctagonX className="h-4 w-4" /> توقف اضطراری فعال است؛ هیچ کار یا روتین جدیدی اجرا نمی‌شود.
            </div>
          )}
        </header>

        <main className="mx-auto max-w-[1480px] px-3 pb-[calc(7rem+env(safe-area-inset-bottom))] pt-5 sm:px-5 lg:pb-10">
          {(backendDown || workerDegraded) && (
            <div role="status" className={cn("mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-2xl px-4 py-3 text-[13.5px] leading-6 ring-1 ring-inset",
              backendDown ? "bg-danger/10 text-danger ring-danger/25" : "bg-warning/10 text-warning ring-warning/25")}>
              <span className="font-semibold">{backendDown ? "سرور در دسترس نیست" : "بخشی از پردازش پس‌زمینه از کار افتاده است"}</span>
              <span className="text-ink/80">
                {backendDown
                  ? "داده‌های این صفحه ممکن است قدیمی باشند؛ تا برقراری اتصال، کار تازه ثبت یا اجرا نمی‌شود."
                  : `حلقه‌های ناسالم: ${(health.data?.worker?.degraded || []).map((x: any) => x.loop).join("، ") || "نامشخص"} — روتین‌ها و صف ممکن است با تأخیر اجرا شوند.`}
              </span>
              <button type="button" onClick={() => { health.refetch(); qc.invalidateQueries(); }} className="ms-auto rounded-full px-3 py-1 text-[12.5px] font-medium ring-1 ring-inset ring-current/30 hover:bg-current/10">
                بررسی دوباره
              </button>
            </div>
          )}
          <AnimatePresence mode="wait">
            <motion.div key={loc.pathname.split("/")[1]} initial={{ opacity: 0, y: 6, filter: "blur(4px)" }} animate={{ opacity: 1, y: 0, filter: "blur(0px)" }} exit={{ opacity: 0 }} transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}>
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>

      {/* داک قرصی شناور موبایل */}
      <nav className="fixed inset-x-3 z-30 lg:hidden" style={{ bottom: "calc(12px + env(safe-area-inset-bottom))" }} aria-label="ناوبری اصلی">
        <div className="die glass-blur mx-auto grid h-[66px] max-w-md grid-cols-5 items-stretch rounded-[30px]">
          {[NAV[0], NAV[1]].map((n) => <DockItem key={n.to} to={n.to} label={n.label} icon={n.icon} end={n.to === "/"} />)}
          <button onClick={() => nav("/chat?voice=1")} className="relative grid place-items-center" aria-label="گفتگوی صوتی با ایجنت">
            <span className="grid h-[54px] w-[54px] -translate-y-4 place-items-center rounded-full text-white ring-4 ring-[rgb(var(--bg))]"
              style={{ background: "linear-gradient(135deg, rgb(var(--stage-1)), rgb(var(--stage-3)))", boxShadow: "0 12px 26px -12px rgb(var(--stage-1))" }}>
              <Mic className="h-5 w-5" />
            </span>
          </button>
          <DockItem to="/desk" label="میز کار" icon={Inbox} badge={d?.inbox?.unread || 0} />
          <button onClick={() => setMenu(true)} className="flex flex-col items-center justify-center gap-0.5 text-[11px] text-muted" aria-label="همه بخش‌ها">
            <Menu className="h-5 w-5" strokeWidth={1.7} />بیشتر
          </button>
        </div>
      </nav>

      <Drawer open={menu} onClose={() => setMenu(false)} title={<span className="flex items-center gap-2.5"><ChipMark state={state} size={30} /> <span className="ltr font-bold">X Agent</span></span>}>
        <SideNav onNavigate={() => setMenu(false)} badges={badges} />
        <div className="mt-5 space-y-2 border-t hairline pt-4">
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
          <p className="flex items-center justify-center gap-1.5 text-[12px] text-muted"><Activity className="h-3.5 w-3.5" /> اتصال زنده: {live === "open" ? "برقرار" : "در حال اتصال"}</p>
        </div>
      </Drawer>
    </div>
  );
}

function DockItem({ to, label, icon: Icon, end, badge }: { to: string; label: string; icon: any; end?: boolean; badge?: number }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => cn("relative flex flex-col items-center justify-center gap-0.5 text-[11px]", isActive ? "font-semibold text-primary" : "text-muted")}>
      {({ isActive }) => (
        <>
          <span className="relative">
            <Icon className="h-5 w-5" strokeWidth={1.7} />
            {!!badge && <span className="num absolute -end-2 -top-1.5 grid h-4 min-w-4 place-items-center rounded-full bg-danger px-1 text-[9.5px] font-bold text-white">{faNum(Math.min(badge, 99))}</span>}
          </span>
          <span className="max-w-full truncate px-1">{label.split(" ")[0]}</span>
          {isActive && <motion.span layoutId="dock-active" className="absolute bottom-1.5 h-1 w-1 rounded-full bg-primary" transition={{ type: "spring", stiffness: 600, damping: 44 }} />}
        </>
      )}
    </NavLink>
  );
}
