import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const FILL_HOST = "flex min-h-0 flex-1 flex-col overflow-hidden";
const FILL_TARGET = "flex min-h-0 flex-1 flex-col";

/**
 * Move one portal host between layouts without remounting its editor or drafts. `contents`
 * gives the host and its desktop place no box, for children that size themselves. With
 * `keepsScroll`, what was scrolled inside the host stays where it was across a move.
 */
export function ResponsiveViewerSlot({
  mobile,
  mobileTarget,
  layout = "fill",
  keepsScroll = false,
  children,
}: {
  mobile: boolean;
  mobileTarget: HTMLElement | null;
  layout?: "fill" | "contents";
  keepsScroll?: boolean;
  children: ReactNode;
}) {
  const desktopTarget = useRef<HTMLDivElement>(null);
  const focusedElement = useRef<HTMLElement | null>(null);
  // Each scrolled element's last offset, read as it scrolls: by the move, CSS may have
  // hidden the old layout and reset it already.
  const scrollOffsets = useRef(new Map<Element, { top: number; left: number }>());
  // The layout is fixed for the slot's life, as the host is.
  const [host] = useState(() => {
    const element = document.createElement("div");
    element.className = layout === "contents" ? "contents" : FILL_HOST;
    return element;
  });

  useLayoutEffect(() => {
    const rememberFocus = (event: FocusEvent) => {
      focusedElement.current =
        event.target instanceof HTMLElement && host.contains(event.target) ? event.target : null;
    };
    const forgetFocus = (event: FocusEvent) => {
      // CSS can hide the old layout before React moves the host. Retain only
      // that implicit blur, not a deliberate blur or focus outside the viewer.
      if (
        event.relatedTarget ||
        (event.target instanceof HTMLElement && event.target.getClientRects().length > 0)
      ) {
        focusedElement.current = null;
      }
    };
    document.addEventListener("focusin", rememberFocus, true);
    host.addEventListener("focusout", forgetFocus, true);
    return () => {
      document.removeEventListener("focusin", rememberFocus, true);
      host.removeEventListener("focusout", forgetFocus, true);
    };
  }, [host]);

  useLayoutEffect(() => {
    if (!keepsScroll) return;
    const offsets = scrollOffsets.current;
    // Scroll events don't bubble, but the capture phase sees every one inside the host.
    const rememberScroll = (event: Event) => {
      const element = event.target;
      if (!(element instanceof Element)) return;
      if (element.scrollTop === 0 && element.scrollLeft === 0) offsets.delete(element);
      else offsets.set(element, { top: element.scrollTop, left: element.scrollLeft });
    };
    host.addEventListener("scroll", rememberScroll, true);
    return () => {
      host.removeEventListener("scroll", rememberScroll, true);
      offsets.clear();
    };
  }, [host, keepsScroll]);

  useLayoutEffect(() => {
    const target = mobile && mobileTarget ? mobileTarget : desktopTarget.current;
    if (!target || host.parentElement === target) return;
    // Reparenting can clear focus and the native selection even though React
    // keeps the editor instance. Preserve both once for the layout transition.
    const activeElement = document.activeElement;
    const active = host.contains(activeElement)
      ? (activeElement as HTMLElement)
      : activeElement === document.body && host.contains(focusedElement.current)
        ? focusedElement.current
        : null;
    const input =
      active instanceof HTMLTextAreaElement ||
      (active instanceof HTMLInputElement && active.selectionStart !== null)
        ? active
        : null;
    const inputSelection = input && {
      start: input.selectionStart,
      end: input.selectionEnd,
      direction: input.selectionDirection ?? undefined,
    };
    const selection = input ? null : document.getSelection();
    const savedSelection =
      selection?.anchorNode &&
      selection.focusNode &&
      host.contains(selection.anchorNode) &&
      host.contains(selection.focusNode)
        ? {
            anchor: selection.anchorNode,
            anchorOffset: selection.anchorOffset,
            focus: selection.focusNode,
            focusOffset: selection.focusOffset,
          }
        : null;
    target.appendChild(host);
    // Moving a node resets the scroll of everything inside it, without a scroll event.
    for (const [element, offset] of scrollOffsets.current) {
      if (!host.contains(element)) {
        scrollOffsets.current.delete(element);
        continue;
      }
      element.scrollTop = offset.top;
      element.scrollLeft = offset.left;
    }
    active?.focus({ preventScroll: true });
    if (input && inputSelection) {
      input.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction);
    }
    if (savedSelection && selection) {
      selection.setBaseAndExtent(
        savedSelection.anchor,
        savedSelection.anchorOffset,
        savedSelection.focus,
        savedSelection.focusOffset,
      );
    }
  }, [host, mobile, mobileTarget]);

  useLayoutEffect(() => () => host.remove(), [host]);

  return (
    <>
      <div ref={desktopTarget} className={layout === "contents" ? "contents" : FILL_TARGET} />
      {createPortal(children, host)}
    </>
  );
}
