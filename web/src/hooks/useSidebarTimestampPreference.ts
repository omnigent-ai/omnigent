import { useSyncExternalStore } from "react";
import {
  DEFAULT_SHOW_SIDEBAR_TIMESTAMPS,
  readShowSidebarTimestamps,
  subscribeShowSidebarTimestamps,
} from "@/lib/sidebarTimestampPreferences";

/** Live, persisted visibility preference for idle-session sidebar timestamps. */
export function useShowSidebarTimestamps(): boolean {
  return useSyncExternalStore(
    subscribeShowSidebarTimestamps,
    readShowSidebarTimestamps,
    () => DEFAULT_SHOW_SIDEBAR_TIMESTAMPS,
  );
}
