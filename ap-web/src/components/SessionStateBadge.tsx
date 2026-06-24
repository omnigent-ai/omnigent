// Sidebar status indicator. Approval surfaces as a "Needs response" tag so
// it reads at a glance; running/unseen stay as compact dots. Verbose copy
// (incl. the approval count) lives in the tooltip.

import { useTranslation } from "react-i18next";
import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionState } from "@/hooks/useSessionState";
import { cn } from "@/lib/utils";

export interface SessionStateBadgeProps {
  state: SessionState;
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => JSX.Element;
}

type TFn = ReturnType<typeof useTranslation<"common">>["t"];

function describe(state: SessionState, t: TFn): Visual {
  switch (state.kind) {
    case "awaiting": {
      const tooltip = t("approvalPromptsWaiting", { count: state.count });
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        render: () => (
          <Badge className="border-transparent bg-warning/15 text-warning">
            {t("needsResponse")}
          </Badge>
        ),
      };
    }
    case "running":
      return {
        kind: state.kind,
        ariaLabel: t("sessionRunning"),
        tooltip: t("sessionRunning"),
        render: () => <RunningDot />,
      };
    case "unseen":
      // Solid (non-pulsing) brand-pink dot — distinguished from the running
      // indicator, which is the same pink but pulsing.
      return {
        kind: state.kind,
        ariaLabel: t("newMessages"),
        tooltip: t("newMessages"),
        render: () => <Dot tone="bg-brand-accent" />,
      };
  }
}

function Dot({ tone }: { tone: string }) {
  return <span aria-hidden className={cn("size-2 shrink-0 rounded-full", tone)} />;
}

export function SessionStateBadge({ state }: SessionStateBadgeProps) {
  const { t } = useTranslation("common");
  const visual = describe(state, t);
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
