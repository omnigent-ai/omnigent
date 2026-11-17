import { useEffect } from "react";
import { toast } from "sonner";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { isFusionModelUid } from "@/lib/devinFusion";
import type { NativeModelOption } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

export interface LaunchSelectionResets {
  model: boolean;
  effort: boolean;
}

/** Which launch choices the session's live catalog does not offer. */
export function launchSelectionResets(
  harness: string | null,
  models: readonly NativeModelOption[],
  model: string | null,
  effort: string | null,
): LaunchSelectionResets {
  if (models.length === 0) return { model: false, effort: false };
  const resetModel =
    !!model && !isFusionModelUid(model) && findNativeModelOption(models, model) === null;
  const effortModel = resetModel ? null : model;
  // Codex-style ladders depend on the running model, which is unknown without a resolvable pick.
  const ladderDependsOnModel = harness === "codex-native" || harness === "devin-native";
  const ladderKnown = !ladderDependsOnModel || (!!effortModel && !isFusionModelUid(effortModel));
  const efforts = ladderKnown ? nativeEffortOptions(harness, models, effortModel) : null;
  const resetEffort =
    !!effort &&
    !!efforts &&
    efforts.length > 0 &&
    !efforts.some((option) => option.value === effort);
  return { model: resetModel, effort: resetEffort };
}

export function launchSelectionNotice(
  resets: LaunchSelectionResets,
  model: string | null,
  effort: string | null,
  modelLabel: string | null = null,
): string | null {
  if (resets.model) {
    return `${modelLabel ?? model} isn't available in this sandbox. Using the default model${resets.effort ? " and effort" : ""}.`;
  }
  if (resets.effort && effort) {
    return `${normalizeEffortLabel(effort)} effort isn't available for this model in this sandbox. Using the default effort.`;
  }
  return null;
}

export interface SandboxLaunch {
  model: string | null;
  effort: string | null;
  /** The launched model's picker label, for the notice. */
  modelLabel: string | null;
}

// Sessions this tab created from New Chat, with the choices they launched with.
const pendingLaunchChecks = new Map<string, SandboxLaunch>();

export function markSandboxLaunch(sessionId: string, launch: SandboxLaunch): void {
  pendingLaunchChecks.set(sessionId, launch);
}

/**
 * Once a sandbox this tab launched reports its catalog, reset a launch model or
 * effort it lacks. Only a choice still equal to the launch pick is reset, so a
 * choice the user made later is never touched, however late the check runs.
 */
export function useSandboxLaunchSelectionCheck(
  sessionId: string | null,
  harness: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const conversationId = useChatStore((state) => state.conversationId);
  useEffect(() => {
    if (!enabled || sessionId === null || conversationId !== sessionId) return;
    const launch = pendingLaunchChecks.get(sessionId);
    if (launch === undefined || harness === null || models.length === 0) return;
    pendingLaunchChecks.delete(sessionId);
    const { sessionModelOverride: model, sessionReasoningEffort: effort } = useChatStore.getState();
    const resets = launchSelectionResets(harness, models, model, effort);
    const resetModel = resets.model && model === launch.model;
    if (!resetModel && !(resets.effort && effort === launch.effort)) return;
    void (async () => {
      try {
        if (resetModel) await useChatStore.getState().setModel(null);
        // Skip the effort if the user switched away or picked a new effort meanwhile.
        const after = useChatStore.getState();
        const resetEffort =
          resets.effort &&
          after.conversationId === sessionId &&
          after.sessionReasoningEffort === launch.effort;
        if (resetEffort) await after.setEffort(null);
        const notice = launchSelectionNotice(
          { model: resetModel, effort: resetEffort },
          model,
          effort,
          launch.modelLabel,
        );
        if (notice !== null) toast.info(notice);
      } catch {
        toast.error("Couldn't switch to the sandbox's default model settings.");
      }
    })();
  }, [enabled, sessionId, conversationId, harness, models]);
}
