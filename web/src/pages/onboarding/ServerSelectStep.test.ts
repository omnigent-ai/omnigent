import { describe, expect, it } from "vitest";
import { isLocalInstall, normalizeServerUrl } from "./ServerSelectStep";

describe("isLocalInstall", () => {
  it("matches only the CLI's plain-HTTP loopback root on its port", () => {
    expect(isLocalInstall("http://localhost:6767/")).toBe(true);
    expect(isLocalInstall("http://127.0.0.1:6767")).toBe(true);
    expect(isLocalInstall("https://localhost:6767/")).toBe(false);
    expect(isLocalInstall("https://localhost:6767/team")).toBe(false);
    expect(isLocalInstall("http://localhost:6767/team")).toBe(false);
    expect(isLocalInstall("http://localhost:8000/")).toBe(false);
    expect(isLocalInstall("http://example.com:6767/")).toBe(false);
  });
});

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
