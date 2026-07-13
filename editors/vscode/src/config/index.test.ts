import { describe, it, expect } from "vitest";
import { resolveServerTarget, hostTypeOf, originOf } from "./index";

describe("hostTypeOf", () => {
  it.each([
    ["http://127.0.0.1:6767", "local"],
    ["http://localhost:6767", "local"],
    ["http://[::1]:6767", "local"],
    ["https://omnigent.example.com", "remote"],
    ["https://dbc-abc123.cloud.databricks.com", "remote"],
    ["not a url", "unknown"],
  ])("classifies %s as %s", (url, expected) => {
    expect(hostTypeOf(url)).toBe(expected);
  });
});

describe("originOf", () => {
  it("strips path and trailing slash", () => {
    expect(originOf("http://127.0.0.1:6767/app/")).toBe("http://127.0.0.1:6767");
  });
});

describe("resolveServerTarget (localhost-only)", () => {
  it("manual loopback override wins over discovered local", () => {
    const r = resolveServerTarget(
      { serverUrl: "http://127.0.0.1:9000" },
      { found: true, baseUrl: "http://127.0.0.1:6767", health: "ok" },
    );
    expect(r.status).toBe("resolved");
    if (r.status === "resolved") {
      expect(r.target.source).toBe("manual");
      expect(r.target.hostType).toBe("local");
      expect(r.target.origin).toBe("http://127.0.0.1:9000");
    }
  });

  it("rejects a manual REMOTE override (remote-unsupported)", () => {
    const r = resolveServerTarget(
      { serverUrl: "https://omnigent.example.com" },
      { found: true, baseUrl: "http://127.0.0.1:6767", health: "ok" },
    );
    expect(r).toEqual({ status: "needs-prompt", reason: "remote-unsupported" });
  });

  it("rejects a malformed manual override (unknown -> remote-unsupported)", () => {
    const r = resolveServerTarget({ serverUrl: "not a url" }, { found: false });
    expect(r).toEqual({ status: "needs-prompt", reason: "remote-unsupported" });
  });

  it("uses discovered local when healthy and no manual override", () => {
    const r = resolveServerTarget(
      { serverUrl: "" },
      { found: true, baseUrl: "http://127.0.0.1:6767", health: "ok" },
    );
    expect(r.status).toBe("resolved");
    if (r.status === "resolved") {
      expect(r.target.source).toBe("discovered");
      expect(r.target.hostType).toBe("local");
      expect(r.target.baseUrl).toBe("http://127.0.0.1:6767");
    }
  });

  it("needs-prompt when discovered local is unhealthy", () => {
    const r = resolveServerTarget(
      { serverUrl: "" },
      { found: true, baseUrl: "http://127.0.0.1:6767", health: "timeout" },
    );
    expect(r).toEqual({ status: "needs-prompt", reason: "local-unhealthy" });
  });

  it("needs-prompt when nothing is available", () => {
    const r = resolveServerTarget({ serverUrl: "" }, { found: false });
    expect(r).toEqual({ status: "needs-prompt", reason: "no-manual-no-local" });
  });
});
