import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));

import { type ShortcutId, shortcutKeys, APP_SHORTCUT_BINDINGS } from "@/lib/shortcuts/registry";

import { type ActionCommand, filterAppCommands, useAppCommands } from "./appCommands";
import { type OpenSessionCommandId, openSessionCommands } from "./openSessionCommands";

function commands(rightSidebarShortcutEnabled = true): ActionCommand[] {
  return renderHook(() =>
    useAppCommands({
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
      rightSidebarShortcutEnabled,
    }),
  ).result.current;
}

describe("useAppCommands", () => {
  it("lists the app's commands in palette order", () => {
    expect(commands().map((command) => command.label)).toEqual([
      "New chat",
      "Go to Inbox",
      "Go to Automations",
      "Go to Settings",
      "Search settings…",
      "Toggle conversations sidebar",
      "Toggle workspace sidebar",
    ]);
  });

  beforeEach(() => navigate.mockReset());

  it("navigates for the Go to commands", () => {
    const byId = new Map(commands().map((command) => [command.id, command]));
    byId.get("go-inbox")!.run();
    byId.get("go-settings")!.run();
    expect(navigate.mock.calls).toEqual([["/inbox"], ["/settings"]]);
  });

  it("drops the workspace sidebar's chord where the route owns it", () => {
    const toggle = (enabled: boolean) =>
      commands(enabled).find((command) => command.id === "toggle-right-sidebar")!;
    expect(toggle(true).shortcut).toBeDefined();
    expect(toggle(false).shortcut).toBeUndefined();
  });
});

describe("filterAppCommands", () => {
  const all = commands();

  it("returns every command for a blank query", () => {
    expect(filterAppCommands(all, "  ")).toBe(all);
  });

  it("lists search-only commands only for a matching nonblank query", () => {
    const preview = {
      ...all[0]!,
      id: "preview",
      label: "Try something new",
      keywords: ["feature preview"],
      searchOnly: true,
    };
    const withPreview = [...all, preview];
    expect(filterAppCommands(withPreview, "  ")).toEqual(all);
    expect(filterAppCommands(withPreview, "preview")).toEqual([preview]);
    expect(filterAppCommands(withPreview, "unmatched")).toEqual([]);
  });

  it("matches the label or a keyword, ignoring case and outer spaces", () => {
    expect(filterAppCommands(all, " SETT ").map((command) => command.id)).toEqual([
      "go-settings",
      "settings-search",
    ]);
    expect(filterAppCommands(all, "cron").map((command) => command.id)).toEqual(["go-tasks"]);
    expect(filterAppCommands(all, "zzz")).toEqual([]);
  });
});

/** Registry hotkeys whose action ⌘K also lists, by the command's id. */
const HOTKEY_COMMANDS: Partial<Record<ShortcutId, string>> = {
  "session.new": "new-chat",
  "settings.open": "go-settings",
  "sidebar.left": "toggle-left-sidebar",
  "sidebar.right": "toggle-right-sidebar",
};

/** Registry hotkeys whose action ⌘K lists among the open session's commands, by the command's id. */
const SESSION_HOTKEY_COMMANDS: Partial<Record<ShortcutId, OpenSessionCommandId>> = {
  "session.addSibling": "add-nested-agent",
  "session.pin": "pin",
  "session.fork": "fork",
  "session.archive": "archive",
  "session.rename": "rename",
};

/** Every open-session command, as an owner on the desktop sees them. */
const SESSION_COMMANDS = openSessionCommands({
  canAddAgent: true,
  pinnable: true,
  pinned: false,
  pinSaving: false,
  canShare: true,
  canFork: true,
  canMarkUnread: true,
  canCreateAutomation: true,
  archived: false,
  desktopShortcuts: true,
});

const NOT_YET = "Not in ⌘K yet.";
const WHERE_PRESSED = "Acts on what has focus or is on screen, so it has no ⌘K row.";
const MENU_ITEM = "A desktop menu item, which runs it with the menu's accelerator.";
const QUICK_PICK_HOST = "Waits for quick_pick_palette, whose ⌘K command its shortcut host runs.";

/** Registry hotkeys with no ⌘K command, and why. */
const HOTKEYS_WITHOUT_COMMANDS: Partial<Record<ShortcutId, string>> = {
  "palette.commands": "Opens ⌘K itself.",
  "palette.sessions": "Opens ⌘K's own session search.",
  "shortcuts.show": NOT_YET,
  "session.toggleUnread": "⌘K marks the session unread, but has no command to mark it read.",
  "history.back": NOT_YET,
  "history.forward": NOT_YET,
  "sessions.recent": "Switches while Control is held.",
  "message.previous": QUICK_PICK_HOST,
  "message.next": QUICK_PICK_HOST,
  "session.next": QUICK_PICK_HOST,
  "session.previous": QUICK_PICK_HOST,
  "session.recent": QUICK_PICK_HOST,
  "session.recentReverse": QUICK_PICK_HOST,
  "session.number": "Picks a sidebar row by its position.",
  "view.mode": NOT_YET,
  "workspace.tab": "Picks a workspace tab by its position.",
  "terminal.new": NOT_YET,
  "model.picker": WHERE_PRESSED,
  "model.cycle": WHERE_PRESSED,
  "effort.cycle": WHERE_PRESSED,
  "composer.focus": WHERE_PRESSED,
  "dictation.toggle": WHERE_PRESSED,
  "approval.accept": WHERE_PRESSED,
  "approval.decline": WHERE_PRESSED,
  "pane.previous": WHERE_PRESSED,
  "pane.next": WHERE_PRESSED,
  "terminal.splitRight": WHERE_PRESSED,
  "terminal.splitDown": WHERE_PRESSED,
  "composer.send": WHERE_PRESSED,
  "composer.sendWithQueue": WHERE_PRESSED,
  "composer.newLine": WHERE_PRESSED,
  "composer.newLineWithAlt": WHERE_PRESSED,
  "composer.previousPrompt": WHERE_PRESSED,
  "composer.nextPrompt": WHERE_PRESSED,
  "composer.cyclePermissionMode": WHERE_PRESSED,
  "response.stop": WHERE_PRESSED,
  "slash.previous": WHERE_PRESSED,
  "slash.next": WHERE_PRESSED,
  "slash.apply": WHERE_PRESSED,
  "slash.dismiss": WHERE_PRESSED,
  "question.pickOption": WHERE_PRESSED,
  "question.next": WHERE_PRESSED,
  "question.cancel": WHERE_PRESSED,
  "fileViewer.exitFullScreen": WHERE_PRESSED,
  "tab.close": MENU_ITEM,
  "window.close": MENU_ITEM,
  "tab.next": MENU_ITEM,
  "tab.previous": MENU_ITEM,
  "tab.reopen": MENU_ITEM,
  "browser.focusAddressBar": MENU_ITEM,
  "browser.newTab": MENU_ITEM,
  "browser.reload": MENU_ITEM,
  "app.quit": MENU_ITEM,
  "workspace.searchContents": NOT_YET,
};

// The New chat row spells out Shift off macOS, as its tooltip does.
const sameKeys = (keys: readonly string[]) => keys.map((key) => (key === "Shift" ? "⇧" : key));

describe("registry hotkeys in ⌘K", () => {
  const all = commands();
  const byId = new Map(all.map((command) => [command.id, command]));

  it("lists every registry hotkey exactly once, as a command or a known gap", () => {
    const listed = [
      ...Object.keys(HOTKEY_COMMANDS),
      ...Object.keys(SESSION_HOTKEY_COMMANDS),
      ...Object.keys(HOTKEYS_WITHOUT_COMMANDS),
    ];
    expect(listed.toSorted()).toEqual(
      APP_SHORTCUT_BINDINGS.map((binding) => binding.id).toSorted(),
    );
  });

  it("shows each mapped hotkey's chord on its command", () => {
    for (const [id, commandId] of Object.entries(HOTKEY_COMMANDS)) {
      const command = byId.get(commandId!);
      expect(command, commandId).toBeDefined();
      expect(sameKeys(command!.shortcut ?? []), id).toEqual(shortcutKeys(id as ShortcutId));
    }
  });

  it("finds each mapped session hotkey among the open session's commands", () => {
    const sessionIds = SESSION_COMMANDS.map((command) => command.id);
    for (const [id, commandId] of Object.entries(SESSION_HOTKEY_COMMANDS)) {
      expect(sessionIds, id).toContain(commandId);
    }
  });

  it("maps every command that shows a chord to its hotkey", () => {
    const mapped = new Set(Object.values(HOTKEY_COMMANDS));
    const withChords = all.filter((command) => command.shortcut !== undefined);
    expect(withChords.map((command) => command.id).filter((id) => !mapped.has(id))).toEqual([]);
  });

  it("drops a gap once ⌘K gains a command for that hotkey", () => {
    for (const id of Object.keys(HOTKEYS_WITHOUT_COMMANDS) as ShortcutId[]) {
      const binding = APP_SHORTCUT_BINDINGS.find((candidate) => candidate.id === id)!;
      const keys = shortcutKeys(id);
      const match = all.find(
        (command) =>
          command.label === binding.label ||
          (keys.length > 0 &&
            command.shortcut !== undefined &&
            sameKeys(command.shortcut).join() === keys.join()),
      );
      expect(match?.id, id).toBeUndefined();
      const sessionMatch = SESSION_COMMANDS.find((command) => command.label === binding.label);
      expect(sessionMatch?.id, id).toBeUndefined();
    }
  });
});
