// What a Close or Reopen came to, with its Try Again, Check Again, Open on GitHub and Dismiss,
// however it was started: the PR page shows it once, under its header, on every tab.

import { ExternalLinkIcon, RefreshCwIcon, RotateCcwIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  stateChainMessage,
  type StateChainNotice,
  type StateNoticeAction,
  stateNoticeActionLabel,
  stateNoticeActions,
} from "./pullRequestStateChain";

/** The icon a notice action's ⌘K row shows. */
export function stateNoticeActionIcon(action: StateNoticeAction) {
  if (action === "open") return ExternalLinkIcon;
  if (action === "check") return RefreshCwIcon;
  if (action === "dismiss") return XIcon;
  return RotateCcwIcon;
}

export function StateChainNoticeView({
  notice,
  prUrl,
  running,
  checking,
  checkFailed,
  onAction,
  className,
}: {
  notice: StateChainNotice;
  prUrl: string;
  /** A change for this PR is running, so Try Again waits. */
  running: boolean;
  checking: boolean;
  checkFailed: boolean;
  /** Every action but Open on GitHub, which is a link. */
  onAction: (action: Exclude<StateNoticeAction, "open">) => void;
  className?: string;
}) {
  const refused = notice.outcome.kind === "refused";
  return (
    <div
      role={refused ? "alert" : "status"}
      className={cn("flex flex-wrap items-center gap-x-2 gap-y-1 text-ui", className)}
    >
      <p className={refused ? "min-w-0 text-destructive" : "min-w-0 text-muted-foreground"}>
        {stateChainMessage(notice)}
        {notice.outcome.kind === "unknown" && checkFailed && (
          <> Couldn’t read the pull request just now. Check again, or open it on GitHub.</>
        )}
      </p>
      {stateNoticeActions(notice).map((action) =>
        action === "open" ? (
          <Button key="open" asChild variant="link" size="sm" className="h-auto p-0">
            <a href={prUrl} target="_blank" rel="noreferrer">
              {stateNoticeActionLabel(notice, "open", false)}
            </a>
          </Button>
        ) : (
          <Button
            key={action}
            type="button"
            variant="link"
            size="sm"
            className={action === "dismiss" ? "h-auto p-0 text-muted-foreground" : "h-auto p-0"}
            disabled={(action === "retry" && running) || (action === "check" && checking)}
            onClick={() => onAction(action)}
          >
            {action === "check" && checking
              ? "Checking…"
              : stateNoticeActionLabel(notice, action, false)}
          </Button>
        ),
      )}
    </div>
  );
}
