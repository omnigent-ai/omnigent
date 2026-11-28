// Convert to draft, Ready for review and Enable and Disable auto-merge: each defined once
// (labels, when it applies, whether it asks first), so the state pill's menu, the Overview
// link, the merge bar and ⌘K can't drift. What each came to is kept per workspace user and
// pull request, outside any component, so it outlives a tab switch.

import { createContext, useSyncExternalStore } from "react";

import type { PullRequestMergeMethod } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  type LifecycleStoreOperation,
  registerUnconfirmedStateChanges,
  unconfirmedStateChangesChanged,
} from "@/lib/pullRequestStateHolds";

import type { HostChangeRoute } from "./pullRequestPageRoute";
import type { StateChangeCapture } from "./usePullRequestStateAction";

export type LifecycleActionId =
  "convert_to_draft" | "ready_for_review" | "enable_auto_merge" | "disable_auto_merge";

/** The two auto-merge changes, which only a read of auto-merge itself can settle. */
export type AutoMergeActionId = Extract<
  LifecycleActionId,
  "enable_auto_merge" | "disable_auto_merge"
>;

export const isAutoMergeAction = (id: LifecycleActionId): id is AutoMergeActionId =>
  id === "enable_auto_merge" || id === "disable_auto_merge";

export interface LifecycleActionDefinition {
  operation: Extract<
    LifecycleStoreOperation,
    | "pull_request_convert_to_draft"
    | "pull_request_ready_for_review"
    | "pull_request_auto_merge_enable"
    | "pull_request_auto_merge_disable"
  >;
  /** The menu item's and button's label. */
  label: string;
  /** The ⌘K row's label. */
  paletteLabel: string;
  /** ⌘K's label for a list row, which names the PR, e.g. "#12 Fix the build". */
  namedPaletteLabel: (name: string) => string;
  /** Where the page can't make it: the item that opens the PR on GitHub instead. */
  githubLabel: string;
  /** ⌘K's label for that item on a list row. */
  namedGithubLabel: (name: string) => string;
  /** Asks first, in a dialog: converting hides it from reviewers, auto-merge merges later. */
  confirms: boolean;
  /** Said while it's on its way to GitHub, e.g. by the header's state pill. */
  running: string;
  done: string;
  failed: string;
  /** Said when GitHub may have made it, before Check Again. */
  maybe: string;
  /** Its notice's name in ⌘K, as "Dismiss … Notice". */
  noticeName: string;
}

export const LIFECYCLE_ACTIONS: Record<LifecycleActionId, LifecycleActionDefinition> = {
  convert_to_draft: {
    operation: "pull_request_convert_to_draft",
    label: "Convert to draft…",
    paletteLabel: "Convert Pull Request to Draft…",
    namedPaletteLabel: (name) => `Convert ${name} to Draft…`,
    githubLabel: "Convert to draft on GitHub",
    namedGithubLabel: (name) => `Convert ${name} to Draft on GitHub`,
    confirms: true,
    running: "Converting to draft…",
    done: "Converted to draft",
    failed: "Couldn’t convert to a draft",
    maybe: "GitHub may have converted this pull request to a draft.",
    noticeName: "Convert to Draft",
  },
  ready_for_review: {
    operation: "pull_request_ready_for_review",
    label: "Ready for review",
    paletteLabel: "Mark Pull Request Ready for Review",
    namedPaletteLabel: (name) => `Mark ${name} Ready for Review`,
    githubLabel: "Mark ready for review on GitHub",
    namedGithubLabel: (name) => `Mark ${name} Ready for Review on GitHub`,
    confirms: false,
    running: "Marking ready for review…",
    done: "Ready for review",
    failed: "Couldn’t mark ready for review",
    maybe: "GitHub may have marked this pull request ready for review.",
    noticeName: "Ready for Review",
  },
  enable_auto_merge: {
    operation: "pull_request_auto_merge_enable",
    label: "Enable auto-merge",
    paletteLabel: "Enable Auto-merge…",
    namedPaletteLabel: (name) => `Enable Auto-merge for ${name}…`,
    githubLabel: "Enable auto-merge on GitHub",
    namedGithubLabel: (name) => `Enable Auto-merge for ${name} on GitHub`,
    confirms: true,
    running: "Turning on auto-merge…",
    done: "Auto-merge turned on",
    failed: "Couldn’t turn on auto-merge",
    maybe: "GitHub may have turned on auto-merge for this pull request.",
    noticeName: "Auto-merge",
  },
  disable_auto_merge: {
    operation: "pull_request_auto_merge_disable",
    label: "Disable auto-merge",
    paletteLabel: "Disable Auto-merge",
    namedPaletteLabel: (name) => `Disable Auto-merge for ${name}`,
    githubLabel: "Disable auto-merge on GitHub",
    namedGithubLabel: (name) => `Disable Auto-merge for ${name} on GitHub`,
    confirms: false,
    running: "Turning off auto-merge…",
    done: "Auto-merge turned off",
    failed: "Couldn’t turn off auto-merge",
    maybe: "GitHub may have turned off auto-merge for this pull request.",
    noticeName: "Auto-merge",
  },
};

export const LIFECYCLE_ACTION_IDS = Object.keys(LIFECYCLE_ACTIONS) as LifecycleActionId[];

/** The message the merge bar shows when GitHub says the PR can merge now. */
export const MERGEABLE_NOW_MESSAGE = "This pull request can merge now. Use Merge instead.";

/**
 * Where an action stands for one PR:
 * - `mac`: the page makes it.
 * - `held`: the page would, but another state change is running or its outcome is unknown.
 * - `github`: it applies, but the page can't make it here; GitHub can.
 * - `hidden`: it doesn't apply to this PR now, or neither the page nor GitHub can make it.
 */
export type LifecycleAvailability =
  | { kind: "mac" }
  | { kind: "held"; reason: string }
  | { kind: "github"; url: string }
  | { kind: "hidden" };

/** What the page knows about the PR, read by every entry point in the same render. */
export interface LifecycleFacts {
  prUrl: string;
  state: string | null | undefined;
  isDraft: boolean;
  /** Whether merging is offered from here at all (the merge bar shows). */
  mergeable: boolean;
  /** GitHub merges this PR's stack together, on GitHub: nothing arms a merge from here. */
  mergesOnGitHub: boolean;
  headSha: string | null | undefined;
  autoMergeOn: boolean;
  /** The merge read the merge bar shares, or null where there is none (yet). */
  macMerge: {
    queue: boolean | null | undefined;
    mergeState: string | null | undefined;
    defaultMethod: PullRequestMergeMethod | null;
  } | null;
  /** False for another provider's PR, such as Azure DevOps', which GitHub can't change. */
  onGitHub?: boolean;
  offered: (operation: LifecycleDefinitionOperation) => boolean;
  held: boolean;
}

type LifecycleDefinitionOperation = LifecycleActionDefinition["operation"];

// The states GitHub turns auto-merge on for; one that can merge now is merged instead.
const AUTO_MERGE_STATES = new Set(["BLOCKED", "BEHIND"]);
export const LIFECYCLE_HELD_REASON =
  "Another change to this pull request is still being made, or needs checking.";

/** Whether `id` applies to the PR, before asking who can make it. */
function applies(id: LifecycleActionId, facts: LifecycleFacts): boolean {
  if (facts.state?.toUpperCase() !== "OPEN") return false;
  switch (id) {
    case "convert_to_draft":
      return !facts.isDraft;
    case "ready_for_review":
      return facts.isDraft;
    case "disable_auto_merge":
      return facts.autoMergeOn;
    case "enable_auto_merge": {
      // Only from the merge read: it alone says the base has no queue.
      const merge = facts.macMerge;
      return (
        facts.mergeable &&
        !facts.mergesOnGitHub &&
        !facts.isDraft &&
        !facts.autoMergeOn &&
        !!facts.headSha &&
        merge !== null &&
        merge.queue === false &&
        !!merge.defaultMethod &&
        AUTO_MERGE_STATES.has(merge.mergeState ?? "")
      );
    }
  }
}

/** Where the page can't make a change: GitHub's own page, or nothing for another provider's PR. */
export function gitHubHandoff(prUrl: string, onGitHub: boolean | undefined): LifecycleAvailability {
  return onGitHub === false ? { kind: "hidden" } : { kind: "github", url: prUrl };
}

export function lifecycleAvailability(
  id: LifecycleActionId,
  facts: LifecycleFacts,
): LifecycleAvailability {
  if (!applies(id, facts)) return { kind: "hidden" };
  const definition = LIFECYCLE_ACTIONS[id];
  if (!facts.offered(definition.operation)) {
    return gitHubHandoff(facts.prUrl, facts.onGitHub);
  }
  return facts.held ? { kind: "held", reason: LIFECYCLE_HELD_REASON } : { kind: "mac" };
}

/** What a change left for its user to see, until dismissed or a fresh read settles it. */
export interface LifecycleNotice {
  id: LifecycleActionId;
  /** The send that left it: whatever started that send knows it by this. */
  attempt: string;
  /** The workspace user and PR it was for. */
  owner: string;
  prUrl: string;
  /**
   * The account it was sent as: only a read as that account can settle an unknown outcome.
   */
  madeAs: {
    login: string;
    generation: number;
    /** The session's host the change was made through. */
    host: HostChangeRoute;
  };
  /** Everything the click bound to, which a Try Again must still be, account included. */
  boundTo: Omit<
    StateChangeCapture<LifecycleActionDefinition["operation"]>,
    "prUrl" | "owner" | "operation"
  >;
  outcome:
    | { kind: "refused"; code: string; message: string }
    /** GitHub may have made it: nothing more is sent until a fresh read. */
    | { kind: "unknown"; message: string }
    | { kind: "abandoned" }
    /** What the click captured no longer held, so nothing was sent. */
    | { kind: "skipped" }
    /**
     * Auto-merge only: Check Again read it the other way from the change. That's where it is
     * now, which can't say whether the change was made and then undone.
     */
    | { kind: "checked"; autoMergeOn: boolean };
}

/**
 * Which page surface (one pane's page) asked: its dialog shows there alone, and leaves when
 * that surface stops being active. Null outside a page, where nothing asks first.
 */
export const LifecycleSurfaceContext = createContext<string | null>(null);

/** A confirmation the user opened, bound to what they saw when they opened it. */
export interface LifecycleConfirmation {
  id: Extract<LifecycleActionId, "convert_to_draft" | "enable_auto_merge">;
  /** The surface it was opened on. */
  surface: string;
  capture: StateChangeCapture<LifecycleDefinitionOperation>;
  /** Auto-merge only: the head and method shown, which the change is pinned to. */
  headSha?: string;
  method?: PullRequestMergeMethod;
}

export interface LifecycleActionState {
  running: LifecycleActionId | null;
  notice: LifecycleNotice | null;
  confirming: LifecycleConfirmation | null;
  /** A Check Again read is under way, from whichever entry point asked. */
  checking: boolean;
  /** The last Check Again couldn't read where the PR stands, so the hold stays. */
  checkFailed: boolean;
}

const IDLE: LifecycleActionState = {
  running: null,
  notice: null,
  confirming: null,
  checking: false,
  checkFailed: false,
};
const states = new Map<string, LifecycleActionState>();
const listeners = new Set<() => void>();

export function lifecycleActionState(key: string): LifecycleActionState {
  return states.get(key) ?? IDLE;
}

export function updateLifecycleActionState(
  key: string,
  change: (current: LifecycleActionState) => LifecycleActionState,
): void {
  const current = lifecycleActionState(key);
  const changed = change(current);
  // Nothing changed, as when a surface that left had no confirmation open: no one re-renders.
  if (changed === current) return;
  // A check's failure belongs to the unknown outcome it checked; it leaves with it.
  const next =
    changed.notice?.outcome.kind === "unknown" ? changed : { ...changed, checkFailed: false };
  if (!next.running && !next.notice && !next.confirming && !next.checking) states.delete(key);
  else states.set(key, next);
  for (const listener of listeners) listener();
  unconfirmedStateChangesChanged();
}

// An unknown outcome holds the PR's merge, auto-merge, queue and state changes, wherever
// they're asked for, until a fresh read settles it.
registerUnconfirmedStateChanges(function* unknownLifecycleChanges() {
  for (const state of states.values()) {
    const notice = state.notice;
    if (notice?.outcome.kind === "unknown") yield { owner: notice.owner, prUrl: notice.prUrl };
  }
});

/** Whether one of these changes holds the PR's other state changes (close, reopen) too. */
export function lifecycleActionsHold(key: string): boolean {
  const state = lifecycleActionState(key);
  return state.running !== null || state.notice?.outcome.kind === "unknown";
}

/** Every PR's notice now, with its key, for a list of what's left to see. */
export function lifecycleNotices(): { key: string; notice: LifecycleNotice }[] {
  return [...states].flatMap(([key, state]) =>
    state.notice ? [{ key, notice: state.notice }] : [],
  );
}

export function subscribeLifecycleActions(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useLifecycleActionState(key: string): LifecycleActionState {
  return useSyncExternalStore(
    subscribeLifecycleActions,
    () => lifecycleActionState(key),
    () => IDLE,
  );
}

/** The notice's sentence, as Overview shows it. */
export function lifecycleNoticeMessage(notice: LifecycleNotice): string {
  const definition = LIFECYCLE_ACTIONS[notice.id];
  switch (notice.outcome.kind) {
    case "refused":
      return `${definition.failed}: ${notice.outcome.message}`;
    case "unknown":
      return `${definition.maybe} Check it before trying again.`;
    case "abandoned":
      return `The workspace account changed before this finished. Check the pull request on GitHub.`;
    case "skipped":
      return `${definition.failed}, because the account or the pull request changed. Nothing was sent.`;
    case "checked":
      return `Auto-merge is currently ${notice.outcome.autoMergeOn ? "on" : "off"}.`;
  }
}

/** What a notice offers, for its buttons and ⌘K rows alike. */
export type LifecycleNoticeAction = "check" | "open" | "dismiss";

export function lifecycleNoticeActions(notice: LifecycleNotice): LifecycleNoticeAction[] {
  // Nothing more is sent until a fresh read; the notice can't be dismissed until then.
  return notice.outcome.kind === "unknown" ? ["check", "open"] : ["open", "dismiss"];
}

/** The label of a notice's action, as its button and its ⌘K row both show it. */
export function lifecycleNoticeActionLabel(
  notice: LifecycleNotice,
  action: LifecycleNoticeAction,
  palette: boolean,
): string {
  switch (action) {
    case "check":
      return palette ? "Check Pull Request Again" : "Check Again";
    case "open":
      return palette ? "Open Pull Request on GitHub" : "Open on GitHub";
    case "dismiss":
      return palette ? `Dismiss ${LIFECYCLE_ACTIONS[notice.id].noticeName} Notice` : "Dismiss";
  }
}

/** Whether `notice` is the merge bar's to show: GitHub said the PR can merge now. */
export function mergeableNowNotice(notice: LifecycleNotice | null): boolean {
  return (
    notice?.id === "enable_auto_merge" &&
    notice.outcome.kind === "refused" &&
    notice.outcome.code === "mergeable_now"
  );
}

/** Forget every PR's state, for tests. */
export function resetLifecycleActionsForTests(): void {
  states.clear();
  for (const listener of listeners) listener();
  unconfirmedStateChangesChanged();
}
