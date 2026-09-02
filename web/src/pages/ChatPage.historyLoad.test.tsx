import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { Profiler, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserMessageBlock } from "@/lib/blocks";
import { useChatStore } from "@/store/chatStore";
import {
  HistoryAutoLoader,
  JumpToTopButton,
  KeepBottomOnViewportResize,
  LatestTurnSpacer,
} from "./ChatPage";

const stickContext = vi.hoisted(() => ({
  scrollRef: { current: null as HTMLElement | null },
  contentRef: { current: null as HTMLElement | null },
  isAtBottom: false,
  scrollToBottom: vi.fn(),
  state: { isAtBottom: false, escapedFromLock: true },
  stopScroll: undefined as (() => void) | undefined,
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

describe("KeepBottomOnViewportResize", () => {
  let resize: (() => void) | null;
  let disconnectSpy = vi.fn<() => void>();
  let nextFrameId: number;
  let frames: Map<number, FrameRequestCallback>;

  beforeEach(() => {
    resize = null;
    disconnectSpy.mockClear();
    nextFrameId = 1;
    frames = new Map();
    stickContext.scrollRef.current = null;
    stickContext.contentRef.current = null;
    stickContext.isAtBottom = false;
    stickContext.state.isAtBottom = false;
    stickContext.state.escapedFromLock = true;
    stickContext.scrollToBottom.mockReset();

    class StubResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resize = () => callback([], this as unknown as ResizeObserver);
      }
      observe() {}
      disconnect() {
        disconnectSpy();
      }
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    vi.stubGlobal(
      "requestAnimationFrame",
      vi.fn((callback: FrameRequestCallback) => {
        const id = nextFrameId++;
        frames.set(id, callback);
        return id;
      }),
    );
    vi.stubGlobal(
      "cancelAnimationFrame",
      vi.fn((id: number) => {
        frames.delete(id);
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    stickContext.scrollRef.current = null;
    stickContext.contentRef.current = null;
    stickContext.isAtBottom = false;
    stickContext.state.isAtBottom = false;
    stickContext.state.escapedFromLock = true;
  });

  function flushFrames() {
    act(() => {
      const callbacks = [...frames.values()];
      frames.clear();
      for (const callback of callbacks) callback(performance.now());
    });
  }

  function makeScrollRoot(scrollTop = 1300) {
    const metrics = { scrollTop, scrollHeight: 2000, clientHeight: 700 };
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;
    return { metrics, scrollRoot };
  }

  it("keeps a bottom-locked transcript pinned after its viewport shrinks", () => {
    const { metrics } = makeScrollRoot();
    stickContext.isAtBottom = true;
    stickContext.state.isAtBottom = true;
    stickContext.state.escapedFromLock = false;
    render(<KeepBottomOnViewportResize />);

    metrics.clientHeight = 650;
    act(() => resize?.());

    expect(stickContext.scrollToBottom).toHaveBeenCalledOnce();
    expect(stickContext.scrollToBottom).toHaveBeenCalledWith("instant");
    expect(frames.size).toBe(1);

    flushFrames();
    expect(stickContext.scrollToBottom).toHaveBeenCalledTimes(2);
  });

  it("leaves an escaped reader anchored by the browser", () => {
    const { metrics } = makeScrollRoot(800);
    render(<KeepBottomOnViewportResize />);

    metrics.clientHeight = 650;
    act(() => resize?.());

    expect(stickContext.scrollToBottom).not.toHaveBeenCalled();
    expect(frames.size).toBe(0);
    expect(metrics.scrollTop).toBe(800);
  });

  it("ignores the public near-bottom alias when the live lock is escaped", () => {
    const { metrics } = makeScrollRoot(1250);
    stickContext.isAtBottom = true;
    stickContext.state.isAtBottom = false;
    stickContext.state.escapedFromLock = true;
    render(<KeepBottomOnViewportResize />);

    metrics.clientHeight = 650;
    act(() => resize?.());

    expect(stickContext.scrollToBottom).not.toHaveBeenCalled();
    expect(frames.size).toBe(0);
    expect(metrics.scrollTop).toBe(1250);
  });

  it("keeps a user escape after a same-resize library reclassification", () => {
    const { metrics, scrollRoot } = makeScrollRoot();
    stickContext.isAtBottom = true;
    stickContext.state.isAtBottom = true;
    stickContext.state.escapedFromLock = false;
    render(<KeepBottomOnViewportResize />);

    metrics.scrollTop = 1250;
    stickContext.state.isAtBottom = false;
    stickContext.state.escapedFromLock = true;
    fireEvent.scroll(scrollRoot);

    stickContext.state.isAtBottom = true;
    stickContext.state.escapedFromLock = false;
    metrics.clientHeight = 650;
    act(() => resize?.());

    expect(stickContext.scrollToBottom).not.toHaveBeenCalled();
    expect(frames.size).toBe(0);
    expect(metrics.scrollTop).toBe(1250);
  });

  it("ignores content-only resize notifications", () => {
    const { metrics } = makeScrollRoot();
    stickContext.isAtBottom = true;
    render(<KeepBottomOnViewportResize />);

    metrics.scrollHeight = 2200;
    act(() => resize?.());

    expect(stickContext.scrollToBottom).not.toHaveBeenCalled();
    expect(frames.size).toBe(0);
  });

  it("disconnects the observer and cancels a queued follow-up frame", () => {
    const { metrics } = makeScrollRoot();
    stickContext.isAtBottom = true;
    stickContext.state.isAtBottom = true;
    stickContext.state.escapedFromLock = false;
    const { unmount } = render(<KeepBottomOnViewportResize />);

    metrics.clientHeight = 650;
    act(() => resize?.());
    expect(frames.size).toBe(1);

    unmount();

    expect(disconnectSpy).toHaveBeenCalledOnce();
    expect(cancelAnimationFrame).toHaveBeenCalled();
    expect(frames.size).toBe(0);
  });
});

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

  it("does not page when the live scroll element arrives after mount", () => {
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

    // The scroll element attaching is not a reader scrolling. Paging here
    // made a freshly opened session keep fetching on its own.
    expect(loadMoreHistory).not.toHaveBeenCalled();
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

  it("never pages on load, even when the window holds a single prompt", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    // One real prompt with older history behind it: the shape that used to
    // make the open keep fetching until it found a second prompt, so the
    // transcript grew and shifted seconds after the page had settled.
    useChatStore.setState({
      blocks: [userBlock("latest"), userBlock("system", "[System: task completed]")],
      hasMoreHistory: true,
      oldestItemId: "item_9",
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    // Parked well clear of the top threshold: nothing the reader did asks
    // for older history.
    setScrollMetrics(scrollRoot, { scrollTop: 2000, scrollHeight: 4000, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);

    expect(loadMoreHistory).not.toHaveBeenCalled();
  });

  it("does not page when the open scrolls the pane to the bottom", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      oldestItemId: "item_9",
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    // A transcript barely taller than the pane: wherever it settles is inside
    // the fetch threshold, so "near the top" is trivially true. Opening still
    // must not fetch — the scroll below is the open's own scroll-to-bottom.
    const metrics = { scrollTop: 0, scrollHeight: 900, clientHeight: 800 };
    setScrollMetrics(scrollRoot, metrics);
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    metrics.scrollTop = 100; // downward: stick-to-bottom settling the view
    fireEvent.scroll(scrollRoot);

    expect(loadMoreHistory).not.toHaveBeenCalled();

    // The reader then scrolls up, which IS a request for older history.
    metrics.scrollTop = 40;
    fireEvent.scroll(scrollRoot);

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("pages on a wheel-up even when the pane has no scroll range", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      oldestItemId: "item_9",
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    // Transcript shorter than the window: scrollTop can never change, so a
    // movement-only rule would strand older history behind a gesture the pane
    // is physically unable to report.
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 400, clientHeight: 900 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    expect(loadMoreHistory).not.toHaveBeenCalled();

    fireEvent.wheel(scrollRoot, { deltaY: -120 });

    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
  });

  it("ignores a wheel-down, which is not a request for older history", () => {
    const loadMoreHistory = vi.fn(async () => {});
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      oldestItemId: "item_9",
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 400, clientHeight: 900 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    fireEvent.wheel(scrollRoot, { deltaY: 120 });

    expect(loadMoreHistory).not.toHaveBeenCalled();
  });

  it("does not cascade after a page settles while parked away from the top", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({
      blocks: [userBlock("latest")],
      hasMoreHistory: true,
      oldestItemId: "item_9",
      loadMoreHistory,
    });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 2000, scrollHeight: 4000, clientHeight: 500 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);

    // A prepend landing (new cursor) is not a reason to fetch again on its
    // own — that self-feeding loop is what made one page turn into many.
    act(() => {
      useChatStore.setState({ loadingMoreHistory: false, oldestItemId: "item_1" });
    });

    expect(loadMoreHistory).not.toHaveBeenCalled();
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

  it("bounds the prepend-fed chain from a single touch drag", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, oldestItemId: "item_50", loadMoreHistory });
    const scrollRoot = document.createElement("div");
    // Folded tool-heavy transcript: one short screen, no scroll range, parked
    // under the load threshold. Height-neutral prepends keep it that way.
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 400, clientHeight: 800 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    expect(loadMoreHistory).not.toHaveBeenCalled();

    // One small downward finger drag — "peek at what's above".
    fireEvent.touchStart(scrollRoot, { touches: [{ clientY: 300 }] });
    fireEvent.touchMove(scrollRoot, { touches: [{ clientY: 360 }] });
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);

    // Each settled page moves the cursor without adding height. The chain may
    // follow for a bounded number of pages, then must wait for a new gesture
    // instead of paging in the entire transcript.
    let settled = 49;
    while (loadMoreHistory.mock.calls.length < 8 && settled > 0) {
      const before = loadMoreHistory.mock.calls.length;
      act(() => {
        useChatStore.setState({ loadingMoreHistory: false, oldestItemId: `item_${settled}` });
      });
      settled -= 1;
      if (loadMoreHistory.mock.calls.length === before) break;
    }

    expect(loadMoreHistory.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it("holds the bound when pages settle mid-drag between touchmoves", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, oldestItemId: "item_50", loadMoreHistory });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 400, clientHeight: 800 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    fireEvent.touchStart(scrollRoot, { touches: [{ clientY: 300 }] });

    // A sustained drag on a fast connection: pages settle while the finger is
    // still moving, so every settle is followed by another touchmove from the
    // SAME gesture. Those mid-drag touchmoves must not refill the budget.
    let settled = 49;
    let fingerY = 320;
    let stalled = 0;
    while (stalled < 2 && settled > 0) {
      const before = loadMoreHistory.mock.calls.length;
      fireEvent.touchMove(scrollRoot, { touches: [{ clientY: fingerY }] });
      fingerY += 10;
      act(() => {
        useChatStore.setState({ loadingMoreHistory: false, oldestItemId: `item_${settled}` });
      });
      settled -= 1;
      if (loadMoreHistory.mock.calls.length === before) stalled += 1;
      else stalled = 0;
    }

    expect(loadMoreHistory.mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(loadMoreHistory.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it("re-grants the chain budget on the reader's next gesture", () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: true });
    });
    useChatStore.setState({ hasMoreHistory: true, oldestItemId: "item_50", loadMoreHistory });
    const scrollRoot = document.createElement("div");
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 400, clientHeight: 800 });
    stickContext.scrollRef.current = scrollRoot;

    render(<HistoryAutoLoader />);
    fireEvent.touchStart(scrollRoot, { touches: [{ clientY: 300 }] });
    fireEvent.touchMove(scrollRoot, { touches: [{ clientY: 360 }] });

    // Exhaust the first gesture's budget with height-neutral prepends.
    let settled = 49;
    let stalled = 0;
    while (stalled < 1 && settled > 0) {
      const before = loadMoreHistory.mock.calls.length;
      act(() => {
        useChatStore.setState({ loadingMoreHistory: false, oldestItemId: `item_${settled}` });
      });
      settled -= 1;
      if (loadMoreHistory.mock.calls.length === before) stalled += 1;
    }
    const afterFirstGesture = loadMoreHistory.mock.calls.length;

    // The chain stopped, but older history is not stranded: the next drag
    // asks again and pages resume.
    fireEvent.touchStart(scrollRoot, { touches: [{ clientY: 300 }] });
    fireEvent.touchMove(scrollRoot, { touches: [{ clientY: 360 }] });

    expect(loadMoreHistory.mock.calls.length).toBe(afterFirstGesture + 1);
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
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 0, clientHeight: 600 });
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
      return this.getAttribute("aria-hidden") !== null ? rect(400) : rect(0);
    });

    const onRender = vi.fn();
    const { container } = render(
      <Profiler id="latest-turn-spacer" onRender={onRender}>
        <LatestTurnSpacer />
      </Profiler>,
    );

    const spacer = container.querySelector<HTMLElement>("div[aria-hidden]")!;
    expect(spacer.style.height).toBe("104px");
    expect(onRender).toHaveBeenCalledTimes(1);
  });

  it("pads a reply that falls short of the viewport so the anchor pins near the top", () => {
    // anchor→end = 400px, viewport 600 → 600 − 400 − 96 = 104, under the
    // 200px cap, so the pin-to-top formula is what this measures.
    expect(measureSpacer({ clientHeight: 600, anchorTop: 0, spacerTop: 400, anchor: "user" })).toBe(
      104,
    );
  });

  it("caps the reserved space so a short turn does not blank most of the viewport", () => {
    // anchor→end = 100px, viewport 600 → the raw pin formula wants
    // 600 − 100 − 96 = 404px of blank (two thirds of the screen). The cap
    // holds it to a third, so earlier turns stay on screen.
    expect(measureSpacer({ clientHeight: 600, anchorTop: 0, spacerTop: 100, anchor: "user" })).toBe(
      200,
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
    // 600 − 400 − 96 = 104 (under the cap, so anchor choice is what's measured).
    expect(measureSpacer({ clientHeight: 600, anchorTop: 0, spacerTop: 400, anchor: "text" })).toBe(
      104,
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
    setScrollMetrics(scrollRoot, { scrollTop: 0, scrollHeight: 0, clientHeight: 600 });
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
    vi.spyOn(spacer, "getBoundingClientRect").mockReturnValue(rect(400));
    act(() => holder.cb?.());
    // Measured from the initial prompt: 600 − 400 − 96 = 104. Retargeting to
    // the promoted prompt would give 600 − 250 − 96 = 254 → capped to 200.
    expect(spacer.style.height).toBe("104px");

    // The consumed event moves the pending prompt into committed blocks. The
    // spacer still measures from the initial prompt instead of retargeting.
    act(() => {
      useChatStore.setState({ blocks: [userBlock("initial-user"), userBlock("pending-user")] });
      holder.cb?.();
    });
    expect(spacer.style.height).toBe("104px");
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
