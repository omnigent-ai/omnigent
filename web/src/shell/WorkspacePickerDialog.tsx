import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { WorkspacePicker, isNavigablePath } from "./WorkspacePicker";

export interface WorkspacePickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (path: string) => void;
  onCancel?: () => void;
  hostId: string | null;
  initialPath?: string;
  workspacePath?: string;
  occupancyForPath?: (absolutePath: string) => number;
  title?: string;
  description?: string;
}

/** Canonical full-screen workspace browser used by every production caller. */
export function WorkspacePickerDialog({
  open,
  onOpenChange,
  onConfirm,
  onCancel,
  hostId,
  initialPath,
  workspacePath,
  occupancyForPath,
  title = "Select working directory",
  description = "Choose a folder to use as the working directory.",
}: WorkspacePickerDialogProps) {
  function handleOpenChange(next: boolean) {
    if (!next) onCancel?.();
    onOpenChange(next);
  }

  function handleConfirm(path: string) {
    onConfirm(path);
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="max-w-[min(64rem,calc(100vw-2rem))] border-0 bg-transparent p-0 shadow-none sm:max-w-[min(64rem,calc(100vw-2rem))]"
        data-testid="workspace-picker-dialog"
      >
        <DialogHeader className="sr-only">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <WorkspacePicker
          hostId={hostId}
          initialPath={initialPath && isNavigablePath(initialPath) ? initialPath : undefined}
          workspacePath={workspacePath}
          occupancyForPath={occupancyForPath}
          onSelect={handleConfirm}
          onClose={() => handleOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}
