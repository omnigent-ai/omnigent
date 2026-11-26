// The Files tree's sort order, hidden files and git-ignored files: one set of actions for the
// tree header's ⋯ and ⌘K, offered by the Files place on screen.

import {
  EyeIcon,
  EyeOffIcon,
  FileCheckIcon,
  FileXIcon,
  ListFilterIcon,
  MoreHorizontalIcon,
} from "lucide-react";
import { useId, useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";
import type { ChangedSort } from "@/lib/changedSort";
import { cn } from "@/lib/utils";
import { CHANGED_SORT_OPTIONS } from "@/shell/fileTree/changedSortOptions";
import { HIDDEN, type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import type { TreeFolderAction } from "./useTreeFolderActions";

export const TREE_OPTIONS_LABEL = "Tree options";
export const FILTER_BY_NAME_LABEL = "Filter by name";
export const SHOW_HIDDEN_FILES_LABEL = "Show Hidden Files";
export const HIDE_HIDDEN_FILES_LABEL = "Hide Hidden Files";
export const SHOW_IGNORED_FILES_LABEL = "Show Git-Ignored Files";
export const HIDE_IGNORED_FILES_LABEL = "Hide Git-Ignored Files";

/** The tree's view settings, kept by the sidebar and shared with the classic Files panel. */
export interface FilesTreeOptions {
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  /** List what Git ignores, marked as ignored. */
  showIgnored: boolean;
  onShowIgnoredChange: (showIgnored: boolean) => void;
}

/** ⌘K's rows: a sort order other than the current one, and showing or hiding hidden and ignored files. */
export function filesTreeOptionCommands(options: FilesTreeOptions): PaletteCommand[] {
  const { sort, onSortChange, showHidden, onShowHiddenChange, showIgnored, onShowIgnoredChange } =
    options;
  return [
    ...CHANGED_SORT_OPTIONS.map(({ value, label, Icon }) => ({
      id: `files-tree-sort:${value}`,
      label: `Sort Files Tree by ${label}`,
      icon: Icon,
      keywords: ["sort", "order", "files", "tree", label.toLowerCase()],
      availability: value === sort ? HIDDEN : undefined,
      run: () => onSortChange(value),
    })),
    {
      id: "files-tree-hidden",
      label: showHidden ? HIDE_HIDDEN_FILES_LABEL : SHOW_HIDDEN_FILES_LABEL,
      icon: showHidden ? EyeOffIcon : EyeIcon,
      keywords: ["hidden", "dotfiles", "files", "tree"],
      run: () => onShowHiddenChange(!showHidden),
    },
    {
      id: "files-tree-ignored",
      label: showIgnored ? HIDE_IGNORED_FILES_LABEL : SHOW_IGNORED_FILES_LABEL,
      icon: showIgnored ? FileXIcon : FileCheckIcon,
      keywords: ["ignored", "gitignore", "git", "build", "files", "tree"],
      run: () => onShowIgnoredChange(!showIgnored),
    },
  ];
}

/** Offers the tree's options in ⌘K while `active`. */
export function useFilesTreeOptionCommands(options: FilesTreeOptions, active: boolean): void {
  const source = useId();
  const { sort, onSortChange, showHidden, onShowHiddenChange, showIgnored, onShowIgnoredChange } =
    options;
  const commands = useMemo(
    () =>
      active
        ? filesTreeOptionCommands({
            sort,
            onSortChange,
            showHidden,
            onShowHiddenChange,
            showIgnored,
            onShowIgnoredChange,
          })
        : [],
    [active, sort, onSortChange, showHidden, onShowHiddenChange, showIgnored, onShowIgnoredChange],
  );
  usePaletteCommands(`files-tree-options:${source}`, "Files", commands);
}

/** What the tree's ⋯ adds to its view settings: the name filter and the folder's actions. */
export interface FilesTreeOptionsMenuProps extends FilesTreeOptions {
  /** A name filter narrows the tree; the trigger shows a dot. */
  filtering: boolean;
  /** Opens the name filter under the header and focuses it, once the menu has closed. */
  onFilter: () => void;
  folderActions: readonly TreeFolderAction[];
}

/**
 * The tree header's ⋯: sort order, hidden and git-ignored files, the name filter and the
 * folder's actions.
 */
export function FilesTreeOptionsMenu({
  sort,
  onSortChange,
  showHidden,
  onShowHiddenChange,
  showIgnored,
  onShowIgnoredChange,
  filtering,
  onFilter,
  folderActions,
}: FilesTreeOptionsMenuProps) {
  // The menu keeps focus while it closes, so the filter's field takes it once it has closed.
  const menuHandOff = useMenuCloseHandOff();
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              data-tree-options-trigger=""
              aria-label={filtering ? `${TREE_OPTIONS_LABEL}, filter on` : TREE_OPTIONS_LABEL}
              className={cn(
                "relative shrink-0 data-[state=open]:text-foreground",
                filtering ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              <MoreHorizontalIcon />
              {filtering ? (
                <span
                  aria-hidden
                  data-testid="tree-filter-dot"
                  className="absolute top-0.5 right-0.5 size-1.5 rounded-full bg-primary"
                />
              ) : null}
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">{TREE_OPTIONS_LABEL}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="w-56"
        onCloseAutoFocus={menuHandOff.onCloseAutoFocus}
      >
        <DropdownMenuLabel>Sort by</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={sort}
          onValueChange={(value) => onSortChange(value as ChangedSort)}
        >
          {CHANGED_SORT_OPTIONS.map(({ value, label, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value}>
              <Icon className="size-3.5" />
              {label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem checked={showHidden} onCheckedChange={onShowHiddenChange}>
          Show hidden files
        </DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem checked={showIgnored} onCheckedChange={onShowIgnoredChange}>
          Show git-ignored files
        </DropdownMenuCheckboxItem>
        <DropdownMenuItem onSelect={() => menuHandOff.runAfterClose(onFilter)}>
          <ListFilterIcon />
          {FILTER_BY_NAME_LABEL}
        </DropdownMenuItem>
        {folderActions.length > 0 ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuGroup aria-label="Folder">
              {folderActions.map((action) => (
                <DropdownMenuItem
                  key={action.key}
                  disabled={action.disabled}
                  onSelect={action.onSelect}
                >
                  {action.icon}
                  {action.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
