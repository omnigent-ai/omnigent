import { afterEach, describe, expect, it, vi } from "vitest";

import { chordKeys, type KeyChordInput } from "./chord";
import {
  activeShortcutChords,
  APP_SHORTCUT_BINDINGS,
  codeMirrorKey,
  currentShortcutEnvironment,
  matchedShortcutChordIndex,
  matchesShortcut,
  menuShortcutChords,
  OPEN_SESSION_CHORDS,
  primaryShortcutChords,
  QUICK_PICK_SHORTCUT_BINDINGS,
  SHORTCUT_BINDINGS,
  SHORTCUT_GROUPS,
  shortcutBinding,
  shortcutKeys,
  type ShortcutEnvironment,
  type ShortcutId,
  type ShortcutPageContext,
  shortcutRowKeys,
  shortcutShownKeys,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "./registry";

function press(init: Partial<KeyChordInput> & { key: string }): KeyChordInput {
  return { metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init };
}

const mac = { mac: true, native: false, desktopMenu: false };
const other = { mac: false, native: false, desktopMenu: false };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("shortcut registry", () => {
  it("looks up each binding by its id", () => {
    expect(SHORTCUT_BINDINGS.map((binding) => binding.id)).toEqual([
      "session.new",
      "session.addSibling",
      "palette.commands",
      "palette.sessions",
      "session.rename",
      "session.toggleUnread",
      "settings.open",
      "shortcuts.show",
      "tab.close",
      "window.close",
      "app.quit",
      "composer.send",
      "composer.sendWithQueue",
      "composer.newLine",
      "composer.newLineWithAlt",
      "composer.previousPrompt",
      "composer.nextPrompt",
      "approval.accept",
      "approval.decline",
      "question.next",
      "question.pickOption",
      "question.cancel",
      "model.picker",
      "model.cycle",
      "effort.cycle",
      "composer.cyclePermissionMode",
      "composer.focus",
      "dictation.toggle",
      "response.stop",
      "sessions.recent",
      "history.back",
      "history.forward",
      "session.number",
      "view.mode",
      "sidebar.left",
      "sidebar.right",
      "workspace.searchContents",
      "workspace.tab",
      "browser.newTab",
      "terminal.new",
      "tab.previous",
      "tab.next",
      "tab.reopen",
      "browser.reload",
      "browser.focusAddressBar",
      "fileViewer.exitFullScreen",
      "pane.previous",
      "pane.next",
      "terminal.splitRight",
      "terminal.splitDown",
      "slash.previous",
      "slash.next",
      "slash.apply",
      "slash.dismiss",
    ]);
    for (const binding of APP_SHORTCUT_BINDINGS) expect(shortcutBinding(binding.id)).toBe(binding);
    expect(shortcutBinding("settings.open").label).toBe("Open Settings");
  });

  it("keeps the bindings that wait for quick_pick_palette apart, in page order", () => {
    expect(QUICK_PICK_SHORTCUT_BINDINGS.map((binding) => binding.id)).toEqual([
      "session.pin",
      "session.archive",
      "session.fork",
      "message.previous",
      "message.next",
      "session.next",
      "session.previous",
      "session.recent",
      "session.recentReverse",
    ]);
    expect(APP_SHORTCUT_BINDINGS).toHaveLength(
      SHORTCUT_BINDINGS.length + QUICK_PICK_SHORTCUT_BINDINGS.length,
    );
    // The Session menu's three keep their page position, between Close window and Quit.
    const ids = APP_SHORTCUT_BINDINGS.map((binding) => binding.id);
    const pin = ids.indexOf("session.pin");
    expect(ids.slice(pin - 1, pin + 4)).toEqual([
      "window.close",
      "session.pin",
      "session.archive",
      "session.fork",
      "app.quit",
    ]);
  });

  it("matches a secondary chord without showing it, and AltGr as the transcript does", () => {
    const shiftPrevious = {
      key: "ArrowUp",
      code: "ArrowUp",
      metaKey: true,
      ctrlKey: false,
      altKey: true,
      shiftKey: true,
    };
    expect(matchesShortcut(shiftPrevious, "message.previous", mac)).toBe(true);
    expect(primaryShortcutChords("message.previous", mac)).toEqual([
      { mod: true, alt: true, key: "ArrowUp", exactKey: true, matchesAltGraph: true },
    ]);
    expect(shortcutKeys("message.previous", mac)).toEqual(["⌘", "⌥", "↑"]);
    // Firefox on macOS reports Option as AltGr; elsewhere AltGr is Ctrl+Alt typing a character.
    const altGraph = { ...shiftPrevious, shiftKey: false, altGraph: true };
    expect(matchesShortcut(altGraph, "message.previous", mac)).toBe(true);
    expect(
      matchesShortcut({ ...altGraph, metaKey: false, ctrlKey: true }, "message.previous", other),
    ).toBe(false);
    expect(
      matchesShortcut(
        { ...altGraph, metaKey: false, ctrlKey: true, altGraph: false },
        "message.previous",
        other,
      ),
    ).toBe(true);
  });

  it("gives a binding that waits for quick_pick_palette no chord another has, in any environment", () => {
    // Chords the registry already shares on purpose, each with why.
    const shared: Record<string, string> = {
      // With quick_pick_palette on, Ctrl+Tab steps sessions instead of switching recent ones.
      "session.next sessions.recent": "takes over",
      "session.previous sessions.recent": "takes over",
      // A focused file editor adds a cursor and takes the key first; the message steps yield.
      "file.addCursorAbove message.previous": "focused editor first",
      "file.addCursorBelow message.next": "focused editor first",
    };
    const environments = [true, false].flatMap((isMac) =>
      [true, false].flatMap((native) =>
        [true, false].map((desktopMenu) => ({ mac: isMac, native, desktopMenu })),
      ),
    );
    const collisions = new Set<string>();
    for (const environment of environments) {
      const owners = new Map<string, string[]>();
      for (const binding of [...APP_SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS]) {
        for (const chord of activeShortcutChords(binding.id, environment)) {
          const signature = chordKeys(chord, environment.mac).join(" ");
          owners.set(signature, [...(owners.get(signature) ?? []), binding.id]);
        }
      }
      for (const ids of owners.values()) {
        const distinct = [...new Set(ids)].toSorted();
        const flagged = distinct.some((id) => shortcutBinding(id as ShortcutId).flag);
        if (flagged && distinct.length > 1 && !(distinct.join(" ") in shared)) {
          collisions.add(distinct.join(" "));
        }
      }
    }
    expect([...collisions]).toEqual([]);
  });

  it("gives a browser the Session menu's Pin, Fork and Archive keys, and the menu its own", () => {
    const menu = { mac: true, native: true, desktopMenu: true };
    expect(activeShortcutChords("session.pin", menu)).toEqual([]);
    expect(menuShortcutChords("session.pin", menu)).toEqual([OPEN_SESSION_CHORDS.pin]);
    expect(activeShortcutChords("session.pin", mac)).toEqual([OPEN_SESSION_CHORDS.pin]);
    // Off macOS, Ctrl+Alt+P and Ctrl+Alt+F stay Go to a file and Search text in files.
    expect(activeShortcutChords("session.pin", other)).toEqual([]);
    expect(activeShortcutChords("session.fork", other)).toEqual([]);
    expect(activeShortcutChords("session.archive", other)).toEqual([OPEN_SESSION_CHORDS.archive]);
  });

  it("draws each alternative and part on a binding's own row", () => {
    const ids = new Set<string>(APP_SHORTCUT_BINDINGS.map((binding) => binding.id));
    for (const binding of APP_SHORTCUT_BINDINGS) {
      const row = binding.alternateOf ?? binding.partOf;
      if (row === undefined) continue;
      expect(ids.has(row), binding.id).toBe(true);
      const target = shortcutBinding(row as ShortcutId);
      expect(target.alternateOf ?? target.partOf, binding.id).toBeUndefined();
    }
  });

  it("starts a new session with ⌘⇧O in a browser and ⌘N in a native shell", () => {
    expect(activeShortcutChords("session.new", { native: false })).toEqual([
      { mod: true, shift: true, key: "o" },
    ]);
    expect(activeShortcutChords("session.new", { native: true })).toEqual([
      { mod: true, key: "n" },
    ]);
    expect(
      matchesShortcut(press({ key: "O", metaKey: true, shiftKey: true }), "session.new", mac),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: "O", ctrlKey: true, shiftKey: true }), "session.new", other),
    ).toBe(true);
    expect(matchesShortcut(press({ key: "n", metaKey: true }), "session.new", mac)).toBe(false);
    expect(
      matchesShortcut(press({ key: "n", metaKey: true }), "session.new", { ...mac, native: true }),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: "n", ctrlKey: true }), "session.new", {
        ...other,
        native: true,
      }),
    ).toBe(true);
  });

  it("removes selected pull requests with ⌘ or Control and Backspace on every platform", () => {
    for (const environment of [mac, other]) {
      for (const modifier of [{ metaKey: true }, { ctrlKey: true }]) {
        expect(
          matchesShortcut(
            press({ key: "Backspace", ...modifier }),
            "pullRequests.removeSelected",
            environment,
          ),
        ).toBe(true);
      }
      expect(
        matchesShortcut(press({ key: "Backspace" }), "pullRequests.removeSelected", environment),
      ).toBe(false);
    }
  });

  it("renames with the physical R key, whatever character Option types", () => {
    expect(
      matchesShortcut(
        press({ key: "®", code: "KeyR", metaKey: true, altKey: true }),
        "session.rename",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }),
        "session.rename",
        other,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "r", code: "KeyR", ctrlKey: true, altKey: true }),
        "session.rename",
        mac,
      ),
    ).toBe(false);
  });

  it("toggles unread with the physical U key, including Option's dead key, but not AltGr", () => {
    const unread = (init: Partial<KeyChordInput>, environment = other) =>
      matchesShortcut(
        press({ key: "u", code: "KeyU", ...init }),
        "session.toggleUnread",
        environment,
      );
    expect(unread({ key: "Dead", metaKey: true, altKey: true }, mac)).toBe(true);
    expect(unread({ ctrlKey: true, altKey: true })).toBe(true);
    expect(unread({ ctrlKey: true, altKey: true }, mac)).toBe(false);
    expect(unread({ ctrlKey: true })).toBe(false);
    expect(unread({ ctrlKey: true, altKey: true, shiftKey: true })).toBe(false);
    expect(unread({ ctrlKey: true, altKey: true, altGraph: true })).toBe(false);
  });

  it("leaves Settings' chord to the desktop menu when there is one", () => {
    const comma = press({ key: "≤", code: "Comma", metaKey: true, altKey: true });
    expect(matchesShortcut(comma, "settings.open", mac)).toBe(true);
    expect(activeShortcutChords("settings.open", { desktopMenu: true })).toEqual([]);
    expect(shortcutKeys("settings.open", { ...mac, desktopMenu: true })).toEqual([]);
    expect(matchesShortcut(comma, "settings.open", { ...mac, desktopMenu: true })).toBe(false);
  });

  it("leaves ⌘[ / ⌘] to the menu only on a shell that routes Go, which came after the bridge", () => {
    const back = press({ key: "[", code: "BracketLeft", metaKey: true });
    const bridgeWithoutGo = { ...mac, native: true, desktopMenu: true, goMenu: false };
    expect(matchesShortcut(back, "history.back", bridgeWithoutGo)).toBe(true);
    expect(menuShortcutChords("history.back", bridgeWithoutGo)).toEqual([]);
    const goMenu = { ...bridgeWithoutGo, goMenu: true };
    expect(matchesShortcut(back, "history.back", goMenu)).toBe(false);
    expect(menuShortcutChords("history.forward", goMenu)).toEqual([
      { mod: true, code: "BracketRight" },
    ]);
    expect(shortcutShownKeys("history.back", currentShortcutEnvironment(goMenu))).toEqual([
      "⌘",
      "[",
    ]);
    expect(shortcutShownKeys("history.back", currentShortcutEnvironment(bridgeWithoutGo))).toEqual([
      "⌘",
      "[",
    ]);
  });

  it("never matches AltGr, which reports as Control+Alt", () => {
    const altGraph = press({
      key: "r",
      code: "KeyR",
      ctrlKey: true,
      altKey: true,
      altGraph: true,
    });
    expect(matchesShortcut(altGraph, "session.rename", other)).toBe(false);
  });

  it("opens Search Text and Go to File off the Mac on their own keys, never on AltGr", () => {
    const searchText = { key: "o", code: "KeyO", ctrlKey: true, altKey: true };
    const goToFile = { key: "P", code: "KeyP", ctrlKey: true, altKey: true, shiftKey: true };
    expect(matchesShortcut(press(searchText), "palette.text", other)).toBe(true);
    expect(matchesShortcut(press(goToFile), "palette.files", other)).toBe(true);
    // AltGr+O types ó on a Polish layout, and reports as Control+Alt.
    expect(
      matchesShortcut(press({ ...searchText, key: "ó", altGraph: true }), "palette.text", other),
    ).toBe(false);
    expect(matchesShortcut(press({ ...goToFile, altGraph: true }), "palette.files", other)).toBe(
      false,
    );
  });
});

describe("General and Navigation bindings", () => {
  it("opens the palette with ⌘K and session search with ⌘⌥S, by the physical S", () => {
    expect(matchesShortcut(press({ key: "K", metaKey: true }), "palette.commands", mac)).toBe(true);
    expect(matchesShortcut(press({ key: "k", ctrlKey: true }), "palette.commands", mac)).toBe(
      false,
    );
    expect(matchesShortcut(press({ key: "k", ctrlKey: true }), "palette.commands", other)).toBe(
      true,
    );
    // Only k and K themselves: not the Kelvin sign, whose lowercase is k.
    expect(matchesShortcut(press({ key: "\u212A", metaKey: true }), "palette.commands", mac)).toBe(
      false,
    );
    expect(
      matchesShortcut(press({ key: "\u212A", ctrlKey: true }), "palette.commands", other),
    ).toBe(false);
    const sessionSearch = press({ key: "ß", code: "KeyS", metaKey: true, altKey: true });
    expect(matchesShortcut(sessionSearch, "palette.sessions", mac)).toBe(true);
    expect(matchesShortcut(sessionSearch, "palette.commands", mac)).toBe(false);
  });

  it("goes back and forward with ⌘[ and ⌘] by physical key", () => {
    const back = press({ key: "[", code: "BracketLeft", ctrlKey: true });
    expect(matchesShortcut(back, "history.back", other)).toBe(true);
    expect(matchesShortcut(back, "history.forward", other)).toBe(false);
    expect(
      matchesShortcut(
        press({ key: "]", code: "BracketRight", metaKey: true }),
        "history.forward",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "{", code: "BracketLeft", metaKey: true, shiftKey: true }),
        "history.back",
        mac,
      ),
    ).toBe(false);
  });

  it("switches recent sessions with Control+Tab on every platform, with or without Shift", () => {
    for (const environment of [mac, other]) {
      expect(
        matchesShortcut(press({ key: "Tab", ctrlKey: true }), "sessions.recent", environment),
      ).toBe(true);
      expect(
        matchesShortcut(
          press({ key: "Tab", ctrlKey: true, shiftKey: true }),
          "sessions.recent",
          environment,
        ),
      ).toBe(true);
    }
    expect(matchesShortcut(press({ key: "Tab", metaKey: true }), "sessions.recent", mac)).toBe(
      false,
    );
    for (const key of ["tab", "TAB"]) {
      expect(matchesShortcut(press({ key, ctrlKey: true }), "sessions.recent", other)).toBe(false);
    }
  });

  it("numbers sidebar sessions ⌘1–⌘9, leaving ⌘0 to Actual Size, adding Alt in a browser", () => {
    const native = { ...mac, native: true };
    expect(
      matchedShortcutChordIndex(press({ key: "1", metaKey: true }), "session.number", native),
    ).toBe(0);
    expect(
      matchedShortcutChordIndex(press({ key: "9", metaKey: true }), "session.number", native),
    ).toBe(8);
    expect(
      matchedShortcutChordIndex(press({ key: "0", metaKey: true }), "session.number", native),
    ).toBe(-1);
    expect(
      matchedShortcutChordIndex(press({ key: "1", metaKey: true }), "session.number", mac),
    ).toBe(-1);
    expect(
      matchedShortcutChordIndex(
        press({ key: "¡", code: "Digit1", metaKey: true, altKey: true }),
        "session.number",
        mac,
      ),
    ).toBe(0);
    expect(
      matchedShortcutChordIndex(
        press({ key: "º", code: "Digit0", ctrlKey: true, altKey: true, altGraph: true }),
        "session.number",
        other,
      ),
    ).toBe(-1);
  });

  it("keeps firing under AltGr for the chords that never checked for it", () => {
    const altGraph = { altGraph: true };
    expect(
      matchesShortcut(press({ key: "/", ctrlKey: true, ...altGraph }), "shortcuts.show", other),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "[", code: "BracketLeft", ctrlKey: true, ...altGraph }),
        "history.back",
        other,
      ),
    ).toBe(true);
    expect(
      matchedShortcutChordIndex(press({ key: "2", ctrlKey: true, ...altGraph }), "session.number", {
        ...other,
        native: true,
      }),
    ).toBe(1);
    expect(
      matchesShortcut(press({ key: "k", ctrlKey: true, ...altGraph }), "palette.commands", other),
    ).toBe(false);
    expect(
      matchesShortcut(press({ key: "Tab", ctrlKey: true, ...altGraph }), "sessions.recent", other),
    ).toBe(false);
  });
});

describe("View bindings", () => {
  it("toggles Chat / Terminal with ⌘⌥\\ by physical key", () => {
    const toggle = press({ key: "«", code: "Backslash", metaKey: true, altKey: true });
    expect(matchesShortcut(toggle, "view.mode", mac)).toBe(true);
    expect(matchesShortcut({ ...toggle, shiftKey: true }, "view.mode", mac)).toBe(false);
  });

  it("toggles the sidebars with ⌘B / ⇧⌘B natively and ⌘⌥[ / ⌘⌥] in a browser", () => {
    const native = { ...mac, native: true };
    expect(matchesShortcut(press({ key: "b", metaKey: true }), "sidebar.left", native)).toBe(true);
    expect(
      matchesShortcut(press({ key: "B", metaKey: true, shiftKey: true }), "sidebar.right", native),
    ).toBe(true);
    expect(matchesShortcut(press({ key: "b", metaKey: true }), "sidebar.left", mac)).toBe(false);
    const left = press({ key: "“", code: "BracketLeft", metaKey: true, altKey: true });
    expect(matchesShortcut(left, "sidebar.left", mac)).toBe(true);
    expect(matchesShortcut(left, "sidebar.left", native)).toBe(false);
    expect(
      matchesShortcut(
        press({ key: "]", code: "BracketRight", ctrlKey: true, altKey: true }),
        "sidebar.right",
        other,
      ),
    ).toBe(true);
  });

  it("picks workspace tabs 1–9 with Control on macOS and Alt elsewhere", () => {
    const control = press({ key: "1", code: "Digit1", ctrlKey: true });
    const alt = press({ key: "¡", code: "Digit1", altKey: true });
    expect(matchedShortcutChordIndex(control, "workspace.tab", mac)).toBe(0);
    expect(matchedShortcutChordIndex(alt, "workspace.tab", mac)).toBe(-1);
    expect(matchedShortcutChordIndex(alt, "workspace.tab", other)).toBe(0);
    expect(matchedShortcutChordIndex(control, "workspace.tab", other)).toBe(-1);
    expect(
      matchedShortcutChordIndex(
        press({ key: "9", code: "Digit9", ctrlKey: true }),
        "workspace.tab",
        mac,
      ),
    ).toBe(8);
    expect(
      matchedShortcutChordIndex(
        press({ key: "0", code: "Digit0", ctrlKey: true }),
        "workspace.tab",
        mac,
      ),
    ).toBe(-1);
  });

  it("opens a new shell with literal Control+` on every platform, even under AltGr", () => {
    const shell = press({ key: "`", code: "Backquote", ctrlKey: true });
    expect(matchesShortcut(shell, "terminal.new", mac)).toBe(true);
    expect(matchesShortcut(shell, "terminal.new", other)).toBe(true);
    expect(matchesShortcut({ ...shell, altGraph: true }, "terminal.new", other)).toBe(true);
    expect(
      matchesShortcut(press({ key: "`", code: "Backquote", metaKey: true }), "terminal.new", mac),
    ).toBe(false);
  });
});

describe("Chat and Pane bindings", () => {
  it("keeps the composer chords on literal Control, even on macOS", () => {
    for (const environment of [mac, other]) {
      expect(
        matchesShortcut(
          press({ key: "M", code: "KeyM", ctrlKey: true, shiftKey: true }),
          "model.picker",
          environment,
        ),
      ).toBe(true);
      expect(
        matchesShortcut(
          press({ key: "µ", code: "KeyM", ctrlKey: true, altKey: true }),
          "model.cycle",
          environment,
        ),
      ).toBe(true);
      expect(
        matchesShortcut(
          press({ key: "e", code: "KeyE", ctrlKey: true, altKey: true }),
          "effort.cycle",
          environment,
        ),
      ).toBe(true);
      expect(
        matchesShortcut(
          press({ key: "L", code: "KeyL", ctrlKey: true, shiftKey: true }),
          "composer.focus",
          environment,
        ),
      ).toBe(true);
    }
    expect(
      matchesShortcut(
        press({ key: "M", code: "KeyM", metaKey: true, shiftKey: true }),
        "model.picker",
        mac,
      ),
    ).toBe(false);
  });

  it("toggles dictation with ⌘⌥V by physical key", () => {
    const dictation = press({ key: "√", code: "KeyV", metaKey: true, altKey: true });
    expect(matchesShortcut(dictation, "dictation.toggle", mac)).toBe(true);
    expect(matchesShortcut(dictation, "dictation.toggle", other)).toBe(false);
    const ctrlAltV = { key: "v", code: "KeyV", ctrlKey: true, altKey: true };
    expect(matchesShortcut(press(ctrlAltV), "dictation.toggle", other)).toBe(true);
    // AltGr reports as Ctrl+Alt, so AltGr+V types a character instead.
    expect(matchesShortcut(press({ ...ctrlAltV, altGraph: true }), "dictation.toggle", other)).toBe(
      false,
    );
  });

  it("accepts with ⌘↵ and declines with a bare Esc, comparing the key exactly", () => {
    expect(matchesShortcut(press({ key: "Enter", metaKey: true }), "approval.accept", mac)).toBe(
      true,
    );
    expect(matchesShortcut(press({ key: "Enter", ctrlKey: true }), "approval.accept", other)).toBe(
      true,
    );
    expect(matchesShortcut(press({ key: "Enter", ctrlKey: true }), "approval.accept", mac)).toBe(
      false,
    );
    expect(matchesShortcut(press({ key: "enter", metaKey: true }), "approval.accept", mac)).toBe(
      false,
    );
    expect(
      matchesShortcut(
        press({ key: "Enter", ctrlKey: true, shiftKey: true }),
        "approval.accept",
        other,
      ),
    ).toBe(false);
    expect(matchesShortcut(press({ key: "Escape" }), "approval.decline", mac)).toBe(true);
    expect(matchesShortcut(press({ key: "Escape", shiftKey: true }), "approval.decline", mac)).toBe(
      false,
    );
    expect(
      matchesShortcut(press({ key: "Escape", altGraph: true }), "approval.decline", other),
    ).toBe(true);
  });

  it("cycles panes with ⌘⌥⇧[ / ⌘⌥⇧] and splits with ⌘⌥⇧\\ and ⌘⌥-", () => {
    expect(
      matchesShortcut(
        press({ key: "”", code: "BracketLeft", metaKey: true, altKey: true, shiftKey: true }),
        "pane.previous",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "}", code: "BracketRight", ctrlKey: true, altKey: true, shiftKey: true }),
        "pane.next",
        other,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "|", code: "Backslash", metaKey: true, altKey: true, shiftKey: true }),
        "terminal.splitRight",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "–", code: "Minus", metaKey: true, altKey: true }),
        "terminal.splitDown",
        mac,
      ),
    ).toBe(true);
    expect(
      matchesShortcut(
        press({ key: "—", code: "Minus", metaKey: true, altKey: true, shiftKey: true }),
        "terminal.splitDown",
        mac,
      ),
    ).toBe(false);
  });
});

describe("currentShortcutEnvironment", () => {
  it("prefers overrides and reads the platform only when asked", () => {
    const platform = vi.fn(() => "MacIntel");
    vi.stubGlobal("navigator", {
      get platform() {
        return platform();
      },
    });
    const environment = currentShortcutEnvironment({ native: true });
    expect(platform).not.toHaveBeenCalled();
    expect(environment.native).toBe(true);
    expect(environment.mac).toBe(true);
    expect(environment.mac).toBe(true);
    expect(platform).toHaveBeenCalledTimes(1);
    // A resolved environment passes through unchanged.
    expect(currentShortcutEnvironment(environment)).toBe(environment);
  });
});

describe("Keyboard shortcuts page metadata", () => {
  it("lists every binding, the sidebar's too, under one of the page's groups", () => {
    const groups = new Set(SHORTCUT_GROUPS.map((group) => group.id));
    expect(groups.size).toBe(SHORTCUT_GROUPS.length);
    for (const binding of [...SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS]) {
      expect(groups.has(binding.group), binding.id).toBe(true);
    }
  });

  const page: ShortcutPageContext = {
    native: false,
    electron: false,
    browser: false,
    submitWithModEnter: false,
    preventsKeyboardSubmit: false,
    contentSearch: false,
    grids: false,
    splitTerminal: false,
    siblingSessions: false,
    desktopCommands: false,
    alwaysSteer: false,
  };
  const resolved = (environment: Partial<ShortcutEnvironment>) =>
    currentShortcutEnvironment({ submitWithModEnter: false, ...environment });

  it("shows a numbered binding's row as a range of digits", () => {
    const native = resolved({ mac: true, native: true, desktopMenu: false });
    expect(shortcutRowKeys("session.number", native, page)).toEqual(["⌘", "1…9"]);
    expect(shortcutRowKeys("workspace.tab", resolved(mac), page)).toEqual(["⌃", "1…4"]);
    expect(
      shortcutRowKeys("workspace.tab", resolved(mac), { ...page, contentSearch: true }),
    ).toEqual(["⌃", "1…5"]);
  });

  it("draws its own row keys only where a row needs them, and others their chord's keys", () => {
    const drawn = SHORTCUT_BINDINGS.filter((binding) => binding.rowKeys !== undefined);
    expect(drawn.map((binding) => binding.id)).toEqual([
      "session.new",
      "question.pickOption",
      "session.number",
      "workspace.tab",
      "slash.previous",
    ]);
    expect(shortcutRowKeys("palette.commands", resolved(mac), page)).toEqual(["⌘", "K"]);
  });

  it("shows a desktop menu's accelerator only where the menu owns the action", () => {
    const menu = resolved({ ...mac, desktopMenu: true });
    expect(shortcutRowKeys("tab.close", menu, page)).toEqual(["⌘", "W"]);
    expect(shortcutRowKeys("tab.close", resolved(mac), page)).toEqual([]);
    // Settings keeps its own chord in a browser and shows the menu's ⌘, in the desktop app.
    expect(shortcutRowKeys("settings.open", menu, page)).toEqual(["⌘", ","]);
    expect(shortcutRowKeys("settings.open", resolved(mac), page)).toEqual(["⌘", "⌥", ","]);
  });
});

describe("codeMirrorKey", () => {
  it("keeps a named key's case and lowercases a character, as CodeMirror's names need", () => {
    expect(codeMirrorKey({ mod: true, code: "Enter" })).toBe("Mod-Enter");
    expect(codeMirrorKey({ alt: true, code: "ArrowUp" })).toBe("Alt-ArrowUp");
    expect(codeMirrorKey({ mod: true, shift: true, code: "KeyF" })).toBe("Mod-Shift-f");
    expect(codeMirrorKey({ mod: true, code: "Slash" })).toBe("Mod-/");
    expect(codeMirrorKey({ ctrl: true, key: "Escape" })).toBe("Ctrl-Escape");
  });
});
