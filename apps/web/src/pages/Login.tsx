import { motion } from "framer-motion";
import { Lock, LogIn, User } from "lucide-react";
import { useState } from "react";
import { Button, Input } from "../components/ui";
import { Orb } from "../components/viz/Orb";
import { post } from "../lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/api/auth/login", { username, password });
      onLogin();
    } catch (err) {
      setError(err instanceof Error ? err.message : "ورود ناموفق");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="grid min-h-dvh place-items-center p-4">
      <div className="app-bg" />
      <motion.div initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          <Orb state={busy ? "thinking" : "idle"} size={170} className="animate-float" />
          <h1 className="mt-2 text-3xl font-extrabold"><span className="text-gradient">X</span> Agent</h1>
          <p className="mt-1 text-sm text-muted">کارمند دیجیتال ۲۴ساعته شما</p>
        </div>
        <form onSubmit={submit} className="glass space-y-3 rounded-3xl p-6 shadow-glow">
          <label className="relative block">
            <User className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" />
            <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="نام کاربری" className="ps-9" autoComplete="username" />
          </label>
          <label className="relative block">
            <Lock className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" />
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="رمز عبور" className="ps-9" autoComplete="current-password" autoFocus />
          </label>
          {error && <p className="rounded-xl bg-danger/10 px-3 py-2 text-xs text-danger">{error}</p>}
          <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy} icon={<LogIn className="h-4 w-4" />}>ورود به پنل</Button>
          <p className="pt-1 text-center text-[11px] leading-5 text-muted">رمز اولیه هنگام نصب نمایش داده شده و در فایل <code className="ltr">data/initial_password.txt</code> ذخیره است.</p>
        </form>
      </motion.div>
    </div>
  );
}
