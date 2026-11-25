// The desktop shell's embedded browser, as the browser tabs' navigation owners
// see it: shells with the embedded browser report URL changes; the oldest report nothing.

import { browserViewOwnerId } from "@/lib/browserTabs";
import { fetchSessionHostId } from "@/lib/fetchSessionHostId";
import {
  browserUrlEventsSupported,
  onBrowserUrlChanged,
  openBrowserViewUrl,
  probeBrowserView,
  supportsBrowser,
} from "@/lib/nativeBridge";
import { previewHostOptions, withPreviewHostRetry } from "@/lib/previewHostOptions";

import type { BrowserNavigationMode } from "./browserTabNavigation";
import type { BrowserNavigationShell } from "./browserTabNavigationRegistry";

/** Whether this shell says where a page went, so Back and Forward can replay it. */
export function desktopBrowserNavigationMode(): BrowserNavigationMode {
  return browserUrlEventsSupported() ? "replay" : "untracked";
}

/** This shell's embedded browser; null where there is none, such as a plain browser tab. */
export function desktopBrowserNavigationShell(): BrowserNavigationShell | null {
  if (!supportsBrowser()) return null;
  const mode = desktopBrowserNavigationMode();
  return {
    mode,
    openOrNavigate: (viewKey, url, isCurrent) => loadOnSessionHost(viewKey, url, isCurrent),
    probe: probeBrowserView,
    onUrlChanged: (listener) =>
      onBrowserUrlChanged(({ conversationId, url }) => listener({ viewKey: conversationId, url })),
  };
}

/**
 * Load `url` in a view on its session's host, as the address bar does, while `isCurrent`.
 * Rejects when the shell refuses the load.
 */
async function loadOnSessionHost(
  viewKey: string,
  url: string,
  isCurrent: () => boolean,
): Promise<void> {
  if (!isCurrent()) return;
  const sessionId = browserViewOwnerId(viewKey);
  const hostId = await fetchSessionHostId(sessionId);
  const options = await previewHostOptions(hostId, url);
  const result = await withPreviewHostRetry(sessionId, hostId, async (retryOptions) =>
    // The tab may have closed, or the user moved on, while the host was looked up.
    isCurrent()
      ? openBrowserViewUrl(viewKey, url, { force: true, ...options, ...retryOptions })
      : null,
  );
  if (result && !result.ok) throw new Error(result.error ?? "Unable to open this page.");
}
