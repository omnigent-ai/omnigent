import { describe, expect, it } from "vitest";

import type { KeyChordInput } from "@/lib/shortcuts/chord";

import { treeCommandClickOpen, treeKeyOpen, treeMiddleClickOpen } from "./treeOpenGesture";

const pointer = { button: 1, shiftKey: false, metaKey: false, ctrlKey: false };
const enter = (overrides: Partial<KeyChordInput> = {}): KeyChordInput => ({
  key: "Enter",
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...overrides,
});

describe("treeMiddleClickOpen", () => {
  it("opens a file row behind, or switches to it with Shift", () => {
    expect(treeMiddleClickOpen(pointer, "src/a.ts")).toEqual({
      path: "src/a.ts",
      background: true,
    });
    expect(treeMiddleClickOpen({ ...pointer, shiftKey: true }, "src/a.ts")).toEqual({
      path: "src/a.ts",
      background: false,
    });
  });

  it("ignores other buttons, folders and clicks outside rows", () => {
    expect(treeMiddleClickOpen({ ...pointer, button: 0 }, "src/a.ts")).toBeNull();
    expect(treeMiddleClickOpen({ ...pointer, button: 2 }, "src/a.ts")).toBeNull();
    expect(treeMiddleClickOpen(pointer, "src/")).toBeNull();
    expect(treeMiddleClickOpen(pointer, null)).toBeNull();
  });
});

describe("treeCommandClickOpen", () => {
  const click = { button: 0, shiftKey: false, metaKey: false, ctrlKey: false };

  it("opens a file row behind on ⌘-click on macOS and Ctrl-click elsewhere, switching with ⇧", () => {
    expect(treeCommandClickOpen({ ...click, metaKey: true }, "a.ts", true)).toEqual({
      path: "a.ts",
      background: true,
    });
    expect(
      treeCommandClickOpen({ ...click, ctrlKey: true, shiftKey: true }, "a.ts", false),
    ).toEqual({ path: "a.ts", background: false });
  });

  it("ignores the other platform's modifier, plain clicks, folders and other buttons", () => {
    expect(treeCommandClickOpen({ ...click, ctrlKey: true }, "a.ts", true)).toBeNull();
    expect(treeCommandClickOpen({ ...click, metaKey: true }, "a.ts", false)).toBeNull();
    expect(treeCommandClickOpen(click, "a.ts", true)).toBeNull();
    expect(treeCommandClickOpen({ ...click, metaKey: true }, "src/", true)).toBeNull();
    expect(treeCommandClickOpen({ ...click, metaKey: true, button: 1 }, "a.ts", true)).toBeNull();
  });
});

describe("treeKeyOpen", () => {
  it("opens on ⌘Enter, switching with ⇧, on macOS", () => {
    expect(treeKeyOpen(enter({ metaKey: true }), "a.ts", true)).toEqual({
      path: "a.ts",
      background: true,
    });
    expect(treeKeyOpen(enter({ metaKey: true, shiftKey: true }), "a.ts", true)).toEqual({
      path: "a.ts",
      background: false,
    });
  });

  it("opens on Ctrl+Enter, switching with ⇧, off macOS", () => {
    expect(treeKeyOpen(enter({ ctrlKey: true }), "a.ts", false)).toEqual({
      path: "a.ts",
      background: true,
    });
    expect(treeKeyOpen(enter({ ctrlKey: true, shiftKey: true }), "a.ts", false)).toEqual({
      path: "a.ts",
      background: false,
    });
  });

  it("leaves the tree's own keys, folders and other platforms' chords alone", () => {
    expect(treeKeyOpen(enter(), "a.ts", true)).toBeNull();
    expect(treeKeyOpen(enter({ metaKey: true, key: " " }), "a.ts", true)).toBeNull();
    expect(treeKeyOpen(enter({ ctrlKey: true }), "a.ts", true)).toBeNull();
    expect(treeKeyOpen(enter({ metaKey: true }), "src/", true)).toBeNull();
    expect(treeKeyOpen(enter({ metaKey: true }), null, true)).toBeNull();
  });
});
