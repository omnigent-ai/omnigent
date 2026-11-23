import { type ReactNode, useEffect, useState } from "react";
import { ChevronDownIcon } from "lucide-react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import {
  ComposerConfigSections,
  type ComposerConfigSection,
} from "@/components/composer/ComposerConfigSections";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { useLaunchModelOptions } from "@/hooks/useLaunchModelOptions";
import { selectableSessionAgents } from "@/lib/agentGrouping";
import { pickerCuration } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  defaultModelLabel,
  nativeModelLabel,
  normalizeEffortLabel,
} from "@/lib/composerModelLabel";
import { readLastHarness } from "@/lib/harnessPreferences";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { readHarnessOptions } from "@/lib/modePreferences";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import type { ProjectConfig } from "@/lib/projectsApi";

const PROJECT_HARNESSES = [...NATIVE_CODING_AGENTS].sort(
  (left, right) => left.sortRank - right.sortRank,
);

export function useProjectChatChoices(
  enabled: boolean,
  host: Host | undefined,
  sandbox: boolean,
  provider: string | null,
) {
  const info = useServerInfo();
  const catalog = useAvailableAgents({ enabled });
  const agents = selectableSessionAgents(
    catalog.data ?? [],
    pickerCuration(info).restrictToAgentNames,
  );
  const [choice, setChoice] = useState<Pick<ProjectConfig, "harness" | "model" | "effort">>({});
  useEffect(() => {
    if (!enabled) setChoice({});
  }, [enabled]);
  const selected = agents.find((agent) => agent.name === "maestro");
  const harness =
    nativeCodingAgentForHarness(choice.harness)?.harness ??
    nativeCodingAgentForHarness(readLastHarness(selected?.id))?.harness ??
    nativeCodingAgentForHarness(selected?.harness)?.harness ??
    PROJECT_HARNESSES[0].harness;
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
  const selectHarness = (nextHarness: string) => setChoice({ harness: nextHarness });
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
    selected,
    harness,
    model,
    effort,
    options,
    selectHarness,
    selectModel,
    selectEffort,
    loading: catalog.isLoading,
    unavailable: !sandbox && !!host && !harnessReadinessOnHost(harness, host).selectable,
    config: {
      agent_id: selected?.id,
      ...(harness ? { harness } : {}),
      ...(model ? { model } : {}),
      ...(effort ? { effort } : {}),
    },
  };
}

function ProjectLaunchControl({
  label,
  value,
  icon,
  section,
  modelCatalog = false,
  disabled,
  onOpenChange,
}: {
  label: string;
  value: string;
  icon?: ReactNode;
  section: ComposerConfigSection;
  modelCatalog?: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const changeOpen = (next: boolean) => {
    setOpen(next);
    onOpenChange(next);
  };
  const menu = {
    ...section,
    choices: section.choices.map((choice) => ({
      ...choice,
      onSelect: choice.onSelect
        ? () => {
            choice.onSelect?.();
            changeOpen(false);
          }
        : undefined,
    })),
  };
  const triggerId = `${section.testId}-trigger`;
  return (
    <div className="grid gap-1.5">
      <label htmlFor={triggerId} className="text-sm">
        {label}
      </label>
      <DropdownMenu open={open} onOpenChange={changeOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            id={triggerId}
            type="button"
            variant="outline"
            aria-label={label}
            disabled={disabled}
            className="h-10 w-full justify-between gap-2 border-input px-3 text-ui mobile-app:h-11"
          >
            <span className="flex min-w-0 items-center gap-2">
              {icon}
              <span className="truncate">{value}</span>
            </span>
            <ChevronDownIcon className="size-4 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-(--radix-dropdown-menu-trigger-width)">
          <ComposerConfigSections
            models={modelCatalog ? menu : undefined}
            extra={modelCatalog ? undefined : [menu]}
          />
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
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
  const disabled = choices.loading || !choices.selected;
  const harnesses: ComposerConfigSection = {
    testId: "project-main-harnesses",
    header: "Harness",
    choices: PROJECT_HARNESSES.map((harness) => {
      const readiness = !sandbox && host ? harnessReadinessOnHost(harness.harness, host) : null;
      return {
        key: harness.harness,
        label: (
          <span className="flex min-w-0 items-center gap-2">
            <ComposerAgentIcon agent={{ name: harness.agentName, harness: harness.harness }} />
            <span className="truncate">{harness.displayName}</span>
          </span>
        ),
        checked: choices.harness === harness.harness,
        disabled: readiness !== null && !readiness.selectable,
        title: readiness?.explanation?.description,
        onSelect: () => choices.selectHarness(harness.harness),
      };
    }),
  };
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
  const harness = nativeCodingAgentForHarness(choices.harness);
  return (
    <div className="space-y-3">
      <ProjectLaunchControl
        label="Harness"
        value={harness?.displayName ?? "Select harness"}
        icon={
          harness && (
            <ComposerAgentIcon agent={{ name: harness.agentName, harness: harness.harness }} />
          )
        }
        section={harnesses}
        disabled={disabled}
        onOpenChange={onOpenChange}
      />
      <ProjectLaunchControl
        label="Model"
        value={
          model
            ? nativeModelLabel(model)
            : choices.model || defaultModelLabel(choices.options.models)
        }
        section={models}
        modelCatalog
        disabled={disabled}
        onOpenChange={onOpenChange}
      />
      {efforts && (
        <ProjectLaunchControl
          label="Effort"
          value={choices.effort ? normalizeEffortLabel(choices.effort) : "Default"}
          section={efforts}
          disabled={disabled}
          onOpenChange={onOpenChange}
        />
      )}
      {!choices.loading && !choices.selected && (
        <p className="text-sm text-destructive" role="alert">
          Maestro is unavailable.
        </p>
      )}
    </div>
  );
}
