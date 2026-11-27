// The PR header's Link button, beside its actions menu, for a PR the session doesn't link
// yet. One click links it with no picker. While the host confirms, the PR stays usable and
// the button says Linking…; it then settles on Linked in the same space, so nothing moves.

import { AlertCircleIcon, CheckIcon, LinkIcon, Loader2Icon, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

import type { LinkToSession } from "./pullRequestLinks/useLinkToSession";

export const LINK_BUTTON_TOOLTIP = "Link to this session";

type Phase = "idle" | "linking" | "linked" | "failed";

const PHASES: Record<Phase, { label: string; icon: LucideIcon }> = {
  idle: { label: "Link", icon: LinkIcon },
  linking: { label: "Linking…", icon: Loader2Icon },
  linked: { label: "Linked", icon: CheckIcon },
  failed: { label: "Retry", icon: AlertCircleIcon },
};

const NOTICE_PHASES: Record<Phase, { label: string; icon: LucideIcon }> = {
  ...PHASES,
  idle: { label: "Link it", icon: LinkIcon },
};

/** Where the button stands: a link this view saw confirmed reads Linked. */
export function linkButtonPhase(link: Pick<LinkToSession, "linked" | "state">): Phase {
  if (link.linked) return "linked";
  if (link.state.status === "pending") return "linking";
  if (link.state.status === "failed") return "failed";
  return "idle";
}

export function PullRequestLinkButton({
  link,
  appearance = "header",
}: {
  link: LinkToSession;
  /** The header's quiet button, or the not-linked notice's own call to action. */
  appearance?: "header" | "notice";
}) {
  const phase = linkButtonPhase(link);
  const phases = appearance === "notice" ? NOTICE_PHASES : PHASES;
  const { label, icon: Icon } = phases[phase];
  const runnable = (phase === "idle" || phase === "failed") && link.availability.available;
  const reason = link.availability.available ? undefined : link.availability.reason;
  const tooltip =
    phase === "failed" && link.state.status === "failed"
      ? `Couldn’t link: ${link.state.message}`
      : phase === "linked"
        ? "Linked to this session"
        : phase === "linking"
          ? "Linking to this session…"
          : (reason ?? LINK_BUTTON_TOOLTIP);
  return (
    <>
      {/* Announced as it changes; the label itself is drawn in a fixed-width cell below. */}
      <span role="status" className="sr-only">
        {phase === "linking" ? "Linking…" : phase === "linked" ? "Linked to this session" : ""}
      </span>
      {phase === "failed" && link.state.status === "failed" && (
        <span role="alert" className="sr-only">
          {`Couldn’t link: ${link.state.message}`}
        </span>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant={appearance === "notice" ? "outline" : "ghost"}
            size={appearance === "notice" ? "sm" : "xs"}
            aria-disabled={!runnable || undefined}
            aria-busy={phase === "linking" || undefined}
            aria-label={phase === "idle" && appearance === "header" ? LINK_BUTTON_TOOLTIP : label}
            data-link-phase={phase}
            onClick={runnable ? link.link : undefined}
            className={cn(
              "shrink-0",
              appearance === "header" && "text-muted-foreground",
              !runnable && "cursor-default hover:bg-transparent",
              phase === "failed" && "text-destructive",
            )}
          >
            <Icon aria-hidden className={cn("size-3.5", phase === "linking" && "animate-spin")} />
            {/* Every label in one cell, only the current one visible, so the width never changes. */}
            <span className="grid">
              {Object.entries(phases).map(([key, value]) => (
                <span
                  key={key}
                  aria-hidden
                  className={cn("col-start-1 row-start-1", key !== phase && "invisible")}
                >
                  {value.label}
                </span>
              ))}
            </span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>{tooltip}</TooltipContent>
      </Tooltip>
    </>
  );
}
