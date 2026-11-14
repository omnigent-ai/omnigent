import { useState } from "react";
import { ComposerWorkspaceTrigger } from "@/components/composer/ComposerControls";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ProjectSummary } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { ProjectRowIcon } from "./ProjectPicker";

// Radio rows announce the selection; keep their check at the row's right edge in primary.
const ROW_CLASS =
  "mobile-touch-target min-h-11 gap-2 pl-3 pr-9 text-sm [&_[data-slot=dropdown-menu-radio-item-indicator]]:right-3 [&_[data-slot=dropdown-menu-radio-item-indicator]_svg]:text-primary";

/**
 * Phone landing chip naming the project a new session starts in. Its menu
 * lists "No project" and every project, without a search field, so choosing
 * one never raises the keyboard. `""` means no project, in `selectedProject`
 * and `onSelect`.
 */
export function LandingProjectChip({
  projects,
  selectedProject,
  onSelect,
}: {
  projects: readonly ProjectSummary[];
  selectedProject: string;
  onSelect: (project: string) => void;
}) {
  const selectedIcon = projects.find((project) => project.name === selectedProject)?.icon;
  const label = selectedProject || "No project";
  const [open, setOpen] = useState(false);
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        {/* The tray's own trigger, so the chip matches the directory chip beside it. */}
        <ComposerWorkspaceTrigger
          kind="directory"
          label={label}
          icon={<ProjectRowIcon icon={selectedIcon} />}
          aria-label={`Project: ${label}`}
          tooltip={`Project: ${label}`}
          menuOpen={open}
          data-testid="new-chat-landing-project-chip"
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        collisionPadding={16}
        className={cn(
          "max-h-[60vh] w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] overflow-y-auto",
          MOBILE_GLASS_SURFACE,
        )}
        data-testid="new-chat-landing-project-menu"
      >
        <DropdownMenuRadioGroup value={selectedProject} onValueChange={onSelect}>
          <DropdownMenuRadioItem className={ROW_CLASS} value="">
            <span className="flex-1 truncate">No project</span>
          </DropdownMenuRadioItem>
          {projects.map((project) => (
            <DropdownMenuRadioItem
              key={project.name}
              className={ROW_CLASS}
              value={project.name}
              textValue={project.name}
            >
              <ProjectRowIcon icon={project.icon} />
              <span className="flex-1 truncate">{project.name}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
