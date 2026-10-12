import { describe, expect, it } from "vitest";

import { serverSupportsCodexPrelaunchPlanMode } from "./codexPlanMode";

describe("serverSupportsCodexPrelaunchPlanMode", () => {
  it.each([
    ["0.18.0", true],
    ["0.18.0.dev0", true],
    ["0.18.3", true],
    ["0.19.0", true],
    ["1.0.0", true],
    ["0.17.9", false],
    ["0.9.0", false],
  ])("judges %s as %s", (version, supported) => {
    expect(serverSupportsCodexPrelaunchPlanMode(version)).toBe(supported);
  });

  it("fails closed when the version is unknown or unparseable", () => {
    expect(serverSupportsCodexPrelaunchPlanMode(null)).toBe(false);
    expect(serverSupportsCodexPrelaunchPlanMode(undefined)).toBe(false);
    expect(serverSupportsCodexPrelaunchPlanMode("dev")).toBe(false);
  });
});
