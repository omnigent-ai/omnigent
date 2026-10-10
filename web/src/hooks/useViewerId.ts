import { useEffect, useState } from "react";
import { getCurrentUserId, resolveIdentity, subscribeIdentity } from "@/lib/identity";

/** Whether the initial viewer-identity request has settled, including failure. */
export function useIdentityReady(): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const settle = () => {
      if (!cancelled) setReady(true);
    };
    void resolveIdentity().then(settle, settle);
    return () => {
      cancelled = true;
    };
  }, []);
  return ready;
}

/**
 * The current viewer's user id, resolved reactively. Uses `getCurrentUserId`
 * (NOT `getCurrentAuthorId`): ownership compares against a session's `owner`
 * grant, which in single-user mode is the reserved ``"local"`` id. ``null``
 * until identity resolves, and follows a probe that only succeeds later (the
 * boot probe failed transiently and was retried).
 */
export function useViewerId(): string | null {
  const [viewerId, setViewerId] = useState<string | null>(() => getCurrentUserId());
  useEffect(() => {
    let cancelled = false;
    const sync = () => {
      if (!cancelled) setViewerId(getCurrentUserId());
    };
    const unsubscribe = subscribeIdentity(sync);
    void resolveIdentity().then(sync, sync);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);
  return viewerId;
}
