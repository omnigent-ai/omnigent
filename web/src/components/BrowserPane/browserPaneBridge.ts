import { supportsBrowser } from "@/lib/nativeBridge";

/** Renderer CSS-pixel bounds pushed to the main process (converted to window
 *  DIPs there via the host zoom factor). */
export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
  devicePixelRatio?: number;
}

/** Result shape shared by the history-navigation bridge calls. */
export interface NavResult {
  ok: boolean;
  canGoBack?: boolean;
  canGoForward?: boolean;
  error?: string;
}

/** Subset of `window.omnigentDesktop` used by the browser pane and tab labels.
 *  Every method is optional; an older shell may predate the browser feature. */
export interface BrowserPaneBridge {
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
    url?: string;
    canGoBack?: boolean;
    canGoForward?: boolean;
  }>;
}

export function getBrowserPaneBridge(): BrowserPaneBridge | null {
  if (!supportsBrowser()) return null;
  const w = window as unknown as { omnigentDesktop?: BrowserPaneBridge };
  return w.omnigentDesktop ?? null;
}
