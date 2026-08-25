// Token rendering helpers for CodeViewer.
// Handles search-match highlighting within Shiki syntax-highlighted tokens.

import type { CSSProperties } from "react";
import type { ThemedToken } from "shiki";

interface TextSegment {
  text: string;
  isMatch: boolean;
  offset: number;
}

function splitAtMatches(text: string, query: string): TextSegment[] {
  const lower = text.toLowerCase();
  const qLower = query.toLowerCase();
  const segments: TextSegment[] = [];
  let pos = 0;
  while (pos <= text.length) {
    const idx = lower.indexOf(qLower, pos);
    if (idx === -1) {
      if (pos < text.length) segments.push({ text: text.slice(pos), isMatch: false, offset: pos });
      break;
    }
    if (idx > pos) segments.push({ text: text.slice(pos, idx), isMatch: false, offset: pos });
    segments.push({ text: text.slice(idx, idx + query.length), isMatch: true, offset: idx });
    pos = idx + query.length;
  }
  return segments;
}

// Shiki encodes font decorations as bit flags on token.fontStyle.
const SHIKI_ITALIC = 1;
const SHIKI_BOLD = 2;
const SHIKI_UNDERLINE = 4;

function buildTokenStyle(token: ThemedToken): CSSProperties {
  return {
    color: token.color,
    fontStyle: token.fontStyle && token.fontStyle & SHIKI_ITALIC ? "italic" : undefined,
    fontWeight: token.fontStyle && token.fontStyle & SHIKI_BOLD ? "bold" : undefined,
    textDecoration: token.fontStyle && token.fontStyle & SHIKI_UNDERLINE ? "underline" : undefined,
    ...(token.htmlStyle as CSSProperties),
  };
}

export function renderLineTokens(
  tokens: ThemedToken[],
  searchQuery: string,
  isCurrentMatch: boolean,
): React.ReactNode {
  return tokens.map((token) => {
    const style = buildTokenStyle(token);
    if (!searchQuery) {
      return (
        <span key={token.offset} className="dark:!text-[var(--shiki-dark)]" style={style}>
          {token.content}
        </span>
      );
    }
    const parts = splitAtMatches(token.content, searchQuery);
    if (parts.length === 1 && !parts[0].isMatch) {
      return (
        <span key={token.offset} className="dark:!text-[var(--shiki-dark)]" style={style}>
          {token.content}
        </span>
      );
    }
    return (
      <span key={token.offset} className="dark:!text-[var(--shiki-dark)]" style={style}>
        {parts.map((seg) =>
          seg.isMatch ? (
            <mark
              key={token.offset + seg.offset}
              className={
                isCurrentMatch
                  ? "rounded-sm bg-orange-400 text-black"
                  : "rounded-sm bg-yellow-300/80"
              }
            >
              {seg.text}
            </mark>
          ) : (
            seg.text
          ),
        )}
      </span>
    );
  });
}
