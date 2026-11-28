// The comment box's Close / Reopen Pull Request: what it offers here (the change through the
// session's host, a link to GitHub, or nothing), and running it, with the box's comment posted first.

import { type BulkRetry, landedEarlier, withBulkRetries } from "@/lib/macWriteRetries";
import {
  type PullRequestActionOrigin,
  usePullRequestActionOrigin,
} from "@/lib/pullRequestActionOrigin";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  type PullRequestStateChangeOutcome,
  usePullRequestLifecycleChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { LifecycleStoreOperation, StateChangeOperation } from "@/lib/pullRequestStateHolds";
import { currentWorkspaceAuthGeneration, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";

import {
  changeStateWithComment,
  type StateChainOutcome,
  setStateChainState,
  type StateChainNotice,
  stateChainKey,
  stateChainState,
  subscribeStateChains,
  useStateChainState,
} from "./pullRequestStateChain";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";

import { keepUnconfirmedReport, newChangeAttempt, settleUnconfirmedReport } from "./changeAttempts";
import { hostLifecycleRoute, hostLifecycleRouteCurrent } from "./hostLifecycleRoute";
import type { HostChangeRoute } from "./pullRequestPageRoute";
import { readFreshPullRequest, type FreshReadResult } from "./freshPullRequestRead";
import {
  lifecycleActionsHold,
  subscribeLifecycleActions,
  useLifecycleActionState,
} from "./pullRequestLifecycleActions";
import {
  STATE_CHANGES,
  stateChangeHeldReason,
  stateChangeOperationFor,
} from "./pullRequestStateChanges";

/** What the box's state slot shows. */
export type PullRequestStateOffer =
  | { kind: "none" }
  /** The page can't make the change here: say where it can be made. */
  | { kind: "link"; label: string; url: string }
  | { kind: "action"; operation: StateChangeOperation; label: string };

/** Everything a click captured that must still hold when its state change is sent. */
export interface StateChangeCapture<Operation extends LifecycleStoreOperation> {
  prUrl: string;
  owner: string;
  authGeneration: number;
  actor: { login: string; generation: number };
  operation: Operation;
  /** The session's host the change is sent to. */
  host: HostChangeRoute;
}

/** Who a change through the session's host is made as: its own account, not named here. */
export const HOST_ACTOR = { login: "", generation: 0 } as const;

type StateChainCapture = StateChangeCapture<StateChangeOperation>;

/**
 * What a click on `operation` binds to now: `conversationId`'s host where it makes the change;
 * null where it doesn't.
 */
export function captureStateChange<Operation extends LifecycleStoreOperation>(
  prUrl: string,
  owner: string,
  operation: Operation,
  conversationId?: string,
): StateChangeCapture<Operation> | null {
  const host = hostLifecycleRoute(conversationId, operation);
  const target = parseCodeHostPullRequestUrl(prUrl);
  if (!host || target?.provider !== "github") return null;
  return {
    prUrl,
    owner,
    authGeneration: currentWorkspaceAuthGeneration(),
    actor: HOST_ACTOR,
    operation,
    host,
  };
}

/** Whether everything `capture` bound to (PR, workspace user, host route) still holds. */
export function captureMatches(
  capture: StateChangeCapture<LifecycleStoreOperation>,
  owner: string,
): boolean {
  return (
    owner === capture.owner &&
    currentWorkspaceAuthGeneration() === capture.authGeneration &&
    hostLifecycleRouteCurrent(capture.host, capture.operation)
  );
}

export interface PullRequestStateAction {
  offer: PullRequestStateOffer;
  /** A chain for this PR and workspace user is running, from this box or another. */
  running: boolean;
  /** The change that chain is making, while it runs. */
  runningOperation: StateChangeOperation | null;
  notice: StateChainNotice | null;
  /** Close or reopen, posting the box's comment first when `withComment`. */
  run: (withComment: boolean) => void;
  /** After a refusal: try the state change alone again, never the comment. */
  retry: () => void;
  /**
   * GitHub may have changed the state: nothing more is sent for this PR until a read made
   * after this asks shows where it stands. Resolves once that read settles.
   */
  checkAgain: () => Promise<void>;
  /** A Check Again read is under way. */
  checking: boolean;
  /** The last Check Again couldn't read where the PR stands, so the hold stays. */
  checkFailed: boolean;
  /** Close or reopen can't run now: one is running, or an earlier one's outcome is unknown. */
  held: boolean;
  /** Why it can't run now, for a disabled item's tooltip and ⌘K row; null while it can. */
  heldReason: string | null;
  dismiss: () => void;
}

/** What Close or Reopen offers for a PR in `prState`: the change here, a link to GitHub, or none. */
export function stateChangeOffer(
  prUrl: string,
  prState: string | null | undefined,
  /** The session whose host makes the change. */
  conversationId?: string,
): PullRequestStateOffer {
  const operation = stateChangeOperationFor(prState);
  if (!operation) return { kind: "none" };
  const definition = STATE_CHANGES[operation];
  if (hostLifecycleRoute(conversationId, operation))
    return { kind: "action", operation, label: definition.label };
  return { kind: "link", label: definition.githubLabel, url: prUrl };
}

/** The writes a state change sends, from the list's or the box's own mutations. */
export interface StateChangeSend {
  /** The page's login these mutations route as (null for the host's account), as rendered. */
  change: (
    operation: StateChangeOperation,
    prUrl: string,
  ) => Promise<PullRequestStateChangeOutcome>;
}

/** The two mutations Close and Reopen send through, for `conversationId`'s reads. */
export function useStateChangeSend(conversationId: string): StateChangeSend {
  const close = usePullRequestLifecycleChange(conversationId, "pull_request_close");
  const reopen = usePullRequestLifecycleChange(conversationId, "pull_request_reopen");
  return {
    change: (operation, prUrl) =>
      operation === "pull_request_close" ? close.change(prUrl) : reopen.change(prUrl),
  };
}

export interface StateChangeStart {
  key: string;
  owner: string;
  capture: StateChainCapture;
  send: StateChangeSend;
  origin: PullRequestActionOrigin | null;
  /** Post a comment first; only the comment box has one. */
  withComment?: boolean;
  post?: () => Promise<boolean>;
  /** Whatever started it went away; a row's run belongs to the store and never is. */
  disposed?: () => boolean;
  /** Also required at the send, beyond the capture, e.g. the PR still in the confirmed state. */
  stillApplies?: () => boolean;
  /** An earlier run of this change already posted its comment. */
  commentPosted?: boolean;
  /** What its caller knows this send by; its notice names it. A new one by default. */
  attempt?: string;
  /** A bulk run's own: send the state change again after a passing failure. */
  retry?: BulkRetry;
}

/** Why a close or reopen failed, for its retries; null when it didn't. */
function stateChangeFailure(outcome: PullRequestStateChangeOutcome) {
  if (outcome.kind === "refused") return { code: outcome.code, cause: outcome.cause ?? null };
  return outcome.kind === "unknown" ? { code: "outcome_unknown" } : null;
}

/**
 * Run Close or Reopen for one PR and workspace user, whichever surface started it: held while
 * another change to the PR runs or an earlier outcome is unknown, sent only while everything
 * the click captured still holds, and what it came to kept in the shared chain store.
 */
export function startStateChange({
  key,
  owner,
  capture,
  send,
  origin,
  withComment = false,
  post = () => Promise.resolve(true),
  disposed = () => false,
  stillApplies = () => true,
  commentPosted = false,
  attempt = newChangeAttempt(),
  retry,
}: StateChangeStart): Promise<StateChainOutcome | null> {
  const current = stateChainState(key);
  // An earlier change may have happened: nothing more until a fresh read says where it is.
  // A draft or auto-merge change for this PR holds it the same way.
  if (current.running || current.notice?.outcome.kind === "unknown") return Promise.resolve(null);
  if (lifecycleActionsHold(key)) return Promise.resolve(null);
  const boundTo = {
    authGeneration: capture.authGeneration,
    actor: capture.actor,
    host: capture.host,
  };
  setStateChainState(key, { running: capture.operation, notice: null });
  // Closing from the Pull Requests page takes the PR out of its lists once GitHub does it;
  // reopening one, an Undo too, brings it back.
  const report = origin?.begin(capture.prUrl, capture.actor);
  // Sent only while everything the click captured still holds.
  const stillCurrent = () => captureMatches(capture, owner) && stillApplies();
  const sendOnce = () => send.change(capture.operation, capture.prUrl);
  return changeStateWithComment({
    withComment,
    post,
    disposed,
    stillCurrent,
    change: () => {
      if (!retry) return sendOnce();
      return withBulkRetries(sendOnce, stateChangeFailure, {
        ...retry,
        rechecksBeforeWrite: true,
        mayRetry: () => retry.mayRetry() && stillCurrent(),
      }).then(({ outcome, afterUnknown }): PullRequestStateChangeOutcome => {
        // A retry after an unknown outcome that finds it done: the earlier send made it.
        if (afterUnknown && outcome.kind === "refused" && landedEarlier(outcome.code)) {
          return {
            kind: "changed",
            state: capture.operation === "pull_request_close" ? "CLOSED" : "OPEN",
          };
        }
        return outcome;
      });
    },
  }).then((result) => {
    if (result.kind === "changed")
      report?.succeeded(capture.operation === "pull_request_close" ? "closed" : "reopened");
    // GitHub may have made it: a Check Again that finds it made reports it then.
    else if (result.kind === "unknown" && report) keepUnconfirmedReport(attempt, report);
    // A retry of the state change alone keeps knowing the comment was posted before.
    const outcome =
      commentPosted && "commentPosted" in result ? { ...result, commentPosted: true } : result;
    const quiet =
      outcome.kind === "changed" ||
      outcome.kind === "comment-failed" ||
      (outcome.kind === "skipped" && !outcome.commentPosted);
    setStateChainState(key, {
      running: null,
      notice: quiet ? null : { operation: capture.operation, attempt, outcome, boundTo },
    });
    return outcome;
  });
}

/**
 * After a refusal: the state change alone again, bound to the PR and account of the first
 * click, never the comment. Null where nothing was refused; skipped where the account moved.
 */
export function retryStateChange(
  start: Omit<StateChangeStart, "capture" | "withComment" | "commentPosted">,
  prUrl: string,
): Promise<StateChainOutcome | null> {
  const { key, owner } = start;
  const notice = stateChainState(key).notice;
  if (!notice || notice.outcome.kind !== "refused") return Promise.resolve(null);
  const capture: StateChainCapture = {
    prUrl,
    owner,
    authGeneration: notice.boundTo.authGeneration,
    actor: notice.boundTo.actor,
    operation: notice.operation,
    host: notice.boundTo.host,
  };
  const commentPosted = notice.outcome.commentPosted;
  if (!captureMatches(capture, owner)) {
    const outcome = { kind: "skipped", reason: "changed", commentPosted } as const;
    setStateChainState(key, { running: null, notice: { ...notice, outcome } });
    return Promise.resolve(outcome);
  }
  return startStateChange({ ...start, capture, commentPosted });
}

/**
 * Check Again after an unknown outcome: a fresh read as the account the change was made as.
 * Once it confirms where the PR stands, the hold clears, unless a newer notice replaced it.
 */
export async function checkStateChangeAgain(
  queryClient: QueryClient,
  conversationId: string,
  key: string,
  prUrl: string,
): Promise<FreshReadResult | null> {
  const notice = stateChainState(key).notice;
  if (notice?.outcome.kind !== "unknown") return null;
  const { result, facts } = await readFreshPullRequest(queryClient, conversationId, prUrl);
  if (result === "confirmed" && facts && stateChainState(key).notice === notice) {
    setStateChainState(key, { ...stateChainState(key), notice: null });
    settleUnconfirmedReport(notice.attempt, notice.operation, facts);
  }
  return result;
}

let holdsVersion = 0;

/** A number that moves whenever any Close/Reopen chain or draft/auto-merge hold changes. */
export function useStateChangeHoldsVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      const changed = () => {
        holdsVersion += 1;
        listener();
      };
      const offChains = subscribeStateChains(changed);
      const offLifecycle = subscribeLifecycleActions(changed);
      return () => {
        offChains();
        offLifecycle();
      };
    },
    () => holdsVersion,
    () => 0,
  );
}

export function usePullRequestStateAction({
  conversationId,
  prUrl,
  prState,
  post,
}: {
  conversationId: string;
  prUrl: string;
  prState: string | null | undefined;
  /** The box's own post; resolves true once GitHub took the comment. */
  post: () => Promise<boolean>;
}): PullRequestStateAction {
  const owner = useWorkspaceDraftOwner();
  const key = stateChainKey(owner, prUrl);
  const chain = useStateChainState(key);
  const origin = usePullRequestActionOrigin();
  const send = useStateChangeSend(conversationId);
  // The box that starts a chain owns its continuation: once it unmounts (navigation, another
  // PR), nothing more is sent for it, and a box mounted later starts its own.
  const disposed = useRef(false);
  useEffect(() => {
    disposed.current = false;
    return () => {
      disposed.current = true;
    };
  }, []);

  const offer = stateChangeOffer(prUrl, prState, conversationId);
  const boxStart = {
    key,
    owner,
    send,
    origin,
    post,
    disposed: () => disposed.current,
  };

  const run = (withComment: boolean) => {
    if (offer.kind !== "action") return;
    const capture = captureStateChange(prUrl, owner, offer.operation, conversationId);
    if (capture) void startStateChange({ ...boxStart, capture, withComment });
  };

  const retry = () => void retryStateChange(boxStart, prUrl);

  const queryClient = useQueryClient();
  const [checking, setChecking] = useState(false);
  const [checkFailed, setCheckFailed] = useState(false);
  const checkAgain = async () => {
    if (checking || stateChainState(key).notice?.outcome.kind !== "unknown") return;
    setChecking(true);
    setCheckFailed(false);
    const result = await checkStateChangeAgain(queryClient, conversationId, key, prUrl);
    if (!disposed.current) {
      setChecking(false);
      setCheckFailed(result === "failed");
    }
  };

  const uncertain = chain.notice?.outcome.kind === "unknown";
  // Re-rendered when a draft or auto-merge change starts or settles, as it holds these too.
  useLifecycleActionState(key);
  const heldReason = stateChangeHeldReason(chain, lifecycleActionsHold(key));
  return {
    offer,
    running: chain.running !== null,
    runningOperation: chain.running,
    notice: chain.notice,
    run,
    retry,
    checkAgain,
    checking,
    checkFailed,
    held: heldReason !== null,
    heldReason,
    // An unknown outcome stays until a fresh read; only the other notices can be dismissed.
    dismiss: () => {
      if (uncertain) return;
      setStateChainState(key, { ...stateChainState(key), notice: null });
    },
  };
}
