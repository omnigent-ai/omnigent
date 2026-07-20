import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OttoEyes } from "./OttoEyes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));

// Reads a pupil's live translate() transform, or null before the rAF
// pipeline has written one.
function transformOf(pupil: SVGGElement): { tx: number; ty: number } | null {
  const match = pupil.style.transform.match(/^translate\((-?[\d.]+)px, (-?[\d.]+)px\)$/);
  if (!match) return null;
  return { tx: Number(match[1]), ty: Number(match[2]) };
}

describe("OttoEyes", () => {
  it("renders the mascot with image semantics", () => {
    const { container } = render(<OttoEyes className="h-18" />);
    const svg = container.querySelector("svg");
    // The new-chat hero is a meaningful image, so the wrapper must override
    // OttoIcon's decorative aria-hidden default; losing the override would
    // silently hide the brand image from screen readers.
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Omnigent");
    expect(svg).toHaveAttribute("aria-hidden", "false");
    expect(svg).toHaveClass("h-18");
  });

  it("follows last-activity: centered on mount, pointer/caret trading off, focus alone doesn't move it", async () => {
    // A real textarea to focus. Its caret point is derived from its
    // getBoundingClientRect (mocked below); jsdom has no layout, so the
    // mirror-div offsets resolve to 0 and the caret lands at the field's
    // content origin — up and to the LEFT of both eye centers (x=10 <
    // eyeX≈41/60), which is all the direction math needs.
    const textarea = document.createElement("textarea");
    textarea.value = "hello";
    document.body.appendChild(textarea);
    vi.spyOn(textarea, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 90,
      right: 210,
      bottom: 130,
      width: 200,
      height: 40,
      x: 10,
      y: 90,
      toJSON: () => ({}),
    } as DOMRect);

    const { container } = render(<OttoEyes />);
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("OttoEyes did not render an svg");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));
    expect(pupils).toHaveLength(2);

    // On mount, before any activity, neither pupil has a transform written —
    // Otto rests centered until the user does something.
    await nextFrame();
    for (const pupil of pupils) {
      expect(transformOf(pupil)).toBeNull();
    }

    const farRightPointer = () =>
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1000, clientY: 50.84 }));

    // A pointermove far to the right is the first genuine activity: both
    // pupils ride the right rim (~+9.3) with ~zero vertical drift (the
    // pointer sits on the eyes' shared row).
    farRightPointer();
    await nextFrame();
    for (const pupil of pupils) {
      const t = transformOf(pupil);
      if (!t) throw new Error("pupil never received a transform");
      expect(t.tx).toBeCloseTo(9.3, 1);
      expect(Math.abs(t.ty)).toBeLessThan(0.1);
    }

    // Focusing the textarea alone must NOT switch the gaze to the caret: a
    // subsequent pointermove should still drive the pupils to the pointer.
    textarea.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    farRightPointer();
    await nextFrame();
    for (const pupil of pupils) {
      const t = transformOf(pupil);
      if (!t) throw new Error("pupil never received a transform");
      expect(t.tx).toBeCloseTo(9.3, 1);
    }

    // A user-initiated caret move (an `input` event, e.g. a keystroke) pulls
    // Otto's gaze to the caret — up and to the LEFT of both eyes, the
    // opposite sign from the far-right pointer.
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    for (const pupil of pupils) {
      const t = transformOf(pupil);
      if (!t) throw new Error("pupil never received a transform");
      expect(t.tx).toBeLessThan(0);
    }

    // Another pointermove swings the pupils back to the pointer, proving the
    // mouse always wins on its own move even with a field still focused.
    farRightPointer();
    await nextFrame();
    for (const pupil of pupils) {
      const t = transformOf(pupil);
      if (!t) throw new Error("pupil never received a transform");
      expect(t.tx).toBeCloseTo(9.3, 1);
    }

    textarea.remove();
  });

  it("ignores input/click on unrelated elements while a field is focused", async () => {
    const textarea = document.createElement("textarea");
    textarea.value = "hello";
    document.body.appendChild(textarea);
    const button = document.createElement("button");
    document.body.appendChild(button);

    const { container } = render(<OttoEyes />);
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("OttoEyes did not render an svg");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));

    // Field focused, but the pointer (far right) is the last mover.
    textarea.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1000, clientY: 50.84 }));
    await nextFrame();
    for (const pupil of pupils) {
      expect(transformOf(pupil)?.tx).toBeCloseTo(9.3, 1);
    }

    // input and click on an UNRELATED element must NOT switch the gaze to the
    // caret — the pupils stay on the pointer (right rim).
    button.dispatchEvent(new Event("input", { bubbles: true }));
    button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await nextFrame();
    for (const pupil of pupils) {
      expect(transformOf(pupil)?.tx).toBeCloseTo(9.3, 1);
    }

    textarea.remove();
    button.remove();
  });

  it("tracks the caret on first input when a field was already focused at mount", async () => {
    // Reproduces the autofocus race: the composer focuses before OttoEyes'
    // effect attaches its focusin listener, so that focusin is never heard.
    // Typing must still track the caret — the effect adopts the already-focused
    // field on mount.
    const textarea = document.createElement("textarea");
    textarea.value = "hello";
    document.body.appendChild(textarea);
    vi.spyOn(textarea, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 90,
      right: 210,
      bottom: 130,
      width: 200,
      height: 40,
      x: 10,
      y: 90,
      toJSON: () => ({}),
    } as DOMRect);
    // Focus BEFORE OttoEyes mounts — no focusin reaches the component.
    textarea.focus();
    expect(document.activeElement).toBe(textarea);

    const { container } = render(<OttoEyes />);
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("OttoEyes did not render an svg");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));

    // Still centered on mount — adopting the field must not move the gaze.
    await nextFrame();
    for (const pupil of pupils) {
      expect(transformOf(pupil)).toBeNull();
    }

    // A keystroke tracks the caret even though no focusin was ever heard.
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    for (const pupil of pupils) {
      const t = transformOf(pupil);
      if (!t) throw new Error("pupil never received a transform");
      expect(t.tx).toBeLessThan(0);
    }

    textarea.remove();
  });
});
