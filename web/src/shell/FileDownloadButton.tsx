// Hover-reveal download button for workspace file rows.
//
// Designed to be placed inside a parent element that carries the Tailwind
// `group` class; visibility is driven by `group-hover:opacity-100` so the
// parent row does not need any JS hover state.  Tooltip rendering relies on
// a `TooltipProvider` ancestor — callers must ensure one exists (e.g. a
// single provider wrapping the entire list).

import { DownloadIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { downloadWorkspaceFile } from "@/hooks/useFileContent";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

interface FileDownloadButtonProps {
  /** Session ID used to fetch file content. */
  conversationId: string;
  /** Workspace-relative file path, e.g. ``"src/main.py"``. */
  path: string;
}

/**
 * A small download icon button that is hidden until the parent row is hovered.
 *
 * The parent row must have the Tailwind ``group`` class for hover-reveal to
 * work.  Tooltip rendering requires a ``TooltipProvider`` ancestor.
 *
 * On success the browser's native save dialog is triggered.  On failure the
 * icon turns red for three seconds with a "Download failed" tooltip, then
 * resets automatically.
 *
 * :param conversationId: Session ID used to fetch file content from the API.
 * :param path: Workspace-relative path of the file to download.
 */
export function FileDownloadButton({ conversationId, path }: FileDownloadButtonProps) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState(false);
  const errorTimerRef = useRef<number>(0);

  useEffect(
    () => () => {
      window.clearTimeout(errorTimerRef.current);
    },
    [],
  );

  const handleClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (downloading) return;
    setDownloading(true);
    setDownloadError(false);
    try {
      await downloadWorkspaceFile(conversationId, path);
    } catch {
      setDownloadError(true);
      window.clearTimeout(errorTimerRef.current);
      errorTimerRef.current = window.setTimeout(() => setDownloadError(false), 3000);
    } finally {
      setDownloading(false);
    }
  };

  const filename = path.split("/").pop() ?? path;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`Download ${filename}`}
          className={cn(
            "shrink-0 cursor-pointer rounded p-0.5 transition-opacity",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            downloadError
              ? "text-destructive opacity-100"
              : "text-muted-foreground hover:bg-muted hover:text-foreground opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
          )}
          onClick={handleClick}
          disabled={downloading}
        >
          <DownloadIcon className="size-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {downloadError ? "Download failed" : "Download"}
      </TooltipContent>
    </Tooltip>
  );
}
