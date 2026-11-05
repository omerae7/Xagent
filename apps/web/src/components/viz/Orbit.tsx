import { useId } from "react";

/** Original, static orbital mark: no canvas, particle loop, or idle GPU animation. */
export function Orbit({ size = 48, className = "" }: { size?: number; className?: string }) {
  const id = useId();
  return <svg width={size} height={size} viewBox="0 0 240 240" fill="none" aria-hidden className={className}>
    <defs><radialGradient id={id}><stop stopColor="currentColor" stopOpacity=".16" /><stop offset="1" stopColor="currentColor" stopOpacity="0" /></radialGradient></defs>
    <circle cx="120" cy="120" r="118" fill={`url(#${id})`} />
    <g stroke="currentColor" strokeWidth="1.2">
      <circle cx="120" cy="120" r="84" opacity=".22" />
      <ellipse cx="120" cy="120" rx="84" ry="30" transform="rotate(-35 120 120)" opacity=".7" />
      <ellipse cx="120" cy="120" rx="84" ry="30" transform="rotate(35 120 120)" opacity=".7" />
      <ellipse cx="120" cy="120" rx="30" ry="84" opacity=".6" />
      <circle cx="120" cy="120" r="15" opacity=".55" />
      <path d="M120 22v14M120 204v14M22 120h14M204 120h14" opacity=".4" />
    </g>
    <circle cx="120" cy="120" r="5" fill="currentColor" />
    <circle cx="188" cy="71" r="3.5" fill="currentColor" />
    <circle cx="49" cy="165" r="3" fill="currentColor" />
  </svg>;
}
