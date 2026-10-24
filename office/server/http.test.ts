import { describe, expect, it } from "vitest";
import { isAllowedHost, isAllowedOrigin } from "./http.ts";

const PORTS = [6780, 6781];

describe("isAllowedHost", () => {
  it.each(["127.0.0.1:6780", "localhost:6781", "[::1]:6780", "localhost"])("allows loopback host %s", (host) => {
    expect(isAllowedHost(host, PORTS)).toBe(true);
  });

  it.each(["evil.example:6780", "127.0.0.1:9999", "192.168.1.2:6780", undefined])("rejects %s", (host) => {
    expect(isAllowedHost(host, PORTS)).toBe(false);
  });
});

describe("isAllowedOrigin", () => {
  it("allows only the office's own loopback origins", () => {
    expect(isAllowedOrigin("http://127.0.0.1:6781", PORTS)).toBe(true);
    expect(isAllowedOrigin("http://localhost:6780", PORTS)).toBe(true);
    expect(isAllowedOrigin("https://evil.example", PORTS)).toBe(false);
    expect(isAllowedOrigin("http://localhost:3000", PORTS)).toBe(false);
    expect(isAllowedOrigin("null", PORTS)).toBe(false);
    expect(isAllowedOrigin(undefined, PORTS)).toBe(false);
  });
});
