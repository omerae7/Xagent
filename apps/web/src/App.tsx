import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { Toaster } from "./components/ui";
import { PadArray } from "./components/viz/Silicon";
import { FAILURE_FA, type FailureKind, failureKind, get, post } from "./lib/api";
import { setLocale } from "./lib/format";
import { connectLive, disconnectLive } from "./lib/live";
import Login from "./pages/Login";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Control = lazy(() => import("./pages/Control"));
const Chat = lazy(() => import("./pages/Chat"));
const Tasks = lazy(() => import("./pages/Tasks"));
const Routines = lazy(() => import("./pages/Routines"));
const Desk = lazy(() => import("./pages/Desk"));
const CRM = lazy(() => import("./pages/CRM"));
const Desktop = lazy(() => import("./pages/Desktop"));
const Tools = lazy(() => import("./pages/Tools"));
const Memory = lazy(() => import("./pages/Memory"));
const Dream = lazy(() => import("./pages/Dream"));
const Usage = lazy(() => import("./pages/Usage"));
const Settings = lazy(() => import("./pages/Settings"));

function ServiceUnavailable({ kind, onRetry }: { kind: FailureKind; onRetry: () => void }) {
  const msg = FAILURE_FA[kind as keyof typeof FAILURE_FA] || FAILURE_FA.server;
  return (
    <div className="grid min-h-dvh place-items-center px-4" data-testid="service-unavailable">
      <div className="app-bg" />
      <div className="die glass-blur max-w-md rounded-[28px] p-6 text-center" role="alert">
        <PadArray state="thinking" size={72} />
        <h1 className="mt-3 text-[18px] font-semibold">سرویس در دسترس نیست</h1>
        <p className="mt-2 text-[13.5px] leading-6 text-muted">{msg}. نشست شما معتبر است و پس از برقراری دوباره اتصال، پنل خودکار باز می‌شود.</p>
        <p className="mt-1 text-[12.5px] text-muted">در حال تلاش دوباره…</p>
        <button type="button" onClick={onRetry} className="mt-4 rounded-full px-4 py-1.5 text-[13px] font-medium ring-1 ring-inset ring-current/30 hover:bg-current/10">تلاش دوباره</button>
      </div>
    </div>
  );
}

function Splash() {
  return (
    <div className="grid min-h-[60vh] place-items-center">
      <PadArray state="thinking" size={96} />
    </div>
  );
}

export default function App() {
  const qc = useQueryClient();
  const [authed, setAuthed] = useState<boolean | null>(null);
  // فقط 401 نشست را باطل می‌کند؛ قطع شبکه یا ۵xx «قطعی سرویس» است (کاربر به صفحه ورود فرستاده نمی‌شود)
  const [outage, setOutage] = useState<FailureKind | null>(null);
  const me = useQuery({
    queryKey: ["me"], queryFn: () => get("/api/auth/me"),
    retry: (n, e) => failureKind(e) !== "auth" && n < 1,
    refetchInterval: outage ? 5000 : false,
  });

  useEffect(() => {
    if (me.isSuccess) {
      setAuthed(true);
      setOutage(null);
    } else if (me.isError) {
      const kind = failureKind(me.error);
      if (kind === "auth") {
        setAuthed(false);
        setOutage(null);
      } else {
        setOutage(kind === "client" ? "server" : kind);  // وضعیت ورود همان می‌ماند
      }
    }
  }, [me.isSuccess, me.isError, me.error, me.dataUpdatedAt, me.errorUpdatedAt]);

  useEffect(() => {
    const h = () => setAuthed(false);
    window.addEventListener("xa:unauthorized", h);
    return () => window.removeEventListener("xa:unauthorized", h);
  }, []);

  useEffect(() => {
    if (!authed) {
      disconnectLive();
      return;
    }
    connectLive();
    get("/api/settings").then((s) => setLocale(s.settings.general.timezone, s.settings.general.calendar)).catch(() => undefined);
  }, [authed]);

  if (authed === null && outage) return <ServiceUnavailable kind={outage} onRetry={() => me.refetch()} />;
  if (authed === null) return <div className="grid min-h-dvh place-items-center"><div className="app-bg" /><PadArray state="thinking" size={120} label="در حال بارگذاری X Agent" /></div>;
  if (!authed) return <><Login onLogin={() => { qc.clear(); setAuthed(true); }} /><Toaster /></>;

  const logout = async () => {
    await post("/api/auth/logout").catch(() => undefined);
    qc.clear();
    setAuthed(false);
  };

  return (
    <Shell onLogout={logout}>
      <Suspense fallback={<Splash />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/chat/:id" element={<Chat />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/routines" element={<Routines />} />
          <Route path="/desk" element={<Desk />} />
          <Route path="/crm" element={<CRM />} />
          <Route path="/desktop" element={<Desktop />} />
          <Route path="/tools" element={<Tools />} />
          <Route path="/memory" element={<Memory />} />
          <Route path="/dream" element={<Dream />} />
          <Route path="/usage" element={<Usage />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/control" element={<Control />} />
          <Route path="*" element={<Dashboard />} />
        </Routes>
      </Suspense>
      <Toaster />
    </Shell>
  );
}
