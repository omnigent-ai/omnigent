import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import { type MatchRange, wordCoalescedRanges } from "./match";

/**
 * How matched text stands out:
 * - `emphasis`: bold only, the default.
 * - `underline`: bold with a quiet underline that marks whole words, for short
 *   named things such as commands, where the match should be easy to spot.
 */
export type MatchHighlight = "emphasis" | "underline";

const HIGHLIGHT_CLASS: Record<MatchHighlight, string> = {
  emphasis: "font-bold text-foreground",
  underline:
    "font-bold text-foreground underline decoration-muted-foreground decoration-1 underline-offset-[3px]",
};

/** `text` with each matched range marked; ranges are sorted, disjoint UTF-16 offsets. */
export function HighlightedText({
  text,
  ranges,
  highlight = "emphasis",
}: {
  text: string;
  ranges: readonly MatchRange[];
  highlight?: MatchHighlight;
}) {
  if (ranges.length === 0) return text;
  // The underline reads as words; the default highlight stays on the exact characters.
  const marked = highlight === "underline" ? wordCoalescedRanges(text, ranges) : ranges;
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const range of marked) {
    const start = Math.max(cursor, Math.min(range.start, text.length));
    const end = Math.min(range.end, text.length);
    if (end <= start) continue;
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(
      <mark
        key={start}
        data-match-highlight={highlight}
        className={cn("bg-transparent", HIGHLIGHT_CLASS[highlight])}
      >
        {text.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}
