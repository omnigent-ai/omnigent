// The header of one commit's patch: back to the commit list, and step to the older or newer commit.

import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { PullRequestCommit } from "@/hooks/usePullRequests";
import { IconButton } from "./PullRequestFileSection";

export function PullRequestCommitBar({
  sha,
  title,
  previous,
  next,
  onBack,
  onOpen,
}: {
  sha: string;
  /** The commit's title when the loaded list has it. */
  title: string | null;
  /** The commit before this one in the list (older), or null at the start. */
  previous: PullRequestCommit | null;
  /** The commit after this one in the list (newer), or null at the end. */
  next: PullRequestCommit | null;
  onBack: () => void;
  onOpen: (sha: string) => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-border px-2 py-1">
      <Button variant="ghost" size="sm" aria-label="Back to commits" onClick={onBack}>
        Back
      </Button>
      <span className="min-w-0 flex-1 truncate text-ui" title={title ?? undefined}>
        {title ?? "Commit"}
      </span>
      <span className="shrink-0 font-mono text-xs text-muted-foreground">{sha.slice(0, 7)}</span>
      <IconButton
        label="Previous commit"
        disabled={!previous}
        onClick={() => previous && onOpen(previous.sha)}
        className="size-5 text-muted-foreground"
      >
        <ChevronUpIcon className="size-3.5" />
      </IconButton>
      <IconButton
        label="Next commit"
        disabled={!next}
        onClick={() => next && onOpen(next.sha)}
        className="size-5 text-muted-foreground"
      >
        <ChevronDownIcon className="size-3.5" />
      </IconButton>
    </div>
  );
}
