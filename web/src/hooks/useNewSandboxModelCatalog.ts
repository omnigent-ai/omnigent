import { useMemo } from "react";
import { previewIsUnsupported, useSandboxModelOptions } from "@/hooks/useSandboxModelOptions";
import { useSavedSandboxSessionCatalog } from "@/hooks/useSandboxSessionCatalog";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import type { NativeModelOption } from "@/lib/types";

const CLAUDE_BUILT_IN_MODEL_OPTIONS: NativeModelOption[] = CLAUDE_NATIVE_MODELS.map((model) => ({
  id: model.id,
  displayName: model.id,
}));

/**
 * The model catalog a sandbox that does not exist yet offers one harness.
 *
 * A bound inference profile lists its configured models. A default catalog (an
 * unbound gateway or a provider hook) lists the provider's preview, or the
 * agent's last sandbox session catalog when one is saved.
 *
 * @param provider - Provider the sandbox launches on, e.g. "modal".
 * @param harness - Harness whose catalog to read, e.g. "codex-native".
 * @param agentId - Agent the session binds, which scopes the catalog.
 * @param user - Viewer the preview is cached for.
 * @param sandboxSelected - Whether the session targets a new sandbox; nothing is read otherwise.
 */
export function useNewSandboxModelCatalog(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
  user: string | null,
  sandboxSelected: boolean,
) {
  const info = useServerInfo();
  const capabilities =
    info !== "loading" && provider !== null
      ? info.sandbox_provider_capabilities?.[provider]
      : undefined;
  const defaultModelsOffered =
    sandboxSelected &&
    capabilities?.default_models === true &&
    capabilities.inference_models !== true;
  const previewsOffered =
    sandboxSelected &&
    provider !== null &&
    info !== "loading" &&
    (capabilities?.inference_models === true || defaultModelsOffered);
  const previewEnabled = previewsOffered && harness !== null;
  const preview = useSandboxModelOptions(provider, harness, agentId, user, previewEnabled);
  // A harness the default catalog cannot serve keeps its ordinary picker.
  const defaultModelsEnabled = defaultModelsOffered && preview.data?.status !== "unconfigured";
  // The agent's last sandbox session catalog is more accurate than the gateway preview.
  const savedCatalog = useSavedSandboxSessionCatalog(
    defaultModelsOffered ? provider : null,
    harness,
    agentId,
  );
  const sessionCatalog =
    defaultModelsEnabled && preview.data?.status !== "unsupported" ? savedCatalog : undefined;
  const inferenceConfigured = previewEnabled && preview.data?.configured === true;
  const pending = previewEnabled && preview.data === undefined && preview.isLoading;
  // Advisory catalogs never block launch; only a provider's certain "unsupported" does.
  const unsupported = previewEnabled && previewIsUnsupported(preview);
  const boundCatalogError =
    previewEnabled && !defaultModelsEnabled
      ? (preview.error?.message ??
        (preview.data?.configured && preview.data.status !== "ready"
          ? (preview.data.error ?? "No usable models are available for this harness.")
          : null))
      : null;
  const defaultCatalogError =
    defaultModelsEnabled && sessionCatalog === undefined
      ? (preview.error?.message ??
        (preview.data !== undefined && ["unavailable", "empty"].includes(preview.data.status)
          ? (preview.data.error ?? "Could not load AI Gateway models.")
          : null))
      : null;
  const catalog = useMemo(
    () =>
      defaultModelsEnabled
        ? (sessionCatalog ?? (defaultCatalogError ? [] : (preview.data?.models ?? [])))
        : inferenceConfigured
          ? preview.data!.models
          : undefined,
    [defaultModelsEnabled, sessionCatalog, defaultCatalogError, inferenceConfigured, preview.data],
  );
  return {
    /** The provider's preview, for its label, revision and status. */
    preview,
    previewsOffered,
    previewEnabled,
    defaultModelsEnabled,
    sessionCatalog,
    inferenceConfigured,
    pending,
    unsupported,
    /** Why a bound catalog lists nothing usable, or null. */
    boundCatalogError,
    /** Why a default catalog could not be listed, or null. */
    defaultCatalogError,
    /** Rows to offer; `undefined` when no sandbox catalog applies to the harness. */
    catalog,
  };
}

/**
 * Rows a new sandbox's model picker offers *harness*: the sandbox catalog, else
 * Claude Code's built-in models unless that catalog is loading or failed.
 *
 * @param harness - The picker's harness, e.g. "claude-native".
 * @param catalog - `catalog` from {@link useNewSandboxModelCatalog}.
 * @param unavailable - Whether the catalog is still loading or failed.
 */
export function newSandboxModelOptions(
  harness: string | null,
  catalog: NativeModelOption[] | undefined,
  unavailable: boolean,
): NativeModelOption[] {
  if (catalog !== undefined) return catalog;
  return harness === "claude-native" && !unavailable ? CLAUDE_BUILT_IN_MODEL_OPTIONS : [];
}

/** Why a launch is blocked when the provider is certain no model serves *name*. */
export function noReachableModelsMessage(name: string | undefined): string {
  return `No models are available for ${name ?? "this harness"}. Ask an admin to enable model access.`;
}
