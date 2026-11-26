// The ⋯ "File actions" at the end of a Files tab's toolbar: find in the file, the file's
// places and paths, a download, and last the session's finders.

import {
  DownloadIcon,
  FileSearchIcon,
  MoreHorizontalIcon,
  SearchIcon,
  TextSearchIcon,
} from "lucide-react";
import { Fragment, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { downloadWorkspaceFile } from "@/hooks/useFileContent";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";
import { WORKSPACE_TEXT_SEARCH_COPY } from "@/lib/workspaceTextSearchCopy";
import { FileCommandMenuItem } from "@/shell/fileCommands/FileCommandMenuItem";
import type { FileCommandId } from "@/shell/fileCommands/model";
import { useFileCommands } from "@/shell/fileCommands/useFileCommands";
import {
  requestFileFinder,
  requestTextSearch,
  useFileFinderAvailable,
  useTextSearchAvailable,
} from "@/store/fileFinder";

export const FILE_ACTIONS_LABEL = "File actions";
export const FIND_IN_FILE_LABEL = "Find in file";
export const GO_TO_FILE_LABEL = "Go to File in Workspace…";

const PLACE_COMMANDS = ["open-in-vscode", "open-in-cursor", "reveal"] as const;
const PATH_COMMANDS = ["copy-path", "copy-relative-path", "copy-link"] as const;

export interface FileActionsMenuProps {
  conversationId: string;
  path: string;
  /** Opens the editor's find bar. */
  onFind: () => void;
}

export function FileActionsMenu({ conversationId, path, onFind }: FileActionsMenuProps) {
  const file = useFileCommands(path, { conversationId });
  // A finder opens once the menu has returned focus to ⋯, so cancelling it lands there.
  const menuHandOff = useMenuCloseHandOff();
  // Go to File needs the session's finder; Search Text also needs its files' text searchable.
  const goToFileAvailable = useFileFinderAvailable(conversationId);
  const textSearchAvailable = useTextSearchAvailable(conversationId);
  const commandRows = (ids: readonly FileCommandId[]) =>
    ids.flatMap((id) => {
      const command = file.commands.find((candidate) => candidate.id === id);
      return command
        ? [
            <FileCommandMenuItem
              key={id}
              command={command}
              onRun={file.run}
              Item={DropdownMenuItem}
            />,
          ]
        : [];
    });
  const groups: { key: string; rows: ReactNode[] }[] = [
    {
      key: "find",
      rows: [
        <DropdownMenuItem
          key="find"
          // The find field takes focus in place of ⋯, once the menu has closed.
          onSelect={() => menuHandOff.runAfterClose(onFind)}
        >
          <SearchIcon />
          {FIND_IN_FILE_LABEL}
        </DropdownMenuItem>,
      ],
    },
    { key: "places", rows: commandRows(PLACE_COMMANDS) },
    { key: "paths", rows: commandRows(PATH_COMMANDS) },
    {
      key: "download",
      rows: [
        <DropdownMenuItem
          key="download"
          onSelect={() => {
            downloadWorkspaceFile(conversationId, path).catch(() => toast.error("Download failed"));
          }}
        >
          <DownloadIcon />
          Download file
        </DropdownMenuItem>,
      ],
    },
    {
      key: "finders",
      rows: [
        ...(goToFileAvailable
          ? [
              <DropdownMenuItem
                key="go-to-file"
                onSelect={() =>
                  menuHandOff.runAfterClose(() => requestFileFinder(conversationId), {
                    afterFocusReturns: true,
                  })
                }
              >
                <FileSearchIcon />
                {GO_TO_FILE_LABEL}
              </DropdownMenuItem>,
            ]
          : []),
        ...(textSearchAvailable
          ? [
              <DropdownMenuItem
                key="search-text"
                onSelect={() =>
                  menuHandOff.runAfterClose(() => requestTextSearch(conversationId), {
                    afterFocusReturns: true,
                  })
                }
              >
                <TextSearchIcon />
                {WORKSPACE_TEXT_SEARCH_COPY.command}
              </DropdownMenuItem>,
            ]
          : []),
      ],
    },
  ].filter((group) => group.rows.length > 0);
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={FILE_ACTIONS_LABEL}>
              <MoreHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">More</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="w-auto min-w-48"
        onCloseAutoFocus={menuHandOff.onCloseAutoFocus}
      >
        {groups.map((group, index) => (
          <Fragment key={group.key}>
            {index > 0 ? <DropdownMenuSeparator /> : null}
            {group.rows}
          </Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
