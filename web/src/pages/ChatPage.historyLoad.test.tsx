import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { Profiler, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserMessageBlock } from "@/lib/blocks";
import { MAX_INITIAL_PAGES } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { HistoryAutoLoader, JumpToTopButton, LatestTurnSpacer } from "./ChatPage";

const stickContext = vi.hoisted(() => ({
  scrollRef: { current: null as HTMLElement | null },
}));

vi.mock("use-stick-to-bottom", () => ({
  useStickToBottomContext: () => stickContext,
}));

const originalLoadMoreHistory = useChatStore.getState().loadMoreHistory;

function userBlock(id: string, text = id): UserMessageBlock {
  return {
    type: "user_message",
    ctx: {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: 0,
      responseId: id,
      itemId: id,
    },
    content: [{ type: "input_text", text }],
  };
}

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
    useChatStore.setState({
      blocks: [userBlock("user_1"), userBlock("user_2")],
      conversationId: "session-1",
      hasMoreHistory: false,
      loadingMoreHistory: false,
      oldestItemId: "item_2",
      historyGeneration: 0,
    });
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({ loadMoreHistory: originalLoadMoreHistory });
    vi.unstubAllGlobals();
  });

  it("renders no visible control", () => {
    const { container } = render(<HistoryAutoLoader />);

    expect(container).toBeEmptyDOMElement();
  });

  // Position across a prepend belongs to native scroll anchoring. These pin
  // the loader to writing nothing: an imperative scrollTop write cancels
  // in-flight momentum, so a page landing mid-flick used to yank the
  // transcript out from under the reader.
  it("leaves the scroll offset alone when a page prepends", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 500, scrollHeight: 100 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    // Scroll near the top to trigger an older-history fetch.
    metrics.scrollTop = 24;
    fireEvent.scroll(scrollRoot);
    metrics.scrollHeight = 180;
    act(() => {
      useChatStore.setState({
        hasMoreHistory: false,
        loadingMoreHistory: false,
        oldestItemId: "item_1",
      });
    });

    expect(metrics.scrollTop).toBe(24);
  });

  it("leaves the offset alone when the user scrolls again during the request", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 500, scrollHeight: 1000 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    metrics.scrollTop = 499;
    fireEvent.scroll(scrollRoot);
    metrics.scrollTop = 0;
    fireEvent.scroll(scrollRoot);

    metrics.scrollHeight = 1800;
    act(() => {
      useChatStore.setState({
        hasMoreHistory: false,
        loadingMoreHistory: false,
        oldestItemId: "item_1",
      });
    });

    expect(metrics.scrollTop).toBe(0);
  });

  it("leaves the offset alone across skeleton insertion and removal", () => {
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 500, scrollHeight: 1000 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;
    const loadMoreHistory = vi.fn(async () => {
      // The loading skeleton prepends 100px before the request settles.
      metrics.scrollHeight = 1100;
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });

    render(<HistoryAutoLoader />);
    metrics.scrollTop = 499;
    fireEvent.scroll(scrollRoot);
    expect(metrics.scrollTop).toBe(499);

    // Then replace the 100px skeleton with a page that leaves the content
    // 400px taller overall — still not the loader's to compensate for.
    metrics.scrollTop = 20;
    fireEvent.scroll(scrollRoot);
    metrics.scrollHeight = 1500;
    act(() => {
      useChatStore.setState({
        hasMoreHistory: false,
        loadingMoreHistory: false,
        oldestItemId: "item_1",
      });
    });

    expect(metrics.scrollTop).toBe(20);
  });

  it("loads older history when the user scrolls near the top", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 500, scrollHeight: 100 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    metrics.scrollTop = 499;
    fireEvent.scroll(scrollRoot);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  // The fetch fires viewports early so the page settles before the reader
  // reaches offset 0, where the browser stops anchoring and a prepend would
  // shift the transcript with nothing to absorb it.
  it("scales the fetch threshold with the viewport", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory, oldestItemId: "item_0" });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 9000, scrollHeight: 20000, clientHeight: 2000 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    // 2.5 viewports = 5000px. Still outside it on a tall pane.
    metrics.scrollTop = 6000;
    fireEvent.scroll(scrollRoot);
    expect(loadMoreHistory).not.toHaveBeenCalled();

    // Inside it — yet far enough from the top that a fixed 500px trigger
    // would not have fired here at all.
    metrics.scrollTop = 4000;
    fireEvent.scroll(scrollRoot);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("attaches when the live scroll element becomes available after mount", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 600, scrollHeight: 1000 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = null;

    function DeferredScroller() {
      const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
      useEffect(() => {
        setScrollElement(scrollRoot);
      }, []);
      return <HistoryAutoLoader scrollElement={scrollElement} />;
    }

    render(<DeferredScroller />);
    metrics.scrollTop = 499;
    fireEvent.scroll(scrollRoot);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("starts initial paging when the live scroll element becomes available after mount", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 600, scrollHeight: 1000, clientHeight: 500 });
    stickContext.scrollRef.current = null;

    function DeferredScroller() {
      const [scrollElement, setScrollElement] = useState<HTMLElement | null>(null);
      useEffect(() => {
        setScrollElement(scrollRoot);
      }, []);
      return <HistoryAutoLoader scrollElement={scrollElement} />;
    }

    render(<DeferredScroller />);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("keeps loading after reaching the top during an in-flight fetch", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, loadMoreHistory });
    const scrollRoot = document.createElement("div");
    const metrics = { scrollTop: 500, scrollHeight: 1000 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    metrics.scrollTop = 499;
    fireEvent.scroll(scrollRoot);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);

    metrics.scrollTop = 0;
    fireEvent.scroll(scrollRoot);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);

    // Tool-heavy pages can collapse without changing scrollHeight. The paging
    // cursor still changes, so completing the request must queue another page.
    act(() => {
      useChatStore.setState({ loadingMoreHistory: false, oldestItemId: "item_1" });
    });
    expect(loadMoreHistory).toHaveBeenCalledTimes(2);
  });

  it("loads to the second real user prompt even when the viewport already scrolls", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({
      blocks: [userBlock("latest"), userBlock("system", "[System: task completed]")],
      hasMoreHistory: true,
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    // Parked well clear of the fetch threshold (2.5 viewports), so only the
    // initial-window build may page — not the scroll-driven path.
    setScrollMetrics(scrollRoot, { scrollTop: 2000, scrollHeight: 4000, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
    expect(useChatStore.getState().loadingMoreHistory).toBe(true);

    act(() => {
      useChatStore.setState({
        blocks: [userBlock("previous"), userBlock("latest")],
        loadingMoreHistory: false,
        oldestItemId: "item_1",
      });
    });

    expect(useChatStore.getState().loadingMoreHistory).toBe(false);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("caps the prompt search at the page cap", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      loadMoreHistory,
    });

    const scrollRoot = document.createElement("div");
    // Clear of the fetch threshold, so the page count below is the cap alone.
    setScrollMetrics(scrollRoot, { scrollTop: 2000, scrollHeight: 4000, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);

    // The newest page counts as page one. Settle older pages without finding a
    // second prompt; the semantic search must stop at the cap — reachability is
    // no longer this loop's job (the spacer keeps the pane scrollable).
    for (let page = 2; page <= MAX_INITIAL_PAGES; page++) {
      act(() => {
        useChatStore.setState({
          loadingMoreHistory: false,
          oldestItemId: `item_${page + 1}`,
        });
      });
    }

    // MAX_INITIAL_PAGES total fetches: the initial page plus (cap − 1) more.
    expect(loadMoreHistory).toHaveBeenCalledTimes(MAX_INITIAL_PAGES - 1);
  });

  it("does not page a short window for viewport fill (the spacer handles reachability)", () => {
    const loadMoreHistory = vi.fn(async () => {});
    // Two prompts already loaded → prompt boundary met. A window too short to
    // scroll must NOT trigger a fetch: the spacer keeps it reachable instead.
    useChatStore.setState({
      blocks: [userBlock("user_1"), userBlock("user_2")],
      hasMoreHistory: true,
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 100, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);

    expect(loadMoreHistory).not.toHaveBeenCalled();
  });

  it("does not auto-load a short window once history is exhausted", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({ loadMoreHistory });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 100, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);

    expect(loadMoreHistory).not.toHaveBeenCalled();
  });
});

describe("LatestTurnSpacer", () => {
  beforeEach(() => {
    stickContext.scrollRef.current = null;
    useChatStore.setState({ blocks: [], historyGeneration: 0 });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function rect(top: number): DOMRect {
    return {
      top,
      bottom: top,
      height: 0,
      left: 0,
      right: 0,
      width: 0,
      x: 0,
      y: top,
      toJSON: () => ({}),
    };
  }

  /**
   * Render the spacer, wire up an optional anchor inside the scroll container,
   * and pin both rects, then drive one measure via the captured ResizeObserver
   * callback (so it runs after the rects are in place). Anchors match the same
   * selectors the component uses: `[data-role="user"]` for a real prompt,
   * `[data-testid="assistant-text-section"]` for assistant text.
   *
   * @returns the height (px) the component set on its spacer div.
   */
  function measureSpacer(opts: {
    clientHeight: number;
    anchorTop: number;
    spacerTop: number;
    anchor: "user" | "text" | "none";
  }): number {
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
    setScrollMetrics(scrollRoot, {
      scrollTop: 0,
      scrollHeight: 0,
      clientHeight: opts.clientHeight,
    });
    stickContext.scrollRef.current = scrollRoot;

    if (opts.anchor !== "none") {
      const anchor = document.createElement("div");
      if (opts.anchor === "user") {
        anchor.dataset.role = "user";
        anchor.dataset.userMessageId = "initial-user";
        useChatStore.setState({ blocks: [userBlock("initial-user")] });
      } else {
        anchor.dataset.testid = "assistant-text-section";
      }
      vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue(rect(opts.anchorTop));
      scrollRoot.append(anchor);
    }

    const { container } = render(<LatestTurnSpacer />);
    const spacer = container.querySelector<HTMLElement>("div[aria-hidden]")!;
    vi.spyOn(spacer, "getBoundingClientRect").mockReturnValue(rect(opts.spacerTop));
    // Re-measure now that the rects are pinned (mount ran against jsdom's 0s).
    act(() => holder.cb?.());

    return parseFloat(spacer.style.height || "0");
  }

  it("applies the initial height without a state-driven second render", () => {
    class StubResizeObserver {
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);

    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 0, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    const anchor = document.createElement("div");
    anchor.dataset.role = "user";
    anchor.dataset.userMessageId = "initial-user";
    scrollRoot.append(anchor);
    useChatStore.setState({ blocks: [userBlock("initial-user")] });
    vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue(rect(0));
    vi.spyOn(HTMLDivElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLDivElement,
    ) {
      return this.getAttribute("aria-hidden") !== null ? rect(100) : rect(0);
    });

    const onRender = vi.fn();
    const { container } = render(
      <Profiler id="latest-turn-spacer" onRender={onRender}>
        <LatestTurnSpacer />
      </Profiler>,
    );

    const spacer = container.querySelector<HTMLElement>("div[aria-hidden]")!;
    expect(spacer.style.height).toBe("304px");
    expect(onRender).toHaveBeenCalledTimes(1);
  });

  it("pads a short reply so the anchor prompt pins to the top", () => {
    // anchor→end = 100px content, viewport 500 → spacer = 500 − 100 − 96 = 304.
    expect(measureSpacer({ clientHeight: 500, anchorTop: 0, spacerTop: 100, anchor: "user" })).toBe(
      304,
    );
  });

  it("collapses to zero once the reply alone exceeds the viewport", () => {
    // Reply taller than the viewport (spacer top far below the anchor):
    // 500 − 900 − 96 < 0, clamped to 0.
    expect(measureSpacer({ clientHeight: 500, anchorTop: 0, spacerTop: 900, anchor: "user" })).toBe(
      0,
    );
  });

  it("anchors to the last assistant text when no user prompt is present", () => {
    // 500 − 50 − 96 = 354.
    expect(measureSpacer({ clientHeight: 500, anchorTop: 0, spacerTop: 50, anchor: "text" })).toBe(
      354,
    );
  });

  it("adds no padding when there is no anchor (pure tool output)", () => {
    expect(measureSpacer({ clientHeight: 500, anchorTop: 0, spacerTop: 0, anchor: "none" })).toBe(
      0,
    );
  });

  it("keeps the initial committed prompt anchored when a pending prompt is promoted", () => {
    const holder: { cb: (() => void) | null } = { cb: null };
    class StubResizeObserver {
      constructor(cb: () => void) {
        holder.cb = cb;
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);

    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 0, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    const initial = document.createElement("div");
    initial.dataset.role = "user";
    initial.dataset.userMessageId = "initial-user";
    vi.spyOn(initial, "getBoundingClientRect").mockReturnValue(rect(0));
    scrollRoot.append(initial);

    const pending = document.createElement("div");
    pending.dataset.role = "user";
    pending.dataset.userMessageId = "pending-user";
    vi.spyOn(pending, "getBoundingClientRect").mockReturnValue(rect(150));
    scrollRoot.append(pending);

    useChatStore.setState({ blocks: [userBlock("initial-user")] });
    const { container } = render(<LatestTurnSpacer />);
    const spacer = container.querySelector<HTMLElement>("div[aria-hidden]")!;
    vi.spyOn(spacer, "getBoundingClientRect").mockReturnValue(rect(200));
    act(() => holder.cb?.());
    expect(spacer.style.height).toBe("204px");

    // The consumed event moves the pending prompt into committed blocks. The
    // spacer still measures from the initial prompt instead of retargeting.
    act(() => {
      useChatStore.setState({ blocks: [userBlock("initial-user"), userBlock("pending-user")] });
      holder.cb?.();
    });
    expect(spacer.style.height).toBe("204px");
  });

  it("does not create an anchor after an initially empty conversation's first prompt commits", () => {
    const holder: { cb: (() => void) | null } = { cb: null };
    class StubResizeObserver {
      constructor(cb: () => void) {
        holder.cb = cb;
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);

    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 0, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    const { container } = render(<LatestTurnSpacer />);
    const spacer = container.querySelector<HTMLElement>("div[aria-hidden]")!;
    vi.spyOn(spacer, "getBoundingClientRect").mockReturnValue(rect(100));

    const pending = document.createElement("div");
    pending.dataset.role = "user";
    pending.dataset.userMessageId = "first-user";
    vi.spyOn(pending, "getBoundingClientRect").mockReturnValue(rect(0));
    scrollRoot.append(pending);

    act(() => {
      useChatStore.setState({ blocks: [userBlock("first-user")] });
      holder.cb?.();
    });
    expect(spacer.style.display).toBe("none");
    expect(spacer.style.height || "0px").toBe("0px");
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
