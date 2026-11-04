/** بازتاب نور روی بلوک‌های تراشه: یک شنونده سراسری (بدون هزینه برای هر کارت) موقعیت نشانگر را در --mx/--my می‌نویسد. */
import { useEffect } from "react";

export function useSheen() {
  useEffect(() => {
    if (!window.matchMedia("(hover: hover)").matches || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let raf = 0;
    let last: { el: HTMLElement; x: number; y: number } | null = null;
    const onMove = (e: PointerEvent) => {
      const el = (e.target as Element | null)?.closest?.(".die") as HTMLElement | null;
      if (!el) return;
      last = { el, x: e.clientX, y: e.clientY };
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!last) return;
        const r = last.el.getBoundingClientRect();
        last.el.style.setProperty("--mx", `${(((last.x - r.left) / r.width) * 100).toFixed(1)}%`);
        last.el.style.setProperty("--my", `${(((last.y - r.top) / r.height) * 100).toFixed(1)}%`);
      });
    };
    document.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      document.removeEventListener("pointermove", onMove);
      cancelAnimationFrame(raf);
    };
  }, []);
}
