// Unit tests for the modal-host resolution latch in sessionHost.ts.
//
// The modal host_id is the fallback slice key for host-less / cross-host routes
// (session list, /v1/hosts, /v1/sessions/updates). It's the MODAL host over
// every session seen this page, RESOLVED ONCE and then frozen — so a
// continuously-refetching session list can't flap the key. These tests pin the
// idempotent-latch and modal-pick behavior.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  _resetModalHostForTest,
  isModalHostResolved,
  modalHostId,
  resolveModalHost,
  setSessionHost,
} from "./sessionHost";

const noLastHost = (): string | null => null;

beforeEach(() => {
  _resetModalHostForTest();
});

afterEach(() => {
  _resetModalHostForTest();
});

describe("modal host resolution", () => {
  it("is unresolved and null before resolveModalHost runs", () => {
    setSessionHost("c1", "host_a");
    expect(isModalHostResolved()).toBe(false);
    expect(modalHostId()).toBe(null);
  });

  it("resolves to the most common host over seen sessions", () => {
    setSessionHost("c1", "host_a");
    setSessionHost("c2", "host_b");
    setSessionHost("c3", "host_b"); // host_b is modal (2 vs 1)
    resolveModalHost(noLastHost);
    expect(isModalHostResolved()).toBe(true);
    expect(modalHostId()).toBe("host_b");
  });

  it("is IDEMPOTENT — later resolves (after the map shifts) do not move the value", () => {
    setSessionHost("c1", "host_a");
    resolveModalHost(noLastHost);
    expect(modalHostId()).toBe("host_a");

    // The list keeps refetching: many more host_b sessions arrive. A re-resolve
    // must NOT re-pick — the frozen value stays host_a (no flap → no WS re-key).
    setSessionHost("c2", "host_b");
    setSessionHost("c3", "host_b");
    setSessionHost("c4", "host_b");
    resolveModalHost(noLastHost);
    expect(modalHostId()).toBe("host_a");
  });

  it("backstops to the injected last-host when the session map is empty", () => {
    // Zero sessions at first settle (empty list / returning user): fall back to
    // the persisted last-picked host so a key still rides.
    resolveModalHost(() => "host_persisted");
    expect(isModalHostResolved()).toBe(true);
    expect(modalHostId()).toBe("host_persisted");
  });

  it("resolves to null (→ no key) when the map is empty and no last-host", () => {
    resolveModalHost(noLastHost);
    expect(isModalHostResolved()).toBe(true); // resolved, so the /updates gate releases
    expect(modalHostId()).toBe(null); // but there's no host → workspace-id default
  });

  it("prefers a real seen host over the last-host backstop", () => {
    setSessionHost("c1", "host_seen");
    resolveModalHost(() => "host_persisted");
    expect(modalHostId()).toBe("host_seen");
  });

  it("releasing the gate is what matters for zero-session users (resolved, key null)", () => {
    // A user with no sessions must still flip isModalHostResolved() true so the
    // /updates socket starts (unkeyed) instead of hanging forever.
    resolveModalHost(noLastHost);
    expect(isModalHostResolved()).toBe(true);
  });
});
