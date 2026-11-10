import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { formatSessionCostUsd } from "@/lib/formatCost";

export function SubagentCost({ costUsd }: { costUsd?: number | null }) {
  if (!isFeatureEnabled(useServerInfo(), "session_overview")) return null;
  const cost = costUsd === null || costUsd === undefined ? null : formatSessionCostUsd(costUsd);
  return (
    <span
      role="note"
      className="shrink-0 text-xs tabular-nums text-muted-foreground"
      title="Cost for this agent only, excluding subagents"
      aria-label={`Agent cost: ${cost ?? "unavailable"}; excludes subagents`}
    >
      {cost ?? "—"}
    </span>
  );
}
