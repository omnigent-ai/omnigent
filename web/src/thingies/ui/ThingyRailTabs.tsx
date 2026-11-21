// Thingies in a session's side panel: a tab for each thingy shown beside the session, a New
// thingy tab that builds one in that session, and the thingy beside New session. The panel keeps
// which tab is selected (saved per session, like its other tabs); thingy code asks for a tab
// through `requestThingyRailTab`. A thingy filling the screen is named in the page's URL, so
// reloading, sharing or going Forward opens it the same way. Phones have no side panel, so a
// thingy there covers the session instead.
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
  THINGY_FULL_SCREEN_PARAM,
  clearThingyRailRequest,
  requestThingyRailTab,
  setFullScreenThingy,
  setThingyNewSessionTabMaximized,
  useThingyRailRequest,
} from "@/lib/thingyRailTabs";
import { cn } from "@/lib/utils";
import { MobilePanelDrawer, useMobilePanelHeaderTarget } from "@/shell/MobilePanelDrawer";
import { SidebarOpenButton } from "@/shell/SidebarOpenButton";
import { scrollTabIntoView } from "@/shell/WorkspaceTabsScroller";
import { type Thingy, type ThingyContent, createThingy } from "@/thingies/api/thingyApi";
import { composeThingyBuildPrompt, thingyNameFrom } from "@/thingies/host/thingyBuild";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import {
  openThingySession,
  thingyIdFromRailKey,
  thingyRailKey,
} from "@/thingies/host/thingySession";
import { ThingyDescribeForm } from "./ThingyDescribeForm";
import { ThingyTemplatesSection } from "./ThingyTemplateGallery";
import { ThingyActions, ThingyName, ThingyPinButton } from "./ThingyToolbar";
import { ThingyUnavailable } from "./ThingyUnavailable";
import { ThingyView } from "./ThingyView";
import {
  setThingyDraft,
  setThingyDraftText,
  thingyDraftText,
  useHasThingyDraft,
} from "./thingyDrafts";
import {
  thingyOpenKeepsSidebar,
  useThingyNavigate,
  type ThingyOpenState,
} from "./thingyNavigation";
import {
  invalidateThingies,
  useLinkedThingiesSync,
  useSessionThingiesQuery,
  useShowThingyInSession,
  useThingiesEnabled,
  useThingy,
} from "./thingyQueries";
import { leaveThingyStart, noteThingyStartFullScreen } from "./useAdoptStartedThingySession";
import { ThingyOpenSessionButton } from "./ThingyOpenSessionButton";

export const THINGY_DRAFT_KEY = "thingy-draft";
const DRAFT_LABEL = "New thingy";

export interface ThingyRailTab {
  /** Unique within the panel, for example `thingy:<id>`. */
  key: string;
  label: string;
}

/** An entry the panel's "Open new" menu shows. */
export interface ThingyRailNewItem {
  key: string;
  label: string;
  icon: typeof PanelsTopLeftIcon;
  onSelect: () => void;
}

/** How a thingy tab is shown. */
export interface ThingyTabView {
  /** Whether it fills the screen, alone in its panel. */
  fullScreen: boolean;
  /** In full screen, the panel's bar where the tab puts its controls; null otherwise. */
  toolbar: HTMLElement | null;
}

export interface ThingyRailTabState {
  tabs: ThingyRailTab[];
  newItems: ThingyRailNewItem[];
  /** The selected tab's key while it still exists, else null. */
  selected: string | null;
  /**
   * Whether the session's thingies are still loading, so a saved thingy tab can't be judged gone
   * yet.
   */
  loading: boolean;
  select: (key: string) => void;
  close: (key: string) => void;
  /** Whether the viewer can close the tabs. */
  canClose: boolean;
  /** On a phone: whether the selected thingy covers the session. */
  mobileOpen: boolean;
  /** On a phone: shows the session again. */
  closeMobile: () => void;
  render: (key: string, view: ThingyTabView) => ReactNode;
}

const NO_TABS: ThingyRailTab[] = [];
const NO_ITEMS: ThingyRailNewItem[] = [];
// How often a request switches the panel to its tab before giving up.
const MAX_SHOW_ATTEMPTS = 5;
// A shown request is kept this long, in case the session's saved tab restores after it.
const SHOWN_GRACE_MS = 1500;

/** The thingy's tab content: its controls above it, or in the panel's bar in full screen. */
function ThingyRailPane({
  thingy,
  view,
  sessionId,
  onDeleted,
  onOpenSession,
}: {
  thingy: Thingy;
  view: ThingyTabView;
  /** The session it is shown beside; none beside New session. */
  sessionId?: string;
  onDeleted: () => void;
  /** Leaves full screen to show the session. */
  onOpenSession?: () => void;
}) {
  const queryClient = useQueryClient();
  const [content, setContent] = useState<ThingyContent | undefined>(undefined);
  return (
    <ThingyView
      thingy={thingy}
      sessionId={sessionId}
      onReloadThingy={() => invalidateThingies(queryClient)}
      onContentLoaded={setContent}
      toolbarTarget={view.toolbar}
      fullScreen={view.fullScreen}
      onOpenSession={onOpenSession}
      renderToolbar={(commentsButton) => (
        <>
          <span className="flex min-w-0 flex-1 items-center">
            <ThingyName thingy={thingy} />
          </span>
          <ThingyPinButton thingy={thingy} />
          {commentsButton}
          <ThingyActions
            thingy={thingy}
            content={content?.revision_id === thingy.published_revision ? content : undefined}
            onDeleted={onDeleted}
            shareInMenu={!view.fullScreen}
          />
        </>
      )}
    />
  );
}

/** Builds a new thingy in this session from a description, or makes one from a template. */
function ThingyDraftPane({ sessionId, view }: { sessionId: string; view: ThingyTabView }) {
  const navigate = useThingyNavigate();
  const showInSession = useShowThingyInSession();
  // Replaces this tab with the thingy's own.
  const openThingyTab = (thingy: Thingy) => {
    setThingyDraft(sessionId, false);
    requestThingyRailTab(sessionId, thingyRailKey(thingy.id));
  };
  const show = (thingy: Thingy) =>
    showInSession.mutateAsync({ thingyId: thingy.id, sessionId, shown: true });
  const build = async (description: string) => {
    const thingy = await createThingy(thingyNameFrom(description));
    await show(thingy);
    await openThingySession(thingy, composeThingyBuildPrompt(thingy, description), navigate, {
      deliver: true,
      reveal: false,
      railSessionId: sessionId,
    });
    openThingyTab(thingy);
  };
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
      <ThingyDescribeForm
        heading={DRAFT_LABEL}
        explanation="Say what it shows, what data it uses and what people do with it. This session builds it here, and its buttons send their work back to this session."
        onBuild={build}
        onCancel={() => setThingyDraft(sessionId, false)}
        errorTitle="Couldn’t start building the thingy"
        initialDescription={thingyDraftText(sessionId)}
        onDescriptionChange={(text) => setThingyDraftText(sessionId, text)}
      />
      {/* The description above is how this tab starts a blank one. */}
      <ThingyTemplatesSection
        minCardWidth={view.fullScreen ? 240 : 160}
        withoutBlank
        onCreated={async (thingy) => {
          await show(thingy);
          openThingyTab(thingy);
        }}
      />
    </div>
  );
}

// The order each session's tabs were last shown in. The server lists a re-shown thingy last;
// keeping the order the viewer saw stops its tab from jumping to the end.
const tabOrders = new Map<string, readonly string[]>();

/** Forgets the tab orders; for tests. */
export function resetThingyTabOrders(): void {
  tabOrders.clear();
}

function inShownOrder(sessionId: string, thingies: Thingy[] | undefined): Thingy[] | undefined {
  if (thingies === undefined) return undefined;
  const previous = tabOrders.get(sessionId) ?? [];
  const byId = new Map(thingies.map((thingy) => [thingy.id, thingy]));
  const kept = previous.filter((id) => byId.has(id));
  const added = thingies.map((thingy) => thingy.id).filter((id) => !kept.includes(id));
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

function savedThingyTab(conversationId: string): string | null {
  return readSessionWorkspaceState(conversationId).selectedThingyKey ?? null;
}

interface Chosen {
  conversationId: string;
  key: string | null;
  /** On a phone: whether the chosen thingy covers the session. */
  mobileOpen: boolean;
}

/**
 * The thingy tabs of a session's side panel, and requests to show one. A request can come
 * before the panel lists the tab, or before the session's saved tab is restored, so it is tried
 * a few times until the panel shows the tab.
 */
export function useThingyRailTabState(
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
    /** Whether the panel shows its thingy tab mode now. */
    showing: boolean;
    /** Makes the panel show its thingy tab mode. */
    onShow: () => void;
    /** Opens the panel when it is closed. */
    onReveal?: () => void;
    /** Whether the panel fills the screen. */
    maximized?: boolean;
    /** Puts the panel in or out of full screen, leaving the sidebar open with `keepSidebar`. */
    onMaximizedChange?: (maximized: boolean, options?: { keepSidebar?: boolean }) => void;
    /** A phone, which has no side panel: a thingy covers the session instead. */
    mobile?: boolean;
    /** Whether this is the page's own panel, which names its full screen thingy in the URL. */
    routed?: boolean;
  },
): ThingyRailTabState {
  useLinkedThingiesSync();
  const enabled = useThingiesEnabled();
  const queryClient = useQueryClient();
  const showInSession = useShowThingyInSession();
  const { thingies: listed, settled } = useSessionThingiesQuery(
    conversationId,
    !isTempConvId(conversationId),
  );
  const thingies = useMemo(() => inShownOrder(conversationId, listed), [conversationId, listed]);
  const draft = useHasThingyDraft(conversationId) && enabled;
  const tabs = useMemo<ThingyRailTab[]>(() => {
    if (thingies === undefined && !draft) return NO_TABS;
    return [
      ...(thingies ?? []).map((thingy) => ({ key: thingyRailKey(thingy.id), label: thingy.name })),
      ...(draft ? [{ key: THINGY_DRAFT_KEY, label: DRAFT_LABEL }] : []),
    ];
  }, [draft, thingies]);
  // Until the server says whether it has thingies, a saved thingy tab may still come back.
  const loading = useServerInfo() === "loading" || (enabled && !settled);

  // The panel isn't keyed by session, so a switch reads the next session's saved tab at once.
  const [chosenState, setChosen] = useState<Chosen>(() => ({
    conversationId,
    key: savedThingyTab(conversationId),
    mobileOpen: false,
  }));
  let chosen = chosenState;
  if (chosen.conversationId !== conversationId) {
    chosen = { conversationId, key: savedThingyTab(conversationId), mobileOpen: false };
    setChosen(chosen);
  }
  const choose = useCallback(
    (key: string | null, mobileOpen = false) => {
      setChosen({ conversationId, key, mobileOpen });
      saveSessionWorkspaceState(conversationId, { selectedThingyKey: key });
    },
    [conversationId],
  );
  // When the chosen thingy goes (deleted, or no longer shared or shown here), the tab next to it
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
  // A thingy that filled the screen gives the screen back to the session when it goes.
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
  // Whether the panel fills the screen for its thingy tabs, even once the selected one is gone.
  const panelFullScreenRef = useRef(false);
  panelFullScreenRef.current = mobile ? chosen.mobileOpen : maximized && showing;
  const closeMobile = useCallback(() => {
    // A request still kept would show the thingy again.
    clearThingyRailRequest();
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

  const requested = useThingyRailRequest((state) => state.requested);
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
    // A phone shows a requested thingy over the session, its only way to show one.
    const shown = mobile
      ? onTab && chosen.mobileOpen
      : showing && onTab && (!requested.maximize || attemptsRef.current.maximized);
    if (attemptsRef.current.count >= MAX_SHOW_ATTEMPTS) {
      clearThingyRailRequest();
      return undefined;
    }
    if (shown) {
      const timer = setTimeout(clearThingyRailRequest, SHOWN_GRACE_MS);
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

  // Thingy code takes its tab out of full screen when it hands the session something to see,
  // such as a quote in the composer.
  const exitFullScreen = useThingyRailRequest((state) => state.exitFullScreen);
  useEffect(() => {
    if (exitFullScreen === null || exitFullScreen.conversationId !== conversationId) return;
    useThingyRailRequest.setState({ exitFullScreen: null });
    if (fullScreenRef.current) leaveFullScreen();
  }, [exitFullScreen, conversationId, leaveFullScreen]);

  // Saves the tab that took a gone thingy's place, and leaves its full screen.
  useEffect(() => {
    if (!loading) lastTabsRef.current = { conversationId, keys: tabs.map((tab) => tab.key) };
    if (!gone) return;
    if (panelFullScreenRef.current) leaveFullScreen();
    choose(selected);
  }, [tabs, loading, conversationId, gone, selected, choose, leaveFullScreen]);

  // The URL names the thingy filling the screen: a link, reload or Forward to it opens it so.
  const location = useLocation();
  const replaceSearchParams = useReplaceSearchParams();
  const urlThingy = routed
    ? new URLSearchParams(location.search).get(THINGY_FULL_SCREEN_PARAM)
    : null;
  const fullScreenThingyId = fullScreen && selected !== null ? thingyIdFromRailKey(selected) : null;
  const handledUrlRef = useRef<string | null>(null);
  const keepSidebar = thingyOpenKeepsSidebar(location);
  useEffect(() => {
    if (urlThingy === null) {
      handledUrlRef.current = null;
      return;
    }
    const marker = `${location.key}\n${urlThingy}`;
    if (handledUrlRef.current === marker) return;
    handledUrlRef.current = marker;
    if (fullScreenThingyId === urlThingy) return;
    requestThingyRailTab(conversationId, thingyRailKey(urlThingy), { maximize: true, keepSidebar });
  }, [urlThingy, location.key, conversationId, fullScreenThingyId, keepSidebar]);
  // Entering or leaving full screen updates the URL in place; the history entry stays the same.
  const shownFullScreenRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = shownFullScreenRef.current;
    shownFullScreenRef.current = fullScreenThingyId;
    if (!routed || previous === fullScreenThingyId) return;
    const params = new URLSearchParams(location.search);
    const current = params.get(THINGY_FULL_SCREEN_PARAM);
    if (fullScreenThingyId !== null && current !== fullScreenThingyId) {
      params.set(THINGY_FULL_SCREEN_PARAM, fullScreenThingyId);
      replaceSearchParams(params);
    } else if (fullScreenThingyId === null && previous !== null && current === previous) {
      params.delete(THINGY_FULL_SCREEN_PARAM);
      replaceSearchParams(params);
    }
    // Only a change of the full screen thingy writes the URL.
  }, [fullScreenThingyId]); // eslint-disable-line react-hooks/exhaustive-deps
  // The sidebar marks the thingy filling the screen.
  useEffect(() => {
    if (routed) setFullScreenThingy(fullScreenThingyId);
  }, [routed, fullScreenThingyId]);
  useEffect(
    () => () => {
      if (routed) setFullScreenThingy(null);
    },
    [routed],
  );

  const byKey = useMemo(
    () => new Map((thingies ?? []).map((thingy) => [thingyRailKey(thingy.id), thingy])),
    [thingies],
  );
  const newItems = useMemo<ThingyRailNewItem[]>(
    () =>
      readOnly || thingies === undefined
        ? NO_ITEMS
        : [
            {
              key: "new-thingy",
              label: "Thingy",
              icon: PanelsTopLeftIcon,
              onSelect: () => {
                setThingyDraft(conversationId, true);
                requestThingyRailTab(conversationId, THINGY_DRAFT_KEY);
              },
            },
          ],
    [conversationId, readOnly, thingies],
  );

  return {
    tabs,
    newItems,
    selected,
    loading,
    select: (key) => {
      // The viewer's own choice ends any request still being kept.
      clearThingyRailRequest();
      choose(key, mobile);
      if (!mobile) onShowRef.current();
    },
    close: (key) => {
      clearThingyRailRequest();
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
      if (key === THINGY_DRAFT_KEY) {
        setThingyDraft(conversationId, false);
        return;
      }
      const thingyId = thingyIdFromRailKey(key);
      if (thingyId !== null)
        showInSession.mutate({ thingyId, sessionId: conversationId, shown: false });
    },
    canClose: !readOnly,
    mobileOpen,
    closeMobile,
    render: (key, view) => {
      if (key === THINGY_DRAFT_KEY)
        return <ThingyDraftPane sessionId={conversationId} view={view} />;
      const thingy = byKey.get(key);
      return thingy ? (
        <ThingyRailPane
          key={thingy.id}
          thingy={thingy}
          view={view}
          sessionId={conversationId}
          onOpenSession={view.fullScreen ? leaveFullScreen : undefined}
          onDeleted={() => {
            // Deleting it from full screen shows the session, docked, with the tab next to it.
            if (panelFullScreenRef.current) leaveFullScreen();
            invalidateThingies(queryClient);
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

/** The thingy tabs in the panel's tab strip, drawn like its side chat tabs. */
export function ThingyRailTabStrip({
  state,
  active,
}: {
  state: ThingyRailTabState;
  /** Whether a thingy tab is the panel's selection. */
  active: boolean;
}) {
  const activeRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Moves only the strip, not the page, to show the tab.
    if (active) scrollTabIntoView(activeRef.current);
  }, [active, state.selected]);
  if (state.tabs.length === 0) return null;
  return (
    <div role="tablist" aria-label="Thingies" className="flex items-center gap-0.5">
      {state.tabs.map(({ key, label }) => {
        const isActive = active && state.selected === key;
        return (
          <div
            key={key}
            ref={isActive ? activeRef : null}
            data-testid="thingy-rail-tab"
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

/** A phone's thingy: it covers the session, with its controls in the drawer's header. */
function ThingyMobileBody({ state }: { state: ThingyRailTabState }) {
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

/** On a phone, the requested thingy over the session; Close shows the session again. */
export function ThingyMobileDrawer({ state }: { state: ThingyRailTabState }) {
  const label = state.tabs.find((tab) => tab.key === state.selected)?.label ?? "Thingy";
  return (
    <MobilePanelDrawer
      open={state.mobileOpen}
      title={label}
      onClose={state.closeMobile}
      testId="thingy-panel-drawer"
    >
      <ThingyMobileBody state={state} />
    </MobilePanelDrawer>
  );
}

/**
 * The start of a full screen thingy's bar: the sidebar button while the sidebar is closed (the
 * bar covers the header that has it), then the slot the thingy puts its controls into.
 */
export function ThingyFullScreenSlot({
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
        <SidebarOpenButton onOpenSidebar={onOpenSidebar} componentId="thingies.open_sidebar" />
      )}
      {/* Inset like the thingy's content below it, and clear of the sidebar button. */}
      <div
        ref={onToolbar}
        data-testid="thingy-toolbar-slot"
        className="ml-2 flex min-w-0 flex-1 items-center gap-1"
      />
    </>
  );
}

/** The thingy New session starts from; it loads the thingy and shows it like a session's tab. */
function ThingyNewSessionPane({
  thingyId,
  view,
  onOpenSession,
  onDeleted,
}: {
  thingyId: string;
  view: ThingyTabView;
  onOpenSession?: () => void;
  onDeleted: () => void;
}) {
  const thingy = useThingy(thingyId, { watch: true });
  if (thingy.isPending) return null;
  if (thingy.isError) {
    return (
      <ThingyUnavailable
        className="m-3"
        thingyId={thingyId}
        error={thingy.error}
        onRetry={() => void thingy.refetch()}
      />
    );
  }
  return (
    <ThingyRailPane
      thingy={thingy.data}
      view={view}
      onOpenSession={view.fullScreen ? onOpenSession : undefined}
      onDeleted={onDeleted}
    />
  );
}

/** The thingy a New session starts from, named by `thingy_start` in New session's URL. */
export function useThingyStartId(): string | null {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const enabled = useThingiesEnabled();
  const landing = rebasePath("/").replace(/\/$/, "");
  const atLanding = pathname.replace(/\/$/, "") === landing;
  return enabled && atLanding ? new URLSearchParams(search).get(THINGY_START_PARAM) : null;
}

const PANEL_ICON_BUTTON =
  "flex size-6 shrink-0 text-muted-foreground hover:text-foreground mobile-touch-target";

/**
 * The full screen thingy beside New session, staying over the session its Create session menu
 * started until that session shows the thingy full screen itself, so the viewer never sees the
 * session in between.
 */
interface ThingyHandoff {
  thingyId: string;
  /** The started session's ID in the URL: its temporary ID, then its real one. */
  sessionId: string;
  /** The thingy's frame connections and failed starts when the handoff began. */
  connections: number;
  failures: number;
}
// After this the session shows as it is, full screen or not.
const HANDOFF_TIMEOUT_MS = 15_000;
// The session's frame has connected; it gets a moment to draw before it shows.
const HANDOFF_SETTLE_MS = 150;

/**
 * The thingy beside New session, when New session was opened from it: docked on the right like a
 * session's side panel, or filling the screen, with the same full screen toggle. Phones show it
 * only full screen; after Open session a chip above New session brings it back.
 */
export function ThingyNewSessionPanel({
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
  const thingyId = useThingyStartId();
  const maximized = useThingyRailRequest((state) => state.newSessionMaximized);
  const [toolbar, setToolbar] = useState<HTMLElement | null>(null);
  const mobile = useIsMobileViewport();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useLocation();
  const rebasePath = useRebasePath();
  const replaceSearchParams = useReplaceSearchParams();
  const shown = active && thingyId !== null;

  // Create session from the full screen thingy's menu moves New session to the session it
  // creates, at its temporary ID first. The thingy stays over it until the session shows it.
  const sessionPrefix = rebasePath("/c/");
  const routeSessionId = location.pathname.startsWith(sessionPrefix)
    ? decodeURIComponent(location.pathname.slice(sessionPrefix.length).replace(/\/$/, ""))
    : null;
  const [lastFullScreen, setLastFullScreen] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<ThingyHandoff | null>(null);
  const shownFullScreen = shown && maximized ? thingyId : null;
  if (shown) {
    if (lastFullScreen !== shownFullScreen) setLastFullScreen(shownFullScreen);
    if (handoff !== null) setHandoff(null);
  } else if (lastFullScreen !== null) {
    setLastFullScreen(null);
    if (routeSessionId !== null && isTempConvId(routeSessionId)) {
      const { frameConnections, failedStarts } = useThingyRailRequest.getState();
      setHandoff({
        thingyId: lastFullScreen,
        sessionId: routeSessionId,
        connections: frameConnections[lastFullScreen] ?? 0,
        failures: failedStarts[lastFullScreen] ?? 0,
      });
    }
  } else if (handoff !== null && routeSessionId !== handoff.sessionId) {
    // The temporary ID gives way to the real one; any other page ends the handoff.
    if (isTempConvId(handoff.sessionId) && routeSessionId !== null && !isTempConvId(routeSessionId))
      setHandoff({ ...handoff, sessionId: routeSessionId });
    else setHandoff(null);
  }
  const coverThingyId = shown ? thingyId : (handoff?.thingyId ?? null);
  const covering = !shown && handoff !== null;
  const handoffThingyId = handoff?.thingyId ?? null;
  const handoffReady = useThingyRailRequest(
    (state) =>
      handoff !== null &&
      state.fullScreenThingyId === handoff.thingyId &&
      (state.frameConnections[handoff.thingyId] ?? 0) > handoff.connections,
  );
  const handoffFailed = useThingyRailRequest(
    (state) => handoff !== null && (state.failedStarts[handoff.thingyId] ?? 0) > handoff.failures,
  );
  // Whether the session took the thingy over full screen, for the sidebar below.
  const handedOverRef = useRef(false);
  useEffect(() => {
    if (handoffThingyId === null) return undefined;
    const timer = setTimeout(
      () => {
        handedOverRef.current = handoffReady;
        setHandoff(null);
      },
      handoffFailed ? 0 : handoffReady ? HANDOFF_SETTLE_MS : HANDOFF_TIMEOUT_MS,
    );
    return () => clearTimeout(timer);
  }, [handoffThingyId, handoffReady, handoffFailed]);
  // The thingy over the session is only a picture of it: the session's own thingy takes clicks.
  const asideRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    asideRef.current?.toggleAttribute("inert", covering);
  });

  const thingy = useThingy(coverThingyId, { watch: true });
  // Full screen lasts while the thingy shows; the next New session starts docked unless thingy
  // code asks again before it opens.
  useEffect(() => {
    if (!shown) setThingyNewSessionTabMaximized(false);
  }, [shown]);
  // The session started here shows the thingy as it is now; leaving New session keeps the note.
  useEffect(() => {
    if (shown && thingyId !== null) noteThingyStartFullScreen(thingyId, maximized);
  }, [shown, thingyId, maximized]);

  // The URL names the thingy while it fills the screen, as on a session's page: a reload or
  // Forward opens it so again.
  const params = new URLSearchParams(location.search);
  const urlThingy = params.get(THINGY_FULL_SCREEN_PARAM);
  const handledUrlRef = useRef<string | null>(null);
  useEffect(() => {
    if (!shown || urlThingy === null || urlThingy !== thingyId) return;
    const marker = `${location.key}\n${urlThingy}`;
    if (handledUrlRef.current === marker) return;
    handledUrlRef.current = marker;
    setThingyNewSessionTabMaximized(true);
  }, [shown, urlThingy, thingyId, location.key]);
  // A link that fills the composer without naming the thingy, such as a request from the thingy
  // before its session exists, shows the composer. Following the link rather than leaving full
  // screen before it keeps the URL write below from replacing the link with the page before it.
  const linksPrompt = params.has("prompt");
  useEffect(() => {
    if (shown && linksPrompt && urlThingy === null) setThingyNewSessionTabMaximized(false);
  }, [shown, linksPrompt, urlThingy, location.key]);
  const setFullScreen = (next: boolean) => {
    setThingyNewSessionTabMaximized(next);
    if (thingyId === null || next === (urlThingy === thingyId)) return;
    const nextParams = new URLSearchParams(location.search);
    if (next) nextParams.set(THINGY_FULL_SCREEN_PARAM, thingyId);
    else nextParams.delete(THINGY_FULL_SCREEN_PARAM);
    replaceSearchParams(nextParams);
  };
  // Thingy code can leave full screen too (a quote sent to the composer); the URL follows.
  const fullScreenId = shown && maximized ? thingyId : null;
  const wasFullScreenRef = useRef(false);
  useEffect(() => {
    const was = wasFullScreenRef.current;
    wasFullScreenRef.current = fullScreenId !== null;
    if (!was || fullScreenId !== null || !shown || urlThingy === null) return;
    const nextParams = new URLSearchParams(location.search);
    nextParams.delete(THINGY_FULL_SCREEN_PARAM);
    replaceSearchParams(nextParams);
    // Only leaving full screen writes the URL here.
  }, [fullScreenId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Like the side panel's full screen, this one closes the sidebar and opens it again after,
  // unless the link that opened the thingy says to leave the sidebar as it was (an open from
  // inside Omnigent; see thingyOpenKeepsSidebar). The Full screen button closes it.
  const keepSidebar = (location.state as ThingyOpenState | null)?.keepSidebar === true;
  const sidebarStashRef = useRef<boolean | null>(null);
  const sidebarOpenRef = useRef(sidebarOpen);
  sidebarOpenRef.current = sidebarOpen;
  const onSidebarOpenChangeRef = useRef(onSidebarOpenChange);
  onSidebarOpenChangeRef.current = onSidebarOpenChange;
  // A session that took the thingy over full screen leaves the sidebar as it is.
  const coverId = fullScreenId ?? handoffThingyId;
  useEffect(() => {
    if (coverId !== null) {
      if (sidebarStashRef.current !== null || keepSidebar || mobile) return;
      sidebarStashRef.current = sidebarOpenRef.current ?? true;
      onSidebarOpenChangeRef.current?.(false);
    } else if (sidebarStashRef.current !== null) {
      const reopen = sidebarStashRef.current && !handedOverRef.current;
      sidebarStashRef.current = null;
      if (reopen) onSidebarOpenChangeRef.current?.(true);
    }
    handedOverRef.current = false;
  }, [coverId, keepSidebar, mobile]);

  if (coverThingyId === null) return null;
  const panelMaximized = covering || maximized;
  const label = thingy.data?.name ?? "Thingy";
  const close = () => {
    const nextParams = new URLSearchParams(location.search);
    nextParams.delete(THINGY_START_PARAM);
    nextParams.delete(THINGY_FULL_SCREEN_PARAM);
    leaveThingyStart(coverThingyId, queryClient);
    replaceSearchParams(nextParams);
  };
  const onDeleted = () => {
    leaveThingyStart(coverThingyId, queryClient);
    // Back must not reopen New session for the deleted thingy.
    navigate("/thingy", { replace: true });
  };
  if (!panelMaximized && mobile) {
    // A phone has no room beside New session: a chip brings the thingy back over it.
    return (
      <div className="pointer-events-none absolute inset-x-0 top-[calc(var(--omnigent-header-height,3rem)+0.5rem)] z-30 flex justify-center px-4">
        <Button
          type="button"
          variant="outline"
          size="sm"
          componentId="thingies.new_session.show"
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
      data-testid="thingy-new-session-panel"
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
          <ThingyFullScreenSlot
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
            <ThingyOpenSessionButton
              sessionId={null}
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
                  componentId="thingies.new_session.close"
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
      <RenderErrorBoundary name="this thingy" resetKey={coverThingyId}>
        <div className="flex min-h-0 flex-1 flex-col">
          <ThingyNewSessionPane
            key={coverThingyId}
            thingyId={coverThingyId}
            view={{ fullScreen: panelMaximized, toolbar: panelMaximized ? toolbar : null }}
            onOpenSession={() => setFullScreen(false)}
            onDeleted={onDeleted}
          />
        </div>
      </RenderErrorBoundary>
    </aside>
  );
}
