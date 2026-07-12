import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { HistoryAutoLoader, JumpToTopButton } from "./ChatPage";

const stickContext = vi.hoisted(() => ({
  scrollRef: { current: null as HTMLElement | null },
}));

vi.mock("use-stick-to-bottom", () => ({
  useStickToBottomContext: () => stickContext,
}));

const originalLoadMoreHistory = useChatStore.getState().loadMoreHistory;

/**
 * Installs mutable layout metrics on a jsdom element.
 *
 * @param el - Scroll container element used by the mocked StickToBottom context.
 * @param metrics - Mutable scroll state that the test can inspect and update.
 *     `clientHeight` defaults to 0 (jsdom default) so the viewport-fill guard
 *     stays dormant unless a test opts in.
 */
function setScrollMetrics(
  el: HTMLElement,
  metrics: { scrollTop: number; scrollHeight: number; clientHeight?: number },
) {
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => metrics.scrollTop,
    set: (value: number) => {
      metrics.scrollTop = value;
    },
  });
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => metrics.scrollHeight,
  });
  Object.defineProperty(el, "clientHeight", {
    configurable: true,
    get: () => metrics.clientHeight ?? 0,
  });
}

describe("HistoryAutoLoader", () => {
  beforeEach(() => {
    stickContext.scrollRef.current = null;
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({ loadMoreHistory: originalLoadMoreHistory });
    vi.unstubAllGlobals();
  });

  it("renders no visible control", () => {
    const { container } = render(
      <HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={false} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("preserves the visible scroll offset after a scroll-up prepend", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 24, scrollHeight: 100 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    const { rerender } = render(
      <HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={false} />,
    );
    // Scroll near the top to trigger an older-history fetch.
    fireEvent.scroll(scrollRoot);
    rerender(<HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={true} />);
    metrics.scrollHeight = 180;
    rerender(<HistoryAutoLoader hasMoreHistory={false} loadingMoreHistory={false} />);

    expect(metrics.scrollTop).toBe(104);
  });

  it("loads older history when the user scrolls near the top", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 299, scrollHeight: 100 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={false} />);
    fireEvent.scroll(scrollRoot);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("auto-loads when the window is too short to scroll", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });
    const scrollRoot = document.createElement("div");
    // Content shorter than the viewport → no scrollbar, scroll trigger can't fire.
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 100, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={false} />);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("does not auto-load a short window once history is exhausted", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 100, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader hasMoreHistory={false} loadingMoreHistory={false} />);

    expect(loadMoreHistory).not.toHaveBeenCalled();
  });

  it("re-fills when the viewport grows so content no longer overflows", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });

    const holder: { cb: (() => void) | null } = { cb: null };
    class StubResizeObserver {
      constructor(cb: () => void) {
        holder.cb = cb;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);

    const scrollRoot = document.createElement("div");
    // Content overflows the viewport at first — scrollbar present, fill dormant.
    const metrics = { scrollTop: 600, scrollHeight: 1000, clientHeight: 500 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader hasMoreHistory={true} loadingMoreHistory={false} />);
    expect(loadMoreHistory).not.toHaveBeenCalled();

    // Window grows: content no longer overflows, so the resize re-check pages.
    metrics.clientHeight = 1200;
    holder.cb?.();

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });
});

describe("JumpToTopButton", () => {
  afterEach(() => {
    cleanup();
    useChatStore.setState({ loadMoreHistory: originalLoadMoreHistory, hasMoreHistory: false });
    vi.useRealTimers();
  });

  // Query by the aria-label attribute rather than role/accessible-name: when
  // hidden the button is aria-hidden (out of the accessibility tree, so its
  // accessible name computes to ""), and these tests assert on its
  // className/visibility rather than reachability.
  const pill = () => {
    const el = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Jump to the first message"]',
    );
    if (!el) throw new Error("Jump-to-top pill not found");
    return el;
  };

  /**
   * A wrapper (hover/anchor) + inner scroll container, plus a stub of the
   * StickToBottom lock controls — mirrors the real ConversationScroller.
   */
  function makeScroller(metrics: {
    scrollTop: number;
    scrollHeight: number;
    clientHeight?: number;
  }) {
    const container = document.createElement("div");
    const scroll = document.createElement("div");
    container.append(scroll);
    setScrollMetrics(scroll, metrics);
    const state = { isAtBottom: true, escapedFromLock: false };
    const stopScroll = vi.fn();
    return { container, scroll, scroller: { el: scroll, state, stopScroll } };
  }

  it("stays non-interactive at the first message (nothing above)", () => {
    const { container, scroller } = makeScroller({
      scrollTop: 0,
      scrollHeight: 100,
      clientHeight: 100,
    });

    render(<JumpToTopButton containerEl={container} scroller={scroller} hasMoreHistory={false} />);
    // Hover the top edge (jsdom getBoundingClientRect().top is 0).
    act(() => {
      fireEvent.mouseMove(container, { clientY: 10 });
    });

    expect(pill().className).toContain("pointer-events-none");
  });

  it("reveals on hover near the top when there is history above", () => {
    const { container, scroller } = makeScroller({
      scrollTop: 0,
      scrollHeight: 100,
      clientHeight: 100,
    });

    render(<JumpToTopButton containerEl={container} scroller={scroller} hasMoreHistory={true} />);
    expect(pill().className).toContain("pointer-events-none");

    // Hovering the wrapper near the top reveals and arms the pill.
    act(() => {
      fireEvent.mouseMove(container, { clientY: 10 });
    });
    expect(pill().className).toContain("pointer-events-auto");

    // Leaving the conversation hides it again.
    act(() => {
      fireEvent.mouseLeave(container);
    });
    expect(pill().className).toContain("pointer-events-none");
  });

  it("reveals when the user scrolls up, then auto-hides after the linger timeout", () => {
    vi.useFakeTimers();
    const { container, scroll, scroller } = makeScroller({
      scrollTop: 500,
      scrollHeight: 1000,
      clientHeight: 400,
    });
    const metrics = scroll as unknown as { scrollTop: number };

    render(<JumpToTopButton containerEl={container} scroller={scroller} hasMoreHistory={true} />);
    // Mount reads the initial position; no scroll yet, so the pill stays hidden.
    expect(pill().className).toContain("pointer-events-none");

    // Scroll up (scrollTop decreases): the pill reveals without any hover.
    act(() => {
      metrics.scrollTop = 300;
      fireEvent.scroll(scroll);
    });
    expect(pill().className).toContain("pointer-events-auto");

    // After the linger window with no further upward scroll, it fades back out.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(pill().className).toContain("pointer-events-none");
  });

  it("does not reveal on a downward scroll", () => {
    const { container, scroll, scroller } = makeScroller({
      scrollTop: 300,
      scrollHeight: 1000,
      clientHeight: 400,
    });
    const metrics = scroll as unknown as { scrollTop: number };

    render(<JumpToTopButton containerEl={container} scroller={scroller} hasMoreHistory={true} />);

    // Scrolling down (scrollTop increases) must not surface the pill.
    act(() => {
      metrics.scrollTop = 600;
      fireEvent.scroll(scroll);
    });
    expect(pill().className).toContain("pointer-events-none");
  });

  it("releases the bottom-lock, pages in all history, then scrolls to the top", async () => {
    const { container, scroller, scroll } = makeScroller({
      scrollTop: 500,
      scrollHeight: 1000,
      clientHeight: 400,
    });
    const metrics = scroll as unknown as { scrollTop: number };

    let calls = 0;
    const loadMoreHistory = vi.fn(async () => {
      calls += 1;
      // Simulate the library trying to re-stick to the bottom on each prepend;
      // jumpToTop must keep clearing the lock for the final scroll to hold.
      scroller.state.isAtBottom = true;
      if (calls >= 2) useChatStore.setState({ hasMoreHistory: false });
    });
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });

    render(<JumpToTopButton containerEl={container} scroller={scroller} hasMoreHistory={true} />);
    fireEvent.click(pill());

    await waitFor(() => expect(useChatStore.getState().hasMoreHistory).toBe(false));
    await waitFor(() => expect(metrics.scrollTop).toBe(0));
    expect(scroller.stopScroll).toHaveBeenCalled();
    expect(scroller.state.isAtBottom).toBe(false);
    expect(loadMoreHistory).toHaveBeenCalledTimes(2);
  });
});
