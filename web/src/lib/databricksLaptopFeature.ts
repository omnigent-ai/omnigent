// Features that turn on by themselves on Databricks laptops. Each keeps three things
// apart: its automatic policy (a constant the feature owns), the user's stored intent
// (Automatic, On or Off) and the effective value both of them and the laptop check give.

import { useSyncExternalStore } from "react";

import { isDatabricksLaptop } from "@/lib/nativeBridge";

/** What the user chose. Nothing stored means Automatic. */
export type DatabricksLaptopFeatureIntent = "automatic" | "on" | "off";

export const DATABRICKS_LAPTOP_FEATURE_INTENTS: readonly DatabricksLaptopFeatureIntent[] = [
  "automatic",
  "on",
  "off",
];

/**
 * Whether the feature is on. On and Off override the policy; Automatic follows it, and
 * only on a laptop the check has confirmed, so a pending or failed check counts as off.
 */
export function databricksLaptopFeatureEnabled({
  intent,
  automaticPolicy,
  laptop,
}: {
  intent: DatabricksLaptopFeatureIntent;
  automaticPolicy: boolean;
  /** `null` while the check is still running. */
  laptop: boolean | null;
}): boolean {
  if (intent === "on") return true;
  if (intent === "off") return false;
  return automaticPolicy && laptop === true;
}

// The desktop shell runs the check once and caches it; the page asks once too.
let laptopAnswer: boolean | null = null;
let laptopRequest: Promise<void> | null = null;
const laptopListeners = new Set<() => void>();

function requestLaptopAnswer(): void {
  if (laptopRequest) return;
  laptopRequest = isDatabricksLaptop().then(
    (answer) => {
      laptopAnswer = answer;
      for (const listener of laptopListeners) listener();
    },
    () => {
      laptopAnswer = false;
      for (const listener of laptopListeners) listener();
    },
  );
}

function subscribeLaptop(listener: () => void): () => void {
  laptopListeners.add(listener);
  requestLaptopAnswer();
  return () => laptopListeners.delete(listener);
}

/** Whether this is a Databricks laptop; `null` until the desktop shell answers. */
export function useDatabricksLaptop(): boolean | null {
  return useSyncExternalStore(
    subscribeLaptop,
    () => laptopAnswer,
    () => null,
  );
}

/** Forget the cached answer, so a test can stub the bridge again. */
export function resetDatabricksLaptopForTests(): void {
  laptopAnswer = null;
  laptopRequest = null;
}

export interface DatabricksLaptopFeaturePreference {
  storageKey: string;
  /** The stored intent; storage errors and unknown values mean Automatic. */
  read: () => DatabricksLaptopFeatureIntent;
  /** Persist the intent and tell this window's listeners. */
  write: (intent: DatabricksLaptopFeatureIntent) => void;
  /** Call `listener` when the intent changes here or in another window. */
  subscribe: (listener: () => void) => () => void;
  /** Tell this window's listeners after a bulk write, such as a settings import. */
  notifyChanged: () => void;
  /** The live intent. */
  useIntent: () => DatabricksLaptopFeatureIntent;
}

export function isDatabricksLaptopFeatureIntent(
  value: unknown,
): value is DatabricksLaptopFeatureIntent {
  return DATABRICKS_LAPTOP_FEATURE_INTENTS.includes(value as DatabricksLaptopFeatureIntent);
}

/** One feature's stored intent, under its own key. */
export function createDatabricksLaptopFeaturePreference(
  storageKey: string,
): DatabricksLaptopFeaturePreference {
  const changeEvent = `${storageKey}-change`;

  const read = (): DatabricksLaptopFeatureIntent => {
    if (typeof window === "undefined") return "automatic";
    try {
      const stored = window.localStorage.getItem(storageKey);
      return isDatabricksLaptopFeatureIntent(stored) ? stored : "automatic";
    } catch {
      return "automatic";
    }
  };

  const notifyChanged = (): void => {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new Event(changeEvent));
  };

  const write = (intent: DatabricksLaptopFeatureIntent): void => {
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

  const useIntent = (): DatabricksLaptopFeatureIntent =>
    useSyncExternalStore(subscribe, read, () => "automatic");

  return { storageKey, read, write, subscribe, notifyChanged, useIntent };
}
