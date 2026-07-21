// Find-in-file bar for the markdown rich-text (TipTap) editor.
//
// The source view and Monaco own their own find; this brings the same
// affordance to the default markdown editor mode. It owns the query + current
// match, drives TipTapSearchExtension's shared state ref to paint highlights,
// and scrolls the active match into view. The visual matches the source-view
// find bar in CodeViewer.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDownIcon, ChevronUpIcon, SearchIcon, XIcon } from "lucide-react";
import type { Editor } from "@tiptap/react";
import type { ReactElement, RefObject } from "react";
import {
  findMatches,
  searchDecorationKey,
  type SearchDecorationState,
} from "./TipTapSearchExtension";

interface MarkdownSearchBarProps {
  editor: Editor | null;
  /** Shared mutable ref read by TipTapSearchExtension's plugin. */
  searchStateRef: RefObject<SearchDecorationState | null>;
  open: boolean;
  /** Called when the bar closes (Escape / ✕) so the toolbar toggle stays in sync. */
  onClose: () => void;
  inputRef: RefObject<HTMLInputElement | null>;
}

export function MarkdownSearchBar({
  editor,
  searchStateRef,
  open,
  onClose,
  inputRef,
}: MarkdownSearchBarProps): ReactElement | null {
  const [query, setQuery] = useState("");
  const [currentIndex, setCurrentIndex] = useState(0);

  // Recount matches whenever the query changes or the document is edited, so
  // "n / m" and navigation stay accurate.
  const [docVersion, setDocVersion] = useState(0);
  useEffect(() => {
    if (!editor) return;
    const bump = () => setDocVersion((v) => v + 1);
    editor.on("update", bump);
    return () => {
      editor.off("update", bump);
    };
  }, [editor]);

  const matchCount = useMemo(() => {
    const trimmed = query.trim();
    if (!editor || !editor.state || !trimmed) return 0;
    // Match the trimmed query the plugin highlights against (searchStateRef
    // below), so the "n / m" count and the highlighted spans never disagree.
    return findMatches(editor.state.doc, trimmed).length;
    // docVersion forces a recount after edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, query, docVersion]);

  // Reset to the first match on every new query.
  useEffect(() => {
    setCurrentIndex(0);
  }, [query]);

  // Clear the query when the bar closes so a reopen starts fresh.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  // Push query + current match into the plugin's shared state and repaint.
  useEffect(() => {
    if (!editor || editor.isDestroyed || !editor.view) return;
    searchStateRef.current = { query: query.trim().toLowerCase(), currentIndex };
    editor.view.dispatch(editor.state.tr.setMeta(searchDecorationKey, "rebuild"));
  }, [editor, query, currentIndex, matchCount, searchStateRef]);

  // Scroll the active match into view.
  useEffect(() => {
    if (!editor || !editor.view || matchCount === 0) return;
    const rafId = requestAnimationFrame(() => {
      editor.view.dom
        .querySelector(".md-search-match-current")
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(rafId);
  }, [editor, currentIndex, matchCount, query]);

  // Focus the input when the bar opens.
  useEffect(() => {
    if (open) {
      const id = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(id);
    }
  }, [open, inputRef]);

  const goNext = useCallback(() => {
    if (matchCount > 0) setCurrentIndex((i) => (i + 1) % matchCount);
  }, [matchCount]);
  const goPrev = useCallback(() => {
    if (matchCount > 0) setCurrentIndex((i) => (i - 1 + matchCount) % matchCount);
  }, [matchCount]);

  const close = useCallback(() => {
    setQuery("");
    onClose();
  }, [onClose]);

  if (!open) return null;

  const safeIndex = matchCount > 0 ? currentIndex % matchCount : 0;

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
