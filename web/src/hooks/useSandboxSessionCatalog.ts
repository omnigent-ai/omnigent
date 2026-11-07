import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
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

// An unchanged catalog is resaved at most this often, so an active sandbox's entry stays fresh.
const RESAVE_INTERVAL_MS = 24 * 60 * 60 * 1000;

/** Compare against the stored JSON, since parsing drops fields the schema doesn't name. */
function storedModelsMatch(text: string, models: readonly NativeModelOption[]) {
  try {
    const stored = (JSON.parse(text) as { models?: unknown }).models;
    return JSON.stringify(stored) === JSON.stringify(models.filter((model) => !model.fusion));
  } catch {
    return false;
  }
}

/** The saved session catalog, rereading whenever one is saved and dropping it once expired. */
export function useSavedSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
): NativeModelOption[] | undefined {
  // The key carries server and user identity, so a late sign-in rereads.
  const key = sandboxSessionCatalogKey(provider, harness);
  const text = useSyncExternalStore(subscribeToSavedCatalogs, () => readModelCatalogCacheText(key));
  const saved = useMemo(
    () => parseModelCatalogCache(text, SANDBOX_SESSION_CATALOG_MAX_AGE_MS),
    [text],
  );
  const [expiredText, setExpiredText] = useState<string | null>(null);
  useEffect(() => {
    if (saved === null) return;
    const remainingMs = saved.savedAt + SANDBOX_SESSION_CATALOG_MAX_AGE_MS - Date.now();
    const timer = window.setTimeout(() => setExpiredText(text), Math.max(0, remainingMs) + 1);
    return () => window.clearTimeout(timer);
  }, [saved, text]);
  if (saved === null || expiredText === text || saved.models.length === 0) return undefined;
  return saved.models;
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
    const text = readModelCatalogCacheText(key);
    const saved = parseModelCatalogCache(text, SANDBOX_SESSION_CATALOG_MAX_AGE_MS);
    const unchanged =
      text !== null &&
      saved !== null &&
      Date.now() - saved.savedAt < RESAVE_INTERVAL_MS &&
      storedModelsMatch(text, models);
    if (unchanged) return;
    writeModelCatalogCache(key, models, false);
    savedCatalogsChanged();
  }, [enabled, key, models]);
}
