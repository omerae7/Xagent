// رندر امن Markdown گفتگو: فقط عناصر React (بدون innerHTML). متن و ویژگی‌ها را React escape می‌کند؛
// پیوند فقط با طرح http/https و پس از تجزیه با URL پذیرفته می‌شود (javascript:/data:/vbscript: و رمزگذاری‌ها رد).
import type { ReactNode } from "react";

const SAFE_SCHEMES = new Set(["http:", "https:"]);
const TRAILING = /[.,،؛:!?؟)\]]+$/;

export function safeHref(raw: string): string | null {
  try {
    const u = new URL(raw);
    return SAFE_SCHEMES.has(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
}

const INLINE = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|(https?:\/\/[^\s<>"'`)]+)/g;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (m[3] && at > 0 && !/[\s(]/.test(text[at - 1])) continue; // پیوند فقط در ابتدای کلمه (مانند قبل)
    if (at > last) out.push(text.slice(last, at));
    const k = `${key}.${n++}`;
    if (m[1] !== undefined) out.push(<code key={k}>{m[1]}</code>);
    else if (m[2] !== undefined) out.push(<strong key={k}>{inline(m[2], k)}</strong>);
    else {
      const url = m[3].replace(TRAILING, "");
      const tail = m[3].slice(url.length);
      const href = safeHref(url);
      out.push(href ? <a key={k} href={href} target="_blank" rel="noopener noreferrer">{url}</a> : url);
      if (tail) out.push(tail);
    }
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block = { kind: "code"; text: string } | { kind: "text"; text: string };

function splitFences(src: string): Block[] {
  const blocks: Block[] = [];
  const re = /```(\w*)\n?([\s\S]*?)```/g;
  let last = 0;
  for (const m of src.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) blocks.push({ kind: "text", text: src.slice(last, at) });
    blocks.push({ kind: "code", text: m[2] });
    last = at + m[0].length;
  }
  if (last < src.length) blocks.push({ kind: "text", text: src.slice(last) });
  return blocks;
}

function textBlock(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let list: { kind: "ul" | "ol"; items: ReactNode[] } | null = null;
  const flush = () => {
    if (!list) return;
    const k = `${key}.l${out.length}`;
    out.push(list.kind === "ul" ? <ul key={k}>{list.items}</ul> : <ol key={k}>{list.items}</ol>);
    list = null;
  };
  text.split("\n").forEach((line, i) => {
    const k = `${key}.${i}`;
    const ul = line.match(/^\s*[-•*]\s+(.*)/);
    const ol = line.match(/^\s*[\d۰-۹]+[.)]\s+(.*)/);
    if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (list && list.kind !== kind) flush();
      if (!list) list = { kind, items: [] };
      list.items.push(<li key={k}>{inline((ul || ol)![1], k)}</li>);
      return;
    }
    flush();
    const h = line.match(/^#{1,4}\s+(.*)/);
    if (h) out.push(<p key={k}><strong>{inline(h[1], k)}</strong></p>);
    else if (line.trim()) out.push(<p key={k}>{inline(line, k)}</p>);
  });
  flush();
  return out;
}

export function ChatMarkdown({ text, className = "prose-chat break-words" }: { text: string; className?: string }) {
  const parts: ReactNode[] = [];
  splitFences(text || "").forEach((b, i) => {
    if (b.kind === "code") parts.push(<pre key={`c${i}`}><code>{b.text}</code></pre>);
    else parts.push(...textBlock(b.text, `t${i}`));
  });
  return <div className={className}>{parts}</div>;
}
