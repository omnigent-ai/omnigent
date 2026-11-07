import { useEffect, useMemo } from "react";
import {
  readModelCatalogCache,
  SANDBOX_SESSION_CATALOG_MAX_AGE_MS,
  sandboxSessionCatalogKey,
  writeModelCatalogCache,
} from "@/lib/modelCatalogCache";
import type { NativeModelOption } from "@/lib/types";

/** Models a running session on this sandbox provider last reported for the harness. */
export function readSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
): NativeModelOption[] | undefined {
  const saved = readModelCatalogCache(
    sandboxSessionCatalogKey(provider, harness),
    SANDBOX_SESSION_CATALOG_MAX_AGE_MS,
  );
  return saved && saved.models.length > 0 ? saved.models : undefined;
}

/** Read the saved session catalog once per provider and harness. */
export function useSavedSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
): NativeModelOption[] | undefined {
  const key = sandboxSessionCatalogKey(provider, harness);
  // The key carries server and user identity, so a late sign-in rereads.
  return useMemo(
    () => (key === null ? undefined : readSandboxSessionCatalog(provider, harness)),
    [key, provider, harness],
  );
}

/**
 * Save the catalog a managed-sandbox session's runner reported.
 *
 * Pass only the session's own catalog, never a launch preview, so the
 * new-session picker learns what the running harness actually offers.
 */
export function useRememberSandboxSessionCatalog(
  provider: string | null,
  harness: string | null,
  models: readonly NativeModelOption[],
  enabled: boolean,
) {
  const key = sandboxSessionCatalogKey(provider, harness);
  useEffect(() => {
    if (enabled && models.length > 0) writeModelCatalogCache(key, models, false);
  }, [enabled, key, models]);
}
