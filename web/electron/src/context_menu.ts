/**
 * Right-click context menu for a shell window's page. Electron ships NO
 * context menu by default, so without this there is no copy/paste/
 * spell-suggestion UI anywhere in the app.
 *
 * The menu is built per-invocation from Chromium's hit-test `params`:
 *   - spelling suggestions + "Add to Dictionary" over a misspelled word
 *     (`spellcheck: true` is set on the window's webPreferences),
 *   - Copy Link Address over a link,
 *   - Cut / Copy / Paste / Select All in editable fields, Copy over a
 *     text selection — each enabled per Chromium's `editFlags`.
 * Right-clicking dead space shows nothing (no popup) rather than a menu
 * of disabled items.
 */

import type { ContextMenuParams, MenuItemConstructorOptions } from "electron";

/** Maximum number of spelling suggestions offered in the context menu. */
const MAX_SPELL_SUGGESTIONS = 5;

/** The parts of Chromium's hit-test the menu reads. */
export type ContextMenuHitTest = Pick<
  ContextMenuParams,
  "misspelledWord" | "dictionarySuggestions" | "linkURL" | "isEditable" | "selectionText"
> & {
  editFlags: Pick<
    ContextMenuParams["editFlags"],
    "canCut" | "canCopy" | "canPaste" | "canSelectAll"
  >;
};

/** The parts of a shell window the menu attaches to. */
export interface ContextMenuWindow {
  webContents: {
    on: (
      event: "context-menu",
      listener: (event: unknown, params: ContextMenuHitTest) => void,
    ) => unknown;
    replaceMisspelling: (word: string) => void;
    session: { addWordToSpellCheckerDictionary: (word: string) => unknown };
  };
}

export interface ContextMenuDeps<Win extends ContextMenuWindow> {
  Menu: {
    buildFromTemplate: (template: MenuItemConstructorOptions[]) => {
      popup: (options: { window: Win }) => void;
    };
  };
  clipboard: { writeText: (text: string) => unknown };
}

export interface ContextMenu<Win> {
  /** Attach the menu to a window's webContents. */
  attach: (win: Win) => void;
}

export function createContextMenu<Win extends ContextMenuWindow>({
  Menu,
  clipboard,
}: ContextMenuDeps<Win>): ContextMenu<Win> {
  function attach(win: Win): void {
    win.webContents.on("context-menu", (_event, params) => {
      const template: MenuItemConstructorOptions[] = [];

      if (params.misspelledWord) {
        for (const suggestion of params.dictionarySuggestions.slice(0, MAX_SPELL_SUGGESTIONS)) {
          template.push({
            label: suggestion,
            click: () => win.webContents.replaceMisspelling(suggestion),
          });
        }
        template.push({
          label: "Add to Dictionary",
          click: () =>
            win.webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
        });
        template.push({ type: "separator" });
      }

      if (params.linkURL) {
        template.push({
          label: "Copy Link Address",
          click: () => clipboard.writeText(params.linkURL),
        });
        template.push({ type: "separator" });
      }

      if (params.isEditable) {
        template.push(
          { role: "cut", enabled: params.editFlags.canCut },
          { role: "copy", enabled: params.editFlags.canCopy },
          { role: "paste", enabled: params.editFlags.canPaste },
          { role: "selectAll", enabled: params.editFlags.canSelectAll },
        );
      } else if (params.selectionText.trim() !== "") {
        template.push({ role: "copy" });
      }

      // Drop a trailing separator (e.g. link menu over a non-editable,
      // unselected area) and skip the popup entirely when nothing applies.
      while (template.length > 0 && template.at(-1)?.type === "separator") {
        template.pop();
      }
      if (template.length === 0) return;
      Menu.buildFromTemplate(template).popup({ window: win });
    });
  }

  return { attach };
}
