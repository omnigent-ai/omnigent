import { describe, it, expect } from "vitest";
import { interpretHealth } from "./health";

describe("interpretHealth", () => {
  it("200 + {status:'ok'} -> ok", () => {
    expect(interpretHealth({ status: 200, body: { status: "ok" } })).toBe("ok");
  });

  it("200 without an ok status body -> unhealthy", () => {
    expect(interpretHealth({ status: 200, body: { status: "degraded" } })).toBe("unhealthy");
    expect(interpretHealth({ status: 200, body: null })).toBe("unhealthy");
  });

  it("non-200 -> unhealthy", () => {
    expect(interpretHealth({ status: 503, body: { status: "ok" } })).toBe("unhealthy");
  });

  it("timeout -> timeout", () => {
    expect(interpretHealth({ timedOut: true })).toBe("timeout");
  });

  it("network error -> unreachable", () => {
    expect(interpretHealth({ networkError: true })).toBe("unreachable");
  });
});
