import { useHarnessEffortFamilies } from "@/lib/agentLabels";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCurrentUserId } from "@/lib/identity";
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import {
  effortLevelsForConv,
  sdkEffortFamilyForHarness,
  shouldShowEffortPicker,
} from "@/lib/composerCapabilities";
import { codexEffortLevelsForModel } from "@/lib/codexNativeModels";
import { harnessUnavailableReasonOnHost } from "@/lib/harnessSetup";
import { useHostModelOptions, type Host } from "./useHosts";
import { useSandboxModelOptions } from "./useSandboxModelOptions";
import type { NativeModelOption } from "@/lib/types";

const EMPTY_MODELS: NativeModelOption[] = [];

/** The chosen launch target's catalog; never substitute a different harness. */
export function useLaunchModelOptions({
  host,
  sandbox,
  provider,
  harness,
  agentId,
  model,
  enabled,
}: {
  host?: Host;
  sandbox: boolean;
  provider: string | null;
  harness: string | null;
  agentId: string | null;
  model: string;
  enabled: boolean;
}) {
  const info = useServerInfo();
  const family = sdkEffortFamilyForHarness(harness, useHarnessEffortFamilies(enabled));
  const readiness = harnessUnavailableReasonOnHost(harness, host);
  const hostModels = useHostModelOptions(
    host?.host_id ?? null,
    harness ?? "",
    enabled &&
      !sandbox &&
      harness !== null &&
      host?.status === "online" &&
      (readiness === null || readiness === "cli-unresponsive"),
  );
  const capabilities =
    info !== "loading" && provider ? info.sandbox_provider_capabilities?.[provider] : undefined;
  const offersSandboxModels = !!(capabilities?.default_models || capabilities?.inference_models);
  const sandboxModels = useSandboxModelOptions(
    provider,
    harness,
    agentId,
    getCurrentUserId(),
    enabled && sandbox && offersSandboxModels,
  );
  const models = sandbox
    ? (sandboxModels.data?.models ?? EMPTY_MODELS)
    : (hostModels.data ?? EMPTY_MODELS);
  const catalogOwnsEffort =
    sandbox &&
    capabilities?.default_models === true &&
    capabilities.inference_models !== true &&
    sandboxModels.data?.configured;
  const effortsForModel = (selected: string) => {
    const currentModel = selected || models.find((option) => option.isDefault)?.id || null;
    const native = nativeEffortOptions(harness, models, currentModel);
    const levels = catalogOwnsEffort
      ? codexEffortLevelsForModel(models, currentModel)
      : native !== null
        ? native.map((option) => option.value)
        : shouldShowEffortPicker({ harness }, family)
          ? effortLevelsForConv({ harness }, models, currentModel, family)
          : [];
    return levels.map((value) => ({ value, label: normalizeEffortLabel(value) }));
  };
  return {
    models,
    efforts: effortsForModel(model),
    effortsForModel,
    loading: enabled && (sandbox ? sandboxModels.isLoading : hostModels.isLoading),
    error: sandbox ? sandboxModels.error : hostModels.error,
    unsupported: sandbox && !sandboxModels.isError && sandboxModels.data?.status === "unsupported",
  };
}
