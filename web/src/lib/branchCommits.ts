// The commits of the comparison on screen, or of the session's branch, which the Changes review
// lists so one can be shown on its own. Only the session's host can list them, through the
// workspace API; until it serves them the section says so.

import { type RevisionSpec, shownCommit } from "@/lib/customComparison";
import {
  type ComparisonProblem,
  comparisonProblemFrom,
  comparisonProblemMessage,
} from "@/lib/workingCopyComparison";
import type { BranchCommit as WireBranchCommit } from "@/lib/workspaceSidebarApi";
import {
  NOT_AVAILABLE_ON_HOST,
  WorkspaceMethodUnavailableError,
  workspaceSidebarSource,
} from "@/lib/workspaceSidebarSource";

/** The query key prefix of a session's listed commits, then its host, workspace root and range. */
export const BRANCH_COMMITS_KEY = "branch-commits";

/** The most commits the host lists, newest first. */
export const MAXIMUM_BRANCH_COMMITS = 200;

/** One commit of the session's branch. */
export interface BranchCommit {
  oid: string;
  subject: string;
  authorName: string;
  /** Milliseconds since the epoch; null when the host didn't say. */
  authoredAtMs: number | null;
  /** It has no parent, so it is shown against the empty tree. */
  root: boolean;
}

/**
 * The base and head commits are listed between: an empty base is the default branch, an empty
 * head is HEAD. The default listing is the session's branch.
 */
export const BRANCH_RANGE: RevisionSpec = { base: "", head: "" };

/**
 * The commits to list for the comparison on screen: its typed base and head, or the session's
 * branch. A single commit shown on its own keeps the `previous` range, so the list stays put.
 */
export function commitsRange(custom: RevisionSpec | null, previous: RevisionSpec): RevisionSpec {
  if (!custom) return BRANCH_RANGE;
  if (shownCommit(custom)) return previous;
  return { base: custom.base, head: custom.head };
}

/** The commits a head has beyond its merge base with a base, newest first. */
export type BranchCommits =
  | {
      kind: "listed";
      commits: BranchCommit[];
      /** Only the newest commits were listed. */
      truncated: boolean;
      /** The base counted from, such as `origin/main`. */
      baseDisplay: string;
    }
  | { kind: "unavailable"; problem: ComparisonProblem };

const OBJECT_NAME = /^([0-9a-f]{40}|[0-9a-f]{64})$/;

function branchCommit(commit: WireBranchCommit): BranchCommit | null {
  if (!OBJECT_NAME.test(commit.object)) return null;
  const seconds = Number(commit.authorTimeUnix);
  return {
    oid: commit.object,
    subject: commit.subject,
    authorName: commit.authorName,
    authoredAtMs: seconds > 0 && Number.isFinite(seconds) ? seconds * 1000 : null,
    root: commit.root,
  };
}

/**
 * Lists `range`'s commits from the session's host. Rejects with
 * {@link WorkspaceMethodUnavailableError} while no route serves them, or for a named head, while
 * the host can't list one.
 */
export async function fetchBranchCommits(
  sessionId: string,
  range: RevisionSpec = BRANCH_RANGE,
  signal?: AbortSignal,
  repository = "",
): Promise<BranchCommits> {
  const response = await workspaceSidebarSource().listBranchCommits(
    sessionId,
    range,
    signal,
    repository,
  );
  const unavailable = response.base?.unavailable;
  if (unavailable) return { kind: "unavailable", problem: comparisonProblemFrom(unavailable) };
  return {
    kind: "listed",
    commits: response.commits.map(branchCommit).filter((commit) => commit !== null),
    truncated: response.truncated,
    baseDisplay: response.base?.baseDisplay || response.base?.reference || "",
  };
}

/** The Commits section until the session's host lists them. */
export const BRANCH_COMMITS_NOT_SERVED = `This branch's commits: ${NOT_AVAILABLE_ON_HOST}`;

export const BRANCH_COMMITS_FAILED = "Couldn't list this branch's commits.";

/** What the Commits section shows. */
export type BranchCommitsView =
  | { kind: "loading" }
  | { kind: "listed"; commits: BranchCommit[]; truncated: boolean; baseDisplay: string }
  | { kind: "unavailable"; message: string; canRetry: boolean };

/** The section's view of a listing, from its answer or why there is none. */
export function branchCommitsView(
  listing: BranchCommits | undefined,
  error: unknown,
): BranchCommitsView {
  if (listing?.kind === "listed") return listing;
  if (listing?.kind === "unavailable") {
    return {
      kind: "unavailable",
      message: comparisonProblemMessage(listing.problem),
      canRetry: listing.problem.transient,
    };
  }
  if (!error) return { kind: "loading" };
  if (error instanceof WorkspaceMethodUnavailableError) {
    return { kind: "unavailable", message: BRANCH_COMMITS_NOT_SERVED, canRetry: false };
  }
  // The host's own refusal says why.
  const message = error instanceof Error && error.message ? error.message : BRANCH_COMMITS_FAILED;
  return { kind: "unavailable", message, canRetry: true };
}
