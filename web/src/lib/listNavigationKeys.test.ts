import { afterEach, describe, expect, it, vi } from "vitest";

import {
  holdReadlineStep,
  isPickListTarget,
  pickListInputProps,
  pickListOwnsKey,
  readlineListStep,
  translateReadlineStepInList,
  withReadlineListSteps,
} from "./listNavigationKeys";

function key(init: KeyboardEventInit & { key: string }, target?: EventTarget): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  if (target) Object.defineProperty(event, "target", { value: target });
  return event;
}

function mount(html: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = html;
  document.body.append(root);
  return root;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("readlineListStep", () => {
  it("reads exact Ctrl-N and Ctrl-P as steps", () => {
    expect(readlineListStep(key({ key: "n", code: "KeyN", ctrlKey: true }))).toBe("next");
    expect(readlineListStep(key({ key: "p", code: "KeyP", ctrlKey: true }))).toBe("previous");
    expect(readlineListStep(key({ key: "N", code: "KeyN", ctrlKey: true }))).toBe("next");
  });

  it.each([
    ["⌘", { metaKey: true }],
    ["Alt", { altKey: true }],
    ["Shift", { shiftKey: true }],
    ["Ctrl+⌘", { metaKey: true, ctrlKey: true }],
    ["no modifier", { ctrlKey: false }],
  ])("ignores N with %s", (_label, modifiers) => {
    expect(readlineListStep(key({ key: "n", code: "KeyN", ctrlKey: true, ...modifiers }))).toBe(
      null,
    );
  });

  it("ignores other letters and arrows", () => {
    expect(readlineListStep(key({ key: "k", code: "KeyK", ctrlKey: true }))).toBe(null);
    expect(readlineListStep(key({ key: "ArrowDown", code: "ArrowDown" }))).toBe(null);
  });

  it("uses the physical key on a non-Latin layout, and the letter on a Latin one", () => {
    expect(readlineListStep(key({ key: "т", code: "KeyN", ctrlKey: true }))).toBe("next");
    // Dvorak: the key labelled N sits where QWERTY has L.
    expect(readlineListStep(key({ key: "n", code: "KeyL", ctrlKey: true }))).toBe("next");
    expect(readlineListStep(key({ key: "l", code: "KeyN", ctrlKey: true }))).toBe(null);
  });

  it("ignores IME composition and keys already handled", () => {
    expect(readlineListStep(key({ key: "n", ctrlKey: true, isComposing: true }))).toBe(null);
    expect(readlineListStep(key({ key: "Process", keyCode: 229, ctrlKey: true }))).toBe(null);
    const handled = key({ key: "n", code: "KeyN", ctrlKey: true });
    handled.preventDefault();
    expect(readlineListStep(handled)).toBe(null);
  });
});

describe("pick list ownership", () => {
  it("is set only while the list is open", () => {
    expect(pickListInputProps(true)).toEqual({ "data-pick-list-input": "" });
    expect(pickListInputProps(false)).toEqual({});
  });

  it("covers an open list's input, a text field in its marked wrapper, and menu rows", () => {
    const root = mount(`
      <input id="marked" data-pick-list-input />
      <input id="plain" />
      <div data-pick-list-input>
        <div id="editor" contenteditable="true"></div>
        <button id="button">Send</button>
      </div>
      <div role="menu">
        <div id="row" role="menuitem" tabindex="-1">Copy</div>
        <input id="nested" />
      </div>
      <div id="loose" role="option"></div>
    `);
    const byId = (id: string) => root.querySelector(`#${id}`);
    expect(isPickListTarget(byId("marked"))).toBe(true);
    expect(isPickListTarget(byId("editor"))).toBe(true);
    expect(isPickListTarget(byId("row"))).toBe(true);
    // Not a list's own field or row: they keep Ctrl-P for the app.
    expect(isPickListTarget(byId("plain"))).toBe(false);
    expect(isPickListTarget(byId("button"))).toBe(false);
    expect(isPickListTarget(byId("nested"))).toBe(false);
    expect(isPickListTarget(byId("loose"))).toBe(false);
    expect(isPickListTarget(null)).toBe(false);
  });

  it("owns only Ctrl-N/Ctrl-P, and only on a list target", () => {
    const root = mount(`<input data-pick-list-input /><input id="plain" />`);
    const marked = root.querySelector("input")!;
    const plain = root.querySelector("#plain")!;
    expect(pickListOwnsKey(key({ key: "p", code: "KeyP", ctrlKey: true }, marked))).toBe(true);
    expect(pickListOwnsKey(key({ key: "p", code: "KeyP", ctrlKey: true }, plain))).toBe(false);
    expect(pickListOwnsKey(key({ key: "p", code: "KeyP", metaKey: true }, marked))).toBe(false);
    expect(pickListOwnsKey(key({ key: "k", code: "KeyK", ctrlKey: true }, marked))).toBe(false);
  });
});

describe("translateReadlineStepInList", () => {
  function menus() {
    const root = mount(`
      <div id="parent" role="menu">
        <div id="parent-row" role="menuitem" tabindex="-1">Open</div>
        <input id="nested" />
      </div>
      <div id="child" role="menu">
        <div id="child-row" role="menuitem" tabindex="-1">Rename</div>
      </div>
    `);
    const byId = (id: string) => root.querySelector<HTMLElement>(`#${id}`)!;
    return { byId };
  }

  function contentEvent(target: Element, currentTarget: Element, init: KeyboardEventInit) {
    const event = key({ key: "n", code: "KeyN", ctrlKey: true, ...init }, target);
    Object.defineProperty(event, "currentTarget", { value: currentTarget });
    return event;
  }

  it("re-sends Ctrl-N/Ctrl-P on a row as ArrowDown/ArrowUp", () => {
    const { byId } = menus();
    const row = byId("parent-row");
    const seen: string[] = [];
    row.addEventListener("keydown", (event) => seen.push(event.key));
    const next = contentEvent(row, byId("parent"), {});
    translateReadlineStepInList(next);
    const previous = contentEvent(row, byId("parent"), { key: "p", code: "KeyP" });
    translateReadlineStepInList(previous);
    expect(seen).toEqual(["ArrowDown", "ArrowUp"]);
    expect(next.defaultPrevented).toBe(true);
    expect(previous.defaultPrevented).toBe(true);
  });

  it("leaves a submenu's key, bubbling into its parent, to the submenu", () => {
    const { byId } = menus();
    const row = byId("child-row");
    const dispatch = vi.spyOn(row, "dispatchEvent");
    const event = contentEvent(row, byId("parent"), {});
    translateReadlineStepInList(event);
    expect(dispatch).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("never re-sends into a field nested in the menu", () => {
    const { byId } = menus();
    const nested = byId("nested");
    const dispatch = vi.spyOn(nested, "dispatchEvent");
    const event = contentEvent(nested, byId("parent"), {});
    translateReadlineStepInList(event);
    expect(dispatch).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("lets a caller that handles the key keep it", () => {
    const { byId } = menus();
    const row = byId("parent-row");
    const dispatch = vi.spyOn(row, "dispatchEvent");
    const handler = withReadlineListSteps((event: KeyboardEvent) => event.preventDefault());
    handler(contentEvent(row, byId("parent"), {}));
    expect(dispatch).not.toHaveBeenCalled();
  });
});

describe("holdReadlineStep", () => {
  it("takes Ctrl-N/Ctrl-P for a list with no rows, and lets everything else pass", () => {
    const preventDefault = vi.fn();
    expect(holdReadlineStep({ key: "p", ctrlKey: true, preventDefault })).toBe(true);
    expect(holdReadlineStep({ key: "n", code: "KeyN", ctrlKey: true, preventDefault })).toBe(true);
    expect(preventDefault).toHaveBeenCalledTimes(2);
    preventDefault.mockClear();
    for (const event of [
      { key: "ArrowUp" },
      { key: "Enter" },
      { key: "p" },
      { key: "p", ctrlKey: true, shiftKey: true },
      { key: "p", ctrlKey: true, defaultPrevented: true },
      { key: "p", ctrlKey: true, nativeEvent: { isComposing: true } },
    ]) {
      expect(holdReadlineStep({ ...event, preventDefault })).toBe(false);
    }
    expect(preventDefault).not.toHaveBeenCalled();
  });
});
