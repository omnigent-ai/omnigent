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
/** Sentinel for an empty mode that inherits another session's, shown beside Default. */
const PERMISSION_SELECT_INHERITED = "__inherited__";

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
  idPrefix = "task",
  inheritedPermissionLabel,
  defaultPermissionMode = "",
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
  /** Prefix for element ids and test ids, so two groups can share a form. */
  idPrefix?: string;
  /** Offers an empty mode under this label because it inherits another session's. */
  inheritedPermissionLabel?: string;
  /** The mode Default saves; "" leaves the mode empty. A listed mode hides Default once left. */
  defaultPermissionMode?: string;
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
  const inheritsPermission = Boolean(inheritedPermissionLabel) && permissionMode === "";
  const permissionForSelectValue = (value: string) => {
    if (value === PERMISSION_SELECT_INHERITED) return "";
    return value === PERMISSION_SELECT_DEFAULT ? defaultPermissionMode : value;
  };
  let permissionSelectValue = permissionMode;
  if (inheritsPermission) permissionSelectValue = PERMISSION_SELECT_INHERITED;
  else if (
    permissionMode === "" ||
    (settings.harness !== "claude-native" && permissionMode === "default")
  )
    permissionSelectValue = PERMISSION_SELECT_DEFAULT;
  // Once left, Default would duplicate a listed mode (Claude lists its default as Manual).
  const showsDefaultPermission =
    permissionSelectValue === PERMISSION_SELECT_DEFAULT ||
    !permissionOptions.some((option) => option.value === defaultPermissionMode);

  return (
    <div className="flex flex-col gap-3 sm:gap-4">
      {settings.supportsModel && (
        <div className="flex flex-col gap-3" data-testid={`${idPrefix}-model-effort-row`}>
          <div
            className="flex w-full min-w-0 items-center justify-between gap-4"
            data-testid={`${idPrefix}-model-control`}
          >
            <Label htmlFor={`${idPrefix}-model`}>Model</Label>
            <Select
              disabled={disabled}
              value={model === "" ? MODEL_SELECT_DEFAULT : model}
              componentId="tasks.scheduled.model"
              valueHasNoPii
              onValueChange={(v) => onModelChange(v === MODEL_SELECT_DEFAULT ? "" : v)}
              onOpenChange={onSelectOpenChange}
            >
              <SelectTrigger
                id={`${idPrefix}-model`}
                data-testid={`${idPrefix}-model-trigger`}
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
              data-testid={`${idPrefix}-effort-control`}
            >
              <Label htmlFor={`${idPrefix}-effort`}>Effort</Label>
              <Select
                disabled={disabled}
                value={effort === "" ? EFFORT_SELECT_NONE : effort}
                componentId="tasks.scheduled.effort"
                valueHasNoPii
                onValueChange={(v) => onEffortChange(v === EFFORT_SELECT_NONE ? "" : v)}
                onOpenChange={onSelectOpenChange}
              >
                <SelectTrigger
                  id={`${idPrefix}-effort`}
                  data-testid={`${idPrefix}-effort-trigger`}
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
          data-testid={`${idPrefix}-permission-control`}
        >
          <Label htmlFor={`${idPrefix}-permission`}>Permissions</Label>
          <Select
            disabled={disabled}
            value={permissionSelectValue}
            componentId="tasks.scheduled.permission_mode"
            valueHasNoPii
            onValueChange={(v) => onPermissionModeChange(permissionForSelectValue(v))}
            onOpenChange={onSelectOpenChange}
          >
            <SelectTrigger
              id={`${idPrefix}-permission`}
              data-testid={`${idPrefix}-permission-trigger`}
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
              {inheritedPermissionLabel && (
                <SelectItem value={PERMISSION_SELECT_INHERITED}>
                  {inheritedPermissionLabel}
                </SelectItem>
              )}
              {showsDefaultPermission && (
                <SelectItem value={PERMISSION_SELECT_DEFAULT}>Default</SelectItem>
              )}
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
