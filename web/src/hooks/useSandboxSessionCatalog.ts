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

function savedCatalogsChanged() {
  for (const listener of savedCatalogListeners) listener();
}

function onStorage(event: StorageEvent) {
  if (event.key === null || event.key.startsWith("omnigent:model-catalog:")) savedCatalogsChanged();
}

// Notified by saves in this tab and storage events from others, so mounted readers reread.
function subscribeToSavedCatalogs(listener: () => void) {
  if (savedCatalogListeners.size === 0) window.addEventListener("storage", onStorage);
  savedCatalogListeners.add(listener);
  return () => {
    savedCatalogListeners.delete(listener);
    if (savedCatalogListeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

/** Models a running session on this sandbox provider last reported for the harness. */
function savedSessionModels(text: string | null): NativeModelOption[] | undefined {
  const saved = parseModelCatalogCache(text, SANDBOX_SESSION_CATALOG_MAX_AGE_MS);
  return saved && saved.models.length > 0 ? saved.models : undefined;
}

/** The saved session catalog, rereading whenever one is saved. */
export function useSavedSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
): NativeModelOption[] | undefined {
  // The key carries server and user identity, so a late sign-in rereads.
  const key = sandboxSessionCatalogKey(provider, harness);
  const text = useSyncExternalStore(subscribeToSavedCatalogs, () => readModelCatalogCacheText(key));
  return useMemo(() => savedSessionModels(text), [text]);
}

/** Save a managed-sandbox session's own catalog; never pass a launch preview. */
export function useRememberSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const key = sandboxSessionCatalogKey(provider, harness);
  useEffect(() => {
    if (!enabled || models.length === 0 || key === null) return;
    writeModelCatalogCache(key, models, false);
    savedCatalogsChanged();
  }, [enabled, key, models]);
}
