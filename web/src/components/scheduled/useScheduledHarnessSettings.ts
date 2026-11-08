import { useEffect } from "react";
import {
  modelCatalogCacheKey,
  readModelCatalogCache,
  writeModelCatalogCache,
} from "@/lib/modelCatalogCache";
import { useHostModelOptions, type Host } from "@/hooks/useHosts";
import { useSandboxModelOptions, previewIsUnsupported } from "@/hooks/useSandboxModelOptions";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCurrentUserId } from "@/lib/identity";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import {
  nativeAgentHasModelSettings,
  nativeCodingAgentForAvailableAgent,
} from "@/lib/nativeCodingAgents";
import {
  nativePermissionOptions,
  UNATTENDED_DISALLOWED_PERMISSION_MODES,
} from "@/lib/nativeHarnessModes";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { harnessUnconfiguredOnHost } from "@/lib/harnessSetup";
import type { NativeModelOption } from "@/lib/types";

const EMPTY_MODELS: NativeModelOption[] = [];

/** Resolve choices for the automation's execution target without launching it. */
export function useScheduledHarnessSettings(
  agent: AvailableAgent | undefined,
  host: Host | undefined,
  sandbox: boolean,
  model: string,
  enabled: boolean,
  sharedAgent = false,
) {
  const info = useServerInfo();
  const harness = nativeCodingAgentForAvailableAgent(agent)?.harness ?? agent?.harness ?? null;
  const nativeModelSettings = nativeAgentHasModelSettings(agent);
  const hostModels = useHostModelOptions(
    sandbox ? null : (host?.host_id ?? null),
    harness ?? "",
    enabled &&
      nativeModelSettings &&
      !sandbox &&
      host?.status === "online" &&
      !harnessUnconfiguredOnHost(harness, host),
  );
  const provider = info === "loading" ? null : info.sandbox_provider;
  const capabilities =
    info !== "loading" && provider ? info.sandbox_provider_capabilities?.[provider] : undefined;
  const sandboxCatalogOffered = !!(capabilities?.default_models || capabilities?.inference_models);
  const sandboxModels = useSandboxModelOptions(
    provider,
    harness,
    sharedAgent ? null : (agent?.id ?? null),
    getCurrentUserId(),
    enabled && sandbox && sandboxCatalogOffered,
  );
  const cacheKey = modelCatalogCacheKey(
    sandbox ? "sandbox" : "host",
    sandbox ? provider : (host?.host_id ?? null),
    harness,
    agent?.id ?? null,
  );
  const cached = sandbox && !sandboxCatalogOffered ? null : readModelCatalogCache(cacheKey);
  const sandboxData = sandboxModels.data;
  const sandboxAuthoritative =
    sandboxCatalogOffered &&
    !sandboxModels.isError &&
    sandboxData !== undefined &&
    sandboxData.status !== "unavailable";
  let liveCatalog = hostModels.data;
  if (sandbox) {
    liveCatalog = undefined;
    if (sandboxAuthoritative)
      liveCatalog = sandboxData.status === "ready" ? sandboxData.models : EMPTY_MODELS;
  }
  const configured =
    sandbox && (sandboxAuthoritative ? sandboxData.configured : cached?.configured) === true;
  const catalogUpdatedAt = sandbox ? sandboxModels.dataUpdatedAt : hostModels.dataUpdatedAt;
  useEffect(() => {
    const failed = sandbox ? sandboxModels.isError : hostModels.isError;
    if (enabled && !failed && liveCatalog !== undefined) {
      writeModelCatalogCache(cacheKey, liveCatalog, configured, catalogUpdatedAt);
    }
  }, [
    cacheKey,
    enabled,
    liveCatalog,
    configured,
    catalogUpdatedAt,
    sandbox,
    sandboxModels.isError,
    hostModels.isError,
  ]);
  const sandboxDefaultModels =
    sandbox &&
    capabilities?.default_models === true &&
    capabilities.inference_models !== true &&
    !(sandboxAuthoritative && sandboxData.status === "unconfigured");
  const supportsModel = nativeModelSettings || sandboxDefaultModels || configured;
  const catalog: readonly NativeModelOption[] | undefined = liveCatalog ?? cached?.models;
  const unsupported = sandbox && sandboxCatalogOffered && previewIsUnsupported(sandboxModels);
  const blockedReason = unsupported
    ? sandboxData?.error ||
      `No models are available for ${agent?.display_name ?? "this harness"} in this sandbox. Choose another harness.`
    : null;
  // Claude aliases cover older servers. Fusion rows require a separate editor;
  // saved concrete model IDs remain available in the field even without a catalog.
  const models: readonly NativeModelOption[] =
    catalog?.filter((option) => !option.fusion) ??
    (harness === "claude-native" && !(sandbox && sandboxCatalogOffered)
      ? CLAUDE_NATIVE_MODELS.map((option) => ({ id: option.id, displayName: option.label }))
      : []);
  // Like New Chat, provider default catalogs own effort choices even for Claude/Pi.
  const effortHarness = sandboxDefaultModels ? "codex-native" : harness;
  const effortsForModel = (picked: string) =>
    nativeEffortOptions(effortHarness, models, picked) ?? [];
  const efforts = effortsForModel(model);
  const supportsHarnessPermissions =
    harness === "claude-native" ||
    (info !== "loading" && info.scheduled_task_harness_settings === true);
  const permissions = supportsHarnessPermissions
    ? nativePermissionOptions(harness).filter(
        (option) => !UNATTENDED_DISALLOWED_PERMISSION_MODES.has(option.value),
      )
    : [];
  const loading = sandbox ? sandboxModels.isLoading : hostModels.isLoading;
  const error = sandbox
    ? (sandboxModels.error ??
      (sandboxData?.status === "unavailable" ? sandboxData.error || "Models unavailable" : null))
    : hostModels.error;
  let catalogMessage: string | null = null;
  if (supportsModel && !blockedReason && models.length === 0) {
    if (!sandbox && host?.status === "offline")
      catalogMessage = "Connect your host to load models.";
    else if (loading) catalogMessage = "Loading models…";
    else if (error)
      catalogMessage = "Could not load models. Default uses the agent's configuration.";
    else catalogMessage = "No model choices are available. Default uses the agent's configuration.";
  }
  return {
    loading,
    harness,
    supportsModel,
    models,
    efforts,
    effortsForModel,
    permissions,
    catalogMessage,
    blockedReason,
  };
}
