// The new Commits page's view model: the history header's count and limit note, search over
// a commit's subject, author and SHA, and the long relative times its cards read.

import {
  commitAuthor,
  type PullRequestCommit,
  type PullRequestCommits,
} from "@/lib/pullRequestCommits";

/** The most commits the desktop shell reads for a pull request, as GitHub lists them. */
export const COMMIT_HISTORY_LIMIT = 250;

export interface CommitHistoryHeading {
  /** "12 commits", or "250 commits shown" when the list stops short of the PR's history. */
  title: string;
  /** Why the list stops short, when it does. */
  note?: string;
}

/**
 * The header for a list. Only a read that says it stopped short gets a note: the limit when it
 * reached it, otherwise how many it shows of how many. An older app's list says nothing.
 */
export function commitHistoryHeading(list: PullRequestCommits): CommitHistoryHeading {
  const count = list.commits.length;
  const noun = count === 1 ? "commit" : "commits";
  if (list.complete === false && count >= COMMIT_HISTORY_LIMIT) {
    return {
      title: `${count} ${noun} shown`,
      note: `History is limited to ${COMMIT_HISTORY_LIMIT} commits`,
    };
  }
  if (list.complete === false && list.total_count > count) {
    return {
      title: `${count} ${noun} shown`,
      note: `Showing ${count} of ${list.total_count} commits`,
    };
  }
  return { title: `${count} ${noun}` };
}

/** When the commit was committed, which its card names; the authored time when that's all. */
export function committedTime(commit: PullRequestCommit): number | null {
  for (const value of [commit.committed_at, commit.authored_at]) {
    const parsed = value ? Date.parse(value) : NaN;
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** Every word of the query appears in the commit's subject, author or SHA. */
export function matchesCommitSearch(commit: PullRequestCommit, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const text = [commit.subject, commitAuthor(commit) ?? "", commit.author_name ?? "", commit.sha]
    .join("\n")
    .toLowerCase();
  return words.every((word) => text.includes(word));
}
