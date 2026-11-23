import { describe, expect, it } from "vitest";
import { FileTreeModel } from "@/components/file-tree/vendored";
import { opensRow, rowClickAction, rowKeyAction, SingleSelection } from "./treeSelection";

/** A real, unrendered file tree model over `paths`, all folders open. */
function makeTree(paths: string[]) {
  const tree = new FileTreeModel({ paths, initialExpansion: "open" });
  return { tree, selection: new SingleSelection(tree) };
}

const key = (
  name: string,
  modifiers: Partial<Record<"shift" | "ctrl" | "meta" | "alt", boolean>> = {},
) => ({
  key: name,
  shiftKey: modifiers.shift ?? false,
  ctrlKey: modifiers.ctrl ?? false,
  metaKey: modifiers.meta ?? false,
  altKey: modifiers.alt ?? false,
});

describe("rowKeyAction", () => {
  it("turns Shift+arrows into plain moves", () => {
    expect(rowKeyAction(key("ArrowDown", { shift: true }))).toEqual({
      kind: "move",
      direction: "next",
    });
    expect(rowKeyAction(key("ArrowUp", { shift: true }))).toEqual({
      kind: "move",
      direction: "previous",
    });
  });

  it("blocks select-all and toggle-select", () => {
    for (const modifier of ["ctrl", "meta"] as const) {
      expect(rowKeyAction(key("a", { [modifier]: true }))).toEqual({ kind: "block" });
      expect(rowKeyAction(key("A", { [modifier]: true, shift: true }))).toEqual({ kind: "block" });
      expect(rowKeyAction(key(" ", { [modifier]: true }))).toEqual({ kind: "block" });
    }
  });

  it("passes everything else to the tree", () => {
    for (const input of [key("ArrowDown"), key("ArrowRight"), key("Enter"), key(" "), key("a")]) {
      expect(rowKeyAction(input)).toEqual({ kind: "pass" });
    }
    // An app shortcut with Shift and a command key isn't the tree's.
    expect(rowKeyAction(key("ArrowDown", { shift: true, meta: true }))).toEqual({ kind: "pass" });
  });
});

describe("rowClickAction", () => {
  const plain = { shiftKey: false, ctrlKey: false, metaKey: false };

  it("opens a file in a new tab on ⌘-click on macOS and Ctrl-click elsewhere", () => {
    const newTab = { kind: "open-in-new-tab", path: "src/b.ts" };
    expect(rowClickAction({ ...plain, metaKey: true }, "src/b.ts", true)).toEqual(newTab);
    expect(rowClickAction({ ...plain, ctrlKey: true }, "src/b.ts", false)).toEqual(newTab);
  });

  it("selects the row alone for other modified clicks, and for a folder", () => {
    const select = { kind: "select", path: "src/b.ts" };
    expect(rowClickAction({ ...plain, shiftKey: true }, "src/b.ts", true)).toEqual(select);
    expect(rowClickAction({ ...plain, ctrlKey: true }, "src/b.ts", true)).toEqual(select);
    expect(rowClickAction({ ...plain, metaKey: true }, "src/b.ts", false)).toEqual(select);
    expect(rowClickAction({ ...plain, metaKey: true, shiftKey: true }, "src/b.ts", true)).toEqual(
      select,
    );
    expect(rowClickAction({ ...plain, metaKey: true }, "src/", true)).toEqual({
      kind: "select",
      path: "src/",
    });
  });

  it("leaves plain clicks, and clicks off any row, to the tree", () => {
    expect(rowClickAction(plain, "src/b.ts")).toEqual({ kind: "pass" });
    expect(rowClickAction({ ...plain, shiftKey: true }, null)).toEqual({ kind: "pass" });
  });
});

describe("SingleSelection", () => {
  it("trims a range or toggle back to the focused row", () => {
    const { tree, selection } = makeTree(["a.ts", "b.ts", "c.ts"]);
    tree.focusPath("a.ts");
    tree.getItem("a.ts")!.select();
    // What a Ctrl-click toggle leaves behind if it reaches the tree.
    tree.focusPath("c.ts");
    tree.togglePathSelectionFromInput("c.ts");
    expect(tree.getSelectedPaths()).toEqual(["a.ts", "c.ts"]);
    selection.normalize();
    expect(tree.getSelectedPaths()).toEqual(["c.ts"]);
  });

  it("leaves a single selection alone", () => {
    const { tree, selection } = makeTree(["a.ts", "b.ts"]);
    tree.getItem("b.ts")!.select();
    selection.normalize();
    expect(tree.getSelectedPaths()).toEqual(["b.ts"]);
  });

  it("selects a modified-click target alone and reports it once to open", () => {
    const { tree, selection } = makeTree(["a.ts", "b.ts", "c.ts"]);
    tree.getItem("a.ts")!.select();
    const result = selection.apply(
      rowClickAction({ shiftKey: true, ctrlKey: false, metaKey: false }, "c.ts"),
    );
    expect(result).toEqual({ consumed: true, activate: "c.ts", newTab: false });
    expect(tree.getSelectedPaths()).toEqual(["c.ts"]);
    expect(tree.getFocusedPath()).toBe("c.ts");
  });

  it("selects a ⌘-click target alone and reports it to open in a new tab", () => {
    const { tree, selection } = makeTree(["a.ts", "b.ts"]);
    tree.getItem("a.ts")!.select();
    const result = selection.apply({ kind: "open-in-new-tab", path: "b.ts" });
    expect(result).toEqual({ consumed: true, activate: "b.ts", newTab: true });
    expect(tree.getSelectedPaths()).toEqual(["b.ts"]);
  });

  it("moves focus without selecting for a Shift+arrow", () => {
    const { tree, selection } = makeTree(["a.ts", "b.ts"]);
    tree.focusPath("a.ts");
    expect(selection.apply(rowKeyAction(key("ArrowDown", { shift: true })))).toEqual({
      consumed: true,
      activate: null,
      newTab: false,
    });
    expect(tree.getFocusedPath()).toBe("b.ts");
    expect(tree.getSelectedPaths()).toEqual([]);
  });

  it("keeps its own changes out of normalization", () => {
    const { tree, selection } = makeTree(["src/a.ts", "src/b.ts"]);
    const unsubscribe = tree.subscribe(() => selection.normalize());
    selection.selectOnly("src/b.ts");
    unsubscribe();
    expect(tree.getSelectedPaths()).toEqual(["src/b.ts"]);
  });
});

describe("opensRow", () => {
  it("opens with Enter or Space alone", () => {
    expect(opensRow(key("Enter"))).toBe(true);
    expect(opensRow(key(" "))).toBe(true);
  });

  it("leaves modified keys and other keys alone", () => {
    expect(opensRow(key("Enter", { meta: true }))).toBe(false);
    expect(opensRow(key(" ", { ctrl: true }))).toBe(false);
    expect(opensRow(key("Enter", { shift: true }))).toBe(false);
    expect(opensRow(key("ArrowDown"))).toBe(false);
  });
});
