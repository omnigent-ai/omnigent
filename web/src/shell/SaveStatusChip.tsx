// The file toolbar's auto-save status: Unsaved, Saving…, Saved, Save failed, or waiting
// for the runner. Nothing while idle.

import { AlertTriangleIcon, CheckIcon, CloudOffIcon, Loader2Icon } from "lucide-react";
import type { Ref } from "react";

import { cn } from "@/lib/utils";
import type { SaveStatus } from "./codeViewerHelpers";

/** `chipRef` rather than `ref`, which React 18 keeps out of a function component's props. */
export function SaveStatusChip({
  status,
  chipRef,
}: {
  status: SaveStatus;
  chipRef?: Ref<HTMLSpanElement>;
}) {
  if (status === "idle") return null;
  return (
    <span
      ref={chipRef}
      aria-live="polite"
      title={
        status === "offline"
          ? "Runner offline — your changes will save when it reconnects"
          : undefined
      }
      className={cn(
        "mr-1 flex shrink-0 items-center gap-1 whitespace-nowrap text-sm",
        status === "error" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      {status === "unsaved" && (
        <>
          <span className="size-1.5 rounded-full bg-muted-foreground/70" />
          Unsaved
        </>
      )}
      {status === "saving" && (
        <>
          <Loader2Icon className="size-3 animate-spin" />
          Saving…
        </>
      )}
      {status === "saved" && (
        <>
          <CheckIcon className="size-3 text-green-500" />
          Saved
        </>
      )}
      {status === "error" && (
        <>
          <AlertTriangleIcon className="size-3" />
          Save failed
        </>
      )}
      {status === "offline" && (
        <>
          <CloudOffIcon className="size-3" />
          Unsaved
        </>
      )}
    </span>
  );
}
