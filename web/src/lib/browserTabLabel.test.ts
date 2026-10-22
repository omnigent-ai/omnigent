import { describe, expect, it } from "vitest";
import {
  browserTabLabel,
  browserTabNameSeed,
  browserUrlLabel,
  NEW_BROWSER_TAB_LABEL,
  normalizeBrowserTabName,
} from "./browserTabLabel";

describe("browserUrlLabel", () => {
  it.each([
    ["https://github.com/", "github.com"],
    ["https://www.example.com/docs", "example.com/docs"],
    ["http://localhost:3000/settings?tab=1#top", "localhost:3000/settings"],
    ["https://github.com/omnigent-ai/omnigent", "github.com/omnigent-ai/omnigent"],
    ["https://github.com/omnigent-ai/omnigent/pull/123", "github.com/…/pull/123"],
    ["https://example.com/a%20b/c/d%20e", "example.com/…/c/d e"],
    ["https://example.com/bad%E0%A4%A/x/y", "example.com/…/x/y"],
    ["http://[::1]:3000/x", "[::1]:3000/x"],
    ["https://example.com/a/b/", "example.com/a/b"],
    ["https://example.com/a%2Fb", "example.com/a%2Fb"],
    ["file:///Users/me/report.html", "report.html"],
    ["file:///", "file:///"],
    ["about:blank#top", "New tab"],
    ["data:text/plain,\u202Etxt.exe", "data:text/plain,%E2%80%AEtxt.exe"],
    ["not a url \u200B\u0007", "not a url %E2%80%8B%07"],
    ["https://evil.test/%E2%80%AEmoc.elgoog", "evil.test/%E2%80%AEmoc.elgoog"],
    ["https://example.com/a%0Ab", "example.com/a%0Ab"],
    ["data:text/plain,hello", "data:text/plain,hello"],
    ["not a url", "not a url"],
    ["", NEW_BROWSER_TAB_LABEL],
    ["   ", NEW_BROWSER_TAB_LABEL],
    ["about:blank", NEW_BROWSER_TAB_LABEL],
    [null, NEW_BROWSER_TAB_LABEL],
    [undefined, NEW_BROWSER_TAB_LABEL],
  ])("labels %j as %j", (url, label) => {
    expect(browserUrlLabel(url)).toBe(label);
  });

  it("shortens a long web URL label", () => {
    expect(browserUrlLabel(`https://example.com/${"a".repeat(100)}`)).toBe(
      `example.com/${"a".repeat(48)}…`,
    );
    expect(browserUrlLabel(`file:///tmp/${"b".repeat(70)}.html`)).toBe(`${"b".repeat(60)}…`);
    expect(browserUrlLabel(`data:text/plain,${"x".repeat(1_000_000)}`)).toBe(
      `data:text/plain,${"x".repeat(44)}…`,
    );
  });

  it("shortens a long URL that has no host", () => {
    const dataUrl = `data:text/html;base64,${"A".repeat(5000)}`;
    expect(browserUrlLabel(dataUrl)).toBe(`${dataUrl.slice(0, 60)}…`);
    expect(browserUrlLabel("x".repeat(70))).toBe(`${"x".repeat(60)}…`);
    // Cut by code point, never through an emoji.
    const emojiUrl = `data:${"x".repeat(54)}${"😀".repeat(3)}`;
    expect(browserUrlLabel(emojiUrl)).toBe(`data:${"x".repeat(54)}😀…`);
  });
});

describe("browser tab name limits", () => {
  it("trims and caps manual names by code point", () => {
    expect(normalizeBrowserTabName("  Docs  ")).toBe("Docs");
    expect(normalizeBrowserTabName("   ")).toBe("");
    expect(normalizeBrowserTabName(`${"x".repeat(79)}😀😀`)).toBe(`${"x".repeat(79)}😀`);
    expect(browserTabNameSeed(`${"x".repeat(79)}😀😀`)).toBe(`${"x".repeat(79)}😀`);
    expect(browserTabNameSeed("Docs")).toBe("Docs");
  });
});

describe("browserTabLabel", () => {
  it("prefers a manual name and falls back to the URL when it is blank", () => {
    expect(browserTabLabel(" Dev server ", "http://localhost:3000/")).toBe("Dev server");
    expect(browserTabLabel("  ", "http://localhost:3000/")).toBe("localhost:3000");
    expect(browserTabLabel(null, null)).toBe(NEW_BROWSER_TAB_LABEL);
  });
});
