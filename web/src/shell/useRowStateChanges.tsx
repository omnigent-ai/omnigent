// Close Pull Request and Reopen Pull Request on a list row's menus and ⌘K: the same definition,
// write route, holds and outcomes as the comment box's, run from the shared chain store so they
// finish even once the menu or the row is gone. Each runs at once, as the user asked for it;
// what it came to is said in a toast, with Undo once made, Try Again for a refusal and Check
// Again where GitHub may have made it.

import { useQueryClient } from "@tanstack/react-query";
import { GitPullRequestArrowIcon, GitPullRequestClosedIcon, RefreshCwIcon } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef } from "react";
import { toast } from "sonner";

import { announceChange } from "@/components/pendingChange/announceChange";
import { holdToast, settleHeldToast, TIMED_TOAST } from "@/lib/heldToast";
import { createLiveScopes } from "@/lib/liveScope";
import { pullRequestIn } from "@/lib/pullRequestReference";
import { pullRequestToastName } from "@/lib/pullRequestToastName";
import type { StateChangeOperation } from "@/lib/pullRequestStateHolds";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { usePullRequestActionOrigin } from "@/lib/pullRequestActionOrigin";
import { newChangeAttempt, recordChangeAttempt } from "@/pullRequestPage/changeAttempts";
import { lifecycleActionsHold } from "@/pullRequestPage/pullRequestLifecycleActions";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import {
  type StateChainOutcome,
  stateChainKey,
  stateChainMessage,
  stateChainState,
} from "@/pullRequestPage/pullRequestStateChain";
import {
  STATE_CHANGES,
  stateChangeHeldReason,
  stateChangeOperationFor,
} from "@/pullRequestPage/pullRequestStateChanges";
import {
  captureStateChange,
  captureMatches,
  type StateChangeCapture,
  checkStateChangeAgain,
  retryStateChange,
  startStateChange,
  stateChangeOffer,
  useStateChangeHoldsVersion,
  useStateChangeSend,
} from "@/pullRequestPage/usePullRequestStateAction";
import { AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

import type { PullRequestListEntry, RowAction } from "./PullRequestListRows";

const ICONS = {
  pull_request_close: GitPullRequestClosedIcon,
  pull_request_reopen: GitPullRequestArrowIcon,
} as const;

const toastIdOf = (key: string) => `pull-request-state:${key}`;

/** An action that sent nothing: its held toast goes, unless the run it waits on replaces it. */
const settleHeld = (key: string) =>
  settleHeldToast(toastIdOf(key), stateChainState(key).running !== null);

const PENDING: Record<StateChangeOperation, string> = {
  pull_request_close: "Closing",
  pull_request_reopen: "Reopening",
};

/** The change that undoes each. */
const INVERSE: Record<StateChangeOperation, StateChangeOperation> = {
  pull_request_close: "pull_request_reopen",
  pull_request_reopen: "pull_request_close",
};

type StateCapture = StateChangeCapture<StateChangeOperation>;

/** What a toast's Undo and Try Again do through the list mounted now. */
interface RowStateRecovery {
  /** The list shows this PR now, so its recovery goes through this list. */
  lists: (url: string) => boolean;
  retry: (
    url: string,
    name: string,
    boundTo: StateCapture,
    evenUnlisted: boolean,
    heldKey: string,
  ) => void;
  undo: (
    url: string,
    name: string,
    operation: StateChangeOperation,
    madeAs: StateCapture,
    heldKey: string,
  ) => void;
}

// Per recovery scope: the page replaces its table on each tab, while the toasts stay.
const LIVE_LISTS = createLiveScopes<RowStateRecovery>();

/**
 * Run `recover` through the list mounted now, with its account and rows; with none mounted,
 * nothing can say who the page reads as, so nothing is sent and the toast says why.
 */
function throughLiveList(
  scope: string,
  url: string,
  toastKey: string,
  notMade: string,
  recover: (live: RowStateRecovery) => void,
) {
  const live = LIVE_LISTS.currentFor(scope, (list) => list.lists(url));
  if (live) recover(live);
  else listClosed(toastKey, notMade);
}

/** What a refused Close or Reopen is tried again as, wherever its page offers Try Again. */
export interface StateRefusal {
  url: string;
  name: string;
  /** The workspace user it was refused for. */
  owner: string;
  /** It was an Undo, or its retry, which may go once the PR left the list. */
  evenUnlisted: boolean;
  /** The attempt it was offered for; a newer outcome for the PR offers its own. */
  attempt?: string;
}

/**
 * Try a refused Close or Reopen again through the list mounted now for `scope`, from its toast,
 * the page's list of outcomes or ⌘K alike: as the refused change was made, never renewed.
 */
export function retryRefusedStateChange(scope: string, key: string, refusal: StateRefusal): void {
  const notice = stateChainState(key).notice;
  if (
    notice?.outcome.kind !== "refused" ||
    (refusal.attempt !== undefined && notice.attempt !== refusal.attempt)
  ) {
    settleHeld(key);
    return;
  }
  const { url, name, owner, evenUnlisted } = refusal;
  const verb = notice.operation === "pull_request_close" ? "closed" : "reopened";
  // As the refused change was made: its Undo, once made, is bound the same way.
  const boundTo: StateCapture = {
    ...notice.boundTo,
    prUrl: url,
    owner,
    operation: notice.operation,
  };
  throughLiveList(scope, url, key, `${pullRequestToastName(url)} wasn’t ${verb}`, (live) =>
    live.retry(url, name, boundTo, evenUnlisted, key),
  );
}

function listClosed(toastKey: string, notMade: string) {
  toast(`${notMade}: the list it was made from isn’t open now.`, {
    id: toastIdOf(toastKey),
    ...TIMED_TOAST,
  });
}

export function useRowStateChanges({
  conversationId,
  recoveryScope,
  entriesByUrl,
  onOpen,
}: {
  conversationId: string;
  /** What owns the toasts across this list's replacement; this list alone without one. */
  recoveryScope?: string;
  /** The list's rows now, for the PR's state when the close is confirmed and sent. */
  entriesByUrl: ReadonlyMap<string, PullRequestListEntry>;
  /** Show the PR, where its comment box keeps the outcome and Check Again. */
  onOpen: (url: string) => void;
}): {
  actionFor: (entry: PullRequestListEntry, name: string) => RowAction | null;
} {
  const ownScope = useId();
  const scope = recoveryScope ?? ownScope;
  // A menu or ⌘K row runs on the list that offered it, and only while that list is shown.
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const holdsVersion = useStateChangeHoldsVersion();
  // The rows offer Close and Reopen once the session's host serves the state change.
  const hostChanges = usePullRequestMethodServed(conversationId, "setPullRequestState");
  const owner = useWorkspaceDraftOwner();
  const origin = usePullRequestActionOrigin();
  const send = useStateChangeSend(conversationId);
  const queryClient = useQueryClient();
  const entriesRef = useRef(entriesByUrl);
  entriesRef.current = entriesByUrl;
  // The PR's row, however another list writes its link.
  const listedEntry = (url: string) => pullRequestIn(entriesRef.current, url);
  // Each send, a retry too, goes only while the row still offers that very change: Close on an
  // open PR (a draft too), Reopen on a closed one; never once merged or changed. An Undo, and
  // its retries, may also go once the page took the row out of its list.
  const stillOffers = useCallback(
    (url: string, operation: StateChangeOperation, evenUnlisted = false) =>
      () => {
        const state = listedEntry(url)?.pr?.state;
        if (!state) return evenUnlisted;
        return stateChangeOperationFor(state) === operation;
      },
    [],
  );

  const report = useCallback(
    (
      outcome: StateChainOutcome | null,
      url: string,
      name: string,
      operation: StateChangeOperation,
      {
        capture,
        evenUnlisted = false,
        heldKey,
      }: {
        /** What it was made under, which its Undo is bound to. */
        capture?: StateChangeCapture<StateChangeOperation>;
        /** It was an Undo, which may go once the row is no longer listed. */
        evenUnlisted?: boolean;
        /** The toast whose action sent it, held open until this replaces it. */
        heldKey?: string;
      } = {},
    ) => {
      const key = stateChainKey(owner, url);
      if (!outcome) {
        if (heldKey) settleHeld(heldKey);
        return;
      }
      // Its toast replaces the one held for it, which may be another workspace user's by now.
      const toastId = toastIdOf(heldKey ?? key);
      const verb = operation === "pull_request_close" ? "closed" : "reopened";
      if (outcome.kind === "changed") {
        const done = `${verb === "closed" ? "Closed" : "Reopened"} ${pullRequestToastName(url)}`;
        announceChange(`${done}.`);
        const madeAs = capture?.actor.login;
        toast(madeAs ? `${done} as @${madeAs}` : done, {
          id: toastId,
          ...TIMED_TOAST,
          action: capture && {
            label: "Undo",
            onClick: (event) => {
              holdToast(
                event,
                toastIdOf(key),
                `${PENDING[INVERSE[operation]]} ${pullRequestToastName(url)}…`,
              );
              throughLiveList(
                scope,
                url,
                key,
                `${pullRequestToastName(url)} wasn’t changed back`,
                (live) => live.undo(url, name, operation, capture, key),
              );
            },
          },
        });
        return;
      }
      const notice = stateChainState(key).notice;
      // Nothing was sent: the PR or the account changed after it was chosen.
      if (!notice) {
        toast(
          `${pullRequestToastName(url)} wasn’t ${verb}: it or the account changed since you chose it.`,
          {
            id: toastId,
            ...TIMED_TOAST,
          },
        );
        return;
      }
      const message = `${pullRequestToastName(url)}: ${stateChainMessage(notice)}`;
      if (notice.outcome.kind === "refused") {
        // It stays until Try Again succeeds or the user closes it, so a refusal can't go unseen.
        toast.error(message, {
          id: toastId,
          duration: Number.POSITIVE_INFINITY,
          closeButton: true,
          action: {
            label: "Try Again",
            onClick: (event) => {
              holdToast(
                event,
                toastIdOf(key),
                `${PENDING[notice.operation]} ${pullRequestToastName(url)}…`,
              );
              retryRefusedStateChange(scope, key, {
                url,
                name,
                owner,
                evenUnlisted,
                attempt: notice.attempt,
              });
            },
          },
        });
      } else if (notice.outcome.kind === "unknown") {
        toast(message, {
          id: toastId,
          duration: Infinity,
          action: {
            label: "Check Again",
            onClick: (event) => {
              holdToast(event, toastIdOf(key), `Checking ${pullRequestToastName(url)}…`);
              checkAgain(url);
            },
          },
        });
      } else {
        toast(message, { id: toastId, ...TIMED_TOAST });
      }
    },
    // checkAgain reads only what these do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conversationId, onOpen, owner, queryClient, scope],
  );

  /** After an unknown outcome: a fresh read as the account it was made as, never a resend. */
  function checkAgain(url: string) {
    const key = stateChainKey(owner, url);
    void checkStateChangeAgain(queryClient, conversationId, key, url).then((result) => {
      if (result === "failed")
        toast.error(
          `${pullRequestToastName(url)}: Couldn’t read it from GitHub. Open it to check.`,
          {
            id: toastIdOf(key),
            duration: Number.POSITIVE_INFINITY,
            action: { label: "Open", onClick: () => onOpen(url) },
          },
        );
      // Confirmed, nothing to check, or the account changed while it read (stale): the hold
      // stays where it wasn't confirmed, and the row offers Check Again.
      else toast.dismiss(toastIdOf(key));
    });
  }

  /** The row's Close or Reopen, or null where the PR is merged or not read yet. */
  const actionFor = useCallback(
    (entry: PullRequestListEntry, name: string): RowAction | null => {
      void holdsVersion;
      const url = entry.association.url;
      const key = stateChainKey(owner, url);
      const chain = stateChainState(key);
      // GitHub may have made it: the row holds until a fresh read as the account it was made as
      // says, and offers that read whatever the PR reads as now, merged or not loaded included.
      if (chain.notice?.outcome.kind === "unknown") {
        return {
          id: "lifecycle:check-state-again",
          section: "lifecycle",
          icon: RefreshCwIcon,
          keywords: [...STATE_CHANGES[chain.notice.operation].keywords, "check", "unknown"],
          label: "Check Again",
          paletteLabel: `Check ${name} Again`,
          run: () => latest.current.checkAgain(url),
        };
      }
      void hostChanges;
      const offer = stateChangeOffer(url, entry.pr?.state, conversationId);
      // Only where the host makes the change; elsewhere the row's Open on GitHub already leads there.
      if (offer.kind !== "action") return null;
      const operation = offer.operation;
      const definition = STATE_CHANGES[operation];
      const held = stateChangeHeldReason(chain, lifecycleActionsHold(key));
      return {
        id: operation === "pull_request_close" ? "close" : "reopen",
        section: "lifecycle",
        icon: ICONS[operation],
        keywords: definition.keywords,
        label: definition.menuLabel,
        paletteLabel: definition.paletteLabel(name),
        availability: held ? unavailableBecause(held) : AVAILABLE,
        movesFocus: definition.confirms,
        run: () => {
          if (mounted.current) latest.current.start(url, name, operation);
          else listClosed(key, `${pullRequestToastName(url)} wasn’t changed`);
        },
      };
    },
    [conversationId, holdsVersion, hostChanges, owner],
  );

  /** A new attempt for `url`, recorded where a page owns this list's recovery. */
  function attemptFor(url: string, name: string, evenUnlisted: boolean): string {
    const id = newChangeAttempt();
    if (recoveryScope) {
      const number = listedEntry(url)?.row.number ?? 0;
      recordChangeAttempt({ id, scope: recoveryScope, owner, url, number, name, evenUnlisted });
    }
    return id;
  }

  /** Send `operation` for `url` now, as the account the page reads as, keeping its row busy. */
  function startChange(url: string, name: string, operation: StateChangeOperation) {
    const capture = captureStateChange(url, owner, operation, conversationId);
    if (!capture) return;
    void startStateChange({
      attempt: attemptFor(url, name, false),
      key: stateChainKey(owner, url),
      owner,
      capture,
      send,
      origin,
      stillApplies: stillOffers(url, operation),
    }).then((outcome) => report(outcome, url, name, operation, { capture }));
  }

  /**
   * After a refusal: the same change again, only where the row still offers it, and as the
   * account, sign-in and workspace user the refused change was made under. Whatever it comes
   * to replaces the clicked toast, `heldKey`.
   */
  function retry(
    url: string,
    name: string,
    boundTo: StateCapture,
    evenUnlisted: boolean,
    heldKey: string,
  ) {
    // Never another workspace user's refusal for the same PR, nor a renewed sign-in.
    if (!captureMatches(boundTo, owner)) {
      toast(`${pullRequestToastName(url)} wasn’t changed: the account changed since.`, {
        id: toastIdOf(heldKey),
        ...TIMED_TOAST,
      });
      return;
    }
    const start = {
      attempt: attemptFor(url, name, evenUnlisted),
      key: stateChainKey(boundTo.owner, url),
      owner,
      send,
      origin,
      stillApplies: stillOffers(url, boundTo.operation, evenUnlisted),
    };
    void retryStateChange(start, url).then((next) =>
      report(next, url, name, boundTo.operation, {
        capture: boundTo,
        evenUnlisted,
        heldKey,
      }),
    );
  }

  /**
   * Undo a change GitHub made: the inverse, as the same account. The page may have taken the
   * row out of its list by then, so only a row still listed in another state stops it.
   */
  function undo(
    url: string,
    name: string,
    operation: StateChangeOperation,
    madeAs: StateChangeCapture<StateChangeOperation>,
    /** The toast whose Undo this is, held open until what it came to replaces it. */
    heldKey: string,
  ) {
    const inverse = INVERSE[operation];
    // Everything the change was made under, the workspace user and sign-in too; never renewed.
    const capture: StateChangeCapture<StateChangeOperation> = { ...madeAs, operation: inverse };
    if (!captureMatches(capture, owner)) {
      toast(`${pullRequestToastName(url)} wasn’t changed back: the account changed since.`, {
        id: toastIdOf(heldKey),
        ...TIMED_TOAST,
      });
      return;
    }
    void startStateChange({
      attempt: attemptFor(url, name, true),
      key: stateChainKey(owner, url),
      owner,
      capture,
      send,
      origin,
      stillApplies: stillOffers(url, inverse, true),
    }).then((outcome) =>
      report(outcome, url, name, inverse, { capture, evenUnlisted: true, heldKey }),
    );
  }

  // Menus run on this list; toasts recover through the scope's list mounted now.
  const latest = useRef({ start: startChange, checkAgain });
  latest.current = { start: startChange, checkAgain };
  LIVE_LISTS.usePublish(scope, {
    lists: (url) => listedEntry(url) !== undefined,
    retry,
    undo,
  });

  // One object while `actionFor` stands, so the rows' actions built from it keep their identity.
  return useMemo(() => ({ actionFor }), [actionFor]);
}
