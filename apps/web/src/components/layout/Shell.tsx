/** پوسته اصلی پنل «سیلیکون زنده»: ریل کناری (دسکتاپ)، داک پایین (موبایل)، نوار بالا با وضعیت زنده، مصرف توکن و بار سیستم. */

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
import { post } from "../../lib/api";
import { compact, faNum, relTime, usd } from "../../lib/format";
import { useLive, useLiveStatus } from "../../lib/live";
import { useSheen } from "../../lib/sheen";
import { toast, toastError, useTheme } from "../../lib/store";
import { Badge, Button, cn, Drawer } from "../ui";
import { type AgentState, ChipMark } from "../viz/Silicon";

export const NAV = [
  { to: "/", label: "داشبورد", icon: LayoutDashboard, group: "work" },
  { to: "/chat", label: "گفتگو", icon: MessageSquare, group: "work" },
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
    if (["run.status", "run.finished", "approval.created", "approval.decided", "notification", "system.stop_all", "task.changed", "routine.changed", "desktop.takeover"].includes(t)) {
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

function SideNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="space-y-5" aria-label="بخش‌ها">
      {GROUPS.map((g) => (
        <div key={g.id}>
          <div className="mb-1.5 px-3 text-[11px] font-medium text-muted/80">{g.label}</div>
          <div className="space-y-0.5">
            {NAV.filter((n) => n.group === g.id).map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    "group relative flex h-10 items-center gap-3 px-3 text-sm transition-colors duration-150",
                    isActive ? "text-ink" : "text-muted hover:text-ink",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.span layoutId="nav-active" className="die die-sm die-2 absolute inset-0" transition={{ type: "spring", stiffness: 600, damping: 46 }}>
                        <span className="film-bar absolute inset-y-2 start-0 w-[2px]" />
                      </motion.span>
                    )}
                    {!isActive && <span className="absolute inset-0 rounded-[5px] bg-line/0 transition-colors group-hover:bg-line/[.07]" />}
                    <n.icon className={cn("relative h-[18px] w-[18px] shrink-0", isActive && "text-primary")} strokeWidth={1.8} />
                    <span className="relative truncate">{n.label}</span>
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

/** نوار پدی کوچک ۸ قطعه‌ای برای نوار بالا */
function PadBar({ value, tone }: { value: number; tone: string }) {
  const v = Math.max(0, Math.min(100, value || 0));
  const lit = Math.round(v / 12.5);
  const c = v > 85 ? "bg-danger" : v > 65 ? "bg-warning" : tone;
  return (
    <span className="flex gap-[2px]" dir="ltr" aria-hidden>
      {Array.from({ length: 8 }, (_, i) => (
        <span key={i} className={cn("h-2.5 w-[5px] rounded-[1px] transition-colors duration-300", i < lit ? c : "bg-line/15")} />
      ))}
    </span>
  );
}

function TokenMeter({ d }: { d: any }) {
  const today = d?.usage?.today;
  if (!today) return null;
  const tokens = (today.tokens_in || 0) + (today.tokens_out || 0);
  const pct = today.limit ? Math.min(100, (today.cost / today.limit) * 100) : 0;
  return (
    <div className="bevel hidden h-10 items-center gap-2.5 bg-surface/70 px-3 ring-1 ring-inset ring-line/15 lg:flex" title="مصرف امروز نسبت به سقف روزانه">
      <Zap className="h-4 w-4 text-primary" strokeWidth={1.8} />
      <div className="text-[11px] leading-4">
        <div className="num font-semibold">{compact(tokens)} توکن</div>
        <div className="num text-muted">{usd(today.cost, 3)} از {usd(today.limit, 2)}</div>
      </div>
      <PadBar value={pct} tone="bg-primary" />
    </div>
  );
}

function LoadMeter({ d }: { d: any }) {
  const [m, setM] = useState<any>(null);
  useLive((x, t) => t === "system.metrics" && setM(x));
  const s = m || d?.system;
  if (!s) return null;
  return (
    <div className="bevel hidden h-10 items-center gap-3 bg-surface/70 px-3 ring-1 ring-inset ring-line/15 md:flex" title="بار لحظه‌ای سیستم">
      <Cpu className="h-4 w-4 text-cyan" strokeWidth={1.8} />
      <div className="grid gap-1 text-[10px] leading-3 text-muted">
        <span className="flex items-center gap-2"><span className="w-7">CPU</span><PadBar value={s.cpu} tone="bg-cyan" /><span className="num w-8 text-ink">{faNum(s.cpu)}٪</span></span>
        <span className="flex items-center gap-2"><span className="w-7">RAM</span><PadBar value={s.mem} tone="bg-accent" /><span className="num w-8 text-ink">{faNum(s.mem)}٪</span></span>
      </div>
    </div>
  );
}

function Notifications({ d }: { d: any }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const nav = useNavigate();
  useLive((x, t) => {
    if (t === "notification") toast(x.title, x.level === "error" ? "error" : x.level === "success" ? "success" : x.level === "warning" || x.level === "approval" ? "warning" : "info");
  });
  const unread = d?.notifications?.unread || 0;
  const items: any[] = d?.notifications?.items || [];
  return (
    <div className="relative">
      <Button variant="ghost" size="icon" onClick={() => setOpen((o) => !o)} aria-label="اعلان‌ها">
        <Bell className="h-[18px] w-[18px]" />
        {unread > 0 && <span className="num absolute -end-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-[3px] bg-danger px-1 text-[9px] font-bold text-white">{faNum(Math.min(unread, 99))}</span>}
      </Button>
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.18 }}
              className="die die-2 fixed end-3 top-14 z-50 w-[min(94vw,380px)] shadow-lift sm:absolute sm:end-0 sm:top-11">
              <div className="flex items-center justify-between border-b hairline px-4 py-3">
                <span className="text-sm font-semibold">اعلان‌ها</span>
                <button className="text-xs text-primary" onClick={async () => { await post("/api/notifications/read-all"); qc.invalidateQueries({ queryKey: ["dashboard"] }); }}>
                  خواندن همه
                </button>
              </div>
              <div className="max-h-96 overflow-y-auto">
                {items.length === 0 && <p className="px-4 py-8 text-center text-sm text-muted">اعلانی نیست</p>}
                {items.map((n) => (
                  <button key={n.id} onClick={() => { setOpen(false); if (n.link) nav(n.link); }}
                    className={cn("block w-full border-b hairline px-4 py-3 text-start transition-colors hover:bg-line/8", !n.read && "bg-primary/5")}>
                    <div className="flex items-center gap-2">
                      <span className={cn("pad", { info: "text-primary", success: "text-success", warning: "text-warning", error: "text-danger", approval: "text-warning" }[n.level as string] || "text-primary")} />
                      <span className="truncate text-sm font-medium">{n.title}</span>
                    </div>
                    {n.body && <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted">{n.body}</p>}
                    <p className="mt-1 text-[10px] text-muted">{relTime(n.ts)}</p>
                  </button>
                ))}
              </div>
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

  useSheen();

  return (
    <div className="min-h-dvh">
      <div className="app-bg" />
      {/* ریل کناری دسکتاپ (سمت راست در RTL) */}
      <aside className="fixed inset-y-0 start-0 z-30 hidden w-[256px] flex-col border-e hairline bg-bg2/90 lg:flex">
        <div className="flex h-16 items-center gap-3 border-b hairline px-5">
          <ChipMark state={state} size={34} />
          <div className="min-w-0">
            <div className="ltr text-[17px] font-extrabold leading-5 tracking-[-0.02em]">X Agent</div>
            <div className="flex items-center gap-1.5 text-[11px] text-muted">
              <span className={cn("pad", live === "open" ? "text-success" : "text-warning")} />
              {STATE_LABEL[state]}
              {live !== "open" && <span>· اتصال زنده قطع</span>}
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-4"><SideNav /></div>
        <div className="space-y-2 border-t hairline p-3">
          <Button variant={stopAll ? "soft" : "danger"} className="w-full" onClick={emergency} icon={stopAll ? <Play className="h-4 w-4" /> : <OctagonX className="h-4 w-4" />}>
            {stopAll ? "ادامه همه کارها" : "توقف اضطراری"}
          </Button>
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
        </div>
      </aside>

      <div className="lg:ps-[256px]">
        {/* نوار بالا */}
        <header className="sticky top-0 z-20 border-b hairline bg-bg/95">
          <div className="mx-auto flex h-16 max-w-[1500px] items-center gap-3 px-4 sm:px-6">
            <div className="flex items-center gap-2 lg:hidden">
              <ChipMark state={state} size={28} />
            </div>
            <div className="flex min-w-0 items-center gap-2.5">
              <h1 className="truncate text-base font-semibold">{current?.label || "X Agent"}</h1>
              <Badge tone={state === "stopped" ? "danger" : state === "waiting" ? "warning" : state === "idle" ? "success" : "primary"} dot className="hidden sm:inline-flex">
                {STATE_LABEL[state]}
              </Badge>
              {d?.approvals_pending > 0 && (
                <button onClick={() => nav("/chat")} className="shrink-0"><Badge tone="warning" dot>{faNum(d.approvals_pending)} درخواست اجازه</Badge></button>
              )}
            </div>
            <div className="ms-auto flex items-center gap-1.5 sm:gap-2">
              <TokenMeter d={d} />
              <LoadMeter d={d} />
              <Button variant="ghost" size="icon" onClick={toggle} aria-label={theme === "dark" ? "حالت روشن" : "حالت تاریک"}>
                <AnimatePresence mode="wait" initial={false}>
                  <motion.span key={theme} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.15 }}>
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
            <div className="flex items-center justify-center gap-2 border-t border-danger/25 bg-danger/10 px-4 py-1.5 text-xs text-danger" role="status">
              <OctagonX className="h-3.5 w-3.5" /> توقف اضطراری فعال است؛ هیچ کار یا روتینی اجرا نمی‌شود.
            </div>
          )}
        </header>

        <main className="mx-auto max-w-[1500px] px-4 pb-[calc(6.5rem+env(safe-area-inset-bottom))] pt-5 sm:px-6 lg:pb-10">
          <AnimatePresence mode="wait">
            <motion.div key={loc.pathname.split("/")[1]} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}>
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>

      {/* داک پایین موبایل */}
      <nav className="fixed inset-x-0 bottom-0 z-30 border-t hairline bg-bg2/95 lg:hidden" style={{ paddingBottom: "env(safe-area-inset-bottom)" }} aria-label="ناوبری اصلی">
        <div className="mx-auto grid h-16 max-w-lg grid-cols-5 items-stretch">
          {[NAV[0], NAV[1]].map((n) => <DockItem key={n.to} to={n.to} label={n.label} icon={n.icon} end={n.to === "/"} />)}
          <button onClick={() => nav("/chat?voice=1")} className="relative grid place-items-center" aria-label="گفتگوی صوتی با ایجنت">
            <span className="die die-sm die-live grid h-12 w-12 -translate-y-3 place-items-center">
              <Mic className="h-5 w-5 text-ink" />
            </span>
          </button>
          <DockItem to="/control" label="کنترل" icon={CircuitBoard} />
          <button onClick={() => setMenu(true)} className="flex flex-col items-center justify-center gap-1 text-[10px] text-muted" aria-label="همه بخش‌ها">
            <Menu className="h-5 w-5" strokeWidth={1.8} />بیشتر
          </button>
        </div>
      </nav>

      <Drawer open={menu} onClose={() => setMenu(false)} title={<span className="flex items-center gap-2.5"><ChipMark state={state} size={26} /> <span className="ltr font-extrabold">X Agent</span></span>}>
        <SideNav onNavigate={() => setMenu(false)} />
        <div className="mt-5 space-y-2 border-t hairline pt-4">
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
          <p className="flex items-center justify-center gap-1.5 text-[11px] text-muted"><Activity className="h-3 w-3" /> اتصال زنده: {live === "open" ? "برقرار" : "در حال اتصال"}</p>
        </div>
      </Drawer>
    </div>
  );
}

function DockItem({ to, label, icon: Icon, end }: { to: string; label: string; icon: any; end?: boolean }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => cn("relative flex flex-col items-center justify-center gap-1 text-[10px]", isActive ? "text-ink" : "text-muted")}>
      {({ isActive }) => (
        <>
          {isActive && <motion.span layoutId="dock-active" className="film-bar absolute inset-x-5 top-0 h-[2px]" transition={{ type: "spring", stiffness: 600, damping: 44 }} />}
          <Icon className={cn("h-5 w-5", isActive && "text-primary")} strokeWidth={1.8} />
          <span className="max-w-full truncate px-1">{label.split(" ")[0]}</span>
        </>
      )}
    </NavLink>
  );
}
