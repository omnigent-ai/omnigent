import { describe, expect, it } from "vitest";

import { normalizeTypedUrl } from "./normalizeTypedUrl";

describe("normalizeTypedUrl", () => {
  it("leaves an explicit http/https scheme untouched (case-insensitive)", () => {
    expect(normalizeTypedUrl("https://example.com")).toBe("https://example.com");
    expect(normalizeTypedUrl("http://go/glean")).toBe("http://go/glean");
    expect(normalizeTypedUrl("HTTP://GO/x")).toBe("HTTP://GO/x");
  });

  it("uses http:// for dotless (corp shortname) hosts", () => {
    expect(normalizeTypedUrl("go/glean")).toBe("http://go/glean");
    expect(normalizeTypedUrl("go")).toBe("http://go");
    expect(normalizeTypedUrl("wiki/SomePage")).toBe("http://wiki/SomePage");
    expect(normalizeTypedUrl("jira/PROJ-123?focus=true")).toBe("http://jira/PROJ-123?focus=true");
  });

  it("uses https:// for dotted hosts", () => {
    expect(normalizeTypedUrl("example.com")).toBe("https://example.com");
    expect(normalizeTypedUrl("example.com/path?q=1")).toBe("https://example.com/path?q=1");
  });

  it("treats a host:port with no dot as dotless", () => {
    expect(normalizeTypedUrl("localhost:3000/x")).toBe("http://localhost:3000/x");
  });

  it("trims surrounding whitespace", () => {
    expect(normalizeTypedUrl("  example.com  ")).toBe("https://example.com");
  });
});
