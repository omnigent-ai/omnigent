// The line a stacked PR's merge area shows about landing it, with the one action it calls
// for. It only advises: nothing here holds a merge back.

import { useId, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { usePaletteCommands } from "@/store/paletteCommands";
import {
  type StackLanding,
  type StackLandingAction,
  stackLandingPaletteCommands,
} from "./stackLandingCommands";

const NO_COMMANDS: never[] = [];

/** ⌘K's landing commands while the PR's page is the active one. */
export function StackLandingPaletteCommands({
  landing,
  active,
}: {
  landing: StackLanding;
  active: boolean;
}): null {
  const commands = useMemo(
    () => (active ? stackLandingPaletteCommands(landing) : NO_COMMANDS),
    [landing, active],
  );
  const instance = useId();
  usePaletteCommands(`pull-request-stack-landing:${instance}`, "Pull request", commands);
  return null;
}

/** The action a notice calls for, in the merge area. */
function noticeAction(landing: StackLanding): StackLandingAction | null {
  switch (landing.notice?.kind) {
    case "merge-parent-first":
      return landing.land;
    case "sync":
      return landing.sync;
    case "stale":
      return landing.reload;
    case "failed":
      return landing.retry;
    default:
      return null;
  }
}

export function PullRequestStackLandingNotice({ landing }: { landing: StackLanding }) {
  const { notice, message } = landing;
  if (!notice || !message) return null;
  const action = noticeAction(landing);
  return (
    <div
      role="note"
      data-testid="pull-request-stack-landing"
      className="flex flex-wrap items-center gap-2 text-ui text-muted-foreground"
    >
      <span className="min-w-0 flex-1">{message}</span>
      {action && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-6 shrink-0"
          title={action.description}
          onClick={action.run}
        >
          {action.buttonLabel}
        </Button>
      )}
    </div>
  );
}
