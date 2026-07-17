// Find-in-file bar for the rendered markdown / notebook preview.
//
// The preview is React-owned DOM, so matches are painted with the CSS Custom
// Highlight API (see previewSearch.ts) rather than by wrapping nodes. This bar
// owns the query + current match, recomputes ranges when the query or rendered
// content changes, and scrolls the active match into view. Its UI matches the
// source-view and editor find bars.

import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { ChevronDownIcon, ChevronUpIcon, SearchIcon, XIcon } from "lucide-react";
import type { ReactElement, RefObject } from "react";
import { clearPreviewHighlights, findTextRanges, paintPreviewHighlights } from "./previewSearch";

interface PreviewSearchBarProps {
  /** The rendered-preview container whose text is searched + highlighted. */
  containerRef: RefObject<HTMLElement | null>;
  /** Bumped by the caller whenever the rendered content changes, so ranges recompute. */
  contentVersion: unknown;
  open: boolean;
  /** Called when the bar closes (Escape / ✕) so the toolbar toggle stays in sync. */
  onClose: () => void;
  inputRef: RefObject<HTMLInputElement | null>;
}

export function PreviewSearchBar({
  containerRef,
  contentVersion,
  open,
  onClose,
  inputRef,
}: PreviewSearchBarProps): ReactElement | null {
  const [query, setQuery] = useState("");
  const [currentIndex, setCurrentIndex] = useState(0);
  const [ranges, setRanges] = useState<Range[]>([]);

  // Recompute match ranges when the query, the open state, or the rendered
  // content changes. Ranges are DOM Ranges into the live preview nodes, so this
  // runs in useLayoutEffect (post-commit) rather than during render: on a
  // content change the new react-markdown/notebook DOM must be committed first,
  // otherwise the walker would build Ranges into nodes about to be replaced.
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!open || !container || !query.trim()) {
      setRanges([]);
      return;
    }
    setRanges(findTextRanges(container, query.trim()));
    // contentVersion forces a rebuild after the preview re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef, open, query, contentVersion]);

  const matchCount = ranges.length;
  const safeIndex = matchCount > 0 ? currentIndex % matchCount : 0;

  // Reset to the first match on every new query.
  useEffect(() => {
    setCurrentIndex(0);
  }, [query]);

  // Clear the query when the bar closes so a reopen starts fresh.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  // Paint the highlights and keep them cleared when the bar is closed or empty.
  useEffect(() => {
    if (!open || matchCount === 0) {
      clearPreviewHighlights();
      return;
    }
    paintPreviewHighlights(ranges, safeIndex);
    return () => clearPreviewHighlights();
  }, [open, ranges, safeIndex, matchCount]);

  // Scroll the active match into view. Ranges have no scrollIntoView, so use the
  // client rect of the current range.
  useEffect(() => {
    if (!open || matchCount === 0) return;
    const rect = ranges[safeIndex]?.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    const container = containerRef.current;
    const scroller = container?.closest("[data-preview-scroll]") ?? container;
    if (!scroller) return;
    const box = scroller.getBoundingClientRect();
    if (rect.top >= box.top && rect.bottom <= box.bottom) return; // already visible
    scroller.scrollBy({
      top: rect.top - box.top - box.height / 2 + rect.height / 2,
      behavior: "smooth",
    });
  }, [open, ranges, safeIndex, matchCount, containerRef]);

  // Focus the input when the bar opens.
  useEffect(() => {
    if (open) {
      const id = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(id);
    }
  }, [open, inputRef]);

  const goNext = useCallback(() => {
    setCurrentIndex((i) => (matchCount > 0 ? (i + 1) % matchCount : 0));
  }, [matchCount]);
  const goPrev = useCallback(() => {
    setCurrentIndex((i) => (matchCount > 0 ? (i - 1 + matchCount) % matchCount : 0));
  }, [matchCount]);

  const close = useCallback(() => {
    setQuery("");
    onClose();
  }, [onClose]);

  if (!open) return null;

  return (
    <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card/90 px-3 py-1.5 backdrop-blur">
      <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            if (e.shiftKey) goPrev();
            else goNext();
          } else if (e.key === "Escape") {
            e.preventDefault();
            close();
          }
        }}
        placeholder="Find…"
        className="min-w-0 flex-1 bg-transparent text-xs outline-none"
      />
      <span className="shrink-0 text-xs text-muted-foreground">
        {query.trim() ? (matchCount > 0 ? `${safeIndex + 1} / ${matchCount}` : "No results") : ""}
      </span>
      <button
        type="button"
        aria-label="Previous match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={matchCount === 0}
        onClick={goPrev}
      >
        <ChevronUpIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Next match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={matchCount === 0}
        onClick={goNext}
      >
        <ChevronDownIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Close search"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted"
        onClick={close}
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}
