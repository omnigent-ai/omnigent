import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isExplicitlyUnread,
  markConversationUnread,
  resetReadStateForTests,
} from "./useUnseenConversations";
import { useToggleUnreadHotkey } from "./useToggleUnreadHotkey";

const { showToast } = vi.hoisted(() => ({ showToast: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn().mockResolvedValue({ ok: true, status: 204 }),
}));

const SESSION = { id: "conv-1", updated_at: 5_000 };

function press(init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key: "u",
    code: "KeyU",
    ctrlKey: true,
    altKey: true,
    ...init,
  });
  window.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  resetReadStateForTests();
  showToast.mockClear();
});
afterEach(cleanup);

describe("useToggleUnreadHotkey", () => {
  it("flags the open session unread, then read again, saying which", () => {
    renderHook(() => useToggleUnreadHotkey(SESSION, false));

    expect(press().defaultPrevented).toBe(true);
    expect(isExplicitlyUnread("conv-1")).toBe(true);
    expect(showToast).toHaveBeenLastCalledWith("Marked as unread");

    press();
    expect(isExplicitlyUnread("conv-1")).toBe(false);
    expect(showToast).toHaveBeenLastCalledWith("Marked as read");
  });

  it("matches ⌘⌥U on macOS and acts on the session open at the time", () => {
    const { rerender } = renderHook(({ target }) => useToggleUnreadHotkey(target, true), {
      initialProps: { target: SESSION },
    });
    expect(press().defaultPrevented).toBe(false);

    rerender({ target: { id: "conv-2", updated_at: 7_000 } });
    // Consuming the press matters here: Chrome and Safari bind ⌘⌥U to View Source.
    expect(press({ ctrlKey: false, metaKey: true, key: "Dead" }).defaultPrevented).toBe(true);
    expect(showToast).toHaveBeenLastCalledWith("Marked as unread");
    expect(isExplicitlyUnread("conv-1")).toBe(false);
    expect(isExplicitlyUnread("conv-2")).toBe(true);
  });

  it("marks a muted session read but never unread", () => {
    const muted = { ...SESSION, labels: { "omnigent.muted": "1" } };
    renderHook(() => useToggleUnreadHotkey(muted, false));
    expect(press().defaultPrevented).toBe(true);
    expect(isExplicitlyUnread("conv-1")).toBe(false);
    expect(showToast).toHaveBeenLastCalledWith("Unmute the session to mark it as unread.");

    // Unread state flagged before muting can still be cleared.
    markConversationUnread("conv-1", SESSION.updated_at);
    press();
    expect(isExplicitlyUnread("conv-1")).toBe(false);
    expect(showToast).toHaveBeenLastCalledWith("Marked as read");
  });

  it("leaves the chord alone without an open session", () => {
    renderHook(() => useToggleUnreadHotkey(null, false));
    expect(press().defaultPrevented).toBe(false);
    expect(showToast).not.toHaveBeenCalled();
  });
});
