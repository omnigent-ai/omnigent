import { InfoIcon } from "lucide-react";
import type { WorkspaceChangesTrackingReason } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Shown atop the Changed-files panel when the runner reports that change
 * tracking can't observe every edit (`tracking.complete === false`). Without
 * it, a non-git workspace — where only the agent's own file-tool edits are
 * recorded — shows a bare empty list that reads as a definitive "no changes",
 * even though native-CLI, shell, or external writes did change files on disk.
 *
 * Mirrors {@link RunnerAsleepHint}'s compact icon + title + description layout
 * so the panel's degraded states read consistently.
 */
const MESSAGES: Record<WorkspaceChangesTrackingReason, string> = {
  non_git_workspace:
    "This workspace isn't a Git repository, so only files edited through the agent's " +
    "built-in file tools are listed. Changes made by the agent's own CLI, shell " +
    "commands, or other processes won't appear here.",
  no_workspace: "This session has no tracked workspace, so file changes can't be listed here.",
};

export function LimitedTrackingNotice({
  reason,
}: {
  reason: WorkspaceChangesTrackingReason | null;
}) {
  // Fall back to the non-git copy for an unknown/absent reason: that's the
  // only way the runner flags incompleteness today, and it's the most useful
  // explanation if a future reason arrives before the UI knows about it.
  const message = (reason && MESSAGES[reason]) || MESSAGES.non_git_workspace;
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
