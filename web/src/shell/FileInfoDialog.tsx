import { useContext } from "react";
import { canReceiveFocus, FilesPanelFocusContext, type FileRowInfo } from "./FileRowActions";
import { CopyPathButton } from "./CopyPathButton";
import { formatBytes } from "./fileStatusUtils";
import { isAbsoluteComposerPath } from "@/lib/composerContext";
import { fileTypeLabel } from "./fileTypeLabel";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

function formatModifiedAt(modifiedAt: number | null | undefined): string {
  if (modifiedAt == null) return "Not available";
  const date = new Date(modifiedAt * 1000);
  return Number.isNaN(date.getTime())
    ? "Not available"
    : new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      }).format(date);
}

function changeStatus(info: FileRowInfo): string {
  if (info.kind === "folder") return info.status ? "Contains changes" : "Not available";
  if (!info.status) return "Not available";

  const counts: string[] = [];
  if (info.linesAdded != null) counts.push(`+${info.linesAdded}`);
  if (info.linesRemoved != null) counts.push(`−${info.linesRemoved}`);
  const label =
    info.status === "created" ? "New file" : info.status === "modified" ? "Modified" : "Deleted";
  if (counts.length === 0) return label;
  return `${label} · ${counts.join(" ")} lines`;
}

const CHANGES_CAPTION =
  "With Git, changes compare the workspace with the last commit. Without Git, only file edits recorded this session are listed; shell edits aren't.";

export function FileInfoDialog({
  info,
  onOpenChange,
  returnFocus,
}: {
  info: FileRowInfo | null;
  onOpenChange: (open: boolean) => void;
  returnFocus: HTMLElement | null;
}) {
  const panelFocusRef = useContext(FilesPanelFocusContext);
  const title = info
    ? `${info.kind === "file" ? "File" : "Folder"} info${info.lastKnown ? " (last known)" : ""}`
    : "File info";

  return (
    <Dialog open={info !== null} onOpenChange={onOpenChange}>
      {info && (
        <DialogContent
          aria-describedby={undefined}
          onEscapeKeyDown={(event) => event.stopPropagation()}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const fallback = panelFocusRef?.current ?? null;
            if (canReceiveFocus(returnFocus)) {
              returnFocus.focus();
            } else if (canReceiveFocus(fallback)) {
              fallback.focus();
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <h3 className="break-words text-base font-medium">{info.name}</h3>
          </DialogHeader>
          <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-ui">
            <dt className="text-muted-foreground">
              {isAbsoluteComposerPath(info.path)
                ? "Host path"
                : info.path.includes("/")
                  ? "Path"
                  : "Location"}
            </dt>
            <dd className="flex min-w-0 items-start gap-2">
              <code className="min-w-0 flex-1 break-all select-text">
                {isAbsoluteComposerPath(info.path)
                  ? info.path
                  : info.path.includes("/")
                    ? info.path
                    : "Workspace root"}
              </code>
              <TooltipProvider>
                <CopyPathButton path={info.path} tooltipSide="left" />
              </TooltipProvider>
            </dd>
            <dt className="text-muted-foreground">Type</dt>
            <dd>{fileTypeLabel(info.name, info.kind)}</dd>
            {info.kind === "file" && !info.lastKnown && (
              <>
                <dt className="text-muted-foreground">Size</dt>
                <dd>
                  {info.bytes == null || !Number.isFinite(info.bytes) || info.bytes < 0
                    ? "Not available"
                    : `${formatBytes(info.bytes)} (${new Intl.NumberFormat().format(info.bytes)} bytes)`}
                </dd>
              </>
            )}
            {!info.lastKnown && (
              <>
                <dt className="text-muted-foreground">Modified</dt>
                <dd>{formatModifiedAt(info.modifiedAt)}</dd>
              </>
            )}
            <dt className="text-muted-foreground">Changes</dt>
            <dd>
              {changeStatus(info)}
              <p className="mt-1 text-muted-foreground text-xs">{CHANGES_CAPTION}</p>
            </dd>
          </dl>
        </DialogContent>
      )}
    </Dialog>
  );
}
