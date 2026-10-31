import { useState } from "react";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  ProjectAppearancePicker,
  ProjectAppearanceTile,
  useRecommendedProjectColor,
} from "@/components/ProjectIconPicker";
import { useCreateProject } from "@/hooks/useConversations";
import type { ProjectAppearance } from "@/lib/projectColors";

/**
 * "New project" control in the Projects group header. Opens a dialog that
 * creates an EMPTY first-class project (`POST /v1/projects`) — the capability
 * the legacy label model can't express. On success the new folder is expanded
 * (via `onCreated`) so the user can immediately file sessions into it.
 */
export function NewProjectButton({ onCreated }: { onCreated: (name: string) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  // The picked mark; null until the user picks, so the recommended color stays
  // live (it can change as the project list loads).
  const [picked, setPicked] = useState<ProjectAppearance | null>(null);
  const recommendedColor = useRecommendedProjectColor();
  // Until the recommendation loads nothing is checked; creating then leaves
  // the color to the server, which picks the same recommendation.
  const appearance = picked ?? { icon: null, color: recommendedColor ?? null };
  const createProject = useCreateProject();

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed === "") return;
    createProject.mutate(
      {
        name: trimmed,
        icon: appearance.icon ?? undefined,
        color: appearance.color ?? undefined,
      },
      {
        onSuccess: (project) => {
          setOpen(false);
          setName("");
          setPicked(null);
          onCreated(project.name);
        },
      },
    );
  };

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="New project"
            data-testid="new-project"
            className="text-muted-foreground"
            onClick={(e) => {
              e.stopPropagation();
              setName("");
              setPicked(null);
              setOpen(true);
            }}
          >
            <PlusIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New project</TooltipContent>
      </Tooltip>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>New project</DialogTitle>
            <DialogDescription>
              Create an empty project, then file sessions into it from a session's menu.
            </DialogDescription>
          </DialogHeader>
          {/* One combined control: the mark preview (left) + name (right) share
              a single border, so it reads as a single input. */}
          <div className="flex items-stretch overflow-hidden rounded-lg border border-input">
            <ProjectAppearanceTile appearance={appearance} size="sm" />
            <input
              autoFocus
              className="w-full bg-transparent px-3 py-2 text-ui outline-none"
              placeholder="Project name…"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
            />
          </div>
          <ProjectAppearancePicker value={appearance} onChange={setPicked} />
          {createProject.isError && (
            <p className="text-ui text-destructive" role="alert">
              {(createProject.error as Error).message}
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
              disabled={createProject.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              data-testid="new-project-confirm"
              loading={createProject.isPending}
              disabled={name.trim() === ""}
              onClick={submit}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
