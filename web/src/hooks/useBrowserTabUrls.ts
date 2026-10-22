import { useEffect, useMemo, useState } from "react";
import { getBrowserPaneBridge } from "@/components/BrowserPane/browserPaneBridge";
import { browserViewId } from "@/hooks/useBrowserTabs";

/** Current URL of every open browser tab in a session, including unmounted tabs. */
export function useBrowserTabUrls(
  conversationId: string,
  tabIds: readonly string[],
): Record<string, string> {
  const [urlsByViewId, setUrlsByViewId] = useState<Record<string, string>>({});
  const tabKey = tabIds.join("\n");

  useEffect(() => {
    const bridge = getBrowserPaneBridge();
    if (!bridge) return;
    const viewIds = new Set(
      tabKey === "" ? [] : tabKey.split("\n").map((tabId) => browserViewId(conversationId, tabId)),
    );
    // A navigation event is newer than any snapshot still in flight.
    const updatedByEvent = new Set<string>();
    let cancelled = false;

    const remember = (viewId: string, url: string) =>
      setUrlsByViewId((current) =>
        current[viewId] === url ? current : { ...current, [viewId]: url },
      );
    const keepOnly = (keep: (viewId: string) => boolean) =>
      setUrlsByViewId((current) => {
        const kept = Object.entries(current).filter(([viewId]) => keep(viewId));
        return kept.length === Object.keys(current).length ? current : Object.fromEntries(kept);
      });
    // A removed tab id may come back (the agent tab); it must not show its old URL.
    keepOnly((viewId) => viewIds.has(viewId));

    const unsubscribeUrl = bridge.onBrowserUrlChanged?.(({ conversationId: viewId, url }) => {
      if (!viewIds.has(viewId)) return;
      updatedByEvent.add(viewId);
      remember(viewId, url);
    });
    const unsubscribeClosed = bridge.onBrowserViewClosed?.(({ conversationId: viewId }) => {
      if (!viewIds.has(viewId)) return;
      updatedByEvent.add(viewId);
      keepOnly((knownViewId) => knownViewId !== viewId);
    });
    for (const viewId of viewIds) {
      void bridge
        .browserHasView?.(viewId)
        .then((view) => {
          if (cancelled || updatedByEvent.has(viewId) || !view?.exists || !view.url) return;
          remember(viewId, view.url);
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
      unsubscribeUrl?.();
      unsubscribeClosed?.();
    };
  }, [conversationId, tabKey]);

  return useMemo(() => {
    const urls: Record<string, string> = {};
    for (const tabId of tabKey === "" ? [] : tabKey.split("\n")) {
      const url = urlsByViewId[browserViewId(conversationId, tabId)];
      if (url) urls[tabId] = url;
    }
    return urls;
  }, [conversationId, tabKey, urlsByViewId]);
}
