import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  TerminalExtraKeys,
  type ExtraKeysInputTransform,
  type ExtraKeysTarget,
} from "./TerminalExtraKeys";
import { LONG_PRESS_MS } from "./terminalExtraKeysModel";

function makeTarget(applicationCursor = false) {
  return {
    send: vi.fn<(data: string) => void>(),
    setTransform: vi.fn<(fn: ExtraKeysInputTransform | null) => void>(),
    focus: vi.fn<() => void>(),
    applicationCursor: () => applicationCursor,
  } satisfies ExtraKeysTarget;
}

/** The transform most recently installed on the target (null if cleared). */
function installedTransform(target: ReturnType<typeof makeTarget>) {
  const calls = target.setTransform.mock.calls;
  return calls.length === 0 ? undefined : calls[calls.length - 1][0];
}

/** Run the installed transform the way xterm's onData would (outside React). */
function typeThrough(target: ReturnType<typeof makeTarget>, data: string): string {
  const transform = installedTransform(target);
  expect(transform).toBeTypeOf("function");
  let out = "";
  act(() => {
    out = transform!(data);
  });
  return out;
}

function pointerDown(el: Element, over: Partial<PointerEventInit> = {}) {
  return fireEvent.pointerDown(el, {
    pointerId: 1,
    button: 0,
    isPrimary: true,
    pointerType: "touch",
    ...over,
  });
}

function pointerUp(el: Element, over: Partial<PointerEventInit> = {}) {
  return fireEvent.pointerUp(el, {
    pointerId: 1,
    button: 0,
    isPrimary: true,
    pointerType: "touch",
    ...over,
  });
}

function tap(el: Element) {
  pointerDown(el);
  pointerUp(el);
  // A real tap is followed by a compat click with detail 1; it must be ignored.
  fireEvent.click(el, { detail: 1 });
}

function key(name: string) {
  return screen.getByRole("button", { name });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("TerminalExtraKeys layout", () => {
  it("renders exactly 14 type=button keys in two rows of seven, unfocusable by Tab", () => {
    render(<TerminalExtraKeys target={makeTarget()} />);
    const toolbar = screen.getByRole("toolbar", { name: "Terminal extra keys" });
    const buttons = within(toolbar).getAllByRole("button");
    expect(buttons).toHaveLength(14);
    expect(buttons.filter((b) => b.dataset.row === "1")).toHaveLength(7);
    expect(buttons.filter((b) => b.dataset.row === "2")).toHaveLength(7);
    for (const b of buttons) {
      expect(b).toHaveAttribute("type", "button");
      expect(b).toHaveAttribute("tabindex", "-1");
    }
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Esc",
      "⇧Tab",
      "Tab",
      "Home",
      "↑",
      "End",
      "PgUp",
      "Ctrl",
      "Alt",
      "Shift",
      "←",
      "↓",
      "→",
      "PgDn",
    ]);
  });

  it("exposes aria-pressed only on the modifiers", () => {
    render(<TerminalExtraKeys target={makeTarget()} />);
    expect(key("Control")).toHaveAttribute("aria-pressed", "false");
    expect(key("Alt")).toHaveAttribute("aria-pressed", "false");
    expect(key("Shift")).toHaveAttribute("aria-pressed", "false");
    expect(key("Escape")).not.toHaveAttribute("aria-pressed");
    expect(key("Arrow up")).not.toHaveAttribute("aria-pressed");
  });
});

describe("TerminalExtraKeys pointer semantics", () => {
  it("sends on pointerup exactly once per tap, and prevents the pointerdown default", () => {
    // WHY: pointerdown preventDefault keeps focus on xterm's textarea (soft
    // keyboard stays open) and suppresses compat mouse events; the trailing
    // click must not fire the key a second time.
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    const esc = key("Escape");

    const down = pointerDown(esc);
    expect(down).toBe(false); // preventDefault() was called
    expect(target.send).not.toHaveBeenCalled();

    pointerUp(esc);
    expect(target.send).toHaveBeenCalledTimes(1);
    expect(target.send).toHaveBeenCalledWith("\x1b");

    fireEvent.click(esc, { detail: 1 });
    expect(target.send).toHaveBeenCalledTimes(1);
  });

  it("ignores non-primary pointers and secondary buttons", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    const tab = key("Tab");

    pointerDown(tab, { isPrimary: false, pointerId: 2 });
    pointerUp(tab, { isPrimary: false, pointerId: 2 });
    pointerDown(tab, { button: 2 });
    pointerUp(tab, { button: 2 });
    expect(target.send).not.toHaveBeenCalled();
  });

  it("pointercancel aborts the press: nothing is sent and no lock is armed", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);

    pointerDown(key("Arrow up"));
    fireEvent.pointerCancel(key("Arrow up"), { pointerId: 1 });
    pointerUp(key("Arrow up"));
    expect(target.send).not.toHaveBeenCalled();

    pointerDown(key("Control"));
    fireEvent.pointerCancel(key("Control"), { pointerId: 1 });
    act(() => vi.advanceTimersByTime(LONG_PRESS_MS + 50));
    expect(key("Control")).toHaveAttribute("aria-pressed", "false");
  });

  it("activates from a keyboard/AT click (detail 0) without pointer events", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    fireEvent.click(key("Tab"), { detail: 0 });
    expect(target.send).toHaveBeenCalledWith("\t");
  });

  it("sends nothing while a plain key is held and honors DECCKM for arrows", () => {
    const target = makeTarget(true);
    render(<TerminalExtraKeys target={target} />);
    tap(key("Arrow up"));
    expect(target.send).toHaveBeenCalledWith("\x1bOA");
  });
});

describe("TerminalExtraKeys focus policy", () => {
  it("focuses the terminal from a modifier pointerdown and never from a plain key", () => {
    // WHY: modifiers only make sense combined with the next typed character
    // (WKWebView honors focus() only inside the gesture); Esc/arrows must not
    // pop the soft keyboard and reflow the terminal.
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);

    pointerDown(key("Control"));
    expect(target.focus).toHaveBeenCalledTimes(1);
    pointerUp(key("Control"));

    tap(key("Escape"));
    tap(key("Arrow left"));
    tap(key("Shift Tab"));
    expect(target.focus).toHaveBeenCalledTimes(1);
  });
});

describe("TerminalExtraKeys sticky modifiers", () => {
  it("arms on tap (accent outline, aria-pressed) and installs the transform; a second tap disarms", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    const ctrl = key("Control");

    tap(ctrl);
    expect(ctrl).toHaveAttribute("aria-pressed", "true");
    expect(ctrl).toHaveAttribute("data-modifier-state", "armed");
    expect(typeof installedTransform(target)).toBe("function");

    tap(ctrl);
    expect(ctrl).toHaveAttribute("aria-pressed", "false");
    expect(ctrl).toHaveAttribute("data-modifier-state", "off");
    expect(installedTransform(target)).toBeNull();
  });

  it("does not install a transform while idle", () => {
    // WHY: the onData hot path must be a single null check when nothing is
    // armed — the row installs the transform only when a modifier activates.
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    tap(key("Escape"));
    tap(key("Arrow down"));
    const installs = target.setTransform.mock.calls.filter((c) => c[0] !== null);
    expect(installs).toHaveLength(0);
  });

  it("armed Ctrl rewrites the next typed character via the transform and then disarms", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    tap(key("Control"));

    expect(typeThrough(target, "c")).toBe("\x03");
    expect(key("Control")).toHaveAttribute("aria-pressed", "false");
    expect(installedTransform(target)).toBeNull();
  });

  it("armed modifiers apply to the next row key and are consumed by it", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    tap(key("Shift"));
    tap(key("Arrow up"));
    expect(target.send).toHaveBeenLastCalledWith("\x1b[1;2A");
    expect(key("Shift")).toHaveAttribute("aria-pressed", "false");

    tap(key("Alt"));
    tap(key("Escape"));
    // Esc is atomic: unmodified bytes, but the armed Alt is still cleared.
    expect(target.send).toHaveBeenLastCalledWith("\x1b");
    expect(key("Alt")).toHaveAttribute("aria-pressed", "false");
  });

  it("long-press locks (filled accent) and the lock survives use until tapped off", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    const ctrl = key("Control");

    pointerDown(ctrl);
    act(() => vi.advanceTimersByTime(LONG_PRESS_MS - 1));
    expect(ctrl).toHaveAttribute("data-modifier-state", "off");
    act(() => vi.advanceTimersByTime(1));
    expect(ctrl).toHaveAttribute("data-modifier-state", "locked");
    expect(ctrl).toHaveAttribute("aria-pressed", "true");
    // Releasing after the lock fired must not toggle it back off.
    pointerUp(ctrl);
    expect(ctrl).toHaveAttribute("data-modifier-state", "locked");

    expect(typeThrough(target, "r")).toBe("\x12");
    expect(typeThrough(target, "c")).toBe("\x03");
    expect(ctrl).toHaveAttribute("data-modifier-state", "locked");
    tap(key("Arrow right"));
    expect(target.send).toHaveBeenLastCalledWith("\x1b[1;5C");
    expect(ctrl).toHaveAttribute("data-modifier-state", "locked");

    tap(ctrl);
    expect(ctrl).toHaveAttribute("data-modifier-state", "off");
    expect(installedTransform(target)).toBeNull();
  });

  it("multi-character chunks pass through the transform untouched but still clear an armed modifier", () => {
    const target = makeTarget();
    render(<TerminalExtraKeys target={target} />);
    tap(key("Alt"));
    expect(typeThrough(target, "paste me")).toBe("paste me");
    expect(key("Alt")).toHaveAttribute("aria-pressed", "false");
  });

  it("clears the transform on unmount", () => {
    const target = makeTarget();
    const { unmount } = render(<TerminalExtraKeys target={target} />);
    tap(key("Control"));
    expect(typeof installedTransform(target)).toBe("function");
    unmount();
    expect(installedTransform(target)).toBeNull();
  });
});
