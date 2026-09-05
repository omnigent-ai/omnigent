import * as React from "react";
import * as DialogPrimitive from "radix-ui/dialog";

import { getEmbedRoot } from "@/lib/host";
import { isIOSShell } from "@/lib/nativeBridge";
import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { XIcon } from "lucide-react";

function Dialog({ ...props }: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />;
}

function DialogTrigger({ ...props }: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />;
}

function DialogPortal({ ...props }: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return (
    <DialogPrimitive.Portal
      data-slot="dialog-portal"
      container={getEmbedRoot() ?? undefined}
      {...props}
    />
  );
}

function DialogClose({ ...props }: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />;
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 isolate z-50 bg-background/60 duration-150 ease-[cubic-bezier(0.16,1,0.3,1)] supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className,
      )}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  style,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean;
}) {
  // On the iOS shell the layout viewport stays full-height when the soft
  // keyboard opens (the native shell keeps the WKWebView full via
  // `.ignoresSafeArea(.keyboard)`), so a modal centered on `50%` and capped at
  // `85vh` sizes and positions against the whole screen — its lower half (and a
  // focused field) sit behind the keyboard. `useIOSViewportLock` publishes the
  // keyboard-aware visible height as `--omnigent-viewport-height` on :root, so
  // pin both the centering origin and the height cap to it (less the safe-area
  // insets) via inline style — inline beats callers' `max-h-[85vh]`/`top`
  // Tailwind classes (which `cn`'s twMerge would otherwise keep). No-op off iOS.
  const iosViewportStyle: React.CSSProperties = isIOSShell()
    ? {
        // Center within the visible viewport (not the full layout height).
        top: "calc(var(--omnigent-viewport-height, 100lvh) / 2)",
        // Cap to the visible area less both safe insets and a small margin, so
        // the modal can never extend behind the keyboard, notch, or home bar.
        maxHeight:
          "calc(var(--omnigent-viewport-height, 100lvh) - var(--omnigent-safe-top, 0px) - var(--omnigent-safe-bottom, 0px) - 1rem)",
      }
    : {};
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          "fixed top-1/2 left-1/2 z-50 grid w-full max-w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 rounded-[30px] bg-popover p-6 text-ui text-popover-foreground duration-150 ease-[cubic-bezier(0.16,1,0.3,1)] outline-none sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 shadow-dialog",
          className,
        )}
        style={{ ...iosViewportStyle, ...style }}
        {...props}
      >
        {/* Hide the native browser view while this dialog is open (#3980).
            Inside Content so it mounts only while the dialog is open. */}
        <SuppressBrowserView />
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close data-slot="dialog-close" asChild>
            <Button variant="ghost" className="absolute top-5 right-6" size="icon-lg">
              <XIcon className="size-5 text-muted-foreground" data-icon-size />
              <span className="sr-only">Close</span>
            </Button>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div data-slot="dialog-header" className={cn("flex flex-col gap-2", className)} {...props} />
  );
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean;
}) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "-mx-6 -mb-6 flex flex-col-reverse gap-2 rounded-b-xl border-t bg-muted/50 p-6 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">Close</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  );
}

function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
        "font-heading min-h-8 leading-[32px] text-ui text-[1.25em] pr-10 font-[600]",
        className,
      )}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn(
        "text-ui text-muted-foreground *:[a]:underline *:[a]:underline-offset-3 *:[a]:hover:text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
};
