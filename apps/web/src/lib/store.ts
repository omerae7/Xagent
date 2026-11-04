/** وضعیت سراسری سبک: تم، اعلان‌های موقت (Toast) و کاربر. */

import { useSyncExternalStore } from "react";

function createStore<T>(initial: T) {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    set: (next: T | ((s: T) => T)) => {
      state = typeof next === "function" ? (next as (s: T) => T)(state) : next;
      listeners.forEach((l) => l());
    },
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
}

// ── تم ──
type Theme = "dark" | "light";
const themeStore = createStore<Theme>((document.documentElement.dataset.theme as Theme) || "light");
export function useTheme() {
  const theme = useSyncExternalStore(themeStore.subscribe, themeStore.get);
  const set = (t: Theme) => {
    document.documentElement.dataset.theme = t;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "dark" ? "#060a12" : "#e9ecef");
    try {
      localStorage.setItem("xa-theme", t);
    } catch {
      /* ذخیره‌سازی در دسترس نیست */
    }
    themeStore.set(t);
  };
  return { theme, setTheme: set, toggle: () => set(theme === "dark" ? "light" : "dark") };
}

// ── Toast ──
export type Toast = { id: number; kind: "info" | "success" | "error" | "warning"; text: string };
const toastStore = createStore<Toast[]>([]);
let seq = 1;
export function toast(text: string, kind: Toast["kind"] = "info", ms = 4200) {
  const id = seq++;
  toastStore.set((t) => [...t, { id, kind, text }]);
  setTimeout(() => toastStore.set((t) => t.filter((x) => x.id !== id)), ms);
}
export const toastError = (e: unknown) => toast(e instanceof Error ? e.message : String(e), "error", 6000);
export function useToasts() {
  return useSyncExternalStore(toastStore.subscribe, toastStore.get);
}
export function dismissToast(id: number) {
  toastStore.set((t) => t.filter((x) => x.id !== id));
}
