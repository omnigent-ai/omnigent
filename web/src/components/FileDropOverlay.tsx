// Drop cue for a file drag, portalled into the drop target so it covers exactly
// the region that accepts the drop. pointer-events-none keeps it out of the way.

import { createPortal } from "react-dom";
import { Paperclip, Upload } from "lucide-react";
import { cn } from "@/lib/utils";

export function FileDropOverlay({
  container,
  label = "Drop files here",
  subtext,
  destination,
  variant = "attach",
}: {
  container: HTMLElement;
  label?: string;
  subtext?: string;
  destination?: string;
  variant?: "upload" | "attach";
}) {
  const Icon = variant === "upload" ? Upload : Paperclip;
  return createPortal(
    <div
      aria-hidden="true"
      className={cn(
        "pointer-events-none absolute inset-0 z-40 flex items-center justify-center",
        variant === "upload"
          ? "m-2 rounded-md border-2 border-dashed border-info bg-background/90 text-info"
          : "bg-background/60 backdrop-blur-[2px]",
      )}
      data-testid="file-drop-overlay"
      data-drop-variant={variant}
    >
      {variant === "attach" && (
        <div className="absolute inset-3 rounded-2xl border-2 border-dashed border-ring" />
      )}
      <div className="mx-3 flex min-w-0 flex-col items-center gap-2 rounded-md border border-border bg-card p-4 text-center text-ui shadow-composer">
        <Icon className="size-6" />
        <span className="font-medium">{label}</span>
        {destination && (
          <span className="max-w-full truncate" title={destination}>
            to {destination}
          </span>
        )}
        {subtext && <span className="text-muted-foreground">{subtext}</span>}
      </div>
    </div>,
    container,
  );
}
