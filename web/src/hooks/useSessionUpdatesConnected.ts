// React binding for the session-updates WebSocket connection state.
//
// Lets the conversations query suspend its fallback HTTP poll while the
// push stream is live (and resume it if the stream drops), without
// threading connection state through every caller.

import { useSyncExternalStore } from "react";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";

/**
 * Subscribe to whether the session-updates stream is currently connected.
 *
 * @returns `true` while the WebSocket is open, `false` otherwise (and on
 *   the server during SSR / before the socket starts).
 */
export function useSessionUpdatesConnected(): boolean {
  return useSyncExternalStore(
    (onChange) => sessionUpdatesSocket.subscribeStatus(onChange),
    () => sessionUpdatesSocket.isConnected(),
    () => false,
  );
}
