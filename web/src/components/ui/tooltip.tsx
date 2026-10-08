"use client";

import * as React from "react";
import * as TooltipPrimitive from "radix-ui/tooltip";

import { getEmbedRoot } from "@/lib/host";
import { cn } from "@/lib/utils";
import { CompactKbd } from "@/components/ui/kbd";

type TooltipShortcut = readonly string[] | readonly (readonly string[])[];

function TooltipShortcutKeys({ shortcut }: { shortcut: TooltipShortcut }) {
  const groups = Array.isArray(shortcut[0])
    ? (shortcut as readonly (readonly string[])[])
    : [shortcut as readonly string[]];

  return (
    <span
      aria-hidden="true"
      data-slot="shortcut-keys"
      className="flex items-center gap-0.5 whitespace-nowrap"
    >
      {groups.map((keys, groupIndex) => (
        <React.Fragment key={`${groupIndex}-${keys.join("-")}`}>
          {groupIndex > 0 && <span className="text-white/50">+</span>}
          <span className="inline-flex items-center gap-0.5">
            {keys.map((key, keyIndex) => (
              <CompactKbd key={`${keyIndex}-${key}`}>{key}</CompactKbd>
            ))}
          </span>
        </React.Fragment>
      ))}
    </span>
  );
}

function TooltipProvider({
  // Keep incidental pointer movement from flashing tooltips. Callers can still
  // override the delay per provider.
  delayDuration = 600,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  );
}

function Tooltip({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />;
}

function TooltipTrigger({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />;
}

function TooltipContent({
  className,
  sideOffset = 8,
  children,
  shortcut,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content> & {
  shortcut?: TooltipShortcut;
}) {
  return (
    <TooltipPrimitive.Portal container={getEmbedRoot() ?? undefined}>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-50 inline-flex w-fit max-w-xs origin-(--radix-tooltip-content-transform-origin) items-center gap-1.5 rounded-lg bg-neutral-900 px-2 py-1.5 text-sm text-white shadow-tooltip dark:bg-popover dark:text-popover-foreground has-data-[slot=kbd]:pr-1.5 has-data-[slot=shortcut-keys]:gap-2.5 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 **:data-[slot=kbd]:relative **:data-[slot=kbd]:isolate **:data-[slot=kbd]:z-50 **:data-[slot=kbd]:rounded-sm **:data-[slot=kbd]:border-neutral-600 **:data-[slot=kbd]:bg-neutral-800 **:data-[slot=kbd]:text-neutral-300 data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-95 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className,
        )}
        {...props}
      >
        {children}
        {shortcut && shortcut.length > 0 && <TooltipShortcutKeys shortcut={shortcut} />}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  );
}

export { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger };
