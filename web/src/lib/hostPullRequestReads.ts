// Reads the new PR page makes on this Mac in main, made instead through the session's host
// with WorkspaceService's methods. Each answers in the shape the page's Mac read parses.

import type { CommitDiff, PullRequestCommit, PullRequestCommits } from "@/lib/pullRequestCommits";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";

/** One PR's lookup answer by its URL, through the session's host, unparsed. */
export function readPullRequestLookupOnHost(
  conversationId: string,
  prUrl: string,
): Promise<unknown> {
  return readPullRequestRpc<unknown>(conversationId, "lookupPullRequest", (client) =>
    client.lookupPullRequest({ prUrl }),
  );
}

/** The host's listed commit, as `ListPullRequestCommits` answers it. */
interface HostCommit {
  sha?: unknown;
  title?: unknown;
  body?: unknown;
  author?: unknown;
  author_login?: unknown;
  date?: unknown;
  committed_at?: unknown;
  url?: unknown;
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function commitFromHost(commit: HostCommit): PullRequestCommit | null {
  const sha = text(commit.sha);
  if (!sha) return null;
  return {
    sha,
    subject: text(commit.title) ?? "",
    body: text(commit.body),
    author_login: text(commit.author_login),
    author_name: text(commit.author),
    authored_at: text(commit.date),
    committed_at: text(commit.committed_at),
    url: text(commit.url),
    // The host's list carries no checks per commit.
    ci_state: null,
  };
}

/**
 * The PR's commits at `headSha`, through the session's host, in the shape the Mac's list has.
 * A list the host cut short is never the whole history.
 */
export async function readPullRequestCommitsOnHost(
  conversationId: string,
  prUrl: string,
  headSha: string,
): Promise<PullRequestCommits> {
  const answer = await readPullRequestRpc<{
    data?: unknown;
    has_more?: unknown;
    total_count?: unknown;
    head_sha?: unknown;
    base_sha?: unknown;
  }>(conversationId, "listPullRequestCommits", (client) =>
    client.listPullRequestCommits({ prUrl, headSha }),
  );
  const listed = Array.isArray(answer.data) ? (answer.data as HostCommit[]) : [];
  const commits = listed.flatMap((commit) => {
    const shaped = commit && typeof commit === "object" ? commitFromHost(commit) : null;
    return shaped ? [shaped] : [];
  });
  const total = typeof answer.total_count === "number" ? answer.total_count : commits.length;
  return {
    total_count: Math.max(total, commits.length),
    commits,
    head_sha: text(answer.head_sha),
    base_sha: text(answer.base_sha),
    complete: answer.has_more !== true && commits.length === listed.length,
  };
}

/** One commit's patch on the PR, through the session's host. */
export async function readCommitDiffOnHost(
  conversationId: string,
  prUrl: string,
  sha: string,
): Promise<CommitDiff> {
  const answer = await readPullRequestRpc<{ patch?: unknown }>(
    conversationId,
    "getCommitDiff",
    (client) => client.getCommitDiff({ prUrl, commitSha: sha }),
  );
  return { sha, patch: text(answer.patch) ?? "" };
}
