import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  COMMAND_HOLD_REVEAL_DELAY_MS,
  useCommandHoldStore,
  useCommandHoldTracking,
} from "./useCommandHold";

type Modifiers = Partial<
  Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey" | "repeat">
>;

/** Mounts the tracker and reads `held` through a subscriber, as the sidebar hints do. */
function renderTracking(isMac: boolean, altJoinsHold = true) {
  return renderHook(() => {
    useCommandHoldTracking(isMac, altJoinsHold);
    return useCommandHoldStore((state) => state.held);
  });
}

/** Dispatches a key event on the window, where the tracker listens. */
function key(type: "keydown" | "keyup", keyName: string, modifiers: Modifiers = {}) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent(type, { key: keyName, ...modifiers }));
  });
}

function elapseRevealDelay() {
  act(() => {
    vi.advanceTimersByTime(COMMAND_HOLD_REVEAL_DELAY_MS);
  });
}

/** Forces `getModifierState("AltGraph")` to report the given value for every key event. */
function setAltGraph(active: boolean) {
  return vi
    .spyOn(KeyboardEvent.prototype, "getModifierState")
    .mockImplementation((modifier) => modifier === "AltGraph" && active);
}

const MAC_COMMAND: Modifiers = { metaKey: true };
const OTHER_COMMAND: Modifiers = { ctrlKey: true };

beforeEach(() => {
  vi.useFakeTimers();
  useCommandHoldStore.setState({ held: false });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  Reflect.deleteProperty(document, "visibilityState");
});

describe("useCommandHoldTracking on macOS", () => {
  it("does not reveal while a key pressed before ⌘ is still down", () => {
    const { result } = renderTracking(true);
    key("keydown", "a");
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(false);

    // Releasing and pressing ⌘ again doesn't forget the key still held.
    key("keyup", "Meta");
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(false);

    key("keyup", "a", MAC_COMMAND);
    key("keyup", "Meta");
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it.each([
    ["⌘C", MAC_COMMAND],
    ["⌃⌘C", { metaKey: true, ctrlKey: true }],
  ])("forgets a key whose keyup macOS drops while ⌘ is down (%s)", (_chord, modifiers) => {
    const { result } = renderTracking(true);
    // After the chord, macOS sends no keyup for C, only for the modifiers.
    key("keydown", "Meta", MAC_COMMAND);
    key("keydown", "c", modifiers);
    key("keyup", "Control");
    key("keyup", "Meta");

    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("reveals once the command key has been held for the delay", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);

    act(() => {
      vi.advanceTimersByTime(COMMAND_HOLD_REVEAL_DELAY_MS - 1);
    });
    expect(result.current).toBe(false);

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("never reveals for a tap shorter than the delay", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    key("keyup", "Meta");

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });

  it("cancels on a chord typed during the delay, so ⌘C never flashes hints", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    key("keydown", "c", MAC_COMMAND);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });

  it("hides at once when another key is pressed while held, including the digit", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);

    key("keydown", "1", MAC_COMMAND);
    expect(result.current).toBe(false);
  });

  it("hides on the command key's keyup even when the event still reports it held", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    // Another Meta key can still be down, so the flags may read held on this keyup.
    key("keyup", "Meta", MAC_COMMAND);
    expect(result.current).toBe(false);
  });

  it("hides at once on the command key's keyup", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    key("keyup", "Meta");
    expect(result.current).toBe(false);
  });

  it("does not start from a keyup while the command key stays down", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    key("keydown", "c", MAC_COMMAND);
    key("keyup", "c", MAC_COMMAND);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });

  it("keeps the hint in a browser tab while Alt joins the hold and when Alt is released", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    key("keydown", "Alt", { metaKey: true, altKey: true });
    expect(result.current).toBe(true);

    key("keyup", "Alt", MAC_COMMAND);
    expect(result.current).toBe(true);
  });

  it("reveals in a browser tab when Alt was already down before the command key", () => {
    const { result } = renderTracking(true);
    key("keydown", "Alt", { altKey: true });
    key("keydown", "Meta", { metaKey: true, altKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("hides in the desktop app when Alt joins, and stays hidden after Alt is released", () => {
    const { result } = renderTracking(true, false);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);

    key("keydown", "Alt", { metaKey: true, altKey: true });
    expect(result.current).toBe(false);

    key("keyup", "Alt", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(false);
  });

  it("does not reveal in the desktop app when Alt was already down before the command key", () => {
    const { result } = renderTracking(true, false);
    key("keydown", "Alt", { altKey: true });
    key("keydown", "Meta", { metaKey: true, altKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(false);
  });

  it("does not reveal in the desktop app when Alt went down in another window", () => {
    const { result } = renderTracking(true, false);
    // No Alt keydown reached the page; only the command key's modifier state shows it.
    key("keydown", "Meta", { metaKey: true, altKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(false);
  });

  it("does not restart the delay on a repeated command keydown", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    key("keydown", "Meta", MAC_COMMAND);

    act(() => {
      vi.advanceTimersByTime(COMMAND_HOLD_REVEAL_DELAY_MS - 300);
    });
    expect(result.current).toBe(true);
  });

  it("keeps the reveal deadline when Shift joins during the delay", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    key("keydown", "Shift", { metaKey: true, shiftKey: true });

    act(() => {
      vi.advanceTimersByTime(COMMAND_HOLD_REVEAL_DELAY_MS - 301);
    });
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(true);
  });

  it.each([
    ["Shift in a browser tab", "Shift", { metaKey: true, shiftKey: true }, true],
    ["Shift in the desktop app", "Shift", { metaKey: true, shiftKey: true }, false],
    ["Control", "Control", { metaKey: true, ctrlKey: true }, true],
  ])(
    "keeps the hint while %s joins the hold and when it is released",
    (_case, joining, modifiers, altJoinsHold) => {
      const { result } = renderTracking(true, altJoinsHold);
      key("keydown", "Meta", MAC_COMMAND);
      elapseRevealDelay();

      key("keydown", joining, modifiers);
      expect(result.current).toBe(true);

      key("keyup", joining, MAC_COMMAND);
      expect(result.current).toBe(true);

      key("keyup", "Meta");
      expect(result.current).toBe(false);
    },
  );

  it.each([
    ["Shift", "Shift", { shiftKey: true }],
    ["Control", "Control", { ctrlKey: true }],
  ])("reveals when %s was already down before ⌘", (_case, joining, modifiers) => {
    const { result } = renderTracking(true, false);
    key("keydown", joining, modifiers);
    key("keydown", "Meta", { metaKey: true, ...modifiers });

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("hides when a key completes the ⇧⌘ chord", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    key("keydown", "Shift", { metaKey: true, shiftKey: true });
    elapseRevealDelay();
    expect(result.current).toBe(true);

    key("keydown", "O", { metaKey: true, shiftKey: true });
    expect(result.current).toBe(false);
  });

  it("ignores Ctrl alone", () => {
    const { result } = renderTracking(true);
    key("keydown", "Control", OTHER_COMMAND);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });
});

describe("useCommandHoldTracking off macOS", () => {
  it("does not reveal while a key pressed before Ctrl is still down", () => {
    const { result } = renderTracking(false);
    key("keydown", "a");
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(false);

    // Off macOS the keyup for a still arrives, so releasing Ctrl doesn't forget it.
    key("keyup", "Control");
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(false);
  });

  it("cancels on a chord that another capture-phase handler stops", () => {
    const { result } = renderTracking(false);
    // The recent-sessions switcher stops Ctrl+Tab this way.
    const stopTab = (e: KeyboardEvent) => {
      if (e.key === "Tab") e.stopPropagation();
    };
    window.addEventListener("keydown", stopTab, true);
    try {
      for (const keyName of ["Control", "Tab"]) {
        act(() => {
          document.body.dispatchEvent(
            new KeyboardEvent("keydown", { key: keyName, ctrlKey: true, bubbles: true }),
          );
        });
      }
      elapseRevealDelay();
      expect(result.current).toBe(false);
    } finally {
      window.removeEventListener("keydown", stopTab, true);
    }
  });

  it("uses Ctrl as the command key", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("ignores Meta, which is not the command key there", () => {
    const { result } = renderTracking(false);
    key("keydown", "Meta", MAC_COMMAND);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });

  it("keeps the hint while Shift joins Ctrl, as in Ctrl+Shift+O", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();

    key("keydown", "Shift", { ctrlKey: true, shiftKey: true });
    expect(result.current).toBe(true);
    key("keyup", "Shift", OTHER_COMMAND);
    expect(result.current).toBe(true);
  });

  it("reveals when Shift was already down before Ctrl", () => {
    const { result } = renderTracking(false);
    key("keydown", "Shift", { shiftKey: true });
    key("keydown", "Control", { ctrlKey: true, shiftKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("hides when Meta joins Ctrl, since it is not a joining key there", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();

    key("keydown", "Meta", { ctrlKey: true, metaKey: true });
    expect(result.current).toBe(false);
  });

  it("ignores Ctrl together with Meta", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", { ctrlKey: true, metaKey: true });

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });

  it("keeps the hint while Alt is held with Ctrl, the browser chord", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);
    key("keydown", "Alt", { ctrlKey: true, altKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("cancels on Alt with Ctrl in the desktop app, which has no Ctrl+Alt session chord", () => {
    const { result } = renderTracking(false, false);
    key("keydown", "Control", OTHER_COMMAND);
    key("keydown", "Alt", { ctrlKey: true, altKey: true });

    elapseRevealDelay();
    expect(result.current).toBe(false);
  });

  it("ignores AltGraph, which intl layouts report as Ctrl+Alt", () => {
    const { result } = renderTracking(false);
    setAltGraph(true);
    key("keydown", "Control", { ctrlKey: true, altKey: true });

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);

    // Once AltGraph lifts, Shift alone cannot revive the hold; a fresh Ctrl press can.
    setAltGraph(false);
    key("keyup", "AltGraph", OTHER_COMMAND);
    key("keydown", "Shift", { ctrlKey: true, shiftKey: true });
    elapseRevealDelay();
    expect(result.current).toBe(false);

    key("keyup", "Control");
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);
  });

  it("hides when AltGraph is pressed during the delay", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);
    setAltGraph(true);
    key("keydown", "AltGraph", { ctrlKey: true, altKey: true });

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(false);
  });
});

describe("useCommandHoldTracking after a canceled hold", () => {
  it.each([
    ["a chord key on macOS", true, true, "c", { metaKey: true }],
    ["a chord key off macOS", false, true, "c", { ctrlKey: true }],
    ["Alt in the desktop app", false, false, "Alt", { ctrlKey: true, altKey: true }],
  ] as const)(
    "keeps a hold canceled by %s hidden when Shift joins, until the command key is pressed again",
    (_case, isMac, altJoinsHold, cancelKey, cancelModifiers) => {
      const { result } = renderTracking(isMac, altJoinsHold);
      const commandKey = isMac ? "Meta" : "Control";
      const command = isMac ? MAC_COMMAND : OTHER_COMMAND;
      key("keydown", commandKey, command);
      elapseRevealDelay();
      key("keydown", cancelKey, cancelModifiers);
      key("keyup", cancelKey, command);
      expect(result.current).toBe(false);

      key("keydown", "Shift", { ...command, shiftKey: true });
      elapseRevealDelay();
      expect(result.current).toBe(false);

      key("keyup", "Shift", command);
      key("keyup", commandKey);
      key("keydown", commandKey, command);
      elapseRevealDelay();
      expect(result.current).toBe(true);
    },
  );
});

describe("useCommandHoldTracking after a missed command keyup", () => {
  it("stays hidden on a repeated command keydown but recovers on a fresh press", () => {
    const { result } = renderTracking(false);
    key("keydown", "Control", OTHER_COMMAND);
    key("keydown", "c", OTHER_COMMAND);
    key("keyup", "c", OTHER_COMMAND);

    key("keydown", "Control", { ...OTHER_COMMAND, repeat: true });
    elapseRevealDelay();
    expect(result.current).toBe(false);

    // The Ctrl keyup was swallowed, so the next press arrives without one.
    key("keydown", "Control", OTHER_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);
  });
});

describe("useCommandHoldTracking window focus", () => {
  it("hides on window blur", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    expect(result.current).toBe(false);
  });

  it("hides when the page becomes hidden", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe(false);
  });

  it("keeps the hint when the page becomes visible again", () => {
    const { result } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe(true);
  });

  it("clears a live hold on unmount", () => {
    const { result, unmount } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    elapseRevealDelay();
    expect(result.current).toBe(true);

    unmount();
    expect(useCommandHoldStore.getState().held).toBe(false);
  });

  it("cancels a pending reveal on unmount", () => {
    const { unmount } = renderTracking(true);
    key("keydown", "Meta", MAC_COMMAND);
    unmount();

    elapseRevealDelay();
    expect(useCommandHoldStore.getState().held).toBe(false);
  });
});
