// Keep a workspace-hosted shell on Omnigent when Databricks navigation or an
// auth hand-back lands the main frame on the bare workspace root. Kept free of
// Electron imports so navigation behavior can be tested with a fake webContents.

import { databricksWorkspaceUiUrl } from "./url.ts";

export const MAX_ROOT_BOUNCES = 1;

/** The parts of a shell window's webContents the bounce watches and drives. */
export interface RootBounceWebContents {
  on: ((event: "did-navigate", listener: (event: unknown, url: string) => void) => unknown) &
    ((
      event: "did-navigate-in-page",
      listener: (event: unknown, url: string, isMainFrame: boolean) => void,
    ) => unknown) &
    ((event: "did-finish-load", listener: () => void) => unknown);
  getURL: () => string;
  loadURL: (url: string) => Promise<unknown>;
}

/** Return a URL's origin, or null when it is not an absolute URL. */
function originOf(rawUrl: string): string | null {
  try {
    return new URL(rawUrl).origin;
  } catch {
    return null;
  }
}

/**
 * Register the bare-root redirect for one shell window.
 *
 * One redirect is allowed until a non-root page on the pinned origin finishes.
 * This prevents a workspace without the mount from looping forever if
 * ``/omnigent`` redirects back to ``/``.
 */
export function registerWorkspaceRootBounce(
  webContents: RootBounceWebContents,
  pinnedOrigin: () => string | null,
): void {
  let rootBounces = 0;

  function bounceIfNeeded(rawUrl: string): void {
    const target = databricksWorkspaceUiUrl(rawUrl);
    const pinned = pinnedOrigin();
    if (!target || !pinned || originOf(rawUrl) !== pinned) return;
    if (rootBounces >= MAX_ROOT_BOUNCES) return;
    rootBounces++;
    void webContents.loadURL(target).catch(() => {});
  }

  // Successful full navigations include server redirects and form-POST auth
  // hand-backs, which can bypass will-navigate.
  webContents.on("did-navigate", (_event, url) => bounceIfNeeded(url));

  // pushState, replaceState, and history navigation do not load a document.
  webContents.on("did-navigate-in-page", (_event, url, isMainFrame) => {
    if (isMainFrame) bounceIfNeeded(url);
  });

  webContents.on("did-finish-load", () => {
    const current = webContents.getURL();
    if (originOf(current) !== pinnedOrigin()) return;
    if (databricksWorkspaceUiUrl(current) === null) rootBounces = 0;
  });
}
