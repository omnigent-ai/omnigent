/** Embedded browser pane. The page is a native Electron WebContentsView the
 *  main process paints over a placeholder `<div>` this component measures
 *  (getBoundingClientRect → IPC). Native overlay, not an iframe/webview: the
 *  agent needs a real Chromium page (screenshot, relay JS, cross-origin nav)
 *  and `<webview>` is deprecated.
 *
 *  A "Browser" tab in the Workspace rail, so it mounts only while selected.
 *  Flex column: a fixed toolbar row (URL bar + nav + DevTools + design-mode)
 *  always on top so the URL bar is reachable from a cold start; below it the
 *  content switches on `viewActive` (measuring placeholder once a view attaches,
 *  else a hint). Bounds-sync (containerRef + syncBounds + rAF/ResizeObserver) is
 *  gated on `viewActive` and measures only below the toolbar.
 *
 *  The agent relay is NOT here (it must listen before the first browser_navigate
 *  auto-selects the tab) — it's hoisted to the always-mounted AppShell. On
 *  unmount the view DETACHES, not destroys (background agent pages survive a tab
 *  switch; destroy only on explicit close). Renders nothing outside Electron. */
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  RotateCwIcon,
  SquareDashedMousePointerIcon,
  WrenchIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { isElectronShell } from "@/lib/nativeBridge";
import { normalizeTypedUrl } from "@/lib/normalizeTypedUrl";
import { cn } from "@/lib/utils";

/** Renderer CSS-pixel bounds pushed to the main process (converted to window
 *  DIPs there via the host zoom factor). */
interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
  devicePixelRatio?: number;
}

/** Result shape shared by the history-navigation bridge calls. */
interface NavResult {
  ok: boolean;
  canGoBack?: boolean;
  canGoForward?: boolean;
  error?: string;
}

/** Subset of `window.omnigentDesktop` the pane calls. Typed locally so the
 *  component doesn't depend on the full nativeBridge type; every method is
 *  optional (an older shell may predate the browser feature). */
interface BrowserPaneBridge {
  browserSetActive?: (conversationId: string | null) => Promise<{ ok: boolean; error?: string }>;
  browserResize?: (
    conversationId: string,
    bounds: Bounds,
  ) => Promise<{ ok: boolean; error?: string }>;
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds: Bounds | undefined,
    opts: { force?: boolean } | undefined,
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
  browserGoBack?: (conversationId: string) => Promise<NavResult>;
  browserGoForward?: (conversationId: string) => Promise<NavResult>;
  browserReload?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  openBrowserDevTools?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  browserEnableDesignMode?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  browserDisableDesignMode?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  onBrowserHostActiveChanged?: (
    callback: (payload: { conversationId: string | null }) => void,
  ) => () => void;
  onBrowserViewCreated?: (callback: (payload: { conversationId: string }) => void) => () => void;
  onBrowserViewClosed?: (
    callback: (payload: { conversationId: string; reason: string | null }) => void,
  ) => () => void;
  onBrowserUrlChanged?: (
    callback: (payload: { conversationId: string; url: string }) => void,
  ) => () => void;
  onBrowserNavState?: (
    callback: (payload: {
      conversationId: string;
      canGoBack: boolean;
      canGoForward: boolean;
    }) => void,
  ) => () => void;
  browserHasView?: (conversationId: string) => Promise<{ exists: boolean }>;
}

function getBridge(): BrowserPaneBridge | null {
  if (!isElectronShell()) return null;
  const w = window as unknown as { omnigentDesktop?: BrowserPaneBridge };
  return w.omnigentDesktop ?? null;
}

export interface BrowserPaneProps {
  /** Conversation whose WebContentsView this pane hosts. */
  conversationId: string;
  /** Extra classes for the measuring placeholder wrapper. */
  className?: string;
}

/**
 * Keeps the agent relay alive for a conversation and, once a native browser
 * view is attached, keeps that view positioned over a measuring placeholder.
 */
export function BrowserPane({ conversationId, className }: BrowserPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const lastBoundsRef = useRef<Bounds | null>(null);
  const electron = isElectronShell();
  // Whether a native view is attached for THIS conversation — drives when the
  // measuring placeholder mounts (no empty pane on an idle conversation).
  const [viewActive, setViewActive] = useState(false);

  // Toolbar state. `currentUrl` tracks the real view URL EXCEPT while the user
  // edits the input (urlEditingRef gates the stomp); canGoBack/Forward drive
  // the arrow buttons.
  const [currentUrl, setCurrentUrl] = useState("");
  // Ref (not state): read synchronously in the url-changed listener; a
  // stale-closure state read would race.
  const urlEditingRef = useRef(false);
  const [canGoBack, setCanGoBack] = useState(false);
  const [canGoForward, setCanGoForward] = useState(false);
  // Design-mode toggle: on, the main process injects the in-page picker; submit
  // routing lives in AppShell. This flag only drives the button + enable/disable IPC.
  const [designMode, setDesignMode] = useState(false);

  // Feed `viewActive` from three signals so the placeholder mounts exactly when
  // a view exists: (1) browser-view-created — first navigate (often detached,
  // no host-active event; breaks the activation deadlock); (2) browserHasView
  // probe on re-mount; (3) host-active-changed for later attach/detach.
  // browser-view-closed flips it false.
  useEffect(() => {
    if (!electron) return;
    const bridge = getBridge();
    if (!bridge) return;
    let cancelled = false;

    // (2) Re-show an already-created view when the pane remounts.
    void bridge.browserHasView?.(conversationId).then((r) => {
      if (!cancelled && r?.exists) setViewActive(true);
    });

    // (1) A view was just created for this conversation (first navigate).
    const unsubCreated = bridge.onBrowserViewCreated?.((payload) => {
      if (payload.conversationId === conversationId) setViewActive(true);
    });
    // (3) Attach/detach transitions. An attach for another conversation, or a
    // detach (null), means this pane's view is no longer the visible one.
    const unsubActive = bridge.onBrowserHostActiveChanged?.((payload) => {
      if (payload.conversationId === conversationId) setViewActive(true);
      else if (payload.conversationId === null) setViewActive(false);
    });
    const unsubClosed = bridge.onBrowserViewClosed?.((payload) => {
      if (payload.conversationId === conversationId) setViewActive(false);
    });
    return () => {
      cancelled = true;
      unsubCreated?.();
      unsubActive?.();
      unsubClosed?.();
    };
  }, [conversationId, electron]);

  // Live-track the real URL + back/forward via did-navigate listeners (redirects,
  // link clicks, agent nav all keep the bar honest), but never stomp the input
  // while the user is editing it (urlEditingRef).
  useEffect(() => {
    if (!electron) return;
    const bridge = getBridge();
    if (!bridge) return;
    const unsubUrl = bridge.onBrowserUrlChanged?.((payload) => {
      if (payload.conversationId !== conversationId) return;
      if (urlEditingRef.current) return;
      setCurrentUrl(payload.url);
    });
    const unsubNav = bridge.onBrowserNavState?.((payload) => {
      if (payload.conversationId !== conversationId) return;
      setCanGoBack(payload.canGoBack);
      setCanGoForward(payload.canGoForward);
    });
    return () => {
      unsubUrl?.();
      unsubNav?.();
    };
  }, [conversationId, electron]);

  // ── Toolbar handlers ─────────────────────────────────────────────────────

  // Submit the URL bar. Normalize the typed value (add scheme) and reuse the
  // relay's own navigate path with force:true so it reloads even if the typed
  // URL matches the current one (explicit "go there" intent).
  const submitUrl = useCallback(() => {
    const bridge = getBridge();
    if (!bridge?.browserOpenOrNavigate) return;
    const raw = currentUrl.trim();
    if (!raw) return;
    const navUrl = normalizeTypedUrl(raw);
    setCurrentUrl(navUrl);
    void bridge.browserOpenOrNavigate(conversationId, navUrl, undefined, { force: true });
  }, [conversationId, currentUrl]);

  const handleBack = useCallback(() => {
    const bridge = getBridge();
    void bridge?.browserGoBack?.(conversationId).then((r) => {
      if (r?.ok) {
        setCanGoBack(!!r.canGoBack);
        setCanGoForward(!!r.canGoForward);
      }
    });
  }, [conversationId]);

  const handleForward = useCallback(() => {
    const bridge = getBridge();
    void bridge?.browserGoForward?.(conversationId).then((r) => {
      if (r?.ok) {
        setCanGoBack(!!r.canGoBack);
        setCanGoForward(!!r.canGoForward);
      }
    });
  }, [conversationId]);

  const handleReload = useCallback(() => {
    const bridge = getBridge();
    void bridge?.browserReload?.(conversationId);
  }, [conversationId]);

  const handleDevTools = useCallback(() => {
    const bridge = getBridge();
    void bridge?.openBrowserDevTools?.(conversationId);
  }, [conversationId]);

  const handleToggleDesignMode = useCallback(() => {
    const bridge = getBridge();
    if (!bridge) return;
    if (designMode) {
      void bridge.browserDisableDesignMode?.(conversationId);
      setDesignMode(false);
    } else {
      void bridge.browserEnableDesignMode?.(conversationId);
      setDesignMode(true);
    }
  }, [conversationId, designMode]);

  // If the view goes away (closed) while design mode is on, drop the pressed
  // state so the button doesn't lie — the injected picker died with the view.
  useEffect(() => {
    if (!viewActive && designMode) setDesignMode(false);
  }, [viewActive, designMode]);

  // Measure the placeholder and push bounds to the main process. These are
  // renderer CSS pixels; the main process converts to WebContentsView DIPs
  // using the host zoom factor (they diverge after Cmd+/Cmd- zoom).
  const syncBounds = useCallback(
    (force = false) => {
      const bridge = getBridge();
      if (!containerRef.current || !bridge?.browserResize) return;
      const rect = containerRef.current.getBoundingClientRect();
      const bounds: Bounds = {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        devicePixelRatio: window.devicePixelRatio,
      };
      if (bounds.width <= 0 || bounds.height <= 0) return;
      const last = lastBoundsRef.current;
      if (
        !force &&
        last &&
        last.x === bounds.x &&
        last.y === bounds.y &&
        last.width === bounds.width &&
        last.height === bounds.height &&
        last.devicePixelRatio === bounds.devicePixelRatio
      ) {
        return;
      }
      lastBoundsRef.current = bounds;
      void bridge.browserResize(conversationId, bounds);
    },
    [conversationId],
  );

  // Attach this conversation's view to the host window when the placeholder is
  // present; DETACH (not destroy) on unmount so a background agent's page keeps
  // running when the user switches away. A later mount re-attaches.
  useEffect(() => {
    if (!electron || !viewActive) return;
    const bridge = getBridge();
    if (!bridge?.browserSetActive) return;
    void bridge.browserSetActive(conversationId);
    // Measure after a couple of frames so any pane-open transition settles
    // before the first bounds land (the rAF loop below corrects stragglers).
    let frame = 0;
    let cancelled = false;
    const measureSoon = () => {
      if (cancelled) return;
      if (frame++ < 5) {
        requestAnimationFrame(measureSoon);
        return;
      }
      syncBounds(true);
    };
    requestAnimationFrame(measureSoon);
    return () => {
      cancelled = true;
      lastBoundsRef.current = null;
      // Detach whatever is currently active (this pane owned it). The view
      // survives in the registry; only an explicit close destroys it.
      try {
        void getBridge()?.browserSetActive?.(null);
      } catch {
        /* swallow — window may be tearing down */
      }
    };
  }, [conversationId, electron, viewActive, syncBounds]);

  // Reconcile bounds every frame while shown (cheap: same-rect setBounds is a
  // no-op + we dedupe via lastBoundsRef). Catches position-only shifts that
  // ResizeObserver misses (it only fires on size). try/catch so a throw during
  // teardown still schedules the next frame — else the rAF chain dies and the
  // overlay strands.
  useEffect(() => {
    if (!electron || !viewActive) return;
    let rafId = 0;
    const tick = () => {
      try {
        syncBounds();
      } catch (e) {
        console.warn("[BrowserPane] syncBounds threw:", e);
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [electron, viewActive, syncBounds]);

  // Defense-in-depth against a hung rAF chain: ResizeObserver (size), window
  // resize, and visibilitychange (tab-back, where rAFs were throttled) each
  // recover bounds on the next interaction.
  useEffect(() => {
    if (!electron || !viewActive || !containerRef.current) return;
    const el = containerRef.current;
    const ro = new ResizeObserver(() => syncBounds());
    ro.observe(el);
    const onResize = () => syncBounds();
    const onVisibility = () => {
      if (document.visibilityState === "visible") syncBounds();
    };
    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [electron, viewActive, syncBounds]);

  // Plain browser (non-Electron): render nothing so the web build has no empty
  // split pane. The relay is a no-op there anyway.
  if (!electron) return null;

  // Flex column: the toolbar (shrink-0) is ALWAYS the first child so the URL bar
  // is reachable from a cold start (typing a URL creates the view on demand →
  // viewActive flips true → the measuring container mounts). Gating the toolbar
  // on viewActive was a deadlock: no page → no toolbar → no way to open a page.
  //
  // LAYOUT TRAP (verified): the native view paints OVER the measured containerRef
  // rect, so the toolbar must be ABOVE it, never inside. The container is the LAST
  // child, flex-1 min-h-0 (not inset:0), so getBoundingClientRect covers only the
  // region below the toolbar and the view fills exactly that.
  //
  // Content area below the always-present toolbar switches on viewActive:
  //   - viewActive: the measuring `containerRef` placeholder. `containerRef` +
  //     syncBounds + the rAF/observer effects (all gated on viewActive above)
  //     keep the native view positioned over the container. NO containerRef is
  //     mounted while !viewActive, so nothing measures an empty div.
  //   - !viewActive: a centered hint. Back/forward are already disabled off
  //     canGoBack/canGoForward (both false with no view); reload + DevTools are
  //     explicitly disabled (nothing to reload / no devtools target yet). The
  //     URL bar stays editable so the user can open the first page.
  return (
    <div
      className={cn("flex min-h-0 min-w-0 flex-col", className)}
      data-browser-pane-conversation={conversationId}
    >
      <div className="flex shrink-0 items-center gap-1 border-border border-b bg-card px-2 py-1.5">
        <button
          type="button"
          onClick={handleBack}
          disabled={!canGoBack}
          aria-label="Go back"
          title="Back"
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <ChevronLeftIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={handleForward}
          disabled={!canGoForward}
          aria-label="Go forward"
          title="Forward"
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <ChevronRightIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={handleReload}
          disabled={!viewActive}
          aria-label="Reload"
          title="Reload"
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <RotateCwIcon className="size-4" />
        </button>
        <input
          type="text"
          value={currentUrl}
          spellCheck={false}
          autoCorrect="off"
          autoCapitalize="off"
          placeholder="Enter a URL"
          aria-label="Address bar"
          onChange={(e) => setCurrentUrl(e.target.value)}
          onFocus={() => {
            urlEditingRef.current = true;
          }}
          onBlur={() => {
            urlEditingRef.current = false;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submitUrl();
              e.currentTarget.blur();
            }
          }}
          className="h-6 min-w-0 flex-1 rounded-md border border-input bg-transparent px-2 text-foreground text-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring dark:bg-input/30"
        />
        <button
          type="button"
          onClick={handleDevTools}
          disabled={!viewActive}
          aria-label="Toggle DevTools"
          title="Toggle DevTools"
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <WrenchIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={handleToggleDesignMode}
          disabled={!viewActive}
          aria-pressed={designMode}
          aria-label={designMode ? "Exit design mode" : "Enter design mode"}
          title={
            designMode
              ? "Click an element in the page, then describe what to change"
              : "Design mode: point at an element to prompt about it"
          }
          className={cn(
            "flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40",
            designMode && "bg-primary/15 text-primary hover:bg-primary/20",
          )}
        >
          <SquareDashedMousePointerIcon className="size-4" />
        </button>
      </div>
      {viewActive ? (
        /* Measuring region — the native WebContentsView paints over this.
           flex-1 min-h-0 so it fills everything BELOW the toolbar; its rect
           is what syncBounds() pushes. Mounted only while viewActive so the
           effects never measure an empty div. */
        <div ref={containerRef} className="min-h-0 min-w-0 flex-1" />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-card px-6 py-8 text-center text-muted-foreground text-sm">
          Enter a URL above to get started — the agent will open pages here too.
        </div>
      )}
    </div>
  );
}
