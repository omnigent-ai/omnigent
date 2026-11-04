import { afterEach, describe, expect, it, vi } from "vitest";
import {
  anchorOccurrence,
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  findAnchorInSource,
  HTML_COMMENT_BRIDGE_RUNTIME,
  injectCommentBridge,
  parseBridgeMessage,
} from "./htmlCommentBridge";
import { SCROLL_RESTORE_BUDGET_MS } from "./useScrollRestore";

// ---------------------------------------------------------------------------
// injectCommentBridge — script/style placement (mirrors prepareHtmlPreviewDoc)
// ---------------------------------------------------------------------------

describe("injectCommentBridge", () => {
  const NONCE = "test-nonce-123";
  const LOADER_URL = "https://app.example/assets/html-comment-bridge.js";

  // The prepared head carries its own <script> (same-page anchors), so the
  // bridge script is located by its nonce.
  it("injects the bridge script before </body> when present", () => {
    const html = "<html><head></head><body><p>hi</p></body></html>";
    const out = injectCommentBridge(html, NONCE, LOADER_URL);
    const scriptAt = out.indexOf(`data-omni-nonce="${NONCE}"`);
    const bodyCloseAt = out.indexOf("</body>");
    expect(scriptAt).toBeGreaterThan(-1);
    expect(scriptAt).toBeLessThan(bodyCloseAt);
  });

  it("falls back to before </html> when there is no body", () => {
    const html = "<html><head></head><p>hi</p></html>";
    const out = injectCommentBridge(html, NONCE, LOADER_URL);
    const scriptAt = out.indexOf(`data-omni-nonce="${NONCE}"`);
    expect(scriptAt).toBeGreaterThan(-1);
    expect(scriptAt).toBeLessThan(out.indexOf("</html>"));
  });

  it("appends to a bare fragment with no body/html", () => {
    const out = injectCommentBridge("<p>just a fragment</p>", NONCE, LOADER_URL);
    // prepareHtmlPreviewDoc prepends its head markup for a bare fragment; the
    // bridge is then appended at the end since there's no </body>/</html> to
    // inject before.
    expect(out).toContain("<p>just a fragment</p>");
    const fragAt = out.indexOf("<p>just a fragment</p>");
    expect(out.indexOf(`data-omni-nonce="${NONCE}"`)).toBeGreaterThan(fragAt);
  });

  it("preserves the prepared <base target=_blank> link behavior", () => {
    const out = injectCommentBridge("<html><head></head><body></body></html>", NONCE, LOADER_URL);
    expect(out).toContain('<base target="_blank">');
  });

  it("includes the highlight style for the Custom Highlight ranges", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL);
    expect(out).toContain("::highlight(omni-comment)");
    expect(out).toContain("::highlight(omni-comment-active)");
  });

  it("loads the static runtime externally for a no-inline CSP", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL);
    expect(out).toContain(`src="${LOADER_URL}"`);
    expect(out).toContain(`data-omni-nonce="${NONCE}"`);
    expect(out).not.toContain(HTML_COMMENT_BRIDGE_RUNTIME);
  });

  it("injects the same runtime inline without a network dependency", () => {
    const out = injectCommentBridge("<body></body>", NONCE);
    expect(out).toContain(`<script data-omni-nonce="${NONCE}" data-omni-protocol=`);
    expect(out).toContain(HTML_COMMENT_BRIDGE_RUNTIME);
    expect(out).not.toContain("<script src=");
  });

  it("keeps the inline runtime body free of closing script tags", () => {
    expect(HTML_COMMENT_BRIDGE_RUNTIME).not.toMatch(/<\/script/i);
  });

  it("escapes runtime URLs and nonces as HTML attributes", () => {
    const out = injectCommentBridge(
      "<body></body>",
      "nonce&\"'<>value",
      'https://app.example/a?x=1&y="2"\'<>',
    );
    expect(out).toContain('src="https://app.example/a?x=1&amp;y=&quot;2&quot;&#39;&lt;&gt;"');
    expect(out).toContain('data-omni-nonce="nonce&amp;&quot;&#39;&lt;&gt;value"');
  });

  it("passes protocol constants to the runtime as data", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL);
    const doc = new DOMParser().parseFromString(out, "text/html");
    const script = doc.querySelector(`script[data-omni-nonce="${NONCE}"]`);
    expect(JSON.parse(script?.getAttribute("data-omni-protocol") ?? "")).toEqual({
      source: BRIDGE_SOURCE,
      types: BRIDGE_MSG,
    });
    expect(HTML_COMMENT_BRIDGE_RUNTIME).not.toMatch(/\b(?:eval|Function)\s*\(/);
    expect(HTML_COMMENT_BRIDGE_RUNTIME).toContain("script instanceof HTMLScriptElement");
  });

  it("ships a syntactically valid classic script", () => {
    expect(() => new Function(HTML_COMMENT_BRIDGE_RUNTIME)).not.toThrow();
  });

  it("hands a saved scroll offset and the restore budget to the runtime", () => {
    const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL, 640);
    const doc = new DOMParser().parseFromString(out, "text/html");
    const script = doc.querySelector(`script[data-omni-nonce="${NONCE}"]`);
    expect(JSON.parse(script?.getAttribute("data-omni-restore-scroll") ?? "")).toEqual({
      top: 640,
      budgetMs: SCROLL_RESTORE_BUDGET_MS,
    });
  });

  it.each([0, -5, Number.NaN, Number.POSITIVE_INFINITY])(
    "asks for no restore when the saved offset is %s",
    (top) => {
      const out = injectCommentBridge("<body></body>", NONCE, LOADER_URL, top);
      expect(out).not.toContain("data-omni-restore-scroll");
      expect(out).not.toContain("data-omni-restore-hide");
    },
  );

  it.each([
    ["a document with a head", "<!DOCTYPE html><html><head><title>Plan</title></head><body>"],
    ["a document without a head", "<!DOCTYPE html><html><body><p>Plan</p></body></html>"],
    ["a bare fragment", "<p>Plan</p>"],
  ])("hides %s from the start of its head while restoring", (_label, html) => {
    const out = injectCommentBridge(html, NONCE, LOADER_URL, 640);
    if (html.startsWith("<!DOCTYPE html>")) expect(out.startsWith("<!DOCTYPE html>")).toBe(true);
    const hide = new DOMParser().parseFromString(out, "text/html").head.firstElementChild;
    expect(hide?.getAttribute("data-omni-restore-hide")).toBe(NONCE);
    expect(hide?.textContent).toBe(
      `html{animation:omni-restore-hide ${2 * SCROLL_RESTORE_BUDGET_MS}ms!important}` +
        "@keyframes omni-restore-hide{from,to{opacity:0;pointer-events:none}}",
    );
  });
});

// ---------------------------------------------------------------------------
// Bridge runtime harness
// ---------------------------------------------------------------------------

const RUNTIME_NONCE = "runtime-nonce";
const runtimeListeners: [EventTarget, string, EventListenerOrEventListenerObject, unknown][] = [];

afterEach(() => {
  for (const [target, type, listener, options] of runtimeListeners.splice(0)) {
    target.removeEventListener(type, listener, options as EventListenerOptions);
  }
  vi.restoreAllMocks();
});

/** Run the runtime in this document, as the injected script would, and connect a port. */
function startRuntime({
  connect = true,
  beforeConnect,
  restoreScroll,
}: {
  connect?: boolean;
  beforeConnect?: () => void;
  /** Value of the script's `data-omni-restore-scroll` attribute. */
  restoreScroll?: string;
} = {}) {
  for (const target of [window, document]) {
    const original = target.addEventListener.bind(target);
    vi.spyOn(target, "addEventListener").mockImplementation((type, listener, options) => {
      if (listener) runtimeListeners.push([target, type, listener, options]);
      original(type, listener, options);
    });
  }
  const script = document.createElement("script");
  script.setAttribute("data-omni-nonce", RUNTIME_NONCE);
  script.setAttribute(
    "data-omni-protocol",
    JSON.stringify({ source: BRIDGE_SOURCE, types: BRIDGE_MSG }),
  );
  if (restoreScroll !== undefined) script.setAttribute("data-omni-restore-scroll", restoreScroll);
  vi.spyOn(document, "currentScript", "get").mockReturnValue(script);
  new Function(HTML_COMMENT_BRIDGE_RUNTIME)();
  beforeConnect?.();

  const port = { postMessage: vi.fn(), onmessage: null };
  if (connect) {
    const init = new Event("message");
    Object.defineProperties(init, {
      data: { value: { source: BRIDGE_SOURCE, nonce: RUNTIME_NONCE, type: BRIDGE_MSG.init } },
      ports: { value: [port] },
    });
    window.dispatchEvent(init);
    port.postMessage.mockClear(); // drop the ready message
  }
  vi.mocked(window.addEventListener).mockRestore();
  vi.mocked(document.addEventListener).mockRestore();
  return port;
}

// ---------------------------------------------------------------------------
// Bridge runtime — zoom shortcuts inside the preview
// ---------------------------------------------------------------------------

describe("bridge runtime zoom shortcuts", () => {
  function press(init: KeyboardEventInit): boolean {
    const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
    document.body.dispatchEvent(event);
    return event.defaultPrevented;
  }

  function sentZoomActions(port: { postMessage: ReturnType<typeof vi.fn> }) {
    return port.postMessage.mock.calls.map(([msg]) => {
      expect(msg).toMatchObject({
        source: BRIDGE_SOURCE,
        nonce: RUNTIME_NONCE,
        type: BRIDGE_MSG.zoom,
      });
      return (msg as { action: string }).action;
    });
  }

  it.each([
    [{ key: "=", metaKey: true }, "in"],
    [{ key: "+", metaKey: true, shiftKey: true }, "in"],
    [{ key: "=", ctrlKey: true }, "in"],
    [{ key: "Unidentified", code: "NumpadAdd", ctrlKey: true }, "in"],
    [{ key: "-", metaKey: true }, "out"],
    [{ key: "_", ctrlKey: true, shiftKey: true }, "out"],
    [{ key: "Unidentified", code: "NumpadSubtract", metaKey: true }, "out"],
    [{ key: "0", metaKey: true }, "reset"],
    [{ key: "Insert", code: "Numpad0", ctrlKey: true }, "reset"],
  ])("cancels %o and asks the parent to zoom %s", (init, action) => {
    const port = startRuntime();
    expect(press(init)).toBe(true);
    expect(sentZoomActions(port)).toEqual([action]);
  });

  it.each([
    { key: "=" },
    { key: "-", shiftKey: true },
    { key: "=", metaKey: true, altKey: true },
    { key: "9", metaKey: true },
    { key: "f", ctrlKey: true },
  ])("leaves %o to the page", (init) => {
    const port = startRuntime();
    expect(press(init)).toBe(false);
    expect(port.postMessage).not.toHaveBeenCalled();
  });

  it("keeps the app's zoom until the parent connects", () => {
    const port = startRuntime({ connect: false });
    expect(press({ key: "=", metaKey: true })).toBe(false);
    expect(port.postMessage).not.toHaveBeenCalled();
  });

  it.each([
    [["resize"], true],
    [["selectionchange"], false],
    [["resize", "selectionchange"], false],
  ])("re-reports the selection after %o with reflow %s", (events, reflow) => {
    vi.useFakeTimers();
    const getClientRects = Object.getOwnPropertyDescriptor(Range.prototype, "getClientRects");
    try {
      const port = startRuntime();
      document.body.innerHTML = "<p>Quarterly revenue</p>";
      // jsdom has no layout, so give the selected range a rect.
      Object.defineProperty(Range.prototype, "getClientRects", {
        configurable: true,
        value: () => [{ left: 1, top: 2, right: 3, bottom: 4 }],
      });
      document.getSelection()?.selectAllChildren(document.body);
      vi.runAllTimers();
      port.postMessage.mockClear();

      for (const type of events) {
        (type === "resize" ? window : document).dispatchEvent(new Event(type));
      }
      vi.advanceTimersByTime(150);
      expect(port.postMessage.mock.calls).toEqual([
        [
          expect.objectContaining({
            type: BRIDGE_MSG.selection,
            text: "Quarterly revenue",
            reflow,
          }),
        ],
      ]);
    } finally {
      if (getClientRects) Object.defineProperty(Range.prototype, "getClientRects", getClientRects);
      else delete (Range.prototype as Partial<Range>).getClientRects;
      document.getSelection()?.removeAllRanges();
      document.body.innerHTML = "";
      vi.useRealTimers();
    }
  });

  it.each([
    ["document listener that cancels it", () => document, (e: Event) => e.preventDefault()],
    ["window listener that cancels it", () => window, (e: Event) => e.preventDefault()],
    ["listener that stops its propagation", () => document, (e: Event) => e.stopPropagation()],
  ])("leaves the shortcut to an artifact %s", (_, target, handler) => {
    // Artifact scripts run after the injected bridge but before the parent connects.
    const port = startRuntime({
      beforeConnect: () => target().addEventListener("keydown", handler),
    });
    press({ key: "=", metaKey: true });
    expect(port.postMessage).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Bridge runtime — scroll restore and scroll reports
// ---------------------------------------------------------------------------

describe("bridge runtime scroll position", () => {
  const BUDGET_MS = 1_000;

  afterEach(() => {
    vi.useRealTimers();
    document.head.querySelectorAll("style[data-omni-restore-hide]").forEach((el) => el.remove());
    document.documentElement.style.removeProperty("overflow-anchor");
  });

  /** The style injected with a saved offset to hide the page, as the runtime finds it. */
  function addRestoreHide(nonce = RUNTIME_NONCE) {
    const style = document.createElement("style");
    style.setAttribute("data-omni-restore-hide", nonce);
    document.head.append(style);
    return style;
  }

  /**
   * Fake frames and the clock, and give jsdom a scrollable page whose offset
   * clamps to `pageHeight`, as a browser's does.
   */
  function fakeScrolling({ pageHeight = 100_000 } = {}) {
    vi.useFakeTimers({
      toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"],
    });
    let scrollY = 0;
    let height = pageHeight;
    let viewportHeight = window.innerHeight;
    const clamp = (top: number) => Math.max(0, Math.min(top, height - viewportHeight));
    vi.spyOn(window, "scrollY", "get").mockImplementation(() => scrollY);
    vi.spyOn(window, "innerHeight", "get").mockImplementation(() => viewportHeight);
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockImplementation(() => height);
    const scrollTo = vi
      .spyOn(window, "scrollTo")
      .mockImplementation((options?: ScrollToOptions | number) => {
        if (typeof options === "object" && typeof options.top === "number") {
          scrollY = clamp(options.top);
        }
      });
    const userScrollTo = (top: number) => {
      scrollY = clamp(top);
      window.dispatchEvent(new Event("scroll"));
    };
    const relayout = (nextScrollY: number) => {
      const previous = scrollY;
      scrollY = clamp(nextScrollY);
      if (scrollY !== previous) window.dispatchEvent(new Event("scroll"));
    };
    /** Resize the page as late content does; the browser may move the offset with it. */
    const setPageHeight = (nextHeight: number, nextScrollY = scrollY) => {
      height = nextHeight;
      relayout(nextScrollY);
    };
    /** Resize the viewport, as zooming the preview does; the browser clamps the offset to fit. */
    const setViewportHeight = (nextHeight: number) => {
      viewportHeight = nextHeight;
      relayout(scrollY);
    };
    return { scrollTo, userScrollTo, setPageHeight, setViewportHeight };
  }

  function sentScrollOffsets(port: { postMessage: ReturnType<typeof vi.fn> }) {
    return port.postMessage.mock.calls
      .map(([msg]) => msg as { type: string; top: number; nonce: string })
      .filter((msg) => msg.type === BRIDGE_MSG.scroll)
      .map((msg) => {
        expect(msg.nonce).toBe(RUNTIME_NONCE);
        return msg.top;
      });
  }

  const restoreTo = (top: number) => JSON.stringify({ top, budgetMs: BUDGET_MS });
  const restoredTo = (top: number) => ({ left: 0, top, behavior: "instant" });
  // jsdom's viewport is 768px tall, so a 1,000px page clamps the offset to 232.
  const CLAMPING_PAGE_HEIGHT = 1_000;

  it("reports the reader's offset once per frame", () => {
    const { userScrollTo } = fakeScrolling();
    const port = startRuntime();

    userScrollTo(120);
    userScrollTo(180);
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([180]);

    userScrollTo(0);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([180, 0]);
  });

  it("reports an offset the reader reached before the channel connected", () => {
    const { userScrollTo } = fakeScrolling();
    const port = startRuntime({
      beforeConnect: () => {
        userScrollTo(900);
        vi.advanceTimersToNextFrame();
      },
    });
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([900]);
  });

  it("does not report a clamped offset when the channel connects mid-restore", () => {
    const { setPageHeight } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const port = startRuntime({
      restoreScroll: restoreTo(640),
      beforeConnect: () => {
        setPageHeight(900); // the page shrinks, pulling the offset back to 132
        vi.advanceTimersToNextFrame();
      },
    });
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  it("re-asserts the saved offset each frame until the budget runs out", () => {
    const { scrollTo, userScrollTo, setPageHeight } = fakeScrolling({
      pageHeight: CLAMPING_PAGE_HEIGHT,
    });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
    expect(window.scrollY).toBe(232);
    expect(hide.isConnected).toBe(true);

    // Late content grows the page; the next frame reaches the saved offset and shows it.
    setPageHeight(2_000);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(640);
    expect(hide.isConnected).toBe(false);
    // The clamped offset is not reported, so it can't overwrite the saved one.
    expect(sentScrollOffsets(port)).toEqual([]);

    vi.advanceTimersByTime(BUDGET_MS);
    const writes = scrollTo.mock.calls.length;
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(writes);
    expect(sentScrollOffsets(port)).toEqual([640]);

    // After the restore settles, the reader's own scrolling is reported again.
    userScrollTo(700);
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([640, 700]);
  });

  it("shows the page short of the saved offset once the budget runs out", () => {
    fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    vi.advanceTimersByTime(BUDGET_MS - 20);
    expect(hide.isConnected).toBe(true);
    expect(sentScrollOffsets(port)).toEqual([]);
    vi.advanceTimersByTime(40);
    expect(hide.isConnected).toBe(false);
    expect(window.scrollY).toBe(232);
    // Saving the offset the page reached keeps the next remount from hiding it for a full budget.
    vi.advanceTimersToNextFrame();
    expect(sentScrollOffsets(port)).toEqual([232]);
  });

  it("re-asserts the saved offset once when a throttled frame lands past the budget", () => {
    const { scrollTo, setPageHeight } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    expect(window.scrollY).toBe(232);
    // A background tab runs no frames while the page grows and the budget passes.
    setPageHeight(2_000);
    vi.spyOn(performance, "now").mockReturnValue(performance.now() + BUDGET_MS);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(640);
    expect(hide.isConnected).toBe(false);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(sentScrollOffsets(port)).toEqual([640]);
  });

  it("turns scroll anchoring off until the restore settles, then gives the page its own", () => {
    fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const rootStyle = document.documentElement.style;
    rootStyle.setProperty("overflow-anchor", "auto");
    startRuntime({ restoreScroll: restoreTo(640) });
    expect(rootStyle.getPropertyValue("overflow-anchor")).toBe("none");
    expect(rootStyle.getPropertyPriority("overflow-anchor")).toBe("important");

    vi.advanceTimersByTime(BUDGET_MS + 20);
    expect(rootStyle.getPropertyValue("overflow-anchor")).toBe("auto");
    expect(rootStyle.getPropertyPriority("overflow-anchor")).toBe("");
  });

  it("keeps restoring when content growing above the reader moves the offset", () => {
    const { scrollTo, setPageHeight } = fakeScrolling();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    // Scroll anchoring shifts the offset by the 300px an image above it adds.
    setPageHeight(100_300, 940);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(window.scrollY).toBe(640);
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  it("keeps restoring when a taller viewport clamps the offset", () => {
    const { setPageHeight, setViewportHeight } = fakeScrolling({ pageHeight: 1_500 });
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    expect(window.scrollY).toBe(640);
    // Zooming the preview out makes its viewport taller, so the page can scroll less.
    setViewportHeight(1_000);
    expect(window.scrollY).toBe(500);
    vi.advanceTimersToNextFrame();
    setPageHeight(3_000);
    vi.advanceTimersToNextFrame();
    expect(window.scrollY).toBe(640);
    expect(sentScrollOffsets(port)).toEqual([]);
  });

  const dispatchToPage = (event: Event) => document.body.dispatchEvent(event);

  /** Press `key` on a focused element of the page, as a reader would. */
  function pressIn(focused: HTMLElement, key: string) {
    document.body.append(focused);
    focused.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    focused.remove();
  }

  const cancelable = { bubbles: true, cancelable: true };

  /** Dispatch `event` to a widget that consumes it, as a slide deck does with its keys. */
  function handledByPage(event: Event) {
    const widget = document.createElement("div");
    widget.addEventListener(event.type, (e) => e.preventDefault());
    document.body.append(widget);
    widget.dispatchEvent(event);
    widget.remove();
  }

  function editableText() {
    const text = document.createElement("div");
    // jsdom doesn't implement isContentEditable.
    Object.defineProperty(text, "isContentEditable", { value: true });
    return text;
  }

  it.each<[string, (userScrollTo: (top: number) => void) => void]>([
    ["the wheel", () => dispatchToPage(new Event("wheel", { bubbles: true }))],
    ["a touch drag", () => dispatchToPage(new Event("touchmove", { bubbles: true }))],
    [
      "a scroll key",
      () => dispatchToPage(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true })),
    ],
    ["a space on a focused link", () => pressIn(document.createElement("a"), " ")],
    ["the scrollbar", (userScrollTo) => userScrollTo(100)],
  ])("stops restoring when the reader takes over with %s", (_label, takeOver) => {
    const { scrollTo, userScrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    const port = startRuntime({ restoreScroll: restoreTo(640) });
    takeOver(userScrollTo);
    vi.advanceTimersToNextFrame();
    expect(hide.isConnected).toBe(false);
    expect(scrollTo).toHaveBeenCalledTimes(1);
    vi.advanceTimersToNextFrame();
    // The offset the page shows is saved even when the input didn't move it.
    expect(sentScrollOffsets(port)).toEqual([window.scrollY]);

    userScrollTo(200);
    vi.advanceTimersToNextFrame();
    expect(window.scrollY).toBe(200);
    expect(sentScrollOffsets(port).at(-1)).toBe(200);
  });

  it.each<[string, () => void]>([
    ["a click", () => dispatchToPage(new Event("pointerdown", { bubbles: true }))],
    ["a tap", () => dispatchToPage(new Event("touchstart", { bubbles: true }))],
    [
      "a modifier key",
      () => dispatchToPage(new KeyboardEvent("keydown", { key: "Control", bubbles: true })),
    ],
    [
      "the preview zoom shortcut",
      () =>
        dispatchToPage(new KeyboardEvent("keydown", { key: "=", ctrlKey: true, bubbles: true })),
    ],
    ["a space typed into a form field", () => pressIn(document.createElement("input"), " ")],
    ["an arrow key in editable text", () => pressIn(editableText(), "ArrowDown")],
    ["a space on a focused button", () => pressIn(document.createElement("button"), " ")],
    ["a space on a <summary>", () => pressIn(document.createElement("summary"), " ")],
    ["a pinch", () => dispatchToPage(new WheelEvent("wheel", { ctrlKey: true, bubbles: true }))],
    [
      "a scroll key the page handles",
      () => handledByPage(new KeyboardEvent("keydown", { key: "PageDown", ...cancelable })),
    ],
    ["a wheel the page handles", () => handledByPage(new WheelEvent("wheel", cancelable))],
  ])("keeps restoring through %s", (_label, interact) => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    const hide = addRestoreHide();
    startRuntime({ restoreScroll: restoreTo(640) });
    interact();
    vi.advanceTimersToNextFrame();
    expect(scrollTo).toHaveBeenCalledTimes(2);
    expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
    expect(hide.isConnected).toBe(true);
  });

  interface RuntimePort {
    onmessage: ((event: { data: unknown }) => void) | null;
    postMessage: ReturnType<typeof vi.fn>;
  }

  /** Restore to 640 in a page whose one comment's highlight starts `highlightTop` px down. */
  function restoreWithComment(
    highlightTop: number,
    run: (port: RuntimePort, hide: HTMLStyleElement) => void,
  ) {
    const rect = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
    try {
      document.body.innerHTML = "<p>Quarterly revenue</p>";
      // jsdom lacks the Custom Highlight API the runtime tracks active ranges with.
      vi.stubGlobal("CSS", { highlights: new Map() });
      vi.stubGlobal("Highlight", Set);
      // jsdom has no layout, so place the commented text explicitly.
      Object.defineProperty(Range.prototype, "getBoundingClientRect", {
        configurable: true,
        value: () => {
          const top = highlightTop - window.scrollY;
          return { top, bottom: top + 20, width: 60, height: 20 };
        },
      });
      const hide = addRestoreHide();
      const port = startRuntime({ restoreScroll: restoreTo(640) }) as unknown as RuntimePort;
      port.onmessage?.({
        data: {
          type: BRIDGE_MSG.setComments,
          comments: [{ id: "c1", anchor_content: "revenue", occ: 0 }],
        },
      });
      run(port, hide);
    } finally {
      if (rect) Object.defineProperty(Range.prototype, "getBoundingClientRect", rect);
      else delete (Range.prototype as Partial<Range>).getBoundingClientRect;
      vi.unstubAllGlobals();
      document.body.innerHTML = "";
    }
  }

  const activate = (port: RuntimePort, commentId: string | null) =>
    port.onmessage?.({
      data: {
        type: BRIDGE_MSG.setActive,
        active: commentId ? { comment_id: commentId, anchor_content: "revenue" } : null,
      },
    });
  const smoothScroll = expect.objectContaining({ behavior: "smooth" });
  // Centers the 20px highlight at 4,640 in jsdom's 768px viewport.
  const toOffscreenComment = { top: 4_266, behavior: "smooth" };

  it.each([
    ["an offscreen comment, scrolling to it", 4_640, true],
    ["an already visible comment, leaving the page still", 740, false],
  ])("stops restoring when the reader picks %s", (_label, highlightTop, scrollsToComment) => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    restoreWithComment(highlightTop, (port, hide) => {
      activate(port, null); // the parent's initial sync on connect
      expect(hide.isConnected).toBe(true);
      activate(port, "c1");
      expect(hide.isConnected).toBe(false);
      if (scrollsToComment) expect(scrollTo).toHaveBeenLastCalledWith(toOffscreenComment);
      else expect(scrollTo).not.toHaveBeenCalledWith(smoothScroll);

      const writes = scrollTo.mock.calls.length;
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(writes);
      expect(sentScrollOffsets(port)).toEqual([window.scrollY]);
    });
  });

  it.each([
    ["", undefined, false],
    [" while late content clamps the page", CLAMPING_PAGE_HEIGHT, true],
  ])(
    "keeps restoring when the initial sync restates a comment visible at the saved offset%s",
    (_label, pageHeight, stillHidden) => {
      const { scrollTo } = fakeScrolling({ pageHeight });
      // Clamped to 232, this highlight is below the viewport; at 640 it is in view.
      restoreWithComment(1_300, (port, hide) => {
        activate(port, "c1");
        expect(scrollTo).not.toHaveBeenCalledWith(smoothScroll);
        const writes = scrollTo.mock.calls.length;
        vi.advanceTimersToNextFrame();
        expect(scrollTo).toHaveBeenCalledTimes(writes + 1);
        expect(scrollTo).toHaveBeenLastCalledWith(restoredTo(640));
        expect(hide.isConnected).toBe(stillHidden);
      });
    },
  );

  // A linked comment reaches the frame in this same first sync, so it must win.
  it("scrolls to an active comment outside the saved view on the initial sync", () => {
    const { scrollTo } = fakeScrolling({ pageHeight: CLAMPING_PAGE_HEIGHT });
    restoreWithComment(4_640, (port, hide) => {
      activate(port, "c1");
      expect(scrollTo).toHaveBeenLastCalledWith(toOffscreenComment);
      expect(hide.isConnected).toBe(false);
      const writes = scrollTo.mock.calls.length;
      vi.advanceTimersToNextFrame();
      expect(scrollTo).toHaveBeenCalledTimes(writes);
    });
  });

  it.each([
    ["no saved offset", undefined],
    ["null", "null"],
    ["invalid JSON", "{"],
    ["a negative offset", JSON.stringify({ top: -10, budgetMs: BUDGET_MS })],
    ["a string offset", JSON.stringify({ top: "640", budgetMs: BUDGET_MS })],
    ["no budget", JSON.stringify({ top: 640 })],
    ["a zero budget", JSON.stringify({ top: 640, budgetMs: 0 })],
  ])("starts without restoring given %s", (_label, restoreScroll) => {
    const { scrollTo } = fakeScrolling();
    const hide = addRestoreHide();
    const artifactStyle = addRestoreHide("another-nonce");
    startRuntime({ restoreScroll });
    expect(hide.isConnected).toBe(false);
    expect(artifactStyle.isConnected).toBe(true);
    vi.advanceTimersToNextFrame();
    expect(scrollTo).not.toHaveBeenCalled();
    expect(runtimeListeners.some(([, type]) => type === "wheel")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// parseBridgeMessage — inbound validation (guards against spoofed postMessage)
// ---------------------------------------------------------------------------

describe("parseBridgeMessage", () => {
  const NONCE = "n1";
  const base = { source: BRIDGE_SOURCE, nonce: NONCE };

  it("accepts a well-formed selection message with its occurrence index", () => {
    const msg = parseBridgeMessage(
      {
        ...base,
        type: BRIDGE_MSG.selection,
        text: "Design Goals",
        occ: 2,
        rect: { left: 1, top: 2, right: 3, bottom: 4 },
      },
      NONCE,
    );
    expect(msg).toEqual({
      type: BRIDGE_MSG.selection,
      text: "Design Goals",
      occ: 2,
      rect: { left: 1, top: 2, right: 3, bottom: 4 },
      reflow: false,
    });
  });

  it("marks a selection re-reported after the frame resized", () => {
    const rect = { left: 0, top: 0, right: 0, bottom: 0 };
    const parse = (reflow: unknown) =>
      parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "x", rect, reflow }, NONCE);
    expect(parse(true)).toMatchObject({ reflow: true });
    expect(parse("true")).toMatchObject({ reflow: false });
  });

  it("defaults occ to 0 when a selection message omits it (older frame)", () => {
    const msg = parseBridgeMessage(
      {
        ...base,
        type: BRIDGE_MSG.selection,
        text: "x",
        rect: { left: 0, top: 0, right: 0, bottom: 0 },
      },
      NONCE,
    );
    expect(msg).toMatchObject({ type: BRIDGE_MSG.selection, occ: 0 });
  });

  it("round-trips anchor text containing quotes and newlines", () => {
    const text = 'He said "hi"\nthen left';
    const msg = parseBridgeMessage(
      { ...base, type: BRIDGE_MSG.selection, text, rect: { left: 0, top: 0, right: 0, bottom: 0 } },
      NONCE,
    );
    expect(msg && "text" in msg && msg.text).toBe(text);
  });

  it("accepts commentClick, selectionCleared, and ready", () => {
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.commentClick, id: "c1" }, NONCE)).toEqual(
      {
        type: BRIDGE_MSG.commentClick,
        id: "c1",
      },
    );
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.selectionCleared }, NONCE)).toEqual({
      type: BRIDGE_MSG.selectionCleared,
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.ready }, NONCE)).toEqual({
      type: BRIDGE_MSG.ready,
    });
  });

  it("accepts a zoom request only with a known action", () => {
    for (const action of ["in", "out", "reset"]) {
      expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom, action }, NONCE)).toEqual({
        type: BRIDGE_MSG.zoom,
        action,
      });
    }
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom, action: "2" }, NONCE)).toBeNull();
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.zoom }, NONCE)).toBeNull();
  });

  it("accepts a scroll report only with a finite, non-negative offset", () => {
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top: 0 }, NONCE)).toEqual({
      type: BRIDGE_MSG.scroll,
      top: 0,
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top: 812.5 }, NONCE)).toEqual({
      type: BRIDGE_MSG.scroll,
      top: 812.5,
    });
    for (const top of [-1, Number.NaN, Number.POSITIVE_INFINITY, "640", undefined]) {
      expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.scroll, top }, NONCE)).toBeNull();
    }
  });

  it("rejects a wrong nonce (spoof from artifact JS)", () => {
    expect(
      parseBridgeMessage({ ...base, nonce: "other", type: BRIDGE_MSG.selectionCleared }, NONCE),
    ).toBeNull();
  });

  it("rejects a wrong source tag", () => {
    expect(
      parseBridgeMessage(
        { source: "evil", nonce: NONCE, type: BRIDGE_MSG.selectionCleared },
        NONCE,
      ),
    ).toBeNull();
  });

  it("rejects an unknown type, a malformed selection, and non-objects", () => {
    expect(parseBridgeMessage({ ...base, type: "omni:bogus" }, NONCE)).toBeNull();
    // Empty text / missing rect must not produce a selection.
    expect(
      parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "   " }, NONCE),
    ).toBeNull();
    expect(
      parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "x" }, NONCE),
    ).toBeNull();
    expect(parseBridgeMessage("not-an-object", NONCE)).toBeNull();
    expect(parseBridgeMessage(null, NONCE)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// findAnchorInSource — rendered selection text -> raw HTML source offsets
// ---------------------------------------------------------------------------

describe("findAnchorInSource", () => {
  it("returns exact offsets when the anchor is a verbatim substring", () => {
    const src = "<h1>Title</h1><p>The quick brown fox.</p>";
    const res = findAnchorInSource(src, "quick brown fox");
    expect(res).not.toBeNull();
    expect(src.slice(res!.start_index, res!.end_index)).toBe("quick brown fox");
  });

  it("trims the anchor before matching", () => {
    const src = "<p>hello world</p>";
    const res = findAnchorInSource(src, "  hello world  ");
    expect(src.slice(res!.start_index, res!.end_index)).toBe("hello world");
  });

  it("tolerates collapsed whitespace via a normalized fallback", () => {
    // Rendered selection collapses the newline+indent the source spells out.
    const src = "<p>Design\n      goals matter</p>";
    const res = findAnchorInSource(src, "Design goals matter");
    expect(res).not.toBeNull();
    expect(src.slice(res!.start_index, res!.end_index)).toBe("Design\n      goals matter");
  });

  it("returns null when the anchor is empty or absent", () => {
    expect(findAnchorInSource("<p>hi</p>", "   ")).toBeNull();
    expect(findAnchorInSource("<p>hi</p>", "not present anywhere")).toBeNull();
  });

  it("picks the first occurrence for repeated text by default", () => {
    const src = "alpha beta alpha";
    const res = findAnchorInSource(src, "alpha");
    expect(res).toEqual({ start_index: 0, end_index: 5 });
  });

  it("resolves the requested occurrence for repeated text", () => {
    // "Aurora Sync" as a title, then again in body prose — selecting the body
    // copy (occurrence 1) must anchor to the SECOND match, not the first.
    const src = "<h1>Aurora Sync</h1><p>Aurora Sync keeps state.</p>";
    const first = src.indexOf("Aurora Sync");
    const second = src.indexOf("Aurora Sync", first + 1);
    expect(findAnchorInSource(src, "Aurora Sync", 0)).toEqual({
      start_index: first,
      end_index: first + "Aurora Sync".length,
    });
    expect(findAnchorInSource(src, "Aurora Sync", 1)).toEqual({
      start_index: second,
      end_index: second + "Aurora Sync".length,
    });
  });

  it("resolves a later occurrence even when an earlier one is whitespace-wrapped", () => {
    const src = "<p>then\n   latency</p><p>then latency again</p>";
    const second = src.indexOf("then latency again");
    expect(findAnchorInSource(src, "then latency", 1)).toEqual({
      start_index: second,
      end_index: second + "then latency".length,
    });
  });

  it("anchors occurrence 0 to the wrapped first copy, not a later verbatim one", () => {
    // Regression: the old occurrence-0 fast path used a verbatim indexOf, which
    // skipped the whitespace-wrapped first rendered copy and landed on the
    // second (verbatim) one — storing the comment at the wrong offset.
    const src = "<p>then\n   latency</p><p>then latency</p>";
    const firstWrapped = src.indexOf("then\n   latency");
    const res = findAnchorInSource(src, "then latency", 0);
    expect(res).not.toBeNull();
    expect(res!.start_index).toBe(firstWrapped);
    expect(src.slice(res!.start_index, res!.end_index)).toBe("then\n   latency");
  });

  it("skips matches inside tags/attributes when counting occurrences", () => {
    // "Submit" appears first in an attribute (non-rendered), then as button
    // text. Occurrence 0 must resolve to the rendered text, not the attribute.
    const src = '<button aria-label="Submit">Submit</button>';
    const rendered = src.indexOf("Submit</button>");
    const res = findAnchorInSource(src, "Submit", 0);
    expect(res).not.toBeNull();
    expect(res!.start_index).toBe(rendered);
  });

  it("skips matches inside <title>, comments, and <script>", () => {
    const src =
      "<head><title>Report</title></head>" +
      "<!-- Report draft -->" +
      "<script>var x = 'Report';</script>" +
      "<h1>Report</h1>";
    const heading = src.lastIndexOf("Report");
    const res = findAnchorInSource(src, "Report", 0);
    expect(res!.start_index).toBe(heading);
  });

  it("returns null when the requested occurrence doesn't exist", () => {
    expect(findAnchorInSource("only once here", "once", 3)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// anchorOccurrence — which copy of repeated anchor text a comment refers to
// ---------------------------------------------------------------------------

describe("anchorOccurrence", () => {
  // A title reused verbatim in the body — the exact case that highlighted both.
  const src = "<h1>Aurora Sync</h1><p>Aurora Sync keeps state.</p>";
  const first = src.indexOf("Aurora Sync");
  const second = src.indexOf("Aurora Sync", first + 1);

  it("returns 0 for the first occurrence (the title)", () => {
    expect(anchorOccurrence(src, "Aurora Sync", first)).toBe(0);
  });

  it("returns 1 for the second occurrence (the body)", () => {
    expect(anchorOccurrence(src, "Aurora Sync", second)).toBe(1);
  });

  it("counts whitespace-tolerantly so wrapped source occurrences still count", () => {
    // First occurrence wraps across lines; the second is the target.
    const wrapped = "<p>then\n   latency</p><p>then latency again</p>";
    const target = wrapped.indexOf("then latency again");
    expect(anchorOccurrence(wrapped, "then latency", target)).toBe(1);
  });

  it("returns 0 for empty anchor text", () => {
    expect(anchorOccurrence(src, "   ", 0)).toBe(0);
  });

  it("does not count matches in non-rendered regions (attributes/comments)", () => {
    // An attribute "Submit" precedes the rendered button text; the rendered copy
    // must still be occurrence 0 so its count aligns with the in-frame bridge.
    const withAttr = '<button title="Submit"><!-- Submit --><span>Submit</span></button>';
    const rendered = withAttr.lastIndexOf("Submit");
    expect(anchorOccurrence(withAttr, "Submit", rendered)).toBe(0);
  });
});
