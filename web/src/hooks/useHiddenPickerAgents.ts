import { useSyncExternalStore } from "react";

import { readHiddenPickerAgents, subscribeHiddenPickerAgents } from "@/lib/pickerEntryVisibility";

const EMPTY: ReadonlySet<string> = new Set();

/**
 * Subscribe to the per-device set of picker entries the user has hidden.
 *
 * Reactive, unlike the read-once ``readHideUnconfiguredHarnesses`` preference:
 * toggling an entry in Settings updates an already-open picker without a
 * reload, and a change in another tab propagates too.
 *
 * @returns Agent names hidden from the picker (empty when none are).
 */
export function useHiddenPickerAgents(): ReadonlySet<string> {
  return useSyncExternalStore(
    subscribeHiddenPickerAgents,
    readHiddenPickerAgents,
    () => EMPTY, // server render: nothing hidden
  );
}
