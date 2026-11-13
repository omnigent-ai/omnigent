import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInertRef } from "@/hooks/useInertRef";
import { cn } from "@/lib/utils";

/** Move one portal host between layouts without remounting its editor or drafts. */
export function ResponsiveViewerSlot({
  mobile,
  mobileTarget,
  target = null,
  hidden = false,
  children,
}: {
  mobile: boolean;
  mobileTarget: HTMLElement | null;
  /** Desktop mount point elsewhere in the layout; the slot's own place is the fallback. */
  target?: HTMLElement | null;
  /** Keeps the viewer laid out but out of sight and reach, e.g. while another tab shows. */
  hidden?: boolean;
  children: ReactNode;
}) {
  const desktopTarget = useRef<HTMLDivElement>(null);
  const setDesktopTarget = useInertRef(hidden, desktopTarget);
  const focusedElement = useRef<HTMLElement | null>(null);
  // Scroll offsets inside the viewer, recorded as they change: a detached node reads 0.
  const scrollOffsets = useRef(new Map<Element, { top: number; left: number }>());
  const [host] = useState(() => {
    const element = document.createElement("div");
    element.className = "flex min-h-0 flex-1 flex-col overflow-hidden";
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
    const rememberScroll = (event: Event) => {
      // Forget elements a file switch removed, so the map never holds detached editors.
      for (const element of scrollOffsets.current.keys()) {
        if (!host.contains(element)) scrollOffsets.current.delete(element);
      }
      if (event.target instanceof Element && event.target.getClientRects().length > 0) {
        const { scrollTop: top, scrollLeft: left } = event.target;
        scrollOffsets.current.set(event.target, { top, left });
      }
    };
    document.addEventListener("focusin", rememberFocus, true);
    host.addEventListener("focusout", forgetFocus, true);
    host.addEventListener("scroll", rememberScroll, true);
    return () => {
      document.removeEventListener("focusin", rememberFocus, true);
      host.removeEventListener("focusout", forgetFocus, true);
      host.removeEventListener("scroll", rememberScroll, true);
    };
  }, [host]);

  useLayoutEffect(() => {
    const destination = mobile && mobileTarget ? mobileTarget : (target ?? desktopTarget.current);
    if (!destination || host.parentElement === destination) return;
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
    destination.appendChild(host);
    // Moving a node resets its scroll; put back where the reader was.
    for (const [element, offset] of scrollOffsets.current) {
      if (!host.contains(element)) {
        scrollOffsets.current.delete(element);
      } else {
        element.scrollTop = offset.top;
        element.scrollLeft = offset.left;
      }
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
  }, [host, mobile, mobileTarget, target]);

  useLayoutEffect(() => () => host.remove(), [host]);

  return (
    <>
      {/* Not display:none: a hidden editor keeps its size, so it returns without relayout. */}
      <div
        ref={setDesktopTarget}
        className={cn(
          "flex min-h-0 flex-col",
          hidden ? "pointer-events-none invisible absolute inset-0" : "flex-1",
        )}
      />
      {createPortal(children, host)}
    </>
  );
}
