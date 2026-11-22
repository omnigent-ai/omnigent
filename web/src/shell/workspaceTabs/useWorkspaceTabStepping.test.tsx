import { act, cleanup, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { adjacentStripEntry, useWorkspaceTabStepping } from "./useWorkspaceTabStepping";
import { resetDesktopCommandsForTests } from "@/lib/desktopCommands";
import type * as Hotkeys from "@/lib/hotkeys";
import type * as ShortcutRegistry from "@/lib/shortcuts/registry";

const platform = vi.hoisted(() => ({ mac: true, native: false }));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof Hotkeys>()),
  isMacPlatform: () => platform.mac,
}));
vi.mock("@/lib/shortcuts/registry", async (importOriginal) => {
  const registry = await importOriginal<typeof ShortcutRegistry>();
  return {
    ...registry,
    currentShortcutEnvironment: (overrides = {}) =>
      registry.currentShortcutEnvironment({ native: platform.native, ...overrides }),
  };
});

describe("adjacentStripEntry", () => {
  const strip = ["files", "terminal:zsh", "browser:1", "file:README.md"];

  it("steps in strip order and wraps at both ends", () => {
    expect(adjacentStripEntry(strip, "terminal:zsh", 1)).toBe("browser:1");
    expect(adjacentStripEntry(strip, "terminal:zsh", -1)).toBe("files");
    expect(adjacentStripEntry(strip, "file:README.md", 1)).toBe("files");
    expect(adjacentStripEntry(strip, "files", -1)).toBe("file:README.md");
  });

  it("starts at the first entry going forward and the last going back when none is selected", () => {
    expect(adjacentStripEntry(strip, null, 1)).toBe("files");
    expect(adjacentStripEntry(strip, null, -1)).toBe("file:README.md");
    expect(adjacentStripEntry(strip, "gone", 1)).toBe("files");
  });

  it("stays on the only entry and has nothing to step to in an empty strip", () => {
    expect(adjacentStripEntry(["files"], "files", 1)).toBe("files");
    expect(adjacentStripEntry([], null, 1)).toBeNull();
  });
});

describe("useWorkspaceTabStepping", () => {
  // The chords belong to a native shell, where a browser's own tab keys don't compete.
  beforeEach(() => {
    platform.native = true;
  });
  afterEach(() => {
    cleanup();
    platform.mac = true;
    platform.native = false;
  });

  /** A strip of buttons tagged as the panel tags them, with the hook stepping its selection. */
  function Strip({ entryIds, enabled = true }: { entryIds: string[]; enabled?: boolean }) {
    const [selected, setSelected] = useState<string | null>(entryIds[1] ?? null);
    useWorkspaceTabStepping({ entryIds, selectedId: selected, onSelect: setSelected, enabled });
    return (
      <div>
        {entryIds.map((id) => (
          <button key={id} type="button" data-workspace-tab-id={id} aria-pressed={id === selected}>
            {id}
          </button>
        ))}
      </div>
    );
  }

  const pressed = () => document.querySelector('[aria-pressed="true"]')?.textContent;

  /** ⇧⌘] / [ on the Mac, Ctrl+Shift+] / [ elsewhere, as the page sees them. */
  const pressChord = async (
    direction: "previous" | "next",
    target: Element = document.body,
    { repeat = false }: { repeat?: boolean } = {},
  ) => {
    const event = new KeyboardEvent("keydown", {
      code: direction === "next" ? "BracketRight" : "BracketLeft",
      key: direction === "next" ? "}" : "{",
      metaKey: platform.mac,
      ctrlKey: !platform.mac,
      shiftKey: true,
      repeat,
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      target.dispatchEvent(event);
      // Focus moves on the next frame.
      await new Promise((resolve) => {
        requestAnimationFrame(() => resolve(undefined));
      });
    });
    return event;
  };

  it.each([true, false])(
    "steps the strip, wrapping, and focuses the tab (Mac: %s)",
    async (mac) => {
      platform.mac = mac;
      render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);

      await pressChord("next");
      expect(pressed()).toBe("browser:1");
      expect(document.activeElement).toHaveTextContent("browser:1");
      await pressChord("next");
      expect(pressed()).toBe("files");
      await pressChord("previous");
      expect(pressed()).toBe("browser:1");
    },
  );

  it.each([".cm-editor", ".monaco-editor"])(
    "leaves the chord to a focused %s, where it folds or unfolds",
    async (editorClass) => {
      render(
        <>
          <Strip entryIds={["files", "terminal:zsh", "browser:1"]} />
          <div className={editorClass.slice(1)}>
            <textarea aria-label="editor" />
          </div>
        </>,
      );
      const editor = document.querySelector("textarea")!;
      editor.focus();

      await pressChord("next", editor);
      expect(pressed()).toBe("terminal:zsh");
      expect(document.activeElement).toBe(editor);
    },
  );

  it("leaves the chord alone while disabled", async () => {
    render(<Strip entryIds={["files", "terminal:zsh"]} enabled={false} />);
    await pressChord("next");
    expect(pressed()).toBe("terminal:zsh");
  });

  describe("in a desktop app with the Go menu", () => {
    type CommandListener = (event: { command: string; browserViewId: string | null }) => void;
    let sendCommand: CommandListener = () => {};
    let reported: string[] = [];
    beforeEach(() => {
      Object.assign(window, {
        omnigentDesktop: {
          kind: "electron",
          onDesktopCommand: (callback: CommandListener) => {
            sendCommand = callback;
            return () => {};
          },
          setDesktopCommandState: (state: { commands: string[] }) => {
            reported = state.commands;
          },
        },
      });
    });
    afterEach(() => {
      resetDesktopCommandsForTests();
      delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
      reported = [];
    });

    const menu = async (command: "previous-tab" | "next-tab") => {
      await act(async () => {
        sendCommand({ command, browserViewId: null });
        await new Promise((resolve) => {
          requestAnimationFrame(() => resolve(undefined));
        });
      });
    };

    it("enables Previous and Next Tab, whose commands from the desktop menu step the strip", async () => {
      render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
      await act(async () => {});
      expect(reported).toEqual(expect.arrayContaining(["next-tab", "previous-tab"]));

      await menu("next-tab");
      expect(pressed()).toBe("browser:1");
      expect(document.activeElement).toHaveTextContent("browser:1");
      await menu("previous-tab");
      expect(pressed()).toBe("terminal:zsh");
    });

    it("steps once on the chord and keeps it from the menu, which gets only unhandled keys", async () => {
      render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
      const event = await pressChord("next");
      expect(pressed()).toBe("browser:1");
      expect(event.defaultPrevented).toBe(true);
    });

    it("steps from the chord on an older shell whose menu sends no commands, and enables neither", async () => {
      const setDesktopCommandState = vi.fn<(state: { commands: string[] }) => void>();
      Object.assign(window, { omnigentDesktop: { kind: "electron", setDesktopCommandState } });
      render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
      await act(async () => {});
      const enabled = setDesktopCommandState.mock.calls.flatMap(([state]) => state.commands);
      expect(enabled).not.toContain("next-tab");
      expect(enabled).not.toContain("previous-tab");
      await pressChord("next");
      expect(pressed()).toBe("browser:1");
      await pressChord("next");
      expect(pressed()).toBe("files");
    });

    it("neither steps nor handles the chord while a code editor has focus", async () => {
      render(
        <>
          <Strip entryIds={["files", "terminal:zsh", "browser:1"]} />
          <div className="cm-editor">
            <textarea aria-label="editor" />
          </div>
        </>,
      );
      const editor = document.querySelector("textarea")!;
      editor.focus();
      const event = await pressChord("next", editor);
      expect(pressed()).toBe("terminal:zsh");
      expect(event.defaultPrevented).toBe(false);
    });

    it("steps for every repeated press, from the chord with autorepeat and from the menu", async () => {
      render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
      await pressChord("next");
      await pressChord("next", document.body, { repeat: true });
      expect(pressed()).toBe("files");
      await menu("next-tab");
      await menu("next-tab");
      expect(pressed()).toBe("browser:1");
    });

    it("keeps the menu items disabled while stepping is off", async () => {
      render(<Strip entryIds={["files", "terminal:zsh"]} enabled={false} />);
      await act(async () => {});
      expect(reported).not.toContain("next-tab");
      expect(reported).not.toContain("previous-tab");
    });
  });

  it("has no chord in a browser, which keeps those keys for its own tabs", async () => {
    platform.native = false;
    render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
    await pressChord("next");
    expect(pressed()).toBe("terminal:zsh");
  });
});
