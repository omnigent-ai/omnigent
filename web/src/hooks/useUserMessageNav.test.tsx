// Invariants:
// - goPrev from outside-end → LAST message (intuitive after sending).
// - Anchor by itemId, not index, so the cursor survives id list shifts.
// - Stale anchor (id not in list) degrades to outside-end.
// - Assertions check the target element's data attr, not just "spy called".

import type * as ConversationModule from "@/components/ai-elements/conversation";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { firstFindRange } from "@/components/chat/chatFindReveal";
import { useChatStore } from "@/store/chatStore";
import { scrollToMessage, useUserMessageNav } from "./useUserMessageNav";

const releaseScrollLock = vi.hoisted(() => vi.fn());
vi.mock("@/components/ai-elements/conversation", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationModule>();
  return {
    ...actual,
    releaseConversationScrollLock: releaseScrollLock,
  };
});

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
  releaseScrollLock.mockClear();
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
    // StickToBottom bottom-lock must be released before the jump or a
    // tall multi-message transcript yanks back to the bottom on resize.
    expect(releaseScrollLock).toHaveBeenCalledOnce();
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

  it("pulls a windowed-out target into the DOM via ensureItemVisible, then scrolls to it", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    // rAF isn't real under fake timers; run the deferred retry synchronously.
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 0;
    });
    // The row for "a" is windowed out (no DOM node). ensureItemVisible reports
    // it scrolled it into the mounted range and mounts the node on the retry.
    setupDom([]);
    const ensureItemVisible = vi.fn((id: string) => {
      setupDom([id]);
      return true;
    });
    const { result } = renderHook(() => useUserMessageNav(["a"], ensureItemVisible));

    act(() => result.current.goPrev());

    expect(ensureItemVisible).toHaveBeenCalledWith("a");
    expect(warn).not.toHaveBeenCalled();
    const target = scrollSpy.mock.contexts.at(-1) as Element;
    expect(target.getAttribute("data-user-message-id")).toBe("a");

    raf.mockRestore();
    warn.mockRestore();
  });

  it("clears flashItemId on the timer", () => {
    setupDom(["a"]);
    const { result } = renderHook(() => useUserMessageNav(["a"]));

    act(() => result.current.goPrev());
    act(() => vi.advanceTimersToNextTimer());
    expect(useChatStore.getState().flashItemId).toBe("a");

    act(() => {
      vi.advanceTimersByTime(1499);
    });
    expect(useChatStore.getState().flashItemId).toBe("a");
    act(() => vi.advanceTimersByTime(1));
    expect(useChatStore.getState().flashItemId).toBe(null);
  });
});

describe("scrollToMessage with a focus range", () => {
  function box(top: number, height: number) {
    return () => ({ top, height, bottom: top + height }) as DOMRect;
  }

  it("centers the range in the panel around it at once and in the transcript smoothly", () => {
    document.body.innerHTML = `
      <div id="transcript" style="overflow-y: auto">
        <div data-message-id="resp_1"><div id="panel" style="overflow-y: auto"><pre>found zorblat</pre></div></div>
      </div>`;
    const transcript = document.getElementById("transcript")!;
    const panel = document.getElementById("panel")!;
    transcript.getBoundingClientRect = box(0, 800);
    panel.getBoundingClientRect = box(100, 200);
    const scrollBy = vi.fn();
    transcript.scrollBy = scrollBy;
    let panelScrollTop = 0;
    Object.defineProperty(panel, "scrollTop", {
      get: () => panelScrollTop,
      set: (value: number) => (panelScrollTop = value),
    });
    const range = document.createRange();
    range.selectNodeContents(panel.querySelector("pre")!.firstChild!);
    range.getBoundingClientRect = box(500, 20);

    scrollToMessage("resp_1", undefined, undefined, () => range);

    // 500 - 100 - (200 - 20) / 2, then 500 - 0 - (800 - 20) / 2 in the transcript.
    expect(panelScrollTop).toBe(310);
    expect(scrollBy).toHaveBeenCalledWith({ top: 110, behavior: "smooth" });
    expect(scrollSpy).not.toHaveBeenCalled();

    // A row above re-measured during the scroll and moved the word off screen.
    const flash = vi.fn();
    scrollToMessage("resp_1", flash, undefined, () => range);
    range.getBoundingClientRect = box(3000, 20);
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(scrollBy).toHaveBeenLastCalledWith({ top: 2610, behavior: "auto" });
    expect(flash).toHaveBeenCalledWith("resp_1");
  });
});

describe("scrollToMessage across the bubbles of one response", () => {
  it("centers the found word in the bubble that holds the current match", () => {
    // One response renders as several bubbles; the word also sits in the first.
    document.body.innerHTML = `
      <div id="transcript" style="overflow-y: auto">
        <div data-message-id="resp_1"><p id="early">an earlier zorblat</p></div>
        <div data-message-id="resp_1"><div data-chat-find-current><pre id="hit">found zorblat</pre></div></div>
      </div>`;
    const transcript = document.getElementById("transcript")!;
    transcript.getBoundingClientRect = () => ({ top: 0, height: 800, bottom: 800 }) as DOMRect;
    const scrollBy = vi.fn();
    transcript.scrollBy = scrollBy;
    // jsdom has no layout for ranges: the word in #hit sits far below the screen.
    Range.prototype.getBoundingClientRect = function (this: Range) {
      const top = this.startContainer.parentElement?.id === "hit" ? 3000 : 100;
      return { top, height: 20, bottom: top + 20 } as DOMRect;
    };
    onTestFinished(() => {
      delete (Range.prototype as Partial<Range>).getBoundingClientRect;
    });

    scrollToMessage("resp_1", undefined, undefined, (el) => firstFindRange(el, "zorblat"));

    expect(scrollBy).toHaveBeenCalledWith({ top: 2610, behavior: "smooth" });
    expect(scrollSpy).not.toHaveBeenCalled();
  });
});
