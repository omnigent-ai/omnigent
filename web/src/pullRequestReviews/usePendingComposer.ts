// A comment box's pending review actions at one place: what its buttons say and do, and the
// notice beside them. Each send decides again at the click, and an answer clears only the
// draft it was sent from, for the workspace user and host account that sent it.

import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { randomUUID } from "@/lib/randomUUID";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";
import { pullRequestCommentTooLong } from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { openLink } from "@/lib/openLinkInApp";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { pendingScopeCurrent } from "@/pullRequestComments/commentEditScope";

import {
  clearSentPendingDraft,
  composerViewCurrent,
  dropPendingRecovery,
  mountComposerView,
  noteComposerView,
  type PendingComposerMode,
  pendingComposerMode,
  pendingRecoveriesOf,
  recordPendingRecovery,
  usePendingRecoveriesVersion,
} from "./pendingComposer";
import { type DraftPlace, placeKey, samePlace, type SentDraft } from "./pendingReview";
import {
  checkPendingHold,
  type PendingChangeOutcome,
  type PendingHold,
  type PendingSentAs,
  pendingHoldsOf,
  sendPendingReviewChange,
  useHostPendingChangeInFlight,
  usePendingHolds,
} from "./pendingReviewChanges";
import {
  hostPendingIdentity,
  hostPendingOffers,
  hostPendingRoute,
  hostPendingScopeOf,
} from "./hostPendingRoute";
import { invalidatePendingReview, usePendingReview } from "./usePendingReview";

/** One button: its label, whether it can run, and what it does. */
export interface PendingComposerAction {
  label: string;
  disabled: boolean;
  run: () => void;
}

/** What the box shows beside its buttons, and the actions the notice offers. */
export interface PendingComposerNotice {
  text: string;
  tone: "status" | "alert";
  actions: PendingComposerAction[];
}

/** The box's pending review controls; null where pending reviews leave the box as it is. */
export interface PendingComposer {
  mode: PendingComposerMode["kind"];
  /** The box's filled action, which ⌘↵ runs. */
  primary: PendingComposerAction;
  secondary: PendingComposerAction | null;
  notice: PendingComposerNotice | null;
}

export const ADDED_NEWER_KEPT = "Your comment was added to your review. Your newer text is kept.";
export const MOVED_BEFORE_ANSWER =
  "The account changed before GitHub answered. Check your pending review on GitHub before sending this again.";
const START_A_REVIEW = "Start a review";
const STARTED_NOT_ADDED = "Your review was started, but this comment wasn’t added.";
const REVIEW_GONE =
  "The review you started is no longer pending; it was submitted or discarded elsewhere. Your comment wasn’t added.";

function newSentId(): string {
  return randomUUID();
}

const shortSha = (sha: string) => sha.slice(0, 7);

/**
 * The pending review controls for the box at `place` whose draft is `draftKey`. `post` is the
 * box's own immediate comment, kept as Comment where a review can still be started. `head` is
 * the head the page shows, which a reply's add is sent against. Where `leads` is `review`, as
 * on GitHub's new line comments, Start a review is the filled action and Comment the other.
 */
export function usePendingComposer({
  conversationId,
  prUrl,
  place,
  draftKey,
  head,
  post,
  commentLabel,
  leads = "comment",
}: {
  conversationId: string;
  prUrl: string;
  place: DraftPlace | null;
  draftKey: readonly string[];
  head: string | null | undefined;
  post: () => void;
  commentLabel: string;
  leads?: "comment" | "review";
}): PendingComposer | null {
  const queryClient = useQueryClient();
  const owner = useWorkspaceDraftOwner();
  usePendingRecoveriesVersion();
  const { state, refetch } = usePendingReview(prUrl, !!place, conversationId);
  const busy = useHostPendingChangeInFlight(prUrl);
  const holds = usePendingHolds(prUrl);
  // Only this session's host, along its current route: another session's sends stay its own.
  const inScope = (scope: PendingSentAs) => {
    const current = hostPendingRoute(conversationId, prUrl);
    return !!current && hostPendingIdentity(scope.host) === hostPendingIdentity(current);
  };
  const atPlace = (candidate: DraftPlace) => !!place && samePlace(candidate, place);
  const holdHere = (from: PendingHold[]) =>
    from.find(
      (each): each is Extract<PendingHold, { kind: "draft_add" }> =>
        each.kind === "draft_add" && atPlace(each.place) && inScope(each.sentAs),
    ) ?? null;
  const recoveryHere = () =>
    pendingRecoveriesOf(draftOwner(), prUrl).find(
      (each) => atPlace(each.place) && inScope(each.sentAs),
    ) ?? null;
  const hold = holdHere(holds);
  const recovery = recoveryHere();

  const mode = pendingComposerMode({ state, hold, recovery });
  // Start goes as a start and an add, so it's offered only where both are served; without
  // them the box comments as it does today.
  const route = hostPendingRoute(conversationId, prUrl);
  const adds = !!route && hostPendingOffers(route, "add");
  const starts = adds && hostPendingOffers(route, "start");
  const unserved =
    (mode.kind === "comment_or_start" && !starts) || (mode.kind === "add_to_review" && !adds);
  const viewKey = place ? [owner, prUrl.toLowerCase(), placeKey(place)].join("\n") : null;
  const draftName = draftKey.join("\n");
  // While this box shows its draft, its view is live; leaving retires it for good.
  useEffect(() => {
    if (!viewKey) return;
    return mountComposerView(draftName.split("\n"), viewKey);
  }, [draftName, viewKey]);
  if (!place || !viewKey) return null;
  // The view this box shows its draft in; an answer applies to the draft only in this view.
  const viewToken = noteComposerView(draftKey, viewKey);
  if (mode.kind === "today" || unserved) return null;

  const draftNow = () => queryClient.getQueryData<PullRequestCommentDraft>(draftKey);
  const openOnGitHub: PendingComposerAction = {
    label: "Open on GitHub",
    disabled: false,
    run: () => openLink(conversationId, prUrl),
  };

  /**
   * Whether an answer taken in `token`'s view may still change the draft: the same workspace
   * user, the same view of it (never one left and come back to), and its scope still current.
   */
  const stillOurs = (sentBy: string, sentAs: PendingSentAs, token: typeof viewToken) =>
    draftOwner() === sentBy &&
    composerViewCurrent(draftKey, token) &&
    pendingScopeCurrent(sentAs, prUrl);
  /** End this send's wait on the draft, and only this send's; another's is left alone. */
  const unpend = (sentId: string, update: Partial<PullRequestCommentDraft> = {}) =>
    queryClient.setQueryData<PullRequestCommentDraft>(
      draftKey,
      (current) =>
        current &&
        (current.sendingId === sentId
          ? { ...current, pending: false, sendingId: undefined, ...update }
          : current),
    );
  /**
   * An add GitHub has: its own half-done start is retired and the reads refreshed wherever the
   * page is now; the draft is cleared only `inView`.
   */
  const confirmed = (sent: SentDraft | null, inView: boolean) => {
    invalidatePendingReview(queryClient, prUrl);
    void invalidateSessionPullRequest(queryClient, conversationId, prUrl, [
      "pr-page-github-review-threads",
    ]);
    if (!sent) return;
    if (sent.recoveryId) dropPendingRecovery(sent.recoveryId);
    if (inView) clearSentPendingDraft(queryClient, sent, ADDED_NEWER_KEPT);
    else unpend(sent.sentId, { sentId: undefined, error: MOVED_BEFORE_ANSWER });
  };

  /** Add the draft into `pendingReviewId`, or start a review with it; decided at the click. */
  const send = (pendingReviewId: string | null, recoveryId: string | null) => {
    // Decided again at the click: a held add here, or a recovery pinned elsewhere, sends nothing.
    if (holdHere(pendingHoldsOf(draftOwner(), prUrl))) return;
    const pinned = recoveryHere();
    if (
      pinned &&
      (pinned.gone || pinned.id !== recoveryId || pinned.reviewId !== pendingReviewId)
    ) {
      return;
    }
    if (!pinned && recoveryId) return;
    const current = draftNow();
    // Decided again at the click, along the session's host route.
    const shown = hostPendingRoute(conversationId, prUrl);
    if (!shown || !current || current.pending || (current.uploading ?? 0) > 0) return;
    if (!current.body.trim() || pullRequestCommentTooLong(current.body)) return;
    const expectedHead = place.kind === "lines" ? place.headSha : head;
    if (!expectedHead) return;
    const sentBy = draftOwner();
    const sentAs: PendingSentAs = {
      login: shown.actor.login,
      host: hostPendingScopeOf(shown),
    };
    const sent: SentDraft = {
      draftKey,
      sentId: newSentId(),
      body: current.body,
      place,
      ...(recoveryId ? { recoveryId } : {}),
    };
    const token = viewToken;
    queryClient.setQueryData<PullRequestCommentDraft>(
      draftKey,
      (latest) =>
        latest && {
          ...latest,
          pending: true,
          error: null,
          sentId: sent.sentId,
          sendingId: sent.sentId,
        },
    );
    void sendPendingReviewChange(
      prUrl,
      shown,
      { kind: "draft_add", body: current.body, place, pendingReviewId, expectedHead },
      sent,
    ).then((outcome: PendingChangeOutcome) => {
      if (outcome.kind === "suppressed") {
        unpend(sent.sentId);
        return;
      }
      // A half-done start is kept for its own scope, whichever view shows the draft now.
      if (outcome.kind === "partial" && draftOwner() === sentBy) {
        recordPendingRecovery({
          owner: sentBy,
          prUrl,
          sentAs,
          reviewId: outcome.reviewId,
          place,
          sent,
        });
      }
      const inView = outcome.kind !== "moved" && stillOurs(sentBy, sentAs, token);
      if (outcome.kind === "done" && draftOwner() === sentBy) {
        confirmed(sent, inView);
        return;
      }
      if (!inView) {
        unpend(sent.sentId, { sentId: undefined, error: MOVED_BEFORE_ANSWER });
        return;
      }
      switch (outcome.kind) {
        case "partial":
          unpend(sent.sentId, { error: STARTED_NOT_ADDED });
          invalidatePendingReview(queryClient, prUrl);
          return;
        case "held":
          unpend(sent.sentId);
          return;
        case "refused":
          unpend(sent.sentId, { sentId: undefined, error: outcome.message });
      }
    });
  };

  const checkAgain = (held: Extract<PendingHold, { kind: "draft_add" }>) => {
    const sentBy = draftOwner();
    const token = viewToken;
    void checkPendingHold(held.id).then((outcome) => {
      if (!outcome || draftOwner() !== sentBy) return;
      // A half-done start is kept for its own scope; the draft changes only in this view.
      if (outcome.kind === "started") {
        recordPendingRecovery({
          owner: sentBy,
          prUrl,
          sentAs: held.sentAs,
          reviewId: outcome.reviewId,
          place: held.place,
          sent: held.local,
        });
        return;
      }
      if (outcome.kind === "landed") confirmed(held.local, stillOurs(sentBy, held.sentAs, token));
    });
  };

  const draft = draftNow();
  const sendable =
    !busy &&
    !!draft &&
    !draft.pending &&
    (draft.uploading ?? 0) === 0 &&
    !!draft.body.trim() &&
    !pullRequestCommentTooLong(draft.body);
  const waiting = (label: string): PendingComposerAction => ({
    label,
    disabled: true,
    run: () => {},
  });
  // The filled action's place stays the same while the pending review is still being checked.
  const leadLabel = leads === "review" ? START_A_REVIEW : commentLabel;

  switch (mode.kind) {
    case "checking":
      return {
        mode: mode.kind,
        primary: waiting(leadLabel),
        secondary: null,
        notice: { text: "Checking for your pending review…", tone: "status", actions: [] },
      };
    case "unreadable":
      return {
        mode: mode.kind,
        primary: waiting(leadLabel),
        secondary: null,
        notice: {
          text: "Couldn’t check for a pending review.",
          tone: "alert",
          actions: [{ label: "Retry", disabled: false, run: refetch }, openOnGitHub],
        },
      };
    case "comment_or_start": {
      const comment: PendingComposerAction = {
        label: commentLabel,
        disabled: !sendable,
        run: () => {
          if (holdHere(pendingHoldsOf(draftOwner(), prUrl)) || recoveryHere()) return;
          post();
        },
      };
      const start: PendingComposerAction = {
        label: START_A_REVIEW,
        disabled: !sendable,
        run: () => send(null, null),
      };
      return {
        mode: mode.kind,
        primary: leads === "review" ? start : comment,
        secondary: leads === "review" ? comment : start,
        notice: null,
      };
    }
    case "add_to_review":
      return {
        mode: mode.kind,
        primary: {
          label: "Add to review",
          disabled: !sendable,
          run: () => send(mode.reviewId, null),
        },
        secondary: null,
        notice: mode.outdated
          ? {
              text: `Your review is on ${shortSha(mode.outdated.reviewCommit)}, from before the latest changes.`,
              tone: "status",
              actions: [],
            }
          : null,
      };
    case "held": {
      const { hold: held } = mode;
      const checked =
        held.checked === "unreadable"
          ? " Couldn’t check just now."
          : held.checked === "unsettled"
            ? " GitHub doesn’t show it yet."
            : "";
      return {
        mode: mode.kind,
        primary: waiting(leadLabel),
        secondary: null,
        notice: {
          text: `Couldn’t confirm whether GitHub saved this.${checked}`,
          tone: "status",
          actions: [
            {
              label: held.checking ? "Checking…" : "Check again",
              disabled: held.checking,
              run: () => checkAgain(held),
            },
            openOnGitHub,
          ],
        },
      };
    }
    case "recovering":
      return {
        mode: mode.kind,
        primary: {
          label: "Add to review",
          disabled: !sendable,
          run: () => send(mode.recovery.reviewId, mode.recovery.id),
        },
        secondary: null,
        notice: { text: STARTED_NOT_ADDED, tone: "status", actions: [openOnGitHub] },
      };
    case "recovery_gone":
      return {
        mode: mode.kind,
        primary: waiting(leadLabel),
        secondary: null,
        notice: {
          text: REVIEW_GONE,
          tone: "status",
          actions: [
            {
              label: "Use for a new comment",
              disabled: false,
              run: () => dropPendingRecovery(mode.recovery.id),
            },
            openOnGitHub,
          ],
        },
      };
  }
}

/**
 * A review conversation's reply box under pending reviews, the same wherever the thread shows
 * (Changes or Comments): replies only where the thread takes them, and never to a thread only
 * the viewer can see yet.
 */
export function usePendingReply(
  thread: Pick<ReviewThread, "id" | "can_reply">,
  context: Pick<ReviewThreadContext, "conversationId" | "prUrl" | "canReply" | "headSha">,
  composer: { draftKey: readonly string[]; post: () => void },
): PendingComposer | null {
  return usePendingComposer({
    conversationId: context.conversationId,
    prUrl: context.prUrl,
    place: context.canReply && thread.can_reply ? { kind: "reply", threadId: thread.id } : null,
    draftKey: composer.draftKey,
    head: context.headSha,
    post: composer.post,
    commentLabel: "Reply",
  });
}
