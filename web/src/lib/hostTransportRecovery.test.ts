import { afterEach, describe, expect, it } from "vitest";
import {
  HOST_TRANSPORT_RETRIES,
  HostUnreachableError,
  clearHostTransportRecovery,
  clearSessionHostTransportRecovery,
  exhaustedHostTransportError,
  hostReadGateOpen,
  hostTransportRetryDelay,
  isHostTransportError,
  mayRetryHostTransport,
  recordHostTransportFailure,
  resetHostTransportRecovery,
  setHostReadGate,
} from "@/lib/hostTransportRecovery";
import { ApiError } from "@/lib/sessionsApi";

class StatusError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`${status}`);
    this.status = status;
  }
}

afterEach(() => resetHostTransportRecovery());

describe("isHostTransportError", () => {
  it("reads gateway answers and an unreachable host as the host not answering", () => {
    for (const status of [502, 503, 504]) {
      expect(isHostTransportError(new StatusError(status)), `${status}`).toBe(true);
      expect(isHostTransportError(new ApiError("x", status, null)), `api ${status}`).toBe(true);
    }
    expect(isHostTransportError(new HostUnreachableError(new TypeError("Failed to fetch")))).toBe(
      true,
    );
  });

  it("leaves answers, sign-in rejections, cancellation and programming errors alone", () => {
    for (const status of [400, 401, 403, 404, 409, 500]) {
      expect(isHostTransportError(new StatusError(status)), `${status}`).toBe(false);
    }
    expect(isHostTransportError(new DOMException("aborted", "AbortError"))).toBe(false);
    expect(isHostTransportError(new TypeError("undefined is not a function"))).toBe(false);
    expect(isHostTransportError(new Error("runner offline"))).toBe(false);
    expect(isHostTransportError({ status: 502 })).toBe(false);
  });
});

describe("a host transport episode", () => {
  it("allows six retries, seven attempts, waiting 1, 2, 4, 8, 15 and 15 seconds", () => {
    expect(HOST_TRANSPORT_RETRIES).toBe(6);
    const delays: number[] = [];
    let attempts = 0;
    const error = new StatusError(502);
    for (;;) {
      attempts += 1;
      const failures = recordHostTransportFailure("read", "s1", error);
      if (!mayRetryHostTransport("read")) break;
      delays.push(hostTransportRetryDelay(failures));
    }
    expect(attempts).toBe(7);
    expect(delays).toEqual([1_000, 2_000, 4_000, 8_000, 15_000, 15_000]);
    expect(exhaustedHostTransportError("read")).toBe(error);
  });

  it("isn't exhausted while retries remain, and a clear starts a fresh one", () => {
    for (let failure = 0; failure < HOST_TRANSPORT_RETRIES; failure++) {
      recordHostTransportFailure("read", "s1", new StatusError(504));
    }
    expect(exhaustedHostTransportError("read")).toBeUndefined();
    expect(mayRetryHostTransport("read")).toBe(true);
    recordHostTransportFailure("read", "s1", new StatusError(504));
    expect(exhaustedHostTransportError("read")).toBeDefined();
    clearHostTransportRecovery("read");
    expect(exhaustedHostTransportError("read")).toBeUndefined();
    expect(mayRetryHostTransport("read")).toBe(false);
  });

  it("clears only the returning session's reads", () => {
    for (let failure = 0; failure <= HOST_TRANSPORT_RETRIES; failure++) {
      recordHostTransportFailure("a/info", "a", new StatusError(502));
      recordHostTransportFailure("a/pr", "a", new StatusError(502));
      recordHostTransportFailure("b/info", "b", new StatusError(502));
    }
    clearSessionHostTransportRecovery("a");
    expect(exhaustedHostTransportError("a/info")).toBeUndefined();
    expect(exhaustedHostTransportError("a/pr")).toBeUndefined();
    expect(exhaustedHostTransportError("b/info")).toBeDefined();
  });
});

describe("the host read gate", () => {
  it("is open until an observer reports the host can't be asked", () => {
    expect(hostReadGateOpen("s1")).toBe(true);
    setHostReadGate("s1", false);
    expect(hostReadGateOpen("s1")).toBe(false);
    expect(hostReadGateOpen("s2")).toBe(true);
    setHostReadGate("s1", true);
    expect(hostReadGateOpen("s1")).toBe(true);
  });
});
