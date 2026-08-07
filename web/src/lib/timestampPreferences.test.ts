import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SHOW_MESSAGE_TIMESTAMPS,
  readShowMessageTimestamps,
  useShowMessageTimestamps,
  writeShowMessageTimestamps,
} from "./timestampPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("timestampPreferences", () => {
  it("defaults to on when nothing is stored", () => {
    expect(DEFAULT_SHOW_MESSAGE_TIMESTAMPS).toBe(true);
    expect(readShowMessageTimestamps()).toBe(true);
  });

  it("round-trips both boolean values", () => {
    writeShowMessageTimestamps(false);
    expect(readShowMessageTimestamps()).toBe(false);

    writeShowMessageTimestamps(true);
    expect(readShowMessageTimestamps()).toBe(true);
  });

  it('treats any non-"false" stored value as on (defensive against hand edits)', () => {
    // Only the exact string "false" disables the stamps; garbage or a stale
    // format falls back to the default (on) rather than silently hiding them.
    localStorage.setItem("omnigent:show-message-timestamps", "0");
    expect(readShowMessageTimestamps()).toBe(true);

    localStorage.setItem("omnigent:show-message-timestamps", "no");
    expect(readShowMessageTimestamps()).toBe(true);

    localStorage.setItem("omnigent:show-message-timestamps", "false");
    expect(readShowMessageTimestamps()).toBe(false);
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break
    // the transcript or settings.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeShowMessageTimestamps(false)).not.toThrow();
    expect(readShowMessageTimestamps()).toBe(true);
  });

  it("notifies mounted subscribers when the toggle flips", () => {
    // Message bubbles sit behind a React.memo boundary, so the Settings
    // toggle reaches them through the subscription hook — a write with no
    // notification would leave already-rendered bubbles stale.
    const { result } = renderHook(() => useShowMessageTimestamps());
    expect(result.current).toBe(true);

    act(() => writeShowMessageTimestamps(false));
    expect(result.current).toBe(false);

    act(() => writeShowMessageTimestamps(true));
    expect(result.current).toBe(true);
  });
});
