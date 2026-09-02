import type { ActionDefinition, ActionId, ArglessActionId } from "./types";

const definitions = [
  {
    id: "workbench.action.showCommands",
    title: "Open command palette",
    category: "General",
    shortcutReference: true,
    keywords: ["commands", "search"],
    icon: "Search",
    palette: false,
  },
  {
    id: "workbench.action.openKeyboardShortcuts",
    title: "Open keyboard shortcuts",
    category: "General",
    shortcutReference: true,
    keywords: ["keybindings", "hotkeys"],
    icon: "Keyboard",
    palette: false,
    paletteOrder: 70,
  },
  {
    id: "workbench.action.navigateInbox",
    title: "Go to Inbox",
    category: "Navigation",
    keywords: ["notifications", "comments", "needs response"],
    icon: "Inbox",
    palette: true,
    paletteOrder: 20,
  },
  {
    id: "workbench.action.navigateAutomations",
    title: "Go to Automations",
    category: "Navigation",
    keywords: ["scheduled", "recurring", "tasks", "cron"],
    icon: "CalendarClock",
    palette: true,
    paletteOrder: 30,
  },
  {
    id: "workbench.action.navigateSettings",
    title: "Go to Settings",
    category: "Navigation",
    keywords: ["preferences", "configuration", "account"],
    icon: "Settings",
    palette: true,
    paletteOrder: 40,
  },
  {
    id: "workbench.action.toggleConversationsSidebar",
    title: "Toggle conversations sidebar",
    category: "View",
    shortcutReference: true,
    keywords: ["panel", "left", "sessions", "sessions list"],
    icon: "PanelLeft",
    palette: true,
    paletteOrder: 50,
  },
  {
    id: "workbench.action.toggleWorkspaceSidebar",
    title: "Toggle workspace sidebar",
    category: "View",
    shortcutReference: true,
    keywords: ["panel", "right", "files", "terminal"],
    icon: "PanelRight",
    palette: true,
    paletteOrder: 60,
  },
  {
    id: "session.action.new",
    title: "New chat",
    category: "General",
    shortcutReference: true,
    keywords: ["chat", "compose", "start", "new session"],
    icon: "SquarePen",
    palette: true,
    paletteOrder: 10,
  },
  {
    id: "session.action.openPrevious",
    title: "Open previous session",
    category: "Navigation",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "session.action.openNext",
    title: "Open next session",
    category: "Navigation",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "session.action.openPinned",
    title: "Open pinned session",
    category: "Navigation",
    shortcutReference: true,
    keywords: ["jump", "slot"],
    palette: false,
  },
  {
    id: "chat.action.acceptApproval",
    title: "Accept approval prompt",
    category: "Chat",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "chat.action.openPreviousMessage",
    title: "Go to previous message",
    category: "Chat",
    palette: false,
  },
  {
    id: "chat.action.openNextMessage",
    title: "Go to next message",
    category: "Chat",
    palette: false,
  },
  {
    id: "composer.action.send",
    title: "Send message",
    category: "Composer",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "composer.action.stop",
    title: "Stop response",
    category: "Composer",
    palette: false,
  },
  {
    id: "composer.action.recallPrevious",
    title: "Recall previous prompt",
    category: "Composer",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "composer.action.recallNext",
    title: "Recall next prompt",
    category: "Composer",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "composer.action.selectPreviousSuggestion",
    title: "Select previous suggestion",
    category: "Composer",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "composer.action.selectNextSuggestion",
    title: "Select next suggestion",
    category: "Composer",
    shortcutReference: true,
    palette: false,
  },
  {
    id: "composer.action.acceptSuggestion",
    title: "Accept suggestion",
    category: "Composer",
    shortcutReference: true,
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
    shortcutReference: true,
    palette: false,
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
    palette: false,
  },
  {
    id: "file.action.save",
    title: "Save file",
    category: "Files",
    palette: false,
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
    palette: false,
  },
  {
    id: "file.action.openNextChanged",
    title: "Open next changed file",
    category: "Files",
    palette: false,
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
    palette: false,
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
    palette: false,
  },
  {
    id: "panel.action.closeTerminals",
    title: "Close terminals panel",
    category: "View",
    palette: false,
  },
  {
    id: "panel.action.closeExecutionLogs",
    title: "Close execution logs",
    category: "View",
    palette: false,
  },
  {
    id: "panel.action.closeMarkdownToc",
    title: "Close table of contents",
    category: "View",
    palette: false,
  },
] as const satisfies readonly ActionDefinition[];

type MissingCatalogId = Exclude<ActionId, (typeof definitions)[number]["id"]>;
type PaletteCatalogId = Extract<(typeof definitions)[number], { palette: true }>["id"];
type InvalidPaletteAction = Exclude<PaletteCatalogId, ArglessActionId>;
const CATALOG_IS_COMPLETE: MissingCatalogId extends never ? true : never = true;
const PALETTE_ACTIONS_ARE_ARGLESS: InvalidPaletteAction extends never ? true : never = true;
void CATALOG_IS_COMPLETE;
void PALETTE_ACTIONS_ARE_ARGLESS;

export const ACTION_CATALOG: readonly ActionDefinition[] = definitions;
export const ACTIONS_BY_ID: ReadonlyMap<ActionId, ActionDefinition> = new Map(
  definitions.map((definition) => [definition.id, definition]),
);

export function getActionDefinition(id: ActionId): ActionDefinition {
  const definition = ACTIONS_BY_ID.get(id);
  if (!definition) throw new Error(`Missing action catalog entry: ${id}`);
  return definition;
}
