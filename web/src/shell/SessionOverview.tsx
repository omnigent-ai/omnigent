// WIP(session_overview): first cut with the session total and per-model costs;
// sub-agent costs come later.

import { type ReactNode, useSyncExternalStore } from "react";
import { XIcon } from "lucide-react";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import { useChatStore } from "@/store/chatStore";

// Tailwind's `xl` breakpoint: wide enough to dock the overview beside chat.
const WIDE_QUERY = "(min-width: 1280px)";

function subscribeWide(callback: () => void): () => void {
  const mql = window.matchMedia(WIDE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

/** True when the viewport is at least Tailwind's `xl` breakpoint. */
export function useIsWideViewport(): boolean {
  return useSyncExternalStore(
    subscribeWide,
    () => window.matchMedia(WIDE_QUERY).matches,
    () => false,
  );
}

/** The bound session's cost: a spinner while the first read runs, then the value. */
function SessionCostValue({ unavailable }: { unavailable: string }) {
  const cost = useChatStore((s) => s.sessionCostUsd);
  const loading = useChatStore((s) => s.loadingConversation || s.sessionUsageLoading);
  if (cost !== null) return formatSessionCostUsd(cost);
  return loading ? <Spinner aria-label="Loading cost" className="size-3.5" /> : unavailable;
}

/**
 * Desktop header chip with the session cost. It toggles the docked overview, or
 * opens `children` (the overview card) as a popover beneath it.
 */
export function SessionCostButton({
  docked,
  open,
  onOpenChange,
  children,
}: {
  docked: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children?: ReactNode;
}) {
  const chip = (
    <button
      type="button"
      aria-expanded={open}
      data-testid="header-session-cost"
      className="hidden h-6 shrink-0 items-center rounded-[6px] px-2 font-mono text-xs tabular-nums text-muted-foreground hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground md:inline-flex"
      // The popover trigger toggles the popover itself.
      onClick={docked ? () => onOpenChange(!open) : undefined}
    >
      <SessionCostValue unavailable="—" />
      <span className="sr-only"> Session overview</span>
    </button>
  );
  if (docked) return chip;
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{chip}</PopoverTrigger>
      <PopoverContent
        align="end"
        aria-label="Session overview"
        className="w-64 bg-transparent p-0 shadow-none ring-0 max-md:hidden"
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}

/** One overview line: a label and its value, left empty while unknown. */
function OverviewRow({
  label,
  title,
  testId,
  children,
}: {
  label: string;
  /** Full text for a label that may truncate. */
  title?: string;
  testId?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="min-w-0 truncate text-muted-foreground" title={title}>
        {label}
      </span>
      <span className="flex shrink-0 items-center font-mono tabular-nums" data-testid={testId}>
        {children}
      </span>
    </div>
  );
}

/** Session cost, each model's cost and the total tokens for the bound session. */
export function SessionOverview({ onClose }: { onClose: () => void }) {
  const usageByModel = useChatStore((s) => s.sessionUsageByModel);
  // Most expensive model first; unpriced models last.
  const models = Object.entries(usageByModel ?? {}).sort(
    ([, a], [, b]) => (b.totalCostUsd ?? -1) - (a.totalCostUsd ?? -1),
  );
  const tokenCounts = models.map(([, usage]) => usage.totalTokens);
  // A model without a count would make the sum partial, so show none.
  const totalTokens =
    tokenCounts.length > 0 && tokenCounts.every((count) => count != null)
      ? tokenCounts.reduce((sum, count) => sum + count, 0)
      : null;
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="text-ui">Session overview</CardTitle>
        <CardAction>
          <button
            type="button"
            aria-label="Hide session overview"
            className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={onClose}
          >
            <XIcon className="size-3.5" />
          </button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <OverviewRow label="Session cost" testId="session-overview-cost">
          <SessionCostValue unavailable="Unavailable" />
        </OverviewRow>
        {models.map(([model, usage]) => (
          <OverviewRow key={model} label={model} title={model}>
            {usage.totalCostUsd != null && formatSessionCostUsd(usage.totalCostUsd)}
          </OverviewRow>
        ))}
        {totalTokens !== null && (
          <OverviewRow label="Tokens">{formatTokenCount(totalTokens)}</OverviewRow>
        )}
      </CardContent>
    </Card>
  );
}
