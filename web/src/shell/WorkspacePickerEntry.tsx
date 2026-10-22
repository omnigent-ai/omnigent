import { ChevronRightIcon, FolderIcon } from "lucide-react";

import type { HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { cn } from "@/lib/utils";
import { MenuItem } from "@/components/ui/menu-item";
import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

export interface WorkspacePickerEntryProps {
  entry: HostFilesystemEntry;
  onOpen: (path: string) => void;
  variant?: "default" | "compact";
}

/** A single folder or file row in a workspace directory listing. */
export function WorkspacePickerEntry({
  entry,
  onOpen,
  variant = "default",
}: WorkspacePickerEntryProps) {
  const isDirectory = entry.type === "directory";
  const compact = variant === "compact";

  return (
    <MenuItem asChild density={compact ? "compact" : "none"} interactive={isDirectory}>
      <button
        type="button"
        disabled={!isDirectory}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => isDirectory && onOpen(entry.path)}
        className={cn(
          "w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
          compact ? "gap-2 px-2 py-[3px]" : "h-auto min-h-11 gap-2.5 px-5 py-2 text-base",
          isDirectory
            ? "cursor-pointer text-foreground"
            : compact
              ? "cursor-default text-muted-foreground"
              : "cursor-not-allowed text-muted-foreground opacity-55",
        )}
        data-testid={`workspace-picker-entry-${entry.name}`}
      >
        {isDirectory ? (
          <FolderIcon
            className={cn("size-5 shrink-0 text-muted-foreground", compact && "size-4")}
          />
        ) : (
          <WorkspaceFileIcon path={entry.path} className={compact ? "size-4" : "size-5"} />
        )}
        <span className="flex-1 truncate">{entry.name}</span>
        {isDirectory && <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" />}
      </button>
    </MenuItem>
  );
}
