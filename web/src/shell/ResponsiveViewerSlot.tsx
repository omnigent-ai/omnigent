import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { confirmGoogleDocsNavigation } from "@/components/GoogleDocsPane/editorLifecycle";
import { Button } from "@/components/ui/button";

const FILL_HOST = "flex min-h-0 flex-1 flex-col overflow-hidden";
const FILL_TARGET = "flex min-h-0 flex-1 flex-col";

/**
 * Move one portal host between layouts without remounting its editor or drafts. `contents`
 * gives the host and its desktop place no box, for children that size themselves.
 */
export function ResponsiveViewerSlot({
  mobile,
  mobileTarget,
  layout = "fill",
  onPlacementChange,
  children,
}: {
  mobile: boolean;
  mobileTarget: HTMLElement | null;
  layout?: "fill" | "contents";
  /** The host must retain the previous target until the move completes. */
  onPlacementChange?: (mobile: boolean) => void;
  children: ReactNode;
}) {
  const desktopTarget = useRef<HTMLDivElement>(null);
  const focusedElement = useRef<HTMLElement | null>(null);
  const [moveBlocked, setMoveBlocked] = useState(false);
  const [moveAttempt, setMoveAttempt] = useState(0);
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
    const target = mobile && mobileTarget ? mobileTarget : desktopTarget.current;
    if (!target) return;
    if (host.parentElement === target) {
      setMoveBlocked(false);
      onPlacementChange?.(mobile && mobileTarget !== null);
      return;
    }
    // Reparenting an existing iframe can reload its cross-origin browsing context.
    if (
      host.parentElement &&
      host.querySelector("iframe[data-google-docs-editor]") &&
      !confirmGoogleDocsNavigation("Move the document editor to another layout")
    ) {
      setMoveBlocked(true);
      return;
    }
    setMoveBlocked(false);
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
    onPlacementChange?.(mobile && mobileTarget !== null);
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
  }, [host, mobile, mobileTarget, moveAttempt, onPlacementChange]);

  useLayoutEffect(() => () => host.remove(), [host]);

  const retryMove = moveBlocked && (
    <div role="status" className="p-3 text-sm">
      Google Docs is still open in the previous layout. Check that it has saved before moving.
      <Button size="sm" variant="outline" onClick={() => setMoveAttempt((value) => value + 1)}>
        Move editor
      </Button>
    </div>
  );
  return (
    <>
      <div ref={desktopTarget} className={layout === "contents" ? "contents" : FILL_TARGET}>
        {!mobile && retryMove}
      </div>
      {mobile && mobileTarget && retryMove && createPortal(retryMove, mobileTarget)}
      {createPortal(children, host)}
    </>
  );
}
