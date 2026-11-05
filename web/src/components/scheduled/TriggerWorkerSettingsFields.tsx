import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { Label } from "./Label";
import { ModelEffortFields } from "./ModelEffortFields";
import type { TriggerWorkerSettings } from "./triggerTemplates";
import type { useScheduledHarnessSettings } from "./useScheduledHarnessSettings";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function TriggerWorkerSettingsFields({
  value,
  pollingAgentId,
  agents,
  selectedAgent,
  settings,
  disabled,
  onChange,
  onSelectOpenChange,
}: {
  value: TriggerWorkerSettings;
  pollingAgentId: string;
  agents: AvailableAgent[];
  selectedAgent: AvailableAgent | undefined;
  settings: ReturnType<typeof useScheduledHarnessSettings>;
  disabled: boolean;
  onChange: (value: TriggerWorkerSettings) => void;
  onSelectOpenChange: (open: boolean) => void;
}) {
  const agentId = value.agentId ?? pollingAgentId;
  return (
    <div className="space-y-3" data-testid="task-worker-settings">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="task-worker-agent">Harness for matching sessions</Label>
        <Select
          value={agentId}
          disabled={disabled}
          onValueChange={(nextAgentId) => onChange({ agentId: nextAgentId, model: "", effort: "" })}
          componentId="tasks.scheduled.worker_harness"
          onOpenChange={onSelectOpenChange}
        >
          <SelectTrigger
            id="task-worker-agent"
            data-testid="task-worker-agent-trigger"
            className="w-full"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
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
      <ModelEffortFields
        model={value.model}
        effort={value.effort}
        permissionMode=""
        settings={{ ...settings, permissions: [] }}
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
        onPermissionModeChange={() => {}}
        onSelectOpenChange={onSelectOpenChange}
      />
      {settings.blockedReason && (
        <p role="alert" className="text-sm text-destructive">
          {settings.blockedReason}
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        Applies to new sessions started for matching items. Existing sessions, including the polling
        session, keep their settings. Default uses the selected harness&apos;s configuration.
      </p>
    </div>
  );
}
