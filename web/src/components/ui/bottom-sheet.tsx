// Bottom sheet for phones: a Radix dialog that slides up from the bottom edge,
// with a grab handle and title that can be dragged down to dismiss it.

import * as React from "react";
import * as DialogPrimitive from "radix-ui/dialog";

import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { cn } from "@/lib/utils";
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from "@/components/ui/dialog";

/** How far the handle must be dragged down before releasing it dismisses the sheet. */
const SWIPE_DISMISS_PX = 80;

const BottomSheetContext = React.createContext<{ open: boolean; close: () => void }>({
  open: false,
  close: () => {},
});

/** Controlled only: a swipe closes the sheet through `onOpenChange(false)`. */
function BottomSheet({
  open,
  onOpenChange,
  ...props
}: Omit<
  React.ComponentProps<typeof DialogPrimitive.Root>,
  "open" | "defaultOpen" | "onOpenChange"
> & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const sheet = React.useMemo(
    () => ({ open, close: () => onOpenChange(false) }),
    [open, onOpenChange],
  );
  return (
    <BottomSheetContext.Provider value={sheet}>
      <Dialog open={open} onOpenChange={onOpenChange} {...props} />
    </BottomSheetContext.Provider>
  );
}

/** Puts the sheet back at rest, e.g. after a drag that didn't dismiss it. */
function resetSheetOffset(sheet: HTMLElement | null, animate: boolean) {
  if (!sheet) return;
  sheet.style.transition = animate ? "transform 200ms ease-out" : "";
  sheet.style.transform = "";
}

/**
 * Grab handle and title: the sheet follows a downward drag on them, and
 * releasing past the threshold closes it, so it slides out from where it was
 * dropped.
 */
function BottomSheetHandle({
  sheetRef,
  title,
}: {
  sheetRef: React.RefObject<HTMLElement | null>;
  title: React.ReactNode;
}) {
  const { open, close } = React.useContext(BottomSheetContext);
  const start = React.useRef<{ pointerId: number; y: number } | null>(null);
  // The handle stays mounted through the exit animation, and a reopen in that
  // window reuses it: a drag the sheet closed under must not carry over.
  React.useEffect(() => {
    if (!open) start.current = null;
  }, [open]);
  const distance = (event: React.PointerEvent<HTMLElement>) =>
    Math.max(0, event.clientY - (start.current?.y ?? event.clientY));
  const cancel = () => {
    if (!start.current) return;
    start.current = null;
    resetSheetOffset(sheetRef.current, true);
  };
  return (
    <div
      className="shrink-0 cursor-grab touch-none select-none"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        start.current = { pointerId: event.pointerId, y: event.clientY };
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        const sheet = sheetRef.current;
        if (start.current?.pointerId !== event.pointerId || !sheet) return;
        sheet.style.transition = "none";
        sheet.style.transform = `translateY(${distance(event)}px)`;
      }}
      onPointerUp={(event) => {
        if (start.current?.pointerId !== event.pointerId) return;
        if (distance(event) > SWIPE_DISMISS_PX) {
          start.current = null;
          close();
        } else {
          cancel();
        }
      }}
      onPointerCancel={cancel}
      onLostPointerCapture={cancel}
    >
      <div
        aria-hidden="true"
        className="mx-auto mt-2 mb-2.5 h-[5px] w-[38px] rounded-full bg-muted-foreground/30"
      />
      <DialogTitle className="min-h-0 px-6 pb-2 text-[17px] leading-6">{title}</DialogTitle>
    </div>
  );
}

function BottomSheetContent({
  className,
  children,
  title,
  style,
  onOpenAutoFocus,
  ...props
}: Omit<React.ComponentProps<typeof DialogPrimitive.Content>, "title"> & {
  /** Heading shown under the grab handle; also the dialog's accessible name. */
  title: React.ReactNode;
}) {
  const contentRef = React.useRef<HTMLDivElement>(null);
  // Reopening during the exit animation reuses the node a swipe moved down.
  const { open } = React.useContext(BottomSheetContext);
  React.useLayoutEffect(() => {
    if (open) resetSheetOffset(contentRef.current, false);
  }, [open]);
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={contentRef}
        data-slot="bottom-sheet-content"
        aria-describedby={undefined}
        className={cn(
          "fixed inset-x-0 bottom-0 z-50 flex max-h-[85dvh] flex-col rounded-t-[26px] bg-popover text-ui text-popover-foreground shadow-dialog outline-none duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] data-open:animate-in data-open:slide-in-from-bottom data-closed:animate-out data-closed:slide-out-to-bottom",
          className,
        )}
        style={{ paddingBottom: "var(--omnigent-safe-bottom, 0px)", ...style }}
        // Focus the sheet itself rather than its first row, so opening it
        // doesn't paint a focus ring on a row the user hasn't reached yet.
        onOpenAutoFocus={(event) => {
          onOpenAutoFocus?.(event);
          if (event.defaultPrevented) return;
          event.preventDefault();
          contentRef.current?.focus();
        }}
        {...props}
      >
        <SuppressBrowserView />
        <BottomSheetHandle sheetRef={contentRef} title={title} />
        {children}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

export { BottomSheet, BottomSheetContent };
