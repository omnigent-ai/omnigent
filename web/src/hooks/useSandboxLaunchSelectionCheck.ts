import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { isFusionModelUid } from "@/lib/devinFusion";
import type { NativeModelOption, SandboxStatus } from "@/lib/types";
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

/** After a watched sandbox launch, reset a model or effort its catalog lacks. */
export function useSandboxLaunchSelectionCheck(
  sessionId: string | null,
  sandboxStatus: SandboxStatus | null,
  harness: string | null,
  models: readonly NativeModelOption[],
) {
  const conversationId = useChatStore((state) => state.conversationId);
  const model = useChatStore((state) => state.sessionModelOverride);
  const effort = useChatStore((state) => state.sessionReasoningEffort);
  const launchSeen = useRef<string | null>(null);
  const checked = useRef<string | null>(null);
  useEffect(() => {
    if (sessionId === null || conversationId !== sessionId) return;
    if (sandboxStatus !== null && sandboxStatus.stage !== "failed") launchSeen.current = sessionId;
    if (launchSeen.current !== sessionId || checked.current === sessionId) return;
    if (harness === null || models.length === 0) return;
    checked.current = sessionId;
    const resets = launchSelectionResets(harness, models, model, effort);
    const notice = launchSelectionNotice(resets, model, effort);
    if (notice === null) return;
    const stillActive = () => useChatStore.getState().conversationId === sessionId;
    // Store setters act on the active session, so a switch away defers the rest until return.
    const deferUntilReturn = () => {
      if (checked.current === sessionId) checked.current = null;
    };
    void (async () => {
      try {
        if (resets.model) await useChatStore.getState().setModel(null);
        if (resets.effort) {
          if (!stillActive()) return deferUntilReturn();
          await useChatStore.getState().setEffort(null);
        }
        if (stillActive()) toast.info(notice);
      } catch {
        if (stillActive()) toast.error("Couldn't switch to the sandbox's default model settings.");
      }
    })();
  }, [sessionId, conversationId, sandboxStatus, harness, models, model, effort]);
}
