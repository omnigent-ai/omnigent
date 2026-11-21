import type { ActionDefinition, ActionId } from "./types";

const definitions = [
  {
    id: "workbench.action.showCommands",
    title: "Open command palette",
    category: "General",
    keywords: ["commands", "search"],
    icon: "Search",
    palette: false,
  },
  {
    id: "workbench.action.openKeyboardShortcuts",
    title: "Open keyboard shortcuts",
    category: "General",
    keywords: ["keybindings", "hotkeys"],
    icon: "Keyboard",
    palette: true,
  },
  {
    id: "workbench.action.navigateInbox",
    title: "Go to Inbox",
    category: "Navigation",
    keywords: ["notifications", "comments", "needs response"],
    icon: "Inbox",
    palette: true,
  },
  {
    id: "workbench.action.navigateAutomations",
    title: "Go to Automations",
    category: "Navigation",
    keywords: ["scheduled", "recurring", "tasks", "cron"],
    icon: "CalendarClock",
    palette: true,
  },
  {
    id: "workbench.action.navigateSettings",
    title: "Go to Settings",
    category: "Navigation",
    keywords: ["preferences", "configuration"],
    icon: "Settings",
    palette: true,
  },
  {
    id: "workbench.action.toggleConversationsSidebar",
    title: "Toggle conversations sidebar",
    category: "View",
    keywords: ["panel", "left", "sessions"],
    icon: "PanelLeft",
    palette: true,
  },
  {
    id: "workbench.action.toggleWorkspaceSidebar",
    title: "Toggle workspace sidebar",
    category: "View",
    keywords: ["panel", "right", "files", "terminal"],
    icon: "PanelRight",
    palette: true,
  },
  {
    id: "session.action.new",
    title: "New session",
    category: "General",
    keywords: ["chat", "compose", "start"],
    icon: "SquarePen",
    palette: true,
  },
  {
    id: "session.action.openPrevious",
    title: "Open previous session",
    category: "Navigation",
    palette: true,
  },
  {
    id: "session.action.openNext",
    title: "Open next session",
    category: "Navigation",
    palette: true,
  },
  {
    id: "session.action.openPinned",
    title: "Open pinned session",
    category: "Navigation",
    keywords: ["jump", "slot"],
    palette: false,
  },
  {
    id: "chat.action.acceptApproval",
    title: "Accept approval prompt",
    category: "Chat",
    palette: true,
  },
  {
    id: "chat.action.openPreviousMessage",
    title: "Go to previous message",
    category: "Chat",
    palette: true,
  },
  {
    id: "chat.action.openNextMessage",
    title: "Go to next message",
    category: "Chat",
    palette: true,
  },
  {
    id: "composer.action.send",
    title: "Send message",
    category: "Composer",
    palette: true,
  },
  {
    id: "composer.action.stop",
    title: "Stop response",
    category: "Composer",
    palette: true,
  },
  {
    id: "composer.action.recallPrevious",
    title: "Recall previous prompt",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.recallNext",
    title: "Recall next prompt",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.selectPreviousSuggestion",
    title: "Select previous suggestion",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.selectNextSuggestion",
    title: "Select next suggestion",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.acceptSuggestion",
    title: "Accept suggestion",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.dismissSuggestions",
    title: "Dismiss suggestions",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.toggleDictation",
    title: "Toggle voice dictation",
    category: "Composer",
    palette: true,
  },
  {
    id: "composer.action.commitDictation",
    title: "Commit voice dictation",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.cancelDictation",
    title: "Cancel voice dictation",
    category: "Composer",
    palette: false,
  },
  {
    id: "file.action.find",
    title: "Find in file",
    category: "Files",
    palette: true,
  },
  {
    id: "file.action.save",
    title: "Save file",
    category: "Files",
    palette: true,
  },
  {
    id: "file.action.selectAllContent",
    title: "Select all file content",
    category: "Files",
    palette: false,
  },
  {
    id: "file.action.openPreviousChanged",
    title: "Open previous changed file",
    category: "Files",
    palette: true,
  },
  {
    id: "file.action.openNextChanged",
    title: "Open next changed file",
    category: "Files",
    palette: true,
  },
  {
    id: "file.action.closeSearch",
    title: "Close file search",
    category: "Files",
    palette: false,
  },
  {
    id: "file.action.close",
    title: "Close file",
    category: "Files",
    palette: true,
  },
  {
    id: "terminal.action.sendSequence",
    title: "Send terminal sequence",
    category: "Terminal",
    palette: false,
  },
  {
    id: "panel.action.closeFiles",
    title: "Close files panel",
    category: "View",
    palette: true,
  },
  {
    id: "panel.action.closeTerminals",
    title: "Close terminals panel",
    category: "View",
    palette: true,
  },
  {
    id: "panel.action.closeExecutionLogs",
    title: "Close execution logs",
    category: "View",
    palette: true,
  },
  {
    id: "panel.action.closeMarkdownToc",
    title: "Close table of contents",
    category: "View",
    palette: true,
  },
] as const satisfies readonly ActionDefinition[];

type MissingCatalogId = Exclude<ActionId, (typeof definitions)[number]["id"]>;
const CATALOG_IS_COMPLETE: MissingCatalogId extends never ? true : never = true;
void CATALOG_IS_COMPLETE;

export const ACTION_CATALOG: readonly ActionDefinition[] = definitions;
export const ACTIONS_BY_ID: ReadonlyMap<ActionId, ActionDefinition> = new Map(
  definitions.map((definition) => [definition.id, definition]),
);

export function getActionDefinition(id: ActionId): ActionDefinition {
  const definition = ACTIONS_BY_ID.get(id);
  if (!definition) throw new Error(`Missing action catalog entry: ${id}`);
  return definition;
}
