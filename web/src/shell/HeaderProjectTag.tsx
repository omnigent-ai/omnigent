import { useState } from "react";
import { FolderPlusIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useMoveToProject } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { ProjectPicker, ProjectRowIcon } from "./ProjectPicker";

/**
 * The breadcrumb's leading folder segment, as a Slack-style "Move
 * conversation" shortcut. Clicking the folder icon opens a searchable project
 * picker so the session can be filed, moved, or unfiled straight from the title
 * — no trip to the kebab menu. Desktop only (the tag is `hidden md:flex`);
 * mobile keeps the equivalent item in the header session menu, since the native
 * mobile shells hide the breadcrumb.
 *
 * Filed sessions show `[folder] /` with a "Currently in: ‹project›" tooltip;
 * unfiled ones show a faint add-to-project folder that only fully reveals on
 * hover, with a "Move session" tooltip — an entry point for an empty state
 * without cluttering the title.
 */
export function HeaderProjectTag({
  conversationId,
  projectName,
  projectIcon,
}: {
  conversationId: string;
  projectName: string | null;
  /** The filed project's emoji icon, or `null`/absent for the folder glyph. */
  projectIcon?: string | null;
}) {
  const moveToProject = useMoveToProject();
  const [open, setOpen] = useState(false);

  const handleSelect = (project: string) => {
    setOpen(false);
    moveToProject.mutate({ id: conversationId, project });
  };

  return (
    <div className="hidden md:flex min-w-0 items-center gap-1.5 text-ui shrink-0">
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <Tooltip>
          {/* Wrap the dropdown trigger in a real span for the tooltip: two
              `asChild` triggers merged onto the same button drop the tooltip's
              hover handlers (the pattern ViewModeToggle uses). */}
          <TooltipTrigger asChild>
            <span className="inline-flex">
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  data-testid="header-project-tag"
                  aria-label={projectName ? `Project: ${projectName}` : "Add to project"}
                  className={cn(
                    "breadcrumb-folder flex shrink-0 cursor-pointer items-center rounded text-muted-foreground transition-opacity hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100 focus-visible:outline-none",
                    // A full-color emoji reads as washed-out when faded, so only
                    // dim the monochrome folder glyphs.
                    projectIcon ? "opacity-100" : projectName ? "opacity-40" : "opacity-30",
                  )}
                >
                  {projectName ? (
                    <ProjectRowIcon icon={projectIcon} className="size-4 text-[16px]" />
                  ) : (
                    <FolderPlusIcon className="size-4" />
                  )}
                </button>
              </DropdownMenuTrigger>
            </span>
          </TooltipTrigger>
          {/* Bottom placement: the header sits at top-0, so a top-side tooltip
              would clip above the viewport edge. A filed session names its
              folder ("Currently in: …"); an unfiled one invites the move. */}
          <TooltipContent side="bottom">
            {projectName ? `Currently in: ${projectName}` : "Move session"}
          </TooltipContent>
        </Tooltip>
        <DropdownMenuContent
          align="start"
          className="min-w-56"
          // Don't return focus to the folder button on close: Radix opens the
          // tooltip on focus, so refocusing the trigger makes "Move session"
          // pop up every time the dropdown is dismissed (even by an outside
          // click, with the pointer nowhere near the tag).
          onCloseAutoFocus={(event) => event.preventDefault()}
        >
          <ProjectPicker currentProject={projectName} onSelect={handleSelect} />
        </DropdownMenuContent>
      </DropdownMenu>
      <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
        /
      </span>
    </div>
  );
}
