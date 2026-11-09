import { Fragment, type ReactNode } from "react";

// A small markdown renderer that builds React elements only: raw HTML in the text stays text,
// because nothing here sets innerHTML. Links are kept only for http(s) and mailto URLs.

const SAFE_URL = /^(https?:\/\/|mailto:)/i;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  // `code`, **bold**, *em* or _em_, [text](url)
  const re = /(`+)([^`]+?)\1|\*\*([^*]+)\*\*|(?<![\w*])\*([^*\s][^*]*?)\*(?!\w)|(?<!\w)_([^_\s][^_]*?)_(?!\w)|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index;
    if (at > last) out.push(text.slice(last, at));
    const k = `${key}-${n++}`;
    if (m[2] !== undefined) out.push(<code key={k}>{m[2]}</code>);
    else if (m[3] !== undefined) out.push(<strong key={k}>{inline(m[3], k)}</strong>);
    else if (m[4] !== undefined) out.push(<em key={k}>{inline(m[4], k)}</em>);
    else if (m[5] !== undefined) out.push(<em key={k}>{inline(m[5], k)}</em>);
    else if (m[6] !== undefined && m[7] !== undefined) {
      out.push(
        SAFE_URL.test(m[7]) ? (
          <a key={k} href={m[7]} target="_blank" rel="noopener noreferrer">
            {m[6]}
          </a>
        ) : (
          <Fragment key={k}>{m[0]}</Fragment>
        ),
      );
    }
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function withBreaks(lines: string[], key: string): ReactNode[] {
  return lines.flatMap((l, i) => (i === 0 ? inline(l, `${key}-${i}`) : [<br key={`${key}-br${i}`} />, ...inline(l, `${key}-${i}`)]));
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  // bound: each pass consumes at least one line
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const k = `b${i}`;
    const fence = /^\s*(```|~~~)(.*)$/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      // bound: stops at the closing fence or the end of the text
      while (i < lines.length && !(lines[i] ?? "").trim().startsWith(fence[1] ?? "```")) body.push(lines[i++] ?? "");
      i++;
      blocks.push(
        <pre key={k} className="md-pre">
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = Math.min((h[1] ?? "#").length + 2, 6);
      const Tag = `h${level}` as "h3" | "h4" | "h5" | "h6";
      blocks.push(<Tag key={k}>{inline(h[2] ?? "", k)}</Tag>);
      i++;
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      // bound: stops at the first line that is not a list item
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i] ?? "")) items.push((lines[i++] ?? "").replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
      const lis = items.map((t, j) => <li key={j}>{inline(t, `${k}-${j}`)}</li>);
      blocks.push(ordered ? <ol key={k}>{lis}</ol> : <ul key={k}>{lis}</ul>);
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quote: string[] = [];
      // bound: stops at the first line that is not quoted
      while (i < lines.length && /^\s*>/.test(lines[i] ?? "")) quote.push((lines[i++] ?? "").replace(/^\s*>\s?/, ""));
      blocks.push(<blockquote key={k}>{withBreaks(quote, k)}</blockquote>);
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    // bound: stops at a blank line or the start of another block
    while (i < lines.length && (lines[i] ?? "").trim() && !/^(\s*(```|~~~|>|[-*+]\s|\d+[.)]\s)|#{1,6}\s)/.test(lines[i] ?? "")) para.push(lines[i++] ?? "");
    if (para.length === 0) para.push(lines[i++] ?? "");
    blocks.push(<p key={k}>{withBreaks(para, k)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
