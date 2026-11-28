// Ready for review, Convert to draft and Enable and Disable auto-merge on a Pull Requests list's
// rows: their menus and ⌘K rows, from the same definitions, holds and capture the PR page uses.
// Each runs at once, as the user asked for it, but Enable, which asks first in a dialog that reads
// the PR's merge options; a toast says what it came to, with Undo once a draft change is made, Try
// Again for a refusal and Check Again where GitHub may have made it.

import { readOnlyHostWriteReason } from "@/lib/readOnlyCodeHosts";
import { useQueryClient } from "@tanstack/react-query";
import {
  GitMergeIcon,
  GitPullRequestArrowIcon,
  GitPullRequestDraftIcon,
  RefreshCwIcon,
} from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";

import {
  type PullRequest,
  type PullRequestMergeMethod,
  type PullRequestMergeOptions,
  useEnablePullRequestAutoMerge,
  usePullRequestAutoMergeDisableChange,
  usePullRequestDraftChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { pullRequestToastName } from "@/lib/pullRequestToastName";
import { holdToast, settleHeldToast, TIMED_TOAST } from "@/lib/heldToast";
import { createLiveScopes } from "@/lib/liveScope";
import { autoMergeKnown } from "@/lib/pullRequestAutoMerge";
import { pullRequestIn } from "@/lib/pullRequestReference";
import { usePullRequestActionOrigin } from "@/lib/pullRequestActionOrigin";
import type { RowAction } from "@/shell/PullRequestListRows";
import { AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

import { newChangeAttempt, recordChangeAttempt } from "./changeAttempts";
import { hostLifecycleRoute } from "./hostLifecycleRoute";
import { RowEnableAutoMergeDialog } from "./EnableAutoMergeDialog";
import {
  checkLifecycleChangeAgain,
  type LifecycleChanges,
  type LifecycleSendOutcome,
  type LifecycleTarget,
  lifecycleHeld,
  refusedLifecycleChange,
  sendLifecycleChange,
  skipLifecycleChange,
} from "./lifecycleRequests";
import {
  LIFECYCLE_ACTIONS,
  LIFECYCLE_HELD_REASON,
  type LifecycleActionDefinition,
  type LifecycleActionId,
  type LifecycleAvailability,
  lifecycleActionState,
  gitHubHandoff,
  lifecycleAvailability,
  lifecycleNoticeMessage,
  subscribeLifecycleActions,
} from "./pullRequestLifecycleActions";
import { usePullRequestPageRoute } from "./pullRequestPageRoute";
import { stateChainKey, subscribeStateChains } from "./pullRequestStateChain";
import {
  captureStateChange,
  captureMatches,
  type StateChangeCapture,
} from "./usePullRequestStateAction";

/** The changes a row offers, in its menu's order. */
const ROW_LIFECYCLE_ACTIONS = [
  "ready_for_review",
  "convert_to_draft",
  "enable_auto_merge",
  "disable_auto_merge",
] as const satisfies readonly LifecycleActionId[];
type RowLifecycleActionId = (typeof ROW_LIFECYCLE_ACTIONS)[number];

export const ROW_LIFECYCLE_ICONS = {
  ready_for_review: GitPullRequestArrowIcon,
  convert_to_draft: GitPullRequestDraftIcon,
  enable_auto_merge: GitMergeIcon,
  disable_auto_merge: GitMergeIcon,
} as const;

export const ROW_LIFECYCLE_KEYWORDS = [
  "pull request",
  "pr",
  "draft",
  "ready",
  "review",
  "auto-merge",
  "automerge",
  "github",
] as const;

/** How a row names each change: those that run at once end in no "…"; Enable asks first. */
const ROW_COPY: Record<
  RowLifecycleActionId,
  {
    label: string;
    paletteLabel: (name: string) => string;
    /** What the toast says once GitHub made it. */
    done: (name: string) => string;
    /** What a held toast says while it runs. */
    pending: (name: string) => string;
  }
> = {
  ready_for_review: {
    label: "Ready for review",
    paletteLabel: (name) => `Mark ${name} Ready for Review`,
    done: (name) => `Marked ${name} ready for review`,
    pending: (name) => `Marking ${name} ready…`,
  },
  convert_to_draft: {
    label: "Convert to draft",
    paletteLabel: (name) => `Convert ${name} to Draft`,
    done: (name) => `Converted ${name} to a draft`,
    pending: (name) => `Converting ${name} to a draft…`,
  },
  enable_auto_merge: {
    label: "Enable auto-merge…",
    paletteLabel: (name) => `Enable Auto-merge for ${name}…`,
    done: (name) => `Turned on auto-merge for ${name}`,
    pending: (name) => `Turning on auto-merge for ${name}…`,
  },
  disable_auto_merge: {
    label: "Disable auto-merge",
    paletteLabel: (name) => `Disable Auto-merge for ${name}`,
    done: (name) => `Turned off auto-merge for ${name}`,
    pending: (name) => `Turning off auto-merge for ${name}…`,
  },
};

/**
 * The change that undoes each. Turning auto-merge back on asks for a method and a head, so
 * Disable has no Undo.
 */
const INVERSE: Partial<Record<RowLifecycleActionId, RowLifecycleActionId>> = {
  ready_for_review: "convert_to_draft",
  convert_to_draft: "ready_for_review",
};

const toastIdOf = (key: string) => `pull-request-lifecycle:${key}`;

/** An action that sent nothing: its held toast goes, unless the run it waits on replaces it. */
const settleHeld = (key: string) =>
  settleHeldToast(toastIdOf(key), lifecycleActionState(key).running !== null);

type LifecycleCapture = StateChangeCapture<LifecycleActionDefinition["operation"]>;

/** What a toast's Undo and Try Again do through the list mounted now. */
interface RowLifecycleRecovery {
  /** The list shows this PR now, so its recovery goes through this list. */
  lists: (url: string) => boolean;
  retry: (
    target: LifecycleTarget,
    id: RowLifecycleActionId,
    name: string,
    evenUnlisted: boolean,
  ) => void;
  undo: (
    target: LifecycleTarget,
    id: RowLifecycleActionId,
    name: string,
    madeAs: LifecycleCapture,
    heldKey: string,
  ) => void;
}

// Per recovery scope: the page replaces its table on each tab, while the toasts stay.
const LIVE_LISTS = createLiveScopes<RowLifecycleRecovery>();

/**
 * Run `recover` through the list mounted now, with its account and rows; with none mounted,
 * nothing can say who the page reads as, so nothing is sent and the toast says why.
 */
function throughLiveList(
  scope: string,
  url: string,
  toastKey: string,
  notMade: string,
  recover: (live: RowLifecycleRecovery) => void,
) {
  const live = LIVE_LISTS.currentFor(scope, (list) => list.lists(url));
  if (live) recover(live);
  else listClosed(toastKey, notMade);
}

/** What a refused Ready or Convert is tried again as, wherever its page offers Try Again. */
export interface LifecycleRefusal {
  url: string;
  number: number;
  name: string;
  /** The workspace user it was refused for. */
  owner: string;
  /** It was an Undo, or its retry, which may go once the PR left the list. */
  evenUnlisted: boolean;
  /** The attempt it was offered for; a newer outcome for the PR offers its own. */
  attempt?: string;
}

const isRowLifecycleAction = (id: LifecycleActionId): id is RowLifecycleActionId =>
  (ROW_LIFECYCLE_ACTIONS as readonly LifecycleActionId[]).includes(id);

/**
 * Try a refused Ready or Convert again through the list mounted now for `scope`, from its toast,
 * the page's list of outcomes or ⌘K alike: at once, as the refused change was made, never renewed.
 */
export function retryRefusedLifecycleChange(
  scope: string,
  key: string,
  refusal: LifecycleRefusal,
): void {
  const notice = lifecycleActionState(key).notice;
  if (
    notice?.outcome.kind !== "refused" ||
    !isRowLifecycleAction(notice.id) ||
    (refusal.attempt !== undefined && notice.attempt !== refusal.attempt)
  ) {
    settleHeld(key);
    return;
  }
  const { url, number, name, owner, evenUnlisted } = refusal;
  const target: LifecycleTarget = { key, owner, prUrl: url, number };
  const id = notice.id;
  throughLiveList(scope, url, key, `${pullRequestToastName(target)} wasn’t changed`, (live) =>
    live.retry(target, id, name, evenUnlisted),
  );
}

function listClosed(toastKey: string, notMade: string) {
  toast(`${notMade}: the list it was made from isn’t open now.`, {
    id: toastIdOf(toastKey),
    ...TIMED_TOAST,
  });
}

/** The rows a list shows now, by URL: a PR missing from it is no longer listed. */
export type ListedLifecycleRows = ReadonlyMap<string, Pick<LifecycleRow, "pr">>;

const NEVER_ON_GITHUB = () => false;

/** An Enable dialog a row opened, and everything its confirmation is bound to. */
interface EnableConfirmation {
  target: LifecycleTarget;
  name: string;
  capture: LifecycleCapture;
  /** The head the dialog shows, which the change is pinned to. */
  headSha: string;
  evenUnlisted: boolean;
  /** The account, list and scope it was asked under, as a count of changes to them. */
  epoch: number;
}

/** What a report knows beyond the outcome: whom it was made as, and which toast it replaces. */
interface ReportContext {
  /** What it was made under, which its Undo is bound to. */
  capture?: LifecycleCapture;
  /** It was an Undo, which may go once the row is no longer listed. */
  evenUnlisted?: boolean;
  /** The toast whose action sent it, held open until this replaces it. */
  heldKey?: string;
}

/** What a Try Again finds when the PR no longer takes the change. */
const NO_LONGER_APPLIES: Record<RowLifecycleActionId, string> = {
  ready_for_review: "it isn’t an open draft any more",
  convert_to_draft: "it isn’t open and ready for review any more",
  enable_auto_merge: "it isn’t open and ready for review with auto-merge off any more",
  disable_auto_merge: "its auto-merge isn’t on any more",
};

/** The row a change is offered on: its PR as read, and how ⌘K names it. */
export interface LifecycleRow {
  url: string;
  number: number;
  /** ⌘K's name for the PR, e.g. "#12 Fix the build". */
  paletteName: string;
  /** The PR's live read; absent while it loads, when nothing is offered. */
  pr: Pick<PullRequest, "state" | "is_draft" | "head_sha" | "auto_merge"> | undefined;
  /** False for another provider's PR, such as Azure DevOps': there's no GitHub to hand off to. */
  onGitHub?: boolean;
}

interface RowFacts {
  held: boolean;
  /** Whether the session's host makes `operation`; elsewhere the row hands off to GitHub. */
  offered?: (operation: LifecycleActionDefinition["operation"]) => boolean;
  /** GitHub merges the PR with its stack, on GitHub: nothing arms a merge from here. */
  mergesOnGitHub?: boolean;
  /** False for another provider's PR, which GitHub can't change. */
  onGitHub?: boolean;
}

/**
 * Whether a row's own read lets it offer Enable auto-merge…: open, ready, its head known, and
 * auto-merge not known to be on. A row's read has no queue or merge state; those are read on
 * the click.
 */
export function rowTakesAutoMergeEnable(
  pr: Pick<PullRequest, "state" | "is_draft" | "head_sha" | "auto_merge"> | undefined,
): boolean {
  return (
    !!pr &&
    pr.state?.toUpperCase() === "OPEN" &&
    !pr.is_draft &&
    !!pr.head_sha &&
    autoMergeKnown(pr) !== "on"
  );
}

/**
 * Where each row change stands for `pr`: the PR page's rule, from the row's own read. A change
 * the session's host makes is offered here; any other that applies hands off to GitHub.
 */
export function rowLifecycleAvailability(
  id: RowLifecycleActionId,
  row: Pick<LifecycleRow, "url" | "pr">,
  facts: RowFacts,
): LifecycleAvailability {
  if (!row.pr) return { kind: "hidden" };
  if (id === "enable_auto_merge") {
    if (!rowTakesAutoMergeEnable(row.pr) || facts.mergesOnGitHub) return { kind: "hidden" };
    if (!facts.offered?.("pull_request_auto_merge_enable"))
      return gitHubHandoff(row.url, facts.onGitHub);
    return facts.held ? { kind: "held", reason: LIFECYCLE_HELD_REASON } : { kind: "mac" };
  }
  return lifecycleAvailability(id, {
    prUrl: row.url,
    state: row.pr.state,
    isDraft: row.pr.is_draft,
    // No row change reads the merge facts, which only turning auto-merge on needs.
    mergeable: false,
    mergesOnGitHub: false,
    headSha: row.pr.head_sha,
    // Only a read that says it's on offers Disable; one that can't say isn't off or on.
    autoMergeOn: autoMergeKnown(row.pr) === "on",
    macMerge: null,
    ...facts,
    offered: facts.offered ?? (() => false),
  });
}

/** The facts a row's changes stand on now. */
function rowFacts(
  url: string,
  owner: string,
  mergesOnGitHub: boolean,
  conversationId: string,
): RowFacts {
  return {
    held: lifecycleHeld(stateChainKey(owner, url)),
    mergesOnGitHub,
    offered: (operation) => !!hostLifecycleRoute(conversationId, operation),
  };
}

export function useRowLifecycleActions(
  conversationId: string,
  enabled: boolean,
  /** Every row the list shows now, so a change checks the PR as it is listed when it's sent. */
  listed: ListedLifecycleRows,
  /** What owns the toasts across this list's replacement; this list alone without one. */
  recoveryScope?: string,
  /** Whether GitHub merges a PR with its stack, as the list's read says now. */
  mergesOnGitHub: (url: string) => boolean = NEVER_ON_GITHUB,
): { actionsFor: (row: LifecycleRow) => RowAction[]; dialog: ReactNode } {
  // Read when a change is asked for and again as it's sent, not when its row was drawn.
  const mergesOnGitHubRef = useRef(mergesOnGitHub);
  mergesOnGitHubRef.current = mergesOnGitHub;
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
  // The rows redraw once the session's host route is decided or moves.
  const hostRoute = usePullRequestPageRoute(conversationId);
  const owner = useWorkspaceDraftOwner();
  const queryClient = useQueryClient();
  // Holds come from either store, so every row is redrawn as a change starts or settles.
  const [storeVersion, redraw] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    const unsubscribeLifecycle = subscribeLifecycleActions(redraw);
    const unsubscribeChains = subscribeStateChains(redraw);
    return () => {
      unsubscribeLifecycle();
      unsubscribeChains();
    };
  }, []);
  const convert = usePullRequestDraftChange(conversationId, "pull_request_convert_to_draft");
  const ready = usePullRequestDraftChange(conversationId, "pull_request_ready_for_review");
  const enable = useEnablePullRequestAutoMerge(conversationId);
  const disable = usePullRequestAutoMergeDisableChange(conversationId);
  const origin = usePullRequestActionOrigin();
  const changes = useMemo<LifecycleChanges>(
    () => ({
      convert,
      ready,
      enable,
      disable,
      toasts: false,
      origin,
    }),
    [convert, ready, enable, disable, origin],
  );
  const listedRef = useRef(listed);
  listedRef.current = listed;
  // The PR's row, however another list writes its link.
  const listedRow = (url: string) => pullRequestIn(listedRef.current, url);

  /**
   * Whether `id` still applies to the PR as listed now: a PR no longer listed, or not read,
   * takes it only for an Undo, whose own change may have taken it out of the list. Null where
   * it does.
   */
  function whyNot(target: LifecycleTarget, id: RowLifecycleActionId, evenUnlisted: boolean) {
    const pr = listedRow(target.prUrl)?.pr;
    if (!pr) return evenUnlisted ? null : `${NO_LONGER_APPLIES[id]}.`;
    // Only whether it still applies matters here: GitHub's handoff or the page's own change.
    const stillTakes = rowLifecycleAvailability(
      id,
      { url: target.prUrl, pr },
      {
        held: lifecycleHeld(target.key),
        mergesOnGitHub: mergesOnGitHubRef.current(target.prUrl),
        onGitHub: true,
      },
    );
    if (stillTakes.kind === "hidden") return `${NO_LONGER_APPLIES[id]}.`;
    return lifecycleHeld(target.key) ? LIFECYCLE_HELD_REASON : null;
  }

  /** Say what a row's change came to, with the recovery its outcome allows. */
  function report(
    outcome: LifecycleSendOutcome | null,
    target: LifecycleTarget,
    id: RowLifecycleActionId,
    name: string,
    { capture, evenUnlisted = false, heldKey }: ReportContext = {},
  ) {
    if (!outcome) {
      if (heldKey) settleHeld(heldKey);
      return;
    }
    // Its toast replaces the one held for it, which may be another workspace user's by now.
    const toastId = toastIdOf(heldKey ?? target.key);
    if (outcome === "changed") {
      const done = ROW_COPY[id].done(pullRequestToastName(target));
      const madeAs = capture?.actor.login;
      const inverse = INVERSE[id];
      toast(madeAs ? `${done} as @${madeAs}` : done, {
        id: toastId,
        ...TIMED_TOAST,
        action: capture &&
          inverse && {
            label: "Undo",
            onClick: (event) => {
              holdToast(
                event,
                toastIdOf(target.key),
                ROW_COPY[inverse].pending(pullRequestToastName(target)),
              );
              throughLiveList(
                scope,
                target.prUrl,
                target.key,
                `${pullRequestToastName(target)} wasn’t changed back`,
                (live) => live.undo(target, id, name, capture, target.key),
              );
            },
          },
      });
      return;
    }
    if (outcome === "held") {
      toast(`${pullRequestToastName(target)}: ${LIFECYCLE_HELD_REASON}`, {
        id: toastId,
        ...TIMED_TOAST,
      });
      return;
    }
    const notice = lifecycleActionState(target.key).notice;
    if (!notice) {
      if (heldKey) settleHeld(heldKey);
      return;
    }
    const message = `${pullRequestToastName(target)}: ${lifecycleNoticeMessage(notice)}`;
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
              toastIdOf(target.key),
              ROW_COPY[id].pending(pullRequestToastName(target)),
            );
            retryRefusedLifecycleChange(scope, target.key, {
              url: target.prUrl,
              number: target.number,
              name,
              owner: target.owner,
              evenUnlisted,
              attempt: notice.attempt,
            });
          },
        },
      });
    } else if (notice.outcome.kind === "unknown") {
      toast(message, {
        id: toastId,
        duration: Number.POSITIVE_INFINITY,
        action: {
          label: "Check Again",
          onClick: (event) => {
            holdToast(event, toastIdOf(target.key), `Checking ${pullRequestToastName(target)}…`);
            latest.current.checkAgain(target);
          },
        },
      });
    } else {
      toast(message, { id: toastId, ...TIMED_TOAST });
    }
  }

  /** A new attempt for `target`, recorded where a page owns this list's recovery. */
  function attemptFor(target: LifecycleTarget, name: string, evenUnlisted: boolean): string {
    const id = newChangeAttempt();
    if (recoveryScope) {
      recordChangeAttempt({
        id,
        scope: recoveryScope,
        owner,
        url: target.prUrl,
        number: target.number,
        name,
        evenUnlisted,
      });
    }
    return id;
  }

  /** Run `id` for `row` now, as the account the page reads as, if the PR as listed takes it. */
  function start(row: LifecycleRow, id: RowLifecycleActionId) {
    const target: LifecycleTarget = {
      key: stateChainKey(owner, row.url),
      owner,
      prUrl: row.url,
      number: row.number,
    };
    // A menu or ⌘K row kept from an earlier render may offer what the PR no longer takes.
    const reason = whyNot(target, id, false);
    if (reason) {
      toast(`${pullRequestToastName(target)} wasn’t changed: ${reason}`, {
        id: toastIdOf(target.key),
        ...TIMED_TOAST,
      });
      return;
    }
    const capture = captureStateChange(
      row.url,
      owner,
      LIFECYCLE_ACTIONS[id].operation,
      conversationId,
    );
    if (!capture) return;
    if (id === "enable_auto_merge") {
      askToEnable({ target, name: row.paletteName, capture, evenUnlisted: false });
      return;
    }
    const attempt = attemptFor(target, row.paletteName, false);
    void sendLifecycleChange(target, id, { capture }, changes, attempt).then((outcome) =>
      latest.current.report(outcome, target, id, row.paletteName, { capture }),
    );
  }

  /**
   * After a refusal: again only where the PR still takes it, with the original change's own
   * eligibility, and only as the account and sign-in the refused change was made under.
   */
  function retry(
    target: LifecycleTarget,
    id: RowLifecycleActionId,
    name: string,
    evenUnlisted: boolean,
  ) {
    const reason = whyNot(target, id, evenUnlisted);
    if (reason) {
      toast(`${pullRequestToastName(target)} wasn’t changed: ${reason}`, {
        id: toastIdOf(target.key),
        ...TIMED_TOAST,
      });
      return;
    }
    const refused = refusedLifecycleChange(target.key);
    if (!refused || refused.id !== id) {
      settleHeld(target.key);
      return;
    }
    const attempt = attemptFor(target, name, evenUnlisted);
    // Only for the workspace user, sign-in and account it was refused for.
    if (!captureMatches(refused.capture, owner)) {
      skipLifecycleChange(target, id, refused.capture, attempt);
      latest.current.report("skipped", target, id, name, { heldKey: target.key });
      return;
    }
    // Turning it on asks again, with the head and methods as they are now, as the same account.
    if (id === "enable_auto_merge") {
      settleHeld(target.key);
      askToEnable({ target, name, capture: refused.capture, evenUnlisted });
      return;
    }
    const sent = sendLifecycleChange(target, id, { capture: refused.capture }, changes, attempt);
    void sent.then((outcome) =>
      latest.current.report(outcome, target, id, name, {
        capture: refused.capture,
        evenUnlisted,
        heldKey: target.key,
      }),
    );
  }

  /**
   * Undo a change GitHub made: the inverse, under everything the change was made under, never
   * renewed. A PR the page took out of its list may be undone; one listed otherwise may not.
   */
  function undo(
    target: LifecycleTarget,
    id: RowLifecycleActionId,
    name: string,
    madeAs: LifecycleCapture,
    heldKey: string,
  ) {
    const inverse = INVERSE[id];
    if (!inverse) return;
    const capture: LifecycleCapture = {
      ...madeAs,
      operation: LIFECYCLE_ACTIONS[inverse].operation,
    };
    if (!captureMatches(capture, owner)) {
      toast(`${pullRequestToastName(target)} wasn’t changed back: the account changed since.`, {
        id: toastIdOf(heldKey),
        ...TIMED_TOAST,
      });
      return;
    }
    const reason = whyNot(target, inverse, true);
    if (reason) {
      toast(`${pullRequestToastName(target)} wasn’t changed back: ${reason}`, {
        id: toastIdOf(heldKey),
        ...TIMED_TOAST,
      });
      return;
    }
    const attempt = attemptFor(target, name, true);
    void sendLifecycleChange(target, inverse, { capture }, changes, attempt).then((outcome) =>
      latest.current.report(outcome, target, inverse, name, {
        capture,
        evenUnlisted: true,
        heldKey,
      }),
    );
  }

  // The Enable dialog this list has open: bound to the account, PR and head it was asked for.
  const [enabling, setEnabling] = useState<EnableConfirmation | null>(null);
  const enablingRef = useRef(enabling);
  enablingRef.current = enabling;
  // What an open question was asked under: any change retires it, even one that changes back.
  const questionKey = JSON.stringify([owner, scope, conversationId, enabled]);
  const question = useRef({ key: questionKey, epoch: 0 });
  if (question.current.key !== questionKey) {
    question.current = { key: questionKey, epoch: question.current.epoch + 1 };
  }
  const questionEpoch = question.current.epoch;
  useEffect(() => {
    setEnabling((open) => (open && open.epoch !== questionEpoch ? null : open));
  }, [questionEpoch]);

  /** Open the dialog for `ask`, at the head its PR is listed at now. */
  function askToEnable(ask: Omit<EnableConfirmation, "headSha" | "epoch">) {
    const headSha = listedRow(ask.target.prUrl)?.pr?.head_sha;
    if (!headSha || !mounted.current) return;
    setEnabling({ ...ask, headSha, epoch: question.current.epoch });
  }

  /**
   * Turn it on with `method`, from the dialog's own read of `options`: only while the PR is
   * listed as it was asked for, at the same head, and as the account the dialog was opened as.
   */
  function confirmEnable(method: PullRequestMergeMethod, options: PullRequestMergeOptions) {
    const asked = enablingRef.current;
    setEnabling(null);
    // A question asked under another account, list or scope says nothing about this one.
    if (!asked || asked.epoch !== question.current.epoch || !mounted.current) return;
    if (!options.methods.includes(method)) return;
    const { target, name, capture, headSha, evenUnlisted } = asked;
    const reason =
      whyNot(target, "enable_auto_merge", evenUnlisted) ??
      (listedRow(target.prUrl)?.pr?.head_sha === headSha
        ? null
        : "its branch moved since. Choose Enable Auto-merge again to turn it on at the new head.");
    if (reason) {
      toast(`${pullRequestToastName(target)} wasn’t changed: ${reason}`, {
        id: toastIdOf(target.key),
        ...TIMED_TOAST,
      });
      return;
    }
    const attempt = attemptFor(target, name, evenUnlisted);
    void sendLifecycleChange(
      target,
      "enable_auto_merge",
      { capture, headSha, method },
      changes,
      attempt,
    ).then((outcome) =>
      latest.current.report(outcome, target, "enable_auto_merge", name, {
        capture,
        evenUnlisted,
        heldKey: target.key,
      }),
    );
  }

  /** After an unknown outcome: a fresh read as the account it was made as, never a resend. */
  function checkAgain(target: LifecycleTarget) {
    const toastId = toastIdOf(target.key);
    void checkLifecycleChangeAgain(queryClient, conversationId, target.key, target.prUrl).then(
      (result) => {
        if (result === "failed")
          toast.error(
            `${pullRequestToastName(target)}: Couldn’t read it from GitHub. Open it to check.`,
            {
              id: toastId,
              duration: Number.POSITIVE_INFINITY,
              action: {
                label: "Open",
                onClick: () => window.open(target.prUrl, "_blank", "noreferrer"),
              },
            },
          );
        // Confirmed, nothing to check, or the account changed while it read (stale): the
        // hold stays where it wasn't confirmed, and the row offers Check Again.
        else toast.dismiss(toastId);
      },
    );
  }

  // Outcomes report, and Check Again reads, as this render offers them; whatever writes runs
  // through the list mounted now.
  const latest = useRef({ report, start, checkAgain });
  latest.current = { report, start, checkAgain };
  LIVE_LISTS.usePublish(scope, {
    lists: (url) => listedRow(url) !== undefined,
    retry,
    undo,
  });

  const actionsFor = useCallback(
    (row: LifecycleRow): RowAction[] => {
      if (!enabled) return [];
      void hostRoute;
      const read = rowFacts(row.url, owner, mergesOnGitHub(row.url), conversationId);
      const facts = { ...read, onGitHub: row.onGitHub };
      const key = stateChainKey(owner, row.url);
      const readOnlyReason = readOnlyHostWriteReason(row.url);
      const actions = ROW_LIFECYCLE_ACTIONS.flatMap((id): RowAction[] => {
        const found = rowLifecycleAvailability(id, row, facts);
        // This app can't change a read-only host's PR: each change shows, unavailable, saying
        // where; the row's own Open link takes it there.
        const availability: LifecycleAvailability =
          readOnlyReason && found.kind !== "hidden"
            ? { kind: "held", reason: readOnlyReason }
            : found;
        const definition = LIFECYCLE_ACTIONS[id];
        const base = {
          id: `lifecycle:${id}` as const,
          section: "lifecycle" as const,
          icon: ROW_LIFECYCLE_ICONS[id],
          keywords: ROW_LIFECYCLE_KEYWORDS,
        };
        switch (availability.kind) {
          case "hidden":
            return [];
          case "github":
            return [
              {
                ...base,
                label: definition.githubLabel,
                paletteLabel: definition.namedGithubLabel(row.paletteName),
                href: availability.url,
                run: () => window.open(availability.url, "_blank", "noreferrer"),
              },
            ];
          case "held":
          case "mac":
            return [
              {
                ...base,
                label: ROW_COPY[id].label,
                paletteLabel: ROW_COPY[id].paletteLabel(row.paletteName),
                availability:
                  availability.kind === "held"
                    ? unavailableBecause(availability.reason)
                    : AVAILABLE,
                run: () => {
                  if (mounted.current) latest.current.start(row, id);
                  else listClosed(key, `${pullRequestToastName(row)} wasn’t changed`);
                },
              },
            ];
        }
      });
      // A change whose outcome is unknown holds the row until it's checked; offer that here.
      const notice = lifecycleActionState(key).notice;
      if (notice?.outcome.kind === "unknown") {
        const target: LifecycleTarget = { key, owner, prUrl: row.url, number: row.number };
        actions.push({
          id: "lifecycle:check-again",
          section: "lifecycle",
          label: "Check Again",
          paletteLabel: `Check ${row.paletteName} Again`,
          icon: RefreshCwIcon,
          keywords: [...ROW_LIFECYCLE_KEYWORDS, "check", "unknown"],
          availability: lifecycleActionState(key).checking
            ? unavailableBecause("It’s being checked now.")
            : AVAILABLE,
          run: () => latest.current.checkAgain(target),
        });
      }
      return actions;
    },
    // storeVersion redraws the rows' holds as either store changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conversationId, enabled, hostRoute, owner, scope, storeVersion, mergesOnGitHub],
  );

  // GitHub merging the PR with its stack retires an unsent question, for good.
  const enablingHandsOff = !!enabling && mergesOnGitHub(enabling.target.prUrl);
  if (enablingHandsOff) setEnabling(null);
  const dialog =
    enabled && enabling && !enablingHandsOff && enabling.epoch === questionEpoch ? (
      <RowEnableAutoMergeDialog
        conversationId={conversationId}
        prUrl={enabling.target.prUrl}
        number={enabling.target.number}
        headSha={enabling.headSha}
        onConfirm={confirmEnable}
        onCancel={() => setEnabling(null)}
      />
    ) : null;

  return { actionsFor, dialog };
}
