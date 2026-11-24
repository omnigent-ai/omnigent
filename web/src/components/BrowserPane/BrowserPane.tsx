/** Embedded browser pane. The page is a native Electron WebContentsView the
 *  main process paints over a placeholder `<div>` this component measures
 *  (getBoundingClientRect → IPC). Native overlay, not an iframe/webview: the
 *  agent needs a real Chromium page (screenshot, relay JS, cross-origin nav)
 *  and `<webview>` is deprecated.
 *
 *  A "Browser" tab in the Workspace rail, so it mounts only while selected.
 *  Flex column: a fixed toolbar row (URL bar + nav + DevTools + design-mode)
 *  always on top so the URL bar is reachable from a cold start; below it the
 *  content switches on `hasView` (measuring placeholder once a view exists,
 *  else a hint). Bounds-sync (containerRef + syncBounds + rAF/ResizeObserver) is
 *  gated on `hasView` and measures only below the toolbar.
 *
 *  The agent relay is NOT here (it must listen before the first browser_navigate
 *  auto-selects the tab) — it's hoisted to the always-mounted AppShell. On
 *  unmount the view DETACHES, not destroys (background agent pages survive a tab
 *  switch; destroy only on explicit close). Renders nothing outside Electron. */
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CookieIcon,
  RotateCwIcon,
  SquareDashedMousePointerIcon,
  WrenchIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ADDRESS_BAR_KEYS,
  MOD_KEY,
  RELOAD_KEYS,
  shortcutText,
} from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { useDesktopCommand } from "@/lib/desktopCommands";
import {
  CHROME_COOKIE_ALL_SITES_SCOPE,
  getDesktopFeatures,
  supportsBrowser,
  supportsDesktopCommands,
  type BrowserHostOptions,
  type ChromeCookieImportResult,
  type ChromeCookiePreviewResult,
} from "@/lib/nativeBridge";
import { readSavedBrowserPage } from "@/lib/browserTabPages";
import { browserViewOwnerId } from "@/lib/browserTabs";
import { fetchSessionHostId } from "@/lib/fetchSessionHostId";
import { resolveIdentity, subscribeIdentity } from "@/lib/identity";
import { normalizeTypedUrl } from "@/lib/normalizeTypedUrl";
import { previewHostOptions, withPreviewHostRetry } from "@/lib/previewHostOptions";
import { cn } from "@/lib/utils";
// Say when the tab's page is open in another desktop window.
import { OpenElsewhereHint } from "@/components/BrowserPane/agentTools/OpenElsewhereHint";
import {
  DesktopDesignTools,
  useHasDesktopDesignTools,
} from "@/components/BrowserPane/designTools/LazyDesktopDesignTools";

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
  browserSetActive?: (
    conversationId: string | null,
    options?: { focus?: boolean },
  ) => Promise<{ ok: boolean; error?: string }>;
  browserResize?: (
    conversationId: string,
    bounds: Bounds,
  ) => Promise<{ ok: boolean; error?: string }>;
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds: Bounds | undefined,
    opts: (BrowserHostOptions & { force?: boolean }) | undefined,
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
  browserGoBack?: (conversationId: string) => Promise<NavResult>;
  browserGoForward?: (conversationId: string) => Promise<NavResult>;
  browserReload?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  openBrowserDevTools?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  /** Local Chrome profiles holding cookies, for the current site and overall (metadata only). */
  browserChromeCookiePreview?: (conversationId: string) => Promise<ChromeCookiePreviewResult>;
  /** Copy a Chrome profile's cookies, for one site or every website, into this conversation's session. */
  browserChromeCookieImport?: (
    conversationId: string,
    args: { profileId: string; expectedHost: string; scope?: string },
  ) => Promise<ChromeCookieImportResult>;
  browserEnableDesignMode?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
  browserDisableDesignMode?: (conversationId: string) => Promise<{ ok: boolean; error?: string }>;
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
  browserHasView?: (conversationId: string) => Promise<{
    exists: boolean;
    /** The page is open in another desktop window, not this one. */
    elsewhere?: boolean;
    url?: string;
    canGoBack?: boolean;
    canGoForward?: boolean;
  }>;
}

function getBridge(): BrowserPaneBridge | null {
  if (!supportsBrowser()) return null;
  const w = window as unknown as { omnigentDesktop?: BrowserPaneBridge };
  return w.omnigentDesktop ?? null;
}

/** Whether the shell offers every website's cookies for this Chrome profile. */
function offersAllSites(preview: ChromeCookiePreviewResult | null, profileId: string): boolean {
  return (preview?.allSitesProfiles ?? []).some((profile) => profile.id === profileId);
}

/**
 * Scopes the user may target for one Chrome profile: every website first, when
 * the shell offers it, then the page's host and only the parent domains that
 * profile actually holds a domain cookie for (main-derived; the shell rejects
 * anything else).
 */
function cookieScopeOptions(
  preview: ChromeCookiePreviewResult | null,
  profileId: string,
): string[] {
  const allSites = offersAllSites(preview, profileId);
  const listed = preview?.profiles?.find((candidate) => candidate.id === profileId);
  // A profile offered only for every website holds nothing for this site.
  if (allSites && !listed) return [CHROME_COOKIE_ALL_SITES_SCOPE];
  const profile = listed ?? preview?.profiles?.[0];
  let siteScopes = preview?.host ? [preview.host] : [];
  if (profile?.scopes && profile.scopes.length > 0) {
    siteScopes = profile.scopes.map((entry) => entry.host);
  } else if (preview?.scopes && preview.scopes.length > 0) {
    siteScopes = preview.scopes;
  }
  return allSites ? [CHROME_COOKIE_ALL_SITES_SCOPE, ...siteScopes] : siteScopes;
}

/** Chrome profiles the dialog offers: any with cookies for some website, else for this site. */
function cookieProfileOptions(
  preview: ChromeCookiePreviewResult | null,
): { id: string; name: string }[] {
  const names = new Map<string, string>();
  for (const profile of [...(preview?.allSitesProfiles ?? []), ...(preview?.profiles ?? [])]) {
    if (!names.has(profile.id)) names.set(profile.id, profile.name);
  }
  return [...names].map(([id, name]) => ({ id, name }));
}

/** Cookies one Chrome profile holds for a scope. */
function cookieCountFor(
  preview: ChromeCookiePreviewResult | null,
  profileId: string,
  scope: string,
): number {
  if (scope === CHROME_COOKIE_ALL_SITES_SCOPE) {
    return preview?.allSitesProfiles?.find((profile) => profile.id === profileId)?.cookieCount ?? 0;
  }
  const profile = preview?.profiles?.find((candidate) => candidate.id === profileId);
  return (
    profile?.scopes?.find((entry) => entry.host === scope)?.cookieCount ??
    (scope === preview?.host ? (profile?.cookieCount ?? 0) : 0)
  );
}

/** Cookies a profile would import for `scope`, or for its first scope when it lacks that one. */
function cookieCountInPicker(
  preview: ChromeCookiePreviewResult | null,
  profileId: string,
  scope: string,
): number {
  const offered = cookieScopeOptions(preview, profileId);
  return cookieCountFor(preview, profileId, offered.includes(scope) ? scope : (offered[0] ?? ""));
}

/** How a scope reads in a sentence. */
function cookieScopeName(scope: string): string {
  return scope === CHROME_COOKIE_ALL_SITES_SCOPE ? "all websites" : scope;
}

export interface BrowserPaneProps {
  /** Native view key: the session ID or a session-scoped browser tab ID. */
  conversationId: string;
  agentBrowser?: boolean;
  /** Whether this pane may attach and position its native view. */
  active?: boolean;
  /** Extra classes for the measuring placeholder wrapper. */
  className?: string;
  /** Take keyboard focus once, in the address bar or the page, then call `onFocused`. */
  focus?: "address" | "page" | null;
  onFocused?: () => void;
}

/**
 * Keeps the agent relay alive for a conversation and, once a native browser
 * view is attached, keeps that view positioned over a measuring placeholder.
 */
export function BrowserPane({
  conversationId,
  className,
  agentBrowser = true,
  active = true,
  focus = null,
  onFocused,
}: BrowserPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const addressBarRef = useRef<HTMLInputElement | null>(null);
  const lastBoundsRef = useRef<Bounds | null>(null);
  const browserSupported = supportsBrowser();
  // A detached view still exists; only `active` controls its attachment.
  const [hasView, setHasView] = useState(false);

  // Toolbar state. `currentUrl` tracks the real view URL EXCEPT while the user
  // edits the input (urlEditingRef gates the stomp); canGoBack/Forward drive
  // the arrow buttons.
  const [currentUrl, setCurrentUrl] = useState("");
  const [navigationError, setNavigationError] = useState<string | null>(null);
  // The page the view has loaded, not what's typed in the address bar: the design tools'
  // requests name it.
  const [pageUrl, setPageUrl] = useState("");
  // Ref (not state): read synchronously in the url-changed listener; a
  // stale-closure state read would race.
  const urlEditingRef = useRef(false);
  // Counts url-changed events, so an older view probe can't overwrite a newer URL.
  const urlEventsRef = useRef(0);
  const urlEditsRef = useRef(0);
  const submitSequenceRef = useRef(0);
  useEffect(
    () => () => {
      submitSequenceRef.current++;
    },
    [conversationId],
  );
  const [canGoBack, setCanGoBack] = useState(false);
  const [canGoForward, setCanGoForward] = useState(false);
  // Design-mode toggle: on, the main process injects the in-page picker; submit
  // routing lives in AppShell. This flag only drives the button + enable/disable IPC.
  const [designMode, setDesignMode] = useState(false);
  const designModeRef = useRef(false);
  // The design tools replace design mode, unless the shell predates them or they can't load.
  const hasDesignTools = useHasDesktopDesignTools();
  // "Import cookies from Chrome" needs BOTH an optional bridge method (older
  // shells predate it) and the shell's desktop feature flag (macOS only).
  const [cookieImportSupported, setCookieImportSupported] = useState(false);
  const [cookiePreview, setCookiePreview] = useState<ChromeCookiePreviewResult | null>(null);
  const [cookieProfileId, setCookieProfileId] = useState("");
  const [cookieScope, setCookieScope] = useState("");
  const [cookieBusy, setCookieBusy] = useState(false);

  // Switching profiles can drop the scope the previous one offered; keep the
  // state (and therefore the import request) on a scope that is still offered.
  useEffect(() => {
    if (!cookiePreview) return;
    const offered = cookieScopeOptions(cookiePreview, cookieProfileId);
    if (offered.length === 0 || offered.includes(cookieScope)) return;
    // A site the user picked narrows to another site when one is offered, never to every website.
    const siteScope = offered.find((scope) => scope !== CHROME_COOKIE_ALL_SITES_SCOPE);
    setCookieScope(cookieScope && siteScope ? siteScope : offered[0]);
  }, [cookiePreview, cookieProfileId, cookieScope]);

  // Track existence through create/close events and a remount probe.
  // Host attach/detach events do not change whether the retained page exists.
  useEffect(() => {
    if (!browserSupported) return;
    const bridge = getBridge();
    if (!bridge) return;
    let cancelled = false;
    let viewChanged = false;
    let unsubscribeIdentity: (() => void) | undefined;
    const urlEvents = urlEventsRef.current;
    const urlEdits = urlEditsRef.current;
    // Recover the address after sign-out; loading it requires Reload or Enter.
    const restoreSavedAddress = () => {
      const saved = readSavedBrowserPage(conversationId);
      if (
        !saved ||
        cancelled ||
        viewChanged ||
        urlEventsRef.current !== urlEvents ||
        urlEditsRef.current !== urlEdits
      )
        return;
      setCurrentUrl(saved.url);
    };

    // A newer lifecycle event takes precedence over this asynchronous snapshot.
    void bridge.browserHasView?.(conversationId).then((r) => {
      if (cancelled || viewChanged || !r) return;
      if (!r.exists) {
        // A page open in another window stays there.
        if (!r.elsewhere) {
          // Keep the original edit/event guards across transient identity failures.
          unsubscribeIdentity = subscribeIdentity(restoreSavedAddress);
          void resolveIdentity()
            .then(restoreSavedAddress)
            .catch(() => {});
        }
        return;
      }
      setHasView(true);
      // A navigation since it asked brought its own URL and back/forward state.
      const fresh = urlEventsRef.current === urlEvents;
      const url = fresh ? r.url : undefined;
      if (url) setPageUrl(url);
      if (!urlEditingRef.current && url) setCurrentUrl(url);
      if (fresh) {
        setCanGoBack(!!r.canGoBack);
        setCanGoForward(!!r.canGoForward);
      }
    });

    const unsubCreated = bridge.onBrowserViewCreated?.((payload) => {
      if (payload.conversationId !== conversationId) return;
      viewChanged = true;
      setHasView(true);
    });
    const unsubClosed = bridge.onBrowserViewClosed?.((payload) => {
      if (payload.conversationId !== conversationId) return;
      viewChanged = true;
      setHasView(false);
      setPageUrl("");
      setCanGoBack(false);
      setCanGoForward(false);
    });
    return () => {
      cancelled = true;
      unsubscribeIdentity?.();
      unsubCreated?.();
      unsubClosed?.();
    };
  }, [conversationId, browserSupported]);

  // Live-track the real URL + back/forward via did-navigate listeners (redirects,
  // link clicks, agent nav all keep the bar honest), but never stomp the input
  // while the user is editing it (urlEditingRef).
  useEffect(() => {
    if (!browserSupported) return;
    const bridge = getBridge();
    if (!bridge) return;
    const unsubUrl = bridge.onBrowserUrlChanged?.((payload) => {
      if (payload.conversationId !== conversationId) return;
      urlEventsRef.current += 1;
      setPageUrl(payload.url);
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
  }, [conversationId, browserSupported]);

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
    setNavigationError(null);
    const sequence = ++submitSequenceRef.current;
    void fetchSessionHostId(browserViewOwnerId(conversationId))
      .then(async (hostId) => {
        const options = await previewHostOptions(hostId, navUrl);
        return withPreviewHostRetry(
          browserViewOwnerId(conversationId),
          hostId,
          async (retryOptions) => {
            if (sequence !== submitSequenceRef.current) return null;
            return bridge.browserOpenOrNavigate!(conversationId, navUrl, undefined, {
              force: true,
              ...options,
              ...retryOptions,
            });
          },
        );
      })
      .then((result) => {
        if (!result || sequence !== submitSequenceRef.current) return;
        if (result.ok) setCurrentUrl(navUrl);
        else setNavigationError(result.error ?? "Unable to open this page.");
      })
      .catch(() => {
        if (sequence === submitSequenceRef.current) setNavigationError("Unable to open this page.");
      });
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
    if (!hasView) {
      submitUrl();
      return;
    }
    const bridge = getBridge();
    void bridge?.browserReload?.(conversationId);
  }, [conversationId, hasView, submitUrl]);

  // Desktop Reload (⌘R) from this toolbar reloads the page rather than the app;
  // Focus Address Bar (⌘L) selects the address. (A focused page reloads itself in the shell.)
  useDesktopCommand(
    "reload",
    active
      ? () => {
          if (!toolbarRef.current?.contains(document.activeElement)) return false;
          handleReload();
          return true;
        }
      : null,
    { priority: 10 },
  );
  useDesktopCommand(
    "focus-address-bar",
    active
      ? () => {
          addressBarRef.current?.focus();
          addressBarRef.current?.select();
        }
      : null,
  );
  // Chords in the toolbar's native titles; DOM tooltips here could sit under the page.
  const chord = (label: string, keys: readonly string[]) =>
    supportsDesktopCommands() ? `${label} (${shortcutText(keys)})` : label;

  const handleDevTools = useCallback(() => {
    const bridge = getBridge();
    void bridge?.openBrowserDevTools?.(conversationId);
  }, [conversationId]);

  const handleToggleDesignMode = useCallback(() => {
    const bridge = getBridge();
    if (!bridge) return;
    if (designMode) {
      designModeRef.current = false;
      void bridge.browserDisableDesignMode?.(conversationId);
      setDesignMode(false);
    } else {
      designModeRef.current = true;
      void bridge.browserEnableDesignMode?.(conversationId);
      setDesignMode(true);
    }
  }, [conversationId, designMode]);

  // Probe the shell's feature gate once: the bridge method alone doesn't mean
  // the machine can decrypt Chrome's cookies (Windows/Linux report false).
  useEffect(() => {
    if (!browserSupported) return;
    const bridge = getBridge();
    if (!bridge?.browserChromeCookiePreview || !bridge.browserChromeCookieImport) return;
    let cancelled = false;
    void getDesktopFeatures().then((features) => {
      if (!cancelled) setCookieImportSupported(features?.chromeCookieImport === true);
    });
    return () => {
      cancelled = true;
    };
  }, [browserSupported]);

  // Step 1: ask main what Chrome holds, for this site and overall (metadata only
  // — the Keychain prompt belongs to the import, not to opening the dialog).
  const handlePreviewChromeCookies = useCallback(async () => {
    const bridge = getBridge();
    if (!bridge?.browserChromeCookiePreview) return;
    setCookieBusy(true);
    try {
      const preview = await bridge.browserChromeCookiePreview(conversationId);
      if (!preview?.ok) {
        toast.error(preview?.error || "Couldn't look for Chrome cookies.");
        return;
      }
      const profiles = preview.profiles ?? [];
      const offered = cookieProfileOptions(preview);
      if (!preview.host || offered.length === 0) {
        toast.info(
          preview.allSitesProfiles
            ? "No Chrome cookies found."
            : `No Chrome cookies found for ${preview.host ?? "this site"}.`,
        );
        return;
      }
      // Prefer a profile that actually holds cookies for this page.
      const preferred = profiles.find((profile) => (profile.cookieCount ?? 0) > 0) ?? offered[0];
      setCookieProfileId(preferred.id);
      // Every website when the shell offers it, else this page's own site.
      setCookieScope(cookieScopeOptions(preview, preferred.id)[0] ?? preview.host);
      setCookiePreview({ ...preview, profiles });
    } catch {
      toast.error("Couldn't look for Chrome cookies.");
    } finally {
      setCookieBusy(false);
    }
  }, [conversationId]);

  // Step 2: the user confirmed in the dialog — copy and reload so the site
  // picks the session up immediately.
  const handleImportChromeCookies = useCallback(async () => {
    const bridge = getBridge();
    const preview = cookiePreview;
    if (!bridge?.browserChromeCookieImport || !preview?.host || !cookieProfileId) return;
    const offered = cookieScopeOptions(preview, cookieProfileId);
    const scope = offered.includes(cookieScope) ? cookieScope : (offered[0] ?? preview.host);
    setCookieBusy(true);
    try {
      const result = await bridge.browserChromeCookieImport(conversationId, {
        profileId: cookieProfileId,
        expectedHost: preview.host,
        scope,
      });
      if (!result?.ok) {
        // Declining the shell's own confirmation is not an error.
        if (result?.cancelled) return;
        toast.error(result?.error || "Couldn't import cookies from Chrome.");
        return;
      }
      const imported = result.imported ?? 0;
      const unusable = (result.skipped ?? 0) + (result.failed ?? 0);
      if (imported === 0) {
        // Keep the dialog open — another Chrome profile may hold the session.
        toast.error(
          (result.failed ?? 0) > 0
            ? `Chrome's cookies for ${cookieScopeName(scope)} couldn't be saved in this browser session.`
            : `Chrome had no usable cookies for ${cookieScopeName(scope)}.`,
        );
        return;
      }
      setCookiePreview(null);
      const suffix = imported === 1 ? "cookie" : "cookies";
      toast.success(
        `Imported ${imported} ${suffix} from Chrome${unusable > 0 ? ` (${unusable} couldn't be used)` : ""}.`,
      );
      void bridge.browserReload?.(conversationId);
    } catch {
      toast.error("Couldn't import cookies from Chrome.");
    } finally {
      setCookieBusy(false);
    }
  }, [conversationId, cookiePreview, cookieProfileId, cookieScope]);

  // If the view goes away (closed) while design mode is on, drop the pressed
  // state so the button doesn't lie — the injected picker died with the view.
  useEffect(() => {
    if ((!hasView || !active) && designMode) {
      designModeRef.current = false;
      setDesignMode(false);
    }
  }, [hasView, active, designMode]);

  // The tools loading late (their chunk retried) replace design mode: turn its picker off.
  useEffect(() => {
    if (!hasDesignTools || !designMode) return;
    designModeRef.current = false;
    void getBridge()?.browserDisableDesignMode?.(conversationId);
    setDesignMode(false);
  }, [hasDesignTools, designMode, conversationId]);

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
    if (!browserSupported || !hasView || !active) return;
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
      if (designModeRef.current) void getBridge()?.browserDisableDesignMode?.(conversationId);
      // Detach whatever is currently active (this pane owned it). The view
      // survives in the registry; only an explicit close destroys it.
      try {
        void getBridge()?.browserSetActive?.(null);
      } catch {
        /* swallow — window may be tearing down */
      }
    };
  }, [conversationId, browserSupported, hasView, active, syncBounds]);

  // A tab just opened takes focus in its address bar, as in a browser; one the
  // user returns to gets it back where it was. The page needs its view attached.
  useEffect(() => {
    if (focus === "address") {
      addressBarRef.current?.focus();
    } else if (focus === "page" && hasView && active) {
      void getBridge()?.browserSetActive?.(conversationId, { focus: true });
    } else {
      return;
    }
    onFocused?.();
  }, [focus, hasView, active, conversationId, onFocused]);

  // Reconcile bounds every frame while shown (cheap: same-rect setBounds is a
  // no-op + we dedupe via lastBoundsRef). Catches position-only shifts that
  // ResizeObserver misses (it only fires on size). try/catch so a throw during
  // teardown still schedules the next frame — else the rAF chain dies and the
  // overlay strands.
  useEffect(() => {
    if (!browserSupported || !hasView || !active) return;
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
  }, [browserSupported, hasView, active, syncBounds]);

  // Defense-in-depth against a hung rAF chain: ResizeObserver (size), window
  // resize, and visibilitychange (tab-back, where rAFs were throttled) each
  // recover bounds on the next interaction.
  useEffect(() => {
    if (!browserSupported || !hasView || !active || !containerRef.current) return;
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
  }, [browserSupported, hasView, active, syncBounds]);

  // No browser-capable shell (plain web build, or a desktop build too old for
  // the embedded browser): render nothing so there's no empty split pane. The
  // relay is a no-op there anyway.
  if (!browserSupported) return null;

  // Flex column: the toolbar (shrink-0) is ALWAYS the first child so the URL bar
  // is reachable from a cold start (typing a URL creates the view on demand →
  // hasView flips true → the measuring container mounts). Gating the toolbar
  // on hasView was a deadlock: no page → no toolbar → no way to open a page.
  //
  // LAYOUT TRAP (verified): the native view paints OVER the measured containerRef
  // rect, so the toolbar must be ABOVE it, never inside. The container is the LAST
  // child, flex-1 min-h-0 (not inset:0), so getBoundingClientRect covers only the
  // region below the toolbar and the view fills exactly that.
  //
  // Content area below the always-present toolbar switches on hasView:
  //   - hasView: the measuring `containerRef` placeholder. `containerRef` +
  //     syncBounds + the rAF/observer effects (all gated on hasView above)
  //     keep the native view positioned over the container. NO containerRef is
  //     mounted while !hasView, so nothing measures an empty div.
  //   - !hasView: a centered hint. Back/forward are already disabled off
  //     canGoBack/canGoForward (both false with no view); Reload can open a saved
  //     address, while DevTools stays disabled until a view exists. The
  //     URL bar stays editable so the user can open the first page.
  const cookieProfiles = cookieProfileOptions(cookiePreview);
  const selectedCookieProfile =
    cookieProfiles.find((profile) => profile.id === cookieProfileId) ?? cookieProfiles[0];
  const cookieScopes = cookieScopeOptions(cookiePreview, selectedCookieProfile?.id ?? "");
  const activeCookieScope = cookieScope || cookieScopes[0] || "";
  const allSitesCookieScope = activeCookieScope === CHROME_COOKIE_ALL_SITES_SCOPE;
  /** Cookies the selected profile holds for one scope. */
  const countForScope = (scope: string) =>
    cookieCountFor(cookiePreview, selectedCookieProfile?.id ?? "", scope);
  /** How a scope reads in the picker. */
  const cookieScopeLabel = (scope: string) =>
    scope === CHROME_COOKIE_ALL_SITES_SCOPE ? "All websites" : scope;
  return (
    <div
      className={cn("flex min-h-0 min-w-0 flex-col", className)}
      data-browser-pane-conversation={conversationId}
    >
      <div
        ref={toolbarRef}
        className="flex shrink-0 items-center gap-1 border-border border-b bg-card px-2 py-1.5"
      >
        <button
          type="button"
          onClick={handleBack}
          disabled={!canGoBack}
          aria-label="Go back"
          title={chord("Back", [MOD_KEY, "["])}
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <ChevronLeftIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={handleForward}
          disabled={!canGoForward}
          aria-label="Go forward"
          title={chord("Forward", [MOD_KEY, "]"])}
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <ChevronRightIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={handleReload}
          disabled={!hasView && !currentUrl.trim()}
          aria-label="Reload"
          title={chord("Reload", RELOAD_KEYS)}
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
          ref={addressBarRef}
          placeholder="Enter a URL"
          aria-label="Address bar"
          title={chord("Address bar", ADDRESS_BAR_KEYS)}
          onChange={(e) => {
            urlEditsRef.current++;
            setCurrentUrl(e.target.value);
          }}
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
          className="h-6 min-w-0 flex-1 rounded-md border border-input bg-transparent px-2 text-foreground text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring dark:bg-input"
        />
        <button
          type="button"
          onClick={handleDevTools}
          disabled={!hasView}
          aria-label="Toggle DevTools"
          title="Toggle DevTools"
          className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
        >
          <WrenchIcon className="size-4" />
        </button>
        {cookieImportSupported && (
          <button
            type="button"
            onClick={handlePreviewChromeCookies}
            disabled={!hasView || cookieBusy}
            aria-label="Import cookies from Chrome"
            title="Import cookies from Chrome so you're signed in here too"
            className="flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40"
          >
            <CookieIcon className="size-4" />
          </button>
        )}
        <DesktopDesignTools
          viewId={conversationId}
          hasView={hasView}
          active={active}
          url={pageUrl}
          container={containerRef}
        />
        <button
          type="button"
          onClick={handleToggleDesignMode}
          hidden={hasDesignTools}
          disabled={!hasView}
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
      {navigationError && (
        <div role="alert" className="px-2 py-1 text-destructive text-xs">
          {navigationError}
        </div>
      )}
      {hasView ? (
        /* Measuring region — the native WebContentsView paints over this.
           flex-1 min-h-0 so it fills everything BELOW the toolbar; its rect
           is what syncBounds() pushes. Mounted only while hasView so the
           effects never measure an empty div.
           ml-1 (4px, matching the WorkspacePanel resize handle's w-1) shifts
           this box right so the native view's rect.left clears the handle at
           the panel's left edge; a native compositor layer ignores z-index and
           would otherwise paint over the handle, making it impossible to grab.
           Margin, not padding: getBoundingClientRect() excludes margin but
           includes padding, so only a margin moves rect.left. */
        <div ref={containerRef} className="ml-1 min-h-0 min-w-0 flex-1" />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-card px-6 py-8 text-center text-muted-foreground text-ui">
          <OpenElsewhereHint viewId={conversationId}>
            {currentUrl.trim()
              ? "Click Reload or press Enter in the address bar to load this page."
              : `Enter a URL above to get started${agentBrowser ? " — the agent will open pages here too." : "."}`}
          </OpenElsewhereHint>
        </div>
      )}
      {/* DialogContent mounts SuppressBrowserView, hiding the native view. */}
      <Dialog
        open={cookiePreview !== null}
        onOpenChange={(open) => {
          if (!open && !cookieBusy) setCookiePreview(null);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Import Chrome cookies</DialogTitle>
            <DialogDescription>
              Agent-controlled tabs share these cookies and can act as you on{" "}
              {allSitesCookieScope ? "every imported site." : "this site."}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-muted-foreground text-xs">Site</span>
              {cookieScopes.length > 1 ? (
                <Select value={activeCookieScope} onValueChange={setCookieScope}>
                  <SelectTrigger aria-label="Site scope" className="w-full">
                    <SelectValue>{cookieScopeLabel(activeCookieScope)}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {cookieScopes.map((scope) => (
                      <SelectItem key={scope} value={scope}>
                        {cookieScopeLabel(scope)} ({countForScope(scope)})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <span className="break-all font-medium text-sm">
                  {cookieScopeLabel(activeCookieScope)}
                </span>
              )}
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-muted-foreground text-xs">Chrome profile</span>
              {cookieProfiles.length > 1 ? (
                <Select value={cookieProfileId} onValueChange={setCookieProfileId}>
                  <SelectTrigger aria-label="Chrome profile" className="w-full">
                    <SelectValue>
                      {selectedCookieProfile?.name} (
                      {cookieCountInPicker(
                        cookiePreview,
                        selectedCookieProfile?.id ?? "",
                        activeCookieScope,
                      )}
                      )
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {cookieProfiles.map((profile) => (
                      <SelectItem key={profile.id} value={profile.id}>
                        {profile.name} (
                        {cookieCountInPicker(cookiePreview, profile.id, activeCookieScope)})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <span className="font-medium text-sm">
                  {selectedCookieProfile?.name} (
                  {cookieCountInPicker(
                    cookiePreview,
                    selectedCookieProfile?.id ?? "",
                    activeCookieScope,
                  )}
                  )
                </span>
              )}
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            In Keychain, choose <strong className="text-foreground">Allow</strong>, not Always
            Allow.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCookiePreview(null)} disabled={cookieBusy}>
              Cancel
            </Button>
            <Button onClick={handleImportChromeCookies} disabled={cookieBusy}>
              {cookieBusy ? "Importing…" : "Import cookies"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
