import { useEffect, useState } from "react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import {
  ComposerConfigSections,
  type ComposerConfigSection,
} from "@/components/composer/ComposerConfigSections";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { useLaunchModelOptions } from "@/hooks/useLaunchModelOptions";
import { useBrainHarnessLabels } from "@/lib/agentLabels";
import { partitionPickerEntries, selectableSessionAgents } from "@/lib/agentGrouping";
import { pickerCuration } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  defaultModelLabel,
  nativeModelLabel,
  normalizeEffortLabel,
} from "@/lib/composerModelLabel";
import { readLastHarness } from "@/lib/harnessPreferences";
import { readHarnessOptions } from "@/lib/modePreferences";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import type { ProjectConfig } from "@/lib/projectsApi";
import { AgentHarnessPicker } from "./NewChatDialog";

export function useProjectChatChoices(
  enabled: boolean,
  host: Host | undefined,
  sandbox: boolean,
  provider: string | null,
) {
  const info = useServerInfo();
  const catalog = useAvailableAgents({ enabled });
  const harnessLabels = useBrainHarnessLabels();
  const agents = selectableSessionAgents(
    catalog.data ?? [],
    pickerCuration(info).restrictToAgentNames,
  );
  const [choice, setChoice] = useState<
    Pick<ProjectConfig, "agent_id" | "harness" | "model" | "effort">
  >({});
  useEffect(() => {
    if (!enabled) setChoice({});
  }, [enabled]);
  const selected = choice.agent_id
    ? agents.find((agent) => agent.id === choice.agent_id)
    : agents.find((agent) => agent.name === "maestro");
  const native = nativeCodingAgentForAvailableAgent(selected);
  const configurable = !!selected?.harness && !native && selected.harness in harnessLabels;
  const harness =
    native?.harness ??
    (configurable ? (choice.harness ?? readLastHarness(selected?.id)) : null) ??
    selected?.harness ??
    null;
  const remembered = readHarnessOptions(harness);
  const model = choice.model ?? remembered.model ?? "";
  const effort = choice.effort ?? remembered.effort ?? "";
  const options = useLaunchModelOptions({
    host,
    sandbox,
    provider,
    harness,
    agentId: selected?.id ?? null,
    model,
    enabled: enabled && !!selected,
  });
  const selectAgent = (agent: AvailableAgent) => {
    if (agent.id !== selected?.id) setChoice({ agent_id: agent.id });
  };
  const selectHarness = (nextHarness: string) =>
    setChoice({ agent_id: selected?.id, harness: nextHarness });
  const selectModel = (nextModel: string) =>
    setChoice((previous) => ({
      ...previous,
      model: nextModel,
      effort: options.effortsForModel(nextModel).some((option) => option.value === effort)
        ? effort
        : "",
    }));
  const selectEffort = (nextEffort: string) =>
    setChoice((previous) => ({ ...previous, effort: nextEffort }));
  return {
    agents,
    selected,
    harness,
    configurable,
    harnessLabels,
    model,
    effort,
    options,
    selectAgent,
    selectHarness,
    selectModel,
    selectEffort,
    loading: catalog.isLoading,
    config: {
      agent_id: selected?.id,
      ...(configurable && harness ? { harness } : {}),
      ...(model ? { model } : {}),
      ...(effort ? { effort } : {}),
    },
  };
}

export function ProjectChatLaunchPicker({
  choices,
  host,
  sandbox,
  onOpenChange,
}: {
  choices: ReturnType<typeof useProjectChatChoices>;
  host?: Host;
  sandbox: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { harnesses, agents } = partitionPickerEntries(choices.agents);
  const selected = choices.selected;
  const models: ComposerConfigSection = {
    testId: "project-chat-models",
    header: "Models",
    leading:
      choices.options.models.length === 0 ? (
        <div className="px-2 py-1 text-xs text-muted-foreground">
          {choices.options.loading ? "Loading models…" : "Models unavailable"}
        </div>
      ) : undefined,
    choices: [
      {
        key: "default",
        label: "Default",
        checked: !choices.model,
        onSelect: () => choices.selectModel(""),
      },
      ...choices.options.models.map((model) => ({
        key: model.id,
        label: nativeModelLabel(model),
        modelOption: model,
        checked: choices.model === model.id,
        onSelect: () => choices.selectModel(model.id),
      })),
    ],
  };
  const efforts: ComposerConfigSection | undefined = choices.options.efforts.length
    ? {
        testId: "project-chat-efforts",
        header: "Effort",
        choices: [
          {
            key: "default",
            label: "Default",
            checked: !choices.effort,
            onSelect: () => choices.selectEffort(""),
          },
          ...choices.options.efforts.map((effort) => ({
            key: effort.value,
            label: effort.label,
            checked: choices.effort === effort.value,
            onSelect: () => choices.selectEffort(effort.value),
          })),
        ],
      }
    : undefined;
  const model = choices.options.models.find((option) => option.id === choices.model);
  return (
    <AgentHarnessPicker
      agentEntries={agents}
      harnessEntries={harnesses}
      effectiveAgentId={selected?.id ?? null}
      agentLabel={selected?.display_name ?? "Select agent"}
      hasAgents={choices.agents.length > 0}
      loading={choices.loading}
      host={host}
      onSelectAgent={choices.selectAgent}
      pendingAgent={null}
      pendingAgentId="__unused__"
      onSelectPending={() => {}}
      onCreateCustomAgent={() => {}}
      allowCreateCustomAgent={false}
      sandboxSelected={sandbox}
      dropdownModal
      onOpenChange={onOpenChange}
      contentAlign="start"
      triggerClassName="max-w-full"
      triggerIcon={selected ? <ComposerAgentIcon agent={selected} /> : undefined}
      modelCatalogAvailability={
        choices.options.loading
          ? "unknown"
          : choices.options.models.length
            ? "available"
            : "unavailable"
      }
      triggerDetails={[
        ...(choices.configurable
          ? [
              {
                label: "SDK",
                value: choices.harnessLabels[choices.harness ?? ""] ?? choices.harness ?? "Default",
              },
            ]
          : []),
        {
          label: "Model",
          value: model
            ? nativeModelLabel(model)
            : choices.model || defaultModelLabel(choices.options.models),
        },
        ...(choices.effort
          ? [{ label: "Effort", value: normalizeEffortLabel(choices.effort) }]
          : []),
      ]}
      isEntryConfigurable={() => true}
      selectedConfigContent={
        <ComposerConfigSections
          sdk={
            choices.configurable
              ? {
                  testId: "project-chat-harnesses",
                  header: "Agent SDK",
                  choices: Object.entries(choices.harnessLabels).map(([id, label]) => ({
                    key: id,
                    label,
                    checked: choices.harness === id,
                    onSelect: () => choices.selectHarness(id),
                  })),
                }
              : undefined
          }
          models={models}
          efforts={efforts}
        />
      }
    />
  );
}
