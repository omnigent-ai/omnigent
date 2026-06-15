// Token rendering helpers for CodeViewer.
// Handles search-match highlighting within Shiki syntax-highlighted tokens.

import type { CSSProperties } from "react";
import type { ThemedToken } from "shiki";

interface TextSegment {
  text: string;
  isMatch: boolean;
}

function splitAtMatches(text: string, query: string): TextSegment[] {
  const lower = text.toLowerCase();
  const qLower = query.toLowerCase();
  const segments: TextSegment[] = [];
  let pos = 0;
  while (pos <= text.length) {
    const idx = lower.indexOf(qLower, pos);
    if (idx === -1) {
      if (pos < text.length) segments.push({ text: text.slice(pos), isMatch: false });
      break;
    }
    if (idx > pos) segments.push({ text: text.slice(pos, idx), isMatch: false });
    segments.push({ text: text.slice(idx, idx + query.length), isMatch: true });
    pos = idx + query.length;
  }
  return segments;
}

// Shiki encodes font decorations as bit flags on token.fontStyle.
// oxlint-disable eslint(no-bitwise)
const SHIKI_ITALIC = 1;
const SHIKI_BOLD = 2;
const SHIKI_UNDERLINE = 4;
// oxlint-enable eslint(no-bitwise)

function buildTokenStyle(token: ThemedToken): CSSProperties {
  return {
    color: token.color,
    // oxlint-disable-next-line eslint(no-bitwise)
    fontStyle: token.fontStyle && token.fontStyle & SHIKI_ITALIC ? "italic" : undefined,
    // oxlint-disable-next-line eslint(no-bitwise)
    fontWeight: token.fontStyle && token.fontStyle & SHIKI_BOLD ? "bold" : undefined,
    // oxlint-disable-next-line eslint(no-bitwise)
    textDecoration: token.fontStyle && token.fontStyle & SHIKI_UNDERLINE ? "underline" : undefined,
    ...(token.htmlStyle as CSSProperties),
  };
}

export function renderLineTokens(
  tokens: ThemedToken[],
  searchQuery: string,
  isCurrentMatch: boolean,
): React.ReactNode {
  return tokens.map((token, ti) => {
    const style = buildTokenStyle(token);
    if (!searchQuery) {
      return (
        <span key={ti} className="dark:!text-[var(--shiki-dark)]" style={style}>
          {token.content}
        </span>
      );
    }
    const parts = splitAtMatches(token.content, searchQuery);
    if (parts.length === 1 && !parts[0].isMatch) {
      return (
        <span key={ti} className="dark:!text-[var(--shiki-dark)]" style={style}>
          {token.content}
        </span>
      );
    }
    return (
      <span key={ti} className="dark:!text-[var(--shiki-dark)]" style={style}>
        {parts.map((seg, si) =>
          seg.isMatch ? (
            <mark
              key={si}
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
