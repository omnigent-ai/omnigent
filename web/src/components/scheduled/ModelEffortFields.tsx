import { ScheduleSelect as Select } from "./ScheduleSelect";
// Harness-specific automation controls; Default leaves the agent configuration unchanged.

import { cn } from "@/lib/utils";
import { pickerTriggerClass } from "./pickerStyles";
import { Label } from "@/components/scheduled/Label";
import { SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { EFFORT_SELECT_NONE, MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import type { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";

/** Sentinel Select value for "no permission override" (use the agent default).
 * Distinct from Claude's real `default` (Manual) mode so leaving the control
 * alone keeps the agent's configured mode rather than forcing Manual. */
const PERMISSION_SELECT_DEFAULT = "__agent_default__";

export function ModelEffortFields({
  model,
  effort,
  permissionMode,
  settings,
  disabled = false,
  onModelChange,
  onEffortChange,
  onPermissionModeChange,
  onSelectOpenChange,
}: {
  /** Selected model id/alias, or "" = agent default (nothing overridden). */
  model: string;
  /** Selected reasoning effort, or "" = agent default. */
  effort: string;
  /** Selected permission mode, or "" = agent default (nothing overridden). */
  permissionMode: string;
  disabled?: boolean;
  settings: ReturnType<typeof useScheduledHarnessSettings>;
  onModelChange: (model: string) => void;
  onEffortChange: (effort: string) => void;
  onPermissionModeChange: (mode: string) => void;
  /** Forwarded to each Select's onOpenChange so the parent Dialog can keep an
   * open dropdown from dismissing the whole modal. */
  onSelectOpenChange?: (open: boolean) => void;
}) {
  const modelOptions = settings.models.map((option) => ({
    id: option.id,
    label: option.displayName ?? option.id,
  }));
  if (model && !modelOptions.some((option) => option.id === model)) {
    modelOptions.push({ id: model, label: model });
  }
  const effortOptions = [...settings.efforts];
  if (effort && !effortOptions.some((option) => option.value === effort)) {
    effortOptions.push({ value: effort, label: effort });
  }
  const permissionOptions = settings.permissions.filter(
    (option) => settings.harness === "claude-native" || option.value !== "default",
  );

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      {settings.supportsModel && (
        <div className="flex flex-col gap-3" data-testid="task-model-effort-row">
          <div
            className="flex w-full min-w-0 items-center justify-between gap-4"
            data-testid="task-model-control"
          >
            <Label htmlFor="task-model">Model</Label>
            <Select
              disabled={disabled}
              value={model === "" ? MODEL_SELECT_DEFAULT : model}
              componentId="tasks.scheduled.model"
              valueHasNoPii
              onValueChange={(v) => onModelChange(v === MODEL_SELECT_DEFAULT ? "" : v)}
              onOpenChange={onSelectOpenChange}
            >
              <SelectTrigger
                id="task-model"
                data-testid="task-model-trigger"
                className={cn(
                  "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                  pickerTriggerClass,
                )}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent
                position="popper"
                align="end"
                className="w-[min(20rem,calc(100vw-2rem))]"
              >
                <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
                {modelOptions.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {effortOptions.length > 0 && (
            <div
              className="flex w-full min-w-0 items-center justify-between gap-4"
              data-testid="task-effort-control"
            >
              <Label htmlFor="task-effort">Effort</Label>
              <Select
                disabled={disabled}
                value={effort === "" ? EFFORT_SELECT_NONE : effort}
                componentId="tasks.scheduled.effort"
                valueHasNoPii
                onValueChange={(v) => onEffortChange(v === EFFORT_SELECT_NONE ? "" : v)}
                onOpenChange={onSelectOpenChange}
              >
                <SelectTrigger
                  id="task-effort"
                  data-testid="task-effort-trigger"
                  className={cn(
                    "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                    pickerTriggerClass,
                  )}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent
                  position="popper"
                  align="end"
                  className="w-[min(20rem,calc(100vw-2rem))]"
                >
                  <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
                  {effortOptions.map((e) => (
                    <SelectItem key={e.value} value={e.value}>
                      {e.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}
      {settings.catalogMessage && (
        <p className="text-sm text-muted-foreground">{settings.catalogMessage}</p>
      )}

      {permissionOptions.length > 0 && (
        <div
          className="flex w-full min-w-0 items-center justify-between gap-4"
          data-testid="task-permission-control"
        >
          <Label htmlFor="task-permission">Permissions</Label>
          <Select
            disabled={disabled}
            value={
              permissionMode === "" ||
              (settings.harness !== "claude-native" && permissionMode === "default")
                ? PERMISSION_SELECT_DEFAULT
                : permissionMode
            }
            componentId="tasks.scheduled.permission_mode"
            valueHasNoPii
            onValueChange={(v) => onPermissionModeChange(v === PERMISSION_SELECT_DEFAULT ? "" : v)}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id="task-permission"
              data-testid="task-permission-trigger"
              className={cn(
                "w-auto max-w-[65%] border-0 shadow-none text-muted-foreground",
                pickerTriggerClass,
              )}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              position="popper"
              align="end"
              className="w-[min(20rem,calc(100vw-2rem))]"
            >
              <SelectItem value={PERMISSION_SELECT_DEFAULT}>Default</SelectItem>
              {permissionOptions.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
