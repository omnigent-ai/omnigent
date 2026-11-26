// "View File" as a review file action, as the reference design names it: shows
// the file itself where the surface shows files, from its header's menu, its
// tree row's menu, and ⌘K.

import { FileIcon } from "lucide-react";

import type { DiffReviewFileAction } from "./diffReviewFileAction";

export const VIEW_FILE_ACTION_ID = "view-file";

export function viewFileAction(path: string, open: (path: string) => void): DiffReviewFileAction {
  return {
    id: VIEW_FILE_ACTION_ID,
    label: "View File",
    icon: FileIcon,
    keywords: ["open", "file", "source"],
    run: () => open(path),
  };
}
