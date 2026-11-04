import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { Toaster } from "./components/ui";
import { PadArray } from "./components/viz/Silicon";
import { get, post } from "./lib/api";
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
  const me = useQuery({ queryKey: ["me"], queryFn: () => get("/api/auth/me"), retry: false });

  useEffect(() => {
    if (me.isSuccess) setAuthed(true);
    if (me.isError) setAuthed(false);
  }, [me.isSuccess, me.isError]);

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
