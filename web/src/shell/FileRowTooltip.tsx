import { useEffect, useState, type ReactElement } from "react";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  formatBytes,
  formatEditedAbsolute,
  formatEditedRelative,
  gitStatusLabel,
} from "./fileStatusUtils";

interface FileRowTooltipProps {
  children: ReactElement;
  path: string;
  status?: WorkspaceChangedFile["status"];
  linesAdded?: number | null;
  linesRemoved?: number | null;
  modifiedAt: number | null;
  bytes: number | null;
}

/** Accessible, collision-aware metadata for a workspace file row. */
export function FileRowTooltip({
  children,
  path,
  status,
  linesAdded,
  linesRemoved,
  modifiedAt,
  bytes,
}: FileRowTooltipProps) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    document.addEventListener("scroll", close, true);
    document.addEventListener("click", close);
    return () => {
      document.removeEventListener("scroll", close, true);
      document.removeEventListener("click", close);
    };
  }, [open]);

  const diff = [
    linesAdded !== null && linesAdded !== undefined ? `+${linesAdded}` : null,
    linesRemoved !== null && linesRemoved !== undefined ? `−${linesRemoved}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const details = [
    status ? gitStatusLabel(status) : null,
    diff || null,
    bytes !== null ? formatBytes(bytes) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Tooltip open={open} onOpenChange={setOpen}>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent
        side="bottom"
        align="start"
        collisionPadding={8}
        className="block max-w-[min(28rem,90vw)] space-y-0.5 px-3 py-2 text-xs"
      >
        <div className="truncate font-medium">{path}</div>
        {details && (
          <div className="truncate text-neutral-300 dark:text-muted-foreground">{details}</div>
        )}
        {modifiedAt !== null && (
          <div className="truncate text-neutral-300 dark:text-muted-foreground">
            Edited {formatEditedRelative(modifiedAt)} · {formatEditedAbsolute(modifiedAt)}
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
