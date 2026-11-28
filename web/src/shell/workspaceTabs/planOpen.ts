// Decides what opening a workspace link does, against a snapshot of one
// session's tabs: which tab it reuses or creates, whether it selects it, which
// dirty tabs the leave guard must ask about, and how history records it. Pure,
// so the tab store can plan, await the guard, and commit only if nothing changed.

import { formatWorkspaceLink } from "./workspaceLink";
import {
  placeIdentity,
  type TabId,
  type WorkspaceLink,
  type WorkspacePlace,
} from "./workspacePlace";

export type Opener = "user" | "link" | "agent" | "restore";
/**
 * `focus` shows the place's tab as it is, or a new one: for the user's own "show me
 * this", such as all pull requests or one pull request's tab, never moving a tab's
 * sub-view. Agents and background opens keep their own rules.
 */
export type Disposition = "auto" | "current" | "new" | "background" | "focus";

export interface OpenOptions {
  opener: Opener;
  disposition?: Disposition;
  /** A click in the Files tab's own tree, which always navigates that tab in place. */
  origin?: "files-tree";
  /** Where a created tab goes: after the selected tab (default), or at the end, as "+" does. */
  position?: "after-selected" | "end";
  /**
   * With `focus`: leave the selection where it is, as a browser's ⌘-click does. The
   * place's tab opens behind the current one, or, if it's already open, stays as it is.
   */
  keepSelection?: boolean;
  /**
   * With `focus`: which open tab counts as already showing the place. `identity`
   * (default) is any tab of the same thing, such as one pull request; `same-place`
   * only another tab showing exactly this place, so one commit or file of a pull
   * request gets its own tab beside the pull request's, even when this tab shows it.
   */
  reuse?: "identity" | "same-place";
}

/** The resource a bound tab owns for its whole life. */
export type TabBinding =
  | { kind: "browser-view"; viewKey: string }
  | { kind: "terminal"; key: string }
  | { kind: "chat"; id?: string }
  | { kind: "agent"; id: string };

export type ResourceState = "live" | "unknown" | "gone";

/** What planning needs to know about one open tab. */
export interface PlanTab {
  id: TabId;
  place: WorkspacePlace;
  binding?: TabBinding;
  role?: "files";
  /** It has unsaved edits. */
  dirty: boolean;
}

export interface OpenState {
  sessionId: string;
  /** In strip order. */
  tabs: readonly PlanTab[];
  selectedTabId: TabId | null;
  /** Tab IDs, most recently selected first. */
  recentlySelected: readonly TabId[];
  /** Live, still loading, or authoritatively gone, for terminal, agent and chat places. */
  resourceState: (place: WorkspacePlace) => ResourceState;
  maxTabs: number;
}

/** How history records the open. */
export type HistoryEffect = "append" | "replace" | "none";

export type OpenPlan =
  | {
      action: "other-session";
      sessionId: string;
      switchSession: boolean;
      link: WorkspaceLink;
      options: OpenOptions;
    }
  | {
      action: "navigate";
      tabId: TabId;
      place: WorkspacePlace;
      select: boolean;
      /** Dirty tabs the leave guard must ask about before committing. */
      guard: TabId[];
      history: HistoryEffect;
    }
  | {
      action: "create";
      place: WorkspacePlace;
      /** The binding kind to create; the store assigns view keys. */
      binding?: TabBinding;
      role?: "files";
      /** Insert after this tab, or at the end when null. */
      after: TabId | null;
      select: boolean;
      history: HistoryEffect;
    }
  | { action: "attention"; tabId: TabId; place: WorkspacePlace };

export type PlanResult = { ok: true; plan: OpenPlan } | { ok: false; reason: string };

export const TAB_CAP_REASON = "Close some workspace tabs to open more.";
export const NO_SUCH_TAB_REASON = "No workspace tab has that ID.";

/** The resource a place needs a bound tab for, or undefined for unbound places. */
export function bindingForPlace(place: WorkspacePlace): TabBinding | undefined {
  switch (place.kind) {
    case "terminal":
      return { kind: "terminal", key: place.key };
    case "agent":
      return { kind: "agent", id: place.id };
    case "chat":
      return place.id === undefined ? { kind: "chat" } : { kind: "chat", id: place.id };
    case "web":
      return { kind: "browser-view", viewKey: "" };
    default:
      return undefined;
  }
}

/** Whether `tab` may show `place`: unbound tabs show unbound places; bound tabs only their own resource. */
export function canShow(tab: PlanTab, place: WorkspacePlace): boolean {
  const needed = bindingForPlace(place);
  if (!tab.binding || !needed) return !tab.binding && !needed;
  if (tab.binding.kind !== needed.kind) return false;
  switch (tab.binding.kind) {
    case "browser-view":
      return true;
    case "terminal":
      return needed.kind === "terminal" && needed.key === tab.binding.key;
    case "agent":
      return needed.kind === "agent" && needed.id === tab.binding.id;
    case "chat":
      return needed.kind === "chat" && needed.id !== undefined && needed.id === tab.binding.id;
  }
}

function refusalFor(place: WorkspacePlace, tab: PlanTab): string {
  switch (tab.binding?.kind) {
    case "terminal":
      return "That tab shows a shell.";
    case "browser-view":
      return "That tab shows a web page.";
    case "chat":
      return "That tab shows a side chat.";
    case "agent":
      return "That tab shows an agent.";
    default:
      return bindingForPlace(place)?.kind === "browser-view"
        ? "Web pages open in a browser tab."
        : "That tab can't show this.";
  }
}

const GONE_REASONS: Partial<Record<WorkspacePlace["kind"], string>> = {
  terminal: "That shell has ended.",
  agent: "That agent is no longer available.",
  chat: "That side chat is no longer available.",
};

/** Semantic equality: two places are the same when their canonical links are. */
export function samePlace(left: WorkspacePlace, right: WorkspacePlace): boolean {
  return formatWorkspaceLink({ place: left }) === formatWorkspaceLink({ place: right });
}

/** The same place, whatever comparison each visit showed, which is its state, not what it is. */
function sameShownPlace(left: WorkspacePlace, right: WorkspacePlace): boolean {
  const withoutComparison = (place: WorkspacePlace): WorkspacePlace => {
    if (place.kind !== "changes") return place;
    const { comparison: _comparison, ...rest } = place;
    return rest;
  };
  return samePlace(withoutComparison(left), withoutComparison(right));
}

/**
 * The place a hidden tab brought forward shows. A Changes visit's old comparison, and the folder
 * that showed it, would undo the session's since; only those the open asks for land with it.
 */
function broughtForward(state: OpenState, tab: PlanTab, asked: WorkspacePlace): WorkspacePlace {
  const shown = tab.place;
  if (shown.kind !== "changes" || tab.id === state.selectedTabId) return shown;
  const { comparison: _recorded, comparisonRoot: _recordedRoot, ...rest } = shown;
  if (asked.kind !== "changes" || asked.comparison === undefined) return rest;
  const place: WorkspacePlace = { ...rest, comparison: asked.comparison };
  if (asked.comparisonRoot !== undefined) place.comparisonRoot = asked.comparisonRoot;
  return place;
}

function withoutPosition(place: WorkspacePlace): WorkspacePlace {
  if (place.kind !== "files" && place.kind !== "changes") return place;
  const { line: _line, comment: _comment, ...rest } = place;
  return rest;
}

/** A move within the same file (line, column, comment) that history records in place. */
function isPositionOnlyMove(from: WorkspacePlace, to: WorkspacePlace): boolean {
  if (from.kind !== "files" && from.kind !== "changes") return false;
  return from.path !== undefined && samePlace(withoutPosition(from), withoutPosition(to));
}

/**
 * Whether moving `tab` to `place` must ask first. Any move of a dirty tab asks,
 * even within the same file: some viewers (rich text) discard edits to show a line.
 */
function needsGuard(tab: PlanTab, place: WorkspacePlace): boolean {
  return tab.dirty && !samePlace(tab.place, place);
}

/**
 * An agent's (background) open of `tab`: shown as attention unless the tab is
 * unselected, clean, and not the Files tab the user browses with; then it moves silently.
 */
function backgroundOpen(state: OpenState, tab: PlanTab, place: WorkspacePlace): OpenPlan {
  if (samePlace(tab.place, place)) return navigate(state, tab, place, false);
  if (tab.id === state.selectedTabId || tab.dirty || tab.role === "files") {
    return { action: "attention", tabId: tab.id, place };
  }
  return navigate(state, tab, place, false);
}

function navigate(
  state: OpenState,
  tab: PlanTab,
  place: WorkspacePlace,
  select: boolean,
): OpenPlan {
  const selected = state.selectedTabId === tab.id;
  let history: HistoryEffect = "none";
  if (select) {
    if (!selected) history = "append";
    else if (samePlace(tab.place, place)) history = "none";
    else history = isPositionOnlyMove(tab.place, place) ? "replace" : "append";
  }
  return {
    action: "navigate",
    tabId: tab.id,
    place,
    select,
    guard: needsGuard(tab, place) ? [tab.id] : [],
    history,
  };
}

function create(
  state: OpenState,
  place: WorkspacePlace,
  select: boolean,
  role?: "files",
  position: OpenOptions["position"] = "after-selected",
): PlanResult {
  if (state.tabs.length >= state.maxTabs) return { ok: false, reason: TAB_CAP_REASON };
  const binding = bindingForPlace(place);
  const plan: OpenPlan = {
    action: "create",
    place,
    after: position === "end" ? null : state.selectedTabId,
    select,
    history: select ? "append" : "none",
  };
  if (binding) plan.binding = binding;
  if (role) plan.role = role;
  return { ok: true, plan };
}

/** Among tabs matching `predicate`, the selected one, else the most recently selected, else the first. */
function preferredTab(state: OpenState, predicate: (tab: PlanTab) => boolean): PlanTab | undefined {
  const matches = state.tabs.filter(predicate);
  if (matches.length <= 1) return matches[0];
  const selected = matches.find((tab) => tab.id === state.selectedTabId);
  if (selected) return selected;
  for (const id of state.recentlySelected) {
    const recent = matches.find((tab) => tab.id === id);
    if (recent) return recent;
  }
  return matches[0];
}

function tabIdentity(tab: PlanTab): string | null {
  return placeIdentity(tab.place);
}

/** The tab this place reuses by identity or binding, if one is open. */
function reusableTab(state: OpenState, place: WorkspacePlace): PlanTab | undefined {
  if (place.kind === "web") {
    return place.url === undefined
      ? undefined
      : preferredTab(
          state,
          (tab) =>
            tab.binding?.kind === "browser-view" &&
            tab.place.kind === "web" &&
            tab.place.url === place.url,
        );
  }
  if (place.kind === "files" && place.path === undefined) {
    return state.tabs.find((tab) => tab.role === "files");
  }
  if (bindingForPlace(place)) return preferredTab(state, (tab) => canShow(tab, place));
  const identity = placeIdentity(place);
  if (identity === null) return undefined;
  return preferredTab(state, (tab) => !tab.binding && tabIdentity(tab) === identity);
}

/**
 * Plan opening `link` in the session `state` describes. Each step decides or
 * falls through; a refusal changes nothing.
 */
export function planOpen(state: OpenState, link: WorkspaceLink, options: OpenOptions): PlanResult {
  const background = options.opener === "agent" || options.disposition === "background";
  const disposition: Disposition = background ? "background" : (options.disposition ?? "auto");
  const effective: OpenOptions = { ...options, disposition };
  const { place } = link;

  // Another session's sidebar: the store re-plans there; only foreground opens switch to it.
  if (link.session !== undefined && link.session !== state.sessionId) {
    const { session: _session, ...rest } = link;
    return {
      ok: true,
      plan: {
        action: "other-session",
        sessionId: link.session,
        switchSession: !background,
        link: rest,
        options: effective,
      },
    };
  }

  if (
    place.kind === "terminal" ||
    place.kind === "agent" ||
    (place.kind === "chat" && place.id !== undefined)
  ) {
    if (state.resourceState(place) === "gone") {
      return { ok: false, reason: GONE_REASONS[place.kind] ?? "That's no longer available." };
    }
  }

  // An explicit tab: it must exist and be able to show the place. No fallback.
  if (link.tab !== undefined) {
    const target = state.tabs.find((tab) => tab.id === link.tab);
    if (!target) return { ok: false, reason: NO_SUCH_TAB_REASON };
    if (!canShow(target, place)) return { ok: false, reason: refusalFor(place, target) };
    if (background) return { ok: true, plan: backgroundOpen(state, target, place) };
    return { ok: true, plan: navigate(state, target, place, true) };
  }

  if (disposition === "new") return create(state, place, true, undefined, options.position);

  if (disposition === "focus") {
    const identity = placeIdentity(place);
    const shown =
      options.reuse === "same-place"
        ? preferredTab(
            state,
            (tab) =>
              tab.id !== state.selectedTabId && !tab.binding && sameShownPlace(tab.place, place),
          )
        : identity === null
          ? undefined
          : preferredTab(state, (tab) => !tab.binding && tabIdentity(tab) === identity);
    // Selecting a tab with its own place keeps its sub-view; an already selected one is no step.
    if (shown) {
      const forward = broughtForward(state, shown, place);
      return { ok: true, plan: navigate(state, shown, forward, !options.keepSelection) };
    }
    return create(state, place, !options.keepSelection);
  }

  const selected = state.tabs.find((tab) => tab.id === state.selectedTabId);

  if (disposition === "current") {
    if (selected && canShow(selected, place))
      return { ok: true, plan: navigate(state, selected, place, true) };
    return create(state, place, true);
  }

  // A click in the Files tab's own tree always shows there.
  if (options.origin === "files-tree" && !background && place.kind === "files") {
    const filesTab = state.tabs.find((tab) => tab.role === "files");
    if (filesTab) return { ok: true, plan: navigate(state, filesTab, place, true) };
    return create(state, place, true, "files");
  }

  const reused = reusableTab(state, place);
  if (reused) {
    if (background) return { ok: true, plan: backgroundOpen(state, reused, place) };
    return { ok: true, plan: navigate(state, reused, place, true) };
  }

  if (
    place.kind === "files" &&
    place.path !== undefined &&
    !background &&
    selected?.role === "files"
  ) {
    return { ok: true, plan: navigate(state, selected, place, true) };
  }

  if (place.kind === "files" && place.path === undefined)
    return create(state, place, !background, "files");
  return create(state, place, !background);
}
