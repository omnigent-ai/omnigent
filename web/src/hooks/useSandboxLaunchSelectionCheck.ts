import { useEffect } from "react";
import { toast } from "sonner";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import { isFusionModelUid } from "@/lib/devinFusion";
import {
  deferLaunchNotice,
  hasLiveSessionCatalog,
  type LaunchNotice,
  markSandboxLaunch,
  takeLaunchNotices,
  takeSandboxLaunch,
} from "@/lib/sandboxSessionProvenance";
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

function showLaunchNotice(notice: LaunchNotice) {
  if (notice.kind === "error") toast.error(notice.text);
  else toast.info(notice.text);
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
    for (const notice of takeLaunchNotices(sessionId)) showLaunchNotice(notice);
    // A snapshot's persisted catalog may be stale, so only the runner's own report is checked.
    if (harness === null || models.length === 0 || !hasLiveSessionCatalog(sessionId)) return;
    const launch = takeSandboxLaunch(sessionId);
    if (launch === null) return;
    // The launch label names the current model only if the user hasn't switched since.
    const modelLabel = launch.model === model ? launch.modelLabel : null;
    const resets = launchSelectionResets(harness, models, model, effort);
    if (!resets.model && !resets.effort) return;
    const stillActive = () => useChatStore.getState().conversationId === sessionId;
    // A notice for a session the user left waits until they return to it.
    const notify = (notice: LaunchNotice) =>
      stillActive() ? showLaunchNotice(notice) : deferLaunchNotice(sessionId, notice);
    const modelNotice = launchSelectionNotice(
      { model: resets.model, effort: false },
      model,
      effort,
      modelLabel,
    );
    let modelWasReset = false;
    void (async () => {
      try {
        if (resets.model) {
          await useChatStore.getState().setModel(null);
          modelWasReset = true;
        }
        // Store setters act on the active session, so a switch away defers the rest until return.
        if (resets.effort && !stillActive()) {
          if (modelNotice !== null)
            deferLaunchNotice(sessionId, { kind: "info", text: modelNotice });
          markSandboxLaunch(sessionId, launch);
          return;
        }
        // An effort picked while the model reset was pending wins.
        const clearEffort =
          resets.effort && useChatStore.getState().sessionReasoningEffort === effort;
        if (clearEffort) await useChatStore.getState().setEffort(null);
        const notice = launchSelectionNotice(
          { model: resets.model, effort: clearEffort },
          model,
          effort,
          modelLabel,
        );
        if (notice !== null) notify({ kind: "info", text: notice });
      } catch {
        if (modelWasReset && modelNotice !== null) notify({ kind: "info", text: modelNotice });
        const text =
          resets.model && !modelWasReset
            ? "Couldn't switch to the sandbox's default model settings."
            : "Couldn't switch to the sandbox's default effort.";
        notify({ kind: "error", text });
      }
    })();
  }, [enabled, sessionId, conversationId, harness, models, model, effort]);
}
