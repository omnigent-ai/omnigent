// Sidebar status indicator. Approval surfaces as a "Needs response" tag so
// it reads at a glance; other states stay compact. Verbose copy
// (incl. the approval count) lives in the tooltip.

import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionState } from "@/hooks/useSessionState";
import { TERMINAL_PROMPT_BLOCKED_ON } from "@/lib/events";
import { cn } from "@/lib/utils";
import type { ReactElement } from "react";
import { CircleAlertIcon } from "lucide-react";

export interface SessionStateBadgeProps {
  state: SessionState;
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => ReactElement;
}

function awaitingTooltip(count: number, terminalReason: string | undefined): string {
  // The runner infers this reason from the terminal's screen, so stay tentative.
  if (terminalReason === TERMINAL_PROMPT_BLOCKED_ON) {
    return "May need your input in the agent's terminal";
  }
  if (terminalReason) return `Waiting for you in the agent's terminal (${terminalReason})`;
  return count === 1 ? "1 approval prompt waiting" : `${count} approval prompts waiting`;
}

function describe(state: SessionState): Visual {
  switch (state.kind) {
    case "awaiting": {
      const tooltip = awaitingTooltip(state.count, state.terminalReason);
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        render: () => (
          <Badge className="border-transparent bg-brand-accent/15 text-brand-accent">
            Needs response
          </Badge>
        ),
      };
    }
    case "running": {
      const tooltip = state.background ? "Working in the background" : "Session running";
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        render: () => <RunningDot className="size-3" />,
      };
    }
    case "starting":
      // Same spinner as running — the session is coming up, not yet working.
      return {
        kind: state.kind,
        ariaLabel: "Session starting up",
        tooltip: "Session starting up",
        render: () => <RunningDot className="size-3" />,
      };
    case "error":
      return {
        kind: state.kind,
        ariaLabel: "Latest message is an error",
        tooltip: "Latest message is an error",
        render: () => (
          <CircleAlertIcon aria-hidden className="size-3.5 shrink-0 text-destructive" />
        ),
      };
    case "disconnected":
      return {
        kind: state.kind,
        ariaLabel: "Host disconnected",
        tooltip: "Host disconnected",
        render: () => (
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full border border-muted-foreground"
          />
        ),
      };
    case "unseen":
      // Solid brand-pink dot — distinguished from the running indicator,
      // which is a grey spinner.
      return {
        kind: state.kind,
        ariaLabel: "New messages",
        tooltip: "New messages",
        render: () => <Dot tone="bg-brand-accent" />,
      };
  }
}

function Dot({ tone }: { tone: string }) {
  return <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", tone)} />;
}

export function SessionStateBadge({ state }: SessionStateBadgeProps) {
  const visual = describe(state);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="session-state-badge"
          data-state={visual.kind}
          role="img"
          aria-label={visual.ariaLabel}
          className="inline-flex h-5 shrink-0 items-center justify-center"
        >
          {visual.render()}
        </span>
      </TooltipTrigger>
      {/* Opens left: the badge sits at the right edge of the narrow
          sidebar, so a right-opening tooltip would overflow the panel. */}
      <TooltipContent side="left">{visual.tooltip}</TooltipContent>
    </Tooltip>
  );
}
