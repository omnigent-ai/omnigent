import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { PROACTIVE_AGENT_DISPLAY_NAME } from "@/lib/proactiveInbox";

import { settingsEntry, settingsEntryFamily } from "./types";

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
  available: (availability) => availability.sessionTitleInstructions,
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
  available: (availability) => availability.inAppBrowser,
  section: "general",
  label: "Open links in the in-app browser",
  description: `Open chat links in-app, reusing matching tabs. ${MOD_KEY}+click also opens in-app; Shift+click opens your default browser.`,
  keywords: ["links", "browser", "in-app", "tabs", "click"],
});

export const OPEN_IN_DESKTOP_APP_ENTRY = settingsEntry({
  id: "open-in-desktop-app",
  available: (availability) => availability.desktopAppHandoff,
  section: "general",
  label: "Open conversations in the desktop app",
  description:
    "When a conversation link opens in this browser and the Omnigent desktop app is running, open it there. Your browser may ask once to let this site reach apps on this device.",
  keywords: ["desktop", "app", "links", "browser", "open", "conversation", "hand off"],
});

export const ADMIN_VIEW_ENTRY = settingsEntry({
  id: "admin-view",
  available: (availability) => availability.adminView,
  section: "general",
  label: "Admin view",
  description: "Turn off to use your normal session access.",
  keywords: ["admin", "administrator", "access", "permissions", "view"],
});

export const PROJECT_KANBAN_BOARD_ENTRY = settingsEntry({
  id: "project-kanban-board",
  section: "general",
  available: (availability) => availability.projectKanban,
  label: "Kanban board views",
  description: "Monitor a project's agents in one view and respond the moment one needs you.",
  keywords: ["kanban", "board", "projects", "agents", "view"],
});

export const BROWSER_AGENT_TOOLS_ENTRY = settingsEntry({
  id: "browser-agent-tools",
  section: "general",
  available: (availability) => availability.inAppBrowser,
  label: "Advanced browser tools for agents",
  description:
    "Let agents go back and forward, reload, manage tabs, hover, right-click, double-click, drag, scroll, press keys, and use dropdowns and checkboxes in the in-app browser. New sessions use this setting. Turning it off also stops these actions in existing sessions on this device.",
  keywords: ["browser", "agent", "tools", "tabs", "hover", "scroll", "desktop"],
});

export const PROACTIVE_AGENT_ENTRY = settingsEntry({
  id: "proactive-agent",
  section: "general",
  available: (availability) => availability.proactiveAgent,
  label: PROACTIVE_AGENT_DISPLAY_NAME,
  description:
    "Reads your recent sessions on a schedule and posts follow-ups, related information, and reminders to your Inbox.",
  keywords: ["proactive", "advisor", "inbox", "follow-ups", "reminders", "agent"],
  parts: { toggle: PROACTIVE_AGENT_DISPLAY_NAME, retry: "Retry" },
  presence: { retry: { states: ["could not load"] } },
});

/** The desktop app's shortcut that brings it forward; a row only desktop builds show. */
export const GLOBAL_SHORTCUT_ENTRY = settingsEntry({
  id: "global-shortcut",
  available: (availability) => availability.globalShortcut,
  section: "general",
  label: "Show Omnigent from anywhere",
  description:
    "A system-wide shortcut: it brings Omnigent to the front from any app, even when Omnigent is in the background.",
  keywords: [
    "shortcut",
    "hotkey",
    "keyboard",
    "global",
    "system-wide",
    "any app",
    "desktop",
    "bring to front",
    "window",
  ],
  // Named by the chord it holds, or by the prompt while it records one. The suggestion names
  // the free chord it proposes.
  parts: { recorder: "Shortcut", suggestion: "Use suggested shortcut" },
  nameTemplates: {
    recorder: { before: "Shortcut: " },
    suggestion: { before: "Use ", after: " instead" },
  },
  // Offered only after the shell couldn't register the chord.
  presence: { suggestion: { states: ["shortcut taken by another app"] } },
});

/** The desktop app's shortcut that opens the New Agent panel; macOS, with `floating_composer`. */
export const FLOATING_COMPOSER_SHORTCUT_ENTRY = settingsEntry({
  id: "floating-composer-shortcut",
  available: (availability) => availability.floatingComposerShortcut,
  section: "general",
  label: "Open New Agent from anywhere",
  description:
    "A system-wide shortcut: it opens the New Agent panel from any app, even when Omnigent is in the background.",
  keywords: [
    "shortcut",
    "hotkey",
    "keyboard",
    "global",
    "system-wide",
    "any app",
    "desktop",
    "new agent",
    "panel",
  ],
  // Named by the chord it holds, or by the prompt while it records one. The suggestion names
  // the free chord it proposes.
  parts: { recorder: "New Agent shortcut", suggestion: "Use suggested New Agent shortcut" },
  nameTemplates: {
    recorder: { before: "New Agent shortcut: " },
    suggestion: { before: "Use ", after: " instead" },
  },
  // Offered only after the shell couldn't register the chord.
  presence: { suggestion: { states: ["shortcut taken by another app"] } },
});

/** The desktop app's opt-in to make room for the right sidebar; a row only desktop builds show. */
export const WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY = settingsEntry({
  id: "widen-window-for-right-sidebar",
  available: (availability) => availability.widenWindowForRightSidebar,
  section: "general",
  label: "Widen window when opening the right sidebar",
  description:
    "Opening the right sidebar makes the window wider, so the chat keeps its width. Closing it shrinks the window back. Maximized and full-screen windows keep their size.",
  keywords: ["window", "resize", "wider", "width", "sidebar", "workspace", "panel", "desktop"],
});

/** Both system-wide shortcuts' rows, by the shell's shortcut id; one control renders either. */
export const GLOBAL_SHORTCUT_ENTRIES = settingsEntryFamily({
  key: "global-shortcuts",
  members: ["show", "floatingComposer"],
  build: (shortcut) =>
    shortcut === "show" ? GLOBAL_SHORTCUT_ENTRY : FLOATING_COMPOSER_SHORTCUT_ENTRY,
});

export const GENERAL_SETTINGS = [
  SUBMIT_WITH_MOD_ENTER_ENTRY,
  ALWAYS_STEER_ENTRY,
  BACKGROUND_SESSION_TITLES_ENTRY,
  SESSION_TITLE_INSTRUCTIONS_ENTRY,
  OPEN_SUBAGENTS_IN_PANEL_ENTRY,
  TERMINAL_CLIPBOARD_ENTRY,
  OPEN_LINKS_IN_APP_ENTRY,
  OPEN_IN_DESKTOP_APP_ENTRY,
  ADMIN_VIEW_ENTRY,
  PROJECT_KANBAN_BOARD_ENTRY,
  BROWSER_AGENT_TOOLS_ENTRY,
  PROACTIVE_AGENT_ENTRY,

  GLOBAL_SHORTCUT_ENTRY,
  FLOATING_COMPOSER_SHORTCUT_ENTRY,
  WIDEN_WINDOW_FOR_RIGHT_SIDEBAR_ENTRY,
] as const;
