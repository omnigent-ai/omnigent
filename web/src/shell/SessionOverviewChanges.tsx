// WIP(session_overview): workspace change rows. Lines include untracked files
// when the runner counts them, and tracked files only otherwise.

import { useSession } from "@/hooks/useSession";
import { useSessionWorktrees } from "@/hooks/useSessionWorktrees";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums">{children}</span>
    </div>
  );
}

/** "Files changed" and "Lines" for a git workspace with uncommitted changes. */
export function SessionOverviewChanges({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  // Shares the composer's worktree read; only a listed worktree proves git.
  const worktrees = useSessionWorktrees(sessionId, session?.hostId, session?.workspace);
  const files = useWorkspaceChangedFiles(sessionId).data?.data ?? [];
  const isGit = worktrees.data?.status === "ok" && worktrees.data.worktrees.length > 0;
  if (!isGit || files.length === 0) return null;
  const counted = files.filter(
    (file) =>
      file.lines_added !== null || file.lines_removed !== null || file.untracked_lines != null,
  );
  const added = counted.reduce(
    (sum, file) => sum + (file.lines_added ?? file.untracked_lines ?? 0),
    0,
  );
  const removed = counted.reduce((sum, file) => sum + (file.lines_removed ?? 0), 0);
  return (
    <>
      <Row label="Files changed">{files.length}</Row>
      {counted.length > 0 && (
        <Row label="Lines">
          <span className="text-green-600 dark:text-green-400">+{added}</span>{" "}
          <span className="text-destructive">−{removed}</span>
        </Row>
      )}
    </>
  );
}
