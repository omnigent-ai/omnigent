// The slash-completion menu mechanics shared by the landing and in-session
// composers: when the draft reads as a lone command token, the hook derives
// the open menu's query/matches/highlight and consumes the keys that
// navigate or complete it. Surface differences (mobile Enter, Escape
// clearing) are explicit options, not harmonized defaults.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useSlashCompletion } from "./useSlashCompletion";

const COMMANDS: Record<string, string> = {
  "/compact": "Compact conversation context",
  "/context": "Show context window usage",
  "/review-pr": "Review a pull request",
  "/cross-review": "Cross-vendor review",
};

type Options = Parameters<typeof useSlashCompletion>[0];

function setup(overrides: Partial<Options> = {}) {
  const onSelect = vi.fn();
  const clearText = vi.fn();
  const props: Options = {
    text: "/",
    commands: COMMANDS,
    prefix: "/",
    status: null,
    mobile: false,
    mobileEnterCompletes: true,
    escapeClearsOnlyWithContent: false,
    allowOpen: true,
    onSelect,
    clearText,
    ...overrides,
  };
  const view = renderHook((p: Options) => useSlashCompletion(p), { initialProps: props });
  return { view, props, onSelect, clearText };
}

/** A minimal key event shaped like the textarea's React keydown. */
function keyEvent(key: string, { shiftKey = false }: { shiftKey?: boolean } = {}) {
  return { key, shiftKey, preventDefault: vi.fn() };
}

const NO_PREFERENCE = { shouldPreferSendOverCompletion: false };
const PREFER_SEND = { shouldPreferSendOverCompletion: true };

describe("useSlashCompletion open condition", () => {
  it("opens on the lone prefix with every command ranked and the first pre-selected", () => {
    const { view } = setup({ text: "/" });
    expect(view.result.current.open).toBe(true);
    expect(view.result.current.query).toBe("");
    // Built-ins rank ahead of skills; insertion order breaks ties.
    expect(view.result.current.matches).toEqual([
      "/compact",
      "/context",
      "/review-pr",
      "/cross-review",
    ]);
    expect(view.result.current.index).toBe(0);
  });

  it("opens on a command token with leading whitespace trimmed", () => {
    const { view } = setup({ text: "  /rev" });
    expect(view.result.current.open).toBe(true);
    expect(view.result.current.query).toBe("rev");
  });

  it("ranks prefix matches ahead of mid-string matches", () => {
    const { view } = setup({ text: "/rev" });
    expect(view.result.current.matches).toEqual(["/review-pr", "/cross-review"]);
  });

  it("stays closed for plain text", () => {
    const { view } = setup({ text: "hello" });
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.query).toBe("");
    expect(view.result.current.matches).toEqual([]);
    expect(view.result.current.index).toBe(-1);
  });

  it("stays closed for file paths (a slash after the prefix)", () => {
    const { view } = setup({ text: "/Users/foo" });
    expect(view.result.current.open).toBe(false);
  });

  it("closes once the command name is complete (a space starts the arguments)", () => {
    const { view } = setup({ text: "/review-pr 123" });
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.matches).toEqual([]);
  });

  it("stays closed while allowOpen is false, ignoring every key", () => {
    const { view, onSelect, clearText } = setup({ text: "/rev", allowOpen: false });
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.query).toBe("");
    expect(view.result.current.matches).toEqual([]);
    for (const key of ["Escape", "Tab", "Enter", "ArrowDown", "ArrowUp"]) {
      expect(view.result.current.handleKey(keyEvent(key), NO_PREFERENCE)).toBe(false);
    }
    expect(onSelect).not.toHaveBeenCalled();
    expect(clearText).not.toHaveBeenCalled();
  });

  it.each(["/", "$"])("opens on the surface prefix even with an empty inventory", (prefix) => {
    // Discovery is still in flight: the menu opens on the prefix so the
    // loading state can render, before any command exists.
    const { view } = setup({ text: `${prefix}rev`, commands: {}, prefix, status: "loading" });
    expect(view.result.current.open).toBe(true);
    expect(view.result.current.matches).toEqual([]);
    expect(view.result.current.pendingCompletion).toBe(true);
  });

  it("opens on both / and a $ surface prefix", () => {
    // A codex-native surface prefixes skills with "$"; both "$" and "/"
    // open the menu there.
    const commands = { "/help": "Show available commands", $review: "Review the change" };
    const dollar = setup({ text: "$rev", commands, prefix: "$" });
    expect(dollar.view.result.current.open).toBe(true);
    expect(dollar.view.result.current.matches).toEqual(["$review"]);
    const slash = setup({ text: "/rev", commands, prefix: "$" });
    expect(slash.view.result.current.open).toBe(true);
    expect(slash.view.result.current.matches).toEqual(["$review"]);
  });

  it("does not open on a prefix the surface does not use", () => {
    // A slash-only surface keeps "$" inert.
    const { view } = setup({ text: "$rev" });
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.matches).toEqual([]);
  });
});

describe("useSlashCompletion highlight retention", () => {
  it("pre-selects the first match for a new query", () => {
    const { view, props } = setup({ text: "/" });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    expect(view.result.current.index).toBe(1);
    view.rerender({ ...props, text: "/rev" });
    expect(view.result.current.index).toBe(0);
  });

  it("resets to -1 when the query has no matches", () => {
    const { view } = setup({ text: "/zzz" });
    expect(view.result.current.open).toBe(true);
    expect(view.result.current.matches).toEqual([]);
    expect(view.result.current.index).toBe(-1);
  });

  it("retains the highlighted name when commands arrive asynchronously", () => {
    const { view, props } = setup({
      text: "/",
      commands: { "/alpha": "a", "/beta": "b" },
    });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    expect(view.result.current.index).toBe(1);

    // Discovery lands later with a reordered, longer list: the highlight
    // follows the selected NAME to its new position.
    view.rerender({
      ...props,
      commands: { "/zebra": "z", "/alpha": "a", "/beta": "b" },
    });
    expect(view.result.current.matches).toEqual(["/zebra", "/alpha", "/beta"]);
    expect(view.result.current.index).toBe(2);
  });

  it("drops the retained highlight when the arrival removes the selected name", () => {
    const { view, props } = setup({
      text: "/",
      commands: { "/alpha": "a", "/beta": "b" },
    });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    view.rerender({
      ...props,
      commands: { "/gamma": "g", "/alpha": "a" },
    });
    expect(view.result.current.index).toBe(0);
  });

  it("resets the highlight to the first match when the menu reopens", () => {
    const { view, props } = setup({ text: "/rev" });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    expect(view.result.current.index).toBe(1);
    view.rerender({ ...props, text: "hello" });
    expect(view.result.current.open).toBe(false);
    view.rerender({ ...props, text: "/rev" });
    expect(view.result.current.index).toBe(0);
  });
});

describe("useSlashCompletion arrow navigation", () => {
  it("wraps ArrowDown from the last match to the first", () => {
    const { view } = setup({ text: "/rev" });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    expect(view.result.current.index).toBe(1);
    const event = keyEvent("ArrowDown");
    let consumed = false;
    act(() => {
      consumed = view.result.current.handleKey(event, NO_PREFERENCE);
    });
    expect(consumed).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(view.result.current.index).toBe(0);
  });

  it("wraps ArrowUp from the first match to the last", () => {
    const { view } = setup({ text: "/rev" });
    const event = keyEvent("ArrowUp");
    let consumed = false;
    act(() => {
      consumed = view.result.current.handleKey(event, NO_PREFERENCE);
    });
    expect(consumed).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(view.result.current.index).toBe(1);
  });

  it("ignores arrows when the menu has no matches", () => {
    const { view } = setup({ text: "/zzz" });
    expect(view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE)).toBe(false);
    expect(view.result.current.handleKey(keyEvent("ArrowUp"), NO_PREFERENCE)).toBe(false);
  });

  it("navigates even when the send chord is preferred over completion", () => {
    const { view } = setup({ text: "/rev" });
    act(() => {
      expect(view.result.current.handleKey(keyEvent("ArrowDown"), PREFER_SEND)).toBe(true);
    });
    expect(view.result.current.index).toBe(1);
  });
});

describe("useSlashCompletion Tab/Enter completion", () => {
  it("completes the highlighted match with Tab", () => {
    const { view, onSelect } = setup({ text: "/rev" });
    const event = keyEvent("Tab");
    expect(view.result.current.handleKey(event, NO_PREFERENCE)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("/review-pr");
  });

  it("completes the highlighted match with Enter on desktop", () => {
    const { view, onSelect } = setup({ text: "/rev" });
    act(() => {
      view.result.current.handleKey(keyEvent("ArrowDown"), NO_PREFERENCE);
    });
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(true);
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("/cross-review");
  });

  it("ignores Shift+Enter (a newline, not a completion)", () => {
    const { view, onSelect } = setup({ text: "/rev" });
    expect(
      view.result.current.handleKey(keyEvent("Enter", { shiftKey: true }), NO_PREFERENCE),
    ).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("does not complete without a highlighted match", () => {
    const { view, onSelect } = setup({ text: "/zzz" });
    expect(view.result.current.handleKey(keyEvent("Tab"), NO_PREFERENCE)).toBe(false);
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("yields Tab and Enter to submission when the send chord is preferred", () => {
    const { view, onSelect } = setup({ text: "/rev" });
    const tab = keyEvent("Tab");
    expect(view.result.current.handleKey(tab, PREFER_SEND)).toBe(false);
    expect(tab.preventDefault).not.toHaveBeenCalled();
    expect(view.result.current.handleKey(keyEvent("Enter"), PREFER_SEND)).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("completes with Enter on mobile when the surface opts in", () => {
    const { view, onSelect } = setup({ text: "/rev", mobile: true, mobileEnterCompletes: true });
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(true);
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("/review-pr");
  });

  it("ignores Enter on mobile when the surface reserves it for newline", () => {
    const { view, onSelect } = setup({ text: "/rev", mobile: true, mobileEnterCompletes: false });
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(false);
    expect(onSelect).not.toHaveBeenCalled();
    // Tab completion is unaffected by the mobile Enter policy.
    expect(view.result.current.handleKey(keyEvent("Tab"), NO_PREFERENCE)).toBe(true);
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("/review-pr");
  });
});

describe("useSlashCompletion loading swallow", () => {
  it("reports pendingCompletion for a lone command token while loading and matchless", () => {
    const { view, props } = setup({ text: "/review", commands: {}, status: "loading" });
    expect(view.result.current.pendingCompletion).toBe(true);
    view.rerender({ ...props, status: "ready" });
    expect(view.result.current.pendingCompletion).toBe(false);
    view.rerender({ ...props, status: "loading", commands: COMMANDS });
    expect(view.result.current.matches.length).toBeGreaterThan(0);
    expect(view.result.current.pendingCompletion).toBe(false);
  });

  it("keeps reporting pendingCompletion while allowOpen closes the menu", () => {
    // Submit blocking keys off pendingCompletion even when the composer is
    // not focused, so it must not flip with the menu's open state.
    const { view, props } = setup({ text: "/review", commands: {}, status: "loading" });
    expect(view.result.current.pendingCompletion).toBe(true);
    view.rerender({ ...props, allowOpen: false });
    expect(view.result.current.open).toBe(false);
    expect(view.result.current.matches).toEqual([]);
    expect(view.result.current.pendingCompletion).toBe(true);
  });

  it.each(["Tab", "Enter"])("swallows %s while skills load with no completion yet", (key) => {
    const { view, onSelect } = setup({ text: "/review", commands: {}, status: "loading" });
    const event = keyEvent(key);
    expect(view.result.current.handleKey(event, NO_PREFERENCE)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("does not swallow Enter on mobile (the wrapper never routes it here)", () => {
    const { view } = setup({
      text: "/review",
      commands: {},
      status: "loading",
      mobile: true,
    });
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(false);
  });

  it("does not swallow Shift+Enter or a send-preferred chord", () => {
    const { view } = setup({ text: "/review", commands: {}, status: "loading" });
    expect(
      view.result.current.handleKey(keyEvent("Enter", { shiftKey: true }), NO_PREFERENCE),
    ).toBe(false);
    expect(view.result.current.handleKey(keyEvent("Tab"), PREFER_SEND)).toBe(false);
    expect(view.result.current.handleKey(keyEvent("Enter"), PREFER_SEND)).toBe(false);
  });

  it("completes instead of swallowing once matches exist", () => {
    const { view, onSelect } = setup({ text: "/rev", status: "loading" });
    expect(view.result.current.pendingCompletion).toBe(false);
    expect(view.result.current.handleKey(keyEvent("Enter"), NO_PREFERENCE)).toBe(true);
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("/review-pr");
  });
});

describe("useSlashCompletion Escape", () => {
  it("clears whenever the menu is open for a clear-on-escape surface", () => {
    const { view, clearText } = setup({
      text: "/zzz",
      escapeClearsOnlyWithContent: false,
    });
    const event = keyEvent("Escape");
    expect(view.result.current.handleKey(event, NO_PREFERENCE)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(clearText).toHaveBeenCalledOnce();
  });

  it("ignores Escape on a content-gated surface with no matches and no status", () => {
    const { view, clearText } = setup({
      text: "/zzz",
      escapeClearsOnlyWithContent: true,
      status: null,
    });
    expect(view.result.current.handleKey(keyEvent("Escape"), NO_PREFERENCE)).toBe(false);
    expect(clearText).not.toHaveBeenCalled();
  });

  it("clears a content-gated surface while discovery is in flight", () => {
    const { view, clearText } = setup({
      text: "/zzz",
      escapeClearsOnlyWithContent: true,
      status: "loading",
    });
    expect(view.result.current.handleKey(keyEvent("Escape"), NO_PREFERENCE)).toBe(true);
    expect(clearText).toHaveBeenCalledOnce();
  });

  it("clears a content-gated surface when matches exist", () => {
    const { view, clearText } = setup({
      text: "/rev",
      escapeClearsOnlyWithContent: true,
    });
    let consumed = false;
    act(() => {
      consumed = view.result.current.handleKey(keyEvent("Escape"), NO_PREFERENCE);
    });
    expect(consumed).toBe(true);
    expect(clearText).toHaveBeenCalledOnce();
  });

  it("ignores Escape while the menu is closed", () => {
    const { view, clearText } = setup({ text: "hello" });
    expect(view.result.current.handleKey(keyEvent("Escape"), NO_PREFERENCE)).toBe(false);
    expect(clearText).not.toHaveBeenCalled();
  });
});

describe("useSlashCompletion key fallthrough", () => {
  it("leaves unrelated keys to the adapter", () => {
    const { view } = setup({ text: "/rev" });
    expect(view.result.current.handleKey(keyEvent("a"), NO_PREFERENCE)).toBe(false);
    expect(view.result.current.handleKey(keyEvent("Backspace"), NO_PREFERENCE)).toBe(false);
  });
});
