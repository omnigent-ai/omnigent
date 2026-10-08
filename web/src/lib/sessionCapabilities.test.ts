import { describe, expect, it } from "vitest";
import {
  SANDBOX_FORK_UNSUPPORTED,
  SANDBOX_SWITCH_HOST_UNSUPPORTED,
  sessionActionRestrictions,
} from "./sessionCapabilities";

describe("session action restrictions", () => {
  it("recognizes the managed snapshot without access to its host record", () => {
    expect(sessionActionRestrictions({ labels: { "omnigent.host_type": "managed" } })).toEqual({
      forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
      switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it.each(["arclet", "lakebox"])(
    "recognizes the %s provider when the session has no synthetic label",
    (provider) => {
      expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: provider })).toEqual({
        forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
        switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
      });
    },
  );

  it("preserves forks from supported sandbox providers, including repository labels", () => {
    const result = sessionActionRestrictions(
      { labels: { "omnigent.sandbox.repo": "https://github.com/org/repo" } },
      { sandbox_provider: "modal" },
    );
    expect(result.forkDisabledReason).toBeUndefined();
    expect(result.switchHostDisabledReason).toBe(SANDBOX_SWITCH_HOST_UNSUPPORTED);
  });

  it("honors the embedded restriction even for a normally forkable provider", () => {
    expect(
      sessionActionRestrictions(
        { labels: { "omnigent.host_type": "managed" } },
        { sandbox_provider: "modal" },
      ),
    ).toEqual({
      forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
      switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it("keeps both actions available for ordinary hosts", () => {
    expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: null })).toEqual({
      forkDisabledReason: undefined,
      switchHostDisabledReason: undefined,
    });
  });
});
