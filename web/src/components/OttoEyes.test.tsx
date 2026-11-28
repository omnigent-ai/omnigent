import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OttoEyes } from "./OttoEyes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const nextFrame = () =>
  new Promise((resolve) => {
    requestAnimationFrame(() => resolve(undefined));
  });

// Reads a pupil's live translate() transform, or null before the rAF
// pipeline has written one.
function transformOf(pupil: SVGGElement): { tx: number; ty: number } | null {
  const match = pupil.style.transform.match(/^translate\((-?[\d.]+)px, (-?[\d.]+)px\)$/);
  if (!match) return null;
  return { tx: Number(match[1]), ty: Number(match[2]) };
}

// The hidden host OttoEyes keeps under <body> for its caret mirror.
const mirrorHost = () => document.querySelector<HTMLElement>('body > div[aria-hidden="true"]');

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

  it("keeps one caret mirror attached across keystrokes and removes it on unmount", async () => {
    // Inserting or removing a node under <body> invalidates style for the whole
    // document (tens of ms per keystroke in WebKit), so typing must only touch
    // a mirror that is already attached.
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const { unmount } = render(<OttoEyes />);
    textarea.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));

    const records: MutationRecord[] = [];
    const bodyChildren = new MutationObserver((batch) => records.push(...batch));
    bodyChildren.observe(document.body, { childList: true });
    const type = (value: string) => {
      textarea.value = value;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      return nextFrame();
    };
    await type("h");
    await type("he");
    await type("hel");
    records.push(...bodyChildren.takeRecords());
    bodyChildren.disconnect();

    const added = records.flatMap((record) => Array.from(record.addedNodes));
    const removed = records.flatMap((record) => Array.from(record.removedNodes));
    expect(added).toHaveLength(1);
    expect(removed).toHaveLength(0);
    const host = added[0] as HTMLElement;
    expect(host.style.position).toBe("fixed");
    expect(host.style.contain).toBe("strict");
    expect(host).toHaveAttribute("aria-hidden", "true");
    expect(host.textContent).toBe("");

    unmount();
    expect(host.isConnected).toBe(false);
    textarea.remove();
  });

  it("measures a password but leaves no trace of it once the field is gone", async () => {
    // The API-key field in the credential dialog is a password input, and the
    // dialog can be cancelled while Otto stays mounted.
    const secret = "sk-synthetic-secret-1234";
    const input = document.createElement("input");
    input.type = "password";
    document.body.appendChild(input);
    let measuredText: string | null | undefined;
    vi.spyOn(input, "getBoundingClientRect").mockImplementation(() => {
      measuredText = mirrorHost()?.textContent;
      return new DOMRect(10, 90, 200, 40);
    });
    render(<OttoEyes />);
    input.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));

    input.value = secret;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    expect(measuredText).toBe(secret);

    input.remove();
    const host = mirrorHost();
    expect(host?.isConnected).toBe(true);
    expect(host?.textContent).toBe("");
    expect(document.body.innerHTML).not.toContain(secret);
  });

  it("clears the caret mirror when measuring the caret throws", async () => {
    const secret = "sk-synthetic-secret-5678";
    const input = document.createElement("input");
    input.type = "password";
    input.value = secret;
    document.body.appendChild(input);
    const failure = new Error("layout unavailable");
    vi.spyOn(input, "getBoundingClientRect").mockImplementation(() => {
      throw failure;
    });
    const errors: unknown[] = [];
    const onError = (e: ErrorEvent) => {
      errors.push(e.error);
      e.preventDefault();
    };
    window.addEventListener("error", onError);
    render(<OttoEyes />);
    input.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));

    input.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    window.removeEventListener("error", onError);

    expect(errors).toEqual([failure]);
    expect(mirrorHost()?.textContent).toBe("");
    expect(document.body.innerHTML).not.toContain(secret);
    input.remove();
  });

  it("restyles the kept caret mirror when the field's width changes", async () => {
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const width = vi.spyOn(textarea, "clientWidth", "get").mockReturnValue(300);
    render(<OttoEyes />);
    textarea.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    const mirrorWidth = () =>
      document.querySelector<HTMLElement>('div[aria-hidden="true"] > div')?.style.width;

    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    expect(mirrorWidth()).toBe("300px");

    width.mockReturnValue(180);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await nextFrame();
    expect(mirrorWidth()).toBe("180px");

    textarea.remove();
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
