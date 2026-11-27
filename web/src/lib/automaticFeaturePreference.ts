// A feature's stored choice: Automatic, On or Off. The automatic policy and the effective value
// stay with the feature; this keeps only the user's intent, with Automatic stored as nothing.

import { createStoredChoicePreference } from "@/lib/storedChoicePreference";

/** What the user chose. Nothing stored means Automatic. */
export type AutomaticFeatureIntent = "automatic" | "on" | "off";

export const AUTOMATIC_FEATURE_INTENTS: readonly AutomaticFeatureIntent[] = [
  "automatic",
  "on",
  "off",
];

export interface AutomaticFeaturePreference {
  storageKey: string;
  /** The stored intent; storage errors and unknown values mean Automatic. */
  read: () => AutomaticFeatureIntent;
  /** Persist the intent and tell this window's listeners. */
  write: (intent: AutomaticFeatureIntent) => void;
  /** Call `listener` when the intent changes here or in another window. */
  subscribe: (listener: () => void) => () => void;
  /** Tell this window's listeners after a bulk write, such as a settings import. */
  notifyChanged: () => void;
  /** The live intent. */
  useIntent: () => AutomaticFeatureIntent;
}

export function isAutomaticFeatureIntent(value: unknown): value is AutomaticFeatureIntent {
  return AUTOMATIC_FEATURE_INTENTS.includes(value as AutomaticFeatureIntent);
}

/** One feature's stored intent, under its own key; Automatic is the default, stored as nothing. */
export function createAutomaticFeaturePreference(storageKey: string): AutomaticFeaturePreference {
  // Automatic stored as nothing means a later policy reaches everyone still on it.
  const stored = createStoredChoicePreference(storageKey, AUTOMATIC_FEATURE_INTENTS, "automatic");
  return {
    storageKey,
    read: stored.read,
    write: (intent) => {
      stored.write(intent);
    },
    subscribe: stored.subscribe,
    notifyChanged: stored.notifyChanged,
    useIntent: stored.use,
  };
}
