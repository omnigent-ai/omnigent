import { describe, expect, it } from "vitest";

import { hasCommandModifier, isEscapeOwnerOpen, isKeyOwningOverlayOpen } from "./hotkeys";

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
