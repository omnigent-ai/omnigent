// Shared hook for the conversation id of the currently-viewed chat route.
//
// Both RunnerHealthProvider and SessionUpdatesProvider render above the
// router's `<Routes>`, so `useParams` has no match there — they match the
// pathname directly instead. Kept in one place so the route shape (`/c/:id`)
// is defined once.

import { useMemo } from "react";
import { useLocation } from "@/lib/routing";

/**
 * Extract the active conversation id from the `/c/:id` route.
 *
 * @returns The conversation id when on a chat route (e.g. `"conv_abc123"`),
 *   otherwise `undefined`.
 */
export function useActiveConversationId(): string | undefined {
  const { pathname } = useLocation();
  return useMemo(() => {
    const match = pathname.match(/^\/c\/([^/]+)/);
    return match ? match[1] : undefined;
  }, [pathname]);
}
