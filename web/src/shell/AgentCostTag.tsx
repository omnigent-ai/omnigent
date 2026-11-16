import { agentCostLabel, type AgentCost } from "@/hooks/useAgentCosts";
import { cn } from "@/lib/utils";

/** Cost beside an agent's name in the Agents list and graph; nothing while unknown. */
export function AgentCostTag({
  cost,
  className,
}: {
  cost: AgentCost | undefined;
  className?: string;
}) {
  const label = agentCostLabel(cost);
  if (label === null) return null;
  return (
    <span
      data-testid="subagent-cost"
      className={cn("shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground", className)}
    >
      {label}
    </span>
  );
}
