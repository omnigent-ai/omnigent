// A commit's description under its title: wrapped as written, and folded to a few lines
// behind Show more when it's long, so the commit's changes stay in view.

import { useId, useState } from "react";

import { cn } from "@/lib/utils";
import { isLongCommitBody } from "@/lib/pullRequestCommits";

export function CommitMessageBody({ body, className }: { body: string; className?: string }) {
  const long = isLongCommitBody(body);
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const folded = long && !expanded;
  return (
    <div className={cn("commit-message-body grid min-w-0 justify-items-start gap-0.5", className)}>
      <p
        id={id}
        data-testid="commit-message-body"
        className={cn(
          "m-0 w-full break-words whitespace-pre-wrap text-xs text-muted-foreground",
          // Folded to its first lines; unfolded, a very long one scrolls in place.
          folded ? "line-clamp-6" : "max-h-[40vh] overflow-y-auto",
        )}
      >
        {body}
      </p>
      {long && (
        <button
          type="button"
          className="rounded-sm text-xs font-medium text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}
