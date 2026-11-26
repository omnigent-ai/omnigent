import { RotateCcwIcon } from "lucide-react";

import { ComposerHarnessTrigger } from "@/components/composer/ComposerControls";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { MAX_SCHEDULED_TASK_RETRIES } from "@/lib/scheduledTasksApi";

const RETRY_COUNTS = Array.from({ length: MAX_SCHEDULED_TASK_RETRIES + 1 }, (_, count) => count);
const RETRY_DELAY_PRESETS_SECONDS = [60, 5 * 60, 15 * 60, 30 * 60, 60 * 60];

function delayLabel(seconds: number): string {
  if (seconds % 3600 === 0) return seconds === 3600 ? "1 hour" : `${seconds / 3600} hours`;
  if (seconds % 60 === 0) return seconds === 60 ? "1 minute" : `${seconds / 60} minutes`;
  return `${seconds} seconds`;
}

/** The summary the trigger shows, e.g. `Off` or `2 retries · 5 minutes apart`. */
export function retrySummary(maxRetries: number, retryDelaySeconds: number): string {
  if (maxRetries === 0) return "Off";
  return `${maxRetries} ${maxRetries === 1 ? "retry" : "retries"} · ${delayLabel(retryDelaySeconds)} apart`;
}

interface AutomationRetryFieldProps {
  maxRetries: number;
  retryDelaySeconds: number;
  onMaxRetriesChange: (maxRetries: number) => void;
  onRetryDelayChange: (retryDelaySeconds: number) => void;
  triggerClassName: string;
}

/**
 * Advanced-settings row for retrying a run whose startup failed before its prompt
 * reached the agent, such as when its host was offline.
 */
export function AutomationRetryField({
  maxRetries,
  retryDelaySeconds,
  onMaxRetriesChange,
  onRetryDelayChange,
  triggerClassName,
}: AutomationRetryFieldProps) {
  // A delay saved outside the presets, e.g. over the API, stays selectable.
  const delays = RETRY_DELAY_PRESETS_SECONDS.includes(retryDelaySeconds)
    ? RETRY_DELAY_PRESETS_SECONDS
    : [...RETRY_DELAY_PRESETS_SECONDS, retryDelaySeconds].sort((a, b) => a - b);
  return (
    <div
      className="flex min-h-9 items-center justify-between gap-4 py-px"
      data-testid="task-retries-field"
    >
      <span className="text-ui text-muted-foreground">Retries</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <ComposerHarnessTrigger
            label="Retries"
            model={retrySummary(maxRetries, retryDelaySeconds)}
            icon={<RotateCcwIcon className="size-4 shrink-0" />}
            className={triggerClassName}
            data-testid="task-retries-trigger"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
          <p className="px-2 py-1.5 text-xs whitespace-normal text-muted-foreground">
            Starts a run again when it fails before its prompt reaches the agent, such as when its
            host is offline. A retry that would start at or after the next scheduled run is dropped.
          </p>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>Retries</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={String(maxRetries)}
            onValueChange={(value) => onMaxRetriesChange(Number(value))}
          >
            {RETRY_COUNTS.map((count) => (
              <DropdownMenuRadioItem
                key={count}
                value={String(count)}
                data-testid={`task-retries-count-${count}`}
              >
                {count === 0 ? "Off" : String(count)}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          {maxRetries > 0 && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>Wait before each retry</DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={String(retryDelaySeconds)}
                onValueChange={(value) => onRetryDelayChange(Number(value))}
              >
                {delays.map((seconds) => (
                  <DropdownMenuRadioItem
                    key={seconds}
                    value={String(seconds)}
                    data-testid={`task-retries-delay-${seconds}`}
                  >
                    {delayLabel(seconds)}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
