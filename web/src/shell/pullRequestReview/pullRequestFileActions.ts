// A pull request file's header actions in a diff review, shared by the PR's
// Changes review and a single commit's review: its path, and the file on the
// PR's code host at the revision the review shows.

import type { SelectedLineRange } from "@pierre/diffs";
import type { DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import { pullRequestFileLink, stackComparisonFileLink } from "../fileCommands/codeHostFileLink";
import {
  REPOSITORY_FILE_ENVIRONMENT,
  reviewFileActions,
  reviewFolderActions,
} from "../fileCommands/reviewFileActions";
import { resolveFileCommandTarget } from "../fileCommands/target";

/** Where a review's files live: the PR, its provider, and the commit the review shows. */
export interface PullRequestFileRevision {
  conversationId: string;
  prUrl: string;
  /** The PR's git provider id; absent from hosts that predate providers (GitHub). */
  provider?: string | null;
  /** The commit whose files the review shows: the PR head, or one of its commits. */
  sha: string | null | undefined;
  /**
   * Where the PR's commits live, such as a fork; absent from hosts too old to say, which
   * open the PR's own repository, and null when it isn't known.
   */
  headRepositoryUrl?: string | null;
}

/**
 * `file`'s header actions: copying its path and, unless it was deleted, opening
 * it on the PR's code host at `revision.sha`, at the selected lines in it.
 */
export function pullRequestFileActions(
  file: DiffReviewFile,
  revision: PullRequestFileRevision,
  selection: { path: string; range: SelectedLineRange } | null,
) {
  const target = resolveFileCommandTarget(file.path, {
    root: null,
    hostId: null,
    conversationId: revision.conversationId,
    deleted: file.change === "deleted",
    repositoryFile: true,
  });
  const codeHostLink = pullRequestFileLink({
    prUrl: revision.prUrl,
    headSha: revision.sha,
    // Only a revision that says passes it on, so "absent" stays absent, not null.
    ...("headRepositoryUrl" in revision ? { headRepositoryUrl: revision.headRepositoryUrl } : {}),
    path: file.path,
    change: file.change,
    selection: selection?.path === file.path ? selection.range : null,
    githubHosts: pullRequestGitHubHosts(revision),
  });
  return reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT, {
    codeHostLink,
    previousPath: file.previousPath ?? null,
  });
}

/** A changed-files tree folder's actions in a PR's review: copying its path in the repository. */
export function pullRequestFolderActions(path: string, conversationId: string) {
  const target = resolveFileCommandTarget(path, {
    root: null,
    hostId: null,
    conversationId,
    repositoryFile: true,
  });
  return reviewFolderActions(target, REPOSITORY_FILE_ENVIRONMENT);
}

/** Where a stacked PR's own changes come from: the PR, and the two heads they're between. */
export interface StackComparisonFileRevision {
  conversationId: string;
  prUrl: string;
  provider?: string | null;
  childHead: string;
  parentHead: string;
  /** Where the PR's and its parent's commits live; a key absent stays absent, not null. */
  repositories?: StackRepositories;
}

/**
 * Where a stacked PR's and its parent's commits live: the PR's as `head_repository_url`, the
 * parent's as `owner/repo`. A key is absent where the data doesn't say, null where it's unknown.
 */
export interface StackRepositories {
  headRepositoryUrl?: string | null;
  parentHeadRepository?: string | null;
}

/**
 * A file's header actions in a stacked PR's own changes: copying its path and opening it on
 * the code host on the side its selected lines are on, at the old path for old lines.
 */
export function stackComparisonFileActions(
  file: DiffReviewFile,
  previousPath: string | null,
  revision: StackComparisonFileRevision,
  selection: { path: string; range: SelectedLineRange } | null,
) {
  const target = resolveFileCommandTarget(file.path, {
    root: null,
    hostId: null,
    conversationId: revision.conversationId,
    deleted: file.change === "deleted",
    repositoryFile: true,
  });
  const codeHostLink = stackComparisonFileLink({
    prUrl: revision.prUrl,
    childHead: revision.childHead,
    parentHead: revision.parentHead,
    ...revision.repositories,
    path: file.path,
    previousPath,
    change: file.change,
    selection: selection?.path === file.path ? selection.range : null,
    githubHosts: pullRequestGitHubHosts(revision),
  });
  return reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT, { codeHostLink, previousPath });
}

/** The PR's own host when its provider is GitHub, so a GitHub Enterprise link is recognized. */
function pullRequestGitHubHosts({
  prUrl,
  provider,
}: Pick<PullRequestFileRevision, "prUrl" | "provider">): string[] {
  // No provider id comes from a host that predates them, which served GitHub.
  if (provider && provider !== "github") return [];
  try {
    return [new URL(prUrl).hostname];
  } catch {
    return [];
  }
}
