import { afterEach, describe, expect, it, vi } from "vitest";

import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";

import { chatClickTakesKeyboard, domPaneFocus } from "./paneFocus";
import type { SplitTarget } from "./terminalSplitTarget";

/** Two panes, each with a focusable terminal surface, and a field outside them. */
function mountPanes() {
  const panes = document.createElement("div");
  const surfaces = ["left", "right"].map((key) => {
    const pane = document.createElement("div");
    pane.dataset.terminalPane = key;
    const surface = document.createElement("textarea");
    surface.setAttribute(TERMINAL_SURFACE_ATTRIBUTE, "");
    pane.append(surface);
    panes.append(pane);
    return surface;
  });
  const outside = document.createElement("input");
  document.body.append(panes, outside);
  return { panes, left: surfaces[0]!, right: surfaces[1]!, outside };
}

function memoryTarget(initial: TerminalSplitState): SplitTarget & { state: TerminalSplitState } {
  const target = {
    id: "split",
    state: initial,
    read: () => target.state,
    update: (
      update: (current: TerminalSplitState | undefined) => TerminalSplitState | undefined,
    ) => {
      target.state = update(target.state) ?? target.state;
    },
  };
  return target;
}

const SPLIT: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: ["left", "right"] },
  focusedKey: "left",
};

afterEach(() => {
  document.body.replaceChildren();
});

describe("domPaneFocus", () => {
  it("reads which pane's terminal has the keyboard", () => {
    const { panes, right, outside } = mountPanes();
    const focus = domPaneFocus(memoryTarget(SPLIT), { current: panes });

    right.focus();
    expect(focus.hasFocus()).toBe(true);
    expect(focus.focusedPane()).toBe("right");

    outside.focus();
    expect(focus.hasFocus()).toBe(false);
    expect(focus.focusedPane()).toBeNull();
  });

  it("names no pane while focus is in the panes but not in a terminal", () => {
    const { panes } = mountPanes();
    const button = document.createElement("button");
    panes.querySelector("[data-terminal-pane='left']")!.append(button);
    button.focus();
    const focus = domPaneFocus(memoryTarget(SPLIT), { current: panes });
    expect(focus.hasFocus()).toBe(true);
    expect(focus.focusedPane()).toBeNull();
  });

  it("moves the split's focused pane, and leaves a pane the split doesn't have alone", () => {
    const { panes } = mountPanes();
    const target = memoryTarget(SPLIT);
    const focus = domPaneFocus(target, { current: panes });

    focus.focus("right");
    expect(target.state.focusedKey).toBe("right");
    focus.focus("gone");
    expect(target.state.focusedKey).toBe("right");
  });

  it("measures the panes' aspect, and 1 while they have no size", () => {
    const { panes } = mountPanes();
    const focus = domPaneFocus(memoryTarget(SPLIT), { current: panes });
    expect(focus.aspect()).toBe(1);
    vi.spyOn(panes, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 800, 400));
    expect(focus.aspect()).toBe(2);
    expect(domPaneFocus(memoryTarget(SPLIT), { current: null }).aspect()).toBe(1);
  });

  it("reports focus leaving and entering the panes, and acting in them, until unsubscribed", () => {
    const { panes, left, right, outside } = mountPanes();
    const focus = domPaneFocus(memoryTarget(SPLIT), { current: panes });
    const listener = { left: vi.fn(), entered: vi.fn(), acted: vi.fn() };
    const unsubscribe = focus.subscribe(listener);

    left.focus();
    expect(listener.entered).toHaveBeenCalledExactlyOnceWith(left);
    // Moving between panes stays inside them: nothing left, nothing entered.
    right.focus();
    expect(listener.left).not.toHaveBeenCalled();
    expect(listener.entered).toHaveBeenCalledOnce();
    outside.focus();
    expect(listener.left).toHaveBeenCalledExactlyOnceWith(right);
    right.focus();
    expect(listener.entered).toHaveBeenCalledTimes(2);
    expect(listener.entered).toHaveBeenLastCalledWith(right);
    outside.focus();

    outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(listener.acted).not.toHaveBeenCalled();
    left.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
    expect(listener.acted).toHaveBeenCalledOnce();

    unsubscribe();
    left.focus();
    left.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(listener.entered).toHaveBeenCalledTimes(2);
    expect(listener.left).toHaveBeenCalledTimes(2);
    expect(listener.acted).toHaveBeenCalledOnce();
  });
});

describe("chatClickTakesKeyboard", () => {
  const chat = () => {
    const root = document.createElement("div");
    root.innerHTML = `<p id="text">Hi <span id="inner">there</span></p><a id="link" href="#">link</a>
      <button id="button"><svg id="icon"></svg></button><div id="editor" contenteditable="true"></div>
      <div role="dialog"><p id="in-dialog">Dialog</p></div>`;
    document.body.append(root);
    return root;
  };
  const noSelection = { isCollapsed: true, toString: () => "" } as Selection;

  it("takes the keyboard for a click on the chat's own space", () => {
    const root = chat();
    expect(chatClickTakesKeyboard(root.querySelector("#text")!, noSelection)).toBe(true);
    expect(chatClickTakesKeyboard(root.querySelector("#inner")!, null)).toBe(true);
    root.remove();
  });

  it.each(["#link", "#button", "#icon", "#editor", "#in-dialog"])(
    "leaves a click on %s to what it clicked",
    (selector) => {
      const root = chat();
      expect(chatClickTakesKeyboard(root.querySelector(selector)!, noSelection)).toBe(false);
      root.remove();
    },
  );

  it("leaves a click that ends a text selection, which taking the keyboard would clear", () => {
    const root = chat();
    const selection = { isCollapsed: false, toString: () => "there" } as Selection;
    expect(chatClickTakesKeyboard(root.querySelector("#text")!, selection)).toBe(false);
    root.remove();
  });
});
