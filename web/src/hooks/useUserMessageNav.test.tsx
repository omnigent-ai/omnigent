// Invariants:
// - goPrev from outside-end → LAST message (intuitive after sending).
// - Anchor by itemId, not index, so the cursor survives id list shifts.
// - Stale anchor (id not in list) degrades to outside-end.
// - Assertions check the target element's data attr, not just "spy called".

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { useUserMessageNav } from "./useUserMessageNav";

function setupDom(ids: string[]): void {
  document.body.innerHTML = ids
    .map((id) => `<div data-user-message-id="${id}">user msg ${id}</div>`)
    .join("");
}

// jsdom lacks scrollIntoView; install a stub so vi.spyOn has a target.
if (!("scrollIntoView" in Element.prototype)) {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    writable: true,
    value: () => {},
  });
}

let scrollSpy: ReturnType<typeof vi.spyOn>;

// The flash is deferred until the smooth-scroll settles (see jumpTo). jsdom
// fires no scroll events, so the settle timer is the path that flashes; advance
// past it generously to trigger the flash in tests.
const SETTLE_MS = 200;

beforeEach(() => {
  vi.useFakeTimers();
  scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
  useChatStore.setState({ flashItemId: null });
});

afterEach(() => {
  scrollSpy.mockRestore();
  document.body.innerHTML = "";
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("useUserMessageNav", () => {
  it("goPrev from null anchor jumps to the LAST user message", () => {
    const ids = ["a", "b", "c"];
    setupDom(ids);

    const { result } = renderHook(() => useUserMessageNav(ids));
    expect(result.current.canPrev).toBe(true);
    expect(result.current.canNext).toBe(false);

    act(() => result.current.goPrev());

    // scrollIntoView called on the element whose attribute matches "c".
    const target = scrollSpy.mock.contexts[0] as Element;
    expect(target.getAttribute("data-user-message-id")).toBe("c");
    expect(useChatStore.getState().flashItemId).toBe(null);
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(useChatStore.getState().flashItemId).toBe("c");
  });

  it("repeated goPrev walks backward and stops at the first message", () => {
    const ids = ["a", "b", "c"];
    setupDom(ids);
    const { result } = renderHook(() => useUserMessageNav(ids));

    act(() => result.current.goPrev()); // outside → "c"
    act(() => result.current.goPrev()); // → "b"
    act(() => result.current.goPrev()); // → "a"
    act(() => result.current.goPrev()); // at "a" — no-op

    expect(scrollSpy).toHaveBeenCalledTimes(3);
    const ordered = scrollSpy.mock.contexts.map((el: unknown) =>
      (el as Element).getAttribute("data-user-message-id"),
    );
    expect(ordered).toEqual(["c", "b", "a"]);
    expect(result.current.canPrev).toBe(false);
    expect(result.current.canNext).toBe(true);
  });

  it("goNext from outside is a no-op (nothing to advance toward)", () => {
    const ids = ["a", "b"];
    setupDom(ids);
    const { result } = renderHook(() => useUserMessageNav(ids));

    act(() => result.current.goNext());

    expect(scrollSpy).not.toHaveBeenCalled();
    expect(useChatStore.getState().flashItemId).toBe(null);
  });

  it("goNext after goPrev walks forward and stops at the last message", () => {
    const ids = ["a", "b", "c"];
    setupDom(ids);
    const { result } = renderHook(() => useUserMessageNav(ids));

    act(() => result.current.goPrev()); // → "c"
    act(() => result.current.goPrev()); // → "b"
    act(() => result.current.goNext()); // → "c"
    act(() => result.current.goNext()); // at "c" — no-op

    const ordered = scrollSpy.mock.contexts.map((el: unknown) =>
      (el as Element).getAttribute("data-user-message-id"),
    );
    expect(ordered).toEqual(["c", "b", "c"]);
    expect(result.current.canNext).toBe(false);
  });

  it("empty id list disables both directions", () => {
    setupDom([]);
    const { result } = renderHook(() => useUserMessageNav([]));

    expect(result.current.canPrev).toBe(false);
    expect(result.current.canNext).toBe(false);
    act(() => result.current.goPrev());
    act(() => result.current.goNext());
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it("treats a stale anchor as outside-end so the next goPrev jumps to last", () => {
    // Pending→committed swap: anchor was "pend_1", promoted to real itemId.
    setupDom(["a", "b", "pend_1"]);
    const { result, rerender } = renderHook(
      ({ ids }: { ids: string[] }) => useUserMessageNav(ids),
      { initialProps: { ids: ["a", "b", "pend_1"] } },
    );

    act(() => result.current.goPrev()); // anchor → "pend_1"
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(useChatStore.getState().flashItemId).toBe("pend_1");

    // Promotion: DOM + ids swap together on the same render tick.
    setupDom(["a", "b", "c"]);
    rerender({ ids: ["a", "b", "c"] });

    // canPrev still true because we're "outside-end" again.
    expect(result.current.canPrev).toBe(true);
    expect(result.current.canNext).toBe(false);

    act(() => result.current.goPrev());
    // Should land on "c" (the LAST id in the new list), not "b".
    const lastCall = scrollSpy.mock.contexts.at(-1) as Element;
    expect(lastCall.getAttribute("data-user-message-id")).toBe("c");
  });

  it("warns and bails when the target DOM element is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    // ids list says "a" exists; DOM has no matching element.
    setupDom([]);
    const { result } = renderHook(() => useUserMessageNav(["a"]));

    act(() => result.current.goPrev());

    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toMatch(/no element/i);
    expect(scrollSpy).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("clears flashItemId on the timer", () => {
    setupDom(["a"]);
    const { result } = renderHook(() => useUserMessageNav(["a"]));

    act(() => result.current.goPrev());
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(useChatStore.getState().flashItemId).toBe("a");

    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(useChatStore.getState().flashItemId).toBe(null);
  });
});
