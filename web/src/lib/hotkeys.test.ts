import { describe, expect, it } from "vitest";

import {
  hasCommandModifier,
  isEscapeOwnerOpen,
  isKeyOwningOverlayOpen,
  messageStepDirection,
} from "./hotkeys";

describe("hasCommandModifier", () => {
  it("requires Meta exclusively on macOS", () => {
    expect(hasCommandModifier({ metaKey: true, ctrlKey: false }, true)).toBe(true);
    expect(hasCommandModifier({ metaKey: false, ctrlKey: true }, true)).toBe(false);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: true }, true)).toBe(false);
  });

  it("requires Control exclusively on Windows and Linux", () => {
    expect(hasCommandModifier({ metaKey: false, ctrlKey: true }, false)).toBe(true);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: false }, false)).toBe(false);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: true }, false)).toBe(false);
  });
});

describe("messageStepDirection", () => {
  const press = (key: string, init: KeyboardEventInit, altGraph = false) => {
    const event = new KeyboardEvent("keydown", { key, ...init });
    if (altGraph) {
      Object.defineProperty(event, "getModifierState", {
        value: (modifier: string) => modifier === "AltGraph",
      });
    }
    return event;
  };

  it("steps with ⌘⌥↑/↓ on macOS and Ctrl+Alt+↑/↓ elsewhere", () => {
    expect(messageStepDirection(press("ArrowUp", { metaKey: true, altKey: true }), true)).toBe(
      "previous",
    );
    expect(messageStepDirection(press("ArrowDown", { ctrlKey: true, altKey: true }), false)).toBe(
      "next",
    );
    expect(messageStepDirection(press("ArrowDown", { ctrlKey: true }), false)).toBeNull();
    expect(messageStepDirection(press("ArrowLeft", { ctrlKey: true, altKey: true }), false)).toBe(
      null,
    );
  });

  it("leaves AltGr arrows alone on Windows and Linux, which report AltGr as Ctrl+Alt", () => {
    // The same event on both: AltGr held, reported with Ctrl and Alt.
    const altGraphDown = press("ArrowDown", { ctrlKey: true, altKey: true }, true);
    expect(messageStepDirection(altGraphDown, false)).toBeNull();
    const altGraphUp = press("ArrowUp", { ctrlKey: true, altKey: true }, true);
    expect(messageStepDirection(altGraphUp, false)).toBeNull();
  });

  it("still steps with ⌘⌥ on macOS, where Firefox reports Option as AltGr", () => {
    const optionUp = press("ArrowUp", { metaKey: true, altKey: true }, true);
    expect(messageStepDirection(optionUp, true)).toBe("previous");
    const optionDown = press("ArrowDown", { metaKey: true, altKey: true }, true);
    expect(messageStepDirection(optionDown, true)).toBe("next");
  });
});

describe("isKeyOwningOverlayOpen", () => {
  it("counts an open overlay unless it encloses the given element", () => {
    document.body.innerHTML =
      '<div aria-modal="true"><form id="inside"></form></div><form id="behind"></form>';
    expect(isKeyOwningOverlayOpen()).toBe(true);
    expect(isKeyOwningOverlayOpen(document.getElementById("behind"))).toBe(true);
    expect(isKeyOwningOverlayOpen(document.getElementById("inside"))).toBe(false);
    document.body.innerHTML = "";
    expect(isKeyOwningOverlayOpen()).toBe(false);
  });
});

describe("isEscapeOwnerOpen", () => {
  it("counts marked panels and open popovers, not closed ones", () => {
    document.body.innerHTML = '<div data-slot="popover-content" data-state="closed"></div>';
    expect(isEscapeOwnerOpen()).toBe(false);
    document.body.innerHTML = '<div data-slot="popover-content" data-state="open"></div>';
    expect(isEscapeOwnerOpen()).toBe(true);
    document.body.innerHTML = '<aside data-escape-owner=""></aside>';
    expect(isEscapeOwnerOpen()).toBe(true);
    document.body.innerHTML = "";
  });
});
