// How each file action looks. ⌘K takes Lucide icons; menus show the editors' own marks.

import type { ReactNode } from "react";
import {
  ClipboardCopyIcon,
  CopyIcon,
  ExternalLinkIcon,
  FileTextIcon,
  FolderInputIcon,
  FolderOpenIcon,
  HistoryIcon,
  Link2Icon,
  type LucideIcon,
  SquareCodeIcon,
  SquarePlusIcon,
} from "lucide-react";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { VSCodeIcon } from "@/components/icons/VSCodeIcon";
import type { FileCommandId } from "./model";

export const FILE_COMMAND_ICONS: Record<FileCommandId, LucideIcon> = {
  open: FileTextIcon,
  "open-in-new-tab": SquarePlusIcon,
  "open-as-root": FolderInputIcon,
  "open-on-code-host": ExternalLinkIcon,
  "copy-path": CopyIcon,
  "copy-relative-path": ClipboardCopyIcon,
  "copy-previous-path": HistoryIcon,
  "copy-link": Link2Icon,
  reveal: FolderOpenIcon,
  "open-in-vscode": SquareCodeIcon,
  "open-in-cursor": SquareCodeIcon,
};

/** A menu row's icon: the editor's mark for "Open in", otherwise the command's icon. */
export function fileCommandMenuIcon(id: FileCommandId): ReactNode {
  if (id === "open-in-vscode" || id === "open-in-cursor") {
    return (
      <span aria-hidden="true" className="inline-flex size-4 shrink-0">
        {id === "open-in-cursor" ? <CursorIcon size={16} /> : <VSCodeIcon size={16} />}
      </span>
    );
  }
  const Icon = FILE_COMMAND_ICONS[id];
  return <Icon className="size-4" />;
}
