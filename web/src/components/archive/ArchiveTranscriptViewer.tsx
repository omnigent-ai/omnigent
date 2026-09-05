import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  ArrowDownIcon,
  ArrowDownToLineIcon,
  ArrowLeftIcon,
  ArrowUpIcon,
  CheckIcon,
  CopyIcon,
  ExternalLinkIcon,
  LinkIcon,
  SearchIcon,
} from "lucide-react";
import {
  type ReactElement,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import {
  fetchSessionItemsPage,
  fetchSessionItemsWindow,
  INITIAL_WINDOW_ITEMS,
  searchSessionItems,
} from "@/lib/sessionsApi";
import { getSessionDeepLink, type SessionLinkTarget } from "@/lib/sessionLinks";
import { useNavigate, useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { BubbleView, collectBubbleMarkdown } from "@/pages/ChatPage";
import { TurnRail, type Turn } from "@/pages/TurnRail";
import type { Conversation } from "@/hooks/useConversations";

interface ArchiveTranscriptViewerProps {
  conversation: Conversation | null;
  onBack?: () => void;
  className?: string;
  returnFocusRef?: RefObject<HTMLElement | null>;
}

interface SelectedQuote {
  text: string;
  target: SessionLinkTarget;
}

const EMPTY_SEARCH_MATCHES: Awaited<ReturnType<typeof searchSessionItems>>["items"] = [];

function ExplainedAction({ content, children }: { content: string; children: ReactElement }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-72">{content}</TooltipContent>
    </Tooltip>
  );
}

function bubbleKey(bubble: Bubble): string {
  if (bubble.kind === "assistant") return `assistant:${bubble.stableId}`;
  if (bubble.kind === "user") return `user:${bubble.stableKey ?? bubble.itemId}`;
  return `${bubble.kind}:${bubble.itemId}`;
}

function bubbleItemIds(bubble: Bubble): string[] {
  if (bubble.kind === "user") return [bubble.itemId];
  if (bubble.kind === "assistant") {
    return bubble.items.flatMap((item) => (item.itemId ? [item.itemId] : []));
  }
  return [bubble.itemId];
}

function bubbleText(bubble: Bubble): string {
  if (bubble.kind === "assistant") return collectBubbleMarkdown(bubble.items).trim();
  if (bubble.kind !== "user") return "";
  return bubble.content
    .flatMap((part) => ("text" in part && typeof part.text === "string" ? [part.text] : []))
    .join("\n")
    .trim();
}

function bubbleTarget(bubble: Bubble): SessionLinkTarget {
  if (bubble.kind === "assistant") {
    const itemId = bubble.items.find((item) => item.itemId)?.itemId;
    return itemId
      ? { kind: "response", responseId: bubble.responseId, itemId }
      : { kind: "session" };
  }
  return { kind: "item", itemId: bubble.itemId };
}

function citation(
  text: string,
  conversation: Conversation,
  link: string,
  target: SessionLinkTarget,
): string {
  const excerpt = text.length > 600 ? `${text.slice(0, 599).trimEnd()}…` : text;
  const targetLabel =
    target.kind === "response"
      ? `response:${target.responseId}`
      : target.kind === "item"
        ? `item:${target.itemId}`
        : "session";
  const clean = (value: string) => value.replace(/[|\r\n⟦⟧]+/g, " ").trim();
  const title = clean(conversation.title?.trim() || "Untitled session");
  const agent = clean(conversation.agent_name ?? "unknown-agent");
  const host = clean(conversation.host_id ?? "unknown-host");
  const quoted = excerpt
    .split(/\r?\n/)
    .map((line) => `> ${line}`)
    .join("\n");
  return `⟦Omnigent reference | session=${conversation.id} | target=${targetLabel} | agent=${agent} | host=${host} | title=${title} | link=${link}⟧\n${quoted}`;
}

export function ArchiveTranscriptViewer({
  conversation,
  onBack,
  className,
  returnFocusRef,
}: ArchiveTranscriptViewerProps) {
  const navigate = useNavigate();
  const rebasePath = useRebasePath();
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [selectedQuote, setSelectedQuote] = useState<SelectedQuote | null>(null);
  const [sessionSearch, setSessionSearch] = useState("");
  const [debouncedSessionSearch, setDebouncedSessionSearch] = useState("");
  const [matchIndex, setMatchIndex] = useState(0);
  const [activeTurnIndex, setActiveTurnIndex] = useState(0);
  const [atBottom, setAtBottom] = useState(false);
  const pendingScrollAnchor = useRef<{ itemId: string; top: number } | null>(null);
  const pendingScrollToBottom = useRef(false);
  const touchStartY = useRef<number | null>(null);
  const requestedFocusItemId = conversation?.search_match?.item_id ?? null;
  const [showMatchWindow, setShowMatchWindow] = useState(requestedFocusItemId !== null);
  const sessionId = conversation?.id ?? null;

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSessionSearch(sessionSearch.trim()), 200);
    return () => window.clearTimeout(timeout);
  }, [sessionSearch]);

  const sessionSearchQuery = useQuery({
    queryKey: ["archive-transcript-search", sessionId, debouncedSessionSearch],
    queryFn: () => searchSessionItems(sessionId as string, debouncedSessionSearch),
    enabled: sessionId !== null && debouncedSessionSearch.length > 0,
    staleTime: 60_000,
    retry: false,
  });
  const matches = sessionSearchQuery.data?.items ?? EMPTY_SEARCH_MATCHES;
  const internalFocusItemId = debouncedSessionSearch ? (matches[matchIndex]?.id ?? null) : null;
  const focusItemId = internalFocusItemId ?? (showMatchWindow ? requestedFocusItemId : null);

  useEffect(() => {
    setShowMatchWindow(requestedFocusItemId !== null);
  }, [requestedFocusItemId, sessionId]);

  useEffect(() => {
    setSelectedQuote(null);
    setCopied(null);
    setSessionSearch("");
    setDebouncedSessionSearch("");
    setMatchIndex(0);
    setActiveTurnIndex(0);
    setAtBottom(false);
  }, [sessionId]);

  useEffect(() => {
    setMatchIndex(0);
  }, [debouncedSessionSearch]);

  const focusedQuery = useQuery({
    queryKey: ["archive-transcript-window", sessionId, focusItemId],
    queryFn: () => fetchSessionItemsWindow(sessionId as string, focusItemId as string),
    enabled: sessionId !== null && focusItemId !== null,
    staleTime: 60_000,
    retry: false,
  });
  const historyQuery = useInfiniteQuery({
    queryKey: ["archive-transcript", sessionId],
    queryFn: ({ pageParam }) =>
      fetchSessionItemsPage(sessionId as string, {
        olderThan: pageParam,
        limit: INITIAL_WINDOW_ITEMS,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.hasMore && page.items[0]?.id ? page.items[0].id : undefined),
    enabled: sessionId !== null && focusItemId === null,
    staleTime: 60_000,
    retry: false,
  });

  const items = useMemo(() => {
    if (focusItemId !== null) return focusedQuery.data?.items ?? [];
    const pages = historyQuery.data?.pages ?? [];
    const seen = new Set<string>();
    return [...pages]
      .reverse()
      .flatMap((page) => page.items)
      .filter((item) => (seen.has(item.id) ? false : (seen.add(item.id), true)));
  }, [focusItemId, focusedQuery.data, historyQuery.data]);
  const bubbles = useMemo(() => buildBubbles(itemsToBlocks(items), null), [items]);
  const turnBubbleIndexes = useMemo(() => {
    const userIndexes = bubbles.flatMap((bubble, index) => (bubble.kind === "user" ? [index] : []));
    return userIndexes.length > 0 ? userIndexes : bubbles.map((_, index) => index);
  }, [bubbles]);
  const turnIndexByBubble = useMemo(
    () => new Map(turnBubbleIndexes.map((bubbleIndex, turnIndex) => [bubbleIndex, turnIndex])),
    [turnBubbleIndexes],
  );
  const turns = useMemo<Turn[]>(
    () =>
      turnBubbleIndexes.map((bubbleIndex) => {
        const bubble = bubbles[bubbleIndex];
        const reply = bubbles[bubbleIndex + 1];
        return {
          itemId: bubbleItemIds(bubble)[0] ?? bubbleKey(bubble),
          userText: bubbleText(bubble),
          responsePreview: reply?.kind === "assistant" ? bubbleText(reply) : "",
        };
      }),
    [bubbles, turnBubbleIndexes],
  );
  const loading = focusItemId !== null ? focusedQuery.isLoading : historyQuery.isLoading;
  const error = focusItemId !== null ? focusedQuery.error : historyQuery.error;
  const markerIndexes = useMemo(() => {
    if (matches.length <= 80) return matches.map((_, index) => index);
    return Array.from({ length: 80 }, (_, index) =>
      Math.round((index * (matches.length - 1)) / 79),
    );
  }, [matches]);

  const updateAtBottom = useCallback(() => {
    const scroller = transcriptRef.current;
    if (!scroller) return;
    const reachedBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 8;
    setAtBottom(reachedBottom);
    const turnElements = [...scroller.querySelectorAll<HTMLElement>("[data-archive-turn]")];
    if (turnElements.length === 0) return;
    if (reachedBottom && scroller.scrollHeight > scroller.clientHeight + 8) {
      setActiveTurnIndex(Number(turnElements.at(-1)?.dataset.archiveTurn ?? 0));
      return;
    }
    const readingLine =
      scroller.getBoundingClientRect().top + Math.min(120, scroller.clientHeight / 4);
    let current = 0;
    for (const element of turnElements) {
      if (element.getBoundingClientRect().top > readingLine) break;
      current = Number(element.dataset.archiveTurn ?? 0);
    }
    setActiveTurnIndex(current);
  }, []);

  const jumpToTurn = useCallback((turnIndex: number) => {
    const element = transcriptRef.current?.querySelector<HTMLElement>(
      `[data-archive-turn="${turnIndex}"]`,
    );
    element?.scrollIntoView({ block: "start", behavior: "smooth" });
    setActiveTurnIndex(turnIndex);
  }, []);

  const jumpToTurnItem = useCallback(
    (itemId: string) => {
      const turnIndex = turns.findIndex((turn) => turn.itemId === itemId);
      if (turnIndex >= 0) jumpToTurn(turnIndex);
    },
    [jumpToTurn, turns],
  );

  const jumpToBottom = useCallback(() => {
    setSessionSearch("");
    setDebouncedSessionSearch("");
    setShowMatchWindow(false);
    setMatchIndex(0);
    if (focusItemId === null) {
      const scroller = transcriptRef.current;
      scroller?.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
      setAtBottom(true);
      return;
    }
    pendingScrollToBottom.current = true;
  }, [focusItemId]);

  useEffect(() => {
    if (!pendingScrollToBottom.current || loading || focusItemId !== null) return;
    const scroller = transcriptRef.current;
    if (!scroller) return;
    pendingScrollToBottom.current = false;
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
  }, [bubbles.length, focusItemId, loading]);

  useEffect(() => {
    updateAtBottom();
  }, [bubbles.length, updateAtBottom]);

  const loadEarlier = useCallback(() => {
    if (
      focusItemId !== null ||
      !historyQuery.hasNextPage ||
      historyQuery.isFetchingNextPage ||
      !transcriptRef.current
    ) {
      return;
    }
    const first = transcriptRef.current.querySelector<HTMLElement>("[data-archive-item]");
    const itemId = first?.dataset.archiveItem?.split(" ")[0];
    if (first && itemId) {
      pendingScrollAnchor.current = { itemId, top: first.getBoundingClientRect().top };
    }
    void historyQuery.fetchNextPage();
  }, [focusItemId, historyQuery]);

  useEffect(() => {
    const anchor = pendingScrollAnchor.current;
    const scroller = transcriptRef.current;
    if (!anchor || !scroller || historyQuery.isFetchingNextPage) return;
    const element = [...scroller.querySelectorAll<HTMLElement>("[data-archive-item]")].find(
      (candidate) => candidate.dataset.archiveItem?.split(" ").includes(anchor.itemId),
    );
    if (element) scroller.scrollTop += element.getBoundingClientRect().top - anchor.top;
    pendingScrollAnchor.current = null;
  }, [bubbles.length, historyQuery.isFetchingNextPage]);

  useEffect(() => {
    if (!focusItemId || items.length === 0) return;
    const element = [
      ...(transcriptRef.current?.querySelectorAll<HTMLElement>("[data-archive-item]") ?? []),
    ].find((candidate) => candidate.dataset.archiveItem?.split(" ").includes(focusItemId));
    element?.scrollIntoView?.({ block: "center" });
  }, [focusItemId, items]);

  useEffect(() => {
    if (copied === null) return;
    const timeout = window.setTimeout(() => setCopied(null), 1600);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  if (conversation === null) {
    return (
      <div className={cn("flex min-h-0 min-w-0 flex-1 items-center justify-center p-6", className)}>
        <p className="max-w-xs text-center text-sm text-muted-foreground">
          Select an archived session to read its conversation.
        </p>
      </div>
    );
  }

  const title = conversation.title?.trim() || "Untitled session";
  const sessionLink = getSessionDeepLink(conversation.id, rebasePath);
  const copy = (value: string, key: string) => {
    void copyText(value).then(() => setCopied(key));
  };

  return (
    <section
      className={cn(
        "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background",
        className,
      )}
      data-testid="archive-transcript-viewer"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        {onBack && (
          <Button type="button" variant="ghost" size="icon-sm" onClick={onBack}>
            <ArrowLeftIcon className="size-4" />
            <span className="sr-only">Back to archive list</span>
          </Button>
        )}
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-semibold" title={title}>
            {title}
          </h2>
          <p className="truncate font-mono text-[11px] text-muted-foreground">
            {conversation.agent_name ?? "Agent not recorded"} · {conversation.id}
          </p>
        </div>
        <ExplainedAction content="Copy the exact Session ID for API calls, logs, and cross-device lookup.">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Copy session ID"
            onClick={() => copy(conversation.id, "session-id")}
          >
            {copied === "session-id" ? <CheckIcon /> : <CopyIcon />}
          </Button>
        </ExplainedAction>
        <ExplainedAction content="Copy a portable Omnigent deep link that opens this archived session on any connected device that can reach its Server.">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Copy session link"
            onClick={() => copy(sessionLink, "session-link")}
          >
            {copied === "session-link" ? <CheckIcon /> : <LinkIcon />}
          </Button>
        </ExplainedAction>
        <ExplainedAction content="Open the complete session page without restoring or changing its archived state.">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Open full session"
            onClick={() => navigate(`/c/${conversation.id}`)}
          >
            <ExternalLinkIcon />
          </Button>
        </ExplainedAction>
      </header>

      <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-2">
        <div className="relative min-w-0 flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={sessionSearch}
            onChange={(event) => setSessionSearch(event.target.value)}
            aria-label="Search this archived session"
            placeholder="Search this session…"
            className="h-8 pl-8 text-base md:text-sm"
          />
        </div>
        {debouncedSessionSearch && (
          <>
            <span
              className="min-w-12 text-center text-xs tabular-nums text-muted-foreground"
              aria-live="polite"
            >
              {sessionSearchQuery.isFetching
                ? "…"
                : matches.length === 0
                  ? "0 / 0"
                  : `${matchIndex + 1} / ${matches.length}${sessionSearchQuery.data?.hasMore ? "+" : ""}`}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Previous session search match"
              disabled={matches.length < 2}
              onClick={() =>
                setMatchIndex((current) => (current - 1 + matches.length) % matches.length)
              }
            >
              <ArrowUpIcon />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Next session search match"
              disabled={matches.length < 2}
              onClick={() => setMatchIndex((current) => (current + 1) % matches.length)}
            >
              <ArrowDownIcon />
            </Button>
          </>
        )}
      </div>

      {selectedQuote && (
        <div className="flex shrink-0 items-center gap-2 border-b bg-accent/40 px-3 py-2">
          <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            “{selectedQuote.text}”
          </p>
          <ExplainedAction content="Copy the selected words together with source metadata and a deep link, ready to paste into another session as directed context.">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() =>
                copy(
                  citation(
                    selectedQuote.text,
                    conversation,
                    getSessionDeepLink(conversation.id, rebasePath, selectedQuote.target),
                    selectedQuote.target,
                  ),
                  "selection",
                )
              }
            >
              {copied === "selection" ? <CheckIcon /> : <CopyIcon />}
              Copy citation
            </Button>
          </ExplainedAction>
        </div>
      )}

      <div className="relative min-h-0 min-w-0 flex-1">
        {debouncedSessionSearch && markerIndexes.length > 1 ? (
          <nav
            className="absolute top-3 bottom-3 left-1 z-10 hidden w-4 flex-col justify-center gap-0.5 md:flex"
            aria-label="Session search matches"
          >
            {markerIndexes.map((index) => (
              <button
                key={index}
                type="button"
                aria-label={`Open search match ${index + 1}`}
                aria-current={matchIndex === index || undefined}
                className={cn(
                  "h-2 w-4 rounded-sm before:mx-auto before:block before:h-0.5 before:w-2 before:rounded-full before:bg-muted-foreground/45",
                  matchIndex === index && "before:w-3 before:bg-foreground",
                )}
                onClick={() => setMatchIndex(index)}
              />
            ))}
          </nav>
        ) : (
          <TurnRail
            turns={turns}
            scroller={transcriptRef.current ? { el: transcriptRef.current } : null}
            hasMoreHistory={focusItemId === null && Boolean(historyQuery.hasNextPage)}
            loadingMoreHistory={historyQuery.isFetchingNextPage}
            onJump={jumpToTurnItem}
            onLoadMoreHistory={loadEarlier}
          />
        )}
        {!debouncedSessionSearch && turnBubbleIndexes.length > 1 && (
          <nav
            className="absolute bottom-[calc(0.75rem+env(safe-area-inset-bottom))] left-3 z-20 flex items-center rounded-full border border-border bg-secondary/95 shadow-md backdrop-blur md:hidden"
            aria-label="Conversation turn controls"
          >
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              aria-label="Previous turn"
              disabled={activeTurnIndex === 0}
              onClick={() => jumpToTurn(Math.max(0, activeTurnIndex - 1))}
            >
              <ArrowUpIcon />
            </Button>
            <span className="min-w-10 text-center text-[11px] tabular-nums text-muted-foreground">
              {activeTurnIndex + 1}/{turnBubbleIndexes.length}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              aria-label="Next turn"
              disabled={activeTurnIndex >= turnBubbleIndexes.length - 1}
              onClick={() =>
                jumpToTurn(Math.min(turnBubbleIndexes.length - 1, activeTurnIndex + 1))
              }
            >
              <ArrowDownIcon />
            </Button>
          </nav>
        )}
        <div
          ref={transcriptRef}
          data-testid="archive-transcript"
          tabIndex={0}
          className={cn(
            "h-full min-h-0 min-w-0 overflow-x-hidden overflow-y-auto px-3 pt-4 pb-[calc(4rem+env(safe-area-inset-bottom))] md:pb-4",
            ((debouncedSessionSearch && markerIndexes.length > 1) ||
              (!debouncedSessionSearch && turnBubbleIndexes.length > 1)) &&
              "md:pl-7",
          )}
          onScroll={updateAtBottom}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            const active = returnFocusRef?.current?.querySelector<HTMLElement>(
              '[aria-selected="true"], [data-active="true"] [data-testid="archived-open-session"]',
            );
            active?.focus();
          }}
          onWheel={(event) => {
            if (event.currentTarget.scrollTop <= 1 && event.deltaY < 0) loadEarlier();
          }}
          onTouchStart={(event) => {
            touchStartY.current = event.touches[0]?.clientY ?? null;
          }}
          onTouchMove={(event) => {
            const y = event.touches[0]?.clientY;
            if (
              y !== undefined &&
              touchStartY.current !== null &&
              event.currentTarget.scrollTop <= 1 &&
              y - touchStartY.current > 36
            ) {
              touchStartY.current = y;
              loadEarlier();
            }
          }}
          onMouseUp={() => {
            const selection = window.getSelection();
            const text = selection?.toString().trim() ?? "";
            const anchor = selection?.anchorNode;
            if (!text || !anchor || !transcriptRef.current?.contains(anchor)) {
              setSelectedQuote(null);
              return;
            }
            const anchorElement =
              anchor.nodeType === Node.ELEMENT_NODE ? (anchor as Element) : anchor.parentElement;
            const article = anchorElement?.closest<HTMLElement>("[data-archive-item]");
            const itemId = article?.dataset.archiveItem?.split(" ")[0];
            if (!itemId) {
              setSelectedQuote(null);
              return;
            }
            const responseId = article.dataset.archiveResponse;
            setSelectedQuote({
              text: text.slice(0, 1200),
              target: responseId
                ? { kind: "response", responseId, itemId }
                : { kind: "item", itemId },
            });
          }}
        >
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading conversation…</p>
          ) : error ? (
            <p className="text-sm text-destructive">Couldn’t load this archived conversation.</p>
          ) : bubbles.length === 0 ? (
            <p className="text-sm text-muted-foreground">This session has no visible messages.</p>
          ) : (
            <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-3">
              {focusItemId === null && historyQuery.hasNextPage && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={historyQuery.isFetchingNextPage}
                  onClick={loadEarlier}
                >
                  {historyQuery.isFetchingNextPage ? "Loading…" : "Load earlier conversation"}
                </Button>
              )}
              {focusItemId !== null && !debouncedSessionSearch && (
                <div className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
                  <span>Showing the matching conversation window</span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    onClick={() => setShowMatchWindow(false)}
                  >
                    View full conversation
                  </Button>
                </div>
              )}
              {bubbles.map((bubble, bubbleIndex) => {
                const ids = bubbleItemIds(bubble);
                const text = bubbleText(bubble);
                const target = bubbleTarget(bubble);
                const link = getSessionDeepLink(conversation.id, rebasePath, target);
                const active = focusItemId !== null && ids.includes(focusItemId);
                const turnIndex = turnIndexByBubble.get(bubbleIndex);
                return (
                  <article
                    key={bubbleKey(bubble)}
                    data-archive-item={ids.join(" ")}
                    data-archive-response={
                      target.kind === "response" ? target.responseId : undefined
                    }
                    data-archive-match={active || undefined}
                    data-archive-turn={turnIndex}
                    data-user-message-id={
                      turnIndex !== undefined ? turns[turnIndex]?.itemId : undefined
                    }
                    className={cn(
                      "group/archive relative min-w-0 rounded-lg px-2 py-1 ring-offset-background",
                      active && "bg-primary/10 ring-2 ring-primary/50",
                    )}
                  >
                    <BubbleView
                      bubble={bubble}
                      readOnly
                      sessionId={conversation.id}
                      isLastAssistant={false}
                    />
                    {text && (
                      <div className="flex justify-end gap-1 pt-1 opacity-60 transition-opacity md:opacity-0 md:group-hover/archive:opacity-100 md:group-focus-within/archive:opacity-100">
                        <ExplainedAction content="Citation copies this message or response text, its source metadata, and its directed deep link so another session can ingest it as context.">
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            aria-label="Copy message citation"
                            onClick={() =>
                              copy(
                                citation(text, conversation, link, target),
                                `citation:${bubbleKey(bubble)}`,
                              )
                            }
                          >
                            {copied === `citation:${bubbleKey(bubble)}` ? (
                              <CheckIcon />
                            ) : (
                              <CopyIcon />
                            )}
                            Citation
                          </Button>
                        </ExplainedAction>
                        <ExplainedAction content="Link copies only the deep link to this exact message or response; it does not include the message text.">
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            aria-label="Copy message link"
                            onClick={() => copy(link, `link:${bubbleKey(bubble)}`)}
                          >
                            {copied === `link:${bubbleKey(bubble)}` ? <CheckIcon /> : <LinkIcon />}
                            Link
                          </Button>
                        </ExplainedAction>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>
        {(focusItemId !== null || !atBottom) && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="secondary"
                size="icon-sm"
                className="absolute right-3 bottom-[calc(0.75rem+env(safe-area-inset-bottom))] z-20 rounded-full shadow-md max-md:size-11 md:bottom-3"
                aria-label="Jump to end of session"
                onClick={jumpToBottom}
              >
                <ArrowDownToLineIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Jump to end</TooltipContent>
          </Tooltip>
        )}
      </div>
    </section>
  );
}
