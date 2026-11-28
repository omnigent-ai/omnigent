// A pull request tab's new page: the session's pull requests and one of them, folded or side
// by side, shown where the session's route serves the PR methods.

import { type RefObject, useCallback, useEffect, useId, useMemo, useRef } from "react";

import { PaneResizeHandle } from "@/components/PaneResizeHandle";
import { Button } from "@/components/ui/button";
import { useElementWidth } from "@/hooks/useElementWidth";
import {
  focusSectionControl,
  SECTION_PICKER_ATTRIBUTE,
} from "@/pullRequestPage/PullRequestPageHeader";
import { useShowPullRequests } from "@/shell/FileViewerContext";
import type { PullRequestTabsMode } from "@/newPullRequestPage/shell/PullRequestPanel";
import { usePullRequestListToggleCommand } from "@/shell/PullRequestListToggle";
import type { PullRequestPanelView, PullRequestSubtab } from "@/shell/pullRequestPanelView";
import { SidebarPullRequestPanel } from "@/shell/SidebarPullRequestPanel";

import { samePlace, type OpenOptions } from "../planOpen";
import type { WorkspaceLink, WorkspacePlace } from "../workspacePlace";
import type { PlaceViewAction, PullRequestPlace } from "./placeViewActions";
import {
  chosenPullRequestSection,
  listedPullRequestChosen,
  pullRequestListAdded,
  pullRequestListShown,
  pullRequestNewTabPlace,
  pullRequestPanelViewFor,
  pullRequestViewChanged,
  shownPullRequestPlace,
  unshownPullRequestParts,
} from "./pullRequestPlaceActions";
import {
  pullRequestListChosen,
  pullRequestListShownNow,
  pullRequestOpening,
  usePullRequestListShown,
} from "./pullRequestListBeside";
import {
  PULL_REQUEST_LIST_BESIDE_MIN_WIDTH,
  usePullRequestListWidth,
} from "./usePullRequestListWidth";

export interface PullRequestPlaceViewProps {
  conversationId: string;
  place: PullRequestPlace;
  /** The tab is selected and the sidebar is open. */
  active: boolean;
  /** Move this tab to `place`: a step in its history, or a correction with `replace`. */
  onNavigate: (place: WorkspacePlace, options: { replace: boolean }) => void;
  /** Open a place elsewhere through the tab store's open rules. */
  onOpen: (link: WorkspaceLink, options: OpenOptions) => void;
}

function useRunAction(
  onNavigate: PullRequestPlaceViewProps["onNavigate"],
  onOpen: PullRequestPlaceViewProps["onOpen"],
) {
  return useCallback(
    (action: PlaceViewAction) => {
      if (action.type === "navigate") onNavigate(action.place, { replace: action.replace });
      else if (action.type === "open") onOpen(action.link, action.options);
    },
    [onNavigate, onOpen],
  );
}

export function PullRequestPlaceView({
  conversationId,
  place,
  active,
  onNavigate,
  onOpen,
}: PullRequestPlaceViewProps) {
  const run = useRunAction(onNavigate, onOpen);
  const view = useMemo(() => pullRequestPanelViewFor(place), [place]);
  // The pull request's own panel, which the list's rows open into, folded or beside it.
  const detailDestination = useId();
  const unshown = unshownPullRequestParts(place);
  const showPullRequests = useShowPullRequests();
  const pullUrl = place.kind === "pull" ? place.url : undefined;
  const listed = place.kind === "pull" && place.list === true;
  const containerRef = useRef<HTMLDivElement>(null);
  const listSlotRef = useRef<HTMLDivElement>(null);
  const detailsSlotRef = useRef<HTMLDivElement>(null);
  const containerWidth = useElementWidth(containerRef);
  const listFits = containerWidth >= PULL_REQUEST_LIST_BESIDE_MIN_WIDTH;
  const listWidth = usePullRequestListWidth(containerRef, containerWidth);
  const listHidden = !usePullRequestListShown(conversationId);
  const listShown = listed && listFits && !listHidden;
  const requestFocus = useListFocusHandoff(listSlotRef, detailsSlotRef, place, listShown);
  // The tabs panel passes a new opener each render; reading it late keeps the mode stable.
  const latestOnOpen = useRef(onOpen);
  latestOnOpen.current = onOpen;
  const latestRun = useRef(run);
  latestRun.current = run;
  const latestPlace = useRef(place);
  latestPlace.current = place;
  // The section the tab's pull request showed last, for the next one chosen while only the list shows.
  const lastPullRequestSection = useRef<PullRequestSubtab>("summary");
  useEffect(() => {
    if (place.kind === "pull") lastPullRequestSection.current = place.view;
  }, [place]);
  const latestListFits = useRef(false);
  latestListFits.current = listFits;
  // A plain click shows the pull request here, beside the list; ⌘-click opens a tab behind.
  const openFromList = useCallback(
    (url: string, options?: { background?: boolean; foreground?: true; linkedCount?: number }) => {
      if (!showPullRequests) return;
      // ⌘-click opens its tab behind the list, ⇧⌘-click opens or selects it in front.
      if (options?.background || options?.foreground) {
        const section = chosenPullRequestSection(
          latestPlace.current,
          lastPullRequestSection.current,
        );
        showPullRequests(
          { kind: "pull", url, view: section },
          { classic: "stay", background: !!options.background },
        );
        return;
      }
      const step = listedPullRequestChosen(
        latestPlace.current,
        url,
        lastPullRequestSection.current,
      );
      // How many PRs the list shows as linked decides whether opening one folds it.
      if (step.type !== "none") pullRequestOpening(conversationId, options?.linkedCount);
      // Where focus lands once the open settled whether the list stays, such as folded for the only PR.
      const listStays = latestListFits.current && pullRequestListShownNow(conversationId);
      // Choosing the one on screen is no step, so no render would place focus after it.
      requestFocus(listStays ? "list" : "details", { now: step.type === "none" });
      latestRun.current(step);
    },
    [showPullRequests, requestFocus, conversationId],
  );
  const toggleList = useCallback(() => {
    // A tab opened on one pull request, from a link or ⌘-click, gains the list when shown.
    if (!listed) {
      pullRequestListChosen(conversationId, true);
      latestRun.current(pullRequestListAdded(latestPlace.current));
      return;
    }
    // Hiding the list from ⌘K takes the row that had focus with it.
    if (!listHidden) requestFocus("details");
    pullRequestListChosen(conversationId, listHidden);
  }, [listed, listHidden, requestFocus, conversationId]);
  // Each pull request has its own tab, and all of them one more, unless the list shows them here.
  const tabsMode = useMemo<PullRequestTabsMode | undefined>(() => {
    if (!showPullRequests) return undefined;
    if (place.kind === "pulls") {
      return {
        kind: "overview",
        onOpenPullRequest: openFromList,
      };
    }
    return {
      kind: "pull",
      onShowAllPullRequests: listed
        ? () => {
            requestFocus("list");
            latestRun.current(pullRequestListShown());
          }
        : () => showPullRequests({ kind: "overview" }, { classic: "stay" }),
      listToggle:
        place.kind === "pull" && listFits ? { shown: listShown, onToggle: toggleList } : undefined,
      onOpenInNewTab: (target, { background }) => {
        if (pullUrl === undefined) return;
        latestOnOpen.current(
          { place: pullRequestNewTabPlace(pullUrl, target) },
          {
            opener: "user",
            disposition: "focus",
            reuse: "same-place",
            keepSelection: background,
          },
        );
      },
    };
  }, [
    place.kind,
    pullUrl,
    showPullRequests,
    requestFocus,
    listed,
    listFits,
    listShown,
    openFromList,
    toggleList,
  ]);
  usePullRequestListToggleCommand(
    tabsMode?.kind === "pull" && active ? tabsMode.listToggle : undefined,
  );
  const listTabsMode = useMemo<PullRequestTabsMode>(
    () => ({ kind: "overview", onOpenPullRequest: openFromList }),
    [openFromList],
  );
  const listView = useMemo<PullRequestPanelView>(
    () => ({ selected: pullUrl, showingDetails: false, subtab: "summary" }),
    [pullUrl],
  );
  // Stable, so the panel skips the renders a sidebar toggle gives this view.
  const onViewChange = useCallback(
    (next: PullRequestPanelView, { replace }: { replace: boolean }) =>
      latestRun.current(pullRequestViewChanged(latestPlace.current, next, replace, samePlace)),
    [],
  );
  // The overview and the list beside a pull request are one mount, so opening a row from the
  // keyboard keeps the list, its active row and its focus.
  const list =
    place.kind === "pulls"
      ? { view, onViewChange, tabsMode, beside: false }
      : listShown
        ? {
            view: listView,
            onViewChange: ignoreListViewChange,
            tabsMode: listTabsMode,
            beside: true,
          }
        : undefined;
  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="workspace-pull-request-place"
      // A two-finger swipe in a sidebar PR tab steps the sidebar's history, as ⌘← / ⌘→ do here.
      data-swipe-history="sidebar"
    >
      {unshown && (
        <div
          role="status"
          className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/50 px-3 py-1.5 text-ui text-muted-foreground"
        >
          <span className="min-w-0 flex-1">{unshown}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onNavigate(shownPullRequestPlace(place), { replace: true })}
          >
            Show what&apos;s here
          </Button>
        </div>
      )}
      <div ref={containerRef} className="flex min-h-0 flex-1">
        {list && (
          <div
            ref={listSlotRef}
            className={
              list.beside
                ? "relative flex min-h-0 shrink-0 flex-col border-r border-border"
                : "flex min-h-0 min-w-0 flex-1 flex-col"
            }
            style={list.beside ? { width: listWidth.width } : undefined}
            data-testid={list.beside ? "workspace-pull-request-list-beside" : undefined}
          >
            {list.beside && <PaneResizeHandle resize={listWidth} />}
            <SidebarPullRequestPanel
              conversationId={conversationId}
              active={active}
              view={list.view}
              onViewChange={list.onViewChange}
              tabsMode={list.tabsMode}
              rowDestination={detailDestination}
            />
          </div>
        )}
        {place.kind === "pull" && (
          <div ref={detailsSlotRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
            <SidebarPullRequestPanel
              conversationId={conversationId}
              active={active}
              view={view}
              onViewChange={onViewChange}
              tabsMode={tabsMode}
              destination={detailDestination}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Where focus lands once a step from the list has rendered, whether a row, its menu or ⌘K took
 * it: the list's grid while the list stays, else the pull request's section control.
 */
function useListFocusHandoff(
  listSlotRef: RefObject<HTMLDivElement | null>,
  detailsSlotRef: RefObject<HTMLDivElement | null>,
  place: PullRequestPlace,
  listShown: boolean,
) {
  // The step a request awaits: it lands once the place or the list's visibility changes,
  // since the tab's navigation may render other updates first.
  const pending = useRef<{ target: FocusTarget; place: PullRequestPlace; listShown: boolean }>(
    null,
  );
  const stopWaiting = useRef<() => void>(() => {});
  const latest = useRef({ place, listShown });
  latest.current = { place, listShown };
  const focusTarget = useCallback(
    (target: FocusTarget) => {
      stopWaiting.current();
      const slot = (target === "list" ? listSlotRef : detailsSlotRef).current;
      if (!slot || slot.contains(document.activeElement)) return;
      stopWaiting.current = focusWhenReady(
        slot,
        target === "list" ? focusGrid : focusSectionControl,
      );
    },
    [listSlotRef, detailsSlotRef],
  );
  useEffect(() => () => stopWaiting.current(), []);
  const request = useCallback(
    (target: FocusTarget, { now = false }: { now?: boolean } = {}) => {
      if (now) focusTarget(target);
      else pending.current = { target, ...latest.current };
    },
    [focusTarget],
  );
  const wasListShown = useRef(listShown);
  useEffect(() => {
    // A workspace narrowing past the list drops it, and with it a focused row.
    const listDropped = wasListShown.current && !listShown;
    wasListShown.current = listShown;
    const focused = document.activeElement;
    if (listDropped && pending.current === null && (!focused || focused === document.body)) {
      focusTarget("details");
      return;
    }
    const step = pending.current;
    if (step === null || (step.place === place && step.listShown === listShown)) return;
    pending.current = null;
    focusTarget(step.target);
  });
  return request;
}

type FocusTarget = "list" | "details";

/**
 * Focus `slot`'s control through `focus`, and keep it there while the slot loads: its first
 * button stands in until the control renders, and a control the load replaces is focused
 * again. Focus the user moves anywhere else ends this for good, as does the returned stop
 * function, which the next step or unmount calls.
 */
function focusWhenReady(slot: HTMLElement, focus: (root: HTMLElement) => boolean): () => void {
  let placed: Element | null = null;
  let placing = false;
  const place = () => {
    placing = true;
    try {
      if (focus(slot)) placed = document.activeElement;
      else if (placed === null || !placed.isConnected) {
        const standIn = slot.querySelector<HTMLElement>("button");
        standIn?.focus();
        placed = standIn && document.activeElement === standIn ? standIn : null;
      }
    } finally {
      placing = false;
    }
  };
  const observer = new MutationObserver(() => {
    const focused = document.activeElement;
    const lost = !focused || focused === document.body || !focused.isConnected;
    // Lost focus comes back; a stand-in gives way to the control once it renders.
    if (lost || (placed !== null && focused === placed && !isSectionOrGrid(placed))) place();
  });
  const onFocusIn = (event: FocusEvent) => {
    if (!placing && event.target !== placed) stop();
  };
  function stop() {
    observer.disconnect();
    document.removeEventListener("focusin", onFocusIn, true);
  }
  place();
  observer.observe(slot, { childList: true, subtree: true });
  document.addEventListener("focusin", onFocusIn, true);
  return stop;
}

function isSectionOrGrid(element: Element): boolean {
  return element.matches(`[${SECTION_PICKER_ATTRIBUTE}], [role="tab"], [role="grid"]`);
}

function focusGrid(root: HTMLElement): boolean {
  const grid = root.querySelector<HTMLElement>('[role="grid"]');
  grid?.focus();
  return !!grid && document.activeElement === grid;
}

/** The list beside a pull request only opens them; it never changes what it shows itself. */
function ignoreListViewChange() {}
