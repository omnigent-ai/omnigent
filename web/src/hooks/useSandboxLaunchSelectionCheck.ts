import { useEffect } from "react";
import { toast } from "sonner";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { isFusionModelUid } from "@/lib/devinFusion";
import { markSandboxLaunch, takeSandboxLaunch } from "@/lib/sandboxSessionProvenance";
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
): string | null {
  if (resets.model) {
    return `${model} isn't available in this sandbox. Using the default model${resets.effort ? " and effort" : ""}.`;
  }
  if (resets.effort && effort) {
    return `${normalizeEffortLabel(effort)} effort isn't available for this model in this sandbox. Using the default effort.`;
  }
  return null;
}

/** Once a sandbox this tab launched reports its catalog, reset a model or effort it lacks. */
export function useSandboxLaunchSelectionCheck(
  sessionId: string | null,
  harness: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const conversationId = useChatStore((state) => state.conversationId);
  const model = useChatStore((state) => state.sessionModelOverride);
  const effort = useChatStore((state) => state.sessionReasoningEffort);
  useEffect(() => {
    if (!enabled || sessionId === null || conversationId !== sessionId) return;
    if (harness === null || models.length === 0 || !takeSandboxLaunch(sessionId)) return;
    const resets = launchSelectionResets(harness, models, model, effort);
    if (launchSelectionNotice(resets, model, effort) === null) return;
    const stillActive = () => useChatStore.getState().conversationId === sessionId;
    void (async () => {
      try {
        if (resets.model) await useChatStore.getState().setModel(null);
        // Store setters act on the active session, so a switch away defers the rest until return.
        if (resets.effort && !stillActive()) return markSandboxLaunch(sessionId);
        // An effort picked while the model reset was pending wins.
        const clearEffort =
          resets.effort && useChatStore.getState().sessionReasoningEffort === effort;
        if (clearEffort) await useChatStore.getState().setEffort(null);
        const notice = launchSelectionNotice(
          { model: resets.model, effort: clearEffort },
          model,
          effort,
        );
        if (notice !== null && stillActive()) toast.info(notice);
      } catch {
        if (stillActive()) toast.error("Couldn't switch to the sandbox's default model settings.");
      }
    })();
  }, [enabled, sessionId, conversationId, harness, models, model, effort]);
}
