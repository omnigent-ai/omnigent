// WIP(session_overview): Codex and SDK sub-agents carry their own priced usage;
// Claude sub-agents have no cost here yet.

import { useAgentCosts } from "@/hooks/useAgentCosts";
import { useChildSessions } from "@/hooks/useChildSessions";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { useChatStore } from "@/store/chatStore";
import { childPrimaryLabel } from "./subagentLabel";

/** The bound session's direct sub-agents, one line each: name and cost. */
export function SessionOverviewSubagents() {
  const sessionId = useChatStore((s) => s.conversationId);
  const { children, error } = useChildSessions(sessionId);
  const costs = useAgentCosts(sessionId);
  // A failed list is not "no sub-agents", so it keeps the section.
  if (children.length === 0 && error === null) return null;
  return (
    <section aria-label="Sub-agents" className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium text-muted-foreground">Sub-agents</h3>
      {children.length === 0 ? (
        <p className="text-muted-foreground">Unavailable</p>
      ) : (
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto">
          {children.map((child) => {
            const cost = costs.data?.get(child.id);
            return (
              <li
                key={child.id}
                className="flex items-baseline justify-between gap-3"
                data-testid={`session-overview-subagent-${child.id}`}
              >
                <span className="min-w-0 truncate text-muted-foreground">
                  {childPrimaryLabel(child)}
                </span>
                {/* An unknown cost, including after a failed read, shows nothing. */}
                <span className="shrink-0 font-mono tabular-nums">
                  {typeof cost === "number" && formatSessionCostUsd(cost)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
