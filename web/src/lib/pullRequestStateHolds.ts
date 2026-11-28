// Pull request changes that hold each other, and the state changes GitHub may not have
// confirmed yet. Host-neutral: any write path consults these before it sends.

import { useSyncExternalStore } from "react";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";

/** The GitHub write set: every write the panel makes, and the reads its controls need. */
export const GITHUB_WRITE_SET_V1 = [
  "pull_request_comment",
  "pull_request_review_reply",
  "pull_request_review_thread_create",
  "pull_request_labels_update",
  "pull_request_reviewers_update",
  "pull_request_details_update",
  "pull_request_auto_merge_disable",
  "pull_request_merge",
  "pull_request_label_options",
  "pull_request_review_candidates",
  "pull_request_merge_options",
] as const;

export type GitHubWriteOperation = (typeof GITHUB_WRITE_SET_V1)[number];

/**
 * Merge queue changes, beyond set v1: each is offered on its own, where the shell lists it
 * and the queue's flag is on, on top of the whole set's route.
 */
export const MERGE_QUEUE_WRITE_OPERATIONS = [
  "pull_request_merge_queue_add",
  "pull_request_merge_queue_remove",
] as const;

export type MergeQueueWriteOperation = (typeof MERGE_QUEUE_WRITE_OPERATIONS)[number];

/**
 * Changes to a PR's state beyond set v1 (closing, reopening, draft or ready, turning on
 * auto-merge): each is offered on its own, where the shell lists it and the lifecycle flag is
 * on, on top of the whole set's route.
 */
export const LIFECYCLE_WRITE_OPERATIONS = [
  "pull_request_close",
  "pull_request_reopen",
  "pull_request_convert_to_draft",
  "pull_request_ready_for_review",
  "pull_request_auto_merge_enable",
  "pull_request_auto_merge_enable_many",
] as const;

export type LifecycleWriteOperation = (typeof LIFECYCLE_WRITE_OPERATIONS)[number];

/** What the PR page's and rows' state store makes: these, and turning auto-merge off. */
export type LifecycleStoreOperation = LifecycleWriteOperation | "pull_request_auto_merge_disable";

/** Closing and reopening: the two the comment box's state chain makes. */
export type StateChangeOperation = Extract<
  LifecycleWriteOperation,
  "pull_request_close" | "pull_request_reopen"
>;

/**
 * Editing and deleting the viewer's own comments, beyond set v1: each is offered on its own,
 * where the shell lists it and the comment edits flag is on, on top of the whole set's route.
 */
export const COMMENT_EDIT_WRITE_OPERATIONS = [
  "pull_request_comment_edit",
  "pull_request_comment_delete",
] as const;

export type CommentEditWriteOperation = (typeof COMMENT_EDIT_WRITE_OPERATIONS)[number];

/**
 * Submitting a review, beyond set v1: offered where the shell lists it and the review flag is
 * on, on top of the whole set's route.
 */
export const REVIEW_WRITE_OPERATIONS = ["pull_request_review_submit"] as const;

export type ReviewWriteOperation = (typeof REVIEW_WRITE_OPERATIONS)[number];

/**
 * A pending review's changes: adding, editing and deleting its drafts, and discarding it.
 * Finishing one is the review submit with its id. Offered only all together, with the read.
 */
export const PENDING_REVIEW_WRITE_OPERATIONS = [
  "pull_request_review_draft_add",
  "pull_request_review_draft_edit",
  "pull_request_review_draft_delete",
  "pull_request_review_discard",
] as const;

export type PendingReviewWriteOperation = (typeof PENDING_REVIEW_WRITE_OPERATIONS)[number];

/**
 * Resolving and unresolving a review conversation, beyond set v1: each is offered where the
 * shell lists it, with no flag of its own. Main makes one change per conversation at a time.
 */
export const THREAD_RESOLUTION_WRITE_OPERATIONS = [
  "pull_request_review_thread_resolve",
  "pull_request_review_thread_unresolve",
] as const;

export type ThreadResolutionWriteOperation = (typeof THREAD_RESOLUTION_WRITE_OPERATIONS)[number];

export const THREAD_RESOLUTION_GROUP: ReadonlySet<string> = new Set(
  THREAD_RESOLUTION_WRITE_OPERATIONS,
);

/**
 * Adding and removing the viewer's reaction on a comment, beyond set v1: offered where the
 * shell lists both and the read that settles one, with no flag of their own.
 */
export const COMMENT_REACTION_WRITE_OPERATIONS = [
  "pull_request_comment_reaction_add",
  "pull_request_comment_reaction_remove",
] as const;

export type CommentReactionWriteOperation = (typeof COMMENT_REACTION_WRITE_OPERATIONS)[number];

/** The read that settles a reaction GitHub didn't confirm, listed with the reaction writes. */
export const COMMENT_REACTIONS_READ = "pull_request_comment_reactions";

export const COMMENT_REACTION_GROUP: ReadonlySet<string> = new Set(
  COMMENT_REACTION_WRITE_OPERATIONS,
);

/** The read of the account's pending review, which the shell lists among its writes. */
export const PENDING_REVIEW_READ = "pull_request_pending_review";
/**
 * The read of one of the viewer's reviews by its id, which settles a discard; a shell without
 * it leaves an unconfirmed discard held.
 */
export const PENDING_REVIEW_STATE_READ = "pull_request_review_state";

/** Any change this page makes; the name is kept from the operation list it came from. */
export type MacWriteOperation =
  | GitHubWriteOperation
  | MergeQueueWriteOperation
  | LifecycleWriteOperation
  | CommentEditWriteOperation
  | ReviewWriteOperation
  | PendingReviewWriteOperation
  | ThreadResolutionWriteOperation
  | CommentReactionWriteOperation;

/** Changes that race each other's state on a PR; main and this page hold one at a time. */
export const MERGE_WRITE_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>([
  "pull_request_merge",
  "pull_request_auto_merge_disable",
  ...MERGE_QUEUE_WRITE_OPERATIONS,
  ...LIFECYCLE_WRITE_OPERATIONS,
]);
// Main makes one comment edit or delete per PR at a time; this page waits the same way.
export const COMMENT_EDIT_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>(
  COMMENT_EDIT_WRITE_OPERATIONS,
);
// A review and single inline comments race the account's pending review, so main holds one.
export const REVIEW_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>([
  ...REVIEW_WRITE_OPERATIONS,
  ...PENDING_REVIEW_WRITE_OPERATIONS,
  "pull_request_review_reply",
  "pull_request_review_thread_create",
]);
export const writeGroup = (operation: MacWriteOperation): string =>
  MERGE_WRITE_GROUP.has(operation)
    ? "merge"
    : COMMENT_EDIT_GROUP.has(operation)
      ? "comment_edit"
      : REVIEW_GROUP.has(operation)
        ? "review"
        : operation;

/** Editing or deleting a comment: a published one, or a draft in the pending review. */
export type CommentChangeOperation =
  CommentEditWriteOperation | "pull_request_review_draft_edit" | "pull_request_review_draft_delete";

/** A pull request as main tells its writes apart: host, repository and number, any case. */
export function pullRequestWriteKey(
  target: Pick<CodeHostPullRequest, "host" | "repository" | "number">,
) {
  return [target.host.toLowerCase(), target.repository.toLowerCase(), target.number].join("\n");
}

// Merge queue changes main couldn't confirm, by workspace user and PR. Nothing more is
// sent for that PR's queue until a read made after Check again shows where it stands.
// Each marker has its own revision, so a check only clears the marker it began for.
interface UnconfirmedQueueChange {
  owner: string;
  prUrl: string;
  operation: MergeQueueWriteOperation;
  revision: number;
  /** The login it was sent as; only it can check it. */
  madeAs: { login: string };
}
const unconfirmedQueueChanges = new Map<string, UnconfirmedQueueChange>();
const unconfirmedQueueListeners = new Set<() => void>();
let unconfirmedQueueVersion = 0;
let unconfirmedQueueRevision = 0;

/** The PR as main tells it apart, on any host, so any spelling of its URL shares one hold. */
function unconfirmedQueueKey(owner: string, prUrl: string): string {
  return `${owner}\n${pullRequestIdentity(prUrl)}`;
}

function unconfirmedQueueChanged(): void {
  unconfirmedQueueVersion += 1;
  for (const listener of unconfirmedQueueListeners) listener();
  holdsChanged();
}

/**
 * `owner`'s `operation` on `prUrl`, sent as `madeAs`, may or may not have happened on GitHub;
 * its revision.
 */
export function markQueueChangeUnconfirmed(
  owner: string,
  prUrl: string,
  operation: MergeQueueWriteOperation,
  madeAs: { login: string },
): number {
  unconfirmedQueueRevision += 1;
  unconfirmedQueueChanges.set(unconfirmedQueueKey(owner, prUrl), {
    owner,
    prUrl,
    operation,
    revision: unconfirmedQueueRevision,
    madeAs: { login: madeAs.login },
  });
  unconfirmedQueueChanged();
  return unconfirmedQueueRevision;
}

/**
 * A read made after the marker at `revision` showed `owner` where `prUrl` stands. A newer
 * marker, from a change made since that check began, stays.
 */
export function confirmQueueChangeChecked(owner: string, prUrl: string, revision: number): void {
  const key = unconfirmedQueueKey(owner, prUrl);
  if (unconfirmedQueueChanges.get(key)?.revision !== revision) return;
  unconfirmedQueueChanges.delete(key);
  unconfirmedQueueChanged();
}

/** The current workspace user's unconfirmed queue change on `prUrl`, or null. */
export function unconfirmedQueueChange(prUrl: string): UnconfirmedQueueChange | null {
  return unconfirmedQueueChanges.get(unconfirmedQueueKey(draftOwner(), prUrl)) ?? null;
}

/** {@link unconfirmedQueueChange}, re-read as it changes or the workspace user does. */
export function useUnconfirmedQueueChange(prUrl: string): UnconfirmedQueueChange | null {
  useWorkspaceDraftOwner();
  useSyncExternalStore(
    (listener) => {
      unconfirmedQueueListeners.add(listener);
      return () => unconfirmedQueueListeners.delete(listener);
    },
    () => unconfirmedQueueVersion,
    () => 0,
  );
  return unconfirmedQueueChange(prUrl);
}

/**
 * Changes to a PR's state GitHub may or may not have made, by workspace user and PR: its link,
 * or its {@link pullRequestIdentity}.
 */
export type UnconfirmedStateChanges = () => Iterable<
  { owner: string; prUrl: string } | { owner: string; identity: string }
>;
// A merge whose answer was lost, by workspace user and PR. Held until a read begun after it
// shows where the PR stands; nothing is sent again meanwhile.
interface UnconfirmedMerge {
  owner: string;
  prUrl: string;
  revision: number;
}
const unconfirmedMerges = new Map<string, UnconfirmedMerge>();
let unconfirmedMergeRevision = 0;

/** The PR as its lock sees it, for `owner`: one key per user and pull request. */
function unconfirmedMergeKey(owner: string, prUrl: string): string {
  return `${owner}\n${pullRequestIdentity(prUrl)}`;
}

/** Hold `owner`'s merge of `prUrl`, whose answer was lost, until a read settles it; its revision. */
export function markMergeUnconfirmed(owner: string, prUrl: string): number {
  unconfirmedMergeRevision += 1;
  unconfirmedMerges.set(unconfirmedMergeKey(owner, prUrl), {
    owner,
    prUrl,
    revision: unconfirmedMergeRevision,
  });
  holdsChanged();
  return unconfirmedMergeRevision;
}

/** The part of a PR read that says where the PR stands. */
interface PullRequestStateAnswer {
  selected_pr_url?: string | null;
  pr?: { state?: string | null } | null;
}

/**
 * Run `read` for `prUrl`. An answer that shows the PR's state settles a merge held when the read
 * began; one that fails leaves the hold in place.
 */
export async function readSettlingMerge<Answer extends PullRequestStateAnswer>(
  prUrl: string,
  read: () => Promise<Answer>,
): Promise<Answer> {
  const key = unconfirmedMergeKey(draftOwner(), prUrl);
  const marker = unconfirmedMerges.get(key);
  const answer = await read();
  const state = answer.pr?.state?.toUpperCase();
  const shows =
    answer.selected_pr_url != null &&
    pullRequestIdentity(answer.selected_pr_url) === pullRequestIdentity(prUrl) &&
    (state === "OPEN" || state === "CLOSED" || state === "MERGED");
  if (marker && shows && unconfirmedMerges.get(key)?.revision === marker.revision) {
    unconfirmedMerges.delete(key);
    holdsChanged();
  }
  return answer;
}

// A queue change GitHub may have made holds the PR's other state changes too; so may a merge
// whose answer was lost.
const unconfirmedStateChangeSources = new Set<UnconfirmedStateChanges>([
  () => unconfirmedQueueChanges.values(),
  () => unconfirmedMerges.values(),
]);

/**
 * Hold merging, auto-merge, the queue and state changes for each PR `source` lists, for its
 * workspace user, as if a write were in flight. Call {@link unconfirmedStateChangesChanged}
 * whenever what it lists changes.
 */
export function registerUnconfirmedStateChanges(source: UnconfirmedStateChanges): () => void {
  unconfirmedStateChangeSources.add(source);
  holdsChanged();
  return () => {
    if (unconfirmedStateChangeSources.delete(source)) holdsChanged();
  };
}

export function unconfirmedStateChangesChanged(): void {
  holdsChanged();
}

const PULL_REQUEST_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)\/?$/;

/** A PR's identity from its link alone, whichever route reads it now, or null. */
export function pullRequestUrlKey(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const match = PULL_REQUEST_PATH.exec(parsed.pathname);
  if (parsed.protocol !== "https:" || !match) return null;
  return pullRequestWriteKey({
    host: parsed.host,
    repository: `${match[1]}/${match[2]}`,
    number: Number(match[3]),
  });
}

/** One key per pull request, whatever case or trailing part its link was opened with. */
export function pullRequestIdentity(url: string): string {
  return pullRequestUrlKey(url) ?? url.toLowerCase();
}

// A change GitHub may have made holds the PR's state for its user until a fresh read.
function unconfirmedFor(key: string): boolean {
  const owner = draftOwner();
  for (const source of unconfirmedStateChangeSources) {
    for (const change of source()) {
      if (change.owner !== owner) continue;
      const identity = "identity" in change ? change.identity : pullRequestUrlKey(change.prUrl);
      if (identity === key) return true;
    }
  }
  return false;
}

/**
 * Whether `operation` on the PR at `prUrl` waits for an unconfirmed change to it. Only a
 * fresh read settles what GitHub may have done.
 */
export function unconfirmedStateChangeHolds(
  prUrl: string | null | undefined,
  operation: MacWriteOperation,
): boolean {
  if (!prUrl || writeGroup(operation) !== "merge") return false;
  const key = pullRequestUrlKey(prUrl);
  return key !== null && unconfirmedFor(key);
}

/** Why a held change wasn't sent. */
export const UNCONFIRMED_STATE_CHANGE_MESSAGE =
  "GitHub may have made an earlier change to this pull request. Check it again before changing it.";

// Pull requests whose state is about to change: a close or reopen is posting its comment
// first, so merging, auto-merge, the queue and other closes wait for it as if it were sent.
const heldStateChanges = new Map<number, string>();
let lastStateChangeHold = 0;

/**
 * Hold the PR at `prUrl`'s state changes as in flight until the returned release runs. The
 * release is called once, right before the held change itself is sent, in the same task.
 */
export function holdPullRequestStateChanges(prUrl: string): () => void {
  lastStateChangeHold += 1;
  const id = lastStateChangeHold;
  heldStateChanges.set(id, pullRequestIdentity(prUrl));
  holdsChanged();
  return () => {
    if (heldStateChanges.delete(id)) holdsChanged();
  };
}

const holdsListeners = new Set<() => void>();

function holdsChanged(): void {
  for (const listener of holdsListeners) listener();
}

/** Hear every change to what holds a pull request's state changes, outside React. */
export function subscribePullRequestStateHolds(listener: () => void): () => void {
  holdsListeners.add(listener);
  return () => holdsListeners.delete(listener);
}

/** What a caller hears when another change to the same pull request is still in progress. */
export const CHANGE_IN_PROGRESS_MESSAGE =
  "This pull request is already being changed. Wait for it to finish.";

/**
 * Send `send` as a state change to the PR at `prUrl`, under the same rules as merging and
 * closing: refused while another is unconfirmed or in flight, and held until its own answer.
 */
export async function sendStateChange<Result>(
  prUrl: string,
  operation: MacWriteOperation,
  send: () => Promise<Result>,
): Promise<Result> {
  if (unconfirmedStateChangeHolds(prUrl, operation)) {
    throw new PullRequestWriteError("unconfirmed", UNCONFIRMED_STATE_CHANGE_MESSAGE);
  }
  if (!pullRequestUrlKey(prUrl) || writeGroup(operation) !== "merge") return send();
  if (pullRequestStateChangesHeld(prUrl)) {
    throw new PullRequestWriteError("busy", CHANGE_IN_PROGRESS_MESSAGE);
  }
  const release = holdPullRequestStateChanges(prUrl);
  try {
    return await send();
  } finally {
    release();
  }
}

/** Whether a merge, auto-merge, queue or state change to `prUrl` waits for a held or unconfirmed one. */
export function pullRequestStateChangesHeld(prUrl: string): boolean {
  const key = pullRequestUrlKey(prUrl);
  if (key === null) return false;
  for (const held of heldStateChanges.values()) {
    if (held === key) return true;
  }
  return unconfirmedFor(key);
}

/** Forget every hold and unconfirmed queue change, so a test starts clean. */
export function resetPullRequestStateHoldsForTests(): void {
  heldStateChanges.clear();
  unconfirmedQueueChanges.clear();
  unconfirmedQueueListeners.clear();
  unconfirmedQueueVersion = 0;
  unconfirmedQueueRevision = 0;
  unconfirmedMerges.clear();
  unconfirmedMergeRevision = 0;
}
