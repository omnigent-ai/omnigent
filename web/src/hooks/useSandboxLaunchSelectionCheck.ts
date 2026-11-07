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
  const efforts = nativeEffortOptions(harness, models, resetModel ? null : model);
  const resetEffort =
    !!effort && efforts !== null && !efforts.some((option) => option.value === effort);
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
    void (async () => {
      try {
        if (resets.model) await useChatStore.getState().setModel(null);
        if (resets.effort && stillActive()) await useChatStore.getState().setEffort(null);
        if (stillActive()) toast.info(notice);
      } catch {
        // The store restores the server's setting; the picker still shows it.
      }
    })();
  }, [sessionId, conversationId, sandboxStatus, harness, models, model, effort]);
}
