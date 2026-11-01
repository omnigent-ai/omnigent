// App-global preference (Settings › General): when projects exist, starting a
// new session first asks which project it belongs to. On unless turned off.

export const NEW_CHAT_PROJECT_PROMPT_STORAGE_KEY = "omnigent:new-chat-project-prompt";

export function readNewChatProjectPromptEnabled(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(NEW_CHAT_PROJECT_PROMPT_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeNewChatProjectPromptEnabled(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (enabled) {
      window.localStorage.removeItem(NEW_CHAT_PROJECT_PROMPT_STORAGE_KEY);
    } else {
      window.localStorage.setItem(NEW_CHAT_PROJECT_PROMPT_STORAGE_KEY, "off");
    }
  } catch {
    // localStorage failures should not make Settings unusable.
  }
}
