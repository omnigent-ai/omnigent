// Renders a Project's chosen emoji or a folder fallback while keeping the glyph
// decorative so controls retain the Project name as their accessible name.

import { FolderIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function ProjectLabel({
  name,
  icon,
  className,
  glyphClassName,
}: {
  name: string;
  /** Chosen emoji icon (unicode grapheme), or null/absent for the folder glyph. */
  icon?: string | null;
  className?: string;
  /** Extra classes for the leading glyph (emoji span or fallback FolderIcon). */
  glyphClassName?: string;
}) {
  return (
    <span className={cn("flex min-w-0 items-center gap-1.5", className)}>
      {icon ? (
        <span
          aria-hidden
          data-testid="project-label-icon"
          className={cn("flex shrink-0 items-center justify-center leading-none", glyphClassName)}
        >
          {icon}
        </span>
      ) : (
        <FolderIcon
          aria-hidden
          data-testid="project-label-fallback"
          className={cn("size-4 shrink-0 text-muted-foreground", glyphClassName)}
        />
      )}
      <span className="truncate">{name}</span>
    </span>
  );
}
