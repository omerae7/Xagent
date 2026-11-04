/** رویدادهای زنده سرور (SSE) با اتصال مجدد خودکار و انتشار به اجزای پنل. */

import { useEffect, useRef, useState, useSyncExternalStore } from "react";

type Handler = (data: any, type: string) => void;
const handlers = new Set<Handler>();
let source: EventSource | null = null;
let status: "connecting" | "open" | "closed" = "closed";
const statusListeners = new Set<() => void>();

const EVENT_TYPES = [
  "run.step",
  "run.status",
  "run.finished",
  "message.created",
  "approval.created",
  "approval.decided",
  "notification",
  "usage",
  "system.metrics",
  "system.stop_all",
  "desktop.takeover",
  "task.changed",
  "routine.changed",
  "crm.changed",
  "tool.changed",
  "memory.revision",
  "memory.undo",
  "dream.started",
  "dream.finished",
  "telegram.status",
  "settings.changed",
  "event.processed",
  "inbox.created",
  "employee.status",
  "model.health",
  "model.fallback",
  // سرور: پس از اتصال/اتصال دوباره و سرریز صف، داده‌ها باید از API دوباره خوانده شوند (Pub/Sub بازپخش ندارد)
  "resync",
  // سرور: توکن باطل شد (خروج/تغییر رمز) — جریان بسته می‌شود
  "auth",
];
const resyncListeners = new Set<(reason: string) => void>();

/** ثبت همگام‌سازی دوباره کامل (مثلاً invalidate همه کوئری‌ها) هنگام اتصال دوباره یا ازدست‌رفتن رویداد */
export function onResync(fn: (reason: string) => void) {
  resyncListeners.add(fn);
  return () => resyncListeners.delete(fn);
}

function setStatus(s: typeof status) {
  status = s;
  statusListeners.forEach((l) => l());
}

export function connectLive() {
  if (source) return;
  setStatus("connecting");
  source = new EventSource("/api/events/stream", { withCredentials: true });
  source.onopen = () => setStatus("open");
  source.onerror = () => setStatus("connecting");
  for (const t of EVENT_TYPES) {
    source.addEventListener(t, (e) => {
      let data: any = {};
      try {
        data = JSON.parse((e as MessageEvent).data);
      } catch {
        /* نادیده */
      }
      if (t === "resync") resyncListeners.forEach((fn) => fn(String(data?.reason || "")));
      if (t === "auth" && data?.status === "revoked") {
        disconnectLive();
        resyncListeners.forEach((fn) => fn("auth"));
        return;
      }
      handlers.forEach((h) => h(data, t));
    });
  }
}

export function disconnectLive() {
  source?.close();
  source = null;
  setStatus("closed");
}

export function useLive(handler: Handler, deps: unknown[] = []) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    const h: Handler = (d, t) => ref.current(d, t);
    handlers.add(h);
    return () => {
      handlers.delete(h);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export function useLiveStatus() {
  return useSyncExternalStore(
    (cb) => {
      statusListeners.add(cb);
      return () => statusListeners.delete(cb);
    },
    () => status,
  );
}

/** نگهداری آخرین N نمونه از بار سیستم برای نمودارهای زنده */
export function useLiveMetrics(initial: any[] = [], max = 60) {
  const [series, setSeries] = useState<any[]>(initial);
  useEffect(() => {
    if (initial.length) setSeries(initial.slice(-max));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial.length]);
  useLive((d, t) => {
    if (t === "system.metrics") setSeries((s) => [...s.slice(-(max - 1)), d]);
  });
  return series;
}
