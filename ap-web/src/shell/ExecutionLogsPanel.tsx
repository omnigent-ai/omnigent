// Right-side panel that shows the raw JSON items for a conversation,
// scoped to either the main thread or a sub-agent (child) session.
// Triggered from `SessionRail`'s execution-logs card.
//
// Mirrors `TerminalsPanel`'s layout contract (mobile overlay, desktop
// resizable push panel, Esc to close). Sub-agent session names can
// be long (``frontend_engineer · chat-panel-ascii-review-retry``), so
// the session selector is a Select dropdown rather than a horizontal
// tab strip — the dropdown fits its trigger to the panel width and
// lets long names truncate inline rather than wrap.
//
// JSON rendering is intentionally minimal: each item is rendered
// collapsed (one-line ``JSON.stringify``) by default and expands to a
// pretty-printed block on click. Items are numbered ``#1`` (oldest)
// through ``#N`` (newest) so users can see how many turns / items the
// session has accumulated.

import {
  BotIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  MessageSquareIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  executionLogTabKey,
  MAIN_EXECUTION_LOG_KEY,
  useChildSessions,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { useSessionItems, type RawSessionItem } from "@/hooks/useSessionItems";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/** True when the focused session's agent loop is live. */
function useFocusedSessionActive(): boolean {
  const status = useChatStore((s) => s.sessionStatus);
  return status === "running" || status === "waiting";
}

interface ExecutionLogsPanelProps {
  open: boolean;
  /**
   * Parent (main) conversation id. Acts as the session id for the
   * pinned "main" entry and as the parent reference for the
   * child-sessions lookup.
   */
  conversationId: string;
  /**
   * Selector key for the entry to activate on open, e.g.
   * ``"executionLog:main"`` or ``"executionLog:conv_child123"``.
   * ``null`` when ``open`` is false.
   */
  initialKey: string | null;
  onClose: () => void;
}

interface LogEntry {
  /** Stable selector key, e.g. ``"executionLog:main"``. */
  key: string;
  /** Session/conversation id the entry pulls items for. */
  sessionId: string;
  /** Display label shown in the dropdown and trigger. */
  label: string;
  /**
   * Icon rendered alongside the label in the dropdown trigger and
   * items. Matches the rail-card iconography
   * (:class:`MessageSquareIcon` for main, :class:`BotIcon` for
   * sub-agents) so users can recognize entries at a glance.
   */
  icon: LucideIcon;
}

/** Poll interval for the active session's items list while the panel is open. */
const ITEMS_POLL_MS = 3_000;

export function ExecutionLogsPanel({
  open,
  conversationId,
  initialKey,
  onClose,
}: ExecutionLogsPanelProps) {
  // No child-sessions poll — status arrives live over the session stream
  // (see chatStore ``session_child_session_updated``).
  const { children } = useChildSessions(open ? conversationId : null);
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(open);
  const [activeKey, setActiveKey] = useState<string>("");
  const ref = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) {
      setActiveKey("");
      return;
    }
    if (initialKey) setActiveKey(initialKey);
  }, [open, initialKey]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (ref.current) {
      if (open) {
        ref.current.removeAttribute("inert");
      } else {
        ref.current.setAttribute("inert", "");
      }
    }
  }, [open]);

  const entries = buildLogEntries(conversationId, children);
  const activeEntry =
    entries.find((e) => e.key === activeKey) ?? entries[0] ?? null;

  return (
    <aside
      ref={ref}
      data-testid="execution-logs-panel"
      data-state={open ? "open" : "closed"}
      style={{ width: panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0 md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
    >
      {isDesktop && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      <header className="flex shrink-0 items-center justify-between border-border border-b px-4 py-3">
        <h2 className="font-medium text-sm">Execution logs</h2>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
          <XIcon className="size-4" />
        </Button>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3 p-4">
        {!open || !activeEntry ? (
          <div className="flex-1" />
        ) : (
          <>
            <Select value={activeEntry.key} onValueChange={setActiveKey}>
              {/* The trigger already has `w-fit` by default. We add
                  `self-start` so the flex column doesn't stretch it
                  across the panel's cross-axis — without this, the
                  trigger fills the full panel width regardless of
                  content. */}
              <SelectTrigger className="self-start">
                <SelectValue>
                  <span className="inline-flex items-center gap-2">
                    <activeEntry.icon className="size-3.5 shrink-0 text-muted-foreground" />
                    {activeEntry.label}
                  </span>
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {entries.map((e) => (
                  <SelectItem key={e.key} value={e.key}>
                    <span className="inline-flex items-center gap-2">
                      <e.icon className="size-3.5 shrink-0 text-muted-foreground" />
                      {e.label}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* Remount the items list when the session changes so the
                expand/collapse state resets between selections. */}
            <SessionItemsList key={activeEntry.sessionId} sessionId={activeEntry.sessionId} />
          </>
        )}
      </div>
    </aside>
  );
}

/**
 * Build the entry list: "main" pinned first, then every child session
 * in the order returned by the child_sessions endpoint.
 */
function buildLogEntries(
  conversationId: string,
  children: ChildSessionInfo[],
): LogEntry[] {
  const main: LogEntry = {
    key: executionLogTabKey(MAIN_EXECUTION_LOG_KEY),
    sessionId: conversationId,
    label: "main",
    icon: MessageSquareIcon,
  };
  const childEntries: LogEntry[] = children.map((c) => ({
    key: executionLogTabKey(c.id),
    sessionId: c.id,
    label: c.title ?? c.tool ?? c.id,
    icon: BotIcon,
  }));
  return [main, ...childEntries];
}

function SessionItemsList({ sessionId }: { sessionId: string }) {
  const sessionActive = useFocusedSessionActive();
  const { items, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useSessionItems(sessionId, sessionActive ? ITEMS_POLL_MS : null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollRootRef = useRef<HTMLDivElement | null>(null);

  // Trigger the next page when the bottom sentinel scrolls into the
  // panel's viewport. We scope the observer to the scroll container
  // (not the document) because the panel scrolls internally; using
  // ``root: null`` would only fire when the sentinel reached the
  // browser viewport, which never happens inside a fixed-height
  // panel.
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRootRef.current;
    if (!sentinel || !root || !hasNextPage || isFetchingNextPage) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          fetchNextPage();
        }
      },
      // Pre-fetch a viewport before the sentinel is visible so the
      // next page is on the wire before the user runs out of rows.
      { root, rootMargin: "200px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  if (isLoading) {
    return <div className="text-muted-foreground text-xs">Loading…</div>;
  }
  if (error) {
    return (
      <div className="text-destructive text-xs">Failed to load items: {error.message}</div>
    );
  }
  if (items.length === 0) {
    return <div className="text-muted-foreground text-xs">No items</div>;
  }
  return (
    <div
      ref={scrollRootRef}
      className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pr-1 font-mono text-xs"
    >
      {items.map((item, idx) => (
        <SessionItemEntry key={itemKey(item, idx)} item={item} index={idx + 1} />
      ))}
      {hasNextPage && (
        <div ref={sentinelRef} className="py-2 text-center text-muted-foreground text-xs">
          {isFetchingNextPage ? "Loading more…" : ""}
        </div>
      )}
    </div>
  );
}

function SessionItemEntry({ item, index }: { item: RawSessionItem; index: number }) {
  const [isExpanded, setIsExpanded] = useState(false);
  // The collapsed form is single-line, trimmed to keep rows compact;
  // the expanded form is the canonical 2-space-indented dump that
  // mirrors the TUI Ctrl+O view.
  const collapsed = JSON.stringify(item);
  const expanded = JSON.stringify(item, null, 2);
  return (
    <div className="rounded-md border border-border bg-muted/40">
      <button
        type="button"
        aria-expanded={isExpanded}
        data-testid="execution-log-entry"
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left hover:bg-muted/60"
        onClick={() => setIsExpanded((v) => !v)}
      >
        {isExpanded ? (
          <ChevronDownIcon className="size-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRightIcon className="size-3 shrink-0 text-muted-foreground" />
        )}
        <span className="shrink-0 text-muted-foreground">#{index}</span>
        {!isExpanded && (
          <span className="truncate text-foreground">{collapsed}</span>
        )}
      </button>
      {isExpanded && (
        <pre className="whitespace-pre-wrap break-words border-t border-border px-2 py-1.5 text-foreground">
          {expanded}
        </pre>
      )}
    </div>
  );
}

/**
 * Best-effort stable React key for an item. Prefers the wire ``id``;
 * falls back to the array index when missing so the panel still
 * renders for malformed rows.
 */
function itemKey(item: RawSessionItem, idx: number): string {
  const id = item.id;
  return typeof id === "string" && id ? id : `idx-${idx}`;
}
