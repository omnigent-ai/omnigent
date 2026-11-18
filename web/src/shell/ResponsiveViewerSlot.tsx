import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** Move one portal host between layouts without remounting its editor or drafts. */
export function ResponsiveViewerSlot({
  mobile,
  mobileTarget,
  children,
}: {
  mobile: boolean;
  mobileTarget: HTMLElement | null;
  children: ReactNode;
}) {
  const desktopTarget = useRef<HTMLDivElement>(null);
  const focusedElement = useRef<HTMLElement | null>(null);
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
    document.addEventListener("focusin", rememberFocus, true);
    host.addEventListener("focusout", forgetFocus, true);
    return () => {
      document.removeEventListener("focusin", rememberFocus, true);
      host.removeEventListener("focusout", forgetFocus, true);
    };
  }, [host]);

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
      <div ref={desktopTarget} className="flex min-h-0 flex-1 flex-col" />
      {createPortal(children, host)}
    </>
  );
}
