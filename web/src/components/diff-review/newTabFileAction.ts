// "Open in new tab" as a review file action: the first row of a file's header menu
// and of its row menu in the changed-files tree.

import { SquarePlusIcon } from "lucide-react";

import { OPEN_IN_NEW_TAB_LABEL } from "@/shell/fileCommands/model";

import type { DiffReviewFileAction } from "./diffReviewFileAction";

export const NEW_TAB_FILE_ACTION_ID = "open-in-new-tab";

export function newTabFileAction(
  path: string,
  open: (path: string, options: { background: boolean }) => void,
): DiffReviewFileAction {
  return {
    id: NEW_TAB_FILE_ACTION_ID,
    label: OPEN_IN_NEW_TAB_LABEL,
    icon: SquarePlusIcon,
    keywords: ["tab", "new", "background"],
    run: () => open(path, { background: true }),
  };
}
