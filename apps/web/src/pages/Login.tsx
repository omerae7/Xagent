import { motion } from "framer-motion";
import { Lock, LogIn, User } from "lucide-react";
import { useState } from "react";
import { Button, Input } from "../components/ui";
import { PadArray } from "../components/viz/Silicon";
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
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }} className="die die-lg w-full max-w-[420px]">
        <div className="flex items-center gap-4 border-b hairline p-5">
          <PadArray state={busy ? "thinking" : error ? "waiting" : "idle"} size={92} n={6} />
          <div className="min-w-0">
            <h1 className="ltr text-[28px] font-extrabold leading-9 tracking-[-0.03em]">X Agent</h1>
            <p className="text-sm text-muted">مدیر و کارمندان دیجیتال شما</p>
          </div>
        </div>
        <form onSubmit={submit} className="space-y-3.5 p-5">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted">نام کاربری</span>
            <span className="relative block">
              <User className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" />
              <Input value={username} onChange={(e) => setUsername(e.target.value)} className="ps-9" autoComplete="username" />
            </span>
          </label>
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-muted">رمز عبور</span>
            <span className="relative block">
              <Lock className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted" />
              <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="ps-9" autoComplete="current-password" autoFocus aria-invalid={!!error} />
            </span>
          </label>
          {error && <p role="alert" className="bevel border border-danger/30 bg-danger/10 px-3 py-2 text-xs leading-5 text-danger">{error}</p>}
          <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy} icon={<LogIn className="h-4 w-4" />}>ورود به پنل</Button>
          <p className="pt-1 text-center text-[11px] leading-5 text-muted">رمز اولیه هنگام نصب نمایش داده شده و در فایل <code className="ltr mono">data/initial_password.txt</code> ذخیره است.</p>
        </form>
      </motion.div>
    </div>
  );
}
