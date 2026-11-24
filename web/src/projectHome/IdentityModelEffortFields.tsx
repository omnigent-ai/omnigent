import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { useLaunchModelOptions } from "@/hooks/useLaunchModelOptions";
import { selectableSessionAgents } from "@/lib/agentGrouping";
import { pickerCuration } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { normalizeEffortLabel, nativeModelLabel } from "@/lib/composerModelLabel";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import type { ProjectConfig } from "@/lib/projectsApi";
import { firstAvailableNativeAgent } from "./projectAgentDefaults";

// Radix Select can't use an empty value, so "Project default" gets a sentinel.
const DEFAULT_CHOICE = "__default__";

export interface IdentitySessionChoice {
  agentId: string;
  model: string;
  effort: string;
}

/**
 * Agent, model and effort for an identity's sessions, chosen from what the
 * project's host and that agent support. Empty values mean the default.
 */
export function IdentityModelEffortFields({
  config,
  agentId,
  model,
  effort,
  disabled,
  agentLocked = false,
  defaultLabel = "Project default",
  onChange,
}: IdentitySessionChoice & {
  config: ProjectConfig | undefined;
  disabled: boolean;
  /** Only the project owner picks the agent of sessions that run as them. */
  agentLocked?: boolean;
  /** Label for the empty choice, e.g. "Default" where these are the project defaults. */
  defaultLabel?: string;
  onChange: (next: IdentitySessionChoice) => void;
}) {
  const info = useServerInfo();
  const sandbox = config?.host_id === SANDBOX_HOST_CHOICE;
  const hosts = useHosts({ enabled: !!config?.host_id && !sandbox });
  const host = sandbox ? undefined : hosts.data?.find((entry) => entry.host_id === config?.host_id);
  const catalog = useAvailableAgents({ enabled: config !== undefined });
  const agents = selectableSessionAgents(
    catalog.data ?? [],
    pickerCuration(info).restrictToAgentNames,
  ).filter((entry) => nativeCodingAgentForAvailableAgent(entry));
  // Sessions launch with the identity's agent, else the project's, else the first usable one.
  const preferred = agentId || config?.agent_id || null;
  const agent =
    (preferred ? agents.find((entry) => entry.id === preferred) : undefined) ??
    firstAvailableNativeAgent(agents, host);
  const harness = config?.harness ?? nativeCodingAgentForAvailableAgent(agent)?.harness ?? null;
  const offline = !sandbox && host?.status === "offline";
  const options = useLaunchModelOptions({
    host,
    sandbox,
    provider: config?.sandbox_provider ?? null,
    harness,
    agentId: agent?.id ?? null,
    model,
    enabled: config !== undefined && !offline,
  });
  // Keep a saved value selectable even when this host doesn't list it.
  const models =
    options.models.some((option) => option.id === model) || !model
      ? options.models
      : [...options.models, { id: model }];
  const efforts = options.effortsForModel(model);
  const effortChoices =
    effort && !efforts.some((option) => option.value === effort)
      ? [...efforts, { value: effort, label: normalizeEffortLabel(effort) }]
      : efforts;
  const hint = offline
    ? `${host?.name ?? "The project's host"} is offline. Reconnect it to choose a model.`
    : !config?.host_id
      ? "The project has no default host, so models can't be listed."
      : options.loading
        ? "Loading models…"
        : options.error || options.unsupported
          ? "Models aren't available for this host."
          : null;
  const unavailable = disabled || offline || options.loading || models.length === 0;
  const selectModel = (value: string) => {
    const next = value === DEFAULT_CHOICE ? "" : value;
    const keepEffort = options.effortsForModel(next).some((option) => option.value === effort);
    onChange({ agentId, model: next, effort: keepEffort ? effort : "" });
  };
  // Another agent has its own catalog, so its model and effort start from the default.
  const selectAgent = (value: string) =>
    onChange({ agentId: value === DEFAULT_CHOICE ? "" : value, model: "", effort: "" });
  // Keep a saved agent selectable even when this catalog doesn't list it.
  const missingAgent = agentId && !agents.some((entry) => entry.id === agentId) ? agentId : null;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
          Agent
          <Select
            value={agentId || DEFAULT_CHOICE}
            onValueChange={selectAgent}
            disabled={disabled || agentLocked || catalog.isLoading}
          >
            <SelectTrigger data-testid="project-team-agent">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_CHOICE}>{defaultLabel}</SelectItem>
              {agents.map((entry) => {
                const native = nativeCodingAgentForAvailableAgent(entry);
                const blocked =
                  !sandbox &&
                  !!host &&
                  !!native &&
                  !harnessReadinessOnHost(native.harness, host).selectable;
                return (
                  <SelectItem key={entry.id} value={entry.id} disabled={blocked}>
                    {entry.display_name}
                  </SelectItem>
                );
              })}
              {missingAgent && <SelectItem value={missingAgent}>{missingAgent}</SelectItem>}
            </SelectContent>
          </Select>
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
          Model
          <Select
            value={model || DEFAULT_CHOICE}
            onValueChange={selectModel}
            disabled={unavailable}
          >
            <SelectTrigger data-testid="project-team-model">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_CHOICE}>{defaultLabel}</SelectItem>
              {models.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {nativeModelLabel(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
          Effort
          <Select
            value={effort || DEFAULT_CHOICE}
            onValueChange={(value) =>
              onChange({ agentId, model, effort: value === DEFAULT_CHOICE ? "" : value })
            }
            disabled={disabled || offline || effortChoices.length === 0}
          >
            <SelectTrigger data-testid="project-team-effort">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_CHOICE}>{defaultLabel}</SelectItem>
              {effortChoices.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      </div>
      {hint && (
        <span className="text-xs text-muted-foreground" data-testid="project-team-model-hint">
          {hint}
        </span>
      )}
    </div>
  );
}
