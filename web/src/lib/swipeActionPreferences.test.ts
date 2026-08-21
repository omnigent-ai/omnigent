import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_SWIPE_ACTIONS,
  isSwipeAction,
  normalizeSwipeActions,
  readSwipeActions,
  useSwipeActions,
  writeSwipeActions,
} from "./swipeActionPreferences";

const STORAGE_KEY = "omnigent:swipe-actions";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("swipeActionPreferences — read/write", () => {
  it("returns the defaults when nothing is stored", () => {
    expect(readSwipeActions()).toEqual(DEFAULT_SWIPE_ACTIONS);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("round-trips a written preference", () => {
    writeSwipeActions({ left: "delete", right: "archive" });
    expect(readSwipeActions()).toEqual({ left: "delete", right: "archive" });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual({
      left: "delete",
      right: "archive",
    });
  });

  it("allows both directions to map to the same action", () => {
    writeSwipeActions({ left: "archive", right: "archive" });
    expect(readSwipeActions()).toEqual({ left: "archive", right: "archive" });
  });

  it("persists a 'none' direction", () => {
    writeSwipeActions({ left: "none", right: "delete" });
    expect(readSwipeActions()).toEqual({ left: "none", right: "delete" });
  });
});

describe("isSwipeAction", () => {
  it("accepts the known actions and rejects everything else", () => {
    expect(isSwipeAction("archive")).toBe(true);
    expect(isSwipeAction("delete")).toBe(true);
    expect(isSwipeAction("none")).toBe(true);
    expect(isSwipeAction("bogus")).toBe(false);
    expect(isSwipeAction(null)).toBe(false);
    expect(isSwipeAction(undefined)).toBe(false);
    expect(isSwipeAction(42)).toBe(false);
  });
});

describe("normalizeSwipeActions", () => {
  it("passes through a valid object", () => {
    expect(normalizeSwipeActions({ left: "delete", right: "none" })).toEqual({
      left: "delete",
      right: "none",
    });
  });

  it("fills absent directions from the defaults", () => {
    expect(normalizeSwipeActions({ left: "delete" })).toEqual({
      left: "delete",
      right: DEFAULT_SWIPE_ACTIONS.right,
    });
  });

  it("makes a present but unrecognized action inert", () => {
    expect(normalizeSwipeActions({ left: "trash", right: "archive" })).toEqual({
      left: "none",
      right: "archive",
    });
  });

  it("maps null, arrays, and primitives to the defaults", () => {
    expect(normalizeSwipeActions(null)).toEqual(DEFAULT_SWIPE_ACTIONS);
    expect(normalizeSwipeActions("nope")).toEqual(DEFAULT_SWIPE_ACTIONS);
    expect(normalizeSwipeActions(123)).toEqual(DEFAULT_SWIPE_ACTIONS);
    expect(normalizeSwipeActions([])).toEqual(DEFAULT_SWIPE_ACTIONS);
  });
});

describe("readSwipeActions — corrupt storage", () => {
  it("returns the safe defaults when localStorage access throws", () => {
    const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage")!;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get: () => {
        throw new DOMException("Storage is unavailable", "SecurityError");
      },
    });

    try {
      expect(readSwipeActions()).toEqual(DEFAULT_SWIPE_ACTIONS);
    } finally {
      Object.defineProperty(window, "localStorage", descriptor);
    }
  });

  it("falls back to the defaults on unparseable JSON", () => {
    localStorage.setItem(STORAGE_KEY, "{not json");
    expect(readSwipeActions()).toEqual(DEFAULT_SWIPE_ACTIONS);
  });

  it("normalizes a partially-valid stored object", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ left: "delete", right: "bogus" }));
    expect(readSwipeActions()).toEqual({ left: "delete", right: DEFAULT_SWIPE_ACTIONS.right });
  });

  it("makes an unrecognized stored action inert", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ left: "trash", right: "archive" }));
    expect(readSwipeActions()).toEqual({ left: "none", right: "archive" });
  });
});

describe("useSwipeActions — live updates", () => {
  it("starts at the current stored value", () => {
    writeSwipeActions({ left: "delete", right: "archive" });
    const { result } = renderHook(() => useSwipeActions());
    expect(result.current).toEqual({ left: "delete", right: "archive" });
  });

  it("re-renders on a same-tab write (Settings change updates mounted rows)", () => {
    const { result } = renderHook(() => useSwipeActions());
    expect(result.current).toEqual(DEFAULT_SWIPE_ACTIONS);

    act(() => {
      writeSwipeActions({ left: "delete", right: "delete" });
    });
    expect(result.current).toEqual({ left: "delete", right: "delete" });
  });

  it("re-renders on a cross-tab storage event", () => {
    const { result } = renderHook(() => useSwipeActions());

    // Another tab wrote the new value; simulate the DOM `storage` broadcast.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ left: "none", right: "archive" }));
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });
    expect(result.current).toEqual({ left: "none", right: "archive" });
  });

  it("ignores storage events for unrelated keys", () => {
    const { result } = renderHook(() => useSwipeActions());
    const before = result.current;

    localStorage.setItem("some:other:key", "x");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "some:other:key" }));
    });
    // Same reference — no spurious re-render/value change.
    expect(result.current).toBe(before);
  });

  it("keeps a stable snapshot reference when the value is unchanged", () => {
    writeSwipeActions({ left: "archive", right: "delete" });
    const { result } = renderHook(() => useSwipeActions());
    const first = result.current;

    // A write of the identical value still pings subscribers; the value must
    // stay referentially stable so consumers don't needlessly re-run effects.
    act(() => {
      writeSwipeActions({ left: "archive", right: "delete" });
    });
    expect(result.current).toBe(first);
  });
});
