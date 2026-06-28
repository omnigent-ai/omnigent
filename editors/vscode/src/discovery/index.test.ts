/**
 * Tests for discoverLocalServer + the live liveness probe, exercised through the
 * injectable DiscoveryIO boundary (no real fs/network) plus a real isPidAlive check.
 */
import { describe, it, expect } from "vitest";
import { discoverLocalServer, type DiscoveryIO, isPidAlive } from "./index";
import type { HealthOutcome } from "./health";

function io(over: Partial<DiscoveryIO>): DiscoveryIO {
  return {
    readPidfile: async () => null,
    isPidAlive: () => true,
    probeHealth: async () => "ok" as HealthOutcome,
    ...over,
  };
}

describe("discoverLocalServer", () => {
  it("returns not-found when there is no pidfile", async () => {
    const r = await discoverLocalServer(io({ readPidfile: async () => null }));
    expect(r).toEqual({ found: false, reason: "no-pidfile" });
  });

  it("returns malformed for a structurally invalid pidfile", async () => {
    const r = await discoverLocalServer(io({ readPidfile: async () => "garbage" }));
    expect(r).toEqual({ found: false, reason: "malformed" });
  });

  it("returns dead when the pid is not alive", async () => {
    const r = await discoverLocalServer(
      io({ readPidfile: async () => "4242\n6767", isPidAlive: () => false }),
    );
    expect(r).toEqual({ found: false, reason: "dead" });
  });

  it("returns a found target with its health outcome when alive", async () => {
    const r = await discoverLocalServer(
      io({
        readPidfile: async () => "4242\n6767",
        isPidAlive: () => true,
        probeHealth: async () => "ok",
      }),
    );
    expect(r).toEqual({
      found: true,
      baseUrl: "http://127.0.0.1:6767",
      pid: 4242,
      port: 6767,
      health: "ok",
    });
  });
});

describe("isPidAlive", () => {
  it("reports the current process as alive", () => {
    expect(isPidAlive(process.pid)).toBe(true);
  });

  it("reports an almost-certainly-unused pid as not alive", () => {
    // 0x7fffffff is well above any real pid; process.kill throws ESRCH.
    expect(isPidAlive(2147483647)).toBe(false);
  });
});
