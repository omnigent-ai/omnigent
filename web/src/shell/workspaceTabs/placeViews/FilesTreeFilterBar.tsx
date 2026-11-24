// The Files tree's name filter, under its header: a name to search for, and globs
// that narrow the search to some files. The tree shows the matches while a name is set.

import { ChevronDownIcon, XIcon } from "lucide-react";
import { type RefObject, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { TreeFilters } from "@/shell/filesPane/useTreeFilters";

import { SIDEBAR_HANDLES_ESCAPE } from "../WorkspaceSidebarLayout";

export const NAME_CONTAINS_LABEL = "Name contains";
export const FILE_GLOBS_LABEL = "Files to include or exclude";

export function FilesTreeFilterBar({
  filters,
  inertGlobs,
  inputRef,
  onClose,
}: {
  filters: TreeFilters;
  /** Globs are set without a name, so they don't filter yet. */
  inertGlobs: boolean;
  inputRef: RefObject<HTMLInputElement | null>;
  /** Clears the filters and closes the bar. */
  onClose: () => void;
}) {
  const { edited } = filters;
  // Globs set earlier stay in view, so what narrows the search is never hidden.
  const [globsOpen, setGlobsOpen] = useState(() => Boolean(edited.include || edited.exclude));
  return (
    <div
      role="search"
      aria-label="Filter the Files tree"
      className="flex shrink-0 flex-col gap-1.5 border-b border-border px-2 py-1.5"
    >
      <div className="flex items-center gap-0.5">
        <Input
          ref={inputRef}
          aria-label={NAME_CONTAINS_LABEL}
          {...{ [SIDEBAR_HANDLES_ESCAPE]: "" }}
          placeholder={NAME_CONTAINS_LABEL}
          className="h-7 flex-1"
          value={edited.nameContains}
          onChange={(event) => filters.change({ nameContains: event.target.value })}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            // Escape empties the name first, and closes the bar once it's empty.
            event.preventDefault();
            event.stopPropagation();
            if (edited.nameContains) filters.clearNameContains();
            else onClose();
          }}
        />
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={FILE_GLOBS_LABEL}
          aria-expanded={globsOpen}
          title={FILE_GLOBS_LABEL}
          className="shrink-0 text-muted-foreground hover:text-foreground"
          onClick={() => setGlobsOpen((open) => !open)}
        >
          <ChevronDownIcon className={cn("transition-transform", globsOpen && "rotate-180")} />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Close filter"
          title="Close filter"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          onClick={onClose}
        >
          <XIcon />
        </Button>
      </div>
      {globsOpen ? (
        <>
          <Input
            aria-label="Files to include"
            placeholder="Files to include, e.g. *.ts, src/**"
            className="h-7 font-mono"
            value={edited.include}
            onChange={(event) => filters.change({ include: event.target.value })}
          />
          <Input
            aria-label="Files to exclude"
            placeholder="Files to exclude, e.g. **/node_modules"
            className="h-7 font-mono"
            value={edited.exclude}
            onChange={(event) => filters.change({ exclude: event.target.value })}
          />
        </>
      ) : null}
      {inertGlobs ? (
        <p className="px-0.5 text-xs text-muted-foreground">Applies once a name is set.</p>
      ) : null}
    </div>
  );
}
