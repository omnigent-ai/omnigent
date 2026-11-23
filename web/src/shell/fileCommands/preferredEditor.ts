// The code editor the user last opened a file in, so the "Open" split button
// defaults to it. Remembered per browser, and kept in sync across windows.

import { useSyncExternalStore } from "react";
import type { CodeEditor } from "@/lib/editorLinks";

const PREFERENCE_EVENT = "omnigent:open-in-app-changed";
const PREFERENCE_KEY = "omnigent:default-code-editor";

function readPreferredEditor(): string {
  try {
    return localStorage.getItem(PREFERENCE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberPreferredEditor(editor: CodeEditor): void {
  try {
    localStorage.setItem(PREFERENCE_KEY, editor);
  } catch {
    // Opening still works if this browser cannot save preferences.
  }
  window.dispatchEvent(new Event(PREFERENCE_EVENT));
}

function subscribeToPreferredEditor(listener: () => void) {
  window.addEventListener(PREFERENCE_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(PREFERENCE_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

/** The remembered editor's id, or "" before one is chosen. */
export function usePreferredEditor(): string {
  return useSyncExternalStore(subscribeToPreferredEditor, readPreferredEditor, () => "");
}
