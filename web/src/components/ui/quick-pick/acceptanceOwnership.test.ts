import { describe, expect, it, vi } from "vitest";

import { createAcceptanceOwnership } from "./acceptanceOwnership";

describe("createAcceptanceOwnership", () => {
  it("runs the action of a close the current acceptance asked for, once", () => {
    const ownership = createAcceptanceOwnership();
    const action = vi.fn();
    const token = ownership.begin();
    ownership.requestClose(token, action);
    expect(ownership.closing()).toBe(true);
    expect(ownership.takeAfterClose()).toBe(action);
    // The close is spent.
    expect(ownership.closing()).toBe(false);
    expect(ownership.takeAfterClose()).toBeNull();
  });

  it("lets a newer acceptance take over from an earlier one", () => {
    const ownership = createAcceptanceOwnership();
    const earlier = ownership.begin();
    const later = ownership.begin();
    expect(ownership.isCurrent(earlier)).toBe(false);
    expect(ownership.isCurrent(later)).toBe(true);
    // The earlier one can no longer ask for a close.
    ownership.requestClose(earlier, vi.fn());
    expect(ownership.closing()).toBe(false);
  });

  it("drops a pending close, and any later request from its acceptance, once revoked", () => {
    const ownership = createAcceptanceOwnership();
    const token = ownership.begin();
    ownership.requestClose(token, vi.fn());
    ownership.revoke();
    expect(ownership.isCurrent(token)).toBe(false);
    expect(ownership.takeAfterClose()).toBeNull();
    ownership.requestClose(token, vi.fn());
    expect(ownership.closing()).toBe(false);
  });

  it("drops a close whose acceptance was superseded before the close finished", () => {
    const ownership = createAcceptanceOwnership();
    const token = ownership.begin();
    ownership.requestClose(token, vi.fn());
    ownership.begin();
    expect(ownership.closing()).toBe(false);
    expect(ownership.takeAfterClose()).toBeNull();
  });
});
