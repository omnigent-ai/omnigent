// The strip above an editor with unsaved edits: a conflict prompt when the file
// changed on disk meanwhile, otherwise why commenting is paused.

import { AlertTriangleIcon, MessageSquareOffIcon } from "lucide-react";

interface EditorSyncBannerProps {
  hasExternalUpdate: boolean;
  onKeepMine: () => void;
  onLoadLatest: () => void;
}

export function EditorSyncBanner({
  hasExternalUpdate,
  onKeepMine,
  onLoadLatest,
}: EditorSyncBannerProps) {
  if (hasExternalUpdate) {
    return (
      <div className="flex items-center gap-2 border-b border-border bg-warning/10 px-4 py-1.5 text-sm text-foreground shrink-0">
        <AlertTriangleIcon className="size-3.5 shrink-0 text-warning" />
        <span className="flex-1">This file was modified externally while you were editing.</span>
        <button
          type="button"
          className="rounded px-2 py-0.5 font-medium hover:bg-muted transition-colors"
          onClick={onKeepMine}
        >
          Keep mine
        </button>
        <button
          type="button"
          className="rounded bg-primary px-2 py-0.5 font-medium text-primary-foreground hover:opacity-90 transition-opacity"
          onClick={onLoadLatest}
        >
          Load latest
        </button>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1.5 border-b border-border bg-muted/50 px-4 py-1.5 text-sm text-muted-foreground shrink-0">
      <MessageSquareOffIcon className="size-3.5 shrink-0" />
      Save your changes to enable commenting on selections.
    </div>
  );
}
