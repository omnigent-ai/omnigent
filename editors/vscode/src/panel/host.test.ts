/**
 * Unit tests for the pure helpers in panel/host.ts (render decision + placeholder).
 */
import { describe, it, expect } from "vitest";
import { shouldUseIframe, renderResolvingHtml } from "./host";
import type { ServerTarget } from "../config";

function target(hostType: ServerTarget["hostType"]): ServerTarget {
  return {
    baseUrl: "http://127.0.0.1:6767",
    origin: "http://127.0.0.1:6767",
    hostType,
    source: "discovered",
  };
}

describe("shouldUseIframe", () => {
  it("uses iframe for a local server", () => {
    expect(shouldUseIframe(target("local"))).toBe(true);
  });
  it("does not use iframe for a remote server", () => {
    expect(shouldUseIframe(target("remote"))).toBe(false);
  });
  it("does not use iframe for an unknown host", () => {
    expect(shouldUseIframe(target("unknown"))).toBe(false);
  });
});

describe("renderResolvingHtml", () => {
  it("is a self-contained placeholder with a CSP and no scripts", () => {
    const html = renderResolvingHtml();
    expect(html).toContain("Content-Security-Policy");
    expect(html).toContain("Resolving Omnigent server");
    expect(html).not.toContain("<script");
  });
});
