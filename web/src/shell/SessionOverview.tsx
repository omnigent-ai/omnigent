// WIP(session_overview): first cut with the session total and per-model usage;
// sub-agent costs come later.

import { useSyncExternalStore } from "react";
import { XIcon } from "lucide-react";
import { ModelUsageBreakdown } from "@/components/AgentInfo";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { formatSessionCostUsd } from "@/lib/formatCost";
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

/** Desktop header chip with the session cost that shows and hides the overview. */
export function SessionCostButton({
  expanded,
  onToggle,
}: {
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={expanded}
      title={expanded ? "Hide session overview" : "Show session overview"}
      data-testid="header-session-cost"
      className="hidden h-6 shrink-0 items-center rounded-[6px] px-2 font-mono text-xs tabular-nums text-muted-foreground hover:bg-muted hover:text-foreground md:inline-flex"
      onClick={onToggle}
    >
      <SessionCostValue unavailable="—" />
      <span className="sr-only"> Session overview</span>
    </button>
  );
}

/** Session cost and per-model token usage for the bound session. */
export function SessionOverview({ onClose }: { onClose: () => void }) {
  const usageByModel = useChatStore((s) => s.sessionUsageByModel);
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
      <CardContent className="flex flex-col gap-3 text-sm">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-muted-foreground">Session cost</span>
          <span
            className="flex items-center font-mono tabular-nums"
            data-testid="session-overview-cost"
          >
            <SessionCostValue unavailable="Unavailable" />
          </span>
        </div>
        {usageByModel != null && Object.keys(usageByModel).length > 0 && (
          <ModelUsageBreakdown usageByModel={usageByModel} />
        )}
      </CardContent>
    </Card>
  );
}
