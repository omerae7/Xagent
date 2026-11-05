/** کلاینت API: کوکی httpOnly برای احراز هویت + هدر ضد CSRF برای درخواست‌های تغییردهنده. */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** نوع شکست: فقط 401 یعنی «نشست نامعتبر»؛ قطع شبکه/۵xx قطعی سرویس است و نباید به صفحه ورود ببرد. */
export type FailureKind = "auth" | "network" | "unavailable" | "server" | "client";

export function failureKind(e: unknown): FailureKind {
  const st = e instanceof ApiError ? e.status : 0;
  if (st === 401) return "auth";
  if (st === 0) return "network";
  if (st === 502 || st === 503 || st === 504) return "unavailable";
  if (st >= 500) return "server";
  return "client";
}

export const FAILURE_FA: Record<Exclude<FailureKind, "auth" | "client">, string> = {
  network: "ارتباط شبکه با سرور برقرار نیست (قطع اتصال یا خطای انتقال)",
  unavailable: "سرویس API در دسترس نیست (در حال راه‌اندازی، به‌روزرسانی یا قطعی موقت)",
  server: "خطای داخلی سرور (۵xx)",
};

type Opts = { method?: string; body?: unknown; form?: FormData; signal?: AbortSignal; raw?: boolean };

export async function api<T = any>(path: string, opts: Opts = {}): Promise<T> {
  const method = opts.method ?? (opts.body !== undefined || opts.form ? "POST" : "GET");
  const headers: Record<string, string> = { "X-XAgent": "1" };
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, credentials: "same-origin", signal: opts.signal });
  } catch {
    window.dispatchEvent(new Event("xa:service-error"));  // قطع انتقال: سنجش فوری سلامت، نه خروج از حساب
    throw new ApiError("ارتباط با سرور برقرار نشد", 0);
  }
  if (res.status >= 500) window.dispatchEvent(new Event("xa:service-error"));
  if (res.status === 401 && !path.startsWith("/api/auth/login")) {
    window.dispatchEvent(new Event("xa:unauthorized"));
  }
  if (!res.ok) {
    let msg = `خطای ${res.status}`;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* بدنه JSON نیست */
    }
    throw new ApiError(msg, res.status);
  }
  if (opts.raw) return res as unknown as T;
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const get = <T = any>(p: string) => api<T>(p);
export const post = <T = any>(p: string, body: unknown = {}) => api<T>(p, { method: "POST", body });
export const put = <T = any>(p: string, body: unknown) => api<T>(p, { method: "PUT", body });
export const patch = <T = any>(p: string, body: unknown) => api<T>(p, { method: "PATCH", body });
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });
