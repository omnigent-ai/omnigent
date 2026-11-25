// The body of an app pane: where the workspace sidebar renders the tab the pane shows.

import { useCallback, useEffect, useRef, useState } from "react";

import { APP_PANE_HOST_ATTRIBUTE, setAppPaneHost } from "./appPanes";
import type { TabId } from "./workspaceTabs/workspacePlace";

// What can take the keyboard in a tab, in document order; a field the tab asks to focus goes first.
const TABBABLE = [
  "input:not([type=hidden]):not([disabled])",
  "textarea:not([disabled])",
  "select:not([disabled])",
  "button:not([disabled])",
  "a[href]",
  "[contenteditable=true]",
  "[tabindex]:not([tabindex='-1'])",
].join(", ");

/** Give the tab in `host` the keyboard: its own autofocus field, else the first it can take. */
function focusTab(host: HTMLElement): void {
  const candidates = [
    ...host.querySelectorAll<HTMLElement>("[autofocus], [data-autofocus]"),
    ...host.querySelectorAll<HTMLElement>(TABBABLE),
  ];
  const target = candidates.find((element) => element.getClientRects().length > 0) ?? host;
  target.focus({ preventScroll: true });
}

export function AppPaneHost({
  conversationId,
  tabId,
  onFocusIn,
  active,
  focusOnActivate,
  focusRequest,
  onFocusRequestTaken,
}: {
  conversationId: string;
  tabId: TabId;
  /** Focus moved into the tab: it renders from the sidebar, so only the DOM sees it here. */
  onFocusIn: () => void;
  /** Whether this is the split's focused pane in a shown view. */
  active: boolean;
  /** Whether becoming the focused pane, by a keyboard move, takes the keyboard too. */
  focusOnActivate: () => boolean;
  /** An explicit request to focus this pane, taken once. */
  focusRequest?: number;
  onFocusRequestTaken?: (id: number) => void;
}) {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const onFocusInRef = useRef(onFocusIn);
  onFocusInRef.current = onFocusIn;
  const focusOnActivateRef = useRef(focusOnActivate);
  focusOnActivateRef.current = focusOnActivate;
  const onFocusRequestTakenRef = useRef(onFocusRequestTaken);
  onFocusRequestTakenRef.current = onFocusRequestTaken;
  const ref = useCallback(
    (next: HTMLElement | null) => {
      setElement(next);
      setAppPaneHost(conversationId, tabId, next);
    },
    [conversationId, tabId],
  );
  useEffect(() => {
    if (!element) return;
    const focusIn = () => onFocusInRef.current();
    element.addEventListener("focusin", focusIn);
    return () => element.removeEventListener("focusin", focusIn);
  }, [element]);
  // The sidebar portals the tab in after the host registers: focus waits for it. Each effect
  // reads the host itself, since the portal may commit after this render but before they run.
  const contentVersion = useHostContentVersion(element);

  // Moved to as the split's focused pane, the tab takes the keyboard, as a terminal pane does,
  // unless the person had put it somewhere else meanwhile.
  const wasActive = useRef(active);
  useEffect(() => {
    if (!element || element.childElementCount === 0) return;
    if (active && !wasActive.current && !element.contains(document.activeElement)) {
      if (focusOnActivateRef.current()) focusTab(element);
    }
    wasActive.current = active;
  }, [active, element, contentVersion]);

  const takenRequest = useRef<number | null>(null);
  useEffect(() => {
    if (!element || element.childElementCount === 0 || !active || focusRequest === undefined) {
      return;
    }
    if (takenRequest.current === focusRequest) return;
    takenRequest.current = focusRequest;
    if (!element.contains(document.activeElement)) focusTab(element);
    onFocusRequestTakenRef.current?.(focusRequest);
  }, [active, element, contentVersion, focusRequest]);

  return (
    <div
      ref={ref}
      {...{ [APP_PANE_HOST_ATTRIBUTE]: tabId }}
      // Holds the keyboard when the tab has nothing of its own to take it.
      tabIndex={-1}
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden bg-background outline-none"
    />
  );
}

/** Changes as the tab portals in or out of `host`, so focus can follow it. */
function useHostContentVersion(host: HTMLElement | null): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!host) return;
    const observer = new MutationObserver(() => setVersion((current) => current + 1));
    observer.observe(host, { childList: true });
    return () => observer.disconnect();
  }, [host]);
  return version;
}
