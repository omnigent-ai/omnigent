// A feature's stored choice: Automatic, On or Off. The automatic policy and the effective value
// stay with the feature; this keeps only the user's intent, with Automatic stored as nothing.

import { useSyncExternalStore } from "react";

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

/** One feature's stored intent, under its own key. */
export function createAutomaticFeaturePreference(storageKey: string): AutomaticFeaturePreference {
  const changeEvent = `${storageKey}-change`;

  const read = (): AutomaticFeatureIntent => {
    if (typeof window === "undefined") return "automatic";
    try {
      const stored = window.localStorage.getItem(storageKey);
      return isAutomaticFeatureIntent(stored) ? stored : "automatic";
    } catch {
      return "automatic";
    }
  };

  const notifyChanged = (): void => {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new Event(changeEvent));
  };

  const write = (intent: AutomaticFeatureIntent): void => {
    if (typeof window === "undefined") return;
    try {
      // Automatic is stored as nothing, so a later policy reaches everyone still on it.
      if (intent === "automatic") window.localStorage.removeItem(storageKey);
      else window.localStorage.setItem(storageKey, intent);
    } catch {
      // A storage failure must not break Settings.
    }
    notifyChanged();
  };

  const subscribe = (listener: () => void): (() => void) => {
    if (typeof window === "undefined") return () => {};
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== storageKey) return;
      listener();
    };
    window.addEventListener(changeEvent, listener);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(changeEvent, listener);
      window.removeEventListener("storage", onStorage);
    };
  };

  const useIntent = (): AutomaticFeatureIntent =>
    useSyncExternalStore(subscribe, read, () => "automatic");

  return { storageKey, read, write, subscribe, notifyChanged, useIntent };
}
