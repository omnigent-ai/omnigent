import type { FileDiffMetadata } from "@pierre/diffs";

import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";

/**
 * A pull request's commits, read through the GitHub fast track. The shapes
 * mirror the desktop shell's `pull_request_commits` and `commit_diff` operations.
 */

export interface PullRequestCommit {
  sha: string;
  /** The message's first line. */
  subject: string;
  /** The rest of the message, at most about 4 KB; absent from older desktop apps. */
  body?: string | null;
  author_login: string | null;
  author_name: string | null;
  authored_at: string | null;
  committed_at: string | null;
  url: string | null;
  /** GitHub's combined status: SUCCESS, FAILURE, ERROR, PENDING, EXPECTED, or null. */
  ci_state: string | null;
}

export interface PullRequestCommits {
  total_count: number;
  commits: PullRequestCommit[];
  /** The head and base the list was read at; absent from older desktop apps. */
  head_sha?: string | null;
  base_sha?: string | null;
  /** The list is the PR's whole history at that head and base; absent from older apps. */
  complete?: boolean;
}

/** A pull request's head and base, as the panel knows them now. */
export interface PullRequestRevision {
  headSha: string | null | undefined;
  baseSha: string | null | undefined;
}

/**
 * Whether `list` describes the pull request at `current`: both its head and its base.
 * `unknown` when either side doesn't say, as an older desktop app's list doesn't.
 */
export function commitListRevision(
  list: PullRequestCommits,
  current: PullRequestRevision,
): "current" | "other" | "unknown" {
  if (!list.head_sha || !list.base_sha || !current.headSha || !current.baseSha) return "unknown";
  return list.head_sha === current.headSha && list.base_sha === current.baseSha
    ? "current"
    : "other";
}

/**
 * Whether the open commit is in the pull request. It has `left` only when the list is
 * the whole history at the pull request's current head and base; a list cut off at
 * GitHub's limit, read at another revision, or from an older app can't tell.
 */
export function openCommitMembership(
  sha: string,
  list: PullRequestCommits,
  current: PullRequestRevision,
): "listed" | "left" | "unknown" {
  if (list.commits.some((commit) => commit.sha === sha)) return "listed";
  return list.complete === true && commitListRevision(list, current) === "current"
    ? "left"
    : "unknown";
}

export interface CommitDiff {
  sha: string;
  patch: string;
}

export type CommitCiTone = "passing" | "failing" | "pending" | "none";

/** The CI glyph tone for a commit's combined status. */
export function commitCiTone(state: string | null): CommitCiTone {
  switch (state) {
    case "SUCCESS":
      return "passing";
    case "FAILURE":
    case "ERROR":
      return "failing";
    case "PENDING":
    case "EXPECTED":
      return "pending";
    default:
      return "none";
  }
}

const CI_LABELS: Record<CommitCiTone, string> = {
  passing: "Checks passed",
  failing: "Checks failed",
  pending: "Checks running",
  none: "No checks",
};

/** The accessible name for a commit's CI glyph. */
export function commitCiLabel(tone: CommitCiTone): string {
  return CI_LABELS[tone];
}

/** The seven-character SHA GitHub shows. */
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

/** How many lines of a description show before it folds behind Show more. */
export const COMMIT_BODY_FOLDED_LINES = 6;

/** Whether `body` runs past what a folded description shows: by its lines, or wrapped ones. */
export function isLongCommitBody(body: string): boolean {
  return body.split("\n").length > COMMIT_BODY_FOLDED_LINES || body.length > 480;
}

/** Who to credit for a commit: their login, else the name git recorded. */
export function commitAuthor(commit: PullRequestCommit): string | null {
  return commit.author_login ?? commit.author_name;
}

/** When the commit was authored, in epoch milliseconds, or null. */
export function commitTime(commit: PullRequestCommit): number | null {
  const time = Date.parse(commit.authored_at ?? commit.committed_at ?? "");
  return Number.isNaN(time) ? null : time;
}

/** A step to the commit just above (-1) or below (1) the one open before it. */
export type CommitStepDirection = -1 | 1;

/**
 * The commits whose patches to read ahead of the one open at `index` in `shas`: both neighbours,
 * the one in `direction` first, and after a step one more that way, so holding ↓ stays ahead.
 */
export function commitsToReadAhead(
  shas: readonly string[],
  index: number,
  direction: CommitStepDirection | null,
): string[] {
  if (index < 0 || index >= shas.length) return [];
  const ahead = direction ?? 1;
  const offsets = [ahead, -ahead, ...(direction ? [2 * direction] : [])];
  return offsets.flatMap((offset) => shas[index + offset] ?? []);
}

/** How the open commit moved from `previous` to `next` in `shas`, when it was one step. */
export function commitStepDirection(
  shas: readonly string[],
  previous: string | undefined,
  next: string | undefined,
): CommitStepDirection | null {
  if (!previous || !next) return null;
  const moved = shas.indexOf(next) - shas.indexOf(previous);
  return shas.includes(previous) && (moved === 1 || moved === -1) ? moved : null;
}

const CHANGE_STATUS: Record<FileDiffMetadata["type"], PullRequestChangedFile["status"]> = {
  new: "created",
  deleted: "deleted",
  change: "modified",
  "rename-pure": "renamed",
  "rename-changed": "renamed",
};

/** The changed-file rows for a single commit, read from its parsed patch. */
export function commitChangedFiles(files: readonly FileDiffMetadata[]): PullRequestChangedFile[] {
  return files.map((file) => {
    const path = file.name;
    const slash = path.lastIndexOf("/");
    return {
      path,
      name: slash >= 0 ? path.slice(slash + 1) : path,
      status: CHANGE_STATUS[file.type] ?? "modified",
      bytes: null,
      modified_at: null,
      lines_added: file.hunks.reduce((total, hunk) => total + hunk.additionLines, 0),
      lines_removed: file.hunks.reduce((total, hunk) => total + hunk.deletionLines, 0),
    };
  });
}

/** How long a commit read may stay pending before the tab says it's taking long. */
export const COMMITS_SLOW_AFTER_MS = 15_000;

/** Where the Commits tab reads: the shell's fast track, or the session's host. */
export type CommitsReadPath = "fast-track" | "host";

/** What the Commits tab shows, so loading, slow, failed and unavailable never look alike. */
export type CommitsTabStatus =
  | { kind: "hidden" }
  /** The fast track can't read for this account, with its own message. */
  | { kind: "unavailable"; message: string }
  /** Checking that the fast track can read as this account. */
  | { kind: "checking" }
  | { kind: "loading" }
  /** Still checking or loading past {@link COMMITS_SLOW_AFTER_MS}. */
  | { kind: "slow" }
  /** `error` is the read's own, so a Mac sign-in problem can offer its recovery. */
  | { kind: "failed"; message: string; error: Error }
  | { kind: "ready" };

export interface CommitsTabFacts {
  /** The read path this PR uses; null where neither can read it. */
  path: CommitsReadPath | null;
  /** The fast track's state, which matters only on the fast-track path. */
  fastTrack:
    { status: "unsupported" | "checking" | "ready" } | { status: "unavailable"; message: string };
  /** The list query: whether it has an answer, and its error. */
  list: { hasData: boolean; error: Error | null };
  /** Whether the current check or read has been pending past the time bound. */
  timedOut: boolean;
  /** Whether the Commits tab is the one on screen. */
  onTab: boolean;
}

/**
 * The Commits tab's state. A PR read through the host never waits on the fast track, which
 * it doesn't use; an unsupported host hides the tab rather than spinning.
 */
export function commitsTabStatus(facts: CommitsTabFacts): CommitsTabStatus {
  if (facts.path === null) return { kind: "hidden" };
  if (facts.path === "fast-track") {
    const { fastTrack } = facts;
    if (fastTrack.status === "unsupported") return { kind: "hidden" };
    if (fastTrack.status === "unavailable")
      return { kind: "unavailable", message: fastTrack.message };
    if (fastTrack.status === "checking") {
      // A check only shows as a tab once someone is on it.
      if (!facts.onTab) return { kind: "hidden" };
      return facts.timedOut ? { kind: "slow" } : { kind: "checking" };
    }
  }
  if (facts.list.hasData) return { kind: "ready" };
  if (facts.list.error) {
    return { kind: "failed", message: facts.list.error.message, error: facts.list.error };
  }
  return facts.timedOut ? { kind: "slow" } : { kind: "loading" };
}
