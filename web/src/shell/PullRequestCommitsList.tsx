// The Commits view's list: the PR's commits, oldest first, with a substring search.

import { Loader2Icon } from "lucide-react";
import { useMemo } from "react";
import { Input } from "@/components/ui/input";
import { PULL_REQUEST_COMMITS_LIMIT, type PullRequestCommit } from "@/hooks/usePullRequests";
import { relativeTime } from "@/lib/relativeTime";

/** Commits whose sha, title or author contains `query`, ignoring case. */
export function filterCommits(
  commits: readonly PullRequestCommit[],
  query: string,
): PullRequestCommit[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [...commits];
  return commits.filter((commit) =>
    [commit.sha, commit.title, commit.author ?? ""].some((field) =>
      field.toLocaleLowerCase().includes(needle),
    ),
  );
}

export function PullRequestCommitsList({
  commits,
  hasMore,
  loading,
  unavailable,
  errorMessage,
  query,
  onQueryChange,
  onSelect,
}: {
  commits: readonly PullRequestCommit[];
  /** True when GitHub has more commits than the list shows. */
  hasMore: boolean;
  loading: boolean;
  /** True when the history cannot be read here, so it is not an empty history. */
  unavailable: boolean;
  errorMessage: string | null;
  query: string;
  onQueryChange: (query: string) => void;
  onSelect: (sha: string) => void;
}) {
  const visible = useMemo(() => filterCommits(commits, query), [commits, query]);

  const renderBody = () => {
    if (unavailable) {
      return <p className="p-3 text-ui text-muted-foreground">Commits are unavailable.</p>;
    }
    if (commits.length === 0) {
      return <p className="p-3 text-ui text-muted-foreground">No commits on this pull request.</p>;
    }
    if (visible.length === 0) {
      return (
        <p className="p-3 text-ui text-muted-foreground">{`No commits match "${query.trim()}"`}</p>
      );
    }
    return (
      <ul className="min-h-0 flex-1 overflow-y-auto">
        {visible.map((commit) => {
          const authoredAt = commit.date ? Date.parse(commit.date) : Number.NaN;
          return (
            <li key={commit.sha}>
              <button
                type="button"
                onClick={() => onSelect(commit.sha)}
                className="flex w-full flex-col gap-0.5 border-b border-border px-3 py-2 text-left text-ui hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none"
              >
                <span className="truncate">{commit.title || commit.sha}</span>
                <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                  <span className="shrink-0 font-mono">{commit.sha.slice(0, 7)}</span>
                  {commit.author && <span className="truncate">{commit.author}</span>}
                  {Number.isFinite(authoredAt) && (
                    <time dateTime={commit.date ?? undefined} className="shrink-0">
                      {relativeTime(authoredAt)}
                    </time>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    );
  };

  // A failed refresh keeps the history already on screen; only an empty pane shows the error alone.
  if (errorMessage && commits.length === 0) {
    return (
      <p role="alert" className="p-3 text-ui text-destructive">
        {errorMessage}
      </p>
    );
  }
  if (loading && commits.length === 0) {
    return (
      <div className="flex items-center justify-center gap-2 p-4 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" aria-hidden="true" />
        Loading commits…
      </div>
    );
  }
  // The root fills the diff pane, so the header and search stay put while the list scrolls.
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-1.5 text-ui">
        <span className="font-medium">
          {commits.length} {commits.length === 1 ? "commit" : "commits"}
        </span>
        {hasMore && (
          <span className="text-xs text-muted-foreground">
            {`History limited to ${PULL_REQUEST_COMMITS_LIMIT} commits`}
          </span>
        )}
      </div>
      {errorMessage && (
        <p
          role="alert"
          className="shrink-0 border-b border-border px-3 py-1 text-xs text-destructive"
        >
          {errorMessage}
        </p>
      )}
      <div className="shrink-0 border-b border-border p-2">
        <Input
          type="search"
          aria-label="Search commits"
          placeholder="Search commits…"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={(event) => {
            // Escape clears the search without closing an enclosing drawer.
            if (event.key !== "Escape" || !query) return;
            event.stopPropagation();
            onQueryChange("");
          }}
        />
      </div>
      {renderBody()}
    </div>
  );
}
