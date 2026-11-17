import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";

import { settingsEntry } from "./types";

export const SUBMIT_WITH_MOD_ENTER_ENTRY = settingsEntry({
  id: "submit-with-mod-enter",
  section: "general",
  label: `Submit with ${MOD_KEY} + Enter on desktop`,
  description: `Off: Enter submits and Shift+Enter or ${ALT_KEY}+Enter inserts a newline. On: Enter inserts a newline and ${MOD_KEY}+Enter submits.`,
  keywords: ["composer", "enter", "newline", "send", "submit", "keyboard"],
});

export const ALWAYS_STEER_ENTRY = settingsEntry({
  id: "always-steer",
  section: "general",
  label: "Always steer",
  description:
    "Send follow-ups into the running turn. Harnesses without steering support use them on the next turn.",
  keywords: ["composer", "queue", "follow-up", "send", "interrupt", "running turn"],
});

export const BACKGROUND_SESSION_TITLES_ENTRY = settingsEntry({
  id: "background-session-titles",
  section: "general",
  label: "Automatically name new sessions",
  description: "Name sessions after the first message. Turn off to keep default names.",
  keywords: ["sessions", "titles", "rename", "auto name"],
});

export const SESSION_TITLE_INSTRUCTIONS_ENTRY = settingsEntry({
  id: "session-title-instructions",
  section: "general",
  label: "Title instructions",
  description:
    "Guidance for automatic session titles, such as a format, language or prefix. Leave blank to use the server's title instructions, or the default prompt.",
  keywords: [
    "session titles",
    "title format",
    "title prefix",
    "title language",
    "naming",
    "prompt",
  ],
  parts: {
    save: "Save title instructions",
    clear: "Clear title instructions",
    retry: "Retry loading title instructions",
  },
  // Each is revealed in place; while it isn't shown, the reveal lands on the field or Retry.
  presence: {
    save: { states: ["loaded", "saved"] },
    clear: { states: ["saved"] },
    retry: { states: ["load failed"] },
  },
});

export const OPEN_SUBAGENTS_IN_PANEL_ENTRY = settingsEntry({
  id: "open-subagents-in-panel",
  section: "general",
  label: "Open sub-agents in the Workspace panel",
  description: "Open sub-agent chats beside the main chat. On phones, they still open full screen.",
  keywords: ["sessions", "sub-agents", "subagents", "workspace", "panel", "side by side"],
});

export const TERMINAL_CLIPBOARD_ENTRY = settingsEntry({
  id: "terminal-clipboard",
  section: "general",
  label: "Copying from terminals",
  description:
    "Control copying from all sessions and terminals on this server, in this browser or app. Allowing it lets terminal programs silently replace your clipboard with unwanted text or commands.",
  keywords: ["clipboard", "copy", "osc 52", "paste", "terminal programs"],
  // As the preference select names them.
  choices: ["Ask before copying", "Allow copying", "Block copying"],
});

export const OPEN_LINKS_IN_APP_ENTRY = settingsEntry({
  id: "open-links-in-app",
  section: "general",
  label: "Open links in the in-app browser",
  description: `Open chat links in-app, reusing matching tabs. ${MOD_KEY}+click also opens in-app; Shift+click opens your default browser.`,
  keywords: ["links", "browser", "in-app", "tabs", "click"],
});

export const ADMIN_VIEW_ENTRY = settingsEntry({
  id: "admin-view",
  section: "general",
  label: "Admin view",
  description: "Turn off to use your normal session access.",
  keywords: ["admin", "administrator", "access", "permissions", "view"],
});

export const GENERAL_SETTINGS = [
  SUBMIT_WITH_MOD_ENTER_ENTRY,
  ALWAYS_STEER_ENTRY,
  BACKGROUND_SESSION_TITLES_ENTRY,
  SESSION_TITLE_INSTRUCTIONS_ENTRY,
  OPEN_SUBAGENTS_IN_PANEL_ENTRY,
  TERMINAL_CLIPBOARD_ENTRY,
  OPEN_LINKS_IN_APP_ENTRY,
  ADMIN_VIEW_ENTRY,
] as const;
