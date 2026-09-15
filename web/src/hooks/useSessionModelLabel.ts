import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { formatStatusModelLabel, nativeModelLabel } from "@/lib/composerModelLabel";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";
import {
  getSessionModelLabelCacheKey,
  readSessionModelLabelCache,
  writeSessionModelLabelCache,
  type SessionModelLabelScope,
} from "@/lib/sessionModelLabelCache";
import type { NativeModelOption } from "@/lib/types";

// Empty session catalogs represent both pending probes and unavailable runners.
export const SESSION_MODEL_LABEL_WAIT_MS = 30_000;

export function useSessionModelLabel(
  scope: SessionModelLabelScope,
  model: string | null,
  options: readonly NativeModelOption[],
  expectsCatalog: boolean,
  confirmed: boolean,
  hostOptions: readonly NativeModelOption[] = [],
) {
  const user = getCurrentUserId();
  const [, identityChanged] = useReducer((revision: number) => revision + 1, 0);
  useEffect(() => {
    if (!expectsCatalog || user !== null) return;
    let cancelled = false;
    // Subscribe to the shared boot probe when its safety timeout mounted us early.
    void resolveIdentity().then((resolvedUser) => {
      if (!cancelled && resolvedUser !== user) identityChanged();
    });
    return () => {
      cancelled = true;
    };
  }, [expectsCatalog, user]);

  const raw = model?.trim() || null;
  const key = confirmed ? getSessionModelLabelCacheKey(scope, raw) : null;
  const catalogReady = options.length > 0;
  const option = options.find((row) => row.id === raw) ?? options.find((row) => row.model === raw);
  const displayName = option?.displayName ?? null;
  const stored = useMemo(() => readSessionModelLabelCache(key), [key]);
  const remembered = useRef<{ key: string; displayName: string | null } | null>(null);
  const cached =
    key !== null && remembered.current?.key === key ? remembered.current.displayName : stored;

  useEffect(() => {
    if (!expectsCatalog || !catalogReady || key === null) return;
    remembered.current = { key, displayName };
    writeSessionModelLabelCache(key, displayName);
  }, [expectsCatalog, catalogReady, key, displayName]);

  // Host names are a display fallback only; the session cache stays session-owned.
  const hostOption =
    hostOptions.find((row) => row.id === raw) ?? hostOptions.find((row) => row.model === raw);
  const fallbackLabel = cached ?? (hostOption ? nativeModelLabel(hostOption) : null);
  const waiting = expectsCatalog && raw !== null && !catalogReady && fallbackLabel === null;
  // Finishing identity bootstrap must not restart an in-flight metadata wait.
  const target = JSON.stringify([scope.sessionId, scope.hostId, scope.agentId, scope.harness, raw]);
  const [timedOutTarget, setTimedOutTarget] = useState<string | null>(null);
  useEffect(() => {
    setTimedOutTarget(null);
    if (!waiting) return;
    const timer = setTimeout(() => setTimedOutTarget(target), SESSION_MODEL_LABEL_WAIT_MS);
    return () => clearTimeout(timer);
  }, [target, waiting]);

  const unavailable = waiting && timedOutTarget === target;
  return {
    label:
      !expectsCatalog || catalogReady
        ? formatStatusModelLabel(raw, options)
        : raw === null
          ? null
          : fallbackLabel,
    loading: waiting && !unavailable,
    unavailable,
  };
}
