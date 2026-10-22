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

describe("normalizeServerUrl", () => {
  it("returns null for empty / whitespace", () => {
    expect(normalizeServerUrl("")).toBeNull();
    expect(normalizeServerUrl("   ")).toBeNull();
  });

  it("defaults remote hosts to HTTPS and loopback to HTTP", () => {
    expect(normalizeServerUrl("localhost:6767")).toBe("http://localhost:6767/");
    expect(normalizeServerUrl("example.com")).toBe("https://example.com/");
    expect(normalizeServerUrl("127.0.0.1:6767")).toBe("http://127.0.0.1:6767/");
    expect(normalizeServerUrl("[::1]:6767")).toBe("http://[::1]:6767/");
  });

  it("preserves an explicit http(s) scheme", () => {
    expect(normalizeServerUrl("https://omni.example.com/")).toBe("https://omni.example.com/");
    expect(normalizeServerUrl("http://omni.example.com/")).toBe("http://omni.example.com/");
  });

  it("matches the shell's root normalization and Databricks organization handling", () => {
    expect(normalizeServerUrl(" example.com/path?extra=value#fragment ")).toBe(
      "https://example.com/",
    );
    expect(normalizeServerUrl("http://workspace.cloud.databricks.com/omnigent?o=123#session")).toBe(
      "https://workspace.cloud.databricks.com/?o=123",
    );
    expect(normalizeServerUrl("http://workspace.cloud.databricks.com:8080/?o=123")).toBe(
      "http://workspace.cloud.databricks.com:8080/?o=123",
    );
  });

  it("rejects non-http schemes and garbage", () => {
    expect(normalizeServerUrl("javascript:alert(1)")).toBeNull();
    expect(normalizeServerUrl("file:///etc/passwd")).toBeNull();
    expect(normalizeServerUrl("ftp://x.com")).toBeNull();
    expect(normalizeServerUrl("not a url")).toBeNull();
    expect(normalizeServerUrl("http://")).toBeNull();
  });
});
