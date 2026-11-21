import { act, cleanup, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { adjacentStripEntry, useWorkspaceTabStepping } from "./useWorkspaceTabStepping";
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
  const pressChord = async (direction: "previous" | "next", target: Element = document.body) => {
    await act(async () => {
      target.dispatchEvent(
        new KeyboardEvent("keydown", {
          code: direction === "next" ? "BracketRight" : "BracketLeft",
          key: direction === "next" ? "}" : "{",
          metaKey: platform.mac,
          ctrlKey: !platform.mac,
          shiftKey: true,
          bubbles: true,
        }),
      );
      // Focus moves on the next frame.
      await new Promise((resolve) => {
        requestAnimationFrame(() => resolve(undefined));
      });
    });
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

  it("has no chord in a browser, which keeps those keys for its own tabs", async () => {
    platform.native = false;
    render(<Strip entryIds={["files", "terminal:zsh", "browser:1"]} />);
    await pressChord("next");
    expect(pressed()).toBe("terminal:zsh");
  });
});
