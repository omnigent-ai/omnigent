// Per-device opt-in to fold a settled turn's earlier assistant messages into
// its "Worked" row. Off by default: the last message is only a heuristic for
// the final answer.

import { useSyncExternalStore } from "react";

export const FOLD_ASSISTANT_NARRATION_STORAGE_KEY = "omnigent:fold-assistant-narration";
const CHANGE_EVENT = "omnigent:fold-assistant-narration-change";

export const DEFAULT_FOLD_ASSISTANT_NARRATION = false;

/** Read the preference; storage errors and unknown values mean the default. */
export function readFoldAssistantNarration(): boolean {
  if (typeof window === "undefined") return DEFAULT_FOLD_ASSISTANT_NARRATION;
  try {
    return window.localStorage.getItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY) === "true";
  } catch {
    return DEFAULT_FOLD_ASSISTANT_NARRATION;
  }
}

/** Persist the preference and notify chats already on screen. */
export function writeFoldAssistantNarration(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled === DEFAULT_FOLD_ASSISTANT_NARRATION) {
      window.localStorage.removeItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY);
    } else {
      window.localStorage.setItem(FOLD_ASSISTANT_NARRATION_STORAGE_KEY, String(enabled));
    }
  } catch {
    // A storage failure must not break Settings.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

function subscribe(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== FOLD_ASSISTANT_NARRATION_STORAGE_KEY) return;
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      // Unreadable storage: re-read, which falls back to the default.
    }
    listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** Live value of the preference, including changes made in other windows. */
export function useFoldAssistantNarration(): boolean {
  return useSyncExternalStore(
    subscribe,
    readFoldAssistantNarration,
    () => DEFAULT_FOLD_ASSISTANT_NARRATION,
  );
}
