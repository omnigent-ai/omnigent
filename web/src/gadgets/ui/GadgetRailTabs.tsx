// Session gadget tabs and the gadget beside New session.
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MaximizeIcon, PanelsTopLeftIcon, XIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useLocation, useNavigate, useRebasePath, useReplaceSearchParams } from "@/lib/routing";
import { readSessionWorkspaceState, saveSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  GADGET_FULL_SCREEN_PARAM,
  clearGadgetRailRequest,
  gadgetCount,
  noteGadgetFullScreenUnavailable,
  requestGadgetRailTab,
  setFullScreenGadget,
  setGadgetNewSessionTabMaximized,
  useGadgetRailRequest,
  noteGadgetHandoffCover,
} from "@/lib/gadgetRailTabs";
import { cn } from "@/lib/utils";
import { MobilePanelDrawer, useMobilePanelHeaderTarget } from "@/shell/MobilePanelDrawer";
import { SidebarOpenButton } from "@/shell/SidebarOpenButton";
import { scrollTabIntoView } from "@/shell/WorkspaceTabsScroller";
import {
  type Gadget,
  type GadgetContent,
  createGadget,
  deleteGadget,
} from "@/gadgets/api/gadgetApi";
import { composeGadgetBuildPrompt, gadgetNameFrom } from "@/gadgets/host/gadgetBuild";
import { GADGET_START_PARAM, sessionIdFromPath } from "@/gadgets/host/gadgetPages";
import {
  mayHaveBeenDelivered,
  openGadgetSession,
  gadgetIdFromRailKey,
  gadgetRailKey,
} from "@/gadgets/host/gadgetSession";
import { GadgetDescribeForm } from "./GadgetDescribeForm";
import { GadgetTemplatesSection } from "./GadgetTemplateGallery";
import { GadgetActions, GadgetName, GadgetPinButton } from "./GadgetToolbar";
import { GadgetUnavailable } from "./GadgetUnavailable";
import { GadgetView } from "./GadgetView";
import {
  type UnfinishedGadgetBuild,
  setGadgetDraft,
  setGadgetDraftText,
  setUnfinishedGadgetBuild,
  gadgetDraftText,
  unfinishedGadgetBuild,
  useHasGadgetDraft,
  gadgetBuildRun,
} from "./gadgetDrafts";
import {
  gadgetOpenKeepsSidebar,
  useGadgetNavigate,
  type GadgetOpenState,
} from "./gadgetNavigation";
import {
  invalidateGadgets,
  isGadgetUnavailable,
  useLinkedGadgetsSync,
  useSessionGadgetsQuery,
  useShowGadgetInSession,
  useGadgetsEnabled,
  useGadget,
} from "./gadgetQueries";
import { leaveGadgetStart, noteGadgetStartFullScreen } from "./useAdoptStartedGadgetSession";
import { GadgetOpenSessionButton } from "./GadgetOpenSessionButton";

export const GADGET_DRAFT_KEY = "gadget-draft";
const DRAFT_LABEL = "New gadget";

export interface GadgetRailTab {
  /** Unique within the panel, for example `gadget:<id>`. */
  key: string;
  label: string;
}

/** An entry the panel's "Open new" menu shows. */
export interface GadgetRailNewItem {
  key: string;
  label: string;
  icon: typeof PanelsTopLeftIcon;
  onSelect: () => void;
}

/** How a gadget tab is shown. */
export interface GadgetTabView {
  /** Whether it fills the screen, alone in its panel. */
  fullScreen: boolean;
  /** In full screen, the panel's bar where the tab puts its controls; null otherwise. */
  toolbar: HTMLElement | null;
}

export interface GadgetRailTabState {
  tabs: GadgetRailTab[];
  newItems: GadgetRailNewItem[];
  /** The selected tab's key while it still exists, else null. */
  selected: string | null;
  /**
   * Whether the session's gadgets are still loading, so a saved gadget tab can't be judged gone
   * yet.
   */
  loading: boolean;
  select: (key: string) => void;
  close: (key: string) => void;
  /** Whether the viewer can close the tabs. */
  canClose: boolean;
  /** On a phone: whether the selected gadget covers the session. */
  mobileOpen: boolean;
  /** On a phone: shows the session again. */
  closeMobile: () => void;
  render: (key: string, view: GadgetTabView) => ReactNode;
}

const NO_TABS: GadgetRailTab[] = [];
const NO_ITEMS: GadgetRailNewItem[] = [];
// How often a request switches the panel to its tab before giving up.
const MAX_SHOW_ATTEMPTS = 5;
// A shown request is kept this long, in case the session's saved tab restores after it.
const SHOWN_GRACE_MS = 1500;

/** The gadget's tab content: its controls above it, or in the panel's bar in full screen. */
function GadgetRailPane({
  gadget,
  view,
  sessionId,
  onDeleted,
  onOpenSession,
}: {
  gadget: Gadget;
  view: GadgetTabView;
  /** The session it is shown beside; none beside New session. */
  sessionId?: string;
  onDeleted: () => void;
  /** Leaves full screen to show the session. */
  onOpenSession?: () => void;
}) {
  const queryClient = useQueryClient();
  const [content, setContent] = useState<GadgetContent | undefined>(undefined);
  return (
    <GadgetView
      gadget={gadget}
      sessionId={sessionId}
      onReloadGadget={() => invalidateGadgets(queryClient)}
      onContentLoaded={setContent}
      toolbarTarget={view.toolbar}
      fullScreen={view.fullScreen}
      onOpenSession={onOpenSession}
      renderToolbar={(commentsButton) => (
        <>
          <span className="flex min-w-0 flex-1 items-center">
            <GadgetName gadget={gadget} />
          </span>
          <GadgetPinButton gadget={gadget} />
          {commentsButton}
          <GadgetActions
            gadget={gadget}
            content={content?.revision_id === gadget.published_revision ? content : undefined}
            onDeleted={onDeleted}
            shareInMenu={!view.fullScreen}
          />
        </>
      )}
    />
  );
}

// The open New gadget panes' Cancel, by session, for closing the tab to share it.
const draftCancels = new Map<string, () => void>();

/** Builds a new gadget in this session from a description, or makes one from a template. */
function GadgetDraftPane({ sessionId, view }: { sessionId: string; view: GadgetTabView }) {
  const navigate = useGadgetNavigate();
  const showInSession = useShowGadgetInSession();
  // Replaces this tab with the gadget's own.
  const openGadgetTab = (gadget: Gadget) => {
    setGadgetDraft(sessionId, false);
    requestGadgetRailTab(sessionId, gadgetRailKey(gadget.id));
  };
  const show = (gadget: Gadget) =>
    showInSession.mutateAsync({ gadgetId: gadget.id, sessionId, shown: true });
  // A build that failed after creating its gadget: Try again continues with that gadget
  // rather than leave it behind and make another. Kept per session, so it survives a visit to
  // another session.
  const unfinished = {
    get current() {
      return unfinishedGadgetBuild(sessionId);
    },
    set current(build: UnfinishedGadgetBuild | null) {
      setUnfinishedGadgetBuild(sessionId, build);
    },
  };
  // Cancel ends the build in progress: each step checks it is still the current one, and a
  // gadget it made after Cancel is deleted. Once the build message is on its way it can't be
  // taken back, so that step finishes and shows the gadget the session is building.
  const runs = gadgetBuildRun(sessionId);
  // Forgets the failed build, deleting its gadget unless its build message may have reached
  // the session: then the session may be building it, so it stays in the viewer's gadgets.
  const dropUnfinished = () => {
    const abandoned = unfinished.current;
    unfinished.current = null;
    if (abandoned && !abandoned.maybeDelivered) {
      void deleteGadget(abandoned.gadget.id).catch(() => undefined);
    }
  };
  const build = async (description: string) => {
    if (runs.delivering !== null) {
      // The last build's message is still on its way: another would send it twice, or drop
      // a gadget the session may be building.
      throw new Error("The last build is still being sent to the session; try again shortly");
    }
    const run = ++runs.run;
    const cancelled = () => run !== runs.run;
    let gadget = unfinished.current?.description === description ? unfinished.current.gadget : null;
    if (gadget === null) {
      // The description changed since the failed try: its gadget is not this one.
      dropUnfinished();
      const created = await createGadget(gadgetNameFrom(description));
      if (cancelled()) {
        void deleteGadget(created.id).catch(() => undefined);
        return;
      }
      gadget = created;
      unfinished.current = { description, gadget };
    }
    await show(gadget);
    // Cancel already deleted the unfinished gadget.
    if (cancelled()) return;
    runs.delivering = run;
    // On its way, it may reach the session: kept rather than deleted until we know.
    unfinished.current = { description, gadget, maybeDelivered: true };
    try {
      await openGadgetSession(gadget, composeGadgetBuildPrompt(gadget, description), navigate, {
        deliver: true,
        reveal: false,
        railSessionId: sessionId,
      });
    } catch (error) {
      if (!mayHaveBeenDelivered(error)) {
        // It never arrived: Try again reuses it, and a cancel meanwhile is finished now.
        if (unfinished.current?.gadget.id === gadget.id) {
          unfinished.current = { description, gadget };
        }
        if (cancelled()) dropUnfinished();
        throw error;
      }
      throw error;
    } finally {
      if (runs.delivering === run) runs.delivering = null;
    }
    if (unfinished.current?.gadget.id === gadget.id) unfinished.current = null;
    // Delivered after a Cancel: the session builds it, but the form the viewer reopened
    // since stays theirs; the gadget shows up among the session's gadgets.
    if (!cancelled()) openGadgetTab(gadget);
  };
  // Cancel leaves no half-made gadget behind, except one already being built.
  const cancel = () => {
    runs.run += 1;
    if (runs.delivering === null) dropUnfinished();
  };
  const cancelRef = useRef(cancel);
  cancelRef.current = cancel;
  useEffect(() => {
    const cancelThis = () => cancelRef.current();
    draftCancels.set(sessionId, cancelThis);
    return () => {
      if (draftCancels.get(sessionId) === cancelThis) draftCancels.delete(sessionId);
    };
  }, [sessionId]);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto p-4">
      {/* Full screen hides the tab strip, so the bar names the tab. */}
      {view.toolbar &&
        createPortal(
          <span className="flex min-w-0 items-center gap-1.5 text-ui font-medium">
            <PanelsTopLeftIcon className="size-4 shrink-0 text-muted-foreground" />
            {DRAFT_LABEL}
          </span>,
          view.toolbar,
        )}
      <GadgetDescribeForm
        heading={DRAFT_LABEL}
        explanation="Say what it shows, what data it uses and what people do with it. This session builds it here, and its buttons send their work back to this session."
        onBuild={build}
        onCancel={() => {
          cancel();
          setGadgetDraft(sessionId, false);
        }}
        errorTitle="Couldn’t start building the gadget"
        initialDescription={gadgetDraftText(sessionId)}
        onDescriptionChange={(text) => setGadgetDraftText(sessionId, text)}
      />
      {/* The description above is how this tab starts a blank one. */}
      <GadgetTemplatesSection
        minCardWidth={view.fullScreen ? 240 : 160}
        withoutBlank
        onCreated={async (gadget) => {
          await show(gadget);
          openGadgetTab(gadget);
        }}
      />
    </div>
  );
}

// The order each session's tabs were last shown in. The server lists a re-shown gadget last;
// keeping the order the viewer saw stops its tab from jumping to the end.
const tabOrders = new Map<string, readonly string[]>();

/** Forgets the tab orders; for tests. */
export function resetGadgetTabOrders(): void {
  tabOrders.clear();
}

function inShownOrder(sessionId: string, gadgets: Gadget[] | undefined): Gadget[] | undefined {
  if (gadgets === undefined) return undefined;
  const previous = tabOrders.get(sessionId) ?? [];
  const byId = new Map(gadgets.map((gadget) => [gadget.id, gadget]));
  const kept = previous.filter((id) => byId.has(id));
  const added = gadgets.map((gadget) => gadget.id).filter((id) => !kept.includes(id));
  const ordered = [...kept, ...added];
  tabOrders.set(sessionId, ordered);
  return ordered.map((id) => byId.get(id)!);
}

/** The tab to show when `key` goes: the one after it, else the one before it. */
function neighborOf(
  keys: readonly string[],
  key: string,
  remaining: readonly string[],
): string | null {
  const index = keys.indexOf(key);
  if (index < 0) return null;
  const candidates = [...keys.slice(index + 1), ...keys.slice(0, index).reverse()];
  return candidates.find((candidate) => remaining.includes(candidate)) ?? null;
}

function savedGadgetTab(conversationId: string): string | null {
  return readSessionWorkspaceState(conversationId).selectedGadgetKey ?? null;
}

interface Chosen {
  conversationId: string;
  key: string | null;
  /** On a phone: whether the chosen gadget covers the session. */
  mobileOpen: boolean;
}

/**
 * The gadget tabs of a session's side panel, and requests to show one. A request can come
 * before the panel lists the tab, or before the session's saved tab is restored, so it is tried
 * a few times until the panel shows the tab.
 */
export function useGadgetRailTabState(
  conversationId: string,
  {
    readOnly,
    showing,
    onShow,
    onReveal,
    maximized = false,
    onMaximizedChange,
    mobile = false,
    routed = true,
  }: {
    readOnly: boolean;
    /** Whether the panel shows its gadget tab mode now. */
    showing: boolean;
    /** Makes the panel show its gadget tab mode. */
    onShow: () => void;
    /** Opens the panel when it is closed. */
    onReveal?: () => void;
    /** Whether the panel fills the screen. */
    maximized?: boolean;
    /** Puts the panel in or out of full screen, leaving the sidebar open with `keepSidebar`. */
    onMaximizedChange?: (maximized: boolean, options?: { keepSidebar?: boolean }) => void;
    /** A phone, which has no side panel: a gadget covers the session instead. */
    mobile?: boolean;
    /** Whether this is the page's own panel, which names its full screen gadget in the URL. */
    routed?: boolean;
  },
): GadgetRailTabState {
  useLinkedGadgetsSync();
  const enabled = useGadgetsEnabled();
  const queryClient = useQueryClient();
  const showInSession = useShowGadgetInSession();
  const { gadgets: listed, settled } = useSessionGadgetsQuery(
    conversationId,
    !isTempConvId(conversationId),
  );
  const gadgets = useMemo(() => inShownOrder(conversationId, listed), [conversationId, listed]);
  const draft = useHasGadgetDraft(conversationId) && enabled;
  const tabs = useMemo<GadgetRailTab[]>(() => {
    if (gadgets === undefined && !draft) return NO_TABS;
    return [
      ...(gadgets ?? []).map((gadget) => ({ key: gadgetRailKey(gadget.id), label: gadget.name })),
      ...(draft ? [{ key: GADGET_DRAFT_KEY, label: DRAFT_LABEL }] : []),
    ];
  }, [draft, gadgets]);
  // Until the server says whether it has gadgets, a saved gadget tab may still come back.
  const loading = useServerInfo() === "loading" || (enabled && !settled);

  // The panel isn't keyed by session, so a switch reads the next session's saved tab at once.
  const [chosenState, setChosen] = useState<Chosen>(() => ({
    conversationId,
    key: savedGadgetTab(conversationId),
    mobileOpen: false,
  }));
  let chosen = chosenState;
  if (chosen.conversationId !== conversationId) {
    chosen = { conversationId, key: savedGadgetTab(conversationId), mobileOpen: false };
    setChosen(chosen);
  }
  const choose = useCallback(
    (key: string | null, mobileOpen = false) => {
      setChosen({ conversationId, key, mobileOpen });
      saveSessionWorkspaceState(conversationId, { selectedGadgetKey: key });
    },
    [conversationId],
  );
  // When the chosen gadget goes (deleted, or no longer shared or shown here), the tab next to it
  // takes its place in the same render, so the panel never falls back to another kind of tab.
  const lastTabsRef = useRef<{ conversationId: string; keys: string[] } | null>(null);
  const tabKeys = tabs.map((tab) => tab.key);
  const last = lastTabsRef.current;
  const gone =
    chosen.key !== null &&
    !tabKeys.includes(chosen.key) &&
    !loading &&
    last?.conversationId === conversationId &&
    last.keys.includes(chosen.key);
  const selected =
    chosen.key !== null && tabKeys.includes(chosen.key)
      ? chosen.key
      : gone && chosen.key !== null
        ? neighborOf(last.keys, chosen.key, tabKeys)
        : null;
  // A gadget that filled the screen gives the screen back to the session when it goes.
  const mobileOpen = mobile && chosen.mobileOpen && selected !== null && !gone;

  const onShowRef = useRef(onShow);
  onShowRef.current = onShow;
  const onRevealRef = useRef(onReveal);
  onRevealRef.current = onReveal;
  const onMaximizedChangeRef = useRef(onMaximizedChange);
  onMaximizedChangeRef.current = onMaximizedChange;
  const fullScreen = selected !== null && !gone && (mobile ? mobileOpen : maximized && showing);
  const fullScreenRef = useRef(fullScreen);
  fullScreenRef.current = fullScreen;
  // Whether the panel fills the screen for its gadget tabs, even once the selected one is gone.
  const panelFullScreenRef = useRef(false);
  panelFullScreenRef.current = mobile ? chosen.mobileOpen : maximized && showing;
  const closeMobile = useCallback(() => {
    // A request still kept would show the gadget again.
    clearGadgetRailRequest();
    setChosen((current) => ({ ...current, mobileOpen: false }));
  }, []);
  // The panel toggles full screen, so a second leave before it re-renders would enter it again.
  const leftFullScreenRef = useRef(false);
  useEffect(() => {
    leftFullScreenRef.current = false;
  }, [maximized]);
  const leaveFullScreen = useCallback(() => {
    if (mobile) {
      closeMobile();
    } else if (!leftFullScreenRef.current) {
      leftFullScreenRef.current = true;
      onMaximizedChangeRef.current?.(false);
    }
  }, [mobile, closeMobile]);

  const requested = useGadgetRailRequest((state) => state.requested);
  const attemptsRef = useRef<{ request: unknown; count: number; maximized: boolean }>({
    request: null,
    count: 0,
    maximized: false,
  });
  useEffect(() => {
    if (requested === null || requested.conversationId !== conversationId) return undefined;
    if (!tabs.some((tab) => tab.key === requested.key)) return undefined;
    if (attemptsRef.current.request !== requested)
      attemptsRef.current = { request: requested, count: 0, maximized: false };
    // Full screen is asked for once: leaving it afterwards is the viewer's choice.
    if (maximized) attemptsRef.current.maximized = true;
    const onTab = chosen.conversationId === conversationId && chosen.key === requested.key;
    // A phone shows a requested gadget over the session, its only way to show one.
    const shown = mobile
      ? onTab && chosen.mobileOpen
      : showing && onTab && (!requested.maximize || attemptsRef.current.maximized);
    if (attemptsRef.current.count >= MAX_SHOW_ATTEMPTS) {
      clearGadgetRailRequest();
      return undefined;
    }
    if (shown) {
      const timer = setTimeout(clearGadgetRailRequest, SHOWN_GRACE_MS);
      return () => clearTimeout(timer);
    }
    attemptsRef.current.count += 1;
    if (mobile) {
      choose(requested.key, true);
      return undefined;
    }
    choose(requested.key);
    onShowRef.current();
    onRevealRef.current?.();
    if (requested.maximize && !attemptsRef.current.maximized)
      onMaximizedChangeRef.current?.(true, { keepSidebar: requested.keepSidebar });
    return undefined;
  }, [requested, conversationId, tabs, showing, chosen, maximized, mobile, choose]);

  // Gadget code takes its tab out of full screen when it hands the session something to see,
  // such as a quote in the composer.
  const exitFullScreen = useGadgetRailRequest((state) => state.exitFullScreen);
  useEffect(() => {
    if (exitFullScreen === null || exitFullScreen.conversationId !== conversationId) return;
    useGadgetRailRequest.setState({ exitFullScreen: null });
    if (fullScreenRef.current) leaveFullScreen();
  }, [exitFullScreen, conversationId, leaveFullScreen]);

  // Saves the tab that took a gone gadget's place, and leaves its full screen.
  useEffect(() => {
    if (!loading) lastTabsRef.current = { conversationId, keys: tabs.map((tab) => tab.key) };
    if (!gone) return;
    if (panelFullScreenRef.current) leaveFullScreen();
    choose(selected);
  }, [tabs, loading, conversationId, gone, selected, choose, leaveFullScreen]);

  // The URL names the gadget filling the screen: a link, reload or Forward to it opens it so.
  const location = useLocation();
  const replaceSearchParams = useReplaceSearchParams();
  const urlGadget = routed
    ? new URLSearchParams(location.search).get(GADGET_FULL_SCREEN_PARAM)
    : null;
  const fullScreenGadgetId = fullScreen && selected !== null ? gadgetIdFromRailKey(selected) : null;
  const handledUrlRef = useRef<string | null>(null);
  const keepSidebar = gadgetOpenKeepsSidebar(location);
  // A URL naming a gadget this panel can't show leaves the session showing; the page then
  // marks its messages read again.
  const urlGadgetUnavailable =
    urlGadget !== null && !loading && !tabKeys.includes(gadgetRailKey(urlGadget));
  useEffect(() => {
    if (!routed) return;
    noteGadgetFullScreenUnavailable(
      urlGadgetUnavailable && urlGadget !== null ? { conversationId, gadgetId: urlGadget } : null,
    );
  }, [routed, urlGadgetUnavailable, urlGadget, conversationId]);
  useEffect(() => {
    if (urlGadget === null) {
      handledUrlRef.current = null;
      return;
    }
    const marker = `${location.key}\n${urlGadget}`;
    if (handledUrlRef.current === marker) return;
    // Asked for only once the session's gadgets are known, and only for one among them: a
    // stale link, or one on a server without gadgets, never opens the workspace for nothing.
    if (loading) return;
    const key = gadgetRailKey(urlGadget);
    if (!tabKeys.includes(key)) {
      // Not linked here (yet): nothing to show, and no request left to show it later. Not
      // marked handled, so a link that lands afterwards still shows it.
      const pending = useGadgetRailRequest.getState().requested;
      if (pending?.conversationId === conversationId && pending.key === key) {
        clearGadgetRailRequest();
      }
      return;
    }
    handledUrlRef.current = marker;
    if (fullScreenGadgetId === urlGadget) return;
    requestGadgetRailTab(conversationId, key, { maximize: true, keepSidebar });
  }, [urlGadget, location.key, conversationId, fullScreenGadgetId, keepSidebar, loading, tabKeys]);
  // Entering or leaving full screen updates the URL in place; the history entry stays the same.
  const shownFullScreenRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = shownFullScreenRef.current;
    shownFullScreenRef.current = fullScreenGadgetId;
    if (!routed || previous === fullScreenGadgetId) return;
    const params = new URLSearchParams(location.search);
    const current = params.get(GADGET_FULL_SCREEN_PARAM);
    if (fullScreenGadgetId !== null && current !== fullScreenGadgetId) {
      params.set(GADGET_FULL_SCREEN_PARAM, fullScreenGadgetId);
      replaceSearchParams(params);
    } else if (fullScreenGadgetId === null && previous !== null && current === previous) {
      params.delete(GADGET_FULL_SCREEN_PARAM);
      replaceSearchParams(params);
    }
    // Only a change of the full screen gadget writes the URL.
  }, [fullScreenGadgetId]); // eslint-disable-line react-hooks/exhaustive-deps
  // The sidebar marks the gadget filling the screen.
  useEffect(() => {
    if (routed) setFullScreenGadget(fullScreenGadgetId);
  }, [routed, fullScreenGadgetId]);
  useEffect(
    () => () => {
      if (routed) setFullScreenGadget(null);
    },
    [routed],
  );

  const byKey = useMemo(
    () => new Map((gadgets ?? []).map((gadget) => [gadgetRailKey(gadget.id), gadget])),
    [gadgets],
  );
  const newItems = useMemo<GadgetRailNewItem[]>(
    () =>
      readOnly || gadgets === undefined
        ? NO_ITEMS
        : [
            {
              key: "new-gadget",
              label: "Gadget",
              icon: PanelsTopLeftIcon,
              onSelect: () => {
                setGadgetDraft(conversationId, true);
                requestGadgetRailTab(conversationId, GADGET_DRAFT_KEY);
              },
            },
          ],
    [conversationId, readOnly, gadgets],
  );

  return {
    tabs,
    newItems,
    selected,
    loading,
    select: (key) => {
      // The viewer's own choice ends any request still being kept.
      clearGadgetRailRequest();
      choose(key, mobile);
      if (!mobile) onShowRef.current();
    },
    close: (key) => {
      clearGadgetRailRequest();
      if (selected === key) {
        // The tab next to it takes its place, as browser tabs do.
        const keys = tabs.map((tab) => tab.key);
        const next = neighborOf(
          keys,
          key,
          keys.filter((other) => other !== key),
        );
        if (next === null && fullScreen) leaveFullScreen();
        choose(next, mobileOpen && next !== null);
      }
      if (key === GADGET_DRAFT_KEY) {
        // Closing the tab cancels its build, as Cancel does.
        draftCancels.get(conversationId)?.();
        setGadgetDraft(conversationId, false);
        return;
      }
      const gadgetId = gadgetIdFromRailKey(key);
      if (gadgetId !== null)
        showInSession.mutate({ gadgetId, sessionId: conversationId, shown: false });
    },
    // A viewer's own shelf: anyone who can read the session hides gadgets beside it for
    // themselves, even where they can't build one there.
    canClose: true,
    mobileOpen,
    closeMobile,
    render: (key, view) => {
      if (key === GADGET_DRAFT_KEY)
        // Keyed by session: a draft's text and its unfinished build belong to one session.
        return <GadgetDraftPane key={conversationId} sessionId={conversationId} view={view} />;
      const gadget = byKey.get(key);
      return gadget ? (
        <GadgetRailPane
          key={gadget.id}
          gadget={gadget}
          view={view}
          sessionId={conversationId}
          onOpenSession={view.fullScreen ? leaveFullScreen : undefined}
          onDeleted={() => {
            // Deleting it from full screen shows the session, docked, with the tab next to it.
            if (panelFullScreenRef.current) leaveFullScreen();
            invalidateGadgets(queryClient);
          }}
        />
      ) : null;
    },
  };
}

const TAB_CLASS =
  "flex h-[24px] shrink-0 items-center gap-[6px] rounded-md px-2 text-ui font-medium leading-5 transition-colors";
const ACTIVE_TAB_CLASS =
  "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground";
const INACTIVE_TAB_CLASS =
  "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground";

/** The gadget tabs in the panel's tab strip, drawn like its side chat tabs. */
export function GadgetRailTabStrip({
  state,
  active,
}: {
  state: GadgetRailTabState;
  /** Whether a gadget tab is the panel's selection. */
  active: boolean;
}) {
  const activeRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Moves only the strip, not the page, to show the tab.
    if (active) scrollTabIntoView(activeRef.current);
  }, [active, state.selected]);
  if (state.tabs.length === 0) return null;
  return (
    <div role="tablist" aria-label="Gadgets" className="flex items-center gap-0.5">
      {state.tabs.map(({ key, label }) => {
        const isActive = active && state.selected === key;
        return (
          <div
            key={key}
            ref={isActive ? activeRef : null}
            data-testid="gadget-rail-tab"
            className={cn(TAB_CLASS, isActive ? ACTIVE_TAB_CLASS : INACTIVE_TAB_CLASS)}
          >
            <button
              type="button"
              role="tab"
              aria-selected={isActive}
              title={label}
              className="flex min-w-0 items-center gap-1"
              onAuxClick={(event) => {
                if (event.button === 1 && state.canClose) {
                  event.preventDefault();
                  state.close(key);
                }
              }}
              onClick={() => state.select(key)}
            >
              <PanelsTopLeftIcon className="size-4 shrink-0" />
              <span className="max-w-[16ch] truncate">{label}</span>
            </button>
            {state.canClose && (
              <button
                type="button"
                aria-label={`Close ${label}`}
                className="flex size-4 items-center justify-center rounded hover:bg-muted"
                onClick={() => state.close(key)}
              >
                <XIcon className="size-3" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A phone's gadget: it covers the session, with its controls in the drawer's header. */
function GadgetMobileBody({ state }: { state: GadgetRailTabState }) {
  const header = useMobilePanelHeaderTarget();
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  if (state.selected === null) return null;
  return (
    <>
      {header?.title &&
        createPortal(
          <div
            ref={setSlot}
            data-mobile-panel-title
            className="flex min-w-0 max-w-[calc(100vw-5rem)] items-center gap-1"
          />,
          header.title,
        )}
      {state.render(state.selected, { fullScreen: true, toolbar: slot })}
    </>
  );
}

/** On a phone, the requested gadget over the session; Close shows the session again. */
export function GadgetMobileDrawer({ state }: { state: GadgetRailTabState }) {
  const label = state.tabs.find((tab) => tab.key === state.selected)?.label ?? "Gadget";
  return (
    <MobilePanelDrawer
      open={state.mobileOpen}
      title={label}
      onClose={state.closeMobile}
      testId="gadget-panel-drawer"
    >
      <GadgetMobileBody state={state} />
    </MobilePanelDrawer>
  );
}

/**
 * The start of a full screen gadget's bar: the sidebar button while the sidebar is closed (the
 * bar covers the header that has it), then the slot the gadget puts its controls into.
 */
export function GadgetFullScreenSlot({
  onToolbar,
  sidebarOpen = true,
  onOpenSidebar,
}: {
  onToolbar: (element: HTMLElement | null) => void;
  sidebarOpen?: boolean;
  onOpenSidebar?: (peek?: boolean) => void;
}) {
  return (
    <>
      {!sidebarOpen && onOpenSidebar && (
        <SidebarOpenButton onOpenSidebar={onOpenSidebar} componentId="gadgets.open_sidebar" />
      )}
      {/* Inset like the gadget's content below it, and clear of the sidebar button. */}
      <div
        ref={onToolbar}
        data-testid="gadget-toolbar-slot"
        className="ml-2 flex min-w-0 flex-1 items-center gap-1"
      />
    </>
  );
}

/** The gadget New session starts from; it loads the gadget and shows it like a session's tab. */
function GadgetNewSessionPane({
  gadgetId,
  view,
  onOpenSession,
  onDeleted,
}: {
  gadgetId: string;
  view: GadgetTabView;
  onOpenSession?: () => void;
  onDeleted: () => void;
}) {
  const gadget = useGadget(gadgetId, { watch: true });
  if (gadget.isPending) return null;
  // A poll that fails for a while (the network, the server restarting) keeps the gadget running
  // on what was loaded; only a gadget that is gone or no longer readable gives way.
  if (gadget.isError && (gadget.data === undefined || isGadgetUnavailable(gadget.error))) {
    return (
      <GadgetUnavailable
        className="m-3"
        gadgetId={gadgetId}
        error={gadget.error}
        onRetry={() => void gadget.refetch()}
      />
    );
  }
  return (
    <GadgetRailPane
      gadget={gadget.data}
      view={view}
      onOpenSession={view.fullScreen ? onOpenSession : undefined}
      onDeleted={onDeleted}
    />
  );
}

/** The gadget a New session starts from, named by `gadget_start` in New session's URL. */
export function useGadgetStartId(): string | null {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const enabled = useGadgetsEnabled();
  const landing = rebasePath("/").replace(/\/$/, "");
  const atLanding = pathname.replace(/\/$/, "") === landing;
  return enabled && atLanding ? new URLSearchParams(search).get(GADGET_START_PARAM) : null;
}

const PANEL_ICON_BUTTON =
  "flex size-6 shrink-0 text-muted-foreground hover:text-foreground mobile-touch-target";

/**
 * The full screen gadget beside New session, staying over the session its Create session menu
 * started until that session shows the gadget full screen itself, so the viewer never sees the
 * session in between.
 */
interface GadgetHandoff {
  gadgetId: string;
  /** The started session's ID in the URL: its temporary ID, then its real one. */
  sessionId: string;
  /** The gadget's frame connections and failed starts when the handoff began. */
  connections: number;
  failures: number;
}
// After this the session shows as it is, full screen or not.
const HANDOFF_TIMEOUT_MS = 15_000;
// The session's frame has connected; it gets a moment to draw before it shows.
const HANDOFF_SETTLE_MS = 150;

/**
 * The gadget beside New session, when New session was opened from it: docked on the right like a
 * session's side panel, or filling the screen, with the same full screen toggle. Phones show it
 * only full screen; after Open session a chip above New session brings it back.
 */
export function GadgetNewSessionPanel({
  active,
  sidebarOpen,
  onOpenSidebar,
  onSidebarOpenChange,
}: {
  /** Whether New session shows (no session is open). */
  active: boolean;
  sidebarOpen?: boolean;
  onOpenSidebar?: (peek?: boolean) => void;
  /** Closes the sidebar for full screen and opens it again after, as the side panel does. */
  onSidebarOpenChange?: (open: boolean) => void;
}) {
  const gadgetId = useGadgetStartId();
  const maximized = useGadgetRailRequest((state) => state.newSessionMaximized);
  const [toolbar, setToolbar] = useState<HTMLElement | null>(null);
  const mobile = useIsMobileViewport();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useLocation();
  const rebasePath = useRebasePath();
  const replaceSearchParams = useReplaceSearchParams();
  const shown = active && gadgetId !== null;

  // Create session from the full screen gadget's menu moves New session to the session it
  // creates, at its temporary ID first. The gadget stays over it until the session shows it.
  const sessionPrefix = rebasePath("/c/");
  const routeSessionId = sessionIdFromPath(location.pathname, sessionPrefix);
  const [lastFullScreen, setLastFullScreen] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<GadgetHandoff | null>(null);
  const shownFullScreen = shown && maximized ? gadgetId : null;
  if (shown) {
    if (lastFullScreen !== shownFullScreen) setLastFullScreen(shownFullScreen);
    if (handoff !== null) setHandoff(null);
  } else if (lastFullScreen !== null) {
    setLastFullScreen(null);
    if (routeSessionId !== null && isTempConvId(routeSessionId)) {
      const { frameConnections, failedStarts } = useGadgetRailRequest.getState();
      setHandoff({
        gadgetId: lastFullScreen,
        sessionId: routeSessionId,
        connections: gadgetCount(frameConnections, lastFullScreen),
        failures: gadgetCount(failedStarts, lastFullScreen),
      });
    }
  } else if (handoff !== null && routeSessionId !== handoff.sessionId) {
    // The temporary ID gives way to the real one; any other page ends the handoff.
    if (isTempConvId(handoff.sessionId) && routeSessionId !== null && !isTempConvId(routeSessionId))
      setHandoff({ ...handoff, sessionId: routeSessionId });
    else setHandoff(null);
  }
  const coverGadgetId = shown ? gadgetId : (handoff?.gadgetId ?? null);
  const covering = !shown && handoff !== null;
  const handoffGadgetId = handoff?.gadgetId ?? null;
  const handoffReady = useGadgetRailRequest(
    (state) =>
      handoff !== null &&
      state.fullScreenGadgetId === handoff.gadgetId &&
      gadgetCount(state.frameConnections, handoff.gadgetId) > handoff.connections,
  );
  const handoffFailed = useGadgetRailRequest(
    (state) =>
      handoff !== null && gadgetCount(state.failedStarts, handoff.gadgetId) > handoff.failures,
  );
  // Whether the session took the gadget over full screen, for the sidebar below.
  const handedOverRef = useRef(false);
  useEffect(() => {
    if (handoffGadgetId === null) return undefined;
    const timer = setTimeout(
      () => {
        handedOverRef.current = handoffReady;
        setHandoff(null);
      },
      handoffFailed ? 0 : handoffReady ? HANDOFF_SETTLE_MS : HANDOFF_TIMEOUT_MS,
    );
    return () => clearTimeout(timer);
  }, [handoffGadgetId, handoffReady, handoffFailed]);
  // While the cover hides the session, its new messages stay unread.
  const coveredSessionId = covering ? (handoff?.sessionId ?? null) : null;
  useEffect(() => {
    noteGadgetHandoffCover(coveredSessionId);
    return () => noteGadgetHandoffCover(null);
  }, [coveredSessionId]);
  // The gadget over the session is only a picture of it: the session's own gadget takes clicks.
  const asideRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    asideRef.current?.toggleAttribute("inert", covering);
  });

  const gadget = useGadget(coverGadgetId, { watch: true });
  // Full screen lasts while the gadget shows; the next New session starts docked unless gadget
  // code asks again before it opens.
  useEffect(() => {
    if (!shown) setGadgetNewSessionTabMaximized(false);
  }, [shown]);
  // The session started here shows the gadget as it is now; leaving New session keeps the note.
  useEffect(() => {
    if (shown && gadgetId !== null) noteGadgetStartFullScreen(gadgetId, maximized);
  }, [shown, gadgetId, maximized]);

  // The URL names the gadget while it fills the screen, as on a session's page: a reload or
  // Forward opens it so again.
  const params = new URLSearchParams(location.search);
  const urlGadget = params.get(GADGET_FULL_SCREEN_PARAM);
  const handledUrlRef = useRef<string | null>(null);
  useEffect(() => {
    if (!shown || urlGadget === null || urlGadget !== gadgetId) return;
    const marker = `${location.key}\n${urlGadget}`;
    if (handledUrlRef.current === marker) return;
    handledUrlRef.current = marker;
    setGadgetNewSessionTabMaximized(true);
  }, [shown, urlGadget, gadgetId, location.key]);
  // A link that fills the composer without naming the gadget, such as a request from the gadget
  // before its session exists, shows the composer. Following the link rather than leaving full
  // screen before it keeps the URL write below from replacing the link with the page before it.
  const linksPrompt = params.has("prompt");
  useEffect(() => {
    if (shown && linksPrompt && urlGadget === null) setGadgetNewSessionTabMaximized(false);
  }, [shown, linksPrompt, urlGadget, location.key]);
  const setFullScreen = (next: boolean) => {
    setGadgetNewSessionTabMaximized(next);
    if (gadgetId === null || next === (urlGadget === gadgetId)) return;
    const nextParams = new URLSearchParams(location.search);
    if (next) nextParams.set(GADGET_FULL_SCREEN_PARAM, gadgetId);
    else nextParams.delete(GADGET_FULL_SCREEN_PARAM);
    replaceSearchParams(nextParams);
  };
  // Gadget code can leave full screen too (a quote sent to the composer); the URL follows.
  const fullScreenId = shown && maximized ? gadgetId : null;
  const wasFullScreenRef = useRef(false);
  useEffect(() => {
    const was = wasFullScreenRef.current;
    wasFullScreenRef.current = fullScreenId !== null;
    if (!was || fullScreenId !== null || !shown || urlGadget === null) return;
    const nextParams = new URLSearchParams(location.search);
    nextParams.delete(GADGET_FULL_SCREEN_PARAM);
    replaceSearchParams(nextParams);
    // Only leaving full screen writes the URL here.
  }, [fullScreenId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Like the side panel's full screen, this one closes the sidebar and opens it again after,
  // unless the link that opened the gadget says to leave the sidebar as it was (an open from
  // inside Omnigent; see gadgetOpenKeepsSidebar). The Full screen button closes it.
  const keepSidebarNow = (location.state as GadgetOpenState | null)?.keepSidebar === true;
  // The choice the cover opened with lasts while it does: creating the session moves to pages
  // that carry no navigation state.
  const keepSidebarLatchRef = useRef<{ gadgetId: string; keep: boolean } | null>(null);
  const sidebarStashRef = useRef<boolean | null>(null);
  const sidebarOpenRef = useRef(sidebarOpen);
  sidebarOpenRef.current = sidebarOpen;
  const onSidebarOpenChangeRef = useRef(onSidebarOpenChange);
  onSidebarOpenChangeRef.current = onSidebarOpenChange;
  // A session that took the gadget over full screen leaves the sidebar as it is.
  const coverId = fullScreenId ?? handoffGadgetId;
  useEffect(() => {
    if (coverId !== null) {
      if (keepSidebarLatchRef.current?.gadgetId !== coverId) {
        keepSidebarLatchRef.current = { gadgetId: coverId, keep: keepSidebarNow };
      }
      if (sidebarStashRef.current !== null || keepSidebarLatchRef.current.keep || mobile) return;
      sidebarStashRef.current = sidebarOpenRef.current ?? true;
      onSidebarOpenChangeRef.current?.(false);
    } else {
      keepSidebarLatchRef.current = null;
      if (sidebarStashRef.current !== null) {
        const reopen = sidebarStashRef.current && !handedOverRef.current;
        sidebarStashRef.current = null;
        if (reopen) onSidebarOpenChangeRef.current?.(true);
      }
    }
    handedOverRef.current = false;
  }, [coverId, keepSidebarNow, mobile]);

  if (coverGadgetId === null) return null;
  const panelMaximized = covering || maximized;
  const label = gadget.data?.name ?? "Gadget";
  const close = () => {
    const nextParams = new URLSearchParams(location.search);
    nextParams.delete(GADGET_START_PARAM);
    nextParams.delete(GADGET_FULL_SCREEN_PARAM);
    leaveGadgetStart(coverGadgetId, queryClient);
    replaceSearchParams(nextParams);
  };
  const onDeleted = () => {
    leaveGadgetStart(coverGadgetId, queryClient);
    // Back must not reopen New session for the deleted gadget.
    navigate("/gadget", { replace: true });
  };
  if (!panelMaximized && mobile) {
    // A phone has no room beside New session: a chip brings the gadget back over it.
    return (
      <div className="pointer-events-none absolute inset-x-0 top-[calc(var(--omnigent-header-height,3rem)+0.5rem)] z-30 flex justify-center px-4">
        <Button
          type="button"
          variant="outline"
          size="sm"
          componentId="gadgets.new_session.show"
          className="pointer-events-auto max-w-full gap-1.5 rounded-full bg-card shadow-sm mobile-touch-target"
          onClick={() => setFullScreen(true)}
        >
          <PanelsTopLeftIcon className="size-4 shrink-0" />
          <span className="truncate">Show {label}</span>
        </Button>
      </div>
    );
  }
  return (
    <aside
      ref={asideRef}
      aria-label={label}
      aria-busy={covering || undefined}
      data-testid="gadget-new-session-panel"
      data-maximized={panelMaximized || undefined}
      className={cn(
        "relative z-40 min-h-0 flex-col overflow-hidden bg-card",
        // Full screen covers New session, as a full screen side panel covers its session.
        panelMaximized
          ? "absolute inset-0 flex"
          : "hidden border-l border-border md:flex md:w-[min(560px,45%)] md:shrink-0",
      )}
    >
      <div
        role="toolbar"
        aria-label={`${label} tabs`}
        className="flex shrink-0 items-center gap-1 border-b border-border px-2 py-3"
      >
        {panelMaximized ? (
          <GadgetFullScreenSlot
            onToolbar={setToolbar}
            sidebarOpen={sidebarOpen}
            onOpenSidebar={onOpenSidebar}
          />
        ) : (
          <div className={cn(TAB_CLASS, ACTIVE_TAB_CLASS, "min-w-0")}>
            <PanelsTopLeftIcon className="size-4 shrink-0" />
            <span className="max-w-[24ch] truncate">{label}</span>
          </div>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1 pl-1">
          {panelMaximized ? (
            // Leaving full screen shows New session, so the button shows the harness New session
            // will start, in grey until it starts, with New session's controls in a popover.
            <GadgetOpenSessionButton
              sessionId={null}
              gadgetId={gadgetId}
              starting={covering}
              onOpenSession={() => setFullScreen(false)}
              className={PANEL_ICON_BUTTON}
            />
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Full screen"
                  className={PANEL_ICON_BUTTON}
                  onClick={() => setFullScreen(true)}
                >
                  <MaximizeIcon className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">Full screen</TooltipContent>
            </Tooltip>
          )}
          {!panelMaximized && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Close ${label}`}
                  className={PANEL_ICON_BUTTON}
                  componentId="gadgets.new_session.close"
                  onClick={close}
                >
                  <XIcon className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">Close</TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>
      <RenderErrorBoundary name="this gadget" resetKey={coverGadgetId}>
        <div className="flex min-h-0 flex-1 flex-col">
          <GadgetNewSessionPane
            key={coverGadgetId}
            gadgetId={coverGadgetId}
            view={{ fullScreen: panelMaximized, toolbar: panelMaximized ? toolbar : null }}
            onOpenSession={() => setFullScreen(false)}
            onDeleted={onDeleted}
          />
        </div>
      </RenderErrorBoundary>
    </aside>
  );
}
