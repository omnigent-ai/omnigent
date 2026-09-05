import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentHoverCard } from "@/components/AgentHoverCard";
import { resolveAgentIcon } from "@/shell/subagentIcons";

/**
 * Selectable card for one available agent.
 *
 * Shared by the new-session picker (NewChatDialog) and the "Add agent"
 * picker (AddAgentDialog) so both render the agent catalog identically.
 * Claude, Codex, and pi agents reuse their own glyphs; qwen falls back
 * to a generic bot icon for now. Nessie matches by name. Everything else
 * falls back to a generic bot icon.
 *
 * @param agent - The catalog entry to render.
 * @param selected - Whether this card is the current selection.
 * @param onSelect - Invoked when the card is clicked.
 * @param compact - When true, render icon + name only (no inline
 *   description) so cards stay even in a horizontal row; the
 *   description is surfaced as a hover tooltip instead.
 * @param hover - When true, wrap the card in a Cursor-style hover
 *   flyout (``AgentHoverCard``) that opens to the right with the
 *   agent's name + description. Additive to the inline description.
 *   Ignored in compact mode, which already surfaces the description
 *   via its own tooltip.
 */
export function AgentCard({
  agent,
  selected,
  onSelect,
  compact = false,
  hover = false,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
  compact?: boolean;
  hover?: boolean;
}) {
  const Icon = resolveAgentIcon({ kind: "catalog", name: agent.name, harness: agent.harness });
  const card = (
    <button
      type="button"
      data-testid={`agent-card-${agent.id}`}
      onClick={onSelect}
      className={`flex w-full items-center gap-3 rounded-lg border p-3 text-left transition ${
        selected ? "border-primary bg-primary/5" : "border-border hover:border-muted-foreground/30"
      } cursor-pointer`}
    >
      <Icon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <span className="text-sm font-semibold">{agent.display_name}</span>
        {!compact && agent.description && (
          <p className="mt-0.5 text-sm text-muted-foreground">{agent.description}</p>
        )}
      </div>
    </button>
  );

  // Compact cards drop the inline description to keep heights even in a
  // row; surface it via a tooltip. Use the component tooltip (≈500ms
  // open) instead of the native ``title`` attribute, whose multi-second
  // browser-imposed delay feels broken.
  if (compact && agent.description) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{card}</TooltipTrigger>
        <TooltipContent>{agent.description}</TooltipContent>
      </Tooltip>
    );
  }
  // Non-compact opt-in: surface the richer Cursor-style flyout to the
  // right on hover. AgentHoverCard no-ops when there's no description.
  if (hover) {
    return <AgentHoverCard agent={agent}>{card}</AgentHoverCard>;
  }
  return card;
}
