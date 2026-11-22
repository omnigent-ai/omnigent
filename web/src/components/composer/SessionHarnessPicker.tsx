import {
  HarnessPicker,
  HarnessPickerConfigRow,
  HarnessPickerConfigPage,
} from "@/components/composer/HarnessPicker";
import { useCallback, useEffect, useRef, useState } from "react";
import { WandSparklesIcon } from "lucide-react";
import { CompactShortcutKeys } from "@/components/KeyboardShortcut";
import { ComposerConfigTooltipRows } from "@/components/composer/ComposerControls";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import type { NativeModelOption } from "@/lib/types";
import { useSessionModelLabel } from "@/hooks/useSessionModelLabel";
import { MODEL_PICKER_KEYS } from "@/hooks/useModelPickerHotkey";
import { codexEffortLevelsForModel, findNativeModelOption } from "@/lib/codexNativeModels";
import { modelConfigurationSourceRows } from "@/lib/modelConfigurationSource";
import { useChatStore } from "@/store/chatStore";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { nativeModelLabel } from "@/components/HarnessConfigControls";
import {
  ComposerConfigSections,
  type ComposerConfigSection,
} from "@/components/composer/ComposerConfigSections";
import {
  ComposerPaletteSettings,
  MODEL_BUSY_REASON,
} from "@/components/composer/composerSectionSettings";
import { useComposerConfigCycleHotkeys } from "@/hooks/useComposerConfigCycleHotkeys";
import { buildFusionSections } from "@/components/composer/fusionSections";
import { fusionOption, isFusionModelUid } from "@/lib/devinFusion";
import {
  compactModelTriggerLabel,
  formatStatusEffortLabel,
  nativeModelTooltip,
} from "@/lib/composerModelLabel";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import {
  effortAfterModelChange,
  selectedModelForPicker,
  type NativeModelPickerKind,
} from "@/lib/composerCapabilities";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useSessionField } from "@/components/composer/sessionPickerInputs";

/** The conversation a picker targets: *sessionId*, else whichever is active now. */
function useTargetConversationId(sessionId: string | undefined): string | null {
  const active = useChatStore((state) => state.conversationId);
  return sessionId ?? active;
}

/**
 * Whether the session surfaces any run-config the gear modal can edit. Shared
 * render guard for the config gear and click-to-open gate for the model/effort
 * label half of the composer's split pill, so both halves stay in lockstep.
 */
function hasSessionConfig({
  showModels,
  showEffort,
}: {
  showModels: boolean;
  showEffort: boolean;
}): boolean {
  return showModels || showEffort;
}

export function SessionHarnessPicker({
  sessionId,
  appearance = "composer",
  busy,
  busyRef,
  setBusy,
  agentName,
  harnessLabel,
  showModels,
  showEffort,
  effortLevels,
  modelPickerKind,
  supportsModelReset,
  codexModelOptions,
  inferenceConfigured,
  inferenceError,
  modelsFromLaunchPreview,
  modelLabelOptions,
  modelLabelHostId,
  costRoutingEligible,
  modelPickWakesSession,
  disabled,
  openNonce = 0,
  modelPickerShortcutEnabled = false,
  paletteSettingsSource,
}: {
  appearance?: "composer" | "header";
  busy: boolean;
  busyRef: { current: boolean };
  setBusy: (busy: boolean) => void;
  agentName: string | null;
  harnessLabel: string | null;
  showModels: boolean;
  showEffort: boolean;
  effortLevels: readonly string[];
  modelPickerKind: NativeModelPickerKind | null;
  supportsModelReset: boolean;
  codexModelOptions: readonly NativeModelOption[];
  inferenceConfigured?: boolean;
  inferenceError?: string | null;
  modelsFromLaunchPreview?: boolean;
  modelLabelOptions: readonly NativeModelOption[];
  modelLabelHostId: string | null | undefined;
  costRoutingEligible: boolean;
  /** A model pick must first wake this native session's runner, which can take a while. */
  modelPickWakesSession: boolean;
  disabled: boolean;
  openNonce?: number;
  /** Whether the Ctrl+Shift+M hotkey opens this picker (mirrors its enabled state). */
  modelPickerShortcutEnabled?: boolean;
  /** Read and change this session instead of the active conversation. */
  sessionId?: string;
  /** Offer Model and Reasoning Effort in ⌘K under this; unset while quick_pick_palette is off. */
  paletteSettingsSource?: string;
}) {
  const isMobile = useIsMobileViewport();
  const [menuOpen, setMenuOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [startingSession, setStartingSession] = useState(false);
  const [cycleDraft, setCycleDraft] = useState<{
    sessionId: string | null;
    model?: { key: string; id: string | null };
    effort?: string;
  } | null>(null);
  const cycleDraftRef = useRef(cycleDraft);
  const cancelCycle = useCallback(() => {
    cycleDraftRef.current = null;
    setCycleDraft(null);
  }, []);
  useEffect(
    () => () => {
      cycleDraftRef.current = null;
    },
    [],
  );
  const appliedOpenNonce = useRef(0);
  const conversationId = useTargetConversationId(sessionId);
  // The live target: a fixed session, or the conversation active at the time of the call.
  const currentConversationId = () => sessionId ?? useChatStore.getState().conversationId;
  // Store calls for the active conversation keep their exact arguments; a fixed session adds its id.
  const target: [] | [string] = sessionId === undefined ? [] : [sessionId];
  const sessionHarness = useSessionField(sessionId, (state) => state.sessionHarness);
  const subAgentName = useSessionField(sessionId, (state) => state.subAgentName);
  const pendingModelChange = useSessionField(sessionId, (state) => state.pendingModelChange);
  const sessionModelSeeded = useSessionField(sessionId, (state) => state.sessionModelSeeded);
  const selectedEffort = useSessionEffort(sessionId);
  const costControlModeOverride = useSessionField(
    sessionId,
    (state) => state.costControlModeOverride,
  );
  const routingOn = costRoutingEligible && costControlModeOverride === "on";
  const {
    effectiveModel,
    modelLabel,
    modelLabelLoading,
    modelLabelUnavailable,
    modelOptions,
    pickerSelectedModel,
  } = useResolvedComposerModel(
    sessionId,
    modelPickerKind,
    codexModelOptions,
    modelLabelOptions,
    modelLabelHostId,
  );
  const modelSummary = modelLabelLoading
    ? "Loading model…"
    : modelLabelUnavailable
      ? "Model name unavailable"
      : modelLabel;
  const nativeAgent =
    nativeCodingAgentForHarness(sessionHarness) ??
    (modelPickerKind ? nativeCodingAgentForHarness(modelPickerKind + "-native") : undefined);
  const iconAgent = {
    name: nativeAgent?.agentName ?? agentName ?? subAgentName ?? "",
    harness: nativeAgent?.harness ?? sessionHarness,
  };
  const summary = useSessionConfigSummary({
    sessionId,
    harnessLabel,
    showModels,
    showEffort,
    codexModelOptions,
    effectiveModel,
    modelLabel: modelSummary,
    costRoutingEligible,
  });
  const configurable = hasSessionConfig({
    showModels,
    showEffort,
  });
  const effortLabel = showEffort && !routingOn ? formatStatusEffortLabel(selectedEffort) : null;
  const label = routingOn
    ? SMART_ROUTING_LABEL
    : modelLabelLoading
      ? ""
      : compactModelTriggerLabel(
          modelSummary ?? nativeAgent?.displayName ?? harnessLabel ?? "Session",
        );
  const availableEfforts =
    modelPickerKind === "codex"
      ? codexEffortLevelsForModel(codexModelOptions, pickerSelectedModel)
      : effortLevels;
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    appliedOpenNonce.current = openNonce;
    if (!disabled && configurable) {
      cancelCycle();
      setMenuOpen(true);
      setConfigOpen(true);
    }
  }, [openNonce, disabled, configurable, cancelCycle]);
  useEffect(() => {
    cancelCycle();
    setMenuOpen(false);
    setConfigOpen(false);
  }, [conversationId, cancelCycle]);
  const apply = async (change: () => Promise<unknown>, wakesSession = false) => {
    if (disabled || busyRef.current || pendingModelChange !== null) return;
    busyRef.current = true;
    setBusy(true);
    setStartingSession(wakesSession);
    try {
      await change();
    } catch {
      // The store rolls back a refused change, so the pill shows what applied.
    } finally {
      busyRef.current = false;
      setBusy(false);
      setStartingSession(false);
    }
  };
  const changeModel = async (modelId: string | null, nextEffort?: string) => {
    const store = useChatStore.getState();
    const sourceSessionId = currentConversationId();
    await store.setModel(modelId, {
      expectConfirmation:
        (modelPickerKind === "claude" || modelPickerKind === "codex") && !modelsFromLaunchPreview,
      ...(sessionId === undefined ? {} : { sessionId }),
    });
    if (currentConversationId() !== sourceSessionId) return;
    if (
      selectedEffort !== null &&
      (isFusionModelUid(modelId) ||
        (nextEffort === undefined &&
          modelPickerKind === "codex" &&
          effortAfterModelChange(
            selectedEffort,
            codexEffortLevelsForModel(codexModelOptions, modelId),
          ) === null))
    )
      await store.setEffort(null, ...target);
    if (costRoutingEligible && routingOn && currentConversationId() === sourceSessionId)
      await store.setCostControlMode("off", ...target);
  };
  const selectModel = (modelId: string | null) => {
    cancelCycle();
    void apply(() => changeModel(modelId), modelPickWakesSession);
  };
  const selectEffort = (effort: string) => {
    cancelCycle();
    void apply(() => useChatStore.getState().setEffort(effort, ...target));
  };
  // Devin Fusion: the composed `fusion-…` id is the model; the lead effort is
  // baked in, so it carries no separate reasoning effort.
  const composerFusionOption = fusionOption(modelOptions);
  const composerFusion = composerFusionOption?.fusion;
  const fusionSelected = composerFusion !== undefined && isFusionModelUid(pickerSelectedModel);
  const modelValueForKey = (key: string): string | null => {
    const option = modelOptions.find((model) => model.id === key);
    if (composerFusion !== undefined && key === composerFusionOption?.id)
      return composerFusion.default;
    return key === "__default__" || (supportsModelReset && option?.isDefault) ? null : key;
  };
  const modelSection: ComposerConfigSection | undefined = showModels
    ? {
        testId: "composer-agent-models",
        header: "Models",
        leading:
          (inferenceConfigured && inferenceError) || modelOptions.length === 0 ? (
            <div className="px-2 py-1 text-xs text-muted-foreground" role="status">
              {inferenceError ?? "No usable models are available for this session."}
            </div>
          ) : undefined,
        choices: [
          ...(supportsModelReset &&
          !inferenceConfigured &&
          !modelOptions.some((model) => model.isDefault)
            ? [
                {
                  key: "__default__",
                  label: "Default",
                  checked: !routingOn && pickerSelectedModel === null,
                  disabled: busy || pendingModelChange !== null || !!inferenceError,
                  onSelect: () => selectModel(null),
                  testId: "composer-agent-model-default",
                },
              ]
            : []),
          ...modelOptions.map((model) => ({
            key: model.id,
            label: nativeModelLabel(model),
            checked:
              !routingOn &&
              (composerFusion !== undefined && model.id === composerFusionOption?.id
                ? isFusionModelUid(pickerSelectedModel)
                : model.id === pickerSelectedModel),
            disabled: busy || pendingModelChange !== null || !!inferenceError,
            onSelect: () => selectModel(modelValueForKey(model.id)),
            testId: `composer-agent-model-${model.id}`,
            title: nativeModelTooltip(model),
            className: "whitespace-normal break-words",
            data: { "data-model-id": model.id },
            modelOption: model,
          })),
          ...(pickerSelectedModel &&
          !isFusionModelUid(pickerSelectedModel) &&
          !modelOptions.some((model) => model.id === pickerSelectedModel)
            ? [
                {
                  key: "__current__",
                  label: `${modelSummary ?? "Default"} (current)`,
                  checked: !routingOn,
                  disabled: true,
                  className: "whitespace-normal break-words",
                  data: { "data-model-id": pickerSelectedModel },
                },
              ]
            : []),
        ],
      }
    : undefined;
  const buildEffortSection = (
    levels: readonly string[],
    current: string | null,
    routed: boolean,
    onSelect: (effort: string) => void,
  ): ComposerConfigSection => ({
    testId: "composer-agent-efforts",
    header: modelPickerKind === "pi" ? "Thinking level" : "Effort",
    choices: levels.map((effort) => ({
      key: effort,
      label: formatStatusEffortLabel(effort) ?? effort,
      checked: !routed && effort === current,
      disabled: routed || busy || pendingModelChange !== null,
      onSelect: () => onSelect(effort),
      testId: `composer-agent-effort-${effort}`,
      data: { "data-effort-level": effort },
    })),
  });
  const effortSection =
    showEffort && availableEfforts.length > 0
      ? buildEffortSection(availableEfforts, selectedEffort, routingOn, selectEffort)
      : undefined;
  const fusionSections: readonly ComposerConfigSection[] | undefined =
    composerFusion !== undefined && fusionSelected && !routingOn
      ? buildFusionSections({
          descriptor: composerFusion,
          modelUid: pickerSelectedModel ?? composerFusion.default,
          testIdPrefix: "composer-agent",
          onChange: selectModel,
          disabled: busy || pendingModelChange !== null,
        })
      : undefined;
  const staged = cycleDraft?.sessionId === conversationId ? cycleDraft : null;
  const appliedModelKey = modelSection?.choices.find((choice) => choice.checked)?.key;
  const appliedModelId =
    composerFusion !== undefined && pickerSelectedModel === "fusion"
      ? composerFusion.default
      : pickerSelectedModel;
  const cycleModel = staged?.model ? staged.model.id : pickerSelectedModel;
  const cycleRoutingOn = routingOn && staged?.model === undefined;
  const cycleEffortLevels =
    modelPickerKind === "codex" || modelPickerKind === "devin"
      ? codexEffortLevelsForModel(codexModelOptions, cycleModel)
      : availableEfforts;
  const stageModel = (
    key: string,
    id = key === appliedModelKey ? appliedModelId : modelValueForKey(key),
  ) => {
    const previous = cycleDraftRef.current;
    const levels =
      modelPickerKind === "codex" || modelPickerKind === "devin"
        ? codexEffortLevelsForModel(codexModelOptions, id)
        : availableEfforts;
    const effort = previous?.sessionId === conversationId ? previous.effort : undefined;
    const next = {
      sessionId: conversationId,
      model: { key, id },
      effort: effort !== undefined && levels.includes(effort) ? effort : undefined,
    };
    cycleDraftRef.current = next;
    setCycleDraft(next);
  };
  const stageEffort = (effort: string) => {
    const previous = cycleDraftRef.current;
    const next = {
      ...(previous?.sessionId === conversationId ? previous : {}),
      sessionId: conversationId,
      effort,
    };
    cycleDraftRef.current = next;
    setCycleDraft(next);
  };
  const cycleModelSection = modelSection && {
    ...modelSection,
    choices: modelSection.choices.map((choice) => ({
      ...choice,
      checked: staged?.model ? choice.key === staged.model.key : choice.checked,
      onSelect: choice.onSelect ? () => stageModel(choice.key) : undefined,
    })),
  };
  const cycleEffortSection: ComposerConfigSection | undefined =
    composerFusion !== undefined && isFusionModelUid(cycleModel) && !cycleRoutingOn
      ? buildFusionSections({
          descriptor: composerFusion,
          modelUid: cycleModel ?? composerFusion.default,
          testIdPrefix: "composer-agent",
          onChange: (modelUid) => stageModel(composerFusionOption!.id, modelUid),
          disabled: busy || pendingModelChange !== null,
        })[1]
      : (showEffort || modelPickerKind === "codex" || modelPickerKind === "devin") &&
          cycleEffortLevels.length > 0
        ? buildEffortSection(
            cycleEffortLevels,
            staged?.effort ?? selectedEffort,
            cycleRoutingOn,
            stageEffort,
          )
        : undefined;
  const applyCycle = () => {
    const draft = cycleDraftRef.current;
    if (!draft || disabled || draft.sessionId !== currentConversationId()) {
      dismissPreview();
      return;
    }
    if (busyRef.current || pendingModelChange !== null) return;
    const modelChanged =
      draft.model !== undefined &&
      (draft.model.key !== appliedModelKey || draft.model.id !== appliedModelId);
    const effort =
      draft.effort !== undefined && cycleEffortLevels.includes(draft.effort)
        ? draft.effort
        : undefined;
    const effortChanged = effort !== undefined && effort !== selectedEffort;
    const modelChoice = modelSection?.choices.find((choice) => choice.key === draft.model?.key);
    if (
      (modelChanged && (!modelChoice?.onSelect || modelChoice.disabled)) ||
      (effortChanged && cycleRoutingOn)
    ) {
      dismissPreview();
      return;
    }
    if (!modelChanged && !effortChanged) {
      dismissPreview();
      return;
    }
    cancelCycle();
    void apply(async () => {
      if (modelChanged) await changeModel(draft.model!.id, effort);
      if (draft.sessionId !== currentConversationId()) return;
      if (effortChanged) await useChatStore.getState().setEffort(effort, ...target);
    }, modelChanged && modelPickWakesSession);
  };
  const { preview, dismissPreview } = useComposerConfigCycleHotkeys(
    cycleModelSection?.choices,
    cycleEffortSection?.choices.filter(({ key }) => key !== "__fast__"),
    // The window-wide hotkeys belong to the main composer's picker.
    sessionId === undefined && !disabled && !busy && pendingModelChange === null,
    busy || pendingModelChange !== null,
    staged ? { apply: applyCycle, cancel: cancelCycle } : undefined,
  );
  useEffect(() => dismissPreview(), [conversationId, disabled, dismissPreview]);
  const configContent = (
    <>
      {startingSession && (
        <div
          className="px-2 py-1 text-xs whitespace-normal text-muted-foreground"
          role="status"
          data-testid="composer-agent-starting-session"
        >
          Starting the session to switch models…
        </div>
      )}
      {costRoutingEligible && showModels && (
        <>
          <DropdownMenuItem
            disabled={busy || pendingModelChange !== null}
            onSelect={() => {
              cancelCycle();
              void apply(() =>
                useChatStore.getState().setCostControlMode(routingOn ? "off" : "on", ...target),
              );
            }}
            data-active={routingOn ? "true" : undefined}
            className="items-center text-13 data-[active=true]:bg-muted data-[active=true]:text-foreground dark:data-[active=true]:bg-muted/50"
          >
            <WandSparklesIcon className="size-4" />
            {SMART_ROUTING_LABEL}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>
      )}
      <ComposerConfigSections
        models={modelSection}
        efforts={effortSection}
        extra={fusionSections}
      />
    </>
  );
  const picker = (
    <HarnessPicker
      contentSide={appearance === "header" ? "bottom" : "top"}
      cyclePreview={
        preview === "models"
          ? cycleModelSection
          : preview === "efforts"
            ? cycleEffortSection
            : undefined
      }
      cyclePreviewHint={
        staged ? "Applies after 2 seconds · Enter to apply · Esc to cancel" : undefined
      }
      onDismissCyclePreview={dismissPreview}
      open={menuOpen}
      onOpenChange={(next) => {
        if (!next || (!disabled && !busy && configurable)) setMenuOpen(next);
        if (!next) setConfigOpen(false);
      }}
      trigger={{
        appearance,
        label: "Configure session",
        model: label,
        effort: effortLabel ?? undefined,
        icon: (
          <ComposerAgentIcon
            agent={iconAgent}
            className={appearance === "header" ? "size-5" : undefined}
          />
        ),
        disabled: busy || !configurable,
        "aria-disabled": disabled || busy || !configurable,
        className: disabled ? "cursor-default opacity-50" : undefined,
        testIdPrefix: "composer",
        "data-testid": "composer-config-gear",
        loading: modelLabelLoading && !routingOn,
        pending:
          (sessionModelSeeded || pendingModelChange !== null) &&
          (modelPickerKind === "claude" || modelPickerKind === "codex"),
      }}
      tooltip={
        <>
          <ComposerConfigTooltipRows rows={summary} />
          {modelPickerShortcutEnabled && (
            <span className="flex items-center gap-2 pt-1 text-muted-foreground">
              Open model picker
              <CompactShortcutKeys keys={MODEL_PICKER_KEYS} />
            </span>
          )}
        </>
      }
      tooltipTestId="composer-config-gear-tooltip"
      testId="composer-agent-menu"
    >
      {isMobile && configOpen ? (
        <HarnessPickerConfigPage
          backTestId="composer-agent-config-back"
          testId="composer-agent-config-menu"
          onBack={() => setConfigOpen(false)}
        >
          {configContent}
        </HarnessPickerConfigPage>
      ) : (
        <HarnessPickerConfigRow
          label={nativeAgent?.displayName ?? harnessLabel ?? "Session"}
          value={routingOn ? SMART_ROUTING_LABEL : (modelSummary ?? "Default")}
          open={configOpen}
          onOpenChange={setConfigOpen}
          isMobile={isMobile}
          disabled={busy || pendingModelChange !== null}
          valueTestId="composer-agent-model-summary"
          testId="composer-agent-edit"
          configTestId="composer-agent-config-menu"
        >
          {configContent}
        </HarnessPickerConfigRow>
      )}
    </HarnessPicker>
  );
  const routing = costRoutingEligible &&
    showModels && {
      active: routingOn,
      unavailableReason: busy || pendingModelChange !== null ? MODEL_BUSY_REASON : undefined,
      select: () => {
        cancelCycle();
        void apply(() => useChatStore.getState().setCostControlMode("on", ...target));
      },
    };
  // The picker keeps its place as a source comes and goes, so it never remounts; without a
  // source the ⌘K settings never mount and their hooks never run.
  return (
    <>
      {paletteSettingsSource !== undefined && (
        <ComposerPaletteSettings
          source={paletteSettingsSource}
          models={disabled ? undefined : modelSection}
          efforts={disabled ? undefined : effortSection}
          effortRouted={routingOn}
          routing={routing || undefined}
        />
      )}
      {picker}
    </>
  );
}

/**
 * Label/value rows summarizing the session's live run-config, for the gear
 * icon's hover tooltip. Mirrors the new-session summary but with in-session
 * values. The Permissions row lives in the modal itself, not this summary.
 */
function useSessionConfigSummary({
  sessionId,
  harnessLabel,
  showModels,
  showEffort,
  codexModelOptions,
  effectiveModel,
  modelLabel,
  costRoutingEligible,
}: {
  sessionId: string | undefined;
  harnessLabel: string | null;
  showModels: boolean;
  showEffort: boolean;
  codexModelOptions: readonly NativeModelOption[];
  effectiveModel: string | null;
  modelLabel: string | null;
  costRoutingEligible: boolean;
}): { label: string; value: string }[] {
  const selectedEffort = useSessionEffort(sessionId);
  const costControlModeOverride = useSessionField(sessionId, (s) => s.costControlModeOverride);
  const routingOn = costRoutingEligible && costControlModeOverride === "on";

  const rows: { label: string; value: string }[] = [];
  if (harnessLabel) rows.push({ label: "Harness", value: harnessLabel });
  if (showModels) {
    rows.push({
      label: "Model",
      value: routingOn ? SMART_ROUTING_LABEL : (modelLabel ?? "Default"),
    });
  }
  // Suppress Effort while routing is on: the router picks the model and its
  // effort per turn, so a pinned effort doesn't apply and would mislead.
  if (showEffort && !routingOn) {
    const effortValue = formatStatusEffortLabel(selectedEffort);
    if (effortValue) rows.push({ label: "Effort", value: effortValue });
  }
  if (!routingOn) {
    const source =
      findNativeModelOption(codexModelOptions, effectiveModel)?.source ??
      codexModelOptions.find((option) => option.source)?.source;
    rows.push(...modelConfigurationSourceRows(source));
  }
  return rows;
}

/**
 * The effort this conversation is actually at.
 *
 * `sessionReasoningEffort` is conversation-scoped, so two live conversations at
 * different efforts each read their own.
 */
function useSessionEffort(sessionId: string | undefined): string | null {
  return useSessionField(sessionId, (s) => s.sessionReasoningEffort);
}

/**
 * Resolve the session's live model + its picker option list from the store,
 * per harness family. Shared by the AgentPicker trigger, the gear config
 * modal, and the gear hover summary so all three agree on what "the current
 * model" is (the resolution differs by wrapper — see the inline notes).
 *
 * @param modelPickerKind Native picker family, or ``null`` for SDK/bundle.
 * @param codexModelOptions Server-provided model options (codex/cursor/…).
 */
function useResolvedComposerModel(
  targetSessionId: string | undefined,
  modelPickerKind: NativeModelPickerKind | null,
  codexModelOptions: readonly NativeModelOption[],
  modelLabelOptions: readonly NativeModelOption[],
  hostId: string | null | undefined,
) {
  const sessionId = useTargetConversationId(targetSessionId);
  const agentId = useSessionField(targetSessionId, (s) => s.boundAgentId);
  const harness = useSessionField(targetSessionId, (s) => s.sessionHarness);
  const sessionModelOverride = useSessionField(targetSessionId, (s) => s.sessionModelOverride);
  const sessionModelSeeded = useSessionField(targetSessionId, (s) => s.sessionModelSeeded);
  const llmModel = useSessionField(targetSessionId, (s) => s.llmModel);
  const nativeVendorOwnsModel = useSessionField(targetSessionId, (s) => s.nativeVendorOwnsModel);

  // Native model pickers populate from the snapshot's runner-backed
  // ``model_options`` field. Claude's rows are the aliases pinned to the
  // launch-time Databricks catalog; Codex carries richer effort metadata.
  const usesServerModelOptions =
    modelPickerKind === "claude" ||
    modelPickerKind === "codex" ||
    modelPickerKind === "cursor" ||
    modelPickerKind === "kiro" ||
    modelPickerKind === "pi" ||
    modelPickerKind === "opencode" ||
    modelPickerKind === "devin" ||
    modelPickerKind === "acp" ||
    modelPickerKind === "configured";
  const modelOptions: readonly {
    id: string;
    model?: string;
    label?: string;
    displayName?: string;
    isDefault?: boolean;
  }[] = usesServerModelOptions ? codexModelOptions : [];
  const isNativeModelPicker = modelPickerKind !== null;

  // The harness's own report is the display authority for claude-/codex-
  // native sessions: `llmModel` carries the verbatim reported model (the
  // launch's own report, or an in-pane switch), and the chip, the gear
  // highlight, and the hover summary all resolve from it alone. The user's
  // request (`sessionModelOverride`) is input, never display state — rendering
  // a request as if it were truth is exactly how a record/pane divergence hides.
  const isReportedModelPicker = modelPickerKind === "claude" || modelPickerKind === "codex";
  const pickerSelectedModel = selectedModelForPicker(
    modelPickerKind,
    codexModelOptions,
    sessionModelOverride,
    llmModel,
  );
  const effectiveModel = sessionModelSeeded
    ? (sessionModelOverride ?? llmModel)
    : nativeVendorOwnsModel
      ? modelPickerKind === "cursor" || modelPickerKind === "kiro"
        ? sessionModelOverride
        : modelPickerKind === "opencode" || modelPickerKind === "pi" || modelPickerKind === "devin"
          ? (sessionModelOverride ?? llmModel)
          : null
      : isReportedModelPicker
        ? llmModel
        : (sessionModelOverride ?? llmModel);
  const {
    label: modelLabel,
    loading: modelLabelLoading,
    unavailable: modelLabelUnavailable,
  } = useSessionModelLabel(
    { sessionId, hostId: hostId ?? null, agentId, harness },
    effectiveModel,
    modelLabelOptions,
    usesServerModelOptions,
    hostId !== undefined &&
      !sessionModelSeeded &&
      (!isReportedModelPicker || effectiveModel === llmModel),
    codexModelOptions,
  );
  return {
    llmModel,
    usesServerModelOptions,
    modelOptions,
    isNativeModelPicker,
    pickerSelectedModel,
    effectiveModel,
    modelLabel,
    modelLabelLoading,
    modelLabelUnavailable,
  };
}
