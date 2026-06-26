import { describe, expect, it } from "vitest";
import {
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  buildBridgeScript,
  findAnchorInSource,
  injectCommentBridge,
  parseBridgeMessage,
} from "./htmlCommentBridge";

// ---------------------------------------------------------------------------
// injectCommentBridge — script/style placement (mirrors prepareHtmlPreviewDoc)
// ---------------------------------------------------------------------------

describe("injectCommentBridge", () => {
  const NONCE = "test-nonce-123";

  it("injects the bridge script before </body> when present", () => {
    const html = "<html><head></head><body><p>hi</p></body></html>";
    const out = injectCommentBridge(html, NONCE);
    const scriptAt = out.indexOf("<script>");
    const bodyCloseAt = out.indexOf("</body>");
    expect(scriptAt).toBeGreaterThan(-1);
    expect(scriptAt).toBeLessThan(bodyCloseAt);
    expect(out).toContain(NONCE);
  });

  it("falls back to before </html> when there is no body", () => {
    const html = "<html><head></head><p>hi</p></html>";
    const out = injectCommentBridge(html, NONCE);
    expect(out.indexOf("<script>")).toBeLessThan(out.indexOf("</html>"));
  });

  it("appends to a bare fragment with no body/html", () => {
    const out = injectCommentBridge("<p>just a fragment</p>", NONCE);
    // prepareHtmlPreviewDoc prepends <base> for a bare fragment; the bridge is
    // then appended at the end since there's no </body>/</html> to inject before.
    expect(out).toContain("<p>just a fragment</p>");
    const fragAt = out.indexOf("<p>just a fragment</p>");
    expect(out.indexOf("<script>")).toBeGreaterThan(fragAt);
  });

  it("preserves the prepared <base target=_blank> link behavior", () => {
    const out = injectCommentBridge("<html><head></head><body></body></html>", NONCE);
    expect(out).toContain('<base target="_blank">');
  });

  it("includes the highlight style for the Custom Highlight ranges", () => {
    const out = injectCommentBridge("<body></body>", NONCE);
    expect(out).toContain("::highlight(omni-comment)");
    expect(out).toContain("::highlight(omni-comment-active)");
  });

  it("substitutes the nonce, source tag, and message types into the script", () => {
    const script = buildBridgeScript(NONCE);
    expect(script).toContain(NONCE);
    expect(script).toContain(BRIDGE_SOURCE);
    expect(script).toContain(BRIDGE_MSG.selection);
    // Placeholders must be fully replaced.
    expect(script).not.toContain("__OMNI_NONCE__");
    expect(script).not.toContain("__OMNI_TYPES__");
  });
});

// ---------------------------------------------------------------------------
// parseBridgeMessage — inbound validation (guards against spoofed postMessage)
// ---------------------------------------------------------------------------

describe("parseBridgeMessage", () => {
  const NONCE = "n1";
  const base = { source: BRIDGE_SOURCE, nonce: NONCE };

  it("accepts a well-formed selection message", () => {
    const msg = parseBridgeMessage(
      { ...base, type: BRIDGE_MSG.selection, text: "Design Goals", rect: { left: 1, top: 2, right: 3, bottom: 4 } },
      NONCE,
    );
    expect(msg).toEqual({
      type: BRIDGE_MSG.selection,
      text: "Design Goals",
      rect: { left: 1, top: 2, right: 3, bottom: 4 },
    });
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
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.commentClick, id: "c1" }, NONCE)).toEqual({
      type: BRIDGE_MSG.commentClick,
      id: "c1",
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.selectionCleared }, NONCE)).toEqual({
      type: BRIDGE_MSG.selectionCleared,
    });
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.ready }, NONCE)).toEqual({
      type: BRIDGE_MSG.ready,
    });
  });

  it("rejects a wrong nonce (spoof from artifact JS)", () => {
    expect(
      parseBridgeMessage({ ...base, nonce: "other", type: BRIDGE_MSG.selectionCleared }, NONCE),
    ).toBeNull();
  });

  it("rejects a wrong source tag", () => {
    expect(
      parseBridgeMessage({ source: "evil", nonce: NONCE, type: BRIDGE_MSG.selectionCleared }, NONCE),
    ).toBeNull();
  });

  it("rejects an unknown type, a malformed selection, and non-objects", () => {
    expect(parseBridgeMessage({ ...base, type: "omni:bogus" }, NONCE)).toBeNull();
    // Empty text / missing rect must not produce a selection.
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "   " }, NONCE)).toBeNull();
    expect(parseBridgeMessage({ ...base, type: BRIDGE_MSG.selection, text: "x" }, NONCE)).toBeNull();
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

  it("picks the first occurrence for repeated text", () => {
    const src = "alpha beta alpha";
    const res = findAnchorInSource(src, "alpha");
    expect(res).toEqual({ start_index: 0, end_index: 5 });
  });
});
