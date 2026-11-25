// Which path changes a pull request: this Mac, through the desktop shell as the
// account the panel already reads it as, or the session's host as today. A write
// goes to the Mac only when the PR is read on this Mac, the shell and this page
// both implement the whole GitHub write set, the release flag and the user's
// setting allow it, and an account is resolved. Otherwise nothing changes.
//
// Where the Pull Requests page reads a PR as one of this Mac's logins by name, its changes
// are made as that login or not at all: never on the session's machine or as the host's
// account, which would act as someone other than who the page shows.
//
// A write captures a ticket at the click and is sent in the same task. Its answer may
// update cached reads only if the same user, read path, account and read order still
// hold at completion. When the workspace user changes, every Mac write of the user
// before is cancelled, and its answer is dropped without a trace.

import { randomUUID } from "@/lib/randomUUID";
import { pullRequestToastName } from "@/lib/pullRequestToastName";
import { pullRequestRecordReaderIn } from "@/lib/pullRequestRecord";
import { type MacWriteRun, macWriteRunGrant, startMacWriteRun } from "@/lib/macWriteRuns";
import {
  NAMED_REFUSED_CODES,
  namedPullRequestWritten,
  namedReadScope,
  namedReadsAvailable,
  namedReaderChanged,
  sameNamedReader,
  namedReaderRefused,
  namedWriterIdentity,
  subscribeNamedReads,
  useNamedReadsVersion,
  type MacNamedReader,
} from "@/lib/macNamedReads";
import { useSyncExternalStore } from "react";

import { parseCodeHostPullRequestUrl, type CodeHostPullRequest } from "@/lib/codeHosts";
import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import {
  desktopReadSupport,
  macAccountChanged,
  macIdentity,
  macIdentityChecking,
  macNamedWritesOffered,
  macReadCandidate,
  macReadTarget,
  refreshDesktopReadSupport,
  repoReaderFor,
  subscribeDesktopReadPath,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
// Installs the repository choices' policy before any write is decided.
import "@/lib/pullRequestRepoReader";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  acceptedMacReader,
  beginMacRead,
  macReadCurrent,
  macReadEpoch,
  macReadScopeKey,
  type MacReadScopeKey,
  type MacReadTicket,
} from "@/lib/macReadScope";
import {
  CODE_HOST_WRITE_OUTCOME_UNKNOWN,
  cancelDesktopCodeHostWrite,
  desktopCodeHostWrite,
  desktopCodeHostWriteAs,
  hasCodeHostWriteBridge,
  type CodeHostWriteSteps,
  type DesktopCodeHost,
  type PendingDraftBaseline,
  type PendingReviewStart,
} from "@/lib/nativeBridge";

/**
 * The whole GitHub write set: every write the panel makes, and the reads its
 * controls need. Neither side routes a write to the Mac until both have all of it.
 */
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

const THREAD_RESOLUTION_GROUP: ReadonlySet<string> = new Set(THREAD_RESOLUTION_WRITE_OPERATIONS);

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

const COMMENT_REACTION_GROUP: ReadonlySet<string> = new Set(COMMENT_REACTION_WRITE_OPERATIONS);

/** The read of the account's pending review, which the shell lists among its writes. */
export const PENDING_REVIEW_READ = "pull_request_pending_review";
/**
 * The read of one of the viewer's reviews by its id, which settles a discard; a shell without
 * it leaves an unconfirmed discard held.
 */
export const PENDING_REVIEW_STATE_READ = "pull_request_review_state";

/** Any change this page sends to the Mac. */
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
const MERGE_WRITE_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>([
  "pull_request_merge",
  "pull_request_auto_merge_disable",
  ...MERGE_QUEUE_WRITE_OPERATIONS,
  ...LIFECYCLE_WRITE_OPERATIONS,
]);
// Main makes one comment edit or delete per PR at a time; this page waits the same way.
const COMMENT_EDIT_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>(
  COMMENT_EDIT_WRITE_OPERATIONS,
);
// A review and single inline comments race the account's pending review, so main holds one.
const REVIEW_GROUP: ReadonlySet<MacWriteOperation> = new Set<MacWriteOperation>([
  ...REVIEW_WRITE_OPERATIONS,
  ...PENDING_REVIEW_WRITE_OPERATIONS,
  "pull_request_review_reply",
  "pull_request_review_thread_create",
]);
const writeGroup = (operation: MacWriteOperation): string =>
  MERGE_WRITE_GROUP.has(operation)
    ? "merge"
    : COMMENT_EDIT_GROUP.has(operation)
      ? "comment_edit"
      : REVIEW_GROUP.has(operation)
        ? "review"
        : operation;

/** The writes this page implements: the whole set, so a shell that has it too routes here. */
export const PAGE_WRITE_OPERATIONS: readonly GitHubWriteOperation[] = [...GITHUB_WRITE_SET_V1];

let pageWriteOperations: readonly string[] = PAGE_WRITE_OPERATIONS;

/** Pretend this page implements `operations`, so tests can exercise the Mac path; null resets. */
export function setPageWriteOperationsForTests(operations: readonly string[] | null): void {
  pageWriteOperations = operations ?? PAGE_WRITE_OPERATIONS;
  changed();
}

/** The remote flag Automatic follows for changes on this Mac. */
export const DESKTOP_CODE_HOST_WRITES_FLAG = "desktop-code-host-writes";
/** The remote flag merge queue changes on this Mac also need. */
export const DESKTOP_CODE_HOST_MERGE_QUEUE_FLAG = "desktop-code-host-merge-queue";
/** The remote flag closing and reopening on this Mac also need. */
export const DESKTOP_CODE_HOST_LIFECYCLE_FLAG = "desktop-code-host-lifecycle-writes";
/** The remote flag editing and deleting comments on this Mac also need. */
export const DESKTOP_CODE_HOST_COMMENT_EDITS_FLAG = "desktop-code-host-comment-edits";
/** The remote flag submitting reviews on this Mac also needs. */
export const DESKTOP_CODE_HOST_REVIEWS_FLAG = "desktop-code-host-review-writes";
/** The flag pending reviews need on top of the review flag. */
export const PULL_REQUEST_PENDING_REVIEWS_FLAG = "pull-request-pending-reviews";

/** The user's choice. There's no On: Automatic follows the release flag, and Off keeps the host. */
export type MacWritesIntent = "automatic" | "off";

const WRITES_INTENT_KEY = "omnigent:desktop-code-host-writes";
const WRITES_INTENT_EVENT = `${WRITES_INTENT_KEY}-change`;

/** The stored intent; storage errors and unknown values mean Automatic. */
export function readMacWritesIntent(): MacWritesIntent {
  if (typeof window === "undefined") return "automatic";
  try {
    return window.localStorage.getItem(WRITES_INTENT_KEY) === "off" ? "off" : "automatic";
  } catch {
    return "automatic";
  }
}

/** Persist the intent; Automatic is stored as nothing, so a later policy reaches everyone on it. */
export function writeMacWritesIntent(intent: MacWritesIntent): void {
  if (typeof window === "undefined") return;
  try {
    if (intent === "automatic") window.localStorage.removeItem(WRITES_INTENT_KEY);
    else window.localStorage.setItem(WRITES_INTENT_KEY, intent);
  } catch {
    // A storage failure must not break Settings.
  }
  window.dispatchEvent(new Event(WRITES_INTENT_EVENT));
}

/** Whether changes may go to the Mac: Off never, Automatic while the release flag is on. */
export function macWritesEnabled(intent: MacWritesIntent, releaseFlag: boolean): boolean {
  return intent === "automatic" && releaseFlag;
}

/** Whether a shell's GitHub entry advertises every write in set v1, at version 1. */
export function shellWritesComplete(github: DesktopCodeHost | null | undefined): boolean {
  const writes = github?.writes;
  if (!writes || writes.version !== 1) return false;
  return GITHUB_WRITE_SET_V1.every((operation) => writes.operations.includes(operation));
}

/** Whether this page implements every write in set v1. */
export function pageWritesComplete(operations: readonly string[] = pageWriteOperations): boolean {
  return GITHUB_WRITE_SET_V1.every((operation) => operations.includes(operation));
}

// The live store: the release flag and the intent; read support comes from the read path.
let releaseFlag = false;
// The merge queue's own flag, off until the shell answers and on a shell without it.
let mergeQueueFlag = false;
let mergeQueueFlagForTests: boolean | null = null;
let pageMergeQueueOperations: readonly string[] = MERGE_QUEUE_WRITE_OPERATIONS;
// Closing and reopening's own flag, off until the shell answers and on a shell without it.
let lifecycleFlag = false;
let lifecycleFlagForTests: boolean | null = null;
let pageLifecycleOperations: readonly string[] = LIFECYCLE_WRITE_OPERATIONS;
// Comment edits' own flag, off until the shell answers and on a shell without it.
let commentEditsFlag = false;
let commentEditsFlagForTests: boolean | null = null;
let pageCommentEditOperations: readonly string[] = COMMENT_EDIT_WRITE_OPERATIONS;
let pageThreadResolutionOperations: readonly string[] = THREAD_RESOLUTION_WRITE_OPERATIONS;
let pageCommentReactionOperations: readonly string[] = COMMENT_REACTION_WRITE_OPERATIONS;

/** Pretend this page implements only `operations`, as an older bundle would; null resets. */
export function setPageCommentReactionOperationsForTests(
  operations: readonly string[] | null,
): void {
  pageCommentReactionOperations = operations ?? COMMENT_REACTION_WRITE_OPERATIONS;
  changed();
}

/** Pretend this page implements only `operations`, as an older bundle would; null resets. */
export function setPageThreadResolutionOperationsForTests(
  operations: readonly string[] | null,
): void {
  pageThreadResolutionOperations = operations ?? THREAD_RESOLUTION_WRITE_OPERATIONS;
  changed();
}
// Reviews' own flag, off until the shell answers and on a shell without it.
let reviewsFlag = false;
let reviewsFlagForTests: boolean | null = null;
let pageReviewOperations: readonly string[] = REVIEW_WRITE_OPERATIONS;
// Pending reviews' own flag, off until the shell answers and on a shell without it.
let pendingReviewsFlag = false;
let pendingReviewsFlagForTests: boolean | null = null;
let pagePendingReviewOperations: readonly string[] = PENDING_REVIEW_WRITE_OPERATIONS;
// Set by tests in place of the shell's flag, so the real subscription can't race them.
let releaseFlagForTests: boolean | null = null;
let started = false;
let stopWatching: (() => void) | null = null;
let version = 0;
const listeners = new Set<() => void>();

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

// While the flag says on, how often to ask the shell again whether it offers writes: it
// offers them only from a freshly fetched flag, which can lag, fail or expire, and tells
// the page nothing when that changes. Quickly at first while it doesn't, then backing off.
const SUPPORT_RECHECK = { first: 2_000, max: 60_000, offered: 5 * 60_000 };
let supportRecheck = SUPPORT_RECHECK;
let supportTimer: ReturnType<typeof setTimeout> | null = null;
let supportRecheckDelay = supportRecheck.first;

/** Whether the shell offers the whole write set now, as its last answer said. */
function shellOffersWrites(): boolean {
  return shellWritesComplete(desktopReadSupport()?.github);
}

/** Ask the shell what it offers, then schedule the next ask from its answer. */
async function recheckShellWrites(): Promise<void> {
  await refreshDesktopReadSupport();
  changed();
  scheduleShellWritesRecheck();
}

function scheduleShellWritesRecheck(): void {
  if (supportTimer !== null) clearTimeout(supportTimer);
  supportTimer = null;
  if (!(releaseFlagForTests ?? releaseFlag)) return;
  // Once it does, now and then, so an expiry is seen before the next click finds it.
  let delay = supportRecheck.offered;
  if (shellOffersWrites()) supportRecheckDelay = supportRecheck.first;
  else {
    delay = supportRecheckDelay;
    supportRecheckDelay = Math.min(supportRecheckDelay * 2, supportRecheck.max);
  }
  supportTimer = setTimeout(() => void recheckShellWrites(), delay);
}

/** After main refused because writes are off, ask the shell again before the next click. */
export function shellWritesTurnedOff(): void {
  supportRecheckDelay = supportRecheck.first;
  void recheckShellWrites();
}

function start(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  const stopFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_WRITES_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === releaseFlag) return;
      releaseFlag = next;
      changed();
      // The shell advertises writes only while the flag is on, so ask it again, and keep
      // asking while it doesn't yet.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const stopQueueFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_MERGE_QUEUE_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === mergeQueueFlag) return;
      mergeQueueFlag = next;
      changed();
      // The shell lists queue changes only while its own copy of the flag is on.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const stopLifecycleFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_LIFECYCLE_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === lifecycleFlag) return;
      lifecycleFlag = next;
      changed();
      // The shell lists closing and reopening only while its own copy of the flag is on.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const stopCommentEditsFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_COMMENT_EDITS_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === commentEditsFlag) return;
      commentEditsFlag = next;
      changed();
      // The shell lists comment edits only while its own copy of the flag is on.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const stopReviewsFlag = subscribeDesktopFeatureFlag(
    DESKTOP_CODE_HOST_REVIEWS_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === reviewsFlag) return;
      reviewsFlag = next;
      changed();
      // The shell lists the review writes only while its own copy of the flag is on.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const stopPendingReviewsFlag = subscribeDesktopFeatureFlag(
    PULL_REQUEST_PENDING_REVIEWS_FLAG,
    { fallback: false, mode: "resolved" },
    ({ value }) => {
      const next = value === true;
      if (next === pendingReviewsFlag) return;
      pendingReviewsFlag = next;
      changed();
      // The shell lists the pending review writes only while its own copy of the flag is on.
      supportRecheckDelay = supportRecheck.first;
      void recheckShellWrites();
    },
  );
  const onFocus = () => {
    if (releaseFlagForTests ?? releaseFlag) void recheckShellWrites();
  };
  window.addEventListener("focus", onFocus);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === WRITES_INTENT_KEY) changed();
  };
  window.addEventListener(WRITES_INTENT_EVENT, changed);
  window.addEventListener("storage", onStorage);
  stopWatching = () => {
    stopFlag();
    stopQueueFlag();
    stopLifecycleFlag();
    stopCommentEditsFlag();
    stopReviewsFlag();
    stopPendingReviewsFlag();
    window.removeEventListener("focus", onFocus);
    if (supportTimer !== null) clearTimeout(supportTimer);
    supportTimer = null;
    window.removeEventListener(WRITES_INTENT_EVENT, changed);
    window.removeEventListener("storage", onStorage);
  };
}

/** Where a write to one pull request goes on this Mac, and as whom. */
export interface MacWriteRoute {
  /** The PR, naming `readAs` where it's read and changed as the page's own login. */
  target: CodeHostPullRequest;
  /** The account the panel shows, which main must still be writing as. */
  actor: { login: string; generation: number };
  /** The page's own login the change is made as; null for the host's account. */
  reader: MacNamedReader | null;
}

/** Who a route writes as: the login and its sign-in, in the host's scope or a named one. */
export type MacWriteIdentity = Pick<MacWriteRoute, "actor" | "reader">;

/**
 * Whether two routes write as the same account: the same scope (the host's, or one named
 * login's), login and sign-in. A host and a named generation can share a number.
 */
export function sameMacRoute(
  left: MacWriteIdentity | null | undefined,
  right: MacWriteIdentity | null | undefined,
): boolean {
  return (
    !!left &&
    !!right &&
    sameNamedReader(left.reader, right.reader) &&
    left.actor.login.toLowerCase() === right.actor.login.toLowerCase() &&
    left.actor.generation === right.actor.generation
  );
}

/**
 * The route `prUrl` is written by now as `madeAs`, the login and scope a change was made as,
 * under that login's sign-in now, which may be newer; null as another login or scope.
 */
export function macRouteAs(
  prUrl: string,
  madeAs: { login: string; reader: MacNamedReader | null },
): MacWriteRoute | null {
  const current = macWriteRoute(prUrl, madeAs.reader);
  return current &&
    sameNamedReader(current.reader, madeAs.reader) &&
    current.actor.login.toLowerCase() === madeAs.login.toLowerCase()
    ? current
    : null;
}

/** The route `prUrl` is written by now, if it's still as `sent`; null once anything moved. */
export function macRouteCurrent(prUrl: string, sent: MacWriteIdentity): MacWriteRoute | null {
  const current = macWriteRoute(prUrl, sent.reader);
  return sameMacRoute(current, sent) ? current : null;
}

/**
 * The route a change rendered for `route` would take at the click, or why not: blocked, or
 * another account than the control showed. `offered` narrows it as `macWriteDispatch` does.
 */
export function macWriteRouteStill(
  prUrl: string,
  route: MacWriteIdentity,
  offered?: (route: MacWriteRoute) => boolean,
): MacWriteRoute | MacWriteError {
  const dispatch = macWriteDispatch(prUrl, route.reader, offered);
  if (dispatch.kind === "blocked") return macWriteBlockedError(dispatch.unavailable);
  if (dispatch.kind === "mac" && sameMacRoute(dispatch.route, route)) return dispatch.route;
  return new MacWriteError("account_changed", MAC_ACTOR_CHANGED);
}

/** A change shown for one Mac account whose actor isn't the one acting now. */
export const MAC_ACTOR_CHANGED =
  "The account on this Mac changed since this was shown. Check it, then try again. Nothing was changed.";

/**
 * Why a PR read on this Mac can't be changed there. The shell's two cases are as far as the
 * page can tell: `shell_too_old` lacks the write bridge, while `shell_writes_unavailable`
 * has it but doesn't offer the set, which a kill switch, a missing gh or a stale flag explain alike.
 */
export type MacWriteUnavailableReason =
  | "provider_unsupported"
  | "release_off"
  | "writes_off"
  | "shell_too_old"
  | "shell_writes_unavailable"
  | "page_unsupported"
  | "account_checking"
  | "no_account"
  | "named_writes_need_update"
  | "named_reads_off"
  | "named_not_read_here";

export interface MacWriteUnavailable {
  reason: MacWriteUnavailableReason;
  target: CodeHostPullRequest;
  /** The page's own login the PR is read as, whose changes nothing else may make. */
  reader: MacNamedReader | null;
}

type MacWriteDecision = { route: MacWriteRoute } | { unavailable: MacWriteUnavailable } | null;

/** Every gate `macWriteRoute` checks, naming the first that fails; null for the host path. */
function decideMacWrite(
  url: string | null | undefined,
  reader: MacNamedReader | null,
): MacWriteDecision {
  start();
  // The page's own login, for a GitHub PR on its host: its changes are made as that login or
  // not at all, whatever the shell says it supports now and wherever the PR is read.
  const intended = namedIntentFor(url, reader);
  if (intended) {
    // The repository's login can't be used now: say why, and nothing is made as anyone else.
    const repo = reader ? null : repoReaderFor(url);
    if (repo?.kind === "blocked") {
      const reasons = {
        pending: "account_checking",
        checking: "account_checking",
        turned_off: "named_reads_off",
        update_needed: "named_writes_need_update",
      } as const satisfies Record<typeof repo.reason, MacWriteUnavailableReason>;
      const { target } = intended;
      return { unavailable: { reason: reasons[repo.reason], target, reader: intended.reader } };
    }
    const read = macReadTarget(url);
    if (!read) {
      const { target } = intended;
      return { unavailable: { reason: "named_not_read_here", target, reader: intended.reader } };
    }
    return decideMacWriteFor({ ...read, readAs: intended.reader.login });
  }
  // (a) Read on this Mac, as GitHub; GitSpoke has no writes on this Mac yet.
  return decideMacWriteFor(macReadTarget(url));
}

/**
 * `reader`, with the PR, where it names the host of the GitHub PR at `url`; null otherwise.
 * Without a view's own login, the repository's chosen one, even while it can't be used: its
 * changes are made as it or not at all.
 */
function namedIntentFor(
  url: string | null | undefined,
  viewReader: MacNamedReader | null,
): { reader: MacNamedReader; target: CodeHostPullRequest } | null {
  const repo = !viewReader && url ? repoReaderFor(url) : null;
  const reader = viewReader ?? (repo && repo.kind !== "none" ? repo.reader : null);
  if (!reader || !url) return null;
  const target = parseCodeHostPullRequestUrl(url);
  if (target?.provider !== "github") return null;
  return target.host.toLowerCase() === reader.host.toLowerCase()
    ? { reader: { host: reader.host.toLowerCase(), login: reader.login }, target }
    : null;
}

function decideMacWriteFor(target: CodeHostPullRequest | null): MacWriteDecision {
  if (!target) return null;
  const named: MacNamedReader | null = target.readAs
    ? { host: target.host.toLowerCase(), login: target.readAs }
    : null;
  const unavailable = (reason: MacWriteUnavailableReason) => ({
    unavailable: { reason, target, reader: named },
  });
  if (target.provider !== "github") return unavailable("provider_unsupported");
  // (c) The release flag and the user's setting allow it. Checked first, since the shell
  // offers nothing while the flag is off.
  if (!(releaseFlagForTests ?? releaseFlag)) return unavailable("release_off");
  if (!macWritesEnabled(readMacWritesIntent(), true)) return unavailable("writes_off");
  // (b) The shell has the bridge and advertises the whole set, and (e) so does this page.
  if (!hasCodeHostWriteBridge()) return unavailable("shell_too_old");
  if (!shellWritesComplete(desktopReadSupport()?.github)) {
    return unavailable("shell_writes_unavailable");
  }
  if (!pageWritesComplete()) return unavailable("page_unsupported");
  // (d) Read as the page's own login: made as that login, with its own generation, or not
  // at all, where the shell makes changes as a named login too.
  if (named) {
    if (!macNamedWritesOffered()) return unavailable("named_writes_need_update");
    if (!namedReadsAvailable()) return unavailable("named_reads_off");
    const writer = namedWriterIdentity(named);
    if (writer === "checking") return unavailable("account_checking");
    if (writer === "refused") return unavailable("no_account");
    return { route: { target, actor: { ...writer }, reader: named } };
  }
  // (d) An account is resolved for the host.
  const identity = macIdentity(target.host);
  if (!identity) {
    return unavailable(macIdentityChecking(target.host) ? "account_checking" : "no_account");
  }
  return {
    route: {
      target,
      actor: { login: identity.login, generation: identity.generation },
      reader: null,
    },
  };
}

/**
 * The Mac route for writes to `url`, as the view reading it as `reader` (the Pull Requests
 * page's own login, or null), or null when there's none. Decided at the click.
 */
export function macWriteRoute(
  url: string | null | undefined,
  reader: MacNamedReader | null,
): MacWriteRoute | null {
  const decision = decideMacWrite(url, reader);
  return decision && "route" in decision ? decision.route : null;
}

/**
 * Whether `route` writes as the login a view reads `route`'s host as, where it reads that host
 * by name; a view reading another host, or none by name, accepts the host's account.
 */
export function macRouteActsAsReadAs(
  route: Pick<MacWriteRoute, "actor" | "target">,
  readAs: MacNamedReader | null,
): boolean {
  if (!readAs || readAs.host.toLowerCase() !== route.target.host.toLowerCase()) return true;
  return readAs.login.toLowerCase() === route.actor.login.toLowerCase();
}

/**
 * Where a change to `url` goes at the click: this Mac, nowhere (`blocked`, for a PR read as
 * the page's own login that can't be changed as it now), or the session's machine. `offered`
 * narrows the Mac route to an operation beyond the whole set.
 */
export type MacWriteDispatch =
  | { kind: "mac"; route: MacWriteRoute }
  | { kind: "blocked"; unavailable: MacWriteUnavailable }
  | { kind: "host" };

export function macWriteDispatch(
  url: string | null | undefined,
  reader: MacNamedReader | null,
  offered?: (route: MacWriteRoute) => boolean,
): MacWriteDispatch {
  const decision = decideMacWrite(url, reader);
  const route = decision && "route" in decision ? decision.route : null;
  if (route && (!offered || offered(route))) return { kind: "mac", route };
  // The page's own login: its change is made as it, on this Mac, or not at all. Nothing
  // below may send it as the host's account or through the session's machine.
  const intended = namedIntentFor(url, reader);
  if (!intended) {
    // Its repository's choice isn't known yet: nothing goes anywhere until it is.
    const target = !reader && url ? parseCodeHostPullRequestUrl(url) : null;
    if (target && repoReaderFor(url).kind === "blocked") {
      return { kind: "blocked", unavailable: { reason: "account_checking", target, reader: null } };
    }
    return { kind: "host" };
  }
  const unavailable: MacWriteUnavailable =
    decision && "unavailable" in decision
      ? decision.unavailable
      : { reason: "shell_writes_unavailable", target: intended.target, reader: intended.reader };
  return { kind: "blocked", unavailable: { ...unavailable, reader: intended.reader } };
}

/**
 * What a blocked change says: why it can't be made as the page's own login now, and that
 * nothing was sent. Main's closed code for it is `blocked`.
 */
export function macWriteBlockedMessage({ reason, target, reader }: MacWriteUnavailable): string {
  if (!reader) {
    return "Checking which account this repository uses on this Mac. Try again in a moment. Nothing was changed.";
  }
  const login = `@${reader.login}`;
  switch (reason) {
    case "named_writes_need_update":
      return `Update the app to make changes as ${login} here. Nothing was changed.`;
    case "account_checking":
      return `Checking ${login}’s sign-in on this Mac. Try again in a moment.`;
    case "no_account":
      return `Sign in to ${target.host} as ${login} on this Mac to make this change. Nothing was changed.`;
    case "writes_off":
      return `Changes as this Mac’s accounts are turned off, so nothing was changed as ${login}.`;
    case "named_reads_off":
      return `Using other GitHub accounts on this Mac is turned off, so nothing was changed as ${login}.`;
    case "named_not_read_here":
      return `Read this pull request on this Mac to make changes as ${login}. Nothing was changed.`;
    default:
      return `This change can’t be made as ${login} from this Mac right now. Nothing was changed.`;
  }
}

/** A change that was never sent because it can't be made as the page's own login now. */
export function macWriteBlockedError(unavailable: MacWriteUnavailable): MacWriteError {
  return new MacWriteError("blocked", macWriteBlockedMessage(unavailable));
}

/** Why `url`, read on this Mac, can't be changed there now; null when it can or isn't read here. */
export function macWriteUnavailable(
  url: string | null | undefined,
  reader: MacNamedReader | null,
): MacWriteUnavailable | null {
  const decision = decideMacWrite(url, reader);
  return decision && "unavailable" in decision ? decision.unavailable : null;
}

/**
 * Whether `url`, read through the session's machine, could be changed on this Mac once read
 * here: every gate but the account, which only a read on this Mac resolves.
 */
export function macCouldWriteOnceReadHere(
  url: string | null | undefined,
  reader: MacNamedReader | null,
): boolean {
  start();
  const candidate = macReadCandidate(url);
  const intended = namedIntentFor(url, reader);
  const decision = decideMacWriteFor(
    candidate && intended ? { ...candidate, readAs: intended.reader.login } : candidate,
  );
  if (!decision) return false;
  if ("route" in decision) return true;
  const { reason } = decision.unavailable;
  return reason === "no_account" || reason === "account_checking";
}

/**
 * Check Again for changes on this Mac: ask the shell what it offers, and re-read who the PR is
 * changed as, the page's own login where it reads as one, else the host's account.
 */
export function recheckMacWrites(
  url: string | null | undefined,
  reader: MacNamedReader | null,
): void {
  shellWritesTurnedOff();
  const intended = namedIntentFor(url, reader);
  if (intended) {
    namedReaderChanged(intended.reader);
    return;
  }
  const target = macReadTarget(url);
  if (target?.provider === "github") macAccountChanged("github", target.host);
}

/**
 * Whether a merge queue change can go to the Mac on `route`: this page implements it, the
 * shell lists it among its writes, and the queue's flag is on. Checked per operation, so a
 * shell offering only one of them offers only that one.
 */
export function macWriteOffers(
  route: MacWriteRoute | null,
  operation: MergeQueueWriteOperation,
): boolean {
  start();
  if (!route) return false;
  if (!pageMergeQueueOperations.includes(operation)) return false;
  const writes = desktopReadSupport()?.github?.writes;
  if (!writes || writes.version !== 1 || !writes.operations.includes(operation)) return false;
  return mergeQueueFlagForTests ?? mergeQueueFlag;
}

/**
 * Whether closing or reopening can go to the Mac on `route`: this page implements it, the
 * shell lists it among its writes, and the lifecycle flag is on now. A shell's list alone
 * never offers it, so a flag turned off since the shell last answered hides it at once.
 */
/**
 * Whether the state store's `operation` can go to the Mac on `route`. Turning auto-merge off is
 * in set v1, so it goes wherever the set does, with or without the lifecycle flag.
 */
export function macLifecycleStoreOffers(
  route: MacWriteRoute | null,
  operation: LifecycleStoreOperation,
): boolean {
  if (operation === "pull_request_auto_merge_disable") return !!route;
  return macLifecycleWriteOffers(route, operation);
}

export function macLifecycleWriteOffers(
  route: MacWriteRoute | null,
  operation: LifecycleWriteOperation,
): boolean {
  start();
  if (!route) return false;
  if (!pageLifecycleOperations.includes(operation)) return false;
  const writes = desktopReadSupport()?.github?.writes;
  if (!writes || writes.version !== 1 || !writes.operations.includes(operation)) return false;
  return lifecycleFlagForTests ?? lifecycleFlag;
}

/**
 * Whether editing or deleting a comment can go to the Mac on `route`: this page implements
 * it, the shell lists it among its writes, and the comment edits flag is on now. A shell's
 * list alone never offers it, so a flag turned off since the shell last answered hides it.
 */
export function macCommentEditOffers(
  route: MacWriteRoute | null,
  operation: CommentEditWriteOperation,
): boolean {
  start();
  if (!route) return false;
  if (!pageCommentEditOperations.includes(operation)) return false;
  const writes = desktopReadSupport()?.github?.writes;
  if (!writes || writes.version !== 1 || !writes.operations.includes(operation)) return false;
  return commentEditsFlagForTests ?? commentEditsFlag;
}

/**
 * Whether resolving or unresolving a conversation can go to the Mac on `route`: this page
 * implements it and the shell lists it among its writes. An older shell offers neither.
 */
export function macThreadResolutionOffers(
  route: MacWriteRoute | null,
  operation: ThreadResolutionWriteOperation,
): boolean {
  start();
  if (!route) return false;
  if (!pageThreadResolutionOperations.includes(operation)) return false;
  const writes = desktopReadSupport()?.github?.writes;
  return !!writes && writes.version === 1 && writes.operations.includes(operation);
}

/**
 * Whether reactions can go to the Mac on `route`: this page implements both writes and the
 * shell lists them with the read that settles one. An older shell offers none, so its page
 * shows reactions it reads without a way to change them.
 */
export function macCommentReactionOffers(route: MacWriteRoute | null): boolean {
  start();
  if (!route) return false;
  const writes = desktopReadSupport()?.github?.writes;
  if (!writes || writes.version !== 1) return false;
  return (
    COMMENT_REACTION_WRITE_OPERATIONS.every(
      (operation) =>
        pageCommentReactionOperations.includes(operation) && writes.operations.includes(operation),
    ) && writes.operations.includes(COMMENT_REACTIONS_READ)
  );
}

/**
 * Whether submitting a review can go to the Mac on `route`: this page implements it, the
 * shell lists it among its writes, and the review flag is on now. A shell's list alone never
 * offers it, so a flag turned off since the shell last answered hides it at once.
 */
export function macReviewWriteOffers(
  route: MacWriteRoute | null,
  operation: ReviewWriteOperation,
): boolean {
  start();
  if (!route) return false;
  if (!pageReviewOperations.includes(operation)) return false;
  const writes = desktopReadSupport()?.github?.writes;
  if (!writes || writes.version !== 1 || !writes.operations.includes(operation)) return false;
  return reviewsFlagForTests ?? reviewsFlag;
}

/**
 * Whether pending reviews go to the Mac on `route`: this page implements every pending change,
 * the shell lists all of them with the pending read and the review submit, and both the review
 * and the pending review flags are on now. A shell offering only some offers none, so the page
 * keeps today's comments and reviews rather than start a review it can't finish or discard.
 */
export function macPendingReviewOffers(route: MacWriteRoute | null): boolean {
  start();
  if (!route) return false;
  if (!PENDING_REVIEW_WRITE_OPERATIONS.every((op) => pagePendingReviewOperations.includes(op))) {
    return false;
  }
  if (!macReviewWriteOffers(route, "pull_request_review_submit")) return false;
  const operations = desktopReadSupport()?.github?.writes?.operations ?? [];
  if (
    ![...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ].every((op) =>
      operations.includes(op),
    )
  ) {
    return false;
  }
  return pendingReviewsFlagForTests ?? pendingReviewsFlag;
}

/** Editing or deleting a comment: a published one, or a draft in the pending review. */
export type CommentChangeOperation =
  CommentEditWriteOperation | "pull_request_review_draft_edit" | "pull_request_review_draft_delete";

const isDraftChange = (operation: CommentChangeOperation) =>
  operation === "pull_request_review_draft_edit" ||
  operation === "pull_request_review_draft_delete";

/**
 * Whether `operation` can go to the Mac on `route` now: a draft's under the pending review
 * offer, a published comment's under the comment edits offer.
 */
export function macCommentChangeOffers(
  route: MacWriteRoute | null,
  operation: CommentChangeOperation,
): boolean {
  return isDraftChange(operation)
    ? macPendingReviewOffers(route)
    : macCommentEditOffers(route, operation as CommentEditWriteOperation);
}

// Merge queue changes main couldn't confirm, by workspace user and PR. Nothing more is
// sent for that PR's queue until a read made after Check again shows where it stands.
// Each marker has its own revision, so a check only clears the marker it began for.
interface UnconfirmedQueueChange {
  operation: MergeQueueWriteOperation;
  revision: number;
  /** The login it was sent as, and the page's own login it was read as; only they can check it. */
  madeAs: { login: string; reader: MacNamedReader | null };
}
const unconfirmedQueueChanges = new Map<string, UnconfirmedQueueChange>();
const unconfirmedQueueListeners = new Set<() => void>();
let unconfirmedQueueVersion = 0;
let unconfirmedQueueRevision = 0;

/** The PR as main tells it apart, so any spelling of its URL shares one hold. */
function unconfirmedQueueKey(owner: string, prUrl: string): string {
  const target = parseCodeHostPullRequestUrl(prUrl);
  return `${owner}\n${target ? pullRequestWriteKey(target) : prUrl.toLowerCase()}`;
}

function unconfirmedQueueChanged(): void {
  unconfirmedQueueVersion += 1;
  for (const listener of unconfirmedQueueListeners) listener();
}

/**
 * `owner`'s `operation` on `prUrl`, sent as `madeAs`, may or may not have happened on GitHub;
 * its revision.
 */
export function markQueueChangeUnconfirmed(
  owner: string,
  prUrl: string,
  operation: MergeQueueWriteOperation,
  madeAs: { login: string; reader: MacNamedReader | null },
): number {
  unconfirmedQueueRevision += 1;
  unconfirmedQueueChanges.set(unconfirmedQueueKey(owner, prUrl), {
    operation,
    revision: unconfirmedQueueRevision,
    madeAs: { login: madeAs.login, reader: madeAs.reader },
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

/** The stored intent, re-read when it changes, for Settings and ⌘K. */
export function useMacWritesIntent(): MacWritesIntent {
  useMacWriteRouteVersion();
  return readMacWritesIntent();
}

/**
 * Whether Automatic makes changes on this Mac now: the rollout's flag is on and the shell
 * offers writes, which it does only from a freshly fetched flag. Re-read as either changes.
 */
export function useMacWritesAutomaticOn(): boolean {
  useMacWriteRouteVersion();
  return (releaseFlagForTests ?? releaseFlag) && shellOffersWrites();
}

/** Re-render when the write route may have changed; returns a version to key memos on. */
export function useMacWriteRouteVersion(): string {
  const readPath = useDesktopReadPathVersion();
  // A named login's proven identity, refusal or new generation moves the route too.
  const named = useNamedReadsVersion();
  const own = useSyncExternalStore(
    (listener) => {
      start();
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
  return `${readPath}:${own}:${named}`;
}

/**
 * Hear every change that can change a write's route: what this Mac reads and as whom, the
 * release flags, the user's setting, and what the shell offers. Outside React.
 */
export function subscribeMacWriteRoute(listener: () => void): () => void {
  start();
  listeners.add(listener);
  const stopReads = subscribeDesktopReadPath(listener);
  const stopNamed = subscribeNamedReads(listener);
  return () => {
    listeners.delete(listener);
    stopReads();
    stopNamed();
  };
}

/** What a write captured at the click, to decide what its answer may touch. */
export interface MacWriteTicket {
  route: MacWriteRoute;
  userId: string | null;
  /** The workspace-user transition count at the click; any later transition retires it. */
  authGeneration: number;
  /** Names the write to the shell, so it can be cancelled. */
  requestId: string;
  /** The workspace user drafts and notices of this write belong to. */
  owner: string;
  scope: MacReadScopeKey;
  /** A place in the scope's read order, taken without counting as a read. */
  read: MacReadTicket;
}

// Moves on every change of workspace user, even back to one seen before, never on a
// re-check that finds the same user. Separate from the Mac read scope's order.
let workspaceAuthGeneration = 0;
let watchedWorkspaceUser: string | null | undefined;
let stopWatchingWorkspaceUser: (() => void) | null = null;
const workspaceAuthListeners = new Set<() => void>();

/** A Mac write in flight, and what to do for its owner if the workspace user changes. */
interface LiveMacWrite {
  ticket: MacWriteTicket;
  operation: MacWriteOperation;
  abandon?: () => void;
}
const liveMacWrites = new Map<string, LiveMacWrite>();
// Every sent write until the shell answers it, whoever is signed in now: main keeps a
// cancelled write's step running, and its one-at-a-time lock, until then.
const nativeMacWrites = new Map<
  string,
  { operation: MacWriteOperation; target: CodeHostPullRequest }
>();
const liveListeners = new Set<() => void>();
// Bumped whenever a write starts or ends, for readers of what's in flight.
let liveVersion = 0;

function liveChanged(): void {
  liveVersion += 1;
  for (const listener of liveListeners) listener();
}

/** A pull request as main tells its writes apart: host, repository and number, any case. */
function pullRequestWriteKey(target: Pick<CodeHostPullRequest, "host" | "repository" | "number">) {
  return [target.host.toLowerCase(), target.repository.toLowerCase(), target.number].join("\n");
}

/**
 * Whether a write of `operation` to this pull request is still in flight on this Mac,
 * from any session in this window. Main makes one at a time per window, operation and
 * pull request, so the next has to wait for it; merge, auto-merge and queue changes count
 * as one operation.
 */
export function macWriteInFlight(
  operation: MacWriteOperation,
  target: Pick<CodeHostPullRequest, "host" | "repository" | "number">,
): boolean {
  // Main holds one change per conversation, not per PR; the resolution store holds each here.
  if (THREAD_RESOLUTION_GROUP.has(operation)) return false;
  // The reaction queue sends a PR's reactions one at a time itself, so none waits here.
  if (COMMENT_REACTION_GROUP.has(operation)) return false;
  const key = pullRequestWriteKey(target);
  const group = writeGroup(operation);
  const same = (write: { operation: MacWriteOperation; target: CodeHostPullRequest }) =>
    writeGroup(write.operation) === group && pullRequestWriteKey(write.target) === key;
  for (const write of nativeMacWrites.values()) if (same(write)) return true;
  if (group === "merge") {
    for (const held of heldStateChanges.values()) {
      if (pullRequestWriteKey(held) === key) return true;
    }
    if (unconfirmedFor(key)) return true;
  }
  // Answered, but its answer is still being applied for the user who sent it.
  for (const write of liveMacWrites.values()) {
    if (same({ operation: write.operation, target: write.ticket.route.target })) return true;
  }
  return false;
}

/** Changes to a PR's state GitHub may or may not have made, by workspace user and PR. */
export type UnconfirmedStateChanges = () => Iterable<{ owner: string; prUrl: string }>;
const unconfirmedStateChangeSources = new Set<UnconfirmedStateChanges>();

/**
 * Hold merging, auto-merge, the queue and state changes for each PR `source` lists, for its
 * workspace user, as if a write were in flight. Call {@link unconfirmedStateChangesChanged}
 * whenever what it lists changes.
 */
export function registerUnconfirmedStateChanges(source: UnconfirmedStateChanges): () => void {
  unconfirmedStateChangeSources.add(source);
  liveChanged();
  return () => {
    if (unconfirmedStateChangeSources.delete(source)) liveChanged();
  };
}

export function unconfirmedStateChangesChanged(): void {
  liveChanged();
}

const PULL_REQUEST_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)\/?$/;

/** A PR's identity from its link alone, whichever route reads it now, or null. */
function pullRequestUrlKey(url: string): string | null {
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
      if (change.owner === owner && pullRequestUrlKey(change.prUrl) === key) return true;
    }
  }
  return false;
}

/**
 * Whether `operation` on the PR at `prUrl` waits for an unconfirmed change to it, on any
 * route: a switch to the session's machine doesn't settle what GitHub may have done.
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
const heldStateChanges = new Map<
  number,
  Pick<CodeHostPullRequest, "host" | "repository" | "number">
>();
let lastStateChangeHold = 0;

/**
 * Hold the PR's state changes as in flight until the returned release runs. The release is
 * called once, right before the held change itself is sent, in the same task.
 */
export function holdPullRequestStateChanges(
  target: Pick<CodeHostPullRequest, "host" | "repository" | "number">,
): () => void {
  lastStateChangeHold += 1;
  const id = lastStateChangeHold;
  heldStateChanges.set(id, target);
  liveChanged();
  return () => {
    if (heldStateChanges.delete(id)) liveChanged();
  };
}

/** Whether a Mac write of `operation` to the PR at `prUrl` is in flight, as a save checks it. */
export function macWriteInFlightFor(
  prUrl: string | null | undefined,
  operation: MacWriteOperation,
): boolean {
  const target = macReadTarget(prUrl);
  return (
    (!!target && macWriteInFlight(operation, target)) ||
    unconfirmedStateChangeHolds(prUrl, operation)
  );
}

/** Changes whenever a Mac write starts or ends, for callers that read {@link macWriteInFlightFor}. */
export function useMacWritesInFlightVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      liveListeners.add(listener);
      return () => liveListeners.delete(listener);
    },
    () => liveVersion,
    () => 0,
  );
}

/** {@link macWriteInFlight} for the PR at `prUrl`, re-read whenever a write starts or ends. */
export function useMacWriteInFlight(
  prUrl: string | null | undefined,
  operation: MacWriteOperation,
): boolean {
  useMacWritesInFlightVersion();
  const target = macReadTarget(prUrl);
  return (
    (!!target && macWriteInFlight(operation, target)) ||
    unconfirmedStateChangeHolds(prUrl, operation)
  );
}

function watchWorkspaceUser(): void {
  if (stopWatchingWorkspaceUser) return;
  watchedWorkspaceUser = getCurrentUserId();
  stopWatchingWorkspaceUser = subscribeIdentity(() => {
    const user = getCurrentUserId();
    if (user === watchedWorkspaceUser) return;
    watchedWorkspaceUser = user;
    workspaceUserChanged();
  });
}

/**
 * Retire every Mac write of the user before: cancel it in the shell, so steps not yet run
 * never run, and let its owner keep what it typed. Runs before anyone reads the new user.
 */
function workspaceUserChanged(): void {
  const retiring = [...liveMacWrites.values()].filter(
    (write) => write.ticket.authGeneration === workspaceAuthGeneration,
  );
  workspaceAuthGeneration += 1;
  for (const write of retiring) {
    liveMacWrites.delete(write.ticket.requestId);
    liveChanged();
    cancelDesktopCodeHostWrite(write.ticket.requestId);
    write.abandon?.();
  }
  for (const listener of workspaceAuthListeners) listener();
}

/** The request this page's `operation` write to `target` was sent as, while it's out; or null. */
export function liveMacWriteRequest(
  operation: MacWriteOperation,
  target: Pick<CodeHostPullRequest, "host" | "repository" | "number">,
): string | null {
  const key = pullRequestWriteKey(target);
  let latest: string | null = null;
  for (const write of liveMacWrites.values()) {
    if (write.operation === operation && pullRequestWriteKey(write.ticket.route.target) === key) {
      latest = write.ticket.requestId;
    }
  }
  return latest;
}

/** Ask the shell to stop the write sent as `requestId`, whatever this Mac offers now. */
export function cancelMacWriteRequest(requestId: string): void {
  cancelDesktopCodeHostWrite(requestId);
}

/** The workspace-user transition count; moves whenever the signed-in user changes. */
export function currentWorkspaceAuthGeneration(): number {
  watchWorkspaceUser();
  return workspaceAuthGeneration;
}

/** Hear every change of workspace user, outside React. */
export function subscribeWorkspaceUser(listener: () => void): () => void {
  watchWorkspaceUser();
  workspaceAuthListeners.add(listener);
  return () => workspaceAuthListeners.delete(listener);
}

/** Re-render when the workspace user changes; returns the user drafts and notices belong to. */
export function useWorkspaceDraftOwner(): string {
  useSyncExternalStore(
    (listener) => {
      watchWorkspaceUser();
      workspaceAuthListeners.add(listener);
      return () => workspaceAuthListeners.delete(listener);
    },
    () => workspaceAuthGeneration,
    () => 0,
  );
  return draftOwner();
}

/** Who drafts belong to now: the workspace user, or a fixed owner before one is known. */
export function draftOwner(): string {
  return getCurrentUserId() ?? "(signed out)";
}

/** Whether `ticket` was taken under the workspace user signed in now. */
export function macWriteTicketCurrent(ticket: MacWriteTicket): boolean {
  return (
    ticket.authGeneration === currentWorkspaceAuthGeneration() &&
    ticket.userId === getCurrentUserId()
  );
}

function newRequestId(): string {
  return randomUUID();
}

/**
 * Authorize a bulk run's changes from its click, as the workspace user signed in now; call it
 * before anything is awaited. End the run once its last change has settled.
 */
export function startPageMacWriteRun(
  entries: readonly {
    route: MacWriteRoute;
    operation: MacWriteOperation;
    params: Record<string, unknown>;
  }[],
): MacWriteRun {
  return startMacWriteRun({
    owner: draftOwner(),
    authGeneration: currentWorkspaceAuthGeneration(),
    writes: desktopReadSupport()?.github?.writes,
    entries,
  });
}

/**
 * Whether `run`'s grant covers sending `operation` on `route` again now, as the workspace user
 * signed in now: without one, a later send would need its own click, so it isn't retried.
 */
export function macWriteRunCovers(
  run: MacWriteRun,
  route: MacWriteRoute,
  operation: MacWriteOperation,
): boolean {
  return (
    run.grantFor({
      owner: draftOwner(),
      authGeneration: currentWorkspaceAuthGeneration(),
      route,
      operation,
    }) !== null
  );
}

export function beginMacWrite(route: MacWriteRoute): MacWriteTicket {
  // A change as the page's own login answers in that login's scope, apart from the host's.
  const scope: MacReadScopeKey = route.reader
    ? namedReadScope(route.reader)
    : macReadScopeKey("github", route.target.host);
  return {
    route,
    userId: scope[1],
    authGeneration: currentWorkspaceAuthGeneration(),
    requestId: newRequestId(),
    owner: draftOwner(),
    scope,
    read: beginMacRead(scope),
  };
}

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/**
 * Whether a finished write's answer, written as `writer`, may update cached reads:
 * the same workspace user, the PR still read on this Mac, no change of reader since
 * the click, the reader shown still the actor or none yet, and the writer the actor.
 * Otherwise the caller only reports the outcome and reads the PR again.
 */
export function macWriteMayPatch(ticket: MacWriteTicket, writer: string): boolean {
  const { actor, target } = ticket.route;
  if (ticket.userId !== getCurrentUserId()) return false;
  if (macReadTarget(target.url)?.provider !== "github") return false;
  if (!macReadCurrent(ticket.read)) return false;
  const reader = acceptedMacReader(ticket.scope);
  if (reader !== null && !sameLogin(reader, actor.login)) return false;
  return sameLogin(writer, actor.login);
}

/**
 * Apply a sent Mac write's answer, independently of React Query's online and mutation
 * queues, which would hold an answer the shell already gave. `applied` gets a success and
 * `failed` a refusal, each only while the workspace user is still the click's; `abandon`
 * stays registered until both are done, so a change of user meanwhile still retires it.
 */
export function settleMacWrite<Data>(
  ticket: MacWriteTicket,
  sent: Promise<{ data: Data; writer: string } | MacWriteSuppressed>,
  handlers: {
    applied: (answer: { data: Data; writer: string }) => Promise<void> | void;
    failed: (error: Error) => void;
  },
): Promise<void> {
  return (async () => {
    try {
      const answer = await sent;
      // The user may have changed in the task boundary since the answer was checked.
      if (answer === MAC_WRITE_SUPPRESSED || !macWriteTicketCurrent(ticket)) return;
      await handlers.applied(answer);
    } catch (error) {
      if (macWriteTicketCurrent(ticket)) {
        handlers.failed(error instanceof Error ? error : new Error(String(error)));
      }
    } finally {
      finishMacWrite(ticket);
    }
  })();
}

/**
 * The write's answer was applied or dropped: a later change of user has nothing of it
 * to retire. For a caller that observes a sent write itself, e.g. as a mutation.
 */
export function finishMacWrite(ticket: MacWriteTicket): void {
  if (liveMacWrites.delete(ticket.requestId)) liveChanged();
}

/** A Mac change without a draft, retired by a change of workspace user. */
export interface AbandonedMacWrite {
  owner: string;
  prUrl: string;
  operation: MacWriteOperation;
}

// Per owner, so only the user who made the change ever sees it.
let abandonedMacWrites: readonly AbandonedMacWrite[] = [];
const abandonedListeners = new Set<() => void>();

function abandonedChanged(): void {
  for (const listener of abandonedListeners) listener();
}

/** Remember, for its owner, a change that may have been made before the user changed. */
export function recordAbandonedMacWrite(write: AbandonedMacWrite): void {
  abandonedMacWrites = [
    ...abandonedMacWrites.filter(
      (entry) =>
        !(
          entry.owner === write.owner &&
          entry.prUrl === write.prUrl &&
          entry.operation === write.operation
        ),
    ),
    write,
  ];
  abandonedChanged();
}

/** Forget a notice its owner dismissed, or one a new change of the same kind replaces. */
export function dismissAbandonedMacWrite(owner: string, prUrl: string, operation: string): void {
  const next = abandonedMacWrites.filter(
    (entry) => !(entry.owner === owner && entry.prUrl === prUrl && entry.operation === operation),
  );
  if (next.length === abandonedMacWrites.length) return;
  abandonedMacWrites = next;
  abandonedChanged();
}

/** Whether the workspace user now signed in left `operation` on `prUrl` unconfirmed. */
export function useAbandonedMacWrite(prUrl: string, operation: MacWriteOperation): boolean {
  const owner = useWorkspaceDraftOwner();
  const entries = useSyncExternalStore(
    (listener) => {
      abandonedListeners.add(listener);
      return () => abandonedListeners.delete(listener);
    },
    () => abandonedMacWrites,
    () => abandonedMacWrites,
  );
  return entries.some(
    (entry) => entry.owner === owner && entry.prUrl === prUrl && entry.operation === operation,
  );
}

/** What a failed several-step or pending review write reported beyond its code. */
export interface MacWriteErrorDetails {
  /** Why a `partial` run stopped. */
  cause?: string;
  /** A batch refused before its sheet: each of its pull requests, and why. */
  results?: unknown;
  steps?: CodeHostWriteSteps;
  draftBaseline?: PendingDraftBaseline;
  reviewStart?: PendingReviewStart;
}

/** A write on this Mac that didn't finish; `outcomeUnknown` means GitHub may have made it. */
export class MacWriteError extends Error {
  readonly code: string;
  /** For a title or description that changed on GitHub: what it is there now. */
  readonly current?: { field: "title" | "body"; value: string };
  /** For a review GitHub didn't confirm: the viewer's reviews that were there before it. */
  readonly knownReviewIds?: readonly string[];
  readonly details: MacWriteErrorDetails;
  constructor(
    code: string,
    message: string,
    current?: { field: "title" | "body"; value: string },
    knownReviewIds?: readonly string[],
    details: MacWriteErrorDetails = {},
  ) {
    super(message);
    this.name = "MacWriteError";
    this.code = code;
    this.current = current;
    this.knownReviewIds = knownReviewIds;
    this.details = details;
  }
  get outcomeUnknown(): boolean {
    return this.code === "outcome_unknown";
  }
}

/** What a Mac write resolves to when its workspace user changed: nothing to show or keep. */
export const MAC_WRITE_SUPPRESSED = Symbol("mac write suppressed");
export type MacWriteSuppressed = typeof MAC_WRITE_SUPPRESSED;

/**
 * Send one write on this Mac as the ticket's actor, before this returns, and resolve its
 * answer and who wrote it. Call it from the click itself, with nothing awaited first: no
 * queue may stand between the click and the shell. `parse` checks the operation's answer;
 * one it refuses means GitHub may have made the change.
 *
 * Resolves `MAC_WRITE_SUPPRESSED`, without sending, for a ticket the workspace user has
 * already left, and once sent, when the user changes before the answer arrives: its
 * caller then changes nothing. `abandon` runs at that change instead, for the old user,
 * until the caller has applied or dropped the answer: settle it with `settleMacWrite`.
 */
export function sendMacWrite<Data>(
  ticket: MacWriteTicket,
  operation: MacWriteOperation,
  params: Record<string, unknown>,
  parse: (data: unknown) => Data | null,
  abandon?: () => void,
): Promise<{ data: Data; writer: string } | MacWriteSuppressed> {
  if (!macWriteTicketCurrent(ticket)) return Promise.resolve(MAC_WRITE_SUPPRESSED);
  const { target, actor, reader } = ticket.route;
  liveMacWrites.set(ticket.requestId, { ticket, operation, abandon });
  nativeMacWrites.set(ticket.requestId, { operation, target });
  liveChanged();
  // A change a bulk run's click authorized goes on that run's grant, sent after the click.
  const runGrant = macWriteRunGrant({
    owner: ticket.owner,
    authGeneration: ticket.authGeneration,
    route: ticket.route,
    operation,
  });
  // The actor goes as main parses it, `{ login, generation }` and nothing more.
  const request = {
    requestId: ticket.requestId,
    provider: "github" as const,
    host: target.host,
    actor: { login: actor.login, generation: actor.generation },
    operation,
    params,
    ...(runGrant ? { runGrant } : {}),
  };
  const sent = reader
    ? desktopCodeHostWriteAs<Data>({ ...request, macLogin: reader.login })
    : desktopCodeHostWrite<Data>(request);
  return sent.then((result) => {
    nativeMacWrites.delete(ticket.requestId);
    liveChanged();
    if (!macWriteTicketCurrent(ticket)) return MAC_WRITE_SUPPRESSED;
    // Whatever the outcome, a view reading the PR as the page's own login reads it again.
    namedPullRequestWritten(target.url, ticket.userId);
    if (!result.ok) {
      if (result.error.code === "writes_disabled") shellWritesTurnedOff();
      // The named login itself changed or signed out: what was read as it is read again.
      if (reader && result.error.code === "account_changed") namedReaderChanged(reader);
      else if (reader && NAMED_REFUSED_CODES.has(result.error.code)) namedReaderRefused(reader);
      const field = result.current?.field;
      const current =
        result.current && (field === "title" || field === "body")
          ? { field: field as "title" | "body", value: result.current.value }
          : undefined;
      throw new MacWriteError(
        result.error.code,
        result.error.message,
        current,
        result.knownReviewIds,
        {
          ...(result.error.cause ? { cause: result.error.cause } : {}),
          ...(result.steps ? { steps: result.steps } : {}),
          ...(result.draftBaseline ? { draftBaseline: result.draftBaseline } : {}),
          ...(result.reviewStart ? { reviewStart: result.reviewStart } : {}),
          ...(result.results ? { results: result.results } : {}),
        },
      );
    }
    const data = parse(result.data);
    if (data === null) throw new MacWriteError("outcome_unknown", CODE_HOST_WRITE_OUTCOME_UNKNOWN);
    return { data, writer: result.actor.login };
  });
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTextOrNull = (value: unknown) => value === null || isText(value);
const isLineOrNull = (value: unknown) =>
  value === null || (Number.isInteger(value) && (value as number) > 0);
const optional = (value: unknown, check: (value: unknown) => boolean) =>
  value === undefined || check(value);
const isSide = (value: unknown) => value === "LEFT" || value === "RIGHT";

/** A conversation comment as the panel shows it: `{ author, body, created_at, url }`. */
function isPostedComment(value: unknown): boolean {
  return (
    isObject(value) &&
    isTextOrNull(value.author) &&
    optional(value.author_id, isTextOrNull) &&
    isText(value.body) &&
    isTextOrNull(value.created_at) &&
    // A posted comment always has its link; the panel merges by it.
    isText(value.url)
  );
}

/** A review comment as the panel shows it, with the id a reply is tracked by. */
function isReviewComment(value: unknown): boolean {
  return (
    isObject(value) &&
    isText(value.id) &&
    value.id.length > 0 &&
    isTextOrNull(value.author) &&
    isText(value.body) &&
    isTextOrNull(value.created_at) &&
    isTextOrNull(value.url) &&
    optional(value.minimized, (minimized) => typeof minimized === "boolean")
  );
}

/** A review conversation with every field the panel reads, and every comment in it. */
function isReviewThread(value: unknown): boolean {
  if (!isObject(value) || !isText(value.id) || value.id.length === 0) return false;
  const comments = value.comments;
  return (
    isText(value.path) &&
    isSide(value.side) &&
    isLineOrNull(value.line) &&
    isLineOrNull(value.start_line) &&
    optional(value.start_side, (side) => side === null || isSide(side)) &&
    optional(value.original_line, isLineOrNull) &&
    optional(value.original_start_line, isLineOrNull) &&
    (value.subject_type === "LINE" || value.subject_type === "FILE") &&
    typeof value.resolved === "boolean" &&
    typeof value.outdated === "boolean" &&
    typeof value.can_reply === "boolean" &&
    isText(value.diff_hunk) &&
    value.pending_comment_ids === undefined &&
    isObject(comments) &&
    Array.isArray(comments.data) &&
    comments.data.length > 0 &&
    comments.data.every(isReviewComment) &&
    typeof comments.has_more === "boolean" &&
    isTextOrNull(comments.next_cursor) &&
    optional(comments.warning, isText)
  );
}

/** A comment answer, `{ comment }` shaped as the panel shows it, or null. */
export function parseCommentAnswer<Comment>(data: unknown): { comment: Comment } | null {
  return isObject(data) && isPostedComment(data.comment)
    ? { comment: data.comment as Comment }
    : null;
}

/** A reply answer, `{ comment }` shaped as a review comment with its id, or null. */
export function parseReplyAnswer<Comment>(data: unknown): { comment: Comment } | null {
  return isObject(data) && isReviewComment(data.comment)
    ? { comment: data.comment as Comment }
    : null;
}

/** A new conversation's answer, `{ thread }`; null `thread` means main couldn't read it back. */
export function parseThreadAnswer<Thread>(data: unknown): { thread: Thread | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "thread")) return null;
  if (data.thread === null) return { thread: null };
  return isReviewThread(data.thread) ? { thread: data.thread as Thread } : null;
}

const isLabel = (value: unknown) =>
  isObject(value) &&
  isText(value.name) &&
  value.name.length > 0 &&
  isTextOrNull(value.color) &&
  isTextOrNull(value.description);

/** A label change's answer: the labels now, or null when main couldn't read them back. */
export function parseLabelsAnswer<Label>(data: unknown): { labels: Label[] | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "labels")) return null;
  if (data.labels === null) return { labels: null };
  return Array.isArray(data.labels) && data.labels.every(isLabel)
    ? { labels: data.labels as Label[] }
    : null;
}

const REVIEWER_STATES = new Set([
  "PENDING",
  "APPROVED",
  "COMMENTED",
  "CHANGES_REQUESTED",
  "DISMISSED",
]);
const isReviewer = (value: unknown) =>
  isObject(value) &&
  isText(value.login) &&
  value.login.length > 0 &&
  (value.kind === "user" || value.kind === "team") &&
  isText(value.state) &&
  REVIEWER_STATES.has(value.state);

/** A reviewer change's answer: the reviewers now, or null when main couldn't read them back. */
export function parseReviewersAnswer<Reviewer>(
  data: unknown,
): { reviewers: Reviewer[] | null } | null {
  if (!isObject(data) || !Object.hasOwn(data, "reviewers")) return null;
  if (data.reviewers === null) return { reviewers: null };
  return Array.isArray(data.reviewers) && data.reviewers.every(isReviewer)
    ? { reviewers: data.reviewers as Reviewer[] }
    : null;
}

/** A merge or auto-merge change's answer: the PR it was made to. */
export function parseMergeAnswer(data: unknown): { pr_url: string } | null {
  return isObject(data) && data.object === "session.github.pr_merge" && isText(data.pr_url)
    ? { pr_url: data.pr_url }
    : null;
}

/** A merge queue change's answer: whether the PR is in the queue now, as main made it. */
export function parseMergeQueueAnswer(data: unknown): { pr_url: string; queued: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_merge_queue" &&
    isText(data.pr_url) &&
    typeof data.queued === "boolean"
    ? { pr_url: data.pr_url, queued: data.queued }
    : null;
}

/** The state a close or reopen left the pull request in, as GitHub answered it. */
export type PullRequestState = "OPEN" | "CLOSED" | "MERGED";

/** A close or reopen's answer: the PR and the state GitHub reported, never the one asked for. */
export function parsePullRequestStateAnswer(
  data: unknown,
): { pr_url: string; state: PullRequestState } | null {
  return isObject(data) &&
    data.object === "session.github.pr_state" &&
    isText(data.pr_url) &&
    (data.state === "OPEN" || data.state === "CLOSED" || data.state === "MERGED")
    ? { pr_url: data.pr_url, state: data.state }
    : null;
}

/** A draft or ready change's answer: whether GitHub has the PR as a draft now. */
export function parsePullRequestDraftAnswer(
  data: unknown,
): { pr_url: string; is_draft: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_draft" &&
    isText(data.pr_url) &&
    typeof data.is_draft === "boolean"
    ? { pr_url: data.pr_url, is_draft: data.is_draft }
    : null;
}

/** Turning on auto-merge's answer: when GitHub says it was turned on. */
export function parseAutoMergeEnableAnswer(
  data: unknown,
): { pr_url: string; enabled_at: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_auto_merge" &&
    isText(data.pr_url) &&
    isText(data.enabled_at)
    ? { pr_url: data.pr_url, enabled_at: data.enabled_at }
    : null;
}

/** How one pull request of a batch ended, as main's ledger says. */
export const AUTO_MERGE_BATCH_OUTCOMES = [
  "enabled",
  "refused",
  "unknown",
  "not_attempted",
  "excluded",
] as const;
export type AutoMergeBatchOutcome = (typeof AUTO_MERGE_BATCH_OUTCOMES)[number];

/** One pull request's result in a batch that turned auto-merge on. */
export interface AutoMergeBatchResult {
  /** Its host, repository and number: its identity in the batch. */
  host: string;
  repository: string;
  number: number;
  outcome: AutoMergeBatchOutcome;
  /** Why it was refused or left out. */
  code: string | null;
  /** Why it wasn't attempted: what stopped the batch. */
  cause: string | null;
  /** main's own words for a refusal. */
  message: string | null;
}

/** Each result a batch's answer lists, or null where any is malformed. */
export function parseAutoMergeBatchResults(value: unknown): AutoMergeBatchResult[] | null {
  if (!Array.isArray(value)) return null;
  const results: AutoMergeBatchResult[] = [];
  for (const entry of value) {
    if (
      !isObject(entry) ||
      !isText(entry.host) ||
      !isText(entry.repository) ||
      typeof entry.number !== "number" ||
      !AUTO_MERGE_BATCH_OUTCOMES.includes(entry.outcome as AutoMergeBatchOutcome)
    ) {
      return null;
    }
    results.push({
      host: entry.host,
      repository: entry.repository,
      number: entry.number,
      outcome: entry.outcome as AutoMergeBatchOutcome,
      code: isText(entry.code) ? entry.code : null,
      cause: isText(entry.cause) ? entry.cause : null,
      message: isText(entry.message) ? entry.message : null,
    });
  }
  return results;
}

/** Turning on auto-merge for several pull requests: each one's result. */
export function parseAutoMergeManyAnswer(
  data: unknown,
): { results: AutoMergeBatchResult[] } | null {
  if (!isObject(data) || data.object !== "session.github.pr_auto_merge_many") return null;
  const results = parseAutoMergeBatchResults(data.results);
  return results ? { results } : null;
}

/** A batch member's identity: its host, repository and number, ignoring case. */
export function autoMergeBatchKey(
  member: Pick<AutoMergeBatchResult, "host" | "repository" | "number">,
): string {
  return [member.host.toLowerCase(), member.repository.toLowerCase(), member.number].join("\n");
}

/** A comment edit's answer: the comment's text as GitHub saved it. */
export function parseCommentEditAnswer(data: unknown): { comment_id: string; body: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_comment_edit" &&
    isText(data.comment_id) &&
    isText(data.body)
    ? { comment_id: data.comment_id, body: data.body }
    : null;
}

/** A comment delete's answer, naming the comment GitHub deleted. */
export function parseCommentDeleteAnswer(data: unknown): { comment_id: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_comment_delete" &&
    isText(data.comment_id)
    ? { comment_id: data.comment_id }
    : null;
}

/** A resolution's answer: the conversation GitHub changed, and whether it's now resolved. */
export function parseThreadResolutionAnswer(
  data: unknown,
): { thread_id: string; resolved: boolean } | null {
  return isObject(data) &&
    data.object === "session.github.pr_review_thread_resolution" &&
    isText(data.thread_id) &&
    typeof data.resolved === "boolean"
    ? { thread_id: data.thread_id, resolved: data.resolved }
    : null;
}

/** What Check Again read of one comment: there (with its text), gone, or not knowable. */
/** A draft added to a pending review: a new conversation, or a reply in one. */
export type DraftAddAnswer<Thread, Comment> =
  | { review_id: string; thread: Thread }
  | { review_id: string; thread_id: string; comment: Comment };

/** A draft add's answer: the review it's in, and the thread or reply GitHub made, or null. */
export function parseDraftAddAnswer<Thread, Comment>(
  data: unknown,
): DraftAddAnswer<Thread, Comment> | null {
  if (!isObject(data) || data.object !== "session.github.pr_review_draft") return null;
  if (!isText(data.review_id) || data.review_id.length === 0) return null;
  if (data.thread !== undefined) {
    return isReviewThread(data.thread)
      ? { review_id: data.review_id, thread: data.thread as Thread }
      : null;
  }
  return isText(data.thread_id) && isReviewComment(data.comment)
    ? { review_id: data.review_id, thread_id: data.thread_id, comment: data.comment as Comment }
    : null;
}

/** A discard's answer: the pending review GitHub deleted, or null. */
export function parseReviewDiscardAnswer(data: unknown): { review_id: string } | null {
  return isObject(data) &&
    data.object === "session.github.pr_review_discard" &&
    isText(data.review_id) &&
    data.review_id.length > 0
    ? { review_id: data.review_id }
    : null;
}

export type CommentState =
  { state: "present"; body: string } | { state: "absent" } | { state: "unreadable" };

export function parseCommentStateAnswer(data: unknown, commentId: string): CommentState | null {
  if (!isObject(data) || data.object !== "session.github.pr_comment_state") return null;
  if (data.comment_id !== commentId) return null;
  if (data.state === "present")
    return isText(data.body) ? { state: "present", body: data.body } : null;
  if (data.state === "absent" || data.state === "unreadable") return { state: data.state };
  return null;
}

/** A submitted review, as GitHub answered it: never the state asked for. */
export interface SubmittedReview {
  id: string;
  state: "COMMENTED" | "APPROVED" | "CHANGES_REQUESTED";
  body: string;
  created_at: string | null;
  url: string | null;
  commit_oid: string;
}

const SUBMITTED_STATES = new Set(["COMMENTED", "APPROVED", "CHANGES_REQUESTED"]);

/** A review submit's answer: the PR, and the review GitHub made. */
export function parseReviewSubmitAnswer(
  data: unknown,
): { pr_url: string; review: SubmittedReview } | null {
  if (!isObject(data) || data.object !== "session.github.pr_review" || !isText(data.pr_url)) {
    return null;
  }
  const review = data.review;
  return isObject(review) &&
    isText(review.id) &&
    SUBMITTED_STATES.has(review.state as string) &&
    isText(review.body) &&
    isTextOrNull(review.created_at) &&
    isTextOrNull(review.url) &&
    isText(review.commit_oid)
    ? { pr_url: data.pr_url, review: review as unknown as SubmittedReview }
    : null;
}

/** One of the viewer's submitted reviews, as the review check reads it. */
export interface CheckedReview {
  id: string;
  state: string;
  body: string;
  created_at: string | null;
  commit_oid: string | null;
}

/** What the review check read: the head, whether a review is pending, the viewer's reviews. */
export interface ReviewCheck {
  head_sha: string;
  author: string | null;
  pending: boolean;
  reviews: CheckedReview[];
}

export function parseReviewCheckAnswer(data: unknown): ReviewCheck | null {
  if (!isObject(data) || data.object !== "session.github.pr_review_check") return null;
  if (!isText(data.head_sha) || !isTextOrNull(data.author) || typeof data.pending !== "boolean") {
    return null;
  }
  if (!Array.isArray(data.reviews)) return null;
  const reviews = data.reviews.filter(
    (review): review is CheckedReview =>
      isObject(review) &&
      isText(review.id) &&
      isText(review.state) &&
      isText(review.body) &&
      isTextOrNull(review.created_at) &&
      isTextOrNull(review.commit_oid),
  );
  if (reviews.length !== data.reviews.length) return null;
  return { head_sha: data.head_sha, author: data.author, pending: data.pending, reviews };
}

/** A title or description change's answer: both as GitHub has them now. */
export function parseDetailsAnswer(data: unknown): { title: string; body: string } | null {
  return isObject(data) && isText(data.title) && isText(data.body)
    ? { title: data.title, body: data.body }
    : null;
}

/**
 * Whether `queryKey` is one of `ticket`'s PR's queries cached under the ticket's Mac scope: the
 * session's own, or the PR's shared record of the account the write acts as, in the epoch it's
 * in now, while no move of the scope came after the click.
 */
export function cachedUnderTicket(
  queryKey: readonly unknown[],
  ticket: MacWriteTicket,
  root: string,
  conversationId: string,
): boolean {
  if (queryKey[0] !== root || queryKey[2] !== ticket.route.target.url) return false;
  if (queryKey[1] === "pr") return writesRecord(queryKey, ticket);
  if (queryKey[1] !== conversationId) return false;
  const tail = queryKey.slice(queryKey.length - ticket.scope.length);
  return (
    queryKey.length >= 3 + ticket.scope.length &&
    tail.every((part, index) => part === ticket.scope[index])
  );
}

/** Whether `ticket`'s write may patch the shared record `queryKey`; see {@link cachedUnderTicket}. */
function writesRecord(queryKey: readonly unknown[], ticket: MacWriteTicket): boolean {
  const record = pullRequestRecordReaderIn(queryKey, ticket.scope);
  return (
    !!record?.reader &&
    record.reader.toLowerCase() === ticket.route.actor.login.toLowerCase() &&
    record.epoch === macReadEpoch(ticket.scope) &&
    macReadCurrent(ticket.read)
  );
}

/** What the user is told when a Mac write finished but the panel can't show its answer. */
export function macWriteDoneMessage(
  operation: MacWriteOperation,
  ticket: MacWriteTicket,
  writer: string,
): string {
  const where = pullRequestToastName(ticket.route.target);
  if (operation === "pull_request_review_reply") return `Replied on ${where} as @${writer}.`;
  if (operation === "pull_request_merge") return `Merged ${where} as @${writer}.`;
  if (operation === "pull_request_merge_queue_add") {
    return `Added ${where} to the merge queue as @${writer}.`;
  }
  if (operation === "pull_request_merge_queue_remove") {
    return `Removed ${where} from the merge queue as @${writer}.`;
  }
  if (operation === "pull_request_auto_merge_disable") {
    return `Turned off auto-merge on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_details_update") {
    return `Saved changes to ${where} as @${writer}.`;
  }
  if (operation === "pull_request_labels_update") {
    return `Changed the labels on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_reviewers_update") {
    return `Changed the reviewers on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_review_thread_create") {
    return `Started a conversation on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_close") return `Closed ${where} as @${writer}.`;
  if (operation === "pull_request_reopen") return `Reopened ${where} as @${writer}.`;
  if (operation === "pull_request_convert_to_draft") {
    return `Converted ${where} to a draft as @${writer}.`;
  }
  if (operation === "pull_request_ready_for_review") {
    return `Marked ${where} ready for review as @${writer}.`;
  }
  if (operation === "pull_request_auto_merge_enable") {
    return `Turned on auto-merge for ${where} as @${writer}.`;
  }
  if (operation === "pull_request_comment_edit") {
    return `Edited a comment on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_comment_delete") {
    return `Deleted a comment on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_review_thread_resolve") {
    return `Resolved a conversation on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_review_thread_unresolve") {
    return `Unresolved a conversation on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_review_submit") {
    return `Submitted your review of ${where} as @${writer}.`;
  }
  if (operation === "pull_request_comment_reaction_add") {
    return `Reacted to a comment on ${where} as @${writer}.`;
  }
  if (operation === "pull_request_comment_reaction_remove") {
    return `Removed your reaction on ${where} as @${writer}.`;
  }
  return `Commented on ${where} as @${writer}.`;
}

/** Forget the store's state, so a test starts clean. */
export function resetDesktopPullRequestWritesForTests(): void {
  if (supportTimer !== null) clearTimeout(supportTimer);
  supportTimer = null;
  supportRecheck = SUPPORT_RECHECK;
  supportRecheckDelay = supportRecheck.first;
  abandonedMacWrites = [];
  abandonedListeners.clear();
  stopWatchingWorkspaceUser?.();
  stopWatchingWorkspaceUser = null;
  watchedWorkspaceUser = undefined;
  workspaceAuthGeneration = 0;
  liveMacWrites.clear();
  nativeMacWrites.clear();
  heldStateChanges.clear();
  liveListeners.clear();
  liveVersion = 0;
  workspaceAuthListeners.clear();
  pageWriteOperations = PAGE_WRITE_OPERATIONS;
  releaseFlagForTests = null;
  releaseFlag = false;
  mergeQueueFlagForTests = null;
  mergeQueueFlag = false;
  lifecycleFlagForTests = null;
  lifecycleFlag = false;
  pageLifecycleOperations = LIFECYCLE_WRITE_OPERATIONS;
  commentEditsFlagForTests = null;
  commentEditsFlag = false;
  pageCommentEditOperations = COMMENT_EDIT_WRITE_OPERATIONS;
  pageThreadResolutionOperations = THREAD_RESOLUTION_WRITE_OPERATIONS;
  pageCommentReactionOperations = COMMENT_REACTION_WRITE_OPERATIONS;
  reviewsFlagForTests = null;
  reviewsFlag = false;
  pageReviewOperations = REVIEW_WRITE_OPERATIONS;
  pendingReviewsFlagForTests = null;
  pendingReviewsFlag = false;
  pagePendingReviewOperations = PENDING_REVIEW_WRITE_OPERATIONS;
  unconfirmedQueueChanges.clear();
  unconfirmedQueueListeners.clear();
  unconfirmedQueueVersion = 0;
  unconfirmedQueueRevision = 0;
  pageMergeQueueOperations = MERGE_QUEUE_WRITE_OPERATIONS;
  stopWatching?.();
  stopWatching = null;
  started = false;
  version = 0;
  listeners.clear();
}

/** Shorter waits between asking the shell again, for tests; null restores the real ones. */
export function setShellWritesRecheckForTests(
  delays: { first: number; max: number; offered: number } | null,
): void {
  supportRecheck = delays ?? SUPPORT_RECHECK;
  supportRecheckDelay = supportRecheck.first;
}

/** Turn the lifecycle flag on or off as the shell would, for tests; null follows the shell. */
export function setLifecycleFlagForTests(value: boolean | null): void {
  lifecycleFlagForTests = value;
  changed();
}

/** Pretend this page implements only `operations` of closing and reopening; null resets. */
export function setPageLifecycleOperationsForTests(operations: readonly string[] | null): void {
  pageLifecycleOperations = operations ?? LIFECYCLE_WRITE_OPERATIONS;
  changed();
}

/** Turn the comment edits flag on or off as the shell would, for tests; null follows the shell. */
export function setCommentEditsFlagForTests(value: boolean | null): void {
  commentEditsFlagForTests = value;
  changed();
}

/** Pretend this page implements only `operations` of the comment edits; null resets. */
export function setPageCommentEditOperationsForTests(operations: readonly string[] | null): void {
  pageCommentEditOperations = operations ?? COMMENT_EDIT_WRITE_OPERATIONS;
  changed();
}

/** Turn the review flag on or off as the shell would, for tests; null follows the shell. */
export function setReviewsFlagForTests(value: boolean | null): void {
  reviewsFlagForTests = value;
  changed();
}

/** Pretend this page implements only `operations` of the review writes; null resets. */
export function setPageReviewOperationsForTests(operations: readonly string[] | null): void {
  pageReviewOperations = operations ?? REVIEW_WRITE_OPERATIONS;
  changed();
}

/** Turn the pending reviews flag on or off as the shell would, for tests; null follows the shell. */
export function setPendingReviewsFlagForTests(value: boolean | null): void {
  pendingReviewsFlagForTests = value;
  changed();
}

/** Pretend this page implements only `operations` of the pending review writes; null resets. */
export function setPagePendingReviewOperationsForTests(operations: readonly string[] | null): void {
  pagePendingReviewOperations = operations ?? PENDING_REVIEW_WRITE_OPERATIONS;
  changed();
}

/** Turn the merge queue's flag on or off as the shell would, for tests; null follows the shell. */
export function setMergeQueueFlagForTests(value: boolean | null): void {
  mergeQueueFlagForTests = value;
  changed();
}

/** Pretend this page implements only `operations` of the queue changes; null resets. */
export function setPageMergeQueueOperationsForTests(operations: readonly string[] | null): void {
  pageMergeQueueOperations = operations ?? MERGE_QUEUE_WRITE_OPERATIONS;
  changed();
}

/** Turn the release flag on or off as the shell would, for tests; null follows the shell again. */
export function setMacWritesReleaseFlagForTests(value: boolean | null): void {
  releaseFlagForTests = value;
  supportRecheckDelay = supportRecheck.first;
  scheduleShellWritesRecheck();
  changed();
}
