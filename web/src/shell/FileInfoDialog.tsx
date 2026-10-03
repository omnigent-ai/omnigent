import { useContext } from "react";
import { canReceiveFocus, FilesPanelFocusContext, type FileRowInfo } from "./FileRowActions";
import { CopyPathButton } from "./CopyPathButton";
import { gitStatusLabel, formatBytes } from "./fileStatusUtils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

function formatModifiedAt(modifiedAt: number | null | undefined): string {
  if (modifiedAt == null) return "Not available";
  const date = new Date(modifiedAt * 1000);
  return Number.isNaN(date.getTime())
    ? "Not available"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function changeStatus(info: FileRowInfo): string {
  if (!info.status) return "Not available";
  const label = gitStatusLabel(info.status);
  return info.lastKnown ? `${label} (last known)` : label;
}

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
          </DialogHeader>
          <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-ui">
            <dt className="text-muted-foreground">Name</dt>
            <dd className="min-w-0 break-words">{info.name}</dd>
            <dt className="text-muted-foreground">Path</dt>
            <dd className="flex min-w-0 items-start gap-2">
              <code className="min-w-0 flex-1 break-all select-text">{info.path}</code>
              <TooltipProvider>
                <CopyPathButton path={info.path} />
              </TooltipProvider>
            </dd>
            <dt className="text-muted-foreground">Kind</dt>
            <dd>{info.kind}</dd>
            {info.kind === "file" && (
              <>
                <dt className="text-muted-foreground">Size</dt>
                <dd>
                  {info.bytes == null || !Number.isFinite(info.bytes)
                    ? "Not available"
                    : formatBytes(info.bytes)}
                </dd>
              </>
            )}
            <dt className="text-muted-foreground">Modified</dt>
            <dd>{formatModifiedAt(info.modifiedAt)}</dd>
            <dt className="text-muted-foreground">Changes</dt>
            <dd>{changeStatus(info)}</dd>
            {info.linesAdded != null && (
              <>
                <dt className="text-muted-foreground">Lines added</dt>
                <dd>{info.linesAdded}</dd>
              </>
            )}
            {info.linesRemoved != null && (
              <>
                <dt className="text-muted-foreground">Lines removed</dt>
                <dd>{info.linesRemoved}</dd>
              </>
            )}
          </dl>
        </DialogContent>
      )}
    </Dialog>
  );
}
