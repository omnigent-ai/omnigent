import { InfoIcon } from "lucide-react";
import type { WorkspaceChangesTrackingReason } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Shown atop the Changed-files panel when the runner reports incomplete
 * tracking (`tracking.complete === false`), so a non-git workspace's partial
 * list is not read as "no changes". Mirrors RunnerAsleepHint's layout.
 */
const MESSAGES: Record<WorkspaceChangesTrackingReason, string> = {
  non_git_workspace:
    "This workspace isn't a Git repository, so this list only shows edits the agent made " +
    "through its file tools while its runner is online. Changes made by shell commands or " +
    "other programs won't appear here.",
  no_workspace: "This session has no tracked workspace, so file changes can't be listed here.",
};

const FALLBACK_MESSAGE =
  "Change tracking is limited for this workspace, so some edits may not be listed here.";

export function LimitedTrackingNotice({
  reason,
}: {
  reason: WorkspaceChangesTrackingReason | null;
}) {
  // An unknown or absent reason still means tracking is incomplete; say so
  // neutrally rather than guess at a cause the UI doesn't recognize.
  const message = (reason && MESSAGES[reason]) || FALLBACK_MESSAGE;
  return (
    <div className="flex flex-col items-start gap-1 px-2 py-1.5 text-muted-foreground text-xs">
      <span className="flex items-center gap-1.5 font-medium text-foreground">
        <InfoIcon className="size-3.5 shrink-0" />
        Limited change tracking
      </span>
      <span>{message}</span>
    </div>
  );
}
