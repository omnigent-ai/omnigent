// Find-in-chat bar. Only on-screen rows render, so matches come from the server,
// one window of items per request, newest first, and the transcript pages history
// in up to each one. Matches are painted with the CSS Custom Highlight API.

import { useCallback, useEffect, useState } from "react";
import { ChevronDownIcon, ChevronUpIcon, SearchIcon, XIcon } from "lucide-react";
import type { ReactElement, RefObject } from "react";
import { searchSessionItems, type SessionSearchMatch } from "@/lib/sessionsApi";
import type { MessageJumpTarget } from "@/hooks/useMessageDeepLink";
import { findTextRanges, highlightsSupported } from "@/shell/previewSearch";

const FIND_DEBOUNCE_MS = 250;
const CHAT_FIND_HIGHLIGHT = "chat-find";

/** Where stepping starts once matches arrive: ⌘F at the newest, a search result at the oldest. */
export type FindStart = "newest" | "oldest";

interface ChatFindBarProps {
  conversationId: string;
  query: string;
  onQueryChange: (query: string) => void;
  start: FindStart;
  inputRef: RefObject<HTMLInputElement | null>;
  /** The match to jump to; a new key on every step. */
  onTarget: (target: MessageJumpTarget | null) => void;
  onClose: () => void;
}

export function ChatFindBar({
  conversationId,
  query,
  onQueryChange,
  start,
  inputRef,
  onTarget,
  onClose,
}: ChatFindBarProps): ReactElement {
  const [result, setResult] = useState<{
    query: string;
    matches: SessionSearchMatch[];
    complete: boolean;
  } | null>(null);
  const [index, setIndex] = useState(0);
  const [step, setStep] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const trimmed = query.trim();
    setFailed(false);
    if (!trimmed) {
      setResult(null);
      return;
    }
    const controller = new AbortController();
    // Pages arrive newest window first. ⌘F starts at the newest match as soon
    // as one lands; a search result starts at the oldest, so it waits for the last page.
    const fetchPages = async () => {
      let matches: SessionSearchMatch[] = [];
      let before: number | undefined;
      let seeded = false;
      do {
        // Each page's cursor comes from the one before it.
        // oxlint-disable-next-line no-await-in-loop
        const page = await searchSessionItems(conversationId, trimmed, {
          before,
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        matches = [...page.matches, ...matches];
        before = page.nextBefore ?? undefined;
        const complete = page.nextBefore === null;
        if (start === "oldest" && !complete) continue;
        setResult({ query: trimmed, matches, complete });
        if (seeded) {
          setIndex((i) => i + page.matches.length);
        } else if (matches.length > 0 || complete) {
          seeded = true;
          setIndex(start === "newest" ? Math.max(0, matches.length - 1) : 0);
          setStep((n) => n + 1);
        }
      } while (before !== undefined);
    };
    const timer = setTimeout(() => {
      fetchPages().catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    }, FIND_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // `start` only seeds the first index of a new query.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, query]);

  const matches = result?.matches ?? [];
  const match = matches[index];
  const matchedQuery = result?.query;
  useEffect(() => {
    onTarget(
      match
        ? {
            key: `find:${step}:${match.id}`,
            candidates: [match.id, match.response_id],
            query: matchedQuery,
          }
        : null,
    );
  }, [match, matchedQuery, step, onTarget]);

  const complete = result?.complete ?? false;
  const stepBy = useCallback(
    (delta: number) => {
      if (matches.length === 0) return;
      // Older pages are still loading, so stepping stops at the oldest loaded match.
      const next = complete
        ? (index + delta + matches.length) % matches.length
        : Math.min(Math.max(index + delta, 0), matches.length - 1);
      if (!complete && next === index) return;
      setIndex(next);
      setStep((n) => n + 1);
    },
    [matches.length, complete, index],
  );

  useEffect(() => {
    const id = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(id);
  }, [inputRef]);

  const searched = result !== null && result.query === query.trim();
  let counter = "";
  if (failed) counter = "Search failed";
  else if (searched && matches.length > 0)
    counter = `${index + 1} / ${matches.length}${complete ? "" : "+"}`;
  else if (searched && complete) counter = "No results";
  else if (query.trim()) counter = "Searching…";

  return (
    <div
      role="search"
      className="pointer-events-auto flex w-80 max-w-[calc(100vw-2rem)] items-center gap-2 rounded-lg border border-border bg-card/95 px-3 py-1.5 shadow-md backdrop-blur"
    >
      <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <input
        ref={inputRef}
        type="text"
        aria-label="Find in chat"
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            stepBy(e.key === "ArrowUp" || (e.key === "Enter" && e.shiftKey) ? -1 : 1);
          } else if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
        placeholder="Find in chat…"
        className="min-w-0 flex-1 bg-transparent text-sm outline-none"
      />
      <span aria-live="polite" className="shrink-0 text-sm text-muted-foreground tabular-nums">
        {counter}
      </span>
      <button
        type="button"
        aria-label="Previous match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={matches.length === 0}
        onClick={() => stepBy(-1)}
      >
        <ChevronUpIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Next match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={matches.length === 0}
        onClick={() => stepBy(1)}
      >
        <ChevronDownIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Close find"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted"
        onClick={onClose}
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}

/**
 * Paint every occurrence of `query` under `root`, repainting as the virtualizer
 * mounts rows or their content renders. No-op where the Highlight API is missing.
 */
export function useChatFindHighlight(query: string, root: HTMLElement | null): void {
  useEffect(() => {
    const trimmed = query.trim();
    if (!root || !trimmed || !highlightsSupported()) return;
    let frame = 0;
    const paint = () => {
      frame = 0;
      CSS.highlights.set(CHAT_FIND_HIGHLIGHT, new Highlight(...findTextRanges(root, trimmed)));
    };
    paint();
    const observer = new MutationObserver(() => {
      if (!frame) frame = requestAnimationFrame(paint);
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      CSS.highlights.delete(CHAT_FIND_HIGHLIGHT);
    };
  }, [query, root]);
}
