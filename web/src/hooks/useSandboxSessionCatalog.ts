import { useEffect, useMemo, useSyncExternalStore } from "react";
import {
  parseModelCatalogCache,
  readModelCatalogCacheText,
  SANDBOX_SESSION_CATALOG_MAX_AGE_MS,
  sandboxSessionCatalogKey,
  writeModelCatalogCache,
} from "@/lib/modelCatalogCache";
import type { NativeModelOption } from "@/lib/types";

const savedCatalogListeners = new Set<() => void>();

function subscribeToSavedCatalogs(listener: () => void) {
  savedCatalogListeners.add(listener);
  return () => savedCatalogListeners.delete(listener);
}

/**
 * The catalog this agent's last sandbox session reported, or undefined.
 *
 * Only a launch hint: a stale entry is harmless because the launch check
 * resets a pick the live sandbox does not offer.
 */
export function useSavedSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
): NativeModelOption[] | undefined {
  const key = sandboxSessionCatalogKey(provider, harness, agentId);
  const text = useSyncExternalStore(subscribeToSavedCatalogs, () => readModelCatalogCacheText(key));
  return useMemo(() => {
    const saved = parseModelCatalogCache(text, SANDBOX_SESSION_CATALOG_MAX_AGE_MS);
    if (!saved || saved.models.length === 0) return undefined;
    // Like the gateway preview it replaces, mark no row default: the picker's
    // own Default row stands for the sandbox's default model.
    return saved.models.map(({ isDefault: _default, ...model }) => model);
  }, [text]);
}

/** Save the catalog a sandbox session reports, for the next new-session picker. */
export function useRememberSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
  agentId: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const key = sandboxSessionCatalogKey(provider, harness, agentId);
  useEffect(() => {
    if (!enabled || key === null || models.length === 0) return;
    writeModelCatalogCache(key, models, false);
    for (const listener of savedCatalogListeners) listener();
  }, [enabled, key, models]);
}
