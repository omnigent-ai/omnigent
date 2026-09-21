import { getOmnigentServerIdentity } from "./host";

export type TerminalClipboardPreference = "ask" | "allow" | "block";

const STORAGE_PREFIX = "omnigent:terminal-clipboard:v1:";
const CHANGE_EVENT = "omnigent:terminal-clipboard-change";

function storageKey(): string | null {
  if (typeof window === "undefined") return null;
  const server = getOmnigentServerIdentity();
  return server ? `${STORAGE_PREFIX}${JSON.stringify(server)}` : null;
}

function isPreference(value: unknown): value is TerminalClipboardPreference {
  return value === "ask" || value === "allow" || value === "block";
}

/** Clipboard trust stays local to this browser/app and Omnigent server. */
export function readTerminalClipboardPreference(): TerminalClipboardPreference {
  const key = storageKey();
  if (key === null) return "ask";
  try {
    const value = window.localStorage.getItem(key);
    return isPreference(value) ? value : "ask";
  } catch {
    return "ask";
  }
}

/** Return false when the decision cannot be saved; callers may offer a session-only choice. */
export function writeTerminalClipboardPreference(value: TerminalClipboardPreference): boolean {
  const key = storageKey();
  if (key === null || !isPreference(value)) return false;
  try {
    if (value === "ask") window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    return false;
  }
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: key }));
  return true;
}

/** Apply grants and revocations to already-mounted terminals, including other tabs. */
export function subscribeTerminalClipboardPreference(
  listener: (value: TerminalClipboardPreference) => void,
): () => void {
  if (typeof window === "undefined") return () => {};

  const onChange = (event: Event) => {
    if ((event as CustomEvent<string>).detail === storageKey()) {
      listener(readTerminalClipboardPreference());
    }
  };
  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== storageKey()) return;
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      listener("ask");
      return;
    }
    listener(readTerminalClipboardPreference());
  };

  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}
