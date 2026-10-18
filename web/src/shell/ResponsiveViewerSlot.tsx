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
  const [host] = useState(() => {
    const element = document.createElement("div");
    element.className = "flex min-h-0 flex-1 flex-col overflow-hidden";
    return element;
  });

  useLayoutEffect(() => {
    const target = mobile && mobileTarget ? mobileTarget : desktopTarget.current;
    if (!target || host.parentElement === target) return;
    // Reparenting can clear focus and the native selection even though React
    // keeps the editor instance. Preserve both once for the layout transition.
    const active = host.contains(document.activeElement)
      ? (document.activeElement as HTMLElement)
      : null;
    const selection = document.getSelection();
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
