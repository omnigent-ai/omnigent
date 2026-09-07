import { describe, expect, it } from "vitest";

import { isBackdropOverlay, isInsidePopper, shouldGuardDialogDismiss } from "./dialogDismissGuard";

/** Build a detached element tree and return the deepest child, so `.closest()`
 *  walks real ancestors the way it would in the DOM. */
function elementInside(...selectors: string[]): HTMLElement {
  let root: HTMLElement | null = null;
  let leaf: HTMLElement | null = null;
  for (const sel of selectors) {
    const el = document.createElement("div");
    // Selectors: `[attr="val"]` → set attr=val; `[attr]` → set attr="".
    const attrVal = sel.match(/^\[([^=\]]+)="([^"]+)"\]$/);
    const attrBare = sel.match(/^\[([^=\]]+)\]$/);
    if (attrVal) el.setAttribute(attrVal[1], attrVal[2]);
    else if (attrBare) el.setAttribute(attrBare[1], "");
    if (root === null) root = el;
    if (leaf !== null) leaf.appendChild(el);
    leaf = el;
  }
  return leaf as HTMLElement;
}

describe("isInsidePopper", () => {
  it("is true for a target inside dropdown-menu content", () => {
    const target = elementInside('[data-slot="dropdown-menu-content"]', "span");
    expect(isInsidePopper(target)).toBe(true);
  });

  it("is true for a target inside select content, popover content, or a listbox", () => {
    expect(isInsidePopper(elementInside('[data-slot="select-content"]', "span"))).toBe(true);
    expect(isInsidePopper(elementInside('[data-slot="popover-content"]', "span"))).toBe(true);
    expect(isInsidePopper(elementInside('[role="listbox"]', "span"))).toBe(true);
    expect(isInsidePopper(elementInside("[data-radix-popper-content-wrapper]", "span"))).toBe(true);
  });

  it("is false for a plain element and for null", () => {
    expect(isInsidePopper(elementInside("div", "span"))).toBe(false);
    expect(isInsidePopper(null)).toBe(false);
  });
});

describe("isBackdropOverlay", () => {
  it("is true only for the dialog overlay", () => {
    expect(isBackdropOverlay(elementInside('[data-slot="dialog-overlay"]'))).toBe(true);
    expect(isBackdropOverlay(elementInside('[data-slot="dropdown-menu-content"]'))).toBe(false);
    expect(isBackdropOverlay(null)).toBe(false);
  });
});

describe("shouldGuardDialogDismiss", () => {
  const plain = elementInside("div", "span");
  const popper = elementInside('[data-slot="dropdown-menu-content"]', "span");
  const overlay = elementInside('[data-slot="dialog-overlay"]');

  it("guards while a dropdown is open (even for a plain-body target)", () => {
    expect(shouldGuardDialogDismiss(plain, { selectOpen: true, msSinceSelectClose: 999 })).toBe(
      true,
    );
  });

  it("guards a click that landed inside portalled dropdown content", () => {
    expect(shouldGuardDialogDismiss(popper, { selectOpen: false, msSinceSelectClose: 999 })).toBe(
      true,
    );
  });

  it("guards the trailing focus-outside within the grace window", () => {
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 100 })).toBe(
      true,
    );
  });

  it("does not guard a plain outside click after the grace window", () => {
    expect(shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 999 })).toBe(
      false,
    );
  });

  it("never guards a genuine backdrop-overlay click, even during the grace window", () => {
    // Backdrop must always dismiss — this is the fix for click-to-close being
    // swallowed by the grace window.
    expect(shouldGuardDialogDismiss(overlay, { selectOpen: true, msSinceSelectClose: 0 })).toBe(
      false,
    );
  });

  it("honors a custom grace window", () => {
    expect(
      shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 200, graceMs: 300 }),
    ).toBe(true);
    expect(
      shouldGuardDialogDismiss(plain, { selectOpen: false, msSinceSelectClose: 400, graceMs: 300 }),
    ).toBe(false);
  });
});
