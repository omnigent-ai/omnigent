// Copy-path button for workspace file rows and the files header.
//
// Two presentations from one component: rows pass `revealOnHover` so the icon
// is hidden until the parent row (which must carry the Tailwind `group` class)
// is hovered, matching FileDownloadButton; the header omits it so the icon is
// always visible next to the hidden-files eye. Tooltip rendering relies on a
// `TooltipProvider` ancestor — callers must ensure one exists.

import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface CopyPathButtonProps {
  /** Path to place on the clipboard, e.g. ``"src/main.py"``. */
  path: string;
  /** Tooltip text at rest, e.g. ``"Copy path"``. */
  label?: string;
  /**
   * Hide the icon until the parent row is hovered. The parent must carry the
   * Tailwind ``group`` class. Omit for always-visible placements (the header).
   */
  revealOnHover?: boolean;
}

/**
 * A small copy icon button that puts *path* on the clipboard.
 *
 * Feedback is transient and in place: the icon becomes a check for two
 * seconds on success, or turns red with a "Copy failed" tooltip for three.
 * No toast — the button is often one of many in a list, so the confirmation
 * belongs on the row the user clicked rather than in a global surface.
 *
 * :param path: The path to copy.
 * :param label: Tooltip text at rest.
 * :param revealOnHover: Hide until the parent ``group`` row is hovered.
 */
export function CopyPathButton({
  path,
  label = "Copy path",
  revealOnHover = false,
}: CopyPathButtonProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const timerRef = useRef<number>(0);

  useEffect(
    () => () => {
      window.clearTimeout(timerRef.current);
    },
    [],
  );

  const handleClick = async (e: React.MouseEvent) => {
    // Defensive: today this button is a SIBLING of a row's clickable
    // element rather than a child, so nothing would propagate -- but the
    // directory rows in FolderTree are themselves buttons, so a future
    // placement inside one must not also open/expand the row.
    e.stopPropagation();
    window.clearTimeout(timerRef.current);
    try {
      await copyText(path);
      setCopyError(false);
      setCopied(true);
      timerRef.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
      setCopyError(true);
      timerRef.current = window.setTimeout(() => setCopyError(false), 3000);
    }
  };

  // Accessible name carries the basename, not the whole path — matching
  // FileDownloadButton. A full path read out on every row is noise for a
  // screen reader, and it makes the name collide with the folder-toggle
  // buttons that legitimately contain directory segments.
  const filename = path.split("/").filter(Boolean).pop() ?? path;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`${label}: ${filename}`}
          className={cn(
            "shrink-0 cursor-pointer rounded p-0.5 transition-opacity",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            copyError
              ? "text-destructive opacity-100"
              : copied
                ? "text-green-600 opacity-100 dark:text-green-400"
                : cn(
                    "text-muted-foreground hover:bg-muted hover:text-foreground",
                    // Feedback states stay visible even on a hover-reveal row,
                    // so a click never leaves the confirmation invisible.
                    revealOnHover && "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
                  ),
          )}
          onClick={handleClick}
          data-testid="copy-path-button"
        >
          {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {copyError ? "Copy failed" : copied ? "Copied" : label}
      </TooltipContent>
    </Tooltip>
  );
}
