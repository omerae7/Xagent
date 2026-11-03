/** کامپوننت‌های پایه رابط کاربری — همه از توکن‌های طراحی تغذیه می‌شوند. */

import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";
import {
  forwardRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
} from "react";
import { createPortal } from "react-dom";
import { dismissToast, useToasts } from "../../lib/store";

export const cn = clsx;

// ── کارت شیشه‌ای ──
export function Card({
  className,
  children,
  glow,
  as = "div",
  ...rest
}: { className?: string; children: ReactNode; glow?: boolean; as?: "div" | "section" } & Record<string, any>) {
  const C: any = motion[as];
  return (
    <C
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.2, 0.7, 0.3, 1] }}
      className={cn("glass rounded-card", glow && "shadow-glow", className)}
      {...rest}
    >
      {children}
    </C>
  );
}

export function CardHeader({ title, icon, actions, sub }: { title: ReactNode; icon?: ReactNode; actions?: ReactNode; sub?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 pt-4 sm:px-5">
      <div className="flex min-w-0 items-center gap-2.5">
        {icon && (
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-primary/12 text-primary ring-1 ring-primary/20">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-semibold">{title}</h3>
          {sub && <p className="truncate text-xs text-muted">{sub}</p>}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

// ── دکمه ──
type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "soft" | "danger" | "outline";
  size?: "sm" | "md" | "lg" | "icon";
  loading?: boolean;
  icon?: ReactNode;
};
export const Button = forwardRef<HTMLButtonElement, BtnProps>(function Button(
  { variant = "soft", size = "md", loading, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-xl font-medium transition-all duration-200 active:scale-[.97] disabled:pointer-events-none disabled:opacity-50",
        size === "sm" && "h-8 px-3 text-xs",
        size === "md" && "h-10 px-4 text-sm",
        size === "lg" && "h-12 px-6 text-[15px]",
        size === "icon" && "h-9 w-9",
        variant === "primary" &&
          "btn-glow bg-gradient-to-l from-primary to-[rgb(var(--primary)/.85)] text-white hover:brightness-110",
        variant === "soft" && "bg-primary/10 text-primary ring-1 ring-primary/20 hover:bg-primary/16",
        variant === "ghost" && "text-muted hover:bg-line/10 hover:text-ink",
        variant === "outline" && "hairline border bg-surface/40 hover:bg-surface/70",
        variant === "danger" && "bg-danger/12 text-danger ring-1 ring-danger/25 hover:bg-danger/20",
        className,
      )}
      {...rest}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  );
});

// ── برچسب ──
const TONES = {
  neutral: "bg-line/10 text-muted ring-line/20",
  primary: "bg-primary/12 text-primary ring-primary/25",
  success: "bg-success/12 text-success ring-success/25",
  warning: "bg-warning/12 text-warning ring-warning/25",
  danger: "bg-danger/12 text-danger ring-danger/25",
  accent: "bg-accent/12 text-accent ring-accent/25",
  cyan: "bg-cyan/12 text-cyan ring-cyan/25",
};
export type Tone = keyof typeof TONES;
export function Badge({ tone = "neutral", children, dot, className }: { tone?: Tone; children: ReactNode; dot?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ring-1", TONES[tone], className)}>
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

// ── فرم ──
const fieldBase =
  "w-full rounded-xl border hairline bg-bg2/60 px-3.5 text-sm text-ink placeholder:text-muted/70 outline-none transition focus:border-primary/60 focus:ring-4 focus:ring-primary/10";
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...p }, ref) {
  return <input ref={ref} className={cn(fieldBase, "h-10", className)} {...p} />;
});
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...p },
  ref,
) {
  return <textarea ref={ref} className={cn(fieldBase, "min-h-[88px] py-2.5 leading-7", className)} {...p} />;
});
export function Select({ className, children, ...p }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(fieldBase, "h-10 appearance-none bg-[length:12px] pe-3 ps-8", className)} {...p}>
      {children}
    </select>
  );
}
export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cn("block space-y-1.5", className)}>
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-[11px] leading-5 text-muted/80">{hint}</span>}
    </label>
  );
}

export function Switch({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full transition-colors duration-300 disabled:opacity-50",
        checked ? "bg-primary shadow-[0_0_14px_rgb(var(--primary)/.6)]" : "bg-line/30",
      )}
    >
      <motion.span
        layout
        transition={{ type: "spring", stiffness: 600, damping: 32 }}
        className={cn("absolute top-0.5 h-5 w-5 rounded-full bg-white shadow", checked ? "start-[22px]" : "start-0.5")}
      />
    </button>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = "md",
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode; tone?: string }[];
  size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex flex-wrap gap-1 rounded-xl border hairline bg-bg2/50 p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={cn(
            "relative rounded-lg px-3 font-medium transition-colors",
            size === "sm" ? "h-7 text-[11px]" : "h-8 text-xs",
            value === o.value ? "text-white" : "text-muted hover:text-ink",
          )}
        >
          {value === o.value && (
            <motion.span
              layoutId={`seg-${options.map((x) => x.value).join("-")}`}
              className={cn("absolute inset-0 rounded-lg", o.tone || "bg-primary")}
              transition={{ type: "spring", stiffness: 500, damping: 36 }}
            />
          )}
          <span className="relative">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, onChange, tabs }: { value: T; onChange: (v: T) => void; tabs: { value: T; label: ReactNode; icon?: ReactNode }[] }) {
  return (
    <div className="no-scrollbar -mx-1 flex gap-1 overflow-x-auto px-1 pb-1">
      {tabs.map((t) => (
        <button
          key={t.value}
          onClick={() => onChange(t.value)}
          className={cn(
            "relative flex h-9 shrink-0 items-center gap-2 rounded-xl px-3.5 text-sm transition-colors",
            value === t.value ? "text-ink" : "text-muted hover:text-ink",
          )}
        >
          {value === t.value && (
            <motion.span layoutId={`tabs-${tabs[0].value}`} className="glass absolute inset-0 rounded-xl" transition={{ type: "spring", stiffness: 500, damping: 38 }} />
          )}
          <span className="relative flex items-center gap-2">
            {t.icon}
            {t.label}
          </span>
        </button>
      ))}
    </div>
  );
}

// ── پنجره و کشو ──
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, onClose]);
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
          <motion.div
            role="dialog"
            aria-modal
            initial={{ y: 40, opacity: 0, scale: 0.98 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 30, opacity: 0 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className={cn(
              "glass-strong relative flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-3xl shadow-2xl sm:rounded-3xl",
              wide ? "sm:max-w-3xl" : "sm:max-w-lg",
            )}
          >
            <div className="flex items-center justify-between border-b hairline px-5 py-4">
              <h2 className="text-base font-semibold">{title}</h2>
              <Button variant="ghost" size="icon" onClick={onClose} aria-label="بستن">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
            {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t hairline px-5 py-3.5">{footer}</div>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <div className="absolute inset-0 bg-black/45 backdrop-blur-sm" onClick={onClose} />
          <motion.aside
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 340, damping: 36 }}
            className="glass-strong absolute inset-y-0 right-0 flex w-full max-w-xl flex-col"
          >
            <div className="flex items-center justify-between border-b hairline px-5 py-4">
              <h2 className="text-base font-semibold">{title}</h2>
              <Button variant="ghost" size="icon" onClick={onClose} aria-label="بستن">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto p-5">{children}</div>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

// ── وضعیت‌ها ──
export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("h-5 w-5 animate-spin text-primary", className)} />;
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton h-4", className)} />;
}

export function Empty({ icon, title, text, action }: { icon?: ReactNode; title: string; text?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-12 text-center">
      {icon && <div className="grid h-14 w-14 place-items-center rounded-2xl bg-primary/10 text-primary ring-1 ring-primary/20">{icon}</div>}
      <div className="space-y-1">
        <p className="font-medium">{title}</p>
        {text && <p className="max-w-sm text-sm leading-6 text-muted">{text}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-2xl border border-danger/25 bg-danger/8 px-4 py-3 text-sm text-danger">
      <span className="flex items-center gap-2">
        <XCircle className="h-4 w-4 shrink-0" />
        {error instanceof Error ? error.message : String(error)}
      </span>
      {retry && (
        <Button size="sm" variant="danger" onClick={retry}>
          تلاش دوباره
        </Button>
      )}
    </div>
  );
}

export function Toaster() {
  const toasts = useToasts();
  const icons = {
    info: <Info className="h-4 w-4 text-primary" />,
    success: <CheckCircle2 className="h-4 w-4 text-success" />,
    error: <XCircle className="h-4 w-4 text-danger" />,
    warning: <AlertTriangle className="h-4 w-4 text-warning" />,
  };
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-24 z-[60] flex flex-col items-center gap-2 px-4 sm:bottom-6 sm:items-start sm:ps-6">
      <AnimatePresence>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout
            initial={{ opacity: 0, y: 16, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, x: -30 }}
            className="glass-strong pointer-events-auto flex max-w-md items-start gap-2.5 rounded-2xl px-4 py-3 text-sm shadow-xl"
          >
            <span className="mt-0.5">{icons[t.kind]}</span>
            <span className="flex-1 leading-6">{t.text}</span>
            <button onClick={() => dismissToast(t.id)} className="text-muted hover:text-ink" aria-label="بستن">
              <X className="h-3.5 w-3.5" />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>,
    document.body,
  );
}

export function PageHeader({ title, sub, icon, actions }: { title: string; sub?: ReactNode; icon?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="flex items-center gap-3">
        {icon && <span className="grid h-11 w-11 place-items-center rounded-2xl bg-gradient-to-br from-primary/25 to-accent/20 text-primary ring-1 ring-primary/25">{icon}</span>}
        <div>
          <h1 className="text-xl font-bold sm:text-2xl">{title}</h1>
          {sub && <p className="mt-0.5 text-sm text-muted">{sub}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, sub, icon, tone = "primary" }: { label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode; tone?: Tone }) {
  return (
    <Card className="relative overflow-hidden p-4">
      <div className={cn("absolute -end-6 -top-6 h-24 w-24 rounded-full opacity-25 blur-2xl", `bg-${tone}`)} />
      <div className="relative flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs text-muted">{label}</p>
          <p className="num mt-1.5 truncate text-2xl font-bold">{value}</p>
          {sub && <p className="mt-1 truncate text-xs text-muted">{sub}</p>}
        </div>
        {icon && <span className={cn("grid h-9 w-9 place-items-center rounded-xl ring-1", TONES[tone])}>{icon}</span>}
      </div>
    </Card>
  );
}

export function confirmAsk(text: string) {
  return window.confirm(text);
}
