import { describe, expect, it } from "vitest";
import {
  ARCLET_FORK_UNSUPPORTED,
  ARCLET_SWITCH_HOST_UNSUPPORTED,
  sessionActionRestrictions,
} from "./sessionCapabilities";

describe("session action restrictions", () => {
  it("recognizes the managed snapshot without access to its host record", () => {
    expect(sessionActionRestrictions({ labels: { "omnigent.host_type": "managed" } })).toEqual({
      forkDisabledReason: ARCLET_FORK_UNSUPPORTED,
      switchHostDisabledReason: ARCLET_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it("recognizes an Arclet host when the sidebar has no synthetic label", () => {
    expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: "arclet" })).toEqual({
      forkDisabledReason: ARCLET_FORK_UNSUPPORTED,
      switchHostDisabledReason: ARCLET_SWITCH_HOST_UNSUPPORTED,
    });
  });

  it("preserves forks from supported sandbox providers, including repository labels", () => {
    const result = sessionActionRestrictions(
      { labels: { "omnigent.sandbox.repo": "https://github.com/org/repo" } },
      { sandbox_provider: "modal" },
    );
    expect(result.forkDisabledReason).toBeUndefined();
    expect(result.switchHostDisabledReason).toContain("managed sandbox");
  });

  it("keeps both actions available for ordinary hosts", () => {
    expect(sessionActionRestrictions({ labels: {} }, { sandbox_provider: null })).toEqual({
      forkDisabledReason: undefined,
      switchHostDisabledReason: undefined,
    });
  });
});
