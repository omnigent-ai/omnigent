// The Changes toolbar's start point: the PR base, or one of the PR's own commits, so the tab
// shows only the changes after it. A commit that heads another PR (a stacked PR's parent) says so.

import { Loader2Icon } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PULL_REQUEST_COMMITS_LIMIT, type PullRequestCommit } from "@/hooks/usePullRequests";

const PR_BASE = "base";

/** "Head of #41" for a commit that is another PR's head, else null. */
export function headOfLabel(commit: PullRequestCommit): string | null {
  const numbers = (commit.head_of ?? []).map((pr) => `#${pr.number}`);
  return numbers.length > 0 ? `Head of ${numbers.join(", ")}` : null;
}

export function PullRequestChangesSince({
  commits,
  headSha,
  hasMore,
  loading,
  errorMessage,
  sinceSha,
  onChange,
  onOpen,
}: {
  /** The PR's commits, oldest first. */
  commits: readonly PullRequestCommit[];
  /** The head the tab shows; nothing comes after it, so it is not a start point. */
  headSha: string | null;
  /** GitHub listed only the first commits, so the newest may be missing. */
  hasMore: boolean;
  loading: boolean;
  /** Why the commit list failed to load; opening the picker again retries. */
  errorMessage: string | null;
  /** The picked commit, or null for the PR base. */
  sinceSha: string | null;
  onChange: (sinceSha: string | null) => void;
  /** The commit list loads once the picker is first opened, and reloads after a failure. */
  onOpen: () => void;
}) {
  // Newest first. The head is matched by sha: a capped list may end before it.
  const options = commits.filter((commit) => commit.sha !== headSha).reverse();
  const picked = commits.find((commit) => commit.sha === sinceSha);
  return (
    <Select
      value={sinceSha ?? PR_BASE}
      onValueChange={(value) => onChange(value === PR_BASE ? null : value)}
      onOpenChange={(open) => open && onOpen()}
    >
      <SelectTrigger size="sm" aria-label="Changes since" className="h-6 max-w-56 text-xs">
        <SelectValue>
          {picked
            ? `Since ${picked.sha.slice(0, 7)}`
            : sinceSha
              ? "Since a commit"
              : "Since PR base"}
        </SelectValue>
      </SelectTrigger>
      <SelectContent
        position="popper"
        align="start"
        // Fits a narrow rail; each row truncates its title instead of widening the list.
        collisionPadding={8}
        className="max-h-80 w-md max-w-(--radix-select-content-available-width)"
      >
        <SelectItem value={PR_BASE}>PR base (all changes)</SelectItem>
        {hasMore && (
          <div className="px-2 py-1 text-xs text-muted-foreground">
            {`Only the first ${PULL_REQUEST_COMMITS_LIMIT} commits are listed`}
          </div>
        )}
        {errorMessage && (
          <div role="alert" className="px-2 py-1.5 text-xs text-destructive">
            {`${errorMessage} Open this list again to retry.`}
          </div>
        )}
        {loading && commits.length === 0 && (
          <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
            <Loader2Icon className="size-3.5 animate-spin" aria-hidden="true" />
            Loading commits…
          </div>
        )}
        {options.map((commit) => {
          const headOf = headOfLabel(commit);
          return (
            <SelectItem
              key={commit.sha}
              value={commit.sha}
              // The row's text box must shrink, or long titles push past the checkmark.
              className="*:[span]:last:min-w-0 *:[span]:last:flex-1"
            >
              {headOf && (
                <span className="shrink-0 rounded bg-primary px-1.5 text-xs font-medium text-primary-foreground">
                  {headOf}
                </span>
              )}
              <span className="shrink-0 font-mono text-xs text-muted-foreground">
                {commit.sha.slice(0, 7)}
              </span>
              <span className="min-w-0 flex-1 truncate">{commit.title || commit.sha}</span>
            </SelectItem>
          );
        })}
      </SelectContent>
    </Select>
  );
}
