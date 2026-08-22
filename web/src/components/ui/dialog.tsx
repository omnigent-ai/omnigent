import * as React from "react";
import * as DialogPrimitive from "radix-ui/dialog";

import { getEmbedRoot } from "@/lib/host";
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

/**
 * The scrollable middle of a dialog. `DialogContent` wraps whatever sits
 * between the header and the footer in one of these, so overflow scrolls
 * *inside* the panel and the footer stays visible without the caller doing
 * anything. It is a flex column as well as a scroller, so a caller that hands
 * its own `flex-1 min-h-0` child (a form, a Tabs) keeps that child's scroller
 * as the only one that ever moves.
 */
function DialogBody({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-body"
      className={cn("flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto", className)}
      {...props}
    />
  );
}

function isSlot(node: React.ReactNode, slot: React.ElementType): boolean {
  return React.isValidElement(node) && node.type === slot;
}

/**
 * Flatten keyless `<>…</>` wrappers: a bare fragment is not a layout box, so a
 * caller that groups a conditional branch's fields *and* its footer in one is
 * still handing the dialog those as siblings. Keyed fragments (a `.map`) are
 * left alone — flattening would drop the key.
 */
function flattenFragments(children: React.ReactNode): React.ReactNode[] {
  const items: React.ReactNode[] = [];
  React.Children.forEach(children, (child) => {
    if (React.isValidElement(child) && child.type === React.Fragment && child.key === null) {
      items.push(...flattenFragments((child.props as { children?: React.ReactNode }).children));
    } else {
      items.push(child);
    }
  });
  return items;
}

/**
 * Split a dialog's children into the leading header, the middle, and the
 * trailing footer. Nulls are kept in place so a conditional child toggling
 * cannot shift its siblings' positional keys and remount them.
 */
function splitDialogChildren(children: React.ReactNode): {
  header: React.ReactNode[];
  body: React.ReactNode[];
  footer: React.ReactNode[];
} {
  const items = flattenFragments(children);
  let start = 0;
  while (start < items.length && isSlot(items[start], DialogHeader)) start += 1;
  let end = items.length;
  while (end > start && isSlot(items[end - 1], DialogFooter)) end -= 1;
  return { header: items.slice(0, start), body: items.slice(start, end), footer: items.slice(end) };
}

// Key by the child's original position so the identity survives the split
// boundary moving between renders.
function keyed(nodes: React.ReactNode[], offset: number): React.ReactNode[] {
  return nodes.map((node, index) => <React.Fragment key={offset + index}>{node}</React.Fragment>);
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean;
}) {
  const { header, body, footer } = splitDialogChildren(children);
  // A body that is already a single `DialogBody` is left alone rather than
  // nested inside a second scroller.
  const wrappedBody =
    body.length === 1 && isSlot(body[0], DialogBody) ? (
      body[0]
    ) : (
      <DialogBody>{keyed(body, header.length)}</DialogBody>
    );
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        // Height safety is the SHARED component's job, on every platform.
        // `top`/`max-h` come from `--omnigent-dialog-*` (index.css), which
        // resolve against the live visual viewport less the safe-area insets —
        // so the panel is centered in, and fits inside, the area the user can
        // actually see rather than the taller layout viewport that `vh` and
        // `top-1/2` measure. They stay Tailwind utilities (not inline style) so
        // a caller that deliberately positions its own panel — the command
        // palette's `top-1/4` — still wins through twMerge.
        className={cn(
          "fixed top-[var(--omnigent-dialog-center)] left-1/2 z-50 flex max-h-[var(--omnigent-dialog-max-height)] w-full max-w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 rounded-[30px] bg-popover p-6 text-ui text-popover-foreground duration-150 ease-[cubic-bezier(0.16,1,0.3,1)] outline-none sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 shadow-dialog",
          className,
        )}
        {...props}
      >
        {/* Hide the native browser view while this dialog is open (#3980).
            Inside Content so it mounts only while the dialog is open. */}
        <SuppressBrowserView />
        {keyed(header, 0)}
        {body.length > 0 ? wrappedBody : null}
        {keyed(footer, header.length + body.length)}
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
    <div
      data-slot="dialog-header"
      className={cn("flex shrink-0 flex-col gap-2", className)}
      {...props}
    />
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
      // `pb` reserves the home indicator / Android gesture bar / native bottom
      // bar (`--omnigent-inset-bottom`) on top of the normal 1.5rem, so the
      // buttons are never under system chrome. 0px off mobile, so desktop is
      // unchanged. Listed after `p-6` so twMerge keeps both.
      className={cn(
        "-mx-6 -mb-6 flex shrink-0 flex-col-reverse gap-2 rounded-b-xl border-t bg-muted/50 p-6 pb-[max(1.5rem,var(--omnigent-inset-bottom))] sm:flex-row sm:justify-end",
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
  DialogBody,
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
