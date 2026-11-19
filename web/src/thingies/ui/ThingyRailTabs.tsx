// Thingies in a session's side panel: a tab for each thingy shown beside the session, a New
// thingy tab that builds one in that session, and the thingy beside New session. The panel keeps
// which tab is selected; thingy code asks for a tab through `requestThingyRailTab`.
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { MaximizeIcon, MessageSquareIcon, PanelsTopLeftIcon, XIcon } from "lucide-react";

import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useLocation, useNavigate, useRebasePath } from "@/lib/routing";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  clearThingyRailRequest,
  requestThingyRailTab,
  setThingyNewSessionTabMaximized,
  useThingyRailRequest,
} from "@/lib/thingyRailTabs";
import { cn } from "@/lib/utils";
import { SidebarOpenButton } from "@/shell/SidebarOpenButton";
import { type Thingy, type ThingyContent, createThingy } from "@/thingies/api/thingyApi";
import { composeThingyBuildPrompt, thingyNameFrom } from "@/thingies/host/thingyBuild";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import {
  openThingySession,
  thingyIdFromRailKey,
  thingyRailKey,
} from "@/thingies/host/thingySession";
import { ThingyDescribeForm } from "./ThingyDescribeForm";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { ThingyTemplatesSection } from "./ThingyTemplateGallery";
import { ThingyActions, ThingyName, ThingyPinButton } from "./ThingyToolbar";
import { ThingyView } from "./ThingyView";
import { setThingyDraft, useHasThingyDraft } from "./thingyDrafts";
import { useThingyNavigate } from "./thingyNavigation";
import {
  invalidateThingies,
  useLinkedThingiesSync,
  useSessionThingies,
  useShowThingyInSession,
  useThingiesEnabled,
  useThingy,
} from "./thingyQueries";
import { useQueryClient } from "@tanstack/react-query";

export const THINGY_DRAFT_KEY = "thingy-draft";

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
  select: (key: string) => void;
  close: (key: string) => void;
  /** Whether the viewer can close the tabs. */
  canClose: boolean;
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
}: {
  thingy: Thingy;
  view: ThingyTabView;
  /** The session it is shown beside; none beside New session. */
  sessionId?: string;
  onDeleted: () => void;
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
function ThingyDraftPane({ sessionId }: { sessionId: string }) {
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
    await openThingySession(thingy, `${composeThingyBuildPrompt(thingy)}${description}`, navigate, {
      deliver: true,
      reveal: false,
      railSessionId: sessionId,
    });
    openThingyTab(thingy);
  };
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto p-4">
      <ThingyDescribeForm
        heading="New thingy"
        explanation="Describe what it shows, what data it uses and what people do with it. This session builds it here, and its buttons send their work back to this session."
        onBuild={build}
        onCancel={() => setThingyDraft(sessionId, false)}
        errorTitle="Couldn’t start building the thingy"
      />
      {/* The description above is how this tab starts a blank one. */}
      <ThingyTemplatesSection
        minCardWidth={160}
        withoutBlank
        onCreated={async (thingy) => {
          await show(thingy);
          openThingyTab(thingy);
        }}
      />
    </div>
  );
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
  },
): ThingyRailTabState {
  useLinkedThingiesSync();
  const enabled = useThingiesEnabled();
  const queryClient = useQueryClient();
  const showInSession = useShowThingyInSession();
  const thingies = useSessionThingies(conversationId, !isTempConvId(conversationId));
  const draft = useHasThingyDraft(conversationId) && enabled;
  const tabs = useMemo<ThingyRailTab[]>(() => {
    if (thingies === undefined && !draft) return NO_TABS;
    return [
      ...(thingies ?? []).map((thingy) => ({ key: thingyRailKey(thingy.id), label: thingy.name })),
      ...(draft ? [{ key: THINGY_DRAFT_KEY, label: "New thingy" }] : []),
    ];
  }, [draft, thingies]);
  const [chosen, setChosen] = useState<{ conversationId: string; key: string } | null>(null);
  const selected =
    chosen !== null &&
    chosen.conversationId === conversationId &&
    tabs.some((tab) => tab.key === chosen.key)
      ? chosen.key
      : null;
  const onShowRef = useRef(onShow);
  onShowRef.current = onShow;
  const onRevealRef = useRef(onReveal);
  onRevealRef.current = onReveal;
  const onMaximizedChangeRef = useRef(onMaximizedChange);
  onMaximizedChangeRef.current = onMaximizedChange;

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
    const shown =
      showing &&
      chosen?.conversationId === conversationId &&
      chosen.key === requested.key &&
      (!requested.maximize || attemptsRef.current.maximized);
    if (attemptsRef.current.count >= MAX_SHOW_ATTEMPTS) {
      clearThingyRailRequest();
      return undefined;
    }
    if (shown) {
      const timer = setTimeout(clearThingyRailRequest, SHOWN_GRACE_MS);
      return () => clearTimeout(timer);
    }
    attemptsRef.current.count += 1;
    setChosen({ conversationId, key: requested.key });
    onShowRef.current();
    onRevealRef.current?.();
    if (requested.maximize && !attemptsRef.current.maximized)
      onMaximizedChangeRef.current?.(true, { keepSidebar: requested.keepSidebar });
    return undefined;
  }, [requested, conversationId, tabs, showing, chosen, maximized]);

  // Thingy code takes its tab out of full screen when it hands the session something to see,
  // such as a quote in the composer.
  const exitFullScreen = useThingyRailRequest((state) => state.exitFullScreen);
  useEffect(() => {
    if (exitFullScreen === null || exitFullScreen.conversationId !== conversationId) return;
    useThingyRailRequest.setState({ exitFullScreen: null });
    if (maximized && showing) onMaximizedChangeRef.current?.(false);
  }, [exitFullScreen, conversationId, maximized, showing]);

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
    select: (key) => {
      // The viewer's own choice ends any request still being kept.
      clearThingyRailRequest();
      setChosen({ conversationId, key });
      onShowRef.current();
    },
    close: (key) => {
      clearThingyRailRequest();
      if (selected === key) setChosen(null);
      if (key === THINGY_DRAFT_KEY) {
        setThingyDraft(conversationId, false);
        return;
      }
      const thingyId = thingyIdFromRailKey(key);
      if (thingyId !== null)
        showInSession.mutate({ thingyId, sessionId: conversationId, shown: false });
    },
    canClose: !readOnly,
    render: (key, view) => {
      if (key === THINGY_DRAFT_KEY) return <ThingyDraftPane sessionId={conversationId} />;
      const thingy = byKey.get(key);
      return thingy ? (
        <ThingyRailPane
          key={thingy.id}
          thingy={thingy}
          view={view}
          sessionId={conversationId}
          onDeleted={() => invalidateThingies(queryClient)}
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
    if (active) activeRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [active, state.selected]);
  return (
    <>
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
    </>
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
      <div
        ref={onToolbar}
        data-testid="thingy-toolbar-slot"
        className="flex min-w-0 flex-1 items-center gap-1"
      />
    </>
  );
}

/** The thingy New session starts from; it loads the thingy and shows it like a session's tab. */
function ThingyNewSessionPane({ thingyId, view }: { thingyId: string; view: ThingyTabView }) {
  const thingy = useThingy(thingyId);
  const navigate = useNavigate();
  if (thingy.isPending) return null;
  if (thingy.isError) {
    return (
      <ThingyErrorNotice
        className="m-3"
        title="Couldn’t open the thingy"
        error={thingy.error}
        onRetry={() => void thingy.refetch()}
      />
    );
  }
  return <ThingyRailPane thingy={thingy.data} view={view} onDeleted={() => navigate("/thingy")} />;
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

/**
 * The thingy beside New session, when New session was opened from it: docked on the right like a
 * session's side panel, or filling the screen, with the same full screen toggle.
 */
export function ThingyNewSessionPanel({
  active,
  sidebarOpen,
  onOpenSidebar,
}: {
  /** Whether New session shows (no session is open). */
  active: boolean;
  sidebarOpen?: boolean;
  onOpenSidebar?: (peek?: boolean) => void;
}) {
  const thingyId = useThingyStartId();
  const thingy = useThingy(active ? thingyId : null);
  const maximized = useThingyRailRequest((state) => state.newSessionMaximized);
  const [toolbar, setToolbar] = useState<HTMLElement | null>(null);
  const shown = active && thingyId !== null;
  // Full screen lasts while the thingy shows; the next New session starts docked unless thingy
  // code asks again before it opens.
  useEffect(() => {
    if (!shown) setThingyNewSessionTabMaximized(false);
  }, [shown]);
  if (!shown) return null;
  const label = thingy.data?.name ?? "Thingy";
  return (
    <aside
      aria-label={label}
      data-testid="thingy-new-session-panel"
      data-maximized={maximized || undefined}
      className={cn(
        "relative z-40 min-h-0 flex-col overflow-hidden bg-card",
        // Full screen covers New session, as a full screen side panel covers its session; phones
        // show the thingy only then.
        maximized
          ? "absolute inset-0 flex"
          : "hidden border-l border-border md:flex md:w-[min(560px,45%)] md:shrink-0",
      )}
    >
      <div
        role="toolbar"
        aria-label={`${label} tabs`}
        className="flex shrink-0 items-center gap-1 border-b border-border px-2 py-3"
      >
        {maximized ? (
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
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={maximized ? "Open session" : "Full screen"}
              className="ml-auto flex size-6 shrink-0 text-muted-foreground hover:text-foreground"
              onClick={() => setThingyNewSessionTabMaximized(!maximized)}
            >
              {maximized ? (
                <MessageSquareIcon className="size-4" />
              ) : (
                <MaximizeIcon className="size-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {maximized ? "Open session" : "Full screen"}
          </TooltipContent>
        </Tooltip>
      </div>
      <RenderErrorBoundary name="this thingy" resetKey={thingyId}>
        <div className="flex min-h-0 flex-1 flex-col">
          <ThingyNewSessionPane
            key={thingyId}
            thingyId={thingyId}
            view={{ fullScreen: maximized, toolbar: maximized ? toolbar : null }}
          />
        </div>
      </RenderErrorBoundary>
    </aside>
  );
}
