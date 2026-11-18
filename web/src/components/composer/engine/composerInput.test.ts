import { afterEach, describe, expect, it, vi } from "vitest";
import { isDraftingTarget } from "@/hooks/useApproveHotkey";
import { insertComposerText, insertPastedText } from "@/lib/composerText";
import {
  COMPOSER_INPUT_ATTRIBUTE,
  ENABLED_COMPOSER_INPUT_SELECTOR,
  composerTargetElement,
  composerTargetOf,
  isComposerInput,
  isComposerTargetFocused,
  registerComposerSurface,
} from "./composerInput";
import { createFakeComposerSurface } from "./testing/fakeComposerSurface";

function markedTextarea(value = "") {
  const textarea = document.createElement("textarea");
  textarea.setAttribute(COMPOSER_INPUT_ATTRIBUTE, "");
  textarea.value = value;
  document.body.append(textarea);
  return textarea;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("composer input targets", () => {
  it("treats a marked textarea as its own target", () => {
    const textarea = markedTextarea("draft");

    expect(isComposerInput(textarea)).toBe(true);
    expect(composerTargetOf(textarea)).toBe(textarea);
    expect(composerTargetElement(textarea)).toBe(textarea);
  });

  it("ignores text fields that aren't composer inputs", () => {
    const textarea = document.createElement("textarea");
    document.body.append(textarea);

    expect(isComposerInput(textarea)).toBe(false);
    expect(composerTargetOf(textarea)).toBeNull();
    expect(composerTargetOf(null)).toBeNull();
  });

  it("finds another engine's surface from its focusable node until it unregisters", () => {
    const surface = createFakeComposerSurface("draft");

    expect(composerTargetOf(surface.element)).toBe(surface);
    expect(composerTargetElement(surface)).toBe(surface.element);

    surface.unregister();
    expect(composerTargetOf(surface.element)).toBeNull();
  });

  it("keeps a replacement registered when the surface it replaced unregisters", () => {
    const first = createFakeComposerSurface("first");
    const second = { ...createFakeComposerSurface("second"), element: first.element };
    const unregisterSecond = registerComposerSurface(second);

    first.unregister();
    expect(composerTargetOf(first.element)).toBe(second);

    unregisterSecond();
    expect(composerTargetOf(first.element)).toBeNull();
  });

  it("reports focus on the surface's node", () => {
    const surface = createFakeComposerSurface();
    expect(isComposerTargetFocused(surface)).toBe(false);

    surface.focus();

    expect(isComposerTargetFocused(surface)).toBe(true);
  });

  it("matches only enabled composer inputs", () => {
    const enabled = markedTextarea();
    const disabled = markedTextarea();
    disabled.disabled = true;
    const surface = createFakeComposerSurface();
    const readOnlySurface = createFakeComposerSurface();
    readOnlySurface.element.setAttribute("aria-disabled", "true");

    expect([...document.querySelectorAll(ENABLED_COMPOSER_INPUT_SELECTOR)]).toEqual([
      enabled,
      surface.element,
    ]);
  });
});

describe("editing through a non-textarea surface", () => {
  it("inserts at the selection as the engine's own edit and reveals the caret", () => {
    const surface = createFakeComposerSurface("hello world");
    surface.setSelectionRange(6, 11);

    insertComposerText(surface, "there");

    expect(surface.inserts).toEqual([{ text: "there", start: 6, end: 11 }]);
    expect(surface.value).toBe("hello there");
    expect(surface.reveals).toBe(1);
  });

  it("replaces an explicit range", () => {
    const surface = createFakeComposerSurface("- item");
    surface.setSelectionRange(0, 0);

    insertComposerText(surface, "list", 2, 6);

    expect(surface.inserts).toEqual([{ text: "list", start: 2, end: 6 }]);
    expect(surface.value).toBe("- list");
  });

  it("inserts a short paste through the surface and claims the event", () => {
    const surface = createFakeComposerSurface("draft ");
    const preventDefault = vi.fn();
    const clipboardData = {
      getData: (type: string) => (type === "text/plain" ? "one\r\ntwo" : ""),
    } as unknown as DataTransfer;

    expect(insertPastedText({ clipboardData, currentTarget: surface, preventDefault })).toBe(true);

    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(surface.value).toBe("draft one\ntwo");
  });

  it("counts a surface holding text as a draft for the approve chord", () => {
    const surface = createFakeComposerSurface("");
    expect(isDraftingTarget(surface.element)).toBe(false);

    surface.value = "half a thought";

    expect(isDraftingTarget(surface.element)).toBe(true);
  });
});
