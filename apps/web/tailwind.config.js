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
        onprimary: v("on-primary"),
        accent: v("accent"),
        cyan: v("cyan"),
        success: v("success"),
        warning: v("warning"),
        danger: v("danger"),
      },
      fontFamily: { sans: ["Vazirmatn", "Tahoma", "system-ui", "sans-serif"] },
      borderRadius: { ctl: "var(--r-ctl)" },
      boxShadow: {
        lift: "0 18px 40px -18px rgb(0 0 0 / .55), 0 2px 6px -2px rgb(0 0 0 / .25)",
      },
      keyframes: {
        "spin-slow": { to: { transform: "rotate(360deg)" } },
        shimmer: { "0%": { backgroundPosition: "200% 0" }, "100%": { backgroundPosition: "-200% 0" } },
        dash: { to: { strokeDashoffset: "0" } },
      },
      animation: {
        "spin-slow": "spin-slow 14s linear infinite",
        "spin-slower": "spin-slow 26s linear infinite",
        shimmer: "shimmer 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
