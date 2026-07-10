import { describe, expect, it } from "vitest";

import { normalizeTypedUrl } from "./normalizeTypedUrl";

describe("normalizeTypedUrl", () => {
  it("leaves an explicit http/https scheme untouched (case-insensitive)", () => {
    expect(normalizeTypedUrl("https://example.com")).toBe("https://example.com");
    expect(normalizeTypedUrl("http://myhost")).toBe("http://myhost");
    expect(normalizeTypedUrl("HTTP://MYHOST/x")).toBe("HTTP://MYHOST/x");
  });

  it("uses http:// for dotless hosts", () => {
    expect(normalizeTypedUrl("myhost")).toBe("http://myhost");
    expect(normalizeTypedUrl("wiki/SomePage")).toBe("http://wiki/SomePage");
    expect(normalizeTypedUrl("wiki/page-123?focus=true")).toBe("http://wiki/page-123?focus=true");
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
