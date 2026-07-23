// Sidebar status indicator. Approval surfaces as a "Needs response" tag so
// it reads at a glance; running/unseen stay as compact dots. Verbose copy
// (incl. the approval count) lives in the tooltip.

import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionState } from "@/hooks/useSessionState";
import { cn } from "@/lib/utils";
import type { ReactElement } from "react";

export interface SessionStateBadgeProps {
  state: SessionState;
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => ReactElement;
}

function describe(state: SessionState): Visual {
  switch (state.kind) {
    case "awaiting": {
      const tooltip =
        state.count === 1 ? "1 approval prompt waiting" : `${state.count} approval prompts waiting`;
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        render: () => (
          <Badge className="border-transparent bg-warning/25 text-warning">Needs response</Badge>
        ),
      };
    }
    case "running":
      return {
        kind: state.kind,
        ariaLabel: "Session running",
        tooltip: "Session running",
        render: () => <RunningDot className="size-2.5" />,
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
