// Phone form of the new-session project step: a bottom sheet listing No
// project and every project, without a search field so the keyboard stays
// down. Desktop asks in the command palette instead (`CommandPalette`).

import type React from "react";
import { CircleSlashIcon } from "lucide-react";

import { BottomSheet, BottomSheetContent } from "@/components/ui/bottom-sheet";
import { useProjects } from "@/hooks/useConversations";
import {
  newSessionPath,
  useLeaveProjectPickerWithoutProjects,
} from "@/hooks/useNewChatProjectPrompt";
import { useNavigate } from "@/lib/routing";

import { MOBILE_GLASS_SURFACE } from "./mobileGlass";
import { ProjectRowIcon } from "./ProjectPicker";

export interface NewSessionProjectSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function SheetRow({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-12 w-full items-center gap-3.5 px-4 text-left text-[17px] text-foreground outline-none active:bg-foreground/[0.06] focus-visible:bg-foreground/[0.06]"
    >
      {icon}
      <span className="min-w-0 flex-1 truncate">{label}</span>
    </button>
  );
}

export function NewSessionProjectSheet({ open, onOpenChange }: NewSessionProjectSheetProps) {
  const navigate = useNavigate();
  const { data: projects = [], isSuccess, isError } = useProjects();
  // The sheet may open before the list resolves; rows arrive as it does.
  useLeaveProjectPickerWithoutProjects(open, onOpenChange);
  const loading = !isSuccess && !isError;

  const start = (projectName: string): void => {
    onOpenChange(false);
    navigate(newSessionPath(projectName));
  };

  return (
    <BottomSheet open={open} onOpenChange={onOpenChange}>
      <BottomSheetContent title="Start a new session in" className={MOBILE_GLASS_SURFACE}>
        <div className="min-h-0 overflow-y-auto overscroll-contain px-2 pb-2">
          <div className="flex flex-col divide-y divide-border overflow-hidden rounded-2xl bg-foreground/[0.04]">
            <SheetRow
              icon={<CircleSlashIcon aria-hidden="true" className="size-5 text-muted-foreground" />}
              label="No project"
              onClick={() => start("")}
            />
            {projects.map((project) => (
              <SheetRow
                key={project.name}
                icon={<ProjectRowIcon icon={project.icon} className="size-5 text-[18px]" />}
                label={project.name}
                onClick={() => start(project.name)}
              />
            ))}
            {loading && (
              <p role="status" className="px-4 py-3 text-[15px] text-muted-foreground">
                Loading projects…
              </p>
            )}
          </div>
        </div>
      </BottomSheetContent>
    </BottomSheet>
  );
}
