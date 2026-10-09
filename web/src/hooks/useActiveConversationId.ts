// Health and live-update providers sit above `<Routes>`, where useParams
// has no match. Read both conversation routes under the app's mount path.

import { useMemo } from "react";
import { useLocation, useRebasePath } from "@/lib/routing";

/**
 * Extract the active conversation id from a chat or Canvas session route.
 *
 * @returns The conversation id when on a chat route (e.g. `"conv_abc123"`),
 *   otherwise `undefined`.
 */
export function useActiveConversationId(): string | undefined {
  const { pathname } = useLocation();
  const rebasePath = useRebasePath();
  return useMemo(() => {
    for (const prefix of [rebasePath("/c/"), rebasePath("/canvas/c/")]) {
      if (!pathname.startsWith(prefix)) continue;
      const match = pathname.slice(prefix.length).match(/^([^/]+)\/?$/);
      if (match) {
        try {
          return decodeURIComponent(match[1]);
        } catch {
          return match[1];
        }
      }
    }
    return undefined;
  }, [pathname, rebasePath]);
}
