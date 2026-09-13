import { useEffect, useState } from "react";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";

/**
 * The current viewer's user id, resolved reactively. Uses `getCurrentUserId`
 * (NOT `getCurrentAuthorId`): ownership compares against a session's `owner`
 * grant, which in single-user mode is the reserved ``"local"`` id. ``null``
 * until identity resolves.
 */
export function useViewerId(): string | null {
  const [viewerId, setViewerId] = useState<string | null>(() => getCurrentUserId());
  useEffect(() => {
    let cancelled = false;
    void resolveIdentity().then(() => {
      if (!cancelled) setViewerId(getCurrentUserId());
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return viewerId;
}
