import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import type { MenuItemConstructorOptions } from "electron";

import {
  createContextMenu,
  type ContextMenuHitTest,
  type ContextMenuWindow,
} from "../src/context_menu.ts";

class FakeContents extends EventEmitter {
  replaced: string[] = [];
  learned: string[] = [];
  session = {
    addWordToSpellCheckerDictionary: (word: string) => {
      this.learned.push(word);
      return true;
    },
  };
  replaceMisspelling(word: string) {
    this.replaced.push(word);
  }
}

class FakeWindow implements ContextMenuWindow {
  webContents = new FakeContents();
}

function harness() {
  const popups: { template: MenuItemConstructorOptions[]; window: FakeWindow }[] = [];
  const copied: string[] = [];
  const menu = createContextMenu<FakeWindow>({
    Menu: {
      buildFromTemplate: (template) => ({
        popup: ({ window }) => popups.push({ template, window }),
      }),
    },
    clipboard: { writeText: (text) => copied.push(text) },
  });
  const win = new FakeWindow();
  menu.attach(win);
  const rightClick = (params: Partial<ContextMenuHitTest>) => {
    win.webContents.emit("context-menu", {}, {
      misspelledWord: "",
      dictionarySuggestions: [],
      linkURL: "",
      isEditable: false,
      selectionText: "",
      editFlags: { canCut: true, canCopy: false, canPaste: true, canSelectAll: false },
      ...params,
    } satisfies ContextMenuHitTest);
    return popups.at(-1);
  };
  return { win, popups, copied, rightClick };
}

/** Click an item the way the menu would; these handlers read none of their arguments. */
const click = (item: MenuItemConstructorOptions | undefined) =>
  (item?.click as (() => void) | undefined)?.();

/** A template as plain data: labels, roles, types and enabled flags. */
const shape = (template: MenuItemConstructorOptions[] | undefined) =>
  template?.map(({ label, role, type, enabled }) =>
    Object.fromEntries(
      Object.entries({ label, role, type, enabled }).filter(([, value]) => value !== undefined),
    ),
  );

describe("page context menu", () => {
  it("shows nothing over dead space or a whitespace-only selection", () => {
    const h = harness();
    h.rightClick({});
    h.rightClick({ selectionText: "   " });
    assert.equal(h.popups.length, 0);
  });

  it("offers Copy over a selection, popped up on the clicked window", () => {
    const h = harness();
    const popup = h.rightClick({ selectionText: "hello" });
    assert.deepEqual(shape(popup?.template), [{ role: "copy" }]);
    assert.equal(popup?.window, h.win);
  });

  it("offers the first five suggestions and Add to Dictionary over a misspelled word", () => {
    const h = harness();
    const suggestions = ["the", "ten", "tea", "tech", "tel", "tee", "th"];
    const popup = h.rightClick({
      misspelledWord: "teh",
      dictionarySuggestions: suggestions,
      isEditable: true,
    });
    assert.deepEqual(shape(popup?.template), [
      ...suggestions.slice(0, 5).map((label) => ({ label })),
      { label: "Add to Dictionary" },
      { type: "separator" },
      { role: "cut", enabled: true },
      { role: "copy", enabled: false },
      { role: "paste", enabled: true },
      { role: "selectAll", enabled: false },
    ]);
    for (const item of popup?.template ?? []) click(item);
    assert.deepEqual(h.win.webContents.replaced, suggestions.slice(0, 5));
    assert.deepEqual(h.win.webContents.learned, ["teh"]);
  });

  it("copies a link's address and drops a trailing separator", () => {
    const h = harness();
    const popup = h.rightClick({ linkURL: "https://example.com/a" });
    assert.deepEqual(shape(popup?.template), [{ label: "Copy Link Address" }]);
    click(popup?.template[0]);
    assert.deepEqual(h.copied, ["https://example.com/a"]);
    const withSelection = h.rightClick({ linkURL: "https://example.com/b", selectionText: "x" });
    assert.deepEqual(shape(withSelection?.template), [
      { label: "Copy Link Address" },
      { type: "separator" },
      { role: "copy" },
    ]);
  });
});
