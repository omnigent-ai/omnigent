// Starting, confirming, sending and checking one PR's draft and auto-merge changes, outside
// React: the PR page's controls and the Pull Requests list's rows both go through these, so
// each change is captured, held and reported the same way wherever it was asked for.

import { type BulkRetry, landedEarlier, withBulkRetries } from "@/lib/macWriteRetries";
import type { QueryClient } from "@tanstack/react-query";

import { showToast } from "@/components/ui/toast";
import type {
  AutoMergeEnableChange,
  PullRequestLifecycleOutcome,
  PullRequestMergeMethod,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type {
  PullRequestActionOrigin,
  PullRequestActionReport,
  PullRequestOwnAction,
} from "@/lib/pullRequestActionOrigin";

import { keepUnconfirmedReport, newChangeAttempt, settleUnconfirmedReport } from "./changeAttempts";
import { readFreshPullRequest, type FreshReadResult } from "./freshPullRequestRead";
import {
  type AutoMergeActionId,
  isAutoMergeAction,
  LIFECYCLE_ACTIONS,
  type LifecycleActionDefinition,
  type LifecycleActionId,
  type LifecycleAvailability,
  type LifecycleConfirmation,
  type LifecycleNotice,
  lifecycleActionState,
  lifecycleActionsHold,
  updateLifecycleActionState,
} from "./pullRequestLifecycleActions";
import { stateChainState } from "./pullRequestStateChain";
import {
  captureStateChange,
  captureMatches,
  type StateChangeCapture,
} from "./usePullRequestStateAction";

/** One change's write, sent through the session's host. */
export interface LifecycleWrite<Change> {
  change: (change: Change) => Promise<PullRequestLifecycleOutcome>;
}

/** The writes that make each change, from the caller's mutation hooks. */
export interface LifecycleChanges {
  convert: LifecycleWrite<{ prUrl: string }>;
  ready: LifecycleWrite<{ prUrl: string }>;
  enable: LifecycleWrite<AutoMergeEnableChange>;
  disable: LifecycleWrite<{ prUrl: string }>;
  /** Whether what a change came to is said in a plain toast; rows say it with actions. */
  toasts: boolean;
  /** The Pull Requests page, which a draft or ready change made there updates; null elsewhere. */
  origin: PullRequestActionOrigin | null;
}

type DraftActionId = Extract<LifecycleActionId, "convert_to_draft" | "ready_for_review">;

/** What the page records once GitHub made each draft change; auto-merge changes record nothing. */
const OWN_ACTION: Record<DraftActionId, PullRequestOwnAction> = {
  convert_to_draft: "converted_to_draft",
  ready_for_review: "marked_ready",
};

const isDraftAction = (id: LifecycleActionId): id is DraftActionId => Object.hasOwn(OWN_ACTION, id);

/** What an auto-merge change leaves auto-merge as, once made. */
const AUTO_MERGE_MADE: Record<AutoMergeActionId, "on" | "off"> = {
  enable_auto_merge: "on",
  disable_auto_merge: "off",
};

/** The PR a change is for, and the workspace user asking. */
export interface LifecycleTarget {
  /** `stateChainKey(owner, prUrl)`, which close and reopen share. */
  key: string;
  owner: string;
  prUrl: string;
  number: number;
  /** The session whose host makes the change. */
  conversationId?: string;
}

/** What a send came to: GitHub's answer, or why nothing was sent (`held`, `skipped`). */
export type LifecycleSendOutcome = PullRequestLifecycleOutcome["kind"] | "held" | "skipped";

/** Whether close/reopen's chain for this PR, or one of these, holds every state change. */
export function lifecycleHeld(key: string): boolean {
  const chain = stateChainState(key);
  return (
    chain.running !== null || chain.notice?.outcome.kind === "unknown" || lifecycleActionsHold(key)
  );
}

type LifecycleCapture = StateChangeCapture<LifecycleActionDefinition["operation"]>;

/** Who a notice's change was made as, and all its click bound to, from `capture`. */
function noticeIdentity(capture: LifecycleCapture): Pick<LifecycleNotice, "madeAs" | "boundTo"> {
  const { authGeneration, actor, host } = capture;
  return {
    madeAs: { ...actor, host },
    boundTo: { authGeneration, actor, host },
  };
}

function settle(
  target: LifecycleTarget,
  id: LifecycleActionId,
  capture: LifecycleCapture,
  outcome: PullRequestLifecycleOutcome,
  toasts: boolean,
  attempt: string,
) {
  const definition = LIFECYCLE_ACTIONS[id];
  const { owner, prUrl } = target;
  const identity = { ...noticeIdentity(capture), attempt };
  let notice: LifecycleNotice | null = null;
  switch (outcome.kind) {
    case "changed":
      if (toasts) showToast(`${definition.done} · #${target.number}`);
      break;
    case "refused":
      notice = { id, owner, prUrl, ...identity, outcome };
      // The merge bar says this one where Merge is.
      if (toasts && outcome.code !== "mergeable_now") {
        showToast(`${definition.failed}: ${outcome.message}`);
      }
      break;
    case "unknown":
      notice = { id, owner, prUrl, ...identity, outcome };
      if (toasts) showToast(`${definition.maybe} Check it before trying again.`);
      break;
    case "abandoned":
      notice = { id, owner, prUrl, ...identity, outcome };
      break;
  }
  updateLifecycleActionState(target.key, (current) => ({ ...current, running: null, notice }));
}

/** Leave `id` unsent: what its click captured no longer holds. Its notice says so. */
export function skipLifecycleChange(
  target: LifecycleTarget,
  id: LifecycleActionId,
  capture: LifecycleCapture,
  attempt: string = newChangeAttempt(),
): void {
  const { owner, prUrl } = target;
  updateLifecycleActionState(target.key, (current) => ({
    ...current,
    confirming: null,
    notice: {
      id,
      owner,
      prUrl,
      attempt,
      ...noticeIdentity(capture),
      outcome: { kind: "skipped" },
    },
  }));
}

/** Why a lifecycle change failed, for its retries; null when it didn't. */
function lifecycleFailure(outcome: PullRequestLifecycleOutcome) {
  if (outcome.kind === "refused") return { code: outcome.code, cause: outcome.cause ?? null };
  return outcome.kind === "unknown" ? { code: "outcome_unknown" } : null;
}

/** Send `id` as `confirmation` captured it, if all it bound to, its account included, holds. */
export async function sendLifecycleChange(
  target: LifecycleTarget,
  id: LifecycleActionId,
  confirmation: Pick<LifecycleConfirmation, "capture" | "headSha" | "method">,
  changes: LifecycleChanges,
  /** What its caller knows this send by; its notice names it. A new one by default. */
  attempt: string = newChangeAttempt(),
  /** A bulk run's own: send a draft or ready change again after a passing failure. */
  retry?: BulkRetry,
): Promise<LifecycleSendOutcome> {
  const { key, owner, prUrl } = target;
  // An earlier change may have happened: nothing more until a fresh read says where it is.
  if (lifecycleHeld(key)) return "held";
  const { capture } = confirmation;
  if (!captureMatches(capture, owner)) {
    skipLifecycleChange(target, id, capture, attempt);
    return "skipped";
  }
  let change: Promise<PullRequestLifecycleOutcome>;
  let report: PullRequestActionReport | undefined;
  if (id === "enable_auto_merge") {
    const { headSha, method } = confirmation;
    if (!headSha || !method) return "skipped";
    change = changes.enable.change({ prUrl, mergeMethod: method, expectedHead: headSha });
  } else if (id === "disable_auto_merge") {
    change = changes.disable.change({ prUrl });
  } else {
    // Made from the Pull Requests page, an Undo too, it moves the PR between the page's lists.
    report = changes.origin?.begin(prUrl, capture.actor);
    const draftChange = id === "convert_to_draft" ? changes.convert : changes.ready;
    change = retry
      ? withBulkRetries(() => draftChange.change({ prUrl }), lifecycleFailure, {
          ...retry,
          rechecksBeforeWrite: true,
          // Sent again only as its click bound it, on the run's grant.
          mayRetry: () => retry.mayRetry() && captureMatches(capture, owner),
        }).then(({ outcome, afterUnknown }) =>
          // A retry after an unknown outcome that finds it done: the earlier send made it.
          afterUnknown && outcome.kind === "refused" && landedEarlier(outcome.code)
            ? { kind: "changed" }
            : outcome,
        )
      : draftChange.change({ prUrl });
  }
  updateLifecycleActionState(key, (current) => ({
    ...current,
    running: id,
    notice: null,
    confirming: null,
  }));
  const outcome = await change;
  if (outcome.kind === "changed" && isDraftAction(id)) report?.succeeded(OWN_ACTION[id]);
  // GitHub may have made it: a Check Again that finds it made reports it then.
  else if (outcome.kind === "unknown" && report) keepUnconfirmedReport(attempt, report);
  settle(target, id, capture, outcome, changes.toasts, attempt);
  return outcome.kind;
}

/**
 * Start `id`: send Ready for review now, resolving to what it came to, or open the others'
 * confirmation on `surface` (null: nothing was sent).
 */
export function requestLifecycleChange(
  target: LifecycleTarget,
  id: LifecycleActionId,
  {
    availability,
    surface,
    headSha,
    defaultMethod,
  }: {
    availability: LifecycleAvailability;
    /** The surface that shows the confirmation; null where nothing can ask first. */
    surface: string | null;
    headSha?: string | null;
    defaultMethod?: PullRequestMergeMethod | null;
  },
  changes: LifecycleChanges,
): Promise<LifecycleSendOutcome> | null {
  const { key, owner, prUrl } = target;
  if (availability.kind !== "mac" || lifecycleActionState(key).confirming) return null;
  const capture = captureStateChange(
    prUrl,
    owner,
    LIFECYCLE_ACTIONS[id].operation,
    target.conversationId,
  );
  if (!capture) return null;
  // Those that don't ask first are sent now.
  if (id === "ready_for_review" || id === "disable_auto_merge") {
    return sendLifecycleChange(target, id, { capture }, changes);
  }
  // Asking first needs a surface to ask on.
  if (!surface) return null;
  // The dialog shows this head and is pinned to it, even if a later read moves the branch.
  const confirming: LifecycleConfirmation =
    id === "enable_auto_merge"
      ? {
          id,
          surface,
          capture,
          headSha: headSha ?? undefined,
          method: defaultMethod ?? undefined,
        }
      : { id, surface, capture };
  updateLifecycleActionState(key, (current) => ({ ...current, confirming }));
  return null;
}

/** The refused change for `key`, captured as its notice's bound identity; null where none was. */
export function refusedLifecycleChange(
  key: string,
): { id: LifecycleActionId; capture: LifecycleCapture } | null {
  const notice = lifecycleActionState(key).notice;
  if (notice?.outcome.kind !== "refused") return null;
  const { id } = notice;
  return {
    id,
    capture: {
      ...notice.boundTo,
      prUrl: notice.prUrl,
      owner: notice.owner,
      operation: LIFECYCLE_ACTIONS[id].operation,
    },
  };
}

/**
 * Try Again after a refusal, as the account and sign-in the refused change was made as: the
 * notice's bound identity, never a fresh capture. Ready and Disable are sent again; Convert asks again on
 * `surface`. Where that identity no longer holds for `owner`, the workspace user now, nothing is
 * sent and the notice says so. Null where there's no refusal to try again.
 */
export function retryLifecycleChange(
  target: LifecycleTarget,
  owner: string,
  surface: string | null,
  changes: LifecycleChanges,
): Promise<LifecycleSendOutcome> | null {
  const refused = refusedLifecycleChange(target.key);
  // Turning auto-merge on asks again with its head and method, from where it was asked for.
  if (!refused || refused.id === "enable_auto_merge") return null;
  const { id, capture } = refused;
  if (!captureMatches(capture, owner)) {
    skipLifecycleChange(target, id, capture);
    return Promise.resolve("skipped");
  }
  // Those that don't ask first are sent now.
  if (id === "ready_for_review" || id === "disable_auto_merge") {
    return sendLifecycleChange(target, id, { capture }, changes);
  }
  if (!surface || lifecycleActionState(target.key).confirming) return null;
  updateLifecycleActionState(target.key, (current) => ({
    ...current,
    confirming: { id, surface, capture },
  }));
  return null;
}

/**
 * Check Again after an unknown outcome: read the PR afresh as the account the change was made
 * as, which settles the notice once it answers. It never sends the change again. Null where
 * there was nothing to check, or a check was already under way.
 */
export async function checkLifecycleChangeAgain(
  queryClient: QueryClient,
  conversationId: string,
  key: string,
  prUrl: string,
): Promise<FreshReadResult | null> {
  const current = lifecycleActionState(key);
  const notice = current.notice;
  if (current.checking || notice?.outcome.kind !== "unknown") return null;
  updateLifecycleActionState(key, (now) => ({ ...now, checking: true, checkFailed: false }));
  const { result, facts } = await readFreshPullRequest(queryClient, conversationId, prUrl);
  // Only the notice this check began for clears, and reports; a newer one stays.
  const stillThisNotice = lifecycleActionState(key).notice === notice;
  // An auto-merge change settles only on what this read says of auto-merge, never on state alone.
  const autoMergeId = isAutoMergeAction(notice.id) ? notice.id : null;
  const answered = !!facts && (!autoMergeId || facts.autoMerge !== "unknown");
  const settles = result === "confirmed" && answered && stillThisNotice;
  const now = autoMergeId && facts ? facts.autoMerge : null;
  updateLifecycleActionState(key, (state) => {
    const checked = { ...state, checking: false };
    if (result === "failed" || (result === "confirmed" && !answered && stillThisNotice)) {
      return { ...checked, checkFailed: true };
    }
    if (!settles) return checked;
    // Read the other way: say where it is now, which clears the hold, without claiming why.
    if (autoMergeId && now !== AUTO_MERGE_MADE[autoMergeId]) {
      return {
        ...checked,
        notice: { ...notice, outcome: { kind: "checked", autoMergeOn: now === "on" } },
      };
    }
    return { ...checked, notice: null };
  });
  if (settles && facts && isDraftAction(notice.id)) {
    settleUnconfirmedReport(notice.attempt, notice.id, facts);
  }
  if (result === "confirmed" && !answered) return "failed";
  return result;
}
