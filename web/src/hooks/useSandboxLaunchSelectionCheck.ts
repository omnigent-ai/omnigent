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

// Sessions this tab created from New Chat, with the launched model's picker label.
// Waking or reopening a session never adds one, so in-session choices stay untouched.
const pendingLaunchChecks = new Map<string, string | null>();
let retiresOnLeave = false;

export function markSandboxLaunch(sessionId: string, modelLabel: string | null): void {
  pendingLaunchChecks.set(sessionId, modelLabel);
  if (retiresOnLeave) return;
  retiresOnLeave = true;
  // Leaving a launch before its catalog arrives retires the check, so a later reopen never resets.
  useChatStore.subscribe((state, previous) => {
    if (previous.conversationId !== null && previous.conversationId !== state.conversationId)
      pendingLaunchChecks.delete(previous.conversationId);
  });
}

/** Once a sandbox this tab launched reports its catalog, reset a model or effort it lacks. */
export function useSandboxLaunchSelectionCheck(
  sessionId: string | null,
  harness: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const conversationId = useChatStore((state) => state.conversationId);
  useEffect(() => {
    if (!enabled || sessionId === null || conversationId !== sessionId) return;
    if (harness === null || models.length === 0 || !pendingLaunchChecks.has(sessionId)) return;
    const modelLabel = pendingLaunchChecks.get(sessionId) ?? null;
    pendingLaunchChecks.delete(sessionId);
    const { sessionModelOverride: model, sessionReasoningEffort: effort } = useChatStore.getState();
    const resets = launchSelectionResets(harness, models, model, effort);
    if (!resets.model && !resets.effort) return;
    void (async () => {
      try {
        if (resets.model) await useChatStore.getState().setModel(null);
        // Skip the effort if the user switched away or picked a new effort meanwhile.
        const after = useChatStore.getState();
        const resetEffort =
          resets.effort &&
          after.conversationId === sessionId &&
          after.sessionReasoningEffort === effort;
        if (resetEffort) await after.setEffort(null);
        const notice = launchSelectionNotice(
          { model: resets.model, effort: resetEffort },
          model,
          effort,
          modelLabel,
        );
        if (notice !== null) toast.info(notice);
      } catch {
        toast.error("Couldn't switch to the sandbox's default model settings.");
      }
    })();
  }, [enabled, sessionId, conversationId, harness, models]);
}
