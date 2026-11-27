import { LayersIcon } from "lucide-react";

import { ComposerHarnessTrigger } from "@/components/composer/ComposerControls";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ScheduledTaskOverlapMode } from "@/lib/scheduledTasksApi";

export const OVERLAP_MODE_LABELS: Record<ScheduledTaskOverlapMode, string> = {
  allow: "Start it anyway",
  skip: "Skip it",
  queue: "Queue it",
};

interface AutomationOverlapFieldProps {
  overlapMode: ScheduledTaskOverlapMode;
  /** The modes the server accepts, in the order to offer them. */
  modes: readonly ScheduledTaskOverlapMode[];
  onChange: (overlapMode: ScheduledTaskOverlapMode) => void;
  triggerClassName: string;
}

/** Advanced-settings row for what a due run does while the previous run is still going. */
export function AutomationOverlapField({
  overlapMode,
  modes,
  onChange,
  triggerClassName,
}: AutomationOverlapFieldProps) {
  return (
    <div
      className="flex min-h-9 items-center justify-between gap-4 py-px"
      data-testid="task-overlap-field"
    >
      <span className="text-ui text-muted-foreground">Overlapping runs</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <ComposerHarnessTrigger
            label="Overlapping runs"
            model={OVERLAP_MODE_LABELS[overlapMode]}
            icon={<LayersIcon className="size-4 shrink-0" />}
            className={triggerClassName}
            data-testid="task-overlap-trigger"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
          <p className="px-2 py-1.5 text-xs whitespace-normal text-muted-foreground">
            What a scheduled run does while the previous run is still going: its session is busy or
            waits on an approval. Only one run waits in the queue; later ones are skipped. Run now
            always starts.
          </p>
          <DropdownMenuSeparator />
          <DropdownMenuRadioGroup
            value={overlapMode}
            onValueChange={(value) => onChange(value as ScheduledTaskOverlapMode)}
          >
            {modes.map((mode) => (
              <DropdownMenuRadioItem key={mode} value={mode} data-testid={`task-overlap-${mode}`}>
                {OVERLAP_MODE_LABELS[mode]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
