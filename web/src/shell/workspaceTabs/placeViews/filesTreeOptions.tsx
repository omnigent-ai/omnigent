// The Files tree's sort order and hidden files: one set of actions for the toolbar's options
// menu and ⌘K, offered by the Files place on screen.

import { EyeIcon, EyeOffIcon, SlidersHorizontalIcon } from "lucide-react";
import { useId, useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { ChangedSort } from "@/lib/changedSort";
import { CHANGED_SORT_OPTIONS } from "@/shell/fileTree/changedSortOptions";
import { HIDDEN, type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

export const FILES_TREE_OPTIONS_LABEL = "Files tree options";
export const SHOW_HIDDEN_FILES_LABEL = "Show Hidden Files";
export const HIDE_HIDDEN_FILES_LABEL = "Hide Hidden Files";

/** The tree's view settings, kept by the sidebar and shared with the classic Files panel. */
export interface FilesTreeOptions {
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
}

/** ⌘K's rows: a sort order other than the current one, and showing or hiding hidden files. */
export function filesTreeOptionCommands(options: FilesTreeOptions): PaletteCommand[] {
  const { sort, onSortChange, showHidden, onShowHiddenChange } = options;
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
      keywords: ["hidden", "dotfiles", "files", "tree", "ignored"],
      run: () => onShowHiddenChange(!showHidden),
    },
  ];
}

/** Offers the tree's options in ⌘K while `active`. */
export function useFilesTreeOptionCommands(options: FilesTreeOptions, active: boolean): void {
  const source = useId();
  const { sort, onSortChange, showHidden, onShowHiddenChange } = options;
  const commands = useMemo(
    () =>
      active ? filesTreeOptionCommands({ sort, onSortChange, showHidden, onShowHiddenChange }) : [],
    [active, sort, onSortChange, showHidden, onShowHiddenChange],
  );
  usePaletteCommands(`files-tree-options:${source}`, "Files", commands);
}

/** The toolbar's menu for the tree's sort order and hidden files. */
export function FilesTreeOptionsMenu({
  sort,
  onSortChange,
  showHidden,
  onShowHiddenChange,
}: FilesTreeOptions) {
  return (
    <DropdownMenu>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={FILES_TREE_OPTIONS_LABEL}
                className="shrink-0 text-muted-foreground"
              >
                <SlidersHorizontalIcon />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>{FILES_TREE_OPTIONS_LABEL}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DropdownMenuContent align="start" className="w-44">
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
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
