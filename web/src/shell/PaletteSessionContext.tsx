// Where a ⌘K session row lives: its project and the session it's nested under.

import { ChevronRightIcon, CornerDownRightIcon, FolderIcon } from "lucide-react";

export function PaletteSessionContext({
  project,
  parent,
}: {
  project: string | null;
  parent: string | null;
}) {
  if (!project && !parent) return null;
  return (
    <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
      {project && (
        <>
          <FolderIcon aria-hidden="true" className="size-3 shrink-0" />
          <span className="truncate" title={project}>
            <span className="sr-only">Project: </span>
            {project}
          </span>
        </>
      )}
      {project && parent && <ChevronRightIcon aria-hidden="true" className="size-3 shrink-0" />}
      {parent && (
        <>
          <CornerDownRightIcon aria-hidden="true" className="size-3 shrink-0" />
          <span className="truncate" title={parent}>
            <span className="sr-only">Nested under: </span>
            {parent}
          </span>
        </>
      )}
    </span>
  );
}
