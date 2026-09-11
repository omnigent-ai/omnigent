import { useCallback, useEffect, useRef, useState } from "react";
import { FolderIcon, LightbulbIcon, PaperclipIcon, PlusIcon, TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const EMPTY_PROJECTS: readonly { name: string }[] = [];

export function ComposerAddMenu({
  disabled,
  onAttach,
  attachDisabled = false,
  onGoal,
  goalActive = false,
  goalDisabled = false,
  goalDescription = "Available after starting a session",
  showGoal = true,
  showPlan = true,
  planDisabled = false,
  planLabel,
  testIdPrefix = "composer",
  onPlan,
  planActive,
  projects = EMPTY_PROJECTS,
  onProjectSelect,
}: {
  disabled: boolean;
  onAttach: () => void;
  attachDisabled?: boolean;
  onGoal?: () => void;
  goalActive?: boolean;
  goalDisabled?: boolean;
  goalDescription?: string;
  showGoal?: boolean;
  showPlan?: boolean;
  planDisabled?: boolean;
  planLabel?: string;
  testIdPrefix?: string;
  onPlan?: () => void;
  planActive: boolean;
  projects?: readonly { name: string }[];
  onProjectSelect?: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const pendingGoalRef = useRef(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [geometry, setGeometry] = useState({ width: 456, alignOffset: 0, sideOffset: 6 });
  const measure = useCallback(() => {
    const trigger = triggerRef.current;
    const composer = trigger?.closest("[data-composer-card]");
    if (!trigger || !composer) return;
    const triggerRect = trigger.getBoundingClientRect();
    const composerRect = composer.getBoundingClientRect();
    setGeometry({
      width: composerRect.width,
      alignOffset: composerRect.left - triggerRect.left,
      sideOffset: triggerRect.top - composerRect.top + 6,
    });
  }, []);
  useEffect(() => {
    if (!open) return;
    const composer = triggerRef.current?.closest("[data-composer-card]");
    if (!composer) return;
    const observer = new ResizeObserver(measure);
    observer.observe(composer);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [open, measure]);

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) measure();
        else setShowProjects(false);
        setOpen(nextOpen);
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          ref={triggerRef}
          type="button"
          size="icon"
          variant="ghost"
          className="size-8 rounded-lg data-[state=open]:bg-muted data-[state=open]:text-foreground md:size-7"
          disabled={disabled}
          aria-label="Add"
          title="Add"
          data-testid={`${testIdPrefix}-attach`}
          componentId={`${testIdPrefix}.add_menu`}
        >
          <PlusIcon
            className="size-4"
            data-icon-size="16"
            data-testid={`${testIdPrefix}-attach-icon`}
          />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        onCloseAutoFocus={() => {
          if (pendingGoalRef.current) {
            pendingGoalRef.current = false;
            requestAnimationFrame(() => onGoal?.());
          }
        }}
        side="top"
        align="start"
        alignOffset={geometry.alignOffset}
        sideOffset={geometry.sideOffset}
        collisionPadding={12}
        aria-label="Add"
        className="composer-add-menu max-w-[calc(100vw-24px)] rounded-[16px]"
        style={{ width: geometry.width }}
        data-testid={`${testIdPrefix}-add-menu`}
      >
        {showProjects ? (
          <div className="flex flex-col gap-0.5">
            <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">
              Choose a project
            </div>
            {projects.length === 0 && <DropdownMenuItem disabled>No projects yet</DropdownMenuItem>}
            {projects.map((project) => (
              <DropdownMenuItem key={project.name} onSelect={() => onProjectSelect?.(project.name)}>
                <span className="flex size-4 shrink-0 items-center justify-center">
                  <FolderIcon className="size-3.5" />
                </span>
                <span className="min-w-0 truncate">{project.name}</span>
              </DropdownMenuItem>
            ))}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-0.5">
              <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">Add</div>
              <DropdownMenuItem onSelect={onAttach} disabled={attachDisabled}>
                <span className="flex size-4 shrink-0 items-center justify-center">
                  <PaperclipIcon className="size-3.5" />
                </span>
                <span>Files and images</span>
              </DropdownMenuItem>
            </div>
            {(showGoal || showPlan || onProjectSelect) && (
              <div className="flex flex-col gap-0.5">
                <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">Session</div>
                <div className="flex flex-col gap-px">
                  {showGoal && (
                    <DropdownMenuItem
                      disabled={!onGoal || goalDisabled}
                      title={!onGoal ? goalDescription : undefined}
                      data-testid="composer-goal-action"
                      data-active={goalActive || undefined}
                      onSelect={() => {
                        pendingGoalRef.current = true;
                      }}
                    >
                      <span className="flex size-4 shrink-0 items-center justify-center">
                        <TargetIcon className="size-3.5" />
                      </span>
                      <span>Goal</span>
                      <span className="truncate text-xs leading-4 text-muted-foreground">
                        {goalDescription}
                      </span>
                    </DropdownMenuItem>
                  )}
                  {showPlan && (
                    <DropdownMenuItem
                      data-testid="composer-plan-action"
                      data-active={planActive || undefined}
                      aria-label={planLabel}
                      disabled={!onPlan || planDisabled}
                      onSelect={onPlan}
                      title={onPlan ? undefined : "This harness does not support plan mode"}
                    >
                      <span className="flex size-4 shrink-0 items-center justify-center">
                        <LightbulbIcon className="size-3.5" />
                      </span>
                      <span>Plan</span>
                      <span className="truncate text-xs leading-4 text-muted-foreground">
                        {planActive ? "Plan mode is on" : "Turn plan mode on"}
                      </span>
                    </DropdownMenuItem>
                  )}
                  {onProjectSelect && (
                    <DropdownMenuItem
                      onSelect={(event) => {
                        event.preventDefault();
                        setShowProjects(true);
                      }}
                    >
                      <span className="flex size-4 shrink-0 items-center justify-center">
                        <FolderIcon className="size-3.5" />
                      </span>
                      <span className="shrink-0">Work in a project</span>
                      <span className="truncate text-xs leading-4 text-muted-foreground">
                        Choose a project for this session
                      </span>
                    </DropdownMenuItem>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
