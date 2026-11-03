/** قالب‌بندی فارسی: ارقام، تاریخ شمسی/میلادی، حجم، زمان نسبی، توکن و هزینه. */

let currentTz = "Asia/Tehran";
let calendar: "jalali" | "gregorian" = "jalali";

export function setLocale(tz: string, cal: "jalali" | "gregorian") {
  currentTz = tz || "Asia/Tehran";
  calendar = cal || "jalali";
}
export const getTz = () => currentTz;

const nf = new Intl.NumberFormat("fa-IR");
export const faNum = (n: number | null | undefined, digits = 0) =>
  n == null || Number.isNaN(n)
    ? "—"
    : new Intl.NumberFormat("fa-IR", { maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(n);
export const faInt = (n: number | null | undefined) => (n == null ? "—" : nf.format(Math.round(n)));
export const toFaDigits = (s: string | number) => String(s).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[+d]);

export function compact(n: number | null | undefined): string {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return faNum(n / 1e9, 1) + "B";
  if (a >= 1e6) return faNum(n / 1e6, 1) + "M";
  if (a >= 1e3) return faNum(n / 1e3, 1) + "K";
  return faNum(n);
}

export const usd = (n: number | null | undefined, digits = 4) =>
  n == null ? "—" : "$" + toFaDigits(Number(n).toFixed(n >= 10 ? 2 : digits));

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${faNum(v, v < 10 ? 1 : 0)} ${u[i]}`;
}

function cal() {
  return calendar === "jalali" ? "fa-IR-u-ca-persian-nu-arab" : "fa-IR-u-ca-gregory";
}

export function fmtDate(iso?: string | null, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat(cal(), {
    timeZone: currentTz,
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(d);
}

export function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  return new Intl.DateTimeFormat("fa-IR", { timeZone: currentTz, hour: "2-digit", minute: "2-digit", hour12: false }).format(
    new Date(iso),
  );
}

export function fmtDay(d: Date): string {
  return new Intl.DateTimeFormat(cal(), { timeZone: currentTz, weekday: "long", day: "numeric", month: "long" }).format(d);
}

export function relTime(iso?: string | null): string {
  if (!iso) return "—";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat("fa", { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 45) return "همین الان";
  if (abs < 3600) return rtf.format(-Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(-Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(-Math.round(diff / 86400), "day");
  return fmtDate(iso, false);
}

export function duration(ms?: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${faInt(ms)} میلی‌ثانیه`;
  const s = ms / 1000;
  if (s < 60) return `${faNum(s, 1)} ثانیه`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${faInt(m)} دقیقه و ${faInt(s % 60)} ثانیه`;
  return `${faInt(Math.floor(m / 60))} ساعت و ${faInt(m % 60)} دقیقه`;
}

export function uptime(sec?: number): string {
  if (!sec) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return d ? `${faInt(d)} روز و ${faInt(h)} ساعت` : `${faInt(h)} ساعت و ${faInt(Math.floor((sec % 3600) / 60))} دقیقه`;
}

/** تاریخ محلی (datetime-local) ← ISO با منطقه زمانی مرورگر */
export function localInputToIso(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}
export function isoToLocalInput(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
