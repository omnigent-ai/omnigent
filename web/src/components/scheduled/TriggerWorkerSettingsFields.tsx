import { cn } from "@/lib/utils";
import { APPROVAL_SKIPPING_PERMISSION_MODES } from "@/lib/nativeHarnessModes";
import { pickerTriggerClass } from "./pickerStyles";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { Label } from "./Label";
import { ModelEffortFields } from "./ModelEffortFields";
import type { TriggerWorkerSettings } from "./triggerTemplates";
import type { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";
import { ScheduleSelect as Select } from "./ScheduleSelect";
import { SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";

export function TriggerWorkerHarnessField({
  value,
  pollingAgentId,
  agents,
  selectedAgent,
  disabled,
  onChange,
  onSelectOpenChange,
}: {
  value: TriggerWorkerSettings;
  pollingAgentId: string;
  agents: AvailableAgent[];
  selectedAgent: AvailableAgent | undefined;
  disabled: boolean;
  onChange: (value: TriggerWorkerSettings) => void;
  onSelectOpenChange: (open: boolean) => void;
}) {
  const agentId = value.agentId ?? pollingAgentId;
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-4">
      <Label htmlFor="task-worker-agent">Harness for matching sessions</Label>
      <Select
        value={agentId}
        disabled={disabled}
        onValueChange={(nextAgentId) =>
          onChange({ agentId: nextAgentId, model: "", effort: "", permissionMode: "" })
        }
        componentId="tasks.scheduled.worker_harness"
        onOpenChange={onSelectOpenChange}
      >
        <SelectTrigger
          id="task-worker-agent"
          data-testid="task-worker-agent-trigger"
          className={cn(
            "w-auto max-w-40 border-0 shadow-none text-muted-foreground",
            pickerTriggerClass,
          )}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="end">
          {!agents.some((agent) => agent.id === agentId) && (
            <SelectItem value={agentId}>{selectedAgent?.display_name ?? agentId}</SelectItem>
          )}
          {agents.map((agent) => (
            <SelectItem key={agent.id} value={agent.id}>
              {agent.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function TriggerWorkerSettingsFields({
  value,
  settings,
  permissionModesSupported = false,
  taskSession = false,
  disabled,
  onChange,
  onSelectOpenChange,
}: {
  value: TriggerWorkerSettings;
  settings: ReturnType<typeof useScheduledHarnessSettings>;
  /** The server applies a session's requested permission mode. */
  permissionModesSupported?: boolean;
  /** Every match goes to one task session, so changes wait for the next one. */
  taskSession?: boolean;
  disabled: boolean;
  onChange: (value: TriggerWorkerSettings) => void;
  onSelectOpenChange: (open: boolean) => void;
}) {
  // The server refuses these for a session an agent starts.
  const permissions = permissionModesSupported
    ? settings.permissions.filter((option) => !APPROVAL_SKIPPING_PERMISSION_MODES.has(option.value))
    : [];
  // The call would be refused when the server can't apply a saved mode, or (for a
  // hand-written prompt) the harness has no such mode, so offer to drop it.
  const unappliedMode =
    value.permissionMode && !permissions.some((option) => option.value === value.permissionMode)
      ? value.permissionMode
      : "";
  const unappliedLabel =
    settings.permissions.find((option) => option.value === unappliedMode)?.label ?? unappliedMode;
  return (
    <div className="space-y-3" data-testid="task-worker-settings">
      <ModelEffortFields
        model={value.model}
        effort={value.effort}
        permissionMode={value.permissionMode}
        settings={{ ...settings, permissions }}
        disabled={disabled}
        onModelChange={(model) =>
          onChange({
            ...value,
            model,
            effort: settings.effortsForModel(model).some((option) => option.value === value.effort)
              ? value.effort
              : "",
          })
        }
        onEffortChange={(effort) => onChange({ ...value, effort })}
        onPermissionModeChange={(permissionMode) => onChange({ ...value, permissionMode })}
        onSelectOpenChange={onSelectOpenChange}
      />
      {unappliedMode && (
        <div
          className="flex items-center justify-between gap-3"
          data-testid="task-worker-permission-unapplied"
        >
          <p className="text-sm text-destructive">
            Matching sessions are set to start in {unappliedLabel},{" "}
            {permissionModesSupported
              ? "which this harness doesn't offer here"
              : "which this server can't apply"}
            , so they won&apos;t start.
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            data-testid="task-worker-permission-clear"
            onClick={() => onChange({ ...value, permissionMode: "" })}
          >
            Clear
          </Button>
        </div>
      )}
      {settings.blockedReason && (
        <p role="alert" className="text-sm text-destructive">
          {settings.blockedReason}
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        {taskSession
          ? "Applies to the next task session. The current task session and the polling session keep their settings; archive the task session so the next match starts one with these. "
          : "Applies to new sessions started for matching items. Existing sessions, including the polling session, keep their settings. "}
        Default uses the selected harness&apos;s configuration.
        {permissions.length > 0 && " Modes that skip approval aren't available here."}
      </p>
    </div>
  );
}
