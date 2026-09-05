import { describe, expect, it } from "vitest";
import { normalizeServerUrl } from "./ServerSelectStep";

// This mirrors electron/src/url.js semantics by hand (the renderer can't import
// the CommonJS main-process module). The test pins the contract so drift from
// that source is caught — the main process re-normalizes on connect, so a
// mismatch shows up as a client-side pre-filter that diverges from the shell.
describe("normalizeServerUrl", () => {
  it("returns null for empty / whitespace", () => {
    expect(normalizeServerUrl("")).toBeNull();
    expect(normalizeServerUrl("   ")).toBeNull();
  });

  it("defaults a bare host to http:// and normalizes to an origin", () => {
    expect(normalizeServerUrl("localhost:6767")).toBe("http://localhost:6767/");
    expect(normalizeServerUrl("example.com")).toBe("http://example.com/");
    expect(normalizeServerUrl("127.0.0.1:6767")).toBe("http://127.0.0.1:6767/");
  });

  it("preserves an explicit http(s) scheme", () => {
    expect(normalizeServerUrl("https://omni.example.com/")).toBe("https://omni.example.com/");
  });

  it("rejects non-http schemes and garbage", () => {
    expect(normalizeServerUrl("javascript:alert(1)")).toBeNull();
    expect(normalizeServerUrl("file:///etc/passwd")).toBeNull();
    expect(normalizeServerUrl("ftp://x.com")).toBeNull();
    expect(normalizeServerUrl("not a url")).toBeNull();
    expect(normalizeServerUrl("http://")).toBeNull();
  });
});
