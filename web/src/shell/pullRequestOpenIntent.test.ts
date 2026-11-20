import { describe, expect, it, vi } from "vitest";

import type { KeyChordInput } from "@/lib/shortcuts/chord";

import { isNewTabEnter, openByIntent, pullRequestOpenIntent } from "./pullRequestOpenIntent";

const none = { metaKey: false, ctrlKey: false, shiftKey: false };

describe("pullRequestOpenIntent", () => {
  it("shows it here on a plain click, and behind the current tab on ⌘-click or a middle click", () => {
    expect(pullRequestOpenIntent({ ...none, button: 0 }, true)).toBe("here");
    expect(pullRequestOpenIntent({ ...none, button: 0, metaKey: true }, true)).toBe("background");
    expect(pullRequestOpenIntent({ ...none, button: 1 }, true)).toBe("background");
    expect(pullRequestOpenIntent({ ...none, button: 1 }, false)).toBe("background");
  });

  it("uses Ctrl rather than ⌘ off macOS", () => {
    expect(pullRequestOpenIntent({ ...none, button: 0, ctrlKey: true }, false)).toBe("background");
    expect(pullRequestOpenIntent({ ...none, button: 0, metaKey: true }, false)).toBe("here");
    expect(pullRequestOpenIntent({ ...none, button: 0, ctrlKey: true }, true)).toBe("here");
  });

  it("opens the new tab in front when Shift joins the gesture, as browsers do", () => {
    expect(pullRequestOpenIntent({ ...none, metaKey: true, shiftKey: true }, true)).toBe(
      "foreground",
    );
    expect(pullRequestOpenIntent({ ...none, ctrlKey: true, shiftKey: true }, false)).toBe(
      "foreground",
    );
    expect(pullRequestOpenIntent({ ...none, button: 1, shiftKey: true }, true)).toBe("foreground");
    expect(pullRequestOpenIntent({ ...none, shiftKey: true }, true)).toBe("here");
  });
});

describe("isNewTabEnter", () => {
  const key = (overrides: Partial<KeyChordInput> = {}): KeyChordInput => ({
    key: "Enter",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    ...overrides,
  });

  it("matches the registry's ⌘Enter and ⇧⌘Enter on macOS", () => {
    expect(isNewTabEnter(key({ metaKey: true }), true)).toBe(true);
    expect(isNewTabEnter(key({ metaKey: true, shiftKey: true }), true)).toBe(true);
  });

  it("matches Ctrl+Enter and ⇧Ctrl+Enter off macOS", () => {
    expect(isNewTabEnter(key({ ctrlKey: true }), false)).toBe(true);
    expect(isNewTabEnter(key({ ctrlKey: true, shiftKey: true }), false)).toBe(true);
  });

  it("leaves other keys and the other platform's modifier alone", () => {
    expect(isNewTabEnter(key(), true)).toBe(false);
    expect(isNewTabEnter(key({ ctrlKey: true }), true)).toBe(false);
    expect(isNewTabEnter(key({ metaKey: true }), false)).toBe(false);
    expect(isNewTabEnter(key({ metaKey: true, altKey: true }), true)).toBe(false);
    expect(isNewTabEnter(key({ ctrlKey: true, altKey: true, altGraph: true }), false)).toBe(false);
    expect(isNewTabEnter(key({ key: "a", metaKey: true }), true)).toBe(false);
  });
});

describe("openByIntent", () => {
  it("shows it here, or opens a new tab behind or in front", () => {
    const here = vi.fn();
    const inNewTab = vi.fn();
    openByIntent("here", here, inNewTab);
    openByIntent("background", here, inNewTab);
    openByIntent("foreground", here, inNewTab);
    expect(here).toHaveBeenCalledTimes(1);
    expect(inNewTab.mock.calls).toEqual([[{ background: true }], [{ background: false }]]);
  });

  it("shows it here for every gesture where nothing opens new tabs", () => {
    const here = vi.fn();
    for (const intent of ["here", "background", "foreground"] as const) openByIntent(intent, here);
    expect(here).toHaveBeenCalledTimes(3);
  });
});
