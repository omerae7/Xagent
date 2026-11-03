/** رنگ‌ها از توکن‌های طراحی (CSS Variables) خوانده می‌شوند تا پوسته بدون تغییر کد قابل تعویض باشد. */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  safelist: ["bg-primary", "bg-success", "bg-warning", "bg-danger", "bg-accent", "bg-cyan", "bg-neutral"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: v("bg"),
        bg2: v("bg-2"),
        surface: v("surface"),
        surface2: v("surface-2"),
        line: v("border"),
        ink: v("text"),
        muted: v("muted"),
        primary: v("primary"),
        accent: v("accent"),
        cyan: v("cyan"),
        success: v("success"),
        warning: v("warning"),
        danger: v("danger"),
      },
      fontFamily: { sans: ["Vazirmatn", "Tahoma", "system-ui", "sans-serif"] },
      borderRadius: { xl2: "var(--radius-lg)", card: "var(--radius-card)" },
      boxShadow: {
        glow: "0 0 0 1px rgb(var(--primary) / .25), 0 8px 40px -8px rgb(var(--primary) / .45)",
        card: "var(--shadow-card)",
      },
      keyframes: {
        "spin-slow": { to: { transform: "rotate(360deg)" } },
        float: { "0%,100%": { transform: "translateY(0)" }, "50%": { transform: "translateY(-6px)" } },
        shimmer: { "0%": { backgroundPosition: "200% 0" }, "100%": { backgroundPosition: "-200% 0" } },
        pulseRing: { "0%": { transform: "scale(.8)", opacity: ".8" }, "100%": { transform: "scale(1.6)", opacity: "0" } },
        dash: { to: { strokeDashoffset: "0" } },
      },
      animation: {
        "spin-slow": "spin-slow 14s linear infinite",
        "spin-slower": "spin-slow 26s linear infinite",
        float: "float 6s ease-in-out infinite",
        shimmer: "shimmer 2.2s linear infinite",
        "pulse-ring": "pulseRing 2.4s cubic-bezier(.2,.6,.3,1) infinite",
      },
    },
  },
  plugins: [],
};
