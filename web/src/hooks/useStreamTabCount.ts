// React binding for the count of same-origin tabs holding a session event
// stream. Backs the too-many-tabs warning banner.

import { useSyncExternalStore } from "react";
import { getStreamTabCount, subscribeStreamTabCount } from "@/lib/streamTabRegistry";

/**
 * Subscribe to how many same-origin tabs currently hold a session event stream.
 *
 * @returns The observed count (including this tab), or 0 during SSR / where the
 *   Web Locks API is unavailable.
 */
export function useStreamTabCount(): number {
  return useSyncExternalStore(subscribeStreamTabCount, getStreamTabCount, () => 0);
}
