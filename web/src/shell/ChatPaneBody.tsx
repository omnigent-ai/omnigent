// The session's web chat, rendered in its column or, while a split shows beside it, in the chat
// pane's body: the same transcript and composer either way.

import { type ReactNode, useCallback, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { CHAT_PANE_BODY_ATTRIBUTE } from "@/lib/paneSurfaces";

interface Placement {
  sessionId: string;
  host: HTMLElement | null;
  marked: boolean;
}

/** Puts `container` where `placement` says, keeping the focus and scroll a move would drop. */
function place(container: HTMLElement, slot: HTMLElement | null, placement: Placement): void {
  const { sessionId, host, marked } = placement;
  // `contents`: in its column, the chat's layout stays as it was.
  container.className = host ? "flex min-h-0 flex-1 flex-col" : "contents";
  if (host || marked) container.setAttribute(CHAT_PANE_BODY_ATTRIBUTE, sessionId);
  else container.removeAttribute(CHAT_PANE_BODY_ATTRIBUTE);
  const target = host ?? slot;
  if (!target || container.parentElement === target) return;
  const focused = document.activeElement;
  const keepsFocus = focused instanceof HTMLElement && container.contains(focused);
  const scrolled = [...container.querySelectorAll<HTMLElement>("*")]
    .filter((element) => element.scrollTop !== 0 || element.scrollLeft !== 0)
    .map((element) => [element, element.scrollTop, element.scrollLeft] as const);
  target.appendChild(container);
  for (const [element, top, left] of scrolled) {
    element.scrollTop = top;
    element.scrollLeft = left;
  }
  if (keepsFocus) focused.focus({ preventScroll: true });
}

/**
 * Render `children` in place, or in `host`, the chat pane's body, when there is one. Where the
 * panes view shows the chat, `marked` marks it in place too, so its keys reach the panes before a
 * first split. The marker names the session, so only its own view's pane keys take the chat's.
 *
 * The chat renders into one element that moves between the two, so the transcript and composer
 * keep their state, focus and scroll as a split opens or closes beside them.
 */
export function ChatPaneBody({
  sessionId,
  host,
  marked = false,
  children,
}: {
  sessionId: string;
  host: HTMLElement | null;
  marked?: boolean;
  children: ReactNode;
}) {
  const [container] = useState(() => document.createElement("div"));
  const slot = useRef<HTMLDivElement | null>(null);
  const placement = useRef<Placement>({ sessionId, host, marked });
  placement.current = { sessionId, host, marked };
  // The slot attaches before the chat's own effects run, so they find it in the page.
  const attachSlot = useCallback(
    (element: HTMLDivElement | null) => {
      slot.current = element;
      if (element) place(container, element, placement.current);
    },
    [container],
  );
  useLayoutEffect(() => {
    place(container, slot.current, { sessionId, host, marked });
  }, [container, sessionId, host, marked]);
  // In a pane's body, it goes with the chat.
  useLayoutEffect(() => () => container.remove(), [container]);
  return (
    <>
      <div ref={attachSlot} className="contents" />
      {createPortal(children, container)}
    </>
  );
}
