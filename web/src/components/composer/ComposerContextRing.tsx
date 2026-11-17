import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatTokenCount } from "@/lib/formatCost";
import { getSessionContextBreakdown } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { ComposerContextBreakdown } from "./ComposerContextBreakdown";

/** Circumference of the progress ring (r=5.5). */
const RING_CIRCUMFERENCE = 2 * Math.PI * 5.5;

/**
 * Compact context-usage ring for the composer workspace bar.
 *
 * Grayscale by design — a near-full context window is a neutral fact, not an
 * error, so the ring never escalates to warning/destructive colors. Self-nulls
 * when there is nothing to show, so the landing/pre-session window (no context
 * figures) renders nothing.
 *
 * @param contextWindow - Total context window in tokens; ``<= 0``/``null``
 *   renders nothing (``0/0`` would be ``NaN%``).
 * @param tokensUsed - Tokens consumed so far, or ``null`` when unknown.
 * @param sessionId - The session whose breakdown a click opens; without one
 *   the ring is a plain tooltip.
 * @param turnActive - Whether a turn is running. An open breakdown refreshes
 *   when a turn starts and ends, and when the transcript's newest entry changes
 *   between turns, since history can change while the measured total stays the same.
 * @param openNonce - Opens the breakdown each time it changes to a new non-zero
 *   value, e.g. from the `/context` command.
 */
export function ComposerContextRing({
  contextWindow,
  tokensUsed,
  sessionId = null,
  turnActive = false,
  openNonce = 0,
  className,
}: {
  contextWindow: number | null;
  tokensUsed: number | null;
  sessionId?: string | null;
  turnActive?: boolean;
  openNonce?: number;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [tooltipOpen, setTooltipOpen] = useState(false);
  // The breakdown is session-local: a conversation switch starts closed.
  useEffect(() => setOpen(false), [sessionId]);
  const appliedOpenNonce = useRef(openNonce);
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    appliedOpenNonce.current = openNonce;
    if (sessionId != null) setOpen(true);
  }, [openNonce, sessionId]);

  if (contextWindow == null || contextWindow <= 0 || tokensUsed == null) return null;

  const pct = Math.min(tokensUsed / contextWindow, 1);
  // Arc, %, label, and tooltip all encode context USED: a fresh session
  // shows an empty ring at 0% and the ring fills as context is consumed.
  const usedArc = pct * RING_CIRCUMFERENCE;
  const usedPct = Math.round(pct * 100);

  const ring = (
    <button
      type="button"
      data-testid="composer-context-ring"
      className={cn(
        "flex shrink-0 items-center rounded-full bg-transparent p-0 text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        className,
      )}
      aria-label={`${usedPct}% of context used`}
    >
      {/* Tight stroke bounds keep the visible icon-to-label gap consistent. */}
      <svg viewBox="1.5 1.5 13 13" width="13" height="13" fill="none" aria-hidden="true">
        {/* Track */}
        <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="2" opacity="0.2" />
        {/* Used arc — skipped at 0, where round linecaps would still paint a dot. */}
        {usedArc > 0 && (
          <circle
            cx="8"
            cy="8"
            r="5.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeDasharray={`${usedArc} ${RING_CIRCUMFERENCE}`}
            transform="rotate(-90 8 8)"
          />
        )}
      </svg>
    </button>
  );
  const tooltip = (
    <TooltipContent side="top" className="flex-col items-start gap-0 px-3 py-2 text-left text-sm">
      <p className="tabular-nums text-ui leading-tight">{usedPct}% context used</p>
      <p className="tabular-nums text-neutral-400 leading-tight dark:text-muted-foreground">
        {formatTokenCount(tokensUsed)} / {formatTokenCount(contextWindow)} tokens
      </p>
    </TooltipContent>
  );
  if (sessionId == null) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{ring}</TooltipTrigger>
        {tooltip}
      </Tooltip>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      {/* The tooltip stays quiet while the breakdown it summarizes is open. */}
      <Tooltip open={tooltipOpen && !open} onOpenChange={setTooltipOpen}>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>{ring}</PopoverTrigger>
        </TooltipTrigger>
        {tooltip}
      </Tooltip>
      <PopoverContent
        side="top"
        align="end"
        collisionPadding={8}
        aria-label="Context window breakdown"
        className="max-h-[min(28rem,var(--radix-popover-content-available-height))] w-[min(22rem,calc(100vw-2rem))] overflow-y-auto p-3"
      >
        <ContextBreakdownLoader
          sessionId={sessionId}
          turnActive={turnActive}
          contextWindow={contextWindow}
          tokensUsed={tokensUsed}
        />
      </PopoverContent>
    </Popover>
  );
}

/** Fetches the breakdown only while the popover body is mounted, i.e. open. */
function ContextBreakdownLoader({
  sessionId,
  turnActive,
  contextWindow,
  tokensUsed,
}: {
  sessionId: string;
  turnActive: boolean;
  contextWindow: number;
  tokensUsed: number;
}) {
  // The newest transcript entry changes when one is appended, committed or merged
  // into; paging older history in prepends, so it leaves the newest entry alone.
  const newestBlock = useChatStore((s) =>
    s.conversationId === sessionId ? s.blocks[s.blocks.length - 1] : undefined,
  );
  const breakdown = useQuery({
    // A new measured total keys a new estimate; history changes refetch below.
    queryKey: ["session-context-breakdown", sessionId, tokensUsed],
    queryFn: ({ signal }) => getSessionContextBreakdown(sessionId, { signal }),
    // Keep this session's last breakdown on screen while a new total's estimate loads.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === sessionId ? previous : undefined,
    staleTime: 0,
    retry: false,
  });
  const { refetch } = breakdown;
  const lastSeen = useRef({ newestBlock, turnActive });
  useEffect(() => {
    const before = lastSeen.current;
    lastSeen.current = { newestBlock, turnActive };
    // Refresh when a turn starts and once it settles, not per streamed entry.
    const turnStarted = turnActive && !before.turnActive;
    const settledChange = !turnActive && (before.turnActive || before.newestBlock !== newestBlock);
    if (turnStarted || settledChange) void refetch();
  }, [newestBlock, turnActive, refetch]);
  return (
    <ComposerContextBreakdown
      contextWindow={contextWindow}
      tokensUsed={tokensUsed}
      breakdown={breakdown.data}
      status={breakdown.status}
    />
  );
}
