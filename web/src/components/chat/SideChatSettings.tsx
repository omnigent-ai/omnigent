import { useEffect, useMemo, useState } from "react";
import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import {
  HarnessPicker,
  HarnessPickerConfigRow,
  HarnessPickerConfigPage,
} from "@/components/composer/HarnessPicker";
import { ComposerConfigSections } from "@/components/composer/ComposerConfigSections";
import {
  compactModelTriggerLabel,
  defaultModelLabel,
  findModelLabelOption,
  formatStatusEffortLabel,
  formatStatusModelLabel,
  nativeModelLabel,
  nativeModelTooltip,
} from "@/lib/composerModelLabel";
import { currentFusionCombo, fusionOption, isFusionModelUid } from "@/lib/devinFusion";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useSession } from "@/hooks/useSession";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useHostModelOptions } from "@/hooks/useHosts";
import { useHarnessEffortFamilies } from "@/lib/agentLabels";
import {
  effortAfterModelChange,
  effortLevelsForConv,
  sdkEffortFamilyForHarness,
  selectedModelForPicker,
  modelPickerKindForConv,
  shouldShowEffortPicker,
} from "@/lib/composerCapabilities";
import { isEditorLevel } from "@/lib/permissionsApi";
import { updateSession } from "@/lib/sessionsApi";
import { parentIdFromLabels } from "@/lib/sessionHost";
import { sideChatSettingsMutationKey, type SideChatModelSettings } from "@/lib/sideChat";
import type { Session } from "@/lib/types";

/** Model controls always address this pane's session, independent of the active main chat. */
export function SideChatSettings({
  session,
  draft,
}: {
  session: Session;
  draft?: {
    settings?: SideChatModelSettings;
    onChange: (updates: SideChatModelSettings) => void;
    disabled: boolean;
  };
}) {
  const queryClient = useQueryClient();
  const state = useConversationEntryState(session.id);
  const nativeSide = session.labels?.["omnigent.codex_native.agent_nickname"] === "Side chat";
  const sourceId = session.parentSessionId ?? parentIdFromLabels(session.labels);
  const nativeSession =
    nativeSide ||
    session.harness === "codex-native" ||
    session.labels?.["omnigent.wrapper"] === "codex-native-ui";
  const settingsSupported = !nativeSession || session.sideChatSettingsSupported === true;
  const settingsParentId = nativeSide && !draft ? sourceId : null;
  const healthSessions = useMemo(
    () => (settingsParentId ? [{ id: settingsParentId }] : []),
    [settingsParentId],
  );
  const runnerHealth = useRunnerHealthRegistration(healthSessions);
  const runnerOnline = settingsParentId ? runnerHealth.get(settingsParentId) : undefined;
  useEffect(() => {
    // The parent runner can reconnect without interrupting the child's browser stream.
    if (runnerOnline !== undefined) {
      void queryClient.invalidateQueries({ queryKey: ["session", session.id] });
    }
  }, [queryClient, session.id, runnerOnline]);
  const { session: parent } = useSession(sourceId);
  const parentState = useConversationEntryState(nativeSide ? sourceId : null);
  const families = useHarnessEffortFamilies();
  const isMobile = useIsMobileViewport();
  const [menuOpen, setMenuOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const mutationKey = sideChatSettingsMutationKey(session.id);
  const saving = useIsMutating({ mutationKey }) > 0;
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationKey,
    mutationFn: (updates: SideChatModelSettings) => updateSession(session.id, updates),
    onSuccess: (updated) => {
      queryClient.setQueryData<Session>(["session", session.id], (previous) => ({
        ...previous,
        ...updated,
      }));
    },
    onError: (cause) => {
      const message =
        cause instanceof Error ? cause.message : "Couldn't update side chat settings.";
      setError(message);
      toast.error(message);
    },
  });
  const catalogs = [state.codexModelOptions, session.codexModelOptions];
  if (nativeSide) catalogs.push(parentState.codexModelOptions, parent?.codexModelOptions);
  const capabilities = nativeSide
    ? { ...session, labels: { ...session.labels, "omnigent.wrapper": "codex-native-ui" } }
    : session;
  const sessionOptions = catalogs.find((catalog) => catalog?.length) ?? [];
  const kind = modelPickerKindForConv(capabilities, sessionOptions);
  const probeHarness =
    kind === "codex" || kind === "claude" || kind === "devin" ? `${kind}-native` : null;
  const { data: hostOptions } = useHostModelOptions(
    session.hostId ?? parent?.hostId ?? null,
    probeHarness ?? "",
    settingsSupported &&
      !session.inferenceConfigured &&
      probeHarness !== null &&
      sessionOptions.length === 0,
  );
  // Share the main composer's host catalog while the session has no model list.
  const options =
    !session.inferenceConfigured && sessionOptions.length === 0
      ? (hostOptions ?? sessionOptions)
      : sessionOptions;
  const family = sdkEffortFamilyForHarness(session.harness, families);
  const reportedModel = state.llmModel ?? session.llmModel ?? null;
  const model =
    draft?.settings?.modelOverride ??
    selectedModelForPicker(kind, options, session.modelOverride ?? null, reportedModel) ??
    reportedModel;
  const fusionModel =
    kind === "devin" && isFusionModelUid(model) ? fusionOption(options) : undefined;
  const selectedModel = fusionModel ?? findModelLabelOption(options, model);
  const nativeAgent =
    nativeCodingAgentForHarness(session.harness) ??
    (kind ? nativeCodingAgentForHarness(`${kind}-native`) : undefined);
  const harnessLabel = nativeAgent?.displayName ?? session.agentName ?? "Session";
  const modelLabel = formatStatusModelLabel(model, options) ?? defaultModelLabel(options);
  const effort =
    draft?.settings?.reasoningEffort !== undefined
      ? draft.settings.reasoningEffort
      : (session.reasoningEffort ?? null);
  const supportsEffort = shouldShowEffortPicker(capabilities, family);
  const efforts = supportsEffort ? effortLevelsForConv(capabilities, options, model, family) : [];
  const showModels = kind !== null && options.length > 0;
  if (
    !settingsSupported ||
    (nativeSession && (!nativeSide || draft)) ||
    (!showModels && efforts.length === 0)
  )
    return null;
  const disabled =
    saving ||
    draft?.disabled ||
    !isEditorLevel(session.permissionLevel) ||
    session.labels?.["omnigent.closed"] === "true";

  function save(updates: SideChatModelSettings) {
    if (disabled || queryClient.isMutating({ mutationKey }) > 0) return;
    if (draft) {
      draft.onChange(updates);
      return;
    }
    setError(null);
    mutation.mutate(updates);
  }

  const configContent = (
    <>
      {error && (
        <p role="alert" className="px-2 py-1 text-xs text-destructive whitespace-normal">
          {error}
        </p>
      )}
      <ComposerConfigSections
        models={
          showModels
            ? {
                testId: "side-chat-models",
                header: "Models",
                choices: options.map((option) => ({
                  key: option.id,
                  label: nativeModelLabel(option),
                  checked:
                    option.id === selectedModel?.id || (model === null && !!option.isDefault),
                  title: nativeModelTooltip(option),
                  className: "whitespace-normal break-words",
                  disabled,
                  onSelect: () => {
                    const nextModel = option.fusion
                      ? currentFusionCombo(option.fusion, model ?? "").modelUid
                      : option.id;
                    const nextEfforts = supportsEffort
                      ? effortLevelsForConv(capabilities, options, nextModel, family)
                      : [];
                    const nextEffort = effortAfterModelChange(effort, nextEfforts);
                    void save({
                      modelOverride: nextModel,
                      ...(supportsEffort && nextEffort !== effort
                        ? { reasoningEffort: nextEffort }
                        : {}),
                    });
                  },
                  data: { "data-model-id": option.id },
                })),
              }
            : undefined
        }
        efforts={
          efforts.length
            ? {
                testId: "side-chat-efforts",
                header: "Effort",
                choices: efforts.map((level) => ({
                  key: level,
                  label: formatStatusEffortLabel(level) ?? level,
                  checked: effort === level,
                  disabled,
                  onSelect: () => void save({ reasoningEffort: level }),
                  data: { "data-effort-level": level },
                })),
              }
            : undefined
        }
      />
    </>
  );
  return (
    <HarnessPicker
      open={menuOpen}
      onOpenChange={(open) => {
        if (!open || !disabled) setMenuOpen(open);
        if (!open) {
          setConfigOpen(false);
          setError(null);
        }
      }}
      trigger={{
        label: "Side chat model and effort",
        model: compactModelTriggerLabel(modelLabel),
        effort: formatStatusEffortLabel(effort) ?? undefined,
        icon: (
          <ComposerAgentIcon
            agent={{
              name: nativeAgent?.agentName ?? session.agentName ?? "",
              harness: nativeAgent?.harness ?? session.harness ?? null,
            }}
          />
        ),
        disabled,
        testIdPrefix: "side-chat",
        "data-testid": "side-chat-settings",
      }}
      testId="side-chat-model-menu"
    >
      {isMobile && configOpen ? (
        <HarnessPickerConfigPage
          backTestId="side-chat-model-back"
          testId="side-chat-model-config"
          onBack={() => setConfigOpen(false)}
        >
          {configContent}
        </HarnessPickerConfigPage>
      ) : (
        <HarnessPickerConfigRow
          label={harnessLabel}
          value={modelLabel}
          open={configOpen}
          onOpenChange={setConfigOpen}
          isMobile={isMobile}
          disabled={disabled}
          testId="side-chat-model-edit"
          configTestId="side-chat-model-config"
        >
          {configContent}
        </HarnessPickerConfigRow>
      )}
    </HarnessPicker>
  );
}
