/** پوسته اصلی پنل: نوار کناری (دسکتاپ)، نوار پایین (موبایل)، نوار بالا با وضعیت زنده، مصرف توکن و بار سیستم. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  Bell,
  Bot,
  Brain,
  CalendarCheck,
  Cpu,
  Gauge as GaugeIcon,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
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
import { toast, toastError, useTheme } from "../../lib/store";
import { Badge, Button, cn, Drawer } from "../ui";
import { Orb, type OrbState } from "../viz/Orb";

export const NAV = [
  { to: "/", label: "داشبورد", icon: LayoutDashboard },
  { to: "/chat", label: "گفتگو", icon: MessageSquare },
  { to: "/tasks", label: "وظایف", icon: CalendarCheck },
  { to: "/routines", label: "روتین‌ها", icon: Repeat },
  { to: "/crm", label: "همکاران و CRM", icon: Users },
  { to: "/desktop", label: "دسکتاپ زنده", icon: Monitor },
  { to: "/tools", label: "ابزارها و مهارت‌ها", icon: Wrench },
  { to: "/memory", label: "حافظه و بازگشت", icon: Brain },
  { to: "/dream", label: "رویا و خودبهبودی", icon: Sparkles },
  { to: "/usage", label: "ارزیابی و مصرف", icon: GaugeIcon },
  { to: "/settings", label: "تنظیمات", icon: Settings },
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

export function agentState(d: any): OrbState {
  if (!d) return "idle";
  if (d.agent?.stop_all) return "stopped";
  return (d.agent?.state as OrbState) || "idle";
}

function SideNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="space-y-1">
      {NAV.map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.to === "/"}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "group relative flex h-11 items-center gap-3 rounded-xl px-3 text-sm transition-colors",
              isActive ? "text-ink" : "text-muted hover:bg-line/8 hover:text-ink",
            )
          }
        >
          {({ isActive }) => (
            <>
              {isActive && (
                <motion.span layoutId="nav-active" className="absolute inset-0 rounded-xl bg-gradient-to-l from-primary/18 to-accent/8 ring-1 ring-primary/25" transition={{ type: "spring", stiffness: 500, damping: 40 }} />
              )}
              {isActive && <motion.span layoutId="nav-bar" className="absolute -start-1 top-2.5 h-6 w-1 rounded-full bg-primary shadow-[0_0_12px_rgb(var(--primary))]" />}
              <n.icon className={cn("relative h-[18px] w-[18px]", isActive && "text-primary")} />
              <span className="relative">{n.label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function TokenMeter({ d }: { d: any }) {
  const today = d?.usage?.today;
  if (!today) return null;
  const tokens = (today.tokens_in || 0) + (today.tokens_out || 0);
  const pct = today.limit ? Math.min(100, (today.cost / today.limit) * 100) : 0;
  return (
    <div className="hidden items-center gap-2.5 rounded-xl border hairline bg-surface/40 px-3 py-1.5 lg:flex" title="مصرف امروز">
      <Zap className="h-4 w-4 text-primary" />
      <div className="text-[11px] leading-4">
        <div className="num font-semibold">{compact(tokens)} توکن</div>
        <div className="num text-muted">{usd(today.cost, 3)} از {usd(today.limit, 2)}</div>
      </div>
      <div className="h-8 w-1.5 overflow-hidden rounded-full bg-line/15">
        <motion.div className={cn("w-full rounded-full", pct > 85 ? "bg-danger" : pct > 60 ? "bg-warning" : "bg-primary")} initial={{ height: 0 }} animate={{ height: `${pct}%`, marginTop: `${100 - pct}%` }} />
      </div>
    </div>
  );
}

function LoadMeter({ d }: { d: any }) {
  const [m, setM] = useState<any>(null);
  useLive((x, t) => t === "system.metrics" && setM(x));
  const s = m || d?.system;
  if (!s) return null;
  const bars = [
    { k: "CPU", v: s.cpu, c: "bg-cyan" },
    { k: "RAM", v: s.mem, c: "bg-accent" },
    { k: "DSK", v: s.disk, c: "bg-primary" },
  ];
  return (
    <div className="hidden items-center gap-2 rounded-xl border hairline bg-surface/40 px-3 py-1.5 md:flex" title="بار سیستم">
      <Cpu className="h-4 w-4 text-cyan" />
      <div className="flex items-end gap-1.5">
        {bars.map((b) => (
          <div key={b.k} className="flex flex-col items-center gap-0.5">
            <div className="flex h-6 w-2 items-end overflow-hidden rounded-full bg-line/15">
              <motion.div className={cn("w-full rounded-full", b.v > 85 ? "bg-danger" : b.c)} animate={{ height: `${Math.max(4, b.v || 0)}%` }} transition={{ type: "spring", stiffness: 80, damping: 15 }} />
            </div>
            <span className="text-[8px] text-muted">{b.k}</span>
          </div>
        ))}
      </div>
      <div className="num text-[11px] leading-4 text-muted">
        <div>{faNum(s.cpu)}٪ CPU</div>
        <div>{faNum(s.mem)}٪ RAM</div>
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
        {unread > 0 && <span className="num absolute -end-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-danger px-1 text-[9px] font-bold text-white">{faNum(Math.min(unread, 99))}</span>}
      </Button>
      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -6 }}
              className="glass-strong absolute end-0 top-11 z-50 w-[min(92vw,360px)] overflow-hidden rounded-2xl shadow-2xl">
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
                    className={cn("block w-full border-b hairline px-4 py-3 text-start transition hover:bg-line/8", !n.read && "bg-primary/5")}>
                    <div className="flex items-center gap-2">
                      <span className={cn("h-2 w-2 shrink-0 rounded-full", { info: "bg-primary", success: "bg-success", warning: "bg-warning", error: "bg-danger", approval: "bg-warning" }[n.level as string] || "bg-primary")} />
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

  return (
    <div className="min-h-dvh">
      <div className="app-bg" />
      {/* نوار کناری دسکتاپ (سمت راست در RTL) */}
      <aside className="fixed inset-y-0 start-0 z-30 hidden w-[264px] flex-col border-e hairline bg-bg2/40 p-4 backdrop-blur-xl lg:flex">
        <div className="mb-6 flex items-center gap-3 px-1">
          <Orb state={state} size={46} />
          <div>
            <div className="text-lg font-extrabold tracking-tight"><span className="text-gradient">X</span> Agent</div>
            <div className="flex items-center gap-1.5 text-[11px] text-muted">
              <span className={cn("h-1.5 w-1.5 rounded-full", live === "open" ? "bg-success shadow-[0_0_8px_rgb(var(--success))]" : "bg-warning")} />
              {STATE_LABEL[state]}
            </div>
          </div>
        </div>
        <div className="-mx-1 flex-1 overflow-y-auto px-1"><SideNav /></div>
        <div className="mt-4 space-y-2 border-t hairline pt-4">
          <Button variant={stopAll ? "soft" : "danger"} className="w-full" onClick={emergency} icon={stopAll ? <Play className="h-4 w-4" /> : <OctagonX className="h-4 w-4" />}>
            {stopAll ? "ادامه همه کارها" : "توقف اضطراری"}
          </Button>
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
        </div>
      </aside>

      <div className="lg:ps-[264px]">
        {/* نوار بالا */}
        <header className="sticky top-0 z-20 border-b hairline bg-bg/55 backdrop-blur-xl">
          <div className="mx-auto flex h-16 max-w-[1500px] items-center gap-3 px-4 sm:px-6">
            <button className="lg:hidden" onClick={() => setMenu(true)} aria-label="منو"><Menu className="h-5 w-5" /></button>
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate text-base font-semibold">{current?.label || "X Agent"}</span>
              <Badge tone={state === "stopped" ? "danger" : state === "waiting" ? "warning" : state === "idle" ? "success" : "primary"} dot className="hidden sm:inline-flex">
                {STATE_LABEL[state]}
              </Badge>
              {d?.approvals_pending > 0 && (
                <button onClick={() => nav("/chat")}><Badge tone="warning" dot>{faNum(d.approvals_pending)} درخواست اجازه</Badge></button>
              )}
            </div>
            <div className="ms-auto flex items-center gap-2">
              <TokenMeter d={d} />
              <LoadMeter d={d} />
              <Button variant="ghost" size="icon" onClick={toggle} aria-label="تغییر تم">
                <AnimatePresence mode="wait" initial={false}>
                  <motion.span key={theme} initial={{ rotate: -90, opacity: 0 }} animate={{ rotate: 0, opacity: 1 }} exit={{ rotate: 90, opacity: 0 }}>
                    {theme === "dark" ? <Sun className="h-[18px] w-[18px]" /> : <Moon className="h-[18px] w-[18px]" />}
                  </motion.span>
                </AnimatePresence>
              </Button>
              <Notifications d={d} />
              <Button variant={stopAll ? "soft" : "danger"} size="icon" className="lg:hidden" onClick={emergency} aria-label="توقف اضطراری">
                {stopAll ? <Play className="h-4 w-4" /> : <OctagonX className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          {stopAll && (
            <div className="border-t border-danger/25 bg-danger/10 px-4 py-1.5 text-center text-xs text-danger">
              ⛔ توقف اضطراری فعال است — هیچ کار یا روتینی اجرا نمی‌شود.
            </div>
          )}
        </header>

        <main className="mx-auto max-w-[1500px] px-4 pb-28 pt-5 sm:px-6 lg:pb-10">
          <AnimatePresence mode="wait">
            <motion.div key={loc.pathname.split("/")[1]} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.25 }}>
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>

      {/* نوار پایین موبایل */}
      <nav className="glass-strong fixed inset-x-3 bottom-3 z-30 flex h-16 items-center justify-around rounded-3xl px-2 shadow-2xl lg:hidden" style={{ paddingBottom: "env(safe-area-inset-bottom)" }}>
        {[NAV[0], NAV[1]].map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => cn("flex w-14 flex-col items-center gap-0.5 text-[10px]", isActive ? "text-primary" : "text-muted")}>
            <n.icon className="h-5 w-5" />{n.label}
          </NavLink>
        ))}
        <button onClick={() => nav("/chat?voice=1")} className="-mt-9 grid place-items-center rounded-full p-1" aria-label="دستیار صوتی">
          <span className="grid h-16 w-16 place-items-center rounded-full bg-bg ring-4 ring-bg"><Orb state={state} size={60} /></span>
        </button>
        {[NAV[2], NAV[9]].map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => cn("flex w-14 flex-col items-center gap-0.5 text-[10px]", isActive ? "text-primary" : "text-muted")}>
            <n.icon className="h-5 w-5" />{n.label.split(" ")[0]}
          </NavLink>
        ))}
      </nav>

      <Drawer open={menu} onClose={() => setMenu(false)} title={<span className="flex items-center gap-2"><Bot className="h-5 w-5 text-primary" /> X Agent</span>}>
        <SideNav onNavigate={() => setMenu(false)} />
        <div className="mt-4 space-y-2 border-t hairline pt-4">
          <Button variant="ghost" className="w-full" onClick={onLogout} icon={<LogOut className="h-4 w-4" />}>خروج</Button>
          <p className="flex items-center justify-center gap-1.5 text-[11px] text-muted"><Activity className="h-3 w-3" /> اتصال زنده: {live === "open" ? "برقرار" : "در حال اتصال"}</p>
        </div>
      </Drawer>
    </div>
  );
}
