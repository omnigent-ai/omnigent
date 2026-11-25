// A tab's content in a host of its own, which moves between the sidebar and an app pane, so the
// tab keeps its tree, scroll and edits as it moves. It renders once its host is in the page,
// so nothing mounts detached and measures nothing.

import { type ReactNode, useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";

export function MovableTabPanel({
  target,
  children,
}: {
  /**
   * Where the tab shows now: the sidebar's content, or its app pane. Null before the first place
   * waits; null later takes the content off the page, keeping its state for the next place.
   */
  target: HTMLElement | null;
  children: ReactNode;
}) {
  const [host] = useState(() => {
    const element = document.createElement("div");
    element.className = "contents";
    return element;
  });
  const [placed, setPlaced] = useState(false);
  useLayoutEffect(() => {
    if (target === null) {
      host.remove();
      return;
    }
    if (host.parentElement !== target) target.appendChild(host);
    setPlaced(true);
  }, [host, target]);
  useLayoutEffect(() => () => host.remove(), [host]);
  return placed ? createPortal(children, host) : null;
}
