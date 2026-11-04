/** کامپوننت‌های پایه رابط — زبان «سیلیکون زنده»: بلوک‌های پَخ‌دار، پدهای مربعی، مسیرهای سیگنال. */

import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, ChevronDown, Info, Loader2, X, XCircle } from "lucide-react";
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

// ── بلوک تراشه (کارت) ──
export function Card({
  className,
  children,
  glow,
  as = "div",
  tone = 1,
  ...rest
}: { className?: string; children: ReactNode; glow?: boolean; as?: "div" | "section"; tone?: 1 | 2 } & Record<string, any>) {
  const C: any = as;
  return (
    <C className={cn("die", glow && "die-live", tone === 2 && "die-2", className)} {...rest}>
      {children}
    </C>
  );
}

export function CardHeader({ title, icon, actions, sub }: { title: ReactNode; icon?: ReactNode; actions?: ReactNode; sub?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 pt-4 sm:px-5">
      <div className="flex min-w-0 items-center gap-2.5">
        {icon && <span className="bevel grid h-8 w-8 shrink-0 place-items-center bg-primary/10 text-primary ring-1 ring-inset ring-primary/20">{icon}</span>}
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-semibold leading-6">{title}</h3>
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
      aria-busy={loading || undefined}
      className={cn(
        "bevel relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-medium transition-[background-color,color,box-shadow,transform] duration-150 ease-out active:translate-y-px disabled:pointer-events-none disabled:opacity-45",
        size === "sm" && "h-8 px-3 text-xs",
        size === "md" && "h-10 px-4 text-sm",
        size === "lg" && "h-12 px-6 text-[15px]",
        size === "icon" && "h-9 w-9",
        variant === "primary" && "bg-primary text-onprimary shadow-[inset_0_1px_0_rgb(255_255_255/.28),0_1px_0_rgb(0_0_0/.2)] hover:bg-primary/90",
        variant === "soft" && "bg-primary/10 text-primary ring-1 ring-inset ring-primary/22 hover:bg-primary/16",
        variant === "ghost" && "text-muted hover:bg-line/10 hover:text-ink",
        variant === "outline" && "bg-surface/60 text-ink ring-1 ring-inset ring-line/20 hover:bg-surface2 hover:ring-line/30",
        variant === "danger" && "bg-danger/10 text-danger ring-1 ring-inset ring-danger/25 hover:bg-danger/18",
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
  primary: "bg-primary/10 text-primary ring-primary/25",
  success: "bg-success/10 text-success ring-success/25",
  warning: "bg-warning/10 text-warning ring-warning/30",
  danger: "bg-danger/10 text-danger ring-danger/25",
  accent: "bg-accent/10 text-accent ring-accent/25",
  cyan: "bg-cyan/10 text-cyan ring-cyan/25",
};
export type Tone = keyof typeof TONES;
export function Badge({ tone = "neutral", children, dot, className }: { tone?: Tone; children: ReactNode; dot?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-[3px] px-2 py-0.5 text-[11px] font-medium leading-5 ring-1 ring-inset", TONES[tone], className)}>
      {dot && <span className="pad" />}
      {children}
    </span>
  );
}

// ── فرم ──
const fieldBase =
  "bevel w-full border hairline bg-surface2/70 px-3.5 text-sm text-ink outline-none transition-[border-color,box-shadow] duration-150 focus:border-primary/60 focus:shadow-[0_0_0_3px_rgb(var(--primary)/.14)] disabled:opacity-55 aria-[invalid=true]:border-danger/60";
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
    <div className="relative">
      <select className={cn(fieldBase, "h-10 appearance-none ps-3 pe-8", className)} {...p}>
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute end-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
    </div>
  );
}
export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cn("block space-y-1.5", className)}>
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-[11px] leading-5 text-muted/90">{hint}</span>}
    </label>
  );
}

/** کلید کشویی سخت‌افزاری (مستطیلی) */
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
        "relative h-6 w-11 shrink-0 rounded-[5px] ring-1 ring-inset transition-colors duration-200 disabled:opacity-45",
        checked ? "bg-primary ring-primary" : "bg-line/15 ring-line/25",
      )}
    >
      <motion.span
        layout
        transition={{ type: "spring", stiffness: 700, damping: 40 }}
        className={cn(
          "absolute top-[3px] h-[18px] w-[18px] rounded-[3px] shadow-[0_1px_2px_rgb(0_0_0/.35)]",
          checked ? "start-[23px] bg-onprimary" : "start-[3px] bg-ink/70",
        )}
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
    <div className="bevel inline-flex flex-wrap gap-0.5 bg-bg2/70 p-0.5 ring-1 ring-inset ring-line/15" role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "relative rounded-[5px] px-3 font-medium transition-colors duration-150",
            size === "sm" ? "h-7 text-[11px]" : "h-8 text-xs",
            value === o.value ? (o.tone ? "text-white" : "text-ink") : "text-muted hover:text-ink",
          )}
        >
          {value === o.value && (
            <motion.span
              layoutId={`seg-${options.map((x) => x.value).join("-")}`}
              className={cn("absolute inset-0 rounded-[5px]", o.tone || "bg-surface2 ring-1 ring-inset ring-line/25")}
              transition={{ type: "spring", stiffness: 600, damping: 42 }}
            />
          )}
          <span className="relative">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

/** زبانه‌ها: زبانه فعال با «مسیر سیگنال» زیرین مشخص می‌شود */
export function Tabs<T extends string>({ value, onChange, tabs }: { value: T; onChange: (v: T) => void; tabs: { value: T; label: ReactNode; icon?: ReactNode }[] }) {
  return (
    <div className="no-scrollbar -mx-1 flex gap-0.5 overflow-x-auto border-b hairline px-1" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={value === t.value}
          onClick={() => onChange(t.value)}
          className={cn(
            "relative flex h-10 shrink-0 items-center gap-2 px-3.5 text-sm transition-colors duration-150",
            value === t.value ? "text-ink" : "text-muted hover:text-ink",
          )}
        >
          <span className="relative flex items-center gap-2">
            {t.icon}
            {t.label}
          </span>
          {value === t.value && (
            <motion.span layoutId={`tabs-${tabs[0].value}`} className="film-bar absolute inset-x-2 -bottom-px h-[2px]" transition={{ type: "spring", stiffness: 600, damping: 44 }} />
          )}
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
          <div className="absolute inset-0 bg-black/55" onClick={onClose} />
          <motion.div
            role="dialog"
            aria-modal
            initial={{ y: 24, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 16, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className={cn("die die-lg die-2 relative flex max-h-[92dvh] w-full flex-col", wide ? "sm:max-w-3xl" : "sm:max-w-lg")}
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
          <div className="absolute inset-0 bg-black/55" onClick={onClose} />
          <motion.aside
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
            className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-s hairline bg-surface"
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
      {icon && <div className="die die-sm grid h-14 w-14 place-items-center text-primary">{icon}</div>}
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
    <div role="alert" className="bevel flex items-center justify-between gap-3 border border-danger/30 bg-danger/8 px-4 py-3 text-sm text-danger">
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
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, x: -24 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            role="status"
            className="die die-sm die-2 pointer-events-auto flex max-w-md items-start gap-2.5 px-4 py-3 text-sm shadow-lift"
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
        {icon && <span className="die die-sm grid h-11 w-11 place-items-center text-primary">{icon}</span>}
        <div>
          <h1 className="text-xl font-bold leading-8 sm:text-[22px]">{title}</h1>
          {sub && <p className="mt-0.5 text-sm text-muted">{sub}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, sub, icon, tone = "primary" }: { label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode; tone?: Tone }) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-xs text-muted"><span className={cn("pad", `text-${tone}`)} />{label}</p>
          <p className="num mt-1.5 truncate text-2xl font-bold">{value}</p>
          {sub && <p className="mt-1 truncate text-xs text-muted">{sub}</p>}
        </div>
        {icon && <span className={cn("bevel grid h-9 w-9 place-items-center ring-1 ring-inset", TONES[tone])}>{icon}</span>}
      </div>
    </Card>
  );
}

export function confirmAsk(text: string) {
  return window.confirm(text);
}
