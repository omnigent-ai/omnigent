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
});

// ---------------------------------------------------------------------------
// Bridge runtime — zoom shortcuts inside the preview
// ---------------------------------------------------------------------------

describe("bridge runtime zoom shortcuts", () => {
  const NONCE = "zoom-nonce";
  const added: [EventTarget, string, EventListenerOrEventListenerObject, unknown][] = [];

  afterEach(() => {
    for (const [target, type, listener, options] of added.splice(0)) {
      target.removeEventListener(type, listener, options as EventListenerOptions);
    }
    vi.restoreAllMocks();
  });

  /** Run the runtime in this document, as the injected script would, and connect a port. */
  function startRuntime({
    connect = true,
    beforeConnect,
  }: { connect?: boolean; beforeConnect?: () => void } = {}) {
    for (const target of [window, document]) {
      const original = target.addEventListener.bind(target);
      vi.spyOn(target, "addEventListener").mockImplementation((type, listener, options) => {
        if (listener) added.push([target, type, listener, options]);
        original(type, listener, options);
      });
    }
    const script = document.createElement("script");
    script.setAttribute("data-omni-nonce", NONCE);
    script.setAttribute(
      "data-omni-protocol",
      JSON.stringify({ source: BRIDGE_SOURCE, types: BRIDGE_MSG }),
    );
    vi.spyOn(document, "currentScript", "get").mockReturnValue(script);
    new Function(HTML_COMMENT_BRIDGE_RUNTIME)();
    beforeConnect?.();

    const port = { postMessage: vi.fn(), onmessage: null };
    if (connect) {
      const init = new Event("message");
      Object.defineProperties(init, {
        data: { value: { source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.init } },
        ports: { value: [port] },
      });
      window.dispatchEvent(init);
      port.postMessage.mockClear(); // drop the ready message
    }
    vi.mocked(window.addEventListener).mockRestore();
    vi.mocked(document.addEventListener).mockRestore();
    return port;
  }

  function press(init: KeyboardEventInit): boolean {
    const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
    document.body.dispatchEvent(event);
    return event.defaultPrevented;
  }

  function sentZoomActions(port: { postMessage: ReturnType<typeof vi.fn> }) {
    return port.postMessage.mock.calls.map(([msg]) => {
      expect(msg).toMatchObject({ source: BRIDGE_SOURCE, nonce: NONCE, type: BRIDGE_MSG.zoom });
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
