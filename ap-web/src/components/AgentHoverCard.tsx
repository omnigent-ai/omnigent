import * as React from "react";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

/**
 * The Cursor-style flyout body: bold name + description paragraph.
 *
 * Shared by both presentation surfaces (the hover card on agent cards
 * and the tooltip on dropdown rows) so the two render identically.
 *
 * @param agent - The catalog entry whose name/description to render.
 * @returns The flyout's inner markup.
 */
function AgentFlyoutBody({ agent }: { agent: AvailableAgent }) {
  // text-sm matches the agent-name font size in the picker rows
  // (DropdownMenuItem is text-sm), like Cursor's flyout.
  return (
    <div className="text-sm">
      <p className="font-semibold leading-snug">{agent.display_name}</p>
      <p className="mt-1 text-xs leading-snug text-muted-foreground">
        {agent.description}
      </p>
    </div>
  );
}

/**
 * Cursor-style hover flyout for one catalog agent, for use OUTSIDE a
 * dropdown menu (e.g. the agent cards in AddAgentDialog).
 *
 * Wraps an arbitrary trigger so hovering it opens a flyout to the
 * right with the agent's ``display_name`` (bold) and ``description``.
 * The trigger is passed through ``asChild`` so the caller keeps full
 * control of the rendered element. When the agent has no description
 * there is nothing to show, so the trigger is returned bare.
 *
 * NOTE: do NOT use this to wrap a ``DropdownMenuItem`` — a HoverCard
 * wrapping a menu item swallows the ref that ``DropdownMenuContent``
 * hands its children for roving focus, and the flyout never opens.
 * For dropdown rows use {@link AgentRowTooltip} instead.
 *
 * @param agent - The catalog entry whose name/description the flyout
 *   shows.
 * @param children - The trigger element to wrap; rendered via
 *   ``asChild`` so its own props/handlers are preserved.
 * @returns The trigger wrapped in a hover flyout, or the bare trigger
 *   when the agent has no description.
 */
export function AgentHoverCard({
  agent,
  children,
}: {
  agent: AvailableAgent;
  children: React.ReactNode;
}) {
  if (!agent.description) return <>{children}</>;

  return (
    // openDelay matches the screenshot's feel — a brief pause before the
    // card appears so quick scans down the list don't flash flyouts.
    <HoverCard openDelay={150} closeDelay={0}>
      <HoverCardTrigger asChild>{children}</HoverCardTrigger>
      {/* side="right" + align="start" places the card to the right of the
          row with its top edge aligned, like Cursor's model picker. */}
      <HoverCardContent
        side="right"
        align="start"
        sideOffset={8}
        className="w-72"
        data-testid={`agent-hover-card-${agent.id}`}
      >
        <AgentFlyoutBody agent={agent} />
      </HoverCardContent>
    </HoverCard>
  );
}

/**
 * Cursor-style flyout for an agent row INSIDE a dropdown menu.
 *
 * Unlike {@link AgentHoverCard}, the trigger here wraps the row's
 * *inner content* (not the ``DropdownMenuItem`` itself), so the menu
 * item stays a direct child of ``DropdownMenuContent`` and keeps its
 * roving focus. A Tooltip (not a HoverCard) is used because tooltips
 * open reliably while a dropdown menu is open; ``side="right"`` opens
 * the flyout beside the row like the screenshot.
 *
 * Falls back to rendering ``children`` bare when the agent has no
 * description.
 *
 * @param agent - The catalog entry whose name/description the flyout
 *   shows.
 * @param children - The row's inner content, rendered as the tooltip
 *   trigger via ``asChild``.
 * @returns The content wrapped in a side tooltip, or bare when there
 *   is no description.
 */
export function AgentRowTooltip({
  agent,
  children,
}: {
  agent: AvailableAgent;
  children: React.ReactNode;
}) {
  if (!agent.description) return <>{children}</>;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent
        side="right"
        align="start"
        // Gap between the open dropdown and the flyout — Cursor leaves a
        // small space here, which reads cleaner than a flush edge.
        sideOffset={16}
        className="w-72 max-w-72 flex-col items-start whitespace-normal text-left"
        data-testid={`agent-hover-card-${agent.id}`}
      >
        <AgentFlyoutBody agent={agent} />
      </TooltipContent>
    </Tooltip>
  );
}
